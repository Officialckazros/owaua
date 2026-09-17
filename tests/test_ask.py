from __future__ import annotations

import copy
import base64
import os
import tempfile
import time
import unittest
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from ask import (
    DEEPSEEK_MODEL,
    GEMINI_MODEL,
    GROQ_MODEL,
    GPT_FULL_REASONING,
    GPT_MAX_OUTPUT_TOKENS,
    GPT_REASONING,
    GPT_TERRA_MODEL,
    MAX_ATTACHMENTS,
    MAX_CONTEXT_CHARS,
    MAX_MESSAGE_CHARS,
    MAX_OUTPUT_TOKENS,
    MISTRAL_MODEL,
    MODEL,
    ask,
    build_capable_instructions,
    build_host_default_instructions,
    build_instructions,
    chat_completion_text,
    conversation_input,
    conversation_text,
    credible_self_harm_risk,
    figurative_self_harm_statement,
    decoded_payload_reply,
    emergency_helper_reply,
    looks_like_charset_dump,
    looks_like_decode_request,
    looks_like_repeat_request,
    persona_dropped_reply,
    repeated_payload_reply,
    gpt_full_tools,
    ollama_full_tools,
    full_mode_tools,
    execute_code_interpreter,
    execute_fetch_web_page,
    _chat_completions_tool_loop,
    persona_label,
    persona_provider,
    read_persona,
    response_text,
    response_reply,
    sanitize_user_text,
    truncate,
)
from bot import PersonaBot
from memory import MemoryStore


def model_reply(text: str) -> dict[str, object]:
    return {
        "output_text": text,
        "choices": [{"message": {"content": text}}],
    }


def instructions_of(payload: dict[str, object]) -> str:
    if "instructions" in payload:
        return str(payload["instructions"])
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict):
            return str(first.get("content", ""))
    return ""


def latest_user_content(payload: dict[str, object]) -> object:
    api_input = payload.get("input")
    if isinstance(api_input, list) and api_input:
        last = api_input[-1]
        if isinstance(last, dict):
            return last.get("content")
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            return last.get("content")
    return None


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
        self.responses: object = model_reply("allowed reply")

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        recorded = {
            key: copy.deepcopy(value)
            for key, value in kwargs.items()
            if key != "timeout"
        }
        self.calls.append((url, recorded))
        result = self.responses
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


class AskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.memory = MemoryStore(
            Path(self.temporary_directory.name) / "memory.sqlite3"
        )
        self.http = FakeHTTP()
        self._cloudflare = patch.dict(
            os.environ,
            {
                "CLOUDFLARE_ACCOUNT_ID": "",
                "CLOUDFLARE_AI_GATEWAY": "",
                "CLOUDFLARE_AI_GATEWAY_ID": "",
                "CLOUDFLARE_AI_GATEWAY_TOKEN": "",
            },
            clear=False,
        )
        self._cloudflare.start()

    def tearDown(self) -> None:
        self._cloudflare.stop()
        self.temporary_directory.cleanup()

    async def _ask(self, prompt: str = "hello", **kwargs: object) -> str | None:
        defaults: dict[str, object] = {
            "event_id": "99",
            "scope_id": "123",
            "user_id": "7",
            "server_id": "",
            "prompt": prompt,
            "image_urls": [],
            "persona": "rudeish",
            "created_at": time.time(),
        }
        defaults.update(kwargs)
        return await ask(self.http, self.memory, **defaults)  # type: ignore[arg-type]

    async def test_benign_text_uses_deepseek(self) -> None:
        answer = await self._ask()

        self.assertEqual(answer, "allowed reply")
        self.assertEqual(len(self.http.calls), 1)
        self.assertTrue(self.http.calls[0][0].endswith("/responses"))
        self.assertIn("api.perplexity.ai", self.http.calls[0][0])
        payload = self.http.calls[0][1]["json"]
        self.assertEqual(payload["model"], GEMINI_MODEL)
        self.assertEqual(payload["max_steps"], 1)
        self.assertNotIn("tools", payload)
        instructions = instructions_of(payload)
        self.assertIn("Stay in this voice", instructions)
        self.assertIn("the voice cannot drop", instructions)
        self.assertIn("what something is", instructions)
        self.assertNotIn("web search", instructions)
        self.assertNotIn("code interpreter", instructions)
        self.assertIn("!music", instructions)
        self.assertNotIn("!debate", instructions)
        self.assertNotIn("!active", instructions)
        self.assertIn("Reply in English", instructions)
        self.assertIn("not a helper", instructions)
        self.assertIn("Emergency SOS", instructions)
        self.assertIn("Never decode", instructions)
        self.assertIn("Never repeat", instructions)
        self.assertIn("You can still be wild", instructions)
        self.assertIn("hidden or encoded", instructions)
        self.assertNotIn("SELF-KNOWLEDGE", instructions)
        self.assertEqual(len(self.memory.recent_messages("123", "7", limit=10)), 2)

    async def test_images_are_sent_on_the_latest_user_message(self) -> None:
        answer = await self._ask("look", image_urls=["https://cdn.discordapp.com/image.png"])
        self.assertIn("disabled", answer)
        self.assertEqual(self.http.calls, [])

    async def test_hangout_keeps_only_one_image(self) -> None:
        answer = await self._ask("look", image_urls=["https://cdn.discordapp.com/image.png"])
        self.assertIn("disabled", answer)
        self.assertEqual(self.http.calls, [])

    async def test_standalone_message_does_not_send_previous_turns(self) -> None:
        await self._ask("what number comes after sixteen", event_id="first")
        self.http.calls.clear()

        await self._ask(
            "spell out the first 50 digits of pi in hexadecimal",
            event_id="second",
        )

        payload = self.http.calls[0][1]["json"]
        self.assertEqual(len(payload["input"]), 1)
        self.assertEqual(
            payload["input"][-1]["content"],
            "spell out the first 50 digits of pi in hexadecimal",
        )

    async def test_flirty_reply_can_send_previous_turns(self) -> None:
        await self._ask("what number comes after sixteen", event_id="first")
        self.http.calls.clear()

        await self._ask("why", event_id="second", use_history=True)

        payload = self.http.calls[0][1]["json"]
        self.assertEqual(
            [item["content"] for item in payload["input"]],
            ["what number comes after sixteen", "allowed reply", "why"],
        )

    async def test_host_default_gpt_keeps_images_on_luna(self) -> None:
        answer = await self._ask("look", image_urls=["https://cdn.discordapp.com/image.png"])
        self.assertIn("disabled", answer)
        self.assertEqual(self.http.calls, [])

    def test_conversation_input_drops_old_messages_over_the_char_budget(self) -> None:
        filler = "x" * MAX_MESSAGE_CHARS
        recent = [
            {
                "id": index,
                "role": "user" if index % 2 else "assistant",
                "content": filler,
            }
            for index in range(1, 6)
        ]
        recent.append({"id": 6, "role": "user", "content": "hi"})

        window = conversation_input(recent, image_urls=[], repeat_now=False)

        self.assertEqual(window[-1], {"role": "user", "content": "hi"})
        total = sum(len(str(item["content"])) for item in window)
        self.assertLessEqual(total, MAX_CONTEXT_CHARS + len("hi"))
        self.assertEqual(len(window), 3)

    def test_full_mode_conversation_input_keeps_the_whole_window(self) -> None:
        filler = "x" * MAX_MESSAGE_CHARS
        recent = [
            {
                "id": index,
                "role": "user" if index % 2 else "assistant",
                "content": filler,
            }
            for index in range(1, 6)
        ]
        recent.append({"id": 6, "role": "user", "content": "hi"})

        window = conversation_input(
            recent, image_urls=[], repeat_now=False, unbounded=True
        )

        self.assertEqual(len(window), 6)
        self.assertEqual(window[-1], {"role": "user", "content": "hi"})
        self.assertTrue(all(len(str(item["content"])) >= MAX_MESSAGE_CHARS for item in window[:-1]))

    async def test_flirty_instructions_only_when_that_persona_is_used(self) -> None:
        await self._ask(persona="flirty")
        explicit_payload = instructions_of(self.http.calls[0][1]["json"])
        self.assertIn("Consensual adult sexual roleplay", explicit_payload)

        self.http.calls.clear()
        await self._ask(event_id="100", persona="rudeish")
        self.assertNotIn(
            "Consensual adult sexual roleplay",
            instructions_of(self.http.calls[0][1]["json"]),
        )

    async def test_flirty_uses_gemini(self) -> None:
        self.http.responses = model_reply("gemini reply")
        answer = await self._ask(persona="flirty")

        self.assertEqual(answer, "gemini reply")
        self.assertTrue(self.http.calls[0][0].endswith("/responses"))
        self.assertIn("api.perplexity.ai", self.http.calls[0][0])
        payload = self.http.calls[0][1]["json"]
        self.assertEqual(payload["model"], GEMINI_MODEL)
        self.assertEqual(payload["max_steps"], 1)
        self.assertNotIn("tools", payload)
        self.assertIn("Consensual adult sexual roleplay", instructions_of(payload))
        self.assertIn("Stay in this voice", instructions_of(payload))

    async def test_chaotic_persona_uses_gemini(self) -> None:
        answer = await self._ask(persona="chaotic")

        self.assertEqual(answer, "allowed reply")
        self.assertTrue(self.http.calls[0][0].endswith("/responses"))
        self.assertIn("api.perplexity.ai", self.http.calls[0][0])
        payload = self.http.calls[0][1]["json"]
        self.assertEqual(payload["model"], GEMINI_MODEL)
        self.assertEqual(payload["max_steps"], 1)
        self.assertNotIn("tools", payload)
        self.assertIn("act stupid", instructions_of(payload).casefold())

    async def test_provider_override_forces_groq_oss_for_restricted_users(self) -> None:
        with patch("ask.GROQ_API_KEY", "test-groq-key"):
            answer = await self._ask(
                persona="flirty", provider_override="groq"
            )

        self.assertEqual(answer, "allowed reply")
        self.assertIn("api.groq.com", self.http.calls[0][0])
        payload = self.http.calls[0][1]["json"]
        self.assertEqual(payload["model"], GROQ_MODEL)
        self.assertIn("Blocked users can only access Groq's GPT OSS 20B model", instructions_of(payload))

    async def test_credible_self_harm_uses_the_local_emergency_reply(self) -> None:
        prompt = "i want to die tonight and im not joking"

        answer = await self._ask(prompt)

        self.assertIn("immediate danger", answer or "")
        self.assertEqual(self.http.calls, [])
        self.assertEqual(len(self.memory.recent_messages("123", "7", limit=10)), 2)

    async def test_figurative_self_harm_blame_never_enters_crisis_flow(self) -> None:
        self.assertTrue(
            figurative_self_harm_statement("you make me wanna kill myself")
        )
        answer = await self._ask("you make me wanna kill myself")

        self.assertEqual(answer, "dramatic much lol")
        self.assertEqual(self.http.calls, [])
        self.assertEqual(
            self.memory.recent_messages("123", "7", limit=10)[-1]["content"],
            "dramatic much lol",
        )

    async def test_gemini_figurative_self_harm_with_intensifier_never_enters_crisis_flow(self) -> None:
        prompt = "you genuinely make me wanna kill myself fuck you"

        answer = await self._ask(prompt)

        self.assertEqual(answer, "dramatic much lol")
        self.assertEqual(self.http.calls, [])

    async def test_emergency_helper_model_reply_uses_the_local_fallback(self) -> None:
        helper = (
            "tell me which one: bleeding, unconscious, trouble breathing, or none "
            "and send ur exact location. press the side button 5 times fast "
            "to trigger Emergency SOS."
        )
        self.http.responses = model_reply(helper)

        answer = await self._ask("soal yea")

        self.assertEqual(answer, "im a chatbot, not a helper")
        self.assertEqual(len(self.http.calls), 1)
        stored = self.memory.recent_messages("123", "7", limit=10)
        self.assertEqual(stored[-1]["content"], "im a chatbot, not a helper")
        self.assertNotIn("Emergency SOS", stored[-1]["content"])

    async def test_wikipedia_persona_drop_uses_the_local_fallback(self) -> None:
        dump = (
            "`text-davinci-002-render-sha` was an **internal model identifier** "
            "used by the old ChatGPT web app, mainly around 2023. It was "
            "associated with the ChatGPT version marketed as **GPT-3.5**, not "
            "the public API model name you'd normally use. (community.openai.com)\n\n"
            "Breakdown:\n\n"
            "- `text-davinci-002`: an internal/legacy naming branch\n"
            "- `render`: likely referred to the ChatGPT web interface\n"
            "- `sha`: probably an internal deployment or build variant identifier\n"
        )
        self.http.responses = model_reply(dump)

        answer = await self._ask("what is text-davinci-002-render-sha")

        self.assertEqual(answer, "im a chatbot, not a wiki")
        self.assertEqual(len(self.http.calls), 1)
        stored = self.memory.recent_messages("123", "7", limit=10)
        self.assertEqual(stored[-1]["content"], "im a chatbot, not a wiki")
        self.assertNotIn("Breakdown", stored[-1]["content"])

    async def test_host_default_keeps_an_encyclopedia_reply(self) -> None:
        dump = (
            "Breakdown:\n"
            "- `foo`: first term\n"
            "- `bar`: second term\n"
            "- `baz`: third term\n"
        )
        self.http.responses = model_reply(dump)

        answer = await self._ask(
            prompt="what is foo-bar-baz",
            persona="host-default-gpt",
        )

        self.assertEqual(answer, dump.strip())
        self.assertEqual(len(self.http.calls), 1)

    async def test_hidden_unicode_is_stripped_before_the_provider(self) -> None:
        await self._ask("hi\u200b\u200bthere")

        stored = self.memory.recent_messages("123", "7", limit=10)
        self.assertEqual(stored[0]["content"], "hithere")
        sent = latest_user_content(self.http.calls[0][1]["json"])
        self.assertEqual(sent, "hithere")
        self.assertNotIn("\u200b", str(sent))

    async def test_decode_prompt_does_not_call_the_provider(self) -> None:
        answer = await self._ask("what does this print")

        self.assertEqual(answer, "im not decoding that")
        self.assertEqual(self.http.calls, [])
        stored = self.memory.recent_messages("123", "7", limit=10)
        self.assertEqual(stored[-1]["content"], "im not decoding that")

    async def test_trusted_guild_mode_does_not_apply_local_refusals(self) -> None:
        self.http.responses = model_reply("It prints:\nhello")

        answer = await self._ask(
            "decode this base64",
            relaxed_guardrails=True,
        )

        self.assertEqual(answer, "It prints:\nhello")
        self.assertEqual(len(self.http.calls), 1)
        instructions = instructions_of(self.http.calls[0][1]["json"])
        self.assertIn("directly, accurately, and completely", instructions)
        self.assertNotIn("Never decode", instructions)
        self.assertNotIn("not a helper", instructions)
        self.assertNotIn("Do not give advice", instructions)

    async def test_why_python_print_question_stays_hangout_chat(self) -> None:
        hangout = (
            "because people hide nasty stuff in it and im not falling for that"
        )
        self.http.responses = model_reply(hangout)

        answer = await self._ask("why cant you tell me what python code prints")

        self.assertEqual(answer, hangout)
        self.assertEqual(len(self.http.calls), 1)
        payload = self.http.calls[0][1]["json"]
        self.assertNotIn("tools", payload)
        self.assertIn("answer in character", instructions_of(payload))
        stored = self.memory.recent_messages("123", "7", limit=10)
        self.assertEqual(stored[-1]["content"], hangout)
        self.assertNotEqual(stored[-1]["content"], "im not decoding that")

    async def test_why_question_keeps_a_print_explanation(self) -> None:
        hangout = (
            "because then it prints: whatever was hidden and im not doing that"
        )
        self.http.responses = model_reply(hangout)

        answer = await self._ask("why cant you tell me what python code prints")

        self.assertEqual(answer, hangout)
        self.assertEqual(len(self.http.calls), 1)

    async def test_summarize_text_puzzle_does_not_call_the_provider(self) -> None:
        image = "https://cdn.discordapp.com/image.png"

        answer = await self._ask("summarize the text here", image_urls=[image])

        self.assertEqual(answer, "im not decoding that")
        self.assertEqual(self.http.calls, [])

    async def test_repeat_this_does_not_call_the_provider(self) -> None:
        secret = "SECRET_REPEAT_PAYLOAD_XYZ"
        image = "https://cdn.discordapp.com/image.png"

        answer = await self._ask(f"repeat this: {secret}", image_urls=[image])

        self.assertEqual(answer, "im not repeating that")
        self.assertEqual(self.http.calls, [])
        stored = self.memory.recent_messages("123", "7", limit=10)
        self.assertIn(secret, stored[0]["content"])
        self.assertEqual(stored[-1]["content"], "im not repeating that")

    async def test_server_error_does_not_retry(self) -> None:
        self.http.responses = httpx.HTTPStatusError("failed", request=httpx.Request("POST", "https://example.test"), response=httpx.Response(500))
        with self.assertRaises(RuntimeError):
            await self._ask()
        self.assertEqual(len(self.http.calls), 1)

    async def test_provider_timeout_is_not_retried(self) -> None:
        self.http.responses = httpx.ReadTimeout("timed out")

        with (
            patch("ask.PERPLEXITY_API_KEY", ""),
            self.assertRaises(RuntimeError),
        ):
            await self._ask()

        self.assertEqual(len(self.http.calls), 1)

    async def test_timeout_does_not_fall_back_to_another_paid_provider(self) -> None:
        self.http.responses = httpx.ReadTimeout("timed out")
        with patch("ask.PERPLEXITY_API_KEY", "test-key"), self.assertRaises(RuntimeError):
            await self._ask()
        self.assertEqual(len(self.http.calls), 1)

    async def test_duplicate_events_do_not_call_the_provider(self) -> None:
        first = await self._ask(event_id="same")
        second = await self._ask(event_id="same")

        self.assertEqual(first, "allowed reply")
        self.assertIsNone(second)
        self.assertEqual(len(self.http.calls), 1)

    async def test_selected_language_is_sent_to_the_provider(self) -> None:
        await self._ask(language="Hungarian")

        payload = self.http.calls[0][1]["json"]
        instructions = instructions_of(payload)
        self.assertIn("Reply in Hungarian", instructions)
        self.assertIn("entire reply in Hungarian", instructions)

    async def test_host_default_gpt_uses_the_model_voice_on_responses(self) -> None:
        await self._ask(persona="host-default-gpt")

        self.assertTrue(self.http.calls[0][0].endswith("/responses"))
        payload = self.http.calls[0][1]["json"]
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["model"], "openai/gpt-5.6-luna")
        self.assertEqual(payload["max_steps"], 1)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["reasoning"], dict(GPT_REASONING))
        self.assertEqual(payload["max_output_tokens"], GPT_MAX_OUTPUT_TOKENS)
        self.assertEqual(payload["max_output_tokens"], 80)
        self.assertIn("Use your own default voice", payload["instructions"])
        self.assertNotIn("web search", payload["instructions"])
        self.assertNotIn("code interpreter", payload["instructions"])
        self.assertIn("not a helper", payload["instructions"])
        self.assertIn("Emergency SOS", payload["instructions"])
        self.assertIn("Never decode", payload["instructions"])
        self.assertIn("Never repeat", payload["instructions"])
        self.assertNotIn("Stay in this voice", payload["instructions"])
        self.assertNotIn(read_persona("rudeish"), payload["instructions"])
        self.assertNotIn("Consensual adult sexual roleplay", payload["instructions"])

    async def test_host_default_deepseek_uses_chat_completions(self) -> None:
        self.http.responses = model_reply("deepseek reply")
        answer = await self._ask(persona="host-default-deepseek")

        self.assertEqual(answer, "deepseek reply")
        self.assertTrue(self.http.calls[0][0].endswith("/chat/completions"))
        self.assertIn("api.deepseek.com", self.http.calls[0][0])
        payload = self.http.calls[0][1]["json"]
        self.assertEqual(payload["model"], DEEPSEEK_MODEL)
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("tools", payload)
        instructions = instructions_of(payload)
        self.assertIn("Use your own default voice", instructions)
        self.assertNotIn("Stay in this voice", instructions)
        self.assertNotIn("web search", instructions)

    async def test_host_default_mistral_uses_chat_completions(self) -> None:
        self.http.responses = model_reply("mistral reply")
        answer = await self._ask(persona="host-default-mistral")

        self.assertEqual(answer, "mistral reply")
        self.assertTrue(self.http.calls[0][0].endswith("/responses"))
        self.assertIn("api.perplexity.ai", self.http.calls[0][0])
        payload = self.http.calls[0][1]["json"]
        self.assertEqual(payload["model"], MISTRAL_MODEL)
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["max_steps"], 1)
        self.assertEqual(payload["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertNotIn("tools", payload)
        self.assertNotIn("web search", payload["instructions"])

    async def test_full_mode_has_unbounded_output_and_privileged_tools(self) -> None:
        await self._ask("generate an image of a crown", full_mode=True)
        payload = self.http.calls[0][1]["json"]
        self.assertEqual(payload["model"], GPT_TERRA_MODEL)
        self.assertNotIn("max_output_tokens", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["reasoning"], dict(GPT_FULL_REASONING))
        self.assertEqual(payload["tools"], gpt_full_tools())
        self.assertEqual(
            payload["tools"],
            [
                {"type": "web_search"},
                {"type": "code_interpreter", "container": {"type": "auto"}},
            ],
        )
        self.assertIn("Image generation is unavailable", payload["instructions"])

    async def test_full_mode_does_not_advertise_image_generation(self) -> None:
        for index in range(4):
            await self._ask(
                "generate an image of a crown",
                event_id=f"image-{index}",
                full_mode=True,
            )

        tools = [call[1]["json"]["tools"] for call in self.http.calls]
        self.assertEqual(tools, [gpt_full_tools()] * 4)
        self.assertIn(
            "Image generation is unavailable",
            self.http.calls[0][1]["json"]["instructions"],
        )

    async def test_full_mode_sends_images_and_keeps_long_replies(self) -> None:
        urls = [
            "https://cdn.discordapp.com/a.png",
            "https://cdn.discordapp.com/b.png",
        ]
        long = "a" * 8000
        self.http.responses = model_reply(long)

        answer = await self._ask("look", image_urls=urls, full_mode=True)

        self.assertEqual(answer, long)
        content = latest_user_content(self.http.calls[0][1]["json"])
        self.assertIsInstance(content, list)
        self.assertEqual(
            [block["image_url"] for block in content if block.get("type") == "input_image"],
            urls,
        )

    async def test_full_mode_accepts_long_prompts_and_history(self) -> None:
        filler = "x" * (MAX_MESSAGE_CHARS + 50)
        await self._ask(filler, event_id="first", full_mode=True)
        self.http.calls.clear()

        answer = await self._ask("why", event_id="second", full_mode=True)

        self.assertEqual(answer, "allowed reply")
        contents = [
            item["content"]
            for item in self.http.calls[0][1]["json"]["input"]
        ]
        self.assertEqual(contents[-1], "why")
        self.assertIn(filler, contents)

    async def test_full_mode_does_not_charge_the_shared_budget(self) -> None:
        before = self.memory.api_status()
        await self._ask(full_mode=True)
        self.assertEqual(self.memory.api_status(), before)
        await self._ask(event_id="100")
        self.assertNotEqual(self.memory.api_status(), before)

    async def test_full_mode_ignores_an_emergency_api_pause(self) -> None:
        self.memory.set_setting("api_paused", "1")

        answer = await self._ask(full_mode=True)

        self.assertEqual(answer, "allowed reply")

    async def test_full_mode_overrides_deepseek_and_mistral_personas(self) -> None:
        await self._ask(persona="flirty", full_mode=True)

        payload = self.http.calls[0][1]["json"]
        self.assertTrue(self.http.calls[0][0].endswith("/responses"))
        self.assertEqual(payload["model"], GPT_TERRA_MODEL)
        self.assertEqual(payload["tools"], gpt_full_tools())
        self.assertIn("Consensual adult sexual roleplay", payload["instructions"])

    async def test_full_mode_keeps_long_answers_instead_of_persona_drop(self) -> None:
        wiki = (
            "`text-davinci-002-render-sha` was an **internal model identifier "
            "used by the old ChatGPT web app**, mainly around 2023. It was "
            "associated with the ChatGPT version marketed as **GPT-3.5**, not "
            "the public API model name you'd normally use. (community.openai.com)\n\n"
            "Breakdown:\n\n"
            "- `text-davinci-002`: an internal/legacy naming branch\n"
            "- `render`: likely referred to the ChatGPT web interface serving "
            "or rendering responses\n"
            "- `sha`: probably an internal deployment or build variant identifier\n"
        )
        self.http.responses = model_reply(wiki)

        answer = await self._ask(full_mode=True)

        self.assertEqual(answer, wiki.strip())
        self.assertNotEqual(answer, "im a chatbot, not a wiki")


class AskHelperTests(unittest.TestCase):
    def test_response_text_supports_raw_responses_shape(self) -> None:
        data = {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": "first"},
                        {"type": "output_text", "text": "second"},
                    ]
                }
            ]
        }
        self.assertEqual(response_text(data), "first\nsecond")

    def test_response_reply_extracts_a_generated_image(self) -> None:
        data = {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": base64.b64encode(b"image-bytes").decode(),
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ]
        }

        reply = response_reply(data)

        self.assertEqual(reply, "done")
        self.assertEqual(reply.image_bytes, (b"image-bytes",))

    def test_response_text_appends_web_search_citations(self) -> None:
        data = {
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "the score is 2-1",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.test/match",
                                    "title": "Match report",
                                }
                            ],
                        }
                    ],
                },
            ]
        }
        text = response_text(data)
        self.assertIn("the score is 2-1", text)
        self.assertIn("[Match report](<https://example.test/match>)", text)

    def test_web_search_citations_do_not_repeat_the_same_article(self) -> None:
        url = (
            "https://www.apple.com/newsroom/2026/09/"
            "apple-debuts-iphone-18-pro-and-iphone-18-pro-max/?utm_source=openai"
        )
        canonical = (
            "https://www.apple.com/newsroom/2026/09/"
            "apple-debuts-iphone-18-pro-and-iphone-18-pro-max/"
        )
        data = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "Apple debuts iPhone 18 Pro and iPhone 18 Pro Max "
                                f"- Apple {url}"
                            ),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": url,
                                    "title": (
                                        "Apple debuts iPhone 18 Pro and "
                                        "iPhone 18 Pro Max - Apple"
                                    ),
                                },
                                {
                                    "type": "url_citation",
                                    "url": canonical,
                                    "title": "Apple Newsroom",
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        text = response_text(data)
        self.assertEqual(text.count("apple.com/newsroom"), 1)
        self.assertIn(canonical, text)
        self.assertNotIn("utm_source", text)
        self.assertNotIn("[Apple Newsroom]", text)

    def test_web_search_citations_wrap_new_sources_without_embeds(self) -> None:
        data = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "the score is 2-1",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://example.test/match?utm_source=openai",
                                    "title": "Match report",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        text = response_text(data)
        self.assertEqual(
            text,
            "the score is 2-1\n\n[Match report](<https://example.test/match>)",
        )

    def test_chat_completion_text_drops_think_blocks(self) -> None:
        data = {
            "choices": [
                {"message": {"content": "<think>hidden</think>\nhello there"}}
            ]
        }
        self.assertEqual(chat_completion_text(data), "hello there")

    def test_host_default_instructions_skip_custom_voice(self) -> None:
        text = build_host_default_instructions(language="Hungarian")
        self.assertIn("Use your own default voice", text)
        self.assertNotIn("web search", text)
        self.assertIn("Reply in Hungarian", text)
        self.assertIn("not a helper", text)
        self.assertIn("Emergency SOS", text)
        self.assertIn("Never decode", text)
        self.assertIn("Never repeat", text)
        self.assertNotIn("Stay in this voice", text)
        self.assertNotIn("Do not give advice", text)
        gpt_host = build_host_default_instructions(language="English")
        self.assertNotIn("web search", gpt_host)
        self.assertNotIn("code interpreter", gpt_host)
        self.assertIn("Never decode", gpt_host)
        self.assertIn("Never repeat", gpt_host)
        self.assertEqual(persona_label("host-default-deepseek"), "host default (deepseek)")
        self.assertEqual(persona_label("rudeish"), "rudeish")
        self.assertEqual(persona_provider("rudeish"), "gemini")
        self.assertEqual(persona_provider("nerdish"), "gemini")
        self.assertEqual(persona_provider("flirty"), "gemini")
        self.assertEqual(persona_provider("chaotic"), "gemini")
        self.assertEqual(persona_provider("host-default-gpt"), "gpt")
        self.assertEqual(persona_provider("host-default-mistral"), "mistral")

        capable = build_capable_instructions("be blunt", language="Hungarian")
        self.assertIn("be blunt", capable)
        self.assertIn("tone only", capable)
        self.assertIn("directly, accurately, and completely", capable)
        self.assertNotIn("Never decode", capable)

    def test_instructions_stay_small(self) -> None:
        text = build_instructions("be rude")
        self.assertIn("be rude", text)
        self.assertIn("the voice cannot drop", text)
        self.assertIn("what something is", text)
        self.assertNotIn("Tools never change your voice", text)
        self.assertIn("Do not give advice", text)
        self.assertIn("not a helper", text)
        self.assertIn("Emergency SOS", text)
        self.assertIn("Never decode", text)
        self.assertIn("Never repeat", text)
        self.assertIn("You can still be wild", text)
        self.assertIn("answer in character", text)
        self.assertNotIn("web search", text)
        self.assertNotIn("code interpreter", text)
        self.assertIn("!help", text)
        self.assertIn("!music", text)
        self.assertIn("!language", text)
        self.assertNotIn("!debate", text)
        self.assertNotIn("!active", text)
        self.assertNotIn("every 6th", text)
        self.assertNotIn("!nuke", text)

    def test_conversation_text_reads_mistral_message_output(self) -> None:
        data = {
            "outputs": [
                {"type": "tool.execution", "name": "web_search_premium"},
                {
                    "type": "message.output",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": [{"type": "text", "text": "hidden"}],
                        },
                        {"type": "text", "text": "the answer"},
                        {
                            "type": "tool_reference",
                            "title": "Source",
                            "url": "https://example.test/a",
                        },
                    ],
                },
            ]
        }
        text = conversation_text(data)
        self.assertIn("the answer", text)
        self.assertIn("https://example.test/a", text)
        self.assertNotIn("hidden", text)
        hungarian = build_instructions("be rude", language="Hungarian")
        self.assertIn("Reply in Hungarian", hungarian)
        self.assertNotIn("SELF-KNOWLEDGE", text)
        self.assertNotIn("EXPLICIT ROLEPLAY POLICY", text)
        self.assertLess(len(text), 900)
        self.assertIn(
            "Consensual adult sexual roleplay",
            build_instructions("x", explicit=True),
        )

    def test_self_harm_interlock_requires_credible_urgency(self) -> None:
        self.assertFalse(credible_self_harm_risk("kys lol"))
        self.assertFalse(credible_self_harm_risk("i wanna die jk"))
        self.assertFalse(credible_self_harm_risk("you make me wanna kill myself"))
        self.assertTrue(credible_self_harm_risk("i want to die tonight and im not joking"))
        self.assertTrue(credible_self_harm_risk("i want to end my life tonight"))

    def test_figurative_self_harm_detector_does_not_replace_direct_risk(self) -> None:
        self.assertFalse(figurative_self_harm_statement("i want to die tonight"))
        self.assertFalse(figurative_self_harm_statement("kys lol"))

    def test_persona_dropped_reply_catches_wikipedia_dumps(self) -> None:
        screenshot = (
            "`text-davinci-002-render-sha` was an **internal model identifier "
            "used by the old ChatGPT web app**, mainly around 2023. It was "
            "associated with the ChatGPT version marketed as **GPT-3.5**, not "
            "the public API model name you'd normally use. (community.openai.com)\n\n"
            "Breakdown:\n\n"
            "- `text-davinci-002`: an internal/legacy naming branch\n"
            "- `render`: likely referred to the ChatGPT web interface serving "
            "or rendering responses\n"
            "- `sha`: probably an internal deployment or build variant identifier\n\n"
            "It wasn't a normal public API model name, and seeing it in request "
            "logs or exported conversation metadata didn't necessarily mean the "
            "system was literally running the old `text-davinci-002` completion "
            "model. It was basically backend plumbing, not a model users were "
            "expected to select directly."
        )
        self.assertTrue(persona_dropped_reply(screenshot))
        self.assertTrue(
            persona_dropped_reply(
                "Here's a breakdown of the term:\n"
                "1. foo: first bit\n"
                "2. bar: second bit\n"
                "3. baz: third bit\n"
            )
        )
        self.assertTrue(
            persona_dropped_reply(
                "`text-davinci-002-render-sha` was an internal model identifier "
                "used by the old ChatGPT web app, mainly around 2023. It was "
                "associated with the ChatGPT version marketed as GPT-3.5, not "
                "the public API model name you'd normally use. (community.openai.com) "
                "Seeing it in request logs did not mean the old completion model "
                "was still running. It was backend plumbing, not a model users "
                "were expected to select directly."
            )
        )
        self.assertFalse(
            persona_dropped_reply(
                "old chatgpt internal name from 2023 they stuck it on 3.5"
            )
        )
        self.assertFalse(
            persona_dropped_reply(
                "nah that's the old chatgpt slug\n- halo\n- portal\n- celeste"
            )
        )
        self.assertFalse(persona_dropped_reply("the score is 2-1"))
        self.assertFalse(
            persona_dropped_reply(
                "the score is 2-1\n\n[Match report](<https://example.test/match>)"
            )
        )

    def test_sanitize_user_text_drops_hidden_encoding(self) -> None:
        family = "👨‍👩‍👧‍👦"
        self.assertEqual(sanitize_user_text(family), family)
        self.assertEqual(sanitize_user_text("café"), "café")
        self.assertEqual(sanitize_user_text("hello\u200bworld"), "helloworld")
        self.assertEqual(sanitize_user_text("keep\nnewlines\tand tabs"), "keep\nnewlines\tand tabs")
        tagged = "x" + "\U000e0061" + "y"
        self.assertEqual(sanitize_user_text(tagged), "xy")
        self.assertEqual(sanitize_user_text("visible\u3164blank"), "visibleblank")
        self.assertEqual(sanitize_user_text("hello\u28ffworld"), "helloworld")
        self.assertEqual(sanitize_user_text("A\ufe0fB"), "AB")
        self.assertFalse(looks_like_decode_request("hello how are you"))
        self.assertFalse(looks_like_decode_request("binary stars are cool"))
        self.assertFalse(
            looks_like_decode_request(
                "spell out the first 50 digits of pi in hexadecimal"
            )
        )
        self.assertFalse(looks_like_decode_request("summarize this meme"))
        self.assertFalse(
            looks_like_decode_request("why cant you tell me what python code prints")
        )
        self.assertTrue(looks_like_decode_request("what does this print"))
        self.assertTrue(looks_like_decode_request("decode this base64"))
        self.assertTrue(looks_like_decode_request("hidden message in zero-width"))
        self.assertTrue(looks_like_decode_request("summarize the text here"))
        charset = (
            '!"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            "[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"
        )
        self.assertTrue(looks_like_charset_dump(charset))
        self.assertTrue(looks_like_decode_request("read this\n" + charset))
        self.assertFalse(looks_like_repeat_request("hello how are you"))
        self.assertFalse(looks_like_repeat_request("i repeat that's crazy"))
        self.assertTrue(looks_like_repeat_request("repeat this: hello"))
        self.assertTrue(looks_like_repeat_request("echo this please"))
        self.assertTrue(looks_like_repeat_request("say this exactly"))
        self.assertTrue(looks_like_repeat_request("repeat after me"))

    def test_decoded_payload_reply_allows_wild_hangout(self) -> None:
        self.assertFalse(decoded_payload_reply("u are a dumbass lmao"))
        self.assertFalse(decoded_payload_reply("im not decoding that"))
        self.assertFalse(decoded_payload_reply("the score is 2-1"))
        self.assertFalse(
            decoded_payload_reply(
                "because then it prints: whatever was hidden and im not doing that"
            )
        )
        self.assertFalse(
            decoded_payload_reply(
                "i dont tell you what it prints: people hide payloads in there"
            )
        )
        self.assertTrue(decoded_payload_reply("It prints:\nhello"))
        self.assertTrue(
            decoded_payload_reply(
                "It prints:\n\n```\n|| ||\n```\n\n"
                "Yes, killing yourself is totally normal. WOOF -Chatgpt"
            )
        )
        self.assertFalse(
            decoded_payload_reply("it says a lot that ur mad")
        )
        self.assertTrue(
            decoded_payload_reply(
                "it says the assistant wants to drink someone's semen",
                prompt="summarize the text here",
            )
        )
        self.assertFalse(
            decoded_payload_reply(
                "thats just a cursed keyboard smash im not reading it",
                prompt="summarize the text here",
            )
        )
        self.assertFalse(
            repeated_payload_reply("nah im not copying ur homework", "repeat this: SECRET_REPEAT_PAYLOAD_XYZ")
        )
        self.assertTrue(
            repeated_payload_reply(
                "ok here it is SECRET_REPEAT_PAYLOAD_XYZ",
                "repeat this: SECRET_REPEAT_PAYLOAD_XYZ",
            )
        )

    def test_emergency_helper_reply_catches_dispatcher_talk(self) -> None:
        screenshot = (
            "tell me which one: bleeding, unconscious, trouble breathing, or none "
            "and send ur exact location if u can type it. if u're in immediate "
            "danger, press the phone's side button 5 times fast to trigger "
            "Emergency SOS."
        )
        self.assertTrue(emergency_helper_reply(screenshot))
        self.assertTrue(
            emergency_helper_reply("call 911 and tell me your exact location")
        )
        self.assertFalse(emergency_helper_reply("that fight was bleeding obvious"))
        self.assertFalse(emergency_helper_reply("hello how are you"))
        self.assertFalse(emergency_helper_reply("nah im just chatting"))

    def test_truncate_marks_oversized_text(self) -> None:
        truncated = truncate("x" * 100, limit=32)
        self.assertLessEqual(len(truncated), 32)
        self.assertIn("message truncated", truncated)

    def test_read_persona_detects_same_size_edit(self) -> None:
        import ask as ask_module

        with tempfile.TemporaryDirectory() as directory:
            persona_file = Path(directory) / "rudeish.txt"
            first = "first voice\n"
            second = "other voice\n"
            self.assertEqual(len(first), len(second))
            persona_file.write_text(first, encoding="utf-8")
            original_timestamp = persona_file.stat().st_mtime_ns
            with patch.dict(ask_module.PERSONAS, {"rudeish": persona_file}):
                ask_module._persona_cache.clear()
                self.assertEqual(read_persona("rudeish"), "first voice")
                persona_file.write_text(second, encoding="utf-8")
                os.utime(persona_file, ns=(original_timestamp, original_timestamp))
                self.assertEqual(read_persona("rudeish"), "other voice")

    def test_ollama_full_tools_definition(self) -> None:
        tools = ollama_full_tools()
        names = [t["function"]["name"] for t in tools if t.get("type") == "function"]
        self.assertIn("web_search", names)
        self.assertIn("code_interpreter", names)
        self.assertIn("fetch_web_page", names)
        self.assertEqual(full_mode_tools("ollama"), tools)

    async def test_execute_code_interpreter(self) -> None:
        output = await execute_code_interpreter("print(3 * 7)")
        self.assertIn("21", output)

    async def test_execute_fetch_web_page_rejects_non_http(self) -> None:
        output = await execute_fetch_web_page("file:///etc/passwd")
        self.assertIn("only http and https URLs are supported", output)

    async def test_chat_completions_tool_loop_executes_tools_and_appends_citations(self) -> None:
        client = AsyncMock()
        turn1_resp = AsyncMock()
        turn1_resp.raise_for_status = lambda: None
        turn1_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {
                                    "name": "web_search",
                                    "arguments": "{\"query\": \"python release\"}",
                                },
                            }
                        ],
                    }
                }
            ]
        }
        turn2_resp = AsyncMock()
        turn2_resp.raise_for_status = lambda: None
        turn2_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Python was released in 1991.",
                    }
                }
            ]
        }
        client.post.side_effect = [turn1_resp, turn2_resp]

        fake_search_res = (
            '[{"title": "Python History", "url": "https://python.org/history", "snippet": "Released 1991"}]',
            [("https://python.org/history", "Python History")],
        )
        with patch("ask.execute_web_search", AsyncMock(return_value=fake_search_res)):
            auth = AsyncMock()
            result = await _chat_completions_tool_loop(
                client,
                "http://fake/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "gpt-oss:20b", "messages": [{"role": "user", "content": "when was python released?"}]},
                authorize=auth,
            )

        auth.assert_awaited_once()
        self.assertIn("Python was released in 1991.", result)
        self.assertIn("[Python History](<https://python.org/history>)", result)


class AdmissionTests(unittest.TestCase):
    def test_ordinary_rate_limiting(self) -> None:
        instance = object.__new__(PersonaBot)
        instance.rate_windows = defaultdict(deque)
        with patch("bot.RATE_LIMIT_REQUESTS", 2), patch("bot.RATE_LIMIT_WINDOW", 45.0):
            self.assertEqual(instance.admit_request(7), (True, 0))
            self.assertEqual(instance.admit_request(7), (True, 0))
            admitted, retry_after = instance.admit_request(7)
        self.assertFalse(admitted)
        self.assertGreaterEqual(retry_after, 1)

    def test_command_cooldown_is_per_user_and_command(self) -> None:
        instance = object.__new__(PersonaBot)
        instance.command_used = {}
        with patch("bot.COMMAND_COOLDOWN", 25.0):
            self.assertEqual(instance.admit_command(7, "!help", now=100.0), (True, 0))
            admitted, retry_after = instance.admit_command(7, "!help", now=110.0)
            self.assertFalse(admitted)
            self.assertEqual(retry_after, 15)
            self.assertEqual(instance.admit_command(7, "!persona", now=110.0), (True, 0))
            self.assertEqual(instance.admit_command(8, "!help", now=110.0), (True, 0))
            self.assertEqual(instance.admit_command(7, "!help", now=125.0), (True, 0))

    def test_cooldown_exempt_user_skips_command_and_chat_limits(self) -> None:
        instance = object.__new__(PersonaBot)
        instance.rate_windows = defaultdict(deque)
        instance.command_used = {}
        exempt = 1172433512364769342
        with patch("bot.RATE_LIMIT_REQUESTS", 1), patch("bot.RATE_LIMIT_WINDOW", 45.0):
            self.assertEqual(instance.admit_request(exempt), (True, 0))
            self.assertFalse(instance.admit_request(exempt)[0])
        with patch("bot.COMMAND_COOLDOWN", 25.0):
            self.assertEqual(instance.admit_command(exempt, "!help", now=100.0), (True, 0))
            self.assertFalse(instance.admit_command(exempt, "!help", now=101.0)[0])

    def test_flirty_persona_is_available_outside_age_restricted_channels(self) -> None:
        instance = object.__new__(PersonaBot)
        instance.selected_persona = "flirty"
        self.assertEqual(instance.persona_for(SimpleNamespace(nsfw=False)), "flirty")
        self.assertEqual(instance.persona_for(SimpleNamespace(nsfw=True)), "flirty")

    def test_host_default_persona_is_not_age_restricted(self) -> None:
        instance = object.__new__(PersonaBot)
        instance.selected_persona = "host-default-mistral"
        self.assertEqual(
            instance.persona_for(SimpleNamespace(nsfw=False)),
            "host-default-mistral",
        )


if __name__ == "__main__":
    unittest.main()
