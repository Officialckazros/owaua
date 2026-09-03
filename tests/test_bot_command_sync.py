"""Regression coverage for immediate guild slash-command deployment."""

from __future__ import annotations

import os
import types
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from owaua import bot


class GuildCommandSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_guild_sync_copies_the_global_catalog_before_syncing(self) -> None:
        tree = types.SimpleNamespace(
            copy_global_to=mock.Mock(),
            sync=mock.AsyncMock(return_value=["model"]),
        )
        with mock.patch.object(bot, "_tree", tree):
            synced = await bot._guild_sync(1535083112709496903)

        self.assertEqual(synced, ["model"])
        guild = tree.copy_global_to.call_args.kwargs["guild"]
        self.assertEqual(guild.id, 1535083112709496903)
        tree.sync.assert_awaited_once_with(guild=guild)


if __name__ == "__main__":
    unittest.main()
