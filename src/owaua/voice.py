"""Voice-channel features.

* ``/join`` / ``/leave`` — connect / disconnect (uses ``VoiceRecvClient`` so
  live transcription is possible; falls back to the stock voice client).
* ``/say`` — Orpheus English TTS (Groq) played from an in-memory buffer via
  ``discord.FFmpegPCMAudio(pipe=True)``.
* ``/stt`` — toggle live transcription: voice receive via
  ``discord-ext-voice-recv`` is sliced into utterances with simple energy-based
  VAD, each utterance is transcribed with Whisper Large v3 Turbo (Groq) and
  posted to the text channel the command was used in.

Voice receive requires ``discord-ext-voice-recv`` (and ``audioop-lts`` on
Python >= 3.13) — if it's missing the STT half degrades gracefully while
``/join`` / ``/leave`` / ``/say`` keep working.
"""

import asyncio
import contextlib
import io
import logging
import threading
import time
import typing
import wave
from typing import Callable, Optional, Tuple

import discord

from owaua import ai_control, config, db
from owaua.scope import Scope
from owaua.services.llm_client import LLMError, llm

log = logging.getLogger("owaua.voice")

try:
    from discord.ext import (
        voice_recv,  # pyright: ignore[reportAttributeAccessIssue, reportUnknownVariableType]
    )

    _VOICE_RECV_OK = True
except Exception as e:
    voice_recv = None
    _VOICE_RECV_OK = False
    log.warning("discord-ext-voice-recv unavailable, live STT disabled: %s", e)


_SAMPLE_RATE = 48000
_CHANNELS = 2
_SAMPLE_WIDTH = 2
_BYTES_PER_SECOND = _SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH
_MAX_TTS_CHARACTERS = 200
_MAX_TTS_AUDIO_BYTES = 10 * 1024 * 1024
_MAX_STT_AUDIO_BYTES = 4 * 1024 * 1024
_MAX_STT_QUEUE_BYTES = 16 * 1024 * 1024
_MAX_ACTIVE_STT_SESSIONS = 4
_TTS_ACQUIRE_TIMEOUT_SECONDS = 0.25
_VOICE_CONNECT_TIMEOUT_SECONDS = 15.0


_stt_sessions: dict[typing.Any, typing.Any] = {}
_tts_slots = asyncio.Semaphore(4)
_stt_lifecycle_lock = asyncio.Lock()
_stt_shutting_down = False


def _voice_channel_of(member: typing.Any) -> Optional[discord.VoiceChannel]:
    if isinstance(member, discord.Member) and member.voice:
        return typing.cast(discord.VoiceChannel, member.voice.channel)
    return None


def _can_control_channel(interaction: discord.Interaction, channel: typing.Any) -> bool:
    """Allow active channel members or users with manage_channels."""
    return _member_can_control_channel(interaction.user, channel)


def _member_can_control_channel(member: object, channel: object) -> bool:
    """Evaluate voice control using one freshly resolved guild member."""
    if not isinstance(member, discord.Member):
        return False
    permissions = member.guild_permissions
    member_id = getattr(member, "id", None)
    if (
        getattr(permissions, "administrator", False)
        or getattr(permissions, "manage_channels", False)
        or getattr(permissions, "manage_guild", False)
        or (
            member_id is not None
            and getattr(getattr(member, "guild", None), "owner_id", None) == member_id
        )
    ):
        return True
    current = _voice_channel_of(member)
    return current is not None and getattr(current, "id", None) == getattr(channel, "id", None)


def _guild_stt_enabled(guild_id: int) -> bool:
    """Voice transcription is off until a guild explicitly opts in."""
    try:
        return (
            db.guild_settings(Scope.guild(guild_id).key).get("voice_transcription_enabled") is True
        )
    except Exception:
        log.exception("could not read voice settings for guild %s", guild_id)
        return False


def _stt_consent_key(guild_id: int) -> str:
    return f"voice_transcription_consent:{Scope.guild(guild_id).key}"


def set_stt_consent(user_id: int, guild_id: int, enabled: bool) -> None:
    """Persist a user's explicit consent for one exact guild scope."""
    numeric_id = int(user_id)
    db.user_flag_set(
        str(numeric_id),
        _stt_consent_key(guild_id),
        "1" if enabled else "0",
    )
    if not enabled:
        for session in list(_stt_sessions.values()):
            if session.guild_id == int(guild_id) and numeric_id in session.consenting_user_ids:
                _request_session_stop(session, announce=True)


def revoke_all_stt_consent(user_id: int) -> None:
    """Immediately stop every live session containing a deleting/blocked user."""
    numeric_id = int(user_id)
    for session in list(_stt_sessions.values()):
        if numeric_id in session.consenting_user_ids:
            _request_session_stop(session, announce=True)


def stop_guild_stt(guild_id: int) -> bool:
    """Request an immediate stop after a guild disables transcription."""
    session = _stt_sessions.get(int(guild_id))
    if session is None:
        return False
    _request_session_stop(session, announce=True)
    return True


def has_stt_consent(user_id: int, guild_id: int) -> bool:
    """Return whether a user opted in within this exact guild scope."""
    try:
        return db.user_flag_get(str(user_id), _stt_consent_key(guild_id)) == "1"
    except Exception:
        log.exception("could not read voice consent for user %s", user_id)
        return False


def _can_start_stt(member: object, channel: object) -> bool:
    """Starting capture always requires effective manage_channels permission."""
    if not isinstance(member, discord.Member):
        return False
    if getattr(member.guild, "owner_id", None) == member.id:
        return True
    try:
        permissions: typing.Any = typing.cast(typing.Any, channel).permissions_for(member)
    except (AttributeError, TypeError):
        permissions = member.guild_permissions
    return bool(
        getattr(permissions, "administrator", False)
        or getattr(permissions, "manage_channels", False)
    )


def _can_view_transcripts(destination: object, member: object) -> bool:
    try:
        permissions: typing.Any = typing.cast(typing.Any, destination).permissions_for(member)
    except (AttributeError, TypeError):
        return False
    return bool(
        getattr(permissions, "administrator", False) or getattr(permissions, "view_channel", False)
    )


def _human_participants(channel: object) -> list[discord.Member]:
    return [
        member
        for member in typing.cast(
            typing.Iterable[typing.Any], getattr(channel, "members", None) or []
        )
        if isinstance(member, discord.Member) and not getattr(member, "bot", False)
    ]


def _rms(pcm: bytes) -> float:
    """RMS of 16-bit PCM normalized to 0..1 (no audioop dependency)."""
    if not pcm:
        return 0.0
    import array

    try:
        samples = array.array("h")
        samples.frombytes(pcm)
    except Exception:
        return 0.0
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return (total / len(samples)) ** 0.5 / 32768.0


def _pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw 48k stereo s16le PCM in a WAV container (for Whisper)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(_CHANNELS)
        w.setsampwidth(_SAMPLE_WIDTH)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(pcm)
    buf.seek(0)
    return buf.read()


class _UtteranceSinkBase:
    """Placeholder used when voice-recv isn't importable (live STT disabled)."""

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        raise RuntimeError("voice receive is unavailable")


if _VOICE_RECV_OK:

    class UtteranceSink(typing.cast(typing.Any, voice_recv).BasicSink):
        """Buffers PCM per user and emits WAV utterances on silence / max length.

        Runs on the voice-receive thread; hands finished utterances to the asyncio
        loop via a thread-safe enqueue callback.
        """

        def __init__(
            self,
            enqueue: Callable[[int, bytes, float], None],
            *,
            silence_ms: float = 800.0,
            min_ms: float = 350.0,
            max_ms: float = min(15.0, config.STT_MAX_UTTERANCE_SECONDS) * 1000.0,
            threshold: float = 0.012,
        ) -> None:
            typing.cast(typing.Any, super()).__init__(self._on_packet, decode=True)
            self._enqueue = enqueue
            self._loop = asyncio.get_running_loop()
            self._buffers: dict[int, bytearray] = {}
            self._last_voice: dict[int, float] = {}
            self._buffer_lock = threading.RLock()
            self._accepting_audio = True
            self._silence_ms = silence_ms
            self._min_ms = min_ms
            self._max_ms = max_ms
            self._threshold = threshold

        def wants_opus(self) -> bool:
            return False

        def _on_packet(self, user: typing.Any, data: typing.Any) -> None:
            with self._buffer_lock:
                if not self._accepting_audio or user is None or getattr(user, "bot", False):
                    return
                pcm = getattr(data, "pcm", None)
                if not pcm:
                    return
                uid = user.id
                now = time.perf_counter() * 1000.0
                if _rms(pcm) >= self._threshold:
                    self._buffers.setdefault(uid, bytearray()).extend(pcm)
                    self._last_voice[uid] = now
                    buf = self._buffers[uid]
                    if len(buf) >= int(_BYTES_PER_SECOND * self._max_ms / 1000.0):
                        self._flush(uid, force=True)
                else:
                    buf = self._buffers.get(uid)
                    if buf and now - self._last_voice.get(uid, now) >= self._silence_ms:
                        self._flush(uid)

        def _flush(self, uid: int, *, force: bool = False) -> None:
            with self._buffer_lock:
                if not self._accepting_audio:
                    return
                buf = self._buffers.pop(uid, None)
                self._last_voice.pop(uid, None)
                if not buf:
                    return
                duration_ms = len(buf) / _BYTES_PER_SECOND * 1000.0
                if duration_ms < self._min_ms and not force:
                    return
                try:
                    wav = _pcm_to_wav(bytes(buf))
                except Exception:
                    log.exception("failed to build wav for %s", uid)
                    return
                try:
                    self._loop.call_soon_threadsafe(self._enqueue, uid, wav, duration_ms)
                except RuntimeError:
                    pass

        def flush_stale(self, now_ms: Optional[float] = None) -> None:
            """Flush any buffer whose last voice packet predates the silence window.

            Safety net for when silence packets stop arriving entirely (the normal
            flush only happens on the next packet).
            """
            with self._buffer_lock:
                if not self._accepting_audio:
                    return
                now = now_ms if now_ms is not None else time.perf_counter() * 1000.0
                for uid in list(self._buffers):
                    if now - self._last_voice.get(uid, now) >= self._silence_ms:
                        self._flush(uid)

        def disarm(self) -> None:
            """Stop accepting audio and discard all untranscribed PCM."""
            with self._buffer_lock:
                self._accepting_audio = False
                self._buffers.clear()
                self._last_voice.clear()

        def cleanup(self) -> None:
            self.disarm()
            typing.cast(typing.Any, super()).cleanup()

else:

    class UtteranceSink(_UtteranceSinkBase):
        """Stub so imports never break when voice-recv is missing."""


class SttSession:
    """One live-transcription session per guild."""

    def __init__(
        self,
        guild_id: int,
        text_channel: typing.Any,
        *,
        controller_id: Optional[int] = None,
        voice_channel_id: Optional[int] = None,
        consenting_user_ids: Optional[set[int]] = None,
    ) -> None:
        self.guild_id = guild_id
        self.channel = text_channel
        self.controller_id = controller_id
        self.voice_channel_id = voice_channel_id
        self.consenting_user_ids = frozenset(consenting_user_ids or set())
        self.queue: asyncio.Queue[tuple[int, bytes, float]] = asyncio.Queue(maxsize=16)
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.sink: typing.Any = None
        self.voice_client: typing.Any = None
        self.accepting_audio = True
        self.announce_stop = False
        self.queued_audio_bytes = 0

    def enqueue(self, uid: int, wav: bytes, duration_ms: float) -> None:
        """Queue an utterance, dropping it if transcription is falling behind."""
        if (
            not self.accepting_audio
            or self.stop_event.is_set()
            or (self.consenting_user_ids and uid not in self.consenting_user_ids)
            or len(wav) > _MAX_STT_AUDIO_BYTES
            or self.queued_audio_bytes + len(wav) > _MAX_STT_QUEUE_BYTES
        ):
            log.warning("discarded STT audio due to consent, lifecycle, or byte limits")
            return
        try:
            typing.cast(typing.Any, self).queue.put_nowait((uid, wav, duration_ms))
            self.queued_audio_bytes += len(wav)
        except asyncio.QueueFull:
            log.warning("stt queue full in guild %s; dropping utterance", self.guild_id)


def _request_session_stop(session: SttSession, *, announce: bool = False) -> None:
    """Synchronously disarm capture and cancel an active worker."""
    session.accepting_audio = False
    session.announce_stop = session.announce_stop or announce
    session.stop_event.set()
    sink = session.sink
    if sink is not None and hasattr(sink, "disarm"):
        with contextlib.suppress(Exception):
            sink.disarm()
    vc = session.voice_client
    if vc is not None:
        with contextlib.suppress(Exception):
            vc.stop_listening()
    task = session.task
    if task is not None and not task.done():
        try:
            loop = task.get_loop()
            loop.call_soon_threadsafe(task.cancel)
        except (AttributeError, RuntimeError):
            task.cancel()


def _drain_stt_queue(session: SttSession) -> None:
    while not typing.cast(typing.Any, session).queue.empty():
        with contextlib.suppress(asyncio.QueueEmpty):
            _uid, wav, _duration_ms = typing.cast(typing.Any, session).queue.get_nowait()
            session.queued_audio_bytes = max(0, session.queued_audio_bytes - len(wav))
            typing.cast(typing.Any, session).queue.task_done()


def _destination_is_usable(
    destination: object,
    guild: discord.Guild,
    bot_member: Optional[discord.Member] = None,
) -> bool:
    if getattr(getattr(destination, "guild", None), "id", None) != guild.id:
        return False
    me = bot_member or guild.me
    if me is None:
        return False
    try:
        permissions: typing.Any = typing.cast(typing.Any, destination).permissions_for(me)
    except (AttributeError, TypeError):
        return False
    return bool(
        getattr(permissions, "administrator", False)
        or (
            getattr(permissions, "view_channel", False)
            and (
                getattr(permissions, "send_messages", False)
                or getattr(permissions, "send_messages_in_threads", False)
            )
        )
    )


def _session_is_authorized(session: SttSession, vc: typing.Any) -> bool:
    """Revalidate connection, controller presence, participant consent, and visibility."""
    if (
        session.stop_event.is_set()
        or not session.accepting_audio
        or vc is None
        or not vc.is_connected()
        or not getattr(config, "STT_ENABLED", False)
        or not _guild_stt_enabled(session.guild_id)
    ):
        return False
    voice_channel = getattr(vc, "channel", None)
    if voice_channel is None or (
        session.voice_channel_id is not None
        and getattr(voice_channel, "id", None) != session.voice_channel_id
    ):
        return False
    participants = _human_participants(voice_channel)
    participant_ids = {member.id for member in participants}
    if session.controller_id is not None and session.controller_id not in participant_ids:
        return False
    controller = next(
        (participant for participant in participants if participant.id == session.controller_id),
        None,
    )
    if controller is None or not _can_start_stt(controller, voice_channel):
        return False
    if participant_ids != set(session.consenting_user_ids):
        return False
    if not _destination_is_usable(session.channel, vc.guild):
        return False
    for participant in participants:
        if participant.id != session.controller_id and not has_stt_consent(
            participant.id, session.guild_id
        ):
            return False
        if not _can_view_transcripts(session.channel, participant):
            return False
    return True


async def _stt_worker(session: SttSession, vc: typing.Any) -> None:
    """Drain finished utterances, transcribe with Whisper, post to the channel."""
    try:
        while _session_is_authorized(session, vc):
            try:
                item: typing.Any = typing.cast(
                    typing.Any, await asyncio.wait_for(session.queue.get(), timeout=0.5)
                )
            except asyncio.TimeoutError:
                if session.sink is not None and hasattr(session.sink, "flush_stale"):
                    session.sink.flush_stale()
                continue
            try:
                uid, wav, duration_ms = item
                session.queued_audio_bytes = max(0, session.queued_audio_bytes - len(wav))
                if (
                    uid not in session.consenting_user_ids
                    or duration_ms < 400
                    or not _session_is_authorized(session, vc)
                ):
                    continue
                text = await llm.transcribe(
                    config.WHISPER_MODEL,
                    wav,
                    api_key=config.GROQ_API_KEY,
                    base_url=config.GROQ_BASE_URL,
                    scope_id=Scope.guild(session.guild_id).key,
                    user_id=str(uid),
                )
                if not _session_is_authorized(session, vc):
                    continue
                text = discord.utils.escape_markdown(
                    discord.utils.escape_mentions((text or "").strip())
                )[:1800]
                if not text:
                    continue
                member = vc.guild.get_member(uid)
                who = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(
                        getattr(member, "display_name", None) or f"user {uid}"
                    )
                )
                await session.channel.send(
                    f"**{who}:** {text}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except asyncio.CancelledError:
                raise
            except (LLMError, ai_control.AIBudgetExceeded):
                log.warning("stt transcription failed")
            except (discord.Forbidden, discord.NotFound):
                session.stop_event.set()
                log.warning("stt destination became unavailable")
            except discord.HTTPException:
                session.stop_event.set()
                log.warning("stt post failed")
            except Exception:
                log.exception("stt utterance processing failed")
            finally:
                typing.cast(typing.Any, session).queue.task_done()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("stt worker failed in guild %s", session.guild_id)
    finally:
        requested_stop = session.stop_event.is_set()
        session.accepting_audio = False
        session.stop_event.set()
        if session.sink is not None and hasattr(session.sink, "disarm"):
            with contextlib.suppress(Exception):
                session.sink.disarm()
        with contextlib.suppress(Exception):
            vc.stop_listening()
        _drain_stt_queue(session)
        if session.announce_stop or not requested_stop:
            try:
                await session.channel.send(
                    "🎙️ Live transcription stopped automatically because the "
                    "controller left, consent changed, or channel access changed.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.warning("could not post automatic transcription stop notice")
        if _stt_sessions.get(session.guild_id) is session:
            _stt_sessions.pop(session.guild_id, None)


async def _stop_stt_session(guild_id: int, vc: typing.Any = None) -> bool:
    """Stop and fully drain one transcription session."""
    session = _stt_sessions.pop(guild_id, None)
    if session is None:
        return False
    if vc is not None:
        session.voice_client = vc
    _request_session_stop(session)
    task = session.task
    if task is not None and task is not asyncio.current_task():
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("stt worker failed while stopping guild %s", guild_id)
    _drain_stt_queue(session)
    return True


async def shutdown() -> None:
    """Cancel every STT worker during client shutdown without flushing audio."""
    global _stt_shutting_down

    async with _stt_lifecycle_lock:
        _stt_shutting_down = True
        for guild_id in list(_stt_sessions):
            await _stop_stt_session(guild_id)


async def _connect(channel: discord.VoiceChannel):
    """Connect using VoiceRecvClient when available, else the stock client."""
    if _VOICE_RECV_OK:
        return await asyncio.wait_for(
            channel.connect(cls=typing.cast(typing.Any, voice_recv).VoiceRecvClient),
            timeout=_VOICE_CONNECT_TIMEOUT_SECONDS,
        )
    return await asyncio.wait_for(channel.connect(), timeout=_VOICE_CONNECT_TIMEOUT_SECONDS)


async def join(interaction: discord.Interaction, channel: typing.Any = None) -> Tuple[bool, str]:
    """Connect the bot to the caller's voice channel."""
    guild = interaction.guild
    if guild is None:
        return False, "this only works in a server."
    vc = guild.voice_client
    if vc is not None and typing.cast(typing.Any, vc).is_connected():
        return (
            False,
            f"already connected to **{typing.cast(typing.Any, vc).channel.name}**.",
        )
    target = channel or _voice_channel_of(interaction.user)
    if target is None:
        return False, "you're not in a voice channel — join one first."
    if getattr(getattr(target, "guild", None), "id", None) != guild.id:
        return False, "that voice channel is not in this server."
    if not _can_control_channel(interaction, target):
        return False, "join that voice channel first (or use manage channels permission)."
    try:
        await _connect(target)
    except (discord.HTTPException, discord.ClientException, asyncio.TimeoutError):
        return False, "couldn't join; Discord rejected the request."
    return True, f"joined **{target.name}**."


async def leave(interaction: discord.Interaction) -> Tuple[bool, str]:
    """Disconnect from voice and stop any live transcription."""
    async with _stt_lifecycle_lock:
        return await _leave_locked(interaction)


async def _leave_locked(interaction: discord.Interaction) -> Tuple[bool, str]:
    """Disconnect while serialized against STT startup and shutdown."""
    guild = interaction.guild
    if guild is None:
        return False, "this only works in a server."
    vc = guild.voice_client
    if vc is None or not typing.cast(typing.Any, vc).is_connected():
        return False, "not in a voice channel."
    if not _can_control_channel(interaction, vc.channel):
        return False, "join my voice channel first (or use manage channels permission)."
    await _stop_stt_session(guild.id, vc)
    try:
        await vc.disconnect(force=True)
    except discord.HTTPException:
        return False, "couldn't disconnect; Discord rejected the request."
    return True, "disconnected."


async def say(interaction: discord.Interaction, text: str) -> Tuple[bool, str]:
    """Synthesize text with the supported Groq Orpheus API and play it."""
    guild = interaction.guild
    if guild is None:
        return False, "this only works in a server."
    if not config.GROQ_API_KEY:
        return False, "groq api key missing — TTS needs it."
    text = str(text or "").strip()
    if not text:
        return False, "text is required."
    if len(text) > _MAX_TTS_CHARACTERS:
        return False, f"text is limited to {_MAX_TTS_CHARACTERS} characters."
    try:
        ai_control.check_tts_budget(str(interaction.user.id))
    except ai_control.AIBudgetExceeded as exc:
        return False, str(exc)
    vc = guild.voice_client
    connected_here = False
    if vc is not None and vc.is_connected() and not _can_control_channel(interaction, vc.channel):
        return False, "join my voice channel first (or use manage channels permission)."
    if vc is None or not vc.is_connected():
        target = _voice_channel_of(interaction.user)
        if target is None:
            return False, "join a voice channel first (or use /join)."
        try:
            vc: typing.Any = typing.cast(typing.Any, await _connect(target))
            connected_here = True
        except (discord.HTTPException, discord.ClientException, asyncio.TimeoutError):
            return False, "couldn't join; Discord rejected the request."
    try:
        await asyncio.wait_for(_tts_slots.acquire(), timeout=_TTS_ACQUIRE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        if connected_here:
            with contextlib.suppress(discord.HTTPException):
                await vc.disconnect()
        return False, "tts is busy; try again shortly."
    try:
        try:
            audio = await llm.speak(
                config.TTS_MODEL,
                text,
                voice=config.TTS_VOICE,
                response_format="wav",
                api_key=config.GROQ_API_KEY,
                base_url=config.GROQ_BASE_URL,
                scope_id=Scope.guild(guild.id).key,
                user_id=str(interaction.user.id),
            )
        except (LLMError, ai_control.AIBudgetExceeded):
            if connected_here:
                with contextlib.suppress(discord.HTTPException):
                    await vc.disconnect()
            return False, "tts provider failed; try again later."
    finally:
        _tts_slots.release()
    if not audio:
        if connected_here:
            with contextlib.suppress(discord.HTTPException):
                await vc.disconnect()
        return False, "tts returned no audio."
    if len(audio) > _MAX_TTS_AUDIO_BYTES:
        if connected_here:
            with contextlib.suppress(discord.HTTPException):
                await vc.disconnect()
        return False, "tts audio exceeded the playback limit."
    try:
        current_member = await guild.fetch_member(interaction.user.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError):
        if connected_here:
            with contextlib.suppress(discord.HTTPException):
                await vc.disconnect()
        return False, "your current voice access could not be revalidated."
    if not vc.is_connected() or not _member_can_control_channel(current_member, vc.channel):
        if connected_here:
            with contextlib.suppress(discord.HTTPException):
                await vc.disconnect()
        return False, "your voice access changed; playback was cancelled."
    try:
        if vc.is_playing():
            vc.stop()
        vc.play(discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True))
    except Exception:
        log.exception("voice playback failed")
        if connected_here:
            with contextlib.suppress(discord.HTTPException):
                await vc.disconnect()
        return False, "playback failed; check the bot logs."
    return True, f"🔊 speaking: {discord.utils.escape_mentions(text[:200])}"


async def toggle_stt(interaction: discord.Interaction) -> Tuple[bool, str]:
    """Toggle live voice transcription under the global lifecycle lock."""
    async with _stt_lifecycle_lock:
        if _stt_shutting_down:
            return False, "live transcription is shutting down."
        return await _toggle_stt_locked(interaction)


async def _toggle_stt_locked(interaction: discord.Interaction) -> Tuple[bool, str]:
    """Toggle one guild session; caller must hold ``_stt_lifecycle_lock``."""
    guild = interaction.guild
    if guild is None:
        return False, "this only works in a server."

    existing = _stt_sessions.get(guild.id)
    if existing is not None:
        vc = guild.voice_client
        if vc is not None and not _can_control_channel(interaction, vc.channel):
            return False, "join my voice channel first (or use manage channels permission)."
        await _stop_stt_session(guild.id, vc)
        return True, "stopped live transcription."

    if not getattr(config, "STT_ENABLED", False):
        return False, "live transcription is disabled (OWAUA_STT_ENABLED=0)."
    if not _guild_stt_enabled(guild.id):
        return False, "live transcription is disabled for this server."
    if not _VOICE_RECV_OK:
        return False, (
            "live transcription needs `discord-ext-voice-recv` — it's not importable "
            "here. /join, /leave and /say still work."
        )
    if not config.GROQ_API_KEY:
        return False, "groq api key missing — whisper needs it."
    if len(_stt_sessions) >= _MAX_ACTIVE_STT_SESSIONS:
        return False, "live transcription is at capacity; try again later."

    vc = guild.voice_client
    already_connected: typing.Any = vc is not None and vc.is_connected()
    voice_channel = (
        typing.cast(typing.Any, vc).channel
        if already_connected
        else _voice_channel_of(interaction.user)
    )
    if voice_channel is None:
        return False, "join a voice channel first (or use /join)."
    member = interaction.user
    if not _can_start_stt(member, voice_channel):
        return False, "starting transcription requires `manage_channels`."
    if _voice_channel_of(member) != voice_channel:
        return False, "join the voice channel before starting transcription."
    destination = interaction.channel
    if destination is None or not hasattr(destination, "permissions_for"):
        return False, "choose a server text channel for transcripts."
    try:
        destination = await guild.fetch_channel(destination.id)
        member = await guild.fetch_member(member.id)
        bot_id = getattr(guild.me, "id", None)
        current_bot = await guild.fetch_member(bot_id) if bot_id is not None else None
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError):
        return False, "the member or transcript channel could not be revalidated."
    if not _can_start_stt(member, voice_channel):
        return False, "starting transcription requires `manage_channels`."
    if not _destination_is_usable(destination, guild, current_bot):
        return False, "I need view and send permission in the transcript channel."

    participants = _human_participants(voice_channel)
    missing_consent = [
        participant
        for participant in participants
        if participant.id != member.id and not has_stt_consent(participant.id, guild.id)
    ]
    if missing_consent:
        names = ", ".join(
            discord.utils.escape_mentions(participant.display_name)[:40]
            for participant in missing_consent[:8]
        )
        return False, f"everyone must opt in before recording; missing consent: {names}."
    hidden_from = [
        participant
        for participant in participants
        if not _can_view_transcripts(destination, participant)
    ]
    if hidden_from:
        return False, "every voice participant must be able to view the transcript channel."

    participant_ids = {participant.id for participant in participants}
    if member.id not in participant_ids:
        return False, "join the voice channel before starting transcription."

    session = SttSession(
        guild.id,
        destination,
        controller_id=member.id,
        voice_channel_id=voice_channel.id,
        consenting_user_ids=participant_ids,
    )
    _stt_sessions[guild.id] = session

    if not already_connected:
        try:
            vc: typing.Any = typing.cast(typing.Any, await _connect(voice_channel))
        except (discord.HTTPException, discord.ClientException, asyncio.TimeoutError):
            if _stt_sessions.get(guild.id) is session:
                _stt_sessions.pop(guild.id, None)
            _request_session_stop(session)
            return False, "couldn't join; Discord rejected the request."
    session.voice_client = vc
    if session.stop_event.is_set():
        if _stt_sessions.get(guild.id) is session:
            _stt_sessions.pop(guild.id, None)
        if not already_connected:
            with contextlib.suppress(discord.HTTPException):
                await typing.cast(typing.Any, vc).disconnect()
        return False, "transcription startup was cancelled; nothing was recorded."
    if not isinstance(vc, typing.cast(typing.Any, voice_recv).VoiceRecvClient):
        if _stt_sessions.get(guild.id) is session:
            _stt_sessions.pop(guild.id, None)
        _request_session_stop(session)
        if not already_connected:
            with contextlib.suppress(discord.HTTPException):
                await typing.cast(typing.Any, vc).disconnect()
        return False, "already connected with a non-recv voice client — /leave first."

    try:
        notice: typing.Any = await typing.cast(typing.Any, destination).send(
            f"🎙️ **Live transcription is starting** in **{voice_channel.name}** by "
            f"{discord.utils.escape_mentions(member.display_name)}. "
            "Audio will be sent to the configured transcription provider. "
            "Use `/stt` to stop it.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        if _stt_sessions.get(guild.id) is session:
            _stt_sessions.pop(guild.id, None)
        _request_session_stop(session)
        if not already_connected:
            with contextlib.suppress(discord.HTTPException):
                await typing.cast(typing.Any, vc).disconnect()
        return False, "couldn't post the recording notice; transcription was not started."

    participants = _human_participants(voice_channel)
    participant_ids = {participant.id for participant in participants}
    startup_valid = bool(
        not session.stop_event.is_set()
        and member.id in participant_ids
        and _can_start_stt(member, voice_channel)
        and _destination_is_usable(destination, guild, current_bot)
        and all(
            participant.id == member.id or has_stt_consent(participant.id, guild.id)
            for participant in participants
        )
        and all(_can_view_transcripts(destination, participant) for participant in participants)
    )
    if not startup_valid:
        if _stt_sessions.get(guild.id) is session:
            _stt_sessions.pop(guild.id, None)
        _request_session_stop(session)
        with contextlib.suppress(discord.HTTPException):
            await notice.edit(
                content="🎙️ Live transcription did not start because consent, membership, "
                "or channel access changed.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if not already_connected:
            with contextlib.suppress(discord.HTTPException):
                await typing.cast(typing.Any, vc).disconnect()
        return False, "consent, membership, or channel access changed; nothing was recorded."

    session.consenting_user_ids = frozenset(participant_ids)
    typing.cast(typing.Any, session).sink = UtteranceSink(session.enqueue)
    try:
        typing.cast(typing.Any, vc).listen(session.sink)
    except Exception:
        log.exception("could not start voice receiver")
        if _stt_sessions.get(guild.id) is session:
            _stt_sessions.pop(guild.id, None)
        _request_session_stop(session)
        with contextlib.suppress(discord.HTTPException):
            await notice.edit(
                content="🎙️ Live transcription failed to start; no recording is active.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if not already_connected:
            with contextlib.suppress(discord.HTTPException):
                await typing.cast(typing.Any, vc).disconnect()
        return False, "couldn't start listening; check the bot logs."
    typing.cast(typing.Any, session).task = asyncio.create_task(
        _stt_worker(session, vc), name=f"stt-guild-{guild.id}"
    )
    try:
        await notice.edit(
            content=(
                f"🎙️ **Live transcription started** in **{voice_channel.name}** by "
                f"{discord.utils.escape_mentions(member.display_name)}. "
                "Audio is sent to the configured transcription provider. "
                "Use `/stt` to stop it."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        await _stop_stt_session(guild.id, vc)
        if not already_connected:
            with contextlib.suppress(discord.HTTPException):
                await typing.cast(typing.Any, vc).disconnect()
        return False, "couldn't update the recording notice; transcription was stopped."
    if session.stop_event.is_set():
        if _stt_sessions.get(guild.id) is session:
            await _stop_stt_session(guild.id, vc)
        with contextlib.suppress(discord.HTTPException):
            await notice.edit(
                content="🎙️ Live transcription was cancelled during startup; no recording is active.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if not already_connected:
            with contextlib.suppress(discord.HTTPException):
                await typing.cast(typing.Any, vc).disconnect()
        return False, "transcription startup was cancelled; nothing remains active."
    return True, (
        f"🎙️ live transcription on in **{voice_channel.name}** — transcripts post "
        f"to #{getattr(destination, 'name', 'this channel')}."
    )
