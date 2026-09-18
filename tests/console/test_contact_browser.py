"""Offline Chromium checks: all console requests terminate in the fixture client."""
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
from sqlalchemy import select
from spark_console.db import session_scope
from spark_console.models import DouyinContactIdentity, DouyinConversation
from tests.console import test_batch_web as fixture_module
import asyncio


def test_alias_search_more_than_100_and_manual_name(tmp_path):
    fixture=fixture_module.BatchWebTests(); fixture.setUp()
    try:
        account,_=fixture.prepare()
        with session_scope(fixture.engine) as db:
            for i in range(105):
                db.add(DouyinContactIdentity(account_id=account,sec_uid=f'uid-{i}',nickname=f'Friend{i:03}'))
            db.add(DouyinContactIdentity(account_id=account,sec_uid='alias',nickname='依依妖妖',remark_name='朋友备注'))
            db.add(DouyinConversation(account_id=account,display_name='仅名称会话'))
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                def serve(route):
                    request=route.request; url=urlsplit(request.url)
                    if url.netloc!='testserver':
                        route.abort();return
                    response=fixture.client.request(request.method,url.path+('?' + url.query if url.query else ''),content=request.post_data_buffer,
                        headers={'content-type':request.headers.get('content-type','')},follow_redirects=True)
                    route.fulfill(status=response.status_code,headers={k:v for k,v in response.headers.items() if k not in {'content-length','content-encoding'}},body=response.content)
                page.route('**/*',serve)
                page.goto('https://testserver/tasks')
                page.locator('[data-friend-options] button').first.wait_for()
                assert page.locator('details.batch-friend-group').get_attribute('open') is None
                page.get_by_role('button',name='加载更多（已显示 50/106）',exact=True).click()
                page.get_by_role('button',name='加载更多（已显示 100/106）',exact=True).click()
                assert page.get_by_role('button',name='＋ Friend104',exact=True).count()==1
                page.locator('[data-friend-search]').fill('依依妖妖')
                page.get_by_role('button',name='＋ 朋友备注',exact=True).wait_for(timeout=2000)
                page.get_by_role('button',name='＋ 朋友备注',exact=True).click()
                assert page.locator('.batch-recipient-card input').first.input_value()=='朋友备注'
                page.locator('.batch-recipient-card textarea').first.fill('保留专属消息')
                page.locator('[data-friend-search]').fill('Friend104')
                page.get_by_role('button',name='＋ Friend104',exact=True).click()
                page.locator('[data-friend-search]').fill('仅名称会话')
                page.get_by_role('button',name='＋ 仅名称会话',exact=True).click()
                page.locator('[data-friend-search]').fill('尚未同步的新好友')
                page.locator('[data-add-searched-name]').click()
                assert '尚未同步的新好友' in page.locator('.batch-recipient-card input').evaluate_all('(els)=>els.map(e=>e.value)')
                assert page.locator('.batch-recipient-card textarea').first.input_value()=='保留专属消息'
                page.locator('[data-refresh-friends]').click()
                expect(page.locator('[data-friend-status]')).to_contain_text('等待')
                assert page.locator('.batch-recipient-card textarea').first.input_value()=='保留专属消息'
                page.set_viewport_size({'width':390,'height':844})
                expect(page.locator('.sidebar')).not_to_be_in_viewport()
                page.screenshot(path=str(tmp_path/'contacts-mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            finally:
                browser.close()
    finally:
        fixture.tearDown()


def test_readonly_collector_keeps_initial_and_virtualized_rows():
    from playwright.async_api import async_playwright
    from core.web_chat import UserInfoCollector
    from spark_console.contact_sync import collect_page, SyncCancelled
    async def exercise():
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route('**/*',lambda route:route.abort())
                await page.set_content('''<div id="list" style="overflow:auto;height:60px"><div style="height:250px">
                <div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">首屏好友</span></div>
                <div class="conversationConversationItemwrapper"><span class="conversationConversationItemtitle">第二位</span></div>
                </div></div><script>document.querySelector('#list').addEventListener('scroll',()=>{
                  document.querySelector('.conversationConversationItemtitle').textContent='滚动后好友';
                });</script>''')
                info=UserInfoCollector()
                names,_=await collect_page(page,info,lambda:False,step_seconds=.02,max_scrolls=10)
                assert {'首屏好友','第二位','滚动后好友'} <= set(names)
                try:
                    await collect_page(page,info,lambda:True,step_seconds=.02)
                except SyncCancelled:
                    pass
                else:
                    raise AssertionError('cancelled collection must stop')
                assert await page.locator('input,textarea,[contenteditable=true]').count()==0
            finally:
                await browser.close()
    asyncio.run(exercise())


def test_failed_reload_after_sync_can_retry_without_losing_draft():
    fixture=fixture_module.BatchWebTests();fixture.setUp()
    try:
        account,_=fixture.prepare()
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page()
                completed=False
                def serve(route):
                    nonlocal completed
                    request=route.request;url=urlsplit(request.url)
                    if url.netloc!='testserver': route.abort();return
                    if request.method=='POST' and url.path.endswith('/contact-sync'):
                        completed=True
                        route.fulfill(status=202,json={'status':'queued','request_id':'fixture'});return
                    if completed and url.path.endswith('/contact-sync'):
                        route.fulfill(status=200,json={'status':'partial','request_id':'fixture'});return
                    if completed and url.path.endswith('/conversations'):
                        route.fulfill(status=503,json={'message':'temporary fixture failure'});return
                    response=fixture.client.request(request.method,url.path,content=request.post_data_buffer,
                        headers={'content-type':request.headers.get('content-type','')})
                    route.fulfill(status=response.status_code,headers={k:v for k,v in response.headers.items() if k not in {'content-length','content-encoding'}},body=response.content)
                page.route('**/*',serve)
                page.goto('https://testserver/tasks')
                expect(page.locator('[data-friend-status]')).to_contain_text('绑定时')
                page.locator('.batch-recipient-card textarea').first.fill('尚未提交的内容')
                page.locator('[data-refresh-friends]').click()
                expect(page.locator('[data-friend-status]')).to_contain_text('读取失败')
                assert page.locator('[data-refresh-friends]').is_enabled()
                assert page.locator('.batch-recipient-card textarea').first.input_value()=='尚未提交的内容'
            finally:
                browser.close()
    finally:
        fixture.tearDown()
