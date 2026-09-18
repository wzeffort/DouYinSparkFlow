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
                if not scope['path'].startswith('/static/'):
                    headers['Cache-Control'] = 'no-store'
                if self.secure:
                    headers['Strict-Transport-Security'] = 'max-age=31536000'
            await send(message)

        # No uploads in this console. Count actual streamed bytes as well as declared sizes.
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            if len(body) + len(chunk) > self.max_body:
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
