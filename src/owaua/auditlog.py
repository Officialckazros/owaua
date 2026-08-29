"""Audit-log context for the brain.

When a user asks "who did X" about the server (who changed a channel, who
banned/kicked someone, who renamed a role, ...), pull real entries from the
Discord audit log and hand them to the model as authoritative context instead
of letting it guess or answer "I can't see that".
"""

import logging
import re
import time
import typing

import discord

_LOG = logging.getLogger(__name__)

_AUDIT_ASK_RE = re.compile(r"\b(?:who|what|why|when|which|check)\b", re.I)
_AUDIT_VERB_RE = re.compile(
    r"\b(?:changed?|deleted?|banned?|kicked?|created?|renamed?|edited?|modified?|"
    r"moved?|removed?|added?|gave|gives?|set(?: up)?|updated?|pinned?|muted?|"
    r"purged?|wiped|cleared|made|slowmode|timed? out|timeout|unbanned?|"
    r"archived?|destroyed?|nicknamed?|touched)\b",
    re.I,
)
_AUDIT_NEG_RE = re.compile(
    r"\b(?:made|created|built)\s+you\b"
    r"|\bwho\s+(?:are|am|owns)\b"
    r"|\b(?:your|the)\s+(?:owner|creator)\b"
    r"|\bmade\s+by\b"
    r"|\b(?:the|this)\s+bot\b(?![ \t]+[a-z])",
    re.I,
)

_MAX_ENTRIES = 80
_RETENTION_DAYS = 90

_ALIAS_KEYS = {
    "color": "colour",
    "expire_behaviour": "expire_behavior",
}


def wants_audit_log(query: str) -> bool:
    """Cheap heuristic: is this question asking about server history/actions?"""
    q = (query or "").strip()
    if not q:
        return False
    if "audit" in q.lower():
        return True
    if _AUDIT_NEG_RE.search(q):
        return False
    return bool(_AUDIT_ASK_RE.search(q) and _AUDIT_VERB_RE.search(q))


def _short(v: typing.Any, n: int = 80) -> str:
    if isinstance(v, (list, tuple, set, dict, str)) and len(typing.cast(typing.Any, v)) == 0:
        return "none"
    s = str(typing.cast(typing.Any, v))
    return s[:n] + "…" if len(s) > n else s


def _change_bits(entry: typing.Any) -> str:
    """Summarize the before->after field changes of an audit entry.

    discord.py 2.x exposes changes as AuditLogChanges with `.before`/`.after`
    AuditLogDiff objects (iterable key->value dicts), NOT a list of changes.
    """
    try:
        before = entry.before
        after = entry.after
    except Exception:
        return ""
    keys: typing.Any = typing.cast(typing.Any, set())
    for diff in (before, after):
        if diff is None:
            continue
        try:
            keys.update(k for k, _ in diff)
        except Exception:
            _LOG.debug("could not enumerate an audit-log change diff", exc_info=True)
            continue
    bits: list[typing.Any] = []
    for key in sorted(keys):
        if key in ("id", "type", "position"):
            continue
        if key in _ALIAS_KEYS and _ALIAS_KEYS[key] in keys:
            continue
        b = getattr(before, key, None) if before is not None else None
        a = getattr(after, key, None) if after is not None else None
        if b is None and a is None:
            continue
        if b is None:
            bits.append(f"{key}: {_short(a)}")
        elif a is None:
            bits.append(f"{key}: {_short(b)} -> (removed)")
        else:
            bits.append(f"{key}: {_short(b)} -> {_short(a)}")
    return "; ".join(bits)


def _target_label(target: typing.Any) -> str:
    if target is None:
        return "?"
    if isinstance(target, (discord.Member, discord.User)):
        return f"@{getattr(target, 'name', '?')} (id={target.id})"
    name = getattr(target, "name", None)
    return str(name) if name else str(target)[:40]


async def fetch_context(query: str, guild: typing.Any, requester: typing.Any = None) -> str:
    """Return formatted audit-log lines for `query`, or "" when not applicable.

    Non-empty results are authoritative context for the brain. When the bot
    can't read the log it returns an explicit note so the model says so instead
    of inventing an answer.
    """
    if guild is None or not wants_audit_log(query):
        return ""
    requester_perms = getattr(requester, "guild_permissions", None)
    if requester_perms is None or not (
        requester_perms.view_audit_log or requester_perms.administrator
    ):
        # Do not even call Discord's audit-log API for an unauthorized user.
        return ""
    me = getattr(guild, "me", None)
    perms = getattr(me, "guild_permissions", None) if me is not None else None
    if me is None or (perms is not None and not (perms.view_audit_log or perms.administrator)):
        return (
            "(NOTE: owaua lacks the view_audit_log permission here, so it "
            "cannot read this server's audit log. If asked who did what, say "
            "that honestly — do NOT guess.)"
        )
    lines: list[typing.Any] = []
    cutoff = time.time() - _RETENTION_DAYS * 86400
    try:
        async for entry in guild.audit_logs(limit=_MAX_ENTRIES):
            if entry.created_at and entry.created_at.timestamp() < cutoff:
                break
            try:
                actor = entry.user
                actor_name = f"@{actor.name} (id={actor.id})" if actor else "unknown"
                label = str(entry.action).rsplit(".", 1)[-1].replace("_", " ")
                target = _target_label(entry.target)
                bits: list[typing.Any] = []
                chg = _change_bits(entry)
                if chg:
                    bits.append(chg)
                if entry.reason:
                    bits.append(f"reason: {_short(entry.reason)}")
                when = entry.created_at.strftime("%Y-%m-%d %H:%M") if entry.created_at else "?"
                lines.append(
                    f"[{when}] {label} by {actor_name} on {target}"
                    + (f" ({'; '.join(bits)})" if bits else "")
                )
            except Exception as e:
                print(f"[audit] entry skipped: {e}")
                continue
    except discord.Forbidden:
        return (
            "(NOTE: owaua cannot read this server's audit log (permission "
            "denied). If asked who did what, say that honestly — do NOT guess.)"
        )
    except (discord.HTTPException, discord.NotFound) as e:
        print(f"[audit] http error: {e}")
        return ""
    except Exception as e:
        print(f"[audit] error: {e}")
        return ""
    if not lines:
        return "(the server audit log has no recent entries to show)"
    return "\n".join(lines)
