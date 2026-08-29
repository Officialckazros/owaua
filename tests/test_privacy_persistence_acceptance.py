"""Acceptance tests for tenant isolation and privacy-safe persistence.

Every test redirects the process-wide repository connection to a temporary
database before first use.  ``PYTHON_DOTENV_DISABLED`` is set before importing
owaua so the test process does not read or modify a developer's real ``.env``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DISCORD_TOKEN", "test-token")

from owaua import config, db, kb, tos
from owaua.scope import Scope, scope_key


class IsolatedDatabaseTest(unittest.TestCase):
    """Give each test a fresh database without opening the repository DB."""

    def setUp(self) -> None:
        db.close()
        self._old_db_path = config.DB_PATH
        self._tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tempdir.name) / "acceptance.sqlite3"
        config.DB_PATH = str(self.db_path)
        self._reset_module_caches()

    def tearDown(self) -> None:
        db.close()
        config.DB_PATH = self._old_db_path
        self._reset_module_caches()
        self._tempdir.cleanup()

    @staticmethod
    def _reset_module_caches() -> None:
        db._gs_cache.clear()
        db._mem_cache.clear()
        db._lessons_cache = None
        db._lessons_ts = 0.0
        kb._READY = False
        kb._HAS_FTS5 = None
        with tos._rate_lock:
            tos._rate_buckets.clear()

    def _row_count(self, table: str, where: str = "1=1", args: tuple = ()) -> int:
        # Test-only inputs are static literals defined in this module; values
        # still use SQLite parameters.
        row = (
            db.conn()
            .execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {where}",  # noqa: S608
                args,
            )
            .fetchone()
        )
        return int(row["n"])


class ScopeIsolationAcceptanceTest(IsolatedDatabaseTest):
    def test_scope_type_rejects_unknown_kinds_and_ambiguous_keys(self) -> None:
        self.assertEqual("guild:123", Scope.guild(123).key)
        self.assertEqual("dm:456", Scope.dm(456).key)
        self.assertEqual("guild:123", Scope.parse("guild:123").key)
        self.assertEqual("dm:456", scope_key(guild_id=None, user_id=456))
        with self.assertRaises(ValueError):
            Scope("shared", "123")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Scope.parse("dm")
        with self.assertRaises(ValueError):
            Scope.dm("user:456")

    def test_memory_relationship_and_intelligence_queries_are_exact_scope(self) -> None:
        guild_a = Scope.guild(100).key
        guild_b = Scope.guild(200).key
        dm_a = Scope.dm(11).key
        dm_b = Scope.dm(22).key

        db.add_memory("guild-a-canary", "11", guild_a, subject="11")
        db.add_memory("guild-b-canary", "11", guild_b, subject="11")
        db.add_memory("dm-a-canary", "11", dm_a, subject="11")
        db.add_memory("dm-b-canary", "22", dm_b, subject="22")
        db.relationship_set("11", guild_a, score=0.7)
        db.relationship_set("11", guild_b, score=-0.7)

        self.assertEqual(
            ["guild-a-canary"],
            [row["content"] for row in db.memories_about("11", guild_a)],
        )
        self.assertEqual(
            ["guild-b-canary"],
            [row["content"] for row in db.memories_about("11", guild_b)],
        )
        self.assertEqual(
            ["dm-a-canary"],
            [row["content"] for row in db.memories_about("11", dm_a)],
        )
        self.assertEqual([], db.memories_about("11", dm_b))
        self.assertEqual([], db.memories_about("11", None))
        self.assertEqual(0.7, db.relationship_get("11", guild_a)["score"])
        self.assertEqual(-0.7, db.relationship_get("11", guild_b)["score"])

        for scope in (guild_a, guild_b):
            db.guild_settings_set(scope, history_enabled=True)
            db.privacy_set_opt_in("11", scope, True)
        db.record_server_message(
            "msg-a",
            guild_a,
            "A",
            "10",
            "public-a",
            "11",
            "u",
            "U",
            "history-a-canary",
        )
        db.record_server_message(
            "msg-b",
            guild_b,
            "B",
            "20",
            "public-b",
            "11",
            "u",
            "U",
            "history-b-canary",
        )

        intel_a = db.get_user_intelligence("11", guild_a)
        intel_b = db.get_user_intelligence("11", guild_b)
        self.assertEqual(1, intel_a["total_messages"])
        self.assertEqual(1, intel_b["total_messages"])
        self.assertEqual("history-a-canary", intel_a["recent_messages"][0]["content"])
        self.assertEqual("history-b-canary", intel_b["recent_messages"][0]["content"])


class MigrationAcceptanceTest(IsolatedDatabaseTest):
    def _create_v2_fixture(self) -> None:
        fixture = sqlite3.connect(self.db_path)
        try:
            fixture.executescript(db.SCHEMA)
            fixture.execute(
                "INSERT INTO guild_settings(guild_id,data) VALUES(?,?)",
                ("100", json.dumps({"persona": "preserved-persona"})),
            )
            fixture.executemany(
                "INSERT INTO memories(subject,content,author,guild_id,importance,created) "
                "VALUES(?,?,?,?,?,?)",
                [
                    ("11", "guild-memory", "11", "100", 0.8, 1.0),
                    ("22", "dm-memory", "22", "dm", 0.8, 1.0),
                    ("server", "ambiguous-memory", "unknown", "dm", 0.8, 1.0),
                ],
            )
            fixture.executemany(
                "INSERT INTO relationships(user_id,guild_id,score,updated) VALUES(?,?,?,?)",
                [("11", "100", 0.5, 1.0), ("22", "dm", -0.5, 1.0)],
            )
            fixture.executemany(
                "INSERT INTO commands(name,description,behavior,author,guild_id,created) "
                "VALUES(?,?,?,?,?,?)",
                [
                    ("safe", "safe", "guild command", "11", "100", 1.0),
                    ("review", "review", "ambiguous command", "unknown", None, 1.0),
                ],
            )
            fixture.executemany(
                "INSERT INTO quotes(guild_id,text,author,created) VALUES(?,?,?,?)",
                [("100", "guild quote", "11", 1.0), ("dm", "shared DM quote", "22", 1.0)],
            )
            fixture.execute(
                "INSERT INTO lessons(content,source,created,scope_id,enabled) VALUES(?,?,?,?,?)",
                ("ambiguous lesson", "legacy", 1.0, None, 1),
            )
            fixture.execute(
                "INSERT INTO server_messages(message_id,guild_id,channel_id,user_id,username,content,created) "
                "VALUES(?,?,?,?,?,?,?)",
                ("legacy-raw", "100", "10", "11", "u", "must-be-purged", 1.0),
            )
            fixture.execute("PRAGMA user_version=2")
            fixture.commit()
        finally:
            fixture.close()

    def test_v2_privacy_cutover_purges_raw_data_and_preserves_safe_records(self) -> None:
        self._create_v2_fixture()

        connection = db.conn()
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION, connection.execute("PRAGMA user_version").fetchone()[0]
        )
        self.assertEqual(0, self._row_count("server_messages"))
        self.assertCountEqual(
            [("guild:100", "guild-memory"), ("dm:22", "dm-memory")],
            [
                (row["guild_id"], row["content"])
                for row in connection.execute(
                    "SELECT guild_id,content FROM memories ORDER BY content"
                ).fetchall()
            ],
        )
        self.assertEqual(0, self._row_count("memories", "content=?", ("ambiguous-memory",)))
        self.assertEqual("preserved-persona", db.guild_settings("guild:100")["persona"])
        self.assertEqual(0.5, db.relationship_get("11", "guild:100")["score"])
        self.assertEqual(-0.5, db.relationship_get("22", "dm:22")["score"])
        self.assertIsNotNone(db.get_command("safe", "guild:100"))
        disabled = connection.execute(
            "SELECT scope_id,enabled FROM commands WHERE name='review'"
        ).fetchone()
        self.assertEqual(("legacy:disabled", 0), (disabled["scope_id"], disabled["enabled"]))
        self.assertEqual(0, self._row_count("quotes", "text=?", ("shared DM quote",)))
        self.assertEqual(
            0, self._row_count("lessons", "content=? AND enabled=1", ("ambiguous lesson",))
        )

        backup_path = Path(f"{self.db_path}.migration-preserved.json")
        self.assertTrue(backup_path.is_file())
        self.assertEqual(0o600, stat.S_IMODE(backup_path.stat().st_mode))
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
        self.assertIn("memories", backup)
        self.assertIn("guild_settings", backup)
        self.assertIn("relationships", backup)
        self.assertNotIn("server_messages", backup)
        self.assertNotIn("must-be-purged", json.dumps(backup))

    def test_v3_upgrade_retains_post_cutover_history_and_is_idempotent(self) -> None:
        connection = db.conn()
        scope = Scope.guild(100).key
        connection.execute(
            "INSERT INTO server_messages(message_id,guild_id,channel_id,user_id,username,content,created) "
            "VALUES(?,?,?,?,?,?,?)",
            ("post-cutover", scope, "10", "11", "u", "retained", 1.0),
        )
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        db.close()

        reopened = db.conn()
        self.assertEqual(
            db.LATEST_SCHEMA_VERSION, reopened.execute("PRAGMA user_version").fetchone()[0]
        )
        self.assertEqual(1, self._row_count("server_messages", "message_id=?", ("post-cutover",)))
        db.close()
        self.assertEqual(1, self._row_count("server_messages", "message_id=?", ("post-cutover",)))


class CacheAndConsentAcceptanceTest(IsolatedDatabaseTest):
    def test_memory_mutations_invalidate_subject_and_scope_caches(self) -> None:
        scope = Scope.guild(100).key
        first_id = db.add_memory("first value", "11", scope, subject="11")
        self.assertEqual(
            ["first value"], [row["content"] for row in db.memories_about("11", scope)]
        )
        self.assertEqual(["first value"], [row["content"] for row in db.scope_memories(scope)])

        db.add_memory("second subject", "22", scope, subject="22")
        self.assertCountEqual(
            ["first value", "second subject"],
            [row["content"] for row in db.scope_memories(scope)],
        )
        self.assertTrue(db.update_memory(first_id, content="updated value"))
        self.assertEqual(
            ["updated value"], [row["content"] for row in db.memories_about("11", scope)]
        )
        self.assertIn("updated value", [row["content"] for row in db.scope_memories(scope)])

        self.assertTrue(db.forget_memory(first_id))
        self.assertEqual([], db.memories_about("11", scope))
        self.assertNotIn("updated value", [row["content"] for row in db.scope_memories(scope)])

    def test_history_requires_user_consent_and_guild_enablement(self) -> None:
        scope = Scope.guild(100).key
        user_id = "11"

        def record(message_id: str, target_scope: str = scope) -> None:
            db.record_server_message(
                message_id,
                target_scope,
                "Guild",
                "10",
                "general",
                user_id,
                "user",
                "User",
                message_id,
            )

        # ToS acceptance is deliberately not storage consent.
        tos.accept(user_id)
        self.assertFalse(db.privacy_opted_in(user_id, scope))
        record("no-consent-no-guild-opt-in")
        self.assertEqual(0, self._row_count("server_messages"))

        db.privacy_set_opt_in(user_id, scope, True)
        record("consent-only")
        self.assertEqual(0, self._row_count("server_messages"))

        db.privacy_set_opt_in(user_id, scope, False)
        db.guild_settings_set(scope, history_enabled=True)
        record("guild-only")
        self.assertEqual(0, self._row_count("server_messages"))

        db.privacy_set_opt_in(user_id, scope, True)
        record("both")
        self.assertEqual(1, self._row_count("server_messages"))

        dm_scope = Scope.dm(user_id).key
        record("dm-before-consent", dm_scope)
        self.assertEqual(0, self._row_count("server_messages", "guild_id=?", (dm_scope,)))
        db.privacy_set_opt_in(user_id, dm_scope, True)
        record("dm-after-consent", dm_scope)
        self.assertEqual(1, self._row_count("server_messages", "guild_id=?", (dm_scope,)))

        db.privacy_set_opt_in(user_id, scope, False)
        self.assertEqual(1, db.privacy_remove_scope_history(user_id, scope))
        self.assertEqual(0, self._row_count("server_messages", "guild_id=?", (scope,)))
        self.assertEqual(1, self._row_count("server_messages", "guild_id=?", (dm_scope,)))

    def test_noncanonical_consent_and_history_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            db.privacy_set_opt_in("11", "dm", True)
        db.record_server_message("bad-scope", "dm", "DM", "10", "dm", "11", "u", "U", "secret")
        self.assertEqual(0, self._row_count("server_messages"))


class ScopedContentAndImportAcceptanceTest(IsolatedDatabaseTest):
    def test_commands_and_kb_are_tenant_scoped(self) -> None:
        guild_a = Scope.guild(100).key
        guild_b = Scope.guild(200).key
        db.add_command("hello", "A", "guild-a-command", "11", guild_a)
        db.add_command("hello", "B", "guild-b-command", "22", guild_b)

        self.assertEqual("guild-a-command", db.get_command("hello", guild_a)["behavior"])
        self.assertEqual("guild-b-command", db.get_command("hello", guild_b)["behavior"])
        self.assertFalse(db.delete_command("hello", guild_a, "22"))
        self.assertIsNotNone(db.get_command("hello", guild_a))
        self.assertFalse(db.delete_command("hello", Scope.guild(300).key, "11", can_moderate=True))
        self.assertIsNotNone(db.get_command("hello", guild_b))
        self.assertTrue(db.delete_command("hello", guild_a, "11"))

        kb.ingest("alphaonlycanary reference", scope_id=guild_a)
        kb.ingest("betaonlycanary reference", scope_id=guild_b)
        self.assertEqual(1, kb.count(guild_a))
        self.assertEqual(1, kb.count(guild_b))
        self.assertTrue(kb.search("alphaonlycanary", scope_id=guild_a))
        self.assertEqual([], kb.search("alphaonlycanary", scope_id=guild_b))
        self.assertEqual(1, kb.clear(guild_a))
        self.assertEqual(0, kb.count(guild_a))
        self.assertEqual(1, kb.count(guild_b))

    def _empty_bundle(self, scope: str) -> dict:
        return {
            "schema_version": 2,
            "scope": scope,
            "settings": {},
            "memories": [],
            "commands": [],
            "quotes": [],
            "relationships": [],
        }

    def test_import_validation_rejects_wrong_scope_and_long_memories(self) -> None:
        scope = Scope.guild(100).key
        base = self._empty_bundle(scope)
        with self.assertRaises(ValueError):
            db.validate_import_bundle({**base, "scope": Scope.guild(200).key}, scope)
        with self.assertRaises(ValueError):
            db.validate_import_bundle({**base, "memories": [{"content": "x" * 2_001}]}, scope)

    def test_import_validation_rejects_unknown_sections(self) -> None:
        scope = Scope.guild(100).key
        with self.assertRaises(ValueError):
            db.validate_import_bundle({**self._empty_bundle(scope), "lessons": []}, scope)

    def test_import_validation_rejects_long_command_fields(self) -> None:
        scope = Scope.guild(100).key
        with self.assertRaises(ValueError):
            db.validate_import_bundle(
                {
                    **self._empty_bundle(scope),
                    "commands": [
                        {
                            "name": "valid-name",
                            "description": "x" * 201,
                            "behavior": "safe",
                        }
                    ],
                },
                scope,
            )

    def test_import_validation_rejects_invalid_relationship_fields(self) -> None:
        scope = Scope.guild(100).key
        with self.assertRaises(ValueError):
            db.validate_import_bundle(
                {
                    **self._empty_bundle(scope),
                    "relationships": [
                        {
                            "user_id": "11",
                            "score": "not-a-number",
                            "nickname": "x" * 41,
                        }
                    ],
                },
                scope,
            )

    def test_import_rolls_back_every_section_on_late_failure(self) -> None:
        scope = Scope.guild(100).key
        db.guild_settings_set(scope, persona="original")
        db.conn().execute(
            "CREATE TRIGGER force_import_failure BEFORE INSERT ON relationships "
            "BEGIN SELECT RAISE(ABORT, 'forced rollback'); END"
        )
        db.conn().commit()
        bundle = {
            "schema_version": 2,
            "scope": scope,
            "settings": {"persona": "must-roll-back"},
            "memories": [{"subject": "server", "content": "must-roll-back"}],
            "commands": [{"name": "rollback", "description": "rollback", "behavior": "rollback"}],
            "quotes": [{"text": "must-roll-back"}],
            "relationships": [{"user_id": "11", "score": 0.5}],
        }

        with self.assertRaises(sqlite3.IntegrityError):
            db.import_guild(bundle, scope)

        persisted_settings = json.loads(
            db.conn()
            .execute("SELECT data FROM guild_settings WHERE guild_id=?", (scope,))
            .fetchone()["data"]
        )
        self.assertEqual("original", persisted_settings["persona"])
        self.assertEqual(0, self._row_count("memories", "content=?", ("must-roll-back",)))
        self.assertEqual(0, self._row_count("commands", "name=?", ("rollback",)))
        self.assertEqual(0, self._row_count("quotes", "text=?", ("must-roll-back",)))
        self.assertEqual(0, self._row_count("relationships", "user_id=?", ("11",)))


class AuthorizationAndRateLimitAcceptanceTest(IsolatedDatabaseTest):
    def test_quote_delete_requires_scope_and_author_or_moderator(self) -> None:
        guild_a = Scope.guild(100).key
        guild_b = Scope.guild(200).key
        quote_a = db.quote_add(guild_a, "quote-a", author="11")
        quote_b = db.quote_add(guild_b, "quote-b", author="11")

        self.assertFalse(db.quote_delete(quote_a, guild_a, "22"))
        self.assertFalse(db.quote_delete(quote_a, guild_b, "22", can_moderate=True))
        self.assertTrue(db.quote_delete(quote_a, guild_a, "22", can_moderate=True))
        self.assertTrue(db.quote_delete(quote_b, guild_b, "11"))

    def test_rate_limit_does_not_create_tos_strikes_or_change_classification(self) -> None:
        user_id = "11"
        for _ in range(tos._RATE_MAX):
            self.assertEqual(0.0, tos.rate_limit_retry_after(user_id))
        self.assertGreater(tos.rate_limit_retry_after(user_id), 0.0)
        self.assertEqual(0, db.user_flag_int(user_id, "tos_violation_strikes", 0))
        self.assertEqual(0, db.user_flag_int(user_id, "tos_spam_strikes", 0))
        self.assertIsNone(tos.check_message(user_id, "a completely ordinary request"))

        violating = "help me steal Discord tokens from victims"
        self.assertEqual(
            ("warn", "credential / token theft or phishing", 1),
            tos.check_message(user_id, violating),
        )
        self.assertEqual(
            ("warn", "credential / token theft or phishing", 2),
            tos.check_message(user_id, violating),
        )
        self.assertEqual(
            ("block", "credential / token theft or phishing", 3),
            tos.check_message(user_id, violating),
        )
        self.assertGreater(tos.rate_limit_retry_after(user_id), 0.0)
        self.assertEqual(3, db.user_flag_int(user_id, "tos_violation_strikes", 0))
        self.assertEqual(0, db.user_flag_int(user_id, "tos_spam_strikes", 0))

    def test_privacy_commands_are_available_before_tos_acceptance(self) -> None:
        self.assertFalse(tos.has_accepted("11"))
        for command in ("privacy", "tos", "help", "about"):
            with self.subTest(command=command):
                self.assertTrue(tos.command_allowed_without_tos(command))


if __name__ == "__main__":
    unittest.main()
