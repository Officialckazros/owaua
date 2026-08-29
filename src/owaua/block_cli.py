"""Global CLI for hard-blocking Discord users from owaua.

Usage:
    block access <user_id>          Hard-block — no chat, DMs, commands, slash
    block unblock <user_id>         Remove a live (CLI) block
    block list                      Show live-blocked user ids
    block help                      This help

Also accepted forms:
    block access <user_id>
    block <user_id>                 shorthand for `block access`
    PYTHONPATH=src python -m owaua.block_cli access <user_id>
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from owaua import blocked

HELP = """\
owaua hard-block CLI

  block access <user_id>     Block a user from ALL bot interaction
  block unblock <user_id>    Unblock a user (CLI blocks only)
  block list                 List live-blocked users
  block help                 Show this help

Notes:
  • Takes effect immediately on a running bot (no restart).
  • Static blocks from code/env (OWAUA_BLOCKED_USERS) are permanent
    until you edit config — `block unblock` only clears CLI blocks.
  • The bot owner cannot be blocked.
"""


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    except (TypeError, ValueError, OSError):
        return "—"


def cmd_access(args: list[str]) -> int:
    if not args:
        print("usage: block access <user_id>", file=sys.stderr)
        return 2
    uid_raw = args[0]
    reason = " ".join(args[1:]).strip()
    try:
        uid = blocked.normalize_user_id(uid_raw)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    owner = (os.getenv("OWAUA_OWNER_ID") or "").strip()
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("OWAUA_OWNER_ID="):
                    owner = line.split("=", 1)[1].strip().strip('"').strip("'") or owner
                    break
        except OSError as exc:
            print(
                f"warning: could not read owner configuration ({type(exc).__name__})",
                file=sys.stderr,
            )

    if uid == owner:
        print("error: refusing to block the bot owner", file=sys.stderr)
        return 1

    newly = blocked.block_user(
        uid,
        reason=reason or "manual block via CLI",
        category="manual_cli",
        trigger_source="cli_command",
    )
    if newly:
        print(f"blocked access for user {uid}")
        print("  they can no longer interact with the bot in any way")
        print("  state: committed to SQLite")
    else:
        print(f"user {uid} was already blocked (updated metadata)")
    if reason:
        print(f"  reason: {reason}")
    return 0


def cmd_unblock(args: list[str]) -> int:
    if not args:
        print("usage: block unblock <user_id>", file=sys.stderr)
        return 2
    try:
        uid = blocked.normalize_user_id(args[0])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    ok = blocked.unblock_user(uid, expected_source="manual")

    if ok:
        print(f"unblocked user {uid}")
        return 0
    print(
        f"user {uid} has no removable manual block; ToS blocks require the ToS review CLI",
        file=sys.stderr,
    )
    return 1



def cmd_list(_args: list[str]) -> int:
    entries = blocked.list_blocked()
    if not entries:
        print("no live-blocked users")
        print("(SQLite block state is empty)")
        return 0
    print(f"{len(entries)} live-blocked user(s):")
    for uid, meta in sorted(entries.items()):
        reason = (meta or {}).get("reason") or ""
        when = _fmt_ts((meta or {}).get("blocked_at"))
        extra = f"  reason={reason}" if reason else ""
        print(f"  {uid}  blocked_at={when}{extra}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0

    cmd = argv[0].lower()
    rest = argv[1:]

    if cmd in ("access", "ban", "block"):
        return cmd_access(rest)
    if cmd in ("unblock", "unban", "allow", "remove"):
        return cmd_unblock(rest)
    if cmd in ("list", "ls", "show"):
        return cmd_list(rest)

    if cmd.isdigit() or (cmd.startswith("<@") and cmd.endswith(">")):
        return cmd_access([cmd] + rest)

    print(f"unknown command: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
