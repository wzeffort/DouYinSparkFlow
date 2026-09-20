import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from spark_console.auth_scanner import ACCOUNT_INFO_URL, DouyinQrScanner
from spark_console.message_content import fetch_quote
from spark_console.models import DouyinAccount, DouyinAccountIdentity
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.crypto import CookieCipher
from tests.console.test_recipient_review import setup, client


@pytest.mark.parametrize('field', ['nickname', 'name'])
def test_profile_reads_authenticated_self_only(field):
    response = SimpleNamespace(status=200, json=AsyncMock(return_value={
        'message': 'success', 'data': {'error_code': 0, 'user_id': '123',
            field: ' 特殊昵称ʊ_ʊ ', 'unique_id': 'my-douyin'}}))
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    assert asyncio.run(DouyinQrScanner._account_profile(SimpleNamespace(request=request))) == ('特殊昵称ʊ_ʊ', 'my-douyin')
    request.get.assert_awaited_once_with(ACCOUNT_INFO_URL, timeout=5000, max_redirects=0)


@pytest.mark.parametrize('body', [None, [], {'nickname': '路人'},
    {'message': 'error', 'data': {'nickname': '路人'}},
    {'message': 'success', 'data': {'error_code': 0, 'nickname': '路人'}}])
def test_invalid_profile_not_used(body):
    context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=
        SimpleNamespace(status=200, json=AsyncMock(return_value=body)))))
    assert asyncio.run(DouyinQrScanner._account_profile(context)) == (None, None)


def test_profile_network_failure_does_not_break_login():
    context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=TimeoutError)))
    assert asyncio.run(DouyinQrScanner._account_profile(context)) == (None, None)


def test_near_deadline_skips_optional_profile():
    async def run():
        context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock()))
        result = await DouyinQrScanner._optional_account_profile(context, asyncio.get_running_loop().time() + .5)
        assert result == (None, None)
        context.request.get.assert_not_called()
    asyncio.run(run())


def test_login_uses_profile_without_creator_center_dom():
    from tests.console.test_auth_scanner import DouyinQrScannerTests
    async def run():
        scanner, _, context = DouyinQrScannerTests()._scanner(
            mode='chat_dom_success', profile_visible=False, login_timeout_seconds=3)
        context.request.get = AsyncMock(return_value=SimpleNamespace(status=200, json=AsyncMock(return_value={
            'message': 'success', 'data': {'error_code': 0, 'user_id': '123', 'name': '真实昵称', 'unique_id': 'douyin-456'}})))
        result = await scanner.run(lambda _: None, lambda _: None, lambda: False)
        assert (result.display_name, result.unique_id) == ('真实昵称', 'douyin-456')
        assert context.closed
    asyncio.run(run())


@pytest.mark.parametrize('extra, attribution', [
    ({'from': '终南别业', 'from_who': '王维'}, '终南别业（王维）'),
    ({'from': '原创', 'from_who': None}, '原创（未知作者）'),
    ({}, '未知来源（未知作者）'),
    ({'from': ['invalid'], 'from_who': 'x'*81}, '未知来源（未知作者）'),
])
def test_quote_retains_attribution(extra, attribution):
    response = Mock(status_code=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [json.dumps({'hitokoto': '行到水穷处，坐看云起时。', **extra}).encode()]
    with patch('spark_console.message_content.requests.get', return_value=response):
        assert fetch_quote(['i']) == '行到水穷处，坐看云起时。\n—— ' + attribution


def test_duplicate_generic_accounts_have_distinct_labels_without_renaming(setup, client):
    db, _, _, owner, stranger, account, *_ = setup
    account.display_name = '抖音账号'
    second = DouyinAccount(owner_user_id=owner.id, display_name='抖音账号', encrypted_cookies=b'x', cookie_nonce=b'x')
    other = DouyinAccount(owner_user_id=stranger.id, display_name='private', encrypted_cookies=b'x', cookie_nonce=b'x')
    db.add_all([second, other]); db.flush()
    db.add(DouyinAccountIdentity(account_id=second.id, douyin_unique_id='second-id'))
    db.commit()
    rows = AccountService(db, None, None).list_owned(owner.id)
    assert len(rows) == 2
    assert len({row['account_label'] for row in rows}) == 2
    assert all(row['display_name'] == '抖音账号' for row in rows)
    page = client.get('/tasks')
    assert page.status_code == 200
    assert '抖音账号 · second-id' in page.text
    assert f'抖音账号 · 账号 {account.id[:8]}' in page.text
    assert other.id not in page.text


def test_relogin_lookup_failure_preserves_existing_name(setup):
    from tests.console.test_auth_worker import _storage_state
    db, _, _, owner, _, account, *_ = setup
    account.display_name = '我的工作号'
    db.add(DouyinAccountIdentity(account_id=account.id, douyin_unique_id='known-id'))
    db.commit()
    service = AccountService(db, CookieCipher(b'c' * 32), AuditService(db))
    rebound = service.create_from_storage_state(owner.id, '抖音账号', _storage_state(), 'known-id')
    assert rebound.id == account.id
    assert rebound.display_name == '我的工作号'
