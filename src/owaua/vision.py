"""Image descriptions and moderation through the vision model."""

import logging
import typing
from typing import Dict, List, Optional, Tuple

import discord

from owaua import ai_control, config
from owaua.services.llm_client import LLMError, coerce_bool, llm, sniff_image_mime

log = logging.getLogger("owaua.vision")

_JSON_PROMPT = (
    "Look at this image and reply with ONLY a JSON object:\n"
    '{"description": "a concrete 2-5 sentence description (subjects, composition, '
    'visible text, context)", "flagged": true|false, "category": "harassment|hate|'
    'sexual|violence|self_harm|illegal|spam|none", "reason": "short justification '
    'if flagged, else empty string"}'
)


def collect_image_urls(message: discord.Message) -> List[str]:
    """Gather image urls from attachments, embeds, stickers and replied-to messages."""
    urls: List[str] = []

    def _add(u: Optional[str]) -> None:
        if u and u not in urls:
            urls.append(u)

    for a in message.attachments or []:
        if a.content_type and a.content_type.startswith("image/"):
            _add(getattr(a, "proxy_url", None) or a.url)
    for e in message.embeds or []:
        if getattr(e, "image", None) and e.image and e.image.url:
            _add(e.image.url)
        if getattr(e, "thumbnail", None) and e.thumbnail and e.thumbnail.url:
            _add(e.thumbnail.url)
    for s in message.stickers or []:
        _add(getattr(s, "url", None))
    return urls


async def describe_bytes(
    image_bytes: bytes,
    prompt: str = "",
    mime: str = "image/png",
    *,
    scope_id: str | None = None,
    user_id: str | None = None,
) -> Tuple[str, Dict[typing.Any, typing.Any]]:
    """Describe image bytes + safety flag in one vision call."""
    detected_mime = sniff_image_mime(image_bytes)
    if detected_mime is None:
        return "that file isn't a supported PNG, JPEG, GIF, or WebP image.", {}
    if len(image_bytes) > config.VISION_MAX_IMAGE_BYTES:
        return (
            f"that image is too large (limit: {config.VISION_MAX_IMAGE_BYTES // 1_000_000} MB).",
            {},
        )
    if not config.LLM_API_KEY:
        return (
            "vision isn't configured — set `OWAUA_LLM_API_KEY` (and `OWAUA_VISION_MODEL`).",
            {},
        )
    text_prompt = prompt.strip() or _JSON_PROMPT
    try:
        result = await llm.vision_json(
            config.VISION_MODEL,
            image_bytes,
            text_prompt,
            mime=detected_mime,
            scope_id=scope_id,
            user_id=user_id,
        )
    except ai_control.AIBudgetExceeded as e:
        return str(e), {}
    except LLMError as e:
        log.warning("vision request failed (%s)", type(e).__name__)
        return "(vision failed: provider unavailable)", {}
    if not result:
        return "(vision returned nothing parseable)", {}
    description = str(result.get("description") or "").strip()
    flag = {
        "flagged": coerce_bool(result.get("flagged")),
        "category": str(result.get("category") or "none"),
        "reason": str(result.get("reason") or ""),
    }
    if flag["flagged"] and not description:
        description = f"⚠️ **flagged: {flag['category']}** — {flag['reason']}"
    return description or "(no description)", flag


async def describe_message(
    message: discord.Message,
    prompt: str = "",
    *,
    scope_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Describe the first image on a message (context-menu entry point)."""
    urls = collect_image_urls(message)
    if not urls:
        return "no image found on that message."
    downloaded = await llm.get_image(urls[0])
    if downloaded is None:
        return "couldn't fetch the image."
    data, mime = downloaded
    description, flag = await describe_bytes(data, prompt, mime, scope_id=scope_id, user_id=user_id)
    if flag.get("flagged"):
        return f"⚠️ **flagged: {flag['category']}** — {flag['reason']}\n\n{description}"
    return description
