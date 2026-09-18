import json
import unittest
import re
from html import unescape
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from spark_console.models import User, SparkTask, TaskQuotaGrant, TaskRun, TaskRunRecipient
from spark_console.db import session_scope
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.crypto import CookieCipher
from tests.console.test_web_user import UserWebTests


class BatchWebTests(unittest.TestCase):
    setUp = UserWebTests.setUp
    tearDown = UserWebTests.tearDown
    login = UserWebTests.login
    def prepare(self, admin=False):
        with session_scope(self.engine) as db:
            user = db.scalar(select(User))
            user.must_change_password = False
            if admin:
                user.role = 'admin'
            account = AccountService(db, CookieCipher(b'c'*32), AuditService(db)).create(
                user.id, 'fixture', b'[{"name":"fixture","value":"test-only"}]')
            account_id = account.id
        self.login()
        response = self.client.get('/tasks')
        csrf = response.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        return account_id, csrf

    def test_batch_form_posts_and_edits_individual_messages(self):
        account, csrf = self.prepare()
        recipients = [dict(target_name='A', message_template='给A的内容'), dict(target_name='B', message_template='给B的内容')]
        response = self.client.post('/tasks', data=dict(csrf_token=csrf, account_id=account,
            send_time='09:00', recipients_json=json.dumps(recipients)), follow_redirects=False)
        self.assertEqual(303, response.status_code)
        with session_scope(self.engine) as db:
            task_id = db.scalar(select(SparkTask.id))
        page = self.client.get(f'/tasks/{task_id}/edit')
        self.assertIn('给B的内容', page.text)
        self.assertIn('recipients_json', page.text)
        recipients[1]['message_template'] = '修改后的B'
        response = self.client.post(f'/tasks/{task_id}/edit', data=dict(csrf_token=csrf, account_id=account,
            send_time='09:00', recipients_json=json.dumps(recipients)), follow_redirects=False)
        self.assertEqual(303, response.status_code)
        self.assertIn('修改后的B', self.client.get(f'/tasks/{task_id}/edit').text)

    def test_admin_creates_exactly_two_independent_monthly_slots(self):
        _, csrf = self.prepare(admin=True)
        before = datetime.now(timezone.utc)
        response = self.client.post('/admin/users', data=dict(csrf_token=csrf, username='monthly-user', monthly_slots='2'))
        self.assertEqual(200, response.status_code)
        with session_scope(self.engine) as db:
            user = db.scalar(select(User).where(User.username == 'monthly-user'))
            grants = db.scalars(select(TaskQuotaGrant).where(TaskQuotaGrant.user_id == user.id,
                TaskQuotaGrant.amount > 0, TaskQuotaGrant.revoked_at.is_(None))).all()
            self.assertEqual([1, 1], [g.amount for g in grants])
            for grant in grants:
                self.assertGreaterEqual(grant.expires_at.replace(tzinfo=timezone.utc), before + timedelta(days=30))
            url = f'/admin/users/{user.id}/quota'
        self.assertIn('续期 30 天', self.client.get(url).text)

    def test_invalid_batch_retains_content_without_saving(self):
        account, csrf = self.prepare()
        items = [dict(target_name=f'A{i}', message_template='需要保留') for i in range(6)]
        response = self.client.post('/tasks', data=dict(csrf_token=csrf, account_id=account,
            send_time='09:00', recipients_json=json.dumps(items)))
        self.assertEqual(400, response.status_code)
        encoded = re.search(r'name="recipients_json" value="([^"]*)"', response.text).group(1)
        self.assertEqual(items, json.loads(unescape(encoded)))
        with session_scope(self.engine) as db:
            self.assertIsNone(db.scalar(select(SparkTask.id)))

    def test_admin_cannot_delete_a_running_batch(self):
        account, csrf = self.prepare(admin=True)
        self.client.post('/tasks', data=dict(csrf_token=csrf, account_id=account, send_time='09:00',
            recipients_json=json.dumps([dict(target_name='A', message_template='hello')])) )
        with session_scope(self.engine) as db:
            task_id = db.scalar(select(SparkTask.id))
            db.add(TaskRun(task_id=task_id, scheduled_for=datetime.now(timezone.utc), status='running'))
        response = self.client.post(f'/admin/tasks/{task_id}/delete', data=dict(csrf_token=csrf), follow_redirects=False)
        self.assertEqual(400, response.status_code)
        with session_scope(self.engine) as db:
            self.assertIsNotNone(db.get(SparkTask, task_id))


    def test_admin_cannot_delete_user_with_running_batch(self):
        account, csrf = self.prepare(admin=True)
        self.client.post('/tasks', data=dict(csrf_token=csrf, account_id=account, send_time='09:00',
            recipients_json=json.dumps([dict(target_name='A', message_template='hello')])))
        with session_scope(self.engine) as db:
            owner = User(username='busy-owner', password_hash='fixture', role='user', status='active')
            db.add(owner)
            db.flush()
            owner_id = owner.id
            task = db.scalar(select(SparkTask))
            task.owner_user_id = owner_id
            run = TaskRun(task_id=task.id, scheduled_for=datetime.now(timezone.utc), status='running')
            db.add(run)
            db.flush()
            run_id = run.id
            db.add(TaskRunRecipient(run_id=run_id, position=0, account_id=account,
                target_name='A', target_sec_uid='', message_template='hello', status='sending'))
        response = self.client.post(f'/admin/users/{owner_id}/delete',
            data=dict(csrf_token=csrf, confirmation='busy-owner'), follow_redirects=False)
        self.assertEqual(400, response.status_code)
        with session_scope(self.engine) as db:
            self.assertIsNotNone(db.get(User, owner_id))
            self.assertIsNotNone(db.get(TaskRun, run_id))
            self.assertEqual('sending', db.get(TaskRunRecipient, (run_id, 0)).status)


del UserWebTests
