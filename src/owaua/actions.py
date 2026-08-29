"""Execution of AI-emitted actions, with permission gating.

The model can *ask* for moderation/admin actions, but the bot only performs
them when the REQUESTING user actually holds the matching Discord permission
(and the bot does too). Instructions in other people's messages can never
trigger an action — only what the requester is entitled to do themselves.
This is the only thing standing between "full server control" and any random
member pinging the bot into doing something destructive — do not weaken it.

All model-emitted actions, including reactions, are proposals until a caller
explicitly marks the request as human-confirmed.  This fail-closed default is
intentional: callers which render confirmation UI must bind that UI to the
requesting member and re-resolve permissions immediately before calling this
module with ``confirmed=True``.
"""

import datetime
import json
import logging
import math
import re
import typing
import urllib.parse
from typing import List, Optional, Union

import discord

from owaua import config

log = logging.getLogger("owaua.actions")

_PERMS = {
    "kick_user": "kick_members",
    "ban_user": "ban_members",
    "assign_role": "manage_roles",
    "remove_role": "manage_roles",
    "create_role": "manage_roles",
    "delete_role": "manage_roles",
    "dm_user": "manage_messages",
    "set_status": "manage_guild",
    "set_server_name": "manage_guild",
    "list_roles": None,
    "timeout_user": "moderate_members",
    "remove_timeout": "moderate_members",
    "set_nickname": "manage_nicknames",
    "purge_messages": "manage_messages",
    "create_channel": "manage_channels",
    "delete_channel": "manage_channels",
    "set_slowmode": "manage_channels",
    "set_channel_topic": "manage_channels",
    "react_message": None,
    "deny_media_perms": "manage_roles",
}

_MAX_REACTS = 5
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):(\d+)>")

_MAX_TIMEOUT_MINUTES = 40320
_MAX_PURGE = 100
_MAX_SLOWMODE = 21600
_MAX_ACTIONS_PER_CONFIRMATION = 1
_MAX_ASSISTANT_PROPOSALS = 5
_MAX_REASON_LENGTH = 500
_MAX_DM_LENGTH = 1500

_EXACT_MEMBER_TARGET_ACTIONS = frozenset(
    {
        "kick_user",
        "ban_user",
        "assign_role",
        "remove_role",
        "dm_user",
        "timeout_user",
        "remove_timeout",
        "set_nickname",
        "deny_media_perms",
        "list_roles",
    }
)
_EXACT_CHANNEL_TARGET_ACTIONS = frozenset(
    {
        "delete_channel",
        "set_slowmode",
        "set_channel_topic",
        "deny_media_perms",
        "purge_messages",
    }
)
_CURRENT_CHANNEL_DEFAULT_ACTIONS = frozenset(
    {
        "purge_messages",
        "set_slowmode",
        "set_channel_topic",
        "react_message",
        "deny_media_perms",
    }
)

_STATUS_KINDS = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}

_CHART_TYPES = frozenset({"bar", "line", "pie", "doughnut", "radar", "polarArea"})
_CHART_COLORS = (
    "#5865f2",
    "#57f287",
    "#fee75c",
    "#eb459e",
    "#ed4245",
    "#4e5d94",
)


_TYPE_ALIASES = {
    "rename": "set_nickname",
    "rename_user": "set_nickname",
    "change_nickname": "set_nickname",
    "nick": "set_nickname",
    "nickname": "set_nickname",
    "set_nick": "set_nickname",
    "kick": "kick_user",
    "kick_member": "kick_user",
    "ban": "ban_user",
    "ban_member": "ban_user",
    "timeout": "timeout_user",
    "mute": "timeout_user",
    "timeout_member": "timeout_user",
    "unmute": "remove_timeout",
    "untimeout": "remove_timeout",
    "remove_mute": "remove_timeout",
    "assign_role": "assign_role",
    "add_role": "assign_role",
    "give_role": "assign_role",
    "remove_role": "remove_role",
    "take_role": "remove_role",
    "delete_role": "delete_role",
    "create_role": "create_role",
    "purge": "purge_messages",
    "clear": "purge_messages",
    "purge_messages": "purge_messages",
    "delete_messages": "purge_messages",
    "dm": "dm_user",
    "dm_user": "dm_user",
    "send_dm": "dm_user",
    "pm": "dm_user",
    "react": "react_message",
    "react_message": "react_message",
    "add_reaction": "react_message",
    "slowmode": "set_slowmode",
    "remove_slowmode": "set_slowmode",
    "disable_slowmode": "set_slowmode",
    "channel_topic": "set_channel_topic",
    "set_topic": "set_channel_topic",
    "server_name": "set_server_name",
    "rename_server": "set_server_name",
    "status": "set_status",
    "deny_media": "deny_media_perms",
}

_ACTION_REQUEST_RE = re.compile(
    r"(?is)(?:"
    r"\b(?:rename|nick(?:name)?|kick|ban|mute|timeout|unmute|untimeout|purge|"
    r"slowmode)\b"
    r"|\b(?:give|assign|add|remove|take|create|delete)\b.{0,80}\b(?:role|channel)\b"
    r"|\b(?:dm|message|react\s+to)\b.{0,80}(?:<@!?\d{15,22}>|\bmessage\b)"
    r"|\b(?:set|change)\b.{0,80}\b(?:server\s+name|status|channel\s+topic)\b"
    r"|\bdeny\b.{0,80}\b(?:media|attachments?|embeds?)\b"
    r")"
)
_UNDO_REQUEST_RE = re.compile(
    r"(?is)^\s*(?:please\s+)?(?:undo|revert|reverse)(?:\s+(?:it|that|this|"
    r"the\s+(?:last|previous)\s+(?:action|change)|the\s+change))?[.!?]*\s*$"
    r"|^\s*(?:please\s+)?(?:change|put)\s+(?:it|that)\s+back[.!?]*\s*$"
)
_PLAN_REQUEST_RE = re.compile(
    r"(?is)(?:\bplan\b|\bpreview\b|\bhow\s+would\s+you\b|"
    r"\bclean\s+up\s+(?:this|the)\s+channel\b|"
    r"\bset\s+up\s+(?:an?\s+)?(?:onboarding|ticket|moderation|automod)\b)"
)
_ACTION_REQUEST_START_RE = re.compile(
    r"(?is)^\s*(?:(?:please\s+)?(?:can|could|would|will)\s+you\s+|"
    r"(?:please\s+)?(?:i\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+)|"
    r"(?:please\s+)?(?:go\s+ahead\s+and\s+)?)(?:"
    r"rename|nick(?:name)?|kick|ban|mute|timeout|unmute|untimeout|purge|clear|"
    r"give|assign|add|remove|take|create|delete|dm|message|react|set|change|"
    r"update|deny|enable|disable|turn)\b"
)
_TERSE_SLOWMODE_REQUEST_RE = re.compile(
    r"(?is)^\s*slowmode\s+(?:off|on|remove|disable|enable|stop|reset|\d)"
)
_USER_MENTION_RE = re.compile(r"<@!?(\d{15,22})>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d{15,22})>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d{15,22})>")
_DURATION_RE = re.compile(r"(?i)(\d{1,6})\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?\b")


def _role_name(value: object) -> str:
    """Return a comparison-safe role name, without accepting an ID as a name."""
    return " ".join(str(value or "").split()).casefold()


def assistant_proposals(raw_actions: object) -> List[dict[typing.Any, typing.Any]]:
    """Return up to five valid assistant proposals, or fail closed.

    Each proposal is still confirmed and executed separately.  The small batch
    limit lets one request express related work without turning Confirm into a
    blanket approval.  Internal dependency fields are derived here, never
    trusted from model output.
    """
    if isinstance(raw_actions, dict):
        candidates: typing.Any = [raw_actions]
    elif (
        isinstance(raw_actions, list)
        and 1 <= len(typing.cast(typing.Any, raw_actions)) <= _MAX_ASSISTANT_PROPOSALS
    ):
        candidates: typing.Any = typing.cast(typing.Any, raw_actions)
    else:
        return []
    if not all(
        isinstance(candidate, dict) and action_type(typing.cast(typing.Any, candidate)) is not None
        for candidate in candidates
    ):
        return []

    proposals: List[dict[typing.Any, typing.Any]] = []
    created_role_names: dict[str, str] = {}
    for candidate in candidates:
        # Do not permit a model to manufacture internal execution metadata.
        proposal: typing.Any = {
            key: value for key, value in candidate.items() if not str(key).startswith("_assistant_")
        }
        if action_type(proposal) == "assign_role":
            role: typing.Any = (
                proposal.get("role") or proposal.get("role_name") or proposal.get("name")
            )
            normalized = _role_name(role)
            if _role_id(role) is None and normalized in created_role_names:
                proposal["_assistant_depends_on_role_name"] = created_role_names[normalized]
        if action_type(proposal) == "create_role":
            name: typing.Any = (
                proposal.get("role") or proposal.get("name") or proposal.get("role_name")
            )
            normalized = _role_name(name)
            if normalized:
                created_role_names[normalized] = str(name).strip()
        proposals.append(proposal)
    return proposals


def bind_assistant_channel_scope(
    proposal: dict[typing.Any, typing.Any], user_text: str
) -> Optional[dict[typing.Any, typing.Any]]:
    """Bind omitted channel targets to the live confirmation channel.

    Models occasionally copy a visible user or guild id into the optional
    ``channel`` field.  For actions documented to default to the current
    channel, trust a target only when the user supplied that exact channel id
    or mention.  Otherwise omit it so confirmation-time context is authoritative.
    """
    canonical = action_type(proposal)
    if canonical not in _CURRENT_CHANNEL_DEFAULT_ACTIONS:
        return dict(proposal)

    text = str(user_text or "")
    requested_ids = set(_CHANNEL_MENTION_RE.findall(text))
    requested_ids.update(re.findall(r"(?<![<@&!])\b(\d{15,22})\b", text))
    scoped = dict(proposal)
    if not requested_ids:
        scoped.pop("channel", None)
        return scoped

    proposed_id = _channel_id(scoped.get("channel"))
    if proposed_id is None or str(proposed_id) not in requested_ids:
        return None
    scoped["channel"] = str(proposed_id)
    return scoped


def looks_like_action_request(text: str) -> bool:
    """Conservatively identify imperative Discord-action requests.

    Merely discussing an action (for example, ``how does slowmode work?``) must
    not replace a useful answer with an action-resolution error.
    """
    value = str(text or "").strip()
    if not _ACTION_REQUEST_RE.search(value):
        return False
    return bool(_ACTION_REQUEST_START_RE.search(value) or _TERSE_SLOWMODE_REQUEST_RE.search(value))


def looks_like_plan_request(text: str) -> bool:
    """Recognize broad staff requests that need a non-mutating preview first."""
    return bool(_PLAN_REQUEST_RE.search(str(text or "")))


def assistant_plan(raw_plan: object, user_text: str = "") -> List[dict[typing.Any, typing.Any]]:
    """Validate a model-authored preview without executing any step."""
    if not isinstance(raw_plan, list) or not 1 <= len(typing.cast(typing.Any, raw_plan)) <= 10:
        return []
    output: List[dict[typing.Any, typing.Any]] = []
    for index, raw in typing.cast(
        typing.Iterable[typing.Any], enumerate(typing.cast(typing.Any, raw_plan), 1)
    ):
        if not isinstance(raw, dict):
            return []
        title = " ".join(str(typing.cast(typing.Any, raw).get("title") or f"Step {index}").split())[
            :120
        ]
        explanation = " ".join(str(typing.cast(typing.Any, raw).get("explanation") or "").split())[
            :500
        ]
        permission = " ".join(str(typing.cast(typing.Any, raw).get("permission") or "").split())[
            :120
        ]
        mutation = bool(typing.cast(typing.Any, raw).get("mutation"))
        action: typing.Any = typing.cast(typing.Any, raw).get("action")
        proposal = None
        if action not in (None, {}, []):
            proposals = assistant_proposals(action)
            if len(proposals) != 1:
                return []
            proposal = bind_assistant_channel_scope(proposals[0], user_text)
            if proposal is None:
                return []
            mutation = is_state_changing(proposal)
            required = _PERMS.get(action_type(proposal) or "")
            if required:
                permission = required
        if mutation and proposal is None:
            return []
        output.append(
            {
                "step": index,
                "title": title,
                "explanation": explanation,
                "permission": permission or "none",
                "mutation": mutation,
                "action": proposal,
            }
        )
    return output


def render_assistant_plan(plan: List[dict[typing.Any, typing.Any]]) -> str:
    lines = [
        "Plan preview — nothing has changed. Each mutation gets its own permission check and Confirm button:",
    ]
    for step in plan:
        suffix = f"permission: `{step['permission']}`"
        suffix += "; separate confirmation required" if step.get("mutation") else "; review-only"
        detail = f" — {step['explanation']}" if step.get("explanation") else ""
        lines.append(f"{step['step']}. **{step['title']}**{detail} ({suffix})")
    lines.append(
        "Ask me to execute one specific step when you are ready; I will preview that single mutation again."
    )
    return "\n".join(lines)[:3_900]


def _duration_seconds(match: re.Match[str]) -> int:
    amount = int(match.group(1))
    unit = str(match.group(2) or "s").lower()
    if unit.startswith(("m", "min")):
        amount *= 60
    elif unit.startswith(("h", "hr")):
        amount *= 3600
    return max(0, min(_MAX_SLOWMODE, amount))


def infer_assistant_proposal(text: str) -> Optional[dict[typing.Any, typing.Any]]:
    """Recover a small set of unambiguous proposals from plain user text.

    This is a resilience fallback for malformed/missing model ``actions``.  It
    deliberately covers only requests whose parameters can be read exactly;
    the returned action still goes through Confirm and all live permission and
    hierarchy checks before anything changes.
    """
    value = " ".join(str(text or "").strip().split())
    if not value or not looks_like_action_request(value):
        return None
    lowered = value.lower()
    channel_match = _CHANNEL_MENTION_RE.search(value)
    channel = channel_match.group(1) if channel_match else None

    if "slowmode" in lowered:
        proposal: dict[str, typing.Any] = {"type": "set_slowmode"}
        if channel:
            proposal["channel"] = channel
        if re.search(r"\b(?:off|remove|disable|stop|reset)\b", lowered):
            proposal["seconds"] = 0
            return proposal
        tail = value[lowered.find("slowmode") + len("slowmode") :]
        duration = _DURATION_RE.search(tail)
        if duration:
            proposal["seconds"] = _duration_seconds(duration)
            return proposal
        return None

    purge = re.search(
        r"(?i)\b(?:purge|clear|delete)\s+(?:the\s+)?(?:last\s+)?(\d{1,3})"
        r"(?:\s+messages?)?\b",
        value,
    )
    if purge and "channel" not in lowered:
        proposal = {"type": "purge_messages", "count": min(100, int(purge.group(1)))}
        if channel:
            proposal["channel"] = channel
        return proposal

    user_match = _USER_MENTION_RE.search(value)
    user = user_match.group(1) if user_match else None
    role_match = _ROLE_MENTION_RE.search(value)
    role = role_match.group(1) if role_match else None

    if user and role:
        if re.search(r"(?i)\b(?:give|assign|add)\b", value):
            return {"type": "assign_role", "target_user": user, "role": role}
        if re.search(r"(?i)\b(?:remove|take)\b", value):
            return {"type": "remove_role", "target_user": user, "role": role}

    if user:
        if re.search(r"(?i)\b(?:unmute|untimeout|remove\s+(?:the\s+)?timeout)\b", value):
            return {"type": "remove_timeout", "target_user": user}
        if re.search(r"(?i)\bkick\b", value):
            return {"type": "kick_user", "target_user": user}
        if re.search(r"(?i)\bban\b", value):
            return {"type": "ban_user", "target_user": user}

    if role and re.search(r"(?i)\bdelete\b.{0,40}\brole\b|\bdelete\s*<@&", value):
        return {"type": "delete_role", "role": role}
    if channel and re.search(r"(?i)\bdelete\b.{0,40}\bchannel\b|\bdelete\s*<#", value):
        return {"type": "delete_channel", "channel": channel}

    topic = re.search(
        r"(?is)\b(?:set|change|update)\s+(?:the\s+)?(?:channel\s+)?topic\s+"
        r"(?:to\s+)?(.+)$",
        value,
    )
    if topic:
        proposal = {"type": "set_channel_topic", "topic": topic.group(1).strip()[:1024]}
        if channel:
            proposal["channel"] = channel
        return proposal

    server_name = re.search(
        r"(?is)\b(?:set|change|rename)\s+(?:the\s+)?server(?:\s+name)?\s+"
        r"(?:to\s+)?(.+)$",
        value,
    )
    if server_name:
        name = server_name.group(1).strip().strip("'\"")[:100]
        if name:
            return {"type": "set_server_name", "name": name}
    return None


def assistant_resolution_message(text: str, model_response: str = "") -> str:
    """Keep a useful model clarification or return action-specific guidance."""
    response = str(model_response or "").strip()
    if response.endswith("?") and len(response) <= 500:
        return response
    lowered = str(text or "").lower()
    if "slowmode" in lowered:
        return (
            "What slowmode delay should I use? Say something like `slowmode 10 seconds` "
            "or `slowmode off`; mention a channel only if you do not mean this one."
        )
    if re.search(r"\b(?:kick|ban|mute|timeout|unmute|untimeout|nickname|rename|dm)\b", lowered):
        return "Mention the exact user and include any duration, nickname, or message needed."
    if "role" in lowered:
        return "Mention the exact user and role, or give the new role name, and try again."
    if "channel" in lowered or "topic" in lowered:
        return "Mention the exact channel and include the new name or topic, then try again."
    return "I recognized a Discord action, but I still need its exact target or value."


def resolve_assistant_output(
    user_text: str,
    raw_actions: object,
    model_response: str,
    *,
    in_guild: bool,
    leak_blocked: bool = False,
    raw_plan: object | None = None,
) -> tuple[str, List[dict[typing.Any, typing.Any]]]:
    """Resolve one assistant turn into safe copy and an ordered proposal batch.

    Prefix and slash commands share this boundary so model-output quirks cannot
    make the two public assistant interfaces behave differently.  A proposal
    remains inert here: callers must still render an invoker-bound confirmation
    and execute it with ``confirmed=True``.
    """
    response = str(model_response or "").strip()
    if leak_blocked:
        return response, []
    plan = assistant_plan(raw_plan, user_text)
    if plan:
        if not in_guild and any(step.get("mutation") for step in plan):
            return "Discord configuration plans only work inside a server; nothing was changed.", []
        return render_assistant_plan(plan), []
    if looks_like_plan_request(user_text) and not raw_plan:
        return (
            "I can preview that safely, but I need a step-by-step plan before proposing changes. "
            "Nothing has changed; try again and I will list permissions and one confirmation per mutation.",
            [],
        )
    proposals = assistant_proposals(raw_actions)
    if proposals:
        scoped_proposals = [
            bind_assistant_channel_scope(proposal, user_text) for proposal in proposals
        ]
        proposals = (
            [proposal for proposal in scoped_proposals if proposal is not None]
            if all(scoped_proposals)
            else []
        )

    requested_action = looks_like_action_request(user_text)
    if requested_action and not proposals:
        inferred = infer_assistant_proposal(user_text)
        proposals = [inferred] if inferred else []

    if proposals and in_guild:
        previews = "\n".join(
            f"{index}. `{preview_action(typing.cast(typing.Any, proposal))}`"
            for index, proposal in enumerate(proposals, 1)
        )
        instruction = (
            "Confirm each action below in order. Nothing has changed yet; every "
            "action is checked again when you confirm it."
            if len(proposals) > 1
            else "Nothing has changed yet; use Confirm below to execute it."
        )
        return f"Ready for {len(proposals)} action(s):\n{previews}\n{instruction}", proposals
    if requested_action and not in_guild:
        return "Discord actions only work inside a server; nothing was changed.", []
    if requested_action and not proposals:
        return assistant_resolution_message(user_text, response), []
    return response, []


def is_undo_request(text: str) -> bool:
    """Return whether an assistant turn is an unambiguous last-action undo."""
    return bool(_UNDO_REQUEST_RE.fullmatch(str(text or "").strip()))


def audit_action_arguments(action: dict[typing.Any, typing.Any]) -> dict[typing.Any, typing.Any]:
    """Create bounded audit metadata without retaining message/reason bodies."""
    if not isinstance(action, dict):
        return {}
    out: dict[str, typing.Any] = {"type": action_type(action) or "unknown"}
    sensitive = {"reason", "message", "content", "text", "dm_content"}
    for key, value in action.items():
        key = str(key)[:80]
        if key in {"type", "action", "name"}:
            continue
        if key in sensitive:
            rendered = str(value or "")
            out[f"{key}_supplied"] = bool(rendered)
            out[f"{key}_length"] = len(rendered)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value[:200] if isinstance(value, str) else value
    return out


def action_target_id(action: dict[typing.Any, typing.Any]) -> Optional[str]:
    """Extract an exact numeric target id for audit indexing when available."""
    if not isinstance(action, dict):
        return None
    raw = (
        action.get("target_user")
        or action.get("user")
        or action.get("target")
        or action.get("member")
        or action.get("user_id")
        or action.get("target_member")
    )
    uid = _uid(raw)
    return str(uid) if uid is not None else None


def is_state_changing(action: dict[typing.Any, typing.Any]) -> bool:
    """Whether a confirmed proposal should enter the assistant undo ledger."""
    action_name = action_type(action)
    return action_name is not None and action_name != "list_roles"


def action_results_ok(
    results: List[str], action: Optional[dict[typing.Any, typing.Any]] = None
) -> bool:
    """Recognize only executor success messages; unknown wording fails closed."""
    if len(results or []) != 1:
        return False
    line = str(results[0]).lower()
    success_prefixes = (
        "kicked ",
        "banned ",
        "gave ",
        "removed ",
        "created role ",
        "deleted role ",
        "dm'd ",
        "timed out ",
        "cleared timeout for ",
        "set ",
        "purged ",
        "created #",
        "deleted #",
        "slowmode in #",
        "updated #",
        "renamed server to ",
        "status set to ",
        "denied attach files and embed links for ",
        "reacted ",
    )
    if any(line.startswith(prefix) for prefix in success_prefixes):
        return "failed" not in line
    return action_type(action or {}) == "list_roles" and " roles: " in line


def _uid(raw: typing.Any) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip().strip("<@!>").strip()
    return int(s) if s.isdigit() else None


def _channel_id(raw: typing.Any) -> Optional[int]:
    if raw is None:
        return None
    value = str(raw).strip()
    if value.startswith("<#") and value.endswith(">"):
        value = value[2:-1]
    return int(value) if value.isdigit() else None


def _role_id(raw: typing.Any) -> Optional[int]:
    if raw is None:
        return None
    value = str(raw).strip()
    if value.startswith("<@&") and value.endswith(">"):
        value = value[3:-1]
    return int(value) if value.isdigit() else None


def chart_url(raw_chart: object) -> Optional[str]:
    """Build a bounded QuickChart URL from a small, data-only chart schema.

    Arbitrary Chart.js options are deliberately ignored so model output cannot
    inject scriptable callbacks or make an unbounded URL. Invalid charts simply
    render without an image.
    """
    if not isinstance(raw_chart, dict):
        return None
    chart_type = str(typing.cast(typing.Any, raw_chart).get("type") or "bar")
    if chart_type not in _CHART_TYPES:
        return None
    labels_raw: typing.Any = typing.cast(typing.Any, raw_chart).get("labels")
    datasets_raw: typing.Any = typing.cast(typing.Any, raw_chart).get("datasets")
    if not isinstance(labels_raw, list) or not isinstance(datasets_raw, list):
        return None
    labels: typing.Any = typing.cast(
        typing.Any,
        [str(label)[:50] for label in typing.cast(typing.Iterable[typing.Any], labels_raw[:20])],
    )
    if not labels:
        return None
    datasets: list[typing.Any] = []
    for index, raw_dataset in typing.cast(
        typing.Iterable[typing.Any], enumerate(typing.cast(typing.Any, datasets_raw[:5]))
    ):
        if not isinstance(raw_dataset, dict) or not isinstance(
            typing.cast(typing.Any, raw_dataset).get("data"), list
        ):
            continue
        values: list[typing.Any] = []
        for raw_value in typing.cast(
            typing.Iterable[typing.Any], raw_dataset["data"][: len(labels)]
        ):
            if isinstance(raw_value, bool):
                values.append(0)
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = 0.0
            if not math.isfinite(value):
                value = 0.0
            values.append(max(-1_000_000_000, min(1_000_000_000, value)))
        values.extend([0.0] * (len(labels) - len(values)))
        color: typing.Any = typing.cast(typing.Any, _CHART_COLORS[index % len(_CHART_COLORS)])
        datasets.append(
            {
                "label": str(
                    typing.cast(typing.Any, raw_dataset).get("label") or f"Series {index + 1}"
                )[:50],
                "data": values,
                "borderColor": color,
                "backgroundColor": color,
            }
        )
    if not datasets:
        return None
    config_payload = {
        "type": chart_type,
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "animation": False,
            "plugins": {"legend": {"display": len(datasets) > 1}},
        },
    }
    encoded = urllib.parse.urlencode(
        {"c": json.dumps(config_payload, ensure_ascii=False, separators=(",", ":"))}
    )
    url = f"https://quickchart.io/chart?{encoded}"
    return url if len(url) <= 8_000 else None


def _has(member: discord.Member, perm: Optional[str], channel: typing.Any = None) -> bool:
    """Whether *member* holds *perm*.

    Guild owner and administrator always pass. When *channel* is given, use
    effective channel overwrites (not just guild-level flags) so a denied
    override in #mod-only can't be bypassed via the bot.
    """
    if perm is None:
        return True
    if member is None or not isinstance(member, discord.Member):
        return False
    if member.guild.owner_id == member.id:
        return True
    if channel is not None and hasattr(channel, "permissions_for"):
        perms = channel.permissions_for(member)
    else:
        perms = member.guild_permissions
    if getattr(perms, "administrator", False):
        return True
    return bool(getattr(perms, perm, False))


def _is_slowmode_channel(channel: typing.Any) -> bool:
    """Whether Discord exposes per-user slowmode editing for this channel."""
    return isinstance(
        channel,
        (
            discord.TextChannel,
            discord.Thread,
            discord.ForumChannel,
            discord.VoiceChannel,
            discord.StageChannel,
        ),
    )


def _slowmode_permission(channel: typing.Any) -> str:
    """Return the effective Discord permission needed to edit slowmode."""
    return "manage_threads" if isinstance(channel, discord.Thread) else "manage_channels"


def _bot_member(guild: typing.Any) -> Optional[discord.Member]:
    if guild is None:
        return None
    return getattr(guild, "me", None)


def _role_above(actor: discord.Member, other: discord.Member) -> bool:
    """True if actor's top role is strictly above other's (owners always win)."""
    if actor is None or other is None:
        return False
    if actor.guild.owner_id == actor.id:
        return True
    if other.guild.owner_id == other.id:
        return False
    return actor.top_role > other.top_role


def _bot_can_act_on(
    guild: typing.Any, target: discord.Member, bot_member: Optional[discord.Member] = None
) -> Optional[str]:
    """Return an error string if the bot cannot moderate *target*, else None."""
    me = bot_member or _bot_member(guild)
    if me is None:
        return "blocked: I am not a member of this server"
    if target.id == me.id:
        return "blocked: I won't act on myself"
    if target.guild.owner_id == target.id:
        return "blocked: can't moderate the server owner"
    if not _role_above(me, target):
        return f"blocked: my role is not above {target.display_name}'s (move my role higher)"
    return None


def _requester_can_act_on(requester: discord.Member, target: discord.Member) -> Optional[str]:
    """Prevent junior mods from using the bot to moderate seniors."""
    if requester.id == target.id:
        return "denied: you can't use a moderation action on yourself"
    if not _role_above(requester, target):
        return (
            f"denied: your role is not above {target.display_name}'s "
            f"(can't moderate equals or seniors via the bot)"
        )
    return None


def _bot_can_manage_role(
    guild: typing.Any, role: discord.Role, bot_member: Optional[discord.Member] = None
) -> Optional[str]:
    me = bot_member or _bot_member(guild)
    if me is None:
        return "blocked: I am not a member of this server"
    if not me.guild_permissions.manage_roles and not me.guild_permissions.administrator:
        return "blocked: I need `manage_roles`"
    if role.is_default():
        return "blocked: can't manage @everyone that way"
    if role >= me.top_role:
        return f"blocked: role `{role.name}` is at or above my top role (move my role higher)"
    if role.managed:
        return f"blocked: `{role.name}` is managed by an integration"
    return None


async def _resolve_role(
    guild: discord.Guild | None, raw: typing.Any, *, fresh: bool = False
) -> Optional[discord.Role]:
    """Resolve a role by exact id or mention, optionally bypassing the cache."""
    if guild is None or raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    role_id = _role_id(s)
    if role_id is None:
        return None
    if fresh:
        try:
            return discord.utils.get(await guild.fetch_roles(), id=role_id)
        except (discord.Forbidden, discord.HTTPException):
            return None
    return guild.get_role(role_id)


async def _resolve_member(
    guild: typing.Any, raw: typing.Any, requester: typing.Any = None, *, fresh: bool = False
) -> Optional[discord.Member]:
    """Resolve a user id, mention, or name to a Member."""
    if raw is None or guild is None:
        return None
    s_raw = str(raw).strip()
    if s_raw.lower() in ("me", "myself", "self") and requester is not None:
        if isinstance(requester, discord.Member):
            return requester
        m = guild.get_member(getattr(requester, "id", None))
        if m:
            return m
    uid = _uid(raw)
    if uid is not None:
        if fresh:
            try:
                return await guild.fetch_member(uid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        m = guild.get_member(uid)
        if m:
            return m
        try:
            return await guild.fetch_member(uid)
        except discord.HTTPException:
            log.debug("member fetch failed in guild %s", getattr(guild, "id", None))
    s = s_raw.lstrip("@").lower()
    if not s:
        return None
    for m in guild.members:
        if (
            m.name.lower() == s
            or m.display_name.lower() == s
            or (getattr(m, "global_name", None) and m.global_name.lower() == s)
            or (getattr(m, "nick", None) and m.nick and m.nick.lower() == s)
        ):
            return m
    try:
        members = await guild.query_members(query=s, limit=5)
        if members:
            return members[0]
    except discord.HTTPException:
        log.debug("member query failed in guild %s", getattr(guild, "id", None))
    return None


async def _resolve_channel(
    guild: typing.Any, current_channel: typing.Any, raw_name: typing.Any, *, fresh: bool = False
):
    """Look up a channel by name or id.

    An omitted target uses the request channel.  An explicit but unknown target
    fails closed instead of silently applying a mutation to the request channel.
    """
    name = str(raw_name or "").strip()
    channel_id = _channel_id(name) if name else getattr(current_channel, "id", None)
    if channel_id is None:
        return None
    if fresh:
        try:
            return await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    ch = guild.get_channel(channel_id)
    if ch is not None:
        return ch
    return None


def action_type(action: dict[typing.Any, typing.Any]) -> Optional[str]:
    """Return the canonical action type for an untrusted model proposal."""
    if not isinstance(action, dict):
        return None
    raw_type = action.get("type") or action.get("action") or action.get("name")
    canonical = _TYPE_ALIASES.get(str(raw_type or "").strip().lower(), raw_type)
    return str(canonical) if canonical in _PERMS else None


def preview_action(action: dict[typing.Any, typing.Any]) -> str:
    """Create a bounded, mention-safe summary for confirmation UI."""
    canonical = action_type(action)
    if canonical is None:
        return "unknown action"
    target = (
        action.get("target_user")
        or action.get("user")
        or action.get("target")
        or action.get("member")
        or action.get("user_id")
        or action.get("target_member")
    )
    details: list[typing.Any] = []
    if target is not None:
        details.append(f"target={str(target)[:80]}")
    if action.get("channel") is not None:
        details.append(f"channel={str(action['channel'])[:80]}")
    role = action.get("role") or action.get("role_name")
    if role is not None:
        details.append(f"role={str(role)[:80]}")
    if canonical == "set_nickname":
        nickname = (
            action.get("nickname")
            or action.get("nick")
            or action.get("new_nickname")
            or action.get("new_nick")
        )
        details.append(f"nickname={str(nickname)[:80] if nickname else '(reset)'}")
    if canonical in {"create_role", "create_channel", "set_server_name"}:
        name = action.get("name")
        if name is not None:
            details.append(f"name={str(name)[:80]}")
    if canonical == "set_slowmode":
        details.append(f"seconds={str(action.get('seconds') or 0)[:8]}")
    if canonical == "set_channel_topic":
        details.append(f"topic={str(action.get('topic') or '')[:100]}")
    if canonical == "purge_messages":
        details.append(f"count={str(action.get('count') or action.get('amount') or 10)[:8]}")
    reason = str(action.get("reason") or "").strip()
    if reason:
        details.append(f"reason={reason[:120]}")
    summary = canonical + (f" ({', '.join(details)})" if details else "")
    return discord.utils.escape_mentions(summary[:400])


def _member_target(action: dict[typing.Any, typing.Any]):
    return (
        action.get("target_user")
        or action.get("user")
        or action.get("target")
        or action.get("member")
        or action.get("user_id")
        or action.get("target_member")
    )


async def prepare_inverse(
    action: dict[typing.Any, typing.Any],
    requester: typing.Any,
    guild: typing.Any,
    channel: typing.Any = None,
) -> Optional[dict[typing.Any, typing.Any]]:
    """Capture the pre-action state needed for a safe one-step undo."""
    action_name = action_type(action)
    if guild is None or action_name is None:
        return None
    target_raw = _member_target(action)
    target = None
    if target_raw is not None:
        target = await _resolve_member(guild, target_raw, requester=requester, fresh=True)

    if action_name == "set_nickname" and target is not None:
        requested = (
            str(
                action.get("nickname")
                or action.get("nick")
                or action.get("name")
                or action.get("new_nickname")
                or action.get("new_nick")
                or ""
            ).strip()
            or None
        )
        if requested == getattr(target, "nick", None):
            return None
        return {
            "type": "set_nickname",
            "target_user": str(target.id),
            "nickname": getattr(target, "nick", None) or "",
            "reason": "revert previous assistant action",
        }

    if action_name in {"assign_role", "remove_role"} and target is not None:
        role_raw = action.get("role") or action.get("role_name") or action.get("name")
        role = await _resolve_role(guild, role_raw, fresh=True)
        if role is None:
            return None
        has_role = any(getattr(item, "id", None) == role.id for item in target.roles)
        if (action_name == "assign_role" and has_role) or (
            action_name == "remove_role" and not has_role
        ):
            return None
        return {
            "type": "remove_role" if action_name == "assign_role" else "assign_role",
            "target_user": str(target.id),
            "role": str(role.id),
            "reason": "revert previous assistant action",
        }

    if action_name in {"timeout_user", "remove_timeout"} and target is not None:
        old_until = getattr(target, "timed_out_until", None)
        now_utc = discord.utils.utcnow()
        if old_until is not None and old_until.tzinfo is None:
            old_until = old_until.replace(tzinfo=datetime.timezone.utc)
        if old_until is not None and old_until > now_utc:
            return {
                "type": "timeout_user",
                "target_user": str(target.id),
                "until": old_until.isoformat(),
                "reason": "revert previous assistant action",
            }
        if action_name == "timeout_user":
            return {
                "type": "remove_timeout",
                "target_user": str(target.id),
                "reason": "revert previous assistant action",
            }
        return None

    if action_name in {"set_slowmode", "set_channel_topic"}:
        scoped = await _resolve_channel(guild, channel, action.get("channel"), fresh=True)
        if scoped is None:
            return None
        if action_name == "set_slowmode":
            try:
                requested = int(action.get("seconds") or 0)
            except (TypeError, ValueError):
                requested = 0
            previous = int(getattr(scoped, "slowmode_delay", 0) or 0)
            if requested == previous:
                return None
            return {
                "type": "set_slowmode",
                "channel": str(scoped.id),
                "seconds": previous,
                "reason": "revert previous assistant action",
            }
        requested = str(action.get("topic") or "")[:1024]
        previous = str(getattr(scoped, "topic", None) or "")[:1024]
        if requested == previous:
            return None
        return {
            "type": "set_channel_topic",
            "channel": str(scoped.id),
            "topic": previous,
            "reason": "revert previous assistant action",
        }

    if action_name == "set_server_name":
        requested = str(action.get("name") or "").strip()
        previous = str(getattr(guild, "name", ""))
        if not previous or requested == previous:
            return None
        return {
            "type": "set_server_name",
            "name": previous,
            "reason": "revert previous assistant action",
        }

    if action_name == "create_role":
        try:
            existing_roles = await guild.fetch_roles()
        except (discord.Forbidden, discord.HTTPException):
            existing_roles = getattr(guild, "roles", [])
        return {
            "_created_type": "role",
            "name": str(action.get("role") or action.get("name") or action.get("role_name") or "")[
                :100
            ],
            "existing_ids": [str(role.id) for role in existing_roles],
        }
    if action_name == "create_channel":
        try:
            existing_channels = await guild.fetch_channels()
        except (discord.Forbidden, discord.HTTPException):
            existing_channels = getattr(guild, "channels", [])
        return {
            "_created_type": "channel",
            "name": str(action.get("name") or "")[:100],
            "existing_ids": [str(item.id) for item in existing_channels],
        }
    return None


async def finalize_inverse(
    seed: Optional[dict[typing.Any, typing.Any]], guild: typing.Any
) -> Optional[dict[typing.Any, typing.Any]]:
    """Resolve ids for newly created resources after a successful action."""
    if not seed or guild is None:
        return None
    created_type = seed.get("_created_type")
    if created_type is None:
        return seed if action_type(seed) is not None else None
    existing: typing.Any = typing.cast(
        typing.Any,
        {
            str(value)
            for value in typing.cast(typing.Iterable[typing.Any], seed.get("existing_ids") or [])
        },
    )
    name = str(seed.get("name") or "")
    try:
        if created_type == "role":
            resources = await guild.fetch_roles()
            inverse_type = "delete_role"
            target_key = "role"
        elif created_type == "channel":
            resources = await guild.fetch_channels()
            inverse_type = "delete_channel"
            target_key = "channel"
        else:
            return None
    except (discord.Forbidden, discord.HTTPException):
        return None
    candidates = [
        item
        for item in resources
        if str(getattr(item, "id", "")) not in existing and getattr(item, "name", None) == name
    ]
    if not candidates:
        return None
    created = max(candidates, key=lambda item: int(item.id))
    return {
        "type": inverse_type,
        target_key: str(created.id),
        "reason": "revert previous assistant action",
    }


def _resolve_emoji(
    guild: typing.Any, raw: typing.Any
) -> Optional[Union[str, discord.Emoji, discord.PartialEmoji]]:
    """Turn model output into something message.add_reaction accepts.

    Accepts unicode ('😂'), :name: / name for server custom emoji, raw id,
    or full Discord markup <:name:id> / <a:name:id>.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _CUSTOM_EMOJI_RE.fullmatch(s)
    if m:
        return discord.PartialEmoji(
            name=m.group(1), id=int(m.group(2)), animated=s.startswith("<a:")
        )
    if s.isdigit() and guild is not None:
        e = guild.get_emoji(int(s))
        if e is not None:
            return e
    name = s.strip(":")
    if guild is not None and name:
        e = discord.utils.get(guild.emojis, name=name)
        if e is not None:
            return e
    return s


async def _react_message(
    a: dict[typing.Any, typing.Any],
    guild: typing.Any,
    channel: typing.Any,
    source_message: typing.Any,
    requester: typing.Any = None,
    bot_member: Optional[discord.Member] = None,
) -> Optional[str]:
    """Add one or more emoji reactions to a message (default: the trigger msg)."""
    raw_list: list[typing.Any] = []
    if a.get("emojis") is not None:
        if isinstance(a["emojis"], list):
            raw_list.extend(typing.cast(typing.Any, a["emojis"]))
        else:
            raw_list.append(a["emojis"])
    if a.get("emoji") is not None:
        if isinstance(a["emoji"], list):
            raw_list.extend(typing.cast(typing.Any, a["emoji"]))
        else:
            raw_list.append(a["emoji"])
    if a.get("reactions") is not None:
        if isinstance(a["reactions"], list):
            raw_list.extend(typing.cast(typing.Any, a["reactions"]))
        else:
            raw_list.append(a["reactions"])

    if not raw_list:
        return "react: no emoji given"

    target_msg = source_message
    mid = a.get("message_id") or a.get("target_message")
    if mid is not None:
        try:
            mid_i = int(str(mid).strip())
        except (TypeError, ValueError):
            return "react: bad message_id"
        ch = channel
        if a.get("channel") and guild is not None:
            ch = await _resolve_channel(guild, channel, a.get("channel"), fresh=True)
            if ch is None:
                return "react: target channel not found"
        if ch is None or not hasattr(ch, "fetch_message"):
            return "react: no channel to fetch message from"
        try:
            target_msg = await ch.fetch_message(mid_i)
        except discord.HTTPException:
            return f"react: message {mid_i} not found"
    elif target_msg is not None and channel is not None and hasattr(channel, "fetch_message"):
        source_id = getattr(target_msg, "id", None)
        if source_id is not None:
            try:
                target_msg = await channel.fetch_message(source_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return "react: source message is no longer available"

    if target_msg is None:
        return "react: no message to react to"

    g = guild or getattr(target_msg, "guild", None)
    me = bot_member or (_bot_member(g) if g is not None else None)
    react_ch = getattr(target_msg, "channel", channel)
    if g is not None and isinstance(requester, discord.Member):
        if getattr(requester.guild, "id", None) != getattr(g, "id", None):
            return "react failed: requester is not a member of that server"
        if react_ch is None or not hasattr(react_ch, "permissions_for"):
            return "react failed: target channel is unavailable"
        requester_perms = react_ch.permissions_for(requester)
        if not (
            requester_perms.administrator
            or (requester_perms.view_channel and requester_perms.read_message_history)
        ):
            return "react failed: you cannot view that message"
    if me is not None and react_ch is not None and hasattr(react_ch, "permissions_for"):
        bp = react_ch.permissions_for(me)
        if not (bp.add_reactions or bp.administrator):
            return "react failed: I need `add_reactions` here"
        if not (bp.read_message_history or bp.administrator):
            return "react failed: I need `read_message_history` here"

    added: list[typing.Any] = []
    failed: list[typing.Any] = []
    for raw in raw_list[:_MAX_REACTS]:
        emoji = _resolve_emoji(g, raw)
        if emoji is None:
            failed.append(str(raw))
            continue
        try:
            await target_msg.add_reaction(emoji)
            added.append(str(raw).strip())
        except discord.Forbidden:
            failed.append(f"{raw} (missing permission)")
        except discord.HTTPException:
            failed.append(f"{raw} (Discord rejected it)")
    if not added:
        return f"react failed: {'; '.join(failed) or 'unknown'}"
    if failed:
        return f"reacted {', '.join(added)}; failed {', '.join(failed)}"
    return None


async def execute_all(
    actions: typing.Any,
    requester: typing.Any,
    guild: typing.Any,
    client: typing.Any,
    channel: typing.Any = None,
    source_message: typing.Any = None,
    *,
    confirmed: bool = False,
    batch_actions: Optional[List[dict[typing.Any, typing.Any]]] = None,
) -> List[str]:
    """Run each action; return short human-readable result lines for the embed.

    `requester` is the member/user who triggered the exchange; `guild` is the
    server it happened in (None in a DM); `channel` is where the request was
    made (used as the default target for channel-scoped actions).
    `source_message` is the triggering Discord message (for react_message).
    Works from both the message path and the slash-command path.
    """
    rid = getattr(requester, "id", None) if requester is not None else None
    if rid is not None and config.is_blocked(rid):
        return []

    proposals: typing.Any = typing.cast(
        typing.Any,
        [
            a
            for a in typing.cast(typing.Iterable[typing.Any], (actions or []))
            if isinstance(a, dict)
        ],
    )
    if not proposals:
        return []
    if not confirmed:
        return [f"confirmation required — nothing executed: {preview_action(proposals[0])}"]
    if len(proposals) > _MAX_ACTIONS_PER_CONFIRMATION:
        return ["denied: a confirmation may execute exactly one action"]
    if guild is None or requester is None or getattr(requester, "id", None) is None:
        return ["denied: confirmed actions require a current guild member"]
    try:
        requester = await guild.fetch_member(requester.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return ["denied: the requesting member could not be revalidated"]

    bot_id = getattr(getattr(client, "user", None), "id", None)
    if bot_id is None:
        bot_id = getattr(_bot_member(guild), "id", None)
    bot_member = None
    if bot_id is not None:
        try:
            bot_member = await guild.fetch_member(bot_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return ["blocked: the bot's current guild permissions could not be revalidated"]

    out: list[typing.Any] = []
    for a in proposals:
        dependency = str(a.get("_assistant_depends_on_role_name") or "").strip()
        if dependency:
            created_role_id = None
            for earlier in batch_actions or []:
                if earlier is a:
                    break
                if action_type(earlier) == "create_role" and _role_name(
                    earlier.get("role") or earlier.get("name") or earlier.get("role_name")
                ) == _role_name(dependency):
                    created_role_id = _role_id(earlier.get("_assistant_created_role_id"))
            if created_role_id is None:
                out.append(
                    f"blocked: create the `{dependency}` role first, then confirm this assignment"
                )
                continue
            a: typing.Any = dict(a)
            a["role"] = str(created_role_id)
        try:
            line = await _one(
                a,
                requester,
                guild,
                client,
                channel,
                source_message,
                bot_member=bot_member,
            )
        except discord.Forbidden:
            act_name: typing.Any = a.get("type") or a.get("action") or "action"
            line = f"blocked: I lack permission or role position for `{act_name}`"
        except Exception:  # noqa: BLE001 - action boundary must fail closed
            act_name: typing.Any = a.get("type") or a.get("action") or "action"
            log.exception("confirmed action %s failed", act_name)
            line = f"failed `{act_name}`; check the bot logs with the request id"
        if line:
            out.append(line)
    return out


async def _one(
    a: dict[typing.Any, typing.Any],
    requester: typing.Any,
    guild: typing.Any,
    client: typing.Any,
    channel: typing.Any = None,
    source_message: typing.Any = None,
    *,
    bot_member: Optional[discord.Member] = None,
) -> Optional[str]:
    t = action_type(a)
    if t is None:
        return "denied: unknown action"

    if guild is None:
        return "actions only work in a server"

    if not isinstance(requester, discord.Member) and hasattr(requester, "id"):
        m = guild.get_member(requester.id)
        if not m:
            try:
                m = await guild.fetch_member(requester.id)
            except discord.HTTPException:
                m = None
        requester = m

    if requester is None or not isinstance(requester, discord.Member):
        return "actions only work in a server"

    if t == "react_message":
        requested_channel = a.get("channel")
        if requested_channel and _channel_id(requested_channel) is None:
            return "denied: `react_message` requires an exact channel id or mention"
        fresh_channel = await _resolve_channel(guild, channel, requested_channel, fresh=True)
        if fresh_channel is None:
            return "react: target channel not found"
        return await _react_message(
            a,
            guild,
            fresh_channel,
            source_message,
            requester=requester,
            bot_member=bot_member,
        )

    reason = (
        str(a.get("reason") or "").strip()[:_MAX_REASON_LENGTH]
        or f"requested via owaua by {requester} ({requester.id})"
    )
    raw_target = (
        a.get("target_user")
        or a.get("user")
        or a.get("target")
        or a.get("member")
        or a.get("user_id")
        or a.get("target_member")
    )
    if t in _EXACT_MEMBER_TARGET_ACTIONS and raw_target:
        self_target = t in {"set_nickname", "list_roles"} and str(raw_target).strip().lower() in {
            "me",
            "myself",
            "self",
        }
        if not self_target and _uid(raw_target) is None:
            return f"denied: `{t}` requires an exact user id or mention"
    target = (
        await _resolve_member(
            guild,
            raw_target,
            requester=requester,
            fresh=t in _EXACT_MEMBER_TARGET_ACTIONS,
        )
        if raw_target
        else None
    )
    me = bot_member or _bot_member(guild)

    _CHANNEL_SCOPED = {
        "purge_messages",
        "set_slowmode",
        "set_channel_topic",
        "deny_media_perms",
        "delete_channel",
    }
    scope_channel = channel
    explicit_channel = a.get("channel")
    if t == "delete_channel":
        explicit_channel = explicit_channel or a.get("name")
    if (
        t in _EXACT_CHANNEL_TARGET_ACTIONS
        and explicit_channel
        and _channel_id(explicit_channel) is None
    ):
        return f"denied: `{t}` requires an exact channel id or mention"
    if t in ("purge_messages", "set_slowmode", "set_channel_topic", "deny_media_perms"):
        scope_channel = await _resolve_channel(guild, channel, a.get("channel"), fresh=True)
    elif t == "delete_channel":
        scope_channel = await _resolve_channel(
            guild,
            None,
            a.get("channel") or a.get("name"),
            fresh=True,
        )

    perm_needed = _PERMS[t]
    if t == "list_roles" and target is not None and target.id != requester.id:
        perm_needed = "view_audit_log"
    if t == "set_status":
        if not getattr(config, "OWNER_ID", "") or str(requester.id) != str(config.OWNER_ID):
            return "denied: changing the bot's global status is owner-only"
    elif t == "set_nickname" and target and requester.id == target.id:
        if not (_has(requester, "manage_nicknames") or _has(requester, "change_nickname")):
            return "denied: you need `change_nickname` to set your own nickname"
    elif t == "set_slowmode" and _is_slowmode_channel(scope_channel):
        perm_needed = _slowmode_permission(scope_channel)
        if not _has(requester, perm_needed, channel=scope_channel):
            return f"denied: you need `{perm_needed}` to use `{t}`"
    else:
        check_ch = scope_channel if t in _CHANNEL_SCOPED else None
        if not _has(requester, perm_needed, channel=check_ch):
            return f"denied: you need `{perm_needed}` to use `{t}`"

    if t == "list_roles":
        if not target:
            return f"list_roles: target user '{raw_target or ''}' not found"
        names = [r.name[:100] for r in target.roles if r.name != "@everyone"][:50]
        rendered = f"{target.display_name} roles: {', '.join(names) or 'none'}"
        return discord.utils.escape_mentions(rendered[:1500])

    if t == "kick_user":
        if not target:
            return f"kick: target user '{raw_target or ''}' not found"
        if target.id == requester.id:
            return "kick: won't kick yourself"
        if me is None or not (
            me.guild_permissions.kick_members or me.guild_permissions.administrator
        ):
            return "blocked: I need `kick_members`"
        err = _bot_can_act_on(guild, target, me) or _requester_can_act_on(requester, target)
        if err:
            return err
        await target.kick(reason=reason)
        return f"kicked {target.display_name}"

    if t == "ban_user":
        if not target:
            return f"ban: target user '{raw_target or ''}' not found"
        if target.id == requester.id:
            return "ban: won't ban yourself"
        if me is None or not (
            me.guild_permissions.ban_members or me.guild_permissions.administrator
        ):
            return "blocked: I need `ban_members`"
        err = _bot_can_act_on(guild, target, me) or _requester_can_act_on(requester, target)
        if err:
            return err
        await target.ban(reason=reason, delete_message_seconds=0)
        return f"banned {target.display_name}"

    if t in ("assign_role", "remove_role"):
        if not target:
            return f"{t}: target user '{raw_target or ''}' not found"
        role_target = str(a.get("role") or a.get("role_name") or a.get("name") or "").strip()
        if _role_id(role_target) is None:
            return f"denied: `{t}` requires an exact role id or mention"
        role = await _resolve_role(guild, role_target, fresh=True)
        if not role:
            return f"{t}: role '{role_target}' not found"
        err = _bot_can_manage_role(guild, role, me)
        if err:
            return err
        if requester.guild.owner_id != requester.id and role >= requester.top_role:
            return f"denied: role `{role.name}` is at or above your top role"
        target_err = _requester_can_act_on(requester, target)
        if target_err:
            return target_err
        bot_target_err = _bot_can_act_on(guild, target, me)
        if bot_target_err:
            return bot_target_err
        if t == "assign_role":
            await target.add_roles(role, reason=reason)
            return f"gave {target.display_name} the {role.name} role"
        await target.remove_roles(role, reason=reason)
        return f"removed {role.name} from {target.display_name}"

    if t == "create_role":
        name = str(a.get("role") or a.get("name") or a.get("role_name") or "").strip()
        if not name:
            return "create_role: no name given"
        if me is None or not (
            me.guild_permissions.manage_roles or me.guild_permissions.administrator
        ):
            return "blocked: I need `manage_roles`"
        colour = discord.Colour.default()
        hex_colour = str(a.get("color") or "").strip().lstrip("#")
        if hex_colour:
            try:
                colour = discord.Colour(int(hex_colour, 16))
            except ValueError:
                pass
        if len(name) > 100:
            return "create_role: role name is too long"
        role = await guild.create_role(
            name=name,
            colour=colour,
            hoist=a.get("hoist") is True,
            mentionable=a.get("mentionable") is True,
            reason=reason,
        )
        # This is runtime-only metadata for a later, separately confirmed
        # assignment from the same ordered assistant batch.  It never comes
        # from model output and cannot select a pre-existing role by name.
        a["_assistant_created_role_id"] = str(role.id)
        return f"created role {role.name}"

    if t == "delete_role":
        name = str(a.get("role") or a.get("name") or a.get("role_name") or "").strip()
        if _role_id(name) is None:
            return "denied: `delete_role` requires an exact role id or mention"
        role = await _resolve_role(guild, name, fresh=True)
        if not role:
            return f"delete_role: role '{name}' not found"
        if role.is_default():
            return "delete_role: can't delete @everyone"
        err = _bot_can_manage_role(guild, role, me)
        if err:
            return err
        if requester.guild.owner_id != requester.id and role >= requester.top_role:
            return f"denied: role `{role.name}` is at or above your top role"
        await role.delete(reason=reason)
        return f"deleted role {role.name}"

    if t == "dm_user":
        if not target:
            return f"dm: target user '{raw_target or ''}' not found"
        from owaua import db as _db

        if _db.user_flag_get(str(target.id), "dm_block") == "1":
            return (
                f"dm blocked: {target.display_name} opted out of bot DMs "
                f"(`!dmunblock` to re-enable)"
            )
        raw = str(
            a.get("dm_content")
            or a.get("message")
            or a.get("content")
            or a.get("text")
            or "(no content)"
        ).strip()[:_MAX_DM_LENGTH]
        header = (
            f"Message from **{requester.display_name}** "
            f"(@{requester.name}, id `{requester.id}`) via owaua\n"
            f"_Reply in the server, not here. Opt out: `!dmblock` · status: `!mydm`_\n\n"
        )
        body = header + raw
        if len(body) > 1900:
            body = body[:1900] + "…"
        try:
            await target.send(body)
        except discord.Forbidden:
            return f"dm failed: {target.display_name} has DMs closed or blocked the bot"
        return f"dm'd {target.display_name} (attributed to {requester.display_name})"

    if t == "timeout_user":
        if not target:
            return f"timeout: target user '{raw_target or ''}' not found"
        if target.id == requester.id:
            return "timeout: won't timeout yourself"
        if me is None or not (
            me.guild_permissions.moderate_members or me.guild_permissions.administrator
        ):
            return "blocked: I need `moderate_members`"
        err = _bot_can_act_on(guild, target, me) or _requester_can_act_on(requester, target)
        if err:
            return err
        now_utc = discord.utils.utcnow()
        raw_until = str(a.get("until") or "").strip()
        until = None
        if raw_until:
            try:
                until = datetime.datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=datetime.timezone.utc)
                until = min(
                    until,
                    now_utc + datetime.timedelta(minutes=_MAX_TIMEOUT_MINUTES),
                )
                if until <= now_utc:
                    return "timeout: stored restore time has already passed"
            except ValueError:
                return "timeout: invalid restore time"
        if until is None:
            try:
                minutes = max(
                    1,
                    min(
                        _MAX_TIMEOUT_MINUTES,
                        int(a.get("minutes") or a.get("duration") or a.get("time") or 10),
                    ),
                )
            except (TypeError, ValueError):
                minutes = 10
            until = now_utc + datetime.timedelta(minutes=minutes)
        else:
            minutes = max(1, math.ceil((until - now_utc).total_seconds() / 60))
        await target.timeout(until, reason=reason)
        return f"timed out {target.display_name} for {minutes}m"

    if t == "remove_timeout":
        if not target:
            return f"remove_timeout: target user '{raw_target or ''}' not found"
        if me is None or not (
            me.guild_permissions.moderate_members or me.guild_permissions.administrator
        ):
            return "blocked: I need `moderate_members`"
        err = _bot_can_act_on(guild, target, me) or _requester_can_act_on(requester, target)
        if err:
            return err
        await target.timeout(None, reason=reason)
        return f"cleared timeout for {target.display_name}"

    if t == "set_nickname":
        if not target:
            return f"set_nickname: target user '{raw_target or ''}' not found"
        self_rename = requester.id == target.id
        if me is None:
            return "blocked: I am not a member of this server"
        if not (me.guild_permissions.manage_nicknames or me.guild_permissions.administrator):
            return "blocked: I need `manage_nicknames`"
        if not self_rename:
            err = _bot_can_act_on(guild, target, me) or _requester_can_act_on(requester, target)
            if err:
                return err
        else:
            err = _bot_can_act_on(guild, target, me)
            if err:
                return err
        nick = (
            str(
                a.get("nickname")
                or a.get("nick")
                or a.get("name")
                or a.get("new_nickname")
                or a.get("new_nick")
                or ""
            ).strip()
            or None
        )
        if nick and len(nick) > 32:
            nick = nick[:32]
        await target.edit(nick=nick, reason=reason)
        return f"set {target.display_name}'s nickname to {nick or '(reset)'}"

    if t == "purge_messages":
        ch = scope_channel
        if ch is None or not hasattr(ch, "purge"):
            return "purge_messages: no channel to purge"
        if me is not None and hasattr(ch, "permissions_for"):
            bp = ch.permissions_for(me)
            if not (bp.manage_messages or bp.administrator):
                return f"blocked: I need `manage_messages` in #{getattr(ch, 'name', ch.id)}"
        try:
            count = max(
                1,
                min(
                    _MAX_PURGE,
                    int(
                        a.get("count") or a.get("amount") or a.get("limit") or a.get("number") or 10
                    ),
                ),
            )
        except (TypeError, ValueError):
            count = 10
        purge_target_raw = a.get("target_user") or a.get("user")
        if purge_target_raw and _uid(purge_target_raw) is None:
            return "denied: a purge target requires an exact user id or mention"
        purge_target = (
            await _resolve_member(guild, purge_target_raw, fresh=True) if purge_target_raw else None
        )
        if purge_target_raw and purge_target is None:
            return "purge_messages: target user not found; nothing was deleted"

        def _matches_target(candidate: discord.Message) -> bool:
            return purge_target is not None and candidate.author.id == purge_target.id

        check = _matches_target if purge_target else None
        deleted = await ch.purge(limit=count, check=check, reason=reason)
        return f"purged {len(deleted)} message(s) in #{getattr(ch, 'name', ch.id)}"

    if t == "create_channel":
        name = str(a.get("name") or "").strip()
        if not name:
            return "create_channel: no name given"
        if len(name) > 100:
            return "create_channel: channel name is too long"
        if me is None or not (
            me.guild_permissions.manage_channels or me.guild_permissions.administrator
        ):
            return "blocked: I need `manage_channels`"
        kind = str(a.get("channel_type") or "text").lower()
        topic = str(a.get("topic") or "")[:1024] or None
        if kind == "voice":
            ch = await guild.create_voice_channel(name, reason=reason)
        else:
            ch = await guild.create_text_channel(name, topic=topic, reason=reason)
        return f"created #{ch.name}"

    if t == "delete_channel":
        name = str(a.get("channel") or a.get("name") or "").strip()
        ch = scope_channel
        if not ch:
            return f"delete_channel: '{name}' not found"
        if me is None or not (
            me.guild_permissions.manage_channels or me.guild_permissions.administrator
        ):
            return "blocked: I need `manage_channels`"
        ch_name = getattr(ch, "name", str(ch.id))
        await ch.delete(reason=reason)
        return f"deleted #{ch_name}"

    if t == "set_slowmode":
        ch = scope_channel
        if not _is_slowmode_channel(ch):
            return "set_slowmode: no slowmode-capable channel to set"
        if me is not None and hasattr(ch, "permissions_for"):
            bp = typing.cast(typing.Any, ch).permissions_for(me)
            required = _slowmode_permission(ch)
            if not (getattr(bp, required, False) or bp.administrator):
                return f"blocked: I need `{required}` in #{typing.cast(typing.Any, ch).name}"
        try:
            seconds = max(0, min(_MAX_SLOWMODE, int(a.get("seconds") or 0)))
        except (TypeError, ValueError):
            seconds = 0
        await typing.cast(typing.Any, ch).edit(slowmode_delay=seconds, reason=reason)
        return f"slowmode in #{typing.cast(typing.Any, ch).name} set to {seconds}s"

    if t == "set_channel_topic":
        ch = scope_channel
        if ch is None or not isinstance(ch, discord.TextChannel):
            return "set_channel_topic: no text channel to set"
        if me is not None and hasattr(ch, "permissions_for"):
            bp = ch.permissions_for(me)
            if not (bp.manage_channels or bp.administrator):
                return f"blocked: I need `manage_channels` in #{ch.name}"
        topic = str(a.get("topic") or "")[:1024]
        await ch.edit(topic=topic, reason=reason)
        return f"updated #{ch.name}'s topic"

    if t == "set_server_name":
        name = str(a.get("name") or "").strip()
        if not name:
            return "set_server_name: no name given"
        if len(name) > 100:
            return "set_server_name: server name is too long"
        if me is None or not (
            me.guild_permissions.manage_guild or me.guild_permissions.administrator
        ):
            return "blocked: I need `manage_guild`"
        await guild.edit(name=name, reason=reason)
        return f"renamed server to {name}"

    if t == "set_status":
        kind = _STATUS_KINDS.get(
            str(a.get("status_kind", "playing")).lower(), discord.ActivityType.playing
        )
        text = str(a.get("status_text", "") or "around")[:128]
        await client.change_presence(activity=discord.Activity(type=kind, name=text))
        return f"status set to {a.get('status_kind', 'playing')} {text}"

    if t == "deny_media_perms":
        if not target:
            return "deny_media_perms: target user not found"
        ch = scope_channel
        if ch is None or not isinstance(
            ch,
            (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.ForumChannel),
        ):
            return "deny_media_perms: no valid channel to modify permissions"
        if me is not None and hasattr(ch, "permissions_for"):
            bp = ch.permissions_for(me)
            if not (bp.manage_roles or bp.administrator):
                return f"blocked: I need `manage_roles` in #{ch.name}"
        target_err = _requester_can_act_on(requester, target)
        if target_err:
            return target_err
        bot_target_err = _bot_can_act_on(guild, target, me)
        if bot_target_err:
            return bot_target_err
        overwrite = ch.overwrites_for(target)
        overwrite.update(attach_files=False, embed_links=False)
        try:
            await ch.set_permissions(target, overwrite=overwrite, reason=reason)
            return f"denied attach files and embed links for {target.display_name} in #{ch.name}"
        except discord.Forbidden:
            return f"denied: I lack permission to modify channel overrides for #{ch.name}"
        except discord.HTTPException:
            return "failed to set permissions; Discord rejected the request"

    return None
