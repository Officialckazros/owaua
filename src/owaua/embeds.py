"""Embed helpers. Every user-facing message goes out as an embed, and all text
is run through de_emoji() so the bot never emits emoji (per design)."""

import datetime
import re
import typing
from urllib.parse import urlsplit

import discord

from owaua import config

_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U00002190-\U000021ff"
    "\U00002b00-\U00002bff"
    "\U0000fe00-\U0000fe0f"
    "\U00002000-\U0000200d"
    "\U000024c2\U00002122\U00003030"
    "]+",
    flags=re.UNICODE,
)


def de_emoji(text: str) -> str:
    if not text:
        return text
    text = _EMOJI.sub("", text)
    return re.sub(r"[ ]{2,}", " ", text).strip()


def fmt_ts(ts: typing.Any) -> str:
    """Format a unix timestamp (seconds) as UTC 'YYYY-MM-DD HH:MM'."""
    if not ts:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (ValueError, OSError, TypeError):
        return "?"


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _markdown_label(text: str) -> str:
    return re.sub(r"([\\\[\]\(\)])", r"\\\1", de_emoji(text or "source"))


def _safe_url(value: str) -> str | None:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return str(value)


def fit_total(embed: discord.Embed, maximum: int = 6000) -> discord.Embed:
    """Keep an embed under Discord's aggregate character limit."""
    overflow = len(embed) - maximum
    if overflow <= 0:
        return embed
    if embed.description:
        keep = max(0, len(embed.description) - overflow - 1)
        embed.description = _clip(embed.description, keep) if keep else None
    while len(embed) > maximum and embed.fields:
        fields = list(embed.fields)
        last = fields[-1]
        overflow = len(embed) - maximum
        keep = max(0, len(typing.cast(typing.Any, last.value)) - overflow - 1)
        if keep:
            embed.set_field_at(
                len(fields) - 1,
                name=last.name,
                value=_clip(typing.cast(typing.Any, last.value), keep),
                inline=last.inline,
            )
            break
        embed.remove_field(len(fields) - 1)
    if len(embed) > maximum and embed.footer.text:
        overflow = len(embed) - maximum
        embed.set_footer(text=_clip(embed.footer.text, max(1, len(embed.footer.text) - overflow)))
    return embed


def say(
    description: str,
    title: str | None = None,
    color: int | None = None,
    image: str | None = None,
    footer: str | None = None,
) -> discord.Embed:
    e = discord.Embed(
        title=_clip(de_emoji(title), 256) if title else None,
        description=_clip(de_emoji(description), 4096),
        color=color if color is not None else config.EMBED_COLOR,
    )
    if image:
        e.set_image(url=image)
    if footer:
        e.set_footer(text=_clip(de_emoji(footer), 2048))
    return fit_total(e)


def error(description: str) -> discord.Embed:
    return say(description, title="Error", color=0xED4245)


def ok(description: str, title: str | None = None) -> discord.Embed:
    return say(description, title=title, color=0x57F287)


def add_support_resources(embed: discord.Embed) -> discord.Embed:
    """Attach real crisis resources. Added by the bot, never left to the model."""
    embed.add_field(
        name="if you need someone right now",
        value=(
            "**US** — call or text **988** (Suicide & Crisis Lifeline)\n"
            "**US** — text **HOME** to **741741** (Crisis Text Line)\n"
            "**UK & ROI** — **116 123** (Samaritans)\n"
            "**Anywhere** — [findahelpline.com](https://findahelpline.com)\n"
            "If you're in immediate danger, please contact your local emergency number."
        ),
        inline=False,
    )
    return fit_total(embed)


def add_sources(embed: discord.Embed, sources: list[typing.Any]) -> discord.Embed:
    """Append just a clickable sources list (answer already woven into the reply)."""
    links: list[typing.Any] = []
    for i, s in enumerate(sources or [], 1):
        title = _markdown_label(s.get("title") or s.get("url") or "source")[:70]
        url = _safe_url(s.get("url"))
        if url:
            links.append(f"{i}. [{title}]({url})")
    if links:
        embed.add_field(name="sources", value=_clip("\n".join(links), 1024), inline=False)
    return fit_total(embed)


def add_search(embed: discord.Embed, res: dict[typing.Any, typing.Any]) -> discord.Embed:
    """Append grounded web-search results (answer + sources) to an existing embed."""
    ans = (res or {}).get("answer") or ""
    if ans:
        embed.add_field(name="from the web", value=_clip(de_emoji(ans), 1024), inline=False)
    links: list[typing.Any] = []
    for i, s in enumerate((res or {}).get("sources") or [], 1):
        title = _markdown_label(s.get("title") or s.get("url") or "source")[:70]
        url = _safe_url(s.get("url"))
        if url:
            links.append(f"{i}. [{title}]({url})")
    if links:
        embed.add_field(name="sources", value=_clip("\n".join(links), 1024), inline=False)
    return fit_total(embed)


def search(query: str, answer: str, sources: list[typing.Any]) -> discord.Embed:
    """Render a grounded web-search answer with a clickable sources list."""
    e = say(answer or "no answer.", title="web search")
    if query:
        e.set_footer(text=_clip(de_emoji(f"searched: {query}"), 2048))
    links: list[typing.Any] = []
    for i, s in enumerate(sources or [], 1):
        title = _markdown_label(s.get("title") or s.get("url") or "source")[:80]
        url = _safe_url(s.get("url"))
        if url:
            links.append(f"{i}. [{title}]({url})")
    if links:
        e.add_field(name="sources", value=_clip("\n".join(links), 1024), inline=False)
    return fit_total(e)
