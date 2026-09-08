"""Durable, privacy-conscious conversation memory for the persona bot."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class MemoryStore:
    """Small SQLite store with one short-lived connection per operation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _managed_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._managed_connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    model_id TEXT NOT NULL DEFAULT '',
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_memory (
                    model_id TEXT NOT NULL DEFAULT '',
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    facts_json TEXT NOT NULL DEFAULT '[]',
                    summarized_through_id INTEGER NOT NULL DEFAULT 0,
                    accepted_context INTEGER NOT NULL DEFAULT 0
                        CHECK (accepted_context IN (0, 1)),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (model_id, scope_id, user_id)
                );
                """
            )
            message_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "accepted" not in message_columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN accepted INTEGER NOT NULL DEFAULT 0"
                )
            if "model_id" not in message_columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN model_id TEXT NOT NULL DEFAULT ''"
                )
            memory_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(conversation_memory)").fetchall()
            }
            if "accepted_context" not in memory_columns:
                db.execute(
                    "ALTER TABLE conversation_memory "
                    "ADD COLUMN accepted_context INTEGER NOT NULL DEFAULT 0"
                )
            if "model_id" not in memory_columns:
                # SQLite cannot add a column to an existing primary key. Rebuild
                # the table so each provider can own an independent memory row.
                db.execute("ALTER TABLE conversation_memory RENAME TO conversation_memory_legacy")
                db.execute(
                    """
                    CREATE TABLE conversation_memory (
                        model_id TEXT NOT NULL DEFAULT '',
                        scope_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        summary TEXT NOT NULL DEFAULT '',
                        facts_json TEXT NOT NULL DEFAULT '[]',
                        summarized_through_id INTEGER NOT NULL DEFAULT 0,
                        accepted_context INTEGER NOT NULL DEFAULT 0
                            CHECK (accepted_context IN (0, 1)),
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (model_id, scope_id, user_id)
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO conversation_memory
                        (model_id, scope_id, user_id, summary, facts_json,
                         summarized_through_id, accepted_context, updated_at)
                    SELECT '', scope_id, user_id, summary, facts_json,
                           summarized_through_id, accepted_context, updated_at
                    FROM conversation_memory_legacy
                    """
                )
                db.execute("DROP TABLE conversation_memory_legacy")
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(model_id, scope_id, user_id, id)
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def append_message(
        self,
        *,
        event_id: str,
        model_id: str = "",
        scope_id: str,
        user_id: str,
        role: str,
        content: str,
        attachments: list[dict[str, str]] | None = None,
        created_at: float | None = None,
        accepted: bool = True,
    ) -> bool:
        payload = json.dumps(attachments or [], ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._managed_connection() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO messages
                    (event_id, model_id, scope_id, user_id, role, content, attachments_json, accepted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    model_id,
                    scope_id,
                    user_id,
                    role,
                    content,
                    payload,
                    int(accepted),
                    created_at if created_at is not None else time.time(),
                ),
            )
            return cursor.rowcount == 1

    def recent_messages(
        self, scope_id: str, user_id: str, *, limit: int, model_id: str = ""
    ) -> list[dict[str, Any]]:
        with self._lock, self._managed_connection() as db:
            rows = db.execute(
                """
                SELECT id, role, content, attachments_json, created_at
                FROM messages
                WHERE model_id = ? AND scope_id = ? AND user_id = ? AND accepted = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (model_id, scope_id, user_id, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            try:
                attachments = json.loads(row["attachments_json"])
            except (TypeError, json.JSONDecodeError):
                attachments = []
            result.append(
                {
                    "id": int(row["id"]),
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                    "attachments": attachments if isinstance(attachments, list) else [],
                    "created_at": float(row["created_at"]),
                }
            )
        return result

    def messages_to_summarize(
        self,
        scope_id: str,
        user_id: str,
        *,
        keep_recent: int,
        limit: int,
        model_id: str = "",
    ) -> list[dict[str, Any]]:
        summary, facts, summarized_through = self.get_memory(scope_id, user_id, model_id=model_id)
        del summary, facts
        with self._lock, self._managed_connection() as db:
            cutoff = db.execute(
                """
                SELECT id FROM messages
                WHERE model_id = ? AND scope_id = ? AND user_id = ? AND accepted = 1
                ORDER BY id DESC LIMIT 1 OFFSET ?
                """,
                (model_id, scope_id, user_id, keep_recent),
            ).fetchone()
            if cutoff is None:
                return []
            rows = db.execute(
                """
                SELECT id, role, content, attachments_json, created_at
                FROM messages
                WHERE model_id = ? AND scope_id = ? AND user_id = ?
                  AND accepted = 1 AND id > ? AND id <= ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (model_id, scope_id, user_id, summarized_through, int(cutoff["id"]), limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def get_memory(
        self, scope_id: str, user_id: str, *, model_id: str = ""
    ) -> tuple[str, list[str], int]:
        with self._lock, self._managed_connection() as db:
            row = db.execute(
                """
                SELECT summary, facts_json, summarized_through_id
                FROM conversation_memory
                WHERE model_id = ? AND scope_id = ? AND user_id = ? AND accepted_context = 1
                """,
                (model_id, scope_id, user_id),
            ).fetchone()
        if row is None:
            return "", [], 0
        try:
            facts = json.loads(row["facts_json"])
        except (TypeError, json.JSONDecodeError):
            facts = []
        clean_facts = [str(value) for value in facts if isinstance(value, str)]
        return str(row["summary"]), clean_facts, int(row["summarized_through_id"])

    def save_memory(
        self,
        scope_id: str,
        user_id: str,
        *,
        model_id: str = "",
        summary: str,
        facts: list[str],
        summarized_through_id: int,
    ) -> None:
        clean_facts = list(dict.fromkeys(fact.strip() for fact in facts if fact.strip()))[:50]
        with self._lock, self._managed_connection() as db:
            db.execute(
                """
                INSERT INTO conversation_memory
                    (model_id, scope_id, user_id, summary, facts_json, summarized_through_id,
                     accepted_context, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(model_id, scope_id, user_id) DO UPDATE SET
                    summary = excluded.summary,
                    facts_json = excluded.facts_json,
                    accepted_context = 1,
                    summarized_through_id = MAX(
                        conversation_memory.summarized_through_id,
                        excluded.summarized_through_id
                    ),
                    updated_at = excluded.updated_at
                WHERE excluded.summarized_through_id >=
                    conversation_memory.summarized_through_id
                """,
                (
                    model_id,
                    scope_id,
                    user_id,
                    summary.strip(),
                    json.dumps(clean_facts, ensure_ascii=False, separators=(",", ":")),
                    summarized_through_id,
                    time.time(),
                ),
            )

    def prune_older_than(self, cutoff_timestamp: float) -> int:
        """Delete raw messages older than the configured retention period."""
        with self._lock, self._managed_connection() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE created_at < ?", (cutoff_timestamp,)
            )
            return cursor.rowcount
