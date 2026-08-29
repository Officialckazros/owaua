"""Tool-calling registry for the /act command.

Defines the JSON schemas handed to the tool-calling model (GPT OSS 20B /
Qwen) plus the permission-gated executors. Every moderation executor checks
that the *invoking member* has the matching Discord permission and that the
target is actually actionable (role hierarchy, owner, self) before doing
anything.
"""

import datetime
import json
import logging
import typing
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import discord

log = logging.getLogger("owaua.function_registry")

MAX_TIMEOUT_MINUTES = 10080
MAX_REASON_LENGTH = 500
MAX_TOOL_CALLS = 1


@dataclass
class ActionContext:
    """Everything an executor needs to run a real action."""

    guild: discord.Guild
    actor: discord.Member
    bot: discord.Client
    channel: Optional[discord.abc.Messageable] = None
    bot_member: Optional[discord.Member] = None


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "kick_user",
            "description": "Kick a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "The target member's Discord user id.",
                    },
                    "reason": {"type": "string", "description": "Reason for the kick."},
                },
                "required": ["user_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ban_user",
            "description": "Ban a member from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "The target member's Discord user id.",
                    },
                    "reason": {"type": "string", "description": "Reason for the ban."},
                },
                "required": ["user_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timeout_user",
            "description": "Timeout (mute) a member for a number of minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "The target member's Discord user id.",
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "Timeout duration in minutes (1-10080).",
                    },
                    "reason": {"type": "string", "description": "Reason for the timeout."},
                },
                "required": ["user_id", "minutes", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_server_info",
            "description": "Get basic server aggregates (requires view_audit_log).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_info",
            "description": "Get your own member profile, or another member with view_audit_log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "The member's Discord user id."},
                },
                "required": ["user_id"],
            },
        },
    },
]


TOOL_PERMS: Dict[str, Optional[str]] = {
    "kick_user": "kick_members",
    "ban_user": "ban_members",
    "timeout_user": "moderate_members",
    "get_server_info": "view_audit_log",
    "get_user_info": None,
}

MUTATING_TOOLS = frozenset({"kick_user", "ban_user", "timeout_user"})


def _actor_has(ctx: ActionContext, perm: Optional[str]) -> bool:
    if not isinstance(ctx.actor, discord.Member):
        return False
    if getattr(ctx.actor.guild, "id", None) != getattr(ctx.guild, "id", None):
        return False
    if perm is None:
        return True
    if ctx.guild.owner_id == ctx.actor.id:
        return True
    if ctx.actor.guild_permissions.administrator:
        return True
    return bool(getattr(ctx.actor.guild_permissions, perm, False))


def _bot_has(ctx: ActionContext, perm: Optional[str]) -> bool:
    if perm is None:
        return True
    member = ctx.bot_member or getattr(ctx.guild, "me", None)
    if not isinstance(member, discord.Member):
        return False
    permissions = member.guild_permissions
    return bool(getattr(permissions, "administrator", False) or getattr(permissions, perm, False))


def _error(msg: str) -> str:
    return f"⛔ {msg}"


async def _fetch_member(
    ctx: ActionContext, user_id: str, *, fresh: bool = False
) -> Optional[discord.Member]:
    """Resolve a member by id (works without the members intent via REST)."""
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        return None
    if not fresh:
        cached = ctx.guild.get_member(int(uid))
        if cached is not None:
            return cached
    try:
        return await ctx.guild.fetch_member(int(uid))
    except (discord.NotFound, discord.HTTPException, discord.Forbidden):
        return None


def _hierarchy_ok(ctx: ActionContext, target: discord.Member) -> Optional[str]:
    """Hierarchy/self guards shared by moderation executors."""
    if target.id == getattr(getattr(ctx.bot, "user", None), "id", None):
        return _error("i'm not going to moderate myself.")
    if target.id == ctx.guild.owner_id:
        return _error("can't moderate the server owner.")
    if target.id == ctx.actor.id:
        return _error("you can't moderate yourself.")
    # Discord's administrator permission does not bypass role hierarchy.  Only
    # the guild owner may act on a member whose top role is equal or higher.
    if ctx.actor.id != ctx.guild.owner_id and ctx.actor.top_role <= target.top_role:
        return _error(f"your top role is not high enough to moderate {target.display_name}.")
    bot_member = ctx.bot_member or ctx.guild.me
    if bot_member is not None and bot_member.top_role <= target.top_role:
        return _error(f"my top role is not high enough to moderate {target.display_name}.")
    return None


async def _kick_user(ctx: ActionContext, **args: Any) -> str:
    target = await _fetch_member(ctx, args.get("user_id", ""), fresh=True)
    if target is None:
        return _error("couldn't find that member in this server.")
    blocked = _hierarchy_ok(ctx, target)
    if blocked:
        return blocked
    reason = str(args.get("reason") or "no reason given")[:MAX_REASON_LENGTH]
    try:
        await target.kick(reason=reason)
    except discord.Forbidden:
        return _error("missing kick_members permission (or role hierarchy blocks it).")
    except discord.HTTPException:
        return _error("kick failed; Discord rejected the request.")
    return f"kicked **{target.display_name}** — {reason}"


async def _ban_user(ctx: ActionContext, **args: Any) -> str:
    target = await _fetch_member(ctx, args.get("user_id", ""), fresh=True)
    if target is None:
        return _error("couldn't find that member in this server.")
    blocked = _hierarchy_ok(ctx, target)
    if blocked:
        return blocked
    reason = str(args.get("reason") or "no reason given")[:MAX_REASON_LENGTH]
    try:
        await target.ban(reason=reason)
    except discord.Forbidden:
        return _error("missing ban_members permission (or role hierarchy blocks it).")
    except discord.HTTPException:
        return _error("ban failed; Discord rejected the request.")
    return f"banned **{target.display_name}** — {reason}"


async def _timeout_user(ctx: ActionContext, **args: Any) -> str:
    target = await _fetch_member(ctx, args.get("user_id", ""), fresh=True)
    if target is None:
        return _error("couldn't find that member in this server.")
    blocked = _hierarchy_ok(ctx, target)
    if blocked:
        return blocked
    try:
        minutes = max(1, min(int(args.get("minutes", 0)), MAX_TIMEOUT_MINUTES))
    except (TypeError, ValueError):
        return _error("invalid timeout duration.")
    reason = str(args.get("reason") or "no reason given")[:MAX_REASON_LENGTH]
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
    try:
        await target.timeout(until, reason=reason)
    except discord.Forbidden:
        return _error("missing moderate_members permission (or role hierarchy blocks it).")
    except discord.HTTPException:
        return _error("timeout failed; Discord rejected the request.")
    return f"timed out **{target.display_name}** for {minutes} min — {reason}"


async def _get_server_info(ctx: ActionContext, **args: Any) -> str:
    g = ctx.guild
    info = {
        "name": g.name,
        "id": str(g.id),
        "member_count": getattr(g, "member_count", None),
        "channel_count": len(g.channels),
        "role_count": len(g.roles),
        "boost_level": getattr(g, "premium_tier", 0),
        "owner": str(getattr(g.owner, "name", g.owner_id)),
    }
    return json.dumps(info, ensure_ascii=False)


async def _get_user_info(ctx: ActionContext, **args: Any) -> str:
    target = await _fetch_member(ctx, args.get("user_id", ""), fresh=True)
    if target is None:
        return json.dumps({"error": "member not found in this server"})
    if target.id != ctx.actor.id:
        try:
            current_actor = await ctx.guild.fetch_member(ctx.actor.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return json.dumps({"error": "requester membership could not be revalidated"})
        is_owner = current_actor.id == ctx.guild.owner_id
        permissions = current_actor.guild_permissions
        if not (
            is_owner
            or getattr(permissions, "administrator", False)
            or getattr(permissions, "view_audit_log", False)
        ):
            return json.dumps({"error": "view_audit_log is required for another member"})
    info = {
        "id": str(target.id),
        "name": target.name,
        "display_name": target.display_name,
        "bot": target.bot,
        "joined_at": target.joined_at.isoformat() if target.joined_at else None,
        "roles": [r.name for r in target.roles if r.name != "@everyone"][:25],
        "top_role": target.top_role.name if target.top_role else None,
    }
    return json.dumps(info, ensure_ascii=False)


_EXECUTORS = {
    "kick_user": _kick_user,
    "ban_user": _ban_user,
    "timeout_user": _timeout_user,
    "get_server_info": _get_server_info,
    "get_user_info": _get_user_info,
}


def preview_tool(name: str, args: Dict[str, Any]) -> str:
    """Return a bounded, mention-safe tool preview for confirmation UI."""
    if name not in _EXECUTORS:
        return "unknown tool"
    values: list[typing.Any] = []
    for key in ("user_id", "minutes", "reason"):
        if key in (args or {}):
            values.append(f"{key}={str(args[key])[:120]}")
    rendered = name + (f" ({', '.join(values)})" if values else "")
    return discord.utils.escape_mentions(rendered[:400])


def audit_tool_arguments(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Return durable audit metadata without retaining free-form message text."""
    values = args if isinstance(args, dict) else {}
    audit: Dict[str, Any] = {}
    if name in _EXECUTORS:
        audit["tool"] = name
    user_id = str(values.get("user_id") or "").strip()
    if user_id.isdigit():
        audit["user_id"] = user_id
    if name == "timeout_user":
        try:
            audit["minutes"] = max(1, min(int(values.get("minutes") or 0), MAX_TIMEOUT_MINUTES))
        except (TypeError, ValueError):
            audit["minutes"] = "invalid"
    reason = str(values.get("reason") or "")
    audit["reason_supplied"] = bool(reason.strip())
    audit["reason_length"] = min(len(reason), MAX_REASON_LENGTH)
    return audit


async def execute_tool(
    name: str,
    args: Dict[str, Any],
    ctx: ActionContext,
    *,
    confirmed: bool = False,
) -> str:
    """Execute one tool call after permission checks and explicit confirmation."""
    if name not in _EXECUTORS:
        return _error(f"unknown tool `{name}`.")
    if not isinstance(args, dict):
        return _error("tool arguments must be an object; nothing was executed.")
    if name in MUTATING_TOOLS and not confirmed:
        return _error(f"confirmation required — nothing executed: {preview_tool(name, args)}")
    if name in MUTATING_TOOLS or TOOL_PERMS.get(name) is not None:
        actor_id = getattr(ctx.actor, "id", None)
        if actor_id is None or ctx.guild is None:
            return _error("the action context is invalid; nothing was executed.")
        try:
            fresh_actor = await ctx.guild.fetch_member(actor_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return _error("the requester could not be revalidated; nothing was executed.")
        fresh_bot_member = None
        if name in MUTATING_TOOLS:
            bot_id = getattr(getattr(ctx.bot, "user", None), "id", None)
            if bot_id is None:
                return _error("the bot member could not be revalidated; nothing was executed.")
            try:
                fresh_bot_member = await ctx.guild.fetch_member(bot_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return _error("the bot member could not be revalidated; nothing was executed.")
        ctx = ActionContext(
            guild=ctx.guild,
            actor=fresh_actor,
            bot=ctx.bot,
            channel=ctx.channel,
            bot_member=fresh_bot_member,
        )
    perm = TOOL_PERMS.get(name)
    if not _actor_has(ctx, perm):
        return _error(f"you need the `{perm}` permission to use `{name}` — nothing was executed.")
    if name in MUTATING_TOOLS and not _bot_has(ctx, perm):
        return _error(f"the bot needs the `{perm}` permission — nothing was executed.")
    try:
        return await _EXECUTORS[name](ctx, **(args or {}))
    except Exception:
        log.exception("tool %s failed", name)
        return _error(f"`{name}` failed; check the bot logs with the request id.")


def tool_calls_from_arguments(calls: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Parse raw tool-call dicts into ``{"name", "arguments"(dict)}``."""
    parsed: List[Dict[str, Any]] = []
    for call in (calls or [])[:MAX_TOOL_CALLS]:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        try:
            args = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            args: dict[typing.Any, typing.Any] = {}
        if not isinstance(args, dict):
            args: dict[typing.Any, typing.Any] = {}
        parsed.append({"name": name, "arguments": args})
    return parsed
