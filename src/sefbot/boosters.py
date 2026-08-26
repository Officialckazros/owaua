"""Dashboard-configured server booster tracking and perks.

Discord exposes new boost system messages and the all-or-nothing Server Booster
role transition.  It does not expose a reliable event when one of several boosts
is removed, so managers can correct the durable count with ``booster adjust``.
"""

from __future__ import annotations

import json
import random
import re
import time
from urllib.parse import urlsplit

import discord

from sefbot import db
from sefbot.scope import Scope

BOOST_MESSAGE_TYPES = {
    discord.MessageType.premium_guild_subscription,
    discord.MessageType.premium_guild_tier_1,
    discord.MessageType.premium_guild_tier_2,
    discord.MessageType.premium_guild_tier_3,
}
STAT_METRICS = {"current_boosts", "all_time_boosts", "current_boosters", "all_time_boosters"}
_PERSONAL_ROLE = "uf:{user_id}:boosterrole:{guild_id}"
_ROLE_CLAIMED = "uf:{user_id}:boosterroleclaimed:{guild_id}"
_PERSONAL_CHANNEL = "uf:{user_id}:boosterchannel:{guild_id}:{kind}"
_MENTION_EMOJI = "uf:{user_id}:boosteremoji:{guild_id}"
_GIFTS = "uf:{user_id}:boostergifts:{guild_id}"
_last_scheduler = 0.0


def _scope(guild: discord.Guild | int | str) -> str:
    raw = getattr(guild, "id", guild)
    return Scope.guild(int(str(raw).removeprefix("guild:"))).key


def config_for(guild: discord.Guild | int | str) -> dict:
    record = db.module_config(_scope(guild), "boosters")
    return record["settings"] if record["enabled"] else {**record["settings"], "tracking_enabled": False}


def _ids(values) -> set[int]:
    output = set()
    for value in values if isinstance(values, list) else []:
        try:
            output.add(int(value))
        except (TypeError, ValueError):
            continue
    return output


def is_manager(member: discord.Member, settings: dict | None = None) -> bool:
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    options = settings or config_for(member.guild)
    return bool({role.id for role in member.roles} & _ids(options.get("manager_role_ids")))


def member_record(member: discord.Member) -> dict:
    return db.booster_member(_scope(member.guild), str(member.id))


def is_eligible(member: discord.Member, settings: dict | None = None) -> bool:
    options = settings or config_for(member.guild)
    return bool(
        member.premium_since
        or ({role.id for role in member.roles} & _ids(options.get("qualifying_role_ids")))
    )


def _boost_count(member: discord.Member) -> int:
    record = member_record(member)
    return max(1 if member.premium_since else 0, int(record["current_boosts"]))


def stats(guild: discord.Guild) -> dict[str, int]:
    result = db.booster_stats(_scope(guild))
    # Discord's native count can repair a lower local current total after downtime.
    result["current_boosts"] = max(result["current_boosts"], int(guild.premium_subscription_count or 0))
    return result


def stats_text(guild: discord.Guild, member: discord.Member | None = None) -> str:
    values = stats(guild)
    lines = [
        f"server boost level: **{guild.premium_tier}**",
        f"current boosts: **{values['current_boosts']}**",
        f"all-time boosts: **{values['all_time_boosts']}**",
        f"current boosters: **{values['current_boosters']}**",
        f"all-time boosters: **{values['all_time_boosters']}**",
    ]
    if member is not None:
        record = member_record(member)
        lines.extend((
            f"{member.mention} recorded current boosts: **{record['current_boosts']}**",
            f"{member.mention} recorded all-time boosts: **{record['all_time_boosts']}**",
        ))
    return "\n".join(lines)


def _safe_url(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    return text if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username else None


def _render(template: object, member: discord.Member, record: dict) -> str:
    values = stats(member.guild)
    replacements = {
        "{user}": member.mention,
        "{username}": member.display_name,
        "{userboosts}": str(record["current_boosts"]),
        "{level}": str(member.guild.premium_tier),
        "{count}": str(values["current_boosts"]),
        "{totalcount}": str(values["all_time_boosts"]),
    }
    text = str(template or "")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _greeting_embed(member: discord.Member, record: dict, settings: dict) -> discord.Embed:
    messages = [str(value) for value in settings.get("greet_messages", []) if str(value).strip()]
    description = _render(random.SystemRandom().choice(messages), member, record) if messages else ""
    embed = discord.Embed(
        title=_render(settings.get("greet_title"), member, record)[:256] or None,
        description=description[:4096] or None,
        colour=_parse_colour(settings.get("greet_color")) or discord.Colour.blurple(),
    )
    author = _render(settings.get("greet_author"), member, record)[:256]
    if author:
        author_kwargs = {"name": author}
        if author_icon := _safe_url(settings.get("greet_author_icon")):
            author_kwargs["icon_url"] = author_icon
        embed.set_author(**author_kwargs)
    footer = _render(settings.get("greet_footer"), member, record)[:2048]
    if footer:
        footer_kwargs = {"text": footer}
        if footer_icon := _safe_url(settings.get("greet_footer_icon")):
            footer_kwargs["icon_url"] = footer_icon
        embed.set_footer(**footer_kwargs)
    thumbnail = _safe_url(settings.get("greet_thumbnail"))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    images = [url for value in settings.get("greet_images", []) if (url := _safe_url(value))]
    fixed_image = _safe_url(settings.get("greet_image"))
    if fixed_image:
        images.append(fixed_image)
    if images:
        embed.set_image(url=random.SystemRandom().choice(images))
    if settings.get("greet_include_stats"):
        values = stats(member.guild)
        embed.add_field(name="server boosts", value=str(values["current_boosts"]), inline=True)
        embed.add_field(name="boost level", value=str(member.guild.premium_tier), inline=True)
        embed.add_field(name="all-time boosts", value=str(values["all_time_boosts"]), inline=True)
    return embed


async def _log(guild: discord.Guild, event: str, title: str, description: str) -> None:
    settings = config_for(guild)
    if event not in {str(value) for value in settings.get("log_events", [])}:
        return
    raw_routes = settings.get("log_routes")
    routes: dict = raw_routes if isinstance(raw_routes, dict) else {}
    raw_id = routes.get(event) or settings.get("log_channel_id")
    channel = guild.get_channel(int(str(raw_id))) if str(raw_id).isdigit() else None
    if isinstance(channel, discord.abc.Messageable):
        try:
            await channel.send(embed=discord.Embed(
                title=title[:256], description=description[:4096],
                colour=_parse_colour(settings.get("log_color")) or discord.Colour.blurple(),
            ))
        except discord.HTTPException:
            pass


async def _send_greeting(
    member: discord.Member, record: dict, settings: dict, *, original: discord.Message | None = None
) -> discord.Message | None:
    if not settings.get("greetings_enabled"):
        return None
    addon = _render(settings.get("greet_addon"), member, record)[:2000]
    embed = _greeting_embed(member, record, settings)
    message_text = embed.description or "Thank you for boosting!"
    sent = None
    channel_id = str(settings.get("greet_channel_id") or "")
    channel = member.guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    kwargs = {
        "content": addon or None,
        "allowed_mentions": discord.AllowedMentions(users=True, roles=True, everyone=False),
    }
    if settings.get("greet_embed"):
        kwargs["embed"] = embed
    else:
        kwargs["content"] = "\n".join(value for value in (message_text, addon) if value)[:2000]
    if isinstance(channel, discord.abc.Messageable):
        try:
            sent = await channel.send(**kwargs)
        except discord.HTTPException:
            pass
    if settings.get("greet_dm"):
        try:
            await member.send(**kwargs)
        except discord.HTTPException:
            pass
    reaction = str(settings.get("greet_reaction") or "").strip()
    if reaction and original is not None and settings.get("react_original"):
        try:
            await original.add_reaction(reaction)
        except discord.HTTPException:
            pass
    if reaction and sent is not None and settings.get("react_custom"):
        try:
            await sent.add_reaction(reaction)
        except discord.HTTPException:
            pass
    return sent


async def _sync_automatic_role(member: discord.Member, active: bool, settings: dict) -> None:
    add_id = str(settings.get("automatic_role_id") or "")
    remove_id = str(settings.get("stop_remove_role_id") or "")
    try:
        if active and settings.get("automatic_role_enabled") and add_id.isdigit():
            role = member.guild.get_role(int(add_id))
            if role and role not in member.roles:
                await member.add_roles(role, reason="booster perk")
        elif not active:
            for raw_id in {add_id, remove_id}:
                role = member.guild.get_role(int(raw_id)) if raw_id.isdigit() else None
                if role and role in member.roles:
                    await member.remove_roles(role, reason="boost ended")
    except discord.HTTPException:
        pass


def _reward_specs(settings: dict, key: str, threshold_key: str) -> list[tuple[int, int]]:
    result = []
    for item in settings.get(key, []):
        if not isinstance(item, dict):
            continue
        try:
            threshold = int(str(item.get(threshold_key)))
            role_id = int(str(item.get("role_id")))
        except (TypeError, ValueError):
            continue
        if threshold >= 0:
            result.append((threshold, role_id))
    return result[:500]


async def sync_rewards(member: discord.Member, settings: dict | None = None) -> None:
    options = settings or config_for(member.guild)
    record = member_record(member)
    age = max(0, time.time() - float(record.get("first_boosted") or time.time())) if record["active"] else 0
    desired = {
        role_id for threshold, role_id in _reward_specs(options, "boost_level_roles", "boosts")
        if record["current_boosts"] >= threshold and record["active"]
    }
    desired |= {
        role_id for threshold, role_id in _reward_specs(options, "boost_age_roles", "seconds")
        if age >= threshold and record["active"]
    }
    managed = {role_id for _, role_id in _reward_specs(options, "boost_level_roles", "boosts")}
    managed |= {role_id for _, role_id in _reward_specs(options, "boost_age_roles", "seconds")}
    try:
        for role_id in desired:
            role = member.guild.get_role(role_id)
            if role and role not in member.roles:
                await member.add_roles(role, reason="booster reward threshold")
        for role_id in managed - desired:
            role = member.guild.get_role(role_id)
            if role and role in member.roles:
                await member.remove_roles(role, reason="booster reward no longer eligible")
    except discord.HTTPException:
        pass


def _role_key(member: discord.Member) -> str:
    return _PERSONAL_ROLE.format(user_id=member.id, guild_id=member.guild.id)


def role_claimed_at(member: discord.Member) -> float | None:
    raw = db.kv_get(_ROLE_CLAIMED.format(user_id=member.id, guild_id=member.guild.id))
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


async def _personal_role(member: discord.Member, *, create: bool) -> discord.Role | None:
    role_id = db.kv_get(_role_key(member)) or db.kv_get(f"boosterrole:{member.guild.id}:{member.id}")
    if str(role_id).isdigit():
        role = member.guild.get_role(int(role_id))
        if role:
            return role
    if not create or not member.guild.me or not member.guild.me.guild_permissions.manage_roles:
        return None
    try:
        role = await member.guild.create_role(name=f"{member.display_name}'s role", reason="personal booster role")
        base_id = str(config_for(member.guild).get("personal_role_base_role_id") or "")
        base = member.guild.get_role(int(base_id)) if base_id.isdigit() else None
        if base and role < member.guild.me.top_role:
            await role.edit(position=max(1, base.position + 1), reason="personal booster role position")
        db.kv_set(_role_key(member), role.id)
        db.kv_set(f"boosterrole:{member.guild.id}:{member.id}", role.id)
        db.kv_set(_ROLE_CLAIMED.format(user_id=member.id, guild_id=member.guild.id), time.time())
        return role
    except discord.HTTPException:
        return None


def _parse_colour(raw: object) -> discord.Colour | None:
    value = str(raw or "").strip().lstrip("#")
    try:
        return discord.Colour(int(value, 16)) if re.fullmatch(r"[0-9a-fA-F]{6}", value) else None
    except ValueError:
        return None


async def set_personal_role(
    member: discord.Member, colour: str, name: str | None = None, *, hoist: bool | None = None,
    icon: bytes | None = None,
) -> tuple[bool, str]:
    settings = config_for(member.guild)
    if not settings.get("personal_roles_enabled"):
        return False, "personal booster roles are disabled."
    if not is_eligible(member, settings) or _boost_count(member) < int(settings.get("personal_role_min_boosts") or 1):
        return False, "you do not meet the configured personal-role requirement."
    parsed = _parse_colour(colour)
    if parsed is None:
        return False, "give me a six-digit hex color such as `#8a2be2`."
    allowed = {str(value).strip().lower().lstrip("#") for value in settings.get("personal_role_allowed_colors", [])}
    if allowed and f"{parsed.value:06x}" not in allowed:
        return False, "that color is not on this server's approved color list."
    role = await _personal_role(member, create=True)
    if role is None:
        return False, "I need Manage Roles and a role above the generated role."
    clean = (name or role.name).strip()[:80]
    folded = clean.casefold()
    if any(str(word).casefold() in folded for word in settings.get("personal_role_banned_words", []) if str(word)):
        return False, "that role name contains a banned word."
    final_name = f"{settings.get('personal_role_prefix', '')}{clean}{settings.get('personal_role_suffix', '')}"[:100]
    kwargs = {"name": final_name, "colour": parsed, "reason": "personal booster role update"}
    if hoist is not None:
        if hoist and not settings.get("personal_role_allow_hoist"):
            return False, "booster-controlled role hoisting is disabled."
        kwargs["hoist"] = bool(hoist)
    if icon is not None:
        kwargs["display_icon"] = icon
    try:
        await role.edit(**kwargs)
        if role not in member.roles:
            await member.add_roles(role, reason="personal booster role owner")
    except (discord.Forbidden, discord.HTTPException, TypeError):
        return False, "Discord rejected the role update; check role hierarchy and role-icon support."
    await _log(member.guild, "role", "Personal booster role updated", f"{member.mention}: {role.mention}")
    return True, f"your personal role is now **{final_name}**."


async def delete_personal_role(member: discord.Member) -> tuple[bool, str]:
    role = await _personal_role(member, create=False)
    db.kv_set(_role_key(member), "")
    db.kv_set(f"boosterrole:{member.guild.id}:{member.id}", "")
    if role:
        try:
            await role.delete(reason="personal booster role deleted")
        except discord.HTTPException:
            return False, "Discord would not let me delete that role."
    return True, "your personal role was deleted; you can claim it again while eligible."


def _gift_ids(member: discord.Member) -> list[int]:
    try:
        value = json.loads(db.kv_get(_GIFTS.format(user_id=member.id, guild_id=member.guild.id), "[]"))
    except (TypeError, json.JSONDecodeError):
        value = []
    return list(dict.fromkeys(int(item) for item in value if str(item).isdigit()))[:100]


async def gift_role(owner: discord.Member, target: discord.Member, *, remove: bool = False) -> tuple[bool, str]:
    settings = config_for(owner.guild)
    role = await _personal_role(owner, create=False)
    if not settings.get("role_gifts_enabled") or role is None:
        return False, "role gifting is unavailable or you have no personal role."
    if _boost_count(owner) < int(settings.get("role_gift_min_boosts") or 1):
        return False, "you do not meet the gift boost requirement."
    gifts = _gift_ids(owner)
    if not remove and target.id not in gifts and len(gifts) >= max(0, int(settings.get("role_gift_slots") or 0)):
        return False, "you have no gift slots remaining."
    try:
        if remove:
            await target.remove_roles(role, reason="personal role gift removed")
            gifts = [item for item in gifts if item != target.id]
        else:
            await target.add_roles(role, reason="personal role gift")
            gifts.append(target.id)
    except discord.HTTPException:
        return False, "Discord rejected the role gift."
    db.kv_set(_GIFTS.format(user_id=owner.id, guild_id=owner.guild.id), json.dumps(list(dict.fromkeys(gifts))))
    return True, f"gift {'removed from' if remove else 'given to'} {target.mention}."


async def return_gift(member: discord.Member) -> tuple[bool, str]:
    removed = 0
    for owner in member.guild.members:
        gifts = _gift_ids(owner)
        if member.id not in gifts:
            continue
        role = await _personal_role(owner, create=False)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason="gifted personal role returned")
            except discord.HTTPException:
                continue
        gifts = [user_id for user_id in gifts if user_id != member.id]
        db.kv_set(_GIFTS.format(user_id=owner.id, guild_id=owner.guild.id), json.dumps(gifts))
        removed += 1
    return (removed > 0, f"returned **{removed}** gifted role(s)." if removed else "you have no gifted personal roles.")


async def claim_private_channel(member: discord.Member, kind: str = "text") -> tuple[bool, str]:
    settings = config_for(member.guild)
    allowed_kind = str(settings.get("private_channel_type") or "text")
    if not settings.get("private_channels_enabled") or kind not in {"text", "voice"}:
        return False, "private booster channels are disabled."
    if allowed_kind not in {kind, "both"}:
        return False, f"this server only allows **{allowed_kind}** booster channels."
    if not is_eligible(member, settings) or _boost_count(member) < int(settings.get("private_channel_min_boosts") or 1):
        return False, "you do not meet the configured private-channel requirement."
    key = _PERSONAL_CHANNEL.format(user_id=member.id, guild_id=member.guild.id, kind=kind)
    old_id = db.kv_get(key)
    old = member.guild.get_channel(int(old_id)) if str(old_id).isdigit() else None
    if old:
        return True, f"your private {kind} channel is {old.mention}."
    me = member.guild.me
    if me is None or not me.guild_permissions.manage_channels:
        return False, "I need Manage Channels to create that."
    overwrites = {
        member.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True, manage_channels=True),
    }
    for role_id in _ids(settings.get("private_channel_allow_role_ids")):
        if role := member.guild.get_role(role_id):
            overwrites[role] = discord.PermissionOverwrite(view_channel=True)
    for role_id in _ids(settings.get("private_channel_deny_role_ids")):
        if role := member.guild.get_role(role_id):
            overwrites[role] = discord.PermissionOverwrite(view_channel=False)
    if settings.get("private_channel_manager_access"):
        for role_id in _ids(settings.get("manager_role_ids")):
            if role := member.guild.get_role(role_id):
                overwrites[role] = discord.PermissionOverwrite(view_channel=True)
        for role in member.guild.roles:
            if role.permissions.manage_guild or role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True)
    category_id = str(settings.get("private_channel_category_id") or "")
    category = member.guild.get_channel(int(category_id)) if category_id.isdigit() else None
    try:
        name = re.sub(r"[^a-z0-9-]", "-", member.display_name.lower())[:70].strip("-") or str(member.id)
        if kind == "voice":
            channel = await member.guild.create_voice_channel(
                f"boost-{name}",
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
            )
        else:
            channel = await member.guild.create_text_channel(
                f"boost-{name}",
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
            )
    except discord.HTTPException:
        return False, "Discord rejected the private-channel creation."
    db.kv_set(key, channel.id)
    await _log(member.guild, "channel", "Private booster channel created", f"{member.mention}: {channel.mention}")
    return True, f"created {channel.mention}."


async def delete_private_channels(member: discord.Member) -> None:
    for kind in ("text", "voice"):
        key = _PERSONAL_CHANNEL.format(user_id=member.id, guild_id=member.guild.id, kind=kind)
        channel_id = db.kv_get(key)
        channel = member.guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        db.kv_set(key, "")
        if channel:
            try:
                await channel.delete(reason="booster eligibility ended")
            except discord.HTTPException:
                pass


async def update_private_channel(
    owner: discord.Member, action: str, *, target: discord.Member | None = None, name: str = ""
) -> tuple[bool, str]:
    """Rename or change one friend's access across an owner's personal channels."""
    settings = config_for(owner.guild)
    channels = []
    for kind in ("text", "voice"):
        key = _PERSONAL_CHANNEL.format(user_id=owner.id, guild_id=owner.guild.id, kind=kind)
        raw = db.kv_get(key)
        channel = owner.guild.get_channel(int(raw)) if str(raw).isdigit() else None
        if channel:
            channels.append(channel)
    if not channels:
        return False, "you do not have a personal booster channel."
    try:
        if action == "rename":
            clean = re.sub(r"[^a-zA-Z0-9 _-]", "", name).strip()[:90]
            if not clean:
                return False, "give the channel a non-empty name."
            for channel in channels:
                await channel.edit(name=clean, reason="booster channel owner rename")
            return True, f"renamed {len(channels)} personal channel(s)."
        if target is None:
            return False, "mention the friend to update."
        if _boost_count(owner) < int(settings.get("private_channel_invite_min_boosts") or 1):
            return False, "you do not meet the channel-invite boost requirement."
        permitted = []
        if action == "invite":
            for channel in channels:
                for overwrite_target, overwrite in channel.overwrites.items():
                    if isinstance(overwrite_target, discord.Member) and overwrite_target.id not in {
                        owner.id, owner.guild.me.id if owner.guild.me else 0
                    } and overwrite.view_channel:
                        permitted.append(overwrite_target.id)
            maximum = max(0, int(settings.get("private_channel_friend_slots") or 0))
            if target.id not in permitted and len(set(permitted)) >= maximum:
                return False, "you have no private-channel friend slots remaining."
        for channel in channels:
            await channel.set_permissions(
                target,
                overwrite=(discord.PermissionOverwrite(view_channel=True, send_messages=True, connect=True)
                           if action == "invite" else None),
                reason="booster channel friend access",
            )
        return True, f"{target.mention} was {'invited' if action == 'invite' else 'removed'}."
    except discord.HTTPException:
        return False, "Discord rejected the channel update."


async def test_greeting(member: discord.Member) -> discord.Message | None:
    settings = {**config_for(member.guild), "greetings_enabled": True}
    return await _send_greeting(member, member_record(member), settings)


def set_mention_emoji(member: discord.Member, emoji: str | None) -> tuple[bool, str]:
    settings = config_for(member.guild)
    if not settings.get("mention_reactions_enabled"):
        return False, "mention auto-reactions are disabled."
    if _boost_count(member) < int(settings.get("mention_reaction_min_boosts") or 1):
        return False, "you do not meet the reaction boost requirement."
    value = str(emoji or "").strip()[:100]
    db.kv_set(_MENTION_EMOJI.format(user_id=member.id, guild_id=member.guild.id), value)
    return True, "mention reaction removed." if not value else f"mention reaction set to {value}."


async def manager_adjust(member: discord.Member, delta: int) -> dict:
    record = db.booster_adjust(_scope(member.guild), str(member.id), delta)
    settings = config_for(member.guild)
    await _sync_automatic_role(member, record["active"], settings)
    await sync_rewards(member, settings)
    if not record["active"] and settings.get("delete_ineligible_personal_role") and not is_eligible(member, settings):
        await delete_personal_role(member)
        await delete_private_channels(member)
    await update_stat_channels(member.guild, settings)
    await _log(
        member.guild, "boost_add" if delta > 0 else "boost_remove", "Boost count corrected",
        f"{member.mention}: {delta:+d}; current={record['current_boosts']}, all-time={record['all_time_boosts']}",
    )
    return record


async def bulk_rank(
    guild: discord.Guild, role: discord.Role, group: str, *, count: int | None = None,
    remove: bool = False,
) -> tuple[int, int]:
    """Apply one role to a recorded booster cohort; return successes and failures."""
    records = db.booster_members(_scope(guild), active=True if group == "current" else None, limit=10_000)
    if group == "alltime":
        records = [record for record in records if record["all_time_boosts"] > 0]
    elif group == "count":
        records = [record for record in records if record["current_boosts"] == int(count or 0)]
    successes = failures = 0
    for record in records:
        member = guild.get_member(int(record["user_id"]))
        if member is None:
            failures += 1
            continue
        try:
            if remove:
                await member.remove_roles(role, reason="booster rank bulk operation")
            else:
                await member.add_roles(role, reason="booster rank bulk operation")
            successes += 1
        except discord.HTTPException:
            failures += 1
    return successes, failures


async def handle_system_message(message: discord.Message) -> bool:
    if message.guild is None or message.type not in BOOST_MESSAGE_TYPES:
        return False
    settings = config_for(message.guild)
    if not settings.get("tracking_enabled") or not isinstance(message.author, discord.Member):
        return True
    record, changed = db.booster_record_event(_scope(message.guild), str(message.author.id), str(message.id))
    if changed:
        await _sync_automatic_role(message.author, True, settings)
        await sync_rewards(message.author, settings)
        await _send_greeting(message.author, record, settings, original=message)
        await update_stat_channels(message.guild, settings)
        await _log(message.guild, "boost_add", "Boost added", f"{message.author.mention} now has {record['current_boosts']} recorded boost(s).")
    return True


async def handle_member_update(before: discord.Member, after: discord.Member) -> str | None:
    settings = config_for(after.guild)
    if not settings.get("tracking_enabled"):
        return None
    if before.premium_since is None and after.premium_since is not None:
        stamp = after.premium_since.timestamp()
        record, started = db.booster_record_sync(_scope(after.guild), str(after.id), boosted_since=stamp, source="member")
        await _sync_automatic_role(after, True, settings)
        await sync_rewards(after, settings)
        if started:
            await _send_greeting(after, record, settings)
            await _log(after.guild, "boost_add", "Booster detected", f"{after.mention} started boosting.")
        await update_stat_channels(after.guild, settings)
        return f"recorded {after.display_name} as a booster"
    if before.premium_since is not None and after.premium_since is None:
        _, stopped = db.booster_record_stop(_scope(after.guild), str(after.id))
        await _sync_automatic_role(after, False, settings)
        await sync_rewards(after, settings)
        if settings.get("delete_ineligible_personal_role") and not is_eligible(after, settings):
            await delete_personal_role(after)
            await delete_private_channels(after)
        if stopped:
            await _log(after.guild, "boost_remove", "Boosting ended", f"{after.mention} removed all detectable boosts.")
        await update_stat_channels(after.guild, settings)
        return f"recorded {after.display_name} as no longer boosting"
    # Role-based personal-role eligibility can also change independently of Nitro.
    lost_revoke_role = bool(
        ({role.id for role in before.roles} - {role.id for role in after.roles})
        & _ids(settings.get("revoke_role_ids"))
    )
    if settings.get("delete_ineligible_personal_role") and (
        not is_eligible(after, settings) or (lost_revoke_role and not after.premium_since)
    ):
        await delete_personal_role(after)
        await delete_private_channels(after)
    return None


async def handle_member_remove(member: discord.Member) -> None:
    settings = config_for(member.guild)
    if not settings.get("tracking_enabled"):
        return
    _, stopped = db.booster_record_stop(_scope(member.guild), str(member.id))
    if settings.get("delete_ineligible_personal_role"):
        await delete_personal_role(member)
        await delete_private_channels(member)
    if stopped:
        await _log(member.guild, "boost_remove", "Booster left server", f"{member} (`{member.id}`) left the server.")
        await update_stat_channels(member.guild, settings)


async def sync_guild(guild: discord.Guild) -> int:
    settings = config_for(guild)
    if not settings.get("tracking_enabled"):
        return 0
    imported = 0
    for member in list(guild.members):
        if member.premium_since is not None:
            _, started = db.booster_record_sync(
                _scope(guild), str(member.id), boosted_since=member.premium_since.timestamp(), source="import"
            )
            imported += int(started)
            await _sync_automatic_role(member, True, settings)
            await sync_rewards(member, settings)
    native_boosters = {member.id for member in guild.members if member.premium_since is not None}
    for record in db.booster_members(_scope(guild), active=True, limit=10_000):
        if int(record["user_id"]) in native_boosters:
            continue
        member = guild.get_member(int(record["user_id"]))
        db.booster_record_stop(_scope(guild), record["user_id"])
        if member:
            await _sync_automatic_role(member, False, settings)
            await sync_rewards(member, settings)
    await apply_emoji_restrictions(guild, settings)
    await update_stat_channels(guild, settings)
    return imported


async def update_stat_channels(guild: discord.Guild, settings: dict | None = None) -> None:
    options = settings or config_for(guild)
    values = stats(guild)
    changed_config = False
    for item in options.get("stat_channels", []):
        if not isinstance(item, dict) or str(item.get("metric")) not in STAT_METRICS:
            continue
        metric = str(item["metric"])
        channel_id = str(item.get("channel_id") or "")
        channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        if channel is not None and item.get("delete"):
            try:
                await channel.delete(reason="dashboard deleted booster statistic channel")
                item["channel_id"] = ""
                item["delete"] = False
                changed_config = True
            except discord.HTTPException:
                pass
            continue
        if channel is None and item.get("create") and guild.me and guild.me.guild_permissions.manage_channels:
            category_id = str(item.get("category_id") or "")
            category = guild.get_channel(int(category_id)) if category_id.isdigit() else None
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
            }
            try:
                channel = await guild.create_voice_channel(
                    str(item.get("name") or metric.replace("_", " ") + ": {value}").replace(
                        "{value}", str(values[metric])
                    )[:100],
                    category=category if isinstance(category, discord.CategoryChannel) else None,
                    overwrites=overwrites,
                    reason="dashboard created booster statistic channel",
                )
                item["channel_id"] = str(channel.id)
                item["create"] = False
                changed_config = True
            except discord.HTTPException:
                continue
        if not isinstance(channel, discord.VoiceChannel):
            continue
        template = str(item.get("name") or metric.replace("_", " ") + ": {value}")[:100]
        name = template.replace("{value}", str(values[metric]))[:100]
        if channel.name != name:
            try:
                await channel.edit(name=name, reason="booster statistic update")
            except discord.HTTPException:
                pass
    if changed_config:
        db.module_config_set(
            _scope(guild), "boosters", enabled=True, settings=options, actor_id="system:booster-stats"
        )


async def apply_emoji_restrictions(guild: discord.Guild, settings: dict | None = None) -> None:
    options = settings or config_for(guild)
    if not options.get("emoji_restrictions_enabled") or not guild.me or not guild.me.guild_permissions.manage_emojis_and_stickers:
        return
    explicit = {
        int(str(item.get("emoji_id"))): item for item in options.get("emoji_restrictions", [])
        if isinstance(item, dict) and str(item.get("emoji_id")).isdigit()
    }
    normal_ids = _ids(options.get("normal_emoji_role_ids"))
    animated_ids = _ids(options.get("animated_emoji_role_ids"))
    for emoji in guild.emojis:
        item = explicit.get(emoji.id, {})
        role_ids = _ids(item.get("role_ids")) if item else (animated_ids if emoji.animated else normal_ids)
        roles = [role for role_id in role_ids if (role := guild.get_role(role_id))]
        try:
            await emoji.edit(roles=roles, reason="dashboard emoji restriction")
        except discord.HTTPException:
            pass


async def handle_mentions(message: discord.Message) -> None:
    if message.guild is None or not message.mentions:
        return
    settings = config_for(message.guild)
    if not settings.get("mention_reactions_enabled"):
        return
    minimum = max(1, int(settings.get("mention_reaction_min_boosts") or 1))
    for member in message.mentions[:10]:
        if _boost_count(member) < minimum:
            continue
        emoji = db.kv_get(_MENTION_EMOJI.format(user_id=member.id, guild_id=message.guild.id))
        if emoji:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass


async def scheduler_tick(client: discord.Client) -> None:
    global _last_scheduler
    await _dashboard_actions_tick(client)
    current = time.monotonic()
    if current - _last_scheduler < 300:
        return
    _last_scheduler = current
    for guild in list(client.guilds):
        settings = config_for(guild)
        if not settings.get("tracking_enabled"):
            continue
        for record in db.booster_members(_scope(guild), active=True, limit=10_000):
            member = guild.get_member(int(record["user_id"]))
            if member:
                await sync_rewards(member, settings)
        await update_stat_channels(guild, settings)


async def _dashboard_actions_tick(client: discord.Client) -> None:
    """Execute authenticated dashboard requests inside the Discord event loop."""
    for guild in list(client.guilds):
        records = db.community_records(
            "booster_dashboard_action",
            _scope(guild),
            due_before=time.time(),
            limit=100,
        )
        for record in records:
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            action = str(data.get("action") or "")
            target_id = str(data.get("target_id") or "")
            result = ""
            status = "completed"
            try:
                if action == "sync":
                    imported = await sync_guild(guild)
                    result = f"synchronized; {imported} newly imported"
                elif action == "test":
                    member = guild.get_member(int(target_id)) if target_id.isdigit() else None
                    if member is None:
                        raise ValueError("target member is unavailable")
                    sent = await test_greeting(member)
                    result = "test greeting sent" if sent else "greeting delivery failed"
                    if sent is None:
                        status = "failed"
                elif action == "reconcile":
                    member = guild.get_member(int(target_id)) if target_id.isdigit() else None
                    if member is None:
                        raise ValueError("target member is unavailable")
                    settings = config_for(guild)
                    record_data = member_record(member)
                    await _sync_automatic_role(member, record_data["active"], settings)
                    await sync_rewards(member, settings)
                    await update_stat_channels(guild, settings)
                    result = "member perks reconciled"
                else:
                    raise ValueError("unknown dashboard booster action")
            except (ValueError, discord.HTTPException) as error:
                status = "failed"
                result = str(error)[:500]
            data["result"] = result
            db.community_record_update(int(record["id"]), data=data, status=status, due=None)


def age_seconds(value: str) -> int | None:
    units = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
             "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
             "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400, "day": 86400,
             "days": 86400, "w": 604800, "week": 604800, "weeks": 604800,
             "month": 2592000, "months": 2592000, "y": 31536000, "year": 31536000,
             "years": 31536000}
    matches = re.findall(r"(\d+)\s*(years?|months?|weeks?|days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|[smhdwy])", value.lower())
    if not matches:
        return None
    return sum(int(amount) * units.get(unit, units.get(unit.removesuffix("s"), 0)) for amount, unit in matches)


def limitations_text() -> str:
    return (
        "Discord exposes new boost messages and when the Server Booster role appears or disappears. "
        "If someone has multiple boosts and removes only one, Discord does not expose a reliable event; "
        "a configured booster manager can correct it with `booster adjust @member -1`."
    )
