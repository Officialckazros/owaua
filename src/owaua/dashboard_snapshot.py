"""Discord-to-dashboard serialization with explicit privacy and size bounds."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

MAX_GUILDS = 500
MAX_MEMBERS = 10_000
MAX_CHANNELS = 500
MAX_ROLES = 500


def serialize_guilds(guilds: Iterable[Any]) -> list[dict[str, Any]]:
    """Return the sanitized live server snapshot consumed by the dashboard."""
    output: list[dict[str, Any]] = []
    for guild in list(guilds)[:MAX_GUILDS]:
        members = list(guild.members)[:MAX_MEMBERS]
        output.append(
            {
                "id": str(guild.id),
                "name": guild.name,
                "icon": str(guild.icon.url) if guild.icon else "",
                "member_count": int(guild.member_count or 0),
                "everyone_permissions": int(guild.default_role.permissions.value),
                "bot_permissions": int(guild.me.guild_permissions.value) if guild.me else 0,
                "members": [
                    {
                        "id": str(member.id),
                        "name": member.display_name[:100],
                        "boosting": member.premium_since is not None,
                    }
                    for member in members
                    if not member.bot
                ],
                "manager_ids": [
                    str(member.id)
                    for member in members
                    if (
                        member.id == guild.owner_id
                        or member.guild_permissions.administrator
                        or member.guild_permissions.manage_guild
                    )
                ],
                "channels": [
                    {
                        "id": str(channel.id),
                        "name": channel.name,
                        "type": str(channel.type),
                        "private": not channel.permissions_for(
                            guild.default_role
                        ).view_channel,
                        "bot_writable": bool(
                            guild.me
                            and channel.permissions_for(guild.me).view_channel
                            and (
                                not hasattr(
                                    channel.permissions_for(guild.me), "send_messages"
                                )
                                or channel.permissions_for(guild.me).send_messages
                            )
                        ),
                    }
                    for channel in list(guild.channels)[:MAX_CHANNELS]
                ],
                "roles": [
                    {"id": str(role.id), "name": role.name, "color": str(role.color)}
                    for role in list(guild.roles)[:MAX_ROLES]
                    if not role.is_default()
                ],
            }
        )
    return output
