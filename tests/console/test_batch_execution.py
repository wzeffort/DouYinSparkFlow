import asyncio
import pytest
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import AsyncMock, patch

from spark_console.executor import DouyinExecutor


@pytest.mark.parametrize('code,retryable', [('chat_ui_unavailable', True), ('login_expired', False)])
def test_unready_chat_records_every_recipient_without_selection_or_send(code, retryable):
    from core.web_chat import ChatReadinessError
    page = SimpleNamespace(goto=AsyncMock(), on=lambda *a: None)
    context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    manager = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    class Playwright:
        async def __aenter__(self): return manager
        async def __aexit__(self, *args): pass
    api = ModuleType('playwright.async_api')
    api.async_playwright = Playwright
    before, recorded = AsyncMock(), AsyncMock()
    executor = DouyinExecutor()
    with patch.dict(sys.modules, {'playwright.async_api': api}), \
         patch('spark_console.executor.wait_for_chat_ready', AsyncMock(side_effect=ChatReadinessError(code))), \
         patch.object(executor, '_batch_recipient', AsyncMock()) as select:
        outcomes = asyncio.run(executor.execute_batch(
            b'[{"name":"fixture","value":"test","domain":".douyin.com","path":"/"}]',
            [dict(target_name=name, message_template='unused') for name in ('A','B','C')],
            before_send=before, on_result=recorded))
    assert len(outcomes) == 3
    assert all(not r.success and r.error_code == code and r.retryable == retryable for r in outcomes)
    assert [call.args[0] for call in recorded.await_args_list] == [0, 1, 2]
    select.assert_not_awaited()
    before.assert_not_awaited()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()


def test_legacy_task_waits_for_app_before_classifying_login_prompt():
    from tests.console.test_credentials import _FakeBrowser, _FakePlaywrightManager
    from core.web_chat import TargetNotFoundError
    browser = _FakeBrowser()
    api = ModuleType('playwright.async_api')
    api.async_playwright = lambda: _FakePlaywrightManager(browser)
    ready = False
    async def hydrate(*args, **kwargs):
        nonlocal ready
        ready = True
    async def login_prompt(*args, **kwargs):
        return not ready
    with patch.dict(sys.modules, {'playwright.async_api': api}), \
         patch('spark_console.executor.wait_for_chat_ready', hydrate), \
         patch('spark_console.executor.page_has_web_chat_login_prompt', login_prompt), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock(side_effect=TargetNotFoundError())):
        outcome = asyncio.run(DouyinExecutor().execute(
            b'[{"name":"fixture","value":"test","domain":".douyin.com","path":"/"}]', 'A', 'unused'))
    assert ready
    assert outcome.error_code == 'target_not_found'


@patch('spark_console.executor.verify_chat_recipient_name', new=AsyncMock())
def test_batch_launches_once_and_sends_distinct_messages_after_checkpoints():
    events = []
    editor = SimpleNamespace(type=AsyncMock(side_effect=lambda text: events.append(('type', text))),
                             fill=AsyncMock(), press=AsyncMock(side_effect=lambda key: events.append(('press', key))))
    editor.element_handle = AsyncMock(return_value=editor)
    page = SimpleNamespace(goto=AsyncMock(), close=AsyncMock(), on=lambda *a: None, wait_for_selector=AsyncMock(),
                           locator=lambda *a: SimpleNamespace(first=editor, count=AsyncMock(return_value=0)))
    page.wait_for_function = AsyncMock(return_value=SimpleNamespace(json_value=AsyncMock(return_value='ready')))
    context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page), close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    launch = AsyncMock(return_value=browser)
    manager = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    class Playwright:
        async def __aenter__(self): return manager
        async def __aexit__(self, *args): pass
    api = ModuleType('playwright.async_api')
    api.async_playwright = Playwright
    async def before(position):
        events.append(('checkpoint', position))
        return True
    async def result(position, result): events.append(('result', position, result.success))
    with patch.dict(sys.modules, {'playwright.async_api': api}), \
         patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock()), \
         patch('core.tasks.confirm_message_sent', AsyncMock()):
        outcomes = asyncio.run(DouyinExecutor().execute_batch(
            b'[{"name":"fixture","value":"test","domain":".douyin.com","path":"/"}]',
            [dict(target_name='A', target_sec_uid='', message_template='甲'),
             dict(target_name='B', target_sec_uid='', message_template='乙')],
            before_send=before, on_result=result))
    assert all(r.success for r in outcomes)
    assert launch.await_count == 1
    assert context.new_page.await_count == 1
    page.goto.assert_awaited_once()
    assert browser.close.await_count == context.close.await_count == 1
    assert [event for event in events if event[0] == 'type'] == [('type', '甲'), ('type', '乙')]
    assert events.index(('checkpoint', 0)) < events.index(('press', 'Enter'))
    assert events.index(('result', 0, True)) < events.index(('type', '乙'))


def test_existing_conversation_is_reselected_and_verified_before_typing():
    editor = SimpleNamespace(is_visible=AsyncMock(return_value=True), type=AsyncMock(), press=AsyncMock())
    page = SimpleNamespace(wait_for_selector=AsyncMock(), locator=lambda *a: SimpleNamespace(first=editor, count=AsyncMock(return_value=1)))
    before = AsyncMock(return_value=True)
    from core.web_chat import RecipientNameError
    with patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock()) as select, \
         patch('spark_console.executor.verify_chat_recipient_name', AsyncMock(side_effect=RecipientNameError())) as verify:
        result = asyncio.run(DouyinExecutor()._batch_recipient(page, None,
            dict(target_name='A', message_template='hello'), 0, before))
    assert result.error_code == 'recipient_name_unverified'
    select.assert_awaited_once()
    verify.assert_awaited_once()
    before.assert_not_awaited()
    editor.type.assert_not_awaited()
    editor.press.assert_not_awaited()


@patch('spark_console.executor.verify_chat_recipient_name', new=AsyncMock())
def test_revoked_authorization_never_presses_send():
    editor = SimpleNamespace(type=AsyncMock(), fill=AsyncMock(), press=AsyncMock())
    editor.element_handle = AsyncMock(return_value=editor)
    page = SimpleNamespace(wait_for_selector=AsyncMock(),
        locator=lambda *a: SimpleNamespace(first=editor, count=AsyncMock(return_value=0)))
    before = AsyncMock(return_value=False)
    with patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock()):
        result = asyncio.run(DouyinExecutor()._batch_recipient(page, None,
            dict(target_name='A', message_template='hello'), 0, before))
    assert result.error_code == 'authorization_ended'
    before.assert_awaited_once_with(0)
    editor.press.assert_not_awaited()
