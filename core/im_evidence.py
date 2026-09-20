"""Read-only conversation identity and request-correlated IM receipts.

Inspired by DouYinSparkFlow v3.2.1's passive-monitor approach. No request replay,
signing, cookies, raw request bodies or full responses are persisted.
"""
import asyncio
import json
from urllib.parse import urlparse


ROWS_JS = r"""() => {
  const result = [];
  for (const el of document.querySelectorAll('[data-e2e="conversation-item"]')) {
    if (!el.getClientRects().length) continue;
    let model;
    for (const key of Object.keys(el)) {
      if (key.startsWith('__reactProps$')) model = el[key]?.conversation || model;
      if (key.startsWith('__reactFiber$')) {
        let node = el[key];
        for (let n=0; node && n<30; n++,node=node.return) {
          if (node.memoizedProps?.conversation) {model=node.memoizedProps.conversation; break;}
        }
      }
      if (model) break;
    }
    if (!model) continue;
    try {
      const title = el.querySelector('.conversationConversationItemtitle')?.textContent?.trim() || '';
      result.push({index:[...document.querySelectorAll('[data-e2e="conversation-item"]')].indexOf(el),
        conv_id:String(model.id || model.conversationId || ''),
        sec_uid:String(model.toParticipantSecUserId || ''), title,
        current:el.matches('.conversationConversationItemcurConversation') || !!el.querySelector('.conversationConversationItemcurConversation')});
    } catch (_) { /* An unavailable getter is not identity evidence. */ }
  }
  return result;
}"""


class ConversationChanged(RuntimeError):
    pass


async def rows(page):
    try:
        value = await asyncio.wait_for(page.evaluate(ROWS_JS), 3)
        return [r for r in value if isinstance(r, dict) and isinstance(r.get('conv_id'), str)] if isinstance(value, list) else []
    except Exception:
        return []


async def select_identity(page, sec_uid):
    """Select an exact stable identity among rendered rows; never fuzzy names."""
    if not sec_uid:
        return None
    matches = [r for r in await rows(page) if r.get('sec_uid') == sec_uid and r.get('conv_id')]
    if len(matches) != 1:
        return None
    row = matches[0]
    # Re-read identity on the locator before clicking: virtual list indices move.
    loc = page.locator('[data-e2e="conversation-item"]').nth(row['index'])
    await loc.click()
    await asyncio.sleep(.15)
    await verify_conversation(page, row['conv_id'], sec_uid)
    return row


async def current_identity(page, sec_uid=None):
    selected = [r for r in await rows(page) if r.get('current')]
    if len(selected) != 1:
        return None
    row = selected[0]
    if sec_uid and row.get('sec_uid') and row['sec_uid'] != sec_uid:
        raise ConversationChanged('选中会话的好友标识不符，本次未发送')
    return row if row.get('conv_id') else None


async def verify_conversation(page, expected, sec_uid=None):
    actual = await current_identity(page, sec_uid)
    if not actual or actual['conv_id'] != expected:
        raise ConversationChanged('聊天会话发生变化或身份不可读，本次未发送')
    return actual


def fields(data):
    """Strict bounded protobuf wire reader; unknown/truncated payload => unknown."""
    if not isinstance(data, bytes) or len(data) > 65536:
        raise ValueError('invalid payload')
    pos, output = 0, []
    def varint():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError('truncated')
            byte = data[pos]; pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('invalid varint')
    while pos < len(data):
        key = varint(); tag, wire = key >> 3, key & 7
        if not tag or len(output) >= 2048:
            raise ValueError('invalid field')
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else 8 if wire == 1 else 4
            if size > len(data) - pos:
                raise ValueError('truncated')
            value = data[pos:pos+size]; pos += size
        else:
            raise ValueError('unsupported wire type')
        output.append((tag, wire, value))
    return output


def receipt(data):
    try:
        envelope = {tag: value for tag, _, value in fields(data)}
        # CMD 100 is send; unknown envelopes never prove success.
        if envelope.get(1) != 100 or not isinstance(envelope.get(3), int):
            return None
        code = envelope[3]
        if code != 0:
            return {'status':'rejected', 'code':code}
        if envelope.get(4, b'').lower() != b'ok':
            return None
        body = {tag: value for tag, _, value in fields(envelope[6])}
        send = {tag: value for tag, _, value in fields(body[100])}
        message_id = send.get(1)
        if not isinstance(message_id, int) or message_id <= 0:
            return None
        return {'status':'accepted', 'code':0, 'message_id':str(message_id)}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def payload_matches(data, conv_id, text):
    """Find exact identity and exact text leaves, not byte substrings or prefixes."""
    if not conv_id or not data or len(data) > 65536:
        return False
    strings = set()
    def json_strings(value, depth=0):
        if depth > 8: return
        if isinstance(value, str): strings.add(value)
        elif isinstance(value, dict):
            for child in value.values(): json_strings(child, depth+1)
        elif isinstance(value, list):
            for child in value[:100]: json_strings(child, depth+1)
    def visit(buf, depth=0):
        if depth > 6: return
        try:
            value = buf.decode('utf-8')
            strings.add(value)
            if value.startswith(('{', '[')):
                json_strings(json.loads(value))
        except (UnicodeDecodeError, ValueError):
            pass
        try:
            for _, wire, value in fields(buf):
                if wire == 2: visit(value, depth+1)
        except ValueError:
            pass
    visit(data)
    return conv_id in strings and text in strings


class SendMonitor:
    """One Enter boundary, one matching request object, one corresponding response."""
    def __init__(self, page, conv_id, text):
        self.page, self.conv_id, self.text = page, conv_id, text
        self.requests = []
        self.result = None
        self.pending = set()

    def start(self):
        self.page.on('request', self.on_request)
        self.page.on('response', self.on_response)

    def on_request(self, request):
        try:
            url = urlparse(request.url)
            if url.scheme == 'https' and url.hostname == 'imapi.douyin.com' and url.path.rstrip('/') == '/v1/message/send' and request.method == 'POST':
                if payload_matches(request.post_data_buffer, self.conv_id, self.text):
                    self.requests.append(request)
        except Exception:
            pass

    def on_response(self, response):
        if any(response.request is request for request in self.requests):
            task = asyncio.create_task(self.decode(response))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def decode(self, response):
        try:
            if response.status == 200:
                self.result = receipt(await asyncio.wait_for(response.body(), 3))
        except Exception:
            pass

    async def wait(self, timeout=6):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if len(self.requests) > 1:
                return None
            if self.result:
                return self.result
            await asyncio.sleep(.1)
        return None

    async def close(self):
        self.page.remove_listener('request', self.on_request)
        self.page.remove_listener('response', self.on_response)
        for task in list(self.pending): task.cancel()
        if self.pending: await asyncio.gather(*list(self.pending), return_exceptions=True)
