from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import aliased

from spark_console.models import User, utc_now
from spark_console.referral_models import Referral, ReferralOutcome, ReferralReceipt


STATUS_LABELS = {"pending": "待完成核验", "blocked": "未发放", "credited": "已入账"}
REASON_LABELS = {
    "douyin_pending": "好友尚未扫码核验抖音账号，或登录已失效",
    "inviter_douyin_pending": "邀请人尚未扫码核验抖音账号，或登录已失效",
    "douyin_reused": "好友绑定的抖音账号已被其他用户核验使用",
    "inviter_douyin_reused": "邀请人绑定的抖音账号已被其他用户核验使用",
    "qq_pending": "尚未验证数字 QQ 邮箱", "qq_reused": "该 QQ 已被其他账号使用，未发奖励",
    "self_referral": "不能邀请自己", "account_unavailable": "账号已停用或删除",
    "inviter_quota_unavailable": "邀请人的基础额度不存在、已撤销或数量为零",
    "friend_quota_unavailable": "好友的基础额度不存在、已撤销或数量为零",
    "credited": "双方奖励在同一事务中入账",
}


def report(db, *, q="", status="", period="", page_number=1, now=None):
    local = (now or utc_now()).astimezone(ZoneInfo("Asia/Shanghai"))
    start = datetime.combine(local.date(), time.min, ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    registered_today = (Referral.created_at >= start) & (Referral.created_at < end)
    paid_today = (Referral.rewarded_at >= start) & (Referral.rewarded_at < end)
    paid = Referral.rewarded_at.is_not(None)
    totals = db.execute(select(func.count(), func.sum(case((paid, 1), else_=0)),
        func.sum(case((registered_today, 1), else_=0)), func.sum(case((paid_today, 1), else_=0)))
        .select_from(Referral)).one()
    ranking = db.execute(select(Referral.inviter_id, User.username,
        func.max(ReferralOutcome.inviter_name).label("saved_name"), func.count().label("registrations"),
        func.sum(case((paid, 1), else_=0)).label("successful"),
        func.sum(case((registered_today, 1), else_=0)).label("today_registered"),
        func.sum(case((paid_today, 1), else_=0)).label("today_paid"),
        func.sum(Referral.inviter_days).label("days"))
        .outerjoin(User, User.id == Referral.inviter_id)
        .outerjoin(ReferralOutcome, ReferralOutcome.friend_id == Referral.friend_id)
        .group_by(Referral.inviter_id, User.username)
        .order_by(func.sum(case((paid, 1), else_=0)).desc(), func.count().desc(), Referral.inviter_id)
        .limit(10)).all()
    inviter, friend = aliased(User), aliased(User)
    query = select(Referral, ReferralOutcome, inviter.username, friend.username).outerjoin(
        ReferralOutcome, ReferralOutcome.friend_id == Referral.friend_id).outerjoin(
        inviter, inviter.id == Referral.inviter_id).outerjoin(friend, friend.id == Referral.friend_id)
    if q:
        query = query.where(or_(inviter.username.contains(q, autoescape=True),
            friend.username.contains(q, autoescape=True),
            ReferralOutcome.inviter_name.contains(q, autoescape=True),
            ReferralOutcome.friend_name.contains(q, autoescape=True)))
    if status in STATUS_LABELS:
        query = query.where(ReferralOutcome.status == status)
    if period == "registered_today": query = query.where(registered_today)
    if period == "paid_today": query = query.where(paid_today)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    pages = max(1, (total + 24) // 25)
    page_number = min(max(1, page_number), pages)
    results = db.execute(query.order_by(Referral.created_at.desc(), Referral.friend_id)
                         .offset((page_number - 1) * 25).limit(25)).all()
    receipts = {}
    if results:
        for receipt in db.scalars(select(ReferralReceipt).where(
                ReferralReceipt.friend_id.in_([row[0].friend_id for row in results]))):
            receipts.setdefault(receipt.friend_id, {})[receipt.recipient] = receipt
    return dict(totals=[value or 0 for value in totals], ranking=ranking, results=results,
        receipts=receipts, total=total, page_number=page_number, pages=pages,
        status_labels=STATUS_LABELS, reason_labels=REASON_LABELS,
        q=q, status=status, period=period, today=local.strftime("%Y-%m-%d"))
