import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.page_send_evidence import START_JS, CHECK_JS, CLOSE_JS
from spark_console.executor import DouyinExecutor, ExecutionResult
from tests.console.test_recipient_review import setup, client


@pytest.mark.parametrize('receipt,page_ok,expected', [
    ('accepted', True, 'complete'), ('rejected', True, 'rejected'),
    (None, True, 'page_confirmed'), (None, False, 'submitted'),
])
def test_confirmation_precedence_and_exactly_one_send(receipt, page_ok, expected):
    async def run():
        editor = SimpleNamespace(press=AsyncMock())
        result = {'status': receipt} if receipt else None
        monitor = SimpleNamespace(start=Mock(), wait=AsyncMock(return_value=result), result=result,
                                  requests=[object()], pending=set(), close=AsyncMock())
        screen = SimpleNamespace(start=AsyncMock(), armed=page_ok, confirmed=AsyncMock(return_value=page_ok), close=AsyncMock())
        with patch('spark_console.executor.SendMonitor', return_value=monitor), \
             patch('spark_console.executor.PageSendEvidence', return_value=screen), \
             patch('spark_console.executor.verify_conversation', AsyncMock()):
            value = await DouyinExecutor()._send_and_confirm(object(), editor, 'text', {'conv_id':'c'})
        assert value.stage == expected
        assert not value.retryable
        editor.press.assert_awaited_once_with('Enter')
        screen.close.assert_awaited_once()
        monitor.close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['page', 'identity'])
def test_late_rejection_overrides_page_evidence(phase):
    async def run():
        monitor = SimpleNamespace(start=Mock(), wait=AsyncMock(return_value=None), result=None,
                                  requests=[object()], pending=set(), close=AsyncMock())
        async def confirmed():
            if phase == 'page':
                monitor.result = {'status':'rejected', 'code':8}
            return True
        async def verify(*_args):
            if phase == 'identity':
                monitor.result = {'status':'rejected', 'code':8}
        screen = SimpleNamespace(start=AsyncMock(), armed=True, confirmed=confirmed, close=AsyncMock())
        with patch('spark_console.executor.SendMonitor', return_value=monitor), patch('spark_console.executor.PageSendEvidence', return_value=screen), patch('spark_console.executor.verify_conversation', verify):
            result = await DouyinExecutor()._send_and_confirm(object(), SimpleNamespace(press=AsyncMock()), 'text', {'conv_id':'c'})
        assert result.stage == 'rejected'
        assert not result.success and not result.retryable
    asyncio.run(run())


def test_enter_exception_never_retries_or_accepts_page():
    async def run():
        screen = SimpleNamespace(start=AsyncMock(), armed=True, confirmed=AsyncMock(return_value=True), close=AsyncMock())
        editor = SimpleNamespace(press=AsyncMock(side_effect=TimeoutError))
        with patch('spark_console.executor.PageSendEvidence', return_value=screen):
            result = await DouyinExecutor()._send_and_confirm(object(), editor, 'text')
        assert result.error_code == 'delivery_uncertain' and not result.retryable
        editor.press.assert_awaited_once()
        screen.confirmed.assert_not_called()
    asyncio.run(run())


def test_broken_monitor_still_allows_page_fallback_without_resending():
    async def run():
        monitor = SimpleNamespace(start=Mock(), wait=AsyncMock(side_effect=TimeoutError), result=None,
                                  requests=[], pending=set(), close=AsyncMock())
        screen = SimpleNamespace(start=AsyncMock(), armed=True, confirmed=AsyncMock(return_value=True), close=AsyncMock())
        editor = SimpleNamespace(press=AsyncMock())
        with patch('spark_console.executor.SendMonitor', return_value=monitor), \
             patch('spark_console.executor.PageSendEvidence', return_value=screen), \
             patch('spark_console.executor.verify_conversation', AsyncMock()):
            result = await DouyinExecutor()._send_and_confirm(object(), editor, 'text', {'conv_id':'c'})
        assert result.stage == 'page_confirmed'
        editor.press.assert_awaited_once_with('Enter')
    asyncio.run(run())


def test_ambiguous_requests_are_never_reported_success():
    async def run():
        monitor = SimpleNamespace(start=Mock(), wait=AsyncMock(return_value={'status':'accepted'}), result={'status':'accepted'},
                                  requests=[object(), object()], pending=set(), close=AsyncMock())
        screen = SimpleNamespace(start=AsyncMock(), armed=True, confirmed=AsyncMock(return_value=True), close=AsyncMock())
        with patch('spark_console.executor.SendMonitor', return_value=monitor), patch('spark_console.executor.PageSendEvidence', return_value=screen):
            result = await DouyinExecutor()._send_and_confirm(object(), SimpleNamespace(press=AsyncMock()), 'text', {'conv_id':'c'})
        assert result.stage == 'submitted' and not result.retryable
    asyncio.run(run())


def test_legacy_execute_uses_shared_confirmation():
    from tests.console.test_credentials import _FakeBrowser, _FakePlaywrightManager
    async def run():
        browser = _FakeBrowser()
        editor = SimpleNamespace(fill=AsyncMock(), type=AsyncMock(), press=AsyncMock())
        page = SimpleNamespace(goto=AsyncMock(), wait_for_selector=AsyncMock(),
                               locator=Mock(return_value=SimpleNamespace(first=editor)))
        browser.context.new_page = AsyncMock(return_value=page)
        executor = DouyinExecutor()
        executor._send_and_confirm = AsyncMock(return_value=ExecutionResult(True, 'page_confirmed'))
        with patch('playwright.async_api.async_playwright', return_value=_FakePlaywrightManager(browser)), \
             patch('spark_console.executor.wait_for_chat_ready', AsyncMock()), \
             patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
             patch('spark_console.executor.select_web_chat_target', AsyncMock(return_value='friend')), \
             patch('spark_console.executor.verify_chat_recipient_name', AsyncMock()), \
             patch('spark_console.executor.current_identity', AsyncMock(return_value={'conv_id':'c'})):
            result = await executor.execute(json.dumps([{'name':'sid','value':'fixture','domain':'.douyin.com','path':'/'}]).encode(), 'friend', 'text')
        assert result.stage == 'page_confirmed'
        executor._send_and_confirm.assert_awaited_once_with(page, editor, 'text', {'conv_id':'c'})
        editor.fill.assert_awaited_once_with('')
        editor.press.assert_not_awaited()  # Only the shared helper can submit.
    asyncio.run(run())


def test_page_evidence_offline_browser():
    from playwright.sync_api import sync_playwright
    html = '''<div data-e2e="conversation-item" class="conversationConversationItemcurConversation">friend</div>
        <div class="RightPanelHeaderconvHeader">friend</div>
        <div id="messages"><div class="MessageBoxContentisFromMe"><div data-e2e="msg-item-content">same text</div></div></div>
        <div contenteditable="true">same text</div>'''
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.abort())
        for case in ['new', 'old_only', 'incoming', 'different', 'switched', 'pending', 'failed', 'prepend', 'rerender', 'editor_busy']:
            page.set_content(html)
            assert page.evaluate(START_JS, 'same text') is True
            assert not page.evaluate(CHECK_JS)
            page.evaluate('''kind => {
              const messages=document.querySelector('#messages');
              if(kind!=='editor_busy') document.querySelector('[contenteditable]').textContent='';
              if(kind==='old_only') return;
              const wrapper=document.createElement('div');
              wrapper.className=kind==='incoming'?'incoming':'MessageBoxContentisFromMe';
              if(kind==='pending') wrapper.setAttribute('aria-busy','true');
              if(kind==='failed') wrapper.setAttribute('data-status','failed');
              const bubble=document.createElement('div'); bubble.dataset.e2e='msg-item-content';
              bubble.textContent=kind==='different'?'other text':'same text'; wrapper.append(bubble);
              if(kind==='prepend') messages.prepend(wrapper); else messages.append(wrapper);
              if(kind==='switched') document.querySelector('[data-e2e="conversation-item"]').className='';
              if(kind==='rerender') messages.innerHTML=messages.innerHTML;
            }''', case)
            assert not page.evaluate(CHECK_JS)
            page.wait_for_timeout(1100)
            assert page.evaluate(CHECK_JS) == (case == 'new'), case
            page.evaluate(CLOSE_JS)
        browser.close()


def test_page_confirmed_is_not_labelled_server_accepted(client, setup):
    db, _, _, _, _, _, _, run, _ = setup
    from sqlalchemy import select
    from spark_console.models import TaskRunRecipient
    rows = db.scalars(select(TaskRunRecipient).where(TaskRunRecipient.run_id == run.id)).all()
    run.status = 'success'
    for row in rows:
        row.status, row.stage = 'success', 'page_confirmed'
    db.commit()
    response = client.get('/runs')
    assert response.status_code == 200
    assert '成功（页面确认）' in response.text
    assert '服务端已接受' not in response.text
