import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.web_chat import RecipientNameError, verify_chat_recipient_name
from spark_console.config import Settings
from spark_console.db import create_schema
from spark_console.executor import DouyinExecutor, ExecutionResult
from spark_console.models import (
    DouyinAccount, ManualRunRetry, RecipientCheckEvidence, RecipientNameApproval,
    SparkTask, SparkTaskRecipient, TaskBatchRetry, TaskRun, TaskRunRecipient, User, WebSession,
)
from spark_console.security import SessionService
from spark_console.services import NotFound, ValidationError
from spark_console.services.batch_execution import BatchRunService
from spark_console.services.manual_retries import ManualRetryService
from spark_console.services.recipient_review import RecipientReviewService, name_approvals, save_evidence
from spark_console.web.app import create_app


def diagnostic(observed='ʊ_ʊ\u200b', reason='name_mismatch'):
    return dict(reason=reason, expected_name='ʊ_ʊ', observed_name=observed,
                header_count=1, title_count=1, editor_count=1,
                header_visible=True, title_visible=True, editor_visible=True)


class Locator:
    def __init__(self, text='', count=1, visible=True):
        self.text, self.n, self.visible = text, count, visible
        self.child = None

    async def count(self): return self.n
    async def is_visible(self): return self.visible
    async def inner_text(self): return self.text
    def locator(self, _selector): return self.child


class Page:
    url = 'https://www.douyin.com/chat'

    def __init__(self, title):
        self.header = Locator()
        self.header.child = Locator(title)
        self.editor = Locator()

    def locator(self, selector):
        return self.header if selector == '.RightPanelHeaderconvHeader' else self.editor


@pytest.mark.parametrize('expected,observed', [('ʊ_ʊ', 'ʊ_ʊ'), ('ᗜ _ ᗜ', 'ᗜ _ ᗜ'), ('é', 'e\u0301')])
def test_exact_or_canonical_equivalent_names_pass(expected, observed):
    result = asyncio.run(verify_chat_recipient_name(Page(observed), expected, timeout=0))
    assert result['reason'] == 'matched'


@pytest.mark.parametrize('observed', ['ʊ_ʊ\u200b', 'u_u', 'ʊ ʊ', 'ʊ_ʊ🔥1114'])
def test_title_differences_are_logged_without_blocking(observed):
    result = asyncio.run(verify_chat_recipient_name(Page(observed), 'ʊ_ʊ', timeout=0))
    assert result['observed_name'] == observed
    assert result['reason'] == 'name_difference_allowed'


def test_unrelated_title_also_passes_without_character_overlap_or_approval():
    result = asyncio.run(verify_chat_recipient_name(Page('other'), 'ʊ_ʊ', timeout=0, approved_names=['other']))
    assert result['reason'] == 'human_approved'
    result = asyncio.run(verify_chat_recipient_name(Page('different'), 'ʊ_ʊ', timeout=0))
    assert result['reason'] == 'name_difference_allowed'


@pytest.mark.parametrize('title', ['ᗜ\u00a0_\u00a0ᗜ', '完全不同的显示名称'])
def test_executor_sends_to_selected_chat_without_matching_title(title):
    import sys
    from types import SimpleNamespace
    page = Page(title)
    editor = SimpleNamespace(fill=AsyncMock(), type=AsyncMock(), press=AsyncMock())
    page.editor.first = page.editor
    page.editor.element_handle = AsyncMock(return_value=editor)
    page.wait_for_selector = AsyncMock()
    with patch.dict(sys.modules, {'core.tasks': SimpleNamespace(confirm_message_sent=AsyncMock())}), \
         patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock(return_value='ᗜ _ ᗜ')):
        result = asyncio.run(DouyinExecutor()._batch_recipient(page, None,
            dict(target_name='ᗜ _ ᗜ', target_sec_uid='uid2', message_template='fixture'),
            0, AsyncMock(return_value=True)))
    assert result.success
    assert result.recipient_diagnostic['reason'] == 'name_difference_allowed'
    editor.press.assert_awaited_once_with('Enter')


@pytest.mark.parametrize('kind', ['editor', 'header', 'title', 'url', 'multiple'])
def test_approval_does_not_bypass_page_readiness(kind):
    page = Page('other')
    if kind == 'editor': page.editor.visible = False
    if kind == 'header': page.header.n = 0
    if kind == 'title': page.header.child.visible = False
    if kind == 'url': page.url = 'https://www.douyin.com/login'
    if kind == 'multiple': page.header.child.n = 2
    with pytest.raises(RecipientNameError):
        asyncio.run(verify_chat_recipient_name(page, 'ʊ_ʊ', timeout=0, approved_names=['other']))


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=f'sqlite:///{tmp_path / "test.db"}',
        cookie_key_file=tmp_path/'cookie.key', session_key_file=tmp_path/'session.key')
    settings.cookie_key_file.write_bytes(b'c' * 32)
    settings.session_key_file.write_bytes(b's' * 32)
    engine = create_engine(settings.database_url, connect_args={'check_same_thread': False})
    create_schema(engine)
    db = Session(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    # Keep the fixture inside the current Shanghai day even around midnight.
    owner = User(username='owner', password_hash='unused', role='admin', must_change_password=False)
    stranger = User(username='stranger', password_hash='unused', role='user', must_change_password=False)
    db.add_all([owner, stranger]); db.flush()
    account = DouyinAccount(owner_user_id=owner.id, display_name='fixture', encrypted_cookies=b'unused',
                            cookie_nonce=b'unused', validation_state='valid')
    db.add(account); db.flush()
    task = SparkTask(owner_user_id=owner.id, douyin_account_id=account.id, target_name='batch',
        send_time='09:10', message_template='fixture', enabled=True, next_run_at=now+timedelta(days=1))
    db.add(task); db.flush()
    run = TaskRun(task_id=task.id, scheduled_for=now, status='partial', stage='batch_complete', finished_at=now)
    db.add(run); db.flush()
    for i, status in enumerate(['submitted', 'uncertain', 'failed']):
        data = dict(position=i, target_name='ʊ_ʊ' if i == 2 else f'friend{i}',
                    target_sec_uid=f'uid{i}', message_template='fixture')
        db.add(SparkTaskRecipient(task_id=task.id, **data))
        db.add(TaskRunRecipient(run_id=run.id, account_id=account.id, status=status,
            stage='selecting_target' if i == 2 else 'sending',
            error_code='recipient_name_unverified' if i == 2 else None, **data))
    db.flush()
    row = db.get(TaskRunRecipient, (run.id, 2))
    save_evidence(db, row, diagnostic()); db.commit()
    yield db, engine, settings, owner, stranger, account, task, run, row
    db.close(); engine.dispose()


def approve(setup):
    db, _, _, owner, _, _, _, run, _ = setup
    service = RecipientReviewService(db)
    digest = service.load(owner, run.id, 2)[-1]
    service.approve(owner, run.id, 2, digest)
    db.commit()


def test_approval_scoped_to_account_uid_name_and_revocable(setup):
    db, _, _, owner, _, account, _, run, row = setup
    approve(setup)
    assert name_approvals(db, account.id, 'uid2', row.target_name)
    assert not name_approvals(db, 'another-account', 'uid2', row.target_name)
    assert not name_approvals(db, account.id, 'another-uid', row.target_name)
    assert not name_approvals(db, account.id, 'uid2', 'renamed')
    RecipientReviewService(db).revoke(owner, run.id, 2)
    assert not name_approvals(db, account.id, 'uid2', row.target_name)


def test_other_user_cannot_read_approve_or_revoke(setup):
    db, _, _, _, stranger, _, _, run, _ = setup
    service = RecipientReviewService(db)
    for action in [lambda: service.load(stranger, run.id, 2),
                   lambda: service.approve(stranger, run.id, 2, ''),
                   lambda: service.revoke(stranger, run.id, 2)]:
        with pytest.raises(NotFound): action()


@pytest.mark.parametrize('change', ['digest', 'target', 'account', 'no_evidence', 'not_ready'])
def test_stale_or_missing_evidence_cannot_be_approved(setup, change):
    db, _, _, owner, _, _, task, run, row = setup
    service = RecipientReviewService(db)
    digest = service.load(owner, run.id, 2)[-1]
    if change == 'digest': digest = 'stale'
    if change == 'target': db.get(SparkTaskRecipient, (task.id, 2)).target_sec_uid = 'different'
    if change == 'account': task.douyin_account_id = None
    if change == 'no_evidence': db.delete(db.get(RecipientCheckEvidence, (run.id, 2)))
    if change == 'not_ready': save_evidence(db, row, diagnostic(reason='page_not_ready'))
    db.flush()
    with pytest.raises(ValidationError): service.approve(owner, run.id, 2, digest)


def test_confirmation_does_not_schedule_send(setup):
    db, _, _, _, _, _, task, _, _ = setup
    before = task.next_run_at
    approve(setup)
    assert task.next_run_at == before
    assert not db.scalars(select(ManualRunRetry)).all()
    assert not db.scalars(select(TaskBatchRetry)).all()


def test_manual_retry_only_failed_recipient_preserves_submitted_and_uncertain(setup):
    db, _, _, owner, _, _, task, run, row = setup
    approve(setup)
    result = ManualRetryService(db).schedule(owner.id, [run.id], owned=True, positions=[2])
    assert result[0]['scheduled_for']
    new_run = TaskRun(task_id=task.id, scheduled_for=result[0]['scheduled_for'])
    db.add(new_run); db.flush()
    pending = BatchRunService(db).prepare(new_run, task)
    assert [item['position'] for item in pending] == [2]
    assert pending[0]['name_approvals'][0]['observed_name'] == 'ʊ_ʊ\u200b'
    assert [r.status for r in BatchRunService(db).rows(new_run.id)] == ['submitted', 'uncertain', 'pending']


def test_cannot_force_retry_of_submitted_or_uncertain(setup):
    db, _, _, owner, _, _, _, run, _ = setup
    for position in [0, 1]:
        result = ManualRetryService(db).schedule(owner.id, [run.id], owned=True, positions=[position])
        assert not result[0]['scheduled_for']


def test_repeated_manual_retry_is_idempotent(setup):
    db, _, _, owner, _, _, _, run, _ = setup
    first = ManualRetryService(db).schedule(owner.id, [run.id], owned=True, positions=[2])
    second = ManualRetryService(db).schedule(owner.id, [run.id], owned=True, positions=[2])
    assert first[0]['scheduled_for'] and not second[0]['scheduled_for']
    assert len(db.scalars(select(ManualRunRetry)).all()) == 1


def test_result_records_sanitized_evidence_and_enables_retry(setup):
    db, _, _, _, _, _, _, run, _ = setup
    evidence = dict(diagnostic(), cookie='do-not-store', body='private conversation')
    BatchRunService(db).record(run.id, 2, ExecutionResult(False, 'selecting_target',
        'recipient_name_unverified', 'not sent', True, evidence))
    assert BatchRunService(db).summary(run.id)[2]
    saved = db.get(RecipientCheckEvidence, (run.id, 2)).diagnostic_json
    assert 'do-not-store' not in saved and 'private conversation' not in saved


@pytest.fixture
def client(setup):
    db, engine, settings, owner, _, _, _, _, _ = setup
    sessions = SessionService(b's' * 32)
    raw = 'a' * 43
    db.add(WebSession(user_id=owner.id, token_hash=sessions.token_hash(raw), csrf_token='fixture-csrf',
                      expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
    db.commit()
    with TestClient(create_app(settings, engine), base_url='https://testserver') as client:
        client.cookies.set('spark_session', raw)
        yield client


def test_review_page_has_evidence_and_no_live_credentials(client, setup):
    *_, run, row = setup
    response = client.get(f'/runs/{run.id}/recipients/2/review')
    assert response.status_code == 200
    assert 'U+200B' in response.text and '不要求人工确认' in response.text
    assert 'fixture-csrf' in response.text  # standard anti-CSRF form field only
    assert 'encrypted_cookies' not in response.text
    assert '查看名称诊断' in client.get('/runs').text


def test_review_post_requires_csrf_and_explicit_checkbox(client, setup):
    db, _, _, owner, _, _, _, run, _ = setup
    url = f'/runs/{run.id}/recipients/2/review'
    digest = RecipientReviewService(db).load(owner, run.id, 2)[-1]
    assert client.post(url, data={'action':'approve'}).status_code == 403
    assert client.post(url, data={'csrf_token':'fixture-csrf', 'action':'approve',
        'evidence_digest':digest}).status_code == 409
    response = client.post(url, data={'csrf_token':'fixture-csrf', 'action':'approve',
        'evidence_digest':digest, 'confirm_same_person':'yes'}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.scalars(select(RecipientNameApproval)).first()


def test_evidence_html_is_escaped(client, setup):
    db, _, _, _, _, _, _, run, row = setup
    save_evidence(db, row, diagnostic('<script>alert(1)</script>')); db.commit()
    response = client.get(f'/runs/{run.id}/recipients/2/review')
    assert '<script>alert(1)</script>' not in response.text
    assert '&lt;script&gt;' in response.text


def test_executor_stops_before_typing_and_marks_name_failure_retryable():
    import sys
    from types import SimpleNamespace
    page = SimpleNamespace(wait_for_selector=AsyncMock())
    with patch.dict(sys.modules, {'core.tasks': SimpleNamespace(confirm_message_sent=AsyncMock())}), \
         patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock(return_value='ʊ_ʊ')), \
         patch('spark_console.executor.verify_chat_recipient_name', AsyncMock(side_effect=RecipientNameError(diagnostic()))):
        before = AsyncMock(return_value=True)
        result = asyncio.run(DouyinExecutor()._batch_recipient(page, None,
            dict(target_name='ʊ_ʊ', target_sec_uid='uid2', message_template='fixture'), 0, before))
    assert not result.success and result.retryable
    assert result.recipient_diagnostic['reason'] == 'name_mismatch'
    before.assert_not_awaited()


def test_owner_without_admin_role_can_confirm_own_friend(setup):
    db, _, _, owner, _, _, _, _, _ = setup
    owner.role = 'user'; db.commit()
    approve(setup)
    assert db.scalars(select(RecipientNameApproval)).first()


def test_http_cross_user_review_is_hidden(client, setup):
    db, _, _, owner, stranger, _, _, run, _ = setup
    record = db.scalar(select(WebSession).where(WebSession.user_id == owner.id))
    record.user_id = stranger.id; db.commit()
    url = f'/runs/{run.id}/recipients/2/review'
    assert client.get(url).status_code == 404
    assert client.post(url, data={'action':'retry', 'csrf_token':'fixture-csrf'}).status_code == 404


def test_auto_retry_includes_only_retryable_failed_row(setup):
    db, _, _, _, _, _, task, run, _ = setup
    batch = BatchRunService(db)
    batch.record(run.id, 2, ExecutionResult(False, 'selecting_target', 'recipient_name_unverified',
        'not sent', True, diagnostic()))
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    batch.remember_retry(task.id, run.id, when)
    retry = TaskRun(task_id=task.id, scheduled_for=when)
    db.add(retry); db.flush()
    pending = batch.prepare(retry, task)
    assert [row['position'] for row in pending] == [2]
    assert [row.status for row in batch.rows(retry.id)][:2] == ['submitted', 'uncertain']


def test_name_retry_budget_survives_long_queue_delays(setup):
    db, _, _, _, _, _, task, run, _ = setup
    batch = BatchRunService(db)
    for attempt in range(1, 4):
        batch.record(run.id, 2, ExecutionResult(False, 'selecting_target', 'recipient_name_unverified',
            'not sent', True, diagnostic()))
        row = db.get(TaskRunRecipient, (run.id, 2))
        assert row.retryable is (attempt < 3)
        if attempt < 3:
            when = datetime.now(timezone.utc) + timedelta(minutes=30 * attempt)
            batch.remember_retry(task.id, run.id, when)
            run = TaskRun(task_id=task.id, scheduled_for=when)
            db.add(run); db.flush()
            batch.prepare(run, task)


@pytest.mark.parametrize('failure_check', [None, 3])
def test_executor_rechecks_approved_pair_at_send_boundary(failure_check):
    import sys
    from types import SimpleNamespace
    editor = SimpleNamespace(fill=AsyncMock(), type=AsyncMock(), press=AsyncMock())
    locator = SimpleNamespace(element_handle=AsyncMock(return_value=editor))
    locator.first = locator
    page = SimpleNamespace(wait_for_selector=AsyncMock(), locator=lambda _: locator)
    evidence = diagnostic(reason='human_approved')
    checks = [evidence, evidence, RecipientNameError(diagnostic()) if failure_check else evidence]
    verify = AsyncMock(side_effect=checks)
    with patch.dict(sys.modules, {'core.tasks': SimpleNamespace(confirm_message_sent=AsyncMock())}), \
         patch('spark_console.executor.page_has_web_chat_login_prompt', AsyncMock(return_value=False)), \
         patch('spark_console.executor.select_web_chat_target', AsyncMock(return_value='ʊ_ʊ')), \
         patch('spark_console.executor.verify_chat_recipient_name', verify):
        result = asyncio.run(DouyinExecutor()._batch_recipient(page, None,
            dict(target_name='ʊ_ʊ', target_sec_uid='uid2', message_template='fixture',
                 name_approvals=[dict(selected_name='ʊ_ʊ', observed_name='ʊ_ʊ\u200b')]),
            0, AsyncMock(return_value=True)))
    assert verify.await_count == 3
    assert verify.await_args.kwargs['approved_names'] == ('ʊ_ʊ\u200b',)
    if failure_check:
        assert not result.success and result.retryable
        editor.press.assert_not_awaited()
    else:
        assert result.success
        editor.press.assert_awaited_once_with('Enter')


def test_review_page_mobile_and_desktop_render(client, setup):
    from pathlib import Path
    from playwright.sync_api import sync_playwright
    db, _, _, _, _, _, _, run, row = setup
    # Long, hostile-looking display names exercise escaping and wrapping.
    save_evidence(db, row, diagnostic('特殊符号\u200b' * 35)); db.commit()
    html = client.get(f'/runs/{run.id}/recipients/2/review').text
    css = (Path(__file__).parents[2] / 'spark_console/static/app.css').read_text(encoding='utf-8')
    artifacts = Path(__file__).parents[2] / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.abort())
        for width, label in [(390, 'mobile'), (1280, 'desktop')]:
            page.set_viewport_size({'width':width, 'height':900})
            page.set_content(html)
            page.add_style_tag(content=css)
            page.get_by_text('查看特殊字符与不可见字符', exact=True).click()
            assert page.get_by_text('当前发送规则', exact=True).is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(artifacts / f'recipient-review-{label}.png'), full_page=True, animations='disabled')
        save_evidence(db, row, diagnostic()); db.commit()
        page.set_viewport_size({'width':390, 'height':900})
        page.set_content(client.get(f'/runs/{run.id}/recipients/2/review').text)
        page.add_style_tag(content=css)
        page.screenshot(path=str(artifacts / 'recipient-review-preview.png'), full_page=True, animations='disabled')
        browser.close()
