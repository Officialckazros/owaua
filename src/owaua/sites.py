"""Host-based static serving for kozzyx.org, kirmy.org, wearegays.net, and femsec."""

from __future__ import annotations

import mimetypes
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import unquote

from aiohttp import web

DEFAULT_SITES_ROOT: Final = "sites"
DEFAULT_SITE: Final = "kozzyx"
SITE_HOSTS: Final[Mapping[str, str]] = {
    "kozzyx.org": "kozzyx",
    "www.kozzyx.org": "kozzyx",
    "kirmy.org": "kirmy",
    "www.kirmy.org": "kirmy",
    "wearegays.net": "wearegays",
    "www.wearegays.net": "wearegays",
    "femsec.wearegays.net": "femsec",
}
FEMSEC_ORIGIN: Final = "https://femsec.wearegays.net"
_FEMSEC_LEAF_REDIRECTS: Final = frozenset(
    {"/about.html", "/css/files.css", "/js/search.js"}
)
_FEMSEC_PREFIXES: Final = (
    "/boxes",
    "/logs",
    "/book",
    "/dep",
    "/mail",
    "/exhibits",
    "/redacted",
    "/fd302",
    "/filings",
    "/contacts",
    "/modules",
    "/commands",
    "/calendar",
    "/foia",
    "/list",
    "/incidents",
    "/fin",
    "/island",
    "/pet",
    "/empty",
    "/press",
    "/img",
)
BLOCKED_SUFFIXES: Final = frozenset(
    {
        ".bak",
        ".old",
        ".orig",
        ".save",
        ".swp",
        ".swo",
        ".tmp",
        ".log",
        ".sql",
        ".sh",
        ".py",
        ".conf",
        ".cfg",
        ".ini",
        ".env",
        ".yml",
        ".yaml",
        ".lock",
        ".map",
        ".json",
        ".md",
        ".inc",
    }
)
STATIC_SUFFIXES: Final = frozenset(
    {".css", ".js", ".jpg", ".jpeg", ".gif", ".png", ".ico", ".svg", ".woff2", ".webp"}
)
EXTRA_MIME_TYPES: Final[Mapping[str, str]] = {
    ".woff2": "font/woff2",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".js": "text/javascript",
}
WEAREGAYS_REDIRECTS: Final[Mapping[str, str]] = {
    "/tos": "/nano-terms.html",
    "/tos.html": "/nano-terms.html",
    "/terms": "/nano-terms.html",
    "/terms.html": "/nano-terms.html",
    "/pp": "/nano-privacy.html",
    "/pp.html": "/nano-privacy.html",
    "/privacy": "/nano-privacy.html",
    "/privacy.html": "/nano-privacy.html",
}
_WEARE_PAGE_RE: Final = re.compile(r"^/weare[a-z]+(?:\.html)?$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
SITE_FLAG: Final = "public_site"


def hostname_of(request: web.Request) -> str:
    candidates = []
    forwarded = request.headers.get("X-Forwarded-Host", "")
    if forwarded:
        candidates.append(forwarded.split(",", 1)[0])
    host = request.headers.get("Host", "")
    if host:
        candidates.append(host)
    for raw in candidates:
        value = raw.strip().lower()
        if not value or _CONTROL_RE.search(value):
            continue
        value = value.split(":", 1)[0]
        if value:
            return value
    return ""


def site_name_for_host(host: str) -> str:
    return SITE_HOSTS.get(host, DEFAULT_SITE)


def resolve_sites_root(value: str | os.PathLike[str] | None = None) -> Path:
    raw = value if value is not None else os.getenv("OWAUA_SITES_ROOT", DEFAULT_SITES_ROOT)
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _blocked_relative(relative: Path) -> bool:
    if relative.suffix.lower() in BLOCKED_SUFFIXES:
        return True
    return any(part.startswith(".") and part != ".well-known" for part in relative.parts)


def resolve_site_file(sites_root: Path, site: str, url_path: str) -> Path | None:
    if _CONTROL_RE.search(url_path) or "\\" in url_path:
        return None
    decoded = unquote(url_path)
    if decoded != url_path and _CONTROL_RE.search(decoded):
        return None
    relative = Path(decoded.lstrip("/"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        if relative.parts:
            return None
        relative = Path()
    if _blocked_relative(relative):
        return None
    root = (sites_root / site).resolve()
    if not root.is_dir():
        return None
    raw_candidate = root / relative if relative.parts else root
    if _has_internal_symlink(root, raw_candidate):
        return None
    try:
        candidate = raw_candidate.resolve()
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_dir():
        raw_candidate = candidate / "index.html"
        if _has_internal_symlink(root, raw_candidate):
            return None
        candidate = raw_candidate.resolve()
    if candidate.is_file():
        try:
            return (
                candidate
                if not _blocked_relative(candidate.relative_to(root))
                else None
            )
        except ValueError:
            return None
    if candidate.suffix.lower() != ".html":
        html_candidate = candidate.with_name(candidate.name + ".html")
        if _has_internal_symlink(root, html_candidate):
            return None
        if html_candidate.is_file() and not _blocked_relative(
            html_candidate.relative_to(root)
        ):
            try:
                html_candidate.resolve().relative_to(root)
            except ValueError:
                return None
            return html_candidate.resolve()
    return None


def _has_internal_symlink(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def apply_site_headers(response: web.StreamResponse, path: str) -> None:
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains; preload"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
    )
    lowered = path.lower()
    if lowered == "/shield.js" or lowered.startswith("/chat/") and lowered.endswith(
        (".css", ".js")
    ):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Expires"] = "0"
    elif Path(lowered).suffix in STATIC_SUFFIXES:
        response.headers["Cache-Control"] = "public, max-age=2592000"


def _redirect(location: str) -> web.StreamResponse:
    raise web.HTTPMovedPermanently(location=location)


def femsec_offsite_location(path: str) -> str | None:
    """Send apex wearegays.net dump URLs to the femsec subdomain."""
    if path in {"/femsec", "/femsec/"}:
        return f"{FEMSEC_ORIGIN}/"
    if path.startswith("/femsec/"):
        rest = path[7:]
        if not rest.startswith("/"):
            rest = f"/{rest}"
        return f"{FEMSEC_ORIGIN}{rest}"
    if path in _FEMSEC_LEAF_REDIRECTS:
        return f"{FEMSEC_ORIGIN}{path}"
    for prefix in _FEMSEC_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return f"{FEMSEC_ORIGIN}{path}"
    return None


def _content_type(path: Path) -> str:
    extra = EXTRA_MIME_TYPES.get(path.suffix.lower())
    if extra:
        return extra
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


async def serve_public_site(request: web.Request, sites_root: Path) -> web.StreamResponse:
    request[SITE_FLAG] = True
    path = request.path or "/"
    site = site_name_for_host(hostname_of(request))
    if path == "/browser-test-collect":
        if site != "wearegays":
            raise web.HTTPNotFound()
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(
                method=request.method, allowed_methods=("POST",)
            )
        return web.json_response(
            {
                "ok": True,
                "received": True,
                "note": (
                    "IP address, Cookie header, and JSON payload logged for "
                    "browser security analytics on wearegays.net."
                ),
            }
        )
    if request.method not in {"GET", "HEAD"}:
        raise web.HTTPNotFound()
    if site == "wearegays":
        femsec_target = femsec_offsite_location(path)
        if femsec_target:
            return _redirect(femsec_target)
        target = WEAREGAYS_REDIRECTS.get(path)
        if target:
            return _redirect(target)
        if _WEARE_PAGE_RE.fullmatch(path.lower()):
            name = path.rsplit("/", 1)[-1]
            if not name.endswith(".html"):
                name = f"{name}.html"
            return _redirect(f"/pages/{name}")
    file_path = resolve_site_file(sites_root, site, path)
    if file_path is None:
        not_found = resolve_site_file(sites_root, site, "/404.html")
        if not_found is not None and path != "/404.html":
            response = web.FileResponse(not_found, status=404)
            response.content_type = _content_type(not_found)
            apply_site_headers(response, "/404.html")
            return response
        raise web.HTTPNotFound()
    response = web.FileResponse(file_path)
    response.content_type = _content_type(file_path)
    apply_site_headers(response, path)
    return response


def attach_site_routes(app: web.Application, sites_root: Path) -> None:
    app["sites_root"] = sites_root
