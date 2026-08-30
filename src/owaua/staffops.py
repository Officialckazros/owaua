"""Durable staff operations, moderation cases, incidents, health, and exports.

This module deliberately stores structured metadata rather than message bodies.
Every record is scoped to one guild and every mutating caller supplies the
staff actor so the same event can be rendered in one consistent timeline.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import typing
from collections import Counter
from urllib.parse import urlsplit, urlunsplit

from owaua import db
from owaua.scope import is_guild_scope

CASE_STATUSES = frozenset({"open", "monitoring", "appealed", "resolved", "expired", "void"})
APPEAL_STATUSES = frozenset({"none", "pending", "accepted", "denied", "withdrawn"})
INCIDENT_STATUSES = frozenset({"open", "acknowledged", "escalated", "resolved", "dismissed"})
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
INCIDENT_SOURCES = frozenset(
    {"malware", "automod", "rules", "assistant", "moderation", "ticket", "feed", "system"}
)
_CASE_KINDS = ("moderation_case",)
_INCIDENT_KIND = "staff_incident"
_CASE_EVENT_KIND = "moderation_case_event"
_CASE_NOTE_KIND = "member_note"
_APPEAL_KIND = "moderation_appeal"
_SAFE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def _scope(guild_id: str | int) -> str:
    value = str(guild_id).strip()
    value = f"guild:{value}" if value.isdigit() else value
    if not is_guild_scope(value):
        raise ValueError("a guild scope is required")
    return value


def _actor(actor_id: str | int) -> str:
    value = str(actor_id).strip()
    if not value or len(value) > 160:
        raise ValueError("a valid actor is required")
    return value


def _text(value: object, maximum: int, *, required: bool = False) -> str:
    clean = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if required and not clean:
        raise ValueError("a value is required")
    return clean[:maximum]


def _snowflake(value: object, *, required: bool = False) -> str:
    clean = str(value or "").strip()
    if required and not clean.isdigit():
        raise ValueError("a Discord member id is required")
    if clean and (not clean.isdigit() or len(clean) > 24):
        raise ValueError("invalid Discord id")
    return clean


def _choice(value: object, choices: frozenset[str], fallback: str) -> str:
    clean = str(value or fallback).strip().lower()
    if clean not in choices:
        raise ValueError(f"unsupported value: {clean}")
    return clean


def _timestamp(value: object) -> float | None:
    if value in (None, "", 0, 0.0):
        return None
    try:
        result = float(typing.cast(typing.Any, value))
    except (TypeError, ValueError) as exc:
        raise ValueError("expiry must be a timestamp") from exc
    if not 0 < result < 32_503_680_000:
        raise ValueError("expiry is out of range")
    return result


def _evidence_links(values: object) -> list[str]:
    if values in (None, ""):
        return []
    if not isinstance(values, list):
        raise ValueError("evidence_links must be a list")
    output: list[str] = []
    for raw in typing.cast(typing.Iterable[typing.Any], values[:20]):
        value = str(raw or "").strip()
        if not value:
            continue
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError("invalid evidence link") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or len(value) > 1_000
        ):
            raise ValueError("evidence links must be normal HTTPS URLs")
        normalized = urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))
        if normalized not in output:
            output.append(normalized)
    return output


def _case_number(record_id: int) -> str:
    return f"CASE-{int(record_id):06d}"


def _event(
    guild_id: str,
    case_id: int,
    actor_id: str,
    event: str,
    detail: dict[typing.Any, typing.Any] | None = None,
) -> int:
    safe_event = str(event).strip().lower()
    if not _SAFE_TOKEN.fullmatch(safe_event):
        raise ValueError("invalid case event")
    return db.community_record_create(
        _CASE_EVENT_KIND,
        guild_id,
        {
            "case_id": int(case_id),
            "event": safe_event,
            "actor_id": _actor(actor_id),
            "detail": detail if isinstance(detail, dict) else {},
        },
        record_key=str(case_id),
    )


def create_case(
    guild_id: str | int,
    *,
    actor_id: str | int,
    subject_id: str | int,
    category: str,
    reason: str,
    severity: str = "medium",
    evidence_links: object | None = None,
    expires_at: object | None = None,
    source: str = "manual",
    assigned_to: str | int | None = None,
) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    subject = _snowflake(subject_id, required=True)
    actor = _actor(actor_id)
    category_value = _text(category, 80, required=True).lower()
    severity_value = _choice(severity, SEVERITIES, "medium")
    expiry = _timestamp(expires_at)
    assigned = _snowflake(assigned_to) if assigned_to else ""
    payload = {
        "case_number": "pending",
        "subject_id": subject,
        "category": category_value,
        "reason": _text(reason, 2_000, required=True),
        "severity": severity_value,
        "evidence_links": _evidence_links(evidence_links),
        "expires_at": expiry,
        "appeal_status": "none",
        "assigned_to": assigned,
        "source": _text(source, 80) or "manual",
        "created_by": actor,
    }
    case_id = db.community_record_create(
        "moderation_case", guild, payload, user_id=subject, status="open", due=expiry
    )
    payload["case_number"] = _case_number(case_id)
    db.community_record_update(case_id, data=payload, status="open", due=expiry)
    _event(
        guild,
        case_id,
        actor,
        "created",
        {
            "category": category_value,
            "severity": severity_value,
            "assigned_to": assigned,
        },
    )
    return get_case(guild, case_id) or {}


def _case_rows(
    guild_id: str, *, status: str | None = None, limit: int = 5_000
) -> list[dict[typing.Any, typing.Any]]:
    rows: list[dict[typing.Any, typing.Any]] = []
    for kind in _CASE_KINDS:
        rows.extend(db.community_records(kind, guild_id, status=status, limit=limit))
    return rows


def get_case(
    guild_id: str | int, case_id: int, *, include_timeline: bool = True
) -> dict[typing.Any, typing.Any] | None:
    guild = _scope(guild_id)
    wanted = int(case_id)
    row = next((item for item in _case_rows(guild, status=None) if item["id"] == wanted), None)
    if row is None:
        return None
    item = {**row, **row["data"]}
    item.pop("data", None)
    item["case_number"] = item.get("case_number") or _case_number(wanted)
    if include_timeline:
        events = [
            event
            for event in db.community_records(_CASE_EVENT_KIND, guild, status=None, limit=5_000)
            if event.get("record_key") == str(wanted)
        ]
        notes = [
            note
            for note in db.community_records(_CASE_NOTE_KIND, guild, status=None, limit=5_000)
            if note.get("record_key") == str(wanted)
        ]
        appeals = [
            appeal
            for appeal in db.community_records(_APPEAL_KIND, guild, status=None, limit=5_000)
            if appeal.get("record_key") == str(wanted)
        ]
        timeline: list[typing.Any] = []
        for event in events:
            timeline.append(
                {"id": event["id"], "kind": "event", "created": event["created"], **event["data"]}
            )
        for note in notes:
            timeline.append(
                {"id": note["id"], "kind": "note", "created": note["created"], **note["data"]}
            )
        for appeal in appeals:
            timeline.append(
                {
                    "id": appeal["id"],
                    "kind": "appeal",
                    "created": appeal["created"],
                    "status": appeal["status"],
                    **appeal["data"],
                }
            )
        item["timeline"] = sorted(timeline, key=lambda entry: (entry["created"], entry["id"]))
    return item


def search_cases(
    guild_id: str | int,
    *,
    query: str = "",
    status: str | None = None,
    subject_id: str | int | None = None,
    assigned_to: str | int | None = None,
    limit: int = 200,
) -> list[dict[typing.Any, typing.Any]]:
    guild = _scope(guild_id)
    if status is not None:
        status = _choice(status, CASE_STATUSES, "open")
    subject = _snowflake(subject_id) if subject_id else ""
    assigned = _snowflake(assigned_to) if assigned_to else ""
    needle = _text(query, 200).casefold()
    output: list[typing.Any] = []
    current_time = time.time()
    for row in reversed(_case_rows(guild, status=None)):
        data = row["data"]
        effective_status = str(row["status"])
        if (
            effective_status in {"open", "monitoring", "appealed"}
            and row.get("due")
            and float(row["due"]) <= current_time
        ):
            effective_status = "expired"
        haystack = " ".join(
            str(data.get(key) or "")
            for key in ("case_number", "subject_id", "category", "reason", "severity", "source")
        ).casefold()
        if status and effective_status != status:
            continue
        if subject and str(data.get("subject_id")) != subject:
            continue
        if assigned and str(data.get("assigned_to")) != assigned:
            continue
        if needle and needle not in haystack:
            continue
        item = {**row, **data, "status": effective_status}
        item.pop("data", None)
        output.append(item)
        if len(output) >= max(1, min(500, int(limit))):
            break
    return output


def add_member_note(
    guild_id: str | int,
    *,
    actor_id: str | int,
    subject_id: str | int,
    note: str,
    case_id: int | None = None,
) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    actor = _actor(actor_id)
    subject = _snowflake(subject_id, required=True)
    if case_id is not None:
        case = get_case(guild, int(case_id), include_timeline=False)
        if case is None or str(case.get("subject_id")) != subject:
            raise ValueError("case does not belong to this member")
    payload = {
        "subject_id": subject,
        "author_id": actor,
        "note": _text(note, 4_000, required=True),
        "case_id": int(case_id) if case_id is not None else None,
        "private": True,
    }
    note_id = db.community_record_create(
        _CASE_NOTE_KIND,
        guild,
        payload,
        user_id=subject,
        record_key=str(case_id) if case_id is not None else None,
    )
    if case_id is not None:
        _event(guild, int(case_id), actor, "note_added", {"note_id": note_id})
    return {"id": note_id, **payload, "created": time.time()}


def open_appeal(
    guild_id: str | int,
    case_id: int,
    *,
    appellant_id: str | int,
    statement: str,
) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    case = get_case(guild, case_id, include_timeline=False)
    if case is None:
        raise ValueError("case not found")
    appellant = _snowflake(appellant_id, required=True)
    if appellant != str(case.get("subject_id")):
        raise ValueError("only the case subject may submit this appeal")
    if str(case.get("appeal_status")) == "pending":
        raise ValueError("an appeal is already pending")
    appeal_id = db.community_record_create(
        _APPEAL_KIND,
        guild,
        {
            "case_id": int(case_id),
            "appellant_id": appellant,
            "statement": _text(statement, 8_000, required=True),
        },
        user_id=appellant,
        record_key=str(case_id),
        status="pending",
    )
    data = {
        key: value
        for key, value in case.items()
        if key
        not in {
            "id",
            "kind",
            "guild_id",
            "user_id",
            "record_key",
            "status",
            "due",
            "created",
            "updated",
            "timeline",
        }
    }
    data["appeal_status"] = "pending"
    db.community_record_update(
        int(case_id), data=data, status="appealed", due=case.get("expires_at")
    )
    _event(guild, int(case_id), appellant, "appeal_opened", {"appeal_id": appeal_id})
    return get_case(guild, int(case_id)) or {}


def update_case(
    guild_id: str | int,
    case_id: int,
    *,
    actor_id: str | int,
    status: str | None = None,
    assigned_to: str | int | None = None,
    appeal_status: str | None = None,
    expires_at: object | None = None,
) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    actor = _actor(actor_id)
    case = get_case(guild, case_id, include_timeline=False)
    if case is None:
        raise ValueError("case not found")
    data = {
        key: value
        for key, value in case.items()
        if key
        not in {
            "id",
            "kind",
            "guild_id",
            "user_id",
            "record_key",
            "status",
            "due",
            "created",
            "updated",
            "timeline",
        }
    }
    changes: dict[typing.Any, typing.Any] = {}
    effective_status = str(case["status"])
    if status is not None:
        effective_status = _choice(status, CASE_STATUSES, "open")
        changes["status"] = effective_status
    if assigned_to is not None:
        data["assigned_to"] = _snowflake(assigned_to) if assigned_to else ""
        changes["assigned_to"] = data["assigned_to"]
    if appeal_status is not None:
        data["appeal_status"] = _choice(appeal_status, APPEAL_STATUSES, "none")
        changes["appeal_status"] = data["appeal_status"]
        if data["appeal_status"] in {"accepted", "denied", "withdrawn"}:
            appeals = [
                item
                for item in db.community_records(_APPEAL_KIND, guild, status="pending", limit=5_000)
                if item.get("record_key") == str(case_id)
            ]
            for appeal in appeals:
                db.community_record_update(appeal["id"], status=data["appeal_status"])
    expiry = case.get("expires_at")
    if expires_at is not None:
        expiry = _timestamp(expires_at)
        data["expires_at"] = expiry
        changes["expires_at"] = expiry
    if not changes:
        raise ValueError("no case changes supplied")
    db.community_record_update(case_id, data=data, status=effective_status, due=expiry)
    _event(guild, case_id, actor, "updated", changes)
    return get_case(guild, case_id) or {}


def record_incident(
    guild_id: str | int,
    *,
    source: str,
    summary: str,
    severity: str = "medium",
    actor_id: str | int = "system",
    subject_id: str | int | None = None,
    reference: str = "",
    assigned_to: str | int | None = None,
    metadata: dict[typing.Any, typing.Any] | None = None,
) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    source_value = _choice(source, INCIDENT_SOURCES, "system")
    actor = _actor(actor_id)
    subject = _snowflake(subject_id) if subject_id else ""
    assigned = _snowflake(assigned_to) if assigned_to else ""
    safe_metadata = metadata if isinstance(metadata, dict) else {}
    encoded = json.dumps(safe_metadata, sort_keys=True, default=str)
    if len(encoded.encode("utf-8")) > 16_000:
        raise ValueError("incident metadata is too large")
    payload = {
        "source": source_value,
        "summary": _text(summary, 1_000, required=True),
        "severity": _choice(severity, SEVERITIES, "medium"),
        "actor_id": actor,
        "subject_id": subject,
        "reference": _text(reference, 1_000),
        "assigned_to": assigned,
        "metadata": safe_metadata,
    }
    record_id = db.community_record_create(
        _INCIDENT_KIND, guild, payload, user_id=subject or None, status="open"
    )
    return {
        "id": record_id,
        "kind": _INCIDENT_KIND,
        "status": "open",
        "created": time.time(),
        **payload,
    }


def update_incident(
    guild_id: str | int,
    incident_id: int,
    *,
    actor_id: str | int,
    status: str | None = None,
    assigned_to: str | int | None = None,
) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    actor = _actor(actor_id)
    row = next(
        (
            item
            for item in db.community_records(_INCIDENT_KIND, guild, status=None, limit=5_000)
            if item["id"] == int(incident_id)
        ),
        None,
    )
    if row is None:
        raise ValueError("incident not found")
    data = dict(row["data"])
    changes = {"updated_by": actor}
    effective_status = str(row["status"])
    if status is not None:
        effective_status = _choice(status, INCIDENT_STATUSES, "open")
        changes["status"] = effective_status
    if assigned_to is not None:
        data["assigned_to"] = _snowflake(assigned_to) if assigned_to else ""
        changes["assigned_to"] = data["assigned_to"]
    if len(changes) == 1:
        raise ValueError("no incident changes supplied")
    history: typing.Any = data.get("history") if isinstance(data.get("history"), list) else []
    history.append({"at": time.time(), **changes})
    data["history"] = history[-100:]
    db.community_record_update(int(incident_id), data=data, status=effective_status)
    return {**row, **data, "status": effective_status}


def incident_center(
    guild_id: str | int,
    *,
    status: str | None = None,
    source: str | None = None,
    assigned_to: str | int | None = None,
    query: str = "",
    limit: int = 250,
) -> list[dict[typing.Any, typing.Any]]:
    guild = _scope(guild_id)
    wanted_status = _choice(status, INCIDENT_STATUSES, "open") if status else None
    wanted_source = _choice(source, INCIDENT_SOURCES, "system") if source else None
    assigned = _snowflake(assigned_to) if assigned_to else ""
    needle = _text(query, 200).casefold()
    items: list[dict[typing.Any, typing.Any]] = []
    for row in db.community_records(_INCIDENT_KIND, guild, status=None, limit=5_000):
        data = row["data"]
        item = {**row, **data}
        item.pop("data", None)
        if wanted_status and item["status"] != wanted_status:
            continue
        if wanted_source and data.get("source") != wanted_source:
            continue
        if assigned and data.get("assigned_to") != assigned:
            continue
        if (
            needle
            and needle
            not in f"{data.get('summary', '')} {data.get('subject_id', '')} {data.get('reference', '')}".casefold()
        ):
            continue
        items.append(item)

    if wanted_source in (None, "ticket"):
        for ticket in db.community_records("ticket", guild, status=None, limit=5_000):
            if ticket["status"] not in {"active", "open", "waiting"}:
                continue
            data = ticket["data"]
            ticket_item = {
                "id": f"ticket:{ticket['id']}",
                "kind": "ticket",
                "source": "ticket",
                "summary": _text(data.get("subject") or "Open support ticket", 1_000),
                "severity": "medium",
                "status": "open",
                "subject_id": str(ticket.get("user_id") or ""),
                "assigned_to": str(data.get("assigned_to") or ""),
                "reference": str(data.get("channel_id") or ""),
                "created": ticket["created"],
                "updated": ticket["updated"],
            }
            if assigned and ticket_item["assigned_to"] != assigned:
                continue
            if (
                needle
                and needle
                not in f"{ticket_item['summary']} {ticket_item['subject_id']} {ticket_item['reference']}".casefold()
            ):
                continue
            items.append(ticket_item)

    if wanted_source in (None, "assistant") and wanted_status in (None, "resolved"):
        rows = (
            db.conn()
            .execute(
                "SELECT id,actor_id,channel_id,action,target_id,result,created "
                "FROM assistant_action_history WHERE scope_id=? ORDER BY created DESC LIMIT 500",
                (guild,),
            )
            .fetchall()
        )
        for row in rows:
            item = dict(row)
            rendered = {
                "id": f"assistant:{item['id']}",
                "kind": "assistant_action",
                "source": "assistant",
                "summary": f"Confirmed assistant action: {item['action']}",
                "severity": "low",
                "status": "resolved",
                "subject_id": str(item.get("target_id") or ""),
                "assigned_to": str(item.get("actor_id") or ""),
                "reference": str(item.get("channel_id") or ""),
                "created": float(item["created"]),
                "updated": float(item["created"]),
            }
            if assigned and rendered["assigned_to"] != assigned:
                continue
            if (
                needle
                and needle not in f"{rendered['summary']} {rendered['subject_id']}".casefold()
            ):
                continue
            items.append(rendered)

    items.sort(
        key=lambda item: float(item.get("updated") or item.get("created") or 0), reverse=True
    )
    return items[: max(1, min(500, int(limit)))]


def server_health(
    guild_id: str | int, guild_snapshot: dict[typing.Any, typing.Any] | None = None
) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    snapshot = guild_snapshot if isinstance(guild_snapshot, dict) else {}
    configs = {item["module"]: item for item in db.module_configs(guild)}
    settings = db.guild_settings(guild)
    recommendations: list[dict[typing.Any, typing.Any]] = []

    def recommend(code: str, severity: str, title: str, explanation: str, module: str = "") -> None:
        recommendations.append(
            {
                "code": code,
                "severity": severity,
                "title": title,
                "explanation": explanation,
                "module": module,
                "automatic_change": False,
            }
        )

    mod_channel = str(settings.get("modlog_channel") or "")
    approval_channel = str(settings.get("approval_channel") or "")
    dangerous_everyone = int(snapshot.get("everyone_permissions") or 0) & (
        0x8 | 0x20 | 0x10 | 0x10000000 | 0x20000000 | 0x40000000
    )
    if dangerous_everyone:
        recommend(
            "risky_everyone_permissions",
            "critical",
            "Review @everyone permissions",
            "The base server role currently includes a high-impact management permission. Remove broad management capabilities directly in Discord after verifying the intended access model.",
            "server_management",
        )
    if (settings.get("moderation_enabled") or settings.get("rules_enabled")) and not mod_channel:
        recommend(
            "missing_mod_log",
            "high",
            "Configure a private moderation log",
            "AI and deterministic reviews need an explicit private, bot-writable destination. No setting was changed.",
            "moderation",
        )
    if settings.get("rules_enabled") and not approval_channel:
        recommend(
            "missing_review_channel",
            "high",
            "Configure a private approval channel",
            "Rule findings cannot be safely approved without a private review channel. No setting was changed.",
            "moderation",
        )
    channel_map: typing.Any = typing.cast(
        typing.Any,
        {
            str(typing.cast(typing.Any, item).get("id")): item
            for item in snapshot.get("channels", [])
            if isinstance(item, dict)
        },
    )
    for channel_id, code, title in (
        (mod_channel, "unsafe_mod_log", "Make the moderation log private and writable"),
        (
            approval_channel,
            "unsafe_approval_channel",
            "Make the approval channel private and writable",
        ),
    ):
        channel: typing.Any = channel_map.get(channel_id) if channel_id else None
        if channel is not None and (not channel.get("private") or not channel.get("bot_writable")):
            recommend(
                code,
                "high",
                title,
                "The selected channel is visible to @everyone or the bot cannot write there. Reviews should stay in a private staff channel.",
                "moderation",
            )
    automod = configs.get("automod", {})
    automod_settings = automod.get("settings", {})
    if automod.get("enabled") and (
        len(automod_settings.get("banned_phrases", [])) > 500
        or int(automod_settings.get("rapid_messages") or 0) < 3
    ):
        recommend(
            "noisy_automod",
            "medium",
            "Review noisy automod rules",
            "This configuration is likely to create a high review volume. Sample recent findings before tightening it.",
            "automod",
        )
    tickets = [
        item
        for item in db.community_records("ticket", guild, status=None, limit=5_000)
        if item["status"] in {"active", "open", "waiting"}
    ]
    now_value = time.time()
    overdue = [item for item in tickets if now_value - float(item["updated"]) > 86_400]
    if overdue:
        recommend(
            "overdue_tickets",
            "medium",
            f"Assign {len(overdue)} overdue ticket(s)",
            "These tickets have had no recorded update for more than 24 hours. Review routing and staffing; this advisor will never close them automatically.",
            "tickets",
        )
    ticket_config = configs.get("tickets", {})
    if ticket_config.get("enabled") and not ticket_config.get("settings", {}).get("staff_role_ids"):
        recommend(
            "ticket_staff_missing",
            "high",
            "Set ticket staff roles",
            "Ticket channels need an explicit staff audience so support requests do not become orphaned.",
            "tickets",
        )
    enabled = sum(1 for item in configs.values() if item.get("enabled"))
    configured = sum(1 for item in configs.values() if item.get("updated") is not None)
    if enabled and configured < max(1, enabled // 4):
        recommend(
            "inactive_configuration",
            "low",
            "Review default-enabled modules",
            "Several modules are enabled only by defaults and have never been reviewed in the dashboard.",
        )
    booster_rows = db.booster_members(guild, limit=10_000)
    member_map = {
        str(typing.cast(typing.Any, item).get("id")): bool(
            typing.cast(typing.Any, item).get("boosting")
        )
        for item in snapshot.get("members", [])
        if isinstance(item, dict)
    }
    drift = [
        row
        for row in booster_rows
        if str(row["user_id"]) in member_map
        and bool(row["active"]) != member_map[str(row["user_id"])]
    ]
    if drift:
        recommend(
            "booster_drift",
            "medium",
            f"Reconcile {len(drift)} booster record(s)",
            "Discord's current booster state differs from the stored tracker. Run the explicit Booster sync action after reviewing the members.",
            "boosters",
        )
    if not configs.get("action_log", {}).get("enabled"):
        recommend(
            "logging_disabled",
            "medium",
            "Enable an action log",
            "A server-wide audit stream makes moderation and recovery easier. Choose private destinations before enabling it.",
            "action_log",
        )

    score = max(
        0,
        100
        - sum(
            {"low": 5, "medium": 12, "high": 22, "critical": 35}[item["severity"]]
            for item in recommendations
        ),
    )
    return {
        "generated_at": now_value,
        "score": score,
        "recommendations": recommendations,
        "counts": {
            "enabled_modules": enabled,
            "open_tickets": len(tickets),
            "overdue_tickets": len(overdue),
            "booster_drift": len(drift),
        },
        "advisory_only": True,
    }


def configure_digest(
    guild_id: str | int,
    *,
    actor_id: str | int,
    cadence: str,
    channel_id: str | int,
    visibility: str,
    enabled: bool,
    sections: list[str] | None = None,
) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    actor = _actor(actor_id)
    cadence_value = _choice(cadence, frozenset({"daily", "weekly"}), "weekly")
    visibility_value = _choice(visibility, frozenset({"staff", "admins"}), "staff")
    channel = _snowflake(channel_id, required=True)
    allowed_sections = {
        "growth",
        "moderation",
        "engagement",
        "highlights",
        "tickets",
        "feeds",
        "scheduled_messages",
    }
    clean_sections = [
        str(item)
        for item in (sections or sorted(allowed_sections))
        if str(item) in allowed_sections
    ]
    rows = db.community_records("scheduled_digest", guild, status=None, limit=50)
    existing = next((item for item in rows if item.get("record_key") == cadence_value), None)
    payload = {
        "cadence": cadence_value,
        "channel_id": channel,
        "visibility": visibility_value,
        "sections": list(dict.fromkeys(clean_sections)),
        "updated_by": actor,
    }
    status = "active" if bool(enabled) else "disabled"
    due = time.time() + (86_400 if cadence_value == "daily" else 7 * 86_400)
    if existing:
        db.community_record_update(existing["id"], data=payload, status=status, due=due)
        record_id = existing["id"]
    else:
        record_id = db.community_record_create(
            "scheduled_digest", guild, payload, record_key=cadence_value, status=status, due=due
        )
    db.dashboard_audit_record(
        guild,
        actor_id=actor,
        action="digest.configured",
        module="scheduled_digests",
        detail={
            "cadence": cadence_value,
            "channel_id": channel,
            "visibility": visibility_value,
            "enabled": bool(enabled),
        },
    )
    return {"id": record_id, "status": status, "due": due, **payload}


def digest_preview(guild_id: str | int) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    cases = search_cases(guild, limit=500)
    incidents = incident_center(guild, limit=500)
    tickets = [
        item
        for item in db.community_records("ticket", guild, status=None, limit=5_000)
        if item["status"] in {"active", "open", "waiting"}
    ]
    interactions = (
        db.conn()
        .execute(
            "SELECT kind,COUNT(*) AS count FROM interactions WHERE guild_id=? GROUP BY kind ORDER BY count DESC LIMIT 20",
            (guild,),
        )
        .fetchall()
    )
    week_ago = time.time() - 7 * 86_400
    growth_rows = (
        db.conn()
        .execute(
            "SELECT kind,COUNT(*) AS count FROM interactions WHERE guild_id=? "
            "AND created>=? AND kind IN ('member_join','member_leave') GROUP BY kind",
            (guild, week_ago),
        )
        .fetchall()
    )
    growth = {str(row["kind"]): int(row["count"]) for row in growth_rows}
    auto_messages = db.module_config(guild, "auto_message")
    upcoming = 0
    if auto_messages["enabled"]:
        for item in auto_messages["settings"].get("messages", [])[:100]:
            if (
                not isinstance(item, dict)
                or typing.cast(typing.Any, item).get("enabled", True) is False
            ):
                continue
            due = float(
                typing.cast(typing.Any, item).get("next_at")
                or typing.cast(typing.Any, item).get("first_at")
                or 0
            )
            if due and due <= time.time() + 7 * 86_400:
                upcoming += 1
    feed_failures = sum(
        item.get("source") == "feed" and item.get("status") not in {"resolved", "dismissed"}
        for item in incidents
    )
    return {
        "generated_at": time.time(),
        "growth": {
            "joins_7d": growth.get("member_join", 0),
            "leaves_7d": growth.get("member_leave", 0),
            "net_7d": growth.get("member_join", 0) - growth.get("member_leave", 0),
        },
        "moderation": {
            "open_cases": sum(
                item["status"] in {"open", "monitoring", "appealed"} for item in cases
            ),
            "new_cases_7d": sum(
                float(item["created"]) >= time.time() - 7 * 86_400 for item in cases
            ),
        },
        "incidents": Counter(
            str(item.get("source") or "unknown")
            for item in incidents
            if item.get("status") not in {"resolved", "dismissed"}
        ),
        "tickets": {
            "open": len(tickets),
            "unanswered_24h": sum(
                time.time() - float(item["updated"]) > 86_400 for item in tickets
            ),
        },
        "engagement": {str(row["kind"]): int(row["count"]) for row in interactions},
        "feeds": {"unresolved_failures": feed_failures},
        "scheduled_messages": {"upcoming_7d": upcoming},
        "privacy": "Aggregate counts only; no message content or personal profiles are included.",
    }


def retention_inventory(guild_id: str | int) -> dict[typing.Any, typing.Any]:
    guild = _scope(guild_id)
    days = int(db.guild_settings(guild).get("retention_days") or 30)
    return {
        "guild_id": guild,
        "content_retention_days": days,
        "modules": [
            {
                "module": "AI chat",
                "data": "consented conversation turns and confirmed assistant action metadata",
                "retention": f"up to {days} days",
                "export": "personal privacy export",
                "delete": "personal privacy delete",
            },
            {
                "module": "Moderation cases",
                "data": "case reason, status, staff notes, appeals, and HTTPS evidence references",
                "retention": "active while open; closed records follow server content retention",
                "export": "case subject privacy export",
                "delete": "case subject privacy delete",
            },
            {
                "module": "Incident center",
                "data": "source, severity, assignment, status, reference, and bounded metadata",
                "retention": "active while unresolved; resolved records follow server content retention",
                "export": "aggregate CSV excludes incident bodies",
                "delete": "subject-linked records covered by privacy delete",
            },
            {
                "module": "Tickets",
                "data": "owner, channel reference, subject, assignment, status, and configured SLA",
                "retention": "active until closed; closed records follow server content retention",
                "export": "personal privacy export",
                "delete": "personal privacy delete",
            },
            {
                "module": "Analytics",
                "data": "aggregate event counts only",
                "retention": f"source events up to {days} days",
                "export": "server aggregate CSV",
                "delete": "no message content or personal profile in aggregate export",
            },
        ],
    }


def analytics_csv(guild_id: str | int) -> str:
    guild = _scope(guild_id)
    rows: list[tuple[str, str, int]] = []
    for row in (
        db.conn()
        .execute(
            "SELECT kind,COUNT(*) AS count FROM interactions WHERE guild_id=? GROUP BY kind",
            (guild,),
        )
        .fetchall()
    ):
        rows.append(("interaction", str(row["kind"]), int(row["count"])))
    cases = search_cases(guild, limit=5_000)
    for status, count in Counter(str(item["status"]) for item in cases).items():
        rows.append(("moderation_case", status, count))
    incidents = incident_center(guild, limit=5_000)
    for source, count in Counter(
        str(item.get("source") or "unknown") for item in incidents
    ).items():
        rows.append(("incident", source, count))
    tickets = db.community_records("ticket", guild, status=None, limit=5_000)
    for status, count in Counter(str(item["status"]) for item in tickets).items():
        rows.append(("ticket", status, count))
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["scope", "metric", "value", "generated_at"])
    generated = int(time.time())
    for scope_name, metric, value in sorted(rows):
        writer.writerow([scope_name, metric, value, generated])
    return output.getvalue()


async def scheduler_tick(client: object) -> None:
    """Deliver due staff digests and opt-in weekly health reports privately."""
    import discord

    from owaua import embeds, moderation

    timestamp = time.time()
    for guild_snapshot in list(getattr(client, "guilds", []) or []):
        guild = _scope(guild_snapshot.id)
        for case in db.community_records(
            "moderation_case", guild, status=None, due_before=timestamp, limit=500
        ):
            if case["status"] in {"open", "monitoring", "appealed"}:
                try:
                    update_case(guild, case["id"], actor_id="system", status="expired")
                except ValueError:
                    continue
        for item in db.community_records(
            "scheduled_digest", guild, status="active", due_before=timestamp, limit=20
        ):
            data = item["data"]
            channel_id = str(data.get("channel_id") or "")
            channel = guild_snapshot.get_channel(int(channel_id)) if channel_id.isdigit() else None
            channel = await moderation._fresh_private_staff_channel(guild_snapshot, channel)
            cadence = str(data.get("cadence") or "weekly")
            interval = 86_400 if cadence == "daily" else 7 * 86_400
            if channel is None:
                db.community_record_update(item["id"], due=timestamp + min(interval, 3_600))
                continue
            preview = digest_preview(guild)
            incident_counts: typing.Any = preview.get("incidents") or {}
            incident_text = (
                ", ".join(f"{name}: {count}" for name, count in sorted(incident_counts.items()))
                or "none"
            )
            message = (
                f"**Growth (7d):** {preview['growth']['joins_7d']} joined; "
                f"{preview['growth']['leaves_7d']} left; net {preview['growth']['net_7d']:+d}\n"
                f"**Moderation:** {preview['moderation']['open_cases']} open case(s); "
                f"{preview['moderation']['new_cases_7d']} new in 7 days\n"
                f"**Open incidents:** {incident_text}\n"
                f"**Tickets:** {preview['tickets']['open']} open; "
                f"{preview['tickets']['unanswered_24h']} unanswered for 24h\n"
                f"**Engagement events:** {sum(preview['engagement'].values())}\n\n"
                f"**Feed failures:** {preview['feeds']['unresolved_failures']} · "
                f"**Upcoming scheduled messages:** {preview['scheduled_messages']['upcoming_7d']}\n\n"
                "Aggregate staff summary only; no message content or personal profiles."
            )
            try:
                await channel.send(
                    embed=embeds.say(message, title=f"{cadence.title()} staff digest"),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                db.community_record_update(
                    item["id"],
                    data={**data, "last_delivered": timestamp},
                    due=timestamp + interval,
                )
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                db.community_record_update(item["id"], due=timestamp + min(interval, 3_600))

        health_config = db.module_config(guild, "server_health")
        settings = health_config["settings"]
        if not health_config["enabled"] or not settings.get("weekly_enabled"):
            continue
        week = int(timestamp // (7 * 86_400))
        marker = f"server-health:{guild}:{week}"
        if db.kv_get(marker):
            continue
        channel_id = str(settings.get("delivery_channel_id") or "")
        channel = guild_snapshot.get_channel(int(channel_id)) if channel_id.isdigit() else None
        channel = await moderation._fresh_private_staff_channel(guild_snapshot, channel)
        if channel is None:
            continue
        snapshot = {
            "members": [
                {"id": str(member.id), "boosting": member.premium_since is not None}
                for member in list(getattr(guild_snapshot, "members", []))[:10_000]
            ]
        }
        report = server_health(guild, snapshot)
        lines = [
            f"**Health score:** {report['score']}/100",
            "This is explanatory advice only; no settings were changed.",
        ]
        for recommendation in report["recommendations"][:10]:
            lines.append(
                f"\n**{recommendation['title']}** ({recommendation['severity']})\n"
                f"{recommendation['explanation']}"
            )
        if not report["recommendations"]:
            lines.append("\nNo actionable configuration or workflow drift was detected this week.")
        try:
            await channel.send(
                embed=embeds.say("\n".join(lines)[:4_000], title="Weekly server health advisor"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            db.kv_set(marker, "1")
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            continue
