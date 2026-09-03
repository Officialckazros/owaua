"""AI routing, tracing, budgeting, and context controls."""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import hashlib
import math
import threading
import time
import typing
import uuid
from typing import Any, Final, Iterable, Mapping, Sequence

from owaua import config, db


@dataclasses.dataclass(frozen=True, slots=True)
class AIPolicy:
    task: str
    default_tier: str
    prompt_version: str
    max_output_tokens: int
    temperature: float
    structured: bool = False
    tools: bool = False
    vision: bool = False
    reasoning_allowed: bool = True
    mutations_require_confirmation: bool = True


POLICIES: Final[dict[str, AIPolicy]] = {
    "chat": AIPolicy("chat", "smart", "brain-v5", 1_200, 0.8),
    "assistant": AIPolicy(
        "assistant", "expert", "assistant-v4", 2_000, 0.35, structured=True, tools=True
    ),
    "memory_extract": AIPolicy(
        "memory_extract", "fast", "memory-v4", 600, 0.15, structured=True, reasoning_allowed=False
    ),
    "structured_repair": AIPolicy(
        "structured_repair",
        "fast",
        "json-repair-v1",
        1_000,
        0.0,
        structured=True,
        reasoning_allowed=False,
    ),
    "workflow": AIPolicy("workflow", "smart", "workflow-v3", 1_200, 0.35),
    "fact_check": AIPolicy("fact_check", "expert", "fact-check-v2", 1_200, 0.2),
    "moderation": AIPolicy(
        "moderation", "fast", "moderation-v3", 600, 0.0, structured=True, reasoning_allowed=False
    ),
    "vision": AIPolicy(
        "vision", "vision", "vision-v3", 700, 0.2, vision=True, reasoning_allowed=False
    ),
    "recap": AIPolicy("recap", "smart", "recap-v4", 1_200, 0.3),
    "planning": AIPolicy(
        "planning", "expert", "planning-v2", 1_600, 0.25, structured=True, tools=True
    ),
    "creative": AIPolicy("creative", "smart", "creative-v2", 1_200, 0.85),
}


def policy_for(task: str) -> AIPolicy:
    return POLICIES.get(str(task or "").strip().lower(), POLICIES["chat"])


@dataclasses.dataclass(frozen=True, slots=True)
class RouteDecision:
    task: str
    tier: str
    prompt_version: str
    max_tokens: int
    temperature: float
    mode: str


def user_mode(user_id: str | None, scope_id: str | None) -> str:
    """Return a bounded per-user mode with a typed guild default."""
    if user_id:
        saved = db.user_flag_get(str(user_id), "ai_mode")
        if saved in {"fast", "balanced", "reasoning"}:
            return saved
    if scope_id:
        saved = str(db.guild_settings(str(scope_id)).get("ai_mode_default") or "fast")
        if saved in {"fast", "balanced", "reasoning"}:
            return saved
    return "fast"


def set_user_mode(user_id: str, mode: str) -> str:
    selected = str(mode or "").strip().lower()
    if selected not in {"fast", "balanced", "reasoning"}:
        raise ValueError("AI mode must be fast, balanced, or reasoning")
    db.user_flag_set(str(user_id), "ai_mode", selected)
    return selected


def route(
    task: str,
    *,
    requested_tier: str = "auto",
    user_id: str | None = None,
    scope_id: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    prompt_version: str | None = None,
) -> RouteDecision:
    """Choose an operational tier while preserving capability requirements."""
    policy = policy_for(task)
    mode = user_mode(user_id, scope_id)
    tier = str(requested_tier or "auto").lower()
    if tier == "auto":
        tier = policy.default_tier
    if policy.vision:
        tier = "vision"
    elif mode == "fast" and policy.default_tier not in {"expert", "big"}:
        tier = "fast"
    elif mode == "reasoning" and policy.reasoning_allowed and tier in {"smart", "fast"}:
        tier = "expert"
    return RouteDecision(
        task=policy.task,
        tier=tier,
        prompt_version=str(prompt_version or policy.prompt_version)[:80],
        max_tokens=max(
            1, min(int(max_tokens or policy.max_output_tokens), policy.max_output_tokens)
        ),
        temperature=max(
            0.0, min(float(policy.temperature if temperature is None else temperature), 1.5)
        ),
        mode=mode,
    )


@dataclasses.dataclass(slots=True)
class _ProviderHealth:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    latency_ms: float = 0.0
    opened_until: float = 0.0
    last_error: str = ""


_health_lock = threading.Lock()
_health: dict[str, _ProviderHealth] = {}


def provider_available(model: str, *, now_value: float | None = None) -> bool:
    now_value = time.monotonic() if now_value is None else now_value
    with _health_lock:
        state = _health.get(str(model))
        return state is None or state.opened_until <= now_value


def record_provider_result(
    model: str,
    *,
    success: bool,
    latency_ms: float,
    error: BaseException | None = None,
) -> None:
    """Update rolling provider health and open a bounded circuit after failures."""
    key = str(model)
    with _health_lock:
        state = _health.setdefault(key, _ProviderHealth())
        latency = max(0.0, float(latency_ms))
        state.latency_ms = (
            latency if state.latency_ms <= 0 else (state.latency_ms * 0.8 + latency * 0.2)
        )
        if success:
            state.successes += 1
            state.consecutive_failures = 0
            state.opened_until = 0.0
            state.last_error = ""
            return
        state.failures += 1
        state.consecutive_failures += 1
        state.last_error = type(error).__name__ if error is not None else "provider_error"
        threshold = max(2, int(config.AI_CIRCUIT_FAILURES))
        if state.consecutive_failures >= threshold:
            exponent = min(4, state.consecutive_failures - threshold)
            state.opened_until = time.monotonic() + min(
                900.0, max(5.0, float(config.AI_CIRCUIT_COOLDOWN_SECONDS)) * (2**exponent)
            )


def provider_health_snapshot() -> list[dict[str, Any]]:
    now_value = time.monotonic()
    with _health_lock:
        rows: list[typing.Any] = []
        for model, state in _health.items():
            total = state.successes + state.failures
            success_rate = state.successes / total if total else 1.0
            rows.append(
                {
                    "model": model,
                    "health": round(success_rate * 100.0, 1),
                    "successes": state.successes,
                    "failures": state.failures,
                    "consecutive_failures": state.consecutive_failures,
                    "latency_ms": round(state.latency_ms, 1),
                    "circuit_open_seconds": round(max(0.0, state.opened_until - now_value), 1),
                    "last_error": state.last_error,
                }
            )
        return sorted(
            rows, key=lambda row: (-row["circuit_open_seconds"], row["health"], row["model"])
        )


_usage_lock = threading.Lock()
_usage: dict[tuple[str, str], collections.deque[float]] = {}
_usage_hourly: dict[tuple[str, str], collections.deque[float]] = {}
_usage_daily: dict[tuple[str, str], collections.deque[float]] = {}

_token_usage: dict[tuple[str, str], collections.deque[tuple[float, int]]] = {}
_token_usage_hourly: dict[tuple[str, str], collections.deque[tuple[float, int]]] = {}
_token_usage_daily: dict[tuple[str, str], collections.deque[tuple[float, int]]] = {}

_provider_attempts: collections.deque[float] = collections.deque()

_active_users_lock = threading.Lock()
_active_users: set[str] = set()

_tool_usage_lock = threading.Lock()
_search_usage: dict[str, collections.deque[float]] = {}
_tts_usage: dict[str, collections.deque[float]] = {}
_recent_queries: dict[tuple[str, str], float] = {}


class AIBudgetExceeded(RuntimeError):
    """A paid-AI request was rejected before another provider call was made."""


def estimate_openai_luna_microusd(
    *, input_tokens: int, max_output_tokens: int, fixed_microusd: int = 0
) -> int:
    """Conservatively reserve Luna's full possible output at current list pricing."""
    # GPT-5.6 Luna is $0.20/M input and $1.20/M output. Expressed in
    # micro-USD, those rates are 0.20 and 1.20 per token respectively.
    return max(
        1,
        int(
            math.ceil(
                max(0, int(input_tokens)) * 0.20
                + max(0, int(max_output_tokens)) * 1.20
                + max(0, int(fixed_microusd))
            )
        ),
    )


def reserve_openai_spend(
    *,
    model: str,
    input_tokens: int,
    max_output_tokens: int,
    scope_id: str | None = None,
    user_id: str | None = None,
    fixed_microusd: int = 0,
) -> None:
    """Record estimated OpenAI usage without imposing a dollar cap."""
    if db.ai_spend_paused():
        raise AIBudgetExceeded("OpenAI spending is paused by the bot owner")
    if user_id and db.ai_user_spend_paused(str(user_id)):
        raise AIBudgetExceeded("OpenAI spending is disabled for this user")
    reserved = estimate_openai_luna_microusd(
        input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        fixed_microusd=fixed_microusd,
    )
    db.ai_spend_reserve(
        user_id=user_id,
        scope_id=scope_id,
        provider="openai",
        model=str(model),
        input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        reserved_microusd=reserved,
        hourly_limit_microusd=None,
        daily_limit_microusd=None,
        monthly_limit_microusd=None,
        user_daily_limit_microusd=None,
        scope_daily_limit_microusd=None,
        total_limit_microusd=None,
    )


def _retry_after(
    bucket: collections.deque[typing.Any], now_value: float, window: float = 60.0
) -> int:
    if not bucket:
        return 1
    oldest = float(
        typing.cast(typing.Any, bucket[0][0] if isinstance(bucket[0], tuple) else bucket[0])
    )
    return max(1, math.ceil(window - (now_value - oldest)))


def _prune_times(bucket: collections.deque[float], now_value: float, window: float = 60.0) -> None:
    while bucket and bucket[0] <= now_value - window:
        bucket.popleft()


def _prune_tokens(
    bucket: collections.deque[tuple[float, int]], now_value: float, window: float = 60.0
) -> None:
    while bucket and bucket[0][0] <= now_value - window:
        bucket.popleft()


def _cleanup_usage_maps(now_value: float) -> None:
    for key, bucket in list(_usage.items()):
        _prune_times(bucket, now_value, 60.0)
        if not bucket:
            _usage.pop(key, None)
    for key, bucket in list(_usage_hourly.items()):
        _prune_times(bucket, now_value, 3600.0)
        if not bucket:
            _usage_hourly.pop(key, None)
    for key, bucket in list(_usage_daily.items()):
        _prune_times(bucket, now_value, 86400.0)
        if not bucket:
            _usage_daily.pop(key, None)

    for key, bucket in list(_token_usage.items()):
        _prune_tokens(bucket, now_value, 60.0)
        if not bucket:
            _token_usage.pop(key, None)
    for key, bucket in list(_token_usage_hourly.items()):
        _prune_tokens(bucket, now_value, 3600.0)
        if not bucket:
            _token_usage_hourly.pop(key, None)
    for key, bucket in list(_token_usage_daily.items()):
        _prune_tokens(bucket, now_value, 86400.0)
        if not bucket:
            _token_usage_daily.pop(key, None)


def _budget_error(
    bucket: collections.deque[typing.Any],
    now_value: float,
    window: float = 60.0,
    msg: str | None = None,
) -> AIBudgetExceeded:
    wait_s = _retry_after(bucket, now_value, window)
    if msg:
        return AIBudgetExceeded(f"{msg}; retry in {wait_s}s")
    if window >= 86400.0:
        return AIBudgetExceeded(f"Daily AI spending limit reached; resets in {wait_s}s")
    if window >= 3600.0:
        return AIBudgetExceeded(f"Hourly AI spending limit reached; retry in {wait_s}s")
    return AIBudgetExceeded(f"AI spending limit reached; retry in {wait_s}s")


def check_request_budget(scope_id: str | None, task: str, *, user_id: str | None = None) -> None:
    """Apply the one requested per-user prompt window."""
    del scope_id, task
    user = str(user_id or "").strip()
    is_owner = bool(user and config.is_bot_owner(user))
    user_min_limit = max(1, int(config.AI_USER_REQUESTS_PER_MINUTE))

    now_value = time.monotonic()
    with _usage_lock:
        _cleanup_usage_maps(now_value)

        if user and not is_owner:
            u_min = _usage.setdefault(("user", user), collections.deque())
            user_window = float(config.AI_USER_REQUEST_WINDOW_SECONDS)
            _prune_times(u_min, now_value, user_window)
            if len(u_min) >= user_min_limit:
                raise _budget_error(
                    u_min,
                    now_value,
                    user_window,
                    f"AI prompt limit reached ({user_min_limit} per {user_window:g}s)",
                )

            u_min.append(now_value)


def reserve_provider_attempt(
    *,
    scope_id: str | None = None,
    user_id: str | None = None,
    estimated_tokens: int = 1,
) -> None:
    """Record one real outbound provider attempt without a spend budget."""
    scope = str(scope_id or "").strip()
    user = str(user_id or "").strip()
    token_cost = max(1, int(estimated_tokens))

    now_value = time.monotonic()
    with _usage_lock:
        _cleanup_usage_maps(now_value)
        _prune_times(_provider_attempts, now_value, 60.0)
        g_min_tokens = _token_usage.setdefault(("global", "*"), collections.deque())
        _prune_tokens(g_min_tokens, now_value, 60.0)
        g_hr_tokens = _token_usage_hourly.setdefault(("global", "*"), collections.deque())
        _prune_tokens(g_hr_tokens, now_value, 3600.0)
        g_day_tokens = _token_usage_daily.setdefault(("global", "*"), collections.deque())
        _prune_tokens(g_day_tokens, now_value, 86400.0)

        _provider_attempts.append(now_value)
        g_min_tokens.append((now_value, token_cost))
        g_hr_tokens.append((now_value, token_cost))
        g_day_tokens.append((now_value, token_cost))
        if scope:
            _token_usage.setdefault(("scope", scope), collections.deque()).append((now_value, token_cost))
        if user:
            _token_usage.setdefault(("user", user), collections.deque()).append((now_value, token_cost))


@contextlib.asynccontextmanager
async def user_ai_guard(user_id: str | None):
    """Enforce per-user active in-flight request limits before initiating an AI turn."""
    if not user_id:
        yield
        return
    uid = str(user_id).strip()
    if not uid or config.is_bot_owner(uid):
        yield
        return
    max_user_concurrency = int(getattr(config, "AI_USER_MAX_CONCURRENCY", 1))
    with _active_users_lock:
        current_active = sum(1 for u in _active_users if u == uid)
        if current_active >= max_user_concurrency:
            raise AIBudgetExceeded(
                "You already have an AI request processing. Please wait for it to complete."
            )
        _active_users.add(uid)
    try:
        yield
    finally:
        with _active_users_lock:
            _active_users.discard(uid)


def check_search_budget(user_id: str | None) -> None:
    """Check rate limits on paid Tavily web searches."""
    if not user_id:
        return
    uid = str(user_id).strip()
    if not uid or config.is_bot_owner(uid):
        return
    now_val = time.monotonic()
    window = 300.0
    limit = max(1, int(getattr(config, "AI_SEARCH_REQUESTS_PER_WINDOW", 4)))
    with _tool_usage_lock:
        bucket = _search_usage.setdefault(uid, collections.deque())
        _prune_times(bucket, now_val, window)
        if len(bucket) >= limit:
            wait_s = max(1, math.ceil(window - (now_val - bucket[0])))
            raise AIBudgetExceeded(f"Web search limit reached ({limit} per 5m); retry in {wait_s}s")
        bucket.append(now_val)


def check_tts_budget(user_id: str | None) -> None:
    """Check rate limits on paid TTS audio generation."""
    if not user_id:
        return
    uid = str(user_id).strip()
    if not uid or config.is_bot_owner(uid):
        return
    now_val = time.monotonic()
    window = 300.0
    limit = max(1, int(getattr(config, "AI_TTS_REQUESTS_PER_WINDOW", 3)))
    with _tool_usage_lock:
        bucket = _tts_usage.setdefault(uid, collections.deque())
        _prune_times(bucket, now_val, window)
        if len(bucket) >= limit:
            wait_s = max(1, math.ceil(window - (now_val - bucket[0])))
            raise AIBudgetExceeded(f"TTS speech limit reached ({limit} per 5m); retry in {wait_s}s")
        bucket.append(now_val)


def check_duplicate_query(user_id: str | None, text: str, min_interval: float = 10.0) -> bool:
    """Check if the user is spamming the exact same query in rapid loop."""
    if not user_id or not text or len(text.strip()) < 3:
        return False
    uid = str(user_id).strip()
    if not uid or config.is_bot_owner(uid):
        return False
    clean_text = " ".join(text.lower().split())
    query_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()[:16]
    now_val = time.monotonic()
    key = (uid, query_hash)
    with _tool_usage_lock:
        last_time = _recent_queries.get(key)
        if last_time is not None and now_val - last_time < min_interval:
            return True
        _recent_queries[key] = now_val
        if len(_recent_queries) > 5_000:
            stale = [k for k, ts in _recent_queries.items() if now_val - ts > 60.0]
            for k in stale:
                _recent_queries.pop(k, None)
    return False


def estimate_tokens(value: object) -> int:
    """Conservative provider-agnostic estimate suitable for budgets and traces."""
    if isinstance(value, str):
        chars = len(value)
    else:
        chars = len(str(value or ""))
    return max(1, math.ceil(chars / 3.6))


def estimate_chat_tokens(system: str, messages: object) -> int:
    """Estimate chat tokens without treating base64 image bytes as text tokens."""

    def _parts(value: object) -> int:
        if isinstance(value, str):
            return estimate_tokens(value)
        if isinstance(value, list):
            return sum(_parts(item) for item in value)
        if isinstance(value, dict):
            part_type = str(typing.cast(typing.Any, value).get("type") or "")
            if part_type in {"image_url", "input_image", "image"}:
                return 2_000
            if "content" in value:
                return _parts(typing.cast(typing.Any, value).get("content")) + estimate_tokens(
                    typing.cast(typing.Any, value).get("role")
                )
            if "text" in value:
                return _parts(typing.cast(typing.Any, value).get("text"))
            return min(2_000, estimate_tokens(typing.cast(typing.Any, value)))
        return estimate_tokens(value)

    return estimate_tokens(system) + _parts(messages)


def _trim_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit < 80:
        return text[:limit]
    left = max(1, int(limit * 0.65))
    right = max(1, limit - left - 34)
    return text[:left] + "\n[older context trimmed]\n" + text[-right:]


def assemble_context(
    required: Sequence[str],
    sections: Iterable[tuple[int, str]],
    *,
    max_chars: int,
) -> str:
    """Build a priority-aware prompt without dropping required sections."""
    hard = [str(part).strip() for part in required if str(part).strip()]
    budget = max(sum(len(part) for part in hard) + 16, int(max_chars))
    remaining = max(0, budget - sum(len(part) + 2 for part in hard))
    seen: set[str] = set()
    optional: list[str] = []
    for _priority, raw in sorted(sections, key=lambda item: item[0]):
        part = str(raw or "").strip()
        fingerprint = " ".join(part.lower().split())[:500]
        if not part or fingerprint in seen or remaining <= 0:
            continue
        seen.add(fingerprint)
        fitted = _trim_middle(part, remaining)
        optional.append(fitted)
        remaining -= len(fitted) + 2
    return "\n\n".join([*hard, *optional])


def begin_trace(
    *,
    task: str,
    scope_id: str | None,
    route_name: str,
    requested_model: str,
    prompt_version: str,
    input_tokens: int,
    max_output_tokens: int,
) -> dict[str, Any]:
    return {
        "trace_id": f"ai_{uuid.uuid4().hex[:12]}",
        "task": str(task)[:40],
        "scope_id": str(scope_id or "")[:80],
        "route": str(route_name)[:40],
        "requested_model": str(requested_model)[:160],
        "served_model": "",
        "prompt_version": str(prompt_version)[:80],
        "input_tokens": max(0, int(input_tokens)),
        "max_output_tokens": max(0, int(max_output_tokens)),
        "started": time.perf_counter(),
        "attempts": 0,
        "fallbacks": 0,
    }


def finish_trace(
    trace: Mapping[str, Any],
    *,
    status: str,
    output_text: str = "",
    error: BaseException | None = None,
) -> None:
    if not trace.get("scope_id"):
        return
    try:
        started = float(trace.get("started") or time.perf_counter())
        db.ai_trace_record(
            trace_id=str(trace.get("trace_id") or "")[:40],
            scope_id=str(trace.get("scope_id") or "")[:80],
            task=str(trace.get("task") or "chat")[:40],
            route=str(trace.get("route") or "smart")[:40],
            requested_model=str(trace.get("requested_model") or "")[:160],
            served_model=str(trace.get("served_model") or "")[:160],
            prompt_version=str(trace.get("prompt_version") or "")[:80],
            status=str(status)[:24],
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            input_tokens=max(0, int(trace.get("input_tokens") or 0)),
            output_tokens=estimate_tokens(output_text) if output_text else 0,
            attempts=max(0, int(trace.get("attempts") or 0)),
            fallbacks=max(0, int(trace.get("fallbacks") or 0)),
            error_type=type(error).__name__[:80] if error is not None else "",
        )
    except Exception:
        return


_SCHEMAS: Final[dict[str, dict[str, type[typing.Any] | tuple[type[typing.Any], ...]]]] = {
    "brain_response": {
        "response": str,
        "title": (str, type(None)),
        "memories": list,
        "relationship": dict,
        "quotes": list,
        "actions": list,
        "plan": list,
        "chart": (dict, str, type(None)),
        "web_search": (str, type(None)),
        "mood": (dict, type(None)),
        "tos_violation": (dict, type(None)),
        "tos_flag": (dict, type(None)),
        "policy_violation": (dict, type(None)),
    },
    "memory_extract": {"memories": list},
}


def validate_structured(value: object, schema: str | None) -> dict[typing.Any, typing.Any] | None:
    """Validate known structured contracts and reject unexpected top-level fields."""
    if not isinstance(value, dict):
        return None
    if not schema:
        return typing.cast(typing.Any, value)
    expected = _SCHEMAS.get(schema)
    if expected is None or set(typing.cast(typing.Any, value)) - set(expected):
        return None
    clean: dict[str, Any] = {}
    for key, kind in expected.items():
        if key not in value:
            continue
        candidate: typing.Any = typing.cast(typing.Any, value[key])
        if not isinstance(candidate, kind):
            return None
        clean[key] = candidate
    if schema == "brain_response" and not str(clean.get("response") or "").strip():
        return None
    if schema == "memory_extract" and "memories" not in clean:
        return None
    return clean


def diagnostics(scope_id: str) -> dict[str, Any]:
    """Return owner-safe metadata for dashboard and Discord diagnostics."""
    return {
        "usage": db.ai_trace_summary(str(scope_id)),
        "recent": db.ai_traces_recent(str(scope_id), limit=20),
        "providers": provider_health_snapshot(),
    }
