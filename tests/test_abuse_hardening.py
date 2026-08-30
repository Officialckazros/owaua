"""Unit tests for bot abuse hardening and cost protection security."""

from __future__ import annotations

import typing
import unittest
from unittest import mock

from owaua import ai_control, config, db, textfiles, tos


class DummyAttachment:
    def __init__(
        self,
        filename: str = "test.txt",
        content_type: str = "text/plain",
        size: int = 100,
        content: bytes = b"Hello world from text file!",
    ):
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self._content = content

    async def read(self) -> bytes:
        return self._content


class DummyMessage:
    def __init__(
        self,
        content: str = "",
        attachments: list[typing.Any] | None = None,
        reference: object | None = None,
    ):
        self.content = content
        self.attachments = attachments or []
        self.reference = reference


class AbuseHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db.close()
        self.old_path = config.DB_PATH
        config.DB_PATH = ":memory:"
        ai_control._health.clear()
        ai_control._usage.clear()
        ai_control._usage_hourly.clear()
        ai_control._usage_daily.clear()
        ai_control._token_usage.clear()
        ai_control._token_usage_hourly.clear()
        ai_control._token_usage_daily.clear()
        ai_control._provider_attempts.clear()
        ai_control._active_users.clear()
        ai_control._search_usage.clear()
        ai_control._tts_usage.clear()
        ai_control._recent_queries.clear()
        tos._rate_buckets.clear()
        tos._hammer_buckets.clear()
        tos._quarantine_until.clear()

    def tearDown(self) -> None:
        db.close()
        config.DB_PATH = self.old_path
        ai_control._health.clear()
        ai_control._usage.clear()
        ai_control._usage_hourly.clear()
        ai_control._usage_daily.clear()
        ai_control._token_usage.clear()
        ai_control._token_usage_hourly.clear()
        ai_control._token_usage_daily.clear()
        ai_control._provider_attempts.clear()
        ai_control._active_users.clear()
        ai_control._search_usage.clear()
        ai_control._tts_usage.clear()
        ai_control._recent_queries.clear()
        tos._rate_buckets.clear()
        tos._hammer_buckets.clear()
        tos._quarantine_until.clear()

    def test_multi_window_request_budget(self) -> None:
        """Verify minute, hourly, and daily request budgets are enforced."""
        with (
            mock.patch.object(config, "AI_REQUESTS_PER_MINUTE", 100),
            mock.patch.object(config, "AI_REQUESTS_PER_HOUR", 200),
            mock.patch.object(config, "AI_REQUESTS_PER_DAY", 500),
            mock.patch.object(config, "AI_USER_REQUESTS_PER_MINUTE", 2),
            mock.patch.object(config, "AI_USER_REQUESTS_PER_HOUR", 3),
            mock.patch.object(config, "AI_USER_REQUESTS_PER_DAY", 4),
        ):
            ai_control.check_request_budget("guild:1", "chat", user_id="123")
            ai_control.check_request_budget("guild:1", "chat", user_id="123")
            with self.assertRaises(ai_control.AIBudgetExceeded):
                ai_control.check_request_budget("guild:1", "chat", user_id="123")

    def test_multi_window_token_budget(self) -> None:
        """Verify token reservations adhere to sliding window ceilings."""
        with (
            mock.patch.object(config, "AI_TOKEN_BUDGET_PER_MINUTE", 50_000),
            mock.patch.object(config, "AI_USER_TOKEN_BUDGET_PER_MINUTE", 10_000),
        ):
            ai_control.reserve_provider_attempt(user_id="123", estimated_tokens=6_000)
            with self.assertRaises(ai_control.AIBudgetExceeded):
                ai_control.reserve_provider_attempt(user_id="123", estimated_tokens=6_000)

    def test_owner_exempt_from_user_budgets(self) -> None:
        """Verify bot owner is exempt from user-level rate and token budgets."""
        with (
            mock.patch.object(config, "OWNER_ID", "9999"),
            mock.patch.object(config, "AI_USER_REQUESTS_PER_MINUTE", 1),
        ):
            ai_control.check_request_budget("guild:1", "chat", user_id="9999")
            ai_control.check_request_budget("guild:1", "chat", user_id="9999")
            ai_control.check_request_budget("guild:1", "chat", user_id="9999")

    async def test_user_in_flight_concurrency_guard(self) -> None:
        """Verify user cannot run multiple AI requests in parallel."""
        async with ai_control.user_ai_guard("user1"):
            with self.assertRaises(ai_control.AIBudgetExceeded) as ctx:
                async with ai_control.user_ai_guard("user1"):
                    pass
            self.assertIn("already have an AI request processing", str(ctx.exception))

        async with ai_control.user_ai_guard("user1"):
            pass

    def test_tool_search_and_tts_rate_limits(self) -> None:
        """Verify search and TTS budgets prevent tool spam."""
        with (
            mock.patch.object(config, "AI_SEARCH_REQUESTS_PER_WINDOW", 2),
            mock.patch.object(config, "AI_TTS_REQUESTS_PER_WINDOW", 2),
        ):
            ai_control.check_search_budget("user_search")
            ai_control.check_search_budget("user_search")
            with self.assertRaises(ai_control.AIBudgetExceeded):
                ai_control.check_search_budget("user_search")

            ai_control.check_tts_budget("user_tts")
            ai_control.check_tts_budget("user_tts")
            with self.assertRaises(ai_control.AIBudgetExceeded):
                ai_control.check_tts_budget("user_tts")

    def test_duplicate_query_check(self) -> None:
        """Verify identical queries within cooldown are flagged."""
        self.assertFalse(
            ai_control.check_duplicate_query("user_dup", "Tell me a joke", min_interval=5.0)
        )
        self.assertTrue(
            ai_control.check_duplicate_query("user_dup", "Tell me a joke", min_interval=5.0)
        )
        self.assertFalse(
            ai_control.check_duplicate_query("user_dup", "Tell me another joke", min_interval=5.0)
        )

    def test_tos_hammer_spam_quarantine_and_escalation(self) -> None:
        """Verify rapid hammer spam triggers quarantine and strike escalation."""
        uid = "7777"
        for _ in range(5):
            tos.rate_limit_retry_after(uid)

        retry = 0.0
        for _ in range(6):
            retry = tos.rate_limit_retry_after(uid)

        self.assertGreaterEqual(retry, 60.0)

    async def test_attachment_clamping(self) -> None:
        """Verify attachment text is clamped and bounded."""
        att = DummyAttachment(filename="huge.txt", content=b"A" * 20_000)
        extracted = await textfiles.read_attachment_text(
            typing.cast(typing.Any, att), max_chars=8_000
        )
        self.assertIsNotNone(extracted)
        self.assertIn("[... truncated", typing.cast(typing.Any, extracted))

        att1 = DummyAttachment(filename="file1.txt", content=b"B" * 7_000)
        att2 = DummyAttachment(filename="file2.txt", content=b"C" * 7_000)
        att3 = DummyAttachment(filename="file3.txt", content=b"D" * 7_000)
        msg = DummyMessage(attachments=[att1, att2, att3])
        all_extracted = await textfiles.extract_message_text_files(
            typing.cast(typing.Any, msg), max_files=2, max_total_chars=12_000
        )
        self.assertIn("file1.txt", all_extracted)
        self.assertIn("file2.txt", all_extracted)
        self.assertNotIn("file3.txt", all_extracted)

    def test_chat_policy_token_ceiling(self) -> None:
        """Verify chat policy is clamped to 1,200 output tokens."""
        policy = ai_control.policy_for("chat")
        self.assertEqual(policy.max_output_tokens, 1_200)
        self.assertEqual(policy.task, "chat")


if __name__ == "__main__":
    unittest.main()
