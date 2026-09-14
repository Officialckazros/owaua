from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import (
    DISCORD_MESSAGE_LIMIT,
    HELP_TEXT,
    OWNER_NOTE_TEXT,
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
    manage_guild: bool = False,
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
        self.bot.rate_windows = defaultdict(deque)
        self.bot.command_used = {}
        self.bot.conversation_locks = defaultdict(asyncio.Lock)
        self.bot.music_tracks = {}
        self.bot.response_languages = {}

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_help_command_lists_the_remaining_commands(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!help", 1, channel))

        self.assertEqual(channel.sent, [HELP_TEXT])
        self.assertEqual(channel.send_kwargs[0].get("suppress_embeds"), True)
        self.assertNotIn("!active", channel.sent[0])
        self.assertIn("!persona rudeish|nerdish|explicit|host default gpt/deepseek/mistral", channel.sent[0])
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

    async def test_owners_note_command_sends_the_owner_message(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!owner's note", 1, channel))

        self.assertEqual(channel.sent, [OWNER_NOTE_TEXT])

    async def test_owners_note_command_accepts_curly_apostrophes(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!owner’s note", 1, channel))

        self.assertEqual(len(channel.sent), 1)
        self.assertIn("ckazros@owaua.com", channel.sent[0])

    async def test_explicit_persona_is_rejected_outside_age_restricted_channels(
        self,
    ) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!persona explicit", 1, channel))

        self.assertEqual(channel.sent, ["explicit only works in age-restricted channels"])
        self.assertEqual(self.bot.selected_persona, "rudeish")

    async def test_explicit_persona_is_allowed_in_age_restricted_channels(self) -> None:
        channel = FakeChannel(nsfw=True)

        await self.bot.on_message(make_message("!persona explicit", 1, channel))

        self.assertEqual(self.bot.selected_persona, "explicit")
        self.assertEqual(channel.sent, ["persona: explicit"])
        self.assertEqual(self.store.get_setting("selected_persona"), "explicit")

    async def test_persona_command_still_matches_when_the_bot_is_pinged(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("<@99> !persona nerdish", 1, channel))

        self.assertEqual(channel.sent, ["persona: nerdish"])
        self.assertEqual(self.bot.selected_persona, "nerdish")

    async def test_host_default_persona_defaults_to_deepseek(self) -> None:
        channel = FakeChannel()
        with patch("bot.host_model_error", return_value=None):
            await self.bot.on_message(make_message("!persona host default", 1, channel))

        self.assertEqual(channel.sent, ["persona: host default (deepseek)"])
        self.assertEqual(self.bot.selected_persona, "host-default-deepseek")
        self.assertEqual(
            self.store.get_setting("selected_persona"), "host-default-deepseek"
        )

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
        self.assertEqual(self.bot.selected_persona, "host-default-mistral")

    async def test_host_default_rejects_an_unknown_model(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(
            make_message("!persona host default claude", 1, channel)
        )

        self.assertIn("!persona host default gpt", channel.sent[0])
        self.assertEqual(self.bot.selected_persona, "rudeish")

    async def test_host_default_reports_a_missing_provider_key(self) -> None:
        channel = FakeChannel()
        with patch("ask.DEEPSEEK_API_KEY", ""):
            await self.bot.on_message(
                make_message("!persona host default deepseek", 1, channel)
            )

        self.assertEqual(channel.sent, ["deepseek is not configured"])
        self.assertEqual(self.bot.selected_persona, "rudeish")

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

    async def test_empty_ping_does_not_call_the_provider(self) -> None:
        channel = FakeChannel()
        with patch("bot.ask", AsyncMock(return_value="hey")) as mocked_ask:
            await self.bot.on_message(
                make_message("<@99>", 1, channel, mentions=[self.bot.user])
            )

        mocked_ask.assert_not_awaited()
        self.assertEqual(channel.sent, [])

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

        await self.bot.on_message(make_message("!memory erase", 1, channel))
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
        self.assertIn("!music <song or URL>", channel.sent[0])

    async def test_music_restart_needs_a_queued_song(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!music restart", 1, channel))

        self.assertEqual(
            channel.sent, ["choose a song first with `!music <song or URL>`"]
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

        self.assertEqual(channel.sent, [HELP_TEXT, HELP_TEXT])


if __name__ == "__main__":
    unittest.main()
