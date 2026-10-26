import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from spark_console.auth_scanner import DouyinQrScanner, LoginTimedOut, ScanCancelled


@pytest.mark.parametrize('outcome', ['corrected', 'cancelled', 'expired'])
def test_sms_rejection_can_retry_without_losing_cancellation_or_deadline(outcome):
    async def run():
        scanner = DouyinQrScanner()
        control = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                                  click=AsyncMock(), fill=AsyncMock())
        page = SimpleNamespace(
            locator=lambda *a: SimpleNamespace(first=control, last=control),
            get_by_text=lambda *a, **k: SimpleNamespace(all=AsyncMock(return_value=[control])),
            keyboard=SimpleNamespace(type=AsyncMock()),
        )
        actions = [{'kind': 'phone', 'text': '13800000000'},
                   {'kind': 'text', 'text': '111111'}]
        if outcome == 'corrected':
            actions.append({'kind': 'text', 'text': '222222'})
        scanner._wait_for_sms_sent = AsyncMock()
        stages, confirmations = [], []
        scanner._account_session_is_authenticated = AsyncMock(
            side_effect=lambda _: control.fill.await_count == 2)
        async def invoke():
            await scanner._sms_login_flow(SimpleNamespace(page=page, context=object()),
                stages.append, confirmations.append,
                lambda: actions.pop(0) if actions else None,
                lambda: outcome == 'cancelled' and control.fill.await_count == 1,
                asyncio.get_running_loop().time() + (1 if outcome != 'expired' else 0.05))
        with patch('spark_console.auth_scanner.asyncio.sleep', new=AsyncMock()):
            if outcome == 'corrected':
                await invoke()
                assert [c.args[0] for c in control.fill.await_args_list] == ['111111', '222222']
                assert confirmations == [True]
            else:
                with pytest.raises(ScanCancelled if outcome == 'cancelled' else LoginTimedOut):
                    await invoke()
                assert confirmations == []
        assert stages == ['awaiting_phone', 'awaiting_sms']
    asyncio.run(run())


@pytest.mark.parametrize('visible,error', [
    ('验证码已发送', None), ('59秒后重新获取', None),
    ('安全验证', 'verification'), ('操作过于频繁', 'verification'),
    ('手机号不正确', 'failed'), ('发送失败', 'failed'), ('', 'failed'),
])
def test_sms_requires_platform_confirmation(visible, error):
    from spark_console.auth_scanner import SmsSendFailed, VerificationRequired
    async def run():
        control = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        page = SimpleNamespace(get_by_text=lambda pattern: SimpleNamespace(
            all=AsyncMock(return_value=[control] if pattern.search(visible) else [])))
        scanner = DouyinQrScanner()
        clock = [0]
        async def tick(_): clock[0] += 1
        with patch('spark_console.auth_scanner.asyncio.get_running_loop',
                   return_value=SimpleNamespace(time=lambda: clock[0])), \
             patch('spark_console.auth_scanner.asyncio.sleep', side_effect=tick):
            if error:
                with pytest.raises(VerificationRequired if error == 'verification' else SmsSendFailed):
                    await scanner._wait_for_sms_sent(page, lambda: False, 100)
            else:
                await scanner._wait_for_sms_sent(page, lambda: False, 100)
    asyncio.run(run())


@pytest.mark.parametrize('cancelled', [True, False])
def test_sms_confirmation_respects_cancel_and_deadline(cancelled):
    from spark_console.auth_scanner import LoginTimedOut, ScanCancelled
    async def run():
        page = SimpleNamespace(get_by_text=lambda pattern: SimpleNamespace(all=AsyncMock(return_value=[])))
        scanner = DouyinQrScanner()
        with pytest.raises(ScanCancelled if cancelled else LoginTimedOut):
            await scanner._wait_for_sms_sent(page, lambda: cancelled,
                asyncio.get_running_loop().time() + (1 if cancelled else -1))
    asyncio.run(run())
