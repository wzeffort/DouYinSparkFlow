"""Anonymous console preview: static sample data, zero database access.

Renders the real console templates for visitors in demo mode so they can see
the product before registering. No session is issued, no real rows are read,
and every mutating endpoint still requires a real login (demo visitors are
redirected to /register by the 401 handler).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    base = datetime.now(timezone.utc).astimezone(timezone.utc) + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _common() -> dict:
    return {
        "user": SimpleNamespace(username="预览访客", role="user"),
        "csrf_token": "demo",
        "is_admin": False,
        "unread_notification_count": 2,
        "personal_name": "预览访客",
        "personal_avatar_version": None,
        "demo_mode": True,
    }


def _platform_status():
    return SimpleNamespace(
        total=12, success=9, confirmed=8, submitted=1, running=1,
        pending=1, failed=1, worker_online=True,
        updated_at=_at(0, 1, 5),
    )


def _accounts():
    return [
        SimpleNamespace(id="demo-acc-1", account_label="抖音 138****6672（有效）"),
        SimpleNamespace(id="demo-acc-2", account_label="抖音 152****3301（有效）"),
    ]


def _tasks():
    grant = SimpleNamespace(
        id="demo-grant-1", amount=1, label="月卡名额",
        starts_at=_at(-10, 9), expires_at=_at(20, 9),
    )
    def task(tid, target, message, send_time, enabled, recipients, binding=None):
        return SimpleNamespace(
            id=tid, target_name=target, message_template=message,
            send_time=send_time, enabled=enabled, recipients=recipients,
            quota_binding=binding,
        )
    return [
        task(
            "demo-task-1", "阿哲", "早安，今天也要元气满满呀 ☀️", "08:30", True,
            [SimpleNamespace(target_name="阿哲", message_template="早安，今天也要元气满满呀 ☀️")],
            SimpleNamespace(grant=grant),
        ),
        task(
            "demo-task-2", "小雨", "午安～记得好好吃饭，别总吃外卖 🍚", "12:10", True,
            [SimpleNamespace(target_name="小雨", message_template="午安～记得好好吃饭，别总吃外卖 🍚")],
        ),
        task(
            "demo-task-3", "老王团队 · 张姐", "张姐晚上好，本周的合作进展整理好啦，明早发您 🌙", "21:45", False,
            [SimpleNamespace(target_name="老王团队 · 张姐", message_template="张姐晚上好，本周的合作进展整理好啦，明早发您 🌙")],
        ),
    ]


def _runs():
    owner = SimpleNamespace(id="demo-user", username="预览访客", role="user")
    account = SimpleNamespace(display_name="主号 · 138****6672")
    def run(rid, status, stage, offset, hour, error_code=None, error_summary=None):
        scheduled = _at(offset, hour)
        return SimpleNamespace(
            id=rid, status=status, stage=stage, scheduled_for=scheduled,
            started_at=scheduled + timedelta(minutes=1),
            finished_at=scheduled + timedelta(minutes=3),
            error_code=error_code, error_summary=error_summary,
        )
    run1 = run("demo-run-1", "success", "complete", 0, 8)
    run2 = run("demo-run-2", "partial", "batch_retry", -1, 12)
    run3 = run(
        "demo-run-3", "failed", "worker_timeout", -2, 22,
        error_code="TIMEOUT", error_summary="执行超时，已自动安排安全重试",
    )
    task_by_run = {
        "demo-run-1": SimpleNamespace(id="demo-task-1", target_name="阿哲"),
        "demo-run-2": SimpleNamespace(id="demo-task-2", target_name="小雨"),
        "demo-run-3": SimpleNamespace(id="demo-task-3", target_name="老王团队 · 张姐"),
    }
    rows = [
        (run1, task_by_run[run1.id], owner, account),
        (run2, task_by_run[run2.id], owner, account),
        (run3, task_by_run[run3.id], owner, account),
    ]
    recipient_results = {
        "demo-run-2": [
            SimpleNamespace(position=1, target_name="小雨", status="success", stage="page_confirmed",
                            message_template="午安～记得好好吃饭 🍚", error_summary=None),
            SimpleNamespace(position=2, target_name="小柯", status="submitted", stage="submitted",
                            message_template="午安，下午的会议资料我放云盘啦 ☁️", error_summary=None),
            SimpleNamespace(position=3, target_name="Vivi", status="failed", stage="late",
                            message_template="Vivi 晚点聊～", error_summary="超过发送窗口，已安排重试"),
        ],
    }
    return {
        "runs": rows,
        "pending_retry_runs": {"demo-run-2", "demo-run-3"},
        "message_contents": {},
        "recipient_results": recipient_results,
        "run_diagnostics": {},
        "run_page": {"page": 1, "pages": 1, "total": 3, "has_previous": False, "has_next": False},
        "show_run_owner": False,
    }


def context_for(page: str) -> dict:
    context = _common()
    if page == "dashboard":
        context.update(
            title="今日续火",
            tasks=_tasks()[:3],
            accounts=_accounts(),
            runs=[row[0] for row in _runs()["runs"]],
            platform_status=_platform_status(),
        )
    elif page == "tasks":
        context.update(
            title="续火任务",
            tasks=_tasks(),
            accounts=_accounts(),
            task_usage=3,
            task_limit=5,
            quota_summary={
                "active_usage": 3,
                "limit": 5,
                "max_saved_tasks": 5,
                "saved_usage": 3,
                "grants": [SimpleNamespace(grant=_tasks()[0].quota_binding.grant, status="active")],
            },
            task_error=None,
        )
    elif page == "runs":
        context.update(title="执行记录", **_runs())
    return context
