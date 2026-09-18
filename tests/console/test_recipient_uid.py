import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from spark_console.executor import DouyinExecutor


def test_batch_without_current_name_evidence_cannot_type_or_send():
    editor = SimpleNamespace(type=AsyncMock(), fill=AsyncMock(), press=AsyncMock())
    page = SimpleNamespace(wait_for_selector=AsyncMock(),
        locator=lambda *a: SimpleNamespace(first=editor, count=AsyncMock(return_value=0)))
    with patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock()), \
         patch('core.tasks.confirm_message_sent', AsyncMock()):
        result = asyncio.run(DouyinExecutor()._batch_recipient(page, None,
            dict(target_name='same name', message_template='must not send'), 0, AsyncMock(return_value=True)))
    assert result.success is False
    assert result.error_code == 'recipient_name_unverified'
    assert not result.retryable
    editor.type.assert_not_awaited()
    editor.press.assert_not_awaited()


@pytest.mark.parametrize('expected,actual,headers,allowed', [
    ('uid-A', 'uid-A', 1, True),
    ('uid-A', 'uid-B', 1, False),
    ('', 'uid-A', 1, False),
    ('uid-A', 'uid-A', 0, False),
    ('uid-A', 'uid-A', 2, False),
])
def test_profile_from_current_chat_header_is_required(expected, actual, headers, allowed):
    async def check():
        from playwright.async_api import async_playwright
        from core.web_chat import verify_chat_recipient_uid, RecipientIdentityError, UserInfoCollector, DouyinUserIdentity
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                opened = []
                context.on('page', lambda p: opened.append(p))
                await context.route('**/*', lambda route: route.fulfill(status=200, body='<html>fixture</html>', content_type='text/html'))
                page = await context.new_page()
                await page.goto('https://www.douyin.com/chat')
                await page.set_content(''.join(
                    '<div class="RightPanelHeaderconvHeader"><button data-apm-action="个人页卡片" '
                    f'onclick="window.open(\'https://www.douyin.com/user/{actual}?from_tab_name=main\')">same name</button></div>'
                    for _ in range(headers)) + '<div class="messageEditorimChatEditorContainer" style="width:100px;height:30px"></div>')
                identities = UserInfoCollector()
                identities.identities[expected] = DouyinUserIdentity(expected)
                if allowed:
                    await verify_chat_recipient_uid(page, expected, identities)
                else:
                    with pytest.raises(RecipientIdentityError):
                        await verify_chat_recipient_uid(page, expected, identities)
                assert len(context.pages) == 1
                assert len(opened) == 1
            finally:
                await browser.close()
    asyncio.run(check())


@pytest.mark.parametrize('failure_at', [1, 2])
@pytest.mark.parametrize('batch', [True, False])
def test_name_failure_prevents_send_in_both_executors(batch, failure_at):
    from core.web_chat import RecipientNameError
    from tests.console.test_credentials import _FakeSendingPage, _FakeBrowser, _FakePlaywrightManager
    from types import ModuleType
    page = _FakeSendingPage()
    page.editor.fill = AsyncMock()
    page.editor.element_handle = AsyncMock(return_value=page.editor)
    page.locator = lambda *a: SimpleNamespace(first=page.editor, count=AsyncMock(return_value=0))
    browser = _FakeBrowser()
    browser.context.new_page = AsyncMock(return_value=page)
    api = ModuleType('playwright.async_api')
    api.async_playwright = lambda: _FakePlaywrightManager(browser)
    verdicts = [None] * (failure_at - 1) + [RecipientNameError()]
    with patch('spark_console.executor.verify_chat_recipient_name', AsyncMock(side_effect=verdicts)), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock()), \
         patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
         patch.dict('sys.modules', {'playwright.async_api': api}):
        if batch:
            result = asyncio.run(DouyinExecutor()._batch_recipient(page, None,
                dict(target_name='same name', message_template='draft only'), 0, AsyncMock(return_value=True)))
        else:
            result = asyncio.run(DouyinExecutor().execute(
                b'[{"name":"fixture","value":"test","domain":".douyin.com","path":"/"}]', 'same name', 'draft only'))
    assert result.error_code == 'recipient_name_unverified'
    assert not result.success and not result.retryable
    assert 'Enter' not in page.editor.pressed


def test_real_nested_editor_switches_two_friends_without_profile_requests():
    async def check():
        from playwright.async_api import async_playwright
        from core.web_chat import UserInfoCollector, DouyinUserIdentity, verify_chat_recipient_uid, RecipientIdentityError
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                context = await browser.new_context()
                requests = []
                async def route(request):
                    requests.append(request.request.url)
                    await request.fulfill(status=200, body='<html></html>', content_type='text/html')
                await context.route('**/*', route)
                page = await context.new_page()
                await page.goto('https://www.douyin.com/chat')
                await page.set_content('''
                  <div class="conversationConversationItemwrapper" onclick="window.currentUid='A';document.getElementById('name').textContent='Alice'"><span class="conversationConversationItemtitle">Alice</span></div>
                  <div class="conversationConversationItemwrapper" onclick="setTimeout(()=>{window.currentUid='B';document.getElementById('name').textContent='Bob'},250)"><span class="conversationConversationItemtitle">Bob</span></div>
                  <div class="RightPanelHeaderconvHeader"><span class="RightPanelHeadertitle" id="name">stale</span><button data-apm-action="个人页卡片" onclick="window.open('/user/'+window.currentUid)">profile</button></div>
                  <div class="messageEditorimChatEditorContainer"><div contenteditable="true">old draft</div></div>
                  <script>window.currentUid='stale';window.sent=[];
                    document.querySelector('[contenteditable]').onkeydown=e=>{
                      if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();window.sent.push([window.currentUid,e.target.innerText]);let sent=document.createElement('p');sent.innerText=e.target.innerText;document.body.appendChild(sent);e.target.innerText='';}
                    };</script>''')
                identities = UserInfoCollector()
                for uid in ('A', 'B'):
                    identities.identities[uid] = DouyinUserIdentity(uid)
                for index, (uid, name, message) in enumerate([('A','Alice','first'),('B','Bob','second')]):
                    result = await DouyinExecutor()._batch_recipient(page, identities,
                        dict(target_name=name, target_sec_uid=uid, message_template=message), index, AsyncMock(return_value=True))
                    assert result.success and result.stage == 'complete', result
                assert await page.evaluate('window.sent') == [['A','first'],['B','second']]
                assert len(context.pages) == 1
                assert not any('/user/' in url for url in requests)
                # A conversation switch during a durable checkpoint cannot send.
                async def changed_during_checkpoint(_):
                    await page.evaluate("window.currentUid='B';document.getElementById('name').textContent='Bob'")
                    return True
                with patch('core.tasks.confirm_message_sent', AsyncMock()):
                    result = await DouyinExecutor()._batch_recipient(page, identities,
                        dict(target_name='Alice', target_sec_uid='A', message_template='must not send'),
                        2, changed_during_checkpoint)
                assert result.error_code == 'recipient_name_unverified'
                assert not result.retryable
                assert await page.evaluate('window.sent') == [['A','first'],['B','second']]
                # A matching header alone is insufficient without API evidence.
                with pytest.raises(RecipientIdentityError):
                    await verify_chat_recipient_uid(page, 'B', UserInfoCollector())
                # Previous successful proof must not survive a changed target.
                with pytest.raises(RecipientIdentityError):
                    await verify_chat_recipient_uid(page, 'A', identities)
            finally:
                await browser.close()
    asyncio.run(check())
