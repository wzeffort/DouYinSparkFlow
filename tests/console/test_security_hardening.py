import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import asyncio
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.console import test_web_user as fixtures
from spark_console.db import session_scope
from spark_console.models import User
from spark_console.web.app import create_app
from spark_console.rate_limit import BoundedRequestLimiter
from spark_console.security import PasswordService
from spark_console.web.security_headers import SecurityBoundaryMiddleware


class SecurityHardeningTests(unittest.TestCase):
    setUp = fixtures.UserWebTests.setUp
    tearDown = fixtures.UserWebTests.tearDown
    login = fixtures.UserWebTests.login

    def test_failed_login_lock_survives_application_recreation(self):
        for _ in range(10):
            self.client.post('/login', data={'username': 'friend', 'password': 'wrong'})
        with TestClient(create_app(self.settings, self.engine), base_url='https://testserver') as client:
            result = client.post('/login', data={'username': 'friend', 'password': 'Temporary-123!'}, follow_redirects=False)
        self.assertEqual(429, result.status_code)
        self.assertIn('retry-after', result.headers)

    def test_expired_account_lock_allows_correct_login(self):
        with session_scope(self.engine) as db:
            user = db.scalar(select(User).where(User.username == 'friend'))
            user.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            user.failed_login_count = 10
        self.assertEqual(303, self.login().status_code)
        with session_scope(self.engine) as db:
            user = db.scalar(select(User).where(User.username == 'friend'))
            self.assertIsNone(user.locked_until)
            self.assertEqual(0, user.failed_login_count)

    def test_password_change_revokes_other_session_and_rotates_current(self):
        self.login()
        old_token = self.client.cookies.get('spark_session')
        with TestClient(self.client.app, base_url='https://testserver') as other:
            other.post('/login', data={'username': 'friend', 'password': 'Temporary-123!'})
            html = self.client.get('/change-password').text
            csrf = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
            result = self.client.post('/change-password', data={
                'csrf_token': csrf, 'current_password': 'Temporary-123!',
                'new_password': 'Replacement-123!', 'new_password_confirmation': 'Replacement-123!',
            }, follow_redirects=False)
            self.assertEqual(303, result.status_code)
            self.assertNotEqual(old_token, self.client.cookies.get('spark_session'))
            self.assertEqual(200, self.client.get('/dashboard', follow_redirects=False).status_code)
            result = other.get('/dashboard', follow_redirects=False)
            self.assertEqual('/login', result.headers.get('location'))

    def test_security_headers_on_pages_and_errors(self):
        for path in ['/login', '/missing-security-test']:
            result = self.client.get(path)
            self.assertEqual('DENY', result.headers.get('x-frame-options'))
            self.assertEqual('nosniff', result.headers.get('x-content-type-options'))
            self.assertEqual('no-store', result.headers.get('cache-control'))
            csp = result.headers.get('content-security-policy', '')
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("object-src 'none'", csp)
            self.assertIn("form-action 'self'", csp)
            script_policy = next((s for s in csp.split(';') if s.strip().startswith('script-src ')), '')
            self.assertNotIn("'unsafe-inline'", script_policy)
            self.assertNotIn("'unsafe-hashes'", script_policy)
            self.assertIn("'nonce-", script_policy)
            self.assertIn('max-age=', result.headers.get('strict-transport-security', ''))

    def test_large_request_is_rejected_before_form_parsing(self):
        result = self.client.post('/login', content=b'x' * 140000, headers={'content-type': 'application/x-www-form-urlencoded'})
        self.assertEqual(413, result.status_code)

    def test_chunked_large_request_is_also_rejected(self):
        result = self.client.post('/login', content=iter([b'x' * 70000, b'y' * 70000]), headers={'content-type': 'application/x-www-form-urlencoded'})
        self.assertEqual(413, result.status_code)

    def test_malformed_session_is_rejected_without_server_error(self):
        self.client.cookies.set('spark_session', 'x' * 1000)
        result = self.client.get('/dashboard', follow_redirects=False)
        self.assertEqual('/login', result.headers.get('location'))

    def test_non_ascii_csrf_is_rejected_without_server_error(self):
        self.login()
        result = self.client.post('/logout', data={'csrf_token': '恶意输入'})
        self.assertEqual(403, result.status_code)

    def test_sql_injection_username_does_not_authenticate_or_change_users(self):
        result = self.client.post('/login', data={'username': "friend' OR 1=1 --", 'password': 'arbitrary'}, follow_redirects=False)
        self.assertEqual(400, result.status_code)
        self.assertNotIn('spark_session', self.client.cookies)
        with session_scope(self.engine) as db:
            self.assertEqual(['friend'], list(db.scalars(select(User.username))))

    def test_unknown_username_spray_is_limited(self):
        statuses = [self.client.post('/login', data={'username': f'unknown{i}', 'password': 'wrong'}).status_code for i in range(35)]
        self.assertIn(429, statuses)

    def test_blocked_ip_does_not_exhaust_budget_for_other_clients(self):
        for _ in range(60):
            self.client.post('/login', data={'username': 'missing', 'password': 'wrong'}, headers={'x-real-ip': '192.0.2.1'})
        result = self.client.post('/login', data={'username': 'friend', 'password': 'Temporary-123!'}, headers={'x-real-ip': '192.0.2.2'}, follow_redirects=False)
        self.assertEqual(303, result.status_code)

    def test_inflight_old_password_login_cannot_survive_password_change(self):
        self.login()
        html = self.client.get('/change-password').text
        csrf = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        entered, release, change_started = Event(), Event(), Event()
        original = PasswordService.verify

        def paused(service, encoded, password):
            valid = original(service, encoded, password)
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test verification barrier timed out')
            return valid

        with TestClient(self.client.app, base_url='https://testserver') as other:
            with patch.object(PasswordService, 'verify', paused), ThreadPoolExecutor(max_workers=2) as pool:
                login_future = pool.submit(other.post, '/login', data={'username': 'friend', 'password': 'Temporary-123!'}, follow_redirects=False)
                self.assertTrue(entered.wait(5))

                def change():
                    change_started.set()
                    return self.client.post('/change-password', data={'csrf_token': csrf, 'current_password': 'Temporary-123!', 'new_password': 'Replacement-123!', 'new_password_confirmation': 'Replacement-123!'}, follow_redirects=False)

                change_future = pool.submit(change)
                self.assertTrue(change_started.wait(5))
                try:
                    # Before the fix, password change finishes while login is paused.
                    from concurrent.futures import TimeoutError
                    try:
                        change_future.result(timeout=1)
                    except TimeoutError:
                        pass
                finally:
                    release.set()
                login_future.result(timeout=5)
                self.assertEqual(303, change_future.result(timeout=5).status_code)
            self.assertEqual('/login', other.get('/dashboard', follow_redirects=False).headers.get('location'))

    def test_database_keys_and_backups_are_not_web_files(self):
        for path in ['/spark.db', '/data/spark.db', '/backups/spark.db', '/secrets/cookie.key', '/.env.console', '/static/../config.py']:
            self.assertEqual(404, self.client.get(path).status_code)

    def test_cookie_flags_and_no_hsts_in_explicit_http_development_mode(self):
        result = self.login()
        cookie = result.headers['set-cookie'].lower()
        for flag in ['httponly', 'secure', 'samesite=strict']:
            self.assertIn(flag, cookie)
        from dataclasses import replace
        app = create_app(replace(self.settings, secure_cookies=False), self.engine)
        with TestClient(app, base_url='http://testserver') as client:
            self.assertNotIn('strict-transport-security', client.get('/login').headers)


class AdmissionTests(unittest.TestCase):
    def test_real_asgi_chunks_enforce_boundary_before_application(self):
        async def exercise(chunks):
            invoked, output = [], []

            async def app(scope, receive, send):
                invoked.append((await receive())['body'])
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'ok'})

            messages = [{'type': 'http.request', 'body': c, 'more_body': i < len(chunks) - 1} for i, c in enumerate(chunks)]
            async def receive():
                return messages.pop(0) if messages else {'type': 'http.disconnect'}
            async def send(message):
                output.append(message)
            await SecurityBoundaryMiddleware(app)({'type': 'http', 'path': '/login', 'headers': []}, receive, send)
            return invoked, output

        invoked, output = asyncio.run(exercise([b'a' * 65536, b'b' * 65536]))
        self.assertEqual(131072, len(invoked[0]))
        invoked, output = asyncio.run(exercise([b'a' * 65536, b'b' * 65536, b'c']))
        self.assertFalse(invoked)
        self.assertEqual(413, output[0]['status'])

    def test_unhandled_error_is_generic_and_not_cacheable(self):
        case = fixtures.UserWebTests()
        case.setUp()
        try:
            @case.client.app.get('/security-test-error')
            def crash():
                raise RuntimeError('private diagnostic must not be returned')
            with TestClient(case.client.app, raise_server_exceptions=False) as client:
                result = client.get('/security-test-error')
            self.assertEqual(500, result.status_code)
            self.assertEqual('no-store', result.headers.get('cache-control'))
            self.assertNotIn('private diagnostic', result.text)
        finally:
            case.tearDown()

    def test_expiry_and_full_key_space_fail_closed(self):
        now = [0]
        limiter = BoundedRequestLimiter(limit=2, window_seconds=10, max_keys=1, now=lambda: now[0])
        self.assertTrue(limiter.allow('a'))
        self.assertTrue(limiter.allow('a'))
        self.assertFalse(limiter.allow('a'))
        self.assertFalse(limiter.allow('b'))
        now[0] = 10
        self.assertTrue(limiter.allow('b'))

    def test_concurrent_calls_cannot_exceed_limit(self):
        limiter = BoundedRequestLimiter(limit=3)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: limiter.allow('same'), range(20)))
        self.assertEqual(3, sum(results))
