"""Opt-in, review-first guild content moderation.

The model is a classifier only. A flag creates a bounded staff review item in
a private channel; it never deletes content, DMs a user, or changes global
block state without a current moderator approving that exact item.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import discord

from sefbot import config, db, embeds
from sefbot.scope import Scope
from sefbot.services.llm_client import LLMError, llm

log = logging.getLogger("sefbot.moderation")

_MAX_CONCURRENT_CHECKS = 4
_MAX_PENDING_REVIEWS = 100
_REVIEW_TIMEOUT_SECONDS = 15 * 60
_MAX_EVIDENCE_LENGTH = 1500

_last_check: dict[tuple[int, int], float] = {}
_pending_reviews: dict[int, "ModerationReviewView"] = {}
_active_checks = 0


def _enabled_for_guild(guild_id: int) -> bool:
    """Moderation requires an explicit per-guild opt-in, not only an env flag."""
    try:
        return (
            db.guild_settings(Scope.guild(guild_id).key).get("moderation_enabled")
            is True
        )
    except Exception:
        log.exception("could not read moderation settings for guild %s", guild_id)
        return False


def _private_staff_channel(
    channel: object,
    guild: discord.Guild,
    bot_member: Optional[discord.Member] = None,
) -> bool:
    """Return whether a channel is private from @everyone and usable by the bot."""
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        everyone = channel.permissions_for(guild.default_role)
        if getattr(everyone, "view_channel", False):
            return False
        me = bot_member or guild.me
        if me is None:
            return False
        bot_perms = channel.permissions_for(me)
        return bool(
            getattr(bot_perms, "administrator", False)
            or (
                getattr(bot_perms, "view_channel", False)
                and getattr(bot_perms, "send_messages", False)
            )
        )
    except (AttributeError, TypeError):
        return False


def _mod_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """Resolve a configured private, bot-writable staff channel for this guild."""
    configured = ""
    try:
        configured = str(
            db.guild_settings(Scope.guild(guild.id).key).get("modlog_channel") or ""
        ).strip()
    except Exception:
        log.exception("could not read mod-log settings for guild %s", guild.id)

    # The environment value remains a compatibility fallback, but resolution is
    # guild-local and still requires a private channel.
    candidates = [configured, str(getattr(config, "MODLOG_CHANNEL", "") or "").strip()]
    for raw_id in candidates:
        if not raw_id.isdigit():
            continue
        channel = guild.get_channel(int(raw_id))
        if _private_staff_channel(channel, guild):
            return channel
        if channel is not None:
            log.warning("refusing public or unwritable mod-log channel %s", raw_id)

    named = discord.utils.get(guild.text_channels, name="mod-log")
    return named if _private_staff_channel(named, guild) else None


async def _fresh_private_staff_channel(
    guild: discord.Guild, channel: object
) -> Optional[discord.TextChannel]:
    """Re-fetch a staff channel so stale overwrites cannot expose review data."""
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return None
    try:
        current = await guild.fetch_channel(channel_id)
        bot_id = getattr(guild.me, "id", None)
        current_bot = await guild.fetch_member(bot_id) if bot_id is not None else None
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return current if _private_staff_channel(current, guild, current_bot) else None


async def private_staff_log_channel(
    guild: discord.Guild, *, preferred_id: str = ""
) -> Optional[discord.TextChannel]:
    """Resolve and revalidate a private incident channel for deterministic safety tools."""
    channel = None
    if str(preferred_id or "").strip().isdigit():
        channel = guild.get_channel(int(preferred_id))
        if channel is not None and not _private_staff_channel(channel, guild):
            log.warning("refusing public or unwritable incident channel %s", preferred_id)
            channel = None
    if channel is None:
        channel = _mod_log_channel(guild)
    if channel is None:
        return None
    return await _fresh_private_staff_channel(guild, channel)


@dataclass(frozen=True)
class ModerationReview:
    guild_id: int
    channel_id: int
    message_id: int
    author_id: int
    category: str
    reason: str
    confidence: float


def _review_permission(member: object, channel: object) -> bool:
    """Require the exact Discord permission used by the proposed deletion."""
    if not isinstance(member, discord.Member):
        return False
    if getattr(member.guild, "owner_id", None) == member.id:
        return True
    try:
        permissions = channel.permissions_for(member)
    except (AttributeError, TypeError):
        permissions = member.guild_permissions
    return bool(
        getattr(permissions, "administrator", False)
        or getattr(permissions, "manage_messages", False)
    )


class ModerationReviewView(discord.ui.View):
    """Short-lived, single-use confirmation for a model moderation flag."""

    def __init__(self, review: ModerationReview, embed: discord.Embed) -> None:
        super().__init__(timeout=_REVIEW_TIMEOUT_SECONDS)
        self.review = review
        self.embed = embed
        self._done = False
        self._lock = asyncio.Lock()
        self._expires_at = time.monotonic() + _REVIEW_TIMEOUT_SECONDS
        self.report_message: Optional[discord.Message] = None

    def _redact_evidence(self) -> None:
        """Replace classifier output/message content with minimal durable metadata."""
        self.embed.description = (
            f"**author id:** `{self.review.author_id}`\n"
            f"**source:** <#{self.review.channel_id}> / `{self.review.message_id}`\n"
            f"**category:** {discord.utils.escape_markdown(self.review.category)}\n"
            f"**confidence:** {self.review.confidence:.0%}\n\n"
            "Evidence was redacted after review."
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        source = guild.get_channel(self.review.channel_id) if guild else None
        allowed = bool(
            guild is not None
            and guild.id == self.review.guild_id
            and source is not None
            and _review_permission(interaction.user, source)
        )
        if not allowed:
            try:
                await interaction.response.send_message(
                    "you need `manage_messages` in the source channel to review this.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        return allowed

    @discord.ui.button(label="Delete message", style=discord.ButtonStyle.danger)
    async def delete_message(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._finish(interaction, delete=True)

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary)
    async def dismiss(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._finish(interaction, delete=False)

    async def _finish(self, interaction: discord.Interaction, *, delete: bool) -> None:
        async with self._lock:
            if self._done:
                await interaction.response.send_message(
                    "this review was already resolved.", ephemeral=True
                )
                return

            guild = interaction.guild
            if (
                guild is None
                or guild.id != self.review.guild_id
                or time.monotonic() >= self._expires_at
                or not _enabled_for_guild(self.review.guild_id)
            ):
                await interaction.response.send_message(
                    "this review expired or moderation was disabled; nothing was deleted.",
                    ephemeral=True,
                )
                return

            actor_id = getattr(interaction.user, "id", None)
            bot_id = getattr(getattr(interaction.client, "user", None), "id", None)
            try:
                source = await guild.fetch_channel(self.review.channel_id)
                current_actor = await guild.fetch_member(actor_id)
                current_bot = await guild.fetch_member(bot_id) if delete else None
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError):
                await interaction.response.send_message(
                    "the source channel or current permissions could not be revalidated; "
                    "nothing was deleted.",
                    ephemeral=True,
                )
                return
            if not _review_permission(current_actor, source):
                await interaction.response.send_message(
                    "your permission changed; nothing was deleted.", ephemeral=True
                )
                return
            if delete and (
                current_bot is None or not _review_permission(current_bot, source)
            ):
                await interaction.response.send_message(
                    "the bot no longer has `manage_messages`; nothing was deleted.",
                    ephemeral=True,
                )
                return

            # The lock serializes all click handlers. Consume immediately after
            # fresh authorization, before fetching or mutating the source message.
            self._done = True
            _pending_reviews.pop(self.review.message_id, None)
            for child in self.children:
                child.disabled = True
            self._redact_evidence()

            outcome = "dismissed — no action taken"
            success = True
            if delete:
                try:
                    message = await source.fetch_message(self.review.message_id)
                    await message.delete(
                        reason=(
                            "approved moderation review by "
                            f"{current_actor} ({current_actor.id})"
                        )
                    )
                    outcome = "deleted after staff approval"
                except discord.NotFound:
                    outcome = "message was already gone"
                except (discord.Forbidden, discord.HTTPException):
                    outcome = "deletion failed; Discord rejected the request"
                    success = False

            self.embed.color = discord.Color.green() if success else discord.Color.red()
            self.embed.set_footer(text=f"{outcome} · reviewed by {current_actor}")
            try:
                await interaction.response.edit_message(embed=self.embed, view=self)
            except discord.HTTPException:
                pass
            try:
                db.log_interaction(
                    "moderation_review_delete"
                    if delete and success
                    else "moderation_review_dismiss",
                    str(self.review.author_id),
                    Scope.guild(self.review.guild_id).key,
                )
            except Exception:
                log.exception("moderation review audit write failed")
            self.stop()

    async def on_timeout(self) -> None:
        async with self._lock:
            if self._done:
                return
            self._done = True
            _pending_reviews.pop(self.review.message_id, None)
            for child in self.children:
                child.disabled = True
            self._redact_evidence()
            self.embed.color = discord.Color.light_grey()
            self.embed.set_footer(text="expired — no action taken")
            if self.report_message is not None:
                try:
                    await self.report_message.edit(embed=self.embed, view=self)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass


async def _queue_review(message: discord.Message, result: dict) -> None:
    """Queue one bounded staff review without mutating the source message."""
    if message.id in _pending_reviews or len(_pending_reviews) >= _MAX_PENDING_REVIEWS:
        log.warning("moderation review queue full or duplicate; dropping message %s", message.id)
        return
    mod_channel = await _fresh_private_staff_channel(
        message.guild, _mod_log_channel(message.guild)
    )
    if mod_channel is None:
        log.warning("no private mod-log channel for guild %s", message.guild.id)
        return

    category = str(result.get("category") or "unspecified")[:80]
    reason = str(result.get("reason") or "classifier supplied no reason")[:500]
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    review = ModerationReview(
        guild_id=message.guild.id,
        channel_id=message.channel.id,
        message_id=message.id,
        author_id=message.author.id,
        category=category,
        reason=reason,
        confidence=confidence,
    )
    evidence = discord.utils.escape_markdown(
        discord.utils.escape_mentions(message.content[:_MAX_EVIDENCE_LENGTH])
    )
    embed = discord.Embed(
        title=f"Moderation review — {discord.utils.escape_markdown(category)}",
        description=(
            f"**author:** {discord.utils.escape_markdown(str(message.author))} "
            f"(`{message.author.id}`)\n"
            f"**channel:** <#{message.channel.id}>\n"
            f"**timestamp:** {embeds.fmt_ts(message.created_at.timestamp())}\n"
            f"**classifier confidence:** {confidence:.0%}\n"
            f"**classifier reason:** {discord.utils.escape_markdown(reason)}\n\n"
            f"**evidence:**\n{evidence or '(no text)'}"
        ),
        color=discord.Color.orange(),
    )
    embed.set_footer(text="model flag only · staff confirmation required")
    view = ModerationReviewView(review, embed)
    try:
        report = await mod_channel.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.warning("could not post moderation review for message %s", message.id)
        return
    view.report_message = report
    _pending_reviews[message.id] = view
    try:
        db.log_interaction(
            "moderation_flag_review",
            str(message.author.id),
            Scope.guild(message.guild.id).key,
        )
    except Exception:
        log.exception("moderation flag audit write failed")


async def _enforce(message: discord.Message, result: dict) -> None:
    """Compatibility name: classifier flags are review-only and never auto-enforced."""
    await _queue_review(message, result)


async def safety_check(message: discord.Message) -> None:
    """Classify one message when its guild explicitly opted in to moderation."""
    global _active_checks

    if not getattr(config, "SAFETY_ENABLED", False) or not config.SAFETY_API_KEY:
        return
    if message.guild is None or not _enabled_for_guild(message.guild.id):
        return
    mod_channel = await _fresh_private_staff_channel(
        message.guild, _mod_log_channel(message.guild)
    )
    if mod_channel is None:
        # Never send content to a classifier when there is nowhere private for
        # a human to review the result.
        return
    if getattr(message.author, "bot", False):
        return
    if not message.content or len(message.content) < 2:
        return
    if _active_checks >= _MAX_CONCURRENT_CHECKS:
        log.warning("moderation classifier at capacity; dropping message %s", message.id)
        return

    key = (message.guild.id, message.author.id)
    now = time.monotonic()
    if now - _last_check.get(key, 0.0) < max(0.0, config.SAFETY_MIN_INTERVAL):
        return
    _last_check[key] = now
    if len(_last_check) > 10_000:
        cutoff = now - max(60.0, config.SAFETY_MIN_INTERVAL * 10)
        for old_key, checked_at in list(_last_check.items()):
            if checked_at < cutoff:
                _last_check.pop(old_key, None)

    _active_checks += 1
    try:
        result = await llm.moderate(
            config.SAFETY_MODEL,
            message.content[:4000],
            base_url=config.SAFETY_BASE_URL,
            api_key=config.SAFETY_API_KEY,
        )
        if not isinstance(result, dict):
            return
        try:
            confidence = float(result.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if result.get("flagged") is True and confidence >= config.SAFETY_MIN_CONFIDENCE:
            await _queue_review(message, result)
    except LLMError:
        log.warning("safety model unavailable")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("safety check crashed for message %s", getattr(message, "id", None))
    finally:
        _active_checks = max(0, _active_checks - 1)
