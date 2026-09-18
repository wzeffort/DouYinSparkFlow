from datetime import datetime,timezone,timedelta
from math import ceil
from sqlalchemy import func,select
from spark_console.models import DouyinAccount,EmailVerificationRequest,SparkTask,TaskQuotaGrant,User,utc_now
from spark_console.services.quota_projection import active_positive_grants

def aware(v):return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
class UserDirectoryService:
 def __init__(self,db):self.db=db
 def rows(self,status_filter='',query='',now=None):
  current=aware(now or utc_now());users=list(self.db.scalars(select(User).order_by(User.created_at,User.username)))
  if query:users=[u for u in users if query.lower() in u.username.lower()]
  accounts=list(self.db.scalars(select(DouyinAccount)));grants=list(self.db.scalars(select(TaskQuotaGrant)));tasks=list(self.db.scalars(select(SparkTask)));pending={x.user_id for x in self.db.scalars(select(EmailVerificationRequest).where(EmailVerificationRequest.purpose=='bind',EmailVerificationRequest.consumed_at.is_(None),EmailVerificationRequest.expires_at>current))}
  result=[]
  for user in users:
   mine=[a for a in accounts if a.owner_user_id==user.id];valid=active_positive_grants([g for g in grants if g.user_id==user.id],current)
   invalid=any(a.validation_state=='invalid' for a in mine);verified=max((aware(a.last_verified_at) for a in mine if a.last_verified_at),default=None)
   if invalid:dlabel='已失效'
   elif mine and any(a.validation_state=='valid' for a in mine):dlabel='正常'
   else:dlabel='待核验'
   if user.email_verified_at:elabel='已验证'
   elif user.id in pending:elabel='待验证'
   else:elabel='未绑定'
   expiries=[aware(g.expires_at) for g in valid if g.expires_at];permanent=any(g.expires_at is None for g in valid)
   end=min(expiries) if expiries else None;days=max(1,ceil((end-current).total_seconds()/86400)) if end else None
   mine_tasks=[t for t in tasks if t.owner_user_id==user.id]
   row={'user':user,'email_label':elabel,'douyin_label':dlabel,'last_verified_at':verified,'quota_limit':sum(g.amount for g in valid),'quota_expires_at':end,'quota_days':days,'quota_permanent':permanent,'active_tasks':sum(t.enabled for t in mine_tasks),'saved_tasks':len(mine_tasks)}
   match=not status_filter or (status_filter=='account_invalid' and invalid) or (status_filter=='email_unverified' and elabel!='已验证') or (status_filter=='quota_expiring' and end and end<=current+timedelta(days=7)) or (status_filter=='quota_expired' and user.role!='admin' and not valid) or (status_filter=='disabled' and user.status!='active')
   if match:result.append(row)
  return result
