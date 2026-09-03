"""Regression tests for SQLite-backed CLI state and legacy migration."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import typing
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from owaua import blocked, config, db, dm, tos


class StatePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = self.root / "private" / "state.db"
        self.blocks = self.root / "blocked_users.json"
        self.contacts = self.root / "dm_contacts.json"
        self.active = self.root / "cli_active_chats.json"
        self.patches = [
            mock.patch.object(config, "DB_PATH", str(self.database)),
            mock.patch.object(blocked, "BLOCKED_FILE", self.blocks),
            mock.patch.object(dm, "CONTACTS_FILE", self.contacts),
            mock.patch.object(dm, "ACTIVE_CHATS_FILE", self.active),
        ]
        db.close()
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        db.close()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tempdir.cleanup()

    def _write_json(self, path: Path, value: object, mode: int = 0o644) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(path, mode)

    def test_block_migration_is_idempotent_and_owner_only(self) -> None:
        self._write_json(
            self.blocks,
            {
                "users": {
                    "123": {"reason": "manual operator block", "blocked_at": 10},
                    "456": {"reason": "tos: confirmed abuse", "blocked_at": 20},
                    "not-an-id": {"reason": "invalid"},
                }
            },
        )

        entries = blocked.list_blocked()
        self.assertEqual(set(entries), {"123", "456"})
        self.assertEqual(entries["123"]["source"], "manual")
        self.assertEqual(entries["456"]["source"], "tos")
        self.assertEqual(stat.S_IMODE(self.blocks.stat().st_mode), 0o600)

        self._write_json(
            self.blocks,
            {"users": {"999": {"reason": "must not be imported later"}}},
            mode=0o600,
        )
        self.assertEqual(set(blocked.list_blocked()), {"123", "456"})

        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.database}{suffix}")
            if candidate.exists():
                self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.database.parent.stat().st_mode),
            0o700,
        )

    def test_legacy_state_symlinks_are_rejected_without_touching_target(self) -> None:
        target = self.root / "outside.json"
        self._write_json(
            target,
            {"users": {"123": {"reason": "must not be followed"}}},
            mode=0o644,
        )
        self.blocks.symlink_to(target)
        with (
            mock.patch.object(blocked, "_warned_invalid_legacy", False),
            self.assertWarns(RuntimeWarning),
        ):
            self.assertEqual(blocked.list_blocked(), {})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertFalse(db.legacy_state_migrated(blocked._migration_name(self.blocks)))

    def test_manual_block_cannot_be_reclassified_or_tos_unblocked(self) -> None:
        self.assertTrue(
            blocked.block_user("123", reason="manual investigation", block_source="manual")
        )
        self.assertFalse(
            blocked.block_user(
                "123",
                reason="tos: later automatic signal",
                block_source="tos",
            )
        )

        metadata = blocked.get_blocked_user("123")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata["source"], "manual")
        self.assertEqual(metadata["reason"], "manual investigation")
        self.assertEqual(len(metadata["history"]), 2)
        self.assertFalse(blocked.unblock_user("123", expected_source="tos"))
        self.assertTrue(blocked.is_dynamically_blocked("123"))
        self.assertTrue(blocked.unblock_user("123", expected_source="manual"))

    def test_concurrent_block_updates_are_atomic_and_bounded(self) -> None:
        def apply(index: int) -> bool:
            return blocked.block_user(
                "777",
                reason=f"tos: event {index}",
                block_source="tos",
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(apply, range(40)))

        self.assertEqual(sum(results), 1)
        metadata = blocked.get_blocked_user("777")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata["source"], "tos")
        self.assertEqual(len(metadata["history"]), 10)

    def test_contacts_migrate_once_and_stale_updates_do_not_win(self) -> None:
        self._write_json(
            self.contacts,
            {
                "101": {
                    "name": "friend",
                    "last_message_at": "2026-08-19T10:00:00+02:00",
                },
                "bad": {"name": "ignored", "last_message_at": "not-a-date"},
            },
        )
        self.assertEqual(
            load := dm.load_contacts(),
            {
                "101": {
                    "name": "friend",
                    "last_message_at": "2026-08-19T08:00:00+00:00",
                }
            },
        )
        self.assertEqual(stat.S_IMODE(self.contacts.stat().st_mode), 0o600)

        dm.save_contacts(
            {
                "101": {
                    "name": "new name",
                    "last_message_at": "2026-08-19T11:00:00+02:00",
                }
            }
        )
        dm.save_contacts(load)
        self.assertEqual(dm.load_contacts()["101"]["name"], "new name")

        self._write_json(
            self.contacts,
            {
                "202": {
                    "name": "late legacy edit",
                    "last_message_at": "2026-08-19T12:00:00+02:00",
                }
            },
            mode=0o600,
        )
        self.assertNotIn("202", dm.load_contacts())

    def test_active_sessions_migrate_and_do_not_release_each_other(self) -> None:
        self._write_json(self.active, {"303": time.time()})
        self.assertTrue(dm.is_cli_conversation_active(303))
        self.assertEqual(stat.S_IMODE(self.active.stat().st_mode), 0o600)

        dm._mark_active(404, "session-a")
        dm._mark_active(404, "session-b")
        dm._mark_inactive(404, "session-a")
        self.assertTrue(dm.is_cli_conversation_active(404))
        dm._mark_inactive(404, "session-b")
        self.assertFalse(dm.is_cli_conversation_active(404))

        db.cli_active_touch("505", "stale", heartbeat=time.time() - 1_000)
        self.assertFalse(dm.is_cli_conversation_active(505))

    def test_privacy_delete_covers_new_user_owned_tables(self) -> None:
        dm.save_contacts(
            {
                "606": {
                    "name": "delete me",
                    "last_message_at": "2026-08-19T12:00:00+02:00",
                }
            }
        )
        db.cli_active_touch("606", "session")
        db.tos_challenge_create("606", "a" * 64, tos.TOS_VERSION, time.time() + 60)
        db.tos_acceptance_set(
            "606",
            tos.TOS_VERSION,
            status="accepted",
            network_hash="b" * 64,
        )
        blocked.block_user("606", reason="tos: delete", block_source="tos")
        db.record_assistant_action(
            actor_id="606",
            scope_id="guild:1",
            channel_id="10",
            action="set_nickname",
            target_id="607",
            parameters={"nickname": "new"},
            result="set nickname",
            inverse={"type": "set_nickname", "target_user": "607", "nickname": "old"},
            source_nonce="privacy-test",
        )

        exported = db.privacy_export("606")
        self.assertEqual(len(exported["dynamic_blocks"]), 1)
        self.assertEqual(len(exported["dm_contacts"]), 1)
        self.assertEqual(len(exported["assistant_actions"]), 1)
        self.assertEqual(exported["tos_acceptance"][0]["status"], "accepted")
        self.assertEqual(len(exported["tos_blocked_networks"]), 1)
        deleted = db.privacy_delete_user("606")
        self.assertEqual(deleted["dynamic_blocks"], 1)
        self.assertEqual(deleted["dm_contacts"], 1)
        self.assertEqual(deleted["cli_active_conversations"], 1)
        self.assertEqual(deleted["assistant_action_history"], 1)
        self.assertEqual(deleted["tos_acceptance_challenges"], 1)
        self.assertEqual(deleted["tos_acceptances"], 1)
        self.assertEqual(deleted["tos_blocked_networks"], 1)
        self.assertFalse(blocked.is_dynamically_blocked("606"))

    def test_privacy_delete_minimizes_but_preserves_malware_security_block(self) -> None:
        blocked.block_user(
            "707",
            reason="tos: malware attachment detected (Win.Trojan.Agent)",
            category="malware",
            offending_text="sha256:abcdef0123456789 length:64",
            channel_id="123",
            guild_id="456",
            guild_name="private guild",
            user_tag="private tag",
            trigger_source="clamav_attachment",
            block_source="tos",
        )

        deleted = db.privacy_delete_user("707")

        self.assertEqual(deleted["dynamic_blocks"], 0)
        self.assertTrue(blocked.is_dynamically_blocked("707"))
        metadata = blocked.get_blocked_user("707")
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata["category"], "malware")
        self.assertEqual(metadata["guild_id"], "")
        self.assertEqual(metadata["guild_name"], "")
        self.assertEqual(metadata["user_tag"], "")
        self.assertEqual(metadata["history"], [])

    def test_assistant_action_history_is_scope_bound_and_consumable(self) -> None:
        first_id = db.record_assistant_action(
            actor_id="10",
            scope_id="guild:1",
            channel_id="20",
            action="set_nickname",
            target_id="11",
            parameters={"nickname": "Raven"},
            result="set nickname to Raven",
            inverse={"type": "set_nickname", "target_user": "11", "nickname": "Before"},
            source_nonce="first",
        )
        self.assertEqual(
            first_id, typing.cast(typing.Any, db.latest_assistant_action("10", "guild:1"))["id"]
        )
        self.assertIsNone(db.latest_assistant_action("10", "guild:2"))
        self.assertIsNone(db.latest_assistant_action("99", "guild:1"))

        db.record_assistant_action(
            actor_id="10",
            scope_id="guild:1",
            channel_id="20",
            action="set_nickname",
            target_id="11",
            parameters={"nickname": "Before"},
            result="restored nickname",
            inverse={"type": "set_nickname", "target_user": "11", "nickname": "Raven"},
            source_nonce="second",
            consumed_action_id=first_id,
        )
        latest = db.latest_assistant_action("10", "guild:1")
        self.assertNotEqual(first_id, typing.cast(typing.Any, latest)["id"])
        self.assertEqual(
            "Raven", typing.cast(typing.Any, typing.cast(typing.Any, latest)["inverse"])["nickname"]
        )
        history = db.recent_assistant_actions("10", "guild:1")
        self.assertEqual(2, len(history))
        self.assertIsNotNone(history[1]["consumed"])

    def test_version_three_database_upgrades_without_replaying_rescope(self) -> None:
        connection = db.conn()
        connection.execute(
            "INSERT INTO commands(scope_id,name,description,behavior,author,uses,created,enabled) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("guild:1", "hello", "desc", "behavior", "1", 0, time.time(), 1),
        )
        for table in (
            "state_migrations",
            "dynamic_blocks",
            "dm_contacts",
            "cli_active_conversations",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        db.close()

        upgraded = db.conn()
        self.assertEqual(
            upgraded.execute("PRAGMA user_version").fetchone()[0],
            db.LATEST_SCHEMA_VERSION,
        )
        row = upgraded.execute(
            "SELECT scope_id,name FROM commands WHERE scope_id=? AND name=?",
            ("guild:1", "hello"),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            {
                str(r[0])
                for r in upgraded.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            & {
                "state_migrations",
                "dynamic_blocks",
                "dm_contacts",
                "cli_active_conversations",
            },
            {
                "state_migrations",
                "dynamic_blocks",
                "dm_contacts",
                "cli_active_conversations",
            },
        )


if __name__ == "__main__":
    unittest.main()
