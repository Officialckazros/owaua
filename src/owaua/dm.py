"""Interactive CLI for DMing Discord users through the owaua bot account.

Full interactive shell (recommended):
    PYTHONPATH=src python -m owaua.dm

Jump straight into a chat with one user:
    PYTHONPATH=src python -m owaua.dm <user_id>

Fire a single message and exit, no shell:
    PYTHONPATH=src python -m owaua.dm <user_id> "message text"
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import stat
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import discord

from owaua import config, db

INTENTS = discord.Intents.default()
INTENTS.dm_messages = True

_ROOT = Path(__file__).resolve().parent.parent.parent
CONTACTS_FILE = Path(os.getenv("OWAUA_DM_CONTACTS_FILE", str(_ROOT / "dm_contacts.json")))

ACTIVE_CHATS_FILE = Path(os.getenv("OWAUA_CLI_ACTIVE_FILE", str(_ROOT / "cli_active_chats.json")))
ACTIVE_HEARTBEAT_SECONDS = 20
_ACTIVE_SESSION_ID = secrets.token_urlsafe(24)
_MAX_LEGACY_BYTES = 4 * 1024 * 1024
_MAX_CONTACTS = 100_000
_warned_legacy_files: set[str] = set()

HELP_TEXT = """\
Commands:
  send <user_id> <message...>   Send a single message, stay in the shell.
  chat <user_id>                 Open a live chat with that user (/back to leave).
  contacts                       List people you've DMed before, most recent first.
  help                           Show this help.
  quit                           Disconnect and exit.
"""


def _migration_name(kind: str, path: Path) -> str:
    identity = str(path.absolute()).encode("utf-8", errors="surrogateescape")
    return f"{kind}-json-v1:{hashlib.sha256(identity).hexdigest()}"


def _secure_json_read(path: Path) -> tuple[str, object | None]:
    """Read an owner-only regular legacy state file without following links."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return "invalid", None
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "invalid", None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "invalid", None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LEGACY_BYTES:
            return "invalid", None
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            return "invalid", None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            payload = handle.read(_MAX_LEGACY_BYTES + 1)
        if len(payload) > _MAX_LEGACY_BYTES:
            return "invalid", None
        try:
            return "ok", json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "invalid", None
    finally:
        os.close(fd)


def _warn_invalid(path: Path) -> None:
    marker = str(path.absolute())
    if marker in _warned_legacy_files:
        return
    _warned_legacy_files.add(marker)
    warnings.warn(
        f"legacy state file {path.name!r} is unsafe or invalid; migration was not marked complete",
        RuntimeWarning,
        stacklevel=2,
    )


def _discord_id(value: object) -> str:
    raw = str(value or "").strip()
    return raw if raw.isdigit() and len(raw) <= 32 else ""


def _safe_terminal_text(value: object, limit: int = 4_000) -> str:
    """Strip control characters so Discord text cannot inject terminal escapes."""
    clean = "".join(
        char
        for char in str(value or "")
        if char in "\n\t" or (char.isprintable() and char != "\x1b")
    )
    return clean[:limit]


def _contact_row(user_id: object, info: object) -> tuple[str, str, str] | None:
    uid = _discord_id(user_id)
    if not uid or not isinstance(info, dict):
        return None
    name = _safe_terminal_text(info.get("name") or "?", 200).strip() or "?"
    last_message_at = str(info.get("last_message_at") or "").strip()[:64]
    try:
        parsed = datetime.fromisoformat(last_message_at)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    last_message_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    return uid, name, last_message_at


def _migrate_contacts() -> None:
    migration = _migration_name("dm-contacts", CONTACTS_FILE)
    if db.legacy_state_migrated(migration):
        return
    status, decoded = _secure_json_read(CONTACTS_FILE)
    if status == "invalid":
        _warn_invalid(CONTACTS_FILE)
        return
    records: list[tuple[str, str, str]] = []
    if isinstance(decoded, dict):
        for user_id, info in list(decoded.items())[:_MAX_CONTACTS]:
            row = _contact_row(user_id, info)
            if row is not None:
                records.append(row)
    db.import_legacy_dm_contacts(migration, records)


def load_contacts() -> dict:
    """Return contacts from SQLite after an idempotent legacy import."""
    _migrate_contacts()
    # Bot startup calls this API, so complete the companion active-session
    # migration here as well before any privacy export/delete can run.
    _migrate_active()
    return db.dm_contacts_all()


def save_contacts(contacts: dict) -> None:
    """Compatibility bulk-upsert for callers that previously saved JSON."""
    _migrate_contacts()
    if not isinstance(contacts, dict):
        raise TypeError("contacts must be a mapping")
    records = []
    for user_id, info in list(contacts.items())[:_MAX_CONTACTS]:
        row = _contact_row(user_id, info)
        if row is not None:
            records.append(row)
    db.dm_contacts_upsert(records)


def _save_contact(user_id: object, name: object, last_message_at: str) -> None:
    row = _contact_row(
        user_id,
        {"name": name, "last_message_at": last_message_at},
    )
    if row is None:
        raise ValueError("invalid DM contact")
    _migrate_contacts()
    db.dm_contacts_upsert([row])


def _migrate_active() -> None:
    migration = _migration_name("cli-active-conversations", ACTIVE_CHATS_FILE)
    if db.legacy_state_migrated(migration):
        return
    status, decoded = _secure_json_read(ACTIVE_CHATS_FILE)
    if status == "invalid":
        _warn_invalid(ACTIVE_CHATS_FILE)
        return
    records: list[tuple[str, str, float]] = []
    if isinstance(decoded, dict):
        current = time.time()
        for user_id, heartbeat in list(decoded.items())[:_MAX_CONTACTS]:
            uid = _discord_id(user_id)
            try:
                stamp = float(heartbeat)
            except (TypeError, ValueError, OverflowError):
                continue
            if uid and 0 <= stamp <= current + 86_400:
                records.append((uid, "legacy-json", stamp))
    db.import_legacy_cli_active(migration, records)


def _load_active() -> dict:
    """Compatibility snapshot using the newest heartbeat per user."""
    _migrate_active()
    rows = (
        db.conn()
        .execute(
            "SELECT user_id,MAX(heartbeat) AS heartbeat "
            "FROM cli_active_conversations GROUP BY user_id"
        )
        .fetchall()
    )
    return {str(row["user_id"]): float(row["heartbeat"]) for row in rows}


def _mark_active(user_id: int, session_id: str = _ACTIVE_SESSION_ID) -> None:
    uid = _discord_id(user_id)
    if not uid:
        raise ValueError("invalid Discord user id")
    _migrate_active()
    db.cli_active_touch(uid, session_id)


def _mark_inactive(user_id: int, session_id: str = _ACTIVE_SESSION_ID) -> None:
    uid = _discord_id(user_id)
    if not uid:
        raise ValueError("invalid Discord user id")
    _migrate_active()
    db.cli_active_remove(uid, session_id)


def is_cli_conversation_active(user_id: int, ttl_seconds: float = 90.0) -> bool:
    """Return whether any live CLI session currently owns this DM user."""
    uid = _discord_id(user_id)
    if not uid:
        return False
    _migrate_active()
    return db.cli_active_is_claimed(uid, ttl_seconds=ttl_seconds)


def message_text(msg: discord.Message) -> str:
    """owaua's own replies go out as embeds, so message.content is often
    empty — fall back to the embed's title/description/fields for those."""
    if msg.content:
        return _safe_terminal_text(msg.content)
    parts = []
    for e in msg.embeds:
        if e.title:
            parts.append(_safe_terminal_text(e.title))
        if e.description:
            parts.append(_safe_terminal_text(e.description))
        for f in e.fields:
            if f.name or f.value:
                parts.append(f"{_safe_terminal_text(f.name)}: {_safe_terminal_text(f.value)}")
    if parts:
        return " | ".join(p for p in parts if p)
    if msg.attachments:
        names = (_safe_terminal_text(a.filename, 250) for a in msg.attachments)
        return f"[attachment: {', '.join(names)}]"
    return "(empty)"


class DMShell(discord.Client):
    def __init__(self):
        super().__init__(intents=INTENTS)
        self.contacts = load_contacts()
        self.chat_target: int | None = None
        self.active_session_id = secrets.token_urlsafe(24)
        self.ready_event = asyncio.Event()

    async def on_ready(self):
        print(f"Connected as {self.user}.\n")
        self.ready_event.set()

    def _touch_contact(self, user: discord.User) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        name = _safe_terminal_text(user, 200)
        self.contacts[str(user.id)] = {
            "name": name,
            "last_message_at": timestamp,
        }
        _save_contact(user.id, name, timestamp)

    async def on_message(self, message: discord.Message):
        if self.user and message.author.id == self.user.id:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        self._touch_contact(message.author)
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        if self.chat_target == message.author.id:
            print(f"\n[{stamp}] {message.author}: {message_text(message)}")
            print("chat> ", end="", flush=True)
        else:
            print(
                f"\n[{stamp}] New DM from {message.author} (id {message.author.id}): "
                f"{message_text(message)}"
            )
            print(f"  -> reply with: chat {message.author.id}")
            print("> ", end="", flush=True)

    async def resolve_user(self, user_id: int) -> discord.User | None:
        try:
            return await self.fetch_user(user_id)
        except discord.NotFound:
            print(f"No Discord user found with id {user_id}.")
        except discord.HTTPException as e:
            print(f"Failed to fetch user {user_id}: {_safe_terminal_text(e, 500)}")
        return None

    async def send_to(self, user: discord.User, content: str) -> bool:
        try:
            await user.send(content)
            self._touch_contact(user)
            return True
        except discord.Forbidden:
            print("Could not send — this user has DMs closed or has blocked the bot.")
        except discord.HTTPException as e:
            print(f"Send failed: {_safe_terminal_text(e, 500)}")
        return False

    async def cmd_contacts(self) -> None:
        if not self.contacts:
            print("No contacts yet — send or receive a DM to add one.")
            return
        rows = sorted(
            self.contacts.items(),
            key=lambda kv: kv[1].get("last_message_at", ""),
            reverse=True,
        )
        for uid, info in rows:
            print(f"  {uid}  {info.get('name', '?')}  (last: {info.get('last_message_at', '?')})")

    async def cmd_send(self, user_id: int, content: str) -> None:
        user = await self.resolve_user(user_id)
        if not user:
            return
        if await self.send_to(user, content):
            print(f"Sent to {user}: {_safe_terminal_text(content)}")

    async def cmd_chat(self, user_id: int) -> None:
        user = await self.resolve_user(user_id)
        if not user:
            return
        channel = user.dm_channel or await user.create_dm()
        print(f"-- Chatting with {user} ({user.id}). /back to return to the menu. --")
        print("Fetching full conversation history...")
        try:
            count = 0
            async for msg in channel.history(limit=None, oldest_first=True):
                who = "you" if msg.author.id == self.user.id else str(msg.author)
                stamp = msg.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                print(f"  [{stamp}] {who}: {message_text(msg)}")
                count += 1
            print(f"-- {count} message(s) total. --")
        except discord.HTTPException as e:
            print(f"Could not fetch history: {_safe_terminal_text(e, 500)}")

        self.chat_target = user_id
        _mark_active(user_id, self.active_session_id)
        heartbeat = asyncio.create_task(self._heartbeat(user_id, self.active_session_id))
        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    line = await loop.run_in_executor(None, lambda: input("chat> "))
                except EOFError:
                    break
                line = line.strip()
                if not line:
                    continue
                if line in ("/back", "/quit", "/exit"):
                    break
                await self.send_to(user, line)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.chat_target = None
            _mark_inactive(user_id, self.active_session_id)
        print("-- Left chat. --\n")

    async def _heartbeat(self, user_id: int, session_id: str) -> None:
        """Keep re-marking this user active so bot.py's staleness check
        (see bot.py's _CLI_ACTIVE_TTL) doesn't let the AI take back over
        mid-conversation."""
        try:
            while True:
                await asyncio.sleep(ACTIVE_HEARTBEAT_SECONDS)
                _mark_active(user_id, session_id)
        except asyncio.CancelledError:
            pass

    async def shell_loop(self, initial_chat_id: int | None = None) -> None:
        await self.ready_event.wait()
        print(HELP_TEXT)

        if initial_chat_id is not None:
            await self.cmd_chat(initial_chat_id)

        loop = asyncio.get_event_loop()
        while not self.is_closed():
            try:
                line = await loop.run_in_executor(None, lambda: input("> "))
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()

            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print(HELP_TEXT)
            elif cmd == "contacts":
                await self.cmd_contacts()
            elif cmd == "send":
                if len(parts) < 3:
                    print("Usage: send <user_id> <message>")
                    continue
                try:
                    uid = int(parts[1])
                except ValueError:
                    print("user_id must be numeric.")
                    continue
                await self.cmd_send(uid, parts[2])
            elif cmd == "chat":
                if len(parts) < 2:
                    print("Usage: chat <user_id>")
                    continue
                try:
                    uid = int(parts[1])
                except ValueError:
                    print("user_id must be numeric.")
                    continue
                await self.cmd_chat(uid)
            else:
                print(f"Unknown command '{cmd}'. Type 'help' for the command list.")
        await self.close()


async def run_one_shot(user_id: int, message: str) -> None:
    client = DMShell()

    @client.event
    async def on_ready():
        print(f"Connected as {client.user}.")
        user = await client.resolve_user(user_id)
        if user and await client.send_to(user, message):
            print(f"Sent to {user}: {message}")
        await client.close()

    await client.start(config.DISCORD_TOKEN)


async def run_shell(initial_chat_id: int | None = None) -> None:
    client = DMShell()
    shell_task = asyncio.create_task(client.shell_loop(initial_chat_id))
    try:
        await client.start(config.DISCORD_TOKEN)
    finally:
        shell_task.cancel()
        await asyncio.gather(shell_task, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="DM Discord users via the owaua bot account.")
    parser.add_argument("user_id", nargs="?", help="Target Discord user ID")
    parser.add_argument("message", nargs="?", help="Message to send (skips the shell if set)")
    args = parser.parse_args()

    try:
        if args.user_id and args.message:
            uid = int(args.user_id)
            asyncio.run(run_one_shot(uid, args.message))
        elif args.user_id:
            uid = int(args.user_id)
            asyncio.run(run_shell(initial_chat_id=uid))
        else:
            asyncio.run(run_shell())
    except ValueError:
        print(f"'{args.user_id}' is not a valid Discord user ID (must be numeric).")
        sys.exit(1)
    except discord.LoginFailure:
        print("Login failed — check DISCORD_TOKEN in .env.")
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
