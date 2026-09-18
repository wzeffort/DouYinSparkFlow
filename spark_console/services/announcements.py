from datetime import datetime, timedelta, timezone
from sqlalchemy import select,update
from spark_console.models import (AdminAnnouncement, AnnouncementRecipient, DouyinAccount,
    TaskQuotaGrant, User, utc_now)
from spark_console.services.quota_projection import active_positive_grants

def aware(value): return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

class AnnouncementValidation(ValueError): pass

class AnnouncementService:
    FILTERS={'email_unverified','account_invalid','quota_expiring','quota_expired'}
    def __init__(self,db):self.db=db
    def audience(self,kind,value,selected,now=None):
        current=aware(now or utc_now());users=list(self.db.scalars(select(User).where(User.status=='active',User.role!='admin').order_by(User.username)))
        if kind=='all':result=users
        elif kind=='manual':
            wanted=set(selected);result=[u for u in users if u.id in wanted]
        elif kind=='status' and value in self.FILTERS:
            if value=='email_unverified':result=[u for u in users if not u.email_ciphertext or not u.email_verified_at]
            elif value=='account_invalid':
                bad=set(self.db.scalars(select(DouyinAccount.owner_user_id).where(DouyinAccount.validation_state=='invalid')));result=[u for u in users if u.id in bad]
            else:
                grants=list(self.db.scalars(select(TaskQuotaGrant).where(TaskQuotaGrant.revoked_at.is_(None))))
                active_grants=active_positive_grants(grants,current)
                active={g.user_id for g in active_grants}
                expiring={g.user_id for g in active_grants if g.expires_at and aware(g.expires_at)<=current+timedelta(days=7)}
                result=[u for u in users if u.id in (expiring if value=='quota_expiring' else ({u.id for u in users}-active))]
        else:raise AnnouncementValidation('无效的接收范围')
        ids=[u.id for u in result]
        if not ids:raise AnnouncementValidation('没有符合条件的接收人')
        if len(ids)>500:raise AnnouncementValidation('单次最多发送给 500 人')
        return ids
    def preview(self,actor_id,kind,value,selected,title,body,now=None,preview_session_hash=None):
        current=aware(now or utc_now());actor=self.db.get(User,actor_id);clean_title=title.strip();clean_body=body.strip()
        if actor is None or actor.role!='admin':raise AnnouncementValidation('仅管理员可发布')
        if not 1<=len(clean_title)<=120 or not 1<=len(clean_body)<=4000:raise AnnouncementValidation('标题或正文长度无效')
        row=AdminAnnouncement(created_by_user_id=actor_id,title=clean_title,body=clean_body,audience_type=kind,audience_value=value or None,preview_session_hash=preview_session_hash,created_at=current)
        self.db.add(row);self.db.flush()
        row.recipients=[AnnouncementRecipient(user_id=x) for x in self.audience(kind,value,selected,current)]
        self.db.flush();return row
    def confirm(self,actor_id,announcement_id,scheduled_for=None,now=None,preview_session_hash=None):
        current=aware(now or utc_now());row=self.db.get(AdminAnnouncement,announcement_id)
        if row is None or row.created_by_user_id!=actor_id or (row.preview_session_hash and row.preview_session_hash!=preview_session_hash):raise AnnouncementValidation('预览已失效')
        if row.status in ('published','scheduled'):return row
        if row.status!='draft' or current-aware(row.created_at)>timedelta(minutes=15):raise AnnouncementValidation('预览已失效')
        when=aware(scheduled_for) if scheduled_for else current
        if when>current+timedelta(days=90):raise AnnouncementValidation('最多预约 90 天')
        row.scheduled_for=when
        if when<=current:row.status='published';row.published_at=current
        else:row.status='scheduled'
        return row
    def publish_due(self,now=None):
        current=aware(now or utc_now());result=self.db.execute(update(AdminAnnouncement).where(AdminAnnouncement.status=='scheduled',AdminAnnouncement.scheduled_for<=current).values(status='published',published_at=current));return result.rowcount
    def withdraw(self,actor_id,announcement_id,now=None):
        row=self.db.get(AdminAnnouncement,announcement_id)
        actor=self.db.get(User,actor_id)
        if row is None or actor is None or actor.role!='admin':raise AnnouncementValidation('公告不存在')
        if row.status!='withdrawn':
            self.db.execute(update(AdminAnnouncement).where(AdminAnnouncement.id==announcement_id,AdminAnnouncement.status.in_(('draft','scheduled','published'))).values(status='withdrawn',withdrawn_at=now or utc_now()));self.db.refresh(row)
        return row
    def for_user(self,user_id,now=None):
        self.publish_due(now)
        return list(self.db.execute(select(AdminAnnouncement,AnnouncementRecipient).join(AnnouncementRecipient).where(AnnouncementRecipient.user_id==user_id,AdminAnnouncement.status=='published').order_by(AdminAnnouncement.published_at.desc())).all())
    def mark_read(self,user_id,announcement_id,now=None):
        recipient=self.db.get(AnnouncementRecipient,(announcement_id,user_id));row=self.db.get(AdminAnnouncement,announcement_id)
        if recipient is None or row is None or row.status!='published':raise AnnouncementValidation('消息不存在')
        recipient.read_at=now or utc_now();return recipient
