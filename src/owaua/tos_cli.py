"""Owner CLI for ToS hard-blocks (auto-enforced by the bot).

Usage:
    tos break                 List every ToS-blocked user + why, then interactive unblock
    tos break list            List only (no prompt)
    tos break unblock <id>    Unblock one user and DM them that they can use the bot
    tos break unblock all     Unblock everyone on the ToS list (with confirm) + DM each
    tos help                  This help

Also:
    PYTHONPATH=src python -m owaua.tos_cli break
"""

from __future__ import annotations

import asyncio
import os
import sys
import typing
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from owaua import blocked
from owaua.legal import TERMS_URL

HELP = """\
owaua ToS break review CLI

  tos break                 List every ToS-blocked user + why, then pick who to unblock
  tos break list            List only
  tos break unblock <id>    Unblock one user and DM them access is restored
  tos break unblock all     Unblock all ToS blocks (asks confirm) + DM each
  tos help                  Show this help

Notes:
  • Only live blocks whose reason starts with "tos:" (auto ToS enforcement)
    and emergency ToS flags are shown. Static env/hardcoded blocks are not listed.
  • Unblock takes effect immediately on a running bot (no restart).
  • After unblock the bot DMs the user that they can use it again (if their
    DMs are open). Use --no-dm to skip the notify.
"""

UNBLOCK_DM = (
    "You've been **unblocked** from owaua.\n"
    "You can use the bot again.\n\n"
    "Please stay within the Terms of Service:\n"
    f"{TERMS_URL}"
)


def _fmt_ts(ts: typing.Any) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "—"


def _is_tos_reason(reason: str) -> bool:
    r = (reason or "").strip().lower()
    return r.startswith("tos:") or r.startswith("tos ")


def _load_local_env() -> None:
    """Load the repository's explicit operator config without using the CWD."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    try:
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
    except Exception as exc:
        print(
            f"[warn] could not load local environment ({type(exc).__name__})",
            file=sys.stderr,
        )


def _discord_token() -> str:
    """Load DISCORD_TOKEN without importing full config (avoids requiring AI keys)."""
    _load_local_env()
    return (os.getenv("DISCORD_TOKEN") or "").strip()


def _db_path() -> Path:
    """Resolve owaua.db without importing config (config needs AI keys + dotenv)."""
    _load_local_env()
    raw = (os.getenv("OWAUA_DB") or "").strip()
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else Path(__file__).resolve().parent.parent.parent / p
    return Path(__file__).resolve().parent.parent.parent / "owaua.db"


def _kv_rows(like: str, value: Optional[str] = None) -> List[Tuple[str, str]]:
    """Read kv rows via raw sqlite (no config import)."""
    import sqlite3

    path = _db_path()
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(str(path))
        try:
            if value is None:
                cur = con.execute("SELECT key, value FROM kv WHERE key LIKE ?", (like,))
            else:
                cur = con.execute(
                    "SELECT key, value FROM kv WHERE key LIKE ? AND value = ?",
                    (like, value),
                )
            return [(str(r[0]), str(r[1]) if r[1] is not None else "") for r in cur.fetchall()]
        finally:
            con.close()
    except Exception as e:
        print(f"[warn] sqlite read failed ({path.name}): {e}", file=sys.stderr)
        return []


def _kv_set(key: str, value: str) -> None:
    import sqlite3

    path = _db_path()
    if not path.exists():
        return
    try:
        con = sqlite3.connect(str(path))
        try:
            con.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print(f"[warn] sqlite write failed for {key}: {e}", file=sys.stderr)


def _collect_emergency_blocks() -> Dict[str, dict[typing.Any, typing.Any]]:
    """Read legacy emergency flags retained in SQLite for compatibility."""
    out: Dict[str, dict[typing.Any, typing.Any]] = {}
    for key, _val in _kv_rows("uf:%:tos_emergency_block", "1"):
        parts = key.split(":")
        if len(parts) < 3:
            continue
        uid = parts[1].strip()
        if not uid.isdigit():
            continue
        out[uid] = {
            "blocked_at": None,
            "reason": "tos: legacy emergency flag",
            "source": "emergency",
        }
    return out


def collect_tos_blocks() -> List[Tuple[str, dict[typing.Any, typing.Any]]]:
    """
    Return sorted list of (user_id, meta) for every ToS hard-block.

    Sources:
      1. Transactional SQLite dynamic-block records marked as ToS enforcement
      2. Legacy SQLite emergency flags
    """
    found: Dict[str, dict[typing.Any, typing.Any]] = {}

    for uid, meta in blocked.list_blocked().items():
        meta = meta if isinstance(meta, dict) else {}
        reason = meta.get("reason") or ""
        if not _is_tos_reason(reason):
            continue
        entry = dict(meta)
        entry.setdefault("source", "sqlite")
        found[str(uid)] = entry

    for uid, meta in _collect_emergency_blocks().items():
        if uid in found:
            found[uid]["emergency_flag"] = True
        else:
            found[uid] = dict(meta)

    return sorted(found.items(), key=lambda kv: float(kv[1].get("blocked_at") or 0), reverse=True)


def print_list(
    entries: List[Tuple[str, dict[typing.Any, typing.Any]]], numbered: bool = True
) -> None:
    if not entries:
        print("no ToS-blocked users")
        print("(SQLite block state is empty)")
        return
    print(f"{len(entries)} ToS-blocked user(s):\n")
    for i, (uid, meta) in enumerate(entries, 1):
        meta = meta if isinstance(meta, dict) else {}
        reason = (meta.get("reason") or "").strip() or "(no reason recorded)"
        when = _fmt_ts(meta.get("blocked_at"))
        src = meta.get("source") or "sqlite"
        cat = meta.get("category") or "general"
        user_tag = meta.get("user_tag") or ""
        offending = (meta.get("offending_text") or "").strip()
        guild_name = meta.get("guild_name") or meta.get("guild_id") or ""
        channel_id = meta.get("channel_id") or ""
        trigger = meta.get("trigger_source") or ""
        strikes = meta.get("strikes_detail") or ""
        history: typing.Any = meta.get("history") or []

        extra = ""
        if meta.get("emergency_flag"):
            extra = "  [+emergency flag]"
        elif src == "emergency":
            extra = "  [emergency only]"

        prefix = f"  [{i}] " if numbered else "  "
        user_label = f"{uid}" + (f" ({user_tag})" if user_tag else "")
        print(f"{prefix}{user_label}")
        print(f"       why:      {reason}")
        print(f"       category: {cat}")
        print(f"       when:     {when}  source: {src}{extra}")
        if guild_name or channel_id:
            loc = f"guild: {guild_name}" if guild_name else ""
            if channel_id:
                loc += f" channel: {channel_id}" if loc else f"channel: {channel_id}"
            print(f"       location: {loc}")
        if trigger or strikes:
            print(f"       trigger:  {trigger or '—'}" + (f" ({strikes})" if strikes else ""))
        if offending:
            lines = offending.splitlines()
            print("       OFFENDING EVIDENCE:")
            print("         ┌──────────────────────────────────────────────────────────")
            for line in lines[:8]:
                print(f"         │ {line}")
            if len(lines) > 8:
                print(f"         │ … ({len(lines) - 8} more lines)")
            print("         └──────────────────────────────────────────────────────────")
        if isinstance(history, list) and len(typing.cast(typing.Any, history)) > 1:
            print(
                f"       history:  {len(typing.cast(typing.Any, history))} violation events recorded"
            )
        print()


def _clear_tos_side_effects(uid: str) -> None:
    """Drop emergency flag / spam bucket so the user is fully free again."""
    _kv_set(f"uf:{uid}:tos_emergency_block", "")
    for key, val in (
        ("tos_leak_strikes", "0"),
        ("tos_violation_strikes", "0"),
        ("tos_spam_strikes", "0"),
        ("tos_model_strikes", "0"),
        ("tos_spam_bucket", ""),
    ):
        _kv_set(f"uf:{uid}:{key}", val)


async def _dm_user(uid: str, text: str, token: str) -> Tuple[bool, str]:
    """Send a one-shot DM via the bot account. Returns (ok, detail)."""
    import discord

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    result: Dict[str, object] = {"ok": False, "detail": "not started"}

    @client.event
    async def on_ready():
        try:
            user = await client.fetch_user(int(uid))
            await user.send(text)
            result["ok"] = True
            result["detail"] = f"dm sent to {user} ({uid})"
        except discord.NotFound:
            result["detail"] = f"no Discord user {uid}"
        except discord.Forbidden:
            result["detail"] = f"DMs closed / bot blocked by {uid}"
        except Exception as e:
            result["detail"] = f"dm failed for {uid}: {e}"
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.LoginFailure:
        return False, "login failed — check DISCORD_TOKEN in .env"
    except Exception as e:
        return False, f"discord error: {e}"
    return bool(result["ok"]), str(result["detail"])


async def _dm_many(uids: List[str], text: str, token: str) -> List[Tuple[str, bool, str]]:
    """One login, many DMs."""
    import discord

    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    ready = asyncio.Event()
    results: List[Tuple[str, bool, str]] = []

    @client.event
    async def on_ready():
        ready.set()

    async def work():
        await ready.wait()
        for uid in uids:
            try:
                user = await client.fetch_user(int(uid))
                await user.send(text)
                results.append((uid, True, f"dm sent to {user}"))
            except discord.NotFound:
                results.append((uid, False, "user not found"))
            except discord.Forbidden:
                results.append((uid, False, "DMs closed / bot blocked"))
            except Exception as e:
                results.append((uid, False, str(e)))
            await asyncio.sleep(0.4)
        await client.close()

    task = asyncio.create_task(work())
    try:
        await client.start(token)
    except discord.LoginFailure:
        return [(u, False, "login failed — check DISCORD_TOKEN in .env") for u in uids]
    except Exception as e:
        return [(u, False, f"discord error: {e}") for u in uids]
    await task
    return results


def unblock_users(uids: List[str], *, notify: bool = True) -> int:
    """Unblock each id, clear ToS side effects, optionally DM. Returns failures count."""
    if not uids:
        print("nothing to unblock")
        return 0

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        print(
            "error: synchronous CLI unblock cannot run inside an active event loop; "
            "use the host CLI or an async bot service",
            file=sys.stderr,
        )
        return 1

    unblocked: List[str] = []
    failures = 0
    emergency_ids = set(_collect_emergency_blocks())
    for uid_raw in uids:
        try:
            uid = blocked.normalize_user_id(uid_raw)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            failures += 1
            continue

        meta = blocked.get_blocked_user(uid)
        if meta is not None and not _is_tos_reason(str(meta.get("reason") or "")):
            print(f"refusing to remove non-ToS/manual block for {uid}", file=sys.stderr)
            failures += 1
            continue
        if meta is None and uid not in emergency_ids:
            print(f"user {uid} has no ToS block", file=sys.stderr)
            failures += 1
            continue

        removed_block = (
            blocked.unblock_user(uid, expected_source="tos") if meta is not None else False
        )
        if meta is not None and not removed_block:
            print(
                f"refusing to remove block for {uid}: its source changed concurrently",
                file=sys.stderr,
            )
            failures += 1
            continue
        _clear_tos_side_effects(uid)
        if removed_block:
            print(f"unblocked {uid} (removed from SQLite block state)")
        else:
            print(f"cleared emergency ToS flags for {uid}")
        unblocked.append(uid)

    if not unblocked:
        return max(1, failures)

    if not notify:
        print("(skipped DM notify — --no-dm)")
        return failures

    token = _discord_token()
    if not token:
        print("warn: no DISCORD_TOKEN — unblocked, but could not DM users", file=sys.stderr)
        return failures + len(unblocked)

    print(f"notifying {len(unblocked)} user(s)…")
    try:
        results = asyncio.run(_dm_many(unblocked, UNBLOCK_DM, token))
    except KeyboardInterrupt:
        print("\n(interrupted during DMs)")
        return 1
    for uid, ok, detail in results:
        mark = "ok" if ok else "fail"
        print(f"  [{mark}] {uid}: {detail}")
        if not ok:
            failures += 1
    return failures


def cmd_break_info(args: list[str]) -> int:
    if not args:
        print("usage: tos break info <user_id>", file=sys.stderr)
        return 2
    try:
        uid = blocked.normalize_user_id(args[0])
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    meta = blocked.get_blocked_user(uid)
    if not meta:
        print(f"user {uid} is not dynamically ToS-blocked.")
        return 1

    print_list([(uid, meta)], numbered=False)
    hist: typing.Any = meta.get("history") or []
    if isinstance(hist, list) and len(typing.cast(typing.Any, hist)) > 1:
        print("Violation History:")
        for idx, ev in typing.cast(
            typing.Iterable[typing.Any], enumerate(typing.cast(typing.Any, hist), 1)
        ):
            ts = _fmt_ts(ev.get("timestamp"))
            reas: typing.Any = typing.cast(typing.Any, ev.get("reason") or "—")
            cat: typing.Any = typing.cast(typing.Any, ev.get("category") or "—")
            txt: typing.Any = typing.cast(typing.Any, ev.get("offending_text") or "—")
            g_name: typing.Any = typing.cast(
                typing.Any, ev.get("guild_name") or ev.get("guild_id") or "—"
            )
            print(f"  [{idx}] {ts} | cat: {cat} | server: {g_name}")
            print(f"      reason: {reas}")
            if txt and txt != "—":
                print(f"      input:  {txt[:200]}")
        print()
    return 0


def cmd_break_list(_args: list[str], *, numbered: bool = False) -> int:
    entries = collect_tos_blocks()
    print_list(entries, numbered=numbered)
    return 0


def cmd_break_unblock(args: list[str], *, notify: bool = True) -> int:
    if not args:
        print("usage: tos break unblock <user_id>|all", file=sys.stderr)
        return 2

    target = args[0].lower()
    if target == "all":
        entries = collect_tos_blocks()
        if not entries:
            print("no ToS-blocked users")
            return 0
        print_list(entries, numbered=True)
        try:
            confirm = (
                input(f"unblock ALL {len(entries)} ToS-blocked user(s)? [y/N] ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        if confirm not in ("y", "yes"):
            print("cancelled")
            return 1
        return unblock_users([uid for uid, _ in entries], notify=notify)

    return unblock_users(args, notify=notify)


def cmd_break_interactive(args: list[str], *, notify: bool = True) -> int:
    """List everyone, then prompt for who to unblock."""
    if args:
        sub = args[0].lower()
        rest = args[1:]
        if sub in ("list", "ls", "show"):
            return cmd_break_list(rest, numbered=True)
        if sub in ("info", "detail", "view", "inspect"):
            return cmd_break_info(rest)
        if sub in ("unblock", "unban", "allow", "remove", "free"):
            return cmd_break_unblock(rest, notify=notify)
        if sub in ("-h", "--help", "help"):
            print(HELP)
            return 0
        if sub.isdigit() or sub.startswith("<@"):
            return cmd_break_unblock(args, notify=notify)

    entries = collect_tos_blocks()
    print_list(entries, numbered=True)
    if not entries:
        return 0

    print("Unblock options:")
    print("  • type a number (e.g. 1) or a Discord user id")
    print("  • comma-separated for several (e.g. 1,3 or id1,id2)")
    print("  • 'all' to unblock everyone")
    print("  • 'q' / empty to quit without changes")
    if not notify:
        print("  (DM notify is OFF — --no-dm)")
    print()

    try:
        choice = input("unblock> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if not choice or choice.lower() in ("q", "quit", "exit", "n", "no"):
        print("done (no changes)")
        return 0

    if choice.lower() == "all":
        return unblock_users([uid for uid, _ in entries], notify=notify)

    targets: List[str] = []
    for part in choice.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit() and len(part) <= 4:
            idx = int(part)
            if 1 <= idx <= len(entries):
                targets.append(entries[idx - 1][0])
            else:
                print(f"error: index {idx} out of range 1–{len(entries)}", file=sys.stderr)
        else:
            targets.append(part)

    if not targets:
        print("no valid targets")
        return 1
    return unblock_users(targets, notify=notify)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    notify = True
    if "--no-dm" in argv:
        notify = False
        argv = [a for a in argv if a != "--no-dm"]
    if "--dm" in argv:
        notify = True
        argv = [a for a in argv if a != "--dm"]

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0

    cmd = argv[0].lower()
    rest = argv[1:]

    if cmd in ("break", "breaks", "violations", "blocked"):
        return cmd_break_interactive(rest, notify=notify)

    if cmd in ("list", "ls", "show"):
        return cmd_break_list(rest, numbered=True)

    if cmd in ("unblock", "unban", "allow", "remove", "free"):
        return cmd_break_unblock(rest, notify=notify)

    print(f"unknown command: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
