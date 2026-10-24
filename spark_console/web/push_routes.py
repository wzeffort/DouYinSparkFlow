from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from spark_console.db import session_scope
from spark_console.push_models import PushChannel, PushDelivery
from spark_console.services.audits import AuditService
from spark_console.services.push_notifications import CHANNELS, ERROR_LABELS, STATUS_LABELS, PushService


def build_push_router(engine, auth, pii, page):
    router = APIRouter()

    def render(request, db, user, context, error=None):
        channels = {row.channel: row for row in db.scalars(
            select(PushChannel).where(PushChannel.user_id == user.id))}
        options = {}
        for name, row in channels.items():
            try:
                options[name] = PushService(db, pii).credentials_for(row).get("option", "")
            except (InvalidTag, ValueError, KeyError):
                options[name] = ""
        deliveries = db.execute(select(PushDelivery, PushChannel.channel).join(PushChannel).where(
            PushChannel.user_id == user.id).order_by(PushDelivery.created_at.desc()).limit(20)).all()
        return page(request, "push_settings.html", 400 if error else 200,
                    title="微信与 QQ 通知", channels=channels, channel_labels=CHANNELS,
                    options=options, deliveries=deliveries, statuses=STATUS_LABELS,
                    errors=ERROR_LABELS, error=error, notice=request.query_params.get("notice"), **context)

    @router.get("/settings/push")
    def settings(request: Request):
        with session_scope(engine) as db:
            user, _record, context = auth.user_context(request, db)
            return render(request, db, user, context)

    @router.post("/settings/push/{channel}")
    def save(request: Request, channel: str, csrf_token: str = Form(default=""),
             token: str = Form(default=""), option: str = Form(default=""),
             enabled: str = Form(default=""), task_success: str = Form(default=""),
             task_failure: str = Form(default=""), login_expired: str = Form(default="")):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record, context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                row = PushService(db, pii).save(user.id, channel, token, option, bool(enabled),
                    bool(task_success), bool(task_failure), bool(login_expired))
            except (ValueError, InvalidTag):
                return render(request, db, user, context, "保存失败，请检查 Token 和渠道编码；首次配置必须填写 Token。")
            AuditService(db).write(user.id, "push.configured", "push_channel", row.id)
        return RedirectResponse("/settings/push?notice=saved", 303)

    @router.post("/settings/push/{channel}/test")
    def test(request: Request, channel: str, csrf_token: str = Form(default="")):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record, context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                PushService(db, pii).queue_test(user.id, channel)
            except ValueError as exc:
                return render(request, db, user, context, str(exc))
        return RedirectResponse("/settings/push?notice=queued", 303)

    @router.post("/settings/push/{channel}/delete")
    def remove(request: Request, channel: str, csrf_token: str = Form(default="")):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record, _context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            row = db.scalar(select(PushChannel).where(
                PushChannel.user_id == user.id, PushChannel.channel == channel))
            if row:
                db.execute(delete(PushDelivery).where(PushDelivery.channel_id == row.id))
                db.delete(row)
                AuditService(db).write(user.id, "push.deleted", "push_channel", row.id)
        return RedirectResponse("/settings/push?notice=deleted", 303)

    return router
