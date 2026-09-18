import unittest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from spark_console.db import session_scope
from spark_console.models import AdminAnnouncement, AnnouncementRecipient, NotificationEvent, User, UserNotification
from tests.console.test_batch_web import BatchWebTests
_prepare = BatchWebTests.prepare

class AnnouncementWebTests(unittest.TestCase):
 setUp=BatchWebTests.setUp;tearDown=BatchWebTests.tearDown;login=BatchWebTests.login
 def prepare(self):return _prepare(self,admin=True)
 def test_admin_previews_confirms_and_never_enqueues_email(self):
  _,csrf=self.prepare()
  with session_scope(self.engine) as db:
   target=User(username='notice-target',password_hash='x');db.add(target);db.flush();target_id=target.id
  preview=self.client.post('/admin/announcements/preview',data={'csrf_token':csrf,'title':'维护提醒','body':'<b>今晚维护</b>','audience_type':'manual','selected_ids':target_id})
  self.assertEqual(200,preview.status_code);self.assertIn('确认发布',preview.text);self.assertIn('&lt;b&gt;今晚维护&lt;/b&gt;',preview.text)
  with session_scope(self.engine) as db:draft=db.scalar(select(AdminAnnouncement).where(AdminAnnouncement.title=='维护提醒'))
  response=self.client.post(f'/admin/announcements/{draft.id}/confirm',data={'csrf_token':csrf},follow_redirects=False)
  self.assertEqual(303,response.status_code)
  with session_scope(self.engine) as db:
   self.assertEqual('published',db.get(AdminAnnouncement,draft.id).status);self.assertEqual(1,db.query(AnnouncementRecipient).count());self.assertEqual(0,db.query(NotificationEvent).count())
 def test_user_directory_supports_status_filter(self):
  _,_=self.prepare()
  response=self.client.get('/admin/users?status=email_unverified')
  self.assertEqual(200,response.status_code);self.assertIn('邮箱未验证',response.text);self.assertIn('抖音状态',response.text)
 def test_announcement_form_has_conditional_audience_controls(self):
  _,_=self.prepare();page=self.client.get('/admin/announcements')
  self.assertIn('data-announcement-form',page.text);self.assertIn('data-audience-panel="manual"',page.text);self.assertIn('announcements.js',page.text)
  self.assertIn('data-user-filter',page.text);self.assertIn('data-selected-count',page.text)
 def test_legacy_notifications_redirects_to_unified_messages(self):
  _,_=self.prepare();response=self.client.get('/notifications',follow_redirects=False)
  self.assertEqual(303,response.status_code);self.assertEqual('/messages',response.headers['location'])
 def test_messages_merge_platform_and_system_items_by_time(self):
  _,_=self.prepare();newer=datetime(2026,9,6,12,tzinfo=timezone.utc);older=newer-timedelta(hours=1)
  with session_scope(self.engine) as db:
   user=db.scalar(select(User));announcement=AdminAnnouncement(created_by_user_id=None,title='较新的平台公告',body='公告正文',audience_type='manual',status='published',published_at=newer,created_at=older)
   db.add(announcement);db.flush();db.add(AnnouncementRecipient(announcement_id=announcement.id,user_id=user.id))
   db.add(UserNotification(user_id=user.id,kind='account',title='较早的系统提醒',summary='提醒正文',dedupe_key='merged-order',created_at=older))
  response=self.client.get('/messages')
  self.assertEqual(200,response.status_code);self.assertLess(response.text.index('较新的平台公告'),response.text.index('较早的系统提醒'))

del BatchWebTests
