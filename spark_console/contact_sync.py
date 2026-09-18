"""Read-only recent conversation collection, executed inside Auth's BrowserSlot.

Never imports an executor or schedules retries. No composer interaction, messages,
screenshots, storage-state persistence or credential logging.
"""
import asyncio
from datetime import timedelta

from sqlalchemy import select
from spark_console.credentials import CredentialPayload
from spark_console.crypto import CookieCipher
from spark_console.db import session_scope
from spark_console.models import ContactSyncState, DouyinAccount, SparkTask, TaskRun, utc_now
from spark_console.services.contacts import ContactSyncService


class SyncCancelled(Exception):
    pass


async def collect_page(page, info, cancelled, *, step_seconds=.5, max_scrolls=50, observed=None):
    """Accumulate virtualized rows across settling/scrolling; never claim full list."""
    observed = observed if observed is not None else {'names':set(), 'identities':{}}
    seen = observed['names']
    async def snapshot():
        if cancelled():
            raise SyncCancelled()
        names = await page.locator('.conversationConversationItemwrapper').evaluate_all('''els => els
            .filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden')
            .map(e=>e.querySelector('.conversationConversationItemtitle')?.innerText?.trim()).filter(Boolean)''')
        seen.update(name[:256] for name in names[:5000-len(seen)])
        observed['identities'].update(dict(list(info.identities.items())[:5000]))
    # The live page changed titles between readiness and hydration; take all samples.
    for _ in range(6):
        await snapshot()
        await asyncio.sleep(step_seconds)
    idle = 0
    for _ in range(max_scrolls):
        before = len(seen)
        moved = await page.evaluate('''() => {
            const first=[...document.querySelectorAll('.conversationConversationItemwrapper')].find(e=>e.getClientRects().length);
            if(!first) return false;
            for(let e=first.parentElement;e && e!==document.body;e=e.parentElement) {
                if(e.scrollHeight>e.clientHeight+10 && ['auto','scroll'].includes(getComputedStyle(e).overflowY)) {
                    const prior=e.scrollTop;e.scrollTop+=Math.max(100,e.clientHeight*.8);
                    return prior!==e.scrollTop;
                }
            }
            return false;
        }''')
        await asyncio.sleep(step_seconds)
        await snapshot()
        idle = idle+1 if not moved and before == len(seen) else 0
        if idle >= 4 or len(seen) >= 5000:
            break
    if cancelled():
        raise SyncCancelled()
    identities = await info.drain()
    observed['identities'].update({x.sec_uid:x for x in identities[:5000]})
    return tuple(sorted(seen)), identities


async def collect_live(payload, cancelled, observed=None):
    from playwright.async_api import async_playwright
    from core.web_chat import UserInfoCollector, wait_for_chat_ready
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(**payload.context_options())
            if payload.cookies_to_add():
                await context.add_cookies(payload.cookies_to_add())
            page = await context.new_page()
            page.set_default_timeout(5000)
            info = UserInfoCollector()
            page.on('response', info.capture)
            if cancelled():
                raise SyncCancelled()
            await page.goto('https://www.douyin.com/chat', wait_until='domcontentloaded', timeout=35000)
            await wait_for_chat_ready(page, timeout=25000)
            return await collect_page(page, info, cancelled, observed=observed)
        finally:
            await asyncio.wait_for(browser.close(), timeout=10)


async def run_contact_sync(settings, engine, *, collect=None, stopping=None):
    observed = {'names':set(), 'identities':{}}
    with session_scope(engine) as db:
        service = ContactSyncService(db)
        service.recover()
        # Yield before starting; never hold the browser through an imminent send.
        if db.scalar(select(TaskRun.id).where(TaskRun.status=='running').limit(1)):
            return False
        if db.scalar(select(SparkTask.id).where(SparkTask.enabled.is_(True),
            SparkTask.douyin_account_id.is_not(None), SparkTask.next_run_at <= utc_now()+timedelta(seconds=100)).limit(1)):
            return False
        claim = service.claim()
        if claim is None:
            return False
    account_id, request_id, tag = claim
    def cancelled():
        if stopping is not None and stopping.is_set():
            return True
        with session_scope(engine) as db:
            row = db.get(ContactSyncState, account_id)
            account = db.get(DouyinAccount, account_id)
            return (row is None or row.request_id != request_id or row.status != 'running'
                    or account is None or ContactSyncService.tag(account) != tag)
    names, identities, error_code = (), (), None
    try:
        with session_scope(engine) as db:
            account = db.get(DouyinAccount, account_id)
            if account is None or ContactSyncService.tag(account) != tag:
                raise SyncCancelled()
            raw = bytearray(CookieCipher(settings.cookie_key_file.read_bytes()).decrypt(account.encrypted_cookies, account.cookie_nonce))
            try:
                payload = CredentialPayload.parse(bytes(raw), account.cookie_version)
            finally:
                raw[:] = b'\0'*len(raw); raw.clear()
        async with asyncio.timeout(90):
            names, identities = await (collect(payload, cancelled) if collect else collect_live(payload, cancelled, observed))
        if cancelled():
            raise SyncCancelled()
    except SyncCancelled:
        error_code = 'cancelled'
    except TimeoutError:
        error_code = 'timeout'
    except Exception as error:
        code = getattr(error, 'code', None)
        error_code = code if code in {'login_expired','chat_ui_unavailable'} else 'sync_failed'
    finally:
        if error_code in {'timeout','sync_failed','chat_ui_unavailable'} and observed['names']:
            names, identities = tuple(observed['names']), tuple(observed['identities'].values())
        with session_scope(engine) as db:
            ContactSyncService(db).finish(account_id, request_id, names, identities, error_code)
    return True
