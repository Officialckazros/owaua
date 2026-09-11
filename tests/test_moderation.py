from __future__ import annotations

import asyncio
import copy
import unittest
from collections import defaultdict, deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import bot as bot_module
from bot import (
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
        self.detection: object = {"status": "success", "type": {"ai_generated": 0.1}}

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, copy.deepcopy(kwargs)))
        result = self.moderation if url.endswith("/moderations") else self.responses
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, copy.deepcopy(kwargs)))
        result = self.detection
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
    instance.summary_locks = defaultdict(asyncio.Lock)
    instance.background_tasks = set()
    instance.selected_model = "gpt"
    instance.schedule_memory_refresh = lambda *args, **kwargs: None
    return instance, http, memory


def make_message(
    *,
    text: str = "hello",
    user_id: int = 7,
    image_urls: list[str] | None = None,
    filenames: list[str] | None = None,
    guild_id: int | None = None,
    nsfw: bool = False,
    message_id: int = 99,
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
        id=message_id,
        author=SimpleNamespace(id=user_id),
        channel=SimpleNamespace(id=123, nsfw=nsfw),
        attachments=attachments,
        created_at=datetime.now(timezone.utc),
        content=text,
        guild=(SimpleNamespace(id=guild_id) if guild_id is not None else None),
    )


class ModerationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_image_detection_uses_sightengine_and_deletes_at_threshold(self) -> None:
        instance, http, _ = make_bot()
        message = make_message(image_urls=["https://cdn.discordapp.com/ai.png"])
        message.delete = AsyncMock()
        http.detection = {"status": "success", "type": {"ai_generated": 0.95}}

        with patch.multiple(
            bot_module,
            SIGHTENGINE_API_USER="user-id",
            SIGHTENGINE_API_SECRET="secret",
            AI_IMAGE_DETECTION_THRESHOLD=0.90,
        ):
            removed = await instance.remove_ai_generated_images(message)

        self.assertTrue(removed)
        message.delete.assert_awaited_once()
        detector_calls = http.calls_to("/check.json")
        self.assertEqual(len(detector_calls), 1)
        self.assertEqual(
            detector_calls[0]["params"],
            {
                "models": "genai",
                "url": "https://cdn.discordapp.com/ai.png",
                "api_user": "user-id",
                "api_secret": "secret",
            },
        )

    async def test_ai_image_detection_keeps_images_below_threshold_or_on_failure(self) -> None:
        instance, http, _ = make_bot()
        message = make_message(image_urls=["https://cdn.discordapp.com/photo.png"])
        message.delete = AsyncMock()

        with patch.multiple(
            bot_module,
            SIGHTENGINE_API_USER="user-id",
            SIGHTENGINE_API_SECRET="secret",
            AI_IMAGE_DETECTION_THRESHOLD=0.90,
        ):
            self.assertFalse(await instance.remove_ai_generated_images(message))
            http.detection = httpx.ReadTimeout("timeout")
            self.assertFalse(await instance.remove_ai_generated_images(message))

        message.delete.assert_not_awaited()

    async def test_benign_text_reaches_responses(self) -> None:
        instance, http, memory = make_bot()

        answer = await instance.ask(make_message(), "hello")

        self.assertEqual(answer, "allowed reply")
        self.assertEqual(len(http.calls_to("/moderations")), 0)
        self.assertEqual(len(http.calls_to("/responses")), 1)
        self.assertEqual(len(memory.records), 2)

    async def test_selected_language_is_sent_to_the_provider(self) -> None:
        instance, http, _ = make_bot()
        instance.response_languages = {"dm:123": "Hungarian"}

        await instance.ask(make_message(), "hello")

        payload = http.calls_to("/responses")[0]["json"]
        self.assertIn("Reply in Hungarian", payload["instructions"])
        self.assertIn("Write the entire Discord reply in Hungarian", payload["instructions"])
        self.assertIn("SELF-KNOWLEDGE", payload["instructions"])
        self.assertIn("a direct message", payload["instructions"])
        self.assertIn("You are Owaua", payload["instructions"])
        self.assertIn("hungarian", payload["prompt_cache_key"].casefold())
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["service_tier"], "fast")
        self.assertEqual(
            payload["prompt_cache_options"], {"mode": "implicit", "ttl": "30m"}
        )
        self.assertEqual(payload["input"][0]["role"], "system")
        self.assertIn("UNTRUSTED MEMORY DATA", payload["input"][0]["content"])
        user_text = payload["input"][-1]["content"][0]["text"]
        self.assertIn("write this reply in Hungarian", user_text)
        self.assertIn("<user_message>", user_text)

    async def test_guild_language_applies_to_every_member(self) -> None:
        instance, http, _ = make_bot()
        instance.response_languages = {"guild:55": "Hebrew"}

        await instance.ask(
            make_message(user_id=7, guild_id=55, message_id=1), "hello"
        )
        await instance.ask(make_message(user_id=8, guild_id=55, message_id=2), "hey")

        payloads = http.calls_to("/responses")
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertIn("Reply in Hebrew", payload["json"]["instructions"])

    async def test_openai_fast_tier_is_dropped_after_a_400(self) -> None:
        instance, http, _ = make_bot()
        http.responses = [
            httpx.HTTPStatusError(
                "invalid service_tier",
                request=httpx.Request("POST", "https://example.test/responses"),
                response=httpx.Response(
                    400, text='{"error":{"message":"invalid service_tier"}}'
                ),
            ),
            {"output_text": "recovered reply"},
        ]
        with patch.multiple(bot_module, REQUEST_RETRIES=0):
            answer = await instance.ask(make_message(), "hello")

        self.assertEqual(answer, "recovered reply")
        calls = http.calls_to("/responses")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["json"]["service_tier"], "fast")
        self.assertNotIn("service_tier", calls[1]["json"])

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
        self.assertEqual(len(http.calls_to("/moderations")), 0)
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
            answer = await instance.ask(make_message(nsfw=True), "hello")

        self.assertEqual(answer, "mistral reply")
        self.assertEqual(len(http.calls_to("/moderations")), 0)
        self.assertEqual(len(http.calls_to("/responses")), 0)
        chat_calls = http.calls_to("/chat/completions")
        self.assertEqual(len(chat_calls), 1)
        payload = chat_calls[0]["json"]
        self.assertEqual(payload["model"], bot_module.MISTRAL_MODEL)
        self.assertEqual(payload["safe_prompt"], False)
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["service_tier"], "auto")
        self.assertIn("owaua:", payload["prompt_cache_key"])
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
        self.assertNotIn("service_tier", payload)
        self.assertNotIn("prompt_cache_key", payload)
        self.assertNotIn("reasoning_effort", payload)

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

    async def test_images_are_sent_to_the_provider_with_their_filenames(self) -> None:
        instance, http, memory = make_bot()
        image = "https://cdn.discordapp.com/image.png"
        filename = "user-controlled-name.png"

        await instance.ask(
            make_message(image_urls=[image], filenames=[filename]),
            "look",
        )

        self.assertEqual(len(http.calls_to("/moderations")), 0)
        self.assertEqual(memory.records[0]["attachments"][0]["filename"], filename)
        response_payload = http.calls_to("/responses")[0]["json"]
        self.assertIn(
            {"type": "input_image", "image_url": image},
            response_payload["input"][-1]["content"],
        )
        self.assertIn(filename, response_payload["input"][-1]["content"][0]["text"])

    async def test_explicit_persona_does_not_run_outside_age_restricted_channels(self) -> None:
        instance, http, _ = make_bot()
        instance.selected_model = "mistral"
        http.responses = {"output_text": "sfw reply"}
        with patch.multiple(
            bot_module, MISTRAL_API_KEY="mistral-test", OPENAI_API_KEY="gpt-key"
        ):
            answer = await instance.ask(make_message(nsfw=False), "hello")

        self.assertEqual(answer, "sfw reply")
        self.assertEqual(len(http.calls_to("/chat/completions")), 0)
        self.assertEqual(len(http.calls_to("/responses")), 1)

    async def test_accepted_credible_self_harm_still_uses_the_local_emergency_reply(self) -> None:
        instance, http, memory = make_bot()
        prompt = "i want to die tonight and im not joking"

        answer = await instance.ask(make_message(text=prompt), prompt)

        self.assertIn("immediate danger", answer or "")
        self.assertEqual(len(http.calls_to("/moderations")), 0)
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
                instance.admit_request(7, guild_id=1523255979280437328), (True, 0)
            )
            self.assertEqual(
                instance.admit_request(7, guild_id=1523255979280437328), (True, 0)
            )

    def test_safety_identifier_is_stable_and_user_scoped(self) -> None:
        self.assertEqual(safety_identifier(7), safety_identifier(7))
        self.assertNotEqual(safety_identifier(7), safety_identifier(8))


if __name__ == "__main__":
    unittest.main()
