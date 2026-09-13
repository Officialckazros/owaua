"""Voice-channel music: YouTube audio via yt-dlp."""

from __future__ import annotations

import asyncio
import logging

import discord

log = logging.getLogger("owaua")

MUSIC_USAGE = (
    "usage: !music <song or URL> | !music start | !music pause | "
    "!music resume | !music restart | !music stop | !music skip | "
    "!music leave | !music now"
)


class _QuietYTDlpLogger:
    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def music_error_reply(action: str, error: Exception) -> str:
    details = str(error).casefold()
    if "available to this channel's members" in details or "members-only" in details:
        return (
            "I couldn't play that: the YouTube video is members-only. "
            "Please use a public video or join the required channel membership."
        )
    return f"I couldn't {action}: {type(error).__name__}"


def missing_voice_permissions(channel: object, bot_user: object | None) -> list[str]:
    if getattr(channel, "guild", None) is None:
        return []
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return []
    guild = channel.guild
    me = getattr(guild, "me", None)
    if me is None and bot_user is not None:
        get_member = getattr(guild, "get_member", None)
        if callable(get_member):
            me = get_member(getattr(bot_user, "id", None))
    if me is None:
        return []
    try:
        perms = permissions_for(me)
    except (AttributeError, TypeError):
        return []
    missing: list[str] = []
    if not getattr(perms, "connect", False):
        missing.append("Connect")
    if not getattr(perms, "speak", False):
        missing.append("Speak")
    return missing


def permission_reply(missing: list[str]) -> str:
    if len(missing) == 1:
        return f"I need the {missing[0]} permission in this channel"
    return (
        "I need "
        + ", ".join(missing[:-1])
        + f", and {missing[-1]} in this channel"
    )


def play_track(voice_client: discord.VoiceClient, track: dict[str, str]) -> None:
    source = discord.FFmpegPCMAudio(
        track["url"],
        before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        options="-vn",
    )
    voice_client.play(source, after=_playback_finished)


def _playback_finished(error: Exception | None) -> None:
    if error is not None:
        log.warning("Music playback failed: %s", error)


async def resolve_music(query: str) -> dict[str, str]:
    import yt_dlp

    lookup = (
        query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    )
    options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietYTDlpLogger(),
    }

    def extract() -> dict[str, str]:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(lookup, download=False)
            if "entries" in info:
                info = next((entry for entry in info["entries"] if entry), None)
            if not info or not info.get("url"):
                raise ValueError("no playable audio found")
            return {
                "title": str(info.get("title", "unknown track")),
                "url": str(info["url"]),
                "query": str(info.get("webpage_url") or query),
            }

    return await asyncio.to_thread(extract)


async def _connect_to_author(
    message: discord.Message, bot_user: object | None
) -> tuple[discord.VoiceClient | None, str | None]:
    if message.author.voice is None or message.author.voice.channel is None:
        return None, None
    target_channel = message.author.voice.channel
    missing = missing_voice_permissions(target_channel, bot_user)
    if missing:
        return None, permission_reply(missing)
    voice_client = message.guild.voice_client
    if voice_client is None:
        voice_client = await target_channel.connect()
    elif voice_client.channel.id != target_channel.id:
        await voice_client.move_to(target_channel)
    return voice_client, None


async def handle_music_command(
    bot: object, message: discord.Message, argument: str
) -> str:
    if message.guild is None:
        return "!music only works in a server voice channel"
    guild_id = message.guild.id
    action = argument.strip()
    action_lower = action.casefold()
    voice_client = message.guild.voice_client
    tracks: dict[int, dict[str, str]] = bot.music_tracks  # type: ignore[attr-defined]

    if action_lower in {"help", ""}:
        return MUSIC_USAGE
    if action_lower in {"leave", "disconnect"}:
        if voice_client is None:
            return "I am not in a voice channel"
        voice_client.stop()
        await voice_client.disconnect()
        tracks.pop(guild_id, None)
        return "left the music voice channel"
    if action_lower == "now":
        track = tracks.get(guild_id)
        return f"now playing: {track['title']}" if track else "nothing is queued"
    if action_lower == "pause":
        if voice_client is not None and voice_client.is_playing():
            voice_client.pause()
            return "music paused"
        return "nothing is playing"
    if action_lower in {"stop", "skip"}:
        if voice_client is not None and (
            voice_client.is_playing() or voice_client.is_paused()
        ):
            voice_client.stop()
            return "music stopped" if action_lower == "stop" else "skipped"
        return "nothing is playing"
    if action_lower in {"start", "resume"}:
        track = tracks.get(guild_id)
        if voice_client is not None and voice_client.is_paused():
            voice_client.resume()
            return f"resumed: {track['title']}" if track else "music resumed"
        if track is None:
            return "choose a song first with `!music <song or URL>`"
        return await _play_or_restart(bot, message, track["query"], verb="playing")
    if action_lower == "restart":
        track = tracks.get(guild_id)
        if track is None:
            return "choose a song first with `!music <song or URL>`"
        return await _play_or_restart(bot, message, track["query"], verb="restarted")

    return await _play_or_restart(bot, message, action, verb="playing")


async def _play_or_restart(
    bot: object,
    message: discord.Message,
    query: str,
    *,
    verb: str,
) -> str:
    if message.author.voice is None or message.author.voice.channel is None:
        if verb == "restarted":
            hint = "`!music restart`"
        elif verb == "playing":
            hint = "`!music <song or URL>`"
        else:
            hint = "`!music start`"
        return f"join a voice channel first, then use {hint}"
    try:
        voice_client, error = await _connect_to_author(
            message, getattr(bot, "user", None)
        )
        if error is not None:
            return error
        assert voice_client is not None
        track = await resolve_music(query)
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        bot.music_tracks[message.guild.id] = track  # type: ignore[attr-defined]
        play_track(voice_client, track)
        return f"{verb}: {track['title']}"
    except (
        discord.ClientException,
        discord.Forbidden,
        discord.HTTPException,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        log.warning("Could not %s music: %s", verb, type(exc).__name__)
        action = "play that" if verb == "playing" else f"{verb.rstrip('ed')} music"
        if verb == "restarted":
            action = "restart that"
        return music_error_reply(action, exc)
