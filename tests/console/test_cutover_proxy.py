import asyncio
import json
import httpx
import pytest
from spark_console.cutover_proxy import Barrier, build_app


def test_barrier_drains_old_requests_and_releases_waiters_to_new(tmp_path):
    async def check():
        gate=Barrier(tmp_path)
        assert await gate.enter()=='old'
        hold=asyncio.create_task(gate.freeze())
        await asyncio.sleep(0)
        waiting=asyncio.create_task(gate.enter())
        await asyncio.sleep(0)
        assert not hold.done() and not waiting.done()
        await gate.leave()
        assert (await hold)['active']==0
        await gate.route('new')
        assert await waiting=='new'
        await gate.leave()
        with pytest.raises(ValueError,match='rollback'):
            await gate.route('old')
        assert Barrier(tmp_path).state['authority']=='postgresql'
    asyncio.run(check())


def test_timeout_restores_old_and_rejects_stale_activation(tmp_path):
    async def check():
        now=[100]
        gate=Barrier(tmp_path,clock=lambda:now[0])
        await gate.freeze(seconds=5)
        assert Barrier(tmp_path).state['phase']=='hold'
        now[0]=106
        await gate.expire()
        assert gate.state['phase']=='old'
        with pytest.raises(ValueError,match='activation'):
            await gate.route('new')
    asyncio.run(check())


def test_drain_timeout_does_not_leave_site_held(tmp_path):
    async def check():
        gate=Barrier(tmp_path)
        await gate.enter()
        with pytest.raises(TimeoutError):
            await gate.freeze(drain_seconds=0.01)
        assert gate.state['phase']=='old'
        await gate.leave()
    asyncio.run(check())


def test_proxy_queues_post_and_preserves_body_cookie_and_redirect(tmp_path):
    async def check():
        calls=[]
        def upstream(request):
            calls.append((request.url.port,request.url.raw_path,request.content,request.headers['cookie']))
            return httpx.Response(303,headers=[('Location','/dashboard'),('Set-Cookie','one=1'),('Set-Cookie','two=2')],stream=httpx.ByteStream(b''))
        app=build_app(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            app.state.client=client
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as browser:
                await app.state.gate.freeze()
                pending=asyncio.create_task(browser.post('/login?next=%2Ftasks',content=b'username=test',headers={'cookie':'existing=token'}))
                await asyncio.sleep(0.02)
                assert calls==[]
                await app.state.gate.route('new')
                response=await pending
                assert calls==[(18900,b'/login?next=%2Ftasks',b'username=test','existing=token')]
                assert response.status_code==303
                assert response.headers.get_list('set-cookie')==['one=1','two=2']
                assert app.state.gate.active==0
    asyncio.run(check())


def test_post_upstream_failure_is_never_retried(tmp_path):
    async def check():
        calls=[]
        def upstream(request):
            calls.append(request.url.port)
            raise httpx.ConnectError('offline')
        app=build_app(tmp_path)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            app.state.client=client
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as browser:
                response=await browser.post('/tasks',content=b'one')
                assert response.status_code==502
                assert calls==[8899]
                assert app.state.gate.active==0
    asyncio.run(check())


def test_dual_mode_keeps_database_heavy_requests_on_new_node(tmp_path):
    async def check():
        ports=[]
        def upstream(request):
            ports.append(request.url.port)
            return httpx.Response(200,stream=httpx.ByteStream(b'ok'))
        app=build_app(tmp_path)
        await app.state.gate.freeze()
        await app.state.gate.route('new')
        await app.state.gate.route('dual')
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            app.state.client=client
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as browser:
                for path in ['/static/app.css','/login','/dashboard']:
                    assert (await browser.get(path)).status_code==200
                assert (await browser.post('/login',content=b'x')).status_code==200
        assert ports==[8900,8900,18900,18900]
    asyncio.run(check())
