"""Shared platform data structures.

Adapters translate vendor events into :class:`InboundMessage` and hand text
back through the runtime outbox as :class:`OutboundMessage` payloads. Keeping
the vocabulary tiny makes the runtime independent of any one chat network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PlatformError(RuntimeError):
    """Raised when a platform adapter cannot send or parse an event."""


def make_session_id(platform: str, target_type: str, target_id: str | int) -> str:
    """Canonical session key, e.g. ``qq:private:10001`` / ``qq:group:123456``."""
    kind = str(target_type or "").strip().lower()
    if kind not in {"private", "group"}:
        raise ValueError(f"invalid target_type: {target_type!r}")
    ident = str(target_id or "").strip()
    if not ident:
        raise ValueError("target_id must not be empty")
    return f"{str(platform).strip().lower()}:{kind}:{ident}"


@dataclass(slots=True)
class InboundMessage:
    """Normalized inbound chat message."""

    platform: str
    event_id: str
    session_id: str
    user_id: str
    group_id: str | None
    text: str
    is_group: bool
    mentioned: bool = False
    reply_to: str | None = None
    # Display name the platform attached to this message (QQ card/nickname).
    # Empty when the adapter has no per-sender name; the runtime still records
    # the account id for per-turn attribution.
    sender_name: str = ""
    # Remote media URLs carried by the message (QQ image segments). The
    # adapter resolves these to inline data URLs for vision-capable LLMs at
    # turn time; the bytes themselves are never persisted.
    images: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundMessage:
    """Normalized text message to send back to a platform."""

    target_type: str  # private | group
    target_id: str
    text: str
    reply_to: str | None = None

    def __post_init__(self) -> None:
        self.target_type = str(self.target_type or "").strip().lower()
        if self.target_type not in {"private", "group"}:
            raise ValueError(f"invalid target_type: {self.target_type!r}")
        self.target_id = str(self.target_id or "").strip()
        if not self.target_id:
            raise ValueError("target_id must not be empty")
        self.text = str(self.text or "")
        self.reply_to = None if self.reply_to is None else str(self.reply_to)

    def to_payload(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "text": self.text,
            "reply_to": self.reply_to,
        }
