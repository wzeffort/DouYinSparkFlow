from __future__ import annotations

import html
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ProviderResult:
    success: bool
    provider_id: str | None = None
    error_code: str | None = None
    retryable: bool = False


class ResendTransport:
    ENDPOINT = "https://api.resend.com/emails"

    def __init__(
        self,
        api_key: str,
        sender: str,
        public_base_url: str,
        *,
        client: httpx.Client | None = None,
    ):
        self._api_key = api_key
        self.sender = sender
        self.public_base_url = public_base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=15)

    def send(
        self,
        event_id: str,
        recipient: str,
        template_key: str,
        payload: dict[str, str],
    ) -> ProviderResult:
        subject, body = self._render(template_key, payload)
        try:
            response = self.client.post(
                self.ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": event_id,
                    "Content-Type": "application/json",
                },
                json={
                    "from": self.sender,
                    "to": [recipient],
                    "subject": subject,
                    "html": body,
                },
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            return ProviderResult(False, error_code="provider_unavailable", retryable=True)
        if response.is_success:
            try:
                provider_id = str(response.json().get("id") or "")[:128]
            except ValueError:
                provider_id = ""
            return ProviderResult(True, provider_id=provider_id or "accepted")
        retryable = response.status_code == 429 or response.status_code >= 500
        return ProviderResult(
            False,
            error_code=f"provider_http_{response.status_code}",
            retryable=retryable,
        )

    def _render(self, template_key: str, payload: dict[str, str]) -> tuple[str, str]:
        if template_key == "verify_email":
            code = html.escape(str(payload.get("code", "")))
            username = html.escape(str(payload.get("username", "用户")))
            return (
                "火花守护邮箱验证码",
                f"<h2>验证邮箱</h2><p>{username}，你的验证码是：</p>"
                f"<p style='font-size:28px;font-weight:700;letter-spacing:6px'>{code}</p>"
                "<p>验证码 10 分钟内有效，请勿转发给他人。</p>",
            )
        if template_key == "reset_password":
            code = html.escape(str(payload.get("code", "")))
            username = html.escape(str(payload.get("username", "用户")))
            return (
                "火花守护密码重置验证码",
                f"<h2>重置登录密码</h2><p>{username}，你的验证码是：</p>"
                f"<p style='font-size:28px;font-weight:700;letter-spacing:6px'>{code}</p>"
                "<p>验证码 10 分钟内有效。如果不是你本人操作，请忽略此邮件。</p>",
            )
        if template_key == "douyin_expired":
            account = html.escape(str(payload.get("account_name", "抖音账号")))
            path = str(payload.get("action_path", "/accounts"))
            if not path.startswith("/") or path.startswith("//"):
                path = "/accounts"
            url = html.escape(self.public_base_url + path, quote=True)
            return (
                "抖音登录状态已失效",
                f"<h2>需要重新绑定抖音账号</h2><p>账号“{account}”的登录状态已失效，相关任务已暂停。</p>"
                f"<p><a href='{url}'>登录火花守护并重新绑定</a></p>"
                "<p>为保护账号安全，邮件中不包含 Cookie 或完整凭据信息。</p>",
            )
        if template_key == "task_failure":
            target = html.escape(str(payload.get("target_name", "目标好友")))
            reason = html.escape(str(payload.get("reason", "任务执行失败")))
            path = str(payload.get("action_path", "/tasks"))
            if not path.startswith("/") or path.startswith("//"):
                path = "/tasks"
            url = html.escape(self.public_base_url + path, quote=True)
            return (
                "续火任务需要处理",
                f"<h2>续火任务需要处理</h2><p>目标好友“{target}”的任务执行异常。</p>"
                f"<p>{reason}</p><p><a href='{url}'>查看并编辑任务</a></p>"
                "<p>邮件中不包含消息内容、Cookie 或完整账号凭据。</p>",
            )
        raise ValueError("unsupported email template")
