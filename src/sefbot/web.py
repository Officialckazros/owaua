"""Public web surface for OpSef legal/health endpoints and static sites.

The Discord client owns :class:`WebService` in production and supplies a
readiness callback for its Discord and database state.  Keeping the HTTP
surface here avoids importing Discord or the bot's configuration at module
import time, which also makes health checks safe during partial startup.

When a ``sites/`` tree is present, Host-based virtual hosts serve kozzyx.org,
kirmy.org, and wearegays.net from that tree. Legal routes stay on ``/sefbot``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import inspect
import ipaddress
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from aiohttp import web

from sefbot.dashboard import (
    DASHBOARD_PREFIX,
    DashboardAuthConfig,
    GuildProvider,
    attach_dashboard_routes,
)
from sefbot.legal import privacy_inner, terms_inner
from sefbot.sites import (
    SITE_FLAG,
    apply_site_headers,
    attach_site_routes,
    resolve_sites_root,
    serve_public_site,
)

log = logging.getLogger("sefbot.web")
DEFAULT_HOST: Final = "0.0.0.0"  # noqa: S104, RUF100 - container listener
DEFAULT_PORT: Final = 8080
MAX_REQUEST_BYTES: Final = 1_024
READINESS_TIMEOUT_SECONDS: Final = 2.0

_STYLE: Final = """
:root{color-scheme:dark;font-family:system-ui,sans-serif;background:#111;color:#eee}
body{max-width:52rem;margin:4rem auto;padding:0 1.2rem 4rem;line-height:1.65}
a{color:#8fc7ff}h1,h2,h3{line-height:1.25}h2{margin-top:2rem}h3{margin-top:1.3rem}
nav{display:flex;gap:1rem;flex-wrap:wrap}
.card{border:1px solid #333;border-radius:.8rem;padding:1rem 1.2rem;background:#181818}
code{background:#222;padding:.1rem .3rem;border-radius:.25rem}
table{width:100%;border-collapse:collapse;font-size:.92rem;margin:1rem 0}
th,td{border:1px solid #333;padding:.45rem .55rem;text-align:left;vertical-align:top}
th{background:#1a1a1a}ul{padding-left:1.2rem}
""".strip()
_STYLE_HASH: Final = base64.b64encode(
    hashlib.sha256(_STYLE.encode("utf-8")).digest()
).decode("ascii")
_READINESS_COMPONENTS: Final = frozenset({"service", "discord", "database"})

ReadinessResult: TypeAlias = bool | Mapping[str, bool]
ReadinessProvider: TypeAlias = Callable[
    [], ReadinessResult | Awaitable[ReadinessResult]
]


class WebConfigurationError(ValueError):
    """Raised when the public web service is configured unsafely."""


@dataclass(slots=True)
class ReadinessState:
    """Mutable lifecycle state suitable for a bot-owned ``WebService``."""

    discord: bool = False
    database: bool = False

    def __call__(self) -> Mapping[str, bool]:
        return {"discord": self.discord, "database": self.database}


def _document(title: str, body: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="index,follow">
  <title>{safe_title} | OpSef</title>
  <style>{_STYLE}</style>
</head>
<body>
  <nav aria-label="Legal and service pages">
    <a href="/sefbot">OpSef</a>
    <a href="/sefbot/terms">Terms</a>
    <a href="/sefbot/privacy">Privacy</a>
  </nav>
  <main>{body}</main>
</body>
</html>"""


def _landing_page() -> str:
    return _document(
        "Discord bot",
        """
<h1>OpSef Discord bot</h1>
<div class="card">
  <p>OpSef is a Discord assistant with opt-in memory and administration tools.
  Ordinary chat cannot execute Discord actions. Raw history is off until a
  server enables it and you opt in separately from the Terms.</p>
  <p>Read the Terms and Privacy Notice before using the bot. They describe the
  running code, including third-party AI providers, strike/blocks, and what
  deletion does not erase. Health endpoints report service availability
  without exposing user, guild, or provider data.</p>
</div>
""",
    )


def _terms_page(contact: str) -> str:
    return _document("Terms of Service", terms_inner(contact))


def _privacy_page(contact: str) -> str:
    return _document("Privacy Notice", privacy_inner(contact))


def _html_response(content: str) -> web.Response:
    return web.Response(text=content, content_type="text/html", charset="utf-8")


def _apply_dashboard_headers(response: web.StreamResponse) -> None:
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; img-src 'self' data:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'",
    )


def _valid_bind_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        labels = host.split(".")
        return bool(labels) and all(
            1 <= len(label) <= 63
            and label.isascii()
            and label[0].isalnum()
            and label[-1].isalnum()
            and all(char.isalnum() or char == "-" for char in label)
            for label in labels
        )


def _normalize_bind_host(host: object) -> str:
    if not isinstance(host, str):
        raise WebConfigurationError("web host is invalid")
    normalized = host.strip()
    if (
        not normalized
        or len(normalized) > 253
        or any(not char.isprintable() or char.isspace() for char in normalized)
        or not _valid_bind_host(normalized)
    ):
        raise WebConfigurationError("web host is invalid")
    return normalized


@web.middleware
async def _site_fallback(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPNotFound:
        if (
            request.path == DASHBOARD_PREFIX
            or request.path.startswith(DASHBOARD_PREFIX + "/")
            or request.path.startswith("/forms/")
        ):
            raise
        sites_root = request.app.get("sites_root")
        if not isinstance(sites_root, Path) or not sites_root.is_dir():
            raise
        return await serve_public_site(request, sites_root)


@web.middleware
async def _security_headers(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as error:
        if (
            request.path == DASHBOARD_PREFIX
            or request.path.startswith(DASHBOARD_PREFIX + "/")
            or request.path.startswith("/forms/")
        ):
            # Preserve Set-Cookie on login/logout redirects. Rebuilding an
            # HTTPException as a Response would discard its cookie jar.
            _apply_dashboard_headers(error)
            raise
        response = web.Response(
            status=error.status,
            reason=error.reason,
            text=error.text,
            headers=error.headers,
        )
        if request.get(SITE_FLAG):
            apply_site_headers(response, request.path)
            return response
    if request.get(SITE_FLAG):
        apply_site_headers(response, request.path)
        return response
    if (
        request.path == DASHBOARD_PREFIX
        or request.path.startswith(DASHBOARD_PREFIX + "/")
        or request.path.startswith("/forms/")
    ):
        # Dashboard responses set a route-specific CSP that allows only their
        # same-origin stylesheet, script and API calls.
        _apply_dashboard_headers(response)
        return response
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; base-uri 'none'; form-action 'none'; "
        f"style-src 'sha256-{_STYLE_HASH}'; frame-ancestors 'none'"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Server"] = "OpSef"
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    cacheable_page = (
        request.method in {"GET", "HEAD"}
        and response.status == 200
        and request.path in {"/sefbot", "/sefbot/terms", "/sefbot/privacy"}
    )
    if cacheable_page:
        response.headers["Cache-Control"] = "public, max-age=300"
        response.headers["X-Robots-Tag"] = "index, follow"
    else:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


async def _resolve_readiness(provider: ReadinessProvider) -> dict[str, bool]:
    result = provider()
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, bool):
        return {"service": result}
    if not isinstance(result, Mapping) or not result:
        raise TypeError("readiness provider must return bool or a non-empty mapping")

    components: dict[str, bool] = {}
    for name, ready in result.items():
        if (
            not isinstance(name, str)
            or name not in _READINESS_COMPONENTS
            or not isinstance(ready, bool)
        ):
            raise TypeError("invalid readiness component")
        components[name] = ready
    return components


def create_app(
    *,
    privacy_contact: str,
    readiness: ReadinessProvider | None = None,
    sites_root: str | os.PathLike[str] | None = None,
    dashboard_token: str | None = None,
    dashboard_auth: DashboardAuthConfig | None = None,
    guild_provider: GuildProvider | None = None,
) -> web.Application:
    """Create the HTTP application without starting a listening socket."""

    if not isinstance(privacy_contact, str):
        raise WebConfigurationError("SEFBOT_PRIVACY_CONTACT must be text")
    contact = privacy_contact.strip()
    if (
        not contact
        or len(contact) > 200
        or any(not char.isprintable() for char in contact)
    ):
        raise WebConfigurationError(
            "SEFBOT_PRIVACY_CONTACT must be a non-empty contact up to 200 characters"
        )
    readiness_provider = readiness or ReadinessState()
    if not callable(readiness_provider):
        raise WebConfigurationError("readiness provider must be callable")
    app = web.Application(
        middlewares=[_security_headers, _site_fallback],
        client_max_size=300_000,
    )

    async def landing(_request: web.Request) -> web.Response:
        return _html_response(_landing_page())

    async def terms(_request: web.Request) -> web.Response:
        return _html_response(_terms_page(contact))

    async def privacy(_request: web.Request) -> web.Response:
        return _html_response(_privacy_page(contact))

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def ready(_request: web.Request) -> web.Response:
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                components = await _resolve_readiness(readiness_provider)
        except Exception as error:  # noqa: BLE001, RUF100 - health checks fail closed
            log.warning("Readiness provider failed (%s)", type(error).__name__)
            return web.json_response({"status": "not_ready"}, status=503)
        is_ready = all(components.values())
        payload = {"status": "ready" if is_ready else "not_ready", **components}
        return web.json_response(payload, status=200 if is_ready else 503)

    async def redirect_landing(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/sefbot")

    async def redirect_terms(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/sefbot/terms")

    async def redirect_privacy(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/sefbot/privacy")

    app.router.add_get("/sefbot", landing)
    app.router.add_get("/sefbot/", redirect_landing)
    app.router.add_get("/sefbot/terms", terms)
    app.router.add_get("/sefbot/privacy", privacy)
    app.router.add_get("/sefbot/tos", redirect_terms)
    app.router.add_get("/opsef-tos.html", redirect_terms)
    app.router.add_get("/opsef-privacy.html", redirect_privacy)
    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    attach_dashboard_routes(
        app,
        access_token=dashboard_token,
        guild_provider=guild_provider,
        auth_config=dashboard_auth,
    )
    attach_site_routes(app, resolve_sites_root(sites_root))
    return app


class WebService:
    """Lifecycle wrapper used by the Discord client."""

    def __init__(
        self,
        *,
        privacy_contact: str,
        readiness: ReadinessProvider,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        dashboard_token: str | None = None,
        dashboard_auth: DashboardAuthConfig | None = None,
        guild_provider: GuildProvider | None = None,
    ) -> None:
        try:
            normalized_port = int(port)
        except (TypeError, ValueError) as error:
            raise WebConfigurationError("web port must be an integer") from error
        if not 1 <= normalized_port <= 65_535:
            raise WebConfigurationError("web port must be between 1 and 65535")
        normalized_host = _normalize_bind_host(host)
        self._app = create_app(
            privacy_contact=privacy_contact,
            readiness=readiness,
            dashboard_token=dashboard_token,
            dashboard_auth=dashboard_auth,
            guild_provider=guild_provider,
        )
        self._host = normalized_host
        self._port = normalized_port
        self._runner: web.AppRunner | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._runner is not None:
                return
            runner = web.AppRunner(self._app, access_log=None)
            try:
                await runner.setup()
                await web.TCPSite(runner, self._host, self._port).start()
            except BaseException:  # noqa: BLE001, RUF100 - includes cancellation cleanup
                await runner.cleanup()
                raise
            self._runner = runner
            log.info("Public web service listening on %s:%d", self._host, self._port)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            runner, self._runner = self._runner, None
            if runner is not None:
                await runner.cleanup()


def _environment_port() -> int:
    for key in ("PORT", "SERVER_PORT"):
        raw_port = os.getenv(key)
        if raw_port is None or str(raw_port).strip() == "":
            continue
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise WebConfigurationError(f"{key} must be an integer") from exc
        if not 1 <= port <= 65_535:
            raise WebConfigurationError(f"{key} must be between 1 and 65535")
        return port
    return DEFAULT_PORT


def main() -> None:
    try:
        contact = os.getenv("SEFBOT_PRIVACY_CONTACT", "")
        app = create_app(
            privacy_contact=contact,
            dashboard_token=os.getenv("SEFBOT_DASHBOARD_TOKEN"),
            dashboard_auth=DashboardAuthConfig(
                public_url=os.getenv("SEFBOT_DASHBOARD_PUBLIC_URL", ""),
                session_secret=os.getenv("SEFBOT_DASHBOARD_SESSION_SECRET", ""),
                firebase_api_key=os.getenv("SEFBOT_FIREBASE_API_KEY", ""),
                firebase_auth_domain=os.getenv("SEFBOT_FIREBASE_AUTH_DOMAIN", ""),
                firebase_project_id=os.getenv("SEFBOT_FIREBASE_PROJECT_ID", ""),
                firebase_app_id=os.getenv("SEFBOT_FIREBASE_APP_ID", ""),
                discord_client_id=os.getenv("SEFBOT_DISCORD_CLIENT_ID", ""),
                discord_client_secret=os.getenv("SEFBOT_DISCORD_CLIENT_SECRET", ""),
            ),
        )
        port = _environment_port()
        host = _normalize_bind_host(os.getenv("SEFBOT_WEB_HOST", DEFAULT_HOST))
    except WebConfigurationError as error:
        raise SystemExit(f"web configuration error: {error}") from None
    web.run_app(
        app,
        host=host,
        port=port,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
