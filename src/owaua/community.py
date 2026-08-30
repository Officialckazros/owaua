"""Dashboard-driven community-management runtime.

This module intentionally keeps enforcement deterministic.  Dashboard rules
are data, never Python or template code, and every destructive operation still
depends on Discord's native permissions and hierarchy.
"""

from __future__ import annotations

import asyncio
import fnmatch
import html
import io
import json
import logging
import math
import re
import secrets
import time
import typing
from collections import defaultdict, deque
from datetime import timedelta
from typing import Final
from urllib.parse import quote, urlsplit

import aiohttp
import discord

from owaua import config as bot_config
from owaua import db, embeds, staffops
from owaua.scope import Scope

log = logging.getLogger("owaua.community")

_URL_RE: Final = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_INVITE_RE: Final = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg)/[\w-]+",
    re.IGNORECASE,
)
_EMOJI_RE: Final = re.compile(r"<a?:\w{2,32}:\d+>|[\U0001F300-\U0001FAFF]")
_DURATION_RE: Final = re.compile(r"^(\d+)(s|m|h|d|w)$", re.IGNORECASE)
_rapid: dict[tuple[int, int], deque[float]] = defaultdict(deque)
_duplicates: dict[tuple[int, int], tuple[str, float]] = {}
_slow: dict[tuple[int, int], float] = {}
_HTTP_HOSTS: Final = frozenset(
    {
        "dog.ceo",
        "icanhazdadjoke.com",
        "pokeapi.co",
        "itunes.apple.com",
        "api.github.com",
        "api.wheretheiss.at",
        "www.reddit.com",
        "www.youtube.com",
        "id.twitch.tv",
        "api.twitch.tv",
        "id.kick.com",
        "api.kick.com",
        "open.tiktokapis.com",
    }
)
_token_cache: dict[str, tuple[str, float]] = {}
_persistent_views_registered = False
_NATIVE_AUTOMOD_RULE_NAME: Final = "owaua: configured blocked phrases"


def _component_slug(value: object, fallback: str) -> str:
    clean = re.sub(r"[^a-z0-9_-]", "", str(value or "").lower())[:30]
    return clean or fallback


def _bot_role_allows(guild: discord.Guild, role: discord.Role) -> bool:
    me = guild.me
    return bool(me and role < me.top_role and role.id != guild.default_role.id)


async def _create_ticket_from_interaction(
    interaction: discord.Interaction,
    panel: dict[typing.Any, typing.Any],
    answers: list[dict[typing.Any, typing.Any]],
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.followup.send(
            "Tickets can only be opened inside the server.", ephemeral=True
        )
        return
    config = _cfg(guild, "tickets")
    if not config["enabled"]:
        await interaction.followup.send("Tickets are currently disabled.", ephemeral=True)
        return
    settings = config["settings"]
    scope = _scope(guild)
    existing = [
        item
        for item in db.community_records(
            "ticket", scope, user_id=str(member.id), status=None, limit=5_000
        )
        if item["status"] in {"active", "open", "waiting"}
    ]
    maximum = max(1, min(100, int(settings.get("max_open_per_member") or 5)))
    if len(existing) >= maximum:
        await interaction.followup.send(
            f"You already have {len(existing)} open ticket(s).", ephemeral=True
        )
        return
    answer_map = {str(item.get("id")): str(item.get("value") or "") for item in answers}
    route: dict[typing.Any, typing.Any] = {}
    routing_rules = (
        panel.get("routing_rules")
        if isinstance(panel.get("routing_rules"), list)
        else settings.get("routing_rules", [])
    )
    for candidate in typing.cast(
        typing.Iterable[typing.Any], typing.cast(typing.Any, routing_rules)[:100]
    ):
        if not isinstance(candidate, dict):
            continue
        field_value = answer_map.get(
            str(typing.cast(typing.Any, candidate).get("field_id") or ""), ""
        ).casefold()
        expected = str(typing.cast(typing.Any, candidate).get("value") or "").casefold()
        operator = str(typing.cast(typing.Any, candidate).get("operator") or "contains")
        matched = bool(expected) and (
            (operator == "equals" and field_value == expected)
            or (operator == "starts_with" and field_value.startswith(expected))
            or (operator == "contains" and expected in field_value)
        )
        if matched:
            route: typing.Any = typing.cast(typing.Any, candidate)
            break
    category_id = str(
        route.get("category_id") or panel.get("category_id") or settings.get("category_id") or ""
    )
    category = guild.get_channel(int(category_id)) if category_id.isdigit() else None
    staff_ids = (
        route.get("staff_role_ids")
        if isinstance(route.get("staff_role_ids"), list)
        else (
            panel.get("staff_role_ids")
            if isinstance(panel.get("staff_role_ids"), list)
            else settings.get("staff_role_ids", [])
        )
    )
    assigned_to = str(route.get("assigned_to") or panel.get("assigned_to") or "")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    for role_id in typing.cast(
        typing.Iterable[typing.Any], typing.cast(typing.Any, staff_ids)[:20]
    ):
        role = guild.get_role(int(role_id)) if str(role_id).isdigit() else None
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
    assigned_member = guild.get_member(int(assigned_to)) if assigned_to.isdigit() else None
    if assigned_member is not None:
        overwrites[assigned_member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
    safe_name = re.sub(r"[^a-z0-9-]", "-", member.name.lower())[:40]
    try:
        channel = await guild.create_text_channel(
            f"ticket-{safe_name}-{secrets.randbelow(10000):04d}",
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=typing.cast(typing.Any, overwrites),
            reason="Persistent ticket panel opened",
        )
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            "I could not create the private ticket channel.", ephemeral=True
        )
        return
    subject = next(
        (str(item.get("value") or "") for item in answers if item.get("value")),
        str(panel.get("title") or "Support request"),
    )[:500]
    sla_hours = max(1, min(720, int(panel.get("sla_hours") or settings.get("sla_hours") or 24)))
    panel_id = _component_slug(panel.get("id"), "default")
    record_id = db.community_record_create(
        "ticket",
        scope,
        {
            "channel_id": str(channel.id),
            "subject": subject,
            "panel_id": panel_id,
            "answers": answers[:5],
            "assigned_to": assigned_to,
            "sla_due": time.time() + sla_hours * 3600,
            "staff_role_ids": [
                str(value)
                for value in typing.cast(
                    typing.Iterable[typing.Any], typing.cast(typing.Any, staff_ids)[:20]
                )
            ],
            "route_id": str(route.get("id") or "")[:40],
            "sla_alerted": False,
            "last_member_activity": time.time(),
        },
        user_id=str(member.id),
        record_key=str(channel.id),
        due=time.time() + sla_hours * 3600,
    )
    staff_mentions = " ".join(
        f"<@&{role_id}>"
        for role_id in typing.cast(
            typing.Iterable[typing.Any], typing.cast(typing.Any, staff_ids)[:20]
        )
        if str(role_id).isdigit()
    )
    if assigned_to.isdigit():
        staff_mentions = (staff_mentions + f" <@{assigned_to}>").strip()
    answer_text = "\n".join(
        f"**{item['label']}:** {item['value']}" for item in answers if item.get("value")
    )
    await _safe_send(
        channel,
        f"Ticket #{record_id} opened by {member.mention}.\n{answer_text or '**Subject:** ' + subject}\n{staff_mentions}",
    )
    await interaction.followup.send(
        f"Your private ticket is ready: {channel.mention}", ephemeral=True
    )


class TicketIntakeModal(discord.ui.Modal):
    def __init__(self, panel: dict[typing.Any, typing.Any]) -> None:
        super().__init__(title=str(panel.get("title") or "Open support ticket")[:45], timeout=300)
        self.panel = panel
        fields: typing.Any = (
            panel.get("intake_fields") if isinstance(panel.get("intake_fields"), list) else []
        )
        if not fields:
            fields = [
                {
                    "id": "subject",
                    "label": "How can staff help?",
                    "required": True,
                    "style": "paragraph",
                }
            ]
        self.fields: list[
            tuple[dict[typing.Any, typing.Any], discord.ui.TextInput[typing.Any]]
        ] = []
        for index, field in enumerate(fields[:5]):
            if not isinstance(field, dict):
                continue
            label = str(typing.cast(typing.Any, field).get("label") or f"Question {index + 1}")[:45]
            text_input: typing.Any = typing.cast(
                typing.Any,
                discord.ui.TextInput(
                    label=label,
                    custom_id=_component_slug(
                        typing.cast(typing.Any, field).get("id"), f"q{index}"
                    ),
                    required=typing.cast(typing.Any, field).get("required", True) is not False,
                    max_length=max(
                        1, min(1000, int(typing.cast(typing.Any, field).get("max_length") or 500))
                    ),
                    style=discord.TextStyle.paragraph
                    if str(typing.cast(typing.Any, field).get("style")) == "paragraph"
                    else discord.TextStyle.short,
                    placeholder=str(typing.cast(typing.Any, field).get("placeholder") or "")[:100]
                    or None,
                ),
            )
            self.add_item(text_input)
            self.fields.append((typing.cast(dict[typing.Any, typing.Any], field), text_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = [
            {
                "id": _component_slug(field.get("id"), "question"),
                "label": str(field.get("label") or item.label)[:100],
                "value": str(item.value)[:1000],
            }
            for field, item in self.fields
        ]
        await _create_ticket_from_interaction(interaction, self.panel, answers)


class PersistentTicketPanel(discord.ui.View):
    def __init__(self, guild_id: int, panel: dict[typing.Any, typing.Any]) -> None:
        super().__init__(timeout=None)
        self.guild_id = int(guild_id)
        self.panel = panel
        slug = _component_slug(panel.get("id"), "default")
        button: typing.Any = typing.cast(
            typing.Any,
            discord.ui.Button(
                label=str(panel.get("button_label") or panel.get("title") or "Open ticket")[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"owaua:ticket:{self.guild_id}:{slug}",
            ),
        )
        button.callback = self.open_ticket
        self.add_item(button)

    async def open_ticket(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This ticket panel belongs to another server.", ephemeral=True
            )
            return
        await interaction.response.send_modal(TicketIntakeModal(self.panel))


class PersistentRoleMenu(discord.ui.View):
    def __init__(self, guild_id: int, menu: dict[typing.Any, typing.Any]) -> None:
        super().__init__(timeout=None)
        self.guild_id = int(guild_id)
        self.menu = menu
        options: list[typing.Any] = []
        for item in menu.get("items", [])[:25]:
            if (
                not isinstance(item, dict)
                or not str(typing.cast(typing.Any, item).get("role_id") or "").isdigit()
            ):
                continue
            options.append(
                discord.SelectOption(
                    label=str(
                        typing.cast(
                            typing.Any,
                            typing.cast(typing.Any, item).get("label")
                            or typing.cast(typing.Any, item).get("name")
                            or item["role_id"],
                        )
                    )[:100],
                    value=str(typing.cast(typing.Any, item["role_id"])),
                    description=str(typing.cast(typing.Any, item).get("description") or "")[:100]
                    or None,
                    emoji=str(typing.cast(typing.Any, item).get("emoji"))
                    if typing.cast(typing.Any, item).get("emoji")
                    else None,
                )
            )
        if options:
            select: typing.Any = typing.cast(
                typing.Any,
                discord.ui.Select(
                    placeholder=str(menu.get("placeholder") or "Choose your roles")[:150],
                    custom_id=f"owaua:roles:{self.guild_id}:{_component_slug(menu.get('id'), 'default')}",
                    min_values=0,
                    max_values=max(
                        1, min(len(options), int(menu.get("max_values") or len(options)))
                    ),
                    options=options,
                ),
            )
            select.callback = self.choose_roles
            self.add_item(select)

    async def choose_roles(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "This role menu is not available here.", ephemeral=True
            )
            return
        config = _cfg(typing.cast(typing.Any, interaction.guild), "reaction_roles")
        welcome = _cfg(typing.cast(typing.Any, interaction.guild), "welcome")
        if config["settings"].get("require_rules_ack"):
            ack_id = str(welcome["settings"].get("rules_ack_role_id") or "")
            if ack_id.isdigit() and all(role.id != int(ack_id) for role in interaction.user.roles):
                await interaction.response.send_message(
                    "Acknowledge the server rules before choosing roles.", ephemeral=True
                )
                return
        selected = {str(value) for value in getattr(self.children[0], "values", [])}
        allowed = {
            str(typing.cast(typing.Any, item).get("role_id"))
            for item in self.menu.get("items", [])
            if isinstance(item, dict)
        }
        changed = 0
        for role_id in allowed:
            role = (
                typing.cast(typing.Any, interaction.guild).get_role(int(role_id))
                if role_id.isdigit()
                else None
            )
            if role is None or not _bot_role_allows(
                typing.cast(typing.Any, interaction.guild), role
            ):
                continue
            has_role = role in interaction.user.roles
            try:
                if role_id in selected and not has_role:
                    await interaction.user.add_roles(role, reason="Persistent self-role menu")
                    changed += 1
                elif (
                    role_id not in selected
                    and has_role
                    and config["settings"].get("remove_on_unselect", True)
                ):
                    await interaction.user.remove_roles(role, reason="Persistent self-role menu")
                    changed += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        await interaction.response.send_message(
            f"Updated {changed} role selection(s).", ephemeral=True
        )


class OnboardingIntroModal(discord.ui.Modal):
    def __init__(self, guild_id: int, settings: dict[typing.Any, typing.Any]) -> None:
        super().__init__(title="Introduce yourself", timeout=300)
        self.guild_id = int(guild_id)
        self.inputs: list[
            tuple[dict[typing.Any, typing.Any], discord.ui.TextInput[typing.Any]]
        ] = []
        questions: typing.Any = (
            settings.get("intro_questions")
            if isinstance(settings.get("intro_questions"), list)
            else []
        )
        for index, question in enumerate(questions[:5]):
            if not isinstance(question, dict):
                continue
            item: typing.Any = typing.cast(
                typing.Any,
                discord.ui.TextInput(
                    label=str(
                        typing.cast(typing.Any, question).get("label") or f"Question {index + 1}"
                    )[:45],
                    custom_id=_component_slug(
                        typing.cast(typing.Any, question).get("id"), f"intro{index}"
                    ),
                    required=typing.cast(typing.Any, question).get("required", False) is True,
                    max_length=max(
                        1, min(500, int(typing.cast(typing.Any, question).get("max_length") or 200))
                    ),
                    style=discord.TextStyle.paragraph
                    if typing.cast(typing.Any, question).get("paragraph")
                    else discord.TextStyle.short,
                ),
            )
            self.add_item(item)
            self.inputs.append((typing.cast(dict[typing.Any, typing.Any], question), item))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "This intro form belongs to another server.", ephemeral=True
            )
            return
        answers = [
            {
                "id": _component_slug(question.get("id"), "intro"),
                "label": str(question.get("label") or item.label)[:100],
                "value": str(item.value)[:500],
            }
            for question, item in self.inputs
        ]
        db.community_record_create(
            "onboarding_intro",
            f"guild:{self.guild_id}",
            {"answers": answers},
            user_id=str(interaction.user.id),
            record_key=str(interaction.user.id),
        )
        await interaction.response.send_message(
            "Intro saved privately for the onboarding workflow.", ephemeral=True
        )


class PersistentOnboardingView(discord.ui.View):
    def __init__(self, guild_id: int, settings: dict[typing.Any, typing.Any]) -> None:
        super().__init__(timeout=None)
        self.guild_id = int(guild_id)
        self.settings = settings
        ack: typing.Any = typing.cast(
            typing.Any,
            discord.ui.Button(
                label="Acknowledge rules",
                style=discord.ButtonStyle.success,
                custom_id=f"owaua:onboard:{guild_id}:ack",
            ),
        )
        ack.callback = self.acknowledge
        self.add_item(ack)
        choices: typing.Any = (
            settings.get("role_choices") if isinstance(settings.get("role_choices"), list) else []
        )
        options = [
            discord.SelectOption(
                label=str(
                    typing.cast(typing.Any, item).get("label")
                    or typing.cast(typing.Any, item).get("role_id")
                )[:100],
                value=str(typing.cast(typing.Any, item).get("role_id")),
                description=str(typing.cast(typing.Any, item).get("description") or "")[:100]
                or None,
            )
            for item in choices[:25]
            if isinstance(item, dict)
            and str(typing.cast(typing.Any, item).get("role_id") or "").isdigit()
        ]
        if options:
            select: typing.Any = typing.cast(
                typing.Any,
                discord.ui.Select(
                    placeholder="Choose optional starter roles",
                    custom_id=f"owaua:onboard:{guild_id}:roles",
                    min_values=0,
                    max_values=len(options),
                    options=options,
                ),
            )
            select.callback = self.choose_roles
            self.add_item(select)
        if isinstance(settings.get("intro_questions"), list) and settings.get("intro_questions"):
            intro: typing.Any = typing.cast(
                typing.Any,
                discord.ui.Button(
                    label="Answer intro questions",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"owaua:onboard:{guild_id}:intro",
                ),
            )
            intro.callback = self.intro
            self.add_item(intro)

    async def acknowledge(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "Open this onboarding step inside the server.", ephemeral=True
            )
            return
        role_id = str(self.settings.get("rules_ack_role_id") or "")
        role = (
            typing.cast(typing.Any, interaction.guild).get_role(int(role_id))
            if role_id.isdigit()
            else None
        )
        if role is None or not _bot_role_allows(typing.cast(typing.Any, interaction.guild), role):
            await interaction.response.send_message(
                "The acknowledgement role is not configured safely.", ephemeral=True
            )
            return
        try:
            await interaction.user.add_roles(role, reason="Rules acknowledged through onboarding")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "I could not assign the acknowledgement role.", ephemeral=True
            )
            return
        db.community_record_create(
            "onboarding_ack",
            _scope(typing.cast(typing.Any, interaction.guild)),
            {"role_id": role_id},
            user_id=str(interaction.user.id),
            record_key=str(interaction.user.id),
        )
        starters = [
            f"<#{value}>"
            for value in self.settings.get("starter_channel_ids", [])[:10]
            if str(value).isdigit()
        ]
        await interaction.response.send_message(
            "Rules acknowledged." + (" Start here: " + " · ".join(starters) if starters else ""),
            ephemeral=True,
        )

    async def choose_roles(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id or not isinstance(
            interaction.user, discord.Member
        ):
            await interaction.response.send_message(
                "Open this onboarding step inside the server.", ephemeral=True
            )
            return
        ack_id = str(self.settings.get("rules_ack_role_id") or "")
        if ack_id.isdigit() and all(role.id != int(ack_id) for role in interaction.user.roles):
            await interaction.response.send_message(
                "Acknowledge the rules before choosing starter roles.", ephemeral=True
            )
            return
        selected_control = next(
            (item for item in self.children if isinstance(item, discord.ui.Select)), None
        )
        selected = {str(value) for value in getattr(selected_control, "values", [])}
        allowed = {
            str(typing.cast(typing.Any, item).get("role_id"))
            for item in self.settings.get("role_choices", [])
            if isinstance(item, dict)
        }
        changed = 0
        for role_id in allowed:
            role = (
                typing.cast(typing.Any, interaction.guild).get_role(int(role_id))
                if role_id.isdigit()
                else None
            )
            if role is None or not _bot_role_allows(
                typing.cast(typing.Any, interaction.guild), role
            ):
                continue
            try:
                if role_id in selected and role not in interaction.user.roles:
                    await interaction.user.add_roles(role, reason="Onboarding role choice")
                    changed += 1
                elif role_id not in selected and role in interaction.user.roles:
                    await interaction.user.remove_roles(
                        role, reason="Onboarding role choice removed"
                    )
                    changed += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        await interaction.response.send_message(
            f"Updated {changed} starter role(s).", ephemeral=True
        )

    async def intro(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "Open this onboarding step inside the server.", ephemeral=True
            )
            return
        await interaction.response.send_modal(OnboardingIntroModal(self.guild_id, self.settings))


def register_persistent_views(client: discord.Client) -> int:
    """Register configured component custom IDs once per process restart."""
    global _persistent_views_registered
    if _persistent_views_registered:
        return 0
    count = 0
    for guild in list(client.guilds):
        tickets = _cfg(guild, "tickets")
        if tickets["enabled"]:
            for panel in tickets["settings"].get("panels", [])[:100]:
                if not isinstance(panel, dict):
                    continue
                view = PersistentTicketPanel(guild.id, typing.cast(typing.Any, panel))
                if view.children:
                    message_id = str(typing.cast(typing.Any, panel).get("message_id") or "")
                    client.add_view(
                        view, message_id=int(message_id) if message_id.isdigit() else None
                    )
                    count += 1
        reaction = _cfg(guild, "reaction_roles")
        if reaction["enabled"]:
            for menu in reaction["settings"].get("menus", [])[:100]:
                if not isinstance(menu, dict):
                    continue
                view = PersistentRoleMenu(guild.id, typing.cast(typing.Any, menu))
                if view.children:
                    message_id = str(typing.cast(typing.Any, menu).get("message_id") or "")
                    client.add_view(
                        view, message_id=int(message_id) if message_id.isdigit() else None
                    )
                    count += 1
        welcome = _cfg(guild, "welcome")
        if welcome["enabled"] and welcome["settings"].get("journey_enabled"):
            client.add_view(PersistentOnboardingView(guild.id, welcome["settings"]))
            count += 1
    _persistent_views_registered = True
    return count


_CARDS: Final = (
    {
        "name": "Cipher Fox",
        "atk": 18,
        "def": 12,
        "skill": "Packet Feint",
        "faction": "Neon",
        "lore": "A trickster born between two encrypted frames.",
    },
    {
        "name": "Iron Warden",
        "atk": 11,
        "def": 21,
        "skill": "Hard Lock",
        "faction": "Bastion",
        "lore": "It never opens the same port twice.",
    },
    {
        "name": "Null Siren",
        "atk": 22,
        "def": 8,
        "skill": "Silent Crash",
        "faction": "Void",
        "lore": "The last sound a dead process remembers.",
    },
    {
        "name": "Patch Witch",
        "atk": 15,
        "def": 16,
        "skill": "Hotfix",
        "faction": "Neon",
        "lore": "She repairs allies while production is still burning.",
    },
    {
        "name": "Root Golem",
        "atk": 17,
        "def": 19,
        "skill": "Privilege Rise",
        "faction": "Bastion",
        "lore": "Built from the permissions nobody meant to grant.",
    },
    {
        "name": "Cache Drake",
        "atk": 20,
        "def": 13,
        "skill": "Warm Start",
        "faction": "Ember",
        "lore": "It sleeps on the fastest path through memory.",
    },
    {
        "name": "Phantom Thread",
        "atk": 19,
        "def": 14,
        "skill": "Race Condition",
        "faction": "Void",
        "lore": "Seen only when the debugger looks away.",
    },
    {
        "name": "Solar Kernel",
        "atk": 16,
        "def": 20,
        "skill": "Core Flare",
        "faction": "Ember",
        "lore": "A tiny sun with an uptime obsession.",
    },
)


def _scope(guild: discord.Guild | int) -> str:
    return Scope.guild(guild if isinstance(guild, int) else guild.id).key


def _cfg(guild: discord.Guild | int, module: str) -> dict[typing.Any, typing.Any]:
    return db.module_config(_scope(guild), module)


def _channel(guild: discord.Guild, raw: object):
    value = str(raw or "")
    return guild.get_channel(int(value)) if value.isdigit() else None


def _ids(values: object) -> set[str]:
    return typing.cast(
        typing.Any,
        {str(value) for value in typing.cast(typing.Iterable[typing.Any], values)}
        if isinstance(values, list)
        else set(),
    )


def _member_roles(member: object) -> set[str]:
    return {str(role.id) for role in getattr(member, "roles", [])}


def _is_bot_member(guild: discord.Guild, member: object) -> bool:
    """Identify owaua itself without relying on a cached User object."""
    bot_member = getattr(guild, "me", None)
    return bool(bot_member and getattr(member, "id", None) == getattr(bot_member, "id", None))


def _render(
    template: object,
    *,
    member: typing.Any = None,
    guild: typing.Any = None,
    channel: typing.Any = None,
    extra: typing.Any = None,
) -> str:
    text = str(template or "")[:4000]
    values = {
        "user.id": str(getattr(member, "id", "")),
        "user.name": str(getattr(member, "display_name", getattr(member, "name", "user"))),
        "user.mention": str(getattr(member, "mention", "")),
        "server.id": str(getattr(guild, "id", "")),
        "server.name": str(getattr(guild, "name", "server")),
        "server.members": str(getattr(guild, "member_count", 0) or 0),
        "channel.id": str(getattr(channel, "id", "")),
        "channel.name": str(getattr(channel, "name", "channel")),
        "channel.mention": str(getattr(channel, "mention", "")),
    }
    for key, value in typing.cast(typing.Iterable[typing.Any], (extra or {}).items()):
        values[str(key)] = str(value)
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text[:2000]


async def _safe_send(
    channel: typing.Any,
    content: str = "",
    *,
    embed: typing.Any = None,
    files: typing.Any = None,
    everyone: bool = False,
):
    if channel is None or not hasattr(channel, "send"):
        return None
    try:
        return await channel.send(
            content=content[:2000] or None,
            embed=embed,
            files=files or None,
            allowed_mentions=discord.AllowedMentions(
                everyone=everyone, users=True, roles=everyone, replied_user=False
            ),
        )
    except (discord.Forbidden, discord.HTTPException):
        return None


def _previewable_attachment(attachment: discord.Attachment) -> bool:
    """Whether Discord can natively render an attachment in an audit-log post."""
    content_type = str(getattr(attachment, "content_type", "") or "").lower()
    if content_type.startswith(("image/", "video/", "audio/")):
        return True
    filename = str(getattr(attachment, "filename", "") or "").lower()
    return filename.endswith(
        (
            ".apng",
            ".avif",
            ".gif",
            ".jpeg",
            ".jpg",
            ".mov",
            ".mp3",
            ".mp4",
            ".mpeg",
            ".ogg",
            ".png",
            ".wav",
            ".webm",
            ".webp",
        )
    )


async def _log_media_files(message: discord.Message) -> list[discord.File]:
    """Copy previewable deleted-message media into the configured private log.

    Attachment CDN URLs can disappear after a deletion. Re-uploading the same
    media to the action-log message lets Discord render its normal image, audio,
    or video UI, without storing attachment bytes in the bot database.
    """
    files: list[discord.File] = []
    for attachment in list(getattr(message, "attachments", None) or [])[:10]:
        if not _previewable_attachment(attachment):
            continue
        try:
            files.append(await attachment.to_file(use_cached=True))
        except (discord.HTTPException, OSError, ValueError):
            continue
    return files


async def _http_get(
    url: str, *, json_response: bool = True, headers: dict[typing.Any, typing.Any] | None = None
):
    """Fetch one bounded response from the small fixed integration allowlist."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _HTTP_HOSTS:
        raise ValueError("unsupported integration endpoint")
    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            headers={"User-Agent": "owaua/2.0 (community integrations)", **(headers or {})},
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            raw = await response.content.read(512_001)
            if len(raw) > 512_000:
                raise ValueError("integration response is too large")
            if json_response:
                return json.loads(raw.decode("utf-8"))
            return raw.decode("utf-8", errors="replace")


async def _http_post(
    url: str,
    *,
    form: dict[typing.Any, typing.Any] | None = None,
    payload: dict[typing.Any, typing.Any] | None = None,
    headers: dict[typing.Any, typing.Any] | None = None,
):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _HTTP_HOSTS:
        raise ValueError("unsupported integration endpoint")
    timeout = aiohttp.ClientTimeout(total=10, connect=4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            data=form,
            json=payload,
            headers={"User-Agent": "owaua/2.0 (community integrations)", **(headers or {})},
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            raw = await response.content.read(512_001)
            if len(raw) > 512_000:
                raise ValueError("integration response is too large")
            return json.loads(raw.decode("utf-8"))


async def _app_token(provider: str) -> str:
    cached = _token_cache.get(provider)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    if provider == "twitch":
        client_id, secret = bot_config.TWITCH_CLIENT_ID, bot_config.TWITCH_CLIENT_SECRET
        endpoint = "https://id.twitch.tv/oauth2/token"
    elif provider == "kick":
        client_id, secret = bot_config.KICK_CLIENT_ID, bot_config.KICK_CLIENT_SECRET
        endpoint = "https://id.kick.com/oauth/token"
    else:
        return ""
    if not client_id or not secret:
        return ""
    data = await _http_post(
        endpoint,
        form={"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"},
    )
    token = str(data.get("access_token") or "")
    if token:
        _token_cache[provider] = (
            token,
            time.time() + max(300, int(data.get("expires_in") or 3600)),
        )
    return token


def _ignored(message: discord.Message, settings: dict[typing.Any, typing.Any]) -> bool:
    if str(message.channel.id) in _ids(settings.get("ignored_channel_ids")):
        return True
    category_id = getattr(message.channel, "category_id", None)
    if category_id is not None and str(category_id) in _ids(settings.get("ignored_category_ids")):
        return True
    return bool(_member_roles(message.author) & _ids(settings.get("ignored_role_ids")))


_LOG_KIND_SETTINGS: Final = {
    "audit": "audit_events",
    "message": "message_events",
    "member": "member_events",
    "moderation": "moderation_events",
    "voice": "voice_events",
    "role": "role_events",
    "channel": "channel_events",
    "thread": "thread_events",
    "server": "server_events",
    "reaction": "reaction_events",
    "command": "command_events",
}
_AUDIT_KIND_PREFIXES: Final = {
    "channel": "channel",
    "overwrite": "channel",
    "role": "role",
    "thread": "thread",
    "message": "message",
    "member": "member",
    "kick": "moderation",
    "ban": "moderation",
    "unban": "moderation",
    "automod": "moderation",
    "guild": "server",
    "invite": "server",
    "webhook": "server",
    "integration": "server",
    "emoji": "server",
    "sticker": "server",
    "scheduled": "server",
    "stage": "voice",
    "soundboard": "server",
    "onboarding": "server",
    "home": "server",
    "creator": "server",
    "app": "server",
    "bot": "server",
}
_AUDIT_SKIP_ACTIONS: Final = frozenset({"message_delete", "message_bulk_delete"})


def _log_value(value: object, *, limit: int = 350) -> str:
    """Render Discord audit values without dumping reprs or unbounded payloads."""
    if value is None:
        text = "Not set"
    elif isinstance(value, bool):
        text = "Yes" if value else "No"
    elif isinstance(value, (list, tuple, set)):
        text: typing.Any = (
            ", ".join(
                _log_value(item, limit=80)
                for item in typing.cast(
                    typing.Iterable[typing.Any], list(typing.cast(typing.Any, value))[:20]
                )
            )
            or "None"
        )
    elif isinstance(value, dict):
        pairs: typing.Any = typing.cast(
            typing.Any, list(typing.cast(typing.Any, value.items()))[:20]
        )
        text: typing.Any = (
            ", ".join(f"{key}: {_log_value(item, limit=80)}" for key, item in pairs) or "None"
        )
    else:
        object_id = getattr(value, "id", None)
        mention = getattr(value, "mention", None)
        name = getattr(value, "display_name", None) or getattr(value, "name", None)
        if mention:
            text = str(mention)
        elif name:
            text = str(name)
        else:
            text = str(value)
        if object_id is not None and str(object_id) not in text:
            text += f" (`{object_id}`)"
    text = embeds.de_emoji(text).replace("```", "''' ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _reaction_label(value: object) -> str:
    raw = str(value or "")
    clean = embeds.de_emoji(raw).strip()
    if clean:
        return clean[:100]
    codepoints = " ".join(f"U+{ord(char):04X}" for char in raw[:8])
    return f"Unicode reaction ({codepoints or 'unknown'})"


def _log_content(value: object, *, limit: int = 1500) -> str:
    """Make untrusted message text visibly data and prevent mention/markdown spoofing."""
    text = str(value or "")
    text = "".join(char if char in "\n\t" or ord(char) >= 32 else " " for char in text)
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _audit_change_lines(entry: discord.AuditLogEntry, *, maximum: int = 18) -> list[str]:
    try:
        before = dict(entry.before)
        after = dict(entry.after)
    except (AttributeError, TypeError, ValueError):
        return []
    lines: list[str] = []
    for key in list(dict.fromkeys([*before, *after]))[:maximum]:
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        lines.append(f"**{key.replace('_', ' ').title()}:** {_log_value(old)} → {_log_value(new)}")
    return lines


def _audit_extra_lines(extra: object) -> list[str]:
    if extra is None:
        return []
    values = getattr(extra, "__dict__", {})
    return [
        f"**{str(key).replace('_', ' ').title()}:** {_log_value(value)}"
        for key, value in list(values.items())[:10]
        if not str(key).startswith("_")
    ]


def _audit_kind(action_name: str) -> str:
    if action_name in {
        "member_prune",
        "member_move",
        "member_disconnect",
        "member_role_update",
    }:
        return "moderation"
    prefix = action_name.split("_", 1)[0]
    return _AUDIT_KIND_PREFIXES.get(prefix, "audit")


def _audit_stream_available(guild: discord.Guild) -> bool:
    settings = _cfg(guild, "action_log")["settings"]
    me = getattr(guild, "me", None)
    permissions = getattr(me, "guild_permissions", None)
    return bool(
        settings.get("audit_events", True)
        and permissions
        and (
            getattr(permissions, "view_audit_log", False)
            or getattr(permissions, "administrator", False)
        )
    )


async def _log(
    guild: discord.Guild,
    kind: str,
    title: str,
    description: str,
    *,
    channel: typing.Any = None,
    actor: typing.Any = None,
    target: typing.Any = None,
    reason: str | None = None,
    changes: list[str] | None = None,
    details: list[str] | None = None,
    event_id: object | None = None,
    color: int | None = None,
    files: list[discord.File] | None = None,
):
    config = _cfg(guild, "action_log")
    if not config["enabled"]:
        return
    settings = config["settings"]
    event_setting = _LOG_KIND_SETTINGS.get(kind)
    if event_setting and not settings.get(event_setting, True):
        return
    if actor is not None:
        if not settings.get("include_bot_events", True) and getattr(actor, "bot", False):
            return
        if str(getattr(actor, "id", "")) in _ids(settings.get("ignored_user_ids")):
            return
        if _member_roles(actor) & _ids(settings.get("ignored_role_ids")):
            return
    if target is not None:
        if str(getattr(target, "id", "")) in _ids(settings.get("ignored_user_ids")):
            return
        if _member_roles(target) & _ids(settings.get("ignored_role_ids")):
            return
    if channel is not None:
        if str(getattr(channel, "id", "")) in _ids(settings.get("ignored_channel_ids")):
            return
        category_id = getattr(channel, "category_id", None)
        if category_id is not None and str(category_id) in _ids(
            settings.get("ignored_category_ids")
        ):
            return
    destination = _channel(guild, settings.get("channel_id"))
    if destination is None:
        return
    palette = {
        "message": 0x5865F2,
        "member": 0x57F287,
        "moderation": 0xED4245,
        "voice": 0x9B59B6,
        "role": 0xFEE75C,
        "channel": 0x3498DB,
        "thread": 0x1ABC9C,
        "server": 0xE67E22,
        "reaction": 0xEB459E,
        "audit": 0x95A5A6,
        "command": 0x5865F2,
    }
    embed = discord.Embed(
        title=embeds.de_emoji(title)[:256],
        description=embeds.de_emoji(description)[:4096],
        color=color if color is not None else palette.get(kind, 0x95A5A6),
        timestamp=discord.utils.utcnow() if settings.get("include_timestamps", True) else None,
    )
    if actor is not None:
        actor_name = (
            getattr(actor, "display_name", None) or getattr(actor, "name", None) or str(actor)
        )
        actor_id = getattr(actor, "id", None)
        actor_value = _log_value(actor)
        if (
            settings.get("include_ids", True)
            and actor_id is not None
            and str(actor_id) not in actor_value
        ):
            actor_value += f" (`{actor_id}`)"
        embed.add_field(name="Actor", value=actor_value[:1024], inline=True)
        avatar = getattr(getattr(actor, "display_avatar", None), "url", None)
        if settings.get("show_avatars", True) and avatar:
            embed.set_author(name=embeds.de_emoji(str(actor_name))[:256], icon_url=str(avatar))
    if target is not None:
        target_value = _log_value(target)
        embed.add_field(name="Target", value=target_value[:1024], inline=True)
    if reason and settings.get("include_reasons", True):
        embed.add_field(
            name="Reason", value=embeds.de_emoji(_log_content(reason, limit=1024)), inline=False
        )
    if changes and settings.get("include_audit_changes", True):
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
    if details:
        embed.add_field(name="Details", value="\n".join(details)[:1024], inline=False)
    footer = [kind.replace("_", " ").title()]
    if settings.get("include_ids", True) and event_id is not None:
        footer.append(f"Event ID: {event_id}")
    embed.set_footer(text=" • ".join(footer)[:2048])
    posted = await _safe_send(destination, embed=embeds.fit_total(embed), files=files)
    if posted is None and files:
        await _safe_send(destination, embed=embeds.fit_total(embed))


async def _handle_afk(message: discord.Message) -> None:
    guild = message.guild
    if guild is None:
        return
    config = _cfg(guild, "afk")
    if not config["enabled"]:
        return
    scope = _scope(guild)
    settings = config["settings"]
    current = db.afk_get(scope, str(message.author.id))
    if current and str(message.channel.id) not in _ids(settings.get("ignored_channel_ids")):
        db.afk_clear(scope, str(message.author.id))
        if isinstance(message.author, discord.Member):
            try:
                await message.author.edit(nick=current.get("original_nick"), reason="AFK ended")
            except (discord.Forbidden, discord.HTTPException):
                pass
        notes = db.afk_notes_pop(scope, str(message.author.id))
        body = f"Welcome back {message.author.mention}; your AFK status was cleared."
        if notes:
            body += "\n" + "\n".join(
                f"• <@{note['author_id']}>: {note['content']}" for note in notes[:10]
            )
            if len(notes) > 10:
                body += f"\n…and {len(notes) - 10} more note(s)."
        await _safe_send(message.channel, body)
    seen: set[int] = set()
    lines: list[typing.Any] = []
    for member in message.mentions[:20]:
        if member.id in seen or member.id == message.author.id:
            continue
        seen.add(member.id)
        status = db.afk_get(scope, str(member.id))
        if status:
            lines.append(f"{member.display_name} is AFK: {status['reason']}")
    if lines:
        await _safe_send(message.channel, "\n".join(lines))


def _filter_matches(message: discord.Message, item: dict[typing.Any, typing.Any]) -> bool:
    content = message.content or ""
    lowered = content.casefold()
    kind = str(item.get("type") or "").lower()
    value = str(item.get("value") or "")
    has_link = bool(_URL_RE.search(content))
    has_invite = bool(_INVITE_RE.search(content))
    has_image = any((a.content_type or "").startswith("image/") for a in message.attachments)
    mapping = {
        "links": has_link,
        "contains_links": has_link,
        "non_links": not has_link,
        "does_not_contain_links": not has_link,
        "invites": has_invite,
        "discord_invites": has_invite,
        "non_invites": not has_invite,
        "images": has_image,
        "non_images": not has_image,
        "includes_text": value.casefold() in lowered,
        "exact_text": lowered == value.casefold(),
        "excludes_text": value.casefold() not in lowered,
        "starts_with": lowered.startswith(value.casefold()),
        "does_not_start_with": not lowered.startswith(value.casefold()),
        "ends_with": lowered.endswith(value.casefold()),
        "numbers_only": content.strip().isdigit(),
        "has_role": value in _member_roles(message.author),
        "does_not_have_role": value not in _member_roles(message.author),
        "embeds": bool(message.embeds),
        "bots": message.author.bot,
        "humans": not message.author.bot,
        "mentions": bool(message.mentions),
        "text_only": bool(content) and not message.attachments and not message.embeds,
    }
    return bool(mapping.get(kind, False))


async def _auto_delete(message: discord.Message) -> bool:
    config = _cfg(typing.cast(typing.Any, message.guild), "auto_delete")
    if not config["enabled"]:
        return False
    for rule in config["settings"].get("rules", [])[:100]:
        if (
            not isinstance(rule, dict)
            or typing.cast(typing.Any, rule).get("enabled", True) is False
        ):
            continue
        if typing.cast(typing.Any, rule).get("channel_id") and str(
            typing.cast(typing.Any, rule["channel_id"])
        ) != str(message.channel.id):
            continue
        filters: typing.Any = [
            item
            for item in typing.cast(
                typing.Iterable[typing.Any], typing.cast(typing.Any, rule).get("filters", [])[:3]
            )
            if isinstance(item, dict)
        ]
        if not filters:
            continue
        checks: typing.Any = [_filter_matches(message, item) for item in filters]
        match = (
            all(checks)
            if str(typing.cast(typing.Any, rule).get("match", "all")) == "all"
            else any(checks)
        )
        if not match:
            continue
        delay = max(0, min(86_400, int(typing.cast(typing.Any, rule).get("delay_seconds") or 0)))
        if delay:
            await asyncio.sleep(delay)
        try:
            await message.delete()
            await _log(
                typing.cast(typing.Any, message.guild),
                "message",
                "Auto Delete",
                f"Deleted a message from {message.author.mention} in {typing.cast(typing.Any, message).channel.mention}.",
                channel=message.channel,
            )
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False
    return False


def _automod_reason(message: discord.Message, settings: dict[typing.Any, typing.Any]) -> str | None:
    if _ignored(message, settings):
        return None
    content = message.content or ""
    if settings.get("allowed_channel_ids") and str(message.channel.id) not in _ids(
        settings["allowed_channel_ids"]
    ):
        return None
    if settings.get("allowed_role_ids") and not (
        _member_roles(message.author) & _ids(settings["allowed_role_ids"])
    ):
        return None
    lowered = content.casefold()
    for phrase in settings.get("banned_phrases", [])[:500]:
        if str(phrase).casefold() in lowered:
            return "banned phrase"
    letters = [char for char in content if char.isalpha()]
    caps = sum(1 for char in letters if char.isupper())
    if len(letters) >= 10 and caps * 100 / len(letters) >= int(
        settings.get("max_caps_percent") or 100
    ):
        return "excessive capitals"
    if content.count("\n") > int(settings.get("max_newlines") or 9999):
        return "chat clearing"
    if len(content) > int(settings.get("max_length") or 9999):
        return "message too long"
    if len(message.mentions) + len(message.role_mentions) > int(
        settings.get("max_mentions") or 9999
    ):
        return "mention spam"
    domains = [
        match.group(0).split("/")[2].lower().split(":")[0] for match in _URL_RE.finditer(content)
    ]
    blocked = {str(value).lower() for value in settings.get("blocked_domains", [])}
    allowed = {str(value).lower() for value in settings.get("allowed_domains", [])}
    if any(domain in blocked for domain in domains):
        return "blocked link"
    if allowed and any(domain not in allowed for domain in domains):
        return "unapproved link"
    key = (typing.cast(typing.Any, message.guild).id, message.author.id)
    now = time.monotonic()
    history = _rapid[key]
    window = max(1, int(settings.get("rapid_window_seconds") or 8))
    while history and now - history[0] > window:
        history.popleft()
    history.append(now)
    if len(history) > max(1, int(settings.get("rapid_messages") or 6)):
        return "rapid message spam"
    duplicate = _duplicates.get(key)
    _duplicates[key] = (lowered[:1000], now)
    if (
        duplicate
        and duplicate[0] == lowered[:1000]
        and now - duplicate[1] <= int(settings.get("duplicate_window_seconds") or 15)
    ):
        return "duplicate text"
    return None


async def _automod(message: discord.Message) -> bool:
    config = _cfg(typing.cast(typing.Any, message.guild), "automod")
    if not config["enabled"]:
        return False
    settings = config["settings"]
    reason = _automod_reason(message, settings)
    if not reason:
        return False
    deleted = False
    if settings.get("delete", True):
        try:
            await message.delete()
            deleted = True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
    if settings.get("warn", True):
        db.community_record_create(
            "warning",
            _scope(typing.cast(typing.Any, message.guild)),
            {"reason": reason, "message_id": str(message.id)},
            user_id=str(message.author.id),
            record_key=str(message.id),
        )
    minutes = max(0, min(40_320, int(settings.get("instant_timeout_minutes") or 0)))
    if minutes and isinstance(message.author, discord.Member):
        try:
            await message.author.timeout(timedelta(minutes=minutes), reason=f"Automod: {reason}")
        except (discord.Forbidden, discord.HTTPException):
            pass
    if settings.get("instant_ban") and isinstance(message.author, discord.Member):
        try:
            await message.author.ban(reason=f"Automod: {reason}")
        except (discord.Forbidden, discord.HTTPException):
            pass
    custom = str(settings.get("custom_response") or "")
    if custom:
        await _safe_send(
            message.channel,
            _render(
                custom,
                member=message.author,
                guild=message.guild,
                channel=message.channel,
                extra={"reason": reason},
            ),
        )
    log_channel = _channel(typing.cast(typing.Any, message.guild), settings.get("log_channel_id"))
    if log_channel:
        await _safe_send(
            log_channel,
            embed=embeds.say(
                f"{message.author.mention} in {typing.cast(typing.Any, message).channel.mention}: **{reason}**",
                title="Automod",
            ),
        )
    return deleted


def _native_automod_keywords(settings: dict[typing.Any, typing.Any]) -> list[str]:
    """Return safe Discord AutoMod keywords from the dashboard configuration."""
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in settings.get("banned_phrases", []):
        value = str(raw).strip()
        # Discord's keyword trigger rejects empty/oversized entries. Keep the
        # native rule bounded rather than allowing one bad dashboard value to
        # prevent the rest of the guild sync.
        if not value or len(value) > 60 or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        keywords.append(value)
        if len(keywords) >= 100:
            break
    return keywords


async def sync_native_automod(guild: discord.Guild) -> bool:
    """Mirror configured blocked phrases into one managed Discord AutoMod rule.

    The rule is deliberately limited to phrases already configured by the
    server owner. It is not created for an empty configuration, and rules
    belonging to other apps or administrators are never modified.
    """
    config = _cfg(guild, "automod")
    if not config["enabled"]:
        return False
    keywords = _native_automod_keywords(config["settings"])
    if not keywords:
        return False
    me = guild.me
    if me is None or not me.guild_permissions.manage_guild:
        return False

    actions = [
        discord.AutoModRuleAction(
            type=discord.AutoModRuleActionType.block_message,
        )
    ]
    log_channel = _channel(guild, config["settings"].get("log_channel_id"))
    if log_channel is not None:
        actions.append(
            discord.AutoModRuleAction(
                type=discord.AutoModRuleActionType.send_alert_message,
                channel_id=log_channel.id,
            )
        )
    trigger = discord.AutoModTrigger(
        type=discord.AutoModRuleTriggerType.keyword,
        keyword_filter=keywords,
    )
    try:
        rules = await guild.fetch_automod_rules()
        managed = next(
            (rule for rule in rules if rule.name == _NATIVE_AUTOMOD_RULE_NAME),
            None,
        )
        if managed is None:
            await guild.create_automod_rule(
                name=_NATIVE_AUTOMOD_RULE_NAME,
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason="Sync owaua configured blocked phrases",
            )
        else:
            await managed.edit(
                name=_NATIVE_AUTOMOD_RULE_NAME,
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason="Sync owaua configured blocked phrases",
            )
        return True
    except (discord.Forbidden, discord.HTTPException):
        log.warning("could not sync native AutoMod for guild %s", guild.id, exc_info=True)
        return False


async def _slowmode(message: discord.Message) -> bool:
    config = _cfg(typing.cast(typing.Any, message.guild), "slowmode")
    if not config["enabled"]:
        return False
    rule: typing.Any = typing.cast(
        typing.Any,
        next(
            (
                r
                for r in typing.cast(
                    list[dict[str, typing.Any]],
                    config["settings"].get("channels", []),
                )
                if isinstance(r, dict)
                and str(typing.cast(typing.Any, r).get("channel_id")) == str(message.channel.id)
            ),
            None,
        ),
    )
    if not rule or str(rule.get("mode", "bot")) != "bot":
        return False
    seconds = max(1, min(21_600, int(rule.get("seconds") or 5)))
    key = (message.channel.id, message.author.id)
    now = time.monotonic()
    previous = _slow.get(key, 0.0)
    _slow[key] = now
    if now - previous >= seconds:
        return False
    try:
        await message.delete()
        return True
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return False


def _triggered(content: str, trigger: str, match: str) -> bool:
    if trigger == "{*}":
        return True
    text, needle = content.casefold(), trigger.casefold()
    if match == "exact":
        return text == needle
    if match == "wildcard":
        return fnmatch.fnmatch(text, needle if "*" in needle else f"*{needle}*")
    return needle in text


async def _autorespond(message: discord.Message) -> None:
    config = _cfg(typing.cast(typing.Any, message.guild), "autoresponder")
    if not config["enabled"]:
        return
    for item in config["settings"].get("responders", [])[:500]:
        if not isinstance(item, dict) or not _triggered(
            message.content or "",
            str(typing.cast(typing.Any, item).get("trigger") or ""),
            str(typing.cast(typing.Any, item).get("match") or "contains"),
        ):
            continue
        roles = _member_roles(message.author)
        if typing.cast(typing.Any, item).get("allowed_role_ids") and not roles & _ids(
            typing.cast(typing.Any, item["allowed_role_ids"])
        ):
            continue
        if roles & _ids(typing.cast(typing.Any, item).get("ignored_role_ids")):
            continue
        if typing.cast(typing.Any, item).get("allowed_channel_ids") and str(
            message.channel.id
        ) not in _ids(typing.cast(typing.Any, item["allowed_channel_ids"])):
            continue
        if str(message.channel.id) in _ids(
            typing.cast(typing.Any, item).get("ignored_channel_ids")
        ):
            continue
        response = _render(
            typing.cast(typing.Any, item).get("response"),
            member=message.author,
            guild=message.guild,
            channel=message.channel,
        )
        if response:
            await _safe_send(message.channel, response)
        for emoji in typing.cast(
            typing.Iterable[typing.Any], typing.cast(typing.Any, item).get("reactions", [])[:3]
        ):
            try:
                await message.add_reaction(str(emoji))
            except (discord.Forbidden, discord.HTTPException):
                pass
        break


async def _highlights(message: discord.Message) -> None:
    config = _cfg(typing.cast(typing.Any, message.guild), "highlights")
    if not config["enabled"] or str(message.channel.id) in _ids(
        config["settings"].get("ignored_channel_ids")
    ):
        return
    for item in db.community_records(
        "highlight", _scope(typing.cast(typing.Any, message.guild)), limit=1000
    ):
        if item.get("user_id") == str(message.author.id):
            continue
        phrase = str(item["data"].get("phrase") or "")
        if phrase and phrase.casefold() in (message.content or "").casefold():
            user = (
                typing.cast(typing.Any, message.guild).get_member(int(item["user_id"]))
                if str(item.get("user_id", "")).isdigit()
                else None
            )
            if user:
                await _safe_send(
                    user,
                    f"Your highlight **{phrase}** was used by {message.author} in **{typing.cast(typing.Any, message.guild).name}** / {typing.cast(typing.Any, message).channel.mention}:\n{message.jump_url}",
                )


async def handle_message(message: discord.Message) -> bool:
    """Run enabled message modules; return True if the message was deleted."""
    if message.guild is None or message.author.bot:
        return False
    await _handle_afk(message)
    if await _auto_delete(message):
        return True
    if await _automod(message):
        return True
    if await _slowmode(message):
        return True
    await _autorespond(message)
    await _highlights(message)
    return False


async def member_join(member: discord.Member) -> None:
    guild = member.guild
    db.log_interaction("member_join", str(member.id), _scope(guild))
    autoban = _cfg(guild, "autoban")
    if autoban["enabled"]:
        settings = autoban["settings"]
        age_hours = (discord.utils.utcnow() - member.created_at).total_seconds() / 3600
        name = member.name.casefold()
        matches = age_hours < int(settings.get("minimum_account_age_hours") or 0)
        matches = matches or any(
            str(v).casefold() in name for v in settings.get("username_contains", [])
        )
        matches = matches or name in {str(v).casefold() for v in settings.get("username_exact", [])}
        matches = matches or any(
            fnmatch.fnmatch(name, str(v).casefold()) for v in settings.get("username_wildcards", [])
        )
        if matches:
            try:
                await member.ban(reason=str(settings.get("reason") or "Autoban rule")[:512])
                await _log(
                    guild, "moderation", "Autoban", f"Banned {member} (`{member.id}`) on join."
                )
                return
            except (discord.Forbidden, discord.HTTPException):
                pass
    roles = _cfg(guild, "autoroles")
    if roles["enabled"]:
        for item in roles["settings"].get("join_roles", [])[:100]:
            if not isinstance(item, dict):
                continue
            role_id = str(typing.cast(typing.Any, item).get("role_id") or "")
            role = guild.get_role(int(role_id)) if role_id.isdigit() else None
            if not role:
                continue
            delay = max(
                0, min(2_592_000, int(typing.cast(typing.Any, item).get("delay_seconds") or 0))
            )
            if delay:
                await asyncio.sleep(delay)
            try:
                await member.add_roles(role, reason="Configured autorole")
            except (discord.Forbidden, discord.HTTPException):
                continue
            remove_after = max(
                0,
                min(2_592_000, int(typing.cast(typing.Any, item).get("remove_after_seconds") or 0)),
            )
            if remove_after:
                await asyncio.sleep(remove_after)
                try:
                    await member.remove_roles(role, reason="Autorole expiry")
                except (discord.Forbidden, discord.HTTPException):
                    pass
    welcome = _cfg(guild, "welcome")
    if welcome["enabled"]:
        settings = welcome["settings"]
        target = _channel(guild, settings.get("channel_id"))
        if target:
            await _safe_send(
                target, _render(settings.get("message"), member=member, guild=guild, channel=target)
            )
            if settings.get("journey_enabled"):
                rules_channel = str(settings.get("rules_channel_id") or "")
                journey_text = (
                    f"{member.mention}, start by reading "
                    + (f"<#{rules_channel}>" if rules_channel.isdigit() else "the server rules")
                    + ", then acknowledge them below. Role choices and intro prompts stay opt-in."
                )
                await typing.cast(typing.Any, target).send(
                    journey_text,
                    view=PersistentOnboardingView(guild.id, settings),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                followup_hours = max(1, min(720, int(settings.get("help_followup_hours") or 24)))
                db.community_record_create(
                    "onboarding_followup",
                    _scope(guild),
                    {
                        "message": str(
                            settings.get("help_message") or "Need a hand getting started?"
                        )[:500]
                    },
                    user_id=str(member.id),
                    record_key=str(member.id),
                    due=time.time() + followup_hours * 3600,
                )
        dm_message = _render(settings.get("dm_message"), member=member, guild=guild)
        if dm_message:
            await _safe_send(member, dm_message)
    announce = _cfg(guild, "announcements")
    if announce["enabled"]:
        target = _channel(guild, announce["settings"].get("channel_id"))
        if target:
            await _safe_send(
                target,
                _render(
                    announce["settings"].get("join_message"),
                    member=member,
                    guild=guild,
                    channel=target,
                ),
            )
    action_settings = _cfg(guild, "action_log")["settings"]
    join_description = f"{member.mention} (`{member.id}`) joined."
    if action_settings.get("show_account_age", True):
        join_description += (
            f" Account created {discord.utils.format_dt(member.created_at, style='R')}."
        )
    await _log(
        guild,
        "member",
        "Member joined",
        join_description,
        actor=member,
        target=member,
        event_id=member.id,
    )


async def member_remove(member: discord.Member) -> None:
    db.log_interaction("member_leave", str(member.id), _scope(member.guild))
    announce = _cfg(member.guild, "announcements")
    if announce["enabled"]:
        target = _channel(member.guild, announce["settings"].get("channel_id"))
        if target:
            await _safe_send(
                target,
                _render(
                    announce["settings"].get("leave_message"),
                    member=member,
                    guild=member.guild,
                    channel=target,
                ),
            )
    if _audit_stream_available(member.guild):
        await asyncio.sleep(0.4)
        try:
            async for entry in member.guild.audit_logs(limit=8):
                if abs((discord.utils.utcnow() - entry.created_at).total_seconds()) > 15:
                    continue
                if entry.action not in {discord.AuditLogAction.kick, discord.AuditLogAction.ban}:
                    continue
                if str(getattr(entry.target, "id", "")) == str(member.id):
                    return
        except (discord.Forbidden, discord.HTTPException):
            pass
    await _log(
        member.guild,
        "member",
        "Member left",
        f"{member} (`{member.id}`) left the server.",
        actor=member,
        target=member,
        event_id=member.id,
    )


async def member_ban(guild: discord.Guild, user: discord.User | discord.Member) -> None:
    announce = _cfg(guild, "announcements")
    if announce["enabled"]:
        target = _channel(guild, announce["settings"].get("channel_id"))
        if target:
            await _safe_send(
                target,
                _render(
                    announce["settings"].get("ban_message"),
                    member=user,
                    guild=guild,
                    channel=target,
                ),
            )
    await gateway_event_log(
        guild,
        "moderation",
        "Member banned",
        f"{user} (`{user.id}`) was banned.",
        audit_backed=True,
        target=user,
    )


async def message_delete(message: discord.Message) -> None:
    if message.guild is None:
        return
    settings = _cfg(message.guild, "action_log")["settings"]
    if not settings.get("include_bot_events", True) and message.author.bot:
        return
    if str(message.author.id) in _ids(settings.get("ignored_user_ids")):
        return
    if _member_roles(message.author) & _ids(settings.get("ignored_role_ids")):
        return
    entry = await recent_audit_entry(
        message.guild,
        discord.AuditLogAction.message_delete,
        channel_id=getattr(message.channel, "id", None),
        target_id=getattr(message.author, "id", None),
    )
    lines = [
        f"**Author:** {_log_value(message.author)}",
        f"**Channel:** {_log_value(message.channel)}",
    ]
    if settings.get("include_message_content", True):
        lines.append(f"**Content:** {_log_content(message.content or '(no text)')}")
    if settings.get("include_attachments", True) and message.attachments:
        files = "\n".join(str(attachment.url) for attachment in message.attachments[:10])
        lines.append(f"**Files:**\n{files}")
    media_files = (
        await _log_media_files(message)
        if settings.get("include_attachments", True) and message.attachments
        else None
    )
    own_message = _is_bot_member(message.guild, message.author)
    await _log(
        message.guild,
        "message",
        "One of my messages was deleted" if own_message else "Message deleted",
        ("I noticed that one of my messages was deleted.\n" if own_message else "")
        + "\n".join(lines),
        channel=message.channel,
        actor=getattr(entry, "user", None) or message.author,
        target=message.author,
        reason=getattr(entry, "reason", None),
        details=_audit_extra_lines(getattr(entry, "extra", None)) if entry else None,
        event_id=getattr(entry, "id", None) or message.id,
        files=media_files,
    )


async def bulk_message_delete(messages: list[discord.Message]) -> None:
    first = next((message for message in messages if message.guild), None)
    if first is None or first.guild is None:
        return
    settings = _cfg(first.guild, "action_log")["settings"]
    entry = await recent_audit_entry(
        first.guild,
        discord.AuditLogAction.message_bulk_delete,
        channel_id=getattr(first.channel, "id", None),
    )
    authors: dict[str, int] = defaultdict(int)
    for message in messages:
        authors[_log_value(message.author, limit=100)] += 1
    lines = [
        f"Deleted **{len(messages)}** messages in {_log_value(first.channel)}.",
        "**Authors:** "
        + ", ".join(f"{name}: {count}" for name, count in list(authors.items())[:20]),
    ]
    sample_size = max(0, min(50, int(settings.get("bulk_delete_sample_size") or 0)))
    if settings.get("include_message_content", True) and sample_size:
        samples: list[typing.Any] = []
        for message in messages[:sample_size]:
            content = _log_content(message.content or "(attachment/no text)", limit=140).replace(
                "\n", " "
            )
            samples.append(f"• {_log_value(message.author, limit=60)}: {content}")
        if samples:
            lines.append("**Sample:**\n" + "\n".join(samples))
    await _log(
        first.guild,
        "message",
        "Bulk message deletion",
        "\n".join(lines)[:3900],
        channel=first.channel,
        actor=getattr(entry, "user", None),
        target=first.channel,
        reason=getattr(entry, "reason", None),
        details=_audit_extra_lines(getattr(entry, "extra", None)) if entry else None,
        event_id=getattr(entry, "id", None),
        color=0xED4245,
    )


async def raw_message_delete(
    client: discord.Client, payload: discord.RawMessageDeleteEvent
) -> None:
    if payload.guild_id is None or payload.cached_message is not None:
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    channel = guild.get_channel_or_thread(payload.channel_id)
    settings = _cfg(guild, "action_log")["settings"]
    saved = db.server_message_get(Scope.guild(guild.id).key, str(payload.message_id))
    entry = await recent_audit_entry(
        guild,
        discord.AuditLogAction.message_delete,
        channel_id=payload.channel_id,
    )
    if saved is not None:
        author_id = str(saved["user_id"])
        get_member = getattr(guild, "get_member", None)
        author = (
            typing.cast(typing.Callable[[int], typing.Any], get_member)(int(author_id))
            if callable(get_member)
            else None
        )
        if author is None:
            author = discord.Object(id=int(author_id))
        lines = [
            f"**Author:** {_log_value(author)}",
            f"**Channel:** {_log_value(channel or saved['channel_name'])}",
            "**Source:** Consent-scoped message history",
        ]
        if settings.get("include_message_content", True):
            lines.append(f"**Content:** {_log_content(saved['content'] or '(no text)')}")
        own_message = str(saved["user_id"]) == str(getattr(getattr(guild, "me", None), "id", ""))
        await _log(
            guild,
            "message",
            "One of my uncached messages was deleted"
            if own_message
            else "Recovered uncached message deletion",
            ("I noticed that one of my messages was deleted.\n" if own_message else "")
            + "\n".join(lines),
            channel=channel,
            actor=getattr(entry, "user", None) or author,
            target=author,
            reason=getattr(entry, "reason", None),
            details=_audit_extra_lines(getattr(entry, "extra", None)) if entry else None,
            event_id=getattr(entry, "id", None) or payload.message_id,
            color=0xED4245,
        )
        return
    await _log(
        guild,
        "message",
        "Uncached message deleted",
        f"Message `{payload.message_id}` was deleted in {_log_value(channel or payload.channel_id)}. Its content was not present in Discord's local cache.",
        channel=channel,
        actor=getattr(entry, "user", None),
        target=channel,
        reason=getattr(entry, "reason", None),
        event_id=getattr(entry, "id", None) or payload.message_id,
        color=0xED4245,
    )


async def raw_bulk_message_delete(
    client: discord.Client,
    payload: discord.RawBulkMessageDeleteEvent,
) -> None:
    if payload.guild_id is None:
        return
    if len(payload.cached_messages) == len(payload.message_ids):
        await bulk_message_delete(list(payload.cached_messages))
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    channel = guild.get_channel_or_thread(payload.channel_id)
    entry = await recent_audit_entry(
        guild,
        discord.AuditLogAction.message_bulk_delete,
        channel_id=payload.channel_id,
    )
    missing = len(payload.message_ids) - len(payload.cached_messages)
    description = (
        f"Deleted **{len(payload.message_ids)}** messages in {_log_value(channel or payload.channel_id)}. "
        f"**{missing}** message(s) were not cached, so their content was unavailable."
    )
    settings = _cfg(guild, "action_log")["settings"]
    sample_size = max(0, min(50, int(settings.get("bulk_delete_sample_size") or 0)))
    uncached_ids = set(payload.message_ids) - {message.id for message in payload.cached_messages}
    recovered = db.server_messages_get(Scope.guild(guild.id).key, uncached_ids)
    if recovered:
        description += f" **{len(recovered)}** uncached message(s) were recovered from consent-scoped message history."
    if settings.get("include_message_content", True) and sample_size:
        samples = [
            f"• {_log_value(message.author, limit=60)}: {_log_content(message.content or '(attachment/no text)', limit=140).replace(chr(10), ' ')}"
            for message in list(payload.cached_messages)[:sample_size]
        ]
        remaining = max(0, sample_size - len(samples))
        samples.extend(
            f"• {_log_value(row['display_name'] or row['username'], limit=60)}: "
            f"{_log_content(row['content'] or '(no text)', limit=140).replace(chr(10), ' ')}"
            for row in list(recovered.values())[:remaining]
        )
        if samples:
            description += "\n**Recovered/cached sample:**\n" + "\n".join(samples)
    await _log(
        guild,
        "message",
        "Bulk message deletion",
        description,
        channel=channel,
        actor=getattr(entry, "user", None),
        target=channel,
        reason=getattr(entry, "reason", None),
        details=_audit_extra_lines(getattr(entry, "extra", None)) if entry else None,
        event_id=getattr(entry, "id", None),
        color=0xED4245,
    )


async def raw_message_edit(client: discord.Client, payload: discord.RawMessageUpdateEvent) -> None:
    if payload.guild_id is None or payload.cached_message is not None:
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    channel = guild.get_channel_or_thread(payload.channel_id)
    settings = _cfg(guild, "action_log")["settings"]
    description = (
        f"Message `{payload.message_id}` was edited in {_log_value(channel or payload.channel_id)}."
    )
    content = payload.data.get("content")
    if settings.get("include_message_content", True) and isinstance(content, str):
        description += f"\n**New content:** {_log_content(content)}"
    await _log(
        guild,
        "message",
        "Uncached message edited",
        description,
        channel=channel,
        target=channel,
        event_id=payload.message_id,
    )


async def message_edit(before: discord.Message, after: discord.Message) -> None:
    if after.guild is None or before.content == after.content:
        return
    settings = _cfg(after.guild, "action_log")["settings"]
    if not settings.get("include_bot_events", True) and after.author.bot:
        return
    lines = [f"**Author:** {_log_value(after.author)}", f"**Channel:** {_log_value(after.channel)}"]
    if settings.get("include_message_content", True):
        lines.extend(
            (
                f"**Before:** {_log_content(before.content or '(empty)', limit=1000)}",
                f"**After:** {_log_content(after.content or '(empty)', limit=1000)}",
            )
        )
    lines.append(f"[Jump to message]({after.jump_url})")
    own_message = _is_bot_member(after.guild, after.author)
    await _log(
        after.guild,
        "message",
        "One of my messages was edited" if own_message else "Message edited",
        ("I noticed that one of my messages was edited.\n" if own_message else "")
        + "\n".join(lines),
        channel=after.channel,
        actor=after.author,
        target=after.channel,
        event_id=after.id,
    )


async def voice_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    guild = member.guild
    if before.channel != after.channel:
        if before.channel and after.channel:
            text = f"{member.mention} moved from **{before.channel.name}** to **{after.channel.name}**."
        elif after.channel:
            text = f"{member.mention} joined **{after.channel.name}**."
        else:
            text = f"{member.mention} left **{typing.cast(typing.Any, before.channel).name}**."
        await _log(
            guild,
            "voice",
            "Voice channel changed",
            text,
            actor=member,
            target=after.channel or before.channel,
        )
    settings = _cfg(guild, "action_log")["settings"]
    if settings.get("include_voice_state_changes", True):
        labels = {
            "self_mute": "Self mute",
            "self_deaf": "Self deafen",
            "mute": "Server mute",
            "deaf": "Server deafen",
            "self_stream": "Screen share",
            "self_video": "Camera",
            "suppress": "Stage suppression",
            "requested_to_speak_at": "Requested to speak",
        }
        changed: list[typing.Any] = []
        for attribute, label_text in labels.items():
            old, new = getattr(before, attribute, None), getattr(after, attribute, None)
            if old != new:
                changed.append(f"**{label_text}:** {_log_value(old)} → {_log_value(new)}")
        if changed:
            await _log(
                guild,
                "voice",
                "Voice state updated",
                f"Voice state changed for {member.mention}.",
                actor=member,
                target=after.channel or before.channel,
                changes=changed,
            )
    config = _cfg(guild, "voice_text")
    if not config["enabled"]:
        return
    for binding in config["settings"].get("bindings", [])[:100]:
        if not isinstance(binding, dict):
            continue
        voice_id, text_id = (
            str(typing.cast(typing.Any, binding).get("voice_channel_id")),
            str(typing.cast(typing.Any, binding).get("text_channel_id")),
        )
        channel = guild.get_channel(int(text_id)) if text_id.isdigit() else None
        if not isinstance(channel, discord.TextChannel):
            continue
        if after.channel and str(after.channel.id) == voice_id:
            try:
                await channel.set_permissions(
                    member,
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    reason="Voice-text link joined",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            if typing.cast(typing.Any, binding).get("join_message"):
                await _safe_send(
                    channel,
                    _render(
                        typing.cast(typing.Any, binding["join_message"]),
                        member=member,
                        guild=guild,
                        channel=channel,
                    ),
                )
        if (
            before.channel
            and str(before.channel.id) == voice_id
            and (not after.channel or str(after.channel.id) != voice_id)
        ):
            try:
                await channel.set_permissions(member, overwrite=None, reason="Voice-text link left")
            except (discord.Forbidden, discord.HTTPException):
                pass
            if typing.cast(typing.Any, binding).get("leave_message"):
                await _safe_send(
                    channel,
                    _render(
                        typing.cast(typing.Any, binding["leave_message"]),
                        member=member,
                        guild=guild,
                        channel=channel,
                    ),
                )
            if (
                typing.cast(typing.Any, binding).get("purge_when_empty")
                and not before.channel.members
            ):
                try:
                    await channel.purge(limit=1000, check=lambda m: not m.pinned)
                except (discord.Forbidden, discord.HTTPException):
                    pass


async def raw_reaction(
    client: discord.Client,
    payload: discord.RawReactionActionEvent,
    *,
    added: bool = True,
) -> None:
    if payload.guild_id is None or client.user is None:
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    reaction = _cfg(guild, "reaction_roles")
    if reaction["enabled"]:
        for menu in reaction["settings"].get("menus", [])[:100]:
            if not isinstance(menu, dict) or str(
                typing.cast(typing.Any, menu).get("message_id")
            ) != str(payload.message_id):
                continue
            for item in typing.cast(
                typing.Iterable[typing.Any], typing.cast(typing.Any, menu).get("items", [])[:20]
            ):
                if not isinstance(item, dict) or str(
                    typing.cast(typing.Any, item).get("emoji")
                ) != str(payload.emoji):
                    continue
                role_id = str(typing.cast(typing.Any, item).get("role_id") or "")
                role = guild.get_role(int(role_id)) if role_id.isdigit() else None
                member = guild.get_member(payload.user_id)
                if role and member:
                    try:
                        mode = str(typing.cast(typing.Any, menu).get("mode", "toggle"))
                        if added and mode != "remove":
                            await member.add_roles(role, reason="Reaction role")
                        elif not added and mode not in {"add", "add_only"}:
                            await member.remove_roles(role, reason="Reaction role removed")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
    if not added:
        return
    star = _cfg(guild, "starboard")
    settings = star["settings"]
    if not star["enabled"] or str(payload.emoji) != str(settings.get("emoji") or "⭐"):
        return
    source = guild.get_channel(payload.channel_id)
    target = _channel(guild, settings.get("channel_id"))
    if not source or not target or str(source.id) in _ids(settings.get("ignored_channel_ids")):
        return
    try:
        message: typing.Any = await typing.cast(typing.Any, source).fetch_message(
            payload.message_id
        )
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return
    reaction_obj: typing.Any = typing.cast(
        typing.Any,
        next(
            (
                r
                for r in typing.cast(typing.Iterable[typing.Any], message.reactions)
                if str(r.emoji) == str(payload.emoji)
            ),
            None,
        ),
    )
    if not reaction_obj or reaction_obj.count < max(1, int(settings.get("threshold") or 3)):
        return
    existing = db.community_records("starboard", _scope(guild), limit=5000)
    if any(item.get("record_key") == str(message.id) for item in existing):
        return
    posted = await _safe_send(
        target,
        content=f"{payload.emoji} **{reaction_obj.count}** · {message.channel.mention} · {message.jump_url}",
        embed=embeds.say((message.content or "(attachment)")[:3900], title=str(message.author)),
    )
    if posted:
        db.community_record_create(
            "starboard",
            _scope(guild),
            {"starboard_message_id": str(posted.id), "stars": reaction_obj.count},
            record_key=str(message.id),
        )


async def reaction_event(
    client: discord.Client,
    payload: discord.RawReactionActionEvent,
    *,
    added: bool,
) -> None:
    if payload.guild_id is None or client.user is None or payload.user_id == client.user.id:
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    actor = guild.get_member(payload.user_id) or discord.Object(id=payload.user_id)
    channel = guild.get_channel_or_thread(payload.channel_id)
    verb = "added" if added else "removed"
    await _log(
        guild,
        "reaction",
        f"Reaction {verb}",
        f"{_log_value(actor)} {verb} **{_reaction_label(payload.emoji)}** on message `{payload.message_id}` in {_log_value(channel or payload.channel_id)}.",
        channel=channel,
        actor=actor,
        target=channel,
        event_id=payload.message_id,
    )


async def poll_vote_event(
    client: discord.Client,
    payload: discord.RawPollVoteActionEvent,
    *,
    added: bool,
) -> None:
    if payload.guild_id is None:
        return
    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return
    actor = guild.get_member(payload.user_id) or discord.Object(id=payload.user_id)
    channel = guild.get_channel_or_thread(payload.channel_id)
    verb = "added" if added else "removed"
    await _log(
        guild,
        "reaction",
        f"Poll vote {verb}",
        f"{_log_value(actor)} {verb} a vote for answer `{payload.answer_id}` on message `{payload.message_id}` in {_log_value(channel or payload.channel_id)}.",
        channel=channel,
        actor=actor,
        target=channel,
        event_id=payload.message_id,
    )


async def command_event(message: discord.Message, name: str, content: str) -> None:
    if message.guild is None or not name:
        return
    settings = _cfg(message.guild, "action_log")["settings"]
    description = f"{_log_value(message.author)} invoked prefix command **{name[:100]}** in {_log_value(message.channel)}."
    if settings.get("include_message_content", True):
        description += f"\n**Input:** {_log_content(content)}"
    await _log(
        message.guild,
        "command",
        "Prefix command invoked",
        description,
        channel=message.channel,
        actor=message.author,
        target=message.channel,
        event_id=message.id,
    )


async def interaction_event(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        return
    data: typing.Any = interaction.data if isinstance(interaction.data, dict) else {}
    interaction_type = getattr(interaction.type, "name", str(interaction.type))
    name = str(data.get("name") or data.get("custom_id") or interaction_type)[:100]
    await _log(
        interaction.guild,
        "command",
        "Discord interaction received",
        f"{_log_value(interaction.user)} used **{interaction_type.replace('_', ' ')}** `{name}` in {_log_value(interaction.channel)}.",
        channel=interaction.channel,
        actor=interaction.user,
        target=interaction.channel,
        event_id=interaction.id,
    )


async def event_log(
    guild: discord.Guild,
    kind: str,
    title: str,
    description: str,
    *,
    channel: typing.Any = None,
    actor: typing.Any = None,
    target: typing.Any = None,
    reason: str | None = None,
    changes: list[str] | None = None,
    details: list[str] | None = None,
    event_id: object | None = None,
    color: int | None = None,
) -> None:
    await _log(
        guild,
        kind,
        title,
        description,
        channel=channel,
        actor=actor,
        target=target,
        reason=reason,
        changes=changes,
        details=details,
        event_id=event_id,
        color=color,
    )


async def gateway_event_log(
    guild: discord.Guild,
    kind: str,
    title: str,
    description: str,
    *,
    audit_backed: bool = False,
    **kwargs: typing.Any,
) -> None:
    """Log a gateway event unless Discord's richer audit stream owns it."""
    if audit_backed and _audit_stream_available(guild):
        return
    await event_log(guild, kind, title, description, **kwargs)


async def audit_entry_log(entry: discord.AuditLogEntry) -> None:
    """Log every Discord administrative action exposed by the audit gateway."""
    action_name = str(getattr(entry.action, "name", entry.action) or "unknown_action")
    if action_name in _AUDIT_SKIP_ACTIONS:
        return
    config = _cfg(entry.guild, "action_log")
    if not config["enabled"] or not config["settings"].get("audit_events", True):
        return
    kind = _audit_kind(action_name)
    action_title = action_name.replace("_", " ").title()
    actor = getattr(entry, "user", None)
    target = getattr(entry, "target", None)
    extra = getattr(entry, "extra", None)
    channel = getattr(extra, "channel", None)
    if channel is None and isinstance(target, (discord.abc.GuildChannel, discord.Thread)):
        channel = target
    category = getattr(entry.action, "category", None)
    color: typing.Any = typing.cast(
        typing.Any,
        {
            discord.AuditLogActionCategory.create: 0x57F287,
            discord.AuditLogActionCategory.delete: 0xED4245,
            discord.AuditLogActionCategory.update: 0xFEE75C,
        }.get(typing.cast(typing.Any, category), None),
    )
    bot_member = getattr(entry.guild, "me", None)
    target_is_bot = _is_bot_member(entry.guild, target)
    target_is_bot_role = bool(
        bot_member
        and target is not None
        and any(
            getattr(role, "id", None) == getattr(target, "id", None)
            for role in getattr(bot_member, "roles", [])
        )
    )
    self_target = target_is_bot or target_is_bot_role
    if self_target:
        action_title = {
            "member_role_update": "My roles changed",
            "role_update": "One of my roles was edited",
        }.get(action_name, f"My settings changed ({action_title})")
    description = (
        "I noticed this change affecting me.\n" if self_target else ""
    ) + f"{_log_value(actor or 'Discord')} performed **{action_title}**"
    if target is not None:
        description += f" on {_log_value(target)}"
    description += "."
    await _log(
        entry.guild,
        kind,
        action_title,
        description,
        channel=channel,
        actor=actor,
        target=target,
        reason=getattr(entry, "reason", None),
        changes=_audit_change_lines(entry),
        details=_audit_extra_lines(extra),
        event_id=entry.id,
        color=color,
    )


async def recent_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    *,
    channel_id: object | None = None,
    target_id: object | None = None,
) -> discord.AuditLogEntry | None:
    """Resolve a recent delete actor without duplicating the audit-stream event."""
    if not _audit_stream_available(guild):
        return None
    await asyncio.sleep(0.4)
    now = discord.utils.utcnow()
    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            if abs((now - entry.created_at).total_seconds()) > 15:
                continue
            entry_target = getattr(entry, "target", None)
            extra = getattr(entry, "extra", None)
            extra_channel = getattr(extra, "channel", None)
            if target_id is not None and str(getattr(entry_target, "id", "")) != str(target_id):
                continue
            if channel_id is not None:
                possible = {
                    str(getattr(entry_target, "id", "")),
                    str(getattr(extra_channel, "id", "")),
                }
                if str(channel_id) not in possible:
                    continue
            return entry
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


def _parse_duration(value: str) -> int | None:
    match = _DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2).lower()
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]


async def handle_prefix_command(message: discord.Message, name: str, arg: str) -> bool:
    """Handle community commands not owned by the legacy command table."""
    if message.guild is None:
        return False
    guild, scope, uid = message.guild, _scope(message.guild), str(message.author.id)
    if name == "afk" and _cfg(guild, "afk")["enabled"]:
        bits = arg.split(maxsplit=2)
        if bits and bits[0].lower() == "list":
            rows = db.afk_list(scope)
            await _safe_send(
                message.channel,
                embed=embeds.say(
                    "\n".join(f"<@{r['user_id']}> — {r['reason']}" for r in rows)
                    or "Nobody is AFK.",
                    title="AFK statuses",
                ),
            )
            return True
        if bits and bits[0].lower() == "note" and message.mentions:
            target = message.mentions[0]
            note = bits[2] if len(bits) > 2 else "Left you a note."
            if not db.afk_get(scope, str(target.id)):
                await _safe_send(message.channel, "That user is not AFK.")
            else:
                db.afk_note_add(
                    scope,
                    str(target.id),
                    uid,
                    note,
                    channel_id=str(message.channel.id),
                    message_id=str(message.id),
                )
                await _safe_send(
                    message.channel, f"I’ll give {target.display_name} your note when they return."
                )
            return True
        if (
            bits
            and bits[0].lower() in {"clear", "reset"}
            and typing.cast(typing.Any, message).author.guild_permissions.manage_messages
        ):
            target = message.mentions[0] if message.mentions else None
            db.afk_clear(scope, str(target.id) if target else None)
            await _safe_send(message.channel, "AFK status cleared.")
            return True
        reason = arg.strip() or "AFK"
        old_nick = getattr(message.author, "nick", None)
        db.afk_set(scope, uid, reason, original_nick=old_nick)
        prefix = str(_cfg(guild, "afk")["settings"].get("nickname_prefix") or "[AFK]")
        try:
            await typing.cast(typing.Any, message).author.edit(
                nick=f"{prefix} {message.author.display_name}"[:32], reason="AFK enabled"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        await _safe_send(message.channel, f"{message.author.mention} is now AFK: {reason}")
        return True
    if name in {"remind", "reminder"} and _cfg(guild, "reminders")["enabled"]:
        bits = arg.split(maxsplit=1)
        seconds = _parse_duration(bits[0]) if bits else None
        if not seconds or len(bits) < 2:
            await _safe_send(message.channel, "Usage: `!remind 30m text` (s/m/h/d/w).")
        else:
            record = db.community_record_create(
                "reminder",
                scope,
                {
                    "text": bits[1][:1800],
                    "channel_id": str(message.channel.id),
                    "jump_url": message.jump_url,
                },
                user_id=uid,
                due=time.time() + min(seconds, 31_536_000),
            )
            await _safe_send(
                message.channel, f"Reminder #{record} set for <t:{int(time.time() + seconds)}:R>."
            )
        return True
    if name == "highlight" and _cfg(guild, "highlights")["enabled"]:
        action, _, phrase = arg.partition(" ")
        if action.lower() == "add" and phrase.strip():
            db.community_record_create(
                "highlight",
                scope,
                {"phrase": phrase.strip()[:100]},
                user_id=uid,
                record_key=phrase.strip().casefold(),
            )
            await _safe_send(message.channel, "Highlight added.")
        elif action.lower() in {"delete", "del", "remove"} and phrase.strip():
            items = db.community_records("highlight", scope, user_id=uid)
            found = next(
                (
                    x
                    for x in items
                    if x["data"].get("phrase", "").casefold() == phrase.strip().casefold()
                ),
                None,
            )
            if found:
                db.community_record_delete(found["id"], guild_id=scope)
            await _safe_send(
                message.channel, "Highlight removed." if found else "Highlight not found."
            )
        else:
            items = db.community_records("highlight", scope, user_id=uid)
            await _safe_send(
                message.channel,
                "Your highlights: "
                + (", ".join(x["data"].get("phrase", "") for x in items) or "none"),
            )
        return True
    if name == "tag" and _cfg(guild, "tags")["enabled"]:
        action, _, rest = arg.partition(" ")
        if action.lower() in {"create", "add", "edit"}:
            tag_name, sep, body = rest.partition(" ")
            if not sep:
                await _safe_send(message.channel, "Usage: `!tag create name content`.")
            else:
                items = db.community_records("tag", scope, status=None, limit=5000)
                old = next((x for x in items if x.get("record_key") == tag_name.casefold()), None)
                if old:
                    db.community_record_update(
                        old["id"], data={"content": body[:1900], "author_id": uid}, status="active"
                    )
                else:
                    db.community_record_create(
                        "tag",
                        scope,
                        {"content": body[:1900], "author_id": uid},
                        user_id=uid,
                        record_key=tag_name.casefold(),
                    )
                await _safe_send(message.channel, "Tag saved.")
        elif action.lower() in {"delete", "del"} and rest:
            item = next(
                (
                    x
                    for x in db.community_records("tag", scope, limit=5000)
                    if x.get("record_key") == rest.casefold()
                ),
                None,
            )
            if item and (
                item["user_id"] == uid
                or typing.cast(typing.Any, message).author.guild_permissions.manage_messages
            ):
                db.community_record_delete(item["id"], guild_id=scope)
            await _safe_send(message.channel, "Tag deleted." if item else "Tag not found.")
        elif action.lower() == "list":
            items = db.community_records("tag", scope, limit=5000)
            await _safe_send(
                message.channel,
                "Tags: " + (", ".join(x.get("record_key") or "" for x in items) or "none"),
            )
        else:
            key = (action or rest).casefold()
            item = next(
                (
                    x
                    for x in db.community_records("tag", scope, limit=5000)
                    if x.get("record_key") == key
                ),
                None,
            )
            await _safe_send(
                message.channel, item["data"].get("content", "") if item else "Tag not found."
            )
        return True
    if name == "pay" and _cfg(guild, "economy")["enabled"] and message.mentions:
        amount_match = re.search(r"\b\d+\b", arg)
        amount = int(amount_match.group()) if amount_match else 0
        target = message.mentions[0]
        if amount <= 0 or target.bot or target.id == message.author.id:
            await _safe_send(message.channel, "Choose another member and a positive amount.")
        else:
            try:
                sender, receiver = db.economy_transfer(uid, str(target.id), amount)
                await _safe_send(
                    message.channel,
                    f"Paid {target.mention} {amount} coins. Balances: {sender} / {receiver}.",
                )
            except ValueError as error:
                await _safe_send(message.channel, str(error))
        return True
    if name in {"coins", "wallet"} and _cfg(guild, "economy")["enabled"]:
        target = message.mentions[0] if message.mentions else message.author
        profile = db.economy_profile(str(target.id))
        await _safe_send(
            message.channel,
            f"{target.display_name}: **{profile['balance']} coins** · **{profile['gems']} gems**.",
        )
        return True
    if name in {"pack", "cards", "fuse", "deck", "battle"} and _cfg(guild, "economy")["enabled"]:
        cards_enabled = bool(_cfg(guild, "economy")["settings"].get("cards_enabled", True))
        if not cards_enabled:
            await _safe_send(message.channel, "Cards and battles are disabled in this server.")
            return True
        owned = db.community_records("card", "global", user_id=uid, limit=5000)
        if name == "pack":
            try:
                balance = db.economy_spend(uid, 100)
            except ValueError as error:
                await _safe_send(message.channel, f"{error} A pack costs 100 coins.")
                return True
            pulled: list[typing.Any] = []
            for _ in range(3):
                base = dict(secrets.choice(_CARDS))
                base["level"] = 1
                card_id = db.community_record_create(
                    "card",
                    "global",
                    base,
                    user_id=uid,
                    record_key=typing.cast(typing.Any, base["name"]).casefold(),
                )
                pulled.append(
                    f"#{card_id} **{base['name']}** ({base['atk']} ATK / {base['def']} DEF)"
                )
            await _safe_send(
                message.channel,
                "Pack opened:\n" + "\n".join(pulled) + f"\nBalance: {balance} coins.",
            )
            return True
        if name == "cards":
            if not owned:
                await _safe_send(
                    message.channel, "No cards yet. Open one with `!pack` (100 coins)."
                )
            else:
                await _safe_send(
                    message.channel,
                    embed=embeds.say(
                        "\n".join(
                            f"#{item['id']} **{item['data'].get('name')}** · L{item['data'].get('level', 1)} · {item['data'].get('atk')} ATK / {item['data'].get('def')} DEF · {item['data'].get('faction')}"
                            for item in owned[:30]
                        ),
                        title=f"{message.author.display_name}'s cards",
                    ),
                )
            return True
        if name == "fuse":
            wanted = arg.strip().casefold()
            matches = [
                item for item in owned if str(item["data"].get("name", "")).casefold() == wanted
            ]
            if len(matches) < 2:
                await _safe_send(message.channel, "You need two copies with that exact card name.")
                return True
            first, second = matches[:2]
            level = (
                max(int(first["data"].get("level") or 1), int(second["data"].get("level") or 1)) + 1
            )
            upgraded = dict(first["data"])
            upgraded.update(
                {
                    "level": level,
                    "atk": int(upgraded.get("atk") or 0) + 3,
                    "def": int(upgraded.get("def") or 0) + 3,
                }
            )
            db.community_record_delete(first["id"])
            db.community_record_delete(second["id"])
            card_id = db.community_record_create(
                "card", "global", upgraded, user_id=uid, record_key=wanted
            )
            await _safe_send(
                message.channel,
                f"Fused into #{card_id} **{upgraded['name']}** level {level} ({upgraded['atk']} ATK / {upgraded['def']} DEF).",
            )
            return True
        deck_rows = db.community_records("deck", "global", user_id=uid, limit=10)
        if name == "deck":
            if arg.lower().startswith("set "):
                ids = [int(value) for value in re.findall(r"\d+", arg[4:])[:5]]
                owned_map = {int(item["id"]): item for item in owned}
                if not ids or any(card_id not in owned_map for card_id in ids):
                    await _safe_send(
                        message.channel,
                        "Use up to five card IDs that you own: `!deck set 12 15 18`.",
                    )
                    return True
                data = {"card_ids": ids}
                if deck_rows:
                    db.community_record_update(deck_rows[0]["id"], data=data)
                else:
                    db.community_record_create("deck", "global", data, user_id=uid, record_key=uid)
                await _safe_send(message.channel, "Battle deck saved.")
            else:
                ids: typing.Any = deck_rows[0]["data"].get("card_ids", []) if deck_rows else []
                by_id = {int(item["id"]): item for item in owned}
                selected: typing.Any = [
                    by_id[int(card_id)] for card_id in ids if int(card_id) in by_id
                ]
                await _safe_send(
                    message.channel,
                    "Your deck:\n"
                    + (
                        "\n".join(f"#{item['id']} {item['data'].get('name')}" for item in selected)
                        or "empty"
                    ),
                )
            return True
        if name == "battle":
            if (
                not message.mentions
                or message.mentions[0].bot
                or message.mentions[0].id == message.author.id
            ):
                await _safe_send(message.channel, "Challenge another member: `!battle @user`.")
                return True
            opponent = message.mentions[0]
            opponent_cards = db.community_records(
                "card", "global", user_id=str(opponent.id), limit=5000
            )
            opponent_decks = db.community_records(
                "deck", "global", user_id=str(opponent.id), limit=10
            )
            own_ids: typing.Any = deck_rows[0]["data"].get("card_ids", []) if deck_rows else []
            other_ids: typing.Any = (
                opponent_decks[0]["data"].get("card_ids", []) if opponent_decks else []
            )
            own_map = {item["id"]: item for item in owned}
            other_map = {item["id"]: item for item in opponent_cards}
            own_deck: typing.Any = [
                own_map[int(card_id)] for card_id in own_ids if int(card_id) in own_map
            ]
            other_deck = [
                other_map[int(card_id)] for card_id in other_ids if int(card_id) in other_map
            ]
            if not own_deck or not other_deck:
                await _safe_send(message.channel, "Both players need a saved non-empty deck.")
                return True
            own_power = sum(
                int(item["data"].get("atk") or 0) + int(item["data"].get("def") or 0)
                for item in own_deck
            ) + secrets.randbelow(21)
            other_power = sum(
                int(item["data"].get("atk") or 0) + int(item["data"].get("def") or 0)
                for item in other_deck
            ) + secrets.randbelow(21)
            if own_power == other_power:
                result = f"Tie at **{own_power}** power."
            else:
                winner = message.author if own_power > other_power else opponent
                db.economy_adjust(str(winner.id), 25)
                result = f"{winner.mention} wins **{own_power}–{other_power}** and earns 25 coins."
            db.community_record_create(
                "battle_history",
                "global",
                {
                    "challenger_id": uid,
                    "opponent_id": str(opponent.id),
                    "challenger_power": own_power,
                    "opponent_power": other_power,
                    "result": result,
                },
                user_id=uid,
            )
            await _safe_send(message.channel, result)
            return True
    if name == "announce" and _cfg(guild, "announcements")["enabled"]:
        if not typing.cast(typing.Any, message).author.guild_permissions.manage_messages:
            await _safe_send(message.channel, "You need Manage Messages.")
        else:
            target = (
                _channel(guild, _cfg(guild, "announcements")["settings"].get("channel_id"))
                or message.channel
            )
            await _safe_send(target, arg, everyone="@everyone" in arg or "@here" in arg)
        return True
    if name == "giveaway" and _cfg(guild, "giveaways")["enabled"]:
        action, _, rest = arg.partition(" ")
        if not typing.cast(typing.Any, message).author.guild_permissions.manage_messages:
            await _safe_send(message.channel, "You need Manage Messages.")
            return True
        if action.lower() == "create":
            parts = [part.strip() for part in rest.split("|")]
            seconds = _parse_duration(parts[0]) if parts else None
            winners = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            prize = parts[2] if len(parts) > 2 else "Mystery prize"
            if not seconds:
                await _safe_send(message.channel, "Usage: `!giveaway create 1h | 2 | Prize`.")
                return True
            end_at = time.time() + min(seconds, 31_536_000)
            post = await _safe_send(
                message.channel,
                embed=embeds.say(
                    f"React with 🎉 to enter.\nEnds <t:{int(end_at)}:R> · {max(1, min(50, winners))} winner(s)",
                    title=prize[:256],
                ),
            )
            if post:
                try:
                    await post.add_reaction("🎉")
                except discord.HTTPException:
                    pass
                db.community_record_create(
                    "giveaway",
                    scope,
                    {
                        "message_id": str(post.id),
                        "channel_id": str(message.channel.id),
                        "prize": prize[:500],
                        "winners": max(1, min(50, winners)),
                    },
                    user_id=uid,
                    record_key=str(post.id),
                    due=end_at,
                )
            return True
        if action.lower() in {"end", "reroll"} and rest.strip().isdigit():
            item = next(
                (
                    x
                    for x in db.community_records("giveaway", scope, status=None, limit=5000)
                    if x.get("record_key") == rest.strip()
                ),
                None,
            )
            if item:
                db.community_record_update(item["id"], status="active", due=time.time())
                await _safe_send(message.channel, "Giveaway queued for drawing.")
            else:
                await _safe_send(message.channel, "Giveaway not found.")
            return True
        await _safe_send(
            message.channel,
            "Use `!giveaway create <duration> | <winners> | <prize>` or `!giveaway end <message_id>`.",
        )
        return True
    if name == "ticket" and _cfg(guild, "tickets")["enabled"]:
        action, _, detail = arg.partition(" ")
        settings = _cfg(guild, "tickets")["settings"]
        existing = [
            x for x in db.community_records("ticket", scope, user_id=uid) if x["status"] == "active"
        ]
        if action.lower() in {"open", "create"}:
            maximum = max(1, min(100, int(settings.get("max_open_per_member") or 5)))
            if len(existing) >= maximum:
                await _safe_send(
                    message.channel, f"You already have {len(existing)} open ticket(s)."
                )
                return True
            category_id = str(settings.get("category_id") or "")
            category = guild.get_channel(int(category_id)) if category_id.isdigit() else None
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                message.author: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
            }
            for role_id in settings.get("staff_role_ids", [])[:20]:
                role = guild.get_role(int(role_id)) if str(role_id).isdigit() else None
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True
                    )
            safe_name = re.sub(r"[^a-z0-9-]", "-", message.author.name.lower())[:40]
            try:
                channel = await guild.create_text_channel(
                    f"ticket-{safe_name}-{secrets.randbelow(10000):04d}",
                    category=category if isinstance(category, discord.CategoryChannel) else None,
                    overwrites=typing.cast(typing.Any, overwrites),
                    reason="Support ticket opened",
                )
            except (discord.Forbidden, discord.HTTPException):
                await _safe_send(message.channel, "I could not create the ticket channel.")
                return True
            record_id = db.community_record_create(
                "ticket",
                scope,
                {"channel_id": str(channel.id), "subject": detail[:500]},
                user_id=uid,
                record_key=str(channel.id),
            )
            mentions = " ".join(
                f"<@&{role_id}>" for role_id in settings.get("staff_role_ids", [])[:20]
            )
            await _safe_send(
                channel,
                f"Ticket #{record_id} opened by {message.author.mention}.\n**Subject:** {detail or 'No subject'}\n{mentions}",
            )
            await _safe_send(message.channel, f"Your ticket is ready: {channel.mention}")
            return True
        if action.lower() in {"close", "resolve"}:
            item = next(
                (
                    x
                    for x in db.community_records("ticket", scope, status=None, limit=5000)
                    if x.get("record_key") == str(message.channel.id)
                ),
                None,
            )
            if not item:
                await _safe_send(message.channel, "This is not a tracked ticket channel.")
                return True
            if (
                item.get("user_id") != uid
                and not typing.cast(typing.Any, message).author.guild_permissions.manage_channels
            ):
                await _safe_send(message.channel, "Only the ticket owner or staff can close it.")
                return True
            lines: list[typing.Any] = []
            try:
                async for entry in message.channel.history(limit=1000, oldest_first=True):
                    lines.append(
                        f"[{entry.created_at.isoformat()}] {entry.author}: {entry.clean_content}"
                    )
            except (discord.Forbidden, discord.HTTPException):
                pass
            transcript_channel = _channel(guild, settings.get("transcript_channel_id"))
            if transcript_channel:
                payload = "\n".join(lines).encode("utf-8")[:2_000_000]
                try:
                    await typing.cast(typing.Any, transcript_channel).send(
                        content=f"Transcript for ticket #{item['id']} ({typing.cast(typing.Any, message).channel.name})",
                        file=discord.File(io.BytesIO(payload), filename=f"ticket-{item['id']}.txt"),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            db.community_record_update(
                item["id"], status="resolved" if action.lower() == "resolve" else "closed"
            )
            await _safe_send(
                message.channel, "Ticket closed. This channel will be deleted in 5 seconds."
            )
            await asyncio.sleep(5)
            try:
                await typing.cast(typing.Any, message).channel.delete(reason="Ticket closed")
            except (discord.Forbidden, discord.HTTPException):
                pass
            return True
        await _safe_send(message.channel, "Use `!ticket open <subject>` or `!ticket close`.")
        return True
    if name == "form" and _cfg(guild, "forms")["enabled"]:
        settings = _cfg(guild, "forms")["settings"]
        slug = arg.strip().lower()
        form: typing.Any = typing.cast(
            typing.Any,
            next(
                (
                    item
                    for item in typing.cast(list[dict[str, typing.Any]], settings.get("forms", []))[
                        :100
                    ]
                    if isinstance(item, dict)
                    and typing.cast(typing.Any, item).get("enabled", True) is not False
                    and str(typing.cast(typing.Any, item).get("slug") or "").lower() == slug
                ),
                None,
            ),
        )
        if not form:
            names = [
                str(typing.cast(typing.Any, item).get("slug"))
                for item in settings.get("forms", [])
                if isinstance(item, dict)
                and typing.cast(typing.Any, item).get("enabled", True) is not False
            ]
            await _safe_send(message.channel, "Forms: " + (", ".join(names) or "none"))
            return True
        token_value = ""
        if form.get("members_only"):
            token_value = secrets.token_urlsafe(24)
            db.community_record_create(
                "form_access",
                scope,
                {"form_slug": slug},
                user_id=uid,
                record_key=token_value,
                due=time.time() + 3600,
            )
        base = str(settings.get("public_base_url") or "https://kozzyx.org").rstrip("/")
        url = f"{base}/forms/{guild.id}/{slug}"
        if token_value:
            url += f"?token={token_value}"
        await _safe_send(
            message.author if form.get("members_only") else message.channel,
            f"Open **{form.get('title') or slug}**: {url}",
        )
        if form.get("members_only"):
            await _safe_send(message.channel, "I sent your private form link by DM.")
        return True
    if name in {"rank", "ranks"} and _cfg(guild, "autoroles")["enabled"]:
        action, _, rank_name = arg.partition(" ")
        ranks: typing.Any = [
            x for x in _cfg(guild, "autoroles")["settings"].get("ranks", []) if isinstance(x, dict)
        ]
        if action.lower() == "list" or not rank_name:
            await _safe_send(
                message.channel,
                "Ranks: " + (", ".join(str(x.get("name") or "") for x in ranks) or "none"),
            )
            return True
        item: typing.Any = next(
            (x for x in ranks if str(x.get("name", "")).casefold() == rank_name.casefold()), None
        )
        role_id = str(item.get("role_id") or "") if item else ""
        role = guild.get_role(int(role_id)) if role_id.isdigit() else None
        if not role:
            await _safe_send(message.channel, "Rank not found.")
        else:
            try:
                if action.lower() in {"join", "add"}:
                    await typing.cast(typing.Any, message).author.add_roles(
                        role, reason="Self-assignable rank"
                    )
                elif action.lower() in {"leave", "remove"}:
                    await typing.cast(typing.Any, message).author.remove_roles(
                        role, reason="Self-assignable rank"
                    )
                await _safe_send(message.channel, "Rank updated.")
            except (discord.Forbidden, discord.HTTPException):
                await _safe_send(message.channel, "I cannot manage that role.")
        return True
    if name in {"coinflip", "flip"} and _cfg(guild, "fun")["enabled"]:
        await _safe_send(message.channel, secrets.choice(["Heads.", "Tails."]))
        return True
    if name == "dice" and _cfg(guild, "fun")["enabled"]:
        sides = max(2, min(1000, int(arg) if arg.isdigit() else 6))
        await _safe_send(
            message.channel, f"You rolled **{secrets.randbelow(sides) + 1}** (d{sides})."
        )
        return True
    if name == "rps" and _cfg(guild, "fun")["enabled"]:
        choice = arg.strip().lower()
        if choice not in {"rock", "paper", "scissors"}:
            await _safe_send(message.channel, "Choose rock, paper, or scissors.")
        else:
            bot_choice = secrets.choice(["rock", "paper", "scissors"])
            wins = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}
            result = (
                "Tie"
                if choice == bot_choice
                else ("You win" if (choice, bot_choice) in wins else "I win")
            )
            await _safe_send(message.channel, f"I chose **{bot_choice}**. {result}.")
        return True
    if (
        name in {"cat", "dog", "pug", "dadjoke", "pokemon", "itunes", "github", "iss"}
        and _cfg(guild, "fun")["enabled"]
    ):
        try:
            if name == "cat":
                text = f"https://cataas.com/cat?cache={secrets.token_hex(4)}"
            elif name in {"dog", "pug"}:
                url = (
                    "https://dog.ceo/api/breeds/image/random"
                    if name == "dog"
                    else "https://dog.ceo/api/breed/pug/images/random"
                )
                data = await _http_get(url)
                text = str(typing.cast(typing.Any, data).get("message") or "No image found.")
            elif name == "dadjoke":
                data = await _http_get(
                    "https://icanhazdadjoke.com/", headers={"Accept": "application/json"}
                )
                text = str(typing.cast(typing.Any, data).get("joke") or "No joke found.")
            elif name == "pokemon":
                term = re.sub(r"[^a-z0-9-]", "", arg.lower())[:50]
                if not term:
                    raise ValueError("Give me a Pokémon name or number.")
                data = await _http_get(f"https://pokeapi.co/api/v2/pokemon/{quote(term)}")
                types: typing.Any = ", ".join(
                    item["type"]["name"]
                    for item in typing.cast(
                        typing.Iterable[typing.Any], typing.cast(typing.Any, data).get("types", [])
                    )
                )
                text = f"**{str(typing.cast(typing.Any, data).get('name', term)).title()}** · #{typing.cast(typing.Any, data).get('id')} · types: {types} · height: {typing.cast(typing.Any, data).get('height')} · weight: {typing.cast(typing.Any, data).get('weight')}"
            elif name == "itunes":
                if not arg.strip():
                    raise ValueError("Give me a song or artist.")
                data = await _http_get(
                    f"https://itunes.apple.com/search?term={quote(arg[:100])}&entity=song&limit=1"
                )
                item: typing.Any = typing.cast(
                    typing.Any, next(iter(typing.cast(typing.Any, data).get("results", [])), None)
                )
                text = (
                    f"**{item['trackName']}** by {item['artistName']} · {item.get('trackViewUrl', '')}"
                    if item
                    else "No song found."
                )
            elif name == "github":
                repo = arg.strip().lower()
                if not re.fullmatch(r"[a-z0-9_.-]{1,39}/[a-z0-9_.-]{1,100}", repo):
                    raise ValueError("Use `owner/repository`.")
                data = await _http_get(f"https://api.github.com/repos/{repo}")
                text = f"**{typing.cast(typing.Any, data).get('full_name', repo)}** · {typing.cast(typing.Any, data).get('stargazers_count', 0)} stars · {typing.cast(typing.Any, data).get('open_issues_count', 0)} open issues\n{typing.cast(typing.Any, data).get('description') or 'No description.'}\n{typing.cast(typing.Any, data).get('html_url', '')}"
            else:
                data = await _http_get("https://api.wheretheiss.at/v1/satellites/25544")
                iss = typing.cast(dict[str, typing.Any], data)
                text = f"ISS position: **{float(iss['latitude']):.3f}, {float(iss['longitude']):.3f}** · altitude {float(iss['altitude']):.1f} km · velocity {float(iss['velocity']):.0f} km/h"
            await _safe_send(message.channel, text)
        except (
            ValueError,
            KeyError,
            TypeError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as error:
            await _safe_send(message.channel, str(error)[:300] or "That lookup failed.")
        return True
    if name == "poll" and _cfg(guild, "fun")["enabled"]:
        parts = [part.strip() for part in arg.split("|") if part.strip()]
        if len(parts) < 3 or len(parts) > 11:
            await _safe_send(
                message.channel, "Usage: `!poll Question | Option 1 | Option 2` (up to 10 options)."
            )
        else:
            marks = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            post = await _safe_send(
                message.channel,
                embed=embeds.say(
                    "\n".join(f"{marks[i]} {option}" for i, option in enumerate(parts[1:])),
                    title=parts[0][:256],
                ),
            )
            if post:
                for mark in marks[: len(parts) - 1]:
                    try:
                        await post.add_reaction(mark)
                    except discord.HTTPException:
                        pass
        return True
    if name == "distance" and _cfg(guild, "fun")["enabled"]:
        try:
            lat1, lon1, lat2, lon2 = [float(value.strip()) for value in arg.split(",")]
            if not (
                -90 <= lat1 <= 90
                and -90 <= lat2 <= 90
                and -180 <= lon1 <= 180
                and -180 <= lon2 <= 180
            ):
                raise ValueError
            phi1, phi2 = math.radians(lat1), math.radians(lat2)
            dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
            hav = (
                math.sin(dphi / 2) ** 2
                + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
            )
            distance = 6371.0088 * 2 * math.atan2(math.sqrt(hav), math.sqrt(1 - hav))
            await _safe_send(
                message.channel,
                f"Great-circle distance: **{distance:,.2f} km** ({distance * 0.621371:,.2f} mi).",
            )
        except (ValueError, TypeError):
            await _safe_send(message.channel, "Usage: `!distance lat1, lon1, lat2, lon2`.")
        return True
    return False


async def _poll_public_feeds(guild: discord.Guild, scope: str, timestamp: float) -> None:
    reddit = _cfg(guild, "reddit")
    if reddit["enabled"]:
        poll_seconds = max(60, min(86_400, int(reddit["settings"].get("poll_minutes") or 5) * 60))
        for index, subscription in enumerate(reddit["settings"].get("subscriptions", [])[:100]):
            if not isinstance(subscription, dict):
                continue
            subreddit = (
                str(typing.cast(typing.Any, subscription).get("subreddit") or "")
                .strip()
                .removeprefix("r/")
            )
            if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", subreddit):
                continue
            key = f"feedpoll:reddit:{scope}:{index}"
            last_poll = float(db.kv_get(key, "0") or 0)
            if timestamp - last_poll < poll_seconds:
                continue
            db.kv_set(key, str(timestamp))
            try:
                payload = await _http_get(f"https://www.reddit.com/r/{subreddit}/new.json?limit=10")
            except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
                continue
            posts: typing.Any = typing.cast(
                typing.Any,
                (
                    typing.cast(typing.Any, payload).get("data", {}).get("children", [])
                    if isinstance(payload, dict)
                    else []
                ),
            )
            seen_key = f"feedseen:reddit:{scope}:{index}"
            seen = str(db.kv_get(seen_key, "") or "")
            fresh: list[typing.Any] = []
            for wrapper in typing.cast(typing.Iterable[typing.Any], posts):
                item: typing.Any = typing.cast(
                    typing.Any,
                    typing.cast(typing.Any, wrapper).get("data", {})
                    if isinstance(wrapper, dict)
                    else {},
                )
                post_id = str(item.get("name") or "")
                if not post_id or post_id == seen:
                    break
                if item.get("over_18") and not typing.cast(typing.Any, subscription).get(
                    "include_nsfw"
                ):
                    continue
                flair = str(typing.cast(typing.Any, subscription).get("flair") or "")
                if flair and str(item.get("link_flair_text") or "").casefold() != flair.casefold():
                    continue
                fresh.append(item)
            channel = _channel(guild, typing.cast(typing.Any, subscription).get("channel_id"))
            for item in reversed(fresh[:5]):
                if channel:
                    link = "https://www.reddit.com" + str(item.get("permalink") or "")
                    await _safe_send(
                        channel,
                        _render(
                            typing.cast(typing.Any, subscription).get("message")
                            or "New post in r/{subreddit}: **{title}**\n{link}",
                            guild=guild,
                            channel=channel,
                            extra={
                                "subreddit": subreddit,
                                "title": item.get("title", ""),
                                "link": link,
                                "author": item.get("author", ""),
                            },
                        ),
                    )
            if posts:
                first: typing.Any = typing.cast(
                    typing.Any, posts[0].get("data", {}) if isinstance(posts[0], dict) else {}
                )
                if first.get("name"):
                    db.kv_set(seen_key, str(first["name"]))
    youtube = _cfg(guild, "youtube")
    if youtube["enabled"]:
        poll_seconds = max(60, min(86_400, int(youtube["settings"].get("poll_minutes") or 5) * 60))
        for index, subscription in enumerate(youtube["settings"].get("subscriptions", [])[:100]):
            if not isinstance(subscription, dict):
                continue
            channel_id = str(
                typing.cast(typing.Any, subscription).get("youtube_channel_id")
                or typing.cast(typing.Any, subscription).get("channel")
                or ""
            )
            if not re.fullmatch(r"UC[A-Za-z0-9_-]{20,30}", channel_id):
                continue
            key = f"feedpoll:youtube:{scope}:{index}"
            last_poll = float(db.kv_get(key, "0") or 0)
            if timestamp - last_poll < poll_seconds:
                continue
            db.kv_set(key, str(timestamp))
            try:
                raw = await _http_get(
                    f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                    json_response=False,
                )
                if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
                    continue
                entries: list[typing.Any] = []
                for block in re.findall(
                    r"<entry\b[^>]*>(.*?)</entry>", raw, re.DOTALL | re.IGNORECASE
                )[:20]:
                    video_match = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", block)
                    title_match = re.search(
                        r"<title>(.*?)</title>", block, re.DOTALL | re.IGNORECASE
                    )
                    if video_match:
                        entries.append(
                            (
                                video_match.group(1).strip(),
                                html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
                                if title_match
                                else "New video",
                            )
                        )
            except (ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                continue
            seen_key = f"feedseen:youtube:{scope}:{index}"
            seen = str(db.kv_get(seen_key, "") or "")
            fresh: list[typing.Any] = []
            for video_id, title in entries:
                if not video_id or video_id == seen:
                    break
                fresh.append((video_id, title))
            channel = _channel(guild, typing.cast(typing.Any, subscription).get("channel_id"))
            for video_id, title in reversed(fresh[:5]):
                if channel:
                    link = f"https://www.youtube.com/watch?v={video_id}"
                    await _safe_send(
                        channel,
                        _render(
                            typing.cast(typing.Any, subscription).get("message")
                            or "**{video.title}**\n{video.link}",
                            guild=guild,
                            channel=channel,
                            extra={
                                "video.title": title,
                                "video.link": link,
                                "channel.link": f"https://www.youtube.com/channel/{channel_id}",
                            },
                        ),
                    )
            if entries:
                latest = entries[0][0]
                if latest:
                    db.kv_set(seen_key, latest)


async def _poll_social_feeds(guild: discord.Guild, scope: str, timestamp: float) -> None:
    twitch = _cfg(guild, "twitch")
    if twitch["enabled"] and bot_config.TWITCH_CLIENT_ID and bot_config.TWITCH_CLIENT_SECRET:
        try:
            token = await _app_token("twitch")
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            token = ""
        if token:
            poll_seconds = max(
                60, min(86_400, int(twitch["settings"].get("poll_minutes") or 5) * 60)
            )
            for index, subscription in enumerate(twitch["settings"].get("subscriptions", [])[:100]):
                if not isinstance(subscription, dict):
                    continue
                login = str(typing.cast(typing.Any, subscription).get("username") or "").lower()
                if not re.fullmatch(r"[a-z0-9_]{3,25}", login):
                    continue
                poll_key = f"feedpoll:twitch:{scope}:{index}"
                if timestamp - float(db.kv_get(poll_key, "0") or 0) < poll_seconds:
                    continue
                db.kv_set(poll_key, str(timestamp))
                try:
                    payload = await _http_get(
                        f"https://api.twitch.tv/helix/streams?user_login={quote(login)}",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Client-Id": bot_config.TWITCH_CLIENT_ID,
                        },
                    )
                except (
                    ValueError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    json.JSONDecodeError,
                ):
                    continue
                streams: typing.Any = typing.cast(
                    typing.Any,
                    typing.cast(typing.Any, payload).get("data", [])
                    if isinstance(payload, dict)
                    else [],
                )
                live: typing.Any = typing.cast(typing.Any, streams[0] if streams else None)
                state_key = f"feedlive:twitch:{scope}:{index}"
                was_live = db.kv_get(state_key, "0") == "1"
                db.kv_set(state_key, "1" if live else "0")
                if live and not was_live:
                    target = _channel(
                        guild, typing.cast(typing.Any, subscription).get("channel_id")
                    )
                    if target:
                        link = f"https://www.twitch.tv/{login}"
                        await _safe_send(
                            target,
                            _render(
                                typing.cast(typing.Any, subscription).get("message")
                                or "**{streamer}** is live: {title}\n{link}",
                                guild=guild,
                                channel=target,
                                extra={
                                    "streamer": live.get("user_name", login),
                                    "title": live.get("title", "Live"),
                                    "game": live.get("game_name", ""),
                                    "viewers": live.get("viewer_count", 0),
                                    "link": link,
                                },
                            ),
                        )
    kick = _cfg(guild, "kick")
    if kick["enabled"] and bot_config.KICK_CLIENT_ID and bot_config.KICK_CLIENT_SECRET:
        try:
            token = await _app_token("kick")
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
            token = ""
        if token:
            poll_seconds = max(60, min(86_400, int(kick["settings"].get("poll_minutes") or 5) * 60))
            for index, subscription in enumerate(kick["settings"].get("subscriptions", [])[:100]):
                if not isinstance(subscription, dict):
                    continue
                broadcaster = str(
                    typing.cast(typing.Any, subscription).get("broadcaster_user_id") or ""
                )
                username = str(typing.cast(typing.Any, subscription).get("username") or "").lower()
                if not broadcaster.isdigit():
                    continue
                poll_key = f"feedpoll:kick:{scope}:{index}"
                if timestamp - float(db.kv_get(poll_key, "0") or 0) < poll_seconds:
                    continue
                db.kv_set(poll_key, str(timestamp))
                try:
                    payload = await _http_get(
                        f"https://api.kick.com/public/v2/livestreams?broadcaster_user_id={broadcaster}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                except (
                    ValueError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    json.JSONDecodeError,
                ):
                    continue
                raw_streams: typing.Any = typing.cast(
                    typing.Any,
                    (
                        typing.cast(typing.Any, payload).get("data", [])
                        if isinstance(payload, dict)
                        else []
                    ),
                )
                streams: typing.Any = (
                    typing.cast(typing.Any, raw_streams).get("data", [])
                    if isinstance(raw_streams, dict)
                    else raw_streams
                )
                live: typing.Any = typing.cast(
                    typing.Any, streams[0] if isinstance(streams, list) and streams else None
                )
                state_key = f"feedlive:kick:{scope}:{index}"
                was_live = db.kv_get(state_key, "0") == "1"
                db.kv_set(state_key, "1" if live else "0")
                if live and not was_live:
                    target = _channel(
                        guild, typing.cast(typing.Any, subscription).get("channel_id")
                    )
                    if target:
                        channel_data: typing.Any = typing.cast(
                            typing.Any,
                            (live.get("channel") if isinstance(live.get("channel"), dict) else {}),
                        )
                        slug = username or str(channel_data.get("slug") or broadcaster)
                        link = f"https://kick.com/{slug}"
                        await _safe_send(
                            target,
                            _render(
                                typing.cast(typing.Any, subscription).get("message")
                                or "**{streamer}** is live: {title}\n{link}",
                                guild=guild,
                                channel=target,
                                extra={
                                    "streamer": slug,
                                    "title": live.get("stream_title")
                                    or live.get("title")
                                    or "Live",
                                    "viewers": live.get("viewer_count", 0),
                                    "link": link,
                                },
                            ),
                        )
    tiktok = _cfg(guild, "tiktok")
    if tiktok["enabled"] and bot_config.TIKTOK_ACCESS_TOKEN:
        poll_seconds = max(60, min(86_400, int(tiktok["settings"].get("poll_minutes") or 5) * 60))
        for index, subscription in enumerate(tiktok["settings"].get("subscriptions", [])[:1]):
            if not isinstance(subscription, dict):
                continue
            poll_key = f"feedpoll:tiktok:{scope}:{index}"
            if timestamp - float(db.kv_get(poll_key, "0") or 0) < poll_seconds:
                continue
            db.kv_set(poll_key, str(timestamp))
            try:
                payload = await _http_post(
                    "https://open.tiktokapis.com/v2/video/list/?fields=id,title,video_description,duration,cover_image_url,embed_link,create_time",
                    payload={"max_count": 10},
                    headers={"Authorization": f"Bearer {bot_config.TIKTOK_ACCESS_TOKEN}"},
                )
            except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
                continue
            videos: typing.Any = typing.cast(
                typing.Any,
                (
                    typing.cast(typing.Any, payload).get("data", {}).get("videos", [])
                    if isinstance(payload, dict)
                    else []
                ),
            )
            seen_key = f"feedseen:tiktok:{scope}:{index}"
            seen = str(db.kv_get(seen_key, "") or "")
            fresh: list[typing.Any] = []
            for video in typing.cast(typing.Iterable[typing.Any], videos):
                video_id = (
                    str(typing.cast(typing.Any, video).get("id") or "")
                    if isinstance(video, dict)
                    else ""
                )
                if not video_id or video_id == seen:
                    break
                fresh.append(video)
            target = _channel(guild, typing.cast(typing.Any, subscription).get("channel_id"))
            for video in reversed(fresh[:5]):
                if target:
                    link = str(
                        video.get("embed_link") or f"https://www.tiktok.com/video/{video.get('id')}"
                    )
                    caption = str(
                        video.get("video_description") or video.get("title") or "New TikTok"
                    )
                    await _safe_send(
                        target,
                        _render(
                            typing.cast(typing.Any, subscription).get("message")
                            or "**{username}** posted: {caption}\n{link}",
                            guild=guild,
                            channel=target,
                            extra={
                                "username": typing.cast(typing.Any, subscription).get(
                                    "username", "TikTok creator"
                                ),
                                "caption": caption,
                                "caption_without_hashtags": re.sub(r"\s*#\w+", "", caption),
                                "link": link,
                                "thumbnail": video.get("cover_image_url", ""),
                            },
                        ),
                    )
            if videos and isinstance(videos[0], dict) and videos[0].get("id"):
                db.kv_set(seen_key, str(videos[0]["id"]))


async def scheduler_tick(client: discord.Client) -> None:
    """Deliver due reminders and dashboard-scheduled messages."""
    timestamp = time.time()
    for guild in list(client.guilds):
        scope = _scope(guild)
        await _poll_public_feeds(guild, scope, timestamp)
        await _poll_social_feeds(guild, scope, timestamp)
        for item in db.community_records(
            "onboarding_followup", scope, due_before=timestamp, limit=500
        ):
            member = (
                guild.get_member(int(item["user_id"]))
                if str(item.get("user_id", "")).isdigit()
                else None
            )
            if member:
                await _safe_send(
                    member, str(item["data"].get("message") or "Need a hand getting started?")
                )
            db.community_record_update(item["id"], status="delivered" if member else "member_left")
        for ticket in db.community_records("ticket", scope, due_before=timestamp, limit=500):
            data = ticket["data"]
            if data.get("sla_alerted"):
                continue
            channel = _channel(guild, data.get("channel_id"))
            if channel:
                await _safe_send(
                    channel,
                    f"Ticket #{ticket['id']} reached its configured first-response SLA. Staff assignment: "
                    f"{('<@' + str(data.get('assigned_to')) + '>') if str(data.get('assigned_to') or '').isdigit() else 'unassigned'}.",
                )
            staffops.record_incident(
                scope,
                source="ticket",
                summary=f"Ticket #{ticket['id']} reached its first-response SLA",
                severity="high",
                subject_id=str(ticket.get("user_id") or "") or None,
                reference=f"channel:{data.get('channel_id', '')}",
                assigned_to=str(data.get("assigned_to") or "") or None,
                metadata={"ticket_id": ticket["id"], "panel_id": str(data.get("panel_id") or "")},
            )
            db.community_record_update(
                ticket["id"], data={**data, "sla_alerted": True}, status="waiting"
            )
        for item in db.community_records("reminder", scope, due_before=timestamp, limit=500):
            user = (
                guild.get_member(int(item["user_id"]))
                if str(item.get("user_id", "")).isdigit()
                else None
            )
            channel = _channel(guild, item["data"].get("channel_id"))
            target = user or channel
            if target:
                jump = item["data"].get("jump_url") or ""
                await _safe_send(
                    target, f"Reminder: {item['data'].get('text', '')}\n{jump}".strip()
                )
            db.community_record_update(item["id"], status="delivered")
        config = _cfg(guild, "auto_message")
        if config["enabled"]:
            for index, item in enumerate(config["settings"].get("messages", [])[:100]):
                if (
                    not isinstance(item, dict)
                    or typing.cast(typing.Any, item).get("enabled", True) is False
                ):
                    continue
                first = float(
                    typing.cast(typing.Any, item).get("first_at")
                    or typing.cast(typing.Any, item).get("next_at")
                    or 0
                )
                if not first or first > timestamp:
                    continue
                interval = max(
                    0, min(604_800, int(typing.cast(typing.Any, item).get("interval_seconds") or 0))
                )
                due = (
                    first
                    if not interval
                    else first + int((timestamp - first) // interval) * interval
                )
                marker = f"automessage:{scope}:{index}:{int(due)}"
                if db.kv_get(marker):
                    continue
                channel = _channel(guild, typing.cast(typing.Any, item).get("channel_id"))
                if channel:
                    await _safe_send(
                        channel, str(typing.cast(typing.Any, item).get("content") or "")
                    )
                db.kv_set(marker, "1")
        purge = _cfg(guild, "auto_purge")
        if purge["enabled"]:
            for index, rule in enumerate(purge["settings"].get("rules", [])[:100]):
                if (
                    not isinstance(rule, dict)
                    or typing.cast(typing.Any, rule).get("enabled", True) is False
                ):
                    continue
                first = float(typing.cast(typing.Any, rule).get("first_at") or 0)
                interval = max(
                    60,
                    min(
                        604_800, int(typing.cast(typing.Any, rule).get("interval_seconds") or 3600)
                    ),
                )
                if not first or first > timestamp:
                    continue
                due = first + int((timestamp - first) // interval) * interval
                marker = f"autopurge:{scope}:{index}:{int(due)}"
                if db.kv_get(marker):
                    continue
                channel = _channel(guild, typing.cast(typing.Any, rule).get("channel_id"))
                if not isinstance(channel, discord.TextChannel):
                    db.kv_set(marker, "invalid-channel")
                    continue
                maximum = max(
                    1,
                    min(
                        5000,
                        int(
                            typing.cast(typing.Any, rule).get("maximum")
                            or purge["settings"].get("maximum_per_run")
                            or 100
                        ),
                    ),
                )
                filters: typing.Any = typing.cast(
                    typing.Any,
                    [
                        x
                        for x in typing.cast(
                            typing.Iterable[typing.Any],
                            typing.cast(typing.Any, rule).get("filters", []),
                        )
                        if isinstance(x, dict)
                    ],
                )
                matched: list[typing.Any] = []
                try:
                    async for entry in channel.history(limit=maximum):
                        if entry.pinned:
                            continue
                        checks: typing.Any = [_filter_matches(entry, item) for item in filters]
                        if not filters or (
                            all(checks)
                            if typing.cast(typing.Any, rule).get("match", "all") == "all"
                            else any(checks)
                        ):
                            matched.append(entry)
                    for start in range(0, len(matched), 100):
                        batch = matched[start : start + 100]
                        try:
                            await channel.delete_messages(batch, reason="Scheduled auto purge")
                        except (discord.Forbidden, discord.HTTPException):
                            for entry in batch:
                                try:
                                    await entry.delete()
                                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                                    pass
                finally:
                    db.kv_set(marker, str(len(matched)))
        for giveaway in db.community_records("giveaway", scope, due_before=timestamp, limit=100):
            data = giveaway["data"]
            channel = _channel(guild, data.get("channel_id"))
            entrants: list[typing.Any] = []
            if channel:
                try:
                    post: typing.Any = await typing.cast(typing.Any, channel).fetch_message(
                        int(data.get("message_id"))
                    )
                    reaction: typing.Any = typing.cast(
                        typing.Any,
                        next(
                            (
                                item
                                for item in typing.cast(typing.Iterable[typing.Any], post.reactions)
                                if str(item.emoji) == "🎉"
                            ),
                            None,
                        ),
                    )
                    if reaction:
                        async for user in typing.cast(
                            typing.AsyncIterable[typing.Any], reaction.users(limit=5000)
                        ):
                            if not typing.cast(typing.Any, user).bot:
                                entrants.append(user)
                except (ValueError, discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
            winner_count = min(len(entrants), max(1, min(50, int(data.get("winners") or 1))))
            winners = secrets.SystemRandom().sample(entrants, winner_count) if winner_count else []
            if channel:
                await _safe_send(
                    channel,
                    f"Giveaway **{data.get('prize', 'Prize')}** ended. "
                    + (
                        "Winners: " + ", ".join(user.mention for user in winners)
                        if winners
                        else "No eligible entries."
                    ),
                )
            for winner in winners:
                await _safe_send(
                    winner, f"You won **{data.get('prize', 'a giveaway')}** in **{guild.name}**."
                )
            db.community_record_update(
                giveaway["id"],
                data={**data, "winner_ids": [str(user.id) for user in winners]},
                status="ended",
            )
        for submission in db.community_records("form_submission", scope, limit=100):
            data = submission["data"]
            target = _channel(guild, data.get("channel_id"))
            if target is None:
                continue
            author_text = (
                "Anonymous"
                if data.get("anonymous")
                else (f"<@{submission['user_id']}>" if submission.get("user_id") else "Web visitor")
            )
            lines = [f"**Submitted by:** {author_text}"]
            for answer in data.get("answers", [])[:50]:
                if not isinstance(answer, dict):
                    continue
                values = (
                    ", ".join(
                        str(value)
                        for value in typing.cast(
                            typing.Iterable[typing.Any],
                            typing.cast(typing.Any, answer).get("values", []),
                        )
                    )
                    or "(no answer)"
                )
                lines.append(
                    f"**{typing.cast(typing.Any, answer).get('label', 'Question')}**\n{values[:1000]}"
                )
            post = await _safe_send(
                target,
                content=" ".join(
                    f"<@&{role_id}>" for role_id in data.get("ping_role_ids", [])[:20]
                ),
                embed=embeds.say(
                    "\n\n".join(lines)[:3900],
                    title=str(data.get("form_title") or "Form submission")[:256],
                ),
            )
            if post:
                for reaction in data.get("reactions", [])[:10]:
                    try:
                        await post.add_reaction(str(reaction))
                    except discord.HTTPException:
                        pass
                if data.get("create_thread"):
                    try:
                        await post.create_thread(
                            name=f"Submission {submission['id']}", auto_archive_duration=1440
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            member = (
                guild.get_member(int(submission["user_id"]))
                if str(submission.get("user_id", "")).isdigit()
                else None
            )
            if member:
                add_roles = [
                    guild.get_role(int(role_id))
                    for role_id in data.get("add_role_ids", [])
                    if str(role_id).isdigit()
                ]
                remove_roles = [
                    guild.get_role(int(role_id))
                    for role_id in data.get("remove_role_ids", [])
                    if str(role_id).isdigit()
                ]
                try:
                    if any(add_roles):
                        await member.add_roles(
                            *(role for role in add_roles if role), reason="Form automation"
                        )
                    if any(remove_roles):
                        await member.remove_roles(
                            *(role for role in remove_roles if role), reason="Form automation"
                        )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            db.community_record_update(submission["id"], status="posted")
