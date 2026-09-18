from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from enum import StrEnum

from core.web_chat import (
    CHAT_EDITOR_SELECTOR,
    WEB_CHAT_URL,
    TargetNotFoundError,
    UserInfoCollector,
    WebChatLoginRequiredError,
    page_has_web_chat_login_prompt,
    select_web_chat_target,
    verify_chat_recipient_uid,
    RecipientIdentityError,
)
from spark_console.credentials import CredentialError, CredentialPayload


logger = logging.getLogger(__name__)


class ExecutionStage(StrEnum):
    STARTING = "starting"
    AUTHENTICATING = "authenticating"
    SELECTING_TARGET = "selecting_target"
    SENDING = "sending"
    CONFIRMING = "confirming"
    SUBMITTED = "submitted"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    stage: str
    error_code: str | None = None
    error_summary: str | None = None
    retryable: bool = False


class DouyinExecutor:
    async def execute_batch(self, cookie_payload, recipients, *, before_send, on_result,
                            credential_version=1):
        """One isolated session per batch. Callbacks must commit before returning."""
        from playwright.async_api import async_playwright

        results = []
        try:
            payload = CredentialPayload.parse(bytes(cookie_payload), credential_version)
        except CredentialError:
            result = ExecutionResult(False, "authenticating", "cookie_invalid", "账号凭据格式无效")
            for position in range(len(recipients)):
                await on_result(position, result)
                results.append(result)
            return results
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = None
            try:
                context = await browser.new_context(**payload.context_options())
                cookies = payload.cookies_to_add()
                if cookies:
                    await context.add_cookies(cookies)
                identities = UserInfoCollector()
                for position, recipient in enumerate(recipients):
                    # Reuse the browser/login context, not the previous chat's composer.
                    page = await context.new_page()
                    try:
                        if recipient.get("target_sec_uid"):
                            page.on("response", identities.capture)
                        await page.goto(WEB_CHAT_URL, wait_until="domcontentloaded", timeout=45000)
                        result = await self._batch_recipient(page, identities, recipient, position, before_send)
                        # Persistence failures abort, never advance to another recipient.
                        await on_result(position, result)
                        results.append(result)
                    finally:
                        await page.close()
                    if result.error_code in {"login_expired", "cookie_invalid", "authorization_ended"}:
                        break
                return results
            finally:
                try:
                    if context is not None:
                        await context.close()
                finally:
                    await browser.close()

    async def _batch_recipient(self, page, identities, recipient, position, before_send):
        from core.tasks import confirm_message_sent

        try:
            async with asyncio.timeout(40):
                if await page_has_web_chat_login_prompt(page):
                    return ExecutionResult(False, "authenticating", "login_expired", "登录已失效，本批次已停止")
                existing = page.locator(CHAT_EDITOR_SELECTOR)
                if await existing.count() and await existing.first.is_visible():
                    return ExecutionResult(False, "selecting_target", "unsafe_initial_conversation",
                                           "页面已打开未知会话，无法安全确认好友；本次未发送")
                uid = recipient.get("target_sec_uid")
                identity = await identities.wait_for(uid) if uid else None
                await select_web_chat_target(page, recipient["target_name"], timeout=20000,
                                             aliases=identity.aliases if identity else ())
                await page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=10000)
                await verify_chat_recipient_uid(page, uid)
                editor = page.locator(CHAT_EDITOR_SELECTOR).first
                # Clear any restored unsent draft in the selected conversation.
                await editor.fill("")
                lines = recipient["message_template"].splitlines()
                for index, line in enumerate(lines):
                    await editor.type(line)
                    if index < len(lines) - 1:
                        await editor.press("Shift+Enter")
                await verify_chat_recipient_uid(page, uid)
        except RecipientIdentityError:
            return ExecutionResult(False, "selecting_target", "recipient_identity_unverified",
                                   "无法核对当前聊天对象 UID，已停止发送；请重新选择已识别的好友")
        except TargetNotFoundError:
            return ExecutionResult(False, "selecting_target", "target_not_found", "未找到完全匹配的好友")
        except Exception:
            return ExecutionResult(False, "selecting_target", "conversation_not_opened", "发送前页面操作失败", True)

        # Outside the page-error handler: a failed durable checkpoint must abort.
        if not await before_send(position):
            return ExecutionResult(False, "authorization", "authorization_ended", "任务已暂停、账号失效或名额到期，本批次已停止")
        try:
            async with asyncio.timeout(20):
                await editor.press("Enter")
        except Exception:
            return ExecutionResult(False, "sending", "delivery_uncertain", "发送结果不明，请核实；不会自动重发")
        try:
            async with asyncio.timeout(20):
                await confirm_message_sent(page, editor, recipient["message_template"], timeout=15000)
        except Exception:
            return ExecutionResult(True, "submitted", "delivery_confirmation_unavailable", "消息已提交，页面未能二次确认")
        return ExecutionResult(True, "complete")

    async def execute(
        self,
        cookie_payload: bytes | bytearray,
        target: str,
        message: str,
        credential_version: int = 1,
        target_sec_uid: str | None = None,
    ) -> ExecutionResult:
        from playwright.async_api import async_playwright
        from core.tasks import confirm_message_sent

        stage = ExecutionStage.AUTHENTICATING
        message_submitted = False
        try:
            payload = CredentialPayload.parse(bytes(cookie_payload), credential_version)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = None
                try:
                    context = await browser.new_context(**payload.context_options())
                    legacy_cookies = payload.cookies_to_add()
                    if legacy_cookies:
                        await context.add_cookies(legacy_cookies)
                    page = await context.new_page()
                    user_info = UserInfoCollector()
                    if target_sec_uid:
                        page.on("response", user_info.capture)
                    await page.goto(WEB_CHAT_URL, wait_until="domcontentloaded", timeout=120000)
                    if await page_has_web_chat_login_prompt(page):
                        raise WebChatLoginRequiredError("Douyin login is required")
                    stage = ExecutionStage.SELECTING_TARGET
                    identity = (
                        await user_info.wait_for(target_sec_uid)
                        if target_sec_uid
                        else None
                    )
                    for attempt in range(2):
                        try:
                            await select_web_chat_target(
                                page,
                                target,
                                timeout=45000 if attempt == 0 else 15000,
                                aliases=identity.aliases if identity else (),
                            )
                            await page.wait_for_selector(
                                CHAT_EDITOR_SELECTOR,
                                timeout=15000 if attempt == 0 else 30000,
                            )
                            break
                        except TargetNotFoundError:
                            raise
                        except Exception as error:
                            if await page_has_web_chat_login_prompt(page):
                                raise WebChatLoginRequiredError(
                                    "Douyin login is required"
                                ) from error
                            if attempt == 1:
                                raise
                    await verify_chat_recipient_uid(page, target_sec_uid)
                    editor = page.locator(CHAT_EDITOR_SELECTOR).first
                    lines = message.splitlines() or [message]
                    for index, line in enumerate(lines):
                        await editor.type(line)
                        if index < len(lines) - 1:
                            await editor.press("Shift+Enter")
                    await verify_chat_recipient_uid(page, target_sec_uid)
                    stage = ExecutionStage.SENDING
                    message_submitted = True
                    await editor.press("Enter")
                    stage = ExecutionStage.CONFIRMING
                    try:
                        await confirm_message_sent(page, editor, message, timeout=20000)
                    except Exception:
                        return ExecutionResult(
                            True,
                            ExecutionStage.SUBMITTED,
                            "delivery_confirmation_unavailable",
                            "消息已提交，页面未能二次确认",
                        )
                    return ExecutionResult(True, ExecutionStage.COMPLETE)
                finally:
                    try:
                        if context is not None:
                            await context.close()
                    finally:
                        await browser.close()
        except RecipientIdentityError:
            return ExecutionResult(False, ExecutionStage.SELECTING_TARGET, "recipient_identity_unverified",
                                   "无法核对当前聊天对象 UID，已停止发送；请重新选择已识别的好友")
        except WebChatLoginRequiredError:
            return ExecutionResult(
                False,
                ExecutionStage.AUTHENTICATING,
                "login_expired",
                "抖音账号信息已过期，请重新登录后再试",
            )
        except TargetNotFoundError:
            return ExecutionResult(False, ExecutionStage.SELECTING_TARGET, "target_not_found", "未找到完全匹配的目标好友")
        except CredentialError:
            return ExecutionResult(False, ExecutionStage.AUTHENTICATING, "cookie_invalid", "账号凭据格式无效")
        except Exception as error:
            logger.warning(
                "douyin execution failed stage=%s exception=%s",
                stage,
                type(error).__name__,
            )
            if stage == ExecutionStage.SELECTING_TARGET:
                return ExecutionResult(
                    False,
                    ExecutionStage.SELECTING_TARGET,
                    "conversation_not_opened",
                    "已找到好友，但聊天窗口没有打开",
                    retryable=True,
                )
            return ExecutionResult(
                False,
                stage,
                "automation_failed",
                "页面操作或发送确认失败",
                retryable=not message_submitted,
            )
