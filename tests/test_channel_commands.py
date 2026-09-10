from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot_client import MessageEventGuard, PersonaBot
from memory_store import MemoryStore


class FakeChannel:
    def __init__(self, channel_id: int = 22) -> None:
        self.id = channel_id
        self.sent: list[str] = []

    async def send(self, content: str, **_: object) -> None:
        self.sent.append(content)


def make_message(
    content: str,
    message_id: int,
    channel: FakeChannel,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=SimpleNamespace(id=33, bot=False),
        channel=channel,
        guild=SimpleNamespace(id=11),
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

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_help_command_lists_new_channel_controls(self) -> None:
        channel = FakeChannel()

        await self.bot.on_message(make_message("!help", 1, channel))

        self.assertEqual(len(channel.sent), 1)
        self.assertIn("!active on|off|status", channel.sent[0])
        self.assertIn("!gifs on|off|status", channel.sent[0])
        self.assertIn("!topic <topic> on", channel.sent[0])

    async def test_topic_drives_independent_tenth_message_gif(self) -> None:
        channel = FakeChannel()
        self.bot.search_gif = AsyncMock(
            return_value="https://static.klipy.com/abc/yuri.gif"
        )

        await self.bot.on_message(
            make_message("!topic yuri from ddlc on", 1, channel)
        )
        with patch("bot_client.settings.KLIPY_API_KEY", "test-key"):
            await self.bot.on_message(make_message("!gifs on", 2, channel))
        for message_id in range(3, 13):
            await self.bot.on_message(
                make_message(f"ordinary message {message_id}", message_id, channel)
            )

        self.bot.search_gif.assert_awaited_once_with("yuri from ddlc")
        self.assertEqual(
            channel.sent[-1], "https://static.klipy.com/abc/yuri.gif"
        )
        self.assertEqual(self.store.active_mode_status("22"), (False, 0))
        self.assertEqual(self.store.gif_mode_status("22"), (True, 0))


if __name__ == "__main__":
    unittest.main()
