from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory_store import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_application_settings_survive_reopening(self) -> None:
        self.assertEqual(self.store.get_setting("selected_persona_model", "gpt"), "gpt")
        self.store.set_setting("selected_persona_model", "deepseek")

        reopened = MemoryStore(self.path)

        self.assertEqual(
            reopened.get_setting("selected_persona_model", "gpt"), "deepseek"
        )

    def test_messages_survive_reopening_and_duplicate_events_are_ignored(self) -> None:
        inserted = self.store.append_message(
            event_id="discord:1",
            scope_id="channel",
            user_id="user",
            role="user",
            content="remember this",
            attachments=[{"kind": "image", "filename": "cat.png"}],
        )
        duplicate = self.store.append_message(
            event_id="discord:1",
            scope_id="channel",
            user_id="user",
            role="user",
            content="duplicate",
        )

        reopened = MemoryStore(self.path)
        messages = reopened.recent_messages("channel", "user", limit=10)

        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual([message["content"] for message in messages], ["remember this"])
        self.assertEqual(messages[0]["attachments"][0]["filename"], "cat.png")

    def test_recent_messages_are_ordered_and_conversation_scoped(self) -> None:
        for number in range(5):
            self.store.append_message(
                event_id=f"event:{number}",
                scope_id="one",
                user_id="user",
                role="user" if number % 2 == 0 else "assistant",
                content=str(number),
            )
        self.store.append_message(
            event_id="other",
            scope_id="two",
            user_id="user",
            role="user",
            content="not included",
        )

        recent = self.store.recent_messages("one", "user", limit=3)

        self.assertEqual([message["content"] for message in recent], ["2", "3", "4"])

    def test_active_mode_persists_and_claims_every_sixth_message(self) -> None:
        self.assertEqual(self.store.active_mode_status("channel"), (False, 0))

        self.store.set_active_mode("channel", True)
        claims = [self.store.record_active_message("channel") for _ in range(12)]

        self.assertEqual(
            claims,
            [False, False, False, False, False, True] * 2,
        )
        self.assertEqual(MemoryStore(self.path).active_mode_status("channel"), (True, 0))

        self.store.set_active_mode("channel", False)
        self.assertFalse(self.store.record_active_message("channel"))
        self.assertEqual(self.store.active_mode_status("channel"), (False, 0))

    def test_gif_mode_has_an_independent_ten_message_cadence(self) -> None:
        self.store.set_active_mode("channel", True)
        self.store.set_gif_mode("channel", True)

        active_claims = []
        gif_claims = []
        for _ in range(10):
            active_claims.append(self.store.record_active_message("channel", interval=6))
            gif_claims.append(self.store.record_gif_message("channel", interval=10))

        self.assertEqual(active_claims.count(True), 1)
        self.assertEqual(gif_claims, [False] * 9 + [True])
        self.assertEqual(self.store.active_mode_status("channel"), (True, 4))
        self.assertEqual(self.store.gif_mode_status("channel"), (True, 0))

        self.store.set_gif_mode("channel", False)
        self.assertFalse(self.store.record_gif_message("channel"))
        self.assertEqual(self.store.active_mode_status("channel"), (True, 4))

    def test_topic_lock_persists_and_can_be_cleared(self) -> None:
        self.assertEqual(self.store.channel_topic("channel"), "")
        self.store.set_topic("channel", "yuri from ddlc")
        self.assertEqual(
            MemoryStore(self.path).channel_topic("channel"), "yuri from ddlc"
        )

        self.store.set_topic("channel", None)
        self.assertEqual(self.store.channel_topic("channel"), "")

    def test_existing_active_channel_table_is_migrated_without_losing_state(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.execute(
                """
                CREATE TABLE active_channels (
                    scope_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.execute(
                "INSERT INTO active_channels VALUES ('channel', 1, 3, 1.0)"
            )

        migrated = MemoryStore(legacy_path)

        self.assertEqual(migrated.active_mode_status("channel"), (True, 3))
        self.assertEqual(migrated.gif_mode_status("channel"), (False, 0))
        self.assertEqual(migrated.channel_topic("channel"), "")
        migrated.set_gif_mode("channel", True)
        migrated.set_topic("channel", "yuri from ddlc")
        self.assertEqual(migrated.gif_mode_status("channel"), (True, 0))
        self.assertEqual(migrated.channel_topic("channel"), "yuri from ddlc")

    def test_old_messages_roll_into_durable_summary_state(self) -> None:
        for number in range(8):
            self.store.append_message(
                event_id=f"event:{number}",
                scope_id="channel",
                user_id="user",
                role="user" if number % 2 == 0 else "assistant",
                content=str(number),
            )

        first_batch = self.store.messages_to_summarize(
            "channel", "user", keep_recent=4, limit=50
        )
        self.assertEqual([message["content"] for message in first_batch], ["0", "1", "2", "3"])

        self.store.save_memory(
            "channel",
            "user",
            summary="They discussed four messages",
            facts=["likes cats", "likes cats", "uses short replies"],
            summarized_through_id=first_batch[-1]["id"],
        )
        summary, facts, cursor = self.store.get_memory("channel", "user")

        self.assertEqual(summary, "They discussed four messages")
        self.assertEqual(facts, ["likes cats", "uses short replies"])
        self.assertEqual(cursor, first_batch[-1]["id"])
        self.assertEqual(
            self.store.messages_to_summarize(
                "channel", "user", keep_recent=4, limit=50
            ),
            [],
        )

    def test_unaccepted_turns_and_context_are_not_reused(self) -> None:
        self.store.append_message(
            event_id="rejected",
            scope_id="channel",
            user_id="user",
            role="user",
            content="must not become model context",
            accepted=False,
        )
        self.store.append_message(
            event_id="accepted",
            scope_id="channel",
            user_id="user",
            role="user",
            content="safe context",
        )

        self.assertEqual(
            [
                message["content"]
                for message in self.store.recent_messages("channel", "user", limit=10)
            ],
            ["safe context"],
        )
        summary_records = self.store.messages_to_summarize(
            "channel", "user", keep_recent=0, limit=10
        )
        self.assertEqual([record["content"] for record in summary_records], ["safe context"])

    def test_server_erase_removes_all_users_and_rejects_stale_writes(self) -> None:
        first_generation = self.store.register_scope("channel-one", "server-a")
        self.store.register_scope("channel-two", "server-a")
        self.store.register_scope("other-channel", "server-b")
        for event_id, scope_id, user_id in (
            ("a-1", "channel-one", "one"),
            ("a-2", "channel-two", "two"),
            ("b-1", "other-channel", "three"),
        ):
            self.store.append_message(
                event_id=event_id,
                scope_id=scope_id,
                user_id=user_id,
                role="user",
                content=event_id,
            )
        self.store.save_memory(
            "channel-one",
            "one",
            summary="old server-a context",
            facts=["old fact"],
            summarized_through_id=1,
        )

        self.store.erase_server_memory("server-a")

        self.assertEqual(self.store.recent_messages("channel-one", "one", limit=10), [])
        self.assertEqual(self.store.recent_messages("channel-two", "two", limit=10), [])
        self.assertEqual(self.store.get_memory("channel-one", "one"), ("", [], 0))
        self.assertEqual(
            [item["content"] for item in self.store.recent_messages("other-channel", "three", limit=10)],
            ["b-1"],
        )
        self.assertFalse(
            self.store.append_message(
                event_id="late-a-response",
                scope_id="channel-one",
                user_id="one",
                role="assistant",
                content="must not be retained after the wipe",
                expected_generation=first_generation,
            )
        )

    def test_legacy_memory_is_quarantined_until_new_turns_are_accepted(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.sqlite3"
        db = sqlite3.connect(legacy_path)
        try:
            db.executescript(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
                CREATE TABLE conversation_memory (
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    facts_json TEXT NOT NULL DEFAULT '[]',
                    summarized_through_id INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_id, user_id)
                );
                INSERT INTO messages
                    (event_id, scope_id, user_id, role, content, created_at)
                VALUES ('legacy:1', 'channel', 'user', 'user', 'unvetted legacy text', 1);
                INSERT INTO conversation_memory
                    (scope_id, user_id, summary, facts_json, summarized_through_id, updated_at)
                VALUES ('channel', 'user', 'legacy summary', '["legacy fact"]', 1, 1);
                """
            )
            db.commit()
        finally:
            db.close()

        migrated = MemoryStore(legacy_path)

        self.assertEqual(migrated.recent_messages("channel", "user", limit=10), [])
        self.assertEqual(migrated.get_memory("channel", "user"), ("", [], 0))


if __name__ == "__main__":
    unittest.main()
