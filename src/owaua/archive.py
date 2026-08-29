"""Permanent, text-only Discord history for explicitly allowlisted guilds."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import discord

from owaua import config, db, embeds
from owaua.scope import Scope

_LOG = logging.getLogger(__name__)
_CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,22}>")
_BATCH_SIZE = 250
_RESCAN_SECONDS = 6 * 60 * 60
_TEXT_FORMAT_VERSION = "1"


def enabled_guild(guild_id: object) -> bool:
    return str(guild_id or "") in config.ARCHIVE_GUILD_IDS


def text_only(content: str) -> str:
    """Remove custom/Unicode emoji while preserving ordinary message text."""
    value = _CUSTOM_EMOJI.sub("", str(content or ""))
    value = embeds.de_emoji(value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()[:2000].rstrip()


def _record(message: discord.Message, content: str) -> dict:
    author = message.author
    created = getattr(message, "created_at", None)
    return {
        "message_id": str(message.id),
        "guild_name": str(getattr(message.guild, "name", "Unknown")),
        "user_id": str(author.id),
        "username": str(getattr(author, "name", author.id)),
        "display_name": str(
            getattr(author, "display_name", None)
            or getattr(author, "name", author.id)
        ),
        "content": content,
        "created_at": created.timestamp() if created is not None else None,
    }


async def store_live_message(message: discord.Message, *, edited: bool = False) -> bool:
    """Persist one live message without attachments, embeds, stickers, or emoji."""
    guild = message.guild
    if guild is None or not enabled_guild(guild.id):
        return False
    scope_id = Scope.guild(guild.id).key
    content = text_only(message.content or "")
    if not content:
        if edited:
            await asyncio.to_thread(db.remove_archived_message, scope_id, str(message.id))
        return False
    record = _record(message, content)
    await asyncio.to_thread(
        db.record_server_message,
        record["message_id"],
        scope_id,
        record["guild_name"],
        str(message.channel.id),
        str(getattr(message.channel, "name", "unknown")),
        record["user_id"],
        record["username"],
        record["display_name"],
        record["content"],
        force=True,
        created_at=record["created_at"],
    )
    return True


async def _archived_threads(parent: Any) -> AsyncIterator[discord.Thread]:
    method = getattr(parent, "archived_threads", None)
    if method is None:
        return
    variants: list[dict[str, bool]] = [{}]
    if isinstance(parent, discord.TextChannel):
        variants.extend(
            [
                {"private": True, "joined": True},
                {"private": True, "joined": False},
            ]
        )
    for options in variants:
        try:
            async for thread in method(limit=None, **options):
                yield thread
        except (discord.Forbidden, discord.HTTPException):
            continue


async def message_channels(guild: discord.Guild) -> AsyncIterator[Any]:
    """Yield every accessible history-bearing channel and thread once."""
    seen: set[int] = set()
    for channel in guild.channels:
        if hasattr(channel, "history") and channel.id not in seen:
            seen.add(channel.id)
            yield channel
    for thread in guild.threads:
        if thread.id not in seen:
            seen.add(thread.id)
            yield thread
    for parent in guild.channels:
        async for thread in _archived_threads(parent):
            if thread.id not in seen:
                seen.add(thread.id)
                yield thread


async def backfill_channel(guild: discord.Guild, channel: Any) -> dict[str, int | str]:
    """Resume one channel from its durable cursor and finish its current history."""
    scope_id = Scope.guild(guild.id).key
    channel_id = str(channel.id)
    channel_name = str(getattr(channel, "name", "unknown"))
    cursor = await asyncio.to_thread(db.archive_channel_cursor, scope_id, channel_id)
    after = None
    if cursor and str(cursor.get("last_message_id") or "").isdigit():
        after = discord.Object(id=int(cursor["last_message_id"]))

    records: list[dict] = []
    seen_in_batch = 0
    last_message_id: str | None = None
    saved = 0
    scanned = 0

    async def flush(*, complete: bool, error: str | None = None) -> None:
        nonlocal records, seen_in_batch, saved
        saved += await asyncio.to_thread(
            db.record_archived_message_batch,
            scope_id,
            channel_id,
            channel_name,
            records,
            last_message_id=last_message_id,
            messages_seen=seen_in_batch,
            complete=complete,
            error=error,
        )
        records = []
        seen_in_batch = 0

    try:
        async for message in channel.history(
            limit=None, after=after, oldest_first=True
        ):
            scanned += 1
            seen_in_batch += 1
            last_message_id = str(message.id)
            content = text_only(message.content or "")
            records.append(_record(message, content))
            if seen_in_batch >= _BATCH_SIZE:
                await flush(complete=False)
        await flush(complete=True)
    except (discord.Forbidden, discord.HTTPException) as exc:
        if seen_in_batch:
            await flush(complete=False)
        await asyncio.to_thread(
            db.record_archived_message_batch,
            scope_id,
            channel_id,
            channel_name,
            [],
            last_message_id=None,
            messages_seen=0,
            complete=False,
            error=f"{type(exc).__name__}: history unavailable",
        )
        _LOG.warning("archive could not read guild=%s channel=%s", guild.id, channel.id)
    return {"channel_id": channel_id, "scanned": scanned, "saved": saved}


async def backfill_guild(guild: discord.Guild) -> dict[str, int | str]:
    if not enabled_guild(guild.id):
        return {"guild_id": str(guild.id), "channels": 0, "scanned": 0, "saved": 0}
    format_key = f"archive:text-format:{guild.id}"
    format_version = await asyncio.to_thread(db.kv_get, format_key)
    if format_version != _TEXT_FORMAT_VERSION:
        result = await asyncio.to_thread(
            db.normalize_archived_message_text,
            Scope.guild(guild.id).key,
            text_only,
        )
        await asyncio.to_thread(db.kv_set, format_key, _TEXT_FORMAT_VERSION)
        _LOG.info(
            "archive normalization guild=%s updated=%s deleted=%s",
            guild.id,
            result["updated"],
            result["deleted"],
        )
    channels = 0
    scanned = 0
    saved = 0
    async for channel in message_channels(guild):
        channels += 1
        result = await backfill_channel(guild, channel)
        scanned += int(result["scanned"])
        saved += int(result["saved"])
    totals: dict[str, int | str] = {
        "guild_id": str(guild.id),
        "channels": channels,
        "scanned": scanned,
        "saved": saved,
    }
    _LOG.info(
        "archive pass complete guild=%s channels=%s scanned=%s text_saved=%s",
        guild.id,
        totals["channels"],
        totals["scanned"],
        totals["saved"],
    )
    return totals


async def archive_loop(client: discord.Client) -> None:
    """Backfill on startup, then discover missed/new channels every six hours."""
    await client.wait_until_ready()
    while not client.is_closed():
        for guild_id in sorted(config.ARCHIVE_GUILD_IDS):
            guild = client.get_guild(int(guild_id))
            if guild is None:
                _LOG.error("configured archive guild %s is not connected", guild_id)
                continue
            try:
                await backfill_guild(guild)
            except Exception:
                _LOG.exception("archive pass failed for guild %s", guild_id)
        await asyncio.sleep(_RESCAN_SECONDS)
