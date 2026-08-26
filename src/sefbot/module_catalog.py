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
    default_enabled: bool = True,
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
    "action_log": _module("Action Log", "Safety", "Choose one global destination and the actor-attributed audit, message, member, role, channel, thread, server, reaction and voice events to send there.", {
        "channel_id": "",
        "audit_events": True, "message_events": True, "member_events": True,
        "moderation_events": True, "voice_events": True, "role_events": True,
        "channel_events": True, "thread_events": True, "server_events": True,
        "reaction_events": True, "command_events": True, "include_message_content": True,
        "include_attachments": True, "include_audit_changes": True,
        "include_reasons": True, "include_ids": True, "include_timestamps": True,
        "include_bot_events": True, "include_voice_state_changes": True,
        "bulk_delete_sample_size": 20, "ignored_channel_ids": [],
        "ignored_category_ids": [], "ignored_role_ids": [], "ignored_user_ids": [],
        "show_account_age": True, "show_avatars": True,
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
    "boosters": _module("Booster Perks", "Engagement", "Boost history, greetings, personal roles and channels, rewards, reactions, counters and logs.", {
        "tracking_enabled": True,
        "greetings_enabled": False, "greet_channel_id": "", "greet_messages": [
            "Thanks {user}! You now have {userboosts} recorded boosts and brought us to {count}."
        ],
        "greet_images": [], "greet_embed": True, "greet_author": "",
        "greet_author_icon": "", "greet_title": "Thank you for boosting!",
        "greet_footer": "", "greet_footer_icon": "", "greet_thumbnail": "",
        "greet_image": "", "greet_color": "5865f2", "greet_addon": "", "greet_dm": False,
        "greet_include_stats": True, "greet_reaction": "", "react_original": False,
        "react_custom": False,
        "automatic_role_enabled": False, "automatic_role_id": "", "stop_remove_role_id": "",
        "personal_roles_enabled": True, "personal_role_min_boosts": 1,
        "personal_role_base_role_id": "", "personal_role_allow_hoist": False,
        "personal_role_allowed_colors": [], "personal_role_prefix": "",
        "personal_role_suffix": "", "personal_role_banned_words": [],
        "qualifying_role_ids": [], "revoke_role_ids": [], "delete_ineligible_personal_role": True,
        "role_gifts_enabled": False, "role_gift_min_boosts": 1, "role_gift_slots": 1,
        "boost_level_roles": [], "boost_age_roles": [],
        "private_channels_enabled": False, "private_channel_category_id": "",
        "private_channel_type": "text", "private_channel_min_boosts": 1,
        "private_channel_friend_slots": 0, "private_channel_invite_min_boosts": 1,
        "private_channel_allow_role_ids": [], "private_channel_deny_role_ids": [],
        "private_channel_manager_access": True,
        "mention_reactions_enabled": False, "mention_reaction_min_boosts": 1,
        "emoji_restrictions_enabled": False, "emoji_restrictions": [],
        "normal_emoji_role_ids": [], "animated_emoji_role_ids": [], "stat_channels": [],
        "log_channel_id": "", "log_color": "5865f2",
        "log_events": ["boost_add", "boost_remove", "role", "channel"],
        "log_routes": {}, "manager_role_ids": [],
    }),
    "custom_commands": _module("Custom Commands", "Community", "Custom responses, variables, routing, permissions and cooldowns.", {
        "commands": [],
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
        "appeals_enabled": True, "appeal_channel_id": "", "case_expiry_days": 30,
        "member_notes_enabled": True, "evidence_links_enabled": True,
        "preserve_ban_messages": True, "autopunish": [], "custom_responses": {},
    }, default_enabled=True, implementation="core live"),
    "incident_center": _module("Staff Incident Center", "Safety", "Unified assignable queue for malware, automod, rules, assistant actions, cases and tickets.", {
        "staff_role_ids": [], "escalation_role_ids": [], "default_assignee_id": "",
        "notify_channel_id": "", "critical_ping_role_id": "",
        "sources": ["malware", "automod", "rules", "assistant", "moderation", "ticket", "feed"],
        "sla_hours": {"critical": 1, "high": 4, "medium": 24, "low": 72},
    }, default_enabled=True, implementation="core live"),
    "malware_scanner": _module(
        "Malware Scanner",
        "Safety",
        "Locally scan non-media attachments, remove malware and block confirmed senders.",
        {
            "block_users": True,
            "exclude_verified_media": True,
            "fail_closed": True,
            "log_channel_id": "",
            "max_file_mb": 100,
            "notify_channel": True,
        },
        default_enabled=True,
        implementation="core live",
    ),
    "reaction_roles": _module("Reaction Roles", "Community", "Reaction, button and dropdown role menus.", {
        "menus": [], "persistent_components": True, "remove_on_unselect": True,
        "require_rules_ack": False, "maximum_roles_per_menu": 25,
    }, implementation="core live"),
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
        "panels": [], "intake_fields": [], "routing_rules": [], "sla_hours": 24,
        "reminder_hours": 12, "auto_close_hours": 0, "require_intake": False,
        "allow_member_close": True, "assignment_mode": "manual",
        "max_open_per_member": 5, "channel_name": "ticket-{user.name}",
    }, implementation="core live"),
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
        "journey_enabled": False, "rules_channel_id": "", "rules_ack_role_id": "",
        "role_choices": [], "intro_questions": [], "starter_channel_ids": [],
        "help_followup_hours": 24, "help_message": "Need a hand getting started?",
    }, implementation="core live"),
    "scheduled_digests": _module("Scheduled Digests", "Automation", "Explicit daily or weekly staff summaries with bounded aggregate sections.", {
        "enabled_cadences": [], "daily_channel_id": "", "weekly_channel_id": "",
        "visibility": "staff", "sections": ["growth", "moderation", "engagement", "highlights", "tickets", "feeds", "scheduled_messages"],
    }, default_enabled=True, implementation="core live"),
    "server_health": _module("Server Health Advisor", "Safety", "Explanatory weekly configuration and workflow recommendations that never auto-change settings.", {
        "weekly_enabled": False, "delivery_channel_id": "", "staff_only": True,
        "check_permissions": True, "check_logging": True, "check_tickets": True,
        "check_automod_noise": True, "check_booster_drift": True,
    }, default_enabled=True, implementation="core live"),
    "analytics_exports": _module("Analytics & Retention", "Administration", "Aggregate CSV exports and per-module retention transparency without message content or profiles.", {
        "aggregate_csv_enabled": True, "include_growth": True, "include_moderation": True,
        "include_engagement": True, "include_feeds": True,
    }, default_enabled=True, implementation="core live"),
    "server_management": _module("Server Management", "Administration", "Role, nickname, member, invite and emoji utilities.", {
        "manager_role_ids": [], "ignored_user_ids": [], "ignored_role_ids": [],
        "ignored_channel_ids": [],
    }, default_enabled=True, implementation="partial"),
    "bot_controls": _module("Bot Controls", "Administration", "Command/module switches, permissions, prefix and diagnostics.", {
        "prefix": "", "disabled_commands": [], "allowed_role_ids": [],
        "ignored_role_ids": [], "allowed_channel_ids": [], "ignored_channel_ids": [],
    }, default_enabled=True, implementation="partial"),
    "localization": _module("Localization", "Administration", "Bot and dashboard language preferences.", {
        "bot_language": "en", "dashboard_language": "en",
    }, default_enabled=True),
}


# Core settings predate the module catalog and are read directly by the AI,
# privacy, rules, moderation and voice runtimes.  Keeping their public editor
# metadata and validation here gives the dashboard one complete, allow-listed
# surface instead of accepting arbitrary guild_settings keys.
SERVER_SETTINGS: Final[dict[str, dict]] = {
    "persona": {
        "label": "AI persona",
        "description": "Optional server-specific system persona. Empty uses the host default.",
        "kind": "textarea",
        "default": "",
        "max_length": 4000,
    },
    "opinion_profile": {
        "label": "Bot opinion addendum",
        "description": "Optional server-specific tastes or standards that refine SefBot's default viewpoints.",
        "kind": "textarea",
        "default": "",
        "max_length": 2000,
    },
    "model": {
        "label": "Chat model",
        "description": "Model used for normal server conversations. Default follows the host configuration.",
        "kind": "model",
        "default": "",
        "max_length": 160,
    },
    "language": {
        "label": "Default reply language",
        "description": "Language name or code used when a member has no personal preference.",
        "kind": "text",
        "default": "",
        "max_length": 80,
    },
    "swear_level": {
        "label": "Persona language level",
        "description": "Controls profanity in AI responses.",
        "kind": "choice",
        "default": "full",
        "choices": ["clean", "medium", "full"],
    },
    "swear_jar_enabled": {
        "label": "Swear jar",
        "description": (
            "Count profanity per member in this server and reply with their updated total. "
            "Only the numeric total is stored."
        ),
        "kind": "boolean",
        "default": False,
    },
    "smart_always": {
        "label": "Prefer smart routing",
        "description": "Use the smart model tier for normal chat; disable to prefer the fast tier.",
        "kind": "boolean",
        "default": True,
    },
    "allowed_channels": {
        "label": "Allowed command channels",
        "description": "Empty allows commands in every channel.",
        "kind": "channel_ids",
        "default": [],
        "maximum": 100,
    },
    "lurk": {
        "label": "Lurk mode",
        "description": "Allow an occasional AI message after the configured channel becomes quiet.",
        "kind": "boolean",
        "default": False,
    },
    "lurk_channel": {
        "label": "Lurk channel",
        "description": "Channel used by lurk mode. Lurk also requires history storage.",
        "kind": "channel_id",
        "default": "",
    },
    "history_enabled": {
        "label": "Server history storage",
        "description": "Opt in to bounded server conversation history for members who also consent.",
        "kind": "boolean",
        "default": False,
    },
    "retention_days": {
        "label": "Content retention days",
        "description": "Maximum age for this server's stored messages, conversations, feedback and audit data.",
        "kind": "integer",
        "default": 30,
        "minimum": 1,
        "maximum": 30,
    },
    "moderation_enabled": {
        "label": "AI moderation review",
        "description": "Send model-classified messages to a private staff review channel. Host safety credentials are also required.",
        "kind": "boolean",
        "default": False,
    },
    "modlog_channel": {
        "label": "Private moderation log",
        "description": "Must be a private text channel writable by SefBot.",
        "kind": "channel_id",
        "default": "",
    },
    "rules_enabled": {
        "label": "Rules review",
        "description": "Enable deterministic server-rule detection and confirmation-based enforcement.",
        "kind": "boolean",
        "default": False,
    },
    "approval_channel": {
        "label": "Private approval channel",
        "description": "Private staff channel for rule and action confirmations.",
        "kind": "channel_id",
        "default": "",
    },
    "voice_transcription_enabled": {
        "label": "Voice transcription",
        "description": "Allow consent-gated speech transcription in this server. Host STT support is also required.",
        "kind": "boolean",
        "default": False,
    },
    "ai_workflows_enabled": {
        "label": "AI workflow toolkit",
        "description": "Enable read-only AI summaries, rewrites, analysis, study tools, reply drafts and grounded fact checks.",
        "kind": "boolean",
        "default": True,
    },
    "ai_default_tone": {
        "label": "AI workflow detail",
        "description": "Default response depth for the AI workflow toolkit.",
        "kind": "choice",
        "default": "balanced",
        "choices": ["concise", "balanced", "detailed"],
    },
    "ai_default_language": {
        "label": "AI workflow language",
        "description": "Optional output language for AI workflows. Empty follows the source or request.",
        "kind": "text",
        "default": "",
        "max_length": 80,
    },
    "ai_max_input_chars": {
        "label": "AI workflow input limit",
        "description": "Maximum characters accepted by one AI workflow request.",
        "kind": "integer",
        "default": 12000,
        "minimum": 1000,
        "maximum": 20000,
    },
    "ai_channel_context_messages": {
        "label": "AI channel context messages",
        "description": "Maximum recent messages considered by channel/thread intelligence.",
        "kind": "integer",
        "default": 30,
        "minimum": 5,
        "maximum": 100,
    },
    "ai_fact_check_search": {
        "label": "Grounded AI fact checking",
        "description": "Allow the fact-check workflow to retrieve current web results and show validated source links.",
        "kind": "boolean",
        "default": True,
    },
    "ai_staff_triage": {
        "label": "AI staff triage",
        "description": "Allow Manage Server members to request advisory moderation triage. It never enforces automatically.",
        "kind": "boolean",
        "default": True,
    },
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


def default_server_settings() -> dict:
    return {key: deepcopy(definition["default"]) for key, definition in SERVER_SETTINGS.items()}


def public_server_settings() -> list[dict]:
    return [
        {"key": key, **deepcopy(definition)}
        for key, definition in SERVER_SETTINGS.items()
    ]


def merge_server_settings(value: object) -> dict:
    """Return the complete, bounded set of dashboard-editable guild settings."""
    clean = default_server_settings()
    if not isinstance(value, dict):
        return clean
    if len(value) > len(SERVER_SETTINGS) + 20:
        raise ValueError("too many server settings")
    for key, candidate in value.items():
        definition = SERVER_SETTINGS.get(key)
        if definition is None:
            continue
        kind = definition["kind"]
        if kind == "boolean" and isinstance(candidate, bool):
            clean[key] = candidate
        elif kind == "integer" and isinstance(candidate, int) and not isinstance(candidate, bool):
            clean[key] = max(
                int(definition.get("minimum", -1_000_000)),
                min(int(definition.get("maximum", 1_000_000)), candidate),
            )
        elif kind in {"text", "textarea", "model"} and isinstance(candidate, str):
            text = candidate.strip()
            if kind == "model" and text and not all(
                char.isalnum() or char in "._:/-" for char in text
            ):
                continue
            clean[key] = text[: int(definition.get("max_length", 4000))]
        elif kind == "choice" and isinstance(candidate, str):
            if candidate in definition.get("choices", []):
                clean[key] = candidate
        elif kind == "channel_id" and isinstance(candidate, str):
            text = candidate.strip()
            if not text or (text.isdigit() and len(text) <= 24):
                clean[key] = text
        elif kind == "channel_ids" and isinstance(candidate, list):
            maximum = int(definition.get("maximum", 100))
            clean[key] = list(dict.fromkeys(
                str(item) for item in candidate
                if str(item).isdigit() and len(str(item)) <= 24
            ))[:maximum]
    return clean


def merge_settings(name: str, value: object) -> dict:
    """Return a bounded, type-compatible settings object for a module."""
    defaults = default_settings(name)
    if not isinstance(value, dict):
        return defaults
    if name == "action_log" and not value.get("channel_id"):
        legacy_destinations = (
            "default_channel_id", "audit_channel_id", "message_channel_id",
            "member_channel_id", "moderation_channel_id", "voice_channel_id",
            "role_channel_id", "channel_channel_id", "thread_channel_id",
            "server_channel_id", "reaction_channel_id", "command_channel_id",
        )
        migrated = next((value.get(key) for key in legacy_destinations if value.get(key)), "")
        if migrated:
            value = {**value, "channel_id": migrated}
    if len(value) > 200:
        raise ValueError("too many settings")
    for key, candidate in value.items():
        if key not in defaults:
            continue
        expected = defaults[key]
        if isinstance(expected, bool) and isinstance(candidate, bool):
            defaults[key] = candidate
        elif isinstance(expected, str) and isinstance(candidate, str):
            text = candidate.strip()
            if key == "prefix":
                if not text or (len(text) <= 8 and not any(char.isspace() for char in text)):
                    defaults[key] = text
            elif key.endswith("_id"):
                if not text or (text.isdigit() and len(text) <= 24):
                    defaults[key] = text
            else:
                defaults[key] = candidate[:4000]
        elif isinstance(expected, int) and not isinstance(expected, bool) and isinstance(candidate, int):
            defaults[key] = max(-1_000_000, min(1_000_000, candidate))
        elif isinstance(expected, float) and isinstance(candidate, (int, float)):
            defaults[key] = max(0.0, min(1000.0, float(candidate)))
        elif isinstance(expected, list) and isinstance(candidate, list):
            if key.endswith("_ids"):
                defaults[key] = list(dict.fromkeys(
                    str(item) for item in candidate
                    if str(item).isdigit() and len(str(item)) <= 24
                ))[:500]
            else:
                defaults[key] = candidate[:500]
        elif isinstance(expected, dict) and isinstance(candidate, dict):
            defaults[key] = dict(list(candidate.items())[:500])
    return defaults
