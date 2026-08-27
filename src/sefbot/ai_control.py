"""Shared routing, policy, tracing, budgeting, and context controls for AI calls.

The module deliberately stores metadata only.  Prompt and response contents never
enter traces, provider-health state, or usage counters.
"""
from __future__ import annotations

import collections
import dataclasses
import math
import threading
import time
import uuid
from typing import Any, Final, Iterable, Mapping, Sequence

from sefbot import config, db


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
    "chat": AIPolicy("chat", "smart", "brain-v5", 4_000, 0.8),
    "assistant": AIPolicy("assistant", "expert", "assistant-v4", 4_000, 0.35, structured=True, tools=True),
    "memory_extract": AIPolicy("memory_extract", "fast", "memory-v4", 600, 0.15, structured=True, reasoning_allowed=False),
    "structured_repair": AIPolicy("structured_repair", "fast", "json-repair-v1", 1_000, 0.0, structured=True, reasoning_allowed=False),
    "workflow": AIPolicy("workflow", "smart", "workflow-v3", 1_200, 0.35),
    "fact_check": AIPolicy("fact_check", "expert", "fact-check-v2", 1_200, 0.2),
    "moderation": AIPolicy("moderation", "fast", "moderation-v3", 600, 0.0, structured=True, reasoning_allowed=False),
    "vision": AIPolicy("vision", "vision", "vision-v3", 700, 0.2, vision=True, reasoning_allowed=False),
    "recap": AIPolicy("recap", "smart", "recap-v4", 1_200, 0.3),
    "planning": AIPolicy("planning", "expert", "planning-v2", 1_600, 0.25, structured=True, tools=True),
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
        saved = str(db.guild_settings(str(scope_id)).get("ai_mode_default") or "balanced")
        if saved in {"fast", "balanced", "reasoning"}:
            return saved
    return "balanced"


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
        max_tokens=max(1, min(int(max_tokens or policy.max_output_tokens), policy.max_output_tokens)),
        temperature=max(0.0, min(float(policy.temperature if temperature is None else temperature), 1.5)),
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
        state.latency_ms = latency if state.latency_ms <= 0 else (state.latency_ms * 0.8 + latency * 0.2)
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
                900.0, max(5.0, float(config.AI_CIRCUIT_COOLDOWN_SECONDS)) * (2 ** exponent)
            )


def provider_health_snapshot() -> list[dict[str, Any]]:
    now_value = time.monotonic()
    with _health_lock:
        rows = []
        for model, state in _health.items():
            total = state.successes + state.failures
            success_rate = state.successes / total if total else 1.0
            rows.append({
                "model": model,
                "health": round(success_rate * 100.0, 1),
                "successes": state.successes,
                "failures": state.failures,
                "consecutive_failures": state.consecutive_failures,
                "latency_ms": round(state.latency_ms, 1),
                "circuit_open_seconds": round(max(0.0, state.opened_until - now_value), 1),
                "last_error": state.last_error,
            })
        return sorted(rows, key=lambda row: (-row["circuit_open_seconds"], row["health"], row["model"]))


_usage_lock = threading.Lock()
_usage: dict[tuple[str, str], collections.deque[float]] = {}


def check_request_budget(scope_id: str | None, task: str) -> None:
    if not scope_id:
        return
    settings = db.guild_settings(str(scope_id))
    limit = max(10, min(600, int(settings.get("ai_requests_per_minute") or config.AI_REQUESTS_PER_MINUTE)))
    key = (str(scope_id), str(task))
    now_value = time.monotonic()
    with _usage_lock:
        bucket = _usage.setdefault(key, collections.deque())
        while bucket and bucket[0] <= now_value - 60.0:
            bucket.popleft()
        if len(bucket) >= limit:
            raise RuntimeError("AI request budget reached for this server; retry in a moment")
        bucket.append(now_value)


def estimate_tokens(value: object) -> int:
    """Conservative provider-agnostic estimate suitable for budgets and traces."""
    if isinstance(value, str):
        chars = len(value)
    else:
        chars = len(str(value or ""))
    return max(1, math.ceil(chars / 3.6))


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
    """Build a deterministic, priority-aware prompt without dropping hard rules.

    Lower numeric priorities are retained first. Required sections are never
    removed; optional context is deduplicated and fitted around them.
    """
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


def finish_trace(trace: Mapping[str, Any], *, status: str, output_text: str = "", error: BaseException | None = None) -> None:
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
        # Observability can never make a user-facing AI request fail.
        return


_SCHEMAS: Final[dict[str, dict[str, type | tuple[type, ...]]]] = {
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


def validate_structured(value: object, schema: str | None) -> dict | None:
    """Validate known structured contracts and reject unexpected top-level fields."""
    if not isinstance(value, dict):
        return None
    if not schema:
        return value
    expected = _SCHEMAS.get(schema)
    if expected is None or set(value) - set(expected):
        return None
    clean: dict[str, Any] = {}
    for key, kind in expected.items():
        if key not in value:
            continue
        candidate = value[key]
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
