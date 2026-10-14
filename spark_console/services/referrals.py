import re
from datetime import timedelta, timezone
from sqlalchemy import func, select, update
from spark_console.models import AppSetting, DouyinAccount, InviteCode, TaskQuotaGrant, User, utc_now
from spark_console.referral_models import (
    AccountReferralIdentity, VerifiedDouyin,
    Referral, ReferralCode, ReferralLock, ReferralOutcome, ReferralReceipt, VerifiedQQ,
)
from spark_console.services import ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.invites import InviteService
from spark_console.services.task_capacity import TaskCapacityService


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ReferralService:
    def __init__(self, db, pii, cipher=None):
        self.db, self.pii, self.cipher = db, pii, cipher

    def lock(self):
        # Database serialization, including across multiple web processes.
        result = self.db.execute(update(ReferralLock).where(ReferralLock.id == 1).values(
            version=ReferralLock.version + 1))
        if result.rowcount != 1:
            raise RuntimeError("Referral schema has not been initialized")

    def seed_existing(self):
        self.lock()
        if self.db.get(AppSetting, "referral_qq_seeded_v1"):
            return
        for user in self.db.scalars(select(User).where(User.email_verified_at.is_not(None))):
            key = self.qq_hash(user)
            if key and self.db.get(VerifiedQQ, key) is None:
                self.db.add(VerifiedQQ(email_hash=key, first_user_id=user.id))
        self.db.add(AppSetting(key="referral_qq_seeded_v1", value="true"))

    def qq_hash(self, user):
        if not user.email_verified_at or not user.email_ciphertext or not self.pii:
            return None
        email = self.pii.decrypt_email(user.email_ciphertext, user.email_nonce,
                                      aad=f"user:{user.id}".encode()).strip().lower()
        if not re.fullmatch(r"[1-9][0-9]{4,11}@qq\.com", email):
            return None
        return self.pii.lookup_hash(email)

    def douyin_state(self, user_id):
        identities = self.db.execute(select(VerifiedDouyin.first_user_id)
            .join(AccountReferralIdentity, AccountReferralIdentity.platform_user_id == VerifiedDouyin.platform_user_id)
            .join(DouyinAccount, DouyinAccount.id == AccountReferralIdentity.account_id)
            .where(DouyinAccount.owner_user_id == user_id,
                   DouyinAccount.validation_state == "valid")).scalars().all()
        if not identities:
            return "douyin_pending"
        if any(owner != user_id for owner in identities):
            return "douyin_reused"
        return None

    @staticmethod
    def reward_tier(successful_count):
        return min(30, 9 + successful_count if successful_count < 3 else 14 + (successful_count - 3) * 3)

    def reward_total(self, user_id):
        # Receipts survive deletion/rebinding and include both sides and old awards.
        return self.db.scalar(select(func.coalesce(func.sum(ReferralReceipt.days), 0)).where(
            ReferralReceipt.user_id == user_id)) or 0

    def remaining_reward(self, user_id):
        return max(0, 90 - self.reward_total(user_id))

    def verified(self, user, now=None):
        """Called only after successful email verification, in the same transaction."""
        self.lock()
        referral = self.db.get(Referral, user.id)
        current = now or utc_now()
        outcome = self.db.get(ReferralOutcome, user.id) if referral else None
        def state(status, reason):
            if outcome is not None and not referral.rewarded_at:
                outcome.status, outcome.reason, outcome.checked_at = status, reason, current

        key = self.qq_hash(user)
        if not key:
            state("pending", "qq_pending")
            return
        identity = self.db.get(VerifiedQQ, key)
        if identity is None:
            identity = VerifiedQQ(email_hash=key, first_user_id=user.id)
            self.db.add(identity)
            self.db.flush()
        if referral and referral.rewarded_at:
            return
        if identity.first_user_id != user.id:
            state("blocked", "qq_reused")
            return
        if not referral:
            return
        if referral.inviter_id == user.id:
            state("blocked", "self_referral")
            return
        inviter = self.db.get(User, referral.inviter_id)
        if not inviter or inviter.status != "active" or user.status != "active":
            state("blocked", "account_unavailable")
            return
        for person in (user, inviter):
            reason = self.douyin_state(person.id)
            if reason:
                state("pending" if reason == "douyin_pending" else "blocked",
                      reason if person.id == user.id else "inviter_" + reason)
                return
        grants = []
        for person in (inviter, user):
            TaskCapacityService(self.db, AuditService(self.db)).bootstrap_user(person)
            grant = self.db.scalar(select(TaskQuotaGrant).where(
                TaskQuotaGrant.user_id == person.id, TaskQuotaGrant.is_initial.is_(True)))
            if not grant or grant.revoked_at or grant.amount <= 0:
                state("blocked", "inviter_quota_unavailable" if person.id == inviter.id else "friend_quota_unavailable")
                return
            grants.append(grant)
        count = self.db.scalar(select(func.count()).select_from(Referral).where(
            Referral.inviter_id == inviter.id, Referral.rewarded_at.is_not(None)))
        tier = self.reward_tier(count)
        days = min(tier, self.remaining_reward(inviter.id))
        friend_days = min(tier, self.remaining_reward(user.id))
        for grant, extra, recipient in zip(grants, (days, friend_days), ("inviter", "friend")):
            before = grant.expires_at
            if extra and grant.expires_at is not None:
                grant.expires_at = max(aware(current), aware(grant.expires_at),
                                       aware(grant.starts_at)) + timedelta(days=extra)
            self.db.add(ReferralReceipt(friend_id=user.id, recipient=recipient,
                user_id=grant.user_id, grant_id=grant.id, days=extra, expires_before=before,
                expires_after=grant.expires_at, unlimited=before is None, created_at=current))
        state("credited", "credited")
        referral.rewarded_at = current
        referral.inviter_days, referral.friend_days = days, friend_days
        self.db.flush()
        AuditService(self.db).write(user.id, "referral.rewarded", "user", user.id,
                                    detail=f"tier={tier};inviter_days={days};friend_days={friend_days}")

    def generated_count(self, user_id):
        return self.db.scalar(select(func.count()).select_from(ReferralCode).where(
            ReferralCode.inviter_id == user_id)) or 0

    def create(self, user):
        self.lock()
        if user.status != "active" or user.role != "user":
            raise ValidationError("仅普通用户可领取邀请奖励")
        key = self.qq_hash(user)
        if not key:
            raise ValidationError("请先在邮箱设置验证数字 QQ 账号@qq.com 邮箱")
        if self.generated_count(user.id) >= 3 and self.douyin_state(user.id):
            raise ValidationError("生成第 4 个及后续邀请码前，请先扫码核验未被其他用户使用的抖音账号")
        self.verified(user)
        if self.db.get(VerifiedQQ, key).first_user_id != user.id:
            raise ValidationError("该 QQ 已参与过邀请活动，请勿重复注册")
        now = utc_now()
        recent = self.db.scalar(select(func.count()).select_from(ReferralCode).join(
            InviteCode, InviteCode.id == ReferralCode.invite_id).where(
                ReferralCode.inviter_id == user.id, InviteCode.created_at > now - timedelta(days=1)))
        if recent >= 20:
            raise ValidationError("每天最多生成 20 个邀请码，请明天再试")
        invite, code = InviteService(self.db, AuditService(self.db), self.cipher).create(user.id)
        self.db.add(ReferralCode(invite_id=invite.id, inviter_id=user.id))
        return invite, code

    def review_bindings(self, user):
        self.verified(user)
        friends = list(self.db.scalars(select(User).join(Referral, Referral.friend_id == User.id)
            .where(Referral.inviter_id == user.id, Referral.rewarded_at.is_(None))))
        for friend in friends:
            self.verified(friend)
