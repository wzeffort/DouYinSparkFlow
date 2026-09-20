import asyncio
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.im_evidence import fields, receipt, payload_matches, SendMonitor, ConversationChanged, verify_conversation
from spark_console.message_content import PREFIX, parse_content, render_content
from spark_console.models import RunMessageContent, TaskRun, TaskRunRecipient
from spark_console.services.batch_execution import BatchRunService
from tests.console.test_recipient_review import setup, client


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def encoded(mode='hitokoto', text='今日 {一言}', **extra):
    return PREFIX + json.dumps(dict(mode=mode, text=text, **extra), ensure_ascii=False)


def test_content_modes_and_fallback():
    assert render_content('旧文案', NOW)[0] == '旧文案'
    assert render_content(encoded(), NOW, quote=lambda _: '诗词')[0] == '今日 诗词'
    assert render_content(encoded(fallback='备用'), NOW, quote=lambda _: 1/0) == ('备用', '一言不可用，已使用备用文案')
    assert render_content(encoded('random', '甲\n乙'), NOW)[0] in {'甲', '乙'}
    first = render_content(encoded('daily', '甲\n乙'), NOW)[0]
    assert first != render_content(encoded('daily', '甲\n乙'), NOW+timedelta(days=1))[0]


@pytest.mark.parametrize('spec', ['{}', 'null', '[]', '{bad', '{"mode":"hitokoto","text":"no token"}',
    '{"mode":"hitokoto","text":"{一言}","types":[{}]}', '{"mode":"random","text":"one"}'])
def test_invalid_config(spec):
    with pytest.raises(ValueError): parse_content(PREFIX+spec)


def varint(n):
    result = b''
    while n > 127: result += bytes([(n & 127) | 128]); n >>= 7
    return result + bytes([n])


def field(tag, value):
    if isinstance(value, int): return varint(tag << 3) + varint(value)
    if isinstance(value, str): value = value.encode()
    return varint(tag << 3 | 2) + varint(len(value)) + value


def response(code=0):
    return field(1,100)+field(3,code)+field(4,'OK')+field(6,field(100,field(1,12345)))


def request_body(conv='0:1:111:222', text='每日火花'):
    return field(8, field(100,field(1,conv)+field(4,json.dumps({'text':text},ensure_ascii=False))))


def test_receipt_and_exact_request_match():
    assert receipt(response()) == {'status':'accepted','code':0,'message_id':'12345'}
    assert receipt(response(8))['status'] == 'rejected'
    assert payload_matches(request_body(), '0:1:111:222', '每日火花')
    assert not payload_matches(request_body(), '0:1:111:223', '每日火花')
    assert not payload_matches(request_body(text='每日火花，旧内容'), '0:1:111:222', '每日火花')


@pytest.mark.parametrize('data',[b'', b'\xff', b'\x12\xff', field(1,99), b'x'*65537], ids=['empty','bad-varint','truncated','wrong-command','oversized'])
def test_unknown_payload_never_proves_success(data):
    assert receipt(data) is None


def test_monitor_binds_exact_request_object_and_discards_late_unrelated_response():
    async def run():
        monitor = SendMonitor(SimpleNamespace(), '0:1:111:222', '每日火花')
        req = SimpleNamespace(url='https://imapi.douyin.com/v1/message/send', method='POST', post_data_buffer=request_body())
        unrelated = SimpleNamespace(status=200, request=object(), body=AsyncMock(return_value=response()))
        monitor.on_response(unrelated)
        assert not monitor.pending
        monitor.on_request(req)
        monitor.on_response(SimpleNamespace(status=200,request=req,body=AsyncMock(return_value=response())))
        await asyncio.gather(*monitor.pending)
        assert (await monitor.wait(.2))['message_id'] == '12345'
        monitor.on_request(req)
        assert await monitor.wait(.2) is None
    asyncio.run(run())


def test_explicit_conversation_mismatch_blocks():
    page = SimpleNamespace(evaluate=AsyncMock(return_value=[dict(conv_id='wrong', current=True, sec_uid='uid')]))
    with pytest.raises(ConversationChanged): asyncio.run(verify_conversation(page,'expected','uid'))


def test_library_text_persisted_and_retry_reuses_it(setup):
    db, _, _, _, _, _, task, original, _ = setup
    old = db.get(TaskRunRecipient,(original.id,2))
    old.message_template = encoded('random','第一句\n第二句')
    old.retryable = True
    db.add(RunMessageContent(run_id=original.id,position=2,text='第一句',source='随机文案库'))
    service=BatchRunService(db)
    at=NOW+timedelta(minutes=1)
    service.remember_retry(task.id,original.id,at)
    run=TaskRun(task_id=task.id,scheduled_for=at,status='running')
    db.add(run);db.flush()
    with patch('spark_console.message_content.render_content',side_effect=AssertionError('must reuse text')):
        # Existing submitted/uncertain rows lack content records; preserve their plain text too.
        for position in (0,1): db.add(RunMessageContent(run_id=original.id,position=position,text='fixture',source='固定文案'))
        db.flush()
        items=service.prepare(run,task)
    assert items[0]['message_template']=='第一句'
    assert db.get(TaskRunRecipient,(run.id,2)).message_template==old.message_template


def test_run_history_shows_actual_text_and_escapes_it(client,setup):
    db, _, _, _, _, _, _, run, _ = setup
    db.add(RunMessageContent(run_id=run.id,position=2,text='<script>example</script>',source='随机文案库',receipt_json='{"status":"unknown"}'))
    db.commit()
    result=client.get('/runs')
    assert result.status_code==200
    assert '&lt;script&gt;example&lt;/script&gt;' in result.text
    assert '发送回执诊断' in result.text


def test_unavailable_api_does_not_run_inside_prepare(setup):
    db, _, _, _, _, _, task, _, _ = setup
    from spark_console.models import SparkTaskRecipient
    db.get(SparkTaskRecipient,(task.id,2)).message_template=encoded()
    run=TaskRun(task_id=task.id,scheduled_for=NOW,status='running')
    db.add(run);db.flush()
    with patch('spark_console.message_content.requests.get',side_effect=AssertionError('HTTP inside transaction')):
        items=BatchRunService(db).prepare(run,task)
    assert items[2]['message_template']==''
    assert db.get(RunMessageContent,(run.id,2)).source=='待生成一言'


def test_general_batch_timeout_retry_limit_is_durable(setup):
    db, _, _, _, _, _, _, run, row = setup
    row.status='pending'
    db.add(RunMessageContent(run_id=run.id,position=2,text='fixed',source='固定文案',attempt_number=3))
    db.flush()
    BatchRunService(db).interrupt(run.id)
    assert not row.retryable and row.status=='failed'


def test_worker_resolves_external_text_before_browser_and_reuses_checkpoint(setup):
    from spark_console.worker import Worker
    db, engine, settings, _, _, account, task, run, row = setup
    row.message_template=encoded(fallback='backup')
    row.status='pending'
    db.add(RunMessageContent(run_id=run.id,position=2,text='',source='待生成一言'))
    db.commit()
    worker=Worker.__new__(Worker)
    worker.engine=engine;worker.settings=settings;worker.execution_timeout_seconds=20;worker.pii=None
    worker._record_task_failure_incident=lambda *args,**kwargs:None
    async def execute(_cookies, recipients, **kwargs):
        assert recipients[0]['message_template']=='generated'
        from sqlalchemy.orm import Session
        with Session(engine) as other:
            assert other.get(RunMessageContent,(run.id,2)).text=='generated'
        from spark_console.executor import ExecutionResult
        await kwargs['on_result'](0,ExecutionResult(True,'submitted'))
    worker.executor=SimpleNamespace(execute_batch=execute)
    with patch('spark_console.message_content.render_content',return_value=('generated','一言')):
        asyncio.run(worker._run_batch(run.id,task.id,account.id,bytearray(b'fake'),1,[{'position':2,'message_template':''}],NOW))


def test_content_picker_offline_browser(client,setup):
    from pathlib import Path
    from urllib.parse import urlsplit
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport={'width':390,'height':844})
        def serve(route):
            path=urlsplit(route.request.url).path
            # Only synthetic authenticated app responses; no network or real sends.
            result=client.get(path)
            route.fulfill(status=result.status_code,body=result.content,headers={'content-type':result.headers.get('content-type','text/html')})
        page.route('**/*',serve)
        page.goto('https://testserver/tasks')
        card=page.locator('.batch-recipient-card').first
        picker=card.locator('select').first
        picker.select_option('hitokoto')
        assert card.get_by_text('接口失败时发送的备用文案').is_visible()
        value=json.loads(page.locator('[name=recipients_json]').input_value())[0]['message_template']
        assert parse_content(value)['mode']=='hitokoto'
        card.get_by_role('button', name='使用每日一言排版').click()
        assert '每日一言' in card.locator('textarea').input_value()
        assert '终南别业（王维）' in card.locator('pre').inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        Path('artifacts').mkdir(exist_ok=True)
        page.screenshot(path='artifacts/profile-quote-mobile.png', full_page=True)
        card.locator('textarea').fill('我的自定义开头\n{一言}')
        assert '我的自定义开头' in card.locator('pre').inner_text()
        picker.select_option('daily')
        card.locator('textarea').fill('早安\n今日开心')
        value=json.loads(page.locator('[name=recipients_json]').input_value())[0]['message_template']
        assert parse_content(value)['text']=='早安\n今日开心'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        Path('artifacts').mkdir(exist_ok=True)
        page.screenshot(path='artifacts/content-picker-mobile.png',full_page=True)
        page.set_viewport_size({'width':1280,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path='artifacts/content-picker-desktop.png',full_page=True)
        browser.close()
