from datetime import timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import exists, or_, select
from spark_console.models import DouyinAccount, TaskQuotaGrant, User, utc_now
from spark_console.reward_models import QuotaGiftReceipt
from spark_console.services.audits import AuditService
from spark_console.services.notifications import NotificationService
from spark_console.services.referrals import ReferralService, aware


class ExpiredQuotaGiftService:
    """Operator-invoked, once-per-campaign gift; never resumes paused tasks."""
    def __init__(self, session, pii):
        self.db, self.pii = session, pii

    def _eligible_query(self, now):
        bound = exists(select(DouyinAccount.id).where(DouyinAccount.owner_user_id == User.id))
        active = exists(select(TaskQuotaGrant.id).where(TaskQuotaGrant.user_id == User.id,
            TaskQuotaGrant.revoked_at.is_(None), TaskQuotaGrant.amount > 0, TaskQuotaGrant.starts_at <= now,
            or_(TaskQuotaGrant.expires_at.is_(None), TaskQuotaGrant.expires_at > now)))
        expired_base = exists(select(TaskQuotaGrant.id).where(TaskQuotaGrant.user_id == User.id,
            TaskQuotaGrant.is_initial.is_(True), TaskQuotaGrant.amount > 0,
            TaskQuotaGrant.revoked_at.is_(None), TaskQuotaGrant.expires_at <= now))
        return select(User).where(User.status == 'active', User.role == 'user', bound, expired_base, ~active,
            User.email_verified_at.is_not(None), User.email_ciphertext.is_not(None), User.email_nonce.is_not(None))

    def eligible_users(self, now=None):
        return list(self.db.scalars(self._eligible_query(aware(now or utc_now())).order_by(User.id)))

    def grant(self, user, campaign, now=None):
        if not campaign or len(campaign) > 64:
            raise ValueError('Invalid quota gift campaign')
        current = aware(now or utc_now())
        ReferralService(self.db, self.pii).lock()
        if self.db.get(QuotaGiftReceipt, (campaign, user.id)):
            return None
        if self.db.scalar(self._eligible_query(current).where(User.id == user.id)) is None:
            return None
        self.db.refresh(user)
        grant = self.db.scalar(select(TaskQuotaGrant).where(TaskQuotaGrant.user_id == user.id,
            TaskQuotaGrant.is_initial.is_(True)))
        self.db.refresh(grant)
        # Decode before touching quota. A bad address aborts the enclosing transaction.
        address = self.pii.decrypt_email(user.email_ciphertext, user.email_nonce, aad=f'user:{user.id}'.encode())
        before = grant.expires_at
        grant.expires_at = current + timedelta(days=14)
        audit = AuditService(self.db)
        notices = NotificationService(self.db, self.pii, audit)
        dedupe = f'quota-gift:{campaign}:{user.id}'
        event = notices.enqueue_template(user.id, 'quota_gift', address, 'quota_gift',
            {'username':user.username, 'days':'14', 'slots':str(grant.amount),
             'expires_at':grant.expires_at.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S（上海时间）'),
             'action_path':'/tasks'}, dedupe, now=current)
        notices.create_in_app(user.id, 'quota_gift', '14 天基础额度已赠送',
            '原有基础额度已从现在起续期 14 天，名额数量不变。已暂停的任务请检查后手动启用。', '/tasks', dedupe)
        receipt = QuotaGiftReceipt(campaign=campaign, user_id=user.id, grant_id=grant.id, days=14,
            expires_before=before, expires_after=grant.expires_at, email_event_id=event.id, created_at=current)
        self.db.add(receipt)
        audit.write(None, 'quota.gift_issued', 'task_quota_grant', grant.id, detail=f'campaign={campaign};days=14')
        self.db.flush()
        return receipt
