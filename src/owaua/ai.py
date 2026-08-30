"""Thin async wrapper around the Groq API (OpenAI-compatible chat completions).

Everything runs in a thread pool because the Groq SDK is sync and we live inside
discord.py's event loop.

Model routing
-------------
* smart  — main brain / chat / recap / hard tasks  (MODEL_SMART)
* fast   — cheap tasks: custom cmds, lurk one-liners, simple tools (MODEL_FAST)
* vision — image understanding when attachments are present (MODEL_VISION)
"""

import asyncio
import concurrent.futures
import http.client
import json
import logging
import queue
import re
import threading
import time
import typing
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Union

from groq import Groq

from owaua import ai_control, config, db

_LOG = logging.getLogger(__name__)

_clients = [Groq(api_key=k, timeout=40.0, max_retries=1) for k in config.GROQ_KEYS]
_idx_lock = threading.Lock()
_idx = 0

_pool_n = (
    max(len(_clients), 1)
    + (1 if config.INCEPTION_API_KEY else 0)
    + (1 if config.CELERIS_API_KEY else 0)
)
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(16, 4 * _pool_n), thread_name_prefix="llm"
)

_ROTATE_ON = (
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "503",
    "502",
    "overloaded",
    "capacity",
    "try again",
)
_TRANSIENT_MARKERS = _ROTATE_ON + (
    "500",
    "504",
    "529",
    "empty content",
    "empty response",
    "malformed",
    "timed out",
    "timeout",
    "temporarily",
    "unavailable",
    "connection reset",
    "broken pipe",
    "remote disconnected",
    "eof occurred",
    "connection aborted",
    "provider returned error",
    "overloaded",
)
_FATAL_MARKERS = (
    "401",
    "invalid api key",
    "no groq api key",
    "no inferx api key",
    "no celeris api key",
    "no inception",
    "no deepseek api key",
    "no openrouter api key",
    "no gemini api key",
    "no cerebras api key",
    "no anthropic",
    "no model available",
)
_SAME_MODEL_ATTEMPTS = 2
_MAX_PROVIDER_RESPONSE_BYTES = 4_000_000

ContentPart = Union[str, dict[str, typing.Any]]
_RunResult = typing.TypeVar("_RunResult")


def shutdown() -> None:
    """Stop the legacy SDK worker pool during client shutdown."""
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)


def _next_index() -> int:
    global _idx
    with _idx_lock:
        i = _idx
        _idx = (_idx + 1) % len(_clients)
    return i


def _is_rate_limited(e: Exception) -> bool:
    return any(s in str(e).lower() for s in _ROTATE_ON)


def _is_gemini(model: str) -> bool:
    return str(model).startswith("gemini")


def _is_mercury(model: str) -> bool:
    """Inception Mercury models (text). Accepts mercury-2, mercury:*, inception/*."""
    m = str(model or "").strip().lower()
    if not m:
        return False
    if m.startswith("mercury:") or m.startswith("inception/"):
        return True
    if m in ("mercury-2", "mercury", "mercury-2-instant", "mercury-instant"):
        return True
    if m.startswith("mercury-"):
        return True
    return False


def _mercury_upstream_model(model: str) -> str:
    """Map alias → Inception model id (always mercury-2 today)."""
    m = str(model or "").strip()
    if m.startswith("mercury:"):
        m = m[len("mercury:") :]
    if m.startswith("inception/"):
        m = m[len("inception/") :]
    if m in ("mercury", "mercury-2-instant", "mercury-instant", "mercury-2", "") or m.startswith(
        "mercury-"
    ):
        return "mercury-2"
    return m or "mercury-2"


class HTTPSConnectionPool:
    def __init__(self, base_url: str, timeout: float = 15.0, max_size: int = 20):
        parsed = urllib.parse.urlparse(base_url)
        hostname = (parsed.hostname or "").lower()
        is_local = hostname in {"localhost", "127.0.0.1", "::1"}
        valid_https = parsed.scheme == "https" and (not is_local or config.ALLOW_LOCAL_ENDPOINTS)
        valid_dev_http = parsed.scheme == "http" and is_local and config.ALLOW_LOCAL_ENDPOINTS
        valid = bool(
            hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (valid_https or valid_dev_http)
        )
        self.host = parsed.netloc if valid else ""
        self.path_prefix = parsed.path.rstrip("/") if valid else ""
        self.connection_type = (
            http.client.HTTPConnection if valid_dev_http else http.client.HTTPSConnection
        )
        self.timeout = timeout
        typing.cast(typing.Any, self).pool = queue.Queue(maxsize=max_size)

    def get(self) -> http.client.HTTPConnection:
        if not self.host:
            raise RuntimeError("invalid provider endpoint")
        try:
            return typing.cast(typing.Any, self).pool.get_nowait()
        except queue.Empty:
            return self.connection_type(self.host, timeout=self.timeout)

    def put(self, conn: http.client.HTTPConnection):
        try:
            typing.cast(typing.Any, self).pool.put_nowait(conn)
        except queue.Full:
            _close_connection(conn)


def _close_connection(conn: http.client.HTTPConnection) -> None:
    """Best-effort disposal for a broken pooled connection."""
    try:
        conn.close()
    except Exception:
        _LOG.debug("failed to close provider connection", exc_info=True)


def _read_limited(response: typing.Any) -> bytes:
    """Read a provider response without allowing unbounded memory growth."""
    payload = response.read(_MAX_PROVIDER_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_PROVIDER_RESPONSE_BYTES:
        raise RuntimeError("provider response exceeded the size limit")
    return payload


def _http_error(exc: urllib.error.HTTPError, provider: str) -> RuntimeError:
    """Keep the status and a short body so fallbacks can classify the failure."""
    body = ""
    try:
        raw = _read_limited(exc)
        body = raw.decode("utf-8", "replace").strip().replace("\n", " ")[:280]
    except Exception:
        _LOG.debug("failed to read %s error body", provider, exc_info=True)
    finally:
        try:
            exc.close()
        except Exception:
            _LOG.debug("failed to close %s error response", provider, exc_info=True)
    extra = f": {body}" if body else ""
    return RuntimeError(f"{provider} request failed ({exc.code}){extra}")


def _choice_text(payload: typing.Any, provider: str) -> str:
    """Pull the assistant text out of an OpenAI-style chat completion body.

    DeepSeek V4 (and some OpenRouter/Cerebras hosts) can return HTTP 200 with
    an ``error`` object, ``content: null``, or reasoning-only output. Those
    must raise so the caller retries or fails over instead of treating silence
    as a successful reply.
    """
    if not isinstance(payload, dict):
        raise RuntimeError(f"{provider} returned a malformed response")
    err: typing.Any = typing.cast(typing.Any, payload).get("error")
    if err:
        if isinstance(err, dict):
            msg: typing.Any = typing.cast(
                typing.Any,
                typing.cast(typing.Any, err).get("message")
                or typing.cast(typing.Any, err).get("code")
                or json.dumps(err)[:200],
            )
            code: typing.Any = typing.cast(
                typing.Any,
                typing.cast(typing.Any, err).get("code")
                or typing.cast(typing.Any, err).get("status")
                or typing.cast(typing.Any, err).get("type")
                or "error",
            )
            raise RuntimeError(f"{provider} request failed ({code}: {msg})")
        raise RuntimeError(f"{provider} request failed ({err})")
    choices: typing.Any = typing.cast(typing.Any, payload).get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"{provider} returned a malformed response")
    first: typing.Any = typing.cast(typing.Any, choices[0] if isinstance(choices[0], dict) else {})
    msg: typing.Any = first.get("message") if isinstance(first.get("message"), dict) else {}
    text: typing.Any = msg.get("content")
    if isinstance(text, list):
        text: typing.Any = typing.cast(
            typing.Any,
            "".join(
                str(typing.cast(typing.Any, p).get("text") or "")
                for p in typing.cast(typing.Iterable[typing.Any], text)
                if isinstance(p, dict)
            ),
        )
    text = str(text or "").strip()
    if not text:
        for key in ("reasoning_content", "reasoning"):
            alt: typing.Any = msg.get(key) or first.get(key)
            if alt and str(alt).strip():
                text = str(alt).strip()
                break
    if not text:
        raise RuntimeError(f"{provider}: empty content")
    return text


def _is_fatal(e: Exception) -> bool:
    s = str(e).lower()
    return any(m in s for m in _FATAL_MARKERS)


def _is_transient(e: Exception) -> bool:
    """True when the same model is worth another attempt."""
    if _is_fatal(e):
        return False
    s = str(e).lower()
    if any(x in s for x in ("404", "not found")):
        return False
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 409, 425, 429, 500, 502, 503, 504, 529)
    if isinstance(e, (TimeoutError, ConnectionError, BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        return True
    return any(m in s for m in _TRANSIENT_MARKERS)


_mercury_pool = HTTPSConnectionPool(config.INCEPTION_BASE_URL, timeout=25.0, max_size=20)
_celeris_pool = HTTPSConnectionPool(config.CELERIS_BASE_URL, timeout=25.0, max_size=20)


def _is_celeris(model: str) -> bool:
    """Celeris-1 diffusion LLM. Accepts celeris-1, celeris:*, celeris/*."""
    m = str(model or "").strip().lower()
    if not m:
        return False
    if m.startswith("celeris:") or m.startswith("celeris/"):
        return True
    return m in ("celeris-1", "celeris", "celeris1")


def _celeris_upstream_model(model: str) -> str:
    m = str(model or "").strip()
    if m.startswith("celeris:"):
        m = m[len("celeris:") :]
    if m.startswith("celeris/"):
        m = m[len("celeris/") :]
    if m in ("celeris", "celeris1", "celeris-1", ""):
        return "celeris-1"
    return m or "celeris-1"


def _flatten_message_content(content: typing.Any) -> str:
    if isinstance(content, list):
        return " ".join(
            typing.cast(typing.Any, p).get("text", "")
            for p in typing.cast(typing.Iterable[typing.Any], content)
            if isinstance(p, dict) and typing.cast(typing.Any, p).get("type") == "text"
        )
    return str(content or "")


def _celeris_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """Celeris-1 (OpenAI-compatible chat completions) via Keep-Alive pool."""
    if not config.CELERIS_API_KEY:
        raise RuntimeError("no celeris api key configured")

    full = ([{"role": "system", "content": system}] if system else []) + [
        {
            "role": m.get("role", "user"),
            "content": _flatten_message_content(m.get("content")),
        }
        for m in messages
    ]
    for msg in full:
        if not isinstance(msg.get("content"), str):
            msg["content"] = str(msg.get("content") or "")

    body = {
        "model": _celeris_upstream_model(model),
        "messages": full,
        "max_tokens": max(int(max_tokens), 64),
        "temperature": float(temperature) if temperature is not None else 0.7,
    }
    data = json.dumps(body).encode("utf-8")
    endpoint = f"{_celeris_pool.path_prefix}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.CELERIS_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "owaua/1.0",
    }

    raw = None
    for attempt in range(2):
        conn = _celeris_pool.get()
        try:
            conn.request("POST", endpoint, body=data, headers=headers)
            res = conn.getresponse()
            raw_bytes = _read_limited(res)
            if res.status >= 400:
                _close_connection(conn)
                raise RuntimeError(f"celeris request failed ({res.status})")
            _celeris_pool.put(conn)
            raw = raw_bytes.decode("utf-8")
            break
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
            _close_connection(conn)
            if attempt == 1:
                raise
        except Exception:
            _close_connection(conn)
            raise

    if not raw:
        raise RuntimeError("celeris: empty response")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("celeris returned a malformed response") from None
    return _choice_text(d, "celeris")


def _mercury_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """Inception Labs Mercury (OpenAI-compatible chat completions) with ultra-fast Keep-Alive pooling."""
    if not config.INCEPTION_API_KEY:
        raise RuntimeError("no inception/mercury api key configured")

    full = ([{"role": "system", "content": system}] if system else []) + [
        {
            "role": m.get("role", "user"),
            "content": (
                " ".join(
                    typing.cast(typing.Any, p).get("text", "")
                    for p in m.get("content")
                    if isinstance(p, dict) and typing.cast(typing.Any, p).get("type") == "text"
                )
                if isinstance(m.get("content"), list)
                else m.get("content")
            ),
        }
        for m in messages
    ]
    for msg in full:
        if not isinstance(msg.get("content"), str):
            msg["content"] = str(msg.get("content") or "")

    body = {
        "model": _mercury_upstream_model(model),
        "messages": full,
        "max_tokens": max(int(max_tokens), 256),
        "temperature": float(temperature) if temperature is not None else 0.75,
        "reasoning_effort": config.MERCURY_REASONING_EFFORT or "instant",
    }
    data = json.dumps(body).encode("utf-8")
    endpoint = f"{_mercury_pool.path_prefix}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.INCEPTION_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "owaua/1.0",
    }

    raw = None
    for attempt in range(2):
        conn = _mercury_pool.get()
        try:
            conn.request("POST", endpoint, body=data, headers=headers)
            res = conn.getresponse()
            raw_bytes = _read_limited(res)
            if res.status >= 400:
                _close_connection(conn)
                raise RuntimeError(f"mercury request failed ({res.status})")
            _mercury_pool.put(conn)
            raw = raw_bytes.decode("utf-8")
            break
        except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
            _close_connection(conn)
            if attempt == 1:
                raise
        except Exception:
            _close_connection(conn)
            raise

    if not raw:
        raise RuntimeError("mercury: empty response")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("mercury returned a malformed response") from None
    return _choice_text(d, "mercury")


def _groq_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """Groq call, rotating across keys (separate orgs = separate quotas)."""
    if not _clients:
        raise RuntimeError("no groq api key configured")
    full = [{"role": "system", "content": system}] + messages if system else list(messages)
    n = len(_clients)
    start = _next_index()
    last = None
    for attempt in range(n):
        try:
            resp = _clients[(start + attempt) % n].chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=full,
            )
            msg = resp.choices[0].message
            text = (msg.content or "").strip()
            if not text:
                alt = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
                text = str(alt or "").strip()
            if not text:
                raise RuntimeError("groq: empty content")
            return text
        except Exception as e:
            last = e
            if _is_rate_limited(e):
                continue
            raise
    if last is not None:
        raise last
    raise RuntimeError("groq: no provider attempt completed")


def _gemini_parts_from_content(c: typing.Any) -> list[typing.Any]:
    """Convert OpenAI-style content (str or multimodal parts) to Gemini parts."""
    if not isinstance(c, list):
        return [{"text": str(c)}]
    parts: list[typing.Any] = []
    for p in typing.cast(typing.Iterable[typing.Any], c):
        if not isinstance(p, dict):
            continue
        if typing.cast(typing.Any, p).get("type") == "text":
            parts.append({"text": typing.cast(typing.Any, p).get("text", "")})
        elif typing.cast(typing.Any, p).get("type") == "image_url":
            url: typing.Any = typing.cast(
                typing.Any,
                typing.cast(typing.Any, (typing.cast(typing.Any, p).get("image_url") or {})).get(
                    "url"
                )
                or "",
            )
            if url.startswith("data:") and ";base64," in url:
                header, b64 = url.split(";base64,", 1)
                mime: typing.Any = typing.cast(
                    typing.Any, header[5:] if header.startswith("data:") else "image/png"
                )
                parts.append({"inline_data": {"mime_type": mime, "data": b64}})
            elif url:
                mime = "image/jpeg"
                if ".png" in url.lower():
                    mime = "image/png"
                elif ".webp" in url.lower():
                    mime = "image/webp"
                parts.append({"file_data": {"mime_type": mime, "file_uri": url}})
    return parts or [{"text": ""}]


def _gemini_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """Google Gemini call (different API shape: system is separate, roles differ)."""
    if not config.GEMINI_KEYS:
        raise RuntimeError("no gemini api key configured")

    contents: list[typing.Any] = []
    for m in messages:
        contents.append(
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": _gemini_parts_from_content(m.get("content")),
            }
        )

    _gemini_off = [
        {"category": cat, "threshold": "BLOCK_NONE"}
        for cat in (
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
            "HARM_CATEGORY_CIVIC_INTEGRITY",
        )
    ]
    body = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        "safetySettings": _gemini_off,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    data = json.dumps(body).encode()

    last = None
    for key in config.GEMINI_KEYS:
        safe_model = urllib.parse.quote(str(model), safe="-._")
        query = urllib.parse.urlencode({"key": key})
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{safe_model}:generateContent?{query}"
        )
        try:
            req = urllib.request.Request(  # noqa: S310
                url, data=data, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
                d = json.loads(_read_limited(r))
            for cand in typing.cast(typing.Iterable[typing.Any], d.get("candidates") or []):
                parts: typing.Any = typing.cast(
                    typing.Any, cand.get("content", {}).get("parts", []) or []
                )
                text: typing.Any = typing.cast(
                    typing.Any,
                    "".join(
                        p.get("text", "") for p in typing.cast(typing.Iterable[typing.Any], parts)
                    ).strip(),
                )
                if text:
                    return text
            last = RuntimeError("gemini: empty content")
            continue
        except urllib.error.HTTPError as e:
            last = _http_error(e, "gemini")
            if e.code in (429, 500, 502, 503, 504):
                continue
            raise last
        except Exception as e:
            last = e
            continue
    if last is not None:
        raise last
    raise RuntimeError("gemini: no provider attempt completed")


def _is_cerebras(model: str) -> bool:
    return str(model).startswith("cb:")


def _cerebras_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """Cerebras (OpenAI-compatible, very fast).

    Two quirks worth keeping: a User-Agent header is REQUIRED (Cloudflare
    rejects urllib's default with a misleading `error code 1010` that looks
    like a bad key), and reasoning-style models can return a message with a
    `reasoning` field but no `content` — which must raise, not return empty.
    """
    if not config.CEREBRAS_API_KEY:
        raise RuntimeError("no cerebras api key configured")

    full = ([{"role": "system", "content": system}] if system else []) + [
        {
            "role": m.get("role", "user"),
            "content": (
                " ".join(
                    typing.cast(typing.Any, p).get("text", "")
                    for p in m["content"]
                    if isinstance(p, dict) and typing.cast(typing.Any, p).get("type") == "text"
                )
                if isinstance(m.get("content"), list)
                else str(m.get("content"))
            ),
        }
        for m in messages
    ]
    body = json.dumps(
        {
            "model": model[3:],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": full,
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310
        "https://api.cerebras.ai/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {config.CEREBRAS_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "owaua/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310
            d = json.loads(_read_limited(response))
    except urllib.error.HTTPError as e:
        raise _http_error(e, "cerebras") from None
    except json.JSONDecodeError:
        raise RuntimeError("cerebras returned a malformed response") from None
    return _choice_text(d, "cerebras")


def _is_openrouter(model: str) -> bool:
    return str(model).startswith("or:")


def _openrouter_key(model: str) -> str:
    """OpenRouter key for a model. DeepSeek models use their own key
    (DEEPSEEK_API_KEY) so !ask / assistant don't eat the main OpenRouter quota."""
    if "deepseek/" in str(model).lower():
        return config.DEEPSEEK_API_KEY or config.OPENROUTER_API_KEY
    return config.OPENROUTER_API_KEY


def _openrouter_content(c: typing.Any):
    """Keep multimodal content parts intact (vision). Flatten only if needed."""
    if isinstance(c, list):
        return typing.cast(typing.Any, c)
    return str(c)


def _openrouter_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """OpenRouter (free tier), OpenAI-compatible.

    Free models share upstream capacity and fail intermittently. Critically,
    OpenRouter reports those failures as HTTP 200 with an {"error": ...} body,
    and some models return content: null — both must raise so the fallback
    chain moves on instead of returning an empty reply.

    Multimodal (vision) messages are forwarded as-is — do not strip image parts.
    """
    api_key = _openrouter_key(model)
    if not api_key:
        raise RuntimeError("no openrouter api key configured")

    full = ([{"role": "system", "content": system}] if system else []) + [
        {"role": m.get("role", "user"), "content": _openrouter_content(m.get("content"))}
        for m in messages
    ]
    has_images = any(
        isinstance(m.get("content"), list)
        and any(
            isinstance(p, dict) and typing.cast(typing.Any, p).get("type") == "image_url"
            for p in m["content"]
        )
        for m in messages
    )
    timeout = 60 if has_images else 25
    body = json.dumps(
        {
            "model": model[3:],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": full,
            "provider": {"require_parameters": False},
            "route": "fallback",
        }
    ).encode()
    req = urllib.request.Request(  # noqa: S310
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "owaua",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            d = json.loads(_read_limited(response))
    except urllib.error.HTTPError as e:
        raise _http_error(e, "openrouter") from None
    except json.JSONDecodeError:
        raise RuntimeError("openrouter returned a malformed response") from None
    return _choice_text(d, "openrouter")


def _is_deepseek(model: str) -> bool:
    return str(model).strip().lower().startswith("deepseek")


def _openai_messages(system: typing.Any, messages: typing.Any) -> list[typing.Any]:
    return ([{"role": "system", "content": system}] if system else []) + [
        {
            "role": m.get("role", "user"),
            "content": (
                " ".join(
                    typing.cast(typing.Any, p).get("text", "")
                    for p in m["content"]
                    if isinstance(p, dict) and typing.cast(typing.Any, p).get("type") == "text"
                )
                if isinstance(m.get("content"), list)
                else str(m.get("content"))
            ),
        }
        for m in messages
    ]


def _post_json(
    url: str,
    headers: dict[typing.Any, typing.Any],
    payload: dict[typing.Any, typing.Any],
    timeout: float,
    provider: str,
) -> dict[typing.Any, typing.Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            return json.loads(_read_limited(response))
    except urllib.error.HTTPError as e:
        raise _http_error(e, provider) from None
    except json.JSONDecodeError:
        raise RuntimeError(f"{provider} returned a malformed response") from None
    except (TimeoutError, urllib.error.URLError) as e:
        raise RuntimeError(f"{provider} request failed (timeout)") from e


def _chat_without_thinking(
    url: str,
    headers: dict[typing.Any, typing.Any],
    model: str,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
    provider: str,
    timeout: float = 45,
) -> str:
    """OpenAI-style chat with DeepSeek thinking turned off.

    V4 Flash thinks at high effort by default. Those reasoning tokens count
    against max_tokens, so a growing Discord thread often comes back HTTP 200
    with empty content — which used to surface as "brain hiccuped". Disable
    thinking for chat; if the host 400s on the extra field, retry without it.
    """
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": _openai_messages(system, messages),
        "thinking": {"type": "disabled"},
    }
    try:
        return _choice_text(_post_json(url, headers, payload, timeout, provider), provider)
    except RuntimeError as e:
        if "request failed (400)" not in str(e) and "request failed (422)" not in str(e):
            raise
        payload.pop("thinking", None)
        return _choice_text(_post_json(url, headers, payload, timeout, provider), provider)


def _deepseek_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """Call DeepSeek's official OpenAI-compatible API."""
    if not config.DEEPSEEK_API_KEY:
        raise RuntimeError("no deepseek api key configured")
    return _chat_without_thinking(
        config.DEEPSEEK_BASE_URL + "/chat/completions",
        {
            "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "owaua/1.0",
        },
        config.canonical_model(model),
        system,
        messages,
        max_tokens,
        temperature,
        "deepseek",
    )


def _is_inferx(model: str) -> bool:
    return str(model or "").strip().lower().startswith("ix:")


def _inferx_upstream_model(model: str) -> str:
    """Map the local InferX alias to the model id served by InferX."""
    value = str(model or "").strip()
    if value.lower().startswith("ix:"):
        value = value[3:]
    if value in {"", "deepseek-v4-flash", "deepseek-v4", "deepseek-flash"}:
        return "deepseek-v4-flash-0731"
    return value


def _inferx_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """Call InferX's OpenAI-compatible DeepSeek endpoint."""
    if not config.INFERX_API_KEY:
        raise RuntimeError("no inferx api key configured")
    return _chat_without_thinking(
        config.INFERX_BASE_URL + "/chat/completions",
        {
            "Authorization": f"Bearer {config.INFERX_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "owaua/1.0",
        },
        _inferx_upstream_model(model),
        system,
        messages,
        max_tokens,
        temperature,
        "inferx",
    )


def deepseek_configured() -> bool:
    """True when the official DeepSeek API credential is configured."""
    return bool(config.DEEPSEEK_API_KEY)


def _is_anthropic(model: str) -> bool:
    return str(model).startswith("claude")


_anthropic_client = None


def _anthropic_generate(
    model: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
) -> str:
    """Anthropic call (paid — expert tier only). Uses the official SDK.

    Note: Opus 4.8 rejects `temperature`/`top_p`/`top_k` with a 400, so the
    temperature argument is deliberately ignored here. Adaptive thinking is on
    because this path exists for correctness-sensitive teaching answers.
    """
    global _anthropic_client
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("no anthropic api key configured")
    if _anthropic_client is None:
        import anthropic

        _anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    msgs: list[typing.Any] = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(
                typing.cast(typing.Any, p).get("text", "")
                for p in typing.cast(typing.Iterable[typing.Any], c)
                if isinstance(p, dict) and typing.cast(typing.Any, p).get("type") == "text"
            )
        msgs.append(
            {
                "role": "assistant" if m.get("role") == "assistant" else "user",
                "content": str(c),
            }
        )

    resp = _anthropic_client.messages.create(
        model=model,
        max_tokens=max(int(max_tokens), 4000),
        system=system,
        thinking={"type": "adaptive"},
        messages=msgs,
    )
    if getattr(resp, "stop_reason", None) == "refusal":
        raise RuntimeError("anthropic refusal")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _generate(
    requested: typing.Any,
    system: typing.Any,
    messages: typing.Any,
    max_tokens: typing.Any,
    temperature: typing.Any,
    fallbacks: typing.Any = None,
    trace: typing.Any = None,
    *,
    scope_id: typing.Any = None,
    user_id: typing.Any = None,
    estimated_tokens: int = 1,
) -> str:
    """Run the request down a fallback chain.

    Quotas are per (provider, org, model), so when one is exhausted a different
    model — or an entirely different provider — still has its own budget.

    `fallbacks` lets a caller pick a different chain: the expert tier passes an
    intelligence-ordered one so !cybersec degrades to the smartest remaining
    model rather than the fastest.
    """
    pool = config.MODEL_FALLBACKS if fallbacks is None else fallbacks
    chain = [requested] + [m for m in pool if m != requested]
    last = None
    attempts_used = 0
    attempt_limit = max(1, min(20, int(config.AI_MAX_PROVIDER_ATTEMPTS)))
    for model in chain:
        if attempts_used >= attempt_limit:
            break
        if not ai_control.provider_available(model):
            continue
        if _is_mercury(model) and not config.INCEPTION_API_KEY:
            continue
        if _is_celeris(model) and not config.CELERIS_API_KEY:
            continue
        if _is_gemini(model) and not config.GEMINI_KEYS:
            continue
        if _is_anthropic(model) and not config.ANTHROPIC_API_KEY:
            continue
        if _is_openrouter(model) and not _openrouter_key(model):
            continue
        if _is_cerebras(model) and not config.CEREBRAS_API_KEY:
            continue
        if _is_inferx(model) and not config.INFERX_API_KEY:
            continue
        if _is_deepseek(model) and not config.DEEPSEEK_API_KEY:
            continue
        if (
            not _is_mercury(model)
            and not _is_celeris(model)
            and not _is_gemini(model)
            and not _is_anthropic(model)
            and not _is_openrouter(model)
            and not _is_cerebras(model)
            and not _is_inferx(model)
            and not _is_deepseek(model)
            and not _clients
        ):
            continue
        if _is_mercury(model):
            fn = _mercury_generate
        elif _is_celeris(model):
            fn = _celeris_generate
        elif _is_anthropic(model):
            fn = _anthropic_generate
        elif _is_cerebras(model):
            fn = _cerebras_generate
        elif _is_openrouter(model):
            fn = _openrouter_generate
        elif _is_inferx(model):
            fn = _inferx_generate
        elif _is_deepseek(model):
            fn = _deepseek_generate
        elif _is_gemini(model):
            fn = _gemini_generate
        else:
            fn = _groq_generate
        model_attempts = min(_SAME_MODEL_ATTEMPTS, attempt_limit - attempts_used)
        for attempt in range(model_attempts):
            started = time.perf_counter()
            ai_control.reserve_provider_attempt(
                scope_id=scope_id,
                user_id=user_id,
                estimated_tokens=estimated_tokens,
            )
            attempts_used += 1
            if trace is not None:
                trace["attempts"] = int(trace.get("attempts") or 0) + 1
            try:
                out = fn(model, system, messages, max_tokens, temperature)
                if not out or not str(out).strip():
                    raise RuntimeError(f"{model}: empty content")
                ai_control.record_provider_result(
                    model,
                    success=True,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                if trace is not None:
                    trace["served_model"] = model
                    trace["fallbacks"] = 0 if model == requested else 1
                if model != requested:
                    print(f"[failover] {requested} exhausted -> served by {model}")
                elif attempt:
                    print(f"[retry] {model} recovered on attempt {attempt + 1}")
                return out
            except Exception as e:
                last = e
                ai_control.record_provider_result(
                    model,
                    success=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=e,
                )
                _LOG.warning(
                    "provider %s attempt %s/%s failed (%s): %s",
                    model,
                    attempt + 1,
                    _SAME_MODEL_ATTEMPTS,
                    type(e).__name__,
                    str(e)[:240],
                )
                if _is_transient(e) and attempt < model_attempts - 1:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                break
        continue
    if last:
        raise last
    raise RuntimeError("no model available")


def friendly_error(e: Exception) -> str:
    """Turn an API error into something a human wants to read."""
    s = str(e)
    sl = s.lower()
    _LOG.warning("provider call failed (%s): %s", type(e).__name__, s[:400])
    if isinstance(e, ai_control.AIBudgetExceeded) or "ai spending limit" in sl:
        return sl.replace("ai spending limit", "AI spending limit", 1)
    if _is_rate_limited(e):
        m = re.search(r"try again in ([0-9hms.]+)", s, re.I)
        wait = f" try again in {m.group(1)}." if m else " give it a few minutes."
        return f"i'm out of tokens for now -{wait}"
    if "credit balance is too low" in sl:
        return (
            "the paid model's out of credit. add credits at console.anthropic.com/settings/billing"
        )
    if "401" in s or "invalid api key" in sl:
        return "my api key got rejected. someone check the AI keys (Mercury/Celeris/Groq)."
    if "mercury" in sl and ("402" in s or "credit" in sl or "quota" in sl):
        return "mercury is out of quota/credits — check inception platform billing."
    if "celeris" in sl and ("401" in s or "invalid" in sl):
        return "celeris key rejected — regenerate at console.celeris.ai"
    if "deepseek" in sl and ("401" in s or "invalid" in sl):
        return "deepseek key rejected — check DEEPSEEK_API_KEY."
    if "timed out" in sl or "timeout" in sl:
        return "that took too long — ping me again"
    if "context" in sl and any(w in sl for w in ("length", "too long", "maximum", "too large")):
        return "that thread's too long for my brain. !resetconvo and try again"
    return "my brain hiccuped. try again in a moment"


async def _run(fn: typing.Callable[[], _RunResult]) -> _RunResult:
    """Run a blocking call on the dedicated pool."""
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, fn)


_llm_sem: Optional[asyncio.Semaphore] = None


def _llm_semaphore() -> asyncio.Semaphore:
    global _llm_sem
    if _llm_sem is None:
        _llm_sem = asyncio.Semaphore(config.AI_MAX_CONCURRENCY)
    return _llm_sem


def _resolve_model(tier: str) -> str:
    t = (tier or "smart").lower()
    if t == "fast":
        return config.MODEL_FAST
    if t == "vision":
        return config.MODEL_VISION
    if t == "expert":
        return config.MODEL_EXPERT
    if t == "big":
        return config.MODEL_BIG
    return config.MODEL_SMART


async def chat(
    system: str,
    messages: List[dict[typing.Any, typing.Any]],
    max_tokens: int = 600,
    temperature: float = 0.7,
    model: Optional[str] = None,
    tier: str = "smart",
    fallbacks: Optional[List[str]] = None,
    task: str = "chat",
    scope_id: Optional[str] = None,
    user_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
) -> str:
    """Run a chat completion and return the text.

    `tier` selects smart/fast/vision when `model` is not set explicitly.
    Message content may be a string or a list of multimodal content parts.
    Pass `fallbacks=[]` to disable the default text-model chain (needed for vision).
    """
    decision = ai_control.route(
        task,
        requested_tier=tier,
        user_id=user_id,
        scope_id=scope_id,
        max_tokens=max_tokens,
        temperature=temperature,
        prompt_version=prompt_version,
    )
    tier = decision.tier
    max_tokens = decision.max_tokens
    temperature = decision.temperature
    use_model = model or _resolve_model(tier)
    if fallbacks is None:
        if tier == "expert":
            fallbacks = config.MODEL_EXPERT_FALLBACKS
        elif tier == "big":
            fallbacks = config.MODEL_BIG_FALLBACKS
    if tier == "vision" and fallbacks is None:
        fallbacks: list[typing.Any] = []

    ai_control.check_request_budget(scope_id, decision.task, user_id=user_id)
    estimated_tokens = ai_control.estimate_chat_tokens(system, messages) + max_tokens
    trace = ai_control.begin_trace(
        task=decision.task,
        scope_id=scope_id,
        route_name=tier,
        requested_model=use_model,
        prompt_version=decision.prompt_version,
        input_tokens=estimated_tokens - max_tokens,
        max_output_tokens=max_tokens,
    )

    def _call():
        return _generate(
            use_model,
            system,
            messages,
            max_tokens,
            temperature,
            fallbacks=fallbacks,
            trace=trace,
            scope_id=scope_id,
            user_id=user_id,
            estimated_tokens=estimated_tokens,
        )

    try:
        async with ai_control.user_ai_guard(user_id):
            async with _llm_semaphore():
                output = await _run(_call)
    except Exception as exc:
        ai_control.finish_trace(trace, status="error", error=exc)
        raise
    ai_control.finish_trace(trace, status="success", output_text=output)
    return output


async def structured(
    system: str,
    messages: List[dict[typing.Any, typing.Any]],
    max_tokens: int = 2000,
    temperature: float = 0.8,
    tier: str = "smart",
    model: Optional[str] = None,
    fallbacks: Optional[List[str]] = None,
    schema: Optional[str] = None,
    task: str = "chat",
    scope_id: Optional[str] = None,
    user_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
) -> Optional[dict[typing.Any, typing.Any]]:
    """Multi-turn chat that must return a single JSON object (the brain's reply)."""
    raw = await chat(
        system=system,
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        tier=tier,
        fallbacks=fallbacks,
        task=task,
        scope_id=scope_id,
        user_id=user_id,
        prompt_version=prompt_version,
    )
    parsed = ai_control.validate_structured(_extract_json(raw), schema)
    repair_enabled = config.AI_STRUCTURED_REPAIR
    if scope_id:
        repair_enabled = repair_enabled and bool(
            db.guild_settings(scope_id).get("ai_structured_repair", True)
        )
    if parsed is not None or not repair_enabled:
        return parsed
    repair_system = (
        "Repair malformed JSON into one valid JSON object. Preserve only information "
        "already present. Never add actions, facts, fields, or claims. Return JSON only."
    )
    repaired = await chat(
        repair_system,
        [{"role": "user", "content": str(raw)[:12_000]}],
        max_tokens=min(max_tokens, 1_000),
        temperature=0.0,
        tier="fast",
        task="structured_repair",
        scope_id=scope_id,
        user_id=user_id,
    )
    return ai_control.validate_structured(_extract_json(repaired), schema)


async def json_call(
    system: str,
    prompt: str,
    max_tokens: int = 800,
    tier: str = "smart",
    schema: Optional[str] = None,
    task: str = "workflow",
    scope_id: Optional[str] = None,
    user_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
) -> Optional[dict[typing.Any, typing.Any]]:
    """Ask the model for a single JSON object and parse it."""
    system = system + "\n\nRespond with ONLY a single valid JSON object, no prose."
    raw = await chat(
        system=system,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.3,
        tier=tier,
        task=task,
        scope_id=scope_id,
        user_id=user_id,
        prompt_version=prompt_version,
    )
    parsed = ai_control.validate_structured(_extract_json(raw), schema)
    repair_enabled = config.AI_STRUCTURED_REPAIR
    if scope_id:
        repair_enabled = repair_enabled and bool(
            db.guild_settings(scope_id).get("ai_structured_repair", True)
        )
    if parsed is not None or not repair_enabled:
        return parsed
    repaired = await chat(
        "Repair malformed JSON into one valid JSON object. Preserve only existing data. "
        "Do not add fields, facts, actions, or claims. Return JSON only.",
        [{"role": "user", "content": str(raw)[:12_000]}],
        max_tokens=min(max_tokens, 1_000),
        temperature=0.0,
        tier="fast",
        task="structured_repair",
        scope_id=scope_id,
        user_id=user_id,
    )
    return ai_control.validate_structured(_extract_json(repaired), schema)


async def describe_images(
    image_urls: List[str],
    caption: str = "",
    *,
    scope_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """Vision pass: short description of attached / embed image URLs.

    Prefers inlined base64 data URLs so Groq (etc.) doesn't have to fetch
    Discord CDN links themselves — those often 403 from datacenter IPs.
    """
    if not image_urls:
        return ""

    import base64

    from owaua.services.llm_client import llm

    async def _prep(url: str) -> Optional[str]:
        downloaded = await llm.get_image(url, max_bytes=config.VISION_MAX_IMAGE_BYTES)
        if not downloaded:
            return None
        data, mime = downloaded
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"

    prepared: List[str] = []
    for url in image_urls[:4]:
        try:
            item = await _prep(url)
            if item:
                prepared.append(item)
        except Exception:
            _LOG.debug("Discord CDN image preparation failed", exc_info=True)
            continue

    if not prepared:
        return ""

    parts: List[dict[typing.Any, typing.Any]] = []
    text = caption.strip() or "Describe what you see in these image(s), briefly and bluntly."
    parts.append({"type": "text", "text": text})
    for url in prepared:
        parts.append({"type": "image_url", "image_url": {"url": url}})

    vision_fallbacks = [config.MODEL_VISION] + list(config.MODEL_VISION_FALLBACKS or [])
    seen: typing.Any = typing.cast(typing.Any, set())
    vision_fallbacks = [m for m in vision_fallbacks if m and not (m in seen or seen.add(m))]

    for i, model in enumerate(vision_fallbacks):
        try:
            out = await chat(
                system=(
                    "You describe Discord images and link-preview screenshots for another AI. "
                    "Be concrete and short (2-6 sentences). No emoji. "
                    "Read and quote any visible text (tweets, headlines, UI labels, memes). "
                    "If it's a screenshot of a social media post, name the author and summarize the post."
                ),
                messages=[{"role": "user", "content": parts}],
                max_tokens=450,
                temperature=0.2,
                model=model,
                tier="vision",
                fallbacks=[],
                scope_id=scope_id,
                user_id=user_id,
            )
            if out and out.strip() and not out.strip().lower().startswith("(vision failed"):
                if i:
                    print(f"[vision] served by fallback {model}")
                return out.strip()
        except ai_control.AIBudgetExceeded:
            raise
        except Exception as e:
            _LOG.warning("vision provider %s failed (%s)", model, type(e).__name__)
            continue
    return "(vision failed: provider unavailable)"


def _ddg_results(query: str, k: int) -> List[dict[typing.Any, typing.Any]]:
    """DuckDuckGo (keyless). Retries across backends since DDG rate-limits bursts."""
    try:
        from ddgs import DDGS as _DDGS  # pyright: ignore[reportUnknownVariableType]
    except ImportError:
        from duckduckgo_search import DDGS as _DDGS  # pyright: ignore[reportUnknownVariableType]
    DDGS: typing.Any = typing.cast(typing.Any, _DDGS)
    errors: list[typing.Any] = []
    for backend in ("auto", "lite", "html"):
        try:
            with DDGS() as d:
                try:
                    r: typing.Any = typing.cast(
                        typing.Any,
                        list(
                            typing.cast(typing.Any, d).text(query, max_results=k, backend=backend)
                        ),
                    )
                except TypeError:
                    r: typing.Any = typing.cast(
                        typing.Any, list(typing.cast(typing.Any, d).text(query, max_results=k))
                    )
            if r:
                return r
        except Exception as e:
            errors.append(f"{backend}: {e}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return []


def _tavily_results(query: str, k: int) -> List[dict[typing.Any, typing.Any]]:
    """Tavily search API (reliable from cloud IPs). Used when TAVILY_API_KEY is set."""
    body = json.dumps(
        {
            "api_key": config.TAVILY_API_KEY,
            "query": query,
            "max_results": k,
            "search_depth": "basic",
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        d = json.loads(_read_limited(resp))
    return [
        {"title": r.get("title"), "href": r.get("url"), "body": r.get("content", "")}
        for r in d.get("results", [])
    ]


def _search_backend(query: str, k: int) -> List[dict[typing.Any, typing.Any]]:
    """Prefer Tavily if configured (reliable), else keyless DuckDuckGo."""
    if config.TAVILY_API_KEY:
        try:
            r = _tavily_results(query, k)
            if r:
                return r
        except Exception:
            _LOG.debug("Tavily search failed; falling back to DuckDuckGo", exc_info=True)
    return _ddg_results(query, k)


async def search_context(
    query: str, k: int = 5
) -> tuple[str, list[dict[str, typing.Any]], str | None]:
    """Raw keyless web search. Returns (context_str, sources, error_or_None).

    context_str is a compact block of the top results for feeding to a model;
    sources is [{"title","url"}...] taken straight from the engine.
    """

    def _fetch() -> tuple[list[dict[typing.Any, typing.Any]], str | None]:
        try:
            return _search_backend(query, k), None
        except Exception as e:
            return [], str(e)

    results, err = await _run(_fetch)
    if err or not results:
        return "", [], err
    sources: list[dict[str, typing.Any]] = []
    for result in results:
        href = str(result.get("href") or "")
        if href:
            sources.append({"title": str(result.get("title") or href)[:200], "url": href})
        if len(sources) >= 5:
            break
    ctx = "\n\n".join(
        f"[{i}] {r.get('title', '')}\n{r.get('href', '')}\n{(r.get('body') or '')[:400]}"
        for i, r in enumerate(results, 1)
    )
    return ctx, sources, None


async def web_search(
    query: str,
    k: int = 5,
    *,
    user_id: Optional[str] = None,
    scope_id: Optional[str] = None,
) -> dict[typing.Any, typing.Any]:
    """Keyless web search that returns a self-contained {answer, sources}.

    Used by the explicit /search and !search commands. Sources come straight
    from the search engine, so their URLs are always real.
    """
    ai_control.check_search_budget(user_id)
    ctx, sources, err = await search_context(query, k)
    if err:
        return {"answer": f"couldn't reach search right now ({err[:80]}).", "sources": []}
    if not ctx:
        return {"answer": "found nothing useful for that.", "sources": []}
    system = (
        "You answer the user's question using ONLY the search results provided. "
        "Be concise and factual (1-4 sentences). No emoji. Do not invent facts or "
        "URLs. If the results don't actually answer it, say so plainly."
    )
    try:
        answer = await chat(
            system,
            [{"role": "user", "content": f"Question: {query}\n\nResults:\n{ctx}\n\nAnswer:"}],
            max_tokens=350,
            temperature=0.3,
            tier="smart",
            task="fact_check",
            scope_id=scope_id,
            user_id=user_id,
        )
    except Exception as e:
        answer = f"found results but couldn't summarise them ({str(e)[:60]})."
    return {"answer": answer, "sources": sources}


def _extract_json(text: str) -> Optional[dict[typing.Any, typing.Any]]:
    if not text or not str(text).strip():
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return typing.cast(typing.Any, parsed)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return typing.cast(typing.Any, parsed)
        except json.JSONDecodeError:
            pass
    m = re.search(r'"response"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if m:
        try:
            return {"response": json.loads(f'"{m.group(1)}"')}
        except json.JSONDecodeError:
            return {"response": m.group(1)}
    return None
