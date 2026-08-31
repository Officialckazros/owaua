"""Canonical privacy scopes for persisted bot data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ScopeKind = Literal["guild", "dm"]


@dataclass(frozen=True, slots=True)
class Scope:
    kind: ScopeKind
    id: str

    def __post_init__(self) -> None:
        if self.kind not in {"guild", "dm"}:
            raise ValueError(f"invalid scope kind: {self.kind!r}")
        value = str(self.id).strip()
        if not value or ":" in value:
            raise ValueError("scope id must be a non-empty Discord identifier")
        object.__setattr__(self, "id", value)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    @classmethod
    def guild(cls, guild_id: object) -> "Scope":
        return cls("guild", str(guild_id))

    @classmethod
    def dm(cls, user_id: object) -> "Scope":
        return cls("dm", str(user_id))

    @classmethod
    def parse(cls, value: object) -> "Scope":
        raw = str(value or "").strip()
        kind, sep, identifier = raw.partition(":")
        if not sep or kind not in {"guild", "dm"}:
            raise ValueError(f"invalid scope key: {raw!r}")
        return cls(kind, identifier)  # type: ignore[arg-type]


def scope_key(*, guild_id: object | None, user_id: object) -> str:
    """Return the exact persistence scope for a Discord request."""
    return Scope.guild(guild_id).key if guild_id is not None else Scope.dm(user_id).key


def is_dm_scope(value: object) -> bool:
    return str(value or "").startswith("dm:")


def is_guild_scope(value: object) -> bool:
    return str(value or "").startswith("guild:")
