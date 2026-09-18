"""Session-bound, server-issued single-use operation keys; same transaction as mutation."""
from datetime import datetime,timedelta,timezone
import hashlib,json,secrets
from sqlalchemy import delete,update
from fastapi import HTTPException
from spark_console.models import AdminOperation,User


class AdminOperations:
    def __init__(self,db,record):
        self.db,self.record=db,record
        actor=db.get(User,record.user_id)
        if actor is None or actor.role!='admin' or actor.status!='active':raise HTTPException(404)

    def issue(self,scope):
        now=datetime.now(timezone.utc)
        self.db.execute(delete(AdminOperation).where(AdminOperation.payload_hash.is_(None),AdminOperation.created_at<now-timedelta(hours=1)))
        raw=secrets.token_urlsafe(32)
        self.db.add(AdminOperation(token_hash=hashlib.sha256(raw.encode()).hexdigest(),session_id=self.record.id,scope=scope,created_at=now))
        return raw

    def execute(self,raw,scope,payload,action):
        with self.db.begin_nested():
            return self._execute(raw,scope,payload,action)

    def _execute(self,raw,scope,payload,action):
        if not raw or len(raw)!=43:raise HTTPException(403,'操作凭据无效，请刷新页面后重试')
        key=hashlib.sha256(raw.encode()).hexdigest()
        row=self.db.get(AdminOperation,key)
        if row is None or row.session_id!=self.record.id or row.scope!=scope:raise HTTPException(403,'操作凭据无效，请刷新页面后重试')
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
        if row.payload_hash is not None:
            if row.payload_hash!=digest:raise HTTPException(409,'此操作已提交，请刷新页面查看结果')
            return False
        now=datetime.now(timezone.utc)
        created=row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
        if created<now-timedelta(hours=1):raise HTTPException(403,'页面已过期，请刷新后重试')
        claimed=self.db.execute(update(AdminOperation).where(AdminOperation.token_hash==key,AdminOperation.payload_hash.is_(None)).values(payload_hash=digest,used_at=now)).rowcount
        if claimed!=1:
            self.db.refresh(row)
            if row.payload_hash!=digest:raise HTTPException(409,'此操作已提交，请刷新页面查看结果')
            return False
        action()
        return True
