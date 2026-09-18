import unittest
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spark_console.db import create_schema
from spark_console.models import DouyinAccount, NotificationEvent, TaskQuotaGrant, User
from spark_console.services.announcements import AnnouncementService, AnnouncementValidation


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite:///:memory:');create_schema(self.engine);self.db=Session(self.engine)
        self.admin=User(username='admin-a',password_hash='x',role='admin');self.ok=User(username='ok-user',password_hash='x')
        self.unbound=User(username='no-email',password_hash='x');self.invalid=User(username='bad-douyin',password_hash='x')
        self.db.add_all([self.admin,self.ok,self.unbound,self.invalid]);self.db.flush()
        self.ok.email_ciphertext=b'x';self.ok.email_verified_at=datetime.now(timezone.utc)
        self.invalid.email_ciphertext=b'x';self.invalid.email_verified_at=datetime.now(timezone.utc)
        self.db.add(DouyinAccount(owner_user_id=self.invalid.id,display_name='bad',encrypted_cookies=b'x',cookie_nonce=b'n',validation_state='invalid'))
        now=datetime(2026,9,6,tzinfo=timezone.utc)
        self.db.add(TaskQuotaGrant(user_id=self.ok.id,amount=1,starts_at=now-timedelta(days=1),expires_at=now+timedelta(days=3),label='soon'))
        self.db.commit();self.now=now

    def tearDown(self):self.db.close();self.engine.dispose()

    def test_three_audiences_are_snapshotted_and_never_enqueue_email(self):
        service=AnnouncementService(self.db)
        all_draft=service.preview(self.admin.id,'all','',[], '全部','正文',self.now)
        invalid=service.preview(self.admin.id,'status','account_invalid',[], '失效','正文',self.now)
        manual=service.preview(self.admin.id,'manual','',[self.ok.id,self.unbound.id], '指定','正文',self.now)
        self.assertEqual(3,len(all_draft.recipients));self.assertEqual([self.invalid.id],[r.user_id for r in invalid.recipients])
        self.assertEqual({self.ok.id,self.unbound.id},{r.user_id for r in manual.recipients})
        self.assertEqual(0,self.db.query(NotificationEvent).count())

    def test_confirm_due_withdraw_and_read_are_owner_scoped(self):
        service=AnnouncementService(self.db)
        draft=service.preview(self.admin.id,'manual','',[self.ok.id],'提醒','<b>纯文本</b>',self.now)
        service.confirm(self.admin.id,draft.id,self.now+timedelta(hours=1),self.now)
        self.assertEqual([],service.for_user(self.ok.id,self.now))
        service.publish_due(self.now+timedelta(hours=2));items=service.for_user(self.ok.id,self.now+timedelta(hours=2))
        self.assertEqual('<b>纯文本</b>',items[0][0].body)
        with self.assertRaises(AnnouncementValidation):service.mark_read(self.unbound.id,draft.id,self.now)
        service.mark_read(self.ok.id,draft.id,self.now);self.assertIsNotNone(draft.recipients[0].read_at)
        service.withdraw(self.admin.id,draft.id,self.now);self.assertEqual([],service.for_user(self.ok.id,self.now+timedelta(hours=2)))

    def test_status_filters_and_limits(self):
        service=AnnouncementService(self.db)
        self.assertEqual([self.unbound.id],service.audience('status','email_unverified',[],self.now))
        self.assertEqual([self.ok.id],service.audience('status','quota_expiring',[],self.now))
        with self.assertRaises(AnnouncementValidation):service.preview(self.admin.id,'manual','',[],'空','正文',self.now)

    def test_preview_is_session_bound_expires_and_confirmation_replays(self):
        service=AnnouncementService(self.db)
        draft=service.preview(self.admin.id,'manual','',[self.ok.id],'标题','正文',self.now,preview_session_hash='session-a')
        with self.assertRaises(AnnouncementValidation):service.confirm(self.admin.id,draft.id,None,self.now,preview_session_hash='session-b')
        with self.assertRaises(AnnouncementValidation):service.confirm(self.admin.id,draft.id,None,self.now+timedelta(minutes=16),preview_session_hash='session-a')
        draft=service.preview(self.admin.id,'manual','',[self.ok.id],'标题2','正文',self.now,preview_session_hash='session-a')
        first=service.confirm(self.admin.id,draft.id,None,self.now,preview_session_hash='session-a')
        second=service.confirm(self.admin.id,draft.id,None,self.now,preview_session_hash='session-a')
        self.assertEqual(first.id,second.id);self.assertEqual('published',second.status)

    def test_quota_expired_includes_user_without_any_active_grant(self):
        self.db.add(TaskQuotaGrant(user_id=self.unbound.id,amount=0,starts_at=self.now-timedelta(days=1),expires_at=None,label='zero-initial'))
        self.db.flush()
        self.assertIn(self.unbound.id,AnnouncementService(self.db).audience('status','quota_expired',[],self.now))

    def test_withdrawn_scheduled_announcement_is_not_published_later(self):
        service=AnnouncementService(self.db)
        draft=service.preview(self.admin.id,'manual','',[self.ok.id],'已取消','正文',self.now)
        service.confirm(self.admin.id,draft.id,self.now+timedelta(hours=1),self.now)
        service.withdraw(self.admin.id,draft.id,self.now+timedelta(minutes=5))
        self.assertEqual(0,service.publish_due(self.now+timedelta(hours=2)))
        self.assertEqual('withdrawn',draft.status)
        self.assertEqual([],service.for_user(self.ok.id,self.now+timedelta(hours=2)))
