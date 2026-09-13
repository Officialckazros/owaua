from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_application_settings_survive_reopening(self) -> None:
        self.assertEqual(self.store.get_setting("selected_persona", "rudeish"), "rudeish")
        self.store.set_setting("selected_persona", "nerdish")

        reopened = MemoryStore(self.path)

        self.assertEqual(reopened.get_setting("selected_persona", "rudeish"), "nerdish")

    def test_messages_survive_reopening_and_duplicate_events_are_ignored(self) -> None:
        inserted = self.store.append_message(
            event_id="discord:1",
            scope_id="channel",
            user_id="user",
            role="user",
            content="remember this",
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

        self.assertEqual(claims, [False, False, False, False, False, True] * 2)
        self.assertEqual(MemoryStore(self.path).active_mode_status("channel"), (True, 0))

        self.store.set_active_mode("channel", False)
        self.assertFalse(self.store.record_active_message("channel"))
        self.assertEqual(self.store.active_mode_status("channel"), (False, 0))

    def test_server_erase_removes_only_that_server(self) -> None:
        self.store.append_message(
            event_id="a-1",
            scope_id="channel-one",
            user_id="one",
            server_id="server-a",
            role="user",
            content="keep-me-not",
        )
        self.store.append_message(
            event_id="a-2",
            scope_id="channel-two",
            user_id="two",
            server_id="server-a",
            role="user",
            content="also-gone",
        )
        self.store.append_message(
            event_id="b-1",
            scope_id="other-channel",
            user_id="three",
            server_id="server-b",
            role="user",
            content="b-1",
        )

        removed = self.store.erase_server_memory("server-a")

        self.assertEqual(removed, 2)
        self.assertEqual(self.store.recent_messages("channel-one", "one", limit=10), [])
        self.assertEqual(self.store.recent_messages("channel-two", "two", limit=10), [])
        self.assertEqual(
            [item["content"] for item in self.store.recent_messages("other-channel", "three", limit=10)],
            ["b-1"],
        )


if __name__ == "__main__":
    unittest.main()
