"""Multi-session roaming (ADR-0014)."""

from .intent import parse_return_command, parse_visit_command
from .model import (
    Room,
    RoamTrip,
    SummaryPayload,
    ThreadCard,
)
from .service import ATTENTION_HEADER, RECAP_HEADER, RoamService
from .summary import deterministic_summary

__all__ = [
    "ATTENTION_HEADER",
    "RECAP_HEADER",
    "RoamService",
    "RoamTrip",
    "Room",
    "SummaryPayload",
    "ThreadCard",
    "deterministic_summary",
    "parse_return_command",
    "parse_visit_command",
]
