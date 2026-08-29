"""Blocked users retain the privacy export/erasure escape hatch."""

from __future__ import annotations

import types
import typing
import unittest
from unittest import mock

import discord

from owaua import bot, config, slash


class BlockedPrivacyAccessTest(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_privacy_export_reaches_handler_after_block(self) -> None:
        message = types.SimpleNamespace(guild=None)
        handler = mock.AsyncMock()
        with (
            mock.patch.object(config, "is_blocked", return_value=True),
            mock.patch.object(bot.tos, "has_accepted", return_value=True),
            mock.patch.object(bot, "_cmd_privacy", handler),
        ):
            await bot._handle_command(message, "privacy export", "dm:42", "42", prefix="!")
        handler.assert_awaited_once_with(message, "export", "dm:42", "42")

    async def test_slash_privacy_export_is_reachable_after_block(self) -> None:
        client = discord.Client(intents=discord.Intents.none())
        tree = slash._BlockingTree(client)
        interaction = types.SimpleNamespace(
            user=types.SimpleNamespace(id=42),
            command=types.SimpleNamespace(name="privacy"),
            guild_id=None,
            channel_id=None,
            data={},
            extras={},
            response=types.SimpleNamespace(
                is_done=lambda: False,
                send_message=mock.AsyncMock(),
            ),
        )
        try:
            with (
                mock.patch.object(config, "is_blocked", return_value=True),
                mock.patch.object(slash.tos, "has_accepted", return_value=True),
            ):
                allowed = await tree.interaction_check(typing.cast(typing.Any, interaction))
            self.assertTrue(allowed)
            interaction.response.send_message.assert_not_awaited()
        finally:
            await client.close()

    async def test_blocked_prefix_export_still_sends_private_data(self) -> None:
        author = types.SimpleNamespace(id=42, send=mock.AsyncMock())
        message = types.SimpleNamespace(author=author, guild=None, channel=types.SimpleNamespace())
        with (
            mock.patch.object(config, "is_blocked", return_value=True),
            mock.patch.object(bot.db, "privacy_export", return_value={"user_id": "42"}),
        ):
            await bot._cmd_privacy(message, "export", "dm:42", "42")
        author.send.assert_awaited_once()
        sent_file = author.send.await_args.kwargs["file"]
        self.assertIsInstance(sent_file, discord.File)

    async def test_blocked_user_cannot_opt_in_new_storage(self) -> None:
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(id=42),
            guild=None,
            channel=types.SimpleNamespace(send=mock.AsyncMock()),
        )
        with (
            mock.patch.object(config, "is_blocked", return_value=True),
            mock.patch.object(bot, "_send_private", new=mock.AsyncMock()) as send_private,
        ):
            await bot._cmd_privacy(message, "opt-in", "dm:42", "42")
        send_private.assert_awaited_once()
        self.assertIn(
            "cannot opt in", typing.cast(typing.Any, send_private.await_args).args[1].description
        )


if __name__ == "__main__":
    unittest.main()
