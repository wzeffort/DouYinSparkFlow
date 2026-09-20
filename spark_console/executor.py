from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from enum import StrEnum

from core.web_chat import (
    CHAT_EDITOR_SELECTOR,
    CHAT_INPUT_SELECTOR,
    WEB_CHAT_URL,
    TargetNotFoundError,
    UserInfoCollector,
    WebChatLoginRequiredError,
    page_has_web_chat_login_prompt,
    select_web_chat_target,
    verify_chat_recipient_name,
    RecipientNameError,
    AmbiguousTargetError,
    ChatReadinessError,
    wait_for_chat_ready,
)
from spark_console.credentials import CredentialError, CredentialPayload
from spark_console.execution_diagnostics import trace_phase
from core.im_evidence import (ConversationChanged, select_identity, current_identity,
                              verify_conversation, SendMonitor)
from core.page_send_evidence import PageSendEvidence


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
    recipient_diagnostic: dict | None = None
    send_receipt: dict | None = None


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
                page = await context.new_page()
                page.on("response", identities.capture)
                try:
                    trace_phase('navigation')
                    await page.goto(WEB_CHAT_URL, wait_until="domcontentloaded", timeout=45000)
                    trace_phase('chat_ready')
                    await wait_for_chat_ready(page, timeout=30000)
                except ChatReadinessError as error:
                    result = ExecutionResult(False, "authenticating", error.code,
                                             str(error), error.code != 'login_expired')
                    for position in range(len(recipients)):
                        await on_result(position, result)
                        results.append(result)
                    return results
                except Exception as error:
                    result = ExecutionResult(False, "authenticating", "chat_load_failed",
                                             "聊天页面加载失败（" + type(error).__name__ + "）；本批次未发送", True)
                    for position in range(len(recipients)):
                        await on_result(position, result)
                        results.append(result)
                    return results
                for position, recipient in enumerate(recipients):
                    result = await self._batch_recipient(page, identities, recipient, position, before_send)
                    # Persistence failures abort, never advance to another recipient.
                    await on_result(position, result)
                    results.append(result)
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
        phase = "检查登录"
        diagnostic = None
        conversation = None

        async def verify(selected_name):
            nonlocal diagnostic
            approved = tuple(pair['observed_name'] for pair in recipient.get('name_approvals', ())
                             if pair['selected_name'] == selected_name)
            diagnostic = await verify_chat_recipient_name(page, selected_name, approved_names=approved)
            if conversation:
                await verify_conversation(page, conversation['conv_id'], recipient.get('target_sec_uid'))

        trace_phase('authenticating', position)
        try:
            async with asyncio.timeout(40):
                if await page_has_web_chat_login_prompt(page):
                    return ExecutionResult(False, "authenticating", "login_expired", "登录已失效，本批次已停止")
                uid = recipient.get("target_sec_uid")
                identity = identities.get(uid) if uid and identities is not None else None
                phase = "选择好友"
                trace_phase('selecting_target', position)
                chosen = await select_identity(page, uid)
                selected_name = chosen['title'] if chosen else await select_web_chat_target(page, recipient["target_name"], timeout=20000,
                                             aliases=identity.aliases if identity else ())
                conversation = chosen or await current_identity(page, uid)
                phase = "等待聊天输入框"
                trace_phase('editor_ready', position)
                await page.wait_for_selector(CHAT_INPUT_SELECTOR, timeout=10000)
                phase = "核对聊天名称"
                trace_phase('recipient_check', position)
                await verify(selected_name)
                editor = await page.locator(CHAT_INPUT_SELECTOR).first.element_handle()
                if editor is None:
                    raise RuntimeError("chat input detached")
                # Clear any restored unsent draft in the selected conversation.
                phase = "填写消息"
                trace_phase('message_input', position)
                await editor.fill("")
                lines = recipient["message_template"].splitlines()
                for index, line in enumerate(lines):
                    await editor.type(line)
                    if index < len(lines) - 1:
                        await editor.press("Shift+Enter")
                phase = "发送前复核名称"
                trace_phase('recipient_check', position)
                await verify(selected_name)
        except ConversationChanged:
            return ExecutionResult(False, "selecting_target", "recipient_identity_unverified",
                                   "会话身份不一致或发生变化，本次未发送", True)
        except RecipientNameError as error:
            return ExecutionResult(False, "selecting_target", "recipient_name_unverified",
                                   str(error), True, error.diagnostic)
        except AmbiguousTargetError:
            return ExecutionResult(False, "selecting_target", "target_name_ambiguous",
                                   "存在多个同名好友，请设置不同的备注后重新选择；本次未发送")
        except TargetNotFoundError:
            return ExecutionResult(False, "selecting_target", "target_not_found", "未找到完全匹配的好友")
        except Exception as error:
            return ExecutionResult(False, "selecting_target", "conversation_not_opened",
                                   f"发送前失败：{phase}（{type(error).__name__}）", True)

        # Outside the page-error handler: a failed durable checkpoint must abort.
        trace_phase('authorization', position)
        if not await before_send(position):
            return ExecutionResult(False, "authorization", "authorization_ended", "任务已暂停、账号失效或名额到期，本批次已停止")
        try:
            await verify(selected_name)
        except ConversationChanged:
            return ExecutionResult(False, "selecting_target", "recipient_identity_unverified",
                                   "发送前会话身份发生变化，本次未发送", True)
        except RecipientNameError as error:
            return ExecutionResult(False, "selecting_target", "recipient_name_unverified",
                                   "发送检查点后聊天对象尚未确认，本次未发送；请查看诊断", True, error.diagnostic)
        return await self._send_and_confirm(page, editor, recipient['message_template'], conversation, diagnostic)

    async def _send_and_confirm(self, page, editor, message, conversation=None, diagnostic=None):
        """Both task formats use the same one-Enter confirmation boundary."""
        monitor = SendMonitor(page, conversation['conv_id'], message) if conversation else None
        screen = PageSendEvidence(page)
        await screen.start(message)
        try:
            if monitor:
                try:
                    monitor.start()
                except Exception:
                    try:
                        await monitor.close()
                    except Exception:
                        pass
                    monitor = None
            try:
                async with asyncio.timeout(20):
                    trace_phase("sending")
                    await editor.press("Enter")
            except Exception:
                return ExecutionResult(False, "sending", "delivery_uncertain", "发送结果不明，请核实；不会自动重发", recipient_diagnostic=diagnostic)
            trace_phase("confirming")
            try:
                evidence = await monitor.wait() if monitor else None
            except Exception:
                evidence = None
            page_confirmed = False
            if not evidence and screen.armed:
                deadline = asyncio.get_running_loop().time() + 6
                while asyncio.get_running_loop().time() < deadline:
                    if monitor and monitor.result:
                        evidence = monitor.result
                        break
                    if await screen.confirmed():
                        page_confirmed = True
                        break
                    await asyncio.sleep(.25)
            # A late explicit rejection takes precedence over page-only evidence.
            if monitor:
                if monitor.pending:
                    await asyncio.gather(*list(monitor.pending), return_exceptions=True)
                evidence = monitor.result or evidence
                if len(monitor.requests) > 1:
                    evidence, page_confirmed = None, False
            if evidence and evidence['status'] == 'rejected':
                return ExecutionResult(False, "rejected", "delivery_rejected", "服务端拒绝本次发送；不会自动重发", recipient_diagnostic=diagnostic, send_receipt=evidence)
            if evidence and evidence['status'] == 'accepted':
                return ExecutionResult(True, "complete", error_summary="服务端已接受（不代表对方已读）", recipient_diagnostic=diagnostic, send_receipt=evidence)
            if page_confirmed:
                if conversation:
                    try:
                        await verify_conversation(page, conversation['conv_id'])
                    except Exception:
                        page_confirmed = False
                if page_confirmed:
                    if monitor and monitor.result:
                        if monitor.result.get('status') == 'rejected':
                            return ExecutionResult(False, "rejected", "delivery_rejected", "服务端拒绝本次发送；不会自动重发", recipient_diagnostic=diagnostic, send_receipt=monitor.result)
                        if monitor.result.get('status') == 'accepted':
                            return ExecutionResult(True, "complete", error_summary="服务端已接受（不代表对方已读）", recipient_diagnostic=diagnostic, send_receipt=monitor.result)
                    return ExecutionResult(True, "page_confirmed", error_summary="成功（页面确认）：当前会话出现本次新增的发送气泡，未取得服务端回执", recipient_diagnostic=diagnostic, send_receipt={'status':'page_confirmed'})
            return ExecutionResult(True, "submitted", "delivery_confirmation_unavailable", "消息已提交，未取得回执或可靠页面确认；不会自动重发", recipient_diagnostic=diagnostic, send_receipt={'status':'unknown', 'identity':'会话标识可读' if conversation else '会话标识不可读'})
        except Exception:
            return ExecutionResult(True, "submitted", "delivery_confirmation_unavailable", "发送确认异常，结果待核实；不会自动重发", recipient_diagnostic=diagnostic)
        finally:
            await screen.close()
            if monitor:
                try:
                    await monitor.close()
                except Exception:
                    pass

    async def execute(
        self,
        cookie_payload: bytes | bytearray,
        target: str,
        message: str,
        credential_version: int = 1,
        target_sec_uid: str | None = None,
    ) -> ExecutionResult:
        from playwright.async_api import async_playwright

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
                    trace_phase('navigation')
                    await page.goto(WEB_CHAT_URL, wait_until="domcontentloaded", timeout=120000)
                    trace_phase('chat_ready')
                    await wait_for_chat_ready(page, timeout=30000)
                    if await page_has_web_chat_login_prompt(page):
                        raise WebChatLoginRequiredError("Douyin login is required")
                    stage = ExecutionStage.SELECTING_TARGET
                    trace_phase('selecting_target')
                    identity = (
                        user_info.get(target_sec_uid)
                        if target_sec_uid
                        else None
                    )
                    for attempt in range(2):
                        try:
                            trace_phase('selecting_target')
                            selected_name = await select_web_chat_target(
                                page,
                                target,
                                timeout=45000 if attempt == 0 else 15000,
                                aliases=identity.aliases if identity else (),
                            )
                            trace_phase('editor_ready')
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
                    trace_phase('recipient_check')
                    await verify_chat_recipient_name(page, selected_name)
                    editor = page.locator(CHAT_INPUT_SELECTOR).first
                    await editor.fill("")
                    trace_phase('message_input')
                    lines = message.splitlines() or [message]
                    for index, line in enumerate(lines):
                        await editor.type(line)
                        if index < len(lines) - 1:
                            await editor.press("Shift+Enter")
                    trace_phase('recipient_check')
                    await verify_chat_recipient_name(page, selected_name)
                    conversation = await current_identity(page, target_sec_uid)
                    stage = ExecutionStage.SENDING
                    trace_phase('sending')
                    message_submitted = True
                    return await self._send_and_confirm(page, editor, message, conversation)
                finally:
                    try:
                        if context is not None:
                            await context.close()
                    finally:
                        await browser.close()
        except ChatReadinessError as error:
            return ExecutionResult(False, ExecutionStage.AUTHENTICATING, error.code,
                                   str(error), error.code != 'login_expired')
        except RecipientNameError as error:
            return ExecutionResult(False, ExecutionStage.SELECTING_TARGET, "recipient_name_unverified",
                                   str(error), True, error.diagnostic)
        except AmbiguousTargetError:
            return ExecutionResult(False, ExecutionStage.SELECTING_TARGET, "target_name_ambiguous",
                                   "存在多个同名好友，请设置不同的备注后重新选择；本次未发送")
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
