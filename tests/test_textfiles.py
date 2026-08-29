"""Unit and integration tests for .txt file reading capabilities."""

from __future__ import annotations

import os
import typing
import unittest
from unittest import mock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DISCORD_TOKEN", "test-token")

import tempfile
from pathlib import Path

from owaua import brain, config, db, kb, textfiles


class DummyAttachment:
    def __init__(
        self,
        filename: str = "test.txt",
        content_type: str = "text/plain",
        size: int = 100,
        content: bytes = b"Hello world from text file!",
    ):
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self._content = content

    async def read(self) -> bytes:
        return self._content


class DummyMessage:
    def __init__(
        self,
        content: str = "",
        attachments: list[typing.Any] | None = None,
        reference: object | None = None,
    ):
        self.content = content
        self.attachments = attachments or []
        self.reference = reference


class TextFilesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db.close()
        self._old_db_path = config.DB_PATH
        self._tempdir = tempfile.TemporaryDirectory()
        config.DB_PATH = str(Path(self._tempdir.name) / "test_textfiles.sqlite3")
        db._gs_cache.clear()
        db._mem_cache.clear()
        db._lessons_cache = None
        db._lessons_ts = 0.0
        kb._READY = False
        kb._HAS_FTS5 = None

    def tearDown(self) -> None:
        db.close()
        config.DB_PATH = self._old_db_path
        db._gs_cache.clear()
        db._mem_cache.clear()
        db._lessons_cache = None
        db._lessons_ts = 0.0
        kb._READY = False
        kb._HAS_FTS5 = None
        self._tempdir.cleanup()

    def test_is_text_attachment(self) -> None:
        self.assertTrue(
            textfiles.is_text_attachment(typing.cast(typing.Any, DummyAttachment("doc.txt")))
        )
        self.assertTrue(
            textfiles.is_text_attachment(typing.cast(typing.Any, DummyAttachment("data.TEXT")))
        )
        self.assertTrue(
            textfiles.is_text_attachment(typing.cast(typing.Any, DummyAttachment("server.log")))
        )
        self.assertTrue(
            textfiles.is_text_attachment(typing.cast(typing.Any, DummyAttachment("sheet.csv")))
        )
        self.assertTrue(
            textfiles.is_text_attachment(
                typing.cast(
                    typing.Any, DummyAttachment("file", content_type="text/plain; charset=utf-8")
                )
            )
        )
        self.assertFalse(
            textfiles.is_text_attachment(
                typing.cast(typing.Any, DummyAttachment("pic.png", "image/png"))
            )
        )
        self.assertFalse(
            textfiles.is_text_attachment(
                typing.cast(typing.Any, DummyAttachment("binary.bin", "application/octet-stream"))
            )
        )
        self.assertFalse(textfiles.is_text_attachment(None))

    async def test_read_attachment_text_utf8(self) -> None:
        att = DummyAttachment(
            filename="sample.txt",
            content="Hello world!\nLine 2".encode("utf-8"),
        )
        result = await textfiles.read_attachment_text(typing.cast(typing.Any, att))
        self.assertIsNotNone(result)
        self.assertIn("[attached text file: sample.txt]", typing.cast(typing.Any, result))
        self.assertIn("Hello world!\nLine 2", typing.cast(typing.Any, result))

    async def test_read_attachment_text_encoding_fallback(self) -> None:
        latin1_bytes = "Café au lait".encode("latin-1")
        att = DummyAttachment(filename="latin.txt", content=latin1_bytes)
        result = await textfiles.read_attachment_text(typing.cast(typing.Any, att))
        self.assertIsNotNone(result)
        self.assertIn("Café au lait", typing.cast(typing.Any, result))

    async def test_read_attachment_text_size_limit(self) -> None:
        att = DummyAttachment(filename="huge.txt", size=10_000_000, content=b"x" * 100)
        result = await textfiles.read_attachment_text(typing.cast(typing.Any, att), max_bytes=1000)
        self.assertIn("exceeds configured limit", typing.cast(typing.Any, result))

    async def test_read_attachment_text_truncation(self) -> None:
        large_text = "A" * 500
        att = DummyAttachment(filename="long.txt", content=large_text.encode("utf-8"), size=500)
        result = await textfiles.read_attachment_text(typing.cast(typing.Any, att), max_chars=100)
        self.assertIn(
            "[... truncated (400 characters omitted) ...]", typing.cast(typing.Any, result)
        )
        self.assertIn("A" * 100, typing.cast(typing.Any, result))

    async def test_extract_message_text_files_direct(self) -> None:
        att1 = DummyAttachment(filename="log1.txt", content=b"log data 1")
        att2 = DummyAttachment(filename="log2.txt", content=b"log data 2")
        msg = DummyMessage(attachments=[att1, att2])
        extracted = await textfiles.extract_message_text_files(typing.cast(typing.Any, msg))
        self.assertIn("log data 1", extracted)
        self.assertIn("log data 2", extracted)

    async def test_extract_message_text_files_from_reply(self) -> None:
        att = DummyAttachment(filename="parent.txt", content=b"parent message content")
        parent_msg = DummyMessage(attachments=[att])
        ref = mock.Mock()
        ref.resolved = parent_msg
        msg = DummyMessage(attachments=[], reference=ref)
        extracted = await textfiles.extract_message_text_files(typing.cast(typing.Any, msg))
        self.assertIn("parent message content", extracted)

    def test_brain_build_system_includes_file_notes(self) -> None:
        file_notes = "[attached text file: error.log]\nStack trace here"
        system = brain.build_system(
            user_id="12345",
            username="Tester",
            query="check this error",
            guild_id="guild-1",
            file_notes=file_notes,
        )
        self.assertIn("<attached-text-files>", system)
        self.assertIn("Stack trace here", system)
        self.assertIn("</attached-text-files>", system)


if __name__ == "__main__":
    unittest.main()
