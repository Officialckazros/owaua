"""owaua Terms of Service — acceptance gate + violation detection.

Canonical page:
  https://wearegays.net/owaua/terms

Users must open a short-lived Discord-bound link, read the public page, and
accept the current version there before normal bot use.
Clear ToS violations warn first; after TOS_STRIKE_LIMIT strikes the user is
hard-blocked via blocked.py.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
import typing
from typing import Optional, Tuple
from urllib.parse import quote

import discord

from owaua import config, db
from owaua.legal import LEGAL_VERSION, PRIVACY_URL, TERMS_URL

TOS_VERSION = LEGAL_VERSION
TOS_URL = TERMS_URL
TOS_ACCEPT_URL = f"{TERMS_URL}/accept"

TOS_STRIKE_LIMIT = 3
_LEAK_STRIKE_LIMIT = 3
_ACCEPTANCE_CHALLENGE_SECONDS = 15 * 60
_NETWORK_RETENTION_SECONDS = 30 * 86_400
_RATE_WINDOW_SEC = 15.0
_RATE_MAX = 5
_HAMMER_WINDOW_SEC = 60.0
_HAMMER_LIMIT = 5
_QUARANTINE_SECONDS = 300.0
_QUARANTINE_STRIKE_LIMIT = 3
_rate_buckets: dict[str, collections.deque[float]] = {}
_hammer_buckets: dict[str, collections.deque[float]] = {}
_quarantine_until: dict[str, float] = {}
_rate_lock = threading.Lock()

TOS_ALLOWED_COMMANDS = frozenset(
    {
        "tos",
        "terms",
        "termsofservice",
        "privacy",
        "privacypolicy",
        "pp",
        "help",
        "about",
        "dmblock",
        "dmunblock",
        "mydm",
    }
)


_MINOR_SEX_RE = re.compile(
    r"(?is)(?:"
    r"(?:child|children|kid|kids|toddler|infant|minor|minors|underage|pre-?teen|preteens?|"
    r"loli|lolita|shota|shotacon|lolicon|"
    r"(?:1[0-7])\s*(?:yo|y/o|year[- ]olds?))"
    r".{0,40}"
    r"(?:sex|sexual|porn|nude|naked|rape|molest|erotic|nsfw|hentai|csam|\bcp\b)"
    r"|"
    r"(?:sex|sexual|porn|nude|naked|rape|molest|erotic|nsfw|hentai|csam|\bcp\b)"
    r".{0,40}"
    r"(?:child|children|kid|kids|toddler|infant|minor|minors|underage|pre-?teen|"
    r"loli|lolita|shota|shotacon|lolicon|(?:1[0-7])\s*(?:yo|y/o|year[- ]olds?))"
    r")"
)

_DOXX_RE = re.compile(
    r"(?is)(?:"
    r"\bdoxx?(?:ing|es|ed)?\b|"
    r"\bswatt?ing\b|"
    r"(?:drop|leak|post|publish|doxx?)\s+(?:their|his|her|someone'?s?)\s+"
    r"(?:address|phone|ssn|social\s*security|real\s*name|home)|"
    r"(?:find|get|give)\s+me\s+(?:their|his|her)\s+(?:home\s*)?address|"
    r"social\s*security\s*number|"
    r"\b\d{3}-\d{2}-\d{4}\b"
    r")"
)

_CRED_THEFT_RE = re.compile(
    r"(?is)(?:"
    r"(?:steal|grab|phish|harvest)\s+(?:discord\s+)?(?:tokens?|passwords?|sessions?|cookies?)|"
    r"(?:discord\s+)?token\s*(?:logger|grabber|stealer)|"
    r"nitro\s*scam\s*link|"
    r"(?:free\s+)?nitro\s+from\s+this\s+link|"
    r"paste\s+(?:your|the)\s+token|"
    r"webhook\s*spammer\s*for\s+raiding"
    r")"
)

_MALWARE_RE = re.compile(
    r"(?is)(?:"
    r"(?:rat\s*stub|remote\s*access\s*trojan)\s+for\s+(?:victims?|discord)|"
    r"undetectable\s+(?:stealer|rat)\s+build|"
    r"spread\s+(?:this\s+)?(?:malware|virus|trojan)\s+on\s+discord"
    r")"
)


def _uid(user_id: typing.Any) -> str:
    return str(user_id or "").strip()


def issue_acceptance_url(user_id: typing.Any) -> str:
    """Issue one opaque, single-use, short-lived web acceptance capability."""
    uid = _uid(user_id)
    if not uid.isdigit() or len(uid) > 24:
        raise ValueError("invalid Discord user id")
    if len(config.TOS_ACCEPTANCE_SECRET) < 32:
        raise RuntimeError("web ToS acceptance is not configured")
    token = secrets.token_urlsafe(32)
    digest = hmac.new(
        config.TOS_ACCEPTANCE_SECRET.encode("utf-8"),
        token.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    db.tos_challenge_create(
        uid,
        digest,
        TOS_VERSION,
        time.time() + _ACCEPTANCE_CHALLENGE_SECONDS,
    )
    return f"{TOS_ACCEPT_URL}?token={quote(token, safe='')}"


def consume_acceptance_challenge(token: str) -> str | None:
    raw = str(token or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,80}", raw):
        return None
    if len(config.TOS_ACCEPTANCE_SECRET) < 32:
        return None
    digest = hmac.new(
        config.TOS_ACCEPTANCE_SECRET.encode("utf-8"),
        raw.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return db.tos_challenge_consume(digest, TOS_VERSION)


def network_fingerprint(address: str) -> str:
    """Return a keyed, non-reversible network token; never persist the IP."""
    if len(config.TOS_ACCEPTANCE_SECRET) < 32:
        raise RuntimeError("web ToS acceptance is not configured")
    parsed = ipaddress.ip_address(str(address or "").strip())
    if isinstance(parsed, ipaddress.IPv6Address):
        normalized = f"v6:{ipaddress.ip_network(f'{parsed}/64', strict=False)}"
    else:
        normalized = f"v4:{parsed.compressed}"
    key = hmac.new(
        config.TOS_ACCEPTANCE_SECRET.encode("utf-8"),
        b"owaua-tos-network-v1",
        hashlib.sha256,
    ).digest()
    return hmac.new(key, normalized.encode("ascii"), hashlib.sha256).hexdigest()


def record_web_acceptance(user_id: str, client_address: str) -> str:
    """Apply the disclosed acceptance/risk decision without storing a raw IP."""
    uid = _uid(user_id)
    if not uid.isdigit() or config.is_blocked(uid):
        return "blocked"
    try:
        fingerprint = network_fingerprint(client_address)
    except (ValueError, RuntimeError):
        return "unavailable"

    network_cutoff = time.time() - _NETWORK_RETENTION_SECONDS
    blocked_match = db.tos_acceptance_network_has_dynamic_block(
        fingerprint,
        since=network_cutoff,
        exclude_user_id=uid,
    )
    if not blocked_match:
        recent_users = db.tos_acceptance_network_users(
            fingerprint,
            since=network_cutoff,
        )
        blocked_match = any(other != uid and config.is_blocked(other) for other in recent_users)
    needs_review = blocked_match
    status = "review" if needs_review else "accepted"
    db.tos_acceptance_set(
        uid,
        TOS_VERSION,
        status=status,
        network_hash=fingerprint,
        risk_code="blocked_network_match" if needs_review else "",
    )
    if needs_review:
        db.user_flag_set(uid, "tos_accepted", "")
        db.user_flag_set(uid, "tos_review_pending", "1")
        return status
    db.user_flag_set(uid, "tos_review_pending", "")
    db.user_flag_set(uid, "tos_accepted", TOS_VERSION)
    db.user_flag_set(uid, "tos_accepted_at", str(time.time()))
    return status


def has_accepted(user_id: typing.Any) -> bool:
    """True if user accepted the current ToS version."""
    uid = _uid(user_id)
    if not uid:
        return False
    if config.is_bot_owner(uid):
        return True
    record = db.tos_acceptance_get(uid)
    return bool(
        (db.user_flag_get(uid, "tos_accepted") or "") == TOS_VERSION
        and record is not None
        and record.get("version") == TOS_VERSION
        and record.get("status") == "accepted"
    )


def accept(user_id: typing.Any) -> None:
    uid = _uid(user_id)
    db.tos_acceptance_set(uid, TOS_VERSION, status="accepted")
    db.user_flag_set(uid, "tos_accepted", TOS_VERSION)
    db.user_flag_set(uid, "tos_accepted_at", str(time.time()))
    db.user_flag_set(uid, "tos_review_pending", "")


def reject(user_id: typing.Any) -> None:
    uid = _uid(user_id)
    db.tos_acceptance_set(uid, TOS_VERSION, status="rejected")
    db.user_flag_set(uid, "tos_accepted", "")
    db.user_flag_set(uid, "tos_rejected_at", str(time.time()))
    db.user_flag_set(uid, "tos_review_pending", "")


def allow_review(user_id: typing.Any) -> bool:
    uid = _uid(user_id)
    if not uid.isdigit() or not db.tos_acceptance_allow(uid, TOS_VERSION):
        return False
    db.user_flag_set(uid, "tos_accepted", TOS_VERSION)
    db.user_flag_set(uid, "tos_accepted_at", str(time.time()))
    db.user_flag_set(uid, "tos_review_pending", "")
    return True


def status_line(user_id: typing.Any) -> str:
    if has_accepted(user_id):
        when = db.user_flag_get(_uid(user_id), "tos_accepted_at") or ""
        try:
            ts = float(when)
            when_s = time.strftime("%Y-%m-%d", time.gmtime(ts))
        except (TypeError, ValueError):
            when_s = "unknown date"
        return f"accepted **v{TOS_VERSION}** ({when_s})"
    record = db.tos_acceptance_get(_uid(user_id))
    if record and record.get("version") == TOS_VERSION and record.get("status") == "review":
        return "**submitted for review** — ordinary commands remain locked"
    return f"**not accepted** — required version **v{TOS_VERSION}**"


def need_accept_message(prefix: str = "!") -> str:
    return (
        f"**Terms of Service required**\n"
        f"Use the button below to read and accept: {TOS_URL}\n"
        f"Privacy: {PRIVACY_URL}\n\n"
        f"After accepting on the website, return to Discord and press "
        f"**I've accepted — check**, or retry your command.\n"
        f"No chat or other commands until you accept **v{TOS_VERSION}**."
    )


class AcceptanceView(discord.ui.View):
    """Short-lived web link plus invoker-bound acceptance controls.

    The web URL is deliberately not a Discord link button.  Link buttons do
    not produce interactions, so anybody who can see the message could open
    the bearer URL.  A regular button lets ``interaction_check`` enforce the
    Discord user binding before the URL is disclosed.
    """

    def __init__(self, user_id: int | str) -> None:
        super().__init__(timeout=float(_ACCEPTANCE_CHALLENGE_SECONDS))
        self.user_id = int(user_id)
        self.acceptance_url = issue_acceptance_url(self.user_id)
        for child in self.children:
            if getattr(child, "custom_id", "") == "tos:read-placeholder":
                typing.cast(typing.Any, child).custom_id = f"tos:read:{self.user_id}"
            elif getattr(child, "label", "") == "I've accepted — check":
                typing.cast(typing.Any, child).custom_id = f"tos:check:{self.user_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "this acceptance link belongs to someone else. Run `/tos` for your own link.",
                ephemeral=True,
            )
            return False
        return True

    async def _send_acceptance_url(self, interaction: discord.Interaction) -> None:
        """Reveal the bearer URL only after the view's user check succeeds."""
        await interaction.response.send_message(
            f"[Open the Terms acceptance page]({self.acceptance_url})",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Read and accept the Terms",
        style=discord.ButtonStyle.primary,
        custom_id="tos:read-placeholder",
    )
    async def read_terms(
        self, interaction: discord.Interaction, _button: discord.ui.Button[typing.Any]
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "this acceptance link belongs to someone else. Run `/tos` for your own link.",
                ephemeral=True,
            )
            return
        await self._send_acceptance_url(interaction)

    @discord.ui.button(
        label="I've accepted — check",
        style=discord.ButtonStyle.success,
    )
    async def check_acceptance(
        self, interaction: discord.Interaction, _button: discord.ui.Button[typing.Any]
    ) -> None:
        if has_accepted(self.user_id):
            for child in self.children:
                typing.cast(typing.Any, child).disabled = True
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="terms accepted",
                    description=(f"ToS **v{TOS_VERSION}** is accepted. You can use owaua now."),
                    color=0x2B2D31,
                ),
                view=self,
            )
            self.stop()
            return
        record = db.tos_acceptance_get(str(self.user_id))
        if record and record.get("status") == "review":
            message = (
                "your acceptance needs a manual abuse-prevention review. "
                f"Contact {config.PRIVACY_CONTACT}; ordinary commands stay locked for now."
            )
        else:
            message = "acceptance is not complete yet. Finish the website form, then check again."
        await interaction.response.send_message(message, ephemeral=True)


def command_allowed_without_tos(cmd_name: str) -> bool:
    return (cmd_name or "").strip().lower() in TOS_ALLOWED_COMMANDS


def detect_hard_violation_info(text: str) -> Optional[Tuple[str, str]]:
    """Return (reason, category) if text is a clear ToS violation, else None."""
    if not text or len(text.strip()) < 4:
        return None
    t = text.strip()
    if _MINOR_SEX_RE.search(t):
        return "sexual content involving minors", "minor_sex_csam"
    if _DOXX_RE.search(t):
        return "doxxing / private personal data abuse", "doxxing"
    if _CRED_THEFT_RE.search(t):
        return "credential / token theft or phishing", "credential_theft"
    if _MALWARE_RE.search(t):
        return "malware distribution / abuse tooling", "malware"
    return None


def detect_hard_violation(text: str) -> Optional[str]:
    """Return a short reason if text is a clear absolute ToS violation."""
    res = detect_hard_violation_info(text)
    return res[0] if res else None


def _strike(user_id: str, key: str) -> int:
    n = db.user_flag_int(user_id, key, 0) + 1
    db.user_flag_set(user_id, key, str(n))
    return n


def note_leak_attempt(user_id: typing.Any) -> Tuple[bool, int]:
    """Count a prompt-leak attempt. Returns (should_block, strike_count)."""
    uid = _uid(user_id)
    if config.is_bot_owner(uid):
        return False, 0
    n = _strike(uid, "tos_leak_strikes")
    return n >= _LEAK_STRIKE_LIMIT, n


def rate_limit_retry_after(user_id: typing.Any) -> float:
    """Return a retry delay or quarantine duration without creating manual block friction."""
    uid = _uid(user_id)
    if not uid or config.is_bot_owner(uid):
        return 0.0
    now = time.monotonic()
    with _rate_lock:
        quarantine_end = _quarantine_until.get(uid, 0.0)
        if now < quarantine_end:
            return max(0.1, quarantine_end - now)
        elif quarantine_end > 0.0:
            _quarantine_until.pop(uid, None)

        bucket = _rate_buckets.setdefault(uid, collections.deque())
        while bucket and now - bucket[0] >= _RATE_WINDOW_SEC:
            bucket.popleft()
        if len(bucket) >= _RATE_MAX:
            hammer = _hammer_buckets.setdefault(uid, collections.deque())
            while hammer and now - hammer[0] >= _HAMMER_WINDOW_SEC:
                hammer.popleft()
            hammer.append(now)
            if len(hammer) >= _HAMMER_LIMIT:
                _quarantine_until[uid] = now + _QUARANTINE_SECONDS
                strikes = _strike(uid, "tos_spam_strikes")
                if strikes >= _QUARANTINE_STRIKE_LIMIT:
                    hard_block(
                        uid,
                        "automated rate-limit flooding / spam attack",
                        category="spam_flood",
                        trigger_source="rate_limiter",
                        strikes_detail=f"hammer strikes: {strikes}",
                    )
                return _QUARANTINE_SECONDS
            return max(0.1, _RATE_WINDOW_SEC - (now - bucket[0]))

        bucket.append(now)
        if len(_rate_buckets) > 10_000:
            stale = [
                key
                for key, values in _rate_buckets.items()
                if not values or now - values[-1] >= _RATE_WINDOW_SEC
            ]
            for key in stale:
                _rate_buckets.pop(key, None)
        return 0.0


def _infer_category(reason: str, explicit_category: str = "") -> str:
    if explicit_category:
        return explicit_category
    r = (reason or "").lower()
    if "minor" in r or "csam" in r or "child" in r:
        return "minor_sex_csam"
    if "doxx" in r or "private" in r or "personal data" in r:
        return "doxxing"
    if "credential" in r or "token" in r or "phish" in r:
        return "credential_theft"
    if "malware" in r or "trojan" in r or "rat" in r:
        return "malware"
    if "leak" in r or "prompt" in r or "exfiltration" in r:
        return "prompt_leak"
    if "spam" in r or "flood" in r:
        return "spam_flood"
    if "model" in r or "policy" in r:
        return "model_policy_flag"
    return "general_tos_violation"


def hard_block(
    user_id: typing.Any,
    reason: str,
    *,
    category: str = "",
    offending_text: str = "",
    channel_id: str = "",
    guild_id: str = "",
    guild_name: str = "",
    user_tag: str = "",
    trigger_source: str = "",
    strikes_detail: str = "",
) -> bool:
    """Persist a ToS hard-block with rich violation metadata. Returns True if newly blocked."""
    uid = _uid(user_id)
    if not uid or config.is_bot_owner(uid):
        return False

    tos_reason = reason if reason.lower().startswith("tos:") else f"tos: {reason}"
    cat = _infer_category(reason, category)

    db.user_flag_set(uid, "tos_emergency_block", "1")

    raw = (offending_text or "").encode("utf-8", errors="replace")
    evidence = ""
    if raw:
        evidence = f"sha256:{hashlib.sha256(raw).hexdigest()[:16]} length:{len(raw)}"

    try:
        from owaua import blocked

        return blocked.block_user(
            uid,
            reason=tos_reason[:250],
            category=cat,
            offending_text=evidence,
            channel_id=channel_id,
            guild_id=guild_id,
            guild_name=guild_name,
            user_tag=user_tag,
            trigger_source=trigger_source,
            strikes_detail=strikes_detail,
        )
    except Exception as e:
        print(f"[tos] block failed for {uid}: {e}")
        return True


def is_emergency_blocked(user_id: typing.Any) -> bool:
    return db.user_flag_get(_uid(user_id), "tos_emergency_block") == "1"


def clear_block_state(user_id: typing.Any) -> None:
    """Clear only ToS-created flags; manual/static blocks are untouched."""
    uid = _uid(user_id)
    with _rate_lock:
        _quarantine_until.pop(uid, None)
        _hammer_buckets.pop(uid, None)
        _rate_buckets.pop(uid, None)
    for key, value in (
        ("tos_emergency_block", ""),
        ("tos_leak_strikes", "0"),
        ("tos_violation_strikes", "0"),
        ("tos_spam_strikes", "0"),
        ("tos_model_strikes", "0"),
        ("tos_spam_bucket", ""),
    ):
        db.user_flag_set(uid, key, value)


def check_message(user_id: typing.Any, text: str):
    """
    Run ToS detectors on a user message.

    Returns (action, reason, strikes) with action "warn" or "block", or None
    if no violation. Hard violations warn for the first TOS_STRIKE_LIMIT - 1
    strikes and only block on the TOS_STRIKE_LIMIT-th. Callers should reply
    with the warning on "warn" and hard_block + blocked reply on "block".
    """
    uid = _uid(user_id)
    if not uid or config.is_bot_owner(uid):
        return None

    info = detect_hard_violation_info(text or "")
    if info:
        reason, _cat = info
        n = _strike(uid, "tos_violation_strikes")
        action = "block" if n >= TOS_STRIKE_LIMIT else "warn"
        return action, reason, n

    return None


def handle_model_tos_flag(user_id: typing.Any, flag: typing.Any) -> Optional[str]:
    """
    Model may emit tos_violation: {\"reason\": \"...\"} or a plain string.
    Returns block reason if actionable.
    """
    if not flag:
        return None
    if config.is_bot_owner(user_id):
        return None
    if isinstance(flag, dict):
        reason = str(
            typing.cast(typing.Any, flag).get("reason")
            or typing.cast(typing.Any, flag).get("type")
            or typing.cast(typing.Any, flag).get("violation")
            or ""
        ).strip()
        severity = str(typing.cast(typing.Any, flag).get("severity") or "high").lower()
    else:
        reason = str(flag).strip()
        severity = "high"
    if not reason or len(reason) < 3:
        return None
    if severity in ("low", "medium", "warn"):
        n = _strike(_uid(user_id), "tos_model_strikes")
        if n >= 3:
            return f"repeated policy abuse ({reason[:80]})"
        return None
    return reason[:120]


def page_footer() -> str:
    return f"ToS v{TOS_VERSION}: {TOS_URL}"
