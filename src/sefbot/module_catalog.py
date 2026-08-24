"""Declarative catalog for every community-management module.

The catalog is shared by Discord runtime code and the web dashboard.  Keeping
defaults and validation metadata in one place prevents the dashboard from
claiming a setting that the bot cannot understand.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Final


def _module(
    title: str,
    category: str,
    description: str,
    settings: dict,
    *,
    default_enabled: bool = False,
    implementation: str = "core live",
) -> dict:
    return {
        "title": title,
        "category": category,
        "description": description,
        "settings": settings,
        "default_enabled": default_enabled,
        "implementation": implementation,
    }


MODULES: Final[dict[str, dict]] = {
    "afk": _module("AFK", "Community", "Away statuses, notes, return alerts and nickname markers.", {
        "nickname_prefix": "[AFK]", "ignored_channel_ids": [], "enhanced_cards": True,
    }),
    "action_log": _module("Action Log", "Safety", "Message, member, role, channel, emoji and voice audit events.", {
        "default_channel_id": "", "message_channel_id": "", "member_channel_id": "",
        "moderation_channel_id": "", "voice_channel_id": "", "ignored_channel_ids": [],
        "ignored_category_ids": [], "ignored_role_ids": [], "show_account_age": True,
        "show_avatars": True,
    }),
    "announcements": _module("Announcements", "Community", "Join, leave, ban and manual announcements.", {
        "channel_id": "", "join_message": "Welcome {user.mention} to **{server.name}**!",
        "leave_message": "**{user.name}** left **{server.name}**.",
        "ban_message": "**{user.name}** was banned.",
    }),
    "auto_delete": _module("Auto Delete", "Automation", "Delete messages matching all or any configured filters.", {
        "rules": [],
    }),
    "auto_message": _module("Auto Message", "Automation", "One-time or repeating channel messages and embeds.", {
        "messages": [],
    }),
    "auto_purge": _module("Auto Purge", "Automation", "Scheduled filtered channel cleanup while preserving pins.", {
        "rules": [], "maximum_per_run": 5000,
    }),
    "autoban": _module("Autoban", "Safety", "Ban newly joined accounts using age and username rules.", {
        "minimum_account_age_hours": 0, "username_contains": [], "username_exact": [],
        "username_wildcards": [], "reason": "Matched an autoban rule",
    }),
    "automod": _module("Automod", "Safety", "Spam, words, links, mentions and phishing protection.", {
        "log_channel_id": "", "banned_phrases": [], "allowed_domains": [],
        "blocked_domains": [], "max_caps_percent": 80, "max_mentions": 5,
        "max_newlines": 8, "max_length": 1800, "duplicate_window_seconds": 15,
        "rapid_messages": 6, "rapid_window_seconds": 8, "delete": True,
        "warn": True, "instant_timeout_minutes": 0, "instant_ban": False,
        "ignored_role_ids": [], "ignored_channel_ids": [], "allowed_role_ids": [],
        "allowed_channel_ids": [], "custom_response": "",
    }),
    "autoresponder": _module("Autoresponder", "Community", "Text, embed and reaction responses with exact or wildcard triggers.", {
        "responders": [],
    }),
    "autoroles": _module("Autoroles & Ranks", "Community", "Join roles, delayed removal and self-assignable ranks.", {
        "join_roles": [], "ranks": [], "one_rank_only": False,
    }),
    "custom_commands": _module("Custom Commands", "Community", "Custom responses, variables, routing, permissions and cooldowns.", {
        "prefix": "!", "commands": [],
    }, default_enabled=True, implementation="partial"),
    "economy": _module("Economy & Cards", "Engagement", "Coins, gems, streaks, cards, decks, fusion and battles.", {
        "daily_base": 100, "work_min": 20, "work_max": 80, "battle_stamina_max": 10,
        "cards_enabled": True, "battle_pass_bonus_percent": 0,
    }, default_enabled=True, implementation="partial"),
    "forms": _module("Forms", "Support", "Public Discord-linked forms, permissions and submission automation.", {
        "public_base_url": "https://kozzyx.org", "forms": [],
    }),
    "fun": _module("Fun", "Engagement", "Games, media, polls and information commands.", {
        "allowed_channel_ids": [], "disabled_commands": [],
    }, default_enabled=True),
    "giveaways": _module("Giveaways", "Engagement", "Timed button giveaways, eligibility, roles and rerolls.", {
        "giveaways": [],
    }, implementation="partial"),
    "highlights": _module("Highlights", "Community", "DM users when their subscribed phrases are mentioned.", {
        "ignored_channel_ids": [],
    }),
    "kick": _module("Kick Notifications", "Feeds", "Kick live notifications through messages or webhooks.", {
        "subscriptions": [], "poll_minutes": 5,
    }),
    "levels": _module("Levels", "Engagement", "Chat XP, rewards, multipliers, profiles and leaderboards.", {
        "xp_min": 8, "xp_max": 15, "cooldown_seconds": 60, "level_up_channel_id": "",
        "level_up_message": "{user.mention} reached level **{level}**!", "reward_roles": [],
        "server_multiplier": 1.0, "channel_multipliers": {}, "boost_mode": "highest",
        "ignored_role_ids": [], "ignored_channel_ids": [], "allowed_role_ids": [],
        "allowed_channel_ids": [],
    }),
    "embedder": _module("Message Embedder", "Content", "Draft, publish and edit managed Discord embeds.", {
        "embeds": [],
    }, implementation="configuration only"),
    "moderation": _module("Moderation", "Safety", "Cases, warnings, timeouts, bans, notes, locks, purge and appeals.", {
        "moderator_role_ids": [], "protected_role_ids": [], "log_channel_id": "",
        "dm_actions": True, "appeal_url": "", "remove_roles_while_muted": False,
        "preserve_ban_messages": True, "autopunish": [], "custom_responses": {},
    }, default_enabled=True, implementation="partial"),
    "reaction_roles": _module("Reaction Roles", "Community", "Reaction, button and dropdown role menus.", {
        "menus": [],
    }, implementation="partial"),
    "reddit": _module("Reddit", "Feeds", "Post new subreddit submissions with flair and NSFW filters.", {
        "subscriptions": [], "poll_minutes": 5,
    }),
    "reminders": _module("Reminders", "Utility", "Personal reminders with jump links.", {
        "maximum_per_user": 100,
    }),
    "slowmode": _module("Slowmode", "Safety", "Discord-native or bot-enforced per-channel rate limits.", {
        "channels": [],
    }),
    "starboard": _module("Starboard", "Engagement", "Highlight popular messages and collect star statistics.", {
        "channel_id": "", "emoji": "⭐", "threshold": 3, "secret": False,
        "ignored_channel_ids": [],
    }),
    "tags": _module("Tags", "Community", "Categorized reusable server text.", {
        "creator_role_ids": [],
    }),
    "tickets": _module("Tickets", "Support", "Private ticket panels, intake fields, routing and transcripts.", {
        "category_id": "", "transcript_channel_id": "", "staff_role_ids": [],
        "panels": [], "max_open_per_member": 5, "channel_name": "ticket-{user.name}",
    }, implementation="partial"),
    "tiktok": _module("TikTok", "Feeds", "New TikTok notifications and previews.", {
        "subscriptions": [], "poll_minutes": 5,
    }),
    "twitch": _module("Twitch", "Feeds", "Twitch go-live notifications.", {
        "subscriptions": [], "poll_minutes": 5,
    }),
    "youtube": _module("YouTube", "Feeds", "Video, Shorts and livestream notifications.", {
        "subscriptions": [], "poll_minutes": 5,
    }),
    "voice_text": _module("Voice Text Linking", "Community", "Grant linked text access while members are in voice.", {
        "bindings": [],
    }),
    "welcome": _module("Welcome", "Community", "Channel, DM and image welcomes with variables.", {
        "channel_id": "", "message": "Welcome {user.mention}!", "dm_message": "",
        "embed": {}, "image_enabled": False, "image_text": "Welcome {user.name}",
    }, implementation="partial"),
    "server_management": _module("Server Management", "Administration", "Role, nickname, member, invite and emoji utilities.", {
        "manager_role_ids": [], "ignored_user_ids": [], "ignored_role_ids": [],
        "ignored_channel_ids": [],
    }, default_enabled=True, implementation="partial"),
    "bot_controls": _module("Bot Controls", "Administration", "Command/module switches, permissions, prefix and diagnostics.", {
        "prefix": "!", "disabled_commands": [], "allowed_role_ids": [],
        "ignored_role_ids": [], "allowed_channel_ids": [], "ignored_channel_ids": [],
    }, default_enabled=True, implementation="partial"),
    "localization": _module("Localization", "Administration", "Bot and dashboard language preferences.", {
        "bot_language": "en", "dashboard_language": "en",
    }, default_enabled=True),
}


def default_settings(name: str) -> dict:
    module = MODULES.get(name)
    if module is None:
        raise KeyError(name)
    return deepcopy(module["settings"])


def public_catalog() -> list[dict]:
    return [
        {"id": name, **deepcopy(definition)}
        for name, definition in MODULES.items()
    ]


def merge_settings(name: str, value: object) -> dict:
    """Return a bounded, type-compatible settings object for a module."""
    defaults = default_settings(name)
    if not isinstance(value, dict):
        return defaults
    if len(value) > 200:
        raise ValueError("too many settings")
    for key, candidate in value.items():
        if key not in defaults:
            continue
        expected = defaults[key]
        if isinstance(expected, bool) and isinstance(candidate, bool):
            defaults[key] = candidate
        elif isinstance(expected, str) and isinstance(candidate, str):
            defaults[key] = candidate[:4000]
        elif isinstance(expected, int) and not isinstance(expected, bool) and isinstance(candidate, int):
            defaults[key] = max(-1_000_000, min(1_000_000, candidate))
        elif isinstance(expected, float) and isinstance(candidate, (int, float)):
            defaults[key] = max(0.0, min(1000.0, float(candidate)))
        elif isinstance(expected, list) and isinstance(candidate, list):
            defaults[key] = candidate[:500]
        elif isinstance(expected, dict) and isinstance(candidate, dict):
            defaults[key] = dict(list(candidate.items())[:500])
    return defaults
