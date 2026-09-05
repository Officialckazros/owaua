from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
