"""Async OpenAI-compatible client for modular AI features."""

import asyncio
import base64
import ipaddress
import json
import logging
import random
import socket
import time
import typing
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

from owaua import ai_control, config

log = logging.getLogger("owaua.services.llm")

_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRYABLE_EXC = (httpx.TimeoutException, httpx.TransportError)
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_MAX_DOWNLOAD_REDIRECTS = 3
_MAX_PROVIDER_RESPONSE_BYTES = 4_000_000
_MAX_AUDIO_RESPONSE_BYTES = 20_000_000
_ALLOWED_MEDIA_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}


class LLMError(RuntimeError):
    """Raised when an upstream LLM call fails after retries."""


def _safe_provider_base(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise LLMError("invalid provider endpoint") from exc
    is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (is_local and not config.ALLOW_LOCAL_ENDPOINTS)
        or (parsed.scheme != "https" and not (config.ALLOW_LOCAL_ENDPOINTS and is_local))
    ):
        raise LLMError("provider endpoint must be HTTPS and contain no credentials")
    return url.rstrip("/")


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        raw = resp.headers.get("retry-after")
        if raw:
            try:
                return min(30.0, max(0.1, float(raw)))
            except ValueError:
                pass
    return min(30.0, (2**attempt) + random.uniform(0.0, 0.5))  # noqa: S311


def _extract_json(raw: str) -> Optional[dict[typing.Any, typing.Any]]:
    """Pull the first JSON object out of a model reply (strips code fences)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def sniff_image_mime(data: bytes) -> Optional[str]:
    """Recognize image formats accepted by the vision endpoint."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def coerce_bool(value: Any) -> bool:
    """Interpret JSON-ish booleans without treating the string "false" as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _validate_download_url(url: str) -> bool:
    """Allow only public HTTP or HTTPS URLs."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password or port not in {None, 80, 443}:
        return False
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(
                    host,
                    port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                ),
            )
        except socket.gaierror:
            return False
        addresses = {record[4][0] for record in records}
        return bool(addresses) and all(
            _is_public_ip(typing.cast(typing.Any, address)) for address in addresses
        )
    return _is_public_ip(host)


class LLMClient:
    """Thin async wrapper around an OpenAI-compatible chat completions API."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
        connect_timeout: float = 15.0,
        max_retries: int = 1,
    ) -> None:
        self.base_url = _safe_provider_base(base_url or config.LLM_BASE_URL)
        self.api_key = api_key if api_key is not None else config.LLM_API_KEY
        self.max_retries = max(
            0, min(int(max_retries), max(0, int(config.AI_MAX_PROVIDER_ATTEMPTS) - 1))
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        """Close the underlying httpx client (call on bot shutdown)."""
        await self._client.aclose()

    def _resolve(self, base_url: Optional[str], api_key: Optional[str]) -> Tuple[str, str]:
        return (
            _safe_provider_base(base_url or self.base_url),
            (api_key or self.api_key or "").strip(),
        )

    async def _post_json(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        *,
        retries: Optional[int] = None,
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST JSON with retries + exponential backoff on 429/5xx/timeouts."""
        attempts = (
            self.max_retries
            if retries is None
            else max(0, min(int(retries), max(0, int(config.AI_MAX_PROVIDER_ATTEMPTS) - 1)))
        )
        health_key = str(payload.get("model") or url)[:160]
        if not ai_control.provider_available(health_key):
            raise LLMError("provider circuit is temporarily open")
        estimated_tokens = ai_control.estimate_chat_tokens("", payload.get("messages") or []) + max(
            0, int(payload.get("max_tokens") or 0)
        )
        for attempt in range(attempts + 1):
            ai_control.reserve_provider_attempt(
                scope_id=scope_id,
                user_id=user_id,
                estimated_tokens=estimated_tokens,
            )
            started = time.perf_counter()
            try:
                resp = await self._client.post(url, headers=headers, json=payload)
            except _RETRYABLE_EXC as e:
                ai_control.record_provider_result(
                    health_key,
                    success=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=e,
                )
                if attempt >= attempts:
                    error_id = uuid.uuid4().hex[:10]
                    log.error(
                        "llm transport failure id=%s after retries (%s)",
                        error_id,
                        type(e).__name__,
                    )
                    raise LLMError(f"provider request failed (error {error_id})") from e
                log.warning("llm transport error (attempt %s): %s", attempt + 1, type(e).__name__)
                await asyncio.sleep(_retry_delay(None, attempt))
                continue
            if resp.status_code in _RETRY_STATUS and attempt < attempts:
                ai_control.record_provider_result(
                    health_key,
                    success=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=LLMError(f"HTTP {resp.status_code}"),
                )
                log.warning("llm http %s (attempt %s)", resp.status_code, attempt + 1)
                await asyncio.sleep(_retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                ai_control.record_provider_result(
                    health_key,
                    success=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=LLMError(f"HTTP {resp.status_code}"),
                )
                error_id = uuid.uuid4().hex[:10]
                log.error("llm http failure id=%s status=%s", error_id, resp.status_code)
                raise LLMError(f"provider returned HTTP {resp.status_code} (error {error_id})")
            if len(resp.content) > _MAX_PROVIDER_RESPONSE_BYTES:
                raise LLMError("provider response exceeded the size limit")
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                ai_control.record_provider_result(
                    health_key,
                    success=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=e,
                )
                raise LLMError("provider returned invalid JSON") from e
            ai_control.record_provider_result(
                health_key,
                success=True,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            return data
        raise LLMError("provider request exhausted retries")

    async def _chat_completion(
        self,
        payload: Dict[str, Any],
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        task: str = "workflow",
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        base, key = self._resolve(base_url, api_key)
        if not key:
            raise LLMError("no API key configured for LLM call")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        ai_control.check_request_budget(scope_id, task, user_id=user_id)
        data = await self._post_json(
            f"{base}/chat/completions",
            headers,
            payload,
            scope_id=scope_id,
            user_id=user_id,
        )
        choices: typing.Any = data.get("choices") or []
        if not choices:
            raise LLMError("provider returned no completion choices")
        return choices[0].get("message") or {}

    async def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 800,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        task: str = "workflow",
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """Run a chat completion and return the assistant's text."""
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}, *msgs]
        payload = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        msg = await self._chat_completion(
            payload,
            base_url=base_url,
            api_key=api_key,
            task=task,
            scope_id=scope_id,
            user_id=user_id,
        )
        return str(msg.get("content") or "").strip()

    async def chat_with_tools(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        *,
        system: Optional[str] = None,
        tool_choice: str = "auto",
        temperature: float = 0.3,
        max_tokens: int = 1000,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        task: str = "assistant",
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Tuple[str, List[Dict[str, str]]]:
        """Return text and tool calls from a chat completion."""
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}, *msgs]
        payload = {
            "model": model,
            "messages": msgs,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        msg = await self._chat_completion(
            payload,
            base_url=base_url,
            api_key=api_key,
            task=task,
            scope_id=scope_id,
            user_id=user_id,
        )
        text = str(msg.get("content") or "").strip()
        calls: List[Dict[str, str]] = []
        for tc in typing.cast(typing.Iterable[typing.Any], msg.get("tool_calls") or []):
            fn: typing.Any = typing.cast(typing.Any, tc.get("function") or {})
            calls.append(
                {
                    "name": str(fn.get("name") or ""),
                    "arguments": str(fn.get("arguments") or "{}"),
                }
            )
        return text, calls

    async def chat_json(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 600,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        task: str = "workflow",
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[dict[typing.Any, typing.Any]]:
        """Chat completion forced to return a single JSON object."""
        sys_text = system or "Respond with ONLY a single valid JSON object, no prose."
        raw = await self.chat(
            model,
            messages,
            system=sys_text,
            temperature=temperature,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
            task=task,
            scope_id=scope_id,
            user_id=user_id,
        )
        return _extract_json(raw)

    async def vision(
        self,
        model: str,
        image_bytes: bytes,
        prompt: str,
        *,
        mime: str = "image/png",
        max_tokens: int = 500,
        temperature: float = 0.2,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        task: str = "vision",
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """Send an image (inlined base64) to a vision model and return its text."""
        parts = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": _data_url(image_bytes, mime)},
            },
        ]
        return await self.chat(
            model,
            [{"role": "user", "content": parts}],
            temperature=temperature,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
            task=task,
            scope_id=scope_id,
            user_id=user_id,
        )

    async def vision_json(
        self,
        model: str,
        image_bytes: bytes,
        prompt: str,
        *,
        mime: str = "image/png",
        max_tokens: int = 700,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        task: str = "vision",
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Optional[dict[typing.Any, typing.Any]]:
        """Vision pass that must return a JSON object (e.g. description + flag)."""
        parts = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": _data_url(image_bytes, mime)},
            },
        ]
        raw = await self.chat(
            model,
            [{"role": "user", "content": parts}],
            system="Respond with ONLY a single valid JSON object, no prose.",
            temperature=0.2,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
            task=task,
            scope_id=scope_id,
            user_id=user_id,
        )
        return _extract_json(raw)

    async def get_image(
        self, url: str, *, max_bytes: Optional[int] = None
    ) -> Optional[Tuple[bytes, str]]:
        """Download a bounded public raster image."""
        limit = max_bytes or config.VISION_MAX_IMAGE_BYTES
        current = (url or "").strip()
        headers = {"User-Agent": "Mozilla/5.0 (compatible; owaua/1.0)"}
        for redirect_count in range(_MAX_DOWNLOAD_REDIRECTS + 1):
            if not await _validate_download_url(current):
                log.warning("blocked unsafe image URL")
                return None
            try:
                media_host = (urlsplit(current).hostname or "").lower().rstrip(".")
            except ValueError:
                return None
            if media_host not in _ALLOWED_MEDIA_HOSTS:
                log.warning("blocked non-Discord media host")
                return None
            try:
                async with self._client.stream(
                    "GET",
                    current,
                    headers=headers,
                    timeout=httpx.Timeout(20.0, connect=10.0),
                    follow_redirects=False,
                ) as resp:
                    if resp.status_code in _REDIRECT_STATUS:
                        if redirect_count >= _MAX_DOWNLOAD_REDIRECTS:
                            return None
                        location = resp.headers.get("location")
                        if not location:
                            return None
                        current = urljoin(current, location)
                        continue
                    if resp.status_code != 200:
                        log.warning("media download http %s", resp.status_code)
                        return None
                    declared_length = resp.headers.get("content-length")
                    if declared_length:
                        try:
                            if int(declared_length) > limit:
                                return None
                        except ValueError:
                            return None
                    declared_type = resp.headers.get("content-type", "").split(";", 1)[0].lower()
                    if declared_type and not declared_type.startswith("image/"):
                        return None
                    chunks = bytearray()
                    async for chunk in resp.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            return None
            except _RETRYABLE_EXC as e:
                log.warning("media download failed: %s", type(e).__name__)
                return None
            data = bytes(chunks)
            mime = sniff_image_mime(data)
            if mime is None:
                log.warning("media download was not a supported image")
                return None
            return data, mime
        return None

    async def get_bytes(self, url: str, *, max_bytes: int = 8_000_000) -> Optional[bytes]:
        """Backward-compatible wrapper returning only verified image bytes."""
        result = await self.get_image(url, max_bytes=max_bytes)
        return result[0] if result else None

    async def moderate(
        self,
        model: str,
        text: str,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Passive content moderation. Returns ``{"flagged", "category", "reason", "confidence"}``."""
        system = (
            "You are a content moderation classifier for a Discord server that allows dark humor, "
            "edgy jokes, and NSFW text. Given a chat message, first decide whether it is a joke "
            "(lol/lmao/jk//j, sarcasm, exaggeration, memes, dark humor). Jokes and banter are "
            "allowed and must NOT be flagged unless they are genuine harassment directed at a "
            "person, hate speech with slurs, threats, illegal activity, sexual content involving "
            "minors, doxxing, or spam/scams. Genuine self-harm or suicidal ideation is a crisis, "
            "NOT a violation — never flag it. "
            'Reply with ONLY JSON: {"flagged": true/false, "category": "harassment|hate|sexual|'
            'violence|self_harm|illegal|spam|none", "reason": "short explanation", '
            '"confidence": 0.0-1.0}.'
        )
        result = await self.chat_json(
            model,
            [{"role": "user", "content": text[:1500]}],
            system=system,
            temperature=0.0,
            base_url=base_url,
            api_key=api_key,
            task="moderation",
            scope_id=scope_id,
            user_id=user_id,
        )
        if not result:
            return {"flagged": False, "category": "none", "reason": "", "confidence": 0.0}
        categories = {
            "harassment",
            "hate",
            "sexual",
            "violence",
            "self_harm",
            "illegal",
            "spam",
            "none",
        }
        category = str(result.get("category") or "none").lower()
        try:
            confidence = min(1.0, max(0.0, float(result.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        return {
            "flagged": coerce_bool(result.get("flagged")),
            "category": category if category in categories else "none",
            "reason": str(result.get("reason") or "")[:500],
            "confidence": confidence,
        }

    async def transcribe(
        self,
        model: str,
        audio_bytes: bytes,
        *,
        filename: str = "audio.wav",
        mime: str = "audio/wav",
        language: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> str:
        """Speech-to-text via the OpenAI-compatible /audio/transcriptions endpoint."""
        base, key = self._resolve(base_url, api_key)
        if not key:
            raise LLMError("no API key configured for transcription")
        headers = {"Authorization": f"Bearer {key}"}
        ai_control.check_request_budget(scope_id, "transcription", user_id=user_id)
        data: Dict[str, Any] = {"model": model}
        if language:
            data["language"] = language
        for attempt in range(self.max_retries + 1):
            ai_control.reserve_provider_attempt(scope_id=scope_id, user_id=user_id)
            try:
                resp = await self._client.post(
                    f"{base}/audio/transcriptions",
                    headers=headers,
                    data=data,
                    files={"file": (filename, audio_bytes, mime)},
                )
            except _RETRYABLE_EXC as e:
                if attempt >= self.max_retries:
                    raise LLMError("transcription provider request failed") from e
                await asyncio.sleep(_retry_delay(None, attempt))
                continue
            if resp.status_code in _RETRY_STATUS and attempt < self.max_retries:
                await asyncio.sleep(_retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"transcription provider returned HTTP {resp.status_code}")
            if len(resp.content) > _MAX_PROVIDER_RESPONSE_BYTES:
                raise LLMError("transcription response exceeded the size limit")
            try:
                return str(resp.json().get("text") or "").strip()
            except json.JSONDecodeError as e:
                raise LLMError("transcription returned invalid JSON") from e
        raise LLMError("transcription exhausted retries")

    async def speak(
        self,
        model: str,
        text: str,
        *,
        voice: str = "troy",
        response_format: str = "wav",
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        scope_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> bytes:
        """Text-to-speech via the OpenAI-compatible /audio/speech endpoint. Returns audio bytes."""
        base, key = self._resolve(base_url, api_key)
        if not key:
            raise LLMError("no API key configured for TTS")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        ai_control.check_request_budget(scope_id, "speech", user_id=user_id)
        payload = {
            "model": model,
            "input": text[:200],
            "voice": voice,
            "response_format": response_format,
        }
        for attempt in range(self.max_retries + 1):
            ai_control.reserve_provider_attempt(
                scope_id=scope_id,
                user_id=user_id,
                estimated_tokens=ai_control.estimate_tokens(payload),
            )
            try:
                resp = await self._client.post(
                    f"{base}/audio/speech", headers=headers, json=payload
                )
            except _RETRYABLE_EXC as e:
                if attempt >= self.max_retries:
                    raise LLMError("TTS provider request failed") from e
                await asyncio.sleep(_retry_delay(None, attempt))
                continue
            if resp.status_code in _RETRY_STATUS and attempt < self.max_retries:
                await asyncio.sleep(_retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise LLMError(f"TTS provider returned HTTP {resp.status_code}")
            if len(resp.content) > _MAX_AUDIO_RESPONSE_BYTES:
                raise LLMError("TTS response exceeded the size limit")
            return resp.content
        raise LLMError("tts exhausted retries")


llm = LLMClient()
