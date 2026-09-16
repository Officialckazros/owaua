"""Owaua — a small Discord hangout bot."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
import discord
import httpx
from dotenv import load_dotenv
from PIL import Image

from ask import (
    HOST_DEFAULT_MODELS,
    MAX_ATTACHMENTS,
    PERSONAS,
    ask,
    host_default_persona,
    host_model_error,
    looks_like_decode_request,
    looks_like_repeat_request,
    persona_label,
    persona_provider,
    sanitize_user_text,
    truncate,
    valid_persona,
)
from cloudflare import describe_protection
from memory import MemoryStore
from security import (OWNER_IDS, BLOCKED_USERS, ALLOWED_GUILDS, ALLOW_DMS,
                      MAX_INFLIGHT, MAX_INPUT_CHARS, MAX_REPLY_CHARS, MAX_TRACKED_USERS)
from music import (
    abandon_music_if_needed,
    configure_music_audit_log,
    handle_music_command,
    log_music_command,
    unrestricted_music_guild,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("owaua")
# CDN URLs can contain bearer-like query signatures. Do not log request URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip().replace("\\_", "_")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "").strip()
MEMORY_DB = ROOT / "data" / "memory.sqlite3"
RATE_LIMIT_REQUESTS = 8
RATE_LIMIT_WINDOW = 60.0
COMMAND_COOLDOWN = 25.0
COOLDOWN_EXEMPT_USER_IDS = frozenset({1172433512364769342})
COMMANDS = frozenset(
    {
        "!help",
        "!persona",
        "!language",
        "!music",
        "!memory",
        "!security",
        "!shutdown",
    }
)
DISCORD_MESSAGE_LIMIT = 1900
ASK_TIMEOUT = 40.0
HANDLER_TIMEOUT = 140.0
MAX_HANDLERS = 16
FULL_MODE_GUILD_ID = 1535083112709496903
FULL_MODE_CHANNEL_ID = 1535083114219700227
FULL_MODE_CHANNEL_IDS = frozenset(
    {
        FULL_MODE_CHANNEL_ID,
        1549566630726602772,
    }
)
FULL_MODE_BLOCKED_USER_IDS = frozenset(
    {
        470617205667790868,
        836988339491962881,
    }
)
FULL_MODE_ENABLE_USER_IDS = frozenset({1172433512364769342})
FULL_MODE_ALLOWED_USER_IDS = frozenset(
    {
        1172433512364769342,
        1121021729649737813,
        1124004905292664985,
        1511064708764139536,
        932956672815689758,
        1439254983219482625,
        1391094791210536970,
    }
)
FULL_MODE_USAGE = "usage: !full mode on"

HELP_TEXT = """**Owaua commands**
`!help` — show this command list
`!owner's note` — a note from the bot's owner
`!persona rudeish|nerdish|flirty|host default gpt/deepseek/mistral` — view or switch this server's persona (Manage Server)
`!language <full name>|reset` — this server's reply language and profile (Manage Server)
`!music help` — play a song in your voice channel
`!memory erase` — erase server memory (Manage Server required)
`!memory erase mine` — erase your own conversation history
`!security status|pause|resume` — API usage and emergency pause (bot owner only)
`!shutdown` — fully stop the bot (bot owner only)

Each command has a 25s cooldown."""

OWNER_NOTE_TEXT = (
    "Hello, I hope you like my bot! I'm trying to keep it as simple as possible "
    "and don't pack it with useless features/commands. I spent a lot of time "
    "developing and (trying) to promote this bot, and I really hope you like it. "
    "I would also like to know what communities this bot is in, so if you see "
    "this message please DM me on Discord (gays._) or on email (ckazros@owaua.com)"
)


def is_owner_note_command(content: str) -> bool:
    """Match `!owner's note`, including curly apostrophes from phones."""
    normalized = " ".join(
        content.strip()
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .casefold()
        .split()
    )
    return normalized == "!owner's note"


def matched_command(text: str) -> str | None:
    """Return the prefix command name, if this message is one."""
    if is_owner_note_command(text):
        return "!owner's note"
    name = text.split(maxsplit=1)[0].lower() if text else ""
    if name in COMMANDS:
        return name
    return None


def is_full_mode_command(content: str) -> bool:
    """Match `!full` and `!full ...` after command_text normalization."""
    normalized = " ".join(content.casefold().split())
    return normalized == "!full" or normalized.startswith("!full ")


def full_mode_location(message: object) -> bool:
    """True only in the one guild/channel where full mode is allowed."""
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    return (
        getattr(guild, "id", None) == FULL_MODE_GUILD_ID
        and getattr(channel, "id", None) in FULL_MODE_CHANNEL_IDS
    )


def full_mode_blocked(user_id: object) -> bool:
    return user_id in FULL_MODE_BLOCKED_USER_IDS


def full_mode_can_enable(user_id: object) -> bool:
    return user_id in FULL_MODE_ENABLE_USER_IDS or full_mode_allowed(user_id)


def full_mode_allowed(user_id: object) -> bool:
    return user_id in FULL_MODE_ALLOWED_USER_IDS and not full_mode_blocked(user_id)


def full_mode_setting_key(user_id: object) -> str:
    return f"full_mode:{user_id}"


PERSONA_USAGE = (
    "usage: !persona rudeish, !persona nerdish, !persona flirty, "
    "or !persona host default gpt/deepseek/mistral"
)
HOST_DEFAULT_USAGE = (
    "usage: !persona host default gpt, !persona host default deepseek, "
    "or !persona host default mistral"
)


def parse_persona_argument(argument: str) -> tuple[str | None, str | None]:
    """Return ``(persona, error)`` for ``!persona`` arguments."""
    text = " ".join(argument.casefold().replace("-", " ").split())
    if not text:
        return None, None
    if text in PERSONAS:
        return text, None
    if text == "host default" or text.startswith("host default "):
        rest = text[len("host default") :].strip()
        if not rest:
            return host_default_persona(), None
        if rest in HOST_DEFAULT_MODELS:
            return host_default_persona(rest), None
        return None, HOST_DEFAULT_USAGE
    return None, PERSONA_USAGE


def parse_language_name(value: str) -> tuple[str | None, str | None]:
    """Validate a human-readable language name for ``!language``."""
    language = " ".join(value.split())
    if not language:
        return None, "usage: !language <full language name> | !language reset"
    if len(language) > 64 or not any(character.isalpha() for character in language):
        return None, "use a full language name, such as `!language hungarian`"
    if not all(character.isalpha() or character in " -'" for character in language):
        return None, "use a full language name, such as `!language hungarian`"
    compact = language.casefold().replace(" ", "")
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,4})?", compact):
        return None, "please type the full language name, not a short code like `hu`"
    return language, None


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
LANGUAGE_ASSET_ALIASES = {
    "france": "french",
    "germany": "german",
    "greece": "greek",
    "hungary": "hungarian",
    "italy": "italian",
    "poland": "polish",
    "romania": "romanian",
    "ukraine": "ukrainian",
}
MAX_PROFILE_IMAGE_BYTES = 8 * 1024 * 1024
AVATAR_SIZE = 1024
BANNER_SIZE = (680, 240)
PROFILE_UPDATE_TIMEOUT = 20.0
DISCORD_RETRY_INITIAL_DELAY = 5.0
DISCORD_RETRY_MAX_DELAY = 300.0
PING_RESPONSE = "yeah?"


def language_asset_key(value: str) -> str:
    key = "".join(character for character in value.casefold() if character.isalpha())
    return LANGUAGE_ASSET_ALIASES.get(key, key)


def language_image_path(
    language: str,
    directories: tuple[str, ...],
    *,
    root: Path | None = None,
) -> Path | None:
    root = ROOT if root is None else root
    key = language_asset_key(language)
    if not key or key == "english":
        return None
    for relative in directories:
        directory = root if relative == "." else root / relative
        try:
            if not directory.is_dir():
                continue
            entries = list(directory.iterdir())
        except OSError:
            log.exception("Could not list profile images in %s", directory)
            continue
        for path in entries:
            try:
                if not path.is_file() or path.suffix.casefold() not in IMAGE_SUFFIXES:
                    continue
            except OSError:
                continue
            if language_asset_key(path.stem) == key:
                return path
    return None


def language_avatar_path(language: str, *, root: Path | None = None) -> Path | None:
    """Return the themed profile picture for a language, if one is shipped."""
    return language_image_path(language, ("pfps", "avatars", "."), root=root)


def language_banner_path(language: str, *, root: Path | None = None) -> Path | None:
    """Return the themed banner for a language, if one is shipped."""
    return language_image_path(language, ("banners",), root=root)


def looks_like_image(data: bytes) -> bool:
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return True
    return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"


def read_image_bytes(path: Path) -> bytes | None:
    try:
        if path.stat().st_size > MAX_PROFILE_IMAGE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        log.exception("Could not read profile image %s", path)
        return None
    if not data or not looks_like_image(data):
        log.warning("Skipping invalid profile image %s", path)
        return None
    return data


def _cover_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_width, target_height = size
    scale = max(target_width / max(image.width, 1), target_height / max(image.height, 1))
    resized = image.resize(
        (max(target_width, round(image.width * scale)),
         max(target_height, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_height) // 2)
    return resized.crop((left, top, left + target_width, top + target_height))


def _save_jpeg(image: Image.Image, *, quality: int) -> bytes:
    converted = image.convert("RGB")
    output = io.BytesIO()
    converted.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def prepare_avatar_bytes(data: bytes) -> bytes | None:
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) <= MAX_PROFILE_IMAGE_BYTES:
        return data
    try:
        with Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "GIF", "WEBP"]) as image:
            if image.width * image.height > 16000000:
                return None
            image.load()
            square = _cover_resize(image.convert("RGB"), (AVATAR_SIZE, AVATAR_SIZE))
            for quality in (90, 80, 65):
                encoded = _save_jpeg(square, quality=quality)
                if len(encoded) <= MAX_PROFILE_IMAGE_BYTES:
                    return encoded
    except Exception as exc:
        log.warning("Could not prepare a profile picture: %s", exc)
        return None
    return None


def prepare_banner_bytes(data: bytes) -> bytes | None:
    try:
        with Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "GIF", "WEBP"]) as image:
            if image.width * image.height > 16000000:
                return None
            image.load()
            banner = _cover_resize(image.convert("RGB"), BANNER_SIZE)
            for quality in (90, 80, 65):
                encoded = _save_jpeg(banner, quality=quality)
                if len(encoded) <= MAX_PROFILE_IMAGE_BYTES:
                    return encoded
    except Exception as exc:
        log.warning("Could not prepare a banner: %s", exc)
        return None
    return None


def profile_asset_payload(field: str, path: Path | None) -> dict[str, bytes | None]:
    if path is None:
        return {field: None}
    raw = read_image_bytes(path)
    if raw is None:
        return {}
    prepared = (
        prepare_avatar_bytes(raw) if field == "avatar" else prepare_banner_bytes(raw)
    )
    if prepared is None:
        log.warning("Skipping unusable %s image %s", field, path)
        return {}
    return {field: prepared}


def language_scope_key(message: object) -> str:
    guild = getattr(message, "guild", None)
    if guild is not None:
        return f"guild:{guild.id}"
    return f"dm:{getattr(message.channel, 'id', '')}"


def split_reply(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    remaining = text.strip()
    if not remaining:
        return []
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        break_at = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if break_at < limit // 2:
            break_at = limit
        chunks.append(remaining[:break_at].rstrip())
        remaining = remaining[break_at:].lstrip()
    return chunks


def referenced_message_context(
    message: object, bot_user_id: int | None = None, *, unbounded: bool = False
) -> str:
    """Quote the Discord message this one is a reply to, if it is cached."""
    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None)
    raw = getattr(resolved, "content", None)
    if not isinstance(raw, str):
        return ""
    if bot_user_id is not None:
        raw = raw.replace(f"<@{bot_user_id}>", " ").replace(
            f"<@!{bot_user_id}>", " "
        )
    quoted = sanitize_user_text(raw).strip()
    if not quoted:
        return ""
    if not unbounded:
        quoted = truncate(quoted)
    author = getattr(resolved, "author", None)
    speaker = (
        "you"
        if bot_user_id is not None and getattr(author, "id", None) == bot_user_id
        else "someone"
    )
    return f"(replying to {speaker}: {quoted})"


def command_text(content: str, bot_user_id: int | None = None) -> str:
    """Normalize a message so prefix commands still match after a ping."""
    text = content.replace("！", "!").strip()
    if bot_user_id is not None:
        text = text.replace(f"<@{bot_user_id}>", " ").replace(
            f"<@!{bot_user_id}>", " "
        )
    return " ".join(text.split())


def age_restricted_channel(channel: object) -> bool:
    return bool(getattr(channel, "nsfw", False))


def image_url(attachment: object) -> str | None:
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    if content_type.startswith("image/"):
        url = getattr(attachment, "url", None)
        if isinstance(url, str) and url:
            return url
    return None


class MessageEventGuard:
    """Keep Discord redeliveries from reaching the reply path twice."""

    def __init__(self, *, ttl: float = 900.0) -> None:
        self.ttl = ttl
        self.capacity = 4096
        self._seen: dict[int, float] = {}

    def claim(self, message_id: int, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        expired = [
            event_id
            for event_id, timestamp in self._seen.items()
            if current - timestamp >= self.ttl
        ]
        for event_id in expired:
            self._seen.pop(event_id, None)
        if message_id in self._seen:
            return False
        if len(self._seen) >= self.capacity:
            return False
        self._seen[message_id] = current
        return True


class PersonaBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            intents=intents, allowed_mentions=discord.AllowedMentions.none()
        )
        self.memory = MemoryStore(MEMORY_DB)
        self.provider_http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=4.0),
            trust_env=False, follow_redirects=False,
            limits=httpx.Limits(max_connections=16)
        )
        self.message_events = MessageEventGuard()
        self.conversation_locks: defaultdict[tuple[str, str], asyncio.Lock] = (
            defaultdict(asyncio.Lock)
        )
        self.rate_windows: defaultdict[int, deque[float]] = defaultdict(deque)
        self.command_used: dict[tuple[int, str], float] = {}
        self.music_tracks: dict[int, dict[str, object]] = {}
        self.response_languages: dict[str, str] = {}
        # Legacy global state was writable by arbitrary users: do not migrate it.
        self.selected_persona = "rudeish"
        self.inflight_users: set[int] = set()
        self.handler_count = 0
        self.full_mode_users: set[int] = set()
        self.shutdown_requested = False
        self.active_handlers: set[asyncio.Task[object]] = set()

    def response_language(self, message: object) -> str:
        scope_key = language_scope_key(message)
        cached = self.response_languages.get(scope_key)
        if cached:
            return cached
        language = self.memory.get_setting(
            f"response_language:{scope_key}", "English"
        )
        if len(self.response_languages) >= MAX_TRACKED_USERS:
            self.response_languages.clear()
        self.response_languages[scope_key] = language
        return language

    def set_response_language(self, message: object, language: str) -> None:
        scope_key = language_scope_key(message)
        if len(self.response_languages) >= MAX_TRACKED_USERS:
            self.response_languages.clear()
        self.response_languages[scope_key] = language
        self.memory.set_setting(f"response_language:{scope_key}", language)

    def persona_for(self, channel: object, message: object | None = None) -> str:
        selected = self.selected_persona
        if selected == "explicit":
            selected = "flirty"
        if message is not None:
            selected = self.memory.get_setting(f"persona:{language_scope_key(message)}", "rudeish")
            # Migrate the old persona name in existing persisted settings.
            if selected == "explicit":
                selected = "flirty"
            if not valid_persona(selected):
                selected = "rudeish"
        return selected

    @staticmethod
    def can_manage_settings(message: object) -> bool:
        if getattr(message, "guild", None) is None:
            return True
        permissions = getattr(message.author, "guild_permissions", None)
        return bool(getattr(permissions, "manage_guild", False))

    def full_mode_enabled_for(self, user_id: object) -> bool:
        try:
            parsed = int(user_id)
        except (TypeError, ValueError):
            return False
        if parsed in self.full_mode_users:
            return True
        if self.memory.get_setting(full_mode_setting_key(parsed), "") != "1":
            return False
        self.full_mode_users.add(parsed)
        return True

    def set_full_mode_for(self, user_id: int, enabled: bool) -> None:
        if enabled:
            self.full_mode_users.add(user_id)
            self.memory.set_setting(full_mode_setting_key(user_id), "1")
            return
        self.full_mode_users.discard(user_id)
        self.memory.set_setting(full_mode_setting_key(user_id), "0")

    def full_mode_active(self, message: object) -> bool:
        if not full_mode_location(message):
            return False
        author = getattr(message, "author", None)
        user_id = getattr(author, "id", None)
        return full_mode_allowed(user_id) and self.full_mode_enabled_for(user_id)

    def admit_request(self, user_id: int) -> tuple[bool, int]:
        now = time.monotonic()
        if user_id not in self.rate_windows and len(self.rate_windows) >= MAX_TRACKED_USERS:
            self.rate_windows = defaultdict(deque, {key: value for key, value in self.rate_windows.items() if value and now-value[-1] < RATE_LIMIT_WINDOW})
            if len(self.rate_windows) >= MAX_TRACKED_USERS:
                return False, 60
        window = self.rate_windows[user_id]
        while window and now - window[0] >= RATE_LIMIT_WINDOW:
            window.popleft()
        if len(window) >= RATE_LIMIT_REQUESTS:
            retry_after = max(1, int(RATE_LIMIT_WINDOW - (now - window[0]) + 0.999))
            return False, retry_after
        window.append(now)
        return True, 0

    def admit_command(
        self, user_id: int, command: str, *, now: float | None = None
    ) -> tuple[bool, int]:
        current = time.monotonic() if now is None else now
        key = (user_id, command)
        last = self.command_used.get(key)
        if last is not None:
            elapsed = current - last
            if elapsed < COMMAND_COOLDOWN:
                retry_after = max(1, int(COMMAND_COOLDOWN - elapsed + 0.999))
                return False, retry_after
        if len(self.command_used) >= MAX_TRACKED_USERS:
            self.command_used = {key: value for key, value in self.command_used.items() if current-value < COMMAND_COOLDOWN}
            if len(self.command_used) >= MAX_TRACKED_USERS:
                return False, 25
        self.command_used[key] = current
        return True, 0

    async def on_ready(self) -> None:
        log.info("Logged in as %s; persona=%s", self.user, self.selected_persona)

    async def on_disconnect(self) -> None:
        # discord.py keeps retrying gateway connections by itself.  This event
        # is still useful when Discord has a partial outage, because it makes
        # the connection state visible without stopping message handling.
        if not self.is_closed():
            log.warning("Disconnected from Discord; waiting for gateway recovery")

    async def on_resumed(self) -> None:
        log.info("Discord gateway session resumed")

    async def setup_hook(self) -> None:
        self.maintenance_task = asyncio.create_task(self._maintain_memory())

    async def _maintain_memory(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(self.memory.prune)
            except Exception:
                log.warning("Memory maintenance failed")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        await abandon_music_if_needed(self, member, before, after)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or getattr(message, "webhook_id", None):
            return
        normalized = command_text(message.content, None if self.user is None else self.user.id)
        if self.shutdown_requested:
            return
        # This emergency command must be reachable even from a DM, blocked
        # guild, or a guild where ordinary commands are filtered. It never
        # sends an acknowledgement: once accepted, all outbound work stops.
        if normalized.casefold() == "!shutdown":
            if message.author.id in OWNER_IDS and self.message_events.claim(message.id):
                await self._shutdown()
            return
        unlimited = self.full_mode_active(message)
        music_command = matched_command(normalized) == "!music"
        unrestricted_music = music_command and unrestricted_music_guild(message.guild)
        music_argument = normalized.split(maxsplit=1)[1].strip() if len(normalized.split(maxsplit=1)) == 2 else ""

        def audit_filtered(reason: str) -> None:
            if music_command:
                log_music_command(
                    message, music_argument, outcome="rejected", reason=reason
                )

        personal_erasure = command_text(message.content, None if self.user is None else self.user.id).casefold() == "!memory erase mine"
        if message.author.id in BLOCKED_USERS and not personal_erasure and not unlimited:
            audit_filtered("blocked_user")
            return
        if message.guild is None and not ALLOW_DMS and message.author.id not in OWNER_IDS and not personal_erasure and not unlimited:
            audit_filtered("direct_messages_disabled")
            return
        if message.guild is not None and ALLOWED_GUILDS and message.guild.id not in ALLOWED_GUILDS and not unlimited and not unrestricted_music:
            audit_filtered("guild_not_allowlisted")
            return
        if not unlimited and not unrestricted_music and len(message.content) > MAX_INPUT_CHARS:
            audit_filtered("message_too_long")
            return
        if (not unlimited and message.guild is not None and matched_command(normalized) is None
                and not (full_mode_location(message) and is_full_mode_command(normalized))
                and not (self.user is not None and self.user in message.mentions)):
            return
        # Claim the event before any cooldown, quota, or reply work. Discord can
        # redeliver an event while the first handler is still running; doing
        # this later allows both deliveries to pass the side-effect checks.
        if not self.message_events.claim(message.id):
            audit_filtered("duplicate_message")
            return
        # Bound complete event handlers, including outbound Discord API waits.
        count = getattr(self, "handler_count", 0)
        if not unlimited and not unrestricted_music and count >= MAX_HANDLERS:
            audit_filtered("handler_capacity")
            return
        self.handler_count = count + 1
        current_task = asyncio.current_task()
        active_handlers = getattr(self, "active_handlers", None)
        if active_handlers is None:
            self.active_handlers = active_handlers = set()
        if current_task is not None:
            active_handlers.add(current_task)
        try:
            if unlimited or unrestricted_music:
                await self._handle_message(message)
            else:
                await asyncio.wait_for(self._handle_message(message), timeout=HANDLER_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("Message handler exceeded deadline")
            audit_filtered("handler_timeout")
        finally:
            if current_task is not None:
                active_handlers.discard(current_task)
            self.handler_count -= 1

    async def _shutdown(self) -> None:
        """Stop processing and close every resource without sending a reply."""
        if self.shutdown_requested:
            return
        self.shutdown_requested = True
        current_task = asyncio.current_task()
        for task in tuple(getattr(self, "active_handlers", ())):
            if task is not current_task and not task.done():
                task.cancel()
        await self.close()

    async def _handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        text = command_text(
            message.content, None if self.user is None else self.user.id
        )
        parts = text.split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) == 2 else ""
        command = matched_command(text)
        if (
            command is None
            and full_mode_location(message)
            and is_full_mode_command(text)
        ):
            command = "!full"
        unlimited = self.full_mode_active(message)
        unrestricted_music = (
            command == "!music" and unrestricted_music_guild(message.guild)
        )
        if command is not None:
            if not unlimited and not unrestricted_music:
                admitted, retry_after = self.admit_command(message.author.id, command)
                if not admitted:
                    await self._reply(message, f"slow down try again in {retry_after}s")
                    if command == "!music":
                        parts_for_audit = text.split(maxsplit=1)
                        log_music_command(
                            message,
                            parts_for_audit[1].strip() if len(parts_for_audit) == 2 else "",
                            outcome="rejected",
                            reason="command_cooldown",
                            response=f"slow down try again in {retry_after}s",
                        )
                    return
        if name == "!security":
            if message.author.id not in OWNER_IDS:
                return
            if argument.casefold() in {"pause", "resume"}:
                self.memory.set_setting("api_paused", "1" if argument.casefold() == "pause" else "0")
            await self._reply(message, self.memory.api_status() + "; paused=" + self.memory.get_setting("api_paused", "0"))
            return
        if name == "!help":
            await self._reply(message, HELP_TEXT)
            return
        if is_owner_note_command(text):
            await self._reply(message, OWNER_NOTE_TEXT)
            return
        if command == "!full":
            await self._reply(message, self._full_mode_command(message, argument))
            return
        if name == "!persona":
            await self._reply(message, self._persona_command(message, argument))
            return
        if name == "!language":
            await self._reply(
                message, await self._language_command(message, argument)
            )
            return
        if name == "!music":
            await self._reply(
                message, await handle_music_command(self, message, argument)
            )
            return
        if name == "!memory":
            await self._reply(message, await self._memory_command(message, argument))
            return

        is_dm = message.guild is None
        mentioned = self.user is not None and self.user in message.mentions
        if not (is_dm or mentioned or unlimited):
            return

        prompt = message.content
        if self.user is not None:
            prompt = (
                prompt.replace(f"<@{self.user.id}>", "")
                .replace(f"<@!{self.user.id}>", "")
                .strip()
            )
        prompt = (prompt if unlimited else sanitize_user_text(prompt)).strip()
        full_mode = unlimited
        attachment_limit = None if full_mode else MAX_ATTACHMENTS
        image_urls = [
            url
            for attachment in message.attachments[:attachment_limit]
            if (url := image_url(attachment))
        ]
        relaxed_guardrails = full_mode
        decode_now = not relaxed_guardrails and looks_like_decode_request(prompt)
        repeat_now = not relaxed_guardrails and looks_like_repeat_request(prompt)
        use_history = full_mode
        if decode_now or repeat_now:
            image_urls = []
        elif prompt or image_urls:
            quoted = referenced_message_context(
                message,
                None if self.user is None else self.user.id,
                unbounded=full_mode,
            )
            if quoted:
                prompt = f"{quoted}\n{prompt}".strip()
                use_history = True
        if not prompt and not image_urls:
            # A plain mention should be cheap and reliable: it must not wait
            # for an AI provider while Discord is recovering from an outage.
            if mentioned:
                await self._reply(message, PING_RESPONSE)
            return

        if not full_mode:
            admitted, retry_after = self.admit_request(message.author.id)
            if not admitted:
                await self._reply(message, f"slow down try again in {retry_after}s")
                return
        scope_id = str(message.channel.id)
        user_id = str(message.author.id)
        inflight = getattr(self, "inflight_users", None)
        if inflight is None:
            self.inflight_users = inflight = set()
        occupies_slot = not full_mode
        if occupies_slot:
            if message.author.id in inflight or len(inflight) >= MAX_INFLIGHT:
                return
            inflight.add(message.author.id)
        try:
            async with message.channel.typing():
                request = ask(
                        self.provider_http,
                        self.memory,
                        event_id=str(message.id),
                        scope_id=scope_id,
                        user_id=user_id,
                        server_id=(
                            str(message.guild.id)
                            if message.guild is not None
                            else ""
                        ),
                        prompt=prompt,
                        image_urls=image_urls,
                        persona=self.persona_for(message.channel, message),
                        language=self.response_language(message),
                        created_at=message.created_at.timestamp(),
                        full_mode=full_mode,
                        use_history=use_history,
                        relaxed_guardrails=relaxed_guardrails,
                    )
                answer = await request if full_mode else await asyncio.wait_for(request, timeout=ASK_TIMEOUT)
            if not answer:
                return
            await self._reply(message, answer, unlimited=full_mode)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("AI reply failed in channel %s", message.channel.id)
            await self._reply(message, "I couldn't reach the AI provider just now.")
        finally:
            if occupies_slot:
                inflight.discard(message.author.id)

    def _full_mode_command(self, message: discord.Message, requested: str) -> str:
        user_id = message.author.id
        if full_mode_blocked(user_id) or not full_mode_can_enable(user_id):
            return "you can't use this"
        text = " ".join(requested.casefold().split())
        if text in {"", "mode"}:
            return (
                "full mode on"
                if self.full_mode_enabled_for(user_id)
                else "full mode off"
            )
        if text == "mode on":
            problem = host_model_error("gpt")
            if problem is not None:
                return problem
            self.set_full_mode_for(user_id, True)
            return "full mode on"
        if text == "mode off":
            self.set_full_mode_for(user_id, False)
            return "full mode off"
        return FULL_MODE_USAGE

    def _persona_command(self, message: discord.Message, requested: str) -> str:
        if not requested.strip():
            current = self.persona_for(message.channel, message)
            return f"persona: {persona_label(current)}"
        if not self.can_manage_settings(message):
            return "you need the Manage Server permission to change server settings"
        persona, error = parse_persona_argument(requested)
        if error is not None:
            return error
        assert persona is not None
        problem = host_model_error(persona_provider(persona))
        if problem is not None:
            return problem
        self.memory.set_setting(f"persona:{language_scope_key(message)}", persona)
        return f"persona: {persona_label(persona)}"

    async def _bot_member(self, guild: discord.Guild) -> object | None:
        member = getattr(guild, "me", None)
        if member is not None:
            return member
        user = self.user
        if user is None:
            return None
        getter = getattr(guild, "get_member", None)
        if callable(getter):
            member = getter(user.id)
            if member is not None:
                return member
        fetcher = getattr(guild, "fetch_member", None)
        if not callable(fetcher):
            return None
        try:
            return await fetcher(user.id)
        except Exception:
            log.exception(
                "Could not fetch the bot member; guild=%s", getattr(guild, "id", "")
            )
            return None

    async def _try_member_edit(self, member: object, **fields: object) -> bool:
        if not fields:
            return False
        edit = getattr(member, "edit", None)
        if not callable(edit):
            return False
        try:
            await edit(**fields)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "Could not update this server’s profile (%s); guild=%s: %s",
                ", ".join(fields),
                getattr(getattr(member, "guild", None), "id", ""),
                exc,
            )
            return False

    async def _apply_guild_language_profile(
        self, guild: discord.Guild, language: str
    ) -> None:
        member = await self._bot_member(guild)
        if member is None:
            log.warning(
                "Cannot update this server’s profile picture; guild=%s",
                getattr(guild, "id", ""),
            )
            return
        avatar_fields = profile_asset_payload("avatar", language_avatar_path(language))
        banner_fields = profile_asset_payload("banner", language_banner_path(language))
        avatar_ok = await self._try_member_edit(member, **avatar_fields)
        banner_ok = await self._try_member_edit(member, **banner_fields)
        log.info(
            "Updated guild profile; guild=%s language=%s avatar=%s banner=%s",
            getattr(guild, "id", ""),
            language,
            avatar_ok and avatar_fields.get("avatar") is not None,
            banner_ok and banner_fields.get("banner") is not None,
        )

    async def _clear_guild_language_profile(self, guild: discord.Guild) -> None:
        member = await self._bot_member(guild)
        if member is None:
            log.warning(
                "Cannot reset this server’s profile picture; guild=%s",
                getattr(guild, "id", ""),
            )
            return
        avatar_ok = await self._try_member_edit(member, avatar=None)
        banner_ok = await self._try_member_edit(member, banner=None)
        log.info(
            "Reset guild profile; guild=%s avatar=%s banner=%s",
            getattr(guild, "id", ""),
            avatar_ok,
            banner_ok,
        )

    async def _language_command(self, message: discord.Message, argument: str) -> str:
        if not argument:
            return f"language: {self.response_language(message)}"
        if not self.can_manage_settings(message):
            return "you need the Manage Server permission to change server settings"
        if argument.casefold() == "reset":
            return await self._reset_language(message)
        language, error = parse_language_name(argument)
        if error is not None:
            return error
        assert language is not None
        self.set_response_language(message, language)
        if message.guild is not None:
            try:
                await asyncio.wait_for(
                    self._apply_guild_language_profile(message.guild, language),
                    timeout=PROFILE_UPDATE_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Profile update failed; guild=%s language=%s",
                    message.guild.id,
                    language,
                )
        if message.guild is None:
            return f"language set to {language}; I’ll reply in it from now on"
        return (
            f"language set to {language}; I’ll reply in it in this server from now on"
        )

    async def _reset_language(self, message: discord.Message) -> str:
        if not self.can_manage_settings(message):
            return "you need the Manage Server permission to change server settings"
        self.set_response_language(message, "English")
        if message.guild is None:
            return "language reset to English; I’ll reply in it from now on"
        try:
            await asyncio.wait_for(
                self._clear_guild_language_profile(message.guild),
                timeout=PROFILE_UPDATE_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Profile reset failed; guild=%s", message.guild.id)
        return (
            "language reset to English; this server’s profile picture and banner "
            "are restored"
        )

    async def _memory_command(self, message: discord.Message, argument: str) -> str:
        if argument.casefold() == "erase mine":
            await asyncio.to_thread(self.memory.erase_user_memory, str(message.author.id))
            return "your conversation memory has been erased; usage counters remain"
        if message.guild is None:
            return "!memory erase only works in a server"
        if argument.casefold() != "erase":
            return "usage: !memory erase"
        if not message.author.guild_permissions.manage_guild:
            return "you need the Manage Server permission to erase server memory"
        removed = await asyncio.to_thread(
            self.memory.erase_server_memory, str(message.guild.id)
        )
        log.info(
            "Erased server memory; server=%s records=%s", message.guild.id, removed
        )
        return "server memory fully erased for every user and channel"

    async def _reply(
        self, message: discord.Message, content: str, *, unlimited: bool = False
    ) -> None:
        if self.shutdown_requested:
            return
        text = content if unlimited else content[:MAX_REPLY_CHARS]
        chunks = split_reply(text)
        for index, chunk in enumerate(chunks):
            if self.shutdown_requested:
                return
            try:
                await message.channel.send(
                    chunk,
                    reference=message if index == 0 else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,
                )
            except (discord.HTTPException, discord.Forbidden):
                log.exception("Could not send a Discord reply")
                return
        for index, image in enumerate(getattr(content, "image_bytes", ())):
            if self.shutdown_requested:
                return
            try:
                await message.channel.send(
                    "",
                    reference=message if not chunks and index == 0 else None,
                    allowed_mentions=discord.AllowedMentions.none(),
                    file=discord.File(io.BytesIO(image), filename=f"owaua-{index + 1}.png"),
                )
            except (discord.HTTPException, discord.Forbidden):
                log.exception("Could not send a generated image")
                return

    async def close(self) -> None:
        current_task = asyncio.current_task()
        for task in tuple(getattr(self, "active_handlers", ())):
            if task is not current_task and not task.done():
                task.cancel()
        task = getattr(self, "maintenance_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for voice in self.voice_clients:
            voice.stop()
            await voice.disconnect(force=True)
        await self.provider_http.aclose()
        await super().close()


def discord_retry_delay(failures: int) -> float:
    """Return a capped exponential delay for recreating a failed client."""
    if failures < 1:
        return 0.0
    exponent = min(failures - 1, 6)
    return min(
        DISCORD_RETRY_INITIAL_DELAY * (2 ** exponent), DISCORD_RETRY_MAX_DELAY
    )


async def start_discord_with_retries(
    token: str,
    *,
    bot_factory: Callable[[], PersonaBot] = PersonaBot,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """Run Discord's built-in reconnect loop and recreate it if it exits.

    discord.py handles ordinary websocket reconnects when ``reconnect=True``.
    This outer loop covers failures that escape that loop, such as a temporary
    gateway or network failure while starting.  Login failures are permanent
    configuration errors and intentionally fail fast instead of retrying.
    """
    failures = 0
    recoverable = (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        discord.ConnectionClosed,
        discord.GatewayNotFound,
        discord.HTTPException,
        OSError,
    )
    while True:
        bot = bot_factory()
        retry_delay: float | None = None
        try:
            await bot.start(token, reconnect=True)
            return
        except asyncio.CancelledError:
            raise
        except discord.LoginFailure:
            raise
        except recoverable as exc:
            failures += 1
            retry_delay = discord_retry_delay(failures)
            log.warning(
                "Discord client stopped (%s); recreating it in %.0fs (attempt %s)",
                type(exc).__name__,
                retry_delay,
                failures,
            )
        finally:
            try:
                await bot.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Failed while closing the Discord client")
        if getattr(bot, "shutdown_requested", False):
            return
        if retry_delay is not None:
            await sleep(retry_delay)


async def main() -> None:
    audit_path = Path(os.getenv("MUSIC_AUDIT_LOG", "data/music-audit.jsonl"))
    if not audit_path.is_absolute():
        audit_path = ROOT / audit_path
    # Initialize the audit destination before credential checks, so operators
    # can verify its location even when startup fails configuration validation.
    configure_music_audit_log(audit_path)
    log.info("Cloudflare protection: %s", describe_protection())
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing; copy .env.example to .env and fill it in"
        )
    if not PERPLEXITY_API_KEY:
        raise RuntimeError(
            "PERPLEXITY_API_KEY is missing; copy .env.example to .env and fill it in"
        )
    await start_discord_with_retries(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
