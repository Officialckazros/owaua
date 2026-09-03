import datetime
import re
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


def fmt_ts(ts) -> str:
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


def _safe_url(value: str | None) -> str | None:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return str(value)


def _source_lines(sources, n=70):
    lines = []
    for i, s in enumerate(sources or [], 1):
        title = _markdown_label(s.get("title") or s.get("url") or "source")[:n]
        url = _safe_url(s.get("url"))
        if url:
            lines.append(f"{i}. [{title}]({url})")
    return lines


def fit_total(embed: discord.Embed, maximum: int = 6000) -> discord.Embed:
    # discord kills embeds over 6000
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
        keep = max(0, len(last.value) - overflow - 1)
        if keep:
            embed.set_field_at(
                len(fields) - 1,
                name=last.name,
                value=_clip(last.value, keep),
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


def partnership(item: dict) -> discord.Embed:
    """Build a complete, bounded Discord embed from a partnership record."""
    def text(key: str, limit: int) -> str:
        return _clip(str(item.get(key) or "").strip(), limit)

    color = item.get("color", "5865f2")
    try:
        color_value = int(str(color).replace("#", ""), 16)
    except (TypeError, ValueError):
        color_value = config.EMBED_COLOR
    e = discord.Embed(
        title=text("title", 256) or text("name", 256) or "Partnership",
        description=text("description", 4096) or None,
        url=_safe_url(text("url", 2048)) if text("url", 2048) else None,
        color=max(0, min(0xFFFFFF, color_value)),
    )
    author_name = text("author_name", 256) or text("name", 256)
    author_url = _safe_url(text("author_url", 2048)) if text("author_url", 2048) else None
    author_icon = _safe_url(text("author_icon_url", 2048)) if text("author_icon_url", 2048) else None
    if author_name:
        e.set_author(name=author_name, url=author_url, icon_url=author_icon)
    thumbnail = _safe_url(text("thumbnail_url", 2048)) if text("thumbnail_url", 2048) else None
    image = _safe_url(text("image_url", 2048)) if text("image_url", 2048) else None
    if thumbnail:
        e.set_thumbnail(url=thumbnail)
    if image:
        e.set_image(url=image)
    footer = text("footer_text", 2048)
    footer_icon = _safe_url(text("footer_icon_url", 2048)) if text("footer_icon_url", 2048) else None
    if footer:
        e.set_footer(text=footer, icon_url=footer_icon)
    if item.get("timestamp") is True:
        e.timestamp = datetime.datetime.now(datetime.timezone.utc)
    for field in item.get("fields", []):
        if not isinstance(field, dict):
            continue
        name = _clip(str(field.get("name") or "Field"), 256)
        value = _clip(str(field.get("value") or "—"), 1024)
        e.add_field(name=name, value=value, inline=bool(field.get("inline", False)))
    return fit_total(e)


def error(description: str) -> discord.Embed:
    return say(description, title="Error", color=0xED4245)


def ok(description: str, title: str | None = None) -> discord.Embed:
    return say(description, title=title, color=0x57F287)


def add_support_resources(embed: discord.Embed) -> discord.Embed:
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


def add_sources(embed: discord.Embed, sources) -> discord.Embed:
    links = _source_lines(sources)
    if links:
        embed.add_field(name="sources", value=_clip("\n".join(links), 1024), inline=False)
    return fit_total(embed)


def add_search(embed: discord.Embed, res) -> discord.Embed:
    res = res or {}
    ans = res.get("answer") or ""
    if ans:
        embed.add_field(name="from the web", value=_clip(de_emoji(ans), 1024), inline=False)
    return add_sources(embed, res.get("sources"))


def search(query: str, answer: str, sources) -> discord.Embed:
    e = say(answer or "no answer.", title="web search")
    if query:
        e.set_footer(text=_clip(de_emoji(f"searched: {query}"), 2048))
    links = _source_lines(sources, 80)
    if links:
        e.add_field(name="sources", value=_clip("\n".join(links), 1024), inline=False)
    return fit_total(e)
