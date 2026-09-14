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

import discord
import httpx
from dotenv import load_dotenv
from PIL import Image

from ask import (
    HOST_DEFAULT_MODELS,
    MAX_ATTACHMENTS,
    PERSONAS,
    ask,
    host_default_model,
    host_default_persona,
    host_model_error,
    looks_like_decode_request,
    looks_like_repeat_request,
    persona_label,
    sanitize_user_text,
    valid_persona,
)
from memory import MemoryStore
from music import handle_music_command

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("owaua")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip().replace("\\_", "_")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
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
    }
)
DISCORD_MESSAGE_LIMIT = 1900
ASK_TIMEOUT = 40.0

HELP_TEXT = """**Owaua commands**
`!help` — show this command list
`!owner's note` — a note from the bot's owner
`!persona rudeish|nerdish|explicit|host default gpt/deepseek/mistral` — view or switch persona (explicit: age-restricted channels only)
`!language <full name>|reset` — this server's reply language (and matching picture and banner); reset restores English and the original look
`!music help` — play a song in your voice channel
`!memory erase` — erase server memory (Manage Server required)

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


PERSONA_USAGE = (
    "usage: !persona rudeish, !persona nerdish, !persona explicit, "
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
        with Image.open(io.BytesIO(data)) as image:
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
        with Image.open(io.BytesIO(data)) as image:
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
            timeout=httpx.Timeout(60.0, connect=4.0)
        )
        self.message_events = MessageEventGuard()
        self.conversation_locks: defaultdict[tuple[str, str], asyncio.Lock] = (
            defaultdict(asyncio.Lock)
        )
        self.rate_windows: defaultdict[int, deque[float]] = defaultdict(deque)
        self.command_used: dict[tuple[int, str], float] = {}
        self.music_tracks: dict[int, dict[str, object]] = {}
        self.response_languages: dict[str, str] = {}
        saved = self.memory.get_setting("selected_persona", "")
        if not valid_persona(saved):
            legacy = self.memory.get_setting("selected_persona_model", "")
            saved = {
                "gpt": "rudeish",
                "deepseek": "nerdish",
                "mistral": "explicit",
            }.get(legacy, "rudeish")
        self.selected_persona = saved

    def response_language(self, message: object) -> str:
        scope_key = language_scope_key(message)
        cached = self.response_languages.get(scope_key)
        if cached:
            return cached
        language = self.memory.get_setting(
            f"response_language:{scope_key}", "English"
        )
        self.response_languages[scope_key] = language
        return language

    def set_response_language(self, message: object, language: str) -> None:
        scope_key = language_scope_key(message)
        self.response_languages[scope_key] = language
        self.memory.set_setting(f"response_language:{scope_key}", language)

    def persona_for(self, channel: object) -> str:
        if self.selected_persona != "explicit" or age_restricted_channel(channel):
            return self.selected_persona
        return "rudeish"

    def admit_request(self, user_id: int) -> tuple[bool, int]:
        if user_id in COOLDOWN_EXEMPT_USER_IDS:
            return True, 0
        now = time.monotonic()
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
        if user_id in COOLDOWN_EXEMPT_USER_IDS:
            return True, 0
        current = time.monotonic() if now is None else now
        key = (user_id, command)
        last = self.command_used.get(key)
        if last is not None:
            elapsed = current - last
            if elapsed < COMMAND_COOLDOWN:
                retry_after = max(1, int(COMMAND_COOLDOWN - elapsed + 0.999))
                return False, retry_after
        self.command_used[key] = current
        return True, 0

    async def on_ready(self) -> None:
        log.info("Logged in as %s; persona=%s", self.user, self.selected_persona)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self.message_events.claim(message.id):
            log.info("Ignoring redelivered Discord event %s", message.id)
            return

        text = command_text(
            message.content, None if self.user is None else self.user.id
        )
        parts = text.split(maxsplit=1)
        name = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) == 2 else ""
        command = matched_command(text)
        if command is not None:
            admitted, retry_after = self.admit_command(message.author.id, command)
            if not admitted:
                await self._reply(message, f"slow down try again in {retry_after}s")
                return

        if name == "!help":
            await self._reply(message, HELP_TEXT)
            return
        if is_owner_note_command(text):
            await self._reply(message, OWNER_NOTE_TEXT)
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
        if not (is_dm or mentioned):
            return

        prompt = message.content
        if self.user is not None:
            prompt = (
                prompt.replace(f"<@{self.user.id}>", "")
                .replace(f"<@!{self.user.id}>", "")
                .strip()
            )
        prompt = sanitize_user_text(prompt).strip()
        image_urls = [
            url
            for attachment in message.attachments[:MAX_ATTACHMENTS]
            if (url := image_url(attachment))
        ]
        if looks_like_decode_request(prompt) or looks_like_repeat_request(prompt):
            image_urls = []
        if not prompt and not image_urls:
            return

        admitted, retry_after = self.admit_request(message.author.id)
        if not admitted:
            await self._reply(message, f"slow down try again in {retry_after}s")
            return

        scope_id = str(message.channel.id)
        user_id = str(message.author.id)
        key = (scope_id, user_id)
        try:
            async with self.conversation_locks[key]:
                async with message.channel.typing():
                    answer = await asyncio.wait_for(
                        ask(
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
                            persona=self.persona_for(message.channel),
                            language=self.response_language(message),
                            created_at=message.created_at.timestamp(),
                        ),
                        timeout=ASK_TIMEOUT,
                    )
            if not answer:
                return
            await self._reply(message, answer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("AI reply failed in channel %s", message.channel.id)
            await self._reply(message, "I couldn't reach the AI provider just now.")

    def _persona_command(self, message: discord.Message, requested: str) -> str:
        if not requested.strip():
            current = self.persona_for(message.channel)
            if self.selected_persona == "explicit" and current != "explicit":
                return (
                    f"persona: {persona_label(current)} "
                    "(explicit only works in age-restricted channels)"
                )
            return f"persona: {persona_label(current)}"
        persona, error = parse_persona_argument(requested)
        if error is not None:
            return error
        assert persona is not None
        if persona == "explicit" and not age_restricted_channel(message.channel):
            return "explicit only works in age-restricted channels"
        alias = host_default_model(persona)
        if alias is not None:
            problem = host_model_error(alias)
            if problem is not None:
                return problem
        self.selected_persona = persona
        self.memory.set_setting("selected_persona", persona)
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

    async def _reply(self, message: discord.Message, content: str) -> None:
        chunks = split_reply(content)
        for index, chunk in enumerate(chunks):
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

    async def close(self) -> None:
        await self.provider_http.aclose()
        await super().close()


async def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing; copy .env.example to .env and fill it in"
        )
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is missing; copy .env.example to .env and fill it in"
        )
    bot = PersonaBot()
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
