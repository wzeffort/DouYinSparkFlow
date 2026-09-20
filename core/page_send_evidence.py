"""Conservative page-only evidence for one send; never submits or retries.

Uses Douyin's message-content hook and outgoing-message class. A DOM result is
not a server acknowledgement and must be labelled separately by callers.
"""
import asyncio


START_JS = r"""text => {
  window.__sparkSendEvidence?.observer?.disconnect();
  window.__sparkSendEvidence = null;
  const visible = e => e.getClientRects().length > 0;
  const rows = [...document.querySelectorAll('[data-e2e="conversation-item"]')]
    .filter(e => visible(e) && (e.matches('.conversationConversationItemcurConversation') || e.querySelector('.conversationConversationItemcurConversation')));
  const headers = [...document.querySelectorAll('.RightPanelHeaderconvHeader')].filter(visible);
  const editors = [...document.querySelectorAll('[contenteditable="true"]')].filter(visible);
  if(rows.length !== 1 || headers.length !== 1 || editors.length !== 1) return false;
  const messages = [...document.querySelectorAll('[data-e2e="msg-item-content"]')].filter(visible);
  const normalize = value => value.replace(/\r\n/g,'\n').replace(/\u00a0/g,' ').trim();
  const state = {row:rows[0],header:headers[0],editor:editors[0],
    title:headers[0].innerText, before:messages, text:normalize(text), candidate:null, since:0, tainted:false};
  if(!state.text || normalize(state.editor.innerText) !== state.text) return false;
  state.observer = new MutationObserver(records => {
    if(records.length) state.tainted = true;
  });
  state.observer.observe(state.row, {attributes:true,attributeFilter:['class']});
  state.observer.observe(state.header, {childList:true,subtree:true,characterData:true});
  window.__sparkSendEvidence = state;
  return true;
}"""

CHECK_JS = r"""() => {
  const s = window.__sparkSendEvidence;
  if(!s || s.tainted) return false;
  const visible = e => e.isConnected && e.getClientRects().length > 0;
  const selected = [...document.querySelectorAll('[data-e2e="conversation-item"]')]
    .filter(e => visible(e) && (e.matches('.conversationConversationItemcurConversation') || e.querySelector('.conversationConversationItemcurConversation')));
  if(selected.length !== 1 || selected[0] !== s.row || !visible(s.header) || s.header.innerText !== s.title || !visible(s.editor)) return false;
  if(s.editor.innerText.trim()) return false;
  const now = [...document.querySelectorAll('[data-e2e="msg-item-content"]')].filter(visible);
  // Keep the entire baseline prefix. History loading / virtualisation is not new-send evidence.
  if(now.length !== s.before.length + 1 || !s.before.every((node,i) => now[i] === node)) return false;
  const candidate = now[now.length-1];
  const outgoing = candidate.closest('.MessageBoxContentisFromMe');
  if(!outgoing || s.before.includes(candidate)) return false;
  const normalize = value => value.replace(/\r\n/g,'\n').replace(/\u00a0/g,' ').trim();
  if(normalize(candidate.innerText) !== s.text) return false;
  const scope = outgoing.parentElement || outgoing;
  if(scope.querySelector('[aria-busy="true"],[data-status="pending"],[data-status="failed"],[class*="sending" i],[class*="failed" i],[class*="error" i],[class*="loading" i]') || /发送失败|重新发送|发送中/.test(scope.innerText)) return false;
  if(s.candidate !== candidate) {s.candidate=candidate;s.since=performance.now();return false;}
  return performance.now()-s.since >= 1000;
}"""

CLOSE_JS = "() => { window.__sparkSendEvidence?.observer?.disconnect(); window.__sparkSendEvidence = null; }"


class PageSendEvidence:
    def __init__(self, page):
        self.page = page
        self.armed = False

    async def start(self, text):
        try:
            self.armed = await asyncio.wait_for(self.page.evaluate(START_JS, text), 3) is True
        except Exception:
            self.armed = False

    async def confirmed(self):
        if not self.armed:
            return False
        try:
            return await asyncio.wait_for(self.page.evaluate(CHECK_JS), 2) is True
        except Exception:
            return False

    async def close(self):
        try:
            await asyncio.wait_for(self.page.evaluate(CLOSE_JS), 2)
        except Exception:
            pass
