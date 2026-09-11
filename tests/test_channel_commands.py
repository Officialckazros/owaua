from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import OWNER_NOTE_TEXT
from bot_client import MessageEventGuard, PersonaBot
from memory_store import MemoryStore


class FakeChannel:
    def __init__(self, channel_id: int = 22, *, nsfw: bool = False) -> None:
        self.id = channel_id
        self.nsfw = nsfw
        self.sent: list[str] = []

    async def send(self, content: str, **_: object) -> None:
        self.sent.append(content)


def make_message(
    content: str,
    message_id: int,
    channel: FakeChannel,
    *,
    author_id: int = 33,
    guild_id: int | None = 11,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=author_id, bot=False),
        channel=channel,
        guild=(SimpleNamespace(id=guild_id) if guild_id is not None else None),
        mentions=[],
        attachments=[],
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
        self.bot.admit_request = lambda *_args, **_kwargs: (True, 0)
        self.bot.response_languages = {}
        self.bot.selected_model = "gpt"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_help_command_lists_new_channel_controls(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!help", 1, channel))

        self.assertEqual(len(channel.sent), 1)
        self.assertIn("!active on|off|status", channel.sent[0])
        self.assertIn("!topic <topic> on|off", channel.sent[0])
        self.assertNotIn("!gifs", channel.sent[0])
        self.assertNotIn("!topic off", channel.sent[0])
        self.assertIn("!owner's note", channel.sent[0])
        self.assertIn("this server's reply language", channel.sent[0])

    async def test_owners_note_command_sends_the_owner_message(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!owner's note", 1, channel))

        self.assertEqual(channel.sent, [OWNER_NOTE_TEXT])

    async def test_owners_note_command_accepts_curly_apostrophes(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!owner’s note", 1, channel))

        self.assertEqual(len(channel.sent), 1)
        self.assertIn("ckazros@owaua.com", channel.sent[0])

    async def test_detected_ai_image_is_removed_before_commands_are_handled(self) -> None:
        channel = FakeChannel()
        self.bot.remove_ai_generated_images = AsyncMock(return_value=True)

        await self.bot.on_message(make_message("!help", 1, channel))

        self.bot.remove_ai_generated_images.assert_awaited_once()
        self.assertEqual(channel.sent, [])

    async def test_topic_drives_independent_tenth_message_gif(self) -> None:
        channel = FakeChannel()
        self.bot.search_gif = AsyncMock(
            return_value="https://static.klipy.com/abc/yuri.gif"
        )

        await self.bot.on_message(
            make_message("!topic yuri from ddlc on", 1, channel)
        )
        for message_id in range(2, 12):
            await self.bot.on_message(
                make_message(f"ordinary message {message_id}", message_id, channel)
            )

        self.bot.search_gif.assert_awaited_once_with("yuri from ddlc")
        self.assertEqual(
            channel.sent[-1], "https://static.klipy.com/abc/yuri.gif"
        )
        self.assertEqual(self.store.active_mode_status("22"), (False, 0))
        self.assertEqual(self.store.gif_message_count("22"), 0)

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

        await self.bot.on_message(
            make_message("!language hebrew", 1, home, guild_id=11)
        )
        await self.bot.on_message(
            make_message("!language", 2, other, guild_id=99)
        )

        self.assertEqual(other.sent, ["language: English"])

    async def test_language_command_still_matches_when_the_bot_is_pinged(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(
            make_message("<@99> !language hebrew", 1, channel)
        )
        await self.bot.on_message(
            make_message("!language hungarian <@!99>", 2, channel)
        )

        self.assertEqual(channel.sent, [
            "language set to hebrew; I’ll reply in it in this server from now on",
            "language set to hungarian; I’ll reply in it in this server from now on",
        ])

    async def test_explicit_persona_is_rejected_outside_age_restricted_channels(
        self,
    ) -> None:
        channel = FakeChannel()

        with patch("bot_client.settings.MISTRAL_API_KEY", "mistral-test"):
            await self.bot.on_message(make_message("!persona explicit", 1, channel))

        self.assertEqual(channel.sent, ["explicit only works in age-restricted channels"])
        self.assertEqual(self.bot.selected_model, "gpt")

    async def test_explicit_persona_is_allowed_in_age_restricted_channels(self) -> None:
        channel = FakeChannel(nsfw=True)

        with patch("bot_client.settings.MISTRAL_API_KEY", "mistral-test"):
            await self.bot.on_message(make_message("!persona explicit", 1, channel))

        self.assertEqual(self.bot.selected_model, "mistral")
        self.assertIn("explicit", channel.sent[0])


if __name__ == "__main__":
    unittest.main()
