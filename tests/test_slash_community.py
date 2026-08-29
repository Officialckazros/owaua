from __future__ import annotations

import unittest

import discord

from owaua import slash


class CommunitySlashCatalogTests(unittest.TestCase):
    def test_community_commands_are_registered_under_discord_limit(self) -> None:
        client = discord.Client(intents=discord.Intents.none())
        tree = slash.setup(client, lambda *_args: None)
        commands = {command.name: command for command in tree.get_commands()}

        self.assertLessEqual(len(commands), 100)
        self.assertTrue(
            {
                "afk",
                "remind",
                "highlight",
                "tag",
                "economy",
                "announce",
                "giveaway",
                "ticket",
                "form",
                "ranks",
                "fun",
            }.issubset(commands)
        )
        self.assertEqual(
            {command.name for command in commands["economy"].commands},
            {"wallet", "pay", "pack", "cards", "fuse", "deck", "battle"},
        )
        self.assertTrue(
            {"coinflip", "dice", "poll", "pokemon", "github", "distance"}
            .issubset(command.name for command in commands["fun"].commands)
        )
