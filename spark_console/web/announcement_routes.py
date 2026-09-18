from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter,Form,HTTPException,Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from spark_console.db import session_scope
from spark_console.models import AdminAnnouncement,User,UserNotification
from spark_console.services.announcements import AnnouncementService,AnnouncementValidation
from spark_console.services.audits import AuditService

FILTER_LABELS={'email_unverified':'邮箱未验证','account_invalid':'抖音账号失效','quota_expiring':'额度 7 天内到期','quota_expired':'额度已到期'}
def build_announcement_router(engine,auth,page):
 router=APIRouter()
 def context(request,db,preview=None):
  admin,_record,base=auth.admin_context(request,db);service=AnnouncementService(db);service.publish_due()
  users=list(db.scalars(select(User).where(User.role!='admin',User.status=='active').order_by(User.username)))
  announcements=list(db.scalars(select(AdminAnnouncement).order_by(AdminAnnouncement.created_at.desc()).limit(50)))
  return admin,dict(users=users,announcements=announcements,preview=preview,filter_labels=FILTER_LABELS,**base)
 @router.get('/admin/announcements')
 def index(request:Request,user_id:str=''):
  with session_scope(engine) as db:
   _,data=context(request,db);data['prefill_user_id']=user_id
   return page(request,'admin_announcements.html',title='公告管理',**data)
 @router.get('/messages')
 def messages(request:Request,page_number:int=1):
  with session_scope(engine) as db:
   user,_record,base=auth.user_context(request,db);service=AnnouncementService(db)
   received=service.for_user(user.id)
   if user.role=='admin':
    sent=list(db.scalars(select(AdminAnnouncement).where(AdminAnnouncement.status!='draft').options(selectinload(AdminAnnouncement.recipients))))
    sent_ids={a.id for a in sent}
    combined=[('sent',a,None,a.published_at or a.created_at) for a in sent]
    combined += [('announcement',a,r,a.published_at) for a,r in received if a.id not in sent_ids]
   else:
    combined=[('announcement',a,r,a.published_at) for a,r in received]
   combined += [('system',n,None,n.created_at) for n in db.scalars(select(UserNotification).where(UserNotification.user_id==user.id)).all()]
   combined.sort(key=lambda x:(aware_time(x[3]),x[1].id),reverse=True);pages=max(1,(len(combined)+7)//8);current=min(max(1,page_number),pages);chunk=combined[(current-1)*8:current*8]
   return page(request,'notifications.html',title='消息中心',message_items=chunk,page_number=current,pages=pages,message_total=len(combined),**base)
 @router.get('/notifications')
 def legacy_notifications():return RedirectResponse('/messages',303)
 @router.post('/messages/system/{notification_id}/read')
 def read_system(request:Request,notification_id:str,csrf_token:str=Form(default='')):
  with session_scope(engine) as db:
   user,record,_=auth.user_context(request,db);auth.csrf(record,csrf_token);notice=db.get(UserNotification,notification_id)
   if notice is None or notice.user_id!=user.id:raise HTTPException(404)
   notice.read_at=datetime.now(timezone.utc)
  return RedirectResponse('/messages#notification-list',303)
 @router.post('/admin/announcements/preview')
 def preview(request:Request,csrf_token:str=Form(default=''),title:str=Form(default=''),body:str=Form(default=''),audience_type:str=Form(default=''),audience_value:str=Form(default=''),selected_ids:list[str]=Form(default=[]),scheduled_for:str=Form(default='')):
  with session_scope(engine) as db:
   admin,record,_=auth.admin_context(request,db);auth.csrf(record,csrf_token);service=AnnouncementService(db)
   try:
    row=service.preview(admin.id,audience_type,audience_value,selected_ids,title,body,preview_session_hash=record.token_hash)
    if scheduled_for:
     row.scheduled_for=datetime.fromisoformat(scheduled_for).replace(tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)
   except (AnnouncementValidation,ValueError):raise HTTPException(400,'公告内容或接收范围无效') from None
   AuditService(db).write(admin.id,'announcement.previewed','admin_announcement',row.id,detail=f'recipients={len(row.recipients)}')
   _,data=context(request,db,row);return page(request,'admin_announcements.html',title='公告预览',**data)
 @router.post('/admin/announcements/{announcement_id}/confirm')
 def confirm(request:Request,announcement_id:str,csrf_token:str=Form(default='')):
  with session_scope(engine) as db:
   admin,record,_=auth.admin_context(request,db);auth.csrf(record,csrf_token);service=AnnouncementService(db);row=db.get(AdminAnnouncement,announcement_id)
   try:service.confirm(admin.id,announcement_id,row.scheduled_for if row else None,preview_session_hash=record.token_hash)
   except AnnouncementValidation:raise HTTPException(409,'预览已失效') from None
   AuditService(db).write(admin.id,'announcement.confirmed','admin_announcement',announcement_id)
  return RedirectResponse('/admin/announcements',303)
 @router.post('/admin/announcements/{announcement_id}/withdraw')
 def withdraw(request:Request,announcement_id:str,csrf_token:str=Form(default='')):
  with session_scope(engine) as db:
   admin,record,_=auth.admin_context(request,db);auth.csrf(record,csrf_token)
   try:AnnouncementService(db).withdraw(admin.id,announcement_id)
   except AnnouncementValidation:raise HTTPException(404) from None
   AuditService(db).write(admin.id,'announcement.withdrawn','admin_announcement',announcement_id)
  return RedirectResponse('/messages#notification-list',303)
 @router.post('/announcements/{announcement_id}/read')
 def read(request:Request,announcement_id:str,csrf_token:str=Form(default='')):
  with session_scope(engine) as db:
   user,record,_=auth.user_context(request,db);auth.csrf(record,csrf_token)
   try:AnnouncementService(db).mark_read(user.id,announcement_id)
   except AnnouncementValidation:raise HTTPException(404) from None
  return RedirectResponse('/messages#notification-list',303)
 return router

def aware_time(value):return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
