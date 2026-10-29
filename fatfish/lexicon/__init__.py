"""Chinese expression / internet-slang library for fatfish."""

from .miner import mine
from .service import (
    PROMPT_HEADER,
    PROMPT_MAX_CHARS,
    VALID_KINDS,
    VALID_STATUSES,
    VALID_STYLES,
    LexiconService,
    load_seed_entries,
    make_id,
)

__all__ = [
    "LexiconService",
    "PROMPT_HEADER",
    "PROMPT_MAX_CHARS",
    "VALID_KINDS",
    "VALID_STATUSES",
    "VALID_STYLES",
    "load_seed_entries",
    "make_id",
    "mine",
]
