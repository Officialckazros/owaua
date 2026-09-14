"""Voice-channel music: YouTube audio via yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess

import discord

log = logging.getLogger("owaua")
FFMPEG_BEFORE_OPTIONS = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 -thread_queue_size 1024"
)
FFMPEG_OPTIONS = "-vn"
YTDLP_FORMAT = (
    "bestaudio[acodec=opus][abr<=160]/"
    "bestaudio[acodec=opus]/"
    "bestaudio/best"
)

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


def ffmpeg_before_options(headers: object = None) -> str:
    """FFmpeg input flags that keep a YouTube stream from underrunning."""
    before = FFMPEG_BEFORE_OPTIONS
    if not isinstance(headers, dict) or not headers:
        return before
    packed = "".join(
        f"{key}: {value}\r\n"
        for key, value in headers.items()
        if key is not None and value is not None
    )
    if not packed:
        return before
    return f"{before} -headers {shlex.quote(packed)}"


def opus_codec(acodec: object = None) -> str | None:
    """Copy existing Opus instead of re-encoding it on the VPS."""
    name = str(acodec or "").split(".")[0].casefold().strip()
    return "copy" if name == "opus" else None


def play_track(
    voice_client: discord.VoiceClient, track: dict[str, object]
) -> None:
    source = discord.FFmpegOpusAudio(
        str(track["url"]),
        bitrate=96,
        codec=opus_codec(track.get("acodec")),
        before_options=ffmpeg_before_options(track.get("http_headers")),
        options=FFMPEG_OPTIONS,
        stderr=subprocess.DEVNULL,
    )
    voice_client.play(source, after=_playback_finished)


def _playback_finished(error: Exception | None) -> None:
    if error is not None:
        log.warning("Music playback failed: %s", error)


def _http_headers(info: object) -> dict[str, str]:
    if not isinstance(info, dict):
        return {}
    headers = info.get("http_headers")
    if not isinstance(headers, dict):
        return {}
    packed: dict[str, str] = {}
    for key, value in headers.items():
        if key is None or value is None:
            continue
        packed[str(key)] = str(value)
    return packed


async def resolve_music(query: str) -> dict[str, object]:
    import yt_dlp

    lookup = (
        query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    )
    options = {
        "format": YTDLP_FORMAT,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "logger": _QuietYTDlpLogger(),
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 15,
        "cachedir": False,
    }

    def extract() -> dict[str, object]:
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
                "acodec": str(info.get("acodec") or ""),
                "http_headers": _http_headers(info),
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
        try:
            voice_client = await target_channel.connect(self_deaf=True)
        except TypeError:
            voice_client = await target_channel.connect()
    elif voice_client.channel.id != target_channel.id:
        await voice_client.move_to(target_channel)
    await _self_deafen(voice_client)
    return voice_client, None


async def _self_deafen(voice_client: object) -> None:
    """Stop decoding everyone else's voice while we play music."""
    guild = getattr(voice_client, "guild", None)
    channel = getattr(voice_client, "channel", None)
    change = getattr(guild, "change_voice_state", None)
    if channel is None or not callable(change):
        return
    try:
        await change(channel=channel, self_deaf=True)
    except Exception:
        log.debug("Could not self-deafen for music", exc_info=True)


async def handle_music_command(
    bot: object, message: discord.Message, argument: str
) -> str:
    if message.guild is None:
        return "!music only works in a server voice channel"
    guild_id = message.guild.id
    action = argument.strip()
    action_lower = action.casefold()
    voice_client = message.guild.voice_client
    tracks: dict[int, dict[str, object]] = bot.music_tracks  # type: ignore[attr-defined]

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
        return await _play_or_restart(
            bot, message, str(track["query"]), verb="playing"
        )
    if action_lower == "restart":
        track = tracks.get(guild_id)
        if track is None:
            return "choose a song first with `!music <song or URL>`"
        return await _play_or_restart(
            bot, message, str(track["query"]), verb="restarted"
        )

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
