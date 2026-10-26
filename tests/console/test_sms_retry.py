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
