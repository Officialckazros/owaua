"""Regression tests for chat-provider error handling.

The Discord "brain hiccuped" embed is the last-resort path: DeepSeek V4 Flash
thinks by default, reasoning tokens eat max_tokens, and empty/error bodies used
to fail the whole fallback chain every few messages.
"""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from sefbot import ai, config


def _http_error(code: int, body: bytes = b"") -> HTTPError:
    return HTTPError(
        "https://example.test/chat/completions",
        code,
        "error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


class ChoiceTextTest(unittest.TestCase):
    def test_content_wins(self) -> None:
        payload = {"choices": [{"message": {"content": "  hello  "}}]}
        self.assertEqual(ai._choice_text(payload, "deepseek"), "hello")

    def test_reasoning_used_when_content_empty(self) -> None:
        payload = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": '{"response": "ok"}',
                }
            }]
        }
        self.assertEqual(ai._choice_text(payload, "deepseek"), '{"response": "ok"}')

    def test_openrouter_error_body_raises(self) -> None:
        payload = {"error": {"message": "Provider returned error", "code": 429}}
        with self.assertRaises(RuntimeError) as ctx:
            ai._choice_text(payload, "openrouter")
        self.assertIn("429", str(ctx.exception))
        self.assertTrue(ai._is_rate_limited(ctx.exception))

    def test_empty_choices_raise(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "empty content"):
            ai._choice_text({"choices": [{"message": {"content": None}}]}, "deepseek")


class DeepseekModelTest(unittest.TestCase):
    def test_legacy_inferx_aliases_migrate_to_official_model(self) -> None:
        self.assertEqual(
            config.canonical_model("ix:deepseek-v4-flash-0731"),
            "deepseek-v4-flash",
        )
        self.assertEqual(
            config.canonical_model("deepseek-v4-flash-0371"),
            "deepseek-v4-flash",
        )


class ExtractJsonTest(unittest.TestCase):
    def test_fenced_object(self) -> None:
        self.assertEqual(ai._extract_json('```json\n{"ok": true}\n```'), {"ok": True})

    def test_salvages_truncated_response_field(self) -> None:
        raw = '{"response": "yo what\'s up", "title": null, "memori'
        self.assertEqual(ai._extract_json(raw), {"response": "yo what's up"})

    def test_empty_is_none(self) -> None:
        self.assertIsNone(ai._extract_json(""))
        self.assertIsNone(ai._extract_json(None))  # type: ignore[arg-type]


class TransientTest(unittest.TestCase):
    def test_empty_content_is_retryable(self) -> None:
        self.assertTrue(ai._is_transient(RuntimeError("deepseek: empty content")))

    def test_timeout_is_retryable(self) -> None:
        self.assertTrue(ai._is_transient(RuntimeError("deepseek request failed (timeout)")))
        self.assertTrue(ai._is_transient(URLError("timed out")))

    def test_auth_is_not_retryable(self) -> None:
        self.assertFalse(ai._is_transient(RuntimeError("deepseek request failed (401)")))
        self.assertTrue(ai._is_fatal(RuntimeError("no deepseek api key configured")))

    def test_404_is_not_retryable(self) -> None:
        self.assertFalse(ai._is_transient(RuntimeError("deepseek request failed (404)")))


class FriendlyErrorTest(unittest.TestCase):
    def test_timeout_is_not_a_generic_hiccup(self) -> None:
        msg = ai.friendly_error(RuntimeError("deepseek request failed (timeout)"))
        self.assertIn("too long", msg)

    def test_rate_limit_still_named(self) -> None:
        msg = ai.friendly_error(RuntimeError("Error code: 429 - try again in 6s"))
        self.assertIn("out of tokens", msg)

    def test_unknown_stays_hiccup(self) -> None:
        self.assertEqual(
            ai.friendly_error(RuntimeError("deepseek: empty content")),
            "my brain hiccuped. try again in a moment",
        )


class GenerateRetryTest(unittest.TestCase):
    def test_retries_empty_then_succeeds(self) -> None:
        calls = {"n": 0}

        def fake(model, system, messages, max_tokens, temperature):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("deepseek: empty content")
            return '{"response": "recovered"}'

        with mock.patch.object(ai, "_deepseek_generate", fake), \
             mock.patch.object(ai.time, "sleep"), \
             mock.patch.object(config, "MODEL_FALLBACKS", []), \
             mock.patch.object(config, "DEEPSEEK_API_KEY", "test-key"):
            out = ai._generate(
                "deepseek-v4-flash",
                "sys",
                [{"role": "user", "content": "hi"}],
                100,
                0.5,
            )
        self.assertEqual(out, '{"response": "recovered"}')
        self.assertEqual(calls["n"], 3)

    def test_empty_string_is_not_treated_as_success(self) -> None:
        calls = {"n": 0}

        def fake(model, system, messages, max_tokens, temperature):
            calls["n"] += 1
            if calls["n"] == 1:
                return ""
            return "ok"

        with mock.patch.object(ai, "_deepseek_generate", fake), \
             mock.patch.object(ai.time, "sleep"), \
             mock.patch.object(config, "MODEL_FALLBACKS", []), \
             mock.patch.object(config, "DEEPSEEK_API_KEY", "test-key"):
            out = ai._generate(
                "deepseek-v4-flash",
                "sys",
                [{"role": "user", "content": "hi"}],
                100,
                0.5,
            )
        self.assertEqual(out, "ok")
        self.assertEqual(calls["n"], 2)


class OfficialDeepseekChatTest(unittest.TestCase):
    def test_sends_thinking_disabled_to_official_api(self) -> None:
        captured = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["body"] = json.loads(req.data.decode())
            payload = json.dumps({
                "choices": [{"message": {"content": '{"response": "hi"}'}}]
            }).encode()
            return mock.MagicMock(
                __enter__=lambda self: self,
                __exit__=mock.Mock(return_value=False),
                read=lambda n=-1: payload,
            )

        with mock.patch("sefbot.ai.urllib.request.urlopen", fake_urlopen), \
             mock.patch.object(config, "DEEPSEEK_API_KEY", "test-key"):
            text = ai._deepseek_generate(
                "deepseek-v4-flash",
                "sys",
                [{"role": "user", "content": "hi"}],
                200,
                0.4,
            )
        self.assertEqual(text, '{"response": "hi"}')
        self.assertEqual(captured["url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "deepseek-v4-flash")
        self.assertEqual(captured["body"]["thinking"], {"type": "disabled"})
        self.assertGreaterEqual(captured["timeout"], 45)

    def test_drops_thinking_on_400_and_retries(self) -> None:
        calls = []

        def fake_urlopen(req, timeout=0):
            body = json.loads(req.data.decode())
            calls.append(body)
            if "thinking" in body:
                raise _http_error(400, b'{"error": "unknown field thinking"}')
            payload = json.dumps({
                "choices": [{"message": {"content": "plain"}}]
            }).encode()
            return mock.MagicMock(
                __enter__=lambda self: self,
                __exit__=mock.Mock(return_value=False),
                read=lambda n=-1: payload,
            )

        with mock.patch("sefbot.ai.urllib.request.urlopen", fake_urlopen), \
             mock.patch.object(config, "DEEPSEEK_API_KEY", "test-key"):
            text = ai._deepseek_generate(
                "deepseek-v4-flash",
                "sys",
                [{"role": "user", "content": "hi"}],
                200,
                0.4,
            )
        self.assertEqual(text, "plain")
        self.assertEqual(len(calls), 2)
        self.assertIn("thinking", calls[0])
        self.assertNotIn("thinking", calls[1])


if __name__ == "__main__":
    unittest.main()
