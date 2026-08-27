from __future__ import annotations

import collections
import time
import unittest
from unittest import mock

from sefbot import ai, ai_control, config, db


class AIControlTest(unittest.TestCase):
    def setUp(self) -> None:
        db.close()
        self.old_path = config.DB_PATH
        config.DB_PATH = ":memory:"
        ai_control._health.clear()
        ai_control._usage.clear()
        ai_control._token_usage.clear()
        ai_control._provider_attempts.clear()

    def tearDown(self) -> None:
        db.close()
        config.DB_PATH = self.old_path
        ai_control._health.clear()
        ai_control._usage.clear()
        ai_control._token_usage.clear()
        ai_control._provider_attempts.clear()

    def test_user_budget_cannot_be_bypassed_by_task_or_scope(self) -> None:
        with mock.patch.object(config, "AI_REQUESTS_PER_MINUTE", 50), mock.patch.object(
            config, "AI_USER_REQUESTS_PER_MINUTE", 2
        ):
            ai_control.check_request_budget("guild:1", "chat", user_id="42")
            ai_control.check_request_budget("dm:42", "workflow", user_id="42")
            with self.assertRaises(ai_control.AIBudgetExceeded):
                ai_control.check_request_budget("guild:2", "vision", user_id="42")

    def test_scope_budget_is_aggregate_across_users_and_tasks(self) -> None:
        db.guild_settings_set("guild:1", ai_requests_per_minute=2)
        with mock.patch.object(config, "AI_REQUESTS_PER_MINUTE", 50), mock.patch.object(
            config, "AI_USER_REQUESTS_PER_MINUTE", 50
        ):
            ai_control.check_request_budget("guild:1", "chat", user_id="1")
            ai_control.check_request_budget("guild:1", "workflow", user_id="2")
            with self.assertRaises(ai_control.AIBudgetExceeded):
                ai_control.check_request_budget("guild:1", "vision", user_id="3")

    def test_provider_attempt_token_reservation_is_atomic(self) -> None:
        with mock.patch.object(config, "AI_PROVIDER_ATTEMPTS_PER_MINUTE", 10), mock.patch.object(
            config, "AI_TOKEN_BUDGET_PER_MINUTE", 1_000
        ), mock.patch.object(config, "AI_USER_TOKEN_BUDGET_PER_MINUTE", 1_000):
            ai_control.reserve_provider_attempt(user_id="42", estimated_tokens=600)
            with self.assertRaises(ai_control.AIBudgetExceeded):
                ai_control.reserve_provider_attempt(user_id="42", estimated_tokens=600)
        self.assertEqual(len(ai_control._provider_attempts), 1)
        self.assertEqual(len(ai_control._token_usage[("global", "*")]), 1)

    def test_multimodal_estimate_does_not_count_base64_transport_as_text(self) -> None:
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + ("A" * 1_000_000)},
                },
            ],
        }]
        self.assertLess(ai_control.estimate_chat_tokens("vision", messages), 3_000)

    def test_expired_identity_buckets_are_removed(self) -> None:
        ai_control._usage[("user", "old")] = collections.deque([1.0])
        ai_control._token_usage[("user", "old")] = collections.deque([(1.0, 100)])
        with mock.patch.object(ai_control.time, "monotonic", return_value=100.0):
            ai_control.check_request_budget("dm:42", "chat", user_id="42")
        self.assertNotIn(("user", "old"), ai_control._usage)
        self.assertNotIn(("user", "old"), ai_control._token_usage)

    def test_modes_route_by_policy_without_weakening_capabilities(self) -> None:
        ai_control.set_user_mode("42", "reasoning")
        self.assertEqual(ai_control.route("workflow", user_id="42").tier, "expert")
        self.assertEqual(ai_control.route("memory_extract", user_id="42").tier, "fast")
        ai_control.set_user_mode("42", "fast")
        self.assertEqual(ai_control.route("chat", user_id="42").tier, "fast")
        self.assertEqual(ai_control.route("vision", user_id="42").tier, "vision")

    def test_circuit_opens_and_success_recovers(self) -> None:
        with mock.patch.object(config, "AI_CIRCUIT_FAILURES", 2), mock.patch.object(
            config, "AI_CIRCUIT_COOLDOWN_SECONDS", 60.0
        ):
            ai_control.record_provider_result("model-a", success=False, latency_ms=10)
            self.assertTrue(ai_control.provider_available("model-a"))
            ai_control.record_provider_result("model-a", success=False, latency_ms=20)
            self.assertFalse(ai_control.provider_available("model-a"))
            ai_control.record_provider_result("model-a", success=True, latency_ms=5)
            self.assertTrue(ai_control.provider_available("model-a"))

    def test_context_preserves_required_and_trims_optional(self) -> None:
        result = ai_control.assemble_context(
            ["HARD SAFETY", "PRIVACY"],
            [(20, "x" * 500), (10, "useful context")],
            max_chars=80,
        )
        self.assertIn("HARD SAFETY", result)
        self.assertIn("PRIVACY", result)
        self.assertIn("useful context", result)
        self.assertLessEqual(len(result), 80)

    def test_trace_is_metadata_only_and_summarized(self) -> None:
        db.ai_trace_record(
            trace_id="ai_test", scope_id="guild:1", task="chat", route="smart",
            requested_model="model-a", served_model="model-b", prompt_version="brain-v5",
            status="success", latency_ms=123, input_tokens=100, output_tokens=30,
            attempts=2, fallbacks=1,
        )
        recent = db.ai_traces_recent("guild:1")
        self.assertEqual(recent[0]["trace_id"], "ai_test")
        self.assertNotIn("prompt", recent[0])
        self.assertNotIn("response", recent[0])
        summary = db.ai_trace_summary("guild:1")
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["fallback_requests"], 1)
        self.assertEqual(summary["success_rate"], 100.0)

    def test_memory_expiry_supersession_and_usage_are_scope_bound(self) -> None:
        old_id = db.add_memory(
            "Uses Windows", "42", "guild:1", subject="42", category="identity"
        )
        new_id = db.add_memory(
            "Switched to macOS", "42", "guild:1", subject="42", category="identity"
        )
        other_id = db.add_memory(
            "Uses Linux", "42", "guild:2", subject="42", category="identity"
        )
        expired_id = db.add_memory(
            "Temporary project", "42", "guild:1", subject="42",
            category="temporary", expires=time.time() - 1,
        )
        self.assertTrue(db.supersede_memory(old_id, new_id, subject="42", scope_id="guild:1"))
        self.assertFalse(db.supersede_memory(other_id, new_id, subject="42", scope_id="guild:1"))
        ids = {int(row["id"]) for row in db.memories_about("42", "guild:1")}
        self.assertEqual(ids, {new_id})
        self.assertNotIn(expired_id, ids)
        db.mark_memories_used([new_id])
        self.assertEqual(int(db.get_memory(new_id)["use_count"]), 1)

    def test_conversation_summary_is_consent_scoped_and_cleared(self) -> None:
        db.privacy_set_opt_in("42", "guild:1", True)
        db.guild_settings_set("guild:1", history_enabled=True)
        db.conversation_summary_set("42", "guild:1", "Ongoing design discussion", 100.0)
        self.assertEqual(
            db.conversation_summary_get("42", "guild:1")["summary"],
            "Ongoing design discussion",
        )
        self.assertIsNone(db.conversation_summary_get("42", "guild:2"))
        db.convo_clear("42", "guild:1")
        self.assertIsNone(db.conversation_summary_get("42", "guild:1"))


class StructuredRepairTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        db.close()
        self.old_path = config.DB_PATH
        config.DB_PATH = ":memory:"

    async def asyncTearDown(self) -> None:
        db.close()
        config.DB_PATH = self.old_path

    async def test_invalid_structured_output_gets_one_validated_repair(self) -> None:
        provider = mock.AsyncMock(
            side_effect=["not json", '{"response":"repaired","actions":[],"memories":[]}']
        )
        with mock.patch.object(ai, "chat", new=provider):
            result = await ai.structured(
                "system", [{"role": "user", "content": "hi"}],
                schema="brain_response", scope_id="guild:1",
            )
        self.assertEqual(result["response"], "repaired")
        self.assertEqual(provider.await_count, 2)

    async def test_repair_still_rejects_unknown_fields(self) -> None:
        provider = mock.AsyncMock(
            side_effect=["not json", '{"response":"x","root_access":true}']
        )
        with mock.patch.object(ai, "chat", new=provider):
            result = await ai.structured(
                "system", [{"role": "user", "content": "hi"}],
                schema="brain_response", scope_id="guild:1",
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
