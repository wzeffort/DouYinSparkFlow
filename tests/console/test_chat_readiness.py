import asyncio
import pytest


@pytest.mark.parametrize('body,expected', [
    ('<div>blank app</div>', 'chat_ui_unavailable'),
    ('<button>扫码登录</button>', 'login_expired'),
    ('<script>setTimeout(()=>document.body.innerHTML=\'<div class="conversationConversationItemwrapper">friend</div>\',100)</script>', None),
    ('<button>扫码登录</button><script>setTimeout(()=>document.body.innerHTML=\'<div class="conversationConversationItemwrapper">friend</div>\',100)</script>', None),
])
def test_readiness_distinguishes_blank_login_and_delayed_list(body, expected):
    async def check():
        import core.web_chat as chat
        assert hasattr(chat, 'wait_for_chat_ready'), 'missing chat readiness gate'
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(body)
                if expected:
                    with pytest.raises(chat.ChatReadinessError) as failure:
                        await chat.wait_for_chat_ready(page, timeout=500)
                    assert failure.value.code == expected
                else:
                    await chat.wait_for_chat_ready(page, timeout=2000)
            finally:
                await browser.close()
    asyncio.run(check())
