"""Regression coverage for slash commands that fail after deferring."""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

import discord
from discord import app_commands

from owaua import slash


class SlashInteractionFailureTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = slash._BlockingTree(self.client)

    async def asyncTearDown(self) -> None:
        await self.client.close()

    def _deferred_interaction(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            command=None,
            command_failed=False,
            response=types.SimpleNamespace(
                is_done=lambda: True,
                type=discord.InteractionResponseType.deferred_channel_message,
            ),
            edit_original_response=mock.AsyncMock(),
            followup=types.SimpleNamespace(send=mock.AsyncMock()),
        )

    async def test_error_replaces_deferred_thinking_response(self) -> None:
        interaction = self._deferred_interaction()

        await self.tree.on_error(
            interaction,
            app_commands.CommandInvokeError(
                types.SimpleNamespace(name="chat"), RuntimeError("provider failed")
            ),
        )

        interaction.edit_original_response.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()

    async def test_timeout_replaces_deferred_thinking_response(self) -> None:
        interaction = self._deferred_interaction()
        interaction.command = types.SimpleNamespace(qualified_name="chat")

        async def never_returns(_tree: object, _interaction: object) -> None:
            await asyncio.Future()

        with (
            mock.patch.object(app_commands.CommandTree, "_call", new=never_returns),
            mock.patch.object(slash, "_COMMAND_TIMEOUT_SECONDS", 0.01),
        ):
            await self.tree._call(interaction)

        self.assertTrue(interaction.command_failed)
        interaction.edit_original_response.assert_awaited_once()

    async def test_error_before_response_sends_ephemeral_message(self) -> None:
        response = types.SimpleNamespace(is_done=lambda: False, send_message=mock.AsyncMock())
        interaction = types.SimpleNamespace(response=response)

        await self.tree._finish_failed_interaction(interaction, "try again")

        response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
