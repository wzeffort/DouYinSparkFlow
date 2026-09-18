import unittest
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from spark_console.db import create_schema
from spark_console.models import DouyinAccount, TaskQuotaGrant, User
from spark_console.services.user_directory import UserDirectoryService

class UserDirectoryTests(unittest.TestCase):
    def test_status_projection_and_filters(self):
        engine=create_engine('sqlite:///:memory:');create_schema(engine);db=Session(engine);now=datetime(2026,9,6,tzinfo=timezone.utc)
        a=User(username='invalid-user',password_hash='x');b=User(username='unbound-user',password_hash='x');db.add_all([a,b]);db.flush()
        a.email_ciphertext=b'x';a.email_verified_at=now;db.add(DouyinAccount(owner_user_id=a.id,display_name='bad',encrypted_cookies=b'x',cookie_nonce=b'n',validation_state='invalid',last_verified_at=now-timedelta(days=1)))
        db.add(TaskQuotaGrant(user_id=a.id,amount=1,starts_at=now-timedelta(days=1),expires_at=now+timedelta(days=2),label='soon'));db.commit()
        db.add(TaskQuotaGrant(user_id=b.id,amount=0,starts_at=now-timedelta(days=1),expires_at=None,label='zero-initial'));db.commit()
        service=UserDirectoryService(db)
        rows=service.rows('', '', now);first=next(x for x in rows if x['user'].id==a.id)
        self.assertEqual('已失效',first['douyin_label']);self.assertEqual('已验证',first['email_label']);self.assertEqual(2,first['quota_days'])
        self.assertEqual([a.id],[x['user'].id for x in service.rows('account_invalid','',now)])
        self.assertEqual([b.id],[x['user'].id for x in service.rows('email_unverified','',now)])
        self.assertEqual([b.id],[x['user'].id for x in service.rows('quota_expired','',now)])
        db.close();engine.dispose()
