"""Provider requests, moderation, and durable-memory workflows.

Settings are read from :mod:`bot` at call time so operational overrides remain
effective without duplicating configuration.
"""

from __future__ import annotations

import asyncio
import json
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
    ProviderError,
    attachment_metadata,
    build_instructions,
    chat_completion_text,
    classify_message,
    contains_self_harm_language,
    credible_self_harm_risk,
    estimate_tokens,
    image_url,
    is_unlimited_guild,
    looks_like_leaked_reasoning,
    model_context_limits,
    model_output_limit,
    moderation_result_is_rejected,
    quality_issues,
    response_text,
    safety_identifier,
    split_discord_message,
    to_chat_completions_payload,
    truncate_for_context,
    uses_chat_completions,
)

log = logging.getLogger("owaua")


class BotService:

    def memory_generation(self, scope_id: str) -> int | None:
        """Get the guild wipe generation when the backing store supports it."""
        getter = getattr(self.memory, "scope_generation", None)
        return getter(scope_id) if getter is not None else None

    @property
    def active_model(self) -> str:
        alias = getattr(self, "selected_model", "gpt")
        return settings.MODEL_ALIASES.get(alias, settings.GPT_MODEL)

    @property
    def explicit_roleplay(self) -> bool:
        return self.active_model == settings.MISTRAL_MODEL

    @staticmethod
    def provider_settings(model: str) -> tuple[str, str]:
        if model == settings.DEEPSEEK_MODEL:
            return settings.DEEPSEEK_API_KEY, settings.DEEPSEEK_BASE_URL
        if model == settings.MISTRAL_MODEL:
            return settings.MISTRAL_API_KEY, settings.MISTRAL_BASE_URL
        return settings.OPENAI_API_KEY, settings.OPENAI_BASE_URL

    @staticmethod
    def conversation_key(message: discord.Message) -> tuple[str, str]:
        return str(message.channel.id), str(message.author.id)

    def memory_get(
        self, scope_id: str, user_id: str, model_id: str
    ) -> tuple[str, list[str], int]:
        """Read provider-specific memory, with compatibility for lightweight test stores."""
        try:
            return self.memory.get_memory(scope_id, user_id, model_id=model_id)
        except TypeError as exc:
            if "model_id" not in str(exc):
                raise
            return self.memory.get_memory(scope_id, user_id)

    def memory_recent(
        self, scope_id: str, user_id: str, *, limit: int, model_id: str
    ) -> list[dict[str, object]]:
        try:
            return self.memory.recent_messages(
                scope_id, user_id, limit=limit, model_id=model_id
            )
        except TypeError as exc:
            if "model_id" not in str(exc):
                raise
            return self.memory.recent_messages(scope_id, user_id, limit=limit)

    def memory_to_summarize(
        self,
        scope_id: str,
        user_id: str,
        *,
        keep_recent: int,
        limit: int,
        model_id: str,
    ) -> list[dict[str, object]]:
        try:
            return self.memory.messages_to_summarize(
                scope_id,
                user_id,
                keep_recent=keep_recent,
                limit=limit,
                model_id=model_id,
            )
        except TypeError as exc:
            if "model_id" not in str(exc):
                raise
            return self.memory.messages_to_summarize(
                scope_id, user_id, keep_recent=keep_recent, limit=limit
            )

    def admit_request(
        self, user_id: int, *, guild_id: int | None = None
    ) -> tuple[bool, int]:
        if is_unlimited_guild(guild_id):
            return True, 0
        now = time.monotonic()
        window = self.rate_windows[user_id]
        while window and now - window[0] >= settings.RATE_LIMIT_WINDOW:
            window.popleft()
        if len(window) >= settings.RATE_LIMIT_REQUESTS:
            retry_after = max(
                1, int(settings.RATE_LIMIT_WINDOW - (now - window[0]) + 0.999)
            )
            return False, retry_after
        window.append(now)
        return True, 0

    def moderation_block_retry_after(self, user_id: int) -> int:
        now = time.monotonic()
        blocked_until = self.moderation_blocks.get(user_id)
        if blocked_until is None:
            return 0
        if blocked_until <= now:
            self.moderation_blocks.pop(user_id, None)
            return 0
        return max(1, int(blocked_until - now + 0.999))

    def record_moderation_rejection(self, user_id: int, *, image_count: int) -> None:
        now = time.monotonic()
        window = self.moderation_failures[user_id]
        while window and now - window[0] >= settings.MODERATION_ABUSE_WINDOW:
            window.popleft()
        window.append(now)
        if len(window) >= settings.MODERATION_ABUSE_MAX_FLAGGED:
            self.moderation_blocks[user_id] = (
                now + settings.MODERATION_ABUSE_BLOCK_SECONDS
            )
        log.info(
            "Moderation rejected input; user=%s images=%s recent_rejections=%s blocked=%s",
            safety_identifier(user_id)[:12],
            image_count,
            len(window),
            user_id in self.moderation_blocks,
        )

    async def moderate_user_input(
        self,
        user_id: int,
        prompt: str,
        image_urls: list[str],
        attachment_filenames: list[str] | None = None,
        *,
        allow_adult_sexual: bool = False,
        guild_id: int | None = None,
    ) -> None:
        """Fail closed before a Discord input can reach memory or Responses."""
        if is_unlimited_guild(guild_id):
            return
        retry_after = self.moderation_block_retry_after(user_id)
        if retry_after:
            raise ModerationBlocked(retry_after)

        moderation_input: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        moderation_input.extend(
            {"type": "text", "text": f"Attachment filename: {filename}"}
            for filename in (attachment_filenames or [])
        )
        moderation_input.extend(
            {
                "type": "image_url",
                "image_url": {"url": url},
            }
            for url in image_urls
        )
        headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        try:
            response = await self.provider_http.post(
                f"{settings.OPENAI_BASE_URL}/moderations",
                headers=headers,
                json={"model": settings.MODERATION_MODEL, "input": moderation_input},
                timeout=settings.MODERATION_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            log.warning("Moderation request failed; error=%s", type(exc).__name__)
            raise ModerationUnavailable("Moderation request failed") from exc

        if not isinstance(data, dict):
            log.warning("Moderation response had an invalid top-level shape")
            raise ModerationUnavailable("Moderation response was malformed")
        results = data.get("results")
        if not isinstance(results, list) or not results:
            log.warning("Moderation response did not contain results")
            raise ModerationUnavailable("Moderation response was malformed")
        flagged = False
        for result in results:
            if not isinstance(result, dict) or not isinstance(
                result.get("flagged"), bool
            ):
                log.warning("Moderation response contained an invalid result")
                raise ModerationUnavailable("Moderation response was malformed")
            try:
                flagged = flagged or moderation_result_is_rejected(
                    result, allow_adult_sexual=allow_adult_sexual
                )
            except ModerationUnavailable:
                log.warning("Moderation response contained an invalid result")
                raise
        if flagged:
            if not is_unlimited_guild(guild_id):
                self.record_moderation_rejection(user_id, image_count=len(image_urls))
            raise ModerationRejected("Moderation rejected the input")

    async def _request_once(
        self,
        payload: dict[str, object],
        *,
        on_delta: settings.DeltaCallback | None = None,
        api_key: str,
        base_url: str,
    ) -> str:
        chat = uses_chat_completions(base_url)
        request_payload = to_chat_completions_payload(payload) if chat else payload
        path = "chat/completions" if chat else "responses"
        extract_text = chat_completion_text if chat else response_text
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if on_delta is None:
            response = await self.provider_http.post(
                f"{base_url}/{path}", headers=headers, json=request_payload
            )
            response.raise_for_status()
            try:
                data = response.json()
            except (TypeError, ValueError) as exc:
                raise ProviderError("The AI provider returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise ProviderError("The AI provider returned invalid JSON")
            answer = extract_text(data)
            if not answer:
                raise ProviderError("The AI provider returned an empty response")
            return answer

        stream_payload = dict(request_payload)
        stream_payload["stream"] = True
        pieces: list[str] = []
        async with self.provider_http.stream(
            "POST", f"{base_url}/{path}", headers=headers, json=stream_payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str):
                        pieces.append(delta)
                        await on_delta("".join(pieces))
                elif event.get("type") == "response.failed":
                    raise ProviderError("The streamed AI response failed")
                else:
                    choices = event.get("choices")
                    if (
                        isinstance(choices, list)
                        and choices
                        and isinstance(choices[0], dict)
                    ):
                        delta = choices[0].get("delta")
                        if isinstance(delta, dict):
                            piece = delta.get("content")
                            if isinstance(piece, str) and piece:
                                pieces.append(piece)
                                await on_delta("".join(pieces))
        answer = "".join(pieces).strip()
        if not answer:
            raise ProviderError("The AI provider returned an empty streamed response")
        return answer

    async def request_ai(
        self,
        payload: dict[str, object],
        *,
        on_delta: settings.DeltaCallback | None = None,
        allow_fallback: bool = True,
    ) -> str:
        selected = str(payload["model"])
        if selected not in settings.ALLOWED_MODELS:
            raise ProviderError(f"Unsupported model: {selected}")
        models = [selected]
        if (
            allow_fallback
            and settings.FALLBACK_MODEL
            and settings.FALLBACK_MODEL not in models
            and self.provider_settings(selected)[1]
            == self.provider_settings(settings.FALLBACK_MODEL)[1]
        ):
            models.append(settings.FALLBACK_MODEL)
        last_error: Exception | None = None
        for model in models:
            model_payload = dict(payload)
            model_payload["model"] = model
            api_key, base_url = self.provider_settings(model)
            if not api_key:
                last_error = ProviderError(f"No API key configured for model {model}")
                continue
            for attempt in range(settings.REQUEST_RETRIES + 1):
                try:
                    return await self._request_once(
                        model_payload,
                        on_delta=on_delta,
                        api_key=api_key,
                        base_url=base_url,
                    )
                except asyncio.CancelledError:
                    raise
                except (httpx.HTTPError, ProviderError) as exc:
                    last_error = exc
                    status = (
                        exc.response.status_code
                        if isinstance(exc, httpx.HTTPStatusError)
                        else None
                    )
                    retryable = status is None or status == 429 or status >= 500
                    if not retryable or attempt >= settings.REQUEST_RETRIES:
                        break
                    delay = min(8.0, 0.5 * (2**attempt))
                    if isinstance(exc, httpx.HTTPStatusError):
                        retry_after = exc.response.headers.get("retry-after")
                        try:
                            delay = min(15.0, max(delay, float(retry_after or 0)))
                        except ValueError:
                            pass
                    log.warning(
                        "AI request failed; model=%s status=%s retry=%s/%s",
                        model,
                        status,
                        attempt + 1,
                        settings.REQUEST_RETRIES,
                    )
                    await asyncio.sleep(delay)
            on_delta = None
        raise ProviderError("The AI provider rejected the request") from last_error

    async def revise_answer(
        self,
        *,
        payload: dict[str, object],
        answer: str,
        issues: list[str],
    ) -> str:
        revision_payload = dict(payload)
        revision_input = list(payload["input"])  # type: ignore[arg-type]
        revision_input.extend(
            [
                {"role": "assistant", "content": answer},
                {
                    "role": "user",
                    "content": (
                        "Silently rewrite that draft once. Keep its meaning and persona, but fix: "
                        + "; ".join(issues)
                        + ". Return only the corrected reply."
                    ),
                },
            ]
        )
        revision_payload["input"] = revision_input
        return await self.request_ai(revision_payload, allow_fallback=True)

    async def ask(
        self,
        message: discord.Message,
        prompt: str,
        *,
        on_delta: settings.DeltaCallback | None = None,
    ) -> str | None:
        scope_id, user_id = self.conversation_key(message)
        guild = getattr(message, "guild", None)
        guild_id = guild.id if guild is not None else None
        unlimited_guild = is_unlimited_guild(guild_id)
        if not unlimited_guild and len(prompt) > settings.MAX_MESSAGE_CHARS:
            raise InputTooLarge("message exceeds the configured character limit")
        if not unlimited_guild and len(message.attachments) > settings.MAX_ATTACHMENTS:
            raise InputTooLarge("too many attachments")
        metadata = attachment_metadata(message)
        image_urls = [
            url for attachment in message.attachments if (url := image_url(attachment))
        ]

        await self.moderate_user_input(
            message.author.id,
            prompt,
            image_urls,
            [item["filename"] for item in metadata],
            allow_adult_sexual=self.explicit_roleplay,
            guild_id=guild_id,
        )

        # Capture the provider for this turn so a persona switch cannot move a
        # message or its summary into another provider's memory mid-request.
        active_model = self.active_model
        expected_generation = self.memory_generation(scope_id)

        inserted = await asyncio.to_thread(
            self.memory.append_message,
            event_id=f"discord:{message.id}",
            model_id=active_model,
            scope_id=scope_id,
            user_id=user_id,
            role="user",
            content=prompt,
            attachments=metadata,
            created_at=message.created_at.timestamp(),
            expected_generation=expected_generation,
        )
        if not inserted:
            log.info("Ignoring duplicate Discord event %s", message.id)
            return None

        if credible_self_harm_risk(prompt):
            answer = (
                "hey im taking that seriously for a sec are u in immediate danger "
                "call ur local emergency services now and tell someone near u to stay with u"
            )
            await asyncio.to_thread(
                self.memory.append_message,
                event_id=f"assistant:{message.id}",
                model_id=active_model,
                scope_id=scope_id,
                user_id=user_id,
                role="assistant",
                content=answer,
                expected_generation=expected_generation,
            )
            return answer

        if contains_self_harm_language(prompt):
            # Do not let a provider invent a crisis-counselling script for
            # ambiguous hyperbole. Explicit imminent risk remains handled by
            # the deterministic emergency handoff above.
            answer = "i'm reading that as u being pissed off, not a request for advice"
            await asyncio.to_thread(
                self.memory.append_message,
                event_id=f"assistant:{message.id}",
                model_id=active_model,
                scope_id=scope_id,
                user_id=user_id,
                role="assistant",
                content=answer,
                expected_generation=expected_generation,
            )
            return answer

        context_message_limit, input_token_limit = model_context_limits(active_model)
        if unlimited_guild:
            context_message_limit = 1_000_000
            input_token_limit = 1_000_000_000
        (summary, facts, _), recent = await asyncio.gather(
            asyncio.to_thread(self.memory_get, scope_id, user_id, active_model),
            asyncio.to_thread(
                self.memory_recent,
                scope_id,
                user_id,
                limit=context_message_limit,
                model_id=active_model,
            ),
        )
        instructions = build_instructions(
            model=active_model,
            memory_summary=summary,
            facts=facts,
            message_kind=classify_message(prompt, has_image=bool(metadata)),
            explicit_roleplay=self.explicit_roleplay,
            response_language=getattr(self, "response_languages", {}).get(
                (scope_id, user_id), "English"
            ),
        )
        context_items: list[dict[str, object]] = []
        context_tokens = 0
        context_budget = max(256, input_token_limit - estimate_tokens(instructions))
        for record in reversed(recent):
            role = str(record["role"])
            text = (
                str(record["content"])
                if unlimited_guild
                else truncate_for_context(str(record["content"]))
            )
            if role == "assistant":
                candidate = {"role": "assistant", "content": text}
            else:
                historical_images = record.get("attachments") or []
                image_note = ""
                if historical_images:
                    names = ", ".join(
                        str(item.get("filename", "image"))[
                            : settings.MAX_ATTACHMENT_FILENAME_CHARS
                        ]
                        for item in historical_images[: settings.MAX_ATTACHMENTS]
                        if isinstance(item, dict)
                    )
                    image_note = (
                        f"\n[This message included image attachment(s): {names}]"
                    )
                content: list[dict[str, object]] = [
                    {
                        "type": "input_text",
                        "text": f"<user_message>\n{text}{image_note}\n</user_message>",
                    }
                ]
                if int(record["id"]) == int(recent[-1]["id"]):
                    for url in (
                        image_urls
                        if unlimited_guild
                        else image_urls[: settings.MAX_ATTACHMENTS]
                    ):
                        content.append({"type": "input_image", "image_url": url})
                candidate = {"role": "user", "content": content}

            candidate_tokens = estimate_tokens(candidate)
            if context_items and context_tokens + candidate_tokens > context_budget:
                continue
            context_items.append(candidate)
            context_tokens += candidate_tokens

        api_input = list(reversed(context_items))

        payload: dict[str, object] = {
            "model": active_model,
            "store": False,
            "instructions": instructions,
            "input": api_input,
            "safety_identifier": safety_identifier(message.author.id),
            "prompt_cache_key": f"persona:{message.author.id}",
        }
        if not unlimited_guild:
            payload["max_output_tokens"] = model_output_limit(active_model)
        answer = await self.request_ai(
            payload,
            on_delta=on_delta if settings.STREAM_RESPONSES else None,
            allow_fallback=True,
        )
        if looks_like_leaked_reasoning(answer):
            if active_model in {settings.DEEPSEEK_MODEL, settings.MISTRAL_MODEL}:
                log.warning(
                    "Discarding leaked non-GPT reasoning without a second completion; model=%s",
                    active_model,
                )
                answer = "huh"
            else:
                log.info(
                    "Discarding leaked model reasoning and requesting a public reply"
                )
                revision_payload = dict(payload)
                revision_input = list(payload["input"])  # type: ignore[arg-type]
                revision_input.append(
                    {
                        "role": "user",
                        "content": (
                            "Reply now with only the in-character Discord message. "
                            "No planning, no analysis, no persona recap."
                        ),
                    }
                )
                revision_payload["input"] = revision_input
                try:
                    rewritten = await self.request_ai(
                        revision_payload, allow_fallback=True
                    )
                    if rewritten and not looks_like_leaked_reasoning(rewritten):
                        answer = rewritten
                    else:
                        answer = "huh"
                except ProviderError:
                    answer = "huh"
        issues = quality_issues(answer)
        if issues:
            log.info(
                "Response quality issues (not retrying for latency): %s",
                "; ".join(issues),
            )

        await asyncio.to_thread(
            self.memory.append_message,
            event_id=f"assistant:{message.id}",
            model_id=active_model,
            scope_id=scope_id,
            user_id=user_id,
            role="assistant",
            content=answer,
            expected_generation=expected_generation,
        )
        if expected_generation is None:
            self.schedule_memory_refresh(scope_id, user_id)
        else:
            self.schedule_memory_refresh(
                scope_id, user_id, expected_generation=expected_generation
            )
        return answer

    def schedule_memory_refresh(
        self,
        scope_id: str,
        user_id: str,
        model_id: str | None = None,
        *,
        expected_generation: int | None = None,
    ) -> None:
        selected_model = model_id or self.active_model
        task = asyncio.create_task(
            self.refresh_memory(
                scope_id, user_id, selected_model, expected_generation=expected_generation
            )
        )
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def refresh_memory(
        self,
        scope_id: str,
        user_id: str,
        model_id: str | None = None,
        *,
        expected_generation: int | None = None,
    ) -> None:
        selected_model = model_id or self.active_model
        key = (scope_id, user_id, selected_model)
        async with self.summary_locks[key]:
            records = await asyncio.to_thread(
                self.memory_to_summarize,
                scope_id,
                user_id,
                keep_recent=model_context_limits(selected_model)[0],
                limit=settings.MEMORY_SUMMARY_BATCH,
                model_id=selected_model,
            )
            if len(records) < settings.MEMORY_SUMMARY_MIN_MESSAGES:
                return
            previous_summary, previous_facts, _ = await asyncio.to_thread(
                self.memory_get, scope_id, user_id, selected_model
            )
            summary_records: list[dict[str, object]] = []
            transcript_tokens = 0
            for record in records:
                line = f"{record['role']}: {truncate_for_context(str(record['content']), 1200)}"
                line_tokens = estimate_tokens(line)
                if (
                    summary_records
                    and transcript_tokens + line_tokens
                    > settings.MEMORY_SUMMARY_MAX_INPUT_TOKENS
                ):
                    break
                summary_records.append(record)
                transcript_tokens += line_tokens
            if not summary_records:
                return
            transcript = "\n".join(
                f"{record['role']}: {truncate_for_context(str(record['content']), 1200)}"
                for record in summary_records
            )
            payload: dict[str, object] = {
                "model": settings.MEMORY_MODEL,
                "store": False,
                "instructions": (
                    "Update durable conversation memory. Summarize continuity, running jokes, "
                    "preferences, and unresolved topics accurately. Keep only stable facts the user "
                    "explicitly stated. Never retain passwords, tokens, contact details, exact "
                    "locations, medical or crisis details, or sexual details. Return strict JSON."
                ),
                "input": (
                    f"Previous summary:\n{previous_summary or 'none'}\n\n"
                    f"Previous stable facts:\n{json.dumps(previous_facts)}\n\n"
                    f"New transcript:\n{transcript}"
                ),
                "max_output_tokens": settings.MEMORY_SUMMARY_MAX_OUTPUT_TOKENS,
                "reasoning": {"effort": "none"},
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "conversation_memory",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "summary": {"type": "string"},
                                "facts": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": 50,
                                },
                            },
                            "required": ["summary", "facts"],
                            "additionalProperties": False,
                        },
                    }
                },
            }
            try:
                raw = await self.request_ai(payload, allow_fallback=False)
                updated = json.loads(raw)
                summary = str(updated.get("summary", "")).strip()
                facts = updated.get("facts", [])
                if not isinstance(facts, list):
                    raise ValueError("memory facts are not a list")
                await asyncio.to_thread(
                    self.memory.save_memory,
                    scope_id,
                    user_id,
                    model_id=selected_model,
                    summary=summary,
                    facts=[str(fact) for fact in facts],
                    summarized_through_id=int(summary_records[-1]["id"]),
                    expected_generation=expected_generation,
                )
            except (ProviderError, ValueError, TypeError, json.JSONDecodeError):
                log.exception(
                    "Could not refresh durable memory for channel %s", scope_id
                )
