"""Public legal, health, acceptance, and static-site HTTP routes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import inspect
import ipaddress
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from aiohttp import web

from owaua import config, network_risk, tos
from owaua.dashboard import (
    DASHBOARD_PREFIX,
    DashboardAuthConfig,
    GuildProvider,
    attach_dashboard_routes,
)
from owaua.legal import privacy_inner, terms_inner
from owaua.sites import (
    SITE_FLAG,
    apply_site_headers,
    attach_site_routes,
    hostname_of,
    resolve_sites_root,
    serve_public_site,
)

log = logging.getLogger("owaua.web")
DEFAULT_HOST: Final = "0.0.0.0"  # noqa: S104, RUF100
DEFAULT_PORT: Final = 8080
MAX_REQUEST_BYTES: Final = 1_024
READINESS_TIMEOUT_SECONDS: Final = 2.0
LEGACY_SLUG: Final = "sef" + "bot"
LEGACY_PRODUCT_SLUG: Final = "op" + "sef"

_STYLE: Final = """
:root{color-scheme:light;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;--paper:#efede7;--surface:#fff;--soft:#f6f5f1;--ink:#111;--muted:#5e5e5a;--line:#c8c6bf}
*{box-sizing:border-box}
body{min-height:100vh;margin:0;padding:clamp(16px,3vw,36px) 0;background:var(--paper);color:var(--ink);line-height:1.68}
nav,main{width:min(100% - 32px,880px);margin-inline:auto;border:1px solid var(--ink)}
nav{position:sticky;z-index:2;top:0;display:flex;align-items:center;gap:0;background:var(--ink);color:#f7f5ef}
nav::before{content:"owaua / web";margin-right:auto;padding:14px 18px;font:700 .72rem/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.12em;text-transform:uppercase}
nav a{padding:13px 15px;border-left:1px solid #454543;color:inherit;font-size:.82rem;text-decoration:none}
nav a:hover,nav a:focus-visible{background:#f7f5ef;color:var(--ink)}
main{padding:clamp(28px,6vw,64px);border-top:0;background:var(--surface)}
a{color:inherit;text-decoration-thickness:1px;text-underline-offset:4px}
a:hover{text-decoration-thickness:2px}
h1,h2,h3{line-height:1.12;letter-spacing:-.035em}h1{max-width:44rem;margin:0 0 1.5rem;font-size:clamp(2.5rem,7vw,5.25rem)}h2{margin:3.5rem 0 1rem;padding-top:1.25rem;border-top:1px solid var(--line);font-size:clamp(1.45rem,3vw,2rem)}h3{margin:2rem 0 .75rem;font-size:1.05rem}
p,li{color:var(--muted)}strong{color:var(--ink)}
.card{margin:1.5rem 0;padding:clamp(18px,3vw,28px);border:1px solid var(--line);border-left:5px solid var(--ink);background:var(--soft)}
code{padding:.12em .34em;border:1px solid #dedcd6;border-radius:2px;background:var(--soft);color:var(--ink);font:.88em ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:.9rem;margin:1.5rem 0}
th,td{padding:.7rem;border:1px solid var(--line);text-align:left;vertical-align:top}
th{background:var(--soft);color:var(--ink);font-size:.75rem;letter-spacing:.04em;text-transform:uppercase}ul{padding-left:1.3rem}
button{padding:.8rem 1.1rem;border:1px solid var(--ink);border-radius:0;background:var(--ink);color:#fff;font:inherit;font-weight:750;cursor:pointer}
button:hover,button:focus-visible{background:#fff;color:var(--ink)}
label.accept{display:flex;gap:.8rem;align-items:flex-start;margin:1.4rem 0;padding:1rem;border:1px solid var(--line);background:#fff}
label.accept input{width:1.1rem;height:1.1rem;margin-top:.3rem;accent-color:var(--ink)}
footer{width:min(100% - 32px,880px);margin:0 auto;padding:18px;border:1px solid var(--ink);border-top:0;background:var(--ink);color:#aaa9a3;font-size:.78rem;letter-spacing:.04em}
footer a{color:#f7f5ef}
:focus-visible{outline:2px solid currentColor;outline-offset:3px}
@media(max-width:620px){body{padding:0}nav,main,footer{width:100%;border-left:0;border-right:0}nav{overflow-x:auto}nav::before{display:none}nav a:first-child{border-left:0}main{padding:28px 20px}table{display:block;overflow-x:auto}}
""".strip()
_STYLE_HASH: Final = base64.b64encode(hashlib.sha256(_STYLE.encode("utf-8")).digest()).decode(
    "ascii"
)
_READINESS_COMPONENTS: Final = frozenset({"service", "discord", "database", "malware_scanner"})

ReadinessResult: TypeAlias = bool | Mapping[str, bool]
ReadinessProvider: TypeAlias = Callable[[], ReadinessResult | Awaitable[ReadinessResult]]


class WebConfigurationError(ValueError):
    """Raised when the public web service is configured unsafely."""


@dataclass(slots=True)
class ReadinessState:
    """Mutable lifecycle state suitable for a bot-owned ``WebService``."""

    discord: bool = False
    database: bool = False
    malware_scanner: bool = False

    def __call__(self) -> Mapping[str, bool]:
        return {
            "discord": self.discord,
            "database": self.database,
            "malware_scanner": self.malware_scanner,
        }


def _document(title: str, body: str) -> str:
    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="index,follow">
  <title>{safe_title} | owaua</title>
  <style>{_STYLE}</style>
</head>
<body>
  <nav aria-label="Legal and service pages">
    <a href="/">Home</a>
    <a href="/owaua">owaua</a>
    <a href="/dashboard">Dashboard</a>
    <a href="/owaua/terms">Terms</a>
    <a href="/owaua/privacy">Privacy</a>
  </nav>
  <main>{body}</main>
  <footer>owaua / Discord bot · <a href="/dashboard">open dashboard</a></footer>
</body>
</html>"""


def _landing_page(contact: str) -> str:
    safe_contact = html.escape(contact, quote=True)
    return _document(
        "Discord bot",
        """
<h1>owaua Discord bot</h1>
<div class="card">
  <p>owaua is a Discord assistant with opt-in memory and administration tools.
  Ordinary chat cannot execute Discord actions. The server history gate starts
  enabled, but no ordinary raw history is stored until you opt in separately
  from the Terms for that exact server.</p>
  <p>Read the Terms and Privacy Notice before using the bot. They describe the
  running code, including third-party AI providers, strike/blocks, and what
  deletion does not erase. Health endpoints report service availability
  without exposing user, guild, or provider data.</p>
  <p>Questions, privacy requests, or reports: <a href="mailto:{safe_contact}">{safe_contact}</a>.</p>
</div>
""".format(safe_contact=safe_contact),
    )


def _acceptance_page(contact: str, token: str) -> str:
    safe_token = html.escape(token, quote=True)
    safe_contact = html.escape(contact)
    body = (
        terms_inner(contact)
        + f"""
<section class="card" aria-labelledby="accept-heading">
  <h2 id="accept-heading">Accept this version</h2>
  <p>For abuse and block-evasion prevention, opening or submitting this page
  processes your client IP address together with your Discord account id. The
  raw address is not stored: owaua stores a keyed network token for at most 30
  days. Cloudflare also supplies the network ASN and organization name, which
  owaua uses in memory to refuse VPN, proxy, Tor, and hosting/datacenter
  networks. Those values are not stored. A match to a currently blocked account
  hard-blocks this Discord account regardless of Discord account age. Shared,
  mobile, school, and workplace networks can be inaccurate. You can contact
  {safe_contact} to appeal a block.</p>
  <form method="post" action="/owaua/terms/accept">
    <input type="hidden" name="token" value="{safe_token}">
    <label class="accept"><input type="checkbox" name="agree" value="yes" required>
      <span>I have read and agree to Terms v{html.escape(tos.TOS_VERSION)}, and I
      understand the disclosed abuse-prevention processing described above and
      in the Privacy Notice.</span></label>
    <button type="submit">Accept and return to Discord</button>
  </form>
</section>
"""
    )
    return _document("Review and accept Terms", body)


def _acceptance_result(title: str, message: str) -> str:
    return _document(
        title,
        f'<h1>{html.escape(title)}</h1><div class="card"><p>{html.escape(message)}</p>'
        "<p>You can close this page and return to Discord.</p></div>",
    )


def _html_response(content: str) -> web.Response:
    return web.Response(text=content, content_type="text/html", charset="utf-8")


def _trusted_proxy(request: web.Request) -> bool:
    supplied_secret = request.headers.get("X-Owaua-Origin-Auth", "")
    try:
        return bool(
            config.TOS_PROXY_SECRET
            and supplied_secret
            and hmac.compare_digest(config.TOS_PROXY_SECRET, supplied_secret)
        )
    except (TypeError, ValueError):
        return False


def _trusted_client_address(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    try:
        return str(ipaddress.ip_address(forwarded)) if _trusted_proxy(request) else ""
    except ValueError:
        return ""


def _trusted_network_hints(request: web.Request) -> tuple[int, str]:
    if not _trusted_proxy(request):
        return 0, ""
    return (
        network_risk.parse_asn(request.headers.get("X-Owaua-ASN", "")),
        network_risk.parse_organization(request.headers.get("X-Owaua-AS-Org", "")),
    )


def _vpn_refusal_page(contact: str) -> str:
    return _acceptance_result(
        "VPN or hosting network blocked",
        "Acceptance is not available from a VPN, proxy, Tor, or hosting/"
        "datacenter network. Turn that off, then open your Discord acceptance "
        f"link again. Contact {contact} if this is a mistake.",
    )


def _apply_dashboard_headers(response: web.StreamResponse) -> None:
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
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
        _apply_dashboard_headers(response)
        return response
    form_action = "'self'" if request.path == "/owaua/terms/accept" else "'none'"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; base-uri 'none'; form-action {form_action}; "
        f"style-src 'sha256-{_STYLE_HASH}'; frame-ancestors 'none'"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Server"] = "owaua"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    cacheable_page = (
        request.method in {"GET", "HEAD"}
        and response.status == 200
        and request.path in {"/owaua", "/owaua/terms", "/owaua/privacy"}
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
    dashboard_auth: DashboardAuthConfig | None = None,
    guild_provider: GuildProvider | None = None,
) -> web.Application:
    """Create the HTTP application without starting a listening socket."""

    if not isinstance(privacy_contact, str):
        raise WebConfigurationError("OWAUA_PRIVACY_CONTACT must be text")
    contact = privacy_contact.strip()
    if not contact or len(contact) > 200 or any(not char.isprintable() for char in contact):
        raise WebConfigurationError(
            "OWAUA_PRIVACY_CONTACT must be a non-empty contact up to 200 characters"
        )
    readiness_provider = readiness or ReadinessState()
    if not callable(readiness_provider):
        raise WebConfigurationError("readiness provider must be callable")
    app = web.Application(
        middlewares=[_security_headers, _site_fallback],
        client_max_size=300_000,
    )

    def legacy_legal_redirect(request: web.Request) -> web.HTTPPermanentRedirect | None:
        """Move the former public legal host without breaking old bookmarks."""
        if hostname_of(request) in {"kozzyx.org", "www.kozzyx.org"}:
            location = f"https://owaua.com{request.path}"
            if request.query_string:
                location = f"{location}?{request.query_string}"
            return web.HTTPPermanentRedirect(location=location)
        return None

    async def landing(request: web.Request) -> web.Response:
        if redirect := legacy_legal_redirect(request):
            raise redirect
        return _html_response(_landing_page(contact))

    async def terms(request: web.Request) -> web.Response:
        if redirect := legacy_legal_redirect(request):
            raise redirect
        return _html_response(_document("Terms of Service", terms_inner(contact)))

    async def privacy(request: web.Request) -> web.Response:
        if redirect := legacy_legal_redirect(request):
            raise redirect
        return _html_response(_document("Privacy Notice", privacy_inner(contact)))

    async def accept_terms_get(request: web.Request) -> web.Response:
        if redirect := legacy_legal_redirect(request):
            raise redirect
        token = str(request.query.get("token") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,80}", token):
            return _html_response(
                _acceptance_result(
                    "Invalid acceptance link",
                    "This link is invalid. Return to Discord and request a new one with /tos.",
                )
            )
        try:
            client_address = _trusted_client_address(request)
            if client_address:
                user_id = tos.peek_acceptance_challenge(token) or ""
                asn, organization = _trusted_network_hints(request)
                if not config.is_bot_owner(user_id) and network_risk.is_restricted_network(
                    asn=asn, organization=organization
                ):
                    return _html_response(_vpn_refusal_page(contact))
                decision, _fingerprint = tos.inspect_web_network(user_id, client_address)
                if decision == "blocked":
                    if user_id:
                        tos.consume_acceptance_challenge(token)
                    return _html_response(
                        _acceptance_result(
                            "Access unavailable",
                            "Access is blocked because this Discord account or network "
                            f"matches a current owaua block. Contact {contact} to appeal.",
                        )
                    )
        except Exception:
            log.exception("acceptance network check failed")
        return _html_response(_acceptance_page(contact, token))

    async def accept_terms_post(request: web.Request) -> web.Response:
        if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_REQUEST_BYTES, actual_size=request.content_length
            )
        form = await request.post()
        token = str(form.get("token") or "")
        if form.get("agree") != "yes":
            raise web.HTTPBadRequest(text="acceptance checkbox is required")
        client_address = _trusted_client_address(request)
        if not client_address:
            return web.Response(
                text=_acceptance_result(
                    "Acceptance temporarily unavailable",
                    "The trusted network check was unavailable. No acceptance was recorded; try again later.",
                ),
                status=503,
                content_type="text/html",
                charset="utf-8",
            )
        peeked_user = tos.peek_acceptance_challenge(token) or ""
        asn, organization = _trusted_network_hints(request)
        if not config.is_bot_owner(peeked_user) and network_risk.is_restricted_network(
            asn=asn, organization=organization
        ):
            return _html_response(_vpn_refusal_page(contact))
        user_id = tos.consume_acceptance_challenge(token)
        if user_id is None:
            return _html_response(
                _acceptance_result(
                    "Expired acceptance link",
                    "This link was already used or expired. Return to Discord and request a new one.",
                )
            )
        result = tos.record_web_acceptance(user_id, client_address)
        if result == "accepted":
            return _html_response(
                _acceptance_result(
                    "Terms accepted",
                    f"owaua Terms v{tos.TOS_VERSION} are accepted for your Discord account.",
                )
            )
        if result == "review":
            return _html_response(
                _acceptance_result(
                    "Acceptance submitted for review",
                    f"An abuse-prevention review is required. Contact {contact} if this is a mistake.",
                )
            )
        if result == "blocked":
            return _html_response(
                _acceptance_result(
                    "Access unavailable",
                    "Access is blocked because this Discord account or network "
                    f"matches a current owaua block. Contact {contact} to appeal.",
                )
            )
        raise web.HTTPServiceUnavailable(text="acceptance temporarily unavailable")

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def ready(_request: web.Request) -> web.Response:
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                components = await _resolve_readiness(readiness_provider)
        except Exception as error:  # noqa: BLE001, RUF100
            log.warning("Readiness provider failed (%s)", type(error).__name__)
            return web.json_response({"status": "not_ready"}, status=503)
        is_ready = all(components.values())
        payload = {"status": "ready" if is_ready else "not_ready", **components}
        return web.json_response(payload, status=200 if is_ready else 503)

    async def redirect_landing(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/owaua")

    async def redirect_terms(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/owaua/terms")

    async def redirect_privacy(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/owaua/privacy")

    async def redirect_legacy_guide(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPPermanentRedirect(location="/")

    async def redirect_legacy_acceptance(request: web.Request) -> web.StreamResponse:
        token = request.query.get("token", "")
        location = "/owaua/terms/accept"
        if re.fullmatch(r"[A-Za-z0-9_-]{40,80}", token) and set(request.query) == {"token"}:
            location = f"{location}?token={token}"
        raise web.HTTPPermanentRedirect(location=location)

    app.router.add_get("/owaua", landing)
    app.router.add_get("/owaua/", redirect_landing)
    app.router.add_get("/owaua/terms", terms)
    app.router.add_get("/owaua/terms/accept", accept_terms_get)
    app.router.add_post("/owaua/terms/accept", accept_terms_post)
    app.router.add_get("/owaua/privacy", privacy)
    app.router.add_get("/owaua/tos", redirect_terms)
    app.router.add_get("/owaua-tos.html", redirect_terms)
    app.router.add_get("/owaua-privacy.html", redirect_privacy)
    legacy_prefix = f"/{LEGACY_SLUG}"
    app.router.add_get(legacy_prefix, redirect_landing)
    app.router.add_get(f"{legacy_prefix}/", redirect_landing)
    app.router.add_get(f"{legacy_prefix}/terms", redirect_terms)
    app.router.add_get(f"{legacy_prefix}/terms/accept", redirect_legacy_acceptance)
    app.router.add_get(f"{legacy_prefix}/privacy", redirect_privacy)
    app.router.add_get(f"{legacy_prefix}/tos", redirect_terms)
    app.router.add_get(f"/{LEGACY_PRODUCT_SLUG}-tos.html", redirect_terms)
    app.router.add_get(f"/{LEGACY_PRODUCT_SLUG}-privacy.html", redirect_privacy)
    app.router.add_get(f"/{LEGACY_PRODUCT_SLUG}", redirect_legacy_guide)
    app.router.add_get(f"/{LEGACY_PRODUCT_SLUG}/", redirect_legacy_guide)
    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    attach_dashboard_routes(
        app,
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
            except BaseException:  # noqa: BLE001, RUF100
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
        contact = os.getenv("OWAUA_PRIVACY_CONTACT", "")
        app = create_app(
            privacy_contact=contact,
            dashboard_auth=DashboardAuthConfig(
                public_url=os.getenv("OWAUA_DASHBOARD_PUBLIC_URL", ""),
                session_secret=os.getenv("OWAUA_DASHBOARD_SESSION_SECRET", ""),
                discord_client_id=os.getenv("OWAUA_DISCORD_CLIENT_ID", ""),
                discord_client_secret=os.getenv("OWAUA_DISCORD_CLIENT_SECRET", ""),
            ),
        )
        port = _environment_port()
        host = _normalize_bind_host(os.getenv("OWAUA_WEB_HOST", DEFAULT_HOST))
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
