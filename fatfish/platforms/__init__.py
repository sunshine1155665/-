"""Chat platform adapters (QQ/OneBot v11 today, text-only)."""

from .base import InboundMessage, OutboundMessage, PlatformError, make_session_id

__all__ = [
    "InboundMessage",
    "OutboundMessage",
    "PlatformError",
    "make_session_id",
]
