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
                CREATE TABLE IF NOT EXISTS memory_scopes (
                    scope_id TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS active_channels (
                    scope_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    message_count INTEGER NOT NULL DEFAULT 0,
                    gifs_enabled INTEGER NOT NULL DEFAULT 0 CHECK (gifs_enabled IN (0, 1)),
                    gif_message_count INTEGER NOT NULL DEFAULT 0,
                    topic TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
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
            if "server_id" not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN server_id TEXT NOT NULL DEFAULT ''")
            if "generation" not in message_columns:
                db.execute("ALTER TABLE messages ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
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
            memory_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(conversation_memory)").fetchall()
            }
            if "server_id" not in memory_columns:
                db.execute(
                    "ALTER TABLE conversation_memory ADD COLUMN server_id TEXT NOT NULL DEFAULT ''"
                )
            if "generation" not in memory_columns:
                db.execute(
                    "ALTER TABLE conversation_memory ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
                )
            channel_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(active_channels)").fetchall()
            }
            if "gifs_enabled" not in channel_columns:
                db.execute(
                    "ALTER TABLE active_channels "
                    "ADD COLUMN gifs_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "gif_message_count" not in channel_columns:
                db.execute(
                    "ALTER TABLE active_channels "
                    "ADD COLUMN gif_message_count INTEGER NOT NULL DEFAULT 0"
                )
            if "topic" not in channel_columns:
                db.execute(
                    "ALTER TABLE active_channels ADD COLUMN topic TEXT NOT NULL DEFAULT ''"
                )
            db.execute(
                """
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(model_id, scope_id, user_id, server_id, generation, id)
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def register_scope(self, scope_id: str, server_id: str) -> int:
        """Associate a Discord channel with its guild and return its wipe generation."""
        with self._lock, self._managed_connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO memory_scopes (scope_id, server_id) VALUES (?, ?)",
                (scope_id, server_id),
            )
            row = db.execute(
                "SELECT server_id, generation FROM memory_scopes WHERE scope_id = ?", (scope_id,)
            ).fetchone()
            if row is None or str(row["server_id"]) != server_id:
                raise ValueError("memory scope is already associated with another server")
            return int(row["generation"])

    def erase_server_memory(self, server_id: str) -> int:
        """Atomically delete a guild's memory and invalidate in-flight writers."""
        with self._lock, self._managed_connection() as db:
            db.execute(
                "UPDATE memory_scopes SET generation = generation + 1 WHERE server_id = ?",
                (server_id,),
            )
            messages = db.execute(
                """
                DELETE FROM messages
                WHERE server_id = ? OR (
                    server_id = '' AND scope_id IN (
                        SELECT scope_id FROM memory_scopes WHERE server_id = ?
                    )
                )
                """,
                (server_id, server_id),
            ).rowcount
            memories = db.execute(
                """
                DELETE FROM conversation_memory
                WHERE server_id = ? OR (
                    server_id = '' AND scope_id IN (
                        SELECT scope_id FROM memory_scopes WHERE server_id = ?
                    )
                )
                """,
                (server_id, server_id),
            ).rowcount
            return messages + memories

    def server_scopes(self, server_id: str) -> set[str]:
        with self._lock, self._managed_connection() as db:
            rows = db.execute(
                "SELECT scope_id FROM memory_scopes WHERE server_id = ?", (server_id,)
            ).fetchall()
        return {str(row["scope_id"]) for row in rows}

    def scope_generation(self, scope_id: str) -> int | None:
        """Return the current guild wipe generation, if this is a guild scope."""
        with self._lock, self._managed_connection() as db:
            row = db.execute(
                "SELECT generation FROM memory_scopes WHERE scope_id = ?", (scope_id,)
            ).fetchone()
            return None if row is None else int(row["generation"])

    def set_active_mode(self, scope_id: str, enabled: bool) -> None:
        """Persist active-member mode for a channel and reset its cadence."""
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
        """Return whether active-member mode is enabled and its current count."""
        with self._lock, self._managed_connection() as db:
            row = db.execute(
                "SELECT enabled, message_count FROM active_channels WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
        if row is None:
            return False, 0
        return bool(row["enabled"]), int(row["message_count"])

    def record_active_message(self, scope_id: str, *, interval: int = 6) -> bool:
        """Count one channel message and claim every ``interval``th response."""
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

    def set_gif_mode(self, scope_id: str, enabled: bool) -> None:
        """Persist automatic GIF mode for a channel and reset its cadence."""
        with self._lock, self._managed_connection() as db:
            db.execute(
                """
                INSERT INTO active_channels
                    (scope_id, gifs_enabled, gif_message_count, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    gifs_enabled = excluded.gifs_enabled,
                    gif_message_count = 0,
                    updated_at = excluded.updated_at
                """,
                (scope_id, int(enabled), time.time()),
            )

    def gif_mode_status(self, scope_id: str) -> tuple[bool, int]:
        """Return whether GIF mode is enabled and its current message count."""
        with self._lock, self._managed_connection() as db:
            row = db.execute(
                "SELECT gifs_enabled, gif_message_count "
                "FROM active_channels WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
        if row is None:
            return False, 0
        return bool(row["gifs_enabled"]), int(row["gif_message_count"])

    def record_gif_message(self, scope_id: str, *, interval: int = 10) -> bool:
        """Count one channel message and claim every ``interval``th GIF response."""
        if interval < 1:
            raise ValueError("interval must be at least 1")
        with self._lock, self._managed_connection() as db:
            cursor = db.execute(
                """
                UPDATE active_channels
                SET gif_message_count = (gif_message_count + 1) % ?, updated_at = ?
                WHERE scope_id = ? AND gifs_enabled = 1
                """,
                (interval, time.time(), scope_id),
            )
            if cursor.rowcount != 1:
                return False
            row = db.execute(
                "SELECT gif_message_count FROM active_channels WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
            return row is not None and int(row["gif_message_count"]) == 0

    def set_topic(self, scope_id: str, topic: str | None) -> None:
        """Set or clear a channel-wide topic lock."""
        with self._lock, self._managed_connection() as db:
            db.execute(
                """
                INSERT INTO active_channels (scope_id, topic, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    topic = excluded.topic,
                    updated_at = excluded.updated_at
                """,
                (scope_id, topic or "", time.time()),
            )

    def channel_topic(self, scope_id: str) -> str:
        """Return the configured topic lock, or an empty string when disabled."""
        with self._lock, self._managed_connection() as db:
            row = db.execute(
                "SELECT topic FROM active_channels WHERE scope_id = ?", (scope_id,)
            ).fetchone()
        return "" if row is None else str(row["topic"])

    @staticmethod
    def _scope_context(db: sqlite3.Connection, scope_id: str) -> tuple[str, int]:
        row = db.execute(
            "SELECT server_id, generation FROM memory_scopes WHERE scope_id = ?", (scope_id,)
        ).fetchone()
        if row is None:
            # Direct messages retain their existing, non-guild-scoped behavior.
            return "", 0
        return str(row["server_id"]), int(row["generation"])

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
        expected_generation: int | None = None,
    ) -> bool:
        payload = json.dumps(attachments or [], ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._managed_connection() as db:
            server_id, generation = self._scope_context(db, scope_id)
            if expected_generation is not None and generation != expected_generation:
                return False
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO messages
                    (event_id, model_id, scope_id, user_id, role, content, attachments_json, accepted, created_at, server_id, generation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    server_id,
                    generation,
                ),
            )
            return cursor.rowcount == 1

    def recent_messages(
        self, scope_id: str, user_id: str, *, limit: int, model_id: str = ""
    ) -> list[dict[str, Any]]:
        with self._lock, self._managed_connection() as db:
            server_id, generation = self._scope_context(db, scope_id)
            rows = db.execute(
                """
                SELECT id, role, content, attachments_json, created_at
                FROM messages
                WHERE model_id = ? AND scope_id = ? AND user_id = ? AND accepted = 1
                  AND server_id = ? AND generation = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (model_id, scope_id, user_id, server_id, generation, limit),
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
            server_id, generation = self._scope_context(db, scope_id)
            cutoff = db.execute(
                """
                SELECT id FROM messages
                WHERE model_id = ? AND scope_id = ? AND user_id = ? AND accepted = 1
                  AND server_id = ? AND generation = ?
                ORDER BY id DESC LIMIT 1 OFFSET ?
                """,
                (model_id, scope_id, user_id, server_id, generation, keep_recent),
            ).fetchone()
            if cutoff is None:
                return []
            rows = db.execute(
                """
                SELECT id, role, content, attachments_json, created_at
                FROM messages
                WHERE model_id = ? AND scope_id = ? AND user_id = ?
                  AND accepted = 1 AND id > ? AND id <= ?
                  AND server_id = ? AND generation = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (model_id, scope_id, user_id, summarized_through, int(cutoff["id"]), server_id, generation, limit),
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
            server_id, generation = self._scope_context(db, scope_id)
            row = db.execute(
                """
                SELECT summary, facts_json, summarized_through_id
                FROM conversation_memory
                WHERE model_id = ? AND scope_id = ? AND user_id = ? AND accepted_context = 1
                  AND server_id = ? AND generation = ?
                """,
                (model_id, scope_id, user_id, server_id, generation),
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
        expected_generation: int | None = None,
    ) -> None:
        clean_facts = list(dict.fromkeys(fact.strip() for fact in facts if fact.strip()))[:50]
        with self._lock, self._managed_connection() as db:
            server_id, generation = self._scope_context(db, scope_id)
            if expected_generation is not None and generation != expected_generation:
                return
            db.execute(
                """
                INSERT INTO conversation_memory
                    (model_id, scope_id, user_id, summary, facts_json, summarized_through_id,
                     accepted_context, updated_at, server_id, generation)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(model_id, scope_id, user_id) DO UPDATE SET
                    summary = excluded.summary,
                    facts_json = excluded.facts_json,
                    accepted_context = 1,
                    summarized_through_id = MAX(
                        conversation_memory.summarized_through_id,
                        excluded.summarized_through_id
                    ),
                    updated_at = excluded.updated_at
                    , server_id = excluded.server_id
                    , generation = excluded.generation
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
                    server_id,
                    generation,
                ),
            )

    def prune_older_than(self, cutoff_timestamp: float) -> int:
        """Delete raw messages older than the configured retention period."""
        with self._lock, self._managed_connection() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE created_at < ?", (cutoff_timestamp,)
            )
            return cursor.rowcount
