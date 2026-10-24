from datetime import timedelta, timezone
from sqlalchemy import func, select, update
from spark_console.models import AppSetting, DouyinAccount, InviteCode, SparkTask, TaskQuotaGrant, User, utc_now
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
        if self.db.get(AppSetting, "referral_email_seeded_v2"):
            return
        for user in self.db.scalars(select(User).where(User.email_verified_at.is_not(None)).order_by(User.created_at, User.id)):
            key = self.verified_email_hash(user)
            if key and self.db.get(VerifiedQQ, key) is None:
                self.db.add(VerifiedQQ(email_hash=key, first_user_id=user.id))
        self.db.add(AppSetting(key="referral_email_seeded_v2", value="true"))

    def verified_email_hash(self, user):
        if not user.email_verified_at or not user.email_ciphertext or not user.email_nonce or not self.pii:
            return None
        email = self.pii.decrypt_email(user.email_ciphertext, user.email_nonce,
                                      aad=f"user:{user.id}".encode()).strip().lower()
        # Keep the existing durable identity ledger, including earlier QQ rewards.
        # Verification admits all mail providers; deleting/rebinding never clears it.
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

        key = self.verified_email_hash(user)
        if not key:
            state("pending", "email_pending")
            return
        identity = self.db.get(VerifiedQQ, key)
        if identity is None:
            identity = VerifiedQQ(email_hash=key, first_user_id=user.id)
            self.db.add(identity)
            self.db.flush()
        if referral and referral.rewarded_at:
            return
        if identity.first_user_id != user.id:
            state("blocked", "email_reused")
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
        inviter_key = self.verified_email_hash(inviter)
        if not inviter_key:
            state('pending', 'inviter_email_pending')
            return
        inviter_identity = self.db.get(VerifiedQQ, inviter_key)
        if inviter_identity is None:
            self.db.add(VerifiedQQ(email_hash=inviter_key, first_user_id=inviter.id))
            self.db.flush()
        elif inviter_identity.first_user_id != inviter.id:
            state('blocked', 'inviter_email_reused')
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
        def credit_stage(stage, requested_days):
            sides = ('inviter', 'friend') if stage == 'registered' else ('active_inviter', 'active_friend')
            receipts = [self.db.get(ReferralReceipt, (user.id, side)) for side in sides]
            if all(receipts):
                return
            if any(receipts):
                raise RuntimeError('Incomplete referral stage receipts')
            awarded = []
            for person, grant, recipient in zip((inviter, user), grants, sides):
                extra = min(requested_days, self.remaining_reward(person.id))
                before = grant.expires_at
                if extra and before is not None:
                    grant.expires_at = max(aware(current), aware(before), aware(grant.starts_at)) + timedelta(days=extra)
                self.db.add(ReferralReceipt(friend_id=user.id, recipient=recipient,
                    user_id=person.id, grant_id=grant.id, days=extra, expires_before=before,
                    expires_after=grant.expires_at, unlimited=before is None, created_at=current))
                awarded.append(extra)
            self.db.flush()
            referral.inviter_days += awarded[0]
            referral.friend_days += awarded[1]
            AuditService(self.db).write(user.id, 'referral.stage_rewarded', 'user', user.id,
                detail=f'stage={stage};inviter_days={awarded[0]};friend_days={awarded[1]}')

        credit_stage('registered', 5)
        reason = self.douyin_state(user.id)
        if reason:
            state('pending' if reason == 'douyin_pending' else 'blocked', reason)
            return
        tasks = self.db.scalars(select(SparkTask)
            .join(DouyinAccount, DouyinAccount.id == SparkTask.douyin_account_id)
            .join(AccountReferralIdentity, AccountReferralIdentity.account_id == DouyinAccount.id)
            .join(VerifiedDouyin, VerifiedDouyin.platform_user_id == AccountReferralIdentity.platform_user_id)
            .where(SparkTask.owner_user_id == user.id, SparkTask.enabled.is_(True),
                DouyinAccount.owner_user_id == user.id, DouyinAccount.validation_state == 'valid',
                VerifiedDouyin.first_user_id == user.id)).all()
        capacity = TaskCapacityService(self.db, AuditService(self.db))
        if not any(capacity.task_authorized(task, current) for task in tasks):
            state('pending', 'task_pending')
            return
        completed = self.db.scalar(select(func.count()).select_from(Referral).where(
            Referral.inviter_id == inviter.id, Referral.rewarded_at.is_not(None))) or 0
        activation_days = self.reward_tier(completed) - 5
        credit_stage('activated', activation_days)
        state("credited", "credited")
        referral.rewarded_at = current
        self.db.flush()
        AuditService(self.db).write(user.id, "referral.rewarded", "user", user.id,
            detail=f"registered=5;activation_tier={activation_days};inviter_days={referral.inviter_days};friend_days={referral.friend_days}")

    def generated_count(self, user_id):
        return self.db.scalar(select(func.count()).select_from(ReferralCode).where(
            ReferralCode.inviter_id == user_id)) or 0

    def create(self, user):
        self.lock()
        if user.status != "active" or user.role != "user":
            raise ValidationError("仅普通用户可领取邀请奖励")
        key = self.verified_email_hash(user)
        if not key:
            raise ValidationError("请先在邮箱设置完成邮箱验证，支持任意有效邮箱")
        if self.generated_count(user.id) >= 3 and self.douyin_state(user.id):
            raise ValidationError("生成第 4 个及后续邀请码前，请先扫码核验未被其他用户使用的抖音账号")
        self.verified(user)
        if self.db.get(VerifiedQQ, key).first_user_id != user.id:
            raise ValidationError("该邮箱已参与过邀请活动，请勿重复注册")
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
