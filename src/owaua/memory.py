"""Last-N conversation memory in SQLite."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from security import API_LIMITS, ApiLimits, BudgetExceeded, DuplicateRequest

MAX_STORED_CHARS = 5700
MAX_STORED_MESSAGES = 10000
CONVERSATION_MESSAGES = 20
RETENTION_SECONDS = 7 * 86400


class MemoryStore:
    """One short-lived connection per operation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        # A committed charge must survive a crash before the HTTP request.
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA max_page_count=32768")
        connection.execute("PRAGMA secure_delete=ON")
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
                    unbounded INTEGER NOT NULL DEFAULT 0 CHECK (unbounded IN (0, 1)),
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
                CREATE TABLE IF NOT EXISTS api_usage (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS api_usage_time_idx ON api_usage(created_at);
                CREATE TABLE IF NOT EXISTS api_totals (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    requests INTEGER NOT NULL DEFAULT 0,
                    last_time REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO api_totals(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS full_image_generation_usage (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS full_image_generation_usage_user_time_idx
                    ON full_image_generation_usage(user_id, created_at);
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "unbounded" not in columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN unbounded INTEGER NOT NULL DEFAULT 0"
                )
            if "server_id" not in columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN server_id TEXT NOT NULL DEFAULT ''"
                )
        os.chmod(self.path, 0o600)
        with self._lock, self._managed_connection() as db:
            self._prune(db)

    @staticmethod
    def _prune(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM messages WHERE unbounded=0 AND created_at<?", (time.time()-RETENTION_SECONDS,))
        db.execute("DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE unbounded=0 ORDER BY id DESC LIMIT -1 OFFSET ?)", (MAX_STORED_MESSAGES,))
        db.execute("UPDATE messages SET content=substr(content,1,?) WHERE unbounded=0 AND length(content)>?", (MAX_STORED_CHARS, MAX_STORED_CHARS))
        db.execute(
            "DELETE FROM messages WHERE id IN (SELECT id FROM "
            "(SELECT id, row_number() OVER (PARTITION BY scope_id,user_id ORDER BY id DESC) AS n FROM messages WHERE unbounded=0) WHERE n>?)",
            (CONVERSATION_MESSAGES,),
        )

    @staticmethod
    def _generation(db: sqlite3.Connection, user_id: str, server_id: str) -> tuple[str, str]:
        values = []
        for key in (f"memory_user_epoch:{user_id}", f"memory_server_epoch:{server_id}"):
            row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            values.append("0" if row is None else row[0])
        return tuple(values)

    def memory_generation(self, user_id: str, server_id: str) -> tuple[str, str]:
        with self._lock, self._managed_connection() as db:
            return self._generation(db, user_id, server_id)

    @staticmethod
    def _bump_generation(db: sqlite3.Connection, key: str) -> None:
        db.execute("INSERT INTO app_settings(key,value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1", (key,))

    def reserve_api_request(
        self, event_id: str, user_id: str, guild_id: str,
        *, limits: ApiLimits = API_LIMITS, now: float | None = None,
        expected_generation: tuple[str, str] | None = None,
        server_id: str = "",
    ) -> None:
        """Charge before sending, atomically across processes; never refund errors.

        The lifetime counter survives rolling retention and memory erasure.
        Time is monotonic in the ledger even after a system-clock rollback.
        Every provider attempt is subject to the same ceilings and emergency
        pause; the erasure fence still protects deleted data.
        """
        with self._lock, self._managed_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if expected_generation is not None and self._generation(db, user_id, server_id) != expected_generation:
                raise BudgetExceeded("This request was cancelled by memory erasure")
            row = db.execute("SELECT value FROM app_settings WHERE key='api_paused'").fetchone()
            if row is not None and row[0] == "1":
                raise BudgetExceeded("AI requests are paused by the owner")
            if db.execute("SELECT 1 FROM api_usage WHERE event_id=?", (event_id,)).fetchone():
                raise DuplicateRequest("Already charged this event")
            total = db.execute("SELECT requests, last_time FROM api_totals WHERE id=1").fetchone()
            current = max(time.time() if now is None else now, total["last_time"])
            # Rolling windows avoid a midnight burst doubling the daily budget.
            rows = db.execute(
                "SELECT user_id, guild_id, created_at FROM api_usage WHERE created_at>?",
                (current - 86400,),
            ).fetchall()
            checks = (
                (total["requests"], limits.lifetime),
                (len(rows), limits.per_day),
                (sum(r["created_at"] > current - 60 for r in rows), limits.per_minute),
                (sum(r["user_id"] == user_id for r in rows), limits.per_user_day),
                (sum(r["guild_id"] == guild_id for r in rows), limits.per_guild_day),
            )
            if any(used >= ceiling for used, ceiling in checks):
                raise BudgetExceeded(
                    "AI request budget reached; try later or DM ckazros or email "
                    "ckazros@owaua.com to request more usage"
                )
            db.execute("INSERT INTO api_usage VALUES (?, ?, ?, ?)", (event_id, user_id, guild_id, current))
            db.execute("UPDATE api_totals SET requests=requests+1, last_time=? WHERE id=1", (current,))
            db.execute("DELETE FROM api_usage WHERE created_at<?", (current - RETENTION_SECONDS,))

    def api_status(self) -> str:
        with self._lock, self._managed_connection() as db:
            total = db.execute("SELECT requests FROM api_totals WHERE id=1").fetchone()[0]
            daily = db.execute("SELECT count(*) FROM api_usage WHERE created_at>?", (time.time()-86400,)).fetchone()[0]
        return f"API attempts: {daily}/{API_LIMITS.per_day} in 24h; {total}/{API_LIMITS.lifetime} lifetime"

    def reserve_full_image_generation(
        self, event_id: str, user_id: str, *, limit: int, now: float | None = None
    ) -> bool:
        """Reserve one of a full-mode user's daily image-generation slots."""
        if limit < 1:
            return False
        current = time.time() if now is None else now
        with self._lock, self._managed_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT 1 FROM full_image_generation_usage WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing is not None:
                return True
            db.execute(
                "DELETE FROM full_image_generation_usage WHERE created_at<?",
                (current - 86400,),
            )
            used = db.execute(
                "SELECT count(*) FROM full_image_generation_usage WHERE user_id=?",
                (user_id,),
            ).fetchone()[0]
            if used >= limit:
                return False
            db.execute(
                "INSERT INTO full_image_generation_usage(event_id,user_id,created_at) VALUES (?, ?, ?)",
                (event_id, user_id, current),
            )
            return True

    def reserve_full_mode_prompt(
        self, event_id: str, user_id: str, *, limit: int, now: float | None = None
    ) -> bool:
        """Reserve one of a promoted full-mode user's lifetime prompts."""
        if limit < 1:
            return False
        current = time.time() if now is None else now
        key = f"full_mode_prompt:{user_id}"
        with self._lock, self._managed_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
            used = 0 if existing is None else int(existing[0])
            event_key = f"{key}:event:{event_id}"
            if db.execute(
                "SELECT 1 FROM app_settings WHERE key=?", (event_key,)
            ).fetchone() is not None:
                return True
            if used >= limit:
                return False
            db.execute(
                "INSERT INTO app_settings(key,value) VALUES (?, ?)",
                (key, str(used + 1)),
            ) if existing is None else db.execute(
                "UPDATE app_settings SET value=? WHERE key=?",
                (str(used + 1), key),
            )
            db.execute("INSERT INTO app_settings(key,value) VALUES (?, '1')", (event_key,))
            return True

    def prune(self) -> None:
        with self._lock, self._managed_connection() as db:
            self._prune(db)

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
        expected_generation: tuple[str, str] | None = None,
        unbounded: bool = False,
    ) -> bool:
        with self._lock, self._managed_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if expected_generation is not None and self._generation(db, user_id, server_id) != expected_generation:
                return False
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO messages
                    (event_id, scope_id, user_id, server_id, role, content, unbounded, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    scope_id,
                    user_id,
                    server_id,
                    role,
                    content if unbounded else content[:MAX_STORED_CHARS],
                    int(unbounded),
                    time.time() if created_at is None else created_at,
                ),
            )
            inserted = cursor.rowcount == 1
            db.execute(
                "DELETE FROM messages WHERE unbounded=0 AND scope_id=? AND user_id=? AND id NOT IN "
                "(SELECT id FROM messages WHERE unbounded=0 AND scope_id=? AND user_id=? ORDER BY id DESC LIMIT ?)",
                (scope_id, user_id, scope_id, user_id, CONVERSATION_MESSAGES),
            )
            db.execute("DELETE FROM messages WHERE unbounded=0 AND created_at<?", (time.time()-RETENTION_SECONDS,))
            db.execute("DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE unbounded=0 ORDER BY id DESC LIMIT -1 OFFSET ?)", (MAX_STORED_MESSAGES,))
            return inserted

    def erase_user_memory(self, user_id: str) -> int:
        with self._lock, self._managed_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._bump_generation(db, f"memory_user_epoch:{user_id}")
            return db.execute("DELETE FROM messages WHERE user_id=?", (user_id,)).rowcount

    def recent_messages(
        self, scope_id: str, user_id: str, *, limit: int | None
    ) -> list[dict[str, object]]:
        with self._lock, self._managed_connection() as db:
            db.execute("DELETE FROM messages WHERE unbounded=0 AND created_at<?", (time.time()-RETENTION_SECONDS,))
            query = """
                SELECT id, role, content, created_at
                FROM messages
                WHERE scope_id = ? AND user_id = ?
                ORDER BY id DESC
            """
            parameters: tuple[object, ...] = (scope_id, user_id)
            if limit is not None:
                query += " LIMIT ?"
                parameters += (limit,)
            rows = db.execute(query, parameters).fetchall()
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
            db.execute("BEGIN IMMEDIATE")
            self._bump_generation(db, f"memory_server_epoch:{server_id}")
            cursor = db.execute(
                "DELETE FROM messages WHERE server_id = ?", (server_id,)
            )
            return cursor.rowcount

    def reset_server_data(self, server_id: str) -> int:
        """Erase all persistent bot data that is scoped to one server."""
        with self._lock, self._managed_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._bump_generation(db, f"memory_server_epoch:{server_id}")
            cursor = db.execute(
                "DELETE FROM messages WHERE server_id = ?", (server_id,)
            )
            db.execute(
                "DELETE FROM app_settings WHERE key = ?",
                (f"response_language:guild:{server_id}",),
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
