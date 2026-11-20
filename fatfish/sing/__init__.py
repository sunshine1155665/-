"""On-demand singing (YuE2 -> RVC) with the shared VRAM lease."""

from .service import (
    RemoteSingerClient,
    RenderRequest,
    SingResult,
    SingService,
    TTSHandoff,
    clean_lyrics,
    friendly_sing_error,
)

__all__ = [
    "RemoteSingerClient",
    "SingResult",
    "SingService",
    "RenderRequest",
    "TTSHandoff",
    "clean_lyrics",
    "friendly_sing_error",
]
