import json
import tempfile
import unittest
from pathlib import Path

import httpx

from spark_console.resend import ResendTransport


class ResendTransportTests(unittest.TestCase):
    def test_sends_allowlisted_template_with_idempotency_key(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["authorization"] = request.headers["authorization"]
            seen["idempotency"] = request.headers["idempotency-key"]
            seen["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "email-123"})

        transport = ResendTransport(
            "test-api-key", "Spark <notify@example.com>", "https://example.com",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        result = transport.send(
            "event-1", "user@example.com", "verify_email",
            {"code": "123456", "username": "alice"},
        )
        self.assertTrue(result.success)
        self.assertEqual("email-123", result.provider_id)
        self.assertEqual("Bearer test-api-key", seen["authorization"])
        self.assertEqual("event-1", seen["idempotency"])
        self.assertNotIn("test-api-key", repr(result))
        self.assertIn("123456", seen["payload"]["html"])

    def test_classifies_retryable_and_permanent_failures_without_body(self):
        for status, retryable in ((429, True), (500, True), (403, False)):
            with self.subTest(status=status):
                client = httpx.Client(transport=httpx.MockTransport(
                    lambda _request, status=status: httpx.Response(
                        status, text="sensitive provider response"
                    )
                ))
                result = ResendTransport(
                    "key", "Spark <notify@example.com>", "https://example.com", client=client
                ).send("event", "u@example.com", "verify_email", {"code": "123456"})
                self.assertFalse(result.success)
                self.assertEqual(retryable, result.retryable)
                self.assertNotIn("sensitive", repr(result))

    def test_task_failure_email_escapes_content_and_links_to_task(self):
        seen = {}

        def handler(request: httpx.Request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "task-email"})

        transport = ResendTransport(
            "key",
            "Spark <notify@example.com>",
            "https://example.com",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        result = transport.send(
            "event-task",
            "user@example.com",
            "task_failure",
            {
                "target_name": "<旧备注>",
                "reason": "请检查好友昵称或备注是否已经修改。",
                "action_path": "/tasks/task-1/edit",
            },
        )
        self.assertTrue(result.success)
        self.assertIn("https://example.com/tasks/task-1/edit", seen["html"])
        self.assertIn("备注", seen["html"])
        self.assertNotIn("<旧备注>", seen["html"])


if __name__ == "__main__":
    unittest.main()
