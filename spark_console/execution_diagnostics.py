"""Bounded, durable phase timings without messages, credentials, names or page data."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import time

from spark_console.db import session_scope
from spark_console.models import TaskRunDiagnostic

PHASES = {'starting':'启动浏览器', 'navigation':'打开聊天页面', 'chat_ready':'等待聊天列表',
    'authenticating':'检查登录', 'selecting_target':'选择好友', 'editor_ready':'等待输入框',
    'recipient_check':'核对聊天对象', 'message_input':'填写消息', 'authorization':'发送前权限检查',
    'sending':'提交发送', 'confirming':'等待页面反馈'}
_active = ContextVar('spark_run_diagnostics', default=None)


class RunTrace:
    def __init__(self, engine, run_id):
        self.engine, self.run_id = engine, run_id
        self.history = []
        self.phase = 'starting'
        self.position = None
        self.started = time.monotonic()
        self.send_started = False

    def persist(self, finished=False):
        now = datetime.now(timezone.utc)
        with session_scope(self.engine) as db:
            row = db.get(TaskRunDiagnostic, self.run_id)
            if row is None:
                row = TaskRunDiagnostic(run_id=self.run_id); db.add(row)
            row.current_phase = self.phase
            row.phase_history = json.dumps(self.history[-100:], separators=(',', ':'))
            row.send_started = self.send_started
            if finished:
                row.finished_at = now
            else:
                row.phase_started_at = now

    def close_phase(self):
        self.history.append({'phase':self.phase, 'position':self.position,
            'ms':max(0, round((time.monotonic()-self.started)*1000))})

    def move(self, phase, position=None):
        if phase not in PHASES or (position is not None and (type(position) is not int or not 0 <= position < 5)):
            raise ValueError('invalid diagnostic phase')
        self.close_phase()
        self.phase, self.position, self.started = phase, position, time.monotonic()
        self.send_started = self.send_started or phase == 'sending'
        self.persist()


def trace_phase(phase, position=None):
    trace = _active.get()
    if trace is not None:
        trace.move(phase, position)


@contextmanager
def trace_run(engine, run_id):
    trace = RunTrace(engine, run_id)
    trace.persist()
    token = _active.set(trace)
    try:
        yield trace
    finally:
        try:
            trace.close_phase(); trace.persist(finished=True)
        finally:
            _active.reset(token)


def public_trace(row):
    if row is None:
        return None
    events = [{'label':PHASES.get(item['phase'], '未知阶段'), 'position':item.get('position'),
        'seconds':round(item['ms']/1000, 2)} for item in json.loads(row.phase_history)]
    return {'events':events, 'last_phase':PHASES.get(row.current_phase, '未知阶段'), 'finished':row.finished_at is not None}
