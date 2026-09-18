"""Account-scoped contact projection and non-destructive snapshot merging."""
import hashlib
from collections import Counter
import uuid
from datetime import timedelta, timezone
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from spark_console.models import ContactSyncState, DouyinAccount, DouyinContactIdentity, DouyinConversation, utc_now
from spark_console.services import Conflict


def contact_items(db, account_id):
    contacts = db.scalars(select(DouyinContactIdentity).where(
        DouyinContactIdentity.account_id == account_id)).all()
    names = db.scalars(select(DouyinConversation.display_name).where(
        DouyinConversation.account_id == account_id)).all()
    items, aliases = [], set()
    for contact in contacts:
        values = list(dict.fromkeys(v for v in (
            contact.remark_name, contact.nickname, contact.unique_id, contact.short_id) if v))
        if not values:
            continue
        aliases.update(values)
        items.append({'name': values[0], 'sec_uid': contact.sec_uid, 'aliases': values,
                      'kind': 'contact', 'source': 'identity',
                      'identity_hint': ('抖音号 ' + (contact.unique_id or contact.short_id)) if (contact.unique_id or contact.short_id) else '标识尾号 ' + contact.sec_uid[-6:]})
    items.extend({'name': name, 'sec_uid': None, 'aliases': [name],
                  'kind': 'unknown', 'source': 'conversation'}
                 for name in names if name not in aliases)
    counts = Counter(item['name'] for item in items)
    for item in items:
        item['same_name'] = counts[item['name']] > 1
    return sorted(items, key=lambda item: (item['kind'] != 'contact', item['name']))


def merge_contacts(db, account_id, names=(), identities=()):
    # A partial/visible snapshot is never evidence that an old friend vanished.
    existing = set(db.scalars(select(DouyinConversation.display_name).where(
        DouyinConversation.account_id == account_id)))
    for value in names:
        name = str(value).strip()[:256]
        if name and name not in existing:
            db.add(DouyinConversation(account_id=account_id, display_name=name))
            existing.add(name)
    for identity in identities:
        uid = str(identity.sec_uid).strip()[:256]
        if not uid:
            continue
        row = db.get(DouyinContactIdentity, (account_id, uid))
        if row is None:
            row = DouyinContactIdentity(account_id=account_id, sec_uid=uid)
            db.add(row)
        for key, maximum in (('nickname',256), ('remark_name',256), ('unique_id',128), ('short_id',64)):
            value = str(getattr(identity, key, None) or '').strip()[:maximum]
            if value:
                previous = getattr(row, key)
                if key in ('nickname','remark_name') and previous and previous != value and previous not in existing:
                    db.add(DouyinConversation(account_id=account_id, display_name=previous))
                    existing.add(previous)
                setattr(row, key, value)
    db.flush()


ACTIVE_SYNC = ('queued', 'running')


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ContactSyncService:
    def __init__(self, db, now=utc_now):
        self.db, self.now = db, now

    @staticmethod
    def tag(account):
        return hashlib.sha256(account.cookie_nonce).hexdigest()

    def request(self, account):
        if account.validation_state == 'invalid':
            raise Conflict('账号登录状态异常，请先重新绑定')
        now = self.now()
        self.expire()
        row = self.db.get(ContactSyncState, account.id)
        if row is not None and row.status in ACTIVE_SYNC:
            return self.public(row)
        if row is not None and (now - _aware(row.requested_at)).total_seconds() < 60:
            raise Conflict('请等待一分钟后再同步')
        try:
            with self.db.begin_nested():
                if row is None:
                    row = ContactSyncState(account_id=account.id)
                    self.db.add(row)
                row.request_id = str(uuid.uuid4())
                row.credential_tag = self.tag(account)
                row.status, row.slot = 'queued', 'global'
                row.requested_at, row.finished_at = now, None
                row.error_code, row.collected_count = None, 0
                self.db.flush()
        except IntegrityError:
            raise Conflict('正在同步其他账号，请稍后再试') from None
        return self.public(row)

    def expire(self):
        self.db.execute(update(ContactSyncState).where(
            ContactSyncState.status.in_(ACTIVE_SYNC),
            ContactSyncState.requested_at <= self.now()-timedelta(minutes=15)
        ).values(status='failed', slot=None, finished_at=self.now(), error_code='expired'))

    def recover(self):
        # Called only by a fresh Auth job inside its singleton/global browser lock.
        self.expire()
        self.db.execute(update(ContactSyncState).where(ContactSyncState.status == 'running')
            .values(status='failed', slot=None, finished_at=self.now(), error_code='interrupted'))

    def claim(self):
        row = self.db.scalar(select(ContactSyncState).where(ContactSyncState.status == 'queued').limit(1))
        if row is None:
            return None
        result = self.db.execute(update(ContactSyncState).where(
            ContactSyncState.account_id == row.account_id,
            ContactSyncState.request_id == row.request_id,
            ContactSyncState.status == 'queued'
        ).values(status='running'))
        return (row.account_id, row.request_id, row.credential_tag) if result.rowcount == 1 else None

    def finish(self, account_id, request_id, names=(), identities=(), error_code=None):
        account = self.db.get(DouyinAccount, account_id)
        row = self.db.get(ContactSyncState, account_id)
        if account is None or row is None or row.request_id != request_id or row.status != 'running':
            return False
        if row.credential_tag != self.tag(account):
            error_code = 'account_changed'
        # Acquire the conditional write before merging so a racing cancel cannot lose.
        can_merge = not error_code or (error_code in {'timeout','sync_failed','chat_ui_unavailable'} and bool(names))
        changed = self.db.execute(update(ContactSyncState).where(
            ContactSyncState.account_id == account_id,
            ContactSyncState.request_id == request_id, ContactSyncState.status == 'running'
        ).values(status='partial' if can_merge else 'failed', slot=None,
                 error_code=error_code, finished_at=self.now(), collected_count=len(set(names)) if can_merge else 0))
        if changed.rowcount != 1:
            return False
        self.db.refresh(account)
        if row.credential_tag != self.tag(account):
            self.db.execute(update(ContactSyncState).where(ContactSyncState.account_id==account_id)
                .values(status='failed', error_code='account_changed', collected_count=0))
            return False
        if can_merge:
            merge_contacts(self.db, account_id, names, identities)
        return True

    def cancel(self, account_id):
        self.db.execute(update(ContactSyncState).where(
            ContactSyncState.account_id == account_id, ContactSyncState.status.in_(ACTIVE_SYNC)
        ).values(status='cancelled', slot=None, finished_at=self.now(), error_code='cancelled'))
        return self.status(account_id)

    def status(self, account_id):
        row = self.db.get(ContactSyncState, account_id)
        result = self.public(row)
        # Read-only status remains truthful even when low resources delay cleanup.
        if row is not None and row.status in ACTIVE_SYNC and _aware(row.requested_at) <= self.now()-timedelta(minutes=15):
            result.update(status='failed', error_code='expired')
        return result

    @staticmethod
    def public(row):
        if row is None:
            return {'status':'never', 'request_id':None, 'finished_at':None, 'collected_count':0, 'complete':False}
        return {'status':row.status, 'request_id':row.request_id,
                'finished_at':_aware(row.finished_at).isoformat() if row.finished_at else None,
                'collected_count':row.collected_count, 'error_code':row.error_code,
                'complete':False, 'scope':'recent_conversations'}
