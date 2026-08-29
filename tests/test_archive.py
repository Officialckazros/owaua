from __future__ import annotations

import datetime
import os
import tempfile
import typing
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DISCORD_TOKEN", "test-token")

from owaua import archive, config, db
from owaua.scope import Scope


class _HistoryChannel:
    def __init__(self, channel_id: int, name: str, messages: list[object]):
        self.id = channel_id
        self.name = name
        self.messages = messages
        self.after_ids: list[int | None] = []

    async def history(self, *, limit: typing.Any, after: typing.Any, oldest_first: typing.Any):
        self.after_ids.append(getattr(after, "id", None))
        after_id = getattr(after, "id", 0) if after else 0
        for message in self.messages:
            if int(typing.cast(typing.Any, typing.cast(typing.Any, message).id)) > int(after_id):
                yield message


class ArchiveTest(unittest.IsolatedAsyncioTestCase):
    guild_id = "1535083112709496903"
    scope_id = Scope.guild(guild_id).key

    def setUp(self) -> None:
        db.close()
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = config.DB_PATH
        self.old_guilds = config.ARCHIVE_GUILD_IDS
        config.DB_PATH = str(Path(self.tempdir.name) / "archive.sqlite3")
        config.ARCHIVE_GUILD_IDS = frozenset({self.guild_id})

    def tearDown(self) -> None:
        db.close()
        config.DB_PATH = self.old_path
        config.ARCHIVE_GUILD_IDS = self.old_guilds
        self.tempdir.cleanup()

    @staticmethod
    def _message(
        message_id: int,
        content: str,
        *,
        user_id: int = 42,
        guild: object | None = None,
        channel: object | None = None,
    ) -> object:
        author = SimpleNamespace(
            id=user_id,
            name=f"user-{user_id}",
            display_name=f"User {user_id}",
        )
        return SimpleNamespace(
            id=message_id,
            content=content,
            author=author,
            guild=guild,
            channel=channel,
            created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
            + datetime.timedelta(seconds=message_id),
        )

    def test_text_only_removes_unicode_and_custom_emoji(self) -> None:
        self.assertEqual(
            "hello world",
            archive.text_only(
                "hello 😀 <:wave:123456789012345678> <a:dance:223456789012345678> world"
            ),
        )
        self.assertEqual("", archive.text_only("😀 <:wave:123456789012345678>"))
        long_text = ("word " * 500) + "😀"
        normalized = archive.text_only(long_text)
        self.assertLessEqual(len(normalized), 2000)
        self.assertEqual(normalized, archive.text_only(normalized))

    async def test_backfill_is_text_only_author_scoped_and_resumable(self) -> None:
        guild = SimpleNamespace(id=int(self.guild_id), name="Archive Guild")
        channel = _HistoryChannel(100, "general", [])
        text = self._message(
            101,
            "capybara notes 😀",
            user_id=42,
            guild=guild,
            channel=channel,
        )
        media_only = self._message(
            102,
            "😀",
            user_id=99,
            guild=guild,
            channel=channel,
        )
        channel.messages = [text, media_only]

        first = await archive.backfill_channel(typing.cast(typing.Any, guild), channel)
        second = await archive.backfill_channel(typing.cast(typing.Any, guild), channel)

        self.assertEqual({"channel_id": "100", "scanned": 2, "saved": 1}, first)
        self.assertEqual({"channel_id": "100", "scanned": 0, "saved": 0}, second)
        self.assertEqual([None, 102], channel.after_ids)
        row = db.conn().execute("SELECT user_id,content,created FROM server_messages").fetchone()
        self.assertEqual("42", row["user_id"])
        self.assertEqual("capybara notes", row["content"])
        self.assertEqual(
            typing.cast(typing.Any, typing.cast(typing.Any, text).created_at).timestamp(),
            row["created"],
        )
        status = db.archive_status(self.scope_id)
        self.assertEqual(1, status["stored_messages"])
        self.assertEqual(2, status["channels"][0]["messages_seen"])
        self.assertEqual(1, status["complete_channels"])

    def test_full_archive_search_is_user_and_guild_scoped(self) -> None:
        for message_id, user_id, content in (
            ("1", "42", "my favorite animal is a capybara"),
            ("2", "99", "my favorite animal is a cat"),
        ):
            db.record_archived_message_batch(
                self.scope_id,
                "100",
                "general",
                [
                    {
                        "message_id": message_id,
                        "guild_name": "Archive Guild",
                        "user_id": user_id,
                        "username": f"user-{user_id}",
                        "display_name": f"User {user_id}",
                        "content": content,
                        "created_at": 100.0 + int(message_id),
                    }
                ],
                last_message_id=message_id,
                complete=True,
            )

        hits = db.search_user_messages("42", self.scope_id, "favorite animal", 20)
        self.assertEqual(["my favorite animal is a capybara"], [hit["content"] for hit in hits])

    def test_location_question_finds_short_nationality_claim_with_context(self) -> None:
        records = [
            {
                "message_id": "10",
                "guild_name": "Archive Guild",
                "user_id": "99",
                "username": "questioner",
                "display_name": "Questioner",
                "content": "what nationality are you",
                "created_at": 100.0,
            },
            {
                "message_id": "11",
                "guild_name": "Archive Guild",
                "user_id": "42",
                "username": "target",
                "display_name": "Target",
                "content": "Croatian",
                "created_at": 101.0,
            },
            {
                "message_id": "12",
                "guild_name": "Archive Guild",
                "user_id": "42",
                "username": "target",
                "display_name": "Target",
                "content": "Im not russian",
                "created_at": 102.0,
            },
            {
                "message_id": "13",
                "guild_name": "Archive Guild",
                "user_id": "42",
                "username": "target",
                "display_name": "Target",
                "content": "I want to travel to Japan",
                "created_at": 103.0,
            },
        ]
        db.record_archived_message_batch(
            self.scope_id,
            "100",
            "general",
            records,
            last_message_id="13",
            complete=True,
        )

        hits = db.search_user_messages("42", self.scope_id, "where does he live", 20)
        self.assertEqual("Croatian", hits[0]["content"])
        self.assertEqual("what nationality are you", hits[0]["context_before"])
        self.assertEqual("Questioner", hits[0]["context_author"])

    def test_retention_keeps_archive_but_removes_other_guild_history(self) -> None:
        old = 1.0
        db.record_server_message(
            "archive-old",
            self.scope_id,
            "Archive Guild",
            "100",
            "general",
            "42",
            "user",
            "User",
            "archive stays",
            force=True,
            created_at=old,
        )
        db.record_server_message(
            "ordinary-old",
            "guild:123",
            "Other Guild",
            "200",
            "general",
            "42",
            "user",
            "User",
            "ordinary expires",
            force=True,
            created_at=old,
        )

        db.cleanup_expired_content(30)
        rows = (
            db.conn()
            .execute("SELECT message_id FROM server_messages ORDER BY message_id")
            .fetchall()
        )
        self.assertEqual(["archive-old"], [row["message_id"] for row in rows])

    def test_normalization_deletes_legacy_emoji_only_rows(self) -> None:
        db.record_server_message(
            "legacy-emoji",
            self.scope_id,
            "Archive Guild",
            "100",
            "general",
            "42",
            "user",
            "User",
            "<:wave:123456789012345678>",
            force=True,
        )
        db.record_server_message(
            "legacy-mixed",
            self.scope_id,
            "Archive Guild",
            "100",
            "general",
            "42",
            "user",
            "User",
            "hello 😀 world",
            force=True,
        )

        result = db.normalize_archived_message_text(self.scope_id, archive.text_only)
        rows = (
            db.conn()
            .execute("SELECT message_id,content FROM server_messages ORDER BY message_id")
            .fetchall()
        )
        self.assertEqual({"updated": 1, "deleted": 1}, result)
        self.assertEqual([("legacy-mixed", "hello world")], [tuple(row) for row in rows])

    async def test_live_edit_to_emoji_only_removes_stored_text(self) -> None:
        guild = SimpleNamespace(id=int(self.guild_id), name="Archive Guild")
        channel = SimpleNamespace(id=100, name="general")
        original = self._message(500, "keep this", guild=guild, channel=channel)
        edited = self._message(500, "😀", guild=guild, channel=channel)

        self.assertTrue(await archive.store_live_message(typing.cast(typing.Any, original)))
        self.assertFalse(
            await archive.store_live_message(typing.cast(typing.Any, edited), edited=True)
        )
        count = (
            db.conn()
            .execute("SELECT COUNT(*) FROM server_messages WHERE message_id='500'")
            .fetchone()[0]
        )
        self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
