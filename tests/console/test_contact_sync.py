import unittest
import asyncio
from datetime import timedelta
from unittest.mock import patch
from sqlalchemy import select
from spark_console.db import session_scope
from spark_console.models import DouyinAccount, User, ContactSyncState, DouyinConversation, utc_now
from spark_console.services.contacts import ContactSyncService
from tests.console.test_batch_web import BatchWebTests


class ContactSyncTests(unittest.TestCase):
    setUp = BatchWebTests.setUp
    tearDown = BatchWebTests.tearDown
    login = BatchWebTests.login
    prepare = BatchWebTests.prepare

    def test_sync_is_explicit_post_idempotent_and_scoped(self):
        account, csrf = self.prepare()
        url = f'/accounts/{account}/contact-sync'
        response = self.client.post(url, data={'csrf_token':csrf})
        self.assertEqual(202, response.status_code)
        self.assertEqual('queued', response.json()['status'])
        again = self.client.post(url, data={'csrf_token':csrf})
        self.assertEqual(response.json()['request_id'], again.json()['request_id'])
        self.assertEqual(response.json()['request_id'], self.client.get(url).json()['request_id'])
        self.assertEqual(404, self.client.post('/accounts/missing/contact-sync', data={'csrf_token':csrf}).status_code)

    def test_sync_requires_csrf_and_ownership(self):
        account, csrf = self.prepare()
        self.assertEqual(403, self.client.post(f'/accounts/{account}/contact-sync').status_code)
        with session_scope(self.engine) as db:
            other = User(username='other-sync',password_hash='fixture',role='user',status='active')
            db.add(other);db.flush()
            db.get(DouyinAccount, account).owner_user_id=other.id
        self.assertEqual(404, self.client.post(f'/accounts/{account}/contact-sync',data={'csrf_token':csrf}).status_code)

    def test_sync_cancel_does_not_change_task_or_login_state(self):
        account, csrf = self.prepare()
        url=f'/accounts/{account}/contact-sync'
        self.assertEqual(202, self.client.post(url,data={'csrf_token':csrf}).status_code)
        response=self.client.post(url+'/cancel',data={'csrf_token':csrf})
        self.assertEqual(200,response.status_code)
        self.assertEqual('cancelled',response.json()['status'])

    def test_cancel_or_rebind_rejects_old_results(self):
        account, csrf = self.prepare()
        self.client.post(f'/accounts/{account}/contact-sync',data={'csrf_token':csrf})
        with session_scope(self.engine) as db:
            claim=ContactSyncService(db).claim()
        with session_scope(self.engine) as db:
            ContactSyncService(db).cancel(account)
        with session_scope(self.engine) as db:
            self.assertFalse(ContactSyncService(db).finish(account,claim[1],['不该保存']))
            self.assertEqual([], list(db.scalars(select(DouyinConversation))))
            row=db.get(ContactSyncState,account)
            row.status='running'; row.slot='global'
            db.get(DouyinAccount,account).cookie_nonce=b'new-binding'
        with session_scope(self.engine) as db:
            ContactSyncService(db).finish(account,claim[1],['不该保存'])
        with session_scope(self.engine) as db:
            self.assertEqual([],list(db.scalars(select(DouyinConversation))))
            self.assertEqual('account_changed',db.get(ContactSyncState,account).error_code)

    def test_worker_merges_live_names_without_creating_send_or_login_jobs(self):
        account, csrf = self.prepare()
        from spark_console.crypto import CookieCipher
        with session_scope(self.engine) as db:
            sealed = CookieCipher(b'c'*32).encrypt(b'[{"name":"fixture","value":"test-only","domain":".douyin.com","path":"/"}]')
            row = db.get(DouyinAccount,account)
            row.encrypted_cookies, row.cookie_nonce = sealed.ciphertext, sealed.nonce
        self.client.post(f'/accounts/{account}/contact-sync',data={'csrf_token':csrf})
        from spark_console import contact_sync
        async def collect(payload, cancelled):
            self.assertFalse(cancelled())
            return (['旧好友','依依妖妖'], ())
        asyncio.run(contact_sync.run_contact_sync(self.settings,self.engine,collect=collect))
        from spark_console.models import TaskRun, DouyinLoginSession, SparkTask
        with session_scope(self.engine) as db:
            self.assertEqual({'旧好友','依依妖妖'},set(db.scalars(select(DouyinConversation.display_name))))
            self.assertEqual('partial',db.get(ContactSyncState,account).status)
            for model in (TaskRun,DouyinLoginSession,SparkTask):
                self.assertEqual([],list(db.scalars(select(model))))

    def test_pending_and_recovery_find_abandoned_sync(self):
        account,csrf=self.prepare()
        self.client.post(f'/accounts/{account}/contact-sync',data={'csrf_token':csrf})
        from spark_console.browser_runtime import has_pending
        with session_scope(self.engine) as db:
            self.assertTrue(has_pending(db,'auth',utc_now()))
            ContactSyncService(db).claim()
        with session_scope(self.engine) as db:
            ContactSyncService(db).recover()
            self.assertEqual('failed',db.get(ContactSyncState,account).status)
            self.assertFalse(has_pending(db,'auth',utc_now()))

    def test_timed_out_status_is_visible_without_browser_resources(self):
        account,csrf=self.prepare()
        self.client.post(f'/accounts/{account}/contact-sync',data={'csrf_token':csrf})
        with session_scope(self.engine) as db:
            row=db.get(ContactSyncState,account)
            row.status='running';row.requested_at=utc_now()-timedelta(minutes=16)
        result=self.client.get(f'/accounts/{account}/contact-sync').json()
        self.assertEqual('failed',result['status'])
        self.assertEqual('expired',result['error_code'])

    def test_late_collector_error_keeps_observed_names_without_claiming_complete(self):
        account,csrf=self.prepare()
        from spark_console.crypto import CookieCipher
        with session_scope(self.engine) as db:
            sealed=CookieCipher(b'c'*32).encrypt(b'[{"name":"fixture","value":"test-only","domain":".douyin.com","path":"/"}]')
            row=db.get(DouyinAccount,account);row.encrypted_cookies,row.cookie_nonce=sealed.ciphertext,sealed.nonce
        self.client.post(f'/accounts/{account}/contact-sync',data={'csrf_token':csrf})
        from spark_console import contact_sync
        async def interrupted(payload,cancelled,observed=None):
            if observed is not None:
                observed['names'].add('已读到的好友')
            raise TimeoutError()
        with patch.object(contact_sync,'collect_live',interrupted):
            asyncio.run(contact_sync.run_contact_sync(self.settings,self.engine))
        with session_scope(self.engine) as db:
            self.assertIsNotNone(db.get(DouyinConversation,(account,'已读到的好友')))
            status=ContactSyncService(db).status(account)
            self.assertEqual('partial',status['status'])
            self.assertEqual('timeout',status['error_code'])
            self.assertFalse(status['complete'])

    def test_global_sync_limit_and_cooldown_are_server_side(self):
        account,csrf=self.prepare()
        from spark_console.crypto import CookieCipher
        from spark_console.services.accounts import AccountService
        from spark_console.services.audits import AuditService
        with session_scope(self.engine) as db:
            owner=db.scalar(select(User.id))
            second=AccountService(db,CookieCipher(b'c'*32),AuditService(db)).create(owner,'second',b'[{"name":"fixture","value":"test"}]').id
        first_url=f'/accounts/{account}/contact-sync'
        second_url=f'/accounts/{second}/contact-sync'
        self.assertEqual(202,self.client.post(first_url,data={'csrf_token':csrf}).status_code)
        self.assertEqual(409,self.client.post(second_url,data={'csrf_token':csrf}).status_code)
        self.client.post(first_url+'/cancel',data={'csrf_token':csrf})
        self.assertEqual(409,self.client.post(first_url,data={'csrf_token':csrf}).status_code)
        self.assertEqual(202,self.client.post(second_url,data={'csrf_token':csrf}).status_code)

    def test_imminent_send_delays_sync_without_claiming(self):
        account,csrf=self.prepare()
        self.client.post(f'/accounts/{account}/contact-sync',data={'csrf_token':csrf})
        self.client.post('/tasks',data={'csrf_token':csrf,'account_id':account,'target_name':'fixture','message_template':'test','send_time':'09:00'})
        from spark_console.models import SparkTask
        from spark_console.browser_runtime import has_pending
        from spark_console.contact_sync import run_contact_sync
        with session_scope(self.engine) as db:
            task=db.scalar(select(SparkTask))
            self.assertIsNotNone(task)
            task.next_run_at=utc_now()+timedelta(seconds=30)
        async def forbidden(*args):
            raise AssertionError('imminent task must not open browser')
        self.assertFalse(asyncio.run(run_contact_sync(self.settings,self.engine,collect=forbidden)))
        with session_scope(self.engine) as db:
            self.assertEqual('queued',db.get(ContactSyncState,account).status)
            self.assertFalse(has_pending(db,'auth',utc_now()))

    def test_reconciliation_runs_under_lock_even_when_memory_blocks_browser(self):
        account,csrf=self.prepare()
        self.client.post(f'/accounts/{account}/contact-sync',data={'csrf_token':csrf})
        with session_scope(self.engine) as db:
            ContactSyncService(db).claim()
        from spark_console.browser_runtime import BrowserSlot,admit_and_run
        def reconcile():
            with session_scope(self.engine) as db:
                ContactSyncService(db).recover()
        slot=BrowserSlot(self.settings.data_dir/'test-reconcile.lock')
        result=admit_and_run(slot,['never-launch'],1,lambda:False,lambda:'low_memory',reconcile=reconcile)
        self.assertEqual('low_memory',result)
        with session_scope(self.engine) as db:
            row=db.get(ContactSyncState,account)
            self.assertEqual('failed',row.status)
            self.assertIsNone(row.slot)


del BatchWebTests
