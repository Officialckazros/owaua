"""Transactional hard-block persistence for owaua.

SQLite is the source of truth. ``blocked_users.json`` is retained only as a
one-time, permission-tightened migration source so existing deployments keep
their blocks without continuing to expose or race on a shared JSON file.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import typing
import warnings
from pathlib import Path

from owaua import db

_ROOT = Path(__file__).resolve().parent.parent.parent
BLOCKED_FILE = Path(os.getenv("OWAUA_BLOCKED_FILE", str(_ROOT / "blocked_users.json")))

_MAX_LEGACY_BYTES = 4 * 1024 * 1024
_MAX_LEGACY_USERS = 100_000
_warned_invalid_legacy = False


def normalize_user_id(user_id: typing.Any) -> str:
    """Strip whitespace / mention wrappers; return a bounded numeric id."""
    raw = str(user_id or "").strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1]
        raw = raw.removeprefix("!")
    raw = raw.strip()
    if not raw.isdigit() or len(raw) > 32:
        raise ValueError(f"invalid Discord user id: {user_id!r}")
    return raw


def _migration_name(path: Path) -> str:
    identity = str(path.absolute()).encode("utf-8", errors="surrogateescape")
    return f"dynamic-blocks-json-v1:{hashlib.sha256(identity).hexdigest()}"


def _secure_json_read(path: Path) -> tuple[str, object | None]:
    """Read a regular legacy file without following symlinks.

    Returns ``("missing", None)``, ``("invalid", None)``, or
    ``("ok", decoded_json)``. A file must be made owner-only before any of
    its contents are read.
    """
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return "invalid", None
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "invalid", None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "invalid", None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LEGACY_BYTES:
            return "invalid", None
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            return "invalid", None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(_MAX_LEGACY_BYTES + 1)
        if len(payload) > _MAX_LEGACY_BYTES:
            return "invalid", None
        try:
            return "ok", json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "invalid", None
    finally:
        os.close(fd)


def _text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _numeric_id(value: object) -> str:
    raw = _text(value, 32)
    return raw if raw.isdigit() else ""


def _evidence(value: object) -> str:
    """Retain useful evidence identity without persisting raw message text."""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("sha256:") and " length:" in text and len(text) <= 100:
        return text
    raw = text.encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(raw).hexdigest()[:16]} length:{len(raw)}"


def _timestamp(value: object, default: float | None = None) -> float:
    fallback = time.time() if default is None else float(default)
    try:
        parsed = float(typing.cast(typing.Any, value))
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not (0 <= parsed <= time.time() + 86_400):
        return fallback
    return parsed


def _history_event(raw: object) -> dict[typing.Any, typing.Any]:
    item: typing.Any = typing.cast(typing.Any, raw if isinstance(raw, dict) else {})
    return {
        "timestamp": _timestamp(item.get("timestamp")),
        "reason": _text(item.get("reason"), 500),
        "category": _text(item.get("category"), 80) or "general",
        "offending_text": _evidence(item.get("offending_text")),
        "guild_id": _numeric_id(item.get("guild_id")),
        "guild_name": _text(item.get("guild_name"), 200),
        "channel_id": _numeric_id(item.get("channel_id")),
        "trigger_source": _text(item.get("trigger_source"), 80) or "unknown",
        "strikes_detail": _text(item.get("strikes_detail"), 500),
        "block_source": _text(item.get("block_source"), 16),
    }


def _source_for(meta: dict[typing.Any, typing.Any]) -> str:
    explicit = _text(meta.get("source") or meta.get("block_source"), 16).lower()
    if explicit in {"manual", "tos", "other"}:
        return explicit
    reason = _text(meta.get("reason"), 500).lower()
    category = _text(meta.get("category"), 80).lower()
    if reason.startswith(("tos:", "tos ")) or category.startswith("tos"):
        return "tos"
    return "manual"


def _clean_metadata(raw: object) -> dict[typing.Any, typing.Any]:
    meta: typing.Any = typing.cast(typing.Any, raw if isinstance(raw, dict) else {})
    blocked_at = _timestamp(meta.get("blocked_at"))
    updated_at = _timestamp(meta.get("updated_at"), blocked_at)
    history: typing.Any = meta.get("history")
    events: list[typing.Any] = []
    if isinstance(history, list):
        events: typing.Any = typing.cast(
            typing.Any,
            [
                _history_event(item)
                for item in typing.cast(typing.Iterable[typing.Any], history[-10:])
            ],
        )
    return {
        "blocked_at": blocked_at,
        "updated_at": updated_at,
        "reason": _text(meta.get("reason"), 500),
        "category": _text(meta.get("category"), 80) or "general",
        "offending_text": _evidence(meta.get("offending_text")),
        "channel_id": _numeric_id(meta.get("channel_id")),
        "guild_id": _numeric_id(meta.get("guild_id")),
        "guild_name": _text(meta.get("guild_name"), 200),
        "user_tag": _text(meta.get("user_tag"), 100),
        "trigger_source": _text(meta.get("trigger_source"), 80) or "unknown",
        "strikes_detail": _text(meta.get("strikes_detail"), 500),
        "history": events,
    }


def _legacy_users(value: object) -> dict[typing.Any, typing.Any]:
    if isinstance(value, list):
        return typing.cast(
            typing.Any,
            {str(user_id): {} for user_id in typing.cast(typing.Iterable[typing.Any], value)},
        )
    if not isinstance(value, dict):
        return {}
    users: typing.Any = typing.cast(typing.Any, value).get("users")
    if isinstance(users, dict):
        return typing.cast(typing.Any, users)
    blocked_ids: typing.Any = typing.cast(typing.Any, value).get("blocked")
    if isinstance(blocked_ids, list):
        return typing.cast(
            typing.Any,
            {str(user_id): {} for user_id in typing.cast(typing.Iterable[typing.Any], blocked_ids)},
        )
    return {}


def _migrate_legacy() -> None:
    global _warned_invalid_legacy
    migration = _migration_name(BLOCKED_FILE)
    if db.legacy_state_migrated(migration):
        return

    status, decoded = _secure_json_read(BLOCKED_FILE)
    if status == "invalid":
        if not _warned_invalid_legacy:
            warnings.warn(
                "legacy block file is unsafe or invalid; SQLite migration was not marked complete",
                RuntimeWarning,
                stacklevel=2,
            )
            _warned_invalid_legacy = True
        return

    records: list[tuple[str, str, dict[typing.Any, typing.Any]]] = []
    for raw_uid, raw_meta in list(_legacy_users(decoded).items())[:_MAX_LEGACY_USERS]:
        try:
            uid = normalize_user_id(raw_uid)
        except ValueError:
            continue
        metadata = _clean_metadata(raw_meta)
        source = _source_for(
            typing.cast(typing.Any, raw_meta if isinstance(raw_meta, dict) else {})
        )
        metadata["source"] = source
        records.append((uid, source, metadata))
    db.import_legacy_dynamic_blocks(migration, records)


def dynamic_blocked_ids() -> set[str]:
    """Return user ids blocked through dynamic SQLite state."""
    _migrate_legacy()
    return set(db.dynamic_blocks_all())


def is_dynamically_blocked(user_id: typing.Any) -> bool:
    try:
        uid = normalize_user_id(user_id)
    except ValueError:
        return False
    _migrate_legacy()
    return db.dynamic_block_get(uid) is not None


def block_user(
    user_id: typing.Any,
    reason: str = "",
    *,
    category: str = "",
    offending_text: str = "",
    channel_id: str = "",
    guild_id: str = "",
    guild_name: str = "",
    user_tag: str = "",
    trigger_source: str = "",
    strikes_detail: str = "",
    block_source: str = "",
) -> bool:
    """Create/update a block atomically; return whether it was newly created."""
    uid = normalize_user_id(user_id)
    _migrate_legacy()
    clean_reason = _text(reason, 500)
    clean_category = _text(category, 80) or "general"
    inferred_source = _text(block_source, 16).lower()
    if inferred_source not in {"manual", "tos", "other"}:
        inferred_source = _source_for({"reason": clean_reason, "category": clean_category})
    timestamp = time.time()
    fields = {
        "reason": clean_reason,
        "category": clean_category,
        "offending_text": _evidence(offending_text),
        "channel_id": _numeric_id(channel_id),
        "guild_id": _numeric_id(guild_id),
        "guild_name": _text(guild_name, 200),
        "user_tag": _text(user_tag, 100),
        "trigger_source": _text(trigger_source, 80) or "unknown",
        "strikes_detail": _text(strikes_detail, 500),
    }
    history_event = {
        "timestamp": timestamp,
        "reason": clean_reason,
        "category": clean_category,
        "offending_text": fields["offending_text"],
        "guild_id": fields["guild_id"],
        "guild_name": fields["guild_name"],
        "channel_id": fields["channel_id"],
        "trigger_source": fields["trigger_source"],
        "strikes_detail": fields["strikes_detail"],
    }
    return db.dynamic_block_apply(
        uid,
        block_source=inferred_source,
        fields=fields,
        history_event=history_event,
    )


def unblock_user(user_id: typing.Any, *, expected_source: str | None = None) -> bool:
    """Remove a block atomically, optionally requiring its current source."""
    uid = normalize_user_id(user_id)
    _migrate_legacy()
    expected = None if expected_source is None else {_text(expected_source, 16).lower()}
    return db.dynamic_block_remove(uid, expected_sources=expected)


def get_blocked_user(user_id: typing.Any) -> dict[typing.Any, typing.Any] | None:
    """Return a detached metadata mapping, or ``None`` if not blocked."""
    try:
        uid = normalize_user_id(user_id)
    except ValueError:
        return None
    _migrate_legacy()
    metadata = db.dynamic_block_get(uid)
    return dict(metadata) if metadata is not None else None


def list_blocked() -> dict[str, dict[typing.Any, typing.Any]]:
    """Return a detached ``{user_id: metadata}`` snapshot."""
    _migrate_legacy()
    return {uid: dict(metadata) for uid, metadata in db.dynamic_blocks_all().items()}
