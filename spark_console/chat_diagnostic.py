"""Bounded, admission-controlled, explicitly read-only chat compatibility probe.

Never calls an executor or fills a composer. Output contains only counts/codes.
An account's credential is decrypted only in the child memory, never serialized.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import signal
import sys
from urllib.parse import urlparse


async def probe(task_id, output):
    from playwright.async_api import async_playwright
    from spark_console.config import Settings
    from spark_console.crypto import CookieCipher
    from spark_console.credentials import CredentialPayload
    from core.web_chat import (UserInfoCollector, wait_for_chat_ready, ChatReadinessError,
                               select_web_chat_target, verify_chat_recipient_name)
    settings = Settings.from_env(os.environ)
    state = {'stage': 'starting', 'script_failures': [], 'script_http_errors': [],
             'document_responses': [], 'document_failures': [], 'page_errors': [], 'verified': []}
    def save():
        output.write_text(json.dumps(state), encoding='utf-8')
    db = sqlite3.connect('file:' + str(settings.data_dir/'spark.db') + '?mode=ro', uri=True)
    account = db.execute('select a.encrypted_cookies,a.cookie_nonce,a.cookie_version from douyin_accounts a '
                         'join spark_tasks t on a.id=t.douyin_account_id where t.id=?', (task_id,)).fetchone()
    recipients = db.execute('select target_name,target_sec_uid from spark_task_recipients '
                            'where task_id=? order by position', (task_id,)).fetchall()
    db.close()
    if not account:
        raise RuntimeError('account unavailable')
    raw = bytearray(CookieCipher(settings.cookie_key_file.read_bytes()).decrypt(account[0], account[1]))
    try:
        payload = CredentialPayload.parse(bytes(raw), account[2])
    finally:
        raw[:] = b'\0' * len(raw)
        raw.clear()
    save()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(**payload.context_options())
            if payload.cookies_to_add():
                await context.add_cookies(payload.cookies_to_add())
            page = await context.new_page()
            page.set_default_timeout(8000)
            info = UserInfoCollector()
            page.on('response', info.capture)
            def failed(request):
                category = {'script':'script_failures', 'document':'document_failures'}.get(request.resource_type)
                if category and len(state[category]) < 10:
                    known = {'net::ERR_CONNECTION_RESET','net::ERR_CONNECTION_TIMED_OUT',
                             'net::ERR_TIMED_OUT','net::ERR_NAME_NOT_RESOLVED',
                             'net::ERR_ABORTED','net::ERR_FAILED'}
                    state[category].append({'host':urlparse(request.url).hostname,
                                                    'network_error': request.failure if request.failure in known else 'other'})
            def response(resp):
                if resp.request.resource_type == 'document' and len(state['document_responses']) < 10:
                    state['document_responses'].append({'host':urlparse(resp.url).hostname,'status':resp.status})
                if resp.request.resource_type == 'script' and resp.status >= 400 and len(state['script_http_errors']) < 10:
                    state['script_http_errors'].append({'host':urlparse(resp.url).hostname,'status':resp.status})
            def page_error(error):
                known = {'Error','TypeError','ReferenceError','SyntaxError','RangeError'}
                name = getattr(error,'name','Error')
                if len(state['page_errors']) < 10:
                    state['page_errors'].append(name if name in known else 'Error')
            page.on('requestfailed', failed)
            page.on('response', response)
            page.on('pageerror', page_error)
            state['stage'] = 'navigation'
            save()
            await page.goto('https://www.douyin.com/chat',wait_until='domcontentloaded',timeout=45000)
            state['stage'] = 'readiness'
            save()
            try:
                await wait_for_chat_ready(page,timeout=30000)
            except ChatReadinessError as error:
                state['error_code'] = error.code
                state['counts'] = await page.evaluate('''() => ({
                    rows:document.querySelectorAll('.conversationConversationItemwrapper').length,
                    editors:document.querySelectorAll('[contenteditable=true]').length,
                    textLength:document.body?.innerText.length || 0,
                    scripts:document.scripts.length})''')
                return
            state['stage'] = 'name_check'
            for index, (name, uid) in enumerate(recipients):
                identity = info.get(uid) if uid else None
                selected = await select_web_chat_target(page,name,timeout=10000,aliases=identity.aliases if identity else ())
                state['name_header_probe'] = await page.locator('.RightPanelHeaderconvHeader').evaluate_all('''(headers, expected) => headers.map(header => ({
                    visible:!!header.getClientRects().length,
                    matchingNodes:[...header.querySelectorAll('*')].filter(el => el.innerText?.trim()===expected).slice(0,12).map(el=>({tag:el.tagName,css:typeof el.className==='string'?el.className.slice(0,160):'',profileAction:el.getAttribute('data-apm-action')==='个人页卡片'})),
                    firstLineMatches:header.innerText?.trim().split('\\n')[0].trim()===expected
                }))''', selected)
                save()
                await verify_chat_recipient_name(page,selected)
                state['verified'].append(index)
                save()
            state['stage'] = 'complete_no_send'
        except Exception as error:
            state['error_type'] = type(error).__name__
        finally:
            state['identity_count'] = len(info.identities) if 'info' in locals() else 0
            if 'page' in locals():
                try:
                    state['final_dom'] = await asyncio.wait_for(page.evaluate('''() => ({
                        rows:document.querySelectorAll('.conversationConversationItemwrapper').length,
                        editors:document.querySelectorAll('[contenteditable=true]').length,
                        textLength:document.body?.innerText.length || 0,
                        scripts:document.scripts.length,
                        verificationPrompt:/请完成验证|安全验证|拖动滑块/.test(document.body?.innerText || '')})'''), 2)
                except Exception:
                    state['dom_unavailable'] = True
            save()
            await browser.close()


if __name__ == '__main__':
    from spark_console.config import Settings
    from spark_console.browser_runtime import BrowserSlot, admit_and_run, resource_reason
    settings = Settings.from_env(os.environ)
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        asyncio.run(probe(sys.argv[2], Path(sys.argv[3])))
    elif len(sys.argv) == 2:
        stopping = False
        def stop(*_):
            global stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        output = settings.data_dir / ('chat-probe-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '.json')
        slot = BrowserSlot(settings.data_dir/'browser-runtime.lock')
        command = [sys.executable,'-m','spark_console.chat_diagnostic','--child',sys.argv[1],str(output)]
        result = admit_and_run(slot,command,110,lambda:stopping,lambda:resource_reason(settings.data_dir))
        print(json.dumps({'admission':result,'result_file':str(output)}),flush=True)
        if output.exists():
            print(output.read_text(),flush=True)
    else:
        raise SystemExit(2)
