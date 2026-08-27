from __future__ import annotations

import unittest
from unittest import mock

from sefbot import ai_control, config, db
from sefbot.services.llm_client import LLMClient


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

    async def test_tool_call_uses_user_scope_and_global_budgets(self) -> None:
        response = mock.MagicMock()
        response.status_code = 200
        response.content = b'{"choices":[{"message":{"content":"ok"}}]}'
        response.json.return_value = {
            "choices": [{"message": {"content": "ok", "tool_calls": []}}]
        }
        with mock.patch.object(self.client._client, "post", new=mock.AsyncMock(return_value=response)):
            text, calls = await self.client.chat_with_tools(
                "tool-model",
                [{"role": "user", "content": "help"}],
                [],
                scope_id="guild:1",
                user_id="42",
            )
        self.assertEqual(text, "ok")
        self.assertEqual(calls, [])
        for key in (("global", "*"), ("scope", "guild:1"), ("user", "42")):
            self.assertEqual(len(ai_control._usage[key]), 1)
            self.assertEqual(len(ai_control._token_usage[key]), 1)


if __name__ == "__main__":
    unittest.main()
