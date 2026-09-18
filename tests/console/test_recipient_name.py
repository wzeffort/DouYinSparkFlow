import asyncio
from unittest.mock import AsyncMock

import pytest


def test_batch_without_uid_sends_only_after_selected_name_is_current():
    async def check():
        from playwright.async_api import async_playwright
        from spark_console.executor import DouyinExecutor
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
                  <div class="conversationConversationItemwrapper" onclick="show('Alice')"><span class="conversationConversationItemtitle">Alice</span></div>
                  <div class="conversationConversationItemwrapper" onclick="setTimeout(()=>show('Bob'),250)"><span class="conversationConversationItemtitle">Bob</span></div>
                  <div class="RightPanelHeaderconvHeader"><span class="RightPanelHeadertitle" id="name">stale</span></div>
                  <div class="messageEditorimChatEditorContainer"><div contenteditable="true">old draft</div></div>
                  <script>window.sent=[];window.show=name=>document.getElementById('name').textContent=name;
                    document.querySelector('[contenteditable]').onkeydown=e=>{
                      if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();window.sent.push([document.getElementById('name').textContent,e.target.innerText]);let sent=document.createElement('p');sent.innerText=e.target.innerText;document.body.appendChild(sent);e.target.innerText='';}
                    };</script>''')
                for index, (name, message) in enumerate([('Alice','first'),('Bob','second')]):
                    result = await DouyinExecutor()._batch_recipient(page, None,
                        dict(target_name=name, target_sec_uid='', message_template=message), index, AsyncMock(return_value=True))
                    assert result.success and result.stage == 'complete', result
                assert await page.evaluate('window.sent') == [['Alice','first'],['Bob','second']]
                assert len(context.pages) == 1
                assert not any('/user/' in url for url in requests)
                async def changed_during_checkpoint(_):
                    await page.evaluate("show('Bob')")
                    return True
                result = await DouyinExecutor()._batch_recipient(page, None,
                    dict(target_name='Alice', message_template='must not send'), 2, changed_during_checkpoint)
                assert result.error_code == 'recipient_name_unverified'
                assert not result.retryable
                assert await page.evaluate('window.sent') == [['Alice','first'],['Bob','second']]
            finally:
                await browser.close()
    asyncio.run(check())


@pytest.mark.parametrize('actual,expected,allowed', [('Alice','Alice',True),('Alice extra','Alice',False),('Bob','Alice',False),('Alice','',False),('Alice  Bob','Alice Bob',False)])
def test_name_confirmation_is_exact_not_substring(actual, expected, allowed):
    async def check():
        import core.web_chat as chat
        assert hasattr(chat, 'verify_chat_recipient_name'), 'name confirmation is missing'
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.route('**/*', lambda route: route.fulfill(body='<html></html>', content_type='text/html'))
                await page.goto('https://www.douyin.com/chat')
                await page.set_content('<div class="RightPanelHeaderconvHeader"><span class="RightPanelHeadertitle" style="white-space:pre">'+actual+'</span></div><div class="messageEditorimChatEditorContainer"><div contenteditable="true"></div></div>')
                if allowed:
                    await chat.verify_chat_recipient_name(page, expected, timeout=300)
                else:
                    with pytest.raises(chat.RecipientNameError):
                        await chat.verify_chat_recipient_name(page, expected, timeout=300)
            finally:
                await browser.close()
    asyncio.run(check())


def test_duplicate_visible_names_never_click_arbitrary_friend():
    from tests.test_web_chat import FakeWebChatPage
    import core.web_chat as chat
    page = FakeWebChatPage(['Alice','Alice'])
    with pytest.raises(chat.TargetNotFoundError):
        asyncio.run(chat.select_web_chat_target(page,'Alice'))
    assert not any(item.clicked for item in page.items)


def test_name_confirmation_requires_one_visible_header_and_editor():
    async def check():
        from playwright.async_api import async_playwright
        from core.web_chat import verify_chat_recipient_name, RecipientNameError
        header = '<div class="RightPanelHeaderconvHeader"><span class="RightPanelHeadertitle">Alice</span></div>'
        editor = '<div class="messageEditorimChatEditorContainer"><div contenteditable="true"></div></div>'
        bodies = [header + header + editor, header, header + editor + editor,
                  '<div class="RightPanelHeaderconvHeader" style="display:none">Alice</div>' + editor,
                  '<div>Alice</div><div class="RightPanelHeaderconvHeader">Bob</div>' + editor,
                  '<div class="RightPanelHeaderconvHeader"><span class="RightPanelHeadertitle">Bob</span><button>Alice</button></div>' + editor]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.route('**/*',lambda route:route.fulfill(body='<html></html>',content_type='text/html'))
                await page.goto('https://www.douyin.com/chat')
                for body in bodies:
                    await page.set_content(body)
                    with pytest.raises(RecipientNameError):
                        await verify_chat_recipient_name(page,'Alice',timeout=100)
            finally:
                await browser.close()
    asyncio.run(check())


def test_duplicate_global_search_results_are_not_clicked():
    async def check():
        from playwright.async_api import async_playwright
        from core.web_chat import select_web_chat_target, AmbiguousTargetError
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content('<input placeholder="搜索"><button onclick="window.clicked=true">Alice</button><button onclick="window.clicked=true">Alice</button><script>window.clicked=false</script>')
                with pytest.raises(AmbiguousTargetError):
                    await select_web_chat_target(page,'Alice',timeout=300)
                assert await page.evaluate('window.clicked') is False
            finally:
                await browser.close()
    asyncio.run(check())
