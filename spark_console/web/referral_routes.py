from urllib.parse import urlencode
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from spark_console.db import session_scope
from spark_console.models import InviteCode, User, utc_now
from spark_console.referral_models import Referral, ReferralCode, ReferralOutcome
from spark_console.services import ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.invites import InviteService
from spark_console.services.referrals import ReferralService, aware


def build_referral_router(engine, auth, pii, cipher, page, public_base_url):
    router = APIRouter()

    def render(request, db, user, context, error=None):
        ReferralService(db, pii).review_bindings(user)
        service = InviteService(db, AuditService(db), cipher)
        rows = db.scalars(select(InviteCode).join(ReferralCode,
            ReferralCode.invite_id == InviteCode.id).where(
                ReferralCode.inviter_id == user.id).order_by(InviteCode.created_at.desc()).limit(20))
        invitations = []
        for row in rows:
            status = ("已使用" if row.used_at else "已撤销" if row.revoked_at else
                      "已过期" if aware(row.expires_at) <= utc_now() else "可分享")
            code = service.reveal(row.id) if status == "可分享" else None
            invitations.append({"status": status, "expires": row.expires_at,
                "code": code, "link": public_base_url + "/register?" + urlencode({"invite": code}) if code else None})
        registered = db.scalar(select(func.count()).select_from(Referral).where(Referral.inviter_id == user.id))
        history_pages = max(1, (registered + 24) // 25)
        try:
            history_page = min(max(1, int(request.query_params.get("page", "1"))), history_pages)
        except ValueError:
            history_page = 1
        history = db.execute(select(Referral, ReferralOutcome, User.username)
            .outerjoin(ReferralOutcome, ReferralOutcome.friend_id == Referral.friend_id)
            .outerjoin(User, User.id == Referral.friend_id).where(Referral.inviter_id == user.id)
            .order_by(Referral.created_at.desc(), Referral.friend_id)
            .offset((history_page - 1) * 25).limit(25)).all()
        count = db.scalar(select(func.count()).select_from(Referral).where(
            Referral.inviter_id == user.id, Referral.rewarded_at.is_not(None)))
        incoming = db.get(Referral, user.id)
        return page(request, "referrals.html", 400 if error else 200, title="邀请好友",
                    invitations=invitations, history=history, count=count,
                    registered=registered, history_page=history_page, history_pages=history_pages,
                    referral_email_verified=bool(ReferralService(db, pii).verified_email_hash(user)), incoming=incoming,
                    douyin_state=ReferralService(db, pii).douyin_state(user.id),
                    generated_count=ReferralService(db, pii).generated_count(user.id),
                    next_reward=ReferralService.reward_tier(count),
                    reward_total=ReferralService(db, pii).reward_total(user.id),
                    reward_remaining=ReferralService(db, pii).remaining_reward(user.id),
                    error=error, **context)

    @router.get("/invitations")
    def index(request: Request):
        with session_scope(engine) as db:
            user, _, context = auth.user_context(request, db)
            return render(request, db, user, context)

    @router.get("/admin/referrals")
    def admin_report(request: Request):
        from spark_console.services.referral_reports import report
        with session_scope(engine) as db:
            _, _, context = auth.admin_context(request, db)
            try:
                number = int(request.query_params.get("page", "1"))
            except ValueError:
                number = 1
            data = report(db, q=request.query_params.get("q", "").strip()[:64],
                status=request.query_params.get("status", ""),
                period=request.query_params.get("period", ""), page_number=number)
            return page(request, "admin_referrals.html", title="邀请奖励日志", **data, **context)

    @router.post("/invitations")
    def create(request: Request, csrf_token: str = Form(default="")):
        with auth.mutation_lock, session_scope(engine) as db:
            user, record, context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                with db.begin_nested():
                    ReferralService(db, pii, cipher).create(user)
            except ValidationError as exc:
                return render(request, db, user, context, str(exc))
        return RedirectResponse("/invitations", 303)

    return router
