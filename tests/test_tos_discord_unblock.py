"""Regression tests for the owner-only Discord ToS unblock command."""

from __future__ import annotations

import tempfile
import types
import typing
import unittest
from pathlib import Path
from unittest import mock

import discord

from owaua import blocked, bot, config, db, tos


class TosDiscordUnblockTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.database = root / "state.db"
        self.patches = [
            mock.patch.object(config, "DB_PATH", str(self.database)),
            mock.patch.object(config, "OWNER_ID", "9001"),
            mock.patch.object(config, "TOS_ACCEPTANCE_SECRET", "a" * 64),
            mock.patch.object(blocked, "BLOCKED_FILE", root / "blocked_users.json"),
        ]
        db.close()
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        db.close()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tempdir.cleanup()

    @staticmethod
    def _message():
        channel = types.SimpleNamespace(
            send=mock.AsyncMock(return_value=types.SimpleNamespace(id=1))
        )
        author = types.SimpleNamespace(id=9001, send=mock.AsyncMock())
        return types.SimpleNamespace(channel=channel, author=author, guild=object())

    async def test_owner_can_unblock_tos_user_and_clear_side_effects(self) -> None:
        uid = "470617205667790868"
        blocked.block_user(uid, reason="tos: confirmed abuse", block_source="tos")
        db.user_flag_set(uid, "tos_emergency_block", "1")
        db.user_flag_set(uid, "tos_violation_strikes", "3")
        recipient = types.SimpleNamespace(send=mock.AsyncMock())
        message = self._message()

        with mock.patch.object(
            bot.client,
            "fetch_user",
            new=mock.AsyncMock(return_value=recipient),
        ):
            await bot._cmd_tos(
                message,
                f"break unblock {uid}",
                "guild:123",
                config.OWNER_ID,
            )

        self.assertFalse(blocked.is_dynamically_blocked(uid))
        self.assertEqual("", db.user_flag_get(uid, "tos_emergency_block"))
        self.assertEqual(0, db.user_flag_int(uid, "tos_violation_strikes"))
        recipient.send.assert_awaited_once()
        response = message.channel.send.await_args.kwargs["embed"].description
        self.assertIn(f"unblocked user `{uid}`", response)
        audit = (
            db.conn()
            .execute(
                "SELECT action,target_id,status FROM action_audit ORDER BY created DESC LIMIT 1"
            )
            .fetchone()
        )
        self.assertEqual(("tos_unblock", uid, "completed"), tuple(audit))

    async def test_non_owner_cannot_unblock_tos_user(self) -> None:
        uid = "470617205667790868"
        blocked.block_user(uid, reason="tos: confirmed abuse", block_source="tos")
        message = self._message()

        await bot._cmd_tos(message, f"break unblock {uid}", "guild:123", "42")

        self.assertTrue(blocked.is_dynamically_blocked(uid))
        response = message.channel.send.await_args.kwargs["embed"].description
        self.assertIn("only the bot owner", response)

    async def test_typed_accept_only_opens_web_flow(self) -> None:
        message = self._message()

        await bot._cmd_tos(message, "accept", "guild:123", "42")

        self.assertFalse(tos.has_accepted("42"))
        sent = message.channel.send.await_args.kwargs
        self.assertIsInstance(sent["view"], tos.AcceptanceView)
        self.assertIn("button below", sent["embed"].description)

    async def test_acceptance_buttons_are_bound_to_the_generating_user(self) -> None:
        owner_id = "42"
        view = tos.AcceptanceView(owner_id)
        buttons = [child for child in view.children if isinstance(child, discord.ui.Button)]
        self.assertEqual(2, len(buttons))
        self.assertEqual(
            {f"tos:read:{owner_id}", f"tos:check:{owner_id}"},
            {button.custom_id for button in buttons},
        )

        other_response = types.SimpleNamespace(send_message=mock.AsyncMock())
        other = types.SimpleNamespace(user=types.SimpleNamespace(id=43), response=other_response)
        self.assertFalse(await view.interaction_check(typing.cast(typing.Any, other)))
        other_response.send_message.assert_awaited_once()

        owner_response = types.SimpleNamespace(send_message=mock.AsyncMock())
        owner = types.SimpleNamespace(user=types.SimpleNamespace(id=42), response=owner_response)
        read_button = next(
            button
            for button in buttons
            if typing.cast(typing.Any, button.custom_id).startswith("tos:read:")
        )
        await read_button.callback(typing.cast(typing.Any, owner))
        owner_response.send_message.assert_awaited_once_with(
            f"[Open the Terms acceptance page]({view.acceptance_url})",
            ephemeral=True,
        )

        # The callback has its own guard as defense in depth if invoked
        # directly instead of through discord.py's View dispatcher.
        direct_other_response = types.SimpleNamespace(send_message=mock.AsyncMock())
        direct_other = types.SimpleNamespace(
            user=types.SimpleNamespace(id=43), response=direct_other_response
        )
        await read_button.callback(typing.cast(typing.Any, direct_other))
        direct_other_response.send_message.assert_awaited_once()

    async def test_tos_command_does_not_remove_manual_block(self) -> None:
        manual_uid = "470617205667790868"
        blocked.block_user(
            manual_uid,
            reason="manual operator block",
            block_source="manual",
        )
        # Keep one ToS entry present so the command reaches its target validation.
        blocked.block_user("123456789", reason="tos: other user", block_source="tos")
        message = self._message()

        await bot._cmd_tos(
            message,
            f"break unblock {manual_uid}",
            "guild:123",
            config.OWNER_ID,
        )

        self.assertTrue(blocked.is_dynamically_blocked(manual_uid))
        response = message.channel.send.await_args.kwargs["embed"].description
        self.assertIn("refusing to remove the non-ToS block", response)

    async def test_owner_receives_complete_global_list_as_text_file_when_large(self) -> None:
        message = self._message()
        for i in range(40):
            blocked.block_user(
                str(1_000_000_000 + i),
                reason="tos: confirmed abuse " + ("x" * 120),
                block_source="tos",
            )

        await bot._cmd_tos(message, "break list", "guild:123", config.OWNER_ID)

        message.author.send.assert_awaited_once()
        sent = message.author.send.await_args.kwargs
        self.assertEqual("owaua-tos-break-list.txt", sent["file"].filename)
        self.assertIn("complete global ToS-break review (40 blocked)", sent["embed"].description)


if __name__ == "__main__":
    unittest.main()
