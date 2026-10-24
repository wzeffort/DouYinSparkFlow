"""HTTP boundary protections; no database, credential, or browser access."""
import secrets
import logging

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse


class SecurityBoundaryMiddleware:
    def __init__(self, app, secure_cookies=True, max_body=131072):
        self.app, self.secure, self.max_body = app, secure_cookies, max_body

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        nonce = secrets.token_urlsafe(24)
        scope.setdefault('state', {})['csp_nonce'] = nonce
        started = False

        async def protected_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
                headers = MutableHeaders(scope=message)
                headers['X-Frame-Options'] = 'DENY'
                headers['X-Content-Type-Options'] = 'nosniff'
                headers['Referrer-Policy'] = 'same-origin'
                headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
                headers['Content-Security-Policy'] = (
                    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
                    "form-action 'self'; img-src 'self' data: blob:; connect-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; font-src 'self'; "
                    f"script-src 'self' 'nonce-{nonce}'"
                )
                if scope['path'].startswith('/static/'):
                    # Every template reference to /static/ carries a ?v= cache
                    # buster, so a year-long immutable lifetime never serves a
                    # stale asset across releases.
                    headers['Cache-Control'] = 'public, max-age=31536000, immutable'
                elif scope['path'] == '/profile/avatar':
                    headers['Cache-Control'] = 'private, no-store'
                else:
                    headers['Cache-Control'] = 'no-store'
                if self.secure:
                    headers['Strict-Transport-Security'] = 'max-age=31536000'
            await send(message)

        # Only image upload endpoints get room for a 2 MiB file and multipart fields.
        uploads = {'/profile/avatar', '/admin/renewals/codes/wechat', '/admin/renewals/codes/alipay'}
        body_limit = 2 * 1024 * 1024 + 16384 if scope.get('method') == 'POST' and scope['path'] in uploads else self.max_body
        # Count actual streamed bytes; a declared Content-Length cannot bypass this boundary.
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > body_limit:
                return await PlainTextResponse('Request too large', 413)(scope, receive, protected_send)
            body.extend(chunk)
            if not message.get('more_body', False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        try:
            await self.app(scope, replay, protected_send)
        except Exception as error:
            # Do not log request bodies, SQL parameters, or exception text containing secrets.
            logging.getLogger(__name__).error('Unhandled HTTP error: %s', type(error).__name__)
            if started:
                raise
            await PlainTextResponse('Internal server error', 500)(scope, receive, protected_send)
