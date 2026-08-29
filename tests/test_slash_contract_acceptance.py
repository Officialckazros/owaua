"""Registration and forwarding contracts for the Discord slash adapter."""

from __future__ import annotations

import os
import typing
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import discord
from discord import app_commands

from owaua import config, slash


class SlashRegistrationAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = slash.setup(self.client, lambda *_args: None)
        self.commands = {command.name: command for command in self.tree.get_commands()}

    async def asyncTearDown(self) -> None:
        await self.client.close()

    def test_command_surface_fits_discord_and_excludes_removed_rce_and_mp3(self) -> None:
        self.assertLessEqual(len(self.commands), 100)
        self.assertNotIn("eval", self.commands)
        self.assertNotIn("exec", self.commands)
        self.assertNotIn("mp3", self.commands)
        self.assertIn("privacy", self.commands)
        self.assertIn("act", self.commands)
        self.assertIn("language", self.commands)
        self.assertIn("profile", self.commands)
        self.assertNotIn("whoami", self.commands)
        for alias in {
            "lang",
            "models",
            "google",
            "infosec",
            "sec",
            "song",
            "assist",
            "level",
            "purge",
            "quotes",
            "relationship",
        }:
            self.assertNotIn(alias, self.commands)
        self.assertIn("mode", self.commands)
        self.assertEqual(len(self.commands), 89)

    def test_general_mode_picker_does_not_advertise_adult_personas(self) -> None:
        command = self.commands["mode"]
        choice: typing.Any = typing.cast(
            typing.Any,
            {
                typing.cast(typing.Any, parameter).name: parameter
                for parameter in typing.cast(
                    typing.Iterable[typing.Any], typing.cast(typing.Any, command).parameters
                )
            }["choice"],
        )
        values: typing.Any = typing.cast(
            typing.Any,
            [
                typing.cast(typing.Any, item).value
                for item in typing.cast(
                    typing.Iterable[typing.Any], typing.cast(typing.Any, choice).choices
                )
            ],
        )
        self.assertEqual(values, ["ai-fast", "ai-balanced", "ai-reasoning", "status"])

    async def test_general_help_hides_age_restricted_features_in_sfw_contexts(self) -> None:
        command = self.commands["help"]
        interaction = SimpleNamespace(
            guild=SimpleNamespace(),
            channel=SimpleNamespace(is_nsfw=lambda: False, parent=None),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )

        await typing.cast(typing.Any, command).callback(typing.cast(typing.Any, interaction))

        embed = interaction.response.send_message.await_args.kwargs["embed"]
        text = embed.description.casefold()
        for forbidden in ("/nsfw", "rule34", "freaky", "horny"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    async def test_general_help_reveals_age_restricted_command_only_in_marked_channel(self) -> None:
        command = self.commands["help"]
        interaction = SimpleNamespace(
            guild=SimpleNamespace(),
            channel=SimpleNamespace(is_nsfw=lambda: True, parent=None),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )

        await typing.cast(typing.Any, command).callback(typing.cast(typing.Any, interaction))

        embed = interaction.response.send_message.await_args.kwargs["embed"]
        self.assertIn("/nsfw", embed.description)
        self.assertIn("age-restricted", embed.description)

    async def test_profile_shows_fetched_banner_and_display_avatar(self) -> None:
        command = self.commands["profile"]
        target = SimpleNamespace(
            id=123,
            name="example",
            global_name="Example User",
            display_name="Example User",
            bot=False,
            created_at=discord.utils.utcnow(),
            joined_at=None,
            display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
        )
        fetched = SimpleNamespace(
            banner=SimpleNamespace(url="https://cdn.example/banner.png"),
            accent_color=None,
        )
        interaction = SimpleNamespace(
            user=target,
            client=SimpleNamespace(fetch_user=mock.AsyncMock(return_value=fetched)),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )

        await typing.cast(typing.Any, command).callback(typing.cast(typing.Any, interaction))

        interaction.client.fetch_user.assert_awaited_once_with(123)
        embed = interaction.response.send_message.await_args.kwargs["embed"]
        self.assertEqual(embed.thumbnail.url, "https://cdn.example/avatar.png")
        self.assertEqual(embed.image.url, "https://cdn.example/banner.png")
        self.assertIn("https://cdn.example/banner.png", embed.description)

    def test_nsfw_command_is_discord_marked_guild_only_and_count_bounded(self) -> None:
        command = self.commands["nsfw"]
        parameters: typing.Any = typing.cast(
            typing.Any,
            {
                typing.cast(typing.Any, parameter).name: parameter
                for parameter in typing.cast(
                    typing.Iterable[typing.Any], typing.cast(typing.Any, command).parameters
                )
            },
        )
        self.assertTrue(command.nsfw)
        self.assertTrue(command.guild_only)
        self.assertEqual(typing.cast(typing.Any, parameters["amount"]).min_value, 1)
        self.assertEqual(typing.cast(typing.Any, parameters["amount"]).max_value, 10)

    async def test_nsfw_command_rechecks_the_live_channel_flag(self) -> None:
        command = self.commands["nsfw"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=991_234_567),
            channel=SimpleNamespace(is_nsfw=lambda: False, parent=None),
            response=SimpleNamespace(send_message=mock.AsyncMock()),
        )
        slash._last_uses.clear()
        with mock.patch.object(slash.rule34, "search", mock.AsyncMock()) as search:
            await typing.cast(typing.Any, command).callback(
                typing.cast(typing.Any, interaction), typing.cast(typing.Any, "kit_gameoverse"), 1
            )
        search.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()

    def test_dead_groq_llama_ids_remap_to_gpt_oss(self) -> None:
        self.assertEqual(
            config.canonical_model("llama-3.3-70b-versatile"),
            "openai/gpt-oss-120b",
        )
        self.assertEqual(
            config.canonical_model("llama-3.1-8b-instant"),
            "openai/gpt-oss-20b",
        )
        self.assertEqual(config.canonical_model("groq"), "openai/gpt-oss-120b")
        self.assertIn("openai/gpt-oss-120b", config.MODEL_FALLBACKS)
        self.assertNotIn("llama-3.3-70b-versatile", config.MODEL_FALLBACKS)

    def test_model_picker_lists_every_live_groq_chat_model(self) -> None:
        command = self.commands["model"]
        parameters: typing.Any = typing.cast(
            typing.Any,
            {
                typing.cast(typing.Any, parameter).name: parameter
                for parameter in typing.cast(
                    typing.Iterable[typing.Any], typing.cast(typing.Any, command).parameters
                )
            },
        )
        self.assertIn("choice", typing.cast(typing.Any, parameters))
        values: typing.Any = typing.cast(
            typing.Any,
            [
                typing.cast(typing.Any, choice).value
                for choice in typing.cast(
                    typing.Iterable[typing.Any],
                    typing.cast(typing.Any, parameters["choice"]).choices,
                )
            ],
        )
        names: typing.Any = typing.cast(
            typing.Any,
            [
                typing.cast(typing.Any, choice).name
                for choice in typing.cast(
                    typing.Iterable[typing.Any],
                    typing.cast(typing.Any, parameters["choice"]).choices,
                )
            ],
        )
        self.assertIn("deepseek", typing.cast(typing.Any, values))
        self.assertIn("big", typing.cast(typing.Any, values))
        for model_id, label in config.GROQ_CHAT_MODELS:
            self.assertIn(model_id, typing.cast(typing.Any, values))
            self.assertIn(label, typing.cast(typing.Any, names))
        self.assertNotIn("llama-3.3-70b-versatile", typing.cast(typing.Any, values))
        self.assertLessEqual(len(typing.cast(typing.Any, values)), 25)
        self.assertEqual(
            len(typing.cast(typing.Any, values)),
            len(typing.cast(typing.Any, set(typing.cast(typing.Any, values)))),
        )

    def test_upload_commands_register_real_attachment_options(self) -> None:
        expected = {
            "kb": {"attachment"},
            "import": {"attachment"},
            "describe": {"image"},
            "read": {"attachment"},
            "chat": {"attachment"},
            "ask": {"attachment"},
            "assistant": {"attachment"},
        }
        for command_name, attachment_names in expected.items():
            with self.subTest(command=command_name):
                parameters: typing.Any = typing.cast(
                    typing.Any,
                    {
                        typing.cast(typing.Any, parameter).name: parameter
                        for parameter in typing.cast(
                            typing.Any, typing.cast(typing.Any, self).commands[command_name]
                        ).parameters
                    },
                )
                for name in attachment_names:
                    self.assertIn(name, typing.cast(typing.Any, parameters))
                    self.assertIs(
                        typing.cast(typing.Any, typing.cast(typing.Any, parameters[name]).type),
                        discord.AppCommandOptionType.attachment,
                    )

    def test_ai_workflows_extend_existing_commands_without_exceeding_discord_limit(self) -> None:
        ask: typing.Any = typing.cast(
            typing.Any,
            {
                typing.cast(typing.Any, parameter).name: parameter
                for parameter in typing.cast(
                    typing.Iterable[typing.Any],
                    typing.cast(
                        typing.Any, typing.cast(typing.Any, self).commands["ask"]
                    ).parameters,
                )
            },
        )
        workflows: typing.Any = typing.cast(
            typing.Any,
            [
                typing.cast(typing.Any, choice).value
                for choice in typing.cast(
                    typing.Iterable[typing.Any], typing.cast(typing.Any, ask["workflow"]).choices
                )
            ],
        )
        self.assertEqual(workflows, [])
        self.assertIsNotNone(
            typing.cast(typing.Any, typing.cast(typing.Any, ask["workflow"]).autocomplete)
        )

        recap: typing.Any = typing.cast(
            typing.Any,
            {
                typing.cast(typing.Any, parameter).name: parameter
                for parameter in typing.cast(
                    typing.Iterable[typing.Any],
                    typing.cast(
                        typing.Any, typing.cast(typing.Any, self).commands["recap"]
                    ).parameters,
                )
            },
        )
        recap_modes: typing.Any = typing.cast(
            typing.Any,
            [
                typing.cast(typing.Any, choice).value
                for choice in typing.cast(
                    typing.Iterable[typing.Any], typing.cast(typing.Any, recap["mode"]).choices
                )
            ],
        )
        self.assertIn("action_items", typing.cast(typing.Any, recap_modes))
        self.assertIn("moderation_triage", typing.cast(typing.Any, recap_modes))

        menu_names = {
            command.name
            for command in self.tree.get_commands()
            if isinstance(command, app_commands.ContextMenu)
        }
        self.assertIn("AI: Summarize", menu_names)
        self.assertIn("AI: Fact-check", menu_names)


if __name__ == "__main__":
    unittest.main()
