from __future__ import annotations

import typing
import unittest
from types import SimpleNamespace
from unittest import mock

import discord

from owaua import config, dm


class OperatorDMGuardrailTests(unittest.IsolatedAsyncioTestCase):
    def test_cli_requires_explicit_enable_and_allowlist(self) -> None:
        with (
            mock.patch.object(config, "DM_CLI_ENABLED", False),
            mock.patch.object(config, "DM_CLI_ALLOW_USER_IDS", frozenset[str]()),
        ):
            self.assertIn("disabled", dm.cli_access_error("42") or "")

        with (
            mock.patch.object(config, "DM_CLI_ENABLED", True),
            mock.patch.object(config, "DM_CLI_ALLOW_USER_IDS", frozenset[str]()),
        ):
            self.assertIn("no recipients", dm.cli_access_error("42") or "")

    def test_cli_rejects_recipient_outside_allowlist(self) -> None:
        with (
            mock.patch.object(config, "DM_CLI_ENABLED", True),
            mock.patch.object(config, "DM_CLI_ALLOW_USER_IDS", frozenset({"42"})),
        ):
            self.assertIsNone(dm.cli_access_error("42"))
            self.assertIn("not in", dm.cli_access_error("43") or "")

    async def test_send_records_content_free_audit(self) -> None:
        user = typing.cast(discord.User, SimpleNamespace(id=42, send=mock.AsyncMock()))
        shell = object.__new__(dm.DMShell)
        shell._touch_contact = mock.Mock()  # type: ignore[method-assign]
        with mock.patch.object(dm.db, "record_action_audit") as audit:
            self.assertTrue(await shell.send_to(user, "private body"))

        parameters = audit.call_args.kwargs["parameters"]
        self.assertEqual(parameters, {"content_supplied": True, "content_length": 12})
        self.assertNotIn("private body", repr(audit.call_args))

    async def test_audit_failure_does_not_turn_a_successful_send_into_a_failure(self) -> None:
        send = mock.AsyncMock()
        user = typing.cast(discord.User, SimpleNamespace(id=42, send=send))
        shell = object.__new__(dm.DMShell)
        shell._touch_contact = mock.Mock()  # type: ignore[method-assign]

        with mock.patch.object(
            dm.db, "record_action_audit", side_effect=OSError("disk full")
        ):
            sent = await shell.send_to(user, "private body")

        self.assertTrue(sent)
        send.assert_awaited_once_with("private body")


if __name__ == "__main__":
    unittest.main()
