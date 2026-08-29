"""Bounded Rule34 image search for the explicitly age-restricted NSFW command."""

from __future__ import annotations

import asyncio
import json
import math
import re
import typing
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import aiohttp

from owaua import config

API_URL: Final = "https://api.rule34.xxx/index.php"
MAX_IMAGES: Final = 10
_MAX_RESPONSE_BYTES: Final = 1_000_000
_CHARACTER_RE: Final = re.compile(r"[a-z0-9][a-z0-9_'().-]{0,99}")
_IMAGE_SUFFIXES: Final = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})
_MINOR_TAGS: Final = frozenset(
    {
        "child",
        "children",
        "infant",
        "kid",
        "kids",
        "loli",
        "lolicon",
        "minor",
        "minors",
        "preteen",
        "shota",
        "shotacon",
        "toddler",
        "underage",
        "young",
    }
)
_EXCLUSIONS: Final = " ".join(f"-{tag}" for tag in sorted(_MINOR_TAGS))


class Rule34Error(RuntimeError):
    """A safe, user-displayable Rule34 lookup failure."""


@dataclass(frozen=True, slots=True)
class Post:
    post_id: int
    image_url: str
    page_url: str


def normalize_character(value: str) -> str:
    """Turn a human-entered character label into one exact Rule34 tag."""
    tag = re.sub(r"\s+", "_", (value or "").strip().lower())
    if not _CHARACTER_RE.fullmatch(tag):
        raise Rule34Error(
            "use one character tag (letters, numbers, underscores, hyphens, or parentheses)."
        )
    if any(part in _MINOR_TAGS for part in re.split(r"[_'().-]+", tag)):
        raise Rule34Error("underage-related character searches are not allowed.")
    return tag


def _character_candidates(tag: str) -> tuple[str, ...]:
    """Try the common ``name_(series)`` form after a plain underscore tag."""
    if "(" not in tag and "_" in tag:
        name, series = tag.rsplit("_", 1)
        if name and series:
            return tag, f"{name}_({series})"
    return (tag,)


def validate_amount(value: int) -> int:
    try:
        amount = int(value)
    except (TypeError, ValueError) as exc:
        raise Rule34Error(f"amount must be between 1 and {MAX_IMAGES}.") from exc
    if not 1 <= amount <= MAX_IMAGES:
        raise Rule34Error(f"amount must be between 1 and {MAX_IMAGES}.")
    return amount


def is_age_restricted_channel(channel: object) -> bool:
    """Use Discord's live NSFW flag, including a thread's parent channel."""
    for candidate in (channel, getattr(channel, "parent", None)):
        if candidate is None:
            continue
        checker = getattr(candidate, "is_nsfw", None)
        if callable(checker):
            try:
                if checker():
                    return True
            except (AttributeError, TypeError):
                pass
        if bool(getattr(candidate, "nsfw", False)):
            return True
    return False


def _rule34_image_url(value: object) -> str | None:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower()
    suffix = next((item for item in _IMAGE_SUFFIXES if parsed.path.lower().endswith(item)), None)
    if (
        parsed.scheme != "https"
        or not hostname
        or not (hostname == "rule34.xxx" or hostname.endswith(".rule34.xxx"))
        or parsed.username
        or parsed.password
        or suffix is None
    ):
        return None
    return parsed.geturl()


def parse_posts(payload: object, amount: int) -> list[Post]:
    """Validate untrusted API JSON and return at most ``amount`` image posts."""
    wanted = validate_amount(amount)
    if not isinstance(payload, list):
        raise Rule34Error("Rule34 returned an unexpected response.")

    posts: list[Post] = []
    seen: set[str] = set()
    for item in typing.cast(typing.Iterable[typing.Any], payload):
        if not isinstance(item, dict):
            continue
        tags = {
            tag.casefold() for tag in str(typing.cast(typing.Any, item).get("tags") or "").split()
        }
        if tags & _MINOR_TAGS:
            continue
        try:
            post_id = int(typing.cast(typing.Any, item).get("id"))
        except (TypeError, ValueError):
            continue
        if post_id <= 0:
            continue
        image_url = _rule34_image_url(typing.cast(typing.Any, item).get("sample_url"))
        image_url = image_url or _rule34_image_url(typing.cast(typing.Any, item).get("file_url"))
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        posts.append(
            Post(
                post_id=post_id,
                image_url=image_url,
                page_url=f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}",
            )
        )
        if len(posts) >= wanted:
            break
    return posts


async def search(character: str, amount: int = 1) -> tuple[str, list[Post]]:
    """Fetch random still/animated images for one character tag."""
    tag = normalize_character(character)
    wanted = validate_amount(amount)
    if not config.RULE34_API_KEY or not config.RULE34_USER_ID:
        raise Rule34Error(
            "Rule34 access is not configured. Set OWAUA_RULE34_USER_ID and "
            "OWAUA_RULE34_API_KEY on the bot host."
        )

    timeout = aiohttp.ClientTimeout(total=12, connect=4)
    batch_limit = min(5, wanted)
    max_attempts = max(3, math.ceil(wanted / batch_limit) * 2)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for query_tag in _character_candidates(tag):
                posts: list[Post] = []
                seen: set[str] = set()
                for attempt in range(max_attempts):
                    params = {
                        "page": "dapi",
                        "s": "post",
                        "q": "index",
                        "json": "1",
                        "limit": str(batch_limit),
                        "tags": f"{query_tag} sort:random {_EXCLUSIONS}",
                        "user_id": config.RULE34_USER_ID,
                        "api_key": config.RULE34_API_KEY,
                    }
                    async with session.get(
                        API_URL,
                        params=params,
                        headers={"User-Agent": "owaua/2.0 (Rule34 age-restricted command)"},
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise Rule34Error("Rule34 is temporarily unavailable.")
                        raw = await response.content.read(_MAX_RESPONSE_BYTES + 1)
                        if len(raw) > _MAX_RESPONSE_BYTES:
                            raise Rule34Error("Rule34 returned too much data.")
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        # Rule34 occasionally emits invalid JSON for an otherwise
                        # successful request. Try a new small random batch instead.
                        payload: list[typing.Any] = []
                    for post in parse_posts(payload, batch_limit):
                        if post.image_url not in seen:
                            seen.add(post.image_url)
                            posts.append(post)
                            if len(posts) >= wanted:
                                return query_tag, posts
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(0.6)
                if posts:
                    return query_tag, posts[:wanted]
    except Rule34Error:
        raise
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise Rule34Error("Rule34 is temporarily unavailable.") from exc

    raise Rule34Error(f"no image posts found for `{tag}`.")
