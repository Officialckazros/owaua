"""Safe music discovery.

owaua deliberately does not download, convert, or redistribute media. The
command returns a normal YouTube search URL that the user may open themselves.
"""

from __future__ import annotations

from typing import Optional, Tuple
from urllib.parse import quote_plus, urlsplit


def _safe_youtube_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in {
            "youtube.com", "www.youtube.com", "youtu.be", "music.youtube.com"
        }
        and not parsed.username
        and not parsed.password
    )

async def search_song(query: str) -> Tuple[Optional[dict], Optional[str]]:
    """Return a validated search link without fetching or downloading media."""
    clean = " ".join((query or "").split())
    if not clean:
        return None, "give me a song name"
    if len(clean) > 200:
        return None, "query is too long (max 200 characters)"
    url = f"https://www.youtube.com/results?search_query={quote_plus(clean)}"
    if not _safe_youtube_url(url):
        return None, "couldn't build a safe music link"
    return {
        "title": clean,
        "uploader": "YouTube search",
        "duration": None,
        "url": url,
        "search_only": True,
    }, None


def format_duration(seconds) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "?"
    minutes, seconds = divmod(max(0, total), 60)
    hours, minutes = divmod(minutes, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}"
        if hours
        else f"{minutes}:{seconds:02d}"
    )
