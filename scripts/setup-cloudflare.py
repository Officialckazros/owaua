#!/usr/bin/env python3
"""Create free Cloudflare AI Gateway + audit Worker. Never billed products.

Needs CLOUDFLARE_API_TOKEN with AI Gateway Edit and Workers/KV Edit. Does not
enable Unified Billing, Workers Paid, R2 Infrequent Access, or Zero Trust seats.
Writes CLOUDFLARE_* keys into .env; never prints the token.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "114651c9288b7d9012b396fe66737d16").strip()
GATEWAY_ID = os.getenv("CLOUDFLARE_AI_GATEWAY", "owaua").strip() or "owaua"
API = "https://api.cloudflare.com/client/v4"
ENV_KEYS = (
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_AI_GATEWAY",
    "CLOUDFLARE_LOG_URL",
    "CLOUDFLARE_LOG_TOKEN",
)


def token() -> str:
    value = os.getenv("CLOUDFLARE_API_TOKEN", "").strip() or os.getenv("CF_API_TOKEN", "").strip()
    if not value:
        raise SystemExit(
            "CLOUDFLARE_API_TOKEN is missing. Create a free-plan token with "
            "AI Gateway Edit and Workers Scripts/KV Edit, then rerun."
        )
    return value


def request(method: str, path: str, secret: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"Cloudflare API {method} {path} failed ({exc.code}): {detail}") from None
    if not body.get("success"):
        raise SystemExit(f"Cloudflare API {method} {path} was not successful")
    return body


def upsert_env(updates: dict[str, str]) -> None:
    path = ROOT / ".env"
    lines = path.read_text().splitlines() if path.is_file() else []
    seen: set[str] = set()
    written: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else ""
        if key in updates:
            written.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            written.append(line)
    missing = [key for key in ENV_KEYS if key in updates and key not in seen]
    if missing:
        if written and written[-1] != "":
            written.append("")
        written.append("# Free Cloudflare extras. Hangout AI uses the gateway; full mode/music do not.")
        for key in missing:
            written.append(f"{key}={updates[key]}")
    path.write_text("\n".join(written) + "\n")
    os.chmod(path, 0o600)


def main() -> None:
    secret = token()
    verify = request("GET", f"/accounts/{ACCOUNT_ID}/tokens/verify", secret)
    status = (verify.get("result") or {}).get("status")
    if status not in {None, "active"}:
        raise SystemExit("Cloudflare API token is not active")

    gateways = request("GET", f"/accounts/{ACCOUNT_ID}/ai-gateway/gateways", secret)
    existing = [
        item
        for item in gateways.get("result") or []
        if isinstance(item, dict) and item.get("id") == GATEWAY_ID
    ]
    body = {
        "id": GATEWAY_ID,
        "cache_invalidate_on_update": True,
        "cache_ttl": 0,
        "collect_logs": True,
        "rate_limiting_interval": 60,
        "rate_limiting_limit": 12,
        "rate_limiting_technique": "sliding",
    }
    if existing:
        request(
            "PUT",
            f"/accounts/{ACCOUNT_ID}/ai-gateway/gateways/{GATEWAY_ID}",
            secret,
            {key: value for key, value in body.items() if key != "id"},
        )
        print(f"Updated free AI Gateway {GATEWAY_ID} (cache off, logs on, 12 req/min)")
    else:
        request("POST", f"/accounts/{ACCOUNT_ID}/ai-gateway/gateways", secret, body)
        print(f"Created free AI Gateway {GATEWAY_ID} (cache off, logs on, 12 req/min)")

    namespaces = request("GET", f"/accounts/{ACCOUNT_ID}/storage/kv/namespaces", secret)
    kv_id = ""
    for item in namespaces.get("result") or []:
        if isinstance(item, dict) and item.get("title") == "owaua-audit":
            kv_id = str(item.get("id") or "")
            break
    if not kv_id:
        created = request(
            "POST",
            f"/accounts/{ACCOUNT_ID}/storage/kv/namespaces",
            secret,
            {"title": "owaua-audit"},
        )
        kv_id = str((created.get("result") or {}).get("id") or "")
    if not kv_id:
        raise SystemExit("Could not create or find the free KV namespace owaua-audit")

    log_token = os.getenv("CLOUDFLARE_LOG_TOKEN", "").strip() or secrets.token_urlsafe(32)
    script = (ROOT / "cloudflare" / "audit-worker.js").read_text()
    metadata = {
        "main_module": "audit-worker.js",
        "bindings": [
            {"type": "kv_namespace", "name": "LOGS", "namespace_id": kv_id},
            {"type": "secret_text", "name": "LOG_TOKEN", "text": log_token},
        ],
        "compatibility_date": "2026-09-15",
    }
    boundary = "----owaua-worker"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
        "Content-Type: application/json\r\n\r\n"
        f"{json.dumps(metadata)}\r\n",
        f"--{boundary}\r\nContent-Disposition: form-data; "
        'name="audit-worker.js"; filename="audit-worker.js"\r\n'
        "Content-Type: application/javascript+module\r\n\r\n"
        f"{script}\r\n",
        f"--{boundary}--\r\n",
    ]
    req = urllib.request.Request(
        f"{API}/accounts/{ACCOUNT_ID}/workers/scripts/owaua-audit",
        data="".join(parts).encode(),
        method="PUT",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            uploaded = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise SystemExit(f"Workers upload failed ({exc.code}): {detail}") from None
    if not uploaded.get("success"):
        raise SystemExit("Workers upload was not successful")

    subdomain = request("GET", f"/accounts/{ACCOUNT_ID}/workers/subdomain", secret)
    host = (subdomain.get("result") or {}).get("subdomain")
    log_url = f"https://owaua-audit.{host}.workers.dev/" if host else ""
    if host:
        request(
            "POST",
            f"/accounts/{ACCOUNT_ID}/workers/scripts/owaua-audit/subdomain",
            secret,
            {"enabled": True},
        )

    updates = {
        "CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID,
        "CLOUDFLARE_AI_GATEWAY": GATEWAY_ID,
    }
    if log_url:
        updates["CLOUDFLARE_LOG_URL"] = log_url
        updates["CLOUDFLARE_LOG_TOKEN"] = log_token
    upsert_env(updates)
    print("Hangout AI will use the free AI Gateway; full mode and music stay direct.")
    if log_url:
        print("Music audit copies also go to the free owaua-audit Worker.")
    print("Updated .env with CLOUDFLARE_* keys. No Cloudflare paid products were enabled.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
