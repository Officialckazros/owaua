from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ask import AssistantReply
from bot import (
    DISCORD_MESSAGE_LIMIT,
    FULL_MODE_ALLOWED_USER_IDS,
    FULL_MODE_BLOCKED_USER_IDS,
    FULL_MODE_CHANNEL_ID,
    FULL_MODE_ENABLE_USER_IDS,
    FULL_MODE_GUILD_ID,
    FULL_MODE_USAGE,
    HELP_TEXT,
    OWNER_HELP_TEXT,
    OWNER_NOTE_TEXT,
    OWNER_IDS,
    PING_RESPONSE,
    pricing_text,
    MessageEventGuard,
    PersonaBot,
)
from memory import MemoryStore


class FakeChannel:
    def __init__(self, channel_id: int = 22, *, nsfw: bool = False) -> None:
        self.id = channel_id
        self.nsfw = nsfw
        self.sent: list[str] = []
        self.send_kwargs: list[dict[str, object]] = []

    async def send(self, content: str, **kwargs: object) -> None:
        self.sent.append(content)
        self.send_kwargs.append(kwargs)

    def typing(self) -> "_Typing":
        return _Typing()


class _Typing:
    async def __aenter__(self) -> "_Typing":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


def profile_edit_fields(member: object) -> dict[str, object]:
    merged: dict[str, object] = {}
    for call in member.edit.await_args_list:
        merged.update(call.kwargs)
    return merged


def make_message(
    content: str,
    message_id: int,
    channel: FakeChannel,
    *,
    author_id: int = 33,
    guild_id: int | None = 11,
    manage_guild: bool = True,
    mentions: list[object] | None = None,
    reference: object | None = None,
    attachments: list[object] | None = None,
) -> SimpleNamespace:
    guild = None
    if guild_id is not None:
        guild = SimpleNamespace(
            id=guild_id,
            voice_client=None,
            me=SimpleNamespace(edit=AsyncMock()),
            get_member=Mock(return_value=None),
        )
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(
            id=author_id,
            bot=False,
            guild_permissions=SimpleNamespace(manage_guild=manage_guild),
        ),
        channel=channel,
        guild=guild,
        mentions=mentions or [],
        reference=reference,
        attachments=attachments or [],
        created_at=datetime.now(timezone.utc),
    )


class ChannelCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        dm_patch = patch("bot.ALLOW_DMS", True)
        dm_patch.start()
        self.addCleanup(dm_patch.stop)
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(
            Path(self.temporary_directory.name) / "memory.sqlite3"
        )
        self.bot = object.__new__(PersonaBot)
        self.bot.memory = self.store
        self.bot.message_events = MessageEventGuard()
        self.bot._connection = SimpleNamespace(user=SimpleNamespace(id=99))
        self.bot.provider_http = SimpleNamespace()
        self.bot.selected_persona = "rudeish"
        self.bot.full_mode_users = set()
        self.bot.rate_windows = defaultdict(deque)
        self.bot.command_used = {}
        self.bot.conversation_locks = defaultdict(asyncio.Lock)
        self.bot.music_tracks = {}
        self.bot.response_languages = {}
        self.bot.shutdown_requested = False
        self.bot.active_handlers = set()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_help_command_lists_the_remaining_commands(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!help", 1, channel))

        self.assertEqual(channel.sent, [HELP_TEXT])
        self.assertEqual(channel.send_kwargs[0].get("suppress_embeds"), True)
        self.assertNotIn("!active", channel.sent[0])
        self.assertIn("!persona rudeish|nerdish|flirty|chaotic|host default gpt/deepseek/mistral", channel.sent[0])
        self.assertIn("!owner's note", channel.sent[0])
        self.assertIn("!memory erase", channel.sent[0])
        self.assertIn("!music help", channel.sent[0])
        self.assertIn("!language <full name>|reset", channel.sent[0])
        self.assertIn("25s cooldown", channel.sent[0])
        self.assertNotIn("!debate", channel.sent[0])
        self.assertNotIn("!topic", channel.sent[0])
        self.assertNotIn("!vc", channel.sent[0])
        self.assertNotIn("!nuke", channel.sent[0])
        self.assertNotIn("!gifs", channel.sent[0])
        self.assertNotIn("!full", channel.sent[0])

    async def test_help_hides_owner_only_commands_from_non_owner(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!help", 1, channel, author_id=33))

        self.assertEqual(channel.sent, [HELP_TEXT])
        self.assertNotIn("!security", channel.sent[0])
        self.assertNotIn("!shutdown", channel.sent[0])

    async def test_help_shows_owner_only_commands_to_configured_help_owner(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(
            make_message("!help", 1, channel, author_id=1172433512364769342)
        )

        self.assertEqual(channel.sent, [OWNER_HELP_TEXT])
        self.assertIn("!security status|pause|resume", channel.sent[0])
        self.assertIn("!shutdown", channel.sent[0])

    async def test_pricing_is_owner_only_and_hidden_from_regular_help(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!help", 1, channel))
        self.assertNotIn("!pricing", channel.sent[0])

        await self.bot.on_message(make_message("!pricing", 2, channel))
        self.assertEqual(len(channel.sent), 1)

    async def test_pricing_is_shown_to_the_configured_help_owner(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(
            make_message("!pricing", 1, channel, author_id=1172433512364769342)
        )

        self.assertEqual(channel.sent, [pricing_text()])
        self.assertIn("openai/gpt-5.6-luna", channel.sent[0])
        self.assertIn("anthropic/claude-haiku-4-5", channel.sent[0])

    async def test_trusted_guild_music_bypasses_command_cooldown(self) -> None:
        channel = FakeChannel()
        first = make_message("!music https://example.test/one", 1, channel,
                             guild_id=FULL_MODE_GUILD_ID)
        second = make_message("!music https://example.test/two", 2, channel,
                              guild_id=FULL_MODE_GUILD_ID)

        with patch("bot.handle_music_command", AsyncMock(return_value="playing")) as music:
            await self.bot.on_message(first)
            await self.bot.on_message(second)

        self.assertEqual(music.await_count, 2)
        self.assertEqual(channel.sent, ["playing", "playing"])
        self.assertEqual(self.bot.command_used, {})

    async def test_shutdown_is_owner_only_and_sends_no_acknowledgement(self) -> None:
        channel = FakeChannel()
        with patch.object(self.bot, "close", new=AsyncMock()) as close:
            await self.bot.on_message(make_message("!shutdown", 1, channel))
            self.assertFalse(self.bot.shutdown_requested)
            close.assert_not_awaited()

            owner = next(iter(OWNER_IDS))
            await self.bot.on_message(
                make_message("!shutdown", 2, channel, author_id=owner, guild_id=None)
            )

        self.assertTrue(self.bot.shutdown_requested)
        self.assertEqual(channel.sent, [])
        close.assert_awaited_once()

    async def test_shutdown_suppresses_late_replies(self) -> None:
        channel = FakeChannel()
        self.bot.shutdown_requested = True

        await self.bot._reply(make_message("!help", 1, channel), "should not send")

        self.assertEqual(channel.sent, [])

    async def test_owners_note_command_sends_the_owner_message(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!owner's note", 1, channel))

        self.assertEqual(channel.sent, [OWNER_NOTE_TEXT])

    async def test_owners_note_command_accepts_curly_apostrophes(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!owner’s note", 1, channel))

        self.assertEqual(len(channel.sent), 1)
        self.assertIn("ckazros@owaua.com", channel.sent[0])

    async def test_concurrent_duplicate_command_event_replies_once(self) -> None:
        channel = FakeChannel()
        message = make_message("!help", 1, channel)

        await asyncio.gather(
            self.bot.on_message(message),
            self.bot.on_message(message),
        )

        self.assertEqual(channel.sent, [HELP_TEXT])

    def _full_mode_message(
        self,
        content: str,
        message_id: int,
        *,
        author_id: int = next(iter(FULL_MODE_ENABLE_USER_IDS)),
        guild_id: int | None = FULL_MODE_GUILD_ID,
        channel_id: int = FULL_MODE_CHANNEL_ID,
        mentions: list[object] | None = None,
        attachments: list[object] | None = None,
    ) -> SimpleNamespace:
        return make_message(
            content,
            message_id,
            FakeChannel(channel_id),
            author_id=author_id,
            guild_id=guild_id,
            mentions=mentions,
            attachments=attachments,
        )

    async def test_full_mode_on_only_works_in_the_allowed_channel(self) -> None:
        owner = next(iter(FULL_MODE_ENABLE_USER_IDS))
        message = self._full_mode_message("!full mode on", 1)

        await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, ["full mode on"])
        self.assertTrue(self.bot.full_mode_enabled_for(owner))
        self.assertEqual(self.store.get_setting(f"full_mode:{owner}"), "1")

    async def test_full_mode_off_disables_the_flag(self) -> None:
        owner = next(iter(FULL_MODE_ENABLE_USER_IDS))
        self.bot.set_full_mode_for(owner, True)
        message = self._full_mode_message("!full mode off", 1)

        await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, ["full mode off"])
        self.assertFalse(self.bot.full_mode_enabled_for(owner))
        self.assertEqual(self.store.get_setting(f"full_mode:{owner}"), "0")

    async def test_full_mode_command_is_ignored_outside_the_allowed_channel(self) -> None:
        same_guild = self._full_mode_message(
            "!full mode on", 1, channel_id=22
        )
        other_guild = self._full_mode_message(
            "!full mode on", 2, guild_id=11
        )
        dm = self._full_mode_message("!full mode on", 3, guild_id=None)

        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(same_guild)
            await self.bot.on_message(other_guild)
            await self.bot.on_message(dm)

        self.assertEqual(same_guild.channel.sent, [])
        self.assertEqual(other_guild.channel.sent, [])
        self.assertEqual(dm.channel.sent, ["hey"])
        self.assertFalse(
            self.bot.full_mode_enabled_for(next(iter(FULL_MODE_ENABLE_USER_IDS)))
        )
        mocked_ask.assert_awaited_once()
        self.assertFalse(mocked_ask.await_args.kwargs["full_mode"])

    async def test_full_mode_command_still_matches_when_the_bot_is_pinged(self) -> None:
        message = self._full_mode_message(
            "<@99> !full mode on", 1, mentions=[self.bot.user]
        )

        await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, ["full mode on"])
        self.assertTrue(
            self.bot.full_mode_enabled_for(next(iter(FULL_MODE_ENABLE_USER_IDS)))
        )

    async def test_blocked_user_cannot_use_full_mode(self) -> None:
        blocked = next(iter(FULL_MODE_BLOCKED_USER_IDS))
        message = self._full_mode_message("!full mode on", 1, author_id=blocked)

        await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, ["you can't use this"])
        self.assertFalse(self.bot.full_mode_enabled_for(blocked))
        self.assertEqual(self.store.get_setting(f"full_mode:{blocked}", ""), "")

    async def test_only_the_owner_can_enable_full_mode(self) -> None:
        message = self._full_mode_message("!full mode on", 1, author_id=33)

        await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, ["you can't use this"])
        self.assertFalse(self.bot.full_mode_enabled_for(33))
        self.assertEqual(self.store.get_setting("full_mode:33", ""), "")

    async def test_allowlisted_user_can_enable_full_mode_for_themselves(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        message = self._full_mode_message(
            "!full mode on", 1, author_id=allowed_user
        )

        await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, ["full mode on"])
        self.assertTrue(self.bot.full_mode_enabled_for(allowed_user))
        self.assertEqual(self.store.get_setting(f"full_mode:{allowed_user}"), "1")

    async def test_unlimited_channel_applies_to_blocked_users_too(
        self,
    ) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        self.bot.set_full_mode_for(allowed_user, True)
        allowed = self._full_mode_message(
            "<@99> hello",
            1,
            author_id=allowed_user,
            mentions=[self.bot.user],
        )
        other = make_message(
            "<@99> hello",
            2,
            FakeChannel(22),
            author_id=allowed_user,
            guild_id=FULL_MODE_GUILD_ID,
            mentions=[self.bot.user],
        )
        blocked = next(iter(FULL_MODE_BLOCKED_USER_IDS))
        blocked_message = self._full_mode_message(
            "<@99> hello", 3, author_id=blocked, mentions=[self.bot.user]
        )

        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(allowed)
            await self.bot.on_message(other)
            await self.bot.on_message(blocked_message)

        self.assertEqual(
            [call.kwargs["full_mode"] for call in mocked_ask.await_args_list],
            [True, False, True],
        )
        self.assertEqual(
            [call.kwargs["relaxed_guardrails"] for call in mocked_ask.await_args_list],
            [True, False, True],
        )

    async def test_blocked_user_is_forced_to_groq_blocked_persona(self) -> None:
        message = make_message("hello", 100, FakeChannel(), mentions=[self.bot.user])
        with patch("bot.BLOCKED_USERS", {33}), patch(
            "bot.ask", AsyncMock(return_value="nope")
        ) as provider:
            await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, ["nope"])
        self.assertEqual(provider.await_args.kwargs["persona"], "blocked")
        self.assertEqual(provider.await_args.kwargs["provider_override"], "groq")
        self.assertFalse(provider.await_args.kwargs["full_mode"])

    async def test_unlimited_channel_does_not_require_allowlisted_users(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        self.bot.set_full_mode_for(allowed_user, True)
        self.bot.set_full_mode_for(33, True)
        self.bot.set_full_mode_for(next(iter(FULL_MODE_ENABLE_USER_IDS)), True)
        allowed = self._full_mode_message(
            "<@99> hello",
            1,
            author_id=allowed_user,
            mentions=[self.bot.user],
        )
        outsider = self._full_mode_message(
            "<@99> hello", 2, author_id=33, mentions=[self.bot.user]
        )
        owner = next(iter(FULL_MODE_ENABLE_USER_IDS))
        owner_message = self._full_mode_message(
            "<@99> hello", 3, author_id=owner, mentions=[self.bot.user]
        )

        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(allowed)
            await self.bot.on_message(outsider)
            await self.bot.on_message(owner_message)

        self.assertEqual(
            [call.kwargs["full_mode"] for call in mocked_ask.await_args_list],
            [True, True, True],
        )

    async def test_full_mode_does_not_answer_unaddressed_channel_messages(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        self.bot.set_full_mode_for(allowed_user, True)
        message = self._full_mode_message("just chatting", 1, author_id=allowed_user)

        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(message)

        mocked_ask.assert_not_awaited()
        self.assertEqual(message.channel.sent, [])

    async def test_unlimited_channel_has_unbounded_memory_without_user_toggle(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        message = self._full_mode_message(
            "<@99> hello",
            1,
            author_id=allowed_user,
            mentions=[self.bot.user],
        )

        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(message)

        self.assertTrue(mocked_ask.await_args.kwargs["full_mode"])
        self.assertTrue(mocked_ask.await_args.kwargs["use_history"])

    async def test_unlimited_channel_does_not_depend_on_user_toggle(self) -> None:
        users = list(FULL_MODE_ALLOWED_USER_IDS)
        first, second = users[0], users[1]
        await self.bot.on_message(
            self._full_mode_message("!full mode on", 1, author_id=first)
        )
        first_ping = self._full_mode_message(
            "<@99> hello", 2, author_id=first, mentions=[self.bot.user]
        )
        second_ping = self._full_mode_message(
            "<@99> hello", 3, author_id=second, mentions=[self.bot.user]
        )

        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(first_ping)
            await self.bot.on_message(second_ping)

        self.assertTrue(self.bot.full_mode_enabled_for(first))
        self.assertFalse(self.bot.full_mode_enabled_for(second))
        self.assertEqual(
            [call.kwargs["full_mode"] for call in mocked_ask.await_args_list],
            [True, True],
        )

    async def test_full_mode_unknown_argument_prints_usage(self) -> None:
        message = self._full_mode_message("!full mode maybe", 1)

        await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, [FULL_MODE_USAGE])
        self.assertFalse(
            self.bot.full_mode_enabled_for(next(iter(FULL_MODE_ENABLE_USER_IDS)))
        )

    async def test_full_mode_ping_outside_the_channel_is_ordinary_chat(self) -> None:
        message = make_message(
            "<@99> !full mode on",
            1,
            FakeChannel(22),
            guild_id=FULL_MODE_GUILD_ID,
            mentions=[self.bot.user],
        )
        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(message)

        mocked_ask.assert_awaited_once()
        self.assertFalse(mocked_ask.await_args.kwargs["full_mode"])
        self.assertFalse(self.bot.full_mode_enabled_for(33))
        self.assertEqual(message.channel.sent, ["hey"])

    async def test_full_mode_reports_a_missing_gpt_key(self) -> None:
        message = self._full_mode_message("!full mode on", 1)
        with patch("bot.host_model_error", return_value="gpt is not configured"):
            await self.bot.on_message(message)

        self.assertEqual(message.channel.sent, ["gpt is not configured"])
        self.assertFalse(
            self.bot.full_mode_enabled_for(next(iter(FULL_MODE_ENABLE_USER_IDS)))
        )

    async def test_full_mode_skips_chat_rate_limit_and_admission(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        self.bot.set_full_mode_for(allowed_user, True)
        self.bot.inflight_users = {1, 2, 3}
        first = self._full_mode_message(
            "<@99> one", 1, author_id=allowed_user, mentions=[self.bot.user]
        )
        second = self._full_mode_message(
            "<@99> two", 2, author_id=allowed_user, mentions=[self.bot.user]
        )
        with (
            patch("bot.RATE_LIMIT_REQUESTS", 1),
            patch("bot.MAX_INFLIGHT", 1),
            patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask,
        ):
            await self.bot.on_message(first)
            await self.bot.on_message(second)

        self.assertEqual(mocked_ask.await_count, 2)
        self.assertEqual(self.bot.inflight_users, {1, 2, 3})
        self.assertEqual(first.channel.sent, ["hey"])
        self.assertEqual(second.channel.sent, ["hey"])

    async def test_full_mode_ignores_unaddressed_channel_messages(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        self.bot.set_full_mode_for(allowed_user, True)
        message = self._full_mode_message("princess treatment", 1, author_id=allowed_user)

        with patch("bot.ask", AsyncMock(return_value="as you wish")) as mocked_ask:
            await self.bot.on_message(message)

        mocked_ask.assert_not_awaited()
        self.assertEqual(message.channel.sent, [])

    async def test_full_mode_has_no_bot_attachment_cap(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        self.bot.set_full_mode_for(allowed_user, True)
        attachments = [
            SimpleNamespace(content_type="image/png", url=f"https://cdn.discordapp.com/{index}.png")
            for index in range(32)
        ]
        message = self._full_mode_message(
            "<@99> inspect these",
            1,
            author_id=allowed_user,
            mentions=[self.bot.user],
            attachments=attachments,
        )

        with patch("bot.ask", AsyncMock(return_value="done")) as mocked_ask:
            await self.bot.on_message(message)

        self.assertEqual(
            mocked_ask.await_args.kwargs["image_urls"],
            [attachment.url for attachment in attachments],
        )

    async def test_reply_attaches_a_generated_image(self) -> None:
        channel = FakeChannel()
        message = make_message("<@99> image", 1, channel, mentions=[self.bot.user])

        await self.bot._reply(
            message,
            AssistantReply("done", image_bytes=(b"image-bytes",)),
            unlimited=True,
        )

        self.assertEqual(channel.sent, ["done", ""])
        image_file = channel.send_kwargs[1]["file"]
        self.assertEqual(image_file.filename, "owaua-1.png")

    async def test_full_mode_skips_command_cooldown(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        self.bot.set_full_mode_for(allowed_user, True)
        first = self._full_mode_message("!help", 1, author_id=allowed_user)
        second = self._full_mode_message("!help", 2, author_id=allowed_user)
        await self.bot.on_message(first)
        await self.bot.on_message(second)
        self.assertEqual(first.channel.sent, [HELP_TEXT])
        self.assertEqual(second.channel.sent, [HELP_TEXT])

    async def test_full_mode_sends_uncapped_replies_and_images(self) -> None:
        allowed_user = next(iter(FULL_MODE_ALLOWED_USER_IDS))
        self.bot.set_full_mode_for(allowed_user, True)
        long = "a" * 8000
        attachments = [
            SimpleNamespace(
                content_type="image/png", url="https://cdn.discordapp.com/a.png"
            ),
            SimpleNamespace(
                content_type="image/jpeg", url="https://cdn.discordapp.com/b.jpg"
            ),
        ]
        message = make_message(
            "<@99> look",
            1,
            FakeChannel(FULL_MODE_CHANNEL_ID),
            author_id=allowed_user,
            guild_id=FULL_MODE_GUILD_ID,
            mentions=[self.bot.user],
            attachments=attachments,
        )
        with patch("bot.ask", AsyncMock(return_value=long)) as mocked_ask:
            await self.bot.on_message(message)

        self.assertEqual(
            mocked_ask.await_args.kwargs["image_urls"],
            [
                "https://cdn.discordapp.com/a.png",
                "https://cdn.discordapp.com/b.jpg",
            ],
        )
        self.assertGreater(len(message.channel.sent), 1)
        self.assertEqual("".join(message.channel.sent), long)

    async def test_flirty_persona_is_allowed_in_all_channels(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!persona flirty", 1, channel))

        self.assertEqual(self.store.get_setting("persona:user:33", "rudeish"), "flirty")
        self.assertEqual(channel.sent, ["persona: flirty"])

    async def test_flirty_persona_reports_a_missing_gemini_key(self) -> None:
        channel = FakeChannel(nsfw=True)
        with patch("ask.PERPLEXITY_API_KEY", ""):
            await self.bot.on_message(make_message("!persona flirty", 1, channel))

        self.assertEqual(channel.sent, ["perplexity is not configured"])
        self.assertEqual(self.store.get_setting("persona:user:33", "rudeish"), "rudeish")

    async def test_persona_command_still_matches_when_the_bot_is_pinged(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("<@99> !persona nerdish", 1, channel))

        self.assertEqual(channel.sent, ["persona: nerdish"])
        self.assertEqual(self.store.get_setting("persona:user:33", "rudeish"), "nerdish")

    async def test_host_default_persona_defaults_to_deepseek(self) -> None:
        channel = FakeChannel()
        with patch("bot.host_model_error", return_value=None):
            await self.bot.on_message(make_message("!persona host default", 1, channel))

        self.assertEqual(channel.sent, ["persona: host default (gpt)"])
        self.assertEqual(self.store.get_setting("persona:user:33", "rudeish"), "host-default-gpt")

    async def test_host_default_persona_selects_deepseek_and_mistral(self) -> None:
        channel = FakeChannel()
        with patch("bot.host_model_error", return_value=None):
            await self.bot.on_message(
                make_message("!persona host default deepseek", 1, channel)
            )
            self.bot.command_used.clear()
            await self.bot.on_message(
                make_message("!persona host default mistral", 2, channel)
            )
            self.bot.command_used.clear()
            await self.bot.on_message(make_message("!persona", 3, channel))

        self.assertEqual(
            channel.sent,
            [
                "persona: host default (deepseek)",
                "persona: host default (mistral)",
                "persona: host default (mistral)",
            ],
        )
        self.assertEqual(self.store.get_setting("persona:user:33", "rudeish"), "host-default-mistral")

    async def test_host_default_rejects_an_unknown_model(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(
            make_message("!persona host default claude", 1, channel)
        )

        self.assertIn("!persona host default gpt", channel.sent[0])
        self.assertEqual(self.store.get_setting("persona:user:33", "rudeish"), "rudeish")

    async def test_host_default_reports_a_missing_provider_key(self) -> None:
        channel = FakeChannel()
        with patch("ask.PERPLEXITY_API_KEY", ""):
            await self.bot.on_message(
                make_message("!persona host default deepseek", 1, channel)
            )

        self.assertEqual(channel.sent, ["deepseek is not configured"])
        self.assertEqual(self.store.get_setting("persona:user:33", "rudeish"), "rudeish")

    async def test_active_command_is_gone(self) -> None:
        channel = FakeChannel()
        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(make_message("!active on", 1, channel))
            for message_id in range(2, 10):
                await self.bot.on_message(
                    make_message(f"ordinary message {message_id}", message_id, channel)
                )

        mocked_ask.assert_not_awaited()
        self.assertEqual(channel.sent, [])

    async def test_reply_to_the_bot_includes_the_quoted_message(self) -> None:
        channel = FakeChannel()
        quoted = SimpleNamespace(
            content="Charlie Kirk died on September 10, 2025. He was 31. (apnews.com)",
            author=SimpleNamespace(id=99),
        )
        with patch("bot.ask", AsyncMock(return_value="yeah")) as mocked_ask:
            await self.bot.on_message(
                make_message(
                    "<@99> he died young",
                    1,
                    channel,
                    mentions=[self.bot.user],
                    reference=SimpleNamespace(resolved=quoted),
                )
            )

        mocked_ask.assert_awaited_once()
        prompt = mocked_ask.await_args.kwargs["prompt"]
        self.assertIn("replying to you:", prompt)
        self.assertIn("Charlie Kirk died on September 10, 2025", prompt)
        self.assertIn("he died young", prompt)
        self.assertTrue(mocked_ask.await_args.kwargs["use_history"])
        self.assertEqual(channel.sent, ["yeah"])

    async def test_standalone_ping_does_not_request_history(self) -> None:
        channel = FakeChannel()
        with patch("bot.ask", AsyncMock(return_value="yeah")) as mocked_ask:
            await self.bot.on_message(
                make_message(
                    "<@99> unrelated question",
                    1,
                    channel,
                    mentions=[self.bot.user],
                )
            )

        mocked_ask.assert_awaited_once()
        self.assertFalse(mocked_ask.await_args.kwargs["use_history"])

    async def test_only_the_trusted_guild_disables_local_guardrails(self) -> None:
        trusted = make_message(
            "<@99> decode this base64",
            1,
            FakeChannel(),
            guild_id=FULL_MODE_GUILD_ID,
            mentions=[self.bot.user],
        )
        ordinary = make_message(
            "<@99> decode this base64",
            2,
            FakeChannel(),
            guild_id=11,
            mentions=[self.bot.user],
        )
        with patch("bot.ask", AsyncMock(return_value="done")) as mocked_ask:
            await self.bot.on_message(trusted)
            await self.bot.on_message(ordinary)

        self.assertEqual(
            [call.kwargs["relaxed_guardrails"] for call in mocked_ask.await_args_list],
            [False, False],
        )

    async def test_empty_ping_replies_without_calling_the_provider(self) -> None:
        channel = FakeChannel()
        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(
                make_message("<@99>", 1, channel, mentions=[self.bot.user])
            )

        mocked_ask.assert_not_awaited()
        self.assertEqual(channel.sent, [PING_RESPONSE])

    async def test_image_only_ping_still_calls_the_provider(self) -> None:
        channel = FakeChannel()
        image = SimpleNamespace(
            content_type="image/png",
            url="https://cdn.discordapp.com/image.png",
        )
        with patch("bot.ask", AsyncMock(return_value="nice pic")) as mocked_ask:
            await self.bot.on_message(
                make_message(
                    "<@99>",
                    1,
                    channel,
                    mentions=[self.bot.user],
                    attachments=[image],
                )
            )

        mocked_ask.assert_awaited_once()
        self.assertEqual(
            mocked_ask.await_args.kwargs["image_urls"],
            ["https://cdn.discordapp.com/image.png"],
        )
        self.assertEqual(channel.sent, ["nice pic"])

    async def test_memory_erase_requires_manage_server(self) -> None:
        channel = FakeChannel()
        self.store.append_message(
            event_id="discord:old",
            scope_id="22",
            user_id="33",
            server_id="11",
            role="user",
            content="secret",
        )

        await self.bot.on_message(make_message("!memory erase", 1, channel, manage_guild=False))
        self.assertEqual(
            channel.sent,
            ["you need the Manage Server permission to erase server memory"],
        )
        self.assertEqual(
            [item["content"] for item in self.store.recent_messages("22", "33", limit=10)],
            ["secret"],
        )

        self.bot.command_used.clear()
        await self.bot.on_message(
            make_message("!memory erase", 2, channel, manage_guild=True)
        )
        self.assertEqual(
            channel.sent[-1], "server memory fully erased for every user and channel"
        )
        self.assertEqual(self.store.recent_messages("22", "33", limit=10), [])

    async def test_reset_all_requires_manage_server_and_resets_server_state(self) -> None:
        channel = FakeChannel()
        self.store.append_message(
            event_id="discord:reset-old",
            scope_id="22",
            user_id="33",
            server_id="11",
            role="user",
            content="secret",
        )
        self.store.set_setting("response_language:guild:11", "Hungarian")
        self.bot.response_languages["guild:11"] = "Hungarian"
        self.bot.music_tracks[11] = {"title": "song"}

        await self.bot.on_message(
            make_message("!reset all", 1, channel, manage_guild=False)
        )
        self.assertEqual(
            channel.sent,
            ["you need the Manage Server permission to reset this server"],
        )
        self.assertEqual(
            [item["content"] for item in self.store.recent_messages("22", "33", limit=10)],
            ["secret"],
        )

        self.bot.command_used.clear()
        with patch("bot.stop_music", AsyncMock()) as stop:
            await self.bot.on_message(make_message("!reset all", 2, channel))

        self.assertEqual(
            channel.sent[-1], "everything for this bot has been reset in this server"
        )
        self.assertEqual(self.store.recent_messages("22", "33", limit=10), [])
        self.assertEqual(self.store.get_setting("response_language:guild:11", ""), "")
        self.assertEqual(self.bot.response_languages, {})
        self.assertNotIn(11, self.bot.music_tracks)
        stop.assert_awaited_once()

    async def test_reset_all_does_not_erase_another_server(self) -> None:
        channel = FakeChannel()
        self.store.append_message(
            event_id="discord:reset-other",
            scope_id="99",
            user_id="33",
            server_id="99",
            role="user",
            content="keep me",
        )
        await self.bot.on_message(make_message("!reset all", 1, channel))
        self.assertEqual(
            [item["content"] for item in self.store.recent_messages("99", "33", limit=10)],
            ["keep me"],
        )

    async def test_music_command_is_server_only(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(
            make_message("!music never gonna give you up", 1, channel, guild_id=None)
        )

        self.assertEqual(channel.sent, ["!music only works in a server voice channel"])

    async def test_music_help_lists_restart(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!music help", 1, channel))

        self.assertEqual(len(channel.sent), 1)
        self.assertIn("!music restart", channel.sent[0])
        self.assertIn("!music <YouTube video or Twitter/X post URL>", channel.sent[0])

    async def test_music_restart_needs_a_queued_song(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!music restart", 1, channel))

        self.assertEqual(
            channel.sent, ["choose media first with `!music <YouTube video or Twitter/X post URL>`"]
        )

    async def test_music_restart_replays_the_queued_track_from_the_start(self) -> None:
        channel = FakeChannel()
        message = make_message("!music restart", 1, channel)
        voice = SimpleNamespace(
            is_playing=lambda: True,
            is_paused=lambda: False,
            stop=Mock(),
            channel=SimpleNamespace(id=7),
        )
        message.guild.voice_client = voice
        message.author.voice = SimpleNamespace(channel=SimpleNamespace(id=7, guild=message.guild))
        self.bot.music_tracks[11] = {
            "title": "Creep",
            "query": "radiohead creep",
            "url": "old",
        }
        refreshed = {
            "title": "Creep",
            "query": "radiohead creep",
            "url": "new",
        }

        with (
            patch("music.resolve_music", AsyncMock(return_value=refreshed)),
            patch("music.download_audio", AsyncMock(return_value=b"OggSfake")),
            patch("music.play_track") as play,
        ):
            await self.bot.on_message(message)

        voice.stop.assert_called_once()
        play.assert_called_once()
        self.assertEqual(self.bot.music_tracks[11]["url"], "new")
        self.assertEqual(channel.sent, ["restarted: Creep"])

    async def test_language_command_applies_to_every_user_and_channel_in_the_server(
        self,
    ) -> None:
        first = FakeChannel(22)
        second = FakeChannel(44)

        await self.bot.on_message(
            make_message("!language hebrew", 1, first, author_id=33)
        )
        self.bot.response_languages.clear()
        await self.bot.on_message(
            make_message("!language", 2, second, author_id=44)
        )

        self.assertEqual(
            first.sent,
            [
                "language set to hebrew; I’ll reply in it in this server from now on",
            ],
        )
        self.assertEqual(second.sent, ["language: hebrew"])

    async def test_language_command_does_not_leak_across_servers(self) -> None:
        home = FakeChannel(22)
        other = FakeChannel(44)

        home_message = make_message("!language hebrew", 1, home, guild_id=11)
        other_message = make_message("!language", 2, other, guild_id=99)
        await self.bot.on_message(home_message)
        self.bot.command_used.clear()
        await self.bot.on_message(other_message)

        self.assertEqual(other.sent, ["language: English"])
        self.assertGreaterEqual(home_message.guild.me.edit.await_count, 1)
        other_message.guild.me.edit.assert_not_called()

    async def test_language_command_still_matches_when_the_bot_is_pinged(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(
            make_message("<@99> !language hebrew", 1, channel)
        )
        self.bot.command_used.clear()
        await self.bot.on_message(
            make_message("!language hungarian <@!99>", 2, channel)
        )

        self.assertEqual(
            channel.sent,
            [
                "language set to hebrew; I’ll reply in it in this server from now on",
                "language set to hungarian; I’ll reply in it in this server from now on",
            ],
        )

    async def test_language_command_changes_only_that_servers_profile_picture(
        self,
    ) -> None:
        home = FakeChannel(22)
        other = FakeChannel(44)
        home_message = make_message("!language hungarian", 1, home, guild_id=11)
        other_message = make_message("!language italian", 2, other, guild_id=99)

        await self.bot.on_message(home_message)
        self.bot.command_used.clear()
        await self.bot.on_message(other_message)

        home_fields = profile_edit_fields(home_message.guild.me)
        other_fields = profile_edit_fields(other_message.guild.me)
        self.assertIsInstance(home_fields.get("avatar"), bytes)
        self.assertIsInstance(home_fields.get("banner"), bytes)
        self.assertIsInstance(other_fields.get("avatar"), bytes)
        self.assertIsInstance(other_fields.get("banner"), bytes)
        self.assertNotEqual(home_fields["avatar"], other_fields["avatar"])
        self.assertNotEqual(home_fields["banner"], other_fields["banner"])

    async def test_language_with_a_picture_but_no_banner_clears_only_the_banner(
        self,
    ) -> None:
        channel = FakeChannel()
        message = make_message("!language greek", 1, channel)

        await self.bot.on_message(message)

        fields = profile_edit_fields(message.guild.me)
        self.assertIsInstance(fields.get("avatar"), bytes)
        self.assertIsNone(fields.get("banner"))

    async def test_language_without_a_themed_picture_restores_that_servers_original(
        self,
    ) -> None:
        channel = FakeChannel()
        message = make_message("!language hebrew", 1, channel)

        await self.bot.on_message(message)

        self.assertEqual(
            profile_edit_fields(message.guild.me), {"avatar": None, "banner": None}
        )

    async def test_language_still_sets_when_banner_upload_fails(self) -> None:
        channel = FakeChannel()
        message = make_message("!language hungarian", 1, channel)

        async def flaky_edit(**kwargs: object) -> None:
            if "banner" in kwargs:
                raise RuntimeError("banner rejected")

        message.guild.me.edit = AsyncMock(side_effect=flaky_edit)

        await self.bot.on_message(message)

        self.assertEqual(
            channel.sent,
            [
                "language set to hungarian; I’ll reply in it in this server from now on"
            ],
        )
        fields = profile_edit_fields(message.guild.me)
        self.assertIsInstance(fields.get("avatar"), bytes)
        self.assertIn("banner", fields)

    async def test_language_still_sets_when_profile_updates_fail(self) -> None:
        channel = FakeChannel()
        message = make_message("!language hungarian", 1, channel)
        message.guild.me.edit = AsyncMock(side_effect=RuntimeError("discord down"))

        await self.bot.on_message(message)

        self.assertEqual(
            channel.sent,
            [
                "language set to hungarian; I’ll reply in it in this server from now on"
            ],
        )

    async def test_broken_profile_image_is_skipped_instead_of_clearing(self) -> None:
        channel = FakeChannel()
        message = make_message("!language hungarian", 1, channel)
        broken = Path(self.temporary_directory.name) / "hungarian.png"
        broken.write_bytes(b"not-an-image")

        with patch("bot.language_avatar_path", return_value=broken):
            await self.bot.on_message(message)

        fields = profile_edit_fields(message.guild.me)
        self.assertNotIn("avatar", fields)
        self.assertIn("banner", fields)

    async def test_invalid_language_command_does_not_change_the_profile_picture(
        self,
    ) -> None:
        channel = FakeChannel()
        message = make_message("!language hu", 1, channel)

        await self.bot.on_message(message)

        message.guild.me.edit.assert_not_called()
        self.assertIn("full language name", channel.sent[0])

    async def test_long_reply_is_sent_in_chunks(self) -> None:
        channel = FakeChannel()
        long = "a" * (DISCORD_MESSAGE_LIMIT + 40)

        with patch("bot.ask", AsyncMock(return_value=long)):
            await self.bot.on_message(
                make_message("go", 1, channel, mentions=[self.bot.user])
            )

        self.assertEqual(len(channel.sent), 2)
        self.assertEqual("".join(channel.sent), long)

    async def test_language_command_in_a_dm_does_not_touch_any_server_picture(
        self,
    ) -> None:
        channel = FakeChannel()
        message = make_message("!language hungarian", 1, channel, guild_id=None)

        await self.bot.on_message(message)

        self.assertIsNone(message.guild)
        self.assertEqual(
            channel.sent,
            ["language set to hungarian; I’ll reply in it from now on"],
        )

    async def test_language_reset_restores_english_and_clears_server_profile(
        self,
    ) -> None:
        channel = FakeChannel()
        set_language = make_message("!language hungarian", 1, channel)
        await self.bot.on_message(set_language)
        self.assertIsInstance(
            profile_edit_fields(set_language.guild.me).get("avatar"), bytes
        )
        self.assertIsInstance(
            profile_edit_fields(set_language.guild.me).get("banner"), bytes
        )

        self.bot.command_used.clear()
        reset = make_message("!language reset", 2, channel)
        reset.guild.me = set_language.guild.me
        await self.bot.on_message(reset)

        self.assertEqual(
            channel.sent[-1],
            "language reset to English; this server’s profile picture and banner "
            "are restored",
        )
        self.assertEqual(
            profile_edit_fields(set_language.guild.me),
            {"avatar": None, "banner": None},
        )
        self.assertEqual(self.bot.response_language(reset), "English")
        self.assertEqual(
            self.store.get_setting("response_language:guild:11"), "English"
        )

        self.bot.command_used.clear()
        self.bot.response_languages.clear()
        await self.bot.on_message(make_message("!language", 3, channel))
        self.assertEqual(channel.sent[-1], "language: English")

    async def test_language_reset_does_not_leak_across_servers(self) -> None:
        home = FakeChannel(22)
        other = FakeChannel(44)
        home_set = make_message("!language hungarian", 1, home, guild_id=11)
        other_set = make_message("!language italian", 2, other, guild_id=99)
        await self.bot.on_message(home_set)
        self.bot.command_used.clear()
        await self.bot.on_message(other_set)

        self.bot.command_used.clear()
        reset = make_message("!language RESET", 3, home, guild_id=11)
        reset.guild.me = home_set.guild.me
        await self.bot.on_message(reset)

        self.assertEqual(
            profile_edit_fields(home_set.guild.me),
            {"avatar": None, "banner": None},
        )
        other_fields = profile_edit_fields(other_set.guild.me)
        self.assertIsInstance(other_fields.get("avatar"), bytes)
        self.assertIsInstance(other_fields.get("banner"), bytes)
        self.assertEqual(self.bot.response_language(other_set), "italian")

    async def test_language_reset_in_a_dm_does_not_touch_any_server_picture(
        self,
    ) -> None:
        channel = FakeChannel()
        message = make_message("!language hungarian", 1, channel, guild_id=None)
        await self.bot.on_message(message)
        self.bot.command_used.clear()
        reset = make_message("!language reset", 2, channel, guild_id=None)

        await self.bot.on_message(reset)

        self.assertIsNone(reset.guild)
        self.assertEqual(
            channel.sent[-1],
            "language reset to English; I’ll reply in it from now on",
        )
        self.assertEqual(self.bot.response_language(reset), "English")

    async def test_same_command_has_a_25_second_cooldown(self) -> None:
        channel = FakeChannel()
        clock = {"now": 1000.0}

        with patch("bot.time.monotonic", side_effect=lambda: clock["now"]):
            await self.bot.on_message(make_message("!help", 1, channel))
            clock["now"] = 1010.0
            await self.bot.on_message(make_message("!help", 2, channel))
            clock["now"] = 1025.0
            await self.bot.on_message(make_message("!help", 3, channel))

        self.assertEqual(channel.sent[0], HELP_TEXT)
        self.assertEqual(channel.sent[1], "slow down try again in 15s")
        self.assertEqual(channel.sent[2], HELP_TEXT)

    async def test_command_cooldown_does_not_block_a_different_command(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!help", 1, channel))
        await self.bot.on_message(make_message("!persona", 2, channel))

        self.assertEqual(channel.sent, [HELP_TEXT, "persona: rudeish"])

    async def test_command_cooldown_is_per_user(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!help", 1, channel, author_id=33))
        await self.bot.on_message(make_message("!help", 2, channel, author_id=44))

        self.assertEqual(channel.sent, [HELP_TEXT, HELP_TEXT])

    async def test_command_cooldown_does_not_apply_to_chat_replies(self) -> None:
        channel = FakeChannel()

        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(make_message("!help", 1, channel))
            await self.bot.on_message(
                make_message("hello", 2, channel, mentions=[self.bot.user])
            )

        mocked_ask.assert_awaited_once()
        self.assertEqual(channel.sent, [HELP_TEXT, "hey"])

    async def test_exempt_user_can_repeat_the_same_command_immediately(self) -> None:
        channel = FakeChannel()
        exempt = 1172433512364769342

        await self.bot.on_message(make_message("!help", 1, channel, author_id=exempt))
        await self.bot.on_message(make_message("!help", 2, channel, author_id=exempt))

        self.assertEqual(channel.sent[0], OWNER_HELP_TEXT)
        self.assertIn("slow down", channel.sent[1])


if __name__ == "__main__":
    unittest.main()
