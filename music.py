"""Voice-channel music from YouTube and Discord file attachments."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
from pathlib import Path

import httpx
import ipaddress
import logging
import mimetypes
import re
import shlex
import subprocess
from pathlib import PurePath
from urllib.parse import parse_qs, urlparse

import discord

log = logging.getLogger("owaua")
FFMPEG_BEFORE_OPTIONS = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 -thread_queue_size 1024 "
    "-protocol_whitelist pipe"
)
FFMPEG_OPTIONS = "-vn -threads 1 -t 900"
MAX_MEDIA_BYTES = 20 * 1024 * 1024
MAX_MUSIC_JOBS = 2
_ACTIVE_SOURCES: set[object] = set()
YTDLP_FORMAT = (
    "bestaudio[acodec=opus][abr<=160]/"
    "bestaudio[acodec=opus]/"
    "bestaudio/best"
)
YTDLP_EXTRACTORS = ["youtube", "youtube:search"]
YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
    }
)
DISCORD_CDN_HOSTS = frozenset(
    {
        "cdn.discordapp.com",
        "media.discordapp.net",
        "cdn.discord.com",
    }
)
PLAYLIST_EXTENSIONS = frozenset(
    {
        ".m3u",
        ".m3u8",
        ".pls",
        ".xspf",
        ".asx",
        ".cue",
        ".wpl",
        ".ram",
        ".smil",
    }
)
PLAYLIST_TYPES = frozenset(
    {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
        "audio/x-mpegurl",
        "audio/x-scpls",
        "application/vnd.ms-wpl",
        "application/xspf+xml",
    }
)
AUDIO_EXTENSIONS = frozenset(
    {
        ".mp3",
        ".flac",
        ".ogg",
        ".opus",
        ".wav",
        ".m4a",
        ".aac",
        ".wma",
        ".weba",
        ".aiff",
        ".aif",
        ".oga",
        ".mp2",
        ".ac3",
        ".xm",
        ".it",
        ".mod",
        ".s3m",
        ".nsf",
        ".spc",
        ".vgm",
        ".vgz",
        ".mp4",
        ".webm",
        ".mkv",
        ".mov",
        ".m4v",
    }
)

MUSIC_USAGE = (
    "usage: !music <song or YouTube URL> or attach an audio file | !music start | "
    "!music pause | !music resume | !music restart | !music stop | "
    "!music skip | !music leave | !music now"
)
NON_YOUTUBE_URL_REPLY = "only YouTube links or a song name work"
PLAYLIST_URL_REPLY = (
    "that YouTube link is a playlist or channel; send a video or search by name"
)
UNSAFE_STREAM_REPLY = "no playable audio found"
LIVE_STREAM_REPLY = "live streams and 24/7 radios aren't allowed"
LONG_TRACK_REPLY = "that video is too long; send a single song under 15 minutes"
MAX_TRACK_SECONDS = 15 * 60

GENERIC_ATTACHMENT_TYPES = {
    "application/octet-stream",
    "binary/octet-stream",
}


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
    if isinstance(error, ValueError):
        message = str(error).strip()
        if message:
            return message
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


def _safe_header_part(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text or any(ch in text for ch in "\r\n\x00"):
        return None
    return text


def ffmpeg_before_options(headers: object = None) -> str:
    """FFmpeg input flags that keep a remote audio stream from underrunning."""
    before = FFMPEG_BEFORE_OPTIONS
    if not isinstance(headers, dict) or not headers:
        return before
    packed = "".join(
        f"{key}: {value}\r\n"
        for key, value in (
            (_safe_header_part(raw_key), _safe_header_part(raw_value))
            for raw_key, raw_value in headers.items()
        )
        if key is not None and value is not None
    )
    if not packed:
        return before
    return f"{before} -headers {shlex.quote(packed)}"


def opus_codec(acodec: object = None) -> str | None:
    """Copy existing Opus instead of re-encoding it on the VPS."""
    name = str(acodec or "").split(".")[0].casefold().strip()
    return "copy" if name == "opus" else None


def safe_http_url(url: object) -> bool:
    """True when FFmpeg can be given this as an HTTP(S) input and nothing else."""
    raw = str(url or "").strip()
    if not raw or any(ch in raw for ch in "\r\n\x00|"):
        return False
    if raw.startswith("-"):
        return False
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or port not in (None, 443):
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or "").casefold()
    if not host:
        return False
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    ):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def discord_cdn_url(url: object) -> bool:
    if not safe_http_url(url):
        return False
    host = (urlparse(str(url).strip()).hostname or "").casefold()
    return host in DISCORD_CDN_HOSTS


def _youtube_host(host: str) -> bool:
    return host.casefold().removeprefix("www.") in YOUTUBE_HOSTS


def youtube_video_id(query: str) -> str | None:
    """Return the 11-character video id from a YouTube watch/share URL."""
    parsed = urlparse(query.strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    host = parsed.hostname or ""
    if not _youtube_host(host):
        return None
    host_key = host.casefold().removeprefix("www.")
    parts = [part for part in (parsed.path or "").split("/") if part]
    params = parse_qs(parsed.query)
    if host_key == "youtu.be":
        candidate = parts[0] if parts else ""
    elif parts and parts[0] in {"embed", "shorts", "live", "v"} and len(parts) >= 2:
        candidate = parts[1]
    else:
        candidate = (params.get("v") or [""])[0]
    candidate = candidate.strip()
    if YOUTUBE_VIDEO_ID.fullmatch(candidate):
        return candidate
    return None


def accepted_youtube_track(info: object) -> None:
    """Reject livestreams and long mixes that keep playing after the requester leaves."""
    if not isinstance(info, dict):
        raise ValueError(UNSAFE_STREAM_REPLY)
    if info.get("is_live") is True:
        raise ValueError(LIVE_STREAM_REPLY)
    live_status = str(info.get("live_status") or "").casefold()
    if live_status in {"is_live", "is_upcoming"}:
        raise ValueError(LIVE_STREAM_REPLY)
    duration = info.get("duration")
    if duration is None:
        raise ValueError(LONG_TRACK_REPLY)
    try:
        seconds = int(duration)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(LONG_TRACK_REPLY)
    if not 0 < seconds <= MAX_TRACK_SECONDS:
        raise ValueError(LONG_TRACK_REPLY)


def music_lookup(query: str) -> str:
    """Turn a user request into a single-video yt-dlp lookup."""
    raw = query.strip()
    if len(raw) > 500:
        raise ValueError("Song query is too long")
    if not raw:
        raise ValueError(MUSIC_USAGE)
    first = raw.split()[0]
    looks_like_url = "://" in first or first.casefold().startswith(("http://", "https://"))
    if looks_like_url:
        video_id = youtube_video_id(raw) or youtube_video_id(first)
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
        parsed = urlparse(first)
        if parsed.scheme in {"http", "https"} and _youtube_host(parsed.hostname or ""):
            raise ValueError(PLAYLIST_URL_REPLY)
        raise ValueError(NON_YOUTUBE_URL_REPLY)
    return f"ytsearch1:{raw}"


def attachment_track(attachment: object) -> dict[str, object] | None:
    """Build a direct FFmpeg track for an attached audio file."""
    url = str(getattr(attachment, "url", "") or "").strip()
    if not discord_cdn_url(url):
        return None

    filename = PurePath(str(getattr(attachment, "filename", "") or "").strip()).name
    suffix = PurePath(filename).suffix.casefold()
    size = getattr(attachment, "size", 0)
    if not isinstance(size, int) or not 0 < size <= MAX_MEDIA_BYTES:
        return None
    if suffix not in {".mp3", ".wav", ".ogg", ".opus", ".flac", ".m4a", ".mp4", ".webm"}:
        return None
    if suffix in PLAYLIST_EXTENSIONS:
        return None

    content_type = str(getattr(attachment, "content_type", "") or "")
    content_type = content_type.partition(";")[0].casefold().strip()
    guessed_type, _encoding = mimetypes.guess_type(filename)
    guessed_type = (guessed_type or "").casefold()
    if content_type in PLAYLIST_TYPES or guessed_type in PLAYLIST_TYPES:
        return None
    is_media = content_type.startswith(
        ("audio/", "video/")
    ) or guessed_type.startswith(
        ("audio/", "video/")
    )
    is_generic = not content_type or content_type in GENERIC_ATTACHMENT_TYPES
    if not is_media and not (is_generic and suffix in AUDIO_EXTENSIONS):
        return None

    title = filename if filename else "attached audio"
    return {
        "title": title,
        "url": url,
        "query": url,
        "acodec": "",
        "http_headers": {},
        "source": "attachment",
    }


def attached_music_track(message: object) -> dict[str, object] | None:
    """Return the first playable-looking attachment on a message."""
    for attachment in getattr(message, "attachments", ()) or ():
        track = attachment_track(attachment)
        if track is not None:
            return track
    return None


def media_format(data: bytes) -> str:
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mov"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 255 and data[1] & 0xE0 == 0xE0):
        return "mp3"
    raise ValueError("Unsupported audio container")


class BoundedAudio(discord.FFmpegOpusAudio):
    def __init__(self, data: bytes) -> None:
        self._cleanup_lock = threading.RLock()
        self._deadline = None
        if not sys.platform.startswith("linux"):
            raise ValueError("Hardened music playback requires a Linux host")
        if len(_ACTIVE_SOURCES) >= MAX_MUSIC_JOBS:
            raise ValueError("Music is busy; try later")
        super().__init__(
            io.BytesIO(data), pipe=True, bitrate=96,
            before_options=f"-nostdin -threads 1 -protocol_whitelist pipe -f {media_format(data)}",
            options=FFMPEG_OPTIONS,
        )
        _ACTIVE_SOURCES.add(self)
        self._deadline = threading.Timer(MAX_TRACK_SECONDS + 15, self.cleanup)
        self._deadline.daemon = True
        self._deadline.start()

    def _spawn_process(self, args, **kwargs):
        # Native media parsers must not inherit Discord/provider credentials.
        kwargs["env"] = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
        kwargs["stderr"] = subprocess.DEVNULL
        command = [sys.executable, "-I", str(Path(__file__).with_name("media_exec.py")), *args]
        return super()._spawn_process(command, **kwargs)

    def cleanup(self) -> None:
        with self._cleanup_lock:
            timer = self._deadline
            if timer is not None:
                timer.cancel()
            try:
                process = getattr(self, "_process", None)
                if process:
                    super().cleanup()
            finally:
                if process:
                    for stream in (process.stdout, process.stdin, process.stderr):
                        if stream is not None:
                            stream.close()
                    process.wait(timeout=2)
                _ACTIVE_SOURCES.discard(self)


def play_track(voice_client: discord.VoiceClient, track: dict[str, object]) -> None:
    data = track.get("audio_bytes")
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_MEDIA_BYTES:
        raise ValueError("Audio must be downloaded within the size limit first")
    source = BoundedAudio(data)
    try:
        voice_client.play(source, after=track.get("_after", _playback_finished))
    except BaseException:
        source.cleanup()
        raise


async def download_audio(track: dict[str, object]) -> bytes:
    url = str(track.get("url", ""))
    if not safe_http_url(url):
        raise ValueError(UNSAFE_STREAM_REPLY)
    host = (urlparse(url).hostname or "").lower()
    if track.get("source") == "attachment":
        allowed = discord_cdn_url(url)
    else:
        allowed = host.endswith(".googlevideo.com")
    if not allowed:
        raise ValueError(UNSAFE_STREAM_REPLY)
    chunks = bytearray()
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=8) as client:
        async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise ValueError("Could not download audio")
            encoding = response.headers.get("content-encoding", "identity")
            if encoding != "identity":
                raise ValueError("Compressed HTTP responses are unsupported")
            async for chunk in response.aiter_raw():
                if len(chunks) + len(chunk) > MAX_MEDIA_BYTES:
                    raise ValueError("Audio exceeds the 20 MiB limit")
                chunks.extend(chunk)
    data = bytes(chunks)
    media_format(data)
    return data


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
        safe_key = _safe_header_part(key)
        safe_value = _safe_header_part(value)
        if safe_key is None or safe_value is None:
            continue
        packed[safe_key] = safe_value
    return packed


async def resolve_music(query: str) -> dict[str, object]:
    lookup = music_lookup(query)
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "SSL_CERT_FILE") if key in os.environ}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(Path(__file__).with_name("music_worker.py")), lookup,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        if process.returncode != 0 or len(output) > 40000:
            raise ValueError("Could not resolve that song")
        info = json.loads(output)
        accepted_youtube_track(info)
        stream = str(info.get("url", ""))
        if not safe_http_url(stream) or not (urlparse(stream).hostname or "").endswith(".googlevideo.com"):
            raise ValueError(UNSAFE_STREAM_REPLY)
        return {"title": str(info.get("title", "unknown track"))[:200], "url": stream,
                "query": query, "acodec": str(info.get("acodec", "")), "http_headers": {}}
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def _connect_to_author(
    message: discord.Message, bot_user: object | None
) -> tuple[discord.VoiceClient | None, str | None]:
    if getattr(message.author, "voice", None) is None or message.author.voice.channel is None:
        return None, None
    target_channel = message.author.voice.channel
    channel_guild = getattr(target_channel, "guild", None)
    if getattr(channel_guild, "id", None) not in {
        None,
        getattr(message.guild, "id", None),
    }:
        return None, "join a voice channel in this server first"
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
        return None, "join my current voice channel to control music"
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


def voice_channel_humans(channel: object) -> list[object]:
    members = getattr(channel, "members", None) or ()
    return [member for member in members if not getattr(member, "bot", False)]


async def stop_music(bot: object, guild: object) -> None:
    """Stop this guild's music and leave its voice channel."""
    tracks = getattr(bot, "music_tracks", {})
    track = tracks.pop(getattr(guild, "id", None), None)
    if track and track.get("_stop_timer"):
        track["_stop_timer"].cancel()
    voice_client = getattr(guild, "voice_client", None)
    if voice_client is not None:
        stop = getattr(voice_client, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                log.debug("Could not stop music", exc_info=True)
        disconnect = getattr(voice_client, "disconnect", None)
        if callable(disconnect):
            try:
                await disconnect()
            except Exception:
                log.debug("Could not leave music voice channel", exc_info=True)
    tracks = getattr(bot, "music_tracks", None)
    guild_id = getattr(guild, "id", None)
    if isinstance(tracks, dict) and guild_id is not None:
        tracks.pop(guild_id, None)


async def abandon_music_if_needed(
    bot: object, member: object, before: object, after: object
) -> bool:
    """Stop music when the requester leaves, or when no one is left listening."""
    if getattr(member, "bot", False):
        return False
    guild = getattr(member, "guild", None)
    voice_client = getattr(guild, "voice_client", None) if guild is not None else None
    channel = getattr(voice_client, "channel", None)
    if guild is None or voice_client is None or channel is None:
        return False
    bot_channel_id = getattr(channel, "id", None)
    left_bot_channel = getattr(getattr(before, "channel", None), "id", None) == bot_channel_id
    still_in_bot_channel = (
        getattr(getattr(after, "channel", None), "id", None) == bot_channel_id
    )
    if not left_bot_channel or still_in_bot_channel:
        return False
    tracks = getattr(bot, "music_tracks", None)
    track = tracks.get(guild.id) if isinstance(tracks, dict) else None
    requester_id = track.get("requested_by") if isinstance(track, dict) else None
    requester_left = requester_id is not None and getattr(member, "id", None) == requester_id
    if voice_channel_humans(channel) and not requester_left:
        return False
    reason = "requester-left" if requester_left else "empty"
    log.info(
        "Music stop guild=%s reason=%s user=%s",
        getattr(guild, "id", None),
        reason,
        getattr(member, "id", None),
    )
    await stop_music(bot, guild)
    return True


async def handle_music_command(bot: object, message: discord.Message, argument: str) -> str:
    if message.guild is None:
        return "!music only works in a server voice channel"
    busy = getattr(bot, "music_busy", None)
    if busy is None:
        bot.music_busy = busy = set()
    sessions = set(bot.music_tracks) | busy
    if message.guild.id not in sessions and len(sessions) >= MAX_MUSIC_JOBS:
        return "Music session limit reached"
    if message.guild.id in busy or len(busy) >= MAX_MUSIC_JOBS:
        return "Music is busy; try later"
    busy.add(message.guild.id)
    try:
        return await asyncio.wait_for(_handle_music_command(bot, message, argument), timeout=50)
    except (asyncio.TimeoutError, httpx.HTTPError):
        return "Music timed out or could not be downloaded"
    finally:
        busy.discard(message.guild.id)


async def _handle_music_command(
    bot: object, message: discord.Message, argument: str
) -> str:
    if message.guild is None:
        return "!music only works in a server voice channel"
    guild_id = message.guild.id
    action = argument.strip()
    action_lower = action.casefold()
    voice_client = message.guild.voice_client
    tracks: dict[int, dict[str, object]] = bot.music_tracks  # type: ignore[attr-defined]
    attached_track = attached_music_track(message)

    if action_lower == "help":
        return MUSIC_USAGE
    if action_lower != "now" and voice_client is not None:
        author_channel = getattr(getattr(message.author, "voice", None), "channel", None)
        if getattr(author_channel, "id", None) != getattr(voice_client.channel, "id", None):
            return "join my current voice channel to control music"
    if action_lower in {"leave", "disconnect"}:
        if voice_client is None:
            return "I am not in a voice channel"
        await stop_music(bot, message.guild)
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
            await stop_music(bot, message.guild)
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
            bot,
            message,
            str(track["query"]),
            verb="playing",
            direct_track=track if track.get("source") == "attachment" else None,
        )
    if action_lower == "restart":
        track = tracks.get(guild_id)
        if track is None:
            return "choose a song first with `!music <song or URL>`"
        return await _play_or_restart(
            bot,
            message,
            str(track["query"]),
            verb="restarted",
            direct_track=track if track.get("source") == "attachment" else None,
        )

    if attached_track is not None:
        return await _play_or_restart(
            bot,
            message,
            str(attached_track["query"]),
            verb="playing",
            direct_track=attached_track,
        )
    if not action:
        return MUSIC_USAGE
    return await _play_or_restart(bot, message, action, verb="playing")


async def _play_or_restart(
    bot: object,
    message: discord.Message,
    query: str,
    *,
    verb: str,
    direct_track: dict[str, object] | None = None,
) -> str:
    if getattr(message.author, "voice", None) is None or message.author.voice.channel is None:
        if verb == "restarted":
            hint = "`!music restart`"
        elif verb == "playing":
            hint = "`!music <song or URL>`"
        else:
            hint = "`!music start`"
        return f"join a voice channel first, then use {hint}"
    connected_here = False
    installed = False
    try:
        track = dict(direct_track or await resolve_music(query))
        track["requested_by"] = getattr(message.author, "id", None)
        audio = await download_audio(track)
        # Validate and download before acquiring a voice connection.
        if len(bot.music_tracks) >= MAX_MUSIC_JOBS and message.guild.id not in bot.music_tracks:
            raise ValueError("Music session limit reached")
        connected_here = message.guild.voice_client is None
        voice_client, error = await _connect_to_author(message, getattr(bot, "user", None))
        if error is not None:
            return error
        if voice_client is None:
            raise ValueError("join a voice channel first")
        author_channel = getattr(getattr(message.author, "voice", None), "channel", None)
        if getattr(author_channel, "id", None) != getattr(voice_client.channel, "id", None):
            raise ValueError("join my current voice channel to control music")
        old_track = bot.music_tracks.get(message.guild.id)
        if old_track and old_track.get("_stop_timer"):
            old_track["_stop_timer"].cancel()
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        loop = asyncio.get_running_loop()
        async def finished():
            if bot.music_tracks.get(message.guild.id) is track:
                await stop_music(bot, message.guild)
        def after(error):
            _playback_finished(error)
            if not loop.is_closed():
                loop.call_soon_threadsafe(lambda: asyncio.create_task(finished()))
        bot.music_tracks[message.guild.id] = track
        try:
            play_track(voice_client, dict(track, audio_bytes=audio, _after=after))
        except BaseException:
            bot.music_tracks.pop(message.guild.id, None)
            raise
        track["_stop_timer"] = loop.call_later(MAX_TRACK_SECONDS + 15, lambda: asyncio.create_task(finished()))
        installed = True
        log.info(
            "Music play guild=%s text=%s user=%s",
            getattr(message.guild, "id", None),
            getattr(message.channel, "id", None),
            track.get("requested_by"),
        )
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

    finally:
        if connected_here and not installed:
            await stop_music(bot, message.guild)
