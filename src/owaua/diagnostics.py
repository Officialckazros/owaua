"""Content-free setup and runtime diagnostics for server operators."""

from __future__ import annotations

import typing
from dataclasses import dataclass

from owaua import config, db
from owaua.module_catalog import MODULES


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: str
    label: str
    detail: str


_ANY_SETUP_KEYS: dict[str, tuple[str, ...]] = {
    "action_log": ("channel_id",),
    "announcements": ("channel_id",),
    "auto_delete": ("rules",),
    "auto_message": ("messages",),
    "auto_purge": ("rules",),
    "autoresponder": ("responders",),
    "autoroles": ("join_roles", "ranks"),
    "custom_commands": ("commands",),
    "embedder": ("embeds",),
    "forms": ("forms",),
    "giveaways": ("giveaways",),
    "kick": ("subscriptions",),
    "partnerships": ("items",),
    "reaction_roles": ("menus",),
    "reddit": ("subscriptions",),
    "scheduled_digests": ("daily_channel_id", "weekly_channel_id"),
    "slowmode": ("channels",),
    "starboard": ("channel_id",),
    "tickets": ("category_id", "panels"),
    "tiktok": ("subscriptions",),
    "twitch": ("subscriptions",),
    "voice_text": ("bindings",),
    "welcome": ("channel_id", "dm_message", "rules_channel_id"),
    "youtube": ("subscriptions",),
}


def _present(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return bool(typing.cast(typing.Any, value))
    return value not in (None, False, 0)


def module_state(guild_id: str, module_name: str) -> dict[str, typing.Any]:
    """Describe whether a module is enabled, usable now, or waiting for setup."""
    record = db.module_config(guild_id, module_name)
    enabled = bool(record["enabled"])
    settings = typing.cast(dict[str, typing.Any], record["settings"])
    state = "disabled"
    reason = "disabled by a server operator"
    if enabled:
        required = _ANY_SETUP_KEYS.get(module_name, ())
        if required and not any(_present(settings.get(key)) for key in required):
            state = "needs_setup"
            reason = "enabled, but no destination or rules are configured"
        else:
            state = "ready"
            reason = "enabled and ready"
    if module_name == "moderation" and enabled and not config.SAFETY_ENABLED:
        state = "degraded"
        reason = "AI moderation is enabled for the server but disabled on the host"
    if module_name == "malware_scanner" and enabled and not config.MALWARE_SCAN_ENABLED:
        state = "degraded"
        reason = "attachment scanning is enabled for the server but disabled on the host"
    return {
        "module": module_name,
        "title": str(MODULES[module_name]["title"]),
        "state": state,
        "reason": reason,
    }


def module_states(guild_id: str) -> list[dict[str, typing.Any]]:
    return [module_state(guild_id, name) for name in MODULES]


def runtime_diagnostics(
    guild: object | None,
    *,
    task_health: dict[str, typing.Any] | None = None,
    malware_ready: bool | None = None,
) -> list[Diagnostic]:
    """Return bounded diagnostics without exposing secrets, content, or user data."""
    checks: list[Diagnostic] = []
    checks.append(
        Diagnostic(
            "ok" if config.OPENAI_API_KEY else "error",
            "AI provider",
            "configured" if config.OPENAI_API_KEY else "OPENAI_API_KEY is missing",
        )
    )
    checks.append(
        Diagnostic(
            "ok" if config.DASHBOARD_PUBLIC_URL else "warning",
            "Dashboard",
            "configured" if config.DASHBOARD_PUBLIC_URL else "public dashboard URL is missing",
        )
    )
    if not config.MALWARE_SCAN_ENABLED:
        checks.append(Diagnostic("info", "Attachment scanner", "disabled on this host"))
    elif malware_ready is not None:
        checks.append(
            Diagnostic(
                "ok" if malware_ready else "error",
                "Attachment scanner",
                "ready" if malware_ready else "enabled but unavailable",
            )
        )

    if guild is None:
        checks.append(Diagnostic("info", "Server checks", "run this command inside a server"))
    else:
        guild_id = f"guild:{getattr(guild, 'id', '')}"
        states = module_states(guild_id)
        counts = {
            state: sum(item["state"] == state for item in states)
            for state in ("ready", "needs_setup", "degraded", "disabled")
        }
        waiting = [item["title"] for item in states if item["state"] == "needs_setup"]
        checks.append(
            Diagnostic(
                "warning" if waiting else "ok",
                "Modules",
                f"{counts['ready']} ready, {counts['needs_setup']} need setup, "
                f"{counts['degraded']} degraded, {counts['disabled']} disabled"
                + (f"; next: {', '.join(waiting[:6])}" if waiting else ""),
            )
        )
        settings = db.guild_settings(guild_id)
        checks.append(
            Diagnostic(
                "ok",
                "History consent",
                "server gate is on; each member must still opt in"
                if settings.get("history_enabled")
                else "server gate is off; no ordinary guild history is stored",
            )
        )
        bot_member = getattr(guild, "me", None)
        permissions = getattr(bot_member, "guild_permissions", None)
        required = (
            ("send_messages", "Send Messages"),
            ("embed_links", "Embed Links"),
            ("read_message_history", "Read Message History"),
        )
        missing = [label for key, label in required if not getattr(permissions, key, False)]
        checks.append(
            Diagnostic(
                "error" if missing else "ok",
                "Core Discord permissions",
                "missing " + ", ".join(missing) if missing else "available",
            )
        )
        top_role = getattr(bot_member, "top_role", None)
        can_manage_roles = bool(getattr(permissions, "manage_roles", False))
        role_position = int(getattr(top_role, "position", 0) or 0)
        hierarchy_ok = can_manage_roles and role_position > 0
        checks.append(
            Diagnostic(
                "ok" if hierarchy_ok else "warning",
                "Role hierarchy",
                f"Manage Roles is available; bot role position is {role_position}"
                if hierarchy_ok
                else (
                    "grant Manage Roles only if role features are needed"
                    if not can_manage_roles
                    else "place the bot role above roles it must manage"
                ),
            )
        )

    if task_health is not None:
        failed = [
            name
            for name, state in task_health.get("background", {}).items()
            if state.get("failures")
        ]
        dropped = int(task_health.get("transient_dropped") or 0)
        checks.append(
            Diagnostic(
                "warning" if failed or dropped else "ok",
                "Background work",
                (
                    f"restarted: {', '.join(failed[:5])}; dropped transient work: {dropped}"
                    if failed or dropped
                    else "healthy"
                ),
            )
        )
    return checks


def format_report(checks: list[Diagnostic]) -> str:
    icons = {"ok": "OK", "warning": "WARN", "error": "ERROR", "info": "INFO"}
    lines = [f"**{icons.get(check.severity, 'INFO')} · {check.label}** — {check.detail}" for check in checks]
    return "\n".join(lines)[:3_900]


def setup_guide(guild_id: str | None) -> str:
    dashboard = config.DASHBOARD_PUBLIC_URL or "the configured dashboard URL"
    scope_note = (
        "Run `/doctor` here after saving settings."
        if guild_id
        else "Run `/setup` again inside the server you manage."
    )
    return (
        "1. Open "
        + dashboard
        + " and choose your server.\n"
        "2. Set destinations and rules for modules marked **needs setup**.\n"
        "3. Keep Action Log message content off unless members have been clearly told.\n"
        "4. Put the owaua role above roles it should manage and grant only needed permissions.\n"
        "5. Explain `/privacy status`, opt-in, export, and deletion to members.\n"
        f"6. {scope_note}"
    )
