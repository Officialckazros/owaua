"""Durable, privacy-conscious conversation memory for the persona bot."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


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

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(scope_id, user_id, id);

                CREATE TABLE IF NOT EXISTS conversation_memory (
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    facts_json TEXT NOT NULL DEFAULT '[]',
                    summarized_through_id INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_id, user_id)
                );
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
        scope_id: str,
        user_id: str,
        role: str,
        content: str,
        attachments: list[dict[str, str]] | None = None,
        created_at: float | None = None,
    ) -> bool:
        payload = json.dumps(attachments or [], ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO messages
                    (event_id, scope_id, user_id, role, content, attachments_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    scope_id,
                    user_id,
                    role,
                    content,
                    payload,
                    created_at if created_at is not None else time.time(),
                ),
            )
            return cursor.rowcount == 1

    def recent_messages(
        self, scope_id: str, user_id: str, *, limit: int
    ) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT id, role, content, attachments_json, created_at
                FROM messages
                WHERE scope_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (scope_id, user_id, limit),
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
    ) -> list[dict[str, Any]]:
        summary, facts, summarized_through = self.get_memory(scope_id, user_id)
        del summary, facts
        with self._lock, self._connect() as db:
            cutoff = db.execute(
                """
                SELECT id FROM messages
                WHERE scope_id = ? AND user_id = ?
                ORDER BY id DESC LIMIT 1 OFFSET ?
                """,
                (scope_id, user_id, keep_recent),
            ).fetchone()
            if cutoff is None:
                return []
            rows = db.execute(
                """
                SELECT id, role, content, attachments_json, created_at
                FROM messages
                WHERE scope_id = ? AND user_id = ?
                  AND id > ? AND id <= ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (scope_id, user_id, summarized_through, int(cutoff["id"]), limit),
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

    def get_memory(self, scope_id: str, user_id: str) -> tuple[str, list[str], int]:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT summary, facts_json, summarized_through_id
                FROM conversation_memory
                WHERE scope_id = ? AND user_id = ?
                """,
                (scope_id, user_id),
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
        summary: str,
        facts: list[str],
        summarized_through_id: int,
    ) -> None:
        clean_facts = list(dict.fromkeys(fact.strip() for fact in facts if fact.strip()))[:50]
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO conversation_memory
                    (scope_id, user_id, summary, facts_json, summarized_through_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, user_id) DO UPDATE SET
                    summary = excluded.summary,
                    facts_json = excluded.facts_json,
                    summarized_through_id = MAX(
                        conversation_memory.summarized_through_id,
                        excluded.summarized_through_id
                    ),
                    updated_at = excluded.updated_at
                WHERE excluded.summarized_through_id >=
                    conversation_memory.summarized_through_id
                """,
                (
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
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE created_at < ?", (cutoff_timestamp,)
            )
            return cursor.rowcount
