"""Free Cloudflare extras that must not be able to take the bot down.

Hangout AI can go through AI Gateway when configured. Full mode, music, Discord,
and SQLite stay on the host. A Cloudflare outage falls back to the same provider
URL without a second reservation or a different paid API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(os.getenv("OWAUA_ENV_FILE") or ROOT / ".env")
log = logging.getLogger("owaua")

GATEWAY_HOST = "gateway.ai.cloudflare.com"
PERPLEXITY_DIRECT = "https://api.perplexity.ai/v1"
_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_GATEWAY_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_UNREACHABLE_STATUS = frozenset({401, 403, 404, 521, 522, 523, 530})
_MAX_LOG_BYTES = 32 * 1024
_PROVIDER_SUFFIX = {
    "perplexity": "compat",
}


def account_id() -> str:
    value = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip().lower()
    return value if _ACCOUNT_ID.fullmatch(value) else ""


def gateway_id() -> str:
    value = (
        os.getenv("CLOUDFLARE_AI_GATEWAY", "").strip()
        or os.getenv("CLOUDFLARE_AI_GATEWAY_ID", "").strip()
    ).lower()
    return value if _GATEWAY_ID.fullmatch(value) else ""


def gateway_enabled() -> bool:
    return bool(account_id() and gateway_id())


def is_gateway_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == GATEWAY_HOST


def gateway_base(provider: str) -> str | None:
    suffix = _PROVIDER_SUFFIX.get(provider)
    if suffix is None or not gateway_enabled():
        return None
    return f"https://{GATEWAY_HOST}/v1/{account_id()}/{gateway_id()}/{suffix}"


def request_headers(*, provider: str = "", user_id: str = "", server_id: str = "") -> dict[str, str]:
    headers = {"cf-aig-skip-cache": "true"}
    token = os.getenv("CLOUDFLARE_AI_GATEWAY_TOKEN", "").strip()
    if token:
        headers["cf-aig-authorization"] = f"Bearer {token}"
    metadata = {"app": "owaua"}
    if provider:
        metadata["provider"] = provider
    if user_id:
        metadata["user"] = hashlib.sha256(user_id.encode()).hexdigest()[:16]
    if server_id:
        metadata["server"] = hashlib.sha256(server_id.encode()).hexdigest()[:16]
    headers["cf-aig-metadata"] = json.dumps(metadata, separators=(",", ":"))
    return headers


def provider_urls(provider: str, direct_base: str, *, full_mode: bool = False) -> tuple[str, str | None]:
    """Return (primary, fallback_or_none) for one provider.

    Full mode stays on the host path: long Agent API calls can outlive the free
    gateway, and a Cloudflare timeout must not strand an already-paid request.
    """
    path = "/responses"
    direct = f"{direct_base.rstrip('/')}{path}"
    # Perplexity Agent API is not a Cloudflare AI Gateway provider. The
    # compat path 403s (error 1010) and would only add hangout latency.
    if full_mode or provider == "perplexity":
        return direct, None
    gateway = gateway_base(provider)
    if gateway and not is_gateway_url(direct_base):
        return f"{gateway.rstrip('/')}{path}", direct
    if is_gateway_url(direct_base):
        fallback = {"perplexity": PERPLEXITY_DIRECT}[provider]
        return direct, f"{fallback.rstrip('/')}{path}"
    return direct, None


def cloudflare_unreachable(exc: BaseException) -> bool:
    """True only when the request likely never reached the paid provider."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    response = getattr(exc, "response", None)
    if response is None:
        return False
    return int(getattr(response, "status_code", 0) or 0) in _UNREACHABLE_STATUS


def log_endpoint() -> str:
    raw = os.getenv("CLOUDFLARE_LOG_URL", "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not host
    ):
        return ""
    if not (host.endswith(".workers.dev") or host in {"owaua.com", "audit.owaua.com"}):
        return ""
    return urlunsplit(("https", host, parts.path or "/", "", ""))


def describe_protection() -> str:
    parts = []
    if gateway_enabled():
        parts.append("ai-gateway")
    if log_endpoint() and os.getenv("CLOUDFLARE_LOG_TOKEN", "").strip():
        parts.append("audit-logs")
    return "+".join(parts) or "off"


def ship_audit_record(payload: str) -> None:
    """Copy a local audit line to the free Worker. Failures never raise."""
    try:
        url = log_endpoint()
        token = os.getenv("CLOUDFLARE_LOG_TOKEN", "").strip()
        if not url or not token or len(payload.encode("utf-8")) > _MAX_LOG_BYTES:
            return
        loop = asyncio.get_running_loop()
    except Exception:
        return
    loop.create_task(_ship_audit_record(url, token, payload), name="owaua-cf-audit")


async def _ship_audit_record(url: str, token: str, payload: str) -> None:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=2.0),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            await client.post(
                url,
                content=payload.encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
    except Exception:
        log.warning("Cloudflare audit copy failed")
