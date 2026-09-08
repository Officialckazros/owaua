"""Discord client lifecycle, commands, and message delivery.

Settings are read from :mod:`bot` at call time so operational overrides remain
effective without duplicating configuration.
"""

from __future__ import annotations

import asyncio
import logging
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
    split_discord_message,
)

log = logging.getLogger("owaua")

from bot_service import BotService


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
        self.selected_model = "mistral" if settings.MISTRAL_API_KEY else "gpt"

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

        parts = message.content.split(maxsplit=1)
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
        if not (is_dm or mentioned):
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
                    answer = await self.ask(message, prompt, on_delta=show_delta)
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
