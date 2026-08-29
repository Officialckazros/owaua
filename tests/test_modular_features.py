"""Regression tests for the modular moderation, vision, and slash helpers."""

import os
import types
import typing
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from owaua import config, rules, slash, voice
from owaua.services.llm_client import (
    _extract_json,
    _validate_download_url,
    coerce_bool,
    sniff_image_mime,
)


class LlmClientHelpersTest(unittest.IsolatedAsyncioTestCase):
    def test_extract_json_accepts_fenced_model_output(self):
        self.assertEqual(_extract_json('```json\n{"ok": true}\n```'), {"ok": True})

    def test_sniff_image_mime_uses_bytes_not_filename(self):
        self.assertEqual(sniff_image_mime(b"\x89PNG\r\n\x1a\nrest"), "image/png")
        self.assertEqual(sniff_image_mime(b"GIF89arest"), "image/gif")
        self.assertIsNone(sniff_image_mime(b"<html>not an image</html>"))

    def test_string_false_is_not_treated_as_true(self):
        self.assertFalse(coerce_bool("false"))
        self.assertFalse(coerce_bool("0"))
        self.assertTrue(coerce_bool("true"))

    async def test_download_url_rejects_private_and_non_http_targets(self):
        for url in (
            "http://127.0.0.1/image.png",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/image.png",
            "file:///etc/passwd",
            "https://user:password@example.com/image.png",
            "https://example.com:8443/image.png",
        ):
            with self.subTest(url=url):
                self.assertFalse(await _validate_download_url(url))


class RulesRegressionTest(unittest.TestCase):
    def test_warn_limit_escalates_from_kick_to_repeat_ban(self):
        rule = rules.Rule("test_warn", "warn", "warn", "Test", "Test detail", warn_limit=2)
        self.assertEqual(rules._resolve_action(rule, 0, False)[0], "warn")
        self.assertEqual(rules._resolve_action(rule, 1, False)[0], "kick")
        self.assertEqual(rules._resolve_action(rule, 2, True)[0], "ban")

    def test_pending_action_carries_rule_detail_for_warning_dm(self):
        pending = rules.PendingAction(
            guild_id=1,
            rule_id="spam",
            rule_name="Spam",
            rule_detail="Do not repeat messages.",
            category="warn",
            action_label="warn",
            offender_id=2,
            offender_tag="user",
            evidence="same text",
            channel_id=3,
            message_id=4,
            strikes=0,
            warn_limit=3,
            timeout_minutes=0,
        )
        self.assertEqual(pending.rule_detail, "Do not repeat messages.")

    def test_kys_reply_to_bot_is_exempt(self):
        class FakeDiscordMessage:
            pass

        bot_user = types.SimpleNamespace(id=99)
        replied = FakeDiscordMessage()
        typing.cast(typing.Any, replied).author = bot_user
        message = FakeDiscordMessage()
        typing.cast(typing.Any, message).content = "kys"
        typing.cast(typing.Any, message).mentions = []
        typing.cast(typing.Any, message).author = types.SimpleNamespace(
            id=1, name="member", display_name="member"
        )
        typing.cast(typing.Any, message).reference = types.SimpleNamespace(resolved=replied)
        client = types.SimpleNamespace(user=bot_user)
        with mock.patch.object(rules.discord, "Message", FakeDiscordMessage):
            self.assertIsNone(rules.detect_rule(client, typing.cast(typing.Any, message)))


class CooldownRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        slash._last_uses.clear()

    async def test_cooldown_honors_rate_and_isolated_commands(self):
        calls: list[typing.Any] = []

        class Response:
            def __init__(self):
                self.messages: list[typing.Any] = []

            async def send_message(self, **kwargs: typing.Any):
                self.messages.append(kwargs)

        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=7), response=Response())

        @slash._cooldown(2, 60)
        async def first(interaction: typing.Any):
            calls.append("first")

        @slash._cooldown(1, 60)
        async def second(interaction: typing.Any):
            calls.append("second")

        await first(typing.cast(typing.Any, interaction))
        await first(typing.cast(typing.Any, interaction))
        await first(typing.cast(typing.Any, interaction))
        await second(typing.cast(typing.Any, interaction))

        self.assertEqual(calls, ["first", "first", "second"])
        self.assertEqual(len(interaction.response.messages), 1)


class VoiceControlRegressionTest(unittest.TestCase):
    def test_tts_defaults_match_the_current_groq_orpheus_contract(self):
        self.assertEqual(config.TTS_MODEL, "canopylabs/orpheus-v1-english")
        self.assertEqual(config.TTS_VOICE, "troy")
        self.assertEqual(voice._MAX_TTS_CHARACTERS, 200)

    def test_voice_control_requires_same_channel_or_manager(self):
        class FakeMember:
            def __init__(self, channel_id: typing.Any, *, manage: bool = False):
                self.voice = types.SimpleNamespace(channel=types.SimpleNamespace(id=channel_id))
                self.guild_permissions = types.SimpleNamespace(
                    administrator=False, manage_guild=manage
                )

        target = types.SimpleNamespace(id=10)
        with mock.patch.object(voice.discord, "Member", FakeMember):
            same = types.SimpleNamespace(user=FakeMember(10))
            different = types.SimpleNamespace(user=FakeMember(11))
            manager = types.SimpleNamespace(user=FakeMember(11, manage=True))
            self.assertTrue(voice._can_control_channel(typing.cast(typing.Any, same), target))
            self.assertFalse(voice._can_control_channel(typing.cast(typing.Any, different), target))
            self.assertTrue(voice._can_control_channel(typing.cast(typing.Any, manager), target))

    def test_stt_queue_is_bounded(self):
        session = voice.SttSession(1, types.SimpleNamespace())
        with mock.patch.object(voice.log, "warning"):
            for index in range(20):
                session.enqueue(index, b"wav", 500.0)
        self.assertEqual(session.queue.qsize(), 16)


if __name__ == "__main__":
    unittest.main()
