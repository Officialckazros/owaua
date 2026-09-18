"""Operator-controlled limits. Invalid configuration fails closed at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("OWAUA_ENV_FILE") or Path(__file__).resolve().parents[2] / ".env")


def limit(name: str, default: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")
    return value


def ids(name: str, default: str = "") -> frozenset[int]:
    values = frozenset(int(value.strip()) for value in os.getenv(name, default).split(",") if value.strip())
    if any(value <= 0 for value in values):
        raise ValueError(f"{name} must contain positive Discord IDs")
    return values


OWNER_IDS = ids("BOT_OWNER_IDS", "1172433512364769342")
BLOCKED_USERS = ids("BOT_BLOCKED_USER_IDS")
ALLOWED_GUILDS = ids("BOT_ALLOWED_GUILD_IDS")
ALLOW_DMS = limit("BOT_ALLOW_DMS", 0, 1) == 1
MAX_INFLIGHT = limit("BOT_MAX_INFLIGHT", 3, 16)
MAX_INPUT_CHARS = 2000
MAX_REPLY_CHARS = 5700
MAX_TRACKED_USERS = 4096


@dataclass(frozen=True)
class ApiLimits:
    per_minute: int = limit("API_REQUESTS_PER_MINUTE", 12, 120)
    per_user_day: int = limit("API_REQUESTS_PER_USER_DAY", 30, 1000)
    per_guild_day: int = limit("API_REQUESTS_PER_GUILD_DAY", 100, 5000)
    per_day: int = limit("API_REQUESTS_PER_DAY", 200, 10000)
    lifetime: int = limit("API_REQUESTS_LIFETIME", 1000, 1000000)


API_LIMITS = ApiLimits()

# Full mode is still bounded by the shared guild, daily, and lifetime ceilings,
# but people on the explicit full-mode allowlist get a little more room in the
# two per-user dimensions. This is intentionally finite and configurable.
FULL_MODE_API_LIMITS = ApiLimits(
    per_minute=limit("FULL_MODE_REQUESTS_PER_MINUTE", 24, 240),
    per_user_day=limit("FULL_MODE_REQUESTS_PER_USER_DAY", 100, 5000),
    per_guild_day=API_LIMITS.per_guild_day,
    per_day=API_LIMITS.per_day,
    lifetime=API_LIMITS.lifetime,
)


class BudgetExceeded(RuntimeError):
    """No provider request may be made after this exception."""


class DuplicateRequest(RuntimeError):
    pass
