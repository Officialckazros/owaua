"""Last-N conversation memory in SQLite."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class MemoryStore:
    """One short-lived connection per operation."""

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
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    server_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_channels (
                    scope_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    message_count INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(scope_id, user_id, id);
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock, self._managed_connection() as db:
            row = db.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._managed_connection() as db:
            db.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def append_message(
        self,
        *,
        event_id: str,
        scope_id: str,
        user_id: str,
        role: str,
        content: str,
        server_id: str = "",
        created_at: float | None = None,
    ) -> bool:
        with self._lock, self._managed_connection() as db:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO messages
                    (event_id, scope_id, user_id, server_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    scope_id,
                    user_id,
                    server_id,
                    role,
                    content,
                    time.time() if created_at is None else created_at,
                ),
            )
            return cursor.rowcount == 1

    def recent_messages(
        self, scope_id: str, user_id: str, *, limit: int
    ) -> list[dict[str, object]]:
        with self._lock, self._managed_connection() as db:
            rows = db.execute(
                """
                SELECT id, role, content, created_at
                FROM messages
                WHERE scope_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (scope_id, user_id, limit),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "role": str(row["role"]),
                "content": str(row["content"]),
                "created_at": float(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def erase_server_memory(self, server_id: str) -> int:
        with self._lock, self._managed_connection() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE server_id = ?", (server_id,)
            )
            return cursor.rowcount

    def set_active_mode(self, scope_id: str, enabled: bool) -> None:
        with self._lock, self._managed_connection() as db:
            db.execute(
                """
                INSERT INTO active_channels
                    (scope_id, enabled, message_count, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    message_count = 0,
                    updated_at = excluded.updated_at
                """,
                (scope_id, int(enabled), time.time()),
            )

    def active_mode_status(self, scope_id: str) -> tuple[bool, int]:
        with self._lock, self._managed_connection() as db:
            row = db.execute(
                "SELECT enabled, message_count FROM active_channels WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
        if row is None:
            return False, 0
        return bool(row["enabled"]), int(row["message_count"])

    def record_active_message(self, scope_id: str, *, interval: int = 6) -> bool:
        if interval < 1:
            raise ValueError("interval must be at least 1")
        with self._lock, self._managed_connection() as db:
            cursor = db.execute(
                """
                UPDATE active_channels
                SET message_count = (message_count + 1) % ?, updated_at = ?
                WHERE scope_id = ? AND enabled = 1
                """,
                (interval, time.time(), scope_id),
            )
            if cursor.rowcount != 1:
                return False
            row = db.execute(
                "SELECT message_count FROM active_channels WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
            return row is not None and int(row["message_count"]) == 0
