"""Deterministic, local profanity counting for the optional swear jar."""

from __future__ import annotations

import re
from typing import Final

# Keep this deliberately narrower than moderation. The swear jar is a playful
# counter, not a safety classifier, and it must not treat threats or protected
# slurs as profanity. Explicit word boundaries prevent false positives such as
# ``assignment`` and ``cockpit`` while allowing common inflected forms and
# harmless obfuscation such as ``F.U.C.K`` or ``fuuuck``.
_SWEAR_WORDS: Final = (
    "arse",
    "arsehole",
    "ass",
    "asshat",
    "asshole",
    "asswipe",
    "badass",
    "ballsack",
    "bastard",
    "bastards",
    "bellend",
    "bitch",
    "bloody",
    "bollock",
    "bollocks",
    "boob",
    "boobs",
    "bugger",
    "bullshit",
    "cock",
    "crap",
    "crappy",
    "cunt",
    "damn",
    "dammit",
    "dick",
    "dickhead",
    "dickheads",
    "dickweed",
    "dickwad",
    "dipshit",
    "douche",
    "douchebag",
    "dumbass",
    "effing",
    "fkn",
    "flog",
    "fuck",
    "fucked",
    "fucker",
    "fucking",
    "fuckface",
    "fuckhead",
    "fuckwit",
    "frigging",
    "fricking",
    "goddamn",
    "goddammit",
    "git",
    "hell",
    "jackass",
    "knob",
    "knobhead",
    "motherfucker",
    "motherfuckers",
    "motherfucking",
    "nonce",
    "pillock",
    "piss",
    "pissed",
    "prick",
    "pussy",
    "screw",
    "shit",
    "shitface",
    "shithole",
    "shithead",
    "shitty",
    "slut",
    "sod",
    "stfu",
    "suck",
    "sucks",
    "tosser",
    "twat",
    "turd",
    "tit",
    "tits",
    "wank",
    "wanker",
    "whore",
    "wtf",
)
_SWEAR_PATTERN: Final = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(
        sorted(
            ("[^a-z0-9]*".join(f"{re.escape(char)}+" for char in word) for word in _SWEAR_WORDS),
            key=len,
            reverse=True,
        )
    )
    + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def count_swears(content: str) -> int:
    """Count swear occurrences without retaining or returning message text."""
    if not content:
        return 0
    return sum(1 for _match in _SWEAR_PATTERN.finditer(str(content)[:4_000]))
