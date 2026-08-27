"""Multilingual routing and reply-language preference.

Uses a lightweight language detector (langdetect — never the LLM, to save
cost) to spot non-English messages, and only when the message is in a
designated multilingual channel (``SEFBOT_MULTILINGUAL_CHANNELS``) routes the
reply to Groq GPT-OSS 20B, which answers back in the user's language.

`!language` / `/language` stores a per-user preference (and an optional
server default). The brain is then told to write the visible reply in that
language.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sefbot import ai_control, config, db
from sefbot.services.llm_client import LLMError, llm

log = logging.getLogger("sefbot.multilingual")

try:
    import langdetect
    from langdetect import detect as _detect_raw

    langdetect.DetectorFactory.seed = 0
    _detect = _detect_raw
except Exception:
    _detect = None

_cache: dict[str, Optional[str]] = {}
_CACHE_MAX = 2048


async def detect_lang(text: str) -> Optional[str]:
    """Detect a message's ISO 639-1 language code (cached, runs off-loop)."""
    if _detect is None:
        return None
    key = (text or "").strip().lower()
    if not key or len(key) < 4:
        return "en"
    hit = _cache.get(key)
    if hit is not None:
        return hit
    loop = asyncio.get_running_loop()
    try:
        lang = await loop.run_in_executor(None, _detect, key)
    except Exception:
        return None
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = lang
    return lang


async def translate_text(
    text: str,
    target_lang: str,
    *,
    scope_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Translate text with the fast model before it hits the brain."""
    system = (
        f"You are a translation assistant. Translate the following text to "
        f"{target_lang} while preserving meaning and tone."
    )
    try:
        from sefbot import ai

        return await ai.chat(
            system,
            [{"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=500,
            tier="fast",
            task="workflow",
            scope_id=scope_id,
            user_id=user_id,
            prompt_version="translation-v1",
        )
    except Exception:
        return text


async def maybe_multilingual_reply(
    channel,
    guild,
    text: str,
    lang: Optional[str],
    *,
    scope_id: str | None = None,
    user_id: str | None = None,
) -> Optional[str]:
    """Return a Groq GPT-OSS 20B reply in the message's language, or None.

    Only fires inside a designated multilingual channel for non-English text.
    """
    if not lang or lang == "en":
        return None
    if guild is None or channel is None:
        return None
    if str(channel.id) not in config.MULTILINGUAL_CHANNELS:
        return None
    if not config.GROQ_API_KEY:
        return None
    system = (
        "You are a friendly multilingual Discord assistant. Reply in the SAME "
        "language the user wrote in. Keep it natural, concise and helpful. "
        "No disclaimers, no 'as an AI' talk, no emoji."
    )
    try:
        return await llm.chat(
            config.MULTILINGUAL_MODEL,
            [{"role": "user", "content": text[:1500]}],
            system=system,
            temperature=0.6,
            max_tokens=600,
            base_url=config.GROQ_BASE_URL,
            api_key=config.GROQ_API_KEY,
            task="workflow",
            scope_id=scope_id,
            user_id=user_id,
        )
    except (LLMError, ai_control.AIBudgetExceeded) as e:
        log.warning("multilingual reply failed: %s", e)
        return None


USER_FLAG = "language"
RESET_TOKENS = frozenset({
    "reset", "clear", "default", "auto", "none", "off", "unset", "remove",
})
STATUS_TOKENS = frozenset({"", "status", "show", "help", "?", "what", "current"})
LIST_TOKENS = frozenset({"list", "langs", "languages", "all", "catalog"})
SERVER_TOKENS = frozenset({"server", "guild", "here"})
_LEAD = re.compile(
    r"(?is)^(please|set|to|in|speak|talk|reply|respond|write|use|switch)\s+"
)
_CUSTOM_OK = re.compile(r"^[\w][\w \-']{1,39}$", re.UNICODE)
_ENGLISH_CODES = frozenset({"en", "eng", "english"})


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    native: str = ""

    @property
    def label(self) -> str:
        native = (self.native or "").strip()
        if native and native.casefold() != self.name.casefold():
            return f"{self.name} ({native})"
        return self.name


# code, English name, native name, extra aliases
_CATALOG_ROWS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("en", "English", "English", ("eng", "en-us", "en-gb", "american", "british")),
    ("es", "Spanish", "español", ("espanol", "castilian", "castellano")),
    ("fr", "French", "français", ("francais",)),
    ("de", "German", "Deutsch", ("deutsch",)),
    ("it", "Italian", "italiano", ()),
    ("pt", "Portuguese", "português", ("portugues", "brazilian", "pt-br", "pt-pt")),
    ("ru", "Russian", "русский", ("russkiy",)),
    ("ja", "Japanese", "日本語", ("jp", "nihongo")),
    ("ko", "Korean", "한국어", ("kr", "hangul")),
    ("zh", "Chinese", "中文", ("mandarin", "chinese", "zh-cn", "zh-tw", "cantonese")),
    ("ar", "Arabic", "العربية", ("arab",)),
    ("he", "Hebrew", "עברית", ("iw", "ivrit", "ivri", "עברית")),
    ("yi", "Yiddish", "ייִדיש", ("yidish",)),
    ("hi", "Hindi", "हिन्दी", ()),
    ("tr", "Turkish", "Türkçe", ("turkce",)),
    ("nl", "Dutch", "Nederlands", ("nederlands", "flemish")),
    ("pl", "Polish", "polski", ()),
    ("uk", "Ukrainian", "українська", ("ua",)),
    ("sv", "Swedish", "svenska", ()),
    ("no", "Norwegian", "norsk", ("nb", "nn", "norsk")),
    ("da", "Danish", "dansk", ()),
    ("fi", "Finnish", "suomi", ("suomi",)),
    ("el", "Greek", "ελληνικά", ("ellinika",)),
    ("th", "Thai", "ไทย", ()),
    ("vi", "Vietnamese", "Tiếng Việt", ("tieng viet",)),
    ("id", "Indonesian", "Bahasa Indonesia", ("bahasa",)),
    ("tl", "Tagalog", "Tagalog", ("filipino", "fil")),
    ("fa", "Persian", "فارسی", ("farsi",)),
    ("ur", "Urdu", "اردو", ()),
    ("bn", "Bengali", "বাংলা", ("bangla",)),
    ("cs", "Czech", "čeština", ("cestina",)),
    ("ro", "Romanian", "română", ("romana",)),
    ("hu", "Hungarian", "magyar", ("magyar",)),
    ("la", "Latin", "Latina", ()),
    ("eo", "Esperanto", "Esperanto", ()),
    ("sw", "Swahili", "Kiswahili", ("kiswahili",)),
)

_BY_CODE: dict[str, Language] = {}
_BY_ALIAS: dict[str, Language] = {}
LANGUAGES: List[Language] = []


def _index_catalog() -> None:
    LANGUAGES.clear()
    _BY_CODE.clear()
    _BY_ALIAS.clear()
    for code, name, native, extra in _CATALOG_ROWS:
        lang = Language(code=code, name=name, native=native)
        LANGUAGES.append(lang)
        _BY_CODE[code] = lang
        aliases = {code, name.casefold(), native.casefold(), *extra}
        for alias in aliases:
            key = " ".join(str(alias).casefold().split())
            if key:
                _BY_ALIAS.setdefault(key, lang)


_index_catalog()


def _strip_filler(raw: str) -> str:
    text = " ".join((raw or "").split())
    while text:
        nxt = _LEAD.sub("", text, count=1).strip()
        if nxt == text:
            break
        text = nxt
    return text


def resolve(raw: str) -> Optional[Language]:
    """Map a user-typed name or ISO code onto a known language."""
    key = " ".join(_strip_filler(raw).casefold().split())
    if not key:
        return None
    return _BY_ALIAS.get(key) or _BY_CODE.get(key)


def coerce(raw: str) -> Optional[Language]:
    """Resolve a known language, or accept a short free-form language name."""
    known = resolve(raw)
    if known:
        return known
    text = " ".join(_strip_filler(raw).split())
    if not text or not _CUSTOM_OK.fullmatch(text):
        return None
    if len(text.split()) > 3:
        return None
    title = text if any(ch.isupper() for ch in text[1:]) else text.title()
    return Language(code=text.casefold(), name=title, native=title)


def get(code: str) -> Optional[Language]:
    if not code:
        return None
    return _BY_CODE.get(code) or coerce(code)


def is_english(lang: Optional[Language]) -> bool:
    return lang is None or lang.code in _ENGLISH_CODES or lang.name.casefold() == "english"


def user_language(user_id: str) -> Optional[Language]:
    return get(db.user_flag_get(str(user_id), USER_FLAG) or "")


def guild_language(guild_id: str) -> Optional[Language]:
    raw = (db.guild_settings(str(guild_id)).get("language") or "").strip()
    return get(raw) if raw else None


def effective_language(user_id: str, guild_id: str) -> Optional[Language]:
    """Personal preference wins; otherwise the server default; else English."""
    personal = user_language(user_id)
    if personal:
        return personal
    return guild_language(guild_id)


def set_user_language(user_id: str, lang: Optional[Language]) -> None:
    db.user_flag_set(str(user_id), USER_FLAG, lang.code if lang else "")


def set_guild_language(guild_id: str, lang: Optional[Language]) -> None:
    db.guild_settings_set(str(guild_id), language=lang.code if lang else "")


def reply_instruction(user_id: str, guild_id: str) -> str:
    """System-prompt block forcing the visible reply into the chosen language."""
    lang = effective_language(user_id, guild_id)
    if is_english(lang):
        return ""
    label = lang.label
    return (
        f"REPLY LANGUAGE — HARD CONSTRAINT from !language / /language: write the "
        f"entire user-visible reply (the JSON \"response\" text, or the plain-text "
        f"answer) in {label}. Keep persona, tone, and length. JSON keys, memory "
        f"contents, relationship fields, and other structured fields stay in "
        f"English. Do not switch to English unless the user asked for English "
        f"this turn. Owner standing orders still override this if they conflict."
    )


def apply_to_system(system: str, user_id: str, guild_id: str) -> str:
    extra = reply_instruction(user_id, guild_id)
    if not extra:
        return system or ""
    return extra + "\n\n" + (system or "")


def catalog_text() -> str:
    lines = [f"`{lang.code}` — {lang.label}" for lang in LANGUAGES]
    return (
        "languages i know by name/code (anything else is stored as a custom name):\n"
        + "\n".join(lines)
    )


def status_text(user_id: str, guild_id: str, prefix: str = "!") -> str:
    p = prefix or "!"
    personal = user_language(user_id)
    server = guild_language(guild_id)
    effective = effective_language(user_id, guild_id)
    if is_english(effective):
        current = "English (default)"
    else:
        current = effective.label
    bits = [f"i'll reply in **{current}**."]
    if personal:
        bits.append(f"your setting: {personal.label}")
    else:
        bits.append("you have no personal override")
    if server:
        bits.append(f"server default: {server.label}")
    else:
        bits.append("no server default")
    bits.append(
        f"\n`{p}language <name>` — set yours (e.g. `{p}language hebrew`)\n"
        f"`{p}language reset` — clear yours (falls back to server/default)\n"
        f"`{p}language list` — names i recognize\n"
        f"`{p}language server <name>` — manage-server sets the default for everyone "
        f"who hasn't picked their own"
    )
    return "\n".join(bits)


def _unknown_message(prefix: str) -> str:
    p = prefix or "!"
    return (
        f"i don't know that language. try a name or ISO code like `hebrew`, "
        f"`spanish`, `ja`. `{p}language list` shows the catalog; other short "
        f"names (klingon, latin, …) are stored as custom."
    )


def set_from_text(raw: str) -> Tuple[Optional[Language], str]:
    """Parse a language argument. Empty error string means success."""
    lang = coerce(raw)
    if lang is None:
        return None, _unknown_message(config.PREFIX)
    return lang, ""


def parse_arg(arg: str) -> Tuple[str, str]:
    """Split `!language` args into (op, rest).

    ops: status, list, reset, set, server_set, server_reset, help
    """
    text = _strip_filler(arg or "").strip()
    if not text:
        return "status", ""
    parts = text.split(maxsplit=1)
    head = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if head in STATUS_TOKENS:
        return "status", ""
    if head in LIST_TOKENS:
        return "list", ""
    if head in RESET_TOKENS and not rest:
        return "reset", ""
    if head in SERVER_TOKENS:
        if not rest or rest.lower() in RESET_TOKENS or rest.lower() in STATUS_TOKENS:
            if not rest or rest.lower() in STATUS_TOKENS:
                return "server_status", ""
            return "server_reset", ""
        return "server_set", rest
    return "set", text
