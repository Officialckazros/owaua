from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from owaua import config, db, diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        db.close()
        self.old_path = config.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory()
        config.DB_PATH = str(Path(self.tempdir.name) / "doctor.sqlite3")

    def tearDown(self) -> None:
        db.close()
        config.DB_PATH = self.old_path
        self.tempdir.cleanup()

    def test_module_state_distinguishes_ready_setup_and_disabled(self) -> None:
        scope = "guild:123"
        self.assertEqual(diagnostics.module_state(scope, "action_log")["state"], "needs_setup")
        db.module_config_set(
            scope,
            "action_log",
            enabled=True,
            settings={"channel_id": "456"},
            actor_id="1",
        )
        self.assertEqual(diagnostics.module_state(scope, "action_log")["state"], "ready")
        db.module_config_set(
            scope,
            "action_log",
            enabled=False,
            settings={},
            actor_id="1",
        )
        self.assertEqual(diagnostics.module_state(scope, "action_log")["state"], "disabled")

    def test_report_contains_no_secret_values(self) -> None:
        permissions = SimpleNamespace(
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            manage_roles=True,
        )
        role = SimpleNamespace(position=10)
        guild = SimpleNamespace(
            id=123,
            me=SimpleNamespace(guild_permissions=permissions, top_role=role),
            roles=[role],
        )
        with (
            mock.patch.object(config, "OPENAI_API_KEY", "super-secret"),
            mock.patch.object(config, "DASHBOARD_PUBLIC_URL", "https://example.invalid/dashboard"),
        ):
            report = diagnostics.format_report(
                diagnostics.runtime_diagnostics(guild, task_health={"background": {}})
            )
        self.assertNotIn("super-secret", report)
        self.assertIn("AI provider", report)
        self.assertIn("Modules", report)


if __name__ == "__main__":
    unittest.main()
