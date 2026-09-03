"""Regression tests for the modular moderation, vision, and slash helpers."""

import os
import types
import typing
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from owaua import community, config, rules, slash, voice
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


class NativeAutomodTest(unittest.IsolatedAsyncioTestCase):
    def test_native_keywords_are_bounded_and_deduplicated(self):
        values = [" spam ", "SPAM", "", "x" * 61, *[f"word-{i}" for i in range(110)]]
        result = community._native_automod_keywords({"banned_phrases": values})
        self.assertEqual(result[:2], ["spam", "word-0"])
        self.assertLessEqual(len(result), 100)

    def test_message_filter_accepts_current_and_legacy_dashboard_field_names(self):
        message = types.SimpleNamespace(
            content="please remove this temporary message",
            attachments=[],
            embeds=[],
            mentions=[],
            author=types.SimpleNamespace(bot=False, roles=[]),
        )
        self.assertTrue(
            community._filter_matches(
                typing.cast(typing.Any, message),
                {"type": "includes_text", "value": "temporary"},
            )
        )
        self.assertTrue(
            community._filter_matches(
                typing.cast(typing.Any, message),
                {"kind": "includes_text", "value": "temporary"},
            )
        )

    async def test_sync_creates_one_native_rule_for_configured_phrases(self):
        guild = types.SimpleNamespace(
            id=123,
            me=types.SimpleNamespace(
                guild_permissions=types.SimpleNamespace(manage_guild=True)
            ),
            get_channel=lambda channel_id: None,
            fetch_automod_rules=mock.AsyncMock(return_value=[]),
            create_automod_rule=mock.AsyncMock(),
        )
        with mock.patch.object(
            community,
            "_cfg",
            return_value={"enabled": True, "settings": {"banned_phrases": ["scam"]}},
        ):
            self.assertTrue(await community.sync_native_automod(typing.cast(typing.Any, guild)))
        guild.create_automod_rule.assert_awaited_once()
        call = guild.create_automod_rule.await_args.kwargs
        self.assertEqual(call["name"], "owaua: configured blocked phrases")
        self.assertEqual(call["trigger"].keyword_filter, ["scam"])

    def test_caps_and_length_checks_are_opt_in(self):
        message = types.SimpleNamespace(
            content="THIS MESSAGE IS LOUD",
            channel=types.SimpleNamespace(id=10, category_id=None),
            author=types.SimpleNamespace(id=456, roles=[]),
            mentions=[],
            role_mentions=[],
            guild=types.SimpleNamespace(id=123),
        )
        settings = {
            "max_caps_enabled": False,
            "max_caps_percent": 80,
            "max_length_enabled": False,
            "max_length": 1800,
            "max_newlines": 8,
            "max_mentions": 5,
            "banned_phrases": [],
            "blocked_domains": [],
            "allowed_domains": [],
            "rapid_messages": 6,
            "rapid_window_seconds": 8,
            "duplicate_window_seconds": 15,
        }

        self.assertIsNone(community._automod_reason(message, settings))
        community._duplicates.clear()
        settings["max_caps_enabled"] = True
        self.assertEqual(community._automod_reason(message, settings), "excessive capitals")
        settings["max_caps_enabled"] = False
        settings["max_length_enabled"] = True
        community._duplicates.clear()
        message.content = "x" * 2000
        self.assertEqual(community._automod_reason(message, settings), "message too long")


class TicketSystemTest(unittest.TestCase):
    def test_ticket_controls_have_channel_bound_persistent_ids(self):
        view = community.TicketControlView(123, 456)
        self.assertEqual(
            [typing.cast(typing.Any, item).custom_id for item in view.children],
            ["owaua:ticket:claim:123:456", "owaua:ticket:close:123:456"],
        )

    def test_ticket_channel_name_uses_configured_template_safely(self):
        member = types.SimpleNamespace(
            id=7,
            name="Casey Example",
            display_name="Casey Example",
            mention="<@7>",
            guild=types.SimpleNamespace(id=42, name="Support"),
        )
        with mock.patch.object(community.secrets, "randbelow", return_value=12):
            result = community._ticket_channel_name(
                {"channel_name": "help-{user.name}"}, typing.cast(typing.Any, member)
            )
        self.assertEqual(result, "help-casey-example-0012")


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
