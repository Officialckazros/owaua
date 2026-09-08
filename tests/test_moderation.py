from __future__ import annotations

import asyncio
import copy
import unittest
from collections import defaultdict, deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import bot as bot_module
from bot import (
    ModerationBlocked,
    ModerationRejected,
    ModerationUnavailable,
    PersonaBot,
    safety_identifier,
)


class FakeResponse:
    def __init__(self, data: object) -> None:
        self.data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.data


class FakeHTTP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.moderation: object = {"results": [{"flagged": False}]}
        self.responses: object = {"output_text": "allowed reply"}

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, copy.deepcopy(kwargs)))
        result = self.moderation if url.endswith("/moderations") else self.responses
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    def calls_to(self, suffix: str) -> list[dict[str, object]]:
        return [kwargs for url, kwargs in self.calls if url.endswith(suffix)]


class FakeMemory:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def append_message(self, **kwargs: object) -> bool:
        if any(record["event_id"] == kwargs["event_id"] for record in self.records):
            return False
        self.records.append(
            {
                "id": len(self.records) + 1,
                "event_id": kwargs["event_id"],
                "role": kwargs["role"],
                "content": kwargs["content"],
                "attachments": kwargs.get("attachments", []),
            }
        )
        return True

    def get_memory(self, scope_id: str, user_id: str) -> tuple[str, list[str], int]:
        return "", [], 0

    def recent_messages(
        self, scope_id: str, user_id: str, *, limit: int
    ) -> list[dict[str, object]]:
        return self.records[-limit:]

    def messages_to_summarize(
        self, scope_id: str, user_id: str, *, keep_recent: int, limit: int
    ) -> list[dict[str, object]]:
        return []


def make_bot() -> tuple[PersonaBot, FakeHTTP, FakeMemory]:
    """Build just the state used by the admission and request methods."""
    instance = object.__new__(PersonaBot)
    http = FakeHTTP()
    memory = FakeMemory()
    instance.provider_http = http
    instance.memory = memory
    instance.rate_windows = defaultdict(deque)
    instance.moderation_failures = defaultdict(deque)
    instance.moderation_blocks = {}
    instance.summary_locks = defaultdict(asyncio.Lock)
    instance.background_tasks = set()
    instance.schedule_memory_refresh = lambda scope_id, user_id: None
    return instance, http, memory


def make_message(
    *,
    text: str = "hello",
    user_id: int = 7,
    image_urls: list[str] | None = None,
    filenames: list[str] | None = None,
    guild_id: int | None = None,
) -> SimpleNamespace:
    attachments = [
        SimpleNamespace(
            content_type="image/png",
            url=url,
            filename=(filenames or [])[index - 1] if filenames else f"image-{index}.png",
        )
        for index, url in enumerate(image_urls or [], start=1)
    ]
    return SimpleNamespace(
        id=99,
        author=SimpleNamespace(id=user_id),
        channel=SimpleNamespace(id=123),
        attachments=attachments,
        created_at=datetime.now(timezone.utc),
        content=text,
        guild=(SimpleNamespace(id=guild_id) if guild_id is not None else None),
    )


class ModerationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_benign_text_reaches_responses_only_after_moderation(self) -> None:
        instance, http, memory = make_bot()

        answer = await instance.ask(make_message(), "hello")

        self.assertEqual(answer, "allowed reply")
        self.assertEqual(len(http.calls_to("/moderations")), 1)
        self.assertEqual(len(http.calls_to("/responses")), 1)
        self.assertEqual(len(memory.records), 2)

    async def test_allowed_input_can_still_use_the_configured_fallback(self) -> None:
        instance, http, _ = make_bot()
        http.responses = [
            httpx.HTTPStatusError(
                "primary unavailable",
                request=httpx.Request("POST", "https://example.test/responses"),
                response=httpx.Response(500),
            ),
            {"output_text": "fallback reply"},
        ]
        with patch.multiple(bot_module, FALLBACK_MODEL="fallback-model", REQUEST_RETRIES=0):
            answer = await instance.ask(make_message(), "hello")

        self.assertEqual(answer, "fallback reply")
        self.assertEqual(len(http.calls_to("/moderations")), 1)
        response_calls = http.calls_to("/responses")
        self.assertEqual(len(response_calls), 2)
        self.assertEqual(response_calls[1]["json"]["model"], "fallback-model")

    async def test_mistral_uses_chat_completions_with_explicit_roleplay(self) -> None:
        instance, http, _ = make_bot()
        instance.selected_model = "mistral"
        http.responses = {
            "choices": [{"message": {"role": "assistant", "content": "mistral reply"}}]
        }
        with patch.multiple(bot_module, MISTRAL_API_KEY="mistral-test"):
            answer = await instance.ask(make_message(), "hello")

        self.assertEqual(answer, "mistral reply")
        self.assertEqual(len(http.calls_to("/moderations")), 1)
        self.assertEqual(len(http.calls_to("/responses")), 0)
        chat_calls = http.calls_to("/chat/completions")
        self.assertEqual(len(chat_calls), 1)
        payload = chat_calls[0]["json"]
        self.assertEqual(payload["model"], bot_module.MISTRAL_MODEL)
        self.assertEqual(payload["safe_prompt"], False)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertIn("EXPLICIT ROLEPLAY POLICY", payload["messages"][0]["content"])

    async def test_deepseek_uses_chat_completions_with_thinking_disabled(self) -> None:
        instance, http, _ = make_bot()
        instance.selected_model = "deepseek"
        http.responses = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "ok wait",
                        "reasoning_content": "We need to respond to the user",
                    }
                }
            ]
        }
        with patch.multiple(bot_module, DEEPSEEK_API_KEY="deepseek-test"):
            answer = await instance.ask(make_message(), "waht")

        self.assertEqual(answer, "ok wait")
        self.assertEqual(len(http.calls_to("/responses")), 0)
        chat_calls = http.calls_to("/chat/completions")
        self.assertEqual(len(chat_calls), 1)
        payload = chat_calls[0]["json"]
        self.assertEqual(payload["model"], bot_module.DEEPSEEK_MODEL)
        self.assertEqual(payload["thinking"], {"type": "disabled"})

    async def test_deepseek_does_not_make_a_second_completion_for_leaked_reasoning(self) -> None:
        instance, http, _ = make_bot()
        instance.selected_model = "deepseek"
        http.responses = [
            {
                "choices": [
                    {
                        "message": {
                            "content": "We need to respond to the user who keeps saying vibrator"
                        }
                    }
                ]
            },
            {"choices": [{"message": {"content": "ok wait"}}]},
        ]
        with patch.multiple(bot_module, DEEPSEEK_API_KEY="deepseek-test"):
            answer = await instance.ask(make_message(), "waht")

        self.assertEqual(answer, "huh")
        self.assertEqual(len(http.calls_to("/chat/completions")), 1)

    async def test_mistral_allows_adult_sexual_flags_but_not_minors(self) -> None:
        instance, http, memory = make_bot()
        instance.selected_model = "mistral"
        http.moderation = {
            "results": [
                {
                    "flagged": True,
                    "categories": {"sexual": True, "sexual/minors": False},
                }
            ]
        }
        http.responses = {
            "choices": [{"message": {"role": "assistant", "content": "explicit ok"}}]
        }
        with patch.multiple(bot_module, MISTRAL_API_KEY="mistral-test"):
            answer = await instance.ask(make_message(text="erp"), "erp")

        self.assertEqual(answer, "explicit ok")
        self.assertEqual(len(http.calls_to("/chat/completions")), 1)
        self.assertEqual(len(memory.records), 2)

        http.moderation = {
            "results": [
                {
                    "flagged": True,
                    "categories": {"sexual": True, "sexual/minors": True},
                }
            ]
        }
        with patch.multiple(bot_module, MISTRAL_API_KEY="mistral-test"):
            with self.assertRaises(ModerationRejected):
                await instance.ask(make_message(text="blocked", user_id=8), "blocked")
        self.assertEqual(len(http.calls_to("/chat/completions")), 1)

    async def test_flagged_text_is_not_sent_to_responses_or_memory(self) -> None:
        instance, http, memory = make_bot()
        http.moderation = {"results": [{"flagged": True}]}

        with self.assertRaises(ModerationRejected):
            await instance.ask(make_message(text="blocked"), "blocked")

        self.assertEqual(len(http.calls_to("/responses")), 0)
        self.assertEqual(memory.records, [])

    async def test_benign_image_uses_multimodal_moderation_before_responses(self) -> None:
        instance, http, _ = make_bot()
        image = "https://cdn.discordapp.com/image.png"

        await instance.ask(make_message(image_urls=[image]), "what is this")

        moderation = http.calls_to("/moderations")[0]["json"]
        self.assertEqual(
            moderation["input"],
            [
                {"type": "text", "text": "what is this"},
                {"type": "text", "text": "Attachment filename: image-1.png"},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        )
        response_payload = http.calls_to("/responses")[0]["json"]
        self.assertIn(
            {"type": "input_image", "image_url": image},
            response_payload["input"][-1]["content"],
        )

    async def test_image_filename_is_moderated_before_context_reuses_it(self) -> None:
        instance, http, memory = make_bot()
        filename = "user-controlled-name.png"

        await instance.ask(
            make_message(
                image_urls=["https://cdn.discordapp.com/image.png"], filenames=[filename]
            ),
            "look",
        )

        moderation_input = http.calls_to("/moderations")[0]["json"]["input"]
        self.assertIn(
            {"type": "text", "text": f"Attachment filename: {filename}"},
            moderation_input,
        )
        self.assertEqual(memory.records[0]["attachments"][0]["filename"], filename)
        response_content = http.calls_to("/responses")[0]["json"]["input"][-1]["content"]
        self.assertIn(filename, response_content[0]["text"])

    async def test_flagged_image_is_not_sent_to_responses_or_memory(self) -> None:
        instance, http, memory = make_bot()
        http.moderation = {"results": [{"flagged": True}]}

        with self.assertRaises(ModerationRejected):
            await instance.ask(
                make_message(image_urls=["https://cdn.discordapp.com/blocked.png"]),
                "look",
            )

        self.assertEqual(len(http.calls_to("/responses")), 0)
        self.assertEqual(memory.records, [])

    async def test_mixed_text_and_image_are_moderated_together(self) -> None:
        instance, http, _ = make_bot()
        image = "https://cdn.discordapp.com/mixed.png"

        await instance.ask(make_message(image_urls=[image]), "describe this image")

        payload = http.calls_to("/moderations")[0]["json"]
        self.assertEqual(payload["model"], "omni-moderation-latest")
        self.assertEqual(len(payload["input"]), 3)

    async def test_timeout_http_errors_and_malformed_responses_fail_closed(self) -> None:
        cases: list[object] = [
            httpx.ReadTimeout("timeout"),
            httpx.HTTPStatusError(
                "client error",
                request=httpx.Request("POST", "https://example.test/moderations"),
                response=httpx.Response(400),
            ),
            httpx.HTTPStatusError(
                "provider error",
                request=httpx.Request("POST", "https://example.test/moderations"),
                response=httpx.Response(500),
            ),
            ValueError("invalid JSON"),
            {"results": []},
            {"results": [{}]},
            "not a moderation object",
        ]
        for result in cases:
            with self.subTest(result=type(result).__name__):
                instance, http, memory = make_bot()
                http.moderation = result
                with self.assertRaises(ModerationUnavailable):
                    await instance.ask(make_message(), "safe input")
                self.assertEqual(len(http.calls_to("/responses")), 0)
                self.assertEqual(memory.records, [])

    async def test_repeated_rejections_temporarily_block_without_another_api_call(self) -> None:
        instance, http, _ = make_bot()
        http.moderation = {"results": [{"flagged": True}]}
        with patch.multiple(
            bot_module,
            MODERATION_ABUSE_MAX_FLAGGED=2,
            MODERATION_ABUSE_WINDOW=60.0,
            MODERATION_ABUSE_BLOCK_SECONDS=120.0,
        ):
            with self.assertRaises(ModerationRejected):
                await instance.moderate_user_input(7, "one", [])
            with self.assertRaises(ModerationRejected):
                await instance.moderate_user_input(7, "two", [])
            with self.assertRaises(ModerationBlocked) as blocked:
                await instance.moderate_user_input(7, "three", [])

        self.assertGreater(blocked.exception.retry_after, 0)
        self.assertEqual(len(http.calls_to("/moderations")), 2)

    async def test_flagged_turn_never_reaches_memory_summarization(self) -> None:
        instance, http, memory = make_bot()
        http.moderation = {"results": [{"flagged": True}]}

        with self.assertRaises(ModerationRejected):
            await instance.ask(make_message(text="rejected"), "rejected")
        await instance.refresh_memory("123", "7")

        self.assertEqual(memory.records, [])
        self.assertEqual(len(http.calls_to("/responses")), 0)

    async def test_credible_self_harm_cannot_bypass_moderation(self) -> None:
        instance, http, memory = make_bot()
        http.moderation = {"results": [{"flagged": True}]}

        with self.assertRaises(ModerationRejected):
            await instance.ask(
                make_message(text="i want to die tonight and im not joking"),
                "i want to die tonight and im not joking",
            )

        self.assertEqual(memory.records, [])
        self.assertEqual(len(http.calls_to("/responses")), 0)

    async def test_accepted_credible_self_harm_still_uses_the_local_emergency_reply(self) -> None:
        instance, http, memory = make_bot()
        prompt = "i want to die tonight and im not joking"

        answer = await instance.ask(make_message(text=prompt), prompt)

        self.assertIn("immediate danger", answer or "")
        self.assertEqual(len(http.calls_to("/moderations")), 1)
        self.assertEqual(len(http.calls_to("/responses")), 0)
        self.assertEqual(len(memory.records), 2)


class ExistingAdmissionControlsTests(unittest.TestCase):
    def test_ordinary_rate_limiting_is_unchanged(self) -> None:
        instance, _, _ = make_bot()
        with patch.multiple(bot_module, RATE_LIMIT_REQUESTS=2, RATE_LIMIT_WINDOW=45.0):
            self.assertEqual(instance.admit_request(7), (True, 0))
            self.assertEqual(instance.admit_request(7), (True, 0))
            admitted, retry_after = instance.admit_request(7)
        self.assertFalse(admitted)
        self.assertGreaterEqual(retry_after, 1)

    def test_configured_guild_bypasses_rate_limiting(self) -> None:
        instance, _, _ = make_bot()
        with patch.multiple(bot_module, RATE_LIMIT_REQUESTS=1, RATE_LIMIT_WINDOW=45.0):
            self.assertEqual(
                instance.admit_request(7, guild_id=1535083112709496903), (True, 0)
            )
            self.assertEqual(
                instance.admit_request(7, guild_id=1535083112709496903), (True, 0)
            )

    def test_safety_identifier_is_stable_and_user_scoped(self) -> None:
        self.assertEqual(safety_identifier(7), safety_identifier(7))
        self.assertNotEqual(safety_identifier(7), safety_identifier(8))


if __name__ == "__main__":
    unittest.main()
