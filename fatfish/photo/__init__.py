"""On-demand Anima Turbo photo generation (ComfyUI + whale-girl LoRA)."""

from .service import (
    PhotoPrompt,
    PhotoResult,
    PhotoService,
    friendly_photo_error,
    wants_photo,
)

__all__ = [
    "PhotoPrompt",
    "PhotoResult",
    "PhotoService",
    "friendly_photo_error",
    "wants_photo",
]
