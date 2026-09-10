"""Discord client lifecycle, commands, and message delivery.

Settings are read from :mod:`bot` at call time so operational overrides remain
effective without duplicating configuration.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import tempfile
import time
from collections import defaultdict, deque

import discord
import httpx

import bot as settings  # Read mutable runtime settings through the facade.
from memory_store import MemoryStore
from bot import (
    InputTooLarge,
    ModerationBlocked,
    ModerationRejected,
    ModerationUnavailable,
    image_url,
    parse_language_name,
    parse_topic_name,
    split_discord_message,
    klipy_gif_urls,
)

log = logging.getLogger("owaua")

VC_VOICES = ("nova", "shimmer", "coral", "marin")
VC_LINES = (
    "hey everyone, I just joined the voice channel",
    "hi, what are you all up to",
    "I have arrived in voice, try not to be boring",
    "hello from the other side of the voice channel",
    "okay, I am here now, somebody say something interesting",
)

from bot_service import BotService


class MessageEventGuard:
    """Keep Discord redeliveries from reaching the response pipeline twice."""

    def __init__(self, *, ttl: float = 900.0) -> None:
        self.ttl = ttl
        self._seen: dict[int, float] = {}

    def claim(self, message_id: int, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        expired = [event_id for event_id, timestamp in self._seen.items()
                   if current - timestamp >= self.ttl]
        for event_id in expired:
            self._seen.pop(event_id, None)
        if message_id in self._seen:
            return False
        self._seen[message_id] = current
        return True


class PersonaBot(discord.Client, BotService):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            intents=intents, allowed_mentions=discord.AllowedMentions.none()
        )
        self.memory = MemoryStore(settings.MEMORY_DB)
        self.provider_http = httpx.AsyncClient(timeout=settings.REQUEST_TIMEOUT)
        self.conversation_locks: defaultdict[tuple[str, str, str], asyncio.Lock] = (
            defaultdict(asyncio.Lock)
        )
        self.summary_locks: defaultdict[tuple[str, str, str], asyncio.Lock] = (
            defaultdict(asyncio.Lock)
        )
        self.active_requests: dict[tuple[str, str, str], asyncio.Task[object]] = {}
        self.rate_windows: defaultdict[int, deque[float]] = defaultdict(deque)
        self.moderation_failures: defaultdict[int, deque[float]] = defaultdict(deque)
        self.moderation_blocks: dict[int, float] = {}
        self.background_tasks: set[asyncio.Task[object]] = set()
        self.message_events = MessageEventGuard()
        self.response_languages: dict[tuple[str, str], str] = {}
        self.voice_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.music_tracks: dict[int, dict[str, str]] = {}
        default_model = "mistral" if settings.MISTRAL_API_KEY else "gpt"
        saved_model = self.memory.get_setting("selected_persona_model", default_model)
        if saved_model not in settings.PERSONA_ALIASES.values():
            saved_model = default_model
        if saved_model == "mistral" and not settings.MISTRAL_API_KEY:
            saved_model = default_model
        if saved_model == "deepseek" and not settings.DEEPSEEK_API_KEY:
            saved_model = default_model
        self.selected_model = saved_model

    async def speak_in_voice(self, voice_client: discord.VoiceClient, text: str) -> str:
        """Generate and play one short TTS line, choosing a voice at random."""
        voice = random.choice(VC_VOICES)
        response = await self.provider_http.post(
            f"{settings.OPENAI_BASE_URL}/audio/speech",
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini-tts",
                "input": text,
                "voice": voice,
                "response_format": "mp3",
            },
        )
        response.raise_for_status()
        audio_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        audio_path = audio_file.name
        try:
            audio_file.write(response.content)
            audio_file.close()
            finished = asyncio.Event()

            def cleanup(error: Exception | None) -> None:
                try:
                    os.unlink(audio_path)
                except FileNotFoundError:
                    pass
                if error is not None:
                    log.warning("Voice playback failed: %s", error)
                loop.call_soon_threadsafe(finished.set)

            loop = asyncio.get_running_loop()
            voice_client.play(discord.FFmpegPCMAudio(audio_path), after=cleanup)
            await finished.wait()
            return voice
        except Exception:
            audio_file.close()
            try:
                os.unlink(audio_path)
            except FileNotFoundError:
                pass
            raise

    async def resolve_music(self, query: str) -> dict[str, str]:
        """Resolve a URL or search phrase to a playable audio stream."""
        import yt_dlp

        lookup = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
        options = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
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

    async def search_gif(self, query: str) -> str | None:
        """Return a real, directly embeddable Klipy GIF for a search phrase."""
        try:
            response = await self.provider_http.get(
                f"{settings.KLIPY_BASE_URL}/search",
                params={
                    "q": query,
                    "key": settings.KLIPY_API_KEY,
                    "limit": settings.GIF_SEARCH_LIMIT,
                    "media_filter": "gif",
                    "contentfilter": "medium",
                    "random": "true",
                },
            )
            response.raise_for_status()
            urls = klipy_gif_urls(response.json())
        except (httpx.HTTPError, ValueError, TypeError):
            # Do not log the HTTP exception itself: its request URL contains the
            # Klipy API key as a query parameter.
            log.warning("Klipy GIF search failed")
            return None
        return random.choice(urls) if urls else None

    def play_music_track(self, voice_client: discord.VoiceClient, track: dict[str, str]) -> None:
        source = discord.FFmpegPCMAudio(
            track["url"],
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            options="-vn",
        )
        voice_client.play(source, after=self._music_playback_finished)

    def _music_playback_finished(self, error: Exception | None) -> None:
        if error is not None:
            log.warning("Music playback failed: %s", error)

    async def on_ready(self) -> None:
        if settings.MEMORY_RETENTION_DAYS:
            cutoff = time.time() - settings.MEMORY_RETENTION_DAYS * 86400
            removed = await asyncio.to_thread(self.memory.prune_older_than, cutoff)
            if removed:
                log.info("Pruned %s expired memory messages", removed)
        log.info(
            "Logged in as %s; model=%s; persona=%s; memory=%s",
            self.user,
            self.active_model,
            settings.MODEL_PERSONAS.get(self.selected_model, "rudeish"),
            settings.MEMORY_DB,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self.message_events.claim(message.id):
            log.info("Ignoring redelivered Discord event %s", message.id)
            return

        parts = message.content.split(maxsplit=1)
        if message.guild is not None:
            # Register every guild channel before it can read or write memory.
            # This makes a server-wide erase exact even after a bot restart.
            await asyncio.to_thread(
                self.memory.register_scope, str(message.channel.id), str(message.guild.id)
            )
        if parts and parts[0].lower() == "!help":
            await message.channel.send(
                settings.HELP_TEXT,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if parts and parts[0].lower() == "!active":
            if message.guild is None:
                reply = "!active only works in a server channel"
            else:
                scope_id = str(message.channel.id)
                action = parts[1].strip().casefold() if len(parts) == 2 else ""
                if action == "on":
                    await asyncio.to_thread(
                        self.memory.set_active_mode, scope_id, True
                    )
                    reply = (
                        "active mode on — I’ll respond to every 6th message "
                        "in this channel"
                    )
                elif action == "off":
                    await asyncio.to_thread(
                        self.memory.set_active_mode, scope_id, False
                    )
                    reply = "active mode off in this channel"
                elif not action or action == "status":
                    enabled, count = await asyncio.to_thread(
                        self.memory.active_mode_status, scope_id
                    )
                    if enabled:
                        remaining = settings.ACTIVE_RESPONSE_INTERVAL - count
                        reply = (
                            "active mode is on — next automatic response in "
                            f"{remaining} message{'s' if remaining != 1 else ''}"
                        )
                    else:
                        reply = "active mode is off in this channel"
                else:
                    reply = "usage: !active on | !active off | !active status"
            await message.channel.send(
                reply,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if parts and parts[0].lower() == "!gifs":
            if message.guild is None:
                reply = "!gifs only works in a server channel"
            else:
                scope_id = str(message.channel.id)
                action = parts[1].strip().casefold() if len(parts) == 2 else ""
                if action == "on" and not settings.KLIPY_API_KEY:
                    reply = (
                        "GIF search is not configured — add KLIPY_API_KEY to .env "
                        "and restart the bot"
                    )
                elif action == "on":
                    await asyncio.to_thread(self.memory.set_gif_mode, scope_id, True)
                    reply = (
                        "GIFs on — I’ll send a relevant GIF every 10th message "
                        "in this channel"
                    )
                elif action == "off":
                    await asyncio.to_thread(self.memory.set_gif_mode, scope_id, False)
                    reply = "GIFs off in this channel"
                elif not action or action == "status":
                    enabled, count = await asyncio.to_thread(
                        self.memory.gif_mode_status, scope_id
                    )
                    if enabled:
                        remaining = settings.GIF_RESPONSE_INTERVAL - count
                        topic = await asyncio.to_thread(
                            self.memory.channel_topic, scope_id
                        )
                        topic_note = f" for `{topic}`" if topic else ""
                        reply = (
                            f"GIFs are on{topic_note} — next GIF in {remaining} "
                            f"message{'s' if remaining != 1 else ''}"
                        )
                        if not settings.KLIPY_API_KEY:
                            reply += " (KLIPY_API_KEY is currently missing)"
                    else:
                        reply = "GIFs are off in this channel"
                else:
                    reply = "usage: !gifs on | !gifs off | !gifs status"
            await message.channel.send(
                reply,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if parts and parts[0].lower() == "!topic":
            if message.guild is None:
                reply = "!topic only works in a server channel"
            else:
                scope_id = str(message.channel.id)
                argument = parts[1].strip() if len(parts) == 2 else ""
                if argument.casefold() == "off":
                    await asyncio.to_thread(self.memory.set_topic, scope_id, None)
                    reply = "topic lock off in this channel"
                else:
                    topic_parts = argument.rsplit(maxsplit=1)
                    if len(topic_parts) == 2 and topic_parts[1].casefold() == "on":
                        topic, error = parse_topic_name(topic_parts[0])
                        if error is not None:
                            reply = error
                        else:
                            assert topic is not None
                            await asyncio.to_thread(
                                self.memory.set_topic, scope_id, topic
                            )
                            reply = f"topic locked to `{topic}`"
                    elif not argument or argument.casefold() == "status":
                        topic = await asyncio.to_thread(
                            self.memory.channel_topic, scope_id
                        )
                        reply = (
                            f"topic is locked to `{topic}`"
                            if topic
                            else "topic lock is off in this channel"
                        )
                    else:
                        reply = "usage: !topic <topic> on | !topic off"
            await message.channel.send(
                reply,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if message.guild is not None and parts and parts[0].lower() == "!memory":
            action = parts[1].strip().lower() if len(parts) == 2 else ""
            if action != "erase":
                reply = "usage: !memory erase"
            elif not message.author.guild_permissions.manage_guild:
                reply = "you need the Manage Server permission to erase server memory"
            else:
                server_id = str(message.guild.id)
                try:
                    # Enumerating the guild before deletion also associates memory
                    # created by older bot versions, which had no server_id column.
                    channels = await message.guild.fetch_channels()
                except discord.HTTPException:
                    log.exception("Could not enumerate channels for memory erase")
                    await message.channel.send(
                        "I couldn't safely verify every server channel, so no memory was erased. Try again shortly.",
                        reference=message,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                await asyncio.gather(
                    *(
                        asyncio.to_thread(
                            self.memory.register_scope, str(channel.id), server_id
                        )
                        for channel in channels
                    )
                )
                scopes = await asyncio.to_thread(self.memory.server_scopes, server_id)
                # Stop requests already using this guild's old context. The store's
                # generation check is the final guard if cancellation races a write.
                for key, task in list(self.active_requests.items()):
                    if key[0] in scopes and task is not asyncio.current_task():
                        task.cancel()
                removed = await asyncio.to_thread(
                    self.memory.erase_server_memory, server_id
                )
                log.info(
                    "Erased server memory; server=%s records=%s", server_id, removed
                )
                reply = "server memory fully erased for every user and channel"
            await message.channel.send(
                reply,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if parts and parts[0].lower() == "!language":
            scope_id, user_id = self.conversation_key(message)
            language_arg = parts[1] if len(parts) == 2 else ""
            if not language_arg.strip():
                language = self.response_language(scope_id, user_id)
                reply = f"language: {language}"
            else:
                language, error = parse_language_name(language_arg)
                if error is not None:
                    reply = error
                else:
                    assert language is not None
                    self.set_response_language(scope_id, user_id, language)
                    reply = f"language set to {language}; I’ll reply in it from now on"
            await message.channel.send(
                reply,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if parts and parts[0].lower() == "!vc":
            if message.guild is None:
                reply = "!vc only works in a server voice channel"
            elif len(parts) == 2 and parts[1].strip().lower() in {"leave", "stop"}:
                voice_client = message.guild.voice_client
                if voice_client is None:
                    reply = "I am not in a voice channel"
                else:
                    await voice_client.disconnect()
                    reply = "left the voice channel"
            elif message.author.voice is None or message.author.voice.channel is None:
                reply = "join a voice channel first, then use `!vc`"
            else:
                target_channel = message.author.voice.channel
                voice_client = message.guild.voice_client
                try:
                    if voice_client is None:
                        voice_client = await target_channel.connect()
                    elif voice_client.channel.id != target_channel.id:
                        await voice_client.move_to(target_channel)
                    async with self.voice_locks[message.guild.id]:
                        line = random.choice(VC_LINES)
                        voice = await self.speak_in_voice(voice_client, line)
                    reply = f"joined {target_channel.mention} and spoke in a random voice ({voice})"
                except (discord.ClientException, discord.Forbidden, discord.HTTPException, OSError, RuntimeError) as exc:
                    log.exception("Could not join or speak in voice channel")
                    reply = f"I couldn't use voice chat right now: {type(exc).__name__}"
            await message.channel.send(
                reply,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if parts and parts[0].lower() == "!music":
            if message.guild is None:
                reply = "!music only works in a server voice channel"
            else:
                guild_id = message.guild.id
                action = parts[1].strip() if len(parts) == 2 else ""
                voice_client = message.guild.voice_client
                action_lower = action.casefold()
                if action_lower in {"help", ""}:
                    reply = (
                        "usage: !music <song or URL> | !music start | !music pause | "
                        "!music resume | !music stop | !music skip | !music leave | !music now"
                    )
                elif action_lower in {"leave", "disconnect"}:
                    if voice_client is None:
                        reply = "I am not in a voice channel"
                    else:
                        voice_client.stop()
                        await voice_client.disconnect()
                        self.music_tracks.pop(guild_id, None)
                        reply = "left the music voice channel"
                elif action_lower == "now":
                    track = self.music_tracks.get(guild_id)
                    reply = f"now playing: {track['title']}" if track else "nothing is queued"
                elif action_lower == "pause":
                    if voice_client is not None and voice_client.is_playing():
                        voice_client.pause()
                        reply = "music paused"
                    else:
                        reply = "nothing is playing"
                elif action_lower in {"start", "resume"}:
                    track = self.music_tracks.get(guild_id)
                    if voice_client is not None and voice_client.is_paused():
                        voice_client.resume()
                        reply = f"resumed: {track['title']}" if track else "music resumed"
                    elif track is None:
                        reply = "choose a song first with `!music <song or URL>`"
                    elif message.author.voice is None or message.author.voice.channel is None:
                        reply = "join a voice channel first, then use `!music start`"
                    else:
                        try:
                            target_channel = message.author.voice.channel
                            if voice_client is None:
                                voice_client = await target_channel.connect()
                            elif voice_client.channel.id != target_channel.id:
                                await voice_client.move_to(target_channel)
                            refreshed = await self.resolve_music(track["query"])
                            self.music_tracks[guild_id] = refreshed
                            self.play_music_track(voice_client, refreshed)
                            reply = f"playing: {refreshed['title']}"
                        except (discord.ClientException, discord.Forbidden, discord.HTTPException, OSError, RuntimeError, ValueError) as exc:
                            log.exception("Could not start music")
                            reply = f"I couldn't start music: {type(exc).__name__}"
                elif action_lower in {"stop", "skip"}:
                    if voice_client is not None and (voice_client.is_playing() or voice_client.is_paused()):
                        voice_client.stop()
                        reply = "music stopped" if action_lower == "stop" else "skipped"
                    else:
                        reply = "nothing is playing"
                else:
                    if message.author.voice is None or message.author.voice.channel is None:
                        reply = "join a voice channel first, then use `!music <song or URL>`"
                    else:
                        try:
                            target_channel = message.author.voice.channel
                            if voice_client is None:
                                voice_client = await target_channel.connect()
                            elif voice_client.channel.id != target_channel.id:
                                await voice_client.move_to(target_channel)
                            track = await self.resolve_music(action)
                            if voice_client.is_playing() or voice_client.is_paused():
                                voice_client.stop()
                            self.music_tracks[guild_id] = track
                            self.play_music_track(voice_client, track)
                            reply = f"playing: {track['title']}"
                        except (discord.ClientException, discord.Forbidden, discord.HTTPException, OSError, RuntimeError, ValueError) as exc:
                            log.exception("Could not play music")
                            reply = f"I couldn't play that: {type(exc).__name__}"
            await message.channel.send(
                reply,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if parts and parts[0].lower() == "!persona":
            requested = parts[1].strip().lower() if len(parts) == 2 else ""
            if not requested:
                reply = f"persona: {settings.MODEL_PERSONAS.get(self.selected_model, 'rudeish')} ({self.active_model})"
            elif requested not in settings.PERSONA_ALIASES:
                reply = (
                    "usage: !persona rudeish, !persona nerdish, or !persona explicit"
                )
            elif (
                settings.PERSONA_ALIASES[requested] == "deepseek"
                and not settings.DEEPSEEK_API_KEY
            ):
                reply = "deepseek is not configured (set DEEPSEEK_API_KEY first)"
            elif (
                settings.PERSONA_ALIASES[requested] == "mistral"
                and not settings.MISTRAL_API_KEY
            ):
                reply = "mistral is not configured (set MISTRAL_API_KEY first)"
            else:
                self.selected_model = settings.PERSONA_ALIASES[requested]
                self.memory.set_setting("selected_persona_model", self.selected_model)
                reply = f"persona: {requested} ({self.active_model})"
            await message.channel.send(
                reply,
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if message.guild is not None and parts and parts[0].lower() == "!nuke":
            if (
                len(parts) == 2
                and parts[1].isdigit()
                and 1 <= int(parts[1]) <= settings.MAX_NUKE_MESSAGES
                and isinstance(message.channel, discord.TextChannel)
                and message.author.guild_permissions.manage_messages
                and message.channel.permissions_for(message.guild.me).manage_messages
            ):
                await message.channel.purge(limit=int(parts[1]))
            return

        is_dm = message.guild is None
        mentioned = self.user is not None and self.user in message.mentions
        active_enabled = False
        active_turn = False
        gif_enabled = False
        gif_turn = False
        topic = ""
        if not is_dm:
            scope_id = str(message.channel.id)
            topic = await asyncio.to_thread(self.memory.channel_topic, scope_id)
            if not message.content.lstrip().startswith("!"):
                (active_enabled, _), (gif_enabled, _) = await asyncio.gather(
                    asyncio.to_thread(self.memory.active_mode_status, scope_id),
                    asyncio.to_thread(self.memory.gif_mode_status, scope_id),
                )
                cadence_checks = []
                if active_enabled:
                    cadence_checks.append(
                        asyncio.to_thread(
                            self.memory.record_active_message,
                            scope_id,
                            interval=settings.ACTIVE_RESPONSE_INTERVAL,
                        )
                    )
                if gif_enabled:
                    cadence_checks.append(
                        asyncio.to_thread(
                            self.memory.record_gif_message,
                            scope_id,
                            interval=settings.GIF_RESPONSE_INTERVAL,
                        )
                    )
                results = await asyncio.gather(*cadence_checks)
                result_index = 0
                if active_enabled:
                    active_turn = results[result_index]
                    result_index += 1
                if gif_enabled:
                    gif_turn = results[result_index]
        should_ai_reply = is_dm or mentioned or active_turn
        if not (should_ai_reply or gif_turn):
            return
        prompt = message.content
        if self.user is not None:
            prompt = (
                prompt.replace(f"<@{self.user.id}>", "")
                .replace(f"<@!{self.user.id}>", "")
                .strip()
            )
        if not prompt and not any(
            image_url(attachment) for attachment in message.attachments
        ):
            prompt = "Hello."

        guild_id = message.guild.id if message.guild is not None else None
        admitted, retry_after = self.admit_request(message.author.id, guild_id=guild_id)
        if not admitted:
            await message.channel.send(
                f"slow down try again in {retry_after}s",
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if gif_turn:
            gif_query = topic or prompt[: settings.MAX_TOPIC_CHARS]
            gif_url = await self.search_gif(gif_query)
            if gif_url is not None:
                await message.channel.send(
                    gif_url,
                    reference=message,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if not should_ai_reply:
                return

        scope_id, user_id = self.conversation_key(message)
        key = (scope_id, user_id, self.active_model)
        current_task = asyncio.current_task()
        previous = self.active_requests.get(key)
        if (
            previous is not None
            and previous is not current_task
            and not previous.done()
        ):
            previous.cancel()
        if current_task is not None:
            self.active_requests[key] = current_task

        streaming_message: discord.Message | None = None
        streamed_text = ""
        last_stream_edit = 0.0

        async def show_delta(text: str) -> None:
            nonlocal streaming_message, streamed_text, last_stream_edit
            streamed_text = text
            now = time.monotonic()
            if (
                now - last_stream_edit < settings.STREAM_EDIT_INTERVAL
                and len(text) < settings.DISCORD_MESSAGE_LIMIT
            ):
                return
            preview = text[: settings.DISCORD_MESSAGE_LIMIT].strip() or "…"
            try:
                if streaming_message is None:
                    streaming_message = await message.channel.send(
                        preview,
                        reference=message,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await streaming_message.edit(content=preview)
                last_stream_edit = now
            except (discord.HTTPException, discord.Forbidden):
                log.exception("Could not update streaming Discord reply")

        try:
            async with self.conversation_locks[key]:
                async with message.channel.typing():
                    answer = await self.ask(
                        message,
                        prompt,
                        on_delta=show_delta,
                        active_mode=active_enabled,
                        topic=topic,
                    )
            if answer is None:
                return
            chunks = split_discord_message(answer)
            if streaming_message is not None:
                if streaming_message.content != chunks[0]:
                    await streaming_message.edit(content=chunks[0])
                for chunk in chunks[1:]:
                    await message.channel.send(
                        chunk, allowed_mentions=discord.AllowedMentions.none()
                    )
            else:
                for chunk in chunks:
                    await message.channel.send(
                        chunk,
                        reference=message,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except asyncio.CancelledError:
            if streaming_message is not None and streamed_text:
                try:
                    await streaming_message.delete()
                except (discord.HTTPException, discord.Forbidden):
                    pass
            return
        except ModerationBlocked as exc:
            await message.channel.send(
                f"AI requests are temporarily unavailable. Try again in {exc.retry_after}s.",
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except ModerationRejected:
            await message.channel.send(
                "This request cannot be processed.",
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except ModerationUnavailable:
            await message.channel.send(
                "I can't complete a safety check right now. Please try again later.",
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except InputTooLarge:
            await message.channel.send(
                "that message is too large please shorten it or attach fewer images",
                reference=message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("AI reply failed in channel %s", message.channel.id)
            error_text = "I couldn't reach the AI provider just now."
            try:
                if streaming_message is not None:
                    await streaming_message.edit(content=error_text)
                else:
                    await message.channel.send(
                        error_text,
                        reference=message,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except (discord.HTTPException, discord.Forbidden):
                log.exception("Could not send provider failure message")
        finally:
            if self.active_requests.get(key) is current_task:
                self.active_requests.pop(key, None)

    async def close(self) -> None:
        for task in list(self.background_tasks):
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        await self.provider_http.aclose()
        await super().close()
