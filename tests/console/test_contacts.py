import unittest
from sqlalchemy import select
from core.web_chat import DouyinUserIdentity
from spark_console.crypto import CookieCipher
from spark_console.db import session_scope
from spark_console.models import DouyinContactIdentity, DouyinConversation, User
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from tests.console.test_batch_web import BatchWebTests


class ContactsTests(unittest.TestCase):
    setUp = BatchWebTests.setUp
    tearDown = BatchWebTests.tearDown
    login = BatchWebTests.login
    prepare = BatchWebTests.prepare

    def test_admin_and_user_both_return_name_only_conversations(self):
        account, _ = self.prepare(admin=True)
        with session_scope(self.engine) as db:
            db.add(DouyinConversation(account_id=account, display_name='依依妖妖'))
        normal = self.client.get(f'/accounts/{account}/conversations').json()['items']
        admin = self.client.get(f'/admin/accounts/{account}/conversations').json()['items']
        self.assertEqual(['依依妖妖'], [x['name'] for x in normal])
        self.assertEqual(['依依妖妖'], [x['name'] for x in admin])

    def test_aliases_preserve_nickname_when_display_uses_remark(self):
        account, _ = self.prepare()
        with session_scope(self.engine) as db:
            db.add(DouyinContactIdentity(account_id=account, sec_uid='one', nickname='依依妖妖', remark_name='备注'))
        item = self.client.get(f'/accounts/{account}/conversations').json()['items'][0]
        self.assertIn('依依妖妖', item.get('aliases', []))
        self.assertIn('备注', item.get('aliases', []))

    def test_rebind_partial_snapshot_preserves_previous_contacts(self):
        self.prepare()
        storage = {'cookies': [{'name': 'fixture', 'value': 'test-only', 'domain': '.douyin.com', 'path': '/', 'expires': -1, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}], 'origins': []}
        with session_scope(self.engine) as db:
            owner = db.scalar(select(User.id))
            service = AccountService(db, CookieCipher(b'c'*32), AuditService(db))
            account = service.create_from_storage_state(owner, '绑定', storage, 'stable-id', ('旧好友',), (DouyinUserIdentity('uid-old', nickname='旧好友'),))
            aid = account.id
        with session_scope(self.engine) as db:
            AccountService(db, CookieCipher(b'c'*32), AuditService(db)).create_from_storage_state(owner, '绑定', storage, 'stable-id', ('新好友',), ())
        with session_scope(self.engine) as db:
            names = set(db.scalars(select(DouyinConversation.display_name).where(DouyinConversation.account_id==aid)))
            self.assertEqual({'旧好友','新好友'}, names)
            self.assertIsNotNone(db.get(DouyinContactIdentity, (aid, 'uid-old')))

    def test_rename_keeps_previous_aliases_searchable(self):
        account,_=self.prepare()
        from spark_console.services.contacts import merge_contacts,contact_items
        with session_scope(self.engine) as db:
            merge_contacts(db,account,(),(DouyinUserIdentity('stable',nickname='旧昵称',remark_name='旧备注'),))
        with session_scope(self.engine) as db:
            merge_contacts(db,account,(),(DouyinUserIdentity('stable',nickname='新昵称',remark_name='新备注'),))
        with session_scope(self.engine) as db:
            names={alias for item in contact_items(db,account) for alias in item['aliases']}
            self.assertTrue({'旧昵称','旧备注','新昵称','新备注'} <= names)


del BatchWebTests
