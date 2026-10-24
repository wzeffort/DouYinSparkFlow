from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select, update
from spark_console.models import NotificationEvent, TaskQuotaGrant, User
from spark_console.referral_models import Referral
from spark_console.renewal_models import RenewalOrder, RenewalPromotionClaim
from spark_console.services import ValidationError
from spark_console.services.task_capacity import TaskCapacityService


PLANS = {
    'month': ('月卡', 30, 688, 888),
    'two_months': ('两个月卡', 60, 1288, 1776),
    'quarter': ('季卡', 90, 1800, 2664),
    'year': ('年卡', 365, 8000, 10656),
    'year_friend': ('好友优惠年卡', 365, 5808, 10656),
}
CHANNELS = {'wechat': '微信', 'alipay': '支付宝'}


class RenewalSubmissionLimited(ValidationError):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


class RenewalService:
    def __init__(self, session, audit, notifications=None):
        self.session, self.audit, self.notifications = session, audit, notifications

    def referral_count(self, user_id):
        return self.session.scalar(select(func.count(Referral.friend_id)).where(
            Referral.inviter_id == user_id, Referral.rewarded_at.is_not(None))) or 0

    def catalog(self, user_id):
        count = self.referral_count(user_id)
        annual_bought = bool(self.session.scalar(select(RenewalOrder.id).where(
            RenewalOrder.user_id == user_id, RenewalOrder.plan_key.in_(['year','year_friend']),
            RenewalOrder.status == 'approved').limit(1)))
        claims = {c.promotion:c for c in self.session.scalars(select(RenewalPromotionClaim).where(
            RenewalPromotionClaim.user_id == user_id))}
        first_claim = claims.get('first_year')
        first_available = not annual_bought and not (first_claim and first_claim.used_at)
        result = []
        for key,(label,days,price,original) in PLANS.items():
            promotion, resume, eligible, reason, badge = None, None, True, '', ''
            if key == 'year':
                if count >= 1:
                    price, badge = 6600, '好友价'
                    reason = '已成功邀请至少 1 位好友，后续年卡仍享 66 元'
                elif first_available:
                    price, promotion, badge = 6600, 'first_year', '首张年卡'
                    reason = '首次购买年卡 66 元；后续无有效好友时为 80 元'
                else:
                    badge = '续费价'
                    reason = '成功邀请 1 位好友后，可恢复 66 元年卡价'
            elif key == 'year_friend':
                promotion, badge = 'friend_year', '每人限一次'
                eligible = count >= 2
                reason = '成功邀请 2 位好友，66 元年卡享 8.8 折，仅限一次'
            elif key == 'quarter':
                badge = '推荐'
            claim = claims.get(promotion) if promotion else None
            if claim:
                eligible = False
                if claim.used_at:
                    reason = '这次好友年卡优惠已使用；后续可按 66 元购买年卡'
                else:
                    resume = claim.order_id
                    reason = '优惠已为待处理订单预留，请继续原订单，避免重复付款'
            result.append(dict(key=key,label=label,days=days,price_cents=price,
                price=f'{price/100:.2f}',original=f'{original/100:.2f}',
                original_months={'month':1,'two_months':2,'quarter':3,'year':12,'year_friend':12}[key],
                saving=f'{(original-price)/100:.2f}',eligible=eligible,reason=reason,badge=badge,
                promotion=promotion,resume_order_id=resume))
        return result

    def _target(self, user_id, grant_id):
        grant = self.session.get(TaskQuotaGrant, grant_id)
        if (not grant or grant.user_id != user_id or grant.revoked_at is not None
                or grant.amount != 1 or grant.expires_at is None):
            raise ValidationError('只能续期自己的单个限时名额；整组额度请联系管理员')
        return grant

    def create(self, user, plan_key, channel, grant_id=None):
        # SQLite acquires its write lock before eligibility is read. This serializes
        # reservations across processes, rather than relying on the Web-only lock.
        self.session.execute(update(User).where(User.id == user.id).values(status=User.status))
        self.session.refresh(user)
        if user.status != 'active' or user.role == 'admin':
            raise ValidationError('当前账号不能购买任务卡')
        if plan_key not in PLANS or channel not in CHANNELS:
            raise ValidationError('请选择有效套餐和支付方式')
        if grant_id: self._target(user.id, grant_id)
        offer = next(p for p in self.catalog(user.id) if p['key'] == plan_key)
        if offer['resume_order_id']:
            return self.owned(user,offer['resume_order_id'])
        if not offer['eligible']:
            raise ValidationError('好友年卡优惠仅限一次，需至少 2 位好友完成邀请奖励核验')
        label, days, price = offer['label'],offer['days'],offer['price_cents']
        # Repeated button clicks reuse the same unpaid order and fixed price.
        existing = self.session.scalar(select(RenewalOrder).where(
            RenewalOrder.user_id == user.id, RenewalOrder.plan_key == plan_key,
            RenewalOrder.channel == channel, RenewalOrder.target_grant_id == grant_id,
            RenewalOrder.price_cents == price,
            RenewalOrder.status == 'pending').order_by(RenewalOrder.created_at.desc()).limit(1))
        if existing: return existing
        if (self.session.scalar(select(func.count(RenewalOrder.id)).where(
                RenewalOrder.user_id == user.id, RenewalOrder.status.in_(['pending', 'submitted']))) or 0) >= 10:
            raise ValidationError('待处理订单较多，请联系 QQ 718868371')
        order = RenewalOrder(user_id=user.id, plan_key=plan_key, days=days, price_cents=price,
                             channel=channel, target_grant_id=grant_id)
        self.session.add(order); self.session.flush()
        if offer['promotion']:
            self.session.add(RenewalPromotionClaim(user_id=user.id,
                promotion=offer['promotion'],order_id=order.id))
            self.session.flush()
        self.audit.write(user.id, 'renewal.created', 'renewal_order', order.id)
        return order

    def owned(self, user, order_id):
        order = self.session.get(RenewalOrder, order_id)
        if not order or order.user_id != user.id: raise ValidationError('订单不存在')
        return order

    def cancel(self, user, order_id):
        self.session.execute(update(User).where(User.id == user.id).values(status=User.status))
        self.session.refresh(user)
        order = self.owned(user, order_id)
        if order.status == 'cancelled': return order
        if user.status != 'active' or user.role == 'admin':
            raise ValidationError('当前账号不能取消续卡订单')
        if order.status != 'pending':
            raise ValidationError('付款声明已提交或订单已处理，不能自行取消。请联系 QQ 718868371 核对。')
        changed = self.session.execute(update(RenewalOrder).where(
            RenewalOrder.id == order.id, RenewalOrder.status == 'pending').values(status='cancelled'))
        if not changed.rowcount: raise ValidationError('订单状态已变更，请刷新页面')
        for claim in self.session.scalars(select(RenewalPromotionClaim).where(
                RenewalPromotionClaim.order_id == order.id, RenewalPromotionClaim.used_at.is_(None))):
            self.session.delete(claim)
        self.session.flush()
        self.audit.write(user.id, 'renewal.cancelled', 'renewal_order', order.id)
        return order

    def submit(self, user, order_id, *, now=None):
        # Acquire the shared SQLite write lock before checking any counters.
        # Limits survive restarts and apply across Web processes and accounts.
        self.session.execute(update(User).where(User.id == user.id).values(status=User.status))
        self.session.refresh(user)
        order = self.owned(user, order_id)
        if order.status != 'pending': return order
        if user.status != 'active' or user.role == 'admin':
            raise ValidationError('当前账号不能提交付款声明')
        current = now or datetime.now(timezone.utc)
        self._check_submission_limits(user.id, current)
        changed = self.session.execute(update(RenewalOrder).where(
            RenewalOrder.id == order.id, RenewalOrder.status == 'pending').values(
                status='submitted', submitted_at=current))
        if changed.rowcount == 0: return order
        self.audit.write(user.id, 'renewal.payment_reported', 'renewal_order', order.id)
        if self.notifications:
            if self._mail_budget_available(current):
                self.notifications.enqueue_template(None, 'renewal_submitted', '2010039681@qq.com',
                    'renewal_submitted', dict(username=user.username, order_id=order.id,
                        plan=PLANS[order.plan_key][0], amount=f'{order.price_cents/100:.2f}',
                        channel=CHANNELS[order.channel], action_path='/admin/renewals?order='+order.id),
                    'renewal-submitted:'+order.id, now=current)
            else:
                # Keep genuine payment reports available for manual review, but
                # never build an email backlog that an attacker can drain later.
                self.audit.write(user.id, 'renewal.email_throttled', 'renewal_order', order.id)
        return order

    def _check_submission_limits(self, user_id, current):
        rejected = self.session.scalar(select(func.count(RenewalOrder.id)).where(
            RenewalOrder.user_id == user_id, RenewalOrder.status == 'rejected',
            RenewalOrder.reviewed_at > current-timedelta(days=7))) or 0
        if rejected >= 3:
            raise RenewalSubmissionLimited(
                '近期多笔付款声明未通过核实，在线提交已暂停。已付款请联系 QQ 718868371 核对。', 604800)
        recent = select(func.count(RenewalOrder.id)).where(RenewalOrder.user_id == user_id)
        if (self.session.scalar(recent.where(
                RenewalOrder.submitted_at > current-timedelta(hours=24))) or 0) >= 3:
            raise RenewalSubmissionLimited(
                '24 小时内最多提交 3 笔付款声明，请稍后再试。已付款请联系 QQ 718868371。', 86400)
        if (self.session.scalar(recent.where(
                RenewalOrder.submitted_at > current-timedelta(minutes=5))) or 0) >= 2:
            raise RenewalSubmissionLimited(
                '付款声明已提交，请等待核实。5 分钟内最多提交 2 笔，请勿重复操作。', 300)

    def _mail_budget_available(self, current):
        recent = select(func.count(NotificationEvent.id)).where(
            NotificationEvent.kind == 'renewal_submitted')
        return ((self.session.scalar(recent.where(
                    NotificationEvent.created_at > current-timedelta(minutes=10))) or 0) < 5
                and (self.session.scalar(recent.where(
                    NotificationEvent.created_at > current-timedelta(hours=24))) or 0) < 30)

    def approve(self, admin, order_id):
        if admin.role != 'admin': raise ValidationError('需要管理员核实到账')
        changed = self.session.execute(update(RenewalOrder).where(
            RenewalOrder.id == order_id, RenewalOrder.status == 'submitted').values(status='fulfilling'))
        order = self.session.get(RenewalOrder, order_id)
        if changed.rowcount == 0:
            if order and order.status == 'approved': return order
            raise ValidationError('订单尚未提交付款确认或已处理')
        user = self.session.get(User, order.user_id)
        if not user or user.status != 'active': raise ValidationError('用户已停用，请先核实')
        now = datetime.now(timezone.utc)
        capacity = TaskCapacityService(self.session, self.audit)
        if order.target_grant_id:
            grant = self._target(order.user_id, order.target_grant_id)
            end = grant.expires_at.replace(tzinfo=timezone.utc) if not grant.expires_at.tzinfo else grant.expires_at
            capacity.update_grant(admin.id, grant.id, 1, grant.starts_at,
                                  max(now, end) + timedelta(days=order.days), grant.label)
        else:
            grant = capacity.grant(admin.id, order.user_id, 1, now,
                                   now+timedelta(days=order.days), PLANS[order.plan_key][0])
        order.fulfilled_grant_id, order.status = grant.id, 'approved'
        order.reviewer_id, order.reviewed_at = admin.id, now
        for claim in self.session.scalars(select(RenewalPromotionClaim).where(
                RenewalPromotionClaim.order_id == order.id)):
            claim.used_at = now
        self.session.flush()
        self.audit.write(admin.id, 'renewal.approved', 'renewal_order', order.id)
        return order

    def reject(self, admin, order_id):
        if admin.role != 'admin': raise ValidationError('需要管理员权限')
        changed = self.session.execute(update(RenewalOrder).where(
            RenewalOrder.id == order_id, RenewalOrder.status == 'submitted').values(
                status='rejected', reviewer_id=admin.id, reviewed_at=datetime.now(timezone.utc)))
        if not changed.rowcount: raise ValidationError('订单不存在或已处理')
        for claim in self.session.scalars(select(RenewalPromotionClaim).where(
                RenewalPromotionClaim.order_id == order_id, RenewalPromotionClaim.used_at.is_(None))):
            self.session.delete(claim)
        self.audit.write(admin.id, 'renewal.rejected', 'renewal_order', order_id)
