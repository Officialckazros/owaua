"""SQLite persistence — the bot's growing brain.

Tables
------
memories      : facts about a subject (user id or 'server')
lessons       : behavioral guidance from feedback
feedback      : raw up/down + corrections
commands      : community prompt-defined commands
interactions  : stats + skill level
kv            : misc key/value (mood, lurk timers, etc.)
relationships : per-user bond score, nickname, grudge
conversations : short-term user↔bot turns
quotes        : hall of shame / saved lines
guild_settings: per-server config (persona, lurk, language, etc.)
"""
import json
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

from sefbot import config, profile_search
from sefbot.module_catalog import default_server_settings, merge_server_settings
from sefbot.scope import is_dm_scope, is_guild_scope

LATEST_SCHEMA_VERSION = 9
MAX_RETENTION_DAYS = 30
_TOS_NETWORK_RETENTION_SECONDS = MAX_RETENTION_DAYS * 86_400
_db_lock = threading.RLock()


class _SerializedConnection(sqlite3.Connection):
    """Serialize access while legacy synchronous callers are migrated.

    sqlite is safe with WAL, but one ``check_same_thread=False`` connection is
    not safe to use concurrently without application-level serialization.
    Important multi-statement operations below additionally hold ``_db_lock``
    for their complete transaction.
    """

    def execute(self, *args, **kwargs):
        with _db_lock:
            return super().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with _db_lock:
            return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with _db_lock:
            return super().executescript(*args, **kwargs)

    def commit(self):
        with _db_lock:
            return super().commit()

    def rollback(self):
        with _db_lock:
            return super().rollback()


_conn: Optional[sqlite3.Connection] = None

_gs_cache: dict = {}
_GS_TTL = 5.0
_lessons_cache: Optional[dict[str, list]] = None
_lessons_ts: float = 0.0
_LESSONS_TTL = 30.0
_mem_cache: dict = {}
_MEM_TTL = 15.0
_MEM_CACHE_MAX = 256


def _mem_cache_set(key, rows) -> None:
    if len(_mem_cache) >= _MEM_CACHE_MAX:
        _mem_cache.clear()
    _mem_cache[key] = (time.time(), rows)


def _mem_cache_get(key):
    hit = _mem_cache.get(key)
    if hit is not None and time.time() - hit[0] < _MEM_TTL:
        return hit[1]
    return None


def _invalidate_memory_cache(*, subject: str | None = None, scope_id: str | None = None) -> None:
    """Evict every cache entry that could contain a mutated memory."""
    for key in list(_mem_cache):
        kind = key[0] if key else None
        if kind == "about" and subject is not None and key[1] != subject:
            continue
        if scope_id is not None:
            cached_scope = key[2] if kind == "about" else key[1] if kind == "scope" else None
            if cached_scope != scope_id:
                continue
        _mem_cache.pop(key, None)


SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL DEFAULT 'server',
    content    TEXT NOT NULL,
    author     TEXT,
    guild_id   TEXT,
    importance REAL DEFAULT 0.5,
    created    REAL NOT NULL,
    updated    REAL
);
CREATE TABLE IF NOT EXISTS lessons (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    content   TEXT NOT NULL UNIQUE,
    source    TEXT,
    created   REAL NOT NULL,
    scope_id  TEXT,
    enabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_msg   TEXT,
    bot_msg    TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    note       TEXT,
    author     TEXT,
    processed  INTEGER DEFAULT 0,
    created    REAL NOT NULL,
    scope_id   TEXT
);
CREATE TABLE IF NOT EXISTS commands (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    behavior    TEXT NOT NULL,
    author      TEXT,
    guild_id    TEXT,
    uses        INTEGER DEFAULT 0,
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS interactions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,
    author   TEXT,
    guild_id TEXT,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS relationships (
    user_id    TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    score      REAL NOT NULL DEFAULT 0.0,
    nickname   TEXT,
    grudge     TEXT,
    bond_label TEXT DEFAULT 'stranger',
    updated    REAL NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);
CREATE TABLE IF NOT EXISTS conversations (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    role     TEXT NOT NULL,
    content  TEXT NOT NULL,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    text     TEXT NOT NULL,
    about    TEXT,
    author   TEXT,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    data     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       TEXT UNIQUE,
    guild_id         TEXT NOT NULL,
    guild_name       TEXT,
    channel_id       TEXT NOT NULL,
    channel_name     TEXT,
    user_id          TEXT NOT NULL,
    username         TEXT NOT NULL,
    display_name     TEXT,
    content          TEXT NOT NULL,
    has_bad_words    INTEGER DEFAULT 0,
    bad_words_found  TEXT,
    created          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS guild_archive_channels (
    guild_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    channel_name    TEXT,
    last_message_id TEXT,
    messages_seen   INTEGER NOT NULL DEFAULT 0,
    complete        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    updated         REAL NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS privacy_consents (
    user_id    TEXT NOT NULL,
    scope_id   TEXT NOT NULL,
    opted_in   INTEGER NOT NULL DEFAULT 0,
    updated    REAL NOT NULL,
    PRIMARY KEY (user_id, scope_id)
);
CREATE TABLE IF NOT EXISTS tos_acceptance_challenges (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    version    TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tos_acceptances (
    user_id         TEXT PRIMARY KEY,
    version         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('accepted', 'review', 'rejected')),
    network_hash    TEXT NOT NULL DEFAULT '',
    network_seen_at REAL,
    risk_code       TEXT NOT NULL DEFAULT '',
    submitted_at    REAL NOT NULL,
    reviewed_at     REAL
);
CREATE TABLE IF NOT EXISTS action_audit (
    nonce          TEXT PRIMARY KEY,
    actor_id       TEXT NOT NULL,
    scope_id       TEXT NOT NULL,
    action         TEXT NOT NULL,
    target_id      TEXT,
    parameters     TEXT NOT NULL DEFAULT '{}',
    source         TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    status         TEXT NOT NULL,
    result         TEXT,
    created        REAL NOT NULL,
    completed      REAL
);
CREATE TABLE IF NOT EXISTS assistant_action_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id     TEXT NOT NULL,
    scope_id     TEXT NOT NULL,
    channel_id   TEXT,
    action       TEXT NOT NULL,
    target_id    TEXT,
    parameters   TEXT NOT NULL DEFAULT '{}',
    result       TEXT NOT NULL,
    inverse      TEXT,
    source_nonce TEXT NOT NULL UNIQUE,
    created      REAL NOT NULL,
    consumed     REAL
);
CREATE INDEX IF NOT EXISTS idx_assistant_action_lookup
ON assistant_action_history(actor_id,scope_id,created DESC);
CREATE TABLE IF NOT EXISTS economy_accounts (
    user_id    TEXT PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
    deposit    INTEGER NOT NULL DEFAULT 0 CHECK(deposit >= 0),
    gems       INTEGER NOT NULL DEFAULT 0 CHECK(gems >= 0),
    updated    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS work_cooldowns (
    user_id    TEXT PRIMARY KEY,
    last_work  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state_migrations (
    name        TEXT PRIMARY KEY,
    migrated_at REAL NOT NULL,
    details     TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dynamic_blocks (
    user_id      TEXT PRIMARY KEY,
    block_source TEXT NOT NULL CHECK(block_source IN ('manual', 'tos', 'other')),
    metadata     TEXT NOT NULL,
    blocked_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dm_contacts (
    user_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cli_active_conversations (
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    heartbeat  REAL NOT NULL,
    PRIMARY KEY (user_id, session_id)
);
CREATE TABLE IF NOT EXISTS user_levels (
    user_id   TEXT NOT NULL,
    guild_id  TEXT NOT NULL,
    xp        INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
    level     INTEGER NOT NULL DEFAULT 0 CHECK(level >= 0),
    messages  INTEGER NOT NULL DEFAULT 0 CHECK(messages >= 0),
    last_xp   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id)
);
CREATE TABLE IF NOT EXISTS daily_claims (
    user_id     TEXT NOT NULL,
    guild_id    TEXT NOT NULL,
    last_claim  REAL NOT NULL DEFAULT 0,
    streak      INTEGER NOT NULL DEFAULT 0 CHECK(streak >= 0),
    PRIMARY KEY (user_id, guild_id)
);
CREATE TABLE IF NOT EXISTS module_settings (
    guild_id  TEXT NOT NULL,
    module    TEXT NOT NULL,
    enabled   INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
    data      TEXT NOT NULL DEFAULT '{}',
    updated   REAL NOT NULL,
    actor_id  TEXT,
    PRIMARY KEY (guild_id, module)
);
CREATE TABLE IF NOT EXISTS swear_jar_counts (
    guild_id TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0 CHECK(count >= 0),
    updated  REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS booster_members (
    guild_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    current_boosts  INTEGER NOT NULL DEFAULT 0 CHECK(current_boosts >= 0),
    all_time_boosts INTEGER NOT NULL DEFAULT 0 CHECK(all_time_boosts >= 0),
    active          INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
    first_boosted   REAL,
    last_boosted    REAL,
    stopped         REAL,
    last_source     TEXT NOT NULL DEFAULT '',
    updated         REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS booster_events (
    guild_id  TEXT NOT NULL,
    event_id  TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    created   REAL NOT NULL,
    PRIMARY KEY (guild_id, event_id)
);
CREATE TABLE IF NOT EXISTS dashboard_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   TEXT NOT NULL,
    actor_id   TEXT NOT NULL,
    action     TEXT NOT NULL,
    module     TEXT,
    detail     TEXT NOT NULL DEFAULT '{}',
    created    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS community_records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    user_id    TEXT,
    record_key TEXT,
    data       TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'active',
    due        REAL,
    created    REAL NOT NULL,
    updated    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS afk_statuses (
    guild_id      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    reason        TEXT NOT NULL,
    original_nick TEXT,
    notify_return INTEGER NOT NULL DEFAULT 1 CHECK(notify_return IN (0, 1)),
    created       REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS afk_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    author_id   TEXT NOT NULL,
    channel_id  TEXT,
    message_id  TEXT,
    content     TEXT NOT NULL,
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_convo_user ON conversations(user_id, guild_id, created);
CREATE INDEX IF NOT EXISTS idx_quotes_guild ON quotes(guild_id);
CREATE INDEX IF NOT EXISTS idx_msg_guild_user ON server_messages(guild_id, user_id, created);
CREATE INDEX IF NOT EXISTS idx_msg_user ON server_messages(user_id, created);
CREATE INDEX IF NOT EXISTS idx_msg_bad ON server_messages(guild_id, has_bad_words);
CREATE INDEX IF NOT EXISTS idx_msg_created ON server_messages(created);
CREATE INDEX IF NOT EXISTS idx_tos_challenge_user
    ON tos_acceptance_challenges(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_tos_acceptance_network
    ON tos_acceptances(network_hash, network_seen_at);
CREATE INDEX IF NOT EXISTS idx_tos_acceptance_review
    ON tos_acceptances(status, submitted_at);
CREATE INDEX IF NOT EXISTS idx_archive_updated
    ON guild_archive_channels(guild_id, updated DESC);
CREATE INDEX IF NOT EXISTS idx_cli_active_heartbeat
    ON cli_active_conversations(heartbeat);
CREATE INDEX IF NOT EXISTS idx_levels_guild_xp ON user_levels(guild_id, xp DESC);
CREATE INDEX IF NOT EXISTS idx_module_settings_guild ON module_settings(guild_id);
CREATE INDEX IF NOT EXISTS idx_swear_jar_user ON swear_jar_counts(user_id);
CREATE INDEX IF NOT EXISTS idx_booster_members_active
    ON booster_members(guild_id, active, updated DESC);
CREATE INDEX IF NOT EXISTS idx_booster_members_user ON booster_members(user_id);
CREATE INDEX IF NOT EXISTS idx_booster_events_created ON booster_events(created);
CREATE INDEX IF NOT EXISTS idx_dashboard_audit_guild
    ON dashboard_audit(guild_id, created DESC);
CREATE INDEX IF NOT EXISTS idx_community_records_lookup
    ON community_records(guild_id, kind, status, due);
CREATE INDEX IF NOT EXISTS idx_community_records_user
    ON community_records(user_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_afk_notes_target ON afk_notes(guild_id, target_id, created);
"""

_WORD = re.compile(r"[a-z0-9]{3,}")


def _table_columns(c: sqlite3.Connection, table: str) -> set[str]:
    return {str(r["name"]) for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _canonical_scope(raw: object, fallback_user: object | None = None) -> Optional[str]:
    value = str(raw or "").strip()
    if is_guild_scope(value) or is_dm_scope(value):
        return value
    if value.isdigit():
        return f"guild:{value}"
    if value == "dm" and str(fallback_user or "").isdigit():
        return f"dm:{fallback_user}"
    return None


def _logical_backup(c: sqlite3.Connection) -> None:
    """Back up only data the privacy migration is allowed to preserve."""
    if config.DB_PATH == ":memory:":
        return
    target = Path(f"{config.DB_PATH}.migration-preserved.json")
    payload = {
        "schema": 1,
        "created": time.time(),
        "memories": [dict(r) for r in c.execute("SELECT * FROM memories").fetchall()],
        "guild_settings": [dict(r) for r in c.execute("SELECT * FROM guild_settings").fetchall()],
        "relationships": [dict(r) for r in c.execute("SELECT * FROM relationships").fetchall()],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        os.chmod(target, 0o600)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _tighten_database_permissions() -> None:
    """Keep the database and SQLite sidecars owner-readable only."""
    if config.DB_PATH == ":memory:":
        return
    base = Path(config.DB_PATH)
    for candidate in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                os.chmod(candidate, 0o600)
        except OSError:
            # Startup validation/integrity still decides whether the database
            # is usable; permission warnings are handled by config startup.
            pass


def _rescope_legacy_rows(c: sqlite3.Connection) -> None:
    # Explicit memories can be safely assigned only when a guild id or DM
    # subject/author identifies their original tenant.
    for row in c.execute("SELECT id,guild_id,author,subject FROM memories").fetchall():
        fallback = row["subject"] if str(row["subject"] or "").isdigit() else row["author"]
        scope = _canonical_scope(row["guild_id"], fallback)
        if scope:
            c.execute("UPDATE memories SET guild_id=? WHERE id=?", (scope, row["id"]))
        else:
            c.execute("DELETE FROM memories WHERE id=?", (row["id"],))

    for row in c.execute(
        "SELECT user_id,guild_id FROM relationships"
    ).fetchall():
        original_scope = row["guild_id"]
        scope = _canonical_scope(original_scope, row["user_id"])
        if scope:
            c.execute(
                "UPDATE relationships SET guild_id=? WHERE user_id=? AND guild_id=?",
                (scope, row["user_id"], original_scope),
            )
        else:
            c.execute(
                "DELETE FROM relationships WHERE user_id=? AND guild_id=?",
                (row["user_id"], original_scope),
            )

    for row in c.execute("SELECT id,guild_id,user_id FROM conversations").fetchall():
        scope = _canonical_scope(row["guild_id"], row["user_id"])
        if scope:
            c.execute(
                "UPDATE conversations SET guild_id=? WHERE id=?",
                (scope, row["id"]),
            )
        else:
            c.execute("DELETE FROM conversations WHERE id=?", (row["id"],))

    for row in c.execute("SELECT id,guild_id,author FROM interactions").fetchall():
        scope = _canonical_scope(row["guild_id"], row["author"])
        if scope:
            c.execute(
                "UPDATE interactions SET guild_id=? WHERE id=?",
                (scope, row["id"]),
            )
        else:
            c.execute("DELETE FROM interactions WHERE id=?", (row["id"],))

    for row in c.execute("SELECT id,guild_id FROM quotes").fetchall():
        scope = _canonical_scope(row["guild_id"])
        if scope and is_guild_scope(scope):
            c.execute("UPDATE quotes SET guild_id=? WHERE id=?", (scope, row["id"]))
        else:
            c.execute("DELETE FROM quotes WHERE id=?", (row["id"],))

    for row in c.execute("SELECT guild_id,data FROM guild_settings").fetchall():
        scope = _canonical_scope(row["guild_id"])
        if not scope or not is_guild_scope(scope):
            c.execute("DELETE FROM guild_settings WHERE guild_id=?", (row["guild_id"],))
            continue
        if scope != row["guild_id"]:
            c.execute(
                "INSERT OR REPLACE INTO guild_settings(guild_id,data) VALUES(?,?)",
                (scope, row["data"]),
            )
            c.execute("DELETE FROM guild_settings WHERE guild_id=?", (row["guild_id"],))

    # Command names used to be global primary keys.  Rebuild with a composite
    # tenant key and keep ambiguous rows disabled for explicit owner review.
    c.execute("DROP TABLE IF EXISTS commands_v3")
    c.execute(
        "CREATE TABLE commands_v3 ("
        "scope_id TEXT NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL,"
        "behavior TEXT NOT NULL,author TEXT,uses INTEGER DEFAULT 0,created REAL NOT NULL,"
        "enabled INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(scope_id,name))"
    )
    for row in c.execute("SELECT * FROM commands").fetchall():
        scope = _canonical_scope(row["guild_id"], row["author"])
        enabled = 1 if scope else 0
        c.execute(
            "INSERT OR REPLACE INTO commands_v3 VALUES(?,?,?,?,?,?,?,?)",
            (
                scope or "legacy:disabled",
                row["name"], row["description"], row["behavior"], row["author"],
                row["uses"], row["created"], enabled,
            ),
        )
    c.execute("DROP TABLE commands")
    c.execute("ALTER TABLE commands_v3 RENAME TO commands")


def _migrate(c: sqlite3.Connection) -> None:
    """Transactional, versioned migrations for existing deployments."""
    with _db_lock:
        version = int(c.execute("PRAGMA user_version").fetchone()[0])
        try:
            c.execute("BEGIN IMMEDIATE")
            cols = _table_columns(c, "memories")
            if "subject" not in cols:
                c.execute("ALTER TABLE memories ADD COLUMN subject TEXT NOT NULL DEFAULT 'server'")
            if "importance" not in cols:
                c.execute("ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 0.5")
            if "updated" not in cols:
                c.execute("ALTER TABLE memories ADD COLUMN updated REAL")

            lesson_cols = _table_columns(c, "lessons")
            if "scope_id" not in lesson_cols:
                c.execute("ALTER TABLE lessons ADD COLUMN scope_id TEXT")
            if "enabled" not in lesson_cols:
                c.execute("ALTER TABLE lessons ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0")
            feedback_cols = _table_columns(c, "feedback")
            if "scope_id" not in feedback_cols:
                c.execute("ALTER TABLE feedback ADD COLUMN scope_id TEXT")

            economy_cols = _table_columns(c, "economy_accounts")
            if "gems" not in economy_cols:
                c.execute(
                    "ALTER TABLE economy_accounts ADD COLUMN gems INTEGER "
                    "NOT NULL DEFAULT 0 CHECK(gems >= 0)"
                )

            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject,guild_id)"
            )

            # Version 3 was the destructive privacy cut-over.  Later schema
            # additions must never replay it: commands no longer even have
            # the legacy ``guild_id`` column after this migration.
            if version < 3:
                _logical_backup(c)
                _rescope_legacy_rows(c)
                # The user explicitly selected a clean privacy cut-over.
                c.execute("DELETE FROM server_messages")
                c.execute("UPDATE lessons SET enabled=0 WHERE scope_id IS NULL OR scope_id='' ")

            c.execute("CREATE INDEX IF NOT EXISTS idx_feedback_scope ON feedback(scope_id,author)")
            c.execute("DROP TABLE IF EXISTS user_geo")
            c.execute("DROP TABLE IF EXISTS geo_tokens")
            if version < 8:
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS server_messages_fts "
                    "USING fts5(content, content='server_messages', content_rowid='id', "
                    "tokenize='unicode61')"
                )
                c.execute(
                    "CREATE TRIGGER IF NOT EXISTS server_messages_fts_insert "
                    "AFTER INSERT ON server_messages BEGIN "
                    "INSERT INTO server_messages_fts(rowid,content) "
                    "VALUES (new.id,new.content); END"
                )
                c.execute(
                    "CREATE TRIGGER IF NOT EXISTS server_messages_fts_delete "
                    "AFTER DELETE ON server_messages BEGIN "
                    "INSERT INTO server_messages_fts(server_messages_fts,rowid,content) "
                    "VALUES ('delete',old.id,old.content); END"
                )
                c.execute(
                    "CREATE TRIGGER IF NOT EXISTS server_messages_fts_update "
                    "AFTER UPDATE OF content ON server_messages BEGIN "
                    "INSERT INTO server_messages_fts(server_messages_fts,rowid,content) "
                    "VALUES ('delete',old.id,old.content); "
                    "INSERT INTO server_messages_fts(rowid,content) "
                    "VALUES (new.id,new.content); END"
                )
                c.execute(
                    "INSERT INTO server_messages_fts(server_messages_fts) VALUES('rebuild')"
                )
            c.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
            c.commit()
            c.execute("PRAGMA optimize")
        except Exception:
            c.rollback()
            raise


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _db_lock:
        if _conn is not None:
            return _conn
        db_path = Path(config.DB_PATH)
        if config.DB_PATH != ":memory:":
            # A newly created private state directory must not inherit a
            # permissive process umask. Existing directories are never chmod'd
            # here because the configured database may intentionally live in
            # a repository or another shared parent.
            db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        candidate = sqlite3.connect(
            config.DB_PATH,
            check_same_thread=False,
            timeout=30.0,
            factory=_SerializedConnection,
        )
        try:
            candidate.row_factory = sqlite3.Row
            candidate.execute("PRAGMA journal_mode=WAL;")
            candidate.execute("PRAGMA synchronous=NORMAL;")
            candidate.execute("PRAGMA busy_timeout=30000;")
            candidate.execute("PRAGMA foreign_keys=ON;")
            candidate.executescript(SCHEMA)
            candidate.commit()
            _migrate(candidate)
        except Exception:
            candidate.close()
            raise
        _conn = candidate
        if config.DB_PATH != ":memory:":
            _tighten_database_permissions()
    return _conn


def close() -> None:
    """Close the process-owned database connection."""
    global _conn
    with _db_lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        _gs_cache.clear()
        _mem_cache.clear()


def integrity_check() -> None:
    """Fail startup if SQLite reports corruption."""
    rows = conn().execute("PRAGMA integrity_check").fetchall()
    results = [str(row[0]) for row in rows]
    if results != ["ok"]:
        raise RuntimeError("database integrity check failed")


def _json_dict(raw: object) -> dict:
    """Decode an object stored in SQLite without trusting its shape."""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _migration_exists(c: sqlite3.Connection, name: str) -> bool:
    return c.execute(
        "SELECT 1 FROM state_migrations WHERE name=?", (str(name),)
    ).fetchone() is not None


def legacy_state_migrated(name: str) -> bool:
    """Return whether a named one-shot legacy state import has committed."""
    return _migration_exists(conn(), str(name))


def import_legacy_dynamic_blocks(
    migration_name: str,
    records: list[tuple[str, str, dict]],
) -> bool:
    """Import legacy block records once, without replacing live SQLite data.

    Returns ``True`` only for the process that committed the migration marker.
    The marker and all imported rows are in the same transaction, which makes
    concurrent bot/CLI startup safe and keeps a partial import from becoming
    authoritative.
    """
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            if _migration_exists(c, migration_name):
                c.rollback()
                return False
            imported = 0
            for user_id, block_source, metadata in records:
                source = block_source if block_source in {"manual", "tos", "other"} else "other"
                clean = dict(metadata) if isinstance(metadata, dict) else {}
                blocked_at = _safe_timestamp(clean.get("blocked_at"), now())
                updated_at = _safe_timestamp(clean.get("updated_at"), blocked_at)
                clean["blocked_at"] = blocked_at
                clean["updated_at"] = updated_at
                clean["source"] = source
                cur = c.execute(
                    "INSERT OR IGNORE INTO dynamic_blocks"
                    "(user_id,block_source,metadata,blocked_at,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        str(user_id), source,
                        json.dumps(clean, ensure_ascii=False, sort_keys=True),
                        blocked_at, updated_at,
                    ),
                )
                imported += max(0, int(cur.rowcount))
            c.execute(
                "INSERT INTO state_migrations(name,migrated_at,details) VALUES(?,?,?)",
                (
                    str(migration_name), now(),
                    json.dumps({"records": imported}, sort_keys=True),
                ),
            )
            c.commit()
            return True
        except Exception:
            c.rollback()
            raise


def dynamic_block_apply(
    user_id: str,
    *,
    block_source: str,
    fields: dict,
    history_event: dict,
) -> bool:
    """Atomically create/update one dynamic block and append its history.

    Manual blocks dominate automatic ToS blocks.  An automatic event may be
    recorded against a manually blocked account, but it cannot silently turn
    that entry into a ToS block that the ToS-review CLI is allowed to remove.
    """
    uid = str(user_id)
    incoming_source = (
        block_source if block_source in {"manual", "tos", "other"} else "other"
    )
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT block_source,metadata,blocked_at FROM dynamic_blocks "
                "WHERE user_id=?",
                (uid,),
            ).fetchone()
            timestamp = now()
            newly_blocked = row is None
            if row is None:
                source = incoming_source
                metadata: dict = {}
                blocked_at = timestamp
            else:
                old_source = str(row["block_source"] or "other")
                source = (
                    "manual"
                    if "manual" in {old_source, incoming_source}
                    else incoming_source
                )
                metadata = _json_dict(row["metadata"])
                blocked_at = _safe_timestamp(row["blocked_at"], timestamp)

            # Never let an automatic update rewrite the visible reason/source
            # of an existing manual block.  It is still retained in history.
            preserve_manual = (
                row is not None
                and str(row["block_source"] or "") == "manual"
                and incoming_source != "manual"
            )
            if not preserve_manual:
                for key, value in fields.items():
                    if value not in (None, "") or key in {"reason", "offending_text"}:
                        metadata[str(key)] = value

            history = metadata.get("history")
            if not isinstance(history, list):
                history = []
            event = dict(history_event) if isinstance(history_event, dict) else {}
            event["block_source"] = incoming_source
            history.append(event)
            metadata["history"] = history[-10:]
            metadata["blocked_at"] = blocked_at
            metadata["updated_at"] = timestamp
            metadata["source"] = source

            c.execute(
                "INSERT INTO dynamic_blocks"
                "(user_id,block_source,metadata,blocked_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "block_source=excluded.block_source,metadata=excluded.metadata,"
                "blocked_at=excluded.blocked_at,updated_at=excluded.updated_at",
                (
                    uid, source,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    blocked_at, timestamp,
                ),
            )

            # A prepared alt may have accepted the Terms before this account
            # was blocked. Hold every recently matching accepted account for
            # owner review at the block boundary, regardless of account age.
            network = c.execute(
                "SELECT network_hash FROM tos_acceptances WHERE user_id=? "
                "AND network_hash!='' AND network_seen_at>=?",
                (uid, timestamp - _TOS_NETWORK_RETENTION_SECONDS),
            ).fetchone()
            if network is not None:
                peers = c.execute(
                    "SELECT user_id FROM tos_acceptances WHERE user_id!=? "
                    "AND status='accepted' AND network_hash=? AND network_seen_at>=?",
                    (
                        uid,
                        str(network["network_hash"]),
                        timestamp - _TOS_NETWORK_RETENTION_SECONDS,
                    ),
                ).fetchall()
                peer_ids = [str(peer["user_id"]) for peer in peers]
                if peer_ids:
                    c.executemany(
                        "UPDATE tos_acceptances SET status='review',"
                        "risk_code='blocked_network_match',submitted_at=?,reviewed_at=NULL "
                        "WHERE user_id=?",
                        [(timestamp, peer_id) for peer_id in peer_ids],
                    )
                    for peer_id in peer_ids:
                        c.execute(
                            "INSERT INTO kv(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"uf:{peer_id}:tos_accepted", ""),
                        )
                        c.execute(
                            "INSERT INTO kv(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (f"uf:{peer_id}:tos_review_pending", "1"),
                        )
            c.commit()
            return newly_blocked
        except Exception:
            c.rollback()
            raise


def dynamic_block_remove(
    user_id: str, *, expected_sources: set[str] | None = None
) -> bool:
    """Atomically remove a block, optionally only if its source still matches."""
    uid = str(user_id)
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            if expected_sources is None:
                cur = c.execute("DELETE FROM dynamic_blocks WHERE user_id=?", (uid,))
            else:
                allowed = {
                    value
                    for value in expected_sources
                    if value in {"manual", "tos", "other"}
                }
                if not allowed:
                    c.rollback()
                    return False
                row = c.execute(
                    "SELECT block_source FROM dynamic_blocks WHERE user_id=?",
                    (uid,),
                ).fetchone()
                if row is None or str(row["block_source"]) not in allowed:
                    c.rollback()
                    return False
                cur = c.execute(
                    "DELETE FROM dynamic_blocks WHERE user_id=? AND block_source=?",
                    (uid, str(row["block_source"])),
                )
            removed = int(cur.rowcount) > 0
            c.commit()
            return removed
        except Exception:
            c.rollback()
            raise


def dynamic_block_get(user_id: str) -> dict | None:
    row = conn().execute(
        "SELECT block_source,metadata,blocked_at,updated_at FROM dynamic_blocks "
        "WHERE user_id=?",
        (str(user_id),),
    ).fetchone()
    if row is None:
        return None
    metadata = _json_dict(row["metadata"])
    metadata["source"] = str(row["block_source"])
    metadata["blocked_at"] = float(row["blocked_at"])
    metadata["updated_at"] = float(row["updated_at"])
    return metadata


def dynamic_blocks_all() -> dict[str, dict]:
    rows = conn().execute(
        "SELECT user_id,block_source,metadata,blocked_at,updated_at "
        "FROM dynamic_blocks ORDER BY user_id"
    ).fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        metadata = _json_dict(row["metadata"])
        metadata["source"] = str(row["block_source"])
        metadata["blocked_at"] = float(row["blocked_at"])
        metadata["updated_at"] = float(row["updated_at"])
        result[str(row["user_id"])] = metadata
    return result


def _safe_timestamp(value: object, default: float) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if not (0 <= timestamp <= now() + 86_400):
        return float(default)
    return timestamp


def import_legacy_dm_contacts(
    migration_name: str, records: list[tuple[str, str, str]]
) -> bool:
    """Import legacy DM contacts exactly once without overwriting live rows."""
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            if _migration_exists(c, migration_name):
                c.rollback()
                return False
            imported = 0
            for user_id, name, last_message_at in records:
                cur = c.execute(
                    "INSERT OR IGNORE INTO dm_contacts"
                    "(user_id,name,last_message_at,updated_at) VALUES(?,?,?,?)",
                    (str(user_id), str(name), str(last_message_at), now()),
                )
                imported += max(0, int(cur.rowcount))
            c.execute(
                "INSERT INTO state_migrations(name,migrated_at,details) VALUES(?,?,?)",
                (
                    str(migration_name), now(),
                    json.dumps({"records": imported}, sort_keys=True),
                ),
            )
            c.commit()
            return True
        except Exception:
            c.rollback()
            raise


def dm_contacts_upsert(records: list[tuple[str, str, str]]) -> None:
    """Upsert contacts atomically, refusing to replace a newer contact row."""
    if not records:
        return
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            timestamp = now()
            c.executemany(
                "INSERT INTO dm_contacts(user_id,name,last_message_at,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "name=excluded.name,last_message_at=excluded.last_message_at,"
                "updated_at=excluded.updated_at "
                "WHERE excluded.last_message_at >= dm_contacts.last_message_at",
                [
                    (str(uid), str(name), str(last_message_at), timestamp)
                    for uid, name, last_message_at in records
                ],
            )
            c.commit()
        except Exception:
            c.rollback()
            raise


def dm_contacts_all() -> dict[str, dict]:
    rows = conn().execute(
        "SELECT user_id,name,last_message_at FROM dm_contacts "
        "ORDER BY last_message_at DESC,user_id"
    ).fetchall()
    return {
        str(row["user_id"]): {
            "name": str(row["name"]),
            "last_message_at": str(row["last_message_at"]),
        }
        for row in rows
    }


def import_legacy_cli_active(
    migration_name: str, records: list[tuple[str, str, float]]
) -> bool:
    """Import legacy active-chat heartbeats exactly once."""
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            if _migration_exists(c, migration_name):
                c.rollback()
                return False
            imported = 0
            for user_id, session_id, heartbeat in records:
                cur = c.execute(
                    "INSERT OR IGNORE INTO cli_active_conversations"
                    "(user_id,session_id,heartbeat) VALUES(?,?,?)",
                    (str(user_id), str(session_id), float(heartbeat)),
                )
                imported += max(0, int(cur.rowcount))
            c.execute(
                "INSERT INTO state_migrations(name,migrated_at,details) VALUES(?,?,?)",
                (
                    str(migration_name), now(),
                    json.dumps({"records": imported}, sort_keys=True),
                ),
            )
            c.commit()
            return True
        except Exception:
            c.rollback()
            raise


def cli_active_touch(user_id: str, session_id: str, heartbeat: float | None = None) -> None:
    timestamp = _safe_timestamp(heartbeat, now()) if heartbeat is not None else now()
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO cli_active_conversations(user_id,session_id,heartbeat) "
                "VALUES(?,?,?) ON CONFLICT(user_id,session_id) DO UPDATE SET "
                "heartbeat=excluded.heartbeat",
                (str(user_id), str(session_id), timestamp),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise


def cli_active_remove(user_id: str, session_id: str | None = None) -> bool:
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            if session_id is None:
                cur = c.execute(
                    "DELETE FROM cli_active_conversations WHERE user_id=?",
                    (str(user_id),),
                )
            else:
                cur = c.execute(
                    "DELETE FROM cli_active_conversations WHERE user_id=? AND session_id=?",
                    (str(user_id), str(session_id)),
                )
            removed = int(cur.rowcount) > 0
            c.commit()
            return removed
        except Exception:
            c.rollback()
            raise


def cli_active_is_claimed(
    user_id: str, *, ttl_seconds: float = 90.0, at: float | None = None
) -> bool:
    ttl = max(1.0, min(float(ttl_seconds), 3_600.0))
    cutoff = (now() if at is None else float(at)) - ttl
    row = conn().execute(
        "SELECT 1 FROM cli_active_conversations "
        "WHERE user_id=? AND heartbeat>=? LIMIT 1",
        (str(user_id), cutoff),
    ).fetchone()
    return row is not None


def cli_active_cleanup(*, ttl_seconds: float = 300.0) -> int:
    ttl = max(1.0, min(float(ttl_seconds), 86_400.0))
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "DELETE FROM cli_active_conversations WHERE heartbeat<?",
                (now() - ttl,),
            )
            removed = max(0, int(cur.rowcount))
            c.commit()
            return removed
        except Exception:
            c.rollback()
            raise


def now() -> float:
    return time.time()


def kv_get(key: str, default=None):
    row = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value) -> None:
    conn().execute(
        "INSERT INTO kv(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn().commit()


_DEFAULT_MOOD = {"label": "neutral", "intensity": 0.4, "valence": 0.0}


def mood_get(guild_id: str) -> dict:
    raw = kv_get(f"mood:{guild_id}")
    if not raw:
        return {**_DEFAULT_MOOD, "updated": now()}
    try:
        d = json.loads(raw)
        return {**_DEFAULT_MOOD, **d}
    except (ValueError, TypeError):
        return {**_DEFAULT_MOOD, "updated": now()}


def mood_set(guild_id: str, label: str, intensity: float, valence: float) -> None:
    kv_set(f"mood:{guild_id}", json.dumps({
        "label": str(label)[:24], "intensity": float(intensity),
        "valence": float(valence), "updated": now(),
    }))


def mood_nudge(guild_id: str, dv: float) -> None:
    d = mood_get(guild_id)
    v = max(-1.0, min(1.0, float(d.get("valence", 0.0)) + dv))
    mood_set(guild_id, d.get("label", "neutral"), d.get("intensity", 0.4), v)


_DEFAULT_GUILD = default_server_settings()


def _guild_settings_key(guild_id: str) -> str:
    raw = str(guild_id).strip()
    return f"guild:{raw}" if raw.isdigit() else raw


def guild_settings(guild_id: str) -> dict:
    guild_id = _guild_settings_key(guild_id)
    hit = _gs_cache.get(guild_id)
    if hit is not None and time.time() - hit[0] < _GS_TTL:
        return dict(hit[1])
    row = conn().execute(
        "SELECT data FROM guild_settings WHERE guild_id=?", (guild_id,)
    ).fetchone()
    if not row:
        d = dict(_DEFAULT_GUILD)
    else:
        try:
            d = merge_server_settings(json.loads(row["data"]))
        except (ValueError, TypeError):
            d = dict(_DEFAULT_GUILD)
    _gs_cache[guild_id] = (time.time(), d)
    return dict(d)


def guild_settings_set(guild_id: str, **patch) -> dict:
    guild_id = _guild_settings_key(guild_id)
    cur = merge_server_settings({**guild_settings(guild_id), **patch})
    c = conn()
    with _db_lock:
        c.execute(
            "INSERT INTO guild_settings(guild_id,data) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
            (guild_id, json.dumps(cur)),
        )
        c.commit()
    _gs_cache[guild_id] = (time.time(), dict(cur))
    return cur


def dashboard_guild_settings_set(
    guild_id: str, settings: dict, *, actor_id: str
) -> dict:
    """Validate, persist and audit one complete dashboard settings update."""
    gid = _guild_settings_key(guild_id)
    previous = guild_settings(gid)
    clean = merge_server_settings(settings)
    changed = sorted(key for key, value in clean.items() if previous.get(key) != value)
    payload = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 32_000:
        raise ValueError("server configuration is too large")
    timestamp = now()
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO guild_settings(guild_id,data) VALUES(?,?) "
                "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
                (gid, payload),
            )
            c.execute(
                "INSERT INTO dashboard_audit(guild_id,actor_id,action,module,detail,created) "
                "VALUES(?,?,?,?,?,?)",
                (
                    gid,
                    str(actor_id)[:100],
                    "settings.updated",
                    "server_settings",
                    json.dumps({"changed": changed}, sort_keys=True),
                    timestamp,
                ),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
    _gs_cache[gid] = (time.time(), dict(clean))
    return clean


def module_config(guild_id: str, module: str) -> dict:
    """Return one module's stored state merged with its current defaults."""
    from sefbot.module_catalog import MODULES, merge_settings

    name = str(module).strip().lower()
    if name not in MODULES:
        raise KeyError(name)
    gid = f"guild:{guild_id}" if str(guild_id).isdigit() else str(guild_id)
    row = conn().execute(
        "SELECT enabled,data,updated,actor_id FROM module_settings "
        "WHERE guild_id=? AND module=?",
        (gid, name),
    ).fetchone()
    if row is None:
        return {
            "module": name,
            "enabled": bool(MODULES[name].get("default_enabled", False)),
            "settings": merge_settings(name, {}),
            "updated": None,
            "actor_id": None,
        }
    return {
        "module": name,
        "enabled": bool(row["enabled"]),
        "settings": merge_settings(name, _json_dict(row["data"])),
        "updated": float(row["updated"]),
        "actor_id": row["actor_id"],
    }


def module_configs(guild_id: str) -> list[dict]:
    from sefbot.module_catalog import MODULES

    return [module_config(guild_id, name) for name in MODULES]


def module_config_set(
    guild_id: str,
    module: str,
    *,
    enabled: bool,
    settings: dict,
    actor_id: str,
) -> dict:
    """Validate and atomically persist a dashboard-controlled module."""
    from sefbot.module_catalog import MODULES, merge_settings

    gid = f"guild:{guild_id}" if str(guild_id).isdigit() else str(guild_id)
    name = str(module).strip().lower()
    if name not in MODULES:
        raise KeyError(name)
    clean = merge_settings(name, settings)
    payload = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 256_000:
        raise ValueError("module configuration is too large")
    timestamp = now()
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO module_settings(guild_id,module,enabled,data,updated,actor_id) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,module) DO UPDATE SET "
                "enabled=excluded.enabled,data=excluded.data,updated=excluded.updated,"
                "actor_id=excluded.actor_id",
                (gid, name, 1 if enabled else 0, payload, timestamp, str(actor_id)[:100]),
            )
            c.execute(
                "INSERT INTO dashboard_audit(guild_id,actor_id,action,module,detail,created) "
                "VALUES(?,?,?,?,?,?)",
                (
                    gid,
                    str(actor_id)[:100],
                    "module.updated",
                    name,
                    json.dumps({"enabled": bool(enabled)}, sort_keys=True),
                    timestamp,
                ),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
    return module_config(gid, name)


def dashboard_audit_list(guild_id: str, limit: int = 100) -> list[dict]:
    gid = f"guild:{guild_id}" if str(guild_id).isdigit() else str(guild_id)
    rows = conn().execute(
        "SELECT id,actor_id,action,module,detail,created FROM dashboard_audit "
        "WHERE guild_id=? ORDER BY created DESC,id DESC LIMIT ?",
        (gid, max(1, min(500, int(limit)))),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["detail"] = _json_dict(item.get("detail"))
        output.append(item)
    return output


def dashboard_audit_record(
    guild_id: str,
    *,
    actor_id: str,
    action: str,
    module: str = "",
    detail: dict | None = None,
) -> None:
    """Append one bounded dashboard action to the server audit trail."""
    gid = _guild_settings_key(guild_id)
    if not is_guild_scope(gid):
        raise ValueError("a guild scope is required")
    payload = json.dumps(detail if isinstance(detail, dict) else {}, sort_keys=True)
    if len(payload.encode("utf-8")) > 16_000:
        raise ValueError("audit detail is too large")
    conn().execute(
        "INSERT INTO dashboard_audit(guild_id,actor_id,action,module,detail,created) "
        "VALUES(?,?,?,?,?,?)",
        (gid, str(actor_id)[:100], str(action)[:100], str(module)[:80], payload, now()),
    )
    conn().commit()


def swear_jar_count(guild_id: str, user_id: str) -> int:
    """Return one member's server-scoped swear total."""
    gid = _guild_settings_key(guild_id)
    uid = str(user_id).strip()
    if not is_guild_scope(gid) or not uid.isdigit():
        return 0
    row = conn().execute(
        "SELECT count FROM swear_jar_counts WHERE guild_id=? AND user_id=?",
        (gid, uid),
    ).fetchone()
    return max(0, int(row["count"])) if row else 0


def swear_jar_increment(guild_id: str, user_id: str, amount: int) -> int:
    """Atomically add a bounded amount and return the new server total."""
    gid = _guild_settings_key(guild_id)
    uid = str(user_id).strip()
    increment = max(0, min(100, int(amount)))
    if not is_guild_scope(gid) or not uid.isdigit() or increment == 0:
        return swear_jar_count(gid, uid)

    maximum = 9_223_372_036_854_775_807
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO swear_jar_counts(guild_id,user_id,count,updated) "
                "VALUES(?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET "
                "count=CASE WHEN swear_jar_counts.count>=?-excluded.count THEN ? "
                "ELSE swear_jar_counts.count+excluded.count END,"
                "updated=excluded.updated",
                (gid, uid, increment, now(), maximum, maximum),
            )
            row = c.execute(
                "SELECT count FROM swear_jar_counts WHERE guild_id=? AND user_id=?",
                (gid, uid),
            ).fetchone()
            c.commit()
        except Exception:
            c.rollback()
            raise
    return max(0, int(row["count"])) if row else 0


def _booster_row(row) -> dict:
    if row is None:
        return {
            "guild_id": "", "user_id": "", "current_boosts": 0,
            "all_time_boosts": 0, "active": False, "first_boosted": None,
            "last_boosted": None, "stopped": None, "last_source": "", "updated": 0.0,
        }
    item = dict(row)
    item["current_boosts"] = max(0, int(item.get("current_boosts") or 0))
    item["all_time_boosts"] = max(item["current_boosts"], int(item.get("all_time_boosts") or 0))
    item["active"] = bool(item.get("active"))
    return item


def booster_member(guild_id: str, user_id: str) -> dict:
    """Return the durable boost record for one server member."""
    gid = _guild_settings_key(guild_id)
    uid = str(user_id).strip()
    if not is_guild_scope(gid) or not uid.isdigit():
        return _booster_row(None)
    row = conn().execute(
        "SELECT * FROM booster_members WHERE guild_id=? AND user_id=?", (gid, uid)
    ).fetchone()
    result = _booster_row(row)
    result["guild_id"], result["user_id"] = gid, uid
    return result


def booster_members(guild_id: str, *, active: bool | None = None, limit: int = 1000) -> list[dict]:
    """List current or historical boosters, newest activity first."""
    gid = _guild_settings_key(guild_id)
    if not is_guild_scope(gid):
        return []
    sql = "SELECT * FROM booster_members WHERE guild_id=?"
    args: list[object] = [gid]
    if active is not None:
        sql += " AND active=?"
        args.append(1 if active else 0)
    sql += " ORDER BY updated DESC,user_id LIMIT ?"
    args.append(max(1, min(10_000, int(limit))))
    return [_booster_row(row) for row in conn().execute(sql, tuple(args)).fetchall()]


def booster_stats(guild_id: str) -> dict[str, int]:
    """Return current/all-time boost and booster totals for a server."""
    gid = _guild_settings_key(guild_id)
    if not is_guild_scope(gid):
        return {"current_boosts": 0, "all_time_boosts": 0, "current_boosters": 0, "all_time_boosters": 0}
    row = conn().execute(
        "SELECT COALESCE(SUM(CASE WHEN active=1 THEN current_boosts ELSE 0 END),0) current_boosts,"
        "COALESCE(SUM(all_time_boosts),0) all_time_boosts,"
        "COALESCE(SUM(active),0) current_boosters,"
        "COALESCE(SUM(CASE WHEN all_time_boosts>0 THEN 1 ELSE 0 END),0) all_time_boosters "
        "FROM booster_members WHERE guild_id=?", (gid,),
    ).fetchone()
    return {name: max(0, int(row[name] or 0)) for name in (
        "current_boosts", "all_time_boosts", "current_boosters", "all_time_boosters"
    )}


def booster_record_sync(
    guild_id: str, user_id: str, *, boosted_since: float | None = None, source: str = "sync"
) -> tuple[dict, bool]:
    """Mark a member active. Returns the record and whether this was a new boost period."""
    gid, uid = _guild_settings_key(guild_id), str(user_id).strip()
    if not is_guild_scope(gid) or not uid.isdigit():
        raise ValueError("a guild scope and numeric user id are required")
    timestamp = now()
    started_at = min(timestamp, float(boosted_since)) if boosted_since else timestamp
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT * FROM booster_members WHERE guild_id=? AND user_id=?", (gid, uid)
            ).fetchone()
            started = old is None or not bool(old["active"])
            if old is None:
                c.execute(
                    "INSERT INTO booster_members(guild_id,user_id,current_boosts,all_time_boosts,"
                    "active,first_boosted,last_boosted,stopped,last_source,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (gid, uid, 1, 1, 1, started_at, timestamp, None, str(source)[:30], timestamp),
                )
            elif started:
                c.execute(
                    "UPDATE booster_members SET current_boosts=1,all_time_boosts=all_time_boosts+1,"
                    "active=1,first_boosted=COALESCE(first_boosted,?),last_boosted=?,stopped=NULL,"
                    "last_source=?,updated=? WHERE guild_id=? AND user_id=?",
                    (started_at, timestamp, str(source)[:30], timestamp, gid, uid),
                )
            else:
                c.execute(
                    "UPDATE booster_members SET current_boosts=MAX(1,current_boosts),active=1,"
                    "first_boosted=COALESCE(first_boosted,?),stopped=NULL,last_source=?,updated=? "
                    "WHERE guild_id=? AND user_id=?",
                    (started_at, str(source)[:30], timestamp, gid, uid),
                )
            row = c.execute(
                "SELECT * FROM booster_members WHERE guild_id=? AND user_id=?", (gid, uid)
            ).fetchone()
            c.commit()
        except Exception:
            c.rollback()
            raise
    return _booster_row(row), started


def booster_record_event(guild_id: str, user_id: str, event_id: str) -> tuple[dict, bool]:
    """Record a Discord boost system message once and increment the member count."""
    gid, uid = _guild_settings_key(guild_id), str(user_id).strip()
    eid = str(event_id).strip()[:200]
    if not is_guild_scope(gid) or not uid.isdigit() or not eid:
        raise ValueError("guild, user and event ids are required")
    timestamp = now()
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            inserted = c.execute(
                "INSERT OR IGNORE INTO booster_events(guild_id,event_id,user_id,created) VALUES(?,?,?,?)",
                (gid, eid, uid, timestamp),
            ).rowcount > 0
            if not inserted:
                row = c.execute(
                    "SELECT * FROM booster_members WHERE guild_id=? AND user_id=?", (gid, uid)
                ).fetchone()
                c.commit()
                return _booster_row(row), False
            old = c.execute(
                "SELECT * FROM booster_members WHERE guild_id=? AND user_id=?", (gid, uid)
            ).fetchone()
            # Gateway order is not stable: a premium_since update can arrive seconds before
            # the matching system message. Count that pair once, while later messages are boosts.
            paired_sync = bool(
                old and old["active"] and old["last_source"] in {"sync", "member", "import"}
                and timestamp - float(old["updated"] or 0) <= 30
            )
            if old is None:
                c.execute(
                    "INSERT INTO booster_members(guild_id,user_id,current_boosts,all_time_boosts,active,"
                    "first_boosted,last_boosted,stopped,last_source,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (gid, uid, 1, 1, 1, timestamp, timestamp, None, "system", timestamp),
                )
            elif paired_sync:
                c.execute(
                    "UPDATE booster_members SET last_source='system',last_boosted=?,updated=? "
                    "WHERE guild_id=? AND user_id=?", (timestamp, timestamp, gid, uid),
                )
            else:
                c.execute(
                    "UPDATE booster_members SET current_boosts=CASE WHEN active=1 THEN current_boosts+1 ELSE 1 END,"
                    "all_time_boosts=all_time_boosts+1,active=1,first_boosted=COALESCE(first_boosted,?),"
                    "last_boosted=?,stopped=NULL,last_source='system',updated=? WHERE guild_id=? AND user_id=?",
                    (timestamp, timestamp, timestamp, gid, uid),
                )
            row = c.execute(
                "SELECT * FROM booster_members WHERE guild_id=? AND user_id=?", (gid, uid)
            ).fetchone()
            c.commit()
        except Exception:
            c.rollback()
            raise
    return _booster_row(row), not paired_sync


def booster_record_stop(guild_id: str, user_id: str) -> tuple[dict, bool]:
    """Mark all boosts removed; Discord cannot expose partial boost removals."""
    gid, uid = _guild_settings_key(guild_id), str(user_id).strip()
    old = booster_member(gid, uid)
    if not old["active"]:
        return old, False
    timestamp = now()
    with _db_lock:
        conn().execute(
            "UPDATE booster_members SET current_boosts=0,active=0,stopped=?,last_source='member',updated=? "
            "WHERE guild_id=? AND user_id=?", (timestamp, timestamp, gid, uid),
        )
        conn().commit()
    return booster_member(gid, uid), True


def booster_adjust(guild_id: str, user_id: str, delta: int) -> dict:
    """Apply a manager correction to current and all-time recorded boosts."""
    gid, uid = _guild_settings_key(guild_id), str(user_id).strip()
    change = max(-10_000, min(10_000, int(delta)))
    if not is_guild_scope(gid) or not uid.isdigit() or change == 0:
        return booster_member(gid, uid)
    timestamp = now()
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT * FROM booster_members WHERE guild_id=? AND user_id=?", (gid, uid)
            ).fetchone()
            if old is None and change < 0:
                c.commit()
                return booster_member(gid, uid)
            current = max(0, int(old["current_boosts"] if old else 0) + change)
            lifetime = max(current, max(0, int(old["all_time_boosts"] if old else 0) + change))
            first = old["first_boosted"] if old else (timestamp if current else None)
            if old is None:
                c.execute(
                    "INSERT INTO booster_members(guild_id,user_id,current_boosts,all_time_boosts,active,"
                    "first_boosted,last_boosted,stopped,last_source,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (gid, uid, current, lifetime, int(current > 0), first,
                     timestamp if current else None, None if current else timestamp, "manual", timestamp),
                )
            else:
                c.execute(
                    "UPDATE booster_members SET current_boosts=?,all_time_boosts=?,active=?,"
                    "first_boosted=?,last_boosted=CASE WHEN ?>0 THEN ? ELSE last_boosted END,"
                    "stopped=CASE WHEN ?>0 THEN NULL ELSE ? END,last_source='manual',updated=? "
                    "WHERE guild_id=? AND user_id=?",
                    (current, lifetime, int(current > 0), first, current, timestamp,
                     current, timestamp, timestamp, gid, uid),
                )
            row = c.execute(
                "SELECT * FROM booster_members WHERE guild_id=? AND user_id=?", (gid, uid)
            ).fetchone()
            c.commit()
        except Exception:
            c.rollback()
            raise
    return _booster_row(row)


def community_record_create(
    kind: str,
    guild_id: str,
    data: dict,
    *,
    user_id: str | None = None,
    record_key: str | None = None,
    status: str = "active",
    due: float | None = None,
) -> int:
    """Create a typed durable record used by reminders, tags, tickets and feeds."""
    safe_kind = re.sub(r"[^a-z0-9_-]", "", str(kind).lower())[:40]
    if not safe_kind:
        raise ValueError("record kind is required")
    payload = json.dumps(data if isinstance(data, dict) else {}, ensure_ascii=False)
    if len(payload.encode("utf-8")) > 256_000:
        raise ValueError("record is too large")
    timestamp = now()
    cur = conn().execute(
        "INSERT INTO community_records(kind,guild_id,user_id,record_key,data,status,due,"
        "created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            safe_kind,
            str(guild_id),
            str(user_id) if user_id is not None else None,
            str(record_key)[:200] if record_key is not None else None,
            payload,
            str(status)[:30],
            float(due) if due is not None else None,
            timestamp,
            timestamp,
        ),
    )
    conn().commit()
    return int(cur.lastrowid)


def community_records(
    kind: str,
    guild_id: str,
    *,
    user_id: str | None = None,
    status: str | None = "active",
    due_before: float | None = None,
    limit: int = 500,
) -> list[dict]:
    uid = str(user_id) if user_id is not None else None
    wanted_status = str(status) if status is not None else None
    deadline = float(due_before) if due_before is not None else None
    rows = conn().execute(
        "SELECT * FROM community_records WHERE kind=? AND guild_id=? "
        "AND (? IS NULL OR user_id=?) AND (? IS NULL OR status=?) "
        "AND (? IS NULL OR (due IS NOT NULL AND due<=?)) "
        "ORDER BY COALESCE(due,created),id LIMIT ?",
        (
            str(kind), str(guild_id), uid, uid, wanted_status, wanted_status,
            deadline, deadline, max(1, min(5000, int(limit))),
        ),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["data"] = _json_dict(item.get("data"))
        output.append(item)
    return output


def community_record_update(
    record_id: int,
    *,
    data: dict | None = None,
    status: str | None = None,
    due: float | None = None,
) -> bool:
    row = conn().execute(
        "SELECT data,status,due FROM community_records WHERE id=?", (int(record_id),)
    ).fetchone()
    if row is None:
        return False
    payload = (
        json.dumps(data, ensure_ascii=False)
        if isinstance(data, dict)
        else str(row["data"])
    )
    cur = conn().execute(
        "UPDATE community_records SET data=?,status=?,due=?,updated=? WHERE id=?",
        (
            payload,
            str(status)[:30] if status is not None else str(row["status"]),
            float(due) if due is not None else row["due"],
            now(),
            int(record_id),
        ),
    )
    conn().commit()
    return int(cur.rowcount) > 0


def community_record_delete(record_id: int, *, guild_id: str | None = None) -> bool:
    if guild_id is None:
        cur = conn().execute("DELETE FROM community_records WHERE id=?", (int(record_id),))
    else:
        cur = conn().execute(
            "DELETE FROM community_records WHERE id=? AND guild_id=?",
            (int(record_id), str(guild_id)),
        )
    conn().commit()
    return int(cur.rowcount) > 0


def afk_set(
    guild_id: str,
    user_id: str,
    reason: str,
    *,
    original_nick: str | None = None,
    notify_return: bool = True,
) -> None:
    conn().execute(
        "INSERT INTO afk_statuses(guild_id,user_id,reason,original_nick,notify_return,created) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET "
        "reason=excluded.reason,original_nick=excluded.original_nick,"
        "notify_return=excluded.notify_return,created=excluded.created",
        (
            str(guild_id), str(user_id), str(reason)[:1000],
            str(original_nick)[:100] if original_nick else None,
            1 if notify_return else 0, now(),
        ),
    )
    conn().commit()


def afk_get(guild_id: str, user_id: str) -> dict | None:
    row = conn().execute(
        "SELECT * FROM afk_statuses WHERE guild_id=? AND user_id=?",
        (str(guild_id), str(user_id)),
    ).fetchone()
    return dict(row) if row else None


def afk_list(guild_id: str, limit: int = 100) -> list[dict]:
    return [
        dict(row) for row in conn().execute(
            "SELECT * FROM afk_statuses WHERE guild_id=? ORDER BY created DESC LIMIT ?",
            (str(guild_id), max(1, min(1000, int(limit)))),
        ).fetchall()
    ]


def afk_clear(guild_id: str, user_id: str | None = None) -> list[dict]:
    rows = afk_list(guild_id, 1000) if user_id is None else [afk_get(guild_id, user_id)]
    clean_rows = [row for row in rows if row is not None]
    if user_id is None:
        conn().execute("DELETE FROM afk_statuses WHERE guild_id=?", (str(guild_id),))
    else:
        conn().execute(
            "DELETE FROM afk_statuses WHERE guild_id=? AND user_id=?",
            (str(guild_id), str(user_id)),
        )
    conn().commit()
    return clean_rows


def afk_note_add(
    guild_id: str,
    target_id: str,
    author_id: str,
    content: str,
    *,
    channel_id: str | None = None,
    message_id: str | None = None,
) -> int:
    cur = conn().execute(
        "INSERT INTO afk_notes(guild_id,target_id,author_id,channel_id,message_id,"
        "content,created) VALUES(?,?,?,?,?,?,?)",
        (
            str(guild_id), str(target_id), str(author_id),
            str(channel_id) if channel_id else None,
            str(message_id) if message_id else None,
            str(content)[:1800], now(),
        ),
    )
    conn().commit()
    return int(cur.lastrowid)


def afk_notes_pop(guild_id: str, target_id: str) -> list[dict]:
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            rows = [
                dict(row) for row in c.execute(
                    "SELECT * FROM afk_notes WHERE guild_id=? AND target_id=? "
                    "ORDER BY created,id",
                    (str(guild_id), str(target_id)),
                ).fetchall()
            ]
            c.execute(
                "DELETE FROM afk_notes WHERE guild_id=? AND target_id=?",
                (str(guild_id), str(target_id)),
            )
            c.commit()
            return rows
        except Exception:
            c.rollback()
            raise


def _bond_label(score: float) -> str:
    if score >= 0.7:
        return "ride-or-die"
    if score >= 0.35:
        return "friend"
    if score >= 0.1:
        return "cool with"
    if score > -0.1:
        return "stranger"
    if score > -0.35:
        return "annoying"
    if score > -0.7:
        return "rival"
    return "nemesis"


def relationship_get(user_id: str, guild_id: str) -> dict:
    row = conn().execute(
        "SELECT * FROM relationships WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    ).fetchone()
    if not row:
        return {
            "user_id": user_id, "guild_id": guild_id, "score": 0.0,
            "nickname": None, "grudge": None, "bond_label": "stranger",
            "updated": now(),
        }
    return dict(row)


def relationship_set(
    user_id: str,
    guild_id: str,
    score: Optional[float] = None,
    nickname: Optional[str] = None,
    grudge: Optional[str] = None,
    delta: float = 0.0,
) -> dict:
    cur = relationship_get(user_id, guild_id)
    s = float(cur["score"])
    if score is not None:
        s = float(score)
    s = max(-1.0, min(1.0, s + float(delta)))
    nick = cur.get("nickname")
    if nickname is not None:
        nickname = str(nickname).strip()[:40]
        nick = nickname or None
    g = cur.get("grudge")
    if grudge is not None:
        grudge = str(grudge).strip()[:200]
        g = grudge or None
    label = _bond_label(s)
    conn().execute(
        "INSERT INTO relationships(user_id,guild_id,score,nickname,grudge,bond_label,updated) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id,guild_id) DO UPDATE SET "
        "score=excluded.score, nickname=excluded.nickname, grudge=excluded.grudge, "
        "bond_label=excluded.bond_label, updated=excluded.updated",
        (user_id, guild_id, s, nick, g, label, now()),
    )
    conn().commit()
    return relationship_get(user_id, guild_id)


def relationship_top(guild_id: str, limit: int = 10, worst: bool = False) -> List[dict]:
    query = (
        "SELECT * FROM relationships WHERE guild_id=? ORDER BY score ASC LIMIT ?"
        if worst
        else "SELECT * FROM relationships WHERE guild_id=? ORDER BY score DESC LIMIT ?"
    )
    rows = conn().execute(query, (guild_id, limit)).fetchall()
    return [dict(r) for r in rows]


def relationships_for_user(user_id: str) -> List[dict]:
    rows = conn().execute(
        "SELECT * FROM relationships WHERE user_id=?",
        (str(user_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def memories_for_subject(subject: str) -> List[sqlite3.Row]:
    subject = normalize_subject(subject)
    return conn().execute(
        "SELECT * FROM memories WHERE subject=?",
        (subject,),
    ).fetchall()


def convo_add(user_id: str, guild_id: str, role: str, content: str) -> None:
    if not history_storage_allowed(str(user_id), str(guild_id)):
        return
    c = conn()
    c.execute(
        "INSERT INTO conversations(user_id,guild_id,role,content,created) VALUES(?,?,?,?,?)",
        (user_id, guild_id, role, (content or "")[:1500], now()),
    )
    keep = max(4, config.CONVO_TURNS * 2)
    c.execute(
        "DELETE FROM conversations WHERE id NOT IN ("
        "  SELECT id FROM conversations WHERE user_id=? AND guild_id=? "
        "  ORDER BY created DESC LIMIT ?"
        ") AND user_id=? AND guild_id=?",
        (user_id, guild_id, keep, user_id, guild_id),
    )
    c.commit()


def convo_get(user_id: str, guild_id: str, limit: int = None) -> List[dict]:
    limit = limit or config.CONVO_TURNS * 2
    rows = conn().execute(
        "SELECT role, content, created FROM conversations "
        "WHERE user_id=? AND guild_id=? ORDER BY created DESC LIMIT ?",
        (user_id, guild_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def convo_clear(user_id: str, guild_id: str) -> int:
    cur = conn().execute(
        "DELETE FROM conversations WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    )
    conn().commit()
    return cur.rowcount


def convo_clear_user(user_id: str) -> int:
    cur = conn().execute(
        "DELETE FROM conversations WHERE user_id=?",
        (str(user_id),),
    )
    conn().commit()
    return max(0, int(cur.rowcount))


def quote_add(guild_id: str, text: str, about: str = None, author: str = None) -> int:
    cur = conn().execute(
        "INSERT INTO quotes(guild_id,text,about,author,created) VALUES(?,?,?,?,?)",
        (guild_id, text.strip()[:500], about, author, now()),
    )
    conn().commit()
    return cur.lastrowid


def quote_random(guild_id: str, about: str = None) -> Optional[dict]:
    if about:
        row = conn().execute(
            "SELECT * FROM quotes WHERE guild_id=? AND about=? ORDER BY RANDOM() LIMIT 1",
            (guild_id, about),
        ).fetchone()
    else:
        row = conn().execute(
            "SELECT * FROM quotes WHERE guild_id=? ORDER BY RANDOM() LIMIT 1",
            (guild_id,),
        ).fetchone()
    return dict(row) if row else None


def quote_list(guild_id: str, limit: int = 20) -> List[dict]:
    rows = conn().execute(
        "SELECT * FROM quotes WHERE guild_id=? ORDER BY created DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def quote_delete(
    qid: int,
    scope_id: str,
    requester_id: str,
    *,
    can_moderate: bool = False,
) -> bool:
    """Delete only a quote owned by this scope and actor (or its moderator)."""
    if can_moderate:
        cur = conn().execute(
            "DELETE FROM quotes WHERE id=? AND guild_id=?", (qid, scope_id)
        )
    else:
        cur = conn().execute(
            "DELETE FROM quotes WHERE id=? AND guild_id=? AND author=?",
            (qid, scope_id, requester_id),
        )
    conn().commit()
    return cur.rowcount > 0


_SNOWFLAKE = re.compile(r"(\d{15,22})")


def normalize_subject(about, default_user: str = None) -> str:
    """Canonical subject key: raw user id, or 'server'.

    The model sometimes emits <@id>, bare ids, or 'me'/'user' — normalize so
    erase/list/get all hit the same rows.
    """
    s = str(about if about is not None else "server").strip()
    if not s:
        return "server"
    low = s.lower()
    if low in ("server", "guild", "channel", "here", "this server"):
        return "server"
    if low in ("me", "user", "them", "this user", "the user", "speaker", "author", "self"):
        return str(default_user) if default_user else "server"
    m = _SNOWFLAKE.search(s)
    if m:
        return m.group(1)
    return s


def _tokens(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def add_memory(content, author, guild_id, subject="server", importance=0.5) -> int:
    """Insert a memory, merging into a near-duplicate if one exists."""
    content = (content or "").strip()
    if not content:
        return 0
    subject = normalize_subject(subject, default_user=author)
    guild_id = str(guild_id) if guild_id is not None else None
    importance = max(0.0, min(1.0, float(importance)))
    existing = memories_about(subject, guild_id)
    new_tok = _tokens(content)
    if new_tok:
        for row in existing:
            old_tok = _tokens(row["content"])
            if not old_tok:
                continue
            overlap = len(new_tok & old_tok) / max(1, len(new_tok | old_tok))
            if overlap >= 0.45:
                new_imp = max(float(row["importance"] or 0.5), importance)
                new_imp = min(1.0, new_imp + 0.05)
                text = content if len(content) >= len(row["content"]) else row["content"]
                conn().execute(
                    "UPDATE memories SET content=?, importance=?, updated=?, author=? WHERE id=?",
                    (text, new_imp, now(), author, row["id"]),
                )
                conn().commit()
                _invalidate_memory_cache(subject=subject, scope_id=guild_id)
                return row["id"]
    cur = conn().execute(
        "INSERT INTO memories(subject,content,author,guild_id,importance,created,updated) "
        "VALUES(?,?,?,?,?,?,?)",
        (subject, content, author, guild_id, importance, now(), now()),
    )
    conn().commit()
    _invalidate_memory_cache(subject=subject, scope_id=guild_id)
    _enforce_memory_cap(subject, guild_id)
    return cur.lastrowid


def _enforce_memory_cap(subject: str, guild_id: str) -> None:
    rows = memories_about(subject, guild_id)
    cap = config.MEMORY_SOFT_CAP
    if len(rows) <= cap:
        return
    extras = rows[cap:]
    for r in extras:
        if float(r["importance"] or 0) < 0.35:
            conn().execute("DELETE FROM memories WHERE id=?", (r["id"],))
        else:
            new_imp = max(0.05, float(r["importance"] or 0.5) * 0.85)
            conn().execute(
                "UPDATE memories SET importance=? WHERE id=?",
                (new_imp, r["id"]),
            )
    conn().commit()
    _invalidate_memory_cache(subject=subject, scope_id=guild_id)


def update_memory(mem_id: int, content: str = None, importance: float = None) -> bool:
    row = conn().execute("SELECT * FROM memories WHERE id=?", (mem_id,)).fetchone()
    if not row:
        return False
    content = content if content is not None else row["content"]
    importance = float(importance) if importance is not None else float(row["importance"])
    conn().execute(
        "UPDATE memories SET content=?, importance=?, updated=? WHERE id=?",
        (content.strip(), max(0.0, min(1.0, importance)), now(), mem_id),
    )
    conn().commit()
    _invalidate_memory_cache(subject=str(row["subject"]), scope_id=str(row["guild_id"] or ""))
    return True


def memories_about(subject: str, guild_id: Optional[str]) -> List[sqlite3.Row]:
    subject = normalize_subject(subject)
    gid = str(guild_id) if guild_id is not None else None
    key = ("about", subject, gid)
    cached = _mem_cache_get(key)
    if cached is not None:
        return cached
    if gid is None:
        return []
    rows = conn().execute(
        "SELECT * FROM memories WHERE subject=? AND guild_id=? "
        "ORDER BY importance DESC, created DESC",
        (subject, gid),
    ).fetchall()
    _mem_cache_set(key, rows)
    return rows


def scope_memories(guild_id: Optional[str]) -> List[sqlite3.Row]:
    if guild_id is None:
        return []
    gid = str(guild_id)
    key = ("scope", gid)
    cached = _mem_cache_get(key)
    if cached is not None:
        return cached
    rows = conn().execute("SELECT * FROM memories WHERE guild_id=?", (gid,)).fetchall()
    _mem_cache_set(key, rows)
    return rows


def get_memory(mem_id: int) -> Optional[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM memories WHERE id=?", (int(mem_id),)
    ).fetchone()


def forget_memory(mem_id: int) -> bool:
    row = get_memory(mem_id)
    cur = conn().execute("DELETE FROM memories WHERE id=?", (int(mem_id),))
    conn().commit()
    if row is not None:
        _invalidate_memory_cache(subject=str(row["subject"]), scope_id=str(row["guild_id"] or ""))
    return cur.rowcount > 0


def forget_memories_about(
    subject: str,
    guild_id: Optional[str],
    *,
    clear_convo: bool = True,
    all_guilds: bool = False,
) -> dict:
    """Wipe long-term memories about a subject.

    Also clears short-term conversation history for that user (so the model
    cannot re-learn the same facts on the next message). Returns counts.
    """
    subject = normalize_subject(subject)
    gid = str(guild_id) if guild_id is not None else None
    if all_guilds or gid is None:
        cur = conn().execute("DELETE FROM memories WHERE subject=?", (subject,))
    else:
        cur = conn().execute(
            "DELETE FROM memories WHERE subject=? AND guild_id=?",
            (subject, gid),
        )
    n_mem = cur.rowcount
    n_convo = 0
    if clear_convo and subject.isdigit():
        if all_guilds or gid is None:
            cur2 = conn().execute(
                "DELETE FROM conversations WHERE user_id=?", (subject,)
            )
            n_convo = cur2.rowcount
        else:
            n_convo = convo_clear(subject, gid)
    conn().commit()
    if all_guilds or gid is None:
        _invalidate_memory_cache(subject=subject)
    else:
        _invalidate_memory_cache(subject=subject, scope_id=gid)
    return {"memories": n_mem, "convo": n_convo}


def compact_memories(subject: str, guild_id: str, keep: int = 15) -> int:
    """Keep top-N by importance; delete the rest. Returns deleted count."""
    subject = normalize_subject(subject)
    rows = memories_about(subject, guild_id)
    if len(rows) <= keep:
        return 0
    drop_ids = [r["id"] for r in rows[keep:]]
    for i in drop_ids:
        conn().execute("DELETE FROM memories WHERE id=?", (i,))
    conn().commit()
    _invalidate_memory_cache(subject=subject, scope_id=str(guild_id))
    return len(drop_ids)


def add_lesson(content: str, source: str = "reflection", scope_id: str | None = None) -> bool:
    global _lessons_cache
    if not scope_id or not (is_guild_scope(scope_id) or is_dm_scope(scope_id)):
        return False
    try:
        conn().execute(
            "INSERT INTO lessons(content,source,created,scope_id,enabled) VALUES(?,?,?,?,1)",
            (content.strip()[:500], source, now(), scope_id),
        )
        conn().commit()
        _lessons_cache = None
        return True
    except sqlite3.IntegrityError:
        return False


def all_lessons(scope_id: str | None = None):
    global _lessons_cache, _lessons_ts
    if not scope_id:
        return []
    now_t = time.time()
    cache_key = str(scope_id)
    if isinstance(_lessons_cache, dict) and now_t - _lessons_ts < _LESSONS_TTL:
        return _lessons_cache.get(cache_key, [])
    rows = conn().execute(
        "SELECT * FROM lessons WHERE scope_id=? AND enabled=1 ORDER BY created",
        (scope_id,),
    ).fetchall()
    _lessons_cache = {cache_key: rows}
    _lessons_ts = now_t
    return rows


def delete_lesson(lesson_id: int, scope_id: str) -> bool:
    global _lessons_cache
    cur = conn().execute(
        "DELETE FROM lessons WHERE id=? AND scope_id=?", (lesson_id, scope_id)
    )
    conn().commit()
    if cur.rowcount:
        _lessons_cache = None
    return cur.rowcount > 0


def add_feedback(user_msg, bot_msg, verdict, author, note=None, scope_id: str | None = None) -> int:
    if not scope_id:
        return 0
    cur = conn().execute(
        "INSERT INTO feedback(user_msg,bot_msg,verdict,note,author,created,scope_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (user_msg, bot_msg, verdict, note, author, now(), scope_id),
    )
    conn().commit()
    return cur.lastrowid


def unprocessed_feedback(limit: int, scope_id: str | None = None):
    selected_scope = str(scope_id or "")
    if not selected_scope:
        first = conn().execute(
            "SELECT scope_id FROM feedback WHERE processed=0 AND scope_id IS NOT NULL "
            "ORDER BY created LIMIT 1"
        ).fetchone()
        if not first:
            return []
        selected_scope = str(first["scope_id"] or "")
    if not (is_guild_scope(selected_scope) or is_dm_scope(selected_scope)):
        return []
    return conn().execute(
        "SELECT * FROM feedback WHERE processed=0 AND scope_id=? ORDER BY created LIMIT ?",
        (selected_scope, limit),
    ).fetchall()


def mark_feedback_processed(ids) -> None:
    if not ids:
        return
    conn().executemany("UPDATE feedback SET processed=1 WHERE id=?", [(i,) for i in ids])
    conn().commit()


def add_command(name, description, behavior, author, guild_id) -> None:
    conn().execute(
        "INSERT INTO commands(scope_id,name,description,behavior,author,created,enabled) "
        "VALUES(?,?,?,?,?,?,1) "
        "ON CONFLICT(scope_id,name) DO UPDATE SET "
        "description=excluded.description, behavior=excluded.behavior, author=excluded.author, enabled=1",
        (guild_id, name.lower(), description, behavior, author, now()),
    )
    conn().commit()


def get_command(name, scope_id: str):
    return conn().execute(
        "SELECT * FROM commands WHERE scope_id=? AND name=? AND enabled=1",
        (scope_id, name.lower()),
    ).fetchone()


def all_commands(scope_id: str):
    return conn().execute(
        "SELECT * FROM commands WHERE scope_id=? AND enabled=1 ORDER BY uses DESC",
        (scope_id,),
    ).fetchall()


def bump_command(name, scope_id: str) -> None:
    conn().execute(
        "UPDATE commands SET uses=uses+1 WHERE scope_id=? AND name=?",
        (scope_id, name.lower()),
    )
    conn().commit()


def delete_command(name, scope_id: str, requester_id: str, *, can_moderate: bool = False) -> bool:
    if can_moderate:
        cur = conn().execute(
            "DELETE FROM commands WHERE scope_id=? AND name=?", (scope_id, name.lower())
        )
    else:
        cur = conn().execute(
            "DELETE FROM commands WHERE scope_id=? AND name=? AND author=?",
            (scope_id, name.lower(), requester_id),
        )
    conn().commit()
    return cur.rowcount > 0


def log_interaction(kind, author, guild_id) -> None:
    conn().execute(
        "INSERT INTO interactions(kind,author,guild_id,created) VALUES(?,?,?,?)",
        (kind, author, guild_id, now()),
    )
    conn().commit()


def stats() -> dict:
    c = conn()

    def q(sql: str, *a):
        return c.execute(sql, a).fetchone()["n"]

    return {
        "interactions": q("SELECT COUNT(*) n FROM interactions"),
        "memories": q("SELECT COUNT(*) n FROM memories"),
        "lessons": q("SELECT COUNT(*) n FROM lessons"),
        "commands": q("SELECT COUNT(*) n FROM commands"),
        "quotes": q("SELECT COUNT(*) n FROM quotes"),
        "relationships": q("SELECT COUNT(*) n FROM relationships"),
        "thumbs_up": q("SELECT COUNT(*) n FROM feedback WHERE verdict='up'"),
        "thumbs_down": q("SELECT COUNT(*) n FROM feedback WHERE verdict='down'"),
    }


def export_guild(guild_id: str) -> dict:
    """Dump a versioned, exact-scope guild bundle."""
    return {
        "schema_version": 2,
        "scope": guild_id,
        "exported_at": now(),
        "settings": guild_settings(guild_id),
        "memories": [dict(r) for r in scope_memories(guild_id)],
        "commands": [dict(r) for r in all_commands(guild_id)],
        "quotes": quote_list(guild_id, limit=500),
        "relationships": [
            dict(r) for r in conn().execute(
                "SELECT * FROM relationships WHERE guild_id=?", (guild_id,)
            ).fetchall()
        ],
    }


_IMPORT_LIMITS = {
    "memories": 500,
    "commands": 100,
    "quotes": 500,
    "relationships": 1000,
}


def validate_import_bundle(data: dict, scope_id: str) -> dict:
    """Validate and normalize ImportBundleV2 before any database mutation."""
    if not isinstance(data, dict):
        raise ValueError("import must be a JSON object")
    allowed_sections = {
        "schema_version",
        "scope",
        "exported_at",
        "settings",
        *_IMPORT_LIMITS,
    }
    unknown_sections = sorted(set(data) - allowed_sections)
    if unknown_sections:
        raise ValueError(f"unsupported import section(s): {', '.join(unknown_sections)}")
    if data.get("schema_version") != 2:
        raise ValueError("unsupported import schema; expected schema_version 2")
    if str(data.get("scope") or "") != str(scope_id):
        raise ValueError("import scope does not match this server")

    clean: dict = {"schema_version": 2, "scope": scope_id}
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    clean["settings"] = {k: v for k, v in settings.items() if k in _DEFAULT_GUILD}

    for section, maximum in _IMPORT_LIMITS.items():
        rows = data.get(section) or []
        if not isinstance(rows, list):
            raise ValueError(f"{section} must be an array")
        if len(rows) > maximum:
            raise ValueError(f"{section} exceeds the {maximum}-row limit")
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"every {section} entry must be an object")
        clean[section] = rows

    for row in clean["memories"]:
        content = str(row.get("content") or "").strip()
        if not content or len(content) > 2_000:
            raise ValueError("memory content must be 1-2000 characters")
    for row in clean["commands"]:
        name = str(row.get("name") or "").lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", name):
            raise ValueError(f"invalid command name: {name!r}")
        description = str(row.get("description") or "")
        if len(description) > 200:
            raise ValueError(f"command {name!r} description is too long")
        if len(str(row.get("behavior") or "")) > 4_000:
            raise ValueError(f"command {name!r} behavior is too long")
    for row in clean["quotes"]:
        if not str(row.get("text") or "").strip() or len(str(row.get("text"))) > 500:
            raise ValueError("quote text must be 1-500 characters")
    for row in clean["relationships"]:
        user_id = str(row.get("user_id") or "").strip()
        if not user_id or len(user_id) > 32:
            raise ValueError("relationship user_id must be 1-32 characters")
        try:
            score = float(row.get("score", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("relationship score must be a number") from exc
        if not math.isfinite(score) or not -1.0 <= score <= 1.0:
            raise ValueError("relationship score must be between -1 and 1")
        if len(str(row.get("nickname") or "")) > 40:
            raise ValueError("relationship nickname is too long")
        if len(str(row.get("grudge") or "")) > 200:
            raise ValueError("relationship grudge is too long")
    return clean


def import_guild(data: dict, guild_id: str) -> dict:
    """Validate and atomically import a versioned exact-scope bundle."""
    counts = {"memories": 0, "lessons": 0, "commands": 0, "quotes": 0, "relationships": 0}
    bundle = validate_import_bundle(data, guild_id)
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            if bundle["settings"]:
                settings = {**guild_settings(guild_id), **bundle["settings"]}
                c.execute(
                    "INSERT INTO guild_settings(guild_id,data) VALUES(?,?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
                    (guild_id, json.dumps(settings)),
                )
            for row in bundle["memories"]:
                c.execute(
                    "INSERT INTO memories(subject,content,author,guild_id,importance,created,updated) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        normalize_subject(row.get("subject"), row.get("author")),
                        str(row["content"]).strip(), str(row.get("author") or "import"),
                        guild_id, max(0.0, min(1.0, float(row.get("importance", 0.5)))),
                        now(), now(),
                    ),
                )
                counts["memories"] += 1
            for row in bundle["commands"]:
                c.execute(
                    "INSERT INTO commands(scope_id,name,description,behavior,author,created,enabled) "
                    "VALUES(?,?,?,?,?,?,1) ON CONFLICT(scope_id,name) DO UPDATE SET "
                    "description=excluded.description,behavior=excluded.behavior,"
                    "author=excluded.author,enabled=1",
                    (
                        guild_id, str(row["name"]).lower(),
                        str(row.get("description") or row["name"])[:200],
                        str(row.get("behavior") or "Respond helpfully.")[:4000],
                        str(row.get("author") or "import"), now(),
                    ),
                )
                counts["commands"] += 1
            for row in bundle["quotes"]:
                c.execute(
                    "INSERT INTO quotes(guild_id,text,about,author,created) VALUES(?,?,?,?,?)",
                    (guild_id, str(row["text"]).strip(), row.get("about"), row.get("author"), now()),
                )
                counts["quotes"] += 1
            for row in bundle["relationships"]:
                uid = str(row.get("user_id") or "")
                if not uid:
                    continue
                score = max(-1.0, min(1.0, float(row.get("score", 0))))
                c.execute(
                    "INSERT INTO relationships(user_id,guild_id,score,nickname,grudge,bond_label,updated) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,guild_id) DO UPDATE SET "
                    "score=excluded.score,nickname=excluded.nickname,grudge=excluded.grudge,"
                    "bond_label=excluded.bond_label,updated=excluded.updated",
                    (
                        uid, guild_id, score, str(row.get("nickname") or "")[:40] or None,
                        str(row.get("grudge") or "")[:200] or None, _bond_label(score), now(),
                    ),
                )
                counts["relationships"] += 1
            c.commit()
        except Exception:
            c.rollback()
            raise
    _gs_cache.pop(guild_id, None)
    _invalidate_memory_cache(scope_id=guild_id)
    return counts


def user_flag_get(user_id: str, key: str, default: str = None) -> Optional[str]:
    return kv_get(f"uf:{user_id}:{key}", default)


def user_flag_set(user_id: str, key: str, value) -> None:
    kv_set(f"uf:{user_id}:{key}", value)


def user_flag_int(user_id: str, key: str, default: int = 0) -> int:
    raw = user_flag_get(user_id, key)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def tos_challenge_create(
    user_id: str, token_hash: str, version: str, expires_at: float
) -> None:
    """Replace a user's pending web-acceptance challenge with a new one."""
    uid = str(user_id)
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM tos_acceptance_challenges WHERE user_id=?", (uid,))
            c.execute(
                "INSERT INTO tos_acceptance_challenges"
                "(token_hash,user_id,version,expires_at,created_at) VALUES(?,?,?,?,?)",
                (str(token_hash), uid, str(version), float(expires_at), now()),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise


def tos_challenge_consume(
    token_hash: str, version: str, *, current_time: float | None = None
) -> str | None:
    """Atomically consume one unexpired challenge and return its Discord id."""
    timestamp = now() if current_time is None else float(current_time)
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT user_id,version,expires_at FROM tos_acceptance_challenges "
                "WHERE token_hash=?",
                (str(token_hash),),
            ).fetchone()
            c.execute(
                "DELETE FROM tos_acceptance_challenges WHERE token_hash=?",
                (str(token_hash),),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
    if (
        row is None
        or str(row["version"]) != str(version)
        or float(row["expires_at"]) < timestamp
    ):
        return None
    return str(row["user_id"])


def tos_acceptance_set(
    user_id: str,
    version: str,
    *,
    status: str,
    network_hash: str = "",
    risk_code: str = "",
    submitted_at: float | None = None,
) -> None:
    """Store the latest bounded ToS decision for one Discord account."""
    if status not in {"accepted", "review", "rejected"}:
        raise ValueError("invalid ToS acceptance status")
    timestamp = now() if submitted_at is None else float(submitted_at)
    normalized_network = str(network_hash or "")[:128]
    c = conn()
    c.execute(
        "INSERT INTO tos_acceptances"
        "(user_id,version,status,network_hash,network_seen_at,risk_code,"
        "submitted_at,reviewed_at) VALUES(?,?,?,?,?,?,?,NULL) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "version=excluded.version,status=excluded.status,"
        "network_hash=CASE WHEN excluded.network_hash!='' THEN excluded.network_hash "
        "ELSE tos_acceptances.network_hash END,"
        "network_seen_at=CASE WHEN excluded.network_hash!='' THEN excluded.network_seen_at "
        "ELSE tos_acceptances.network_seen_at END,"
        "risk_code=excluded.risk_code,submitted_at=excluded.submitted_at,reviewed_at=NULL",
        (
            str(user_id),
            str(version),
            status,
            normalized_network,
            timestamp if normalized_network else None,
            str(risk_code or "")[:80],
            timestamp,
        ),
    )
    c.commit()


def tos_acceptance_get(user_id: str) -> dict | None:
    row = conn().execute(
        "SELECT user_id,version,status,network_hash,network_seen_at,risk_code,"
        "submitted_at,reviewed_at FROM tos_acceptances WHERE user_id=?",
        (str(user_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def tos_acceptance_network_users(network_hash: str, *, since: float) -> list[str]:
    if not network_hash:
        return []
    rows = conn().execute(
        "SELECT user_id FROM tos_acceptances WHERE network_hash=? "
        "AND network_seen_at>=? ORDER BY network_seen_at DESC LIMIT 100",
        (str(network_hash), float(since)),
    ).fetchall()
    return [str(row["user_id"]) for row in rows]


def tos_acceptance_network_has_dynamic_block(
    network_hash: str, *, since: float, exclude_user_id: str = ""
) -> bool:
    """Return whether a recent matching acceptance belongs to a live block."""
    if not network_hash:
        return False
    row = conn().execute(
        "SELECT 1 FROM tos_acceptances a "
        "JOIN dynamic_blocks b ON b.user_id=a.user_id "
        "WHERE a.network_hash=? AND a.network_seen_at>=? AND a.user_id!=? LIMIT 1",
        (str(network_hash), float(since), str(exclude_user_id)),
    ).fetchone()
    return row is not None


def tos_acceptance_reviews(limit: int = 100) -> list[dict]:
    rows = conn().execute(
        "SELECT user_id,version,status,risk_code,submitted_at FROM tos_acceptances "
        "WHERE status='review' ORDER BY submitted_at ASC LIMIT ?",
        (max(1, min(500, int(limit))),),
    ).fetchall()
    return [dict(row) for row in rows]


def tos_acceptance_allow(user_id: str, version: str) -> bool:
    timestamp = now()
    cur = conn().execute(
        "UPDATE tos_acceptances SET status='accepted',version=?,risk_code='',"
        "reviewed_at=? WHERE user_id=? AND status='review'",
        (str(version), timestamp, str(user_id)),
    )
    conn().commit()
    return int(cur.rowcount) > 0


def privacy_opted_in(user_id: str, scope_id: str) -> bool:
    row = conn().execute(
        "SELECT opted_in FROM privacy_consents WHERE user_id=? AND scope_id=?",
        (str(user_id), str(scope_id)),
    ).fetchone()
    return bool(row and row["opted_in"])


def privacy_set_opt_in(user_id: str, scope_id: str, opted_in: bool) -> None:
    if not (is_guild_scope(scope_id) or is_dm_scope(scope_id)):
        raise ValueError("privacy consent requires a canonical scope")
    conn().execute(
        "INSERT INTO privacy_consents(user_id,scope_id,opted_in,updated) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id,scope_id) DO UPDATE SET "
        "opted_in=excluded.opted_in,updated=excluded.updated",
        (str(user_id), str(scope_id), 1 if opted_in else 0, now()),
    )
    conn().commit()


def privacy_remove_scope_history(user_id: str, scope_id: str) -> int:
    cur = conn().execute(
        "DELETE FROM server_messages WHERE user_id=? AND guild_id=?",
        (str(user_id), str(scope_id)),
    )
    conn().commit()
    return max(0, int(cur.rowcount))


def history_storage_allowed(user_id: str, scope_id: str) -> bool:
    if not privacy_opted_in(user_id, scope_id):
        return False
    if is_dm_scope(scope_id):
        return True
    if not is_guild_scope(scope_id):
        return False
    return bool(guild_settings(scope_id).get("history_enabled", False))


def archive_scope_enabled(scope_id: str) -> bool:
    """Return whether an exact guild scope has deployment-level archiving."""
    value = str(scope_id or "")
    return value.startswith("guild:") and value[6:] in config.ARCHIVE_GUILD_IDS


def privacy_export(user_id: str) -> dict:
    """Return only data owned by, authored by, or explicitly about a user."""
    uid = str(user_id)
    c = conn()
    return {
        "schema_version": 1,
        "user_id": uid,
        "exported_at": now(),
        "consents": [
            dict(r) for r in c.execute(
                "SELECT scope_id,opted_in,updated FROM privacy_consents WHERE user_id=?",
                (uid,),
            ).fetchall()
        ],
        "memories": [
            dict(r) for r in c.execute(
                "SELECT * FROM memories WHERE subject=? OR author=?", (uid, uid)
            ).fetchall()
        ],
        "conversations": [
            dict(r) for r in c.execute(
                "SELECT * FROM conversations WHERE user_id=?", (uid,)
            ).fetchall()
        ],
        "relationships": [
            dict(r) for r in c.execute(
                "SELECT * FROM relationships WHERE user_id=?", (uid,)
            ).fetchall()
        ],
        "quotes": [
            dict(r) for r in c.execute(
                "SELECT * FROM quotes WHERE author=? OR about=?", (uid, uid)
            ).fetchall()
        ],
        "feedback": [
            dict(r) for r in c.execute(
                "SELECT * FROM feedback WHERE author=?", (uid,)
            ).fetchall()
        ],
        "interactions": [
            dict(r) for r in c.execute(
                "SELECT * FROM interactions WHERE author=?", (uid,)
            ).fetchall()
        ],
        "messages": [
            dict(r) for r in c.execute(
                "SELECT message_id,guild_id,channel_id,content,created FROM server_messages "
                "WHERE user_id=? ORDER BY created", (uid,)
            ).fetchall()
        ],
        "dm_contacts": [
            dict(r) for r in c.execute(
                "SELECT user_id,name,last_message_at FROM dm_contacts WHERE user_id=?",
                (uid,),
            ).fetchall()
        ],
        "dynamic_blocks": [
            {"user_id": uid, **metadata}
            for metadata in [dynamic_block_get(uid)]
            if metadata is not None
        ],
        "tos_acceptance": [
            dict(r) for r in c.execute(
                "SELECT user_id,version,status,network_hash,network_seen_at,risk_code,"
                "submitted_at,reviewed_at FROM tos_acceptances WHERE user_id=?",
                (uid,),
            ).fetchall()
        ],
        "assistant_actions": [
            dict(r) for r in c.execute(
                "SELECT id,scope_id,channel_id,action,target_id,parameters,result,"
                "inverse,created,consumed FROM assistant_action_history "
                "WHERE actor_id=? ORDER BY created", (uid,)
            ).fetchall()
        ],
        "community_records": [
            dict(r) for r in c.execute(
                "SELECT * FROM community_records WHERE user_id=? ORDER BY created", (uid,)
            ).fetchall()
        ],
        "afk_statuses": [
            dict(r) for r in c.execute(
                "SELECT * FROM afk_statuses WHERE user_id=? ORDER BY created", (uid,)
            ).fetchall()
        ],
        "afk_notes": [
            dict(r) for r in c.execute(
                "SELECT * FROM afk_notes WHERE target_id=? OR author_id=? ORDER BY created",
                (uid, uid),
            ).fetchall()
        ],
        "swear_jar_counts": [
            dict(r) for r in c.execute(
                "SELECT guild_id,user_id,count,updated FROM swear_jar_counts "
                "WHERE user_id=? ORDER BY guild_id",
                (uid,),
            ).fetchall()
        ],
        "booster_members": [
            dict(r) for r in c.execute(
                "SELECT * FROM booster_members WHERE user_id=? ORDER BY guild_id",
                (uid,),
            ).fetchall()
        ],
    }


def privacy_delete_user(user_id: str) -> dict[str, int]:
    """Transactionally erase all user-owned content and revoke consent."""
    uid = str(user_id)
    delete_queries = {
        "memories": ("DELETE FROM memories WHERE subject=? OR author=?", (uid, uid)),
        "conversations": ("DELETE FROM conversations WHERE user_id=?", (uid,)),
        "relationships": ("DELETE FROM relationships WHERE user_id=?", (uid,)),
        "quotes": ("DELETE FROM quotes WHERE author=? OR about=?", (uid, uid)),
        "feedback": ("DELETE FROM feedback WHERE author=?", (uid,)),
        "interactions": ("DELETE FROM interactions WHERE author=?", (uid,)),
        "server_messages": ("DELETE FROM server_messages WHERE user_id=?", (uid,)),
        "privacy_consents": ("DELETE FROM privacy_consents WHERE user_id=?", (uid,)),
        "tos_acceptance_challenges": (
            "DELETE FROM tos_acceptance_challenges WHERE user_id=?", (uid,)
        ),
        "tos_acceptances": ("DELETE FROM tos_acceptances WHERE user_id=?", (uid,)),
        "commands": ("DELETE FROM commands WHERE author=?", (uid,)),
        "economy_accounts": ("DELETE FROM economy_accounts WHERE user_id=?", (uid,)),
        "work_cooldowns": ("DELETE FROM work_cooldowns WHERE user_id=?", (uid,)),
        "dm_contacts": ("DELETE FROM dm_contacts WHERE user_id=?", (uid,)),
        "cli_active_conversations": ("DELETE FROM cli_active_conversations WHERE user_id=?", (uid,)),
        "assistant_action_history": (
            "DELETE FROM assistant_action_history WHERE actor_id=?", (uid,)
        ),
        "community_records": ("DELETE FROM community_records WHERE user_id=?", (uid,)),
        "afk_statuses": ("DELETE FROM afk_statuses WHERE user_id=?", (uid,)),
        "afk_notes": (
            "DELETE FROM afk_notes WHERE target_id=? OR author_id=?", (uid, uid)
        ),
        "swear_jar_counts": ("DELETE FROM swear_jar_counts WHERE user_id=?", (uid,)),
        "booster_members": ("DELETE FROM booster_members WHERE user_id=?", (uid,)),
        "booster_events": ("DELETE FROM booster_events WHERE user_id=?", (uid,)),
    }
    counts: dict[str, int] = {}
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            for table, (sql, args) in delete_queries.items():
                cur = c.execute(sql, args)
                counts[table] = max(0, int(cur.rowcount))
            block_row = c.execute(
                "SELECT metadata FROM dynamic_blocks WHERE user_id=?", (uid,)
            ).fetchone()
            block_metadata = _json_dict(block_row["metadata"]) if block_row else {}
            preserve_malware_block = (
                str(block_metadata.get("category") or "").lower() == "malware"
                and str(block_metadata.get("trigger_source") or "").lower()
                == "clamav_attachment"
            )
            if preserve_malware_block:
                minimal_block = {
                    "reason": "malware attachment detected",
                    "category": "malware",
                    "offending_text": str(block_metadata.get("offending_text") or "")[:100],
                    "channel_id": "",
                    "guild_id": "",
                    "guild_name": "",
                    "user_tag": "",
                    "trigger_source": "clamav_attachment",
                    "strikes_detail": "security block retained after privacy deletion",
                    "history": [],
                }
                c.execute(
                    "UPDATE dynamic_blocks SET metadata=?,updated_at=? WHERE user_id=?",
                    (
                        json.dumps(minimal_block, sort_keys=True, separators=(",", ":")),
                        now(),
                        uid,
                    ),
                )
                counts["dynamic_blocks"] = 0
            else:
                cur = c.execute("DELETE FROM dynamic_blocks WHERE user_id=?", (uid,))
                counts["dynamic_blocks"] = max(0, int(cur.rowcount))
            cur = c.execute("DELETE FROM kv WHERE key LIKE ?", (f"uf:{uid}:%",))
            counts["user_flags"] = max(0, int(cur.rowcount))
            c.commit()
        except Exception:
            c.rollback()
            raise
    _invalidate_memory_cache()
    return counts


def cleanup_expired_content(retention_days: int = MAX_RETENTION_DAYS) -> dict[str, int]:
    days = max(1, min(MAX_RETENTION_DAYS, int(retention_days)))
    cutoff = now() - days * 86_400
    c = conn()
    counts = {}
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            archive_scopes = tuple(
                f"guild:{guild_id}" for guild_id in sorted(config.ARCHIVE_GUILD_IDS)
            )
            if archive_scopes:
                placeholders = ",".join("?" for _ in archive_scopes)
                cur = c.execute(
                    f"DELETE FROM server_messages WHERE created<? "  # noqa: S608
                    f"AND guild_id NOT IN ({placeholders})",
                    (cutoff, *archive_scopes),
                )
            else:
                cur = c.execute("DELETE FROM server_messages WHERE created<?", (cutoff,))
            counts["server_messages"] = max(0, int(cur.rowcount))
            cur = c.execute("DELETE FROM conversations WHERE created<?", (cutoff,))
            counts["conversations"] = max(0, int(cur.rowcount))
            cur = c.execute("DELETE FROM feedback WHERE created<?", (cutoff,))
            counts["feedback"] = max(0, int(cur.rowcount))
            cur = c.execute(
                "DELETE FROM assistant_action_history WHERE created<?", (cutoff,)
            )
            counts["assistant_action_history"] = max(0, int(cur.rowcount))
            cur = c.execute("DELETE FROM dashboard_audit WHERE created<?", (cutoff,))
            counts["dashboard_audit"] = max(0, int(cur.rowcount))
            cur = c.execute("DELETE FROM booster_events WHERE created<?", (cutoff,))
            counts["booster_events"] = max(0, int(cur.rowcount))
            cur = c.execute(
                "DELETE FROM tos_acceptance_challenges WHERE expires_at<?", (now(),)
            )
            counts["tos_acceptance_challenges"] = max(0, int(cur.rowcount))
            cur = c.execute(
                "DELETE FROM tos_acceptances WHERE status!='accepted' AND submitted_at<?",
                (cutoff,),
            )
            counts["tos_acceptance_reviews"] = max(0, int(cur.rowcount))
            cur = c.execute(
                "UPDATE tos_acceptances SET network_hash='',network_seen_at=NULL,risk_code='' "
                "WHERE status='accepted' AND network_seen_at<?",
                (cutoff,),
            )
            counts["tos_acceptance_networks_minimized"] = max(0, int(cur.rowcount))
            cur = c.execute(
                "DELETE FROM community_records WHERE status!='active' AND updated<?",
                (cutoff,),
            )
            counts["community_records"] = max(0, int(cur.rowcount))
            cur = c.execute(
                "DELETE FROM cli_active_conversations WHERE heartbeat<?",
                (now() - 300.0,),
            )
            counts["cli_active_conversations"] = max(0, int(cur.rowcount))
            c.commit()
        except Exception:
            c.rollback()
            raise
    return counts


def cleanup_guild_content(guild_id: str, retention_days: int) -> dict[str, int]:
    """Apply a guild's stricter retention window without touching other tenants."""
    gid = _guild_settings_key(guild_id)
    days = max(1, min(MAX_RETENTION_DAYS, int(retention_days)))
    cutoff = now() - days * 86_400
    statements = {
        "server_messages": ("DELETE FROM server_messages WHERE guild_id=? AND created<?", (gid, cutoff)),
        "conversations": ("DELETE FROM conversations WHERE guild_id=? AND created<?", (gid, cutoff)),
        "feedback": ("DELETE FROM feedback WHERE scope_id=? AND created<?", (gid, cutoff)),
        "assistant_action_history": (
            "DELETE FROM assistant_action_history WHERE scope_id=? AND created<?",
            (gid, cutoff),
        ),
        "dashboard_audit": ("DELETE FROM dashboard_audit WHERE guild_id=? AND created<?", (gid, cutoff)),
        "community_records": (
            "DELETE FROM community_records WHERE guild_id=? AND status!='active' AND updated<?",
            (gid, cutoff),
        ),
    }
    if archive_scope_enabled(gid):
        statements.pop("server_messages")
    counts: dict[str, int] = {}
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            for table, (sql, args) in statements.items():
                cur = c.execute(sql, args)
                counts[table] = max(0, int(cur.rowcount))
            c.commit()
        except Exception:
            c.rollback()
            raise
    return counts


def record_action_audit(
    *, nonce: str, actor_id: str, scope_id: str, action: str,
    target_id: str | None, parameters: dict, source: str,
    correlation_id: str, status: str, result: str | None = None,
) -> None:
    conn().execute(
        "INSERT INTO action_audit(nonce,actor_id,scope_id,action,target_id,parameters,"
        "source,correlation_id,status,result,created,completed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(nonce) DO UPDATE SET status=excluded.status,result=excluded.result,"
        "completed=excluded.completed",
        (
            nonce, actor_id, scope_id, action[:80], target_id,
            json.dumps(parameters, sort_keys=True, default=str)[:4000], source[:40],
            correlation_id[:80], status[:40], (result or "")[:500] or None,
            now(), now() if status not in {"pending"} else None,
        ),
    )
    conn().commit()


def record_assistant_action(
    *, actor_id: str, scope_id: str, channel_id: str | None, action: str,
    target_id: str | None, parameters: dict, result: str,
    inverse: dict | None, source_nonce: str, consumed_action_id: int | None = None,
) -> int:
    """Persist one confirmed assistant outcome and optionally consume its undo."""
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            if consumed_action_id is not None:
                c.execute(
                    "UPDATE assistant_action_history SET consumed=? "
                    "WHERE id=? AND actor_id=? AND scope_id=? AND consumed IS NULL",
                    (now(), int(consumed_action_id), str(actor_id), str(scope_id)),
                )
            cur = c.execute(
                "INSERT INTO assistant_action_history(actor_id,scope_id,channel_id,"
                "action,target_id,parameters,result,inverse,source_nonce,created) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    str(actor_id), str(scope_id),
                    str(channel_id) if channel_id is not None else None,
                    str(action)[:80], str(target_id) if target_id else None,
                    json.dumps(parameters, sort_keys=True, default=str)[:4000],
                    str(result)[:500],
                    json.dumps(inverse, sort_keys=True, default=str)[:4000]
                    if inverse else None,
                    str(source_nonce)[:100], now(),
                ),
            )
            action_id = int(cur.lastrowid)
            c.commit()
            return action_id
        except Exception:
            c.rollback()
            raise


def latest_assistant_action(actor_id: str, scope_id: str) -> dict | None:
    """Return the most recent unconsumed assistant outcome for this exact scope."""
    row = conn().execute(
        "SELECT id,channel_id,action,target_id,result,inverse,created "
        "FROM assistant_action_history WHERE actor_id=? AND scope_id=? "
        "AND consumed IS NULL ORDER BY created DESC,id DESC LIMIT 1",
        (str(actor_id), str(scope_id)),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["inverse"] = json.loads(item["inverse"]) if item["inverse"] else None
    except json.JSONDecodeError:
        item["inverse"] = None
    return item


def recent_assistant_actions(
    actor_id: str, scope_id: str, limit: int = 5,
) -> list[dict]:
    """Return bounded action summaries for assistant self-knowledge."""
    safe_limit = max(1, min(10, int(limit)))
    return [
        dict(row) for row in conn().execute(
            "SELECT action,target_id,result,created,consumed FROM assistant_action_history "
            "WHERE actor_id=? AND scope_id=? ORDER BY created DESC,id DESC LIMIT ?",
            (str(actor_id), str(scope_id), safe_limit),
        ).fetchall()
    ]


def economy_balance(user_id: str) -> int:
    row = conn().execute(
        "SELECT balance FROM economy_accounts WHERE user_id=?", (str(user_id),)
    ).fetchone()
    return int(row["balance"]) if row else 0


def economy_profile(user_id: str) -> dict[str, int]:
    row = conn().execute(
        "SELECT balance,deposit,gems FROM economy_accounts WHERE user_id=?",
        (str(user_id),),
    ).fetchone()
    if row is None:
        return {"balance": 0, "deposit": 0, "gems": 0}
    return {key: int(row[key]) for key in ("balance", "deposit", "gems")}


def economy_spend(user_id: str, amount: int) -> int:
    """Atomically debit a positive coin amount or raise on insufficient funds."""
    uid, value = str(user_id), int(amount)
    if value <= 0:
        raise ValueError("The amount must be positive.")
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT balance FROM economy_accounts WHERE user_id=?", (uid,)
            ).fetchone()
            balance = int(row["balance"]) if row else 0
            if balance < value:
                c.rollback()
                raise ValueError("You do not have enough coins.")
            balance -= value
            c.execute(
                "UPDATE economy_accounts SET balance=?,updated=? WHERE user_id=?",
                (balance, now(), uid),
            )
            c.commit()
            return balance
        except ValueError:
            raise
        except Exception:
            c.rollback()
            raise


def economy_adjust(user_id: str, delta: int) -> int:
    uid = str(user_id)
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            current = economy_balance(uid)
            balance = max(0, current + int(delta))
            c.execute(
                "INSERT INTO economy_accounts(user_id,balance,updated) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,updated=excluded.updated",
                (uid, balance, now()),
            )
            c.commit()
            return balance
        except Exception:
            c.rollback()
            raise


def economy_transfer(sender_id: str, receiver_id: str, amount: int) -> tuple[int, int]:
    """Atomically transfer coins without allowing self-pay or overdrafts."""
    sender, receiver = str(sender_id), str(receiver_id)
    value = int(amount)
    if sender == receiver:
        raise ValueError("You cannot pay yourself.")
    if value <= 0:
        raise ValueError("The transfer amount must be positive.")
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            sender_row = c.execute(
                "SELECT balance FROM economy_accounts WHERE user_id=?", (sender,)
            ).fetchone()
            sender_balance = int(sender_row["balance"]) if sender_row else 0
            if sender_balance < value:
                c.rollback()
                raise ValueError("You do not have enough coins.")
            receiver_row = c.execute(
                "SELECT balance FROM economy_accounts WHERE user_id=?", (receiver,)
            ).fetchone()
            receiver_balance = int(receiver_row["balance"]) if receiver_row else 0
            timestamp = now()
            sender_balance -= value
            receiver_balance += value
            c.execute(
                "INSERT INTO economy_accounts(user_id,balance,updated) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,updated=excluded.updated",
                (sender, sender_balance, timestamp),
            )
            c.execute(
                "INSERT INTO economy_accounts(user_id,balance,updated) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,updated=excluded.updated",
                (receiver, receiver_balance, timestamp),
            )
            c.commit()
            return sender_balance, receiver_balance
        except ValueError:
            raise
        except Exception:
            c.rollback()
            raise


def economy_leaderboard(limit: int = 10) -> list[tuple[str, dict]]:
    rows = conn().execute(
        "SELECT user_id,balance,deposit FROM economy_accounts ORDER BY balance DESC LIMIT ?",
        (max(1, min(100, int(limit))),),
    ).fetchall()
    return [
        (str(r["user_id"]), {"balance": int(r["balance"]), "deposit": int(r["deposit"])})
        for r in rows
    ]


def xp_needed(level: int) -> int:
    """Total XP required to advance from ``level`` to ``level + 1``."""
    lvl = max(0, int(level))
    return 5 * lvl * lvl + 50 * lvl + 100


def level_for_xp(xp: int) -> int:
    """Highest level whose cumulative requirement is met by ``xp``."""
    remaining = max(0, int(xp))
    level = 0
    while remaining >= xp_needed(level):
        remaining -= xp_needed(level)
        level += 1
    return level


def levels_profile(user_id: str, guild_id: str) -> dict:
    row = conn().execute(
        "SELECT xp,level,messages,last_xp FROM user_levels WHERE user_id=? AND guild_id=?",
        (str(user_id), str(guild_id)),
    ).fetchone()
    if not row:
        return {"xp": 0, "level": 0, "messages": 0, "last_xp": 0.0}
    return {
        "xp": int(row["xp"]),
        "level": int(row["level"]),
        "messages": int(row["messages"]),
        "last_xp": float(row["last_xp"]),
    }


def levels_award(
    user_id: str, guild_id: str, amount: int, cooldown_seconds: float
) -> dict | None:
    """Atomically award XP if off cooldown.

    Returns ``None`` while the user is still on cooldown, otherwise a dict
    with ``gained``, ``leveled_to`` (only when a level-up happened) and the
    updated ``xp``/``level``.
    """
    uid = str(user_id)
    gid = str(guild_id)
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT xp,level,messages,last_xp FROM user_levels "
                "WHERE user_id=? AND guild_id=?",
                (uid, gid),
            ).fetchone()
            current_time = now()
            xp = int(row["xp"]) if row else 0
            level = int(row["level"]) if row else 0
            messages = (int(row["messages"]) + 1) if row else 1
            last_xp = float(row["last_xp"]) if row else 0.0
            if cooldown_seconds > 0 and (current_time - last_xp) < cooldown_seconds:
                if row:
                    c.execute(
                        "UPDATE user_levels SET messages=? WHERE user_id=? AND guild_id=?",
                        (messages, uid, gid),
                    )
                    c.commit()
                c.rollback()
                return None
            gained = max(0, int(amount))
            new_xp = xp + gained
            new_level = level_for_xp(new_xp)
            leveled_to = new_level if new_level > level else None
            c.execute(
                "INSERT INTO user_levels(user_id,guild_id,xp,level,messages,last_xp) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(user_id,guild_id) DO UPDATE SET xp=excluded.xp,"
                "level=excluded.level,messages=excluded.messages,last_xp=excluded.last_xp",
                (uid, gid, new_xp, new_level, messages, current_time),
            )
            c.commit()
            result = {
                "gained": gained,
                "xp": new_xp,
                "level": new_level,
                "messages": messages,
            }
            if leveled_to is not None:
                result["leveled_to"] = leveled_to
                result["next_needed"] = xp_needed(leveled_to)
            return result
        except Exception:
            c.rollback()
            raise


def levels_top(guild_id: str, limit: int = 10) -> list[dict]:
    rows = conn().execute(
        "SELECT user_id,xp,level,messages FROM user_levels "
        "WHERE guild_id=? ORDER BY xp DESC LIMIT ?",
        (str(guild_id), max(1, min(100, int(limit)))),
    ).fetchall()
    return [
        {
            "user_id": str(r["user_id"]),
            "xp": int(r["xp"]),
            "level": int(r["level"]),
            "messages": int(r["messages"]),
        }
        for r in rows
    ]


def daily_claim(
    user_id: str, guild_id: str, reward: int, *, streak_window: float = 172_800.0
) -> tuple[float, int, int]:
    """Atomically claim a daily reward.

    Returns ``(seconds_until_next_claim, credited, streak)``.  ``credited`` is
    ``0`` when the claim is still on cooldown.  A claim within ``streak_window``
    of the previous one keeps the streak alive; anything longer resets it.
    """
    uid = str(user_id)
    gid = str(guild_id)
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT last_claim,streak FROM daily_claims WHERE user_id=? AND guild_id=?",
                (uid, gid),
            ).fetchone()
            current_time = now()
            last_claim = float(row["last_claim"]) if row else 0.0
            streak = int(row["streak"]) if row else 0
            remaining = max(0.0, 86_400.0 - (current_time - last_claim))
            if remaining > 0:
                c.rollback()
                return remaining, 0, streak
            streak = streak + 1 if (current_time - last_claim) <= streak_window else 1
            c.execute(
                "INSERT INTO daily_claims(user_id,guild_id,last_claim,streak) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(user_id,guild_id) DO UPDATE SET "
                "last_claim=excluded.last_claim,streak=excluded.streak",
                (uid, gid, current_time, streak),
            )
            c.commit()
            return 0.0, max(0, int(reward)), streak
        except Exception:
            c.rollback()
            raise


def economy_claim_work(user_id: str, reward: int, cooldown_seconds: int = 60) -> tuple[int, int]:
    """Atomically enforce cooldown and credit work; returns (seconds_left,balance)."""
    uid = str(user_id)
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT last_work FROM work_cooldowns WHERE user_id=?", (uid,)
            ).fetchone()
            current_time = now()
            if row:
                remaining = int(max(0.0, cooldown_seconds - (current_time - float(row["last_work"]))))
                if remaining > 0:
                    balance = economy_balance(uid)
                    c.rollback()
                    return remaining, balance
            current = economy_balance(uid)
            balance = max(0, current + int(reward))
            c.execute(
                "INSERT INTO economy_accounts(user_id,balance,updated) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,updated=excluded.updated",
                (uid, balance, current_time),
            )
            c.execute(
                "INSERT INTO work_cooldowns(user_id,last_work) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_work=excluded.last_work",
                (uid, current_time),
            )
            c.commit()
            return 0, balance
        except Exception:
            c.rollback()
            raise


_BAD_PATTERNS = [
    r"\bfuck\w*", r"\bshit\w*", r"\bbitch\w*", r"\basshole\w*", r"\bbastard\w*",
    r"\bdick\w*", r"\bpussy\w*", r"\bcunt\w*", r"\bwhore\w*", r"\bslut\w*",
    r"\bnigger\w*", r"\bnigga\w*", r"\bfaggot\w*", r"\bretard\w*", r"\bkys\b",
    r"\bkill\s+your\s*self\b", r"\bstfu\b", r"\bshut\s+the\s+fuck\s+up\b",
    r"\bdumbass\w*", r"\bmotherfucker\w*", r"\bpiece\s+of\s+shit\b"
]
_BAD_RE = re.compile("|".join(_BAD_PATTERNS), re.IGNORECASE)


def detect_bad_words(content: str) -> tuple:
    if not content:
        return False, []
    matches = list(set(_BAD_RE.findall(content.lower())))
    return bool(matches), matches


def record_server_message(
    message_id: str,
    guild_id: str,
    guild_name: str,
    channel_id: str,
    channel_name: str,
    user_id: str,
    username: str,
    display_name: str,
    content: str,
    *,
    force: bool = False,
    created_at: float | None = None,
) -> None:
    if (
        not content
        or not user_id
        or not (is_guild_scope(guild_id) or is_dm_scope(guild_id))
        or (not force and not history_storage_allowed(str(user_id), str(guild_id)))
    ):
        return
    c = conn()
    _record_server_message_row(
        c,
        message_id=message_id,
        guild_id=guild_id,
        guild_name=guild_name,
        channel_id=channel_id,
        channel_name=channel_name,
        user_id=user_id,
        username=username,
        display_name=display_name,
        content=content,
        created_at=created_at,
    )
    c.commit()


def _record_server_message_row(
    c: sqlite3.Connection,
    *,
    message_id: str,
    guild_id: str,
    guild_name: str,
    channel_id: str,
    channel_name: str,
    user_id: str,
    username: str,
    display_name: str,
    content: str,
    created_at: float | None,
) -> None:
    clean_content = str(content or "")[:2000]
    if not clean_content:
        return
    has_bad, matches = detect_bad_words(clean_content)
    bad_str = json.dumps(matches) if has_bad else ""
    timestamp = _safe_timestamp(created_at, now()) if created_at is not None else now()
    c.execute(
        """
        INSERT INTO server_messages (
            message_id, guild_id, guild_name, channel_id, channel_name,
            user_id, username, display_name, content, has_bad_words,
            bad_words_found, created
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            content=excluded.content,
            display_name=excluded.display_name,
            has_bad_words=excluded.has_bad_words,
            bad_words_found=excluded.bad_words_found
        """,
        (
            str(message_id), str(guild_id), str(guild_name or "DM/Unknown"),
            str(channel_id), str(channel_name or "unknown"), str(user_id),
            str(username), str(display_name or username), clean_content,
            1 if has_bad else 0, bad_str, timestamp
        )
    )


def record_archived_message_batch(
    guild_id: str,
    channel_id: str,
    channel_name: str,
    records: list[dict],
    *,
    last_message_id: str | None,
    messages_seen: int | None = None,
    complete: bool,
    error: str | None = None,
) -> int:
    """Atomically upsert one text-only archive batch and advance its cursor."""
    gid = str(guild_id)
    if not archive_scope_enabled(gid):
        raise ValueError("archive batch requires an allowlisted guild scope")
    cid = str(channel_id)
    saved = 0
    c = conn()
    with _db_lock:
        try:
            c.execute("BEGIN IMMEDIATE")
            for record in records:
                content = str(record.get("content") or "")
                if not content:
                    c.execute(
                        "DELETE FROM server_messages WHERE guild_id=? AND message_id=?",
                        (gid, str(record["message_id"])),
                    )
                    continue
                _record_server_message_row(
                    c,
                    message_id=str(record["message_id"]),
                    guild_id=gid,
                    guild_name=str(record.get("guild_name") or "Unknown"),
                    channel_id=cid,
                    channel_name=str(channel_name or "unknown"),
                    user_id=str(record["user_id"]),
                    username=str(record.get("username") or record["user_id"]),
                    display_name=str(
                        record.get("display_name")
                        or record.get("username")
                        or record["user_id"]
                    ),
                    content=content,
                    created_at=record.get("created_at"),
                )
                saved += 1
            c.execute(
                "INSERT INTO guild_archive_channels("
                "guild_id,channel_id,channel_name,last_message_id,messages_seen,"
                "complete,last_error,updated) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(guild_id,channel_id) DO UPDATE SET "
                "channel_name=excluded.channel_name,"
                "last_message_id=COALESCE(excluded.last_message_id,"
                "guild_archive_channels.last_message_id),"
                "messages_seen=guild_archive_channels.messages_seen+excluded.messages_seen,"
                "complete=excluded.complete,last_error=excluded.last_error,"
                "updated=excluded.updated",
                (
                    gid,
                    cid,
                    str(channel_name or "unknown")[:100],
                    str(last_message_id) if last_message_id else None,
                    max(0, int(messages_seen if messages_seen is not None else len(records))),
                    1 if complete else 0,
                    str(error or "")[:500] or None,
                    now(),
                ),
            )
            c.commit()
        except Exception:
            c.rollback()
            raise
    return saved


def archive_channel_cursor(guild_id: str, channel_id: str) -> dict | None:
    row = conn().execute(
        "SELECT * FROM guild_archive_channels WHERE guild_id=? AND channel_id=?",
        (str(guild_id), str(channel_id)),
    ).fetchone()
    return dict(row) if row else None


def archive_status(guild_id: str) -> dict:
    gid = str(guild_id)
    rows = conn().execute(
        "SELECT channel_id,channel_name,last_message_id,messages_seen,complete,"
        "last_error,updated FROM guild_archive_channels WHERE guild_id=? "
        "ORDER BY channel_name,channel_id",
        (gid,),
    ).fetchall()
    total = conn().execute(
        "SELECT COUNT(*) FROM server_messages WHERE guild_id=?", (gid,)
    ).fetchone()[0]
    return {
        "guild_id": gid,
        "stored_messages": int(total),
        "channels": [dict(row) for row in rows],
        "complete_channels": sum(bool(row["complete"]) for row in rows),
        "errors": sum(bool(row["last_error"]) for row in rows),
    }


def remove_archived_message(guild_id: str, message_id: str) -> bool:
    """Remove a row whose edited content no longer contains archiveable text."""
    gid = str(guild_id)
    if not archive_scope_enabled(gid):
        return False
    cur = conn().execute(
        "DELETE FROM server_messages WHERE guild_id=? AND message_id=?",
        (gid, str(message_id)),
    )
    conn().commit()
    return bool(cur.rowcount)


def normalize_archived_message_text(guild_id: str, sanitizer) -> dict[str, int]:
    """Rewrite legacy archive rows to the current text-only storage format."""
    gid = str(guild_id)
    if not archive_scope_enabled(gid):
        raise ValueError("archive normalization requires an allowlisted guild scope")
    updated = 0
    deleted = 0
    last_id = 0
    c = conn()
    while True:
        rows = c.execute(
            "SELECT id,content FROM server_messages "
            "WHERE guild_id=? AND id>? ORDER BY id LIMIT 1000",
            (gid, last_id),
        ).fetchall()
        if not rows:
            break
        last_id = int(rows[-1]["id"])
        with _db_lock:
            try:
                c.execute("BEGIN IMMEDIATE")
                for row in rows:
                    original = str(row["content"] or "")
                    clean = str(sanitizer(original) or "")
                    if not clean:
                        deleted += max(
                            0,
                            int(
                                c.execute(
                                    "DELETE FROM server_messages WHERE id=? AND guild_id=?",
                                    (row["id"], gid),
                                ).rowcount
                            ),
                        )
                    elif clean != original:
                        has_bad, matches = detect_bad_words(clean)
                        updated += max(
                            0,
                            int(
                                c.execute(
                                    "UPDATE server_messages SET content=?,has_bad_words=?,"
                                    "bad_words_found=? WHERE id=? AND guild_id=?",
                                    (
                                        clean,
                                        1 if has_bad else 0,
                                        json.dumps(matches) if has_bad else "",
                                        row["id"],
                                        gid,
                                    ),
                                ).rowcount
                            ),
                        )
                c.commit()
            except Exception:
                c.rollback()
                raise
    return {"updated": updated, "deleted": deleted}


_STOP_WORDS = {
    "and", "the", "to", "a", "of", "in", "is", "you", "i", "it", "that", "for",
    "on", "my", "me", "we", "they", "are", "was", "this", "with", "your", "at",
    "be", "but", "so", "like", "just", "have", "has", "not", "do", "did", "im",
    "u", "ur", "what", "when", "who", "how", "why", "can", "cant", "dont", "get",
    "go", "up", "out", "off", "no", "yeah", "yes", "ok", "okay", "lol", "lmao",
    "fr", "ngl", "tbh", "rn", "bro", "man", "guys", "about", "there", "here",
    "them", "him", "her", "his", "their", "would", "could", "should", "will",
    "am", "an", "or", "as", "if", "than", "then", "too", "very", "really",
    "actually", "know", "think", "say", "said", "says", "want", "need", "let",
    "tell", "make", "made", "come", "came", "see", "look", "back", "been",
    "being", "from", "into", "over", "under", "again", "more", "most", "some",
    "any", "all", "every", "one", "two", "time", "day", "still", "even",
    "though", "though", "thing", "things", "cause", "cuz", "cause", "gonna",
    "wanna", "gotta", "dont", "didnt", "doesnt", "wasnt", "isnt", "arent",
    "aint", "idk", "ikr", "omg", "wtf", "bruh", "dude", "yall", "y'all",
}


def _top_words(rows: List[sqlite3.Row], n: int = 20) -> List[str]:
    """Most common non-stop words across a batch of recorded messages."""
    from collections import Counter
    cnt = Counter()
    for r in rows:
        content = r["content"] or ""
        cnt.update(
            w for w in _WORD.findall(content.lower())
            if w not in _STOP_WORDS
        )
    return [w for w, _ in cnt.most_common(n)]


def get_user_intelligence(user_id: str, guild_id: Optional[str] = None) -> dict:
    """Full recorded history for a user — totals, monthly activity, channels,
    favorite words, flagged messages, recent + random old message samples."""
    c = conn()
    uid = str(user_id)
    gid = str(guild_id or "")
    if not gid:
        return _empty_user_intelligence(uid)
    w = "user_id=? AND guild_id=?"
    args = [uid, gid]

    def q(sql: str, extra: str = "", limit: int = None):
        lim = f" LIMIT {int(limit)}" if limit else ""
        return c.execute(sql.format(w=w) + extra + lim, tuple(args))

    total_row = q("SELECT COUNT(*) n FROM server_messages WHERE {w}").fetchone()
    bad_row = q("SELECT COUNT(*) n FROM server_messages WHERE {w} AND has_bad_words=1").fetchone()
    ts = q("SELECT MIN(created) min_ts, MAX(created) max_ts FROM server_messages WHERE {w}").fetchone()
    day_row = q(
        "SELECT COUNT(DISTINCT strftime('%Y-%m-%d', created, 'unixepoch')) d "
        "FROM server_messages WHERE {w}"
    ).fetchone()
    len_row = q(
        "SELECT AVG(LENGTH(content)) avg_len, MAX(LENGTH(content)) max_len "
        "FROM server_messages WHERE {w}"
    ).fetchone()
    bad_msgs = q(
        "SELECT channel_id, channel_name, content, bad_words_found, created FROM server_messages "
        "WHERE {w} AND has_bad_words=1 ORDER BY created DESC", limit=15
    ).fetchall()
    recent_msgs = q(
        "SELECT channel_id, channel_name, content, created FROM server_messages "
        "WHERE {w} ORDER BY created DESC", limit=60
    ).fetchall()
    sample_msgs = q(
        "SELECT channel_id, channel_name, content, created FROM ("
        "SELECT channel_id, channel_name, content, created FROM server_messages "
        "WHERE {w} ORDER BY created DESC LIMIT 5000"
        ") ORDER BY RANDOM()", limit=12
    ).fetchall()
    monthly = q(
        "SELECT strftime('%Y-%m', created, 'unixepoch') m, COUNT(*) n "
        "FROM server_messages WHERE {w} GROUP BY m ORDER BY m DESC", limit=12
    ).fetchall()
    channels = q(
        "SELECT channel_name, COUNT(*) n FROM server_messages WHERE {w} "
        "GROUP BY channel_name ORDER BY n DESC", limit=8
    ).fetchall()
    word_rows = q(
        "SELECT content FROM server_messages WHERE {w} ORDER BY created DESC", limit=2000
    ).fetchall()
    user_info = c.execute(
        "SELECT username, display_name FROM server_messages WHERE user_id=? AND guild_id=? "
        "ORDER BY created DESC LIMIT 1",
        (uid, gid),
    ).fetchone()

    return {
        "user_id": uid,
        "username": user_info["username"] if user_info else uid,
        "display_name": user_info["display_name"] if user_info else uid,
        "total_messages": total_row["n"] if total_row else 0,
        "bad_message_count": bad_row["n"] if bad_row else 0,
        "first_seen": ts["min_ts"] if ts else None,
        "last_seen": ts["max_ts"] if ts else None,
        "active_days": day_row["d"] if day_row else 0,
        "avg_len": int(len_row["avg_len"] or 0) if len_row else 0,
        "max_len": int(len_row["max_len"] or 0) if len_row else 0,
        "bad_messages": [dict(r) for r in bad_msgs],
        "recent_messages": [dict(r) for r in recent_msgs],
        "sample_messages": [dict(r) for r in sample_msgs],
        "monthly": [{"month": r["m"], "n": r["n"]} for r in monthly],
        "channels": [{"channel_name": r["channel_name"] or "unknown", "n": r["n"]} for r in channels],
        "top_words": _top_words(word_rows, 20),
    }


def search_user_messages(
    user_id: str,
    guild_id: str,
    question: str,
    limit: int = 40,
) -> List[dict]:
    """Retrieve question-relevant rows from a user's complete text archive."""
    uid, gid = str(user_id), str(guild_id)
    maximum = max(1, min(100, int(limit)))
    terms = []
    for term in _WORD.findall(str(question or "").lower()):
        if term not in _STOP_WORDS and term not in terms:
            terms.append(term)
    profile_query = profile_search.is_location_question(question)
    if profile_query:
        terms.extend(term for term in profile_search.PROFILE_TERMS if term not in terms)
    if not terms:
        return []
    if not profile_query:
        terms = terms[:12]
    match_query = " OR ".join(f'"{term}"' for term in terms)
    candidate_limit = max(maximum * 10, 500) if profile_query else maximum
    c = conn()
    try:
        rows = c.execute(
            "SELECT m.message_id,m.channel_id,m.channel_name,m.content,m.created "
            "FROM server_messages_fts AS f "
            "JOIN server_messages AS m ON m.id=f.rowid "
            "WHERE server_messages_fts MATCH ? AND m.user_id=? AND m.guild_id=? "
            "ORDER BY bm25(server_messages_fts),m.created DESC LIMIT ?",
            (match_query, uid, gid, candidate_limit),
        ).fetchall()
    except sqlite3.OperationalError:
        if profile_query:
            rows = c.execute(
                "SELECT message_id,channel_id,channel_name,content,created "
                "FROM server_messages WHERE user_id=? AND guild_id=? "
                "ORDER BY created DESC LIMIT ?",
                (uid, gid, candidate_limit),
            ).fetchall()
            ranked = [dict(row) for row in rows]
            ranked.sort(
                key=lambda row: (
                    profile_search.claim_score(str(row["content"])),
                    float(row["created"]),
                ),
                reverse=True,
            )
            results = [
                row for row in ranked if profile_search.claim_score(str(row["content"])) > 0
            ][:maximum]
            return _with_message_context(c, gid, results)
        matches: dict[str, dict] = {}
        for term in terms:
            fallback_rows = c.execute(
                "SELECT message_id,channel_id,channel_name,content,created "
                "FROM server_messages WHERE user_id=? AND guild_id=? "
                "AND LOWER(content) LIKE ? ORDER BY created DESC LIMIT ?",
                (uid, gid, f"%{term}%", maximum),
            ).fetchall()
            for row in fallback_rows:
                matches[str(row["message_id"])] = dict(row)
        return sorted(
            matches.values(), key=lambda row: float(row["created"]), reverse=True
        )[:maximum]
    results = [dict(row) for row in rows]
    if profile_query:
        results.sort(
            key=lambda row: (
                profile_search.claim_score(str(row["content"])),
                float(row["created"]),
            ),
            reverse=True,
        )
        results = [
            row for row in results if profile_search.claim_score(str(row["content"])) > 0
        ]
        return _with_message_context(c, gid, results[:maximum])
    return results


def _with_message_context(
    c: sqlite3.Connection, guild_id: str, rows: list[dict]
) -> list[dict]:
    """Attach the immediately preceding visible-scope message as claim context."""
    for row in rows:
        previous = c.execute(
            "SELECT display_name,content FROM server_messages "
            "WHERE guild_id=? AND channel_id=? AND created<? "
            "ORDER BY created DESC,id DESC LIMIT 1",
            (guild_id, row["channel_id"], row["created"]),
        ).fetchone()
        if previous is not None:
            row["context_before"] = str(previous["content"] or "")[:300]
            row["context_author"] = str(previous["display_name"] or "user")[:100]
    return rows


def _empty_user_intelligence(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "username": user_id,
        "display_name": user_id,
        "total_messages": 0,
        "bad_message_count": 0,
        "first_seen": None,
        "last_seen": None,
        "active_days": 0,
        "avg_len": 0,
        "max_len": 0,
        "bad_messages": [],
        "recent_messages": [],
        "sample_messages": [],
        "monthly": [],
        "channels": [],
        "top_words": [],
    }


def get_user_bad_messages(user_id: str, guild_id: Optional[str] = None, limit: int = 20) -> List[dict]:
    c = conn()
    uid = str(user_id)
    gid = str(guild_id or "")
    if not gid:
        return []
    rows = c.execute(
        "SELECT channel_id, channel_name, content, bad_words_found, created FROM server_messages "
        "WHERE user_id=? AND guild_id=? AND has_bad_words=1 ORDER BY created DESC LIMIT ?",
        (uid, gid, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def get_server_intelligence(guild_id: str) -> dict:
    """Full recorded history for a server — totals, active users, monthly
    activity, top channels, top words, top senders, flagged messages."""
    c = conn()
    gid = str(guild_id)
    total = c.execute("SELECT COUNT(*) n FROM server_messages WHERE guild_id=?", (gid,)).fetchone()["n"]
    bad_total = c.execute("SELECT COUNT(*) n FROM server_messages WHERE guild_id=? AND has_bad_words=1", (gid,)).fetchone()["n"]
    users = c.execute("SELECT COUNT(DISTINCT user_id) n FROM server_messages WHERE guild_id=?", (gid,)).fetchone()["n"]
    ts = c.execute(
        "SELECT MIN(created) min_ts, MAX(created) max_ts FROM server_messages WHERE guild_id=?", (gid,)
    ).fetchone()
    top_senders = c.execute(
        "SELECT user_id, username, display_name, COUNT(*) cnt, "
        "SUM(has_bad_words) bad_cnt FROM server_messages WHERE guild_id=? "
        "GROUP BY user_id ORDER BY cnt DESC LIMIT 10", (gid,)
    ).fetchall()
    recent_bad = c.execute(
        "SELECT username, display_name, channel_name, content, bad_words_found, created "
        "FROM server_messages WHERE guild_id=? AND has_bad_words=1 ORDER BY created DESC LIMIT 10", (gid,)
    ).fetchall()
    monthly = c.execute(
        "SELECT strftime('%Y-%m', created, 'unixepoch') m, COUNT(*) n "
        "FROM server_messages WHERE guild_id=? GROUP BY m ORDER BY m DESC LIMIT 12", (gid,)
    ).fetchall()
    channels = c.execute(
        "SELECT channel_name, COUNT(*) n FROM server_messages WHERE guild_id=? "
        "GROUP BY channel_name ORDER BY n DESC LIMIT 8", (gid,)
    ).fetchall()
    word_rows = c.execute(
        "SELECT content FROM server_messages WHERE guild_id=? ORDER BY created DESC LIMIT 3000", (gid,)
    ).fetchall()
    return {
        "guild_id": gid,
        "total_messages": total,
        "bad_messages_total": bad_total,
        "active_users": users,
        "first_seen": ts["min_ts"] if ts else None,
        "last_seen": ts["max_ts"] if ts else None,
        "top_senders": [dict(r) for r in top_senders],
        "recent_bad_messages": [dict(r) for r in recent_bad],
        "monthly": [{"month": r["m"], "n": r["n"]} for r in monthly],
        "channels": [{"channel_name": r["channel_name"] or "unknown", "n": r["n"]} for r in channels],
        "top_words": _top_words(word_rows, 20),
    }


def find_user_by_name(query: str, guild_id: Optional[str] = None) -> Optional[dict]:
    """Look up recorded user by username, display_name, or user ID."""
    if not query:
        return None
    c = conn()
    q = query.strip().lstrip("@")
    m = _SNOWFLAKE.search(q)
    gid = str(guild_id or "")
    if not gid:
        return None
    if m:
        uid = m.group(1)
        row = c.execute(
            "SELECT user_id, username, display_name FROM server_messages "
            "WHERE user_id=? AND guild_id=? LIMIT 1", (uid, gid)
        ).fetchone()
        if row:
            return dict(row)
        return {"user_id": uid, "username": uid, "display_name": uid}

    row = c.execute(
        "SELECT user_id, username, display_name FROM server_messages "
        "WHERE guild_id=? AND (username LIKE ? OR display_name LIKE ?) "
        "ORDER BY created DESC LIMIT 1",
        (gid, f"%{q}%", f"%{q}%")
    ).fetchone()
    return dict(row) if row else None
