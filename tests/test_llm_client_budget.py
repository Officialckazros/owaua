from __future__ import annotations

import unittest
from unittest import mock

from owaua import ai_control, config, db
from owaua.services.llm_client import LLMClient


class LLMClientBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        db.close()
        self.old_path = config.DB_PATH
        config.DB_PATH = ":memory:"
        ai_control._usage.clear()
        ai_control._token_usage.clear()
        ai_control._provider_attempts.clear()
        self.client = LLMClient(
            base_url="https://provider.example/v1", api_key="test-key", max_retries=0
        )

    async def asyncTearDown(self) -> None:
        await self.client.close()
        db.close()
        config.DB_PATH = self.old_path
        ai_control._usage.clear()
        ai_control._token_usage.clear()
        ai_control._provider_attempts.clear()

    async def test_tool_call_records_usage_without_a_second_budget(self) -> None:
        response = mock.MagicMock()
        response.status_code = 200
        response.content = b'{"choices":[{"message":{"content":"ok"}}]}'
        response.json.return_value = {"choices": [{"message": {"content": "ok", "tool_calls": []}}]}
        with mock.patch.object(
            self.client._client, "post", new=mock.AsyncMock(return_value=response)
        ):
            text, calls = await self.client.chat_with_tools(
                "tool-model",
                [{"role": "user", "content": "help"}],
                [],
                scope_id="guild:1",
                user_id="42",
            )
        self.assertEqual(text, "ok")
        self.assertEqual(calls, [])
        self.assertEqual(len(ai_control._usage[("user", "42")]), 1)
        for key in (("global", "*"), ("scope", "guild:1"), ("user", "42")):
            self.assertEqual(len(ai_control._token_usage[key]), 1)

    async def test_free_openai_moderation_accepts_images_without_paid_spend(self) -> None:
        response = mock.MagicMock()
        response.status_code = 200
        response.content = b'{"results":[{"flagged":true}]}'
        response.json.return_value = {
            "results": [
                {
                    "flagged": True,
                    "categories": {"violence": True},
                    "category_scores": {"violence": 0.96},
                }
            ]
        }
        post = mock.AsyncMock(return_value=response)
        with mock.patch.object(self.client._client, "post", new=post):
            result = await self.client.moderate(
                "omni-moderation-latest",
                "test",
                image_bytes=b"\x89PNG\r\n\x1a\nimage",
                image_mime="image/png",
                scope_id="guild:1",
                user_id="42",
            )
        self.assertTrue(result["flagged"])
        self.assertEqual(result["category"], "violence")
        payload = post.await_args.kwargs["json"]
        self.assertEqual(payload["model"], "omni-moderation-latest")
        self.assertTrue(payload["input"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(db.ai_spend_summary()["month_requests"], 0)


if __name__ == "__main__":
    unittest.main()
