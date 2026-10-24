from __future__ import annotations

import json
import re
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from spark_console.models import DouyinAccount, SparkTask, TaskRun, TaskRunRecipient, User, utc_now, uuid_string
from spark_console.push_models import PushChannel, PushDelivery
from spark_console.services.notifications import _aware


CHANNELS = {"wechat": "微信服务号", "qq": "QQ"}
STATUS_LABELS = {
    "pending": "等待发送", "sending": "正在提交", "accepted": "平台已受理",
    "failed": "发送失败", "unknown": "结果待核实", "cancelled": "已取消",
}
ERROR_LABELS = {
    "push_code_900": "PushPlus 限制发送，请到平台处理后重新保存配置",
    "push_code_903": "Token 无效，请更新配置",
    "push_code_905": "请先在 PushPlus 完成实名认证",
    "push_code_888": "PushPlus 积分不足",
    "push_connect": "连接失败，正在按计划重试",
    "push_unknown": "请求结果不确定，请在 PushPlus 消息记录中核实；不会自动重发",
    "push_expired": "通知已超过 24 小时，已停止发送",
    "push_credentials": "凭证无法解密，请重新保存 Token",
}


class PushService:
    def __init__(self, db, pii):
        self.db, self.pii = db, pii

    def credentials_for(self, channel):
        return json.loads(self.pii.decrypt_bytes(
            channel.credentials, channel.nonce, aad=f"push-channel:{channel.id}".encode()))

    def save(self, user_id, channel, token, option, enabled, success, failure, expired, now=None):
        current = now or utc_now()
        if channel not in CHANNELS:
            raise ValueError("请选择微信服务号或 QQ")
        row = self.db.scalar(select(PushChannel).where(
            PushChannel.user_id == user_id, PushChannel.channel == channel))
        token, option = token.strip(), option.strip()
        if not token and row:
            token = self.credentials_for(row)["token"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
            raise ValueError("请输入有效的 PushPlus Token")
        if option and not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", option):
            raise ValueError("渠道编码只能包含字母、数字、点、横线和下划线")
        if row is None:
            row = PushChannel(id=uuid_string(), user_id=user_id, channel=channel)
            self.db.add(row)
        else:
            # Queued messages must not silently move to a newly configured recipient.
            self.db.execute(update(PushDelivery).where(
                PushDelivery.channel_id == row.id, PushDelivery.status == "pending"
            ).values(status="cancelled", payload=None, nonce=None))
        row.credentials, row.nonce = self.pii.encrypt_bytes(
            json.dumps({"token": token, "option": option}).encode(),
            aad=f"push-channel:{row.id}".encode())
        row.enabled = enabled
        row.task_success, row.task_failure, row.login_expired = success, failure, expired
        row.enabled_since, row.blocked_code = current, None
        self.db.flush()
        return row

    def enqueue(self, channel, kind, key, title, content, source_id=None, now=None):
        current = now or utc_now()
        key = f"{channel.id}:{key}"
        if self.db.scalar(select(PushDelivery.id).where(PushDelivery.dedupe_key == key)):
            return None
        event = PushDelivery(id=uuid_string(), channel_id=channel.id, kind=kind,
                             dedupe_key=key, source_id=source_id, created_at=current,
                             next_attempt_at=current, status="pending", attempts=0)
        event.payload, event.nonce = self.pii.encrypt_bytes(
            json.dumps({"title": title, "content": content}, ensure_ascii=False).encode(),
            aad=f"push-delivery:{event.id}".encode())
        try:
            with self.db.begin_nested():
                self.db.add(event)
                self.db.flush()
        except IntegrityError:
            return None
        return event

    def queue_test(self, user_id, channel, now=None):
        current = now or utc_now()
        row = self.db.scalar(select(PushChannel).where(
            PushChannel.user_id == user_id, PushChannel.channel == channel))
        if row is None or not row.enabled or row.blocked_code:
            raise ValueError("请先保存并启用通知配置；如有平台错误，请处理后重新保存")
        cutoff = current - timedelta(seconds=60)
        if row.last_test_at and _aware(row.last_test_at) > cutoff:
            raise ValueError("测试过于频繁，请等待 60 秒后重试")
        row.last_test_at = current
        return self.enqueue(row, "test", f"test:{uuid_string()}", "火花守护通知测试",
                            "微信 / QQ 通知配置测试。收到此消息说明该渠道可以接收通知。", now=current)

    def collect(self, now):
        # No payload retention after completion; keep only 30 days of delivery metadata.
        self.db.execute(delete(PushDelivery).where(
            PushDelivery.created_at < now - timedelta(days=30),
            PushDelivery.status.not_in(["pending", "sending"])))
        channels = self.db.scalars(select(PushChannel).join(User).where(
            PushChannel.enabled.is_(True), PushChannel.blocked_code.is_(None),
            User.status == "active")).all()
        for channel in channels:
            since = max(_aware(channel.enabled_since), now - timedelta(days=1))
            statuses = []
            if channel.task_success:
                statuses.extend(["success", "submitted"])
            if channel.task_failure:
                statuses.extend(["failed", "partial"])
            # Correlated anti-join and limit avoid scanning all historical runs in Python.
            seen = select(PushDelivery.id).where(
                PushDelivery.channel_id == channel.id, PushDelivery.kind == "task",
                PushDelivery.source_id == TaskRun.id).exists()
            runs = self.db.execute(select(TaskRun, SparkTask).join(
                SparkTask, TaskRun.task_id == SparkTask.id).where(
                    SparkTask.owner_user_id == channel.user_id, TaskRun.finished_at >= since,
                    TaskRun.finished_at <= now, TaskRun.status.in_(statuses), ~seen
                ).order_by(TaskRun.finished_at, TaskRun.id).limit(100)).all()
            for run, task in runs:
                label = {"success": "执行成功", "submitted": "已提交，待确认",
                         "failed": "执行失败", "partial": "部分完成"}[run.status]
                if run.status == "success" and (run.stage == "submitted" or self.db.scalar(
                    select(TaskRunRecipient.run_id).where(TaskRunRecipient.run_id == run.id,
                                                         TaskRunRecipient.status == "submitted").limit(1))):
                    label = "已执行，部分结果待确认"
                title = f"续火任务{label}"
                # Do not forward user message text, raw exceptions, cookies or credentials.
                content = f"目标：{task.target_name}\n结果：{label}\n请登录火花守护查看执行记录。"
                self.enqueue(channel, "task", f"run:{run.id}", title, content, run.id, now)
            if channel.login_expired:
                accounts = self.db.scalars(select(DouyinAccount).where(
                    DouyinAccount.owner_user_id == channel.user_id,
                    DouyinAccount.validation_state == "invalid",
                    DouyinAccount.invalidated_at >= channel.enabled_since)).all()
                for account in accounts:
                    incident = account.auth_incident_id or str(account.invalidated_at)
                    self.enqueue(channel, "login", f"login:{account.id}:{incident}",
                                 "抖音登录已失效，请重新绑定",
                                 f"账号：{account.display_name}\n请登录火花守护重新绑定抖音账号。",
                                 account.id, now)

    def claim(self, now):
        # A process may die after provider acceptance. Preserve uncertainty, not duplicates.
        self.db.execute(update(PushDelivery).where(
            PushDelivery.status == "sending", PushDelivery.claimed_at < now - timedelta(minutes=5)
        ).values(status="unknown", error_code="push_unknown", payload=None, nonce=None))
        candidates = self.db.scalars(select(PushDelivery).where(
            PushDelivery.status == "pending", PushDelivery.next_attempt_at <= now
        ).order_by(PushDelivery.next_attempt_at, PushDelivery.id).limit(100)).all()
        for event in candidates:
            row = self.db.get(PushChannel, event.channel_id)
            user = self.db.get(User, row.user_id) if row else None
            allowed = row and row.enabled and not row.blocked_code and user and user.status == "active"
            if allowed and event.kind == "login":
                account = self.db.get(DouyinAccount, event.source_id)
                allowed = (account and account.owner_user_id == user.id
                           and account.validation_state == "invalid"
                           and str(account.auth_incident_id or account.invalidated_at) in event.dedupe_key)
            if allowed and event.kind == "task":
                run = self.db.get(TaskRun, event.source_id)
                task = self.db.get(SparkTask, run.task_id) if run else None
                allowed = task and task.owner_user_id == user.id
            if not allowed or _aware(event.created_at) < now - timedelta(days=1):
                if allowed:
                    event.error_code = "push_expired"
                event.status, event.payload, event.nonce = "cancelled", None, None
                continue
            recent = self.db.scalar(select(PushChannel.id).where(
                PushChannel.user_id == row.user_id,
                PushChannel.last_attempt_at > now - timedelta(seconds=30)))
            if recent:
                continue
            changed = self.db.execute(update(PushDelivery).where(
                PushDelivery.id == event.id, PushDelivery.status == "pending"
            ).values(status="sending", claimed_at=now, attempts=PushDelivery.attempts + 1))
            if changed.rowcount != 1:
                continue
            row.last_attempt_at = now
            self.db.flush()
            return event, row
        return None

    def complete(self, event_id, result, now):
        event = self.db.get(PushDelivery, event_id)
        if not event or event.status != "sending":
            return
        event.error_code = result.error_code
        if result.success:
            event.status, event.provider_id = "accepted", result.provider_id
        elif result.error_code == "push_unknown":
            event.status = "unknown"
        elif result.retryable and event.attempts < 4:
            event.status = "pending"
            event.next_attempt_at = now + timedelta(seconds=(60, 300, 1200)[event.attempts - 1])
        else:
            event.status = "failed"
        if result.error_code in {"push_code_900", "push_code_903", "push_code_905", "push_code_888"}:
            channel = self.db.get(PushChannel, event.channel_id)
            channel.blocked_code = result.error_code
            if result.error_code == "push_code_900":
                self.db.execute(update(PushChannel).where(PushChannel.user_id == channel.user_id)
                                .values(blocked_code=result.error_code))
        if event.status != "pending":
            event.payload, event.nonce = None, None
