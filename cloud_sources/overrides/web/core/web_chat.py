import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse

from utils.logger import setup_logger


WEB_CHAT_URL = "https://www.douyin.com/chat"
CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
CHAT_INPUT_SELECTOR = CHAT_EDITOR_SELECTOR + ' [contenteditable="true"]'
SEARCH_INPUT_SELECTORS = (
    'input[placeholder="搜索"]',
    'input[placeholder*="搜索"]',
)
LOGIN_PROMPT_SELECTORS = (
    "text=扫码登录",
    "text=手机号登录",
    "text=验证码登录",
)

logger = setup_logger(level=logging.DEBUG)


@dataclass(frozen=True)
class DouyinUserIdentity:
    sec_uid: str
    short_id: str | None = None
    unique_id: str | None = None
    nickname: str | None = None
    remark_name: str | None = None

    @property
    def aliases(self) -> tuple[str, ...]:
        values = (
            self.remark_name,
            self.nickname,
            self.unique_id,
            self.short_id,
        )
        return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip()))


class UserInfoCollector:
    PATH = "/aweme/v1/web/im/user/info"

    def __init__(self):
        self.identities: dict[str, DouyinUserIdentity] = {}
        self._changed = asyncio.Event()
        self._pending: set[asyncio.Task] = set()

    def capture(self, response) -> None:
        if self.PATH not in urlparse(response.url).path:
            return
        task = asyncio.create_task(self.handle_response(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def handle_response(self, response) -> None:
        if self.PATH not in urlparse(response.url).path or response.status != 200:
            return
        try:
            body = await response.json()
        except Exception:
            return
        data = body.get("data", ()) if isinstance(body, dict) else ()
        for item in data if isinstance(data, list) else ():
            if not isinstance(item, dict):
                continue
            sec_uid = str(item.get("sec_uid") or "").strip()
            if not sec_uid:
                continue
            self.identities[sec_uid] = DouyinUserIdentity(
                sec_uid=sec_uid,
                short_id=_optional_string(item.get("short_id")),
                unique_id=_optional_string(item.get("unique_id")),
                nickname=_optional_string(item.get("nickname")),
                remark_name=_optional_string(item.get("remark_name")),
            )
            self._changed.set()

    def get(self, sec_uid: str) -> DouyinUserIdentity | None:
        return self.identities.get(sec_uid)

    async def wait_for(self, sec_uid: str, timeout: float = 15.0) -> DouyinUserIdentity | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            identity = self.get(sec_uid)
            if identity is not None:
                return identity
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), deadline - loop.time())
            except TimeoutError:
                break
        return self.get(sec_uid)

    async def drain(self) -> tuple[DouyinUserIdentity, ...]:
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        return tuple(self.identities.values())


def _optional_string(value) -> str | None:
    text = str(value or "").strip()
    return text or None


class TargetNotFoundError(RuntimeError):
    """Raised when the requested friend is absent from the web chat list."""


class AmbiguousTargetError(TargetNotFoundError):
    """Name-only selection cannot safely choose among visible exact matches."""


class RecipientNameError(RuntimeError):
    """The current conversation has not confirmed the selected display name."""


async def verify_chat_recipient_name(page, expected_name, timeout=5000):
    """User-selected name policy: exact visible header, no UID/profile request.

    This is deliberately weaker than stable identity proof. Duplicate selection
    is rejected separately; names outside the current header are not evidence.
    """
    try:
        if not isinstance(expected_name, str) or not expected_name.strip():
            raise RecipientNameError()
        location = urlparse(page.url)
        if (location.scheme, location.netloc, location.path) != ('https', 'www.douyin.com', '/chat'):
            raise RecipientNameError()
        expected_name = expected_name.strip()
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            header = page.locator('.RightPanelHeaderconvHeader')
            editor = page.locator(CHAT_INPUT_SELECTOR)
            if (await header.count() == 1 and await header.is_visible()
                    and await editor.count() == 1 and await editor.is_visible()):
                # Verified against the real chat page; ancillary header buttons
                # or status badges must never count as the recipient's name.
                title = header.locator('.RightPanelHeadertitle')
                if (await title.count() == 1 and await title.is_visible()
                        and (await title.inner_text()).strip() == expected_name):
                    return
            if asyncio.get_running_loop().time() >= deadline:
                raise RecipientNameError()
            await asyncio.sleep(0.1)
    except Exception:
        raise RecipientNameError('当前聊天窗口名称与所选好友不一致或无法确认，已停止发送') from None


class WebChatLoginRequiredError(RuntimeError):
    """Raised when the saved web-chat session has returned to a login page."""


class RecipientIdentityError(RuntimeError):
    """The currently open chat cannot be attested to the requested sec_uid."""


class ChatReadinessError(RuntimeError):
    MESSAGES = {
        'login_expired': '抖音页面要求重新登录；本次未发送',
        'chat_ui_unavailable': '聊天列表未加载或为空；本批次未进入好友选择，请检查页面加载与登录状态',
    }

    def __init__(self, code):
        self.code = code
        super().__init__(self.MESSAGES[code])


async def wait_for_chat_ready(page, timeout=30000):
    """Do not mistake DOMContentLoaded (possibly a blank SPA) for usable chat."""
    state_script = """() => {
        const visible = el => !!el.getClientRects().length &&
            getComputedStyle(el).visibility !== 'hidden';
        const prompts = ['扫码登录', '手机号登录', '验证码登录'];
        if ([...document.querySelectorAll('button,span,div')].some(el =>
            el.children.length === 0 && visible(el) && prompts.includes(el.textContent.trim())))
            return 'login_expired';
        if ([...document.querySelectorAll('.conversationConversationItemwrapper')].some(visible))
            return 'ready';
        return 'chat_ui_unavailable';
    }"""
    try:
        # The initial app shell can show login controls before stored login state
        # finishes hydrating. Give it the full readiness budget before invalidation.
        result = await page.wait_for_function(
            "() => {const state = (" + state_script + ")(); return state === 'ready' ? state : false;}",
            timeout=timeout)
        state = await result.json_value()
        if state != 'ready':
            raise ChatReadinessError('chat_ui_unavailable')
    except ChatReadinessError:
        raise
    except Exception:
        try:
            state = await page.evaluate(state_script)
        except Exception:
            state = 'chat_ui_unavailable'
        if state == 'login_expired':
            raise ChatReadinessError('login_expired') from None
        raise ChatReadinessError('chat_ui_unavailable') from None


async def verify_chat_recipient_uid(page, expected_uid: str | None, identities=None) -> None:
    """Compare the current header's link with the API identity without opening it.

    This automation-owned page suppresses window.open for its entire lifetime.
    Only a URL generated synchronously by this exact header click is proof;
    delayed actions, missing API identities and layout changes fail closed.
    No profile navigation or framework state inspection is performed.
    """
    try:
        if not expected_uid or expected_uid != expected_uid.strip():
            raise RecipientIdentityError()
        identity = identities.get(expected_uid) if identities is not None else None
        if identity is None or identity.sec_uid != expected_uid:
            raise RecipientIdentityError()
        location = urlparse(page.url)
        if location.scheme != 'https' or location.netloc != 'www.douyin.com' or location.path != '/chat':
            raise RecipientIdentityError()
        header = page.locator('.RightPanelHeaderconvHeader [data-apm-action="个人页卡片"]')
        editor = page.locator(CHAT_EDITOR_SELECTOR)
        if await header.count() != 1 or not await header.is_visible():
            raise RecipientIdentityError()
        if await editor.count() != 1 or not await editor.is_visible():
            raise RecipientIdentityError()
        probe_script = """el => {
            const key = '__sparkRecipientUidProbe_v1';
            let state = window[key];
            if (!state) {
                state = {urls: [], active: false};
                state.block = function(url) {
                    if (state.active) state.urls.push(String(url));
                    return null;
                };
                window[key] = state;
                window.open = state.block;
            }
            if (window.open !== state.block) throw new Error('probe unavailable');
            state.urls = [];
            state.active = true;
            try { el.click(); } finally { state.active = false; }
            return state.urls;
        }"""
        # The previous editor/header can remain visible while React switches chats.
        # Wait for UID proof, not merely visibility or a matching display name.
        deadline = asyncio.get_running_loop().time() + 3.0
        while True:
            urls = await header.evaluate(probe_script)
            if len(urls) != 1:
                raise RecipientIdentityError()
            profile = urlparse(urljoin(page.url, urls[0]))
            if (profile.scheme != 'https' or profile.netloc != 'www.douyin.com'
                    or not profile.path.startswith('/user/')):
                raise RecipientIdentityError()
            if profile.path == '/user/' + expected_uid:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise RecipientIdentityError()
            await asyncio.sleep(0.1)
    except Exception:
        raise RecipientIdentityError('无法核对当前聊天对象 UID，已停止发送；请重新选择已识别的好友') from None


async def page_has_web_chat_login_prompt(page) -> bool:
    """Return whether the Douyin chat page visibly asks the account to log in."""
    for selector in LOGIN_PROMPT_SELECTORS:
        try:
            locator = page.locator(selector)
            if await locator.count() == 0:
                continue
            candidate = locator.first if hasattr(locator, "first") else locator
            if not hasattr(candidate, "is_visible") or await candidate.is_visible():
                return True
        except (AttributeError, TypeError):
            return False
    return False


async def list_visible_web_chat_targets(page, timeout=30000):
    """Return unique, visible conversation titles from the signed-in chat list."""
    await page.wait_for_selector(CONVERSATION_ITEM_SELECTOR, timeout=timeout)
    targets = []
    seen = set()
    for item in await page.locator(CONVERSATION_ITEM_SELECTOR).all():
        if hasattr(item, "is_visible") and not await item.is_visible():
            continue
        title = (
            await item.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
        ).strip()
        if title and title not in seen:
            seen.add(title)
            targets.append(title)
    return targets


async def _click_search_result(result) -> None:
    try:
        send_button = result.locator(
            "xpath=ancestor::*[.//*[normalize-space()='发消息']][1]"
            "//*[normalize-space()='发消息' and "
            "not(.//*[normalize-space()='发消息'])]"
        )
        if await send_button.count() > 0 and await send_button.is_visible():
            await send_button.click()
            return
    except (AttributeError, TypeError):
        pass
    await result.click()


async def _wait_for_visible_search_results(page, candidate, timeout_ms):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(3.0, max(0.2, timeout_ms / 1000))
    while True:
        exact = page.get_by_text(candidate, exact=True)
        if await exact.count() > 0:
            results = await exact.all() if hasattr(exact, "all") else [exact.first]
            visible = []
            for result in results:
                if hasattr(result, "is_visible") and not await result.is_visible():
                    continue
                visible.append(result)
            if visible:
                return visible
        remaining = deadline - loop.time()
        if remaining <= 0:
            return []
        await asyncio.sleep(min(0.2, remaining))


def _preferred_conversation(matches, candidates):
    # API aliases put the current remark first. A uniquely matching remark
    # must not be made ambiguous by another contact's shared nickname.
    for name in candidates:
        exact = [pair for pair in matches if pair[1] == name]
        if len(exact) > 1:
            raise AmbiguousTargetError('存在多个同名好友，请设置不同的备注后重新选择')
        if exact:
            return exact[0]
    return None


async def select_web_chat_target(page, target, timeout=30000, aliases=()):
    """Select one exact target, preferring real conversation rows over page text."""
    normalized_target = target.strip()
    candidates = tuple(
        dict.fromkeys(
            value.strip() for value in (*aliases, normalized_target) if value and value.strip()
        )
    )

    matches = []
    for item in await page.locator(CONVERSATION_ITEM_SELECTOR).all():
        if hasattr(item, "is_visible") and not await item.is_visible():
            continue
        title = (
            await item.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
        ).strip()
        if title in candidates:
            matches.append((item, title))
    preferred = _preferred_conversation(matches, candidates)
    if preferred:
        await preferred[0].click()
        return preferred[1]

    for selector in SEARCH_INPUT_SELECTORS:
        try:
            search = page.locator(selector)
            if await search.count() == 0:
                continue
            field = search.first
            for candidate in candidates:
                await field.fill(candidate)
                results = await _wait_for_visible_search_results(
                    page, candidate, timeout
                )
                if len(results) > 1:
                    raise AmbiguousTargetError('搜索结果存在多个同名好友，请设置不同的备注后重新选择')
                if results:
                    await _click_search_result(results[0])
                    return candidate
        except (AttributeError, TypeError):
            # Older page doubles and older layouts have no global search surface.
            break

    await page.wait_for_selector(CONVERSATION_ITEM_SELECTOR, timeout=timeout)

    matches = []
    for item in await page.locator(CONVERSATION_ITEM_SELECTOR).all():
        if hasattr(item, "is_visible") and not await item.is_visible():
            continue
        title = (
            await item.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
        ).strip()
        if title in candidates:
            matches.append((item, title))
    preferred = _preferred_conversation(matches, candidates)
    if preferred:
        await preferred[0].click()
        return preferred[1]

    raise TargetNotFoundError(f"未在抖音聊天列表中找到好友 {normalized_target}")


async def run_wz_web_chat_probe():
    """Send and verify one WZ message through https://www.douyin.com/chat."""
    from core.browser import get_browser
    from core.msg_builder import build_message
    from core.tasks import confirm_message_sent
    from utils.config import get_userData

    users = get_userData()
    if len(users) != 1:
        raise RuntimeError("WZ 单向测试必须且只能包含一个账号")

    user = users[0]
    targets = user.get("targets", [])
    if len(targets) != 1:
        raise RuntimeError("WZ 单向测试必须且只能包含一个目标好友")

    username = user.get("username", "未知用户")
    target = targets[0]
    playwright, browser = await get_browser()
    context = None

    try:
        context = await browser.new_context()
        context.set_default_navigation_timeout(120000)
        context.set_default_timeout(120000)
        await context.add_cookies(user["cookies"])
        page = await context.new_page()
        await page.goto(WEB_CHAT_URL)

        try:
            await select_web_chat_target(page, target)
            await page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=30000)
        except Exception:
            os.makedirs(os.path.join("logs", "diagnostics"), exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(
                path=os.path.join(
                    "logs", "diagnostics", f"web_chat_probe_{timestamp}.png"
                ),
                full_page=True,
            )
            raise

        chat_input = page.locator(CHAT_EDITOR_SELECTOR).first
        message = build_message()
        lines = message.split("\n")
        for index, line in enumerate(lines):
            await chat_input.type(line)
            if index < len(lines) - 1:
                await chat_input.press("Shift+Enter")

        logger.info(f"账号 {username} 准备通过抖音网页聊天发送消息给 {target}")
        await chat_input.press("Enter")
        await confirm_message_sent(page, chat_input, message)
        logger.info(f"账号 {username} 给好友 {target} 发送消息并确认送达完成")
    finally:
        if context is not None:
            await context.close()
        await browser.close()
        await playwright.stop()
