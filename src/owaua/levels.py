"""Per-user XP/levels and every server-booster perk in one place.

Levels
------
Every guild message grants XP (cooldown-gated per user).  Boosters earn
1.5x XP.  Level-ups are announced in-channel.

Booster perks
-------------
- 1.5x chat XP multiplier
- +50% work pay and a 30s (instead of 60s) work cooldown
- Better gamble odds (55% vs 40%)
- Bigger daily stipend ($250 vs $100) plus streak tracking
- A bot-managed custom role (own name + color) via ``!boosterrole``
- Automatic removal of that role when boosting stops
"""
import secrets

import discord

from owaua import db, embeds, opsec

XP_COOLDOWN_SECONDS = 60.0
BASE_XP_MIN, BASE_XP_MAX = 15, 25
BOOSTER_XP_MULTIPLIER = 1.5

WORK_COOLDOWN_SECONDS = 60
BOOSTER_WORK_COOLDOWN_SECONDS = 30
BOOSTER_WORK_BONUS = 0.5
WORK_REWARD_MIN, WORK_REWARD_MAX = 50, 499

GAMBLE_WIN_CHANCE = 0.40
BOOSTER_GAMBLE_WIN_CHANCE = 0.55

DAILY_REWARD = 100
DAILY_BOOSTER_REWARD = 250

LEVEL_TITLES = [
    (50, "legend"),
    (30, "mythic"),
    (20, "veteran"),
    (10, "regular"),
    (5, "known"),
    (1, "newcomer"),
]

_BOOSTER_ROLE_KEY = "boosterrole:{guild_id}:{user_id}"


def is_booster(member) -> bool:
    return getattr(member, "premium_since", None) is not None


def level_title(level: int) -> str:
    for threshold, title in LEVEL_TITLES:
        if level >= threshold:
            return title
    return "lurker"


def xp_multiplier(is_boosting: bool) -> float:
    return BOOSTER_XP_MULTIPLIER if is_boosting else 1.0


def work_cooldown_seconds(is_boosting: bool) -> int:
    return BOOSTER_WORK_COOLDOWN_SECONDS if is_boosting else WORK_COOLDOWN_SECONDS


def gamble_win_chance(is_boosting: bool) -> float:
    return BOOSTER_GAMBLE_WIN_CHANCE if is_boosting else GAMBLE_WIN_CHANCE


def daily_reward(is_boosting: bool) -> int:
    return DAILY_BOOSTER_REWARD if is_boosting else DAILY_REWARD


def perks_summary(is_boosting: bool) -> str:
    if not is_boosting:
        return (
            "no active perks. boost this server to unlock:\n"
            "- **1.5x** chat XP\n"
            f"- **+{int(BOOSTER_WORK_BONUS * 100)}%** work pay, "
            f"{BOOSTER_WORK_COOLDOWN_SECONDS}s work cooldown\n"
            f"- gamble odds **{int(BOOSTER_GAMBLE_WIN_CHANCE * 100)}%** "
            f"(vs {int(GAMBLE_WIN_CHANCE * 100)}%)\n"
            f"- daily stipend **${DAILY_BOOSTER_REWARD}** (vs ${DAILY_REWARD})\n"
            "- your own custom role name + color (`boosterrole`)"
        )
    return (
        "**active booster perks**\n"
        f"- **{BOOSTER_XP_MULTIPLIER}x** chat XP\n"
        f"- **+{int(BOOSTER_WORK_BONUS * 100)}%** work pay, "
        f"{BOOSTER_WORK_COOLDOWN_SECONDS}s work cooldown\n"
        f"- gamble odds **{int(BOOSTER_GAMBLE_WIN_CHANCE * 100)}%**\n"
        f"- daily stipend **${DAILY_BOOSTER_REWARD}** (+ streak bonus)\n"
        "- custom role via `boosterrole`"
    )


def award_message(
    user_id: str,
    guild_id: str,
    *,
    is_boosting: bool,
    settings: dict | None = None,
    channel_id: str | None = None,
) -> dict | None:
    """Record a message for XP; returns level-up info or None when on cooldown."""
    options = settings if isinstance(settings, dict) else {}
    low = max(1, min(1000, int(options.get("xp_min") or BASE_XP_MIN)))
    high = max(low, min(1000, int(options.get("xp_max") or BASE_XP_MAX)))
    base = low + secrets.randbelow(high - low + 1)
    server_multiplier = max(0.0, min(100.0, float(options.get("server_multiplier") or 1.0)))
    channel_multiplier = 1.0
    channel_multipliers = options.get("channel_multipliers")
    if isinstance(channel_multipliers, dict) and channel_id is not None:
        try:
            channel_multiplier = max(
                0.0, min(100.0, float(channel_multipliers.get(str(channel_id), 1.0)))
            )
        except (TypeError, ValueError):
            channel_multiplier = 1.0
    configured = (
        max(server_multiplier, channel_multiplier)
        if options.get("boost_mode") == "highest"
        else server_multiplier * channel_multiplier
    )
    amount = int(base * configured * xp_multiplier(is_boosting))
    return db.levels_award(
        user_id,
        guild_id,
        amount,
        max(0.0, min(86_400.0, float(options.get("cooldown_seconds") or XP_COOLDOWN_SECONDS))),
    )


def progress_bar(xp_in_level: int, needed: int, width: int = 12) -> str:
    filled = int(width * min(1.0, xp_in_level / max(1, needed)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def rank_card(user_id: str, guild_id: str) -> str:
    profile = db.levels_profile(user_id, guild_id)
    level = profile["level"]
    total_needed = sum(db.xp_needed(lvl) for lvl in range(level))
    into_level = profile["xp"] - total_needed
    needed = db.xp_needed(level)
    bar = progress_bar(into_level, needed)
    return (
        f"**level {level}** ({level_title(level)}) — {profile['xp']} xp "
        f"total, {profile['messages']} messages\n"
        f"{bar} {into_level}/{needed} to next level"
    )


async def _resolve_booster_role(
    guild: discord.Guild, member: discord.Member, *, create: bool = True
) -> discord.Role | None:
    """Find (or create) the bot-managed personal role for this booster."""
    key = _BOOSTER_ROLE_KEY.format(guild_id=guild.id, user_id=member.id)
    role_id = db.kv_get(key)
    if role_id:
        role = guild.get_role(int(role_id))
        if role is not None:
            return role
        db.kv_set(key, "")
    me = guild.me
    if not create or me is None or not me.guild_permissions.manage_roles:
        return None
    try:
        role = await guild.create_role(
            name=f"{member.display_name}'s boost",
            reason="booster custom role perk",
        )
    except discord.HTTPException:
        return None
    db.kv_set(key, str(role.id))
    return role


async def set_booster_role(
    member: discord.Member, color_hex: str, name: str | None = None
) -> tuple[bool, str]:
    """Create/update the booster's custom role. Returns (ok, message)."""
    guild = member.guild
    if member.premium_since is None:
        return False, "only server boosters get a custom role."
    color = _parse_color(color_hex)
    if color is None:
        return False, "give me a hex color like `#8a2be2` or `8a2be2`."
    role = await _resolve_booster_role(guild, member)
    if role is None:
        return False, "i need **Manage Roles** and a position above members to do that."
    clean_name = (name or role.name).strip()[:60]
    try:
        await role.edit(name=clean_name, colour=color, reason="booster custom role perk")
        await member.add_roles(role, reason="booster custom role perk")
    except discord.HTTPException:
        return False, "couldn't update your role — check my permissions."
    return True, f"your role is now **{clean_name}** in #{color}"


async def handle_member_update(before: discord.Member, after: discord.Member) -> str | None:
    """Remove the custom role when a member stops boosting. Returns a notice."""
    if before.premium_since is not None and after.premium_since is None:
        key = _BOOSTER_ROLE_KEY.format(guild_id=after.guild.id, user_id=after.id)
        role_id = db.kv_get(key)
        if not role_id:
            return None
        role = after.guild.get_role(int(role_id))
        db.kv_set(key, "")
        if role is not None:
            try:
                await role.delete(reason="boost ended")
            except discord.HTTPException:
                pass
            return f"removed {after.display_name}'s booster role (boost ended)."
    return None


def _parse_color(raw: str):
    text = (raw or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    return discord.Colour(value)


def build_daily_reply(user_id: str, guild_id: str, is_boosting: bool):
    """Claim the daily stipend; returns (embed, ok)."""
    remaining, credited, streak = db.daily_claim(
        user_id, guild_id, daily_reward(is_boosting)
    )
    if remaining > 0:
        hours, mins = divmod(int(remaining // 60), 60)
        return (
            embeds.error(
                f"already claimed today — next claim in {hours}h {mins}m."
            ),
            False,
        )
    balance = opsec.add_balance(user_id, credited)
    label = "booster stipend" if is_boosting else "daily stipend"
    return (
        embeds.say(
            f"{label}: **${credited}** — streak **{streak}** day(s). "
            f"balance: ${balance}."
        ),
        True,
    )
