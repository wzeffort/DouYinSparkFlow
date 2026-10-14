"""Loopback HTTP gateway with a durable, bounded cutover request barrier.

Control is a mode-0600 Unix socket, never an HTTP route. Once PostgreSQL has
accepted traffic, the legacy SQLite backend cannot be selected again.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import time

import httpx
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, StreamingResponse

HOP_HEADERS = {b'connection', b'keep-alive', b'proxy-authenticate', b'proxy-authorization',
               b'te', b'trailer', b'transfer-encoding', b'upgrade'}


class Barrier:
    def __init__(self, directory, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'route.json'
        self.clock = clock
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            'phase': 'old', 'authority': 'sqlite', 'fallback': 'old', 'deadline': None}
        self.condition = asyncio.Condition()
        self.active = self.queued = 0

    def persist(self):
        temporary = self.directory/'route.json.tmp'
        with temporary.open('w') as file:
            json.dump(self.state, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self.path)
        if os.name == 'posix':
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def status(self):
        return {**self.state, 'active': self.active, 'queued': self.queued}

    async def enter(self):
        async with self.condition:
            if self.state['phase'] == 'hold':
                self.queued += 1
                try:
                    await self.condition.wait_for(lambda: self.state['phase'] != 'hold')
                finally:
                    self.queued -= 1
            self.active += 1
            return self.state['phase']

    async def leave(self):
        async with self.condition:
            self.active -= 1
            self.condition.notify_all()

    async def freeze(self, seconds=80, drain_seconds=15):
        if not 5 <= seconds <= 90:
            raise ValueError('hold duration outside safety bounds')
        async with self.condition:
            if self.state['phase'] == 'hold':
                raise ValueError('barrier already held')
            self.state.update(fallback=self.state['phase'], phase='hold', deadline=self.clock()+seconds)
            self.persist()
            try:
                await asyncio.wait_for(self.condition.wait_for(lambda: self.active == 0), drain_seconds)
            except BaseException:
                self.state.update(phase=self.state['fallback'], deadline=None)
                self.persist()
                self.condition.notify_all()
                raise
            return self.status()

    async def route(self, target):
        async with self.condition:
            if target not in {'old', 'new', 'dual'}:
                raise ValueError('invalid target')
            if target == 'old' and self.state['authority'] != 'sqlite':
                raise ValueError('legacy SQLite rollback is forbidden after activation')
            if target == 'new' and self.state['authority'] == 'sqlite':
                if self.state['phase'] != 'hold' or self.active or self.clock() >= self.state['deadline']:
                    raise ValueError('activation requires an unexpired, drained barrier')
            if target == 'dual' and self.state['authority'] != 'postgresql':
                raise ValueError('dual routing requires PostgreSQL authority')
            self.state.update(phase=target, fallback=target, deadline=None)
            if target in {'new', 'dual'}:
                self.state['authority'] = 'postgresql'
            self.persist()
            self.condition.notify_all()
            return self.status()

    async def expire(self):
        async with self.condition:
            if self.state['phase'] == 'hold' and self.clock() >= self.state['deadline']:
                self.state.update(phase=self.state['fallback'], deadline=None)
                self.persist()
                self.condition.notify_all()


def build_app(directory, *, transport=None):
    gate = Barrier(directory)

    @asynccontextmanager
    async def lifespan(app):
        async def control(reader, writer):
            try:
                data = json.loads(await asyncio.wait_for(reader.readline(), 3))
                if data['action'] == 'hold':
                    result = await gate.freeze(data.get('seconds', 80))
                elif data['action'] == 'route':
                    result = await gate.route(data['target'])
                elif data['action'] == 'status':
                    result = gate.status()
                else:
                    raise ValueError('unknown control action')
                response = {'ok': True, **result}
            except Exception:
                response = {'ok': False, 'error': 'control action rejected'}
            writer.write(json.dumps(response).encode()+b'\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        socket = Path(directory)/'control.sock'
        if socket.exists():
            socket.unlink()
        server = await asyncio.start_unix_server(control, path=socket)
        os.chmod(socket, 0o600)
        async def watchdog():
            while True:
                await gate.expire()
                await asyncio.sleep(0.25)
        task = asyncio.create_task(watchdog())
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                    timeout=httpx.Timeout(110, connect=5),
                                    limits=httpx.Limits(max_connections=128)) as client:
            app.state.client = client
            yield
        task.cancel()
        server.close()
        await server.wait_closed()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gate = gate

    @app.api_route('/{path:path}', methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'])
    async def proxy(request: Request, path: str):
        # Keep the request body while held, then send it exactly once.
        body = await request.body()
        if len(body) > 8*1024*1024:
            return JSONResponse({'detail':'Request too large'}, status_code=413)
        phase = await gate.enter()
        target = 'http://127.0.0.1:8899' if phase == 'old' else 'http://127.0.0.1:18900'
        if phase == 'dual' and request.method in {'GET','HEAD'} and (path.startswith('static/') or path in {'login','favicon.ico'}):
            target = 'http://127.0.0.1:8900'
        try:
            raw_path = request.scope['raw_path'].decode('ascii')
            query = request.scope['query_string'].decode('ascii')
            url = target+raw_path+('?' + query if query else '')
            headers = [(key,value) for key,value in request.headers.raw if key.lower() not in HOP_HEADERS]
            outgoing = app.state.client.build_request(request.method, url, headers=headers, content=body)
            response = await app.state.client.send(outgoing, stream=True, follow_redirects=False)
        except asyncio.CancelledError:
            await gate.leave()
            raise
        except Exception:
            await gate.leave()
            # Never retry a POST against another backend.
            return JSONResponse({'detail':'Upstream unavailable'}, status_code=502)
        async def content():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await gate.leave()
        result = StreamingResponse(content(), status_code=response.status_code)
        result.raw_headers = [(key,value) for key,value in response.headers.raw if key.lower() not in HOP_HEADERS]
        return result
    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(build_app('/control'), host='127.0.0.1', port=8903, proxy_headers=False, access_log=False)
