"""Sticker import and tagging pipeline.

The pipeline is deliberately dependency-light:

* image discovery / hashing / validation uses the standard library (Pillow is
  used only when it happens to be installed);
* OCR uses ``pytesseract`` only when that package and a Tesseract binary are
  available, otherwise it returns an empty string;
* tagging calls the existing :class:`fatfish.llm.LLMClient` with an
  OpenAI-style ``image_url`` content part.  ``deepseek-v4.1-flash`` accepts
  image parts, but if a model/endpoint rejects the request the pipeline falls
  back to filename/OCR-only, conservative tags.

Nothing here writes to the database directly: ``import_stickers`` calls
``StickerService.add_sticker`` so all sticker invariants (embedding, timestamps,
usage_count preservation) stay in the service.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .service import INTENT_KEYWORDS, StickerService

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
INTENT_TAXONOMY = tuple(INTENT_KEYWORDS.keys())
DEFAULT_LIMIT = 12
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_MAX_TOKENS = 1500
DEFAULT_TEXT_MAX_TOKENS = 1000
MAX_TAGS = 8
MAX_INTENTS = 4
MAX_INLINE_IMAGE_BYTES = 4 * 1024 * 1024

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_TEMPLATE_HINTS = (
    "base",
    "template",
    "blank",
    "empty",
    "solid",
    "底图",
    "模板",
    "素体",
    "空白",
)

VISION_PROMPT = (
    "你是聊天表情包标注助手。请观察这张图片，只输出一个 JSON 对象，"
    "不要输出 markdown、代码块或任何解释。字段如下：\n"
    "caption: 中文短标题（不超过20字）；\n"
    "description: 一句中文描述；\n"
    "tags: 中文关键词数组，最多8个；\n"
    "emotion: 对象，包含 primary（情绪词）和 intensity（1-5 的整数）；\n"
    f"intents: 数组，只能从这些值里选：{', '.join(INTENT_TAXONOMY)}；\n"
    "intensity: 整体强度，1-5 的整数；\n"
    "style: 风格，例如 cartoon/anime/photo/meme/text/3d；\n"
    "has_text: 图中是否有文字，布尔值；\n"
    "is_base: 是否是无人物的纯底图/空白模板素材，布尔值；\n"
    "text_content: 图中文字内容，没有文字就填空字符串。\n"
    "只输出 JSON。"
)

TEXT_PROMPT = (
    "你无法查看图片内容。请只根据下面提供的文件名、尺寸和 OCR 文字，"
    "为聊天表情包生成保守的 JSON 标注。不要臆测画面细节；不确定的字段留空。"
    "只输出一个 JSON 对象，不要输出 markdown、代码块或任何解释。字段如下：\n"
    "caption: 中文短标题（不超过20字）；\n"
    "description: 一句中文描述；\n"
    "tags: 中文关键词数组，最多8个，只使用文件名/OCR 中出现的线索；\n"
    "emotion: 对象，包含 primary（情绪词）和 intensity（1-5 的整数）；\n"
    f"intents: 数组，只能从这些值里选：{', '.join(INTENT_TAXONOMY)}；\n"
    "intensity: 整体强度，1-5 的整数；\n"
    "style: 风格；\n"
    "has_text: 图中是否有文字，布尔值；\n"
    "is_base: 是否是纯底图/空白模板，布尔值；\n"
    "text_content: OCR 文字，没有就填空字符串。\n"
    "只输出 JSON。"
)


@dataclass(frozen=True, slots=True)
class ImageFile:
    """A discovered, valid, content-addressed image."""

    path: Path
    sha256: str
    size: int
    width: int
    height: int
    mime: str

    @property
    def sticker_id(self) -> str:
        return self.sha256[:16]

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()


class _RateLimiter:
    """Spaces request starts at least ``interval`` seconds apart."""

    def __init__(self) -> None:
        self._next_at = 0.0

    def wait(self, interval: float) -> None:
        if interval <= 0:
            return
        now = time.monotonic()
        if now < self._next_at:
            time.sleep(self._next_at - now)
            now = time.monotonic()
        self._next_at = now + interval


_RATE_LIMITER = _RateLimiter()


def iter_images(
    source_dir: str | Path, *, limit: int | None = None
) -> Iterator[ImageFile]:
    """Yield valid images from ``source_dir``.

    Only png/jpg/jpeg/gif/webp are considered.  Zero-byte, unreadable and
    malformed images are skipped.  Files are deduplicated by SHA-256, so two
    identical files with different names yield a single image.  Ordering is
    deterministic (case-insensitive filename).
    """

    root = Path(source_dir)
    if not root.is_dir():
        return

    effective_limit = None if limit is None or int(limit) <= 0 else int(limit)
    seen: set[str] = set()
    yielded = 0

    try:
        entries = sorted(
            root.rglob("*"),
            key=lambda p: str(p.relative_to(root)).casefold(),
        )
    except OSError:
        return

    for path in entries:
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue

        valid, width, height = _probe_image(path)
        if not valid:
            continue

        digest = sha256_file(path)
        if not digest or digest in seen:
            continue

        seen.add(digest)
        yield ImageFile(
            path=path,
            sha256=digest,
            size=size,
            width=width,
            height=height,
            mime=_mime_for(path),
        )
        yielded += 1
        if effective_limit is not None and yielded >= effective_limit:
            return


def sha256_file(path: str | Path) -> str | None:
    """Return the hex SHA-256 of a file, or ``None`` if it cannot be read."""

    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def ocr_text(path: str | Path) -> str:
    """Best-effort OCR.  Returns ``""`` unless pytesseract/tesseract exist."""

    try:
        import pytesseract  # type: ignore[import-not-found]
    except Exception:
        return ""

    image_path = str(path)
    try:
        text = pytesseract.image_to_string(image_path)
    except Exception:
        try:
            text = pytesseract.image_to_string(image_path, lang="chi_sim+eng")
        except Exception:
            return ""
    return " ".join(str(text).split())[:1000]


def describe_with_deepseek(
    llm: Any,
    path: str | Path,
    ocr_text: str = "",
    *,
    cache_path: str | Path | None = None,
    use_vision: bool = True,
    min_interval: float = DEFAULT_MIN_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Tag one image, using the LLM's vision support when possible.

    The result is cached by SHA-256 in ``tag_cache.json`` (or ``cache_path``)
    and reused on later runs, so the API is not called twice for one file.

    Returned keys include ``caption``, ``description``, ``tags``, ``emotion``,
    ``intents``, ``intensity``, ``style``, ``has_text``, ``is_base``,
    ``text_content``, ``vision_used`` and ``needs_review``.  Run accounting keys
    (``from_cache``, ``prompt_tokens``, ``completion_tokens``, ``latency_ms``,
    ``api_calls``) are included as well.
    """

    image_path = Path(path)
    digest = sha256_file(image_path) or ""
    cache_file = _resolve_cache_path(llm, cache_path)
    cache = _load_cache(cache_file)

    cached: dict[str, Any] | None = None
    if digest:
        raw_cached = cache.get(digest)
        if isinstance(raw_cached, dict):
            cached = dict(raw_cached)
            # A vision run may upgrade a cached metadata-only tag, so only
            # serve non-vision cache entries when vision is not requested.
            if not use_vision or cached.get("vision_used"):
                return _cached_result(cached)

    fallback = _fallback_result(image_path, ocr_text, vision_used=False)
    fallback["needs_review"] = True
    llm_available = _llm_available(llm)
    result: dict[str, Any] | None = None
    vision_attempted = False

    if use_vision and llm_available:
        vision_attempted = True
        data_url = _image_data_url(image_path)
        if data_url is not None:
            prompt = VISION_PROMPT
            if ocr_text.strip():
                prompt += f"\n参考OCR文字（可能不准确）：{ocr_text[:500]}"

            messages = [
                {"role": "system", "content": "你只输出合法 JSON，不要解释。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ]

            response = _chat(
                llm,
                messages,
                max_tokens=int(max_tokens),
                min_interval=min_interval,
                timeout=timeout,
                session_id=f"sticker-tag-{digest[:12] or 'unknown'}",
            )
            if response is not None:
                parsed = _parse_json_object(str(getattr(response, "text", "") or ""))
                if parsed is not None:
                    result = _normalize_payload(
                        parsed,
                        image_path,
                        ocr_text,
                        vision_used=True,
                        needs_review=not str(parsed.get("caption") or "").strip(),
                    )
                    result.update(_usage_fields(response))

    if result is None and cached is not None and not cached.get("vision_used"):
        # Vision was disabled/unavailable or failed: reuse the metadata-only
        # tag instead of paying for the same text call again.
        result = _cached_result(cached)

    if result is None and vision_attempted:
        # Vision failed (rejected, timeout, empty or non-JSON answer).  Ask
        # once more with filename + dimensions + OCR only, then mark the tag
        # for review because there is no visual evidence.
        result = _describe_with_text(
            llm,
            image_path,
            ocr_text,
            digest=digest,
            min_interval=min_interval,
            timeout=timeout,
        )

    if result is None:
        result = fallback

    result.setdefault("from_cache", False)
    result.setdefault("prompt_tokens", 0)
    result.setdefault("completion_tokens", 0)
    result.setdefault("latency_ms", 0.0)
    result.setdefault("api_calls", 0)

    # Cache successful LLM tags (vision or metadata-only).  Metadata-only
    # entries are ignored by vision runs above, so a working vision model can
    # still upgrade them later.
    if digest and int(result.get("api_calls") or 0) > 0:
        cache[digest] = {
            key: value for key, value in result.items() if key != "from_cache"
        }
        _save_cache(cache_file, cache)

    return result


def _chat(
    llm: Any,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    min_interval: float,
    timeout: float,
    session_id: str,
) -> Any:
    """Rate-limited LLM call; returns ``None`` on any failure."""

    _RATE_LIMITER.wait(float(min_interval))
    with _temporary_timeout(llm, timeout):
        try:
            return llm.chat(
                messages,
                max_tokens=int(max_tokens),
                temperature=0.2,
                session_id=session_id,
            )
        except Exception:
            return None


def _usage_fields(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    return {
        "from_cache": False,
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "latency_ms": float(getattr(response, "latency_ms", 0.0) or 0.0),
        "api_calls": 1,
    }


def _cached_result(cached: dict[str, Any]) -> dict[str, Any]:
    result = dict(cached)
    result["from_cache"] = True
    result["prompt_tokens"] = 0
    result["completion_tokens"] = 0
    result["latency_ms"] = 0.0
    result["api_calls"] = 0
    return result


def _describe_with_text(
    llm: Any,
    image_path: Path,
    ocr: str,
    *,
    digest: str,
    min_interval: float,
    timeout: float,
) -> dict[str, Any] | None:
    """Metadata-only fallback: filename + dimensions + OCR, never pixels."""

    _, width, height = _probe_image(image_path)
    prompt = (
        f"{TEXT_PROMPT}\n"
        f"文件名：{image_path.name}\n"
        f"尺寸：{width}x{height}\n"
        f"OCR文字：{ocr.strip() or '无'}"
    )
    messages = [
        {"role": "system", "content": "你只输出合法 JSON，不要解释。"},
        {"role": "user", "content": prompt},
    ]
    response = _chat(
        llm,
        messages,
        max_tokens=DEFAULT_TEXT_MAX_TOKENS,
        min_interval=min_interval,
        timeout=timeout,
        session_id=f"sticker-text-{digest[:12] or 'unknown'}",
    )
    if response is None:
        return None

    parsed = _parse_json_object(str(getattr(response, "text", "") or ""))
    if parsed is None:
        return None

    result = _normalize_payload(
        parsed,
        image_path,
        ocr,
        vision_used=False,
        needs_review=True,
    )
    # Without pixels, base/template status may only come from filename/style.
    result["is_base"] = _template_hint(image_path, result)
    result["has_text"] = bool(ocr.strip())
    if not result["text_content"] and ocr.strip():
        result["text_content"] = ocr.strip()[:500]
    note = "（未使用视觉，仅依据文件名/OCR）"
    description = str(result.get("description") or "")
    result["description"] = (description + note if description else note)[:500]
    if not result["tags"]:
        result["tags"] = _filename_tags(image_path)[:MAX_TAGS]
    if not result["intents"]:
        fallback = _fallback_result(image_path, ocr, vision_used=False)
        result["intents"] = fallback["intents"][:MAX_INTENTS]
    result.update(_usage_fields(response))
    return result


def import_stickers(
    service: StickerService,
    llm: Any,
    *,
    source_dir: str | Path = "data/stickers/incoming",
    base_dir: str | Path = "data/stickers/base",
    index_path: str | Path | None = None,
    cache_path: str | Path | None = None,
    limit: int | None = DEFAULT_LIMIT,
    apply: bool = False,
    use_vision: bool = True,
    promote_no_text: bool = False,
    min_interval: float = DEFAULT_MIN_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Tag images and (when ``apply``) import them through StickerService.

    ``source_dir`` images are considered first, followed by images already in
    ``base_dir`` (deduplicated by SHA-256).  With ``apply=False`` nothing is
    written: no DB rows, no copied base images, no index file.

    Images the model marks as base material (or clean, text-free templates) are
    copied to ``base_dir`` and get ``content["base"] = True``.  Sticker ids are
    ``sha256[:16]`` so a repeated run updates rather than duplicates rows.
    ``promote_no_text`` additionally treats every text-free image as a base
    candidate (useful for character poses kept as future meme templates).
    """

    source_root = Path(source_dir)
    base_root = Path(base_dir)
    index_file = (
        Path(index_path)
        if index_path is not None
        else _default_data_dir(service) / "stickers" / "index.json"
    )

    effective_limit = None if limit is None or int(limit) <= 0 else int(limit)
    images: list[ImageFile] = []
    seen: set[str] = set()
    for root in (source_root, base_root):
        if not root.is_dir():
            continue
        for image in iter_images(root):
            if image.sha256 in seen:
                continue
            seen.add(image.sha256)
            images.append(image)
            if effective_limit is not None and len(images) >= effective_limit:
                break
        if effective_limit is not None and len(images) >= effective_limit:
            break

    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    review: list[str] = []
    counts = {
        "scanned": len(images),
        "tagged": 0,
        "indexed": 0,
        "base": 0,
        "vision_used": 0,
        "needs_review": 0,
        "already_indexed": 0,
        "cache_hits": 0,
        "api_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 0.0,
    }

    for image in images:
        try:
            text = ocr_text(image.path)
            result = describe_with_deepseek(
                llm,
                image.path,
                text,
                cache_path=cache_path,
                use_vision=use_vision,
                min_interval=min_interval,
                timeout=timeout,
            )
        except Exception as exc:  # one bad image must not abort the run
            errors.append({"file": str(image.path), "error": f"{type(exc).__name__}: {exc}"})
            continue

        counts["tagged"] += 1
        if result.get("vision_used"):
            counts["vision_used"] += 1
        if result.get("from_cache"):
            counts["cache_hits"] += 1
        counts["api_calls"] += int(result.get("api_calls", 0) or 0)
        counts["prompt_tokens"] += int(result.get("prompt_tokens", 0) or 0)
        counts["completion_tokens"] += int(result.get("completion_tokens", 0) or 0)
        counts["latency_ms"] += float(result.get("latency_ms", 0.0) or 0.0)

        in_base_dir = _is_relative_to(image.path, base_root)
        is_base = bool(result.get("is_base")) or in_base_dir
        if not is_base and not result.get("has_text"):
            is_base = bool(promote_no_text) or _clean_template_hint(
                image.path, result
            )

        file_path = image.path
        if is_base and not in_base_dir:
            destination = _base_destination(base_root, image)
            file_path = destination
            counts["base"] += 1
            if apply:
                try:
                    base_root.mkdir(parents=True, exist_ok=True)
                    if not destination.exists() or sha256_file(destination) != image.sha256:
                        shutil.copy2(image.path, destination)
                except OSError as exc:
                    errors.append(
                        {
                            "file": str(image.path),
                            "error": f"base copy failed: {type(exc).__name__}: {exc}",
                        }
                    )
                    continue
        elif is_base:
            counts["base"] += 1

        sticker_id = image.sticker_id
        existing = service.get(sticker_id)
        if existing is not None:
            counts["already_indexed"] += 1

        content: dict[str, Any] = {
            "sha256": image.sha256,
            "source_path": str(image.path),
            "vision_used": bool(result.get("vision_used")),
            "has_text": bool(result.get("has_text")),
            "is_base": bool(is_base),
            "description": str(result.get("description") or ""),
        }
        if is_base:
            content["base"] = True

        if apply:
            service.add_sticker(
                str(file_path),
                caption=str(result.get("caption") or image.path.stem)[:80],
                tags=list(result.get("tags") or []),
                emotion=dict(result.get("emotion") or {}),
                intents=list(result.get("intents") or []),
                intensity=float(result.get("intensity", 2.0)),
                style=str(result.get("style") or ""),
                source="imported",
                content=content,
                sticker_id=sticker_id,
            )
            counts["indexed"] += 1

        entry = {
            "id": sticker_id,
            "file": str(file_path),
            "caption": str(result.get("caption") or ""),
            "tags": list(result.get("tags") or []),
            "intents": list(result.get("intents") or []),
            "has_text": bool(result.get("has_text")),
            "is_base": bool(is_base),
            "vision_used": bool(result.get("vision_used")),
            "needs_review": bool(result.get("needs_review")),
            "sha256": image.sha256,
        }
        entries.append(entry)
        if entry["needs_review"]:
            review.append(entry["file"])

    counts["needs_review"] = len(review)
    counts["latency_ms"] = round(counts["latency_ms"], 1)

    total_entries = 0
    if apply and entries:
        merged = _merge_index(index_file, entries)
        _write_index(
            index_file,
            merged,
            source_root=source_root,
            base_root=base_root,
            model=str(getattr(llm, "model", "") or ""),
            counts=counts,
        )
        total_entries = len(merged)

    return {
        "mode": "apply" if apply else "dry-run",
        "source_dir": str(source_root),
        "base_dir": str(base_root),
        "index_path": str(index_file),
        "limit": effective_limit,
        "vision_requested": bool(use_vision),
        "counts": counts,
        "total_index_entries": total_entries,
        "review": review,
        "errors": errors,
        "images": entries,
    }


def _probe_image(path: Path) -> tuple[bool, int, int]:
    """Validate an image and return ``(ok, width, height)``.

    Uses Pillow when present for a deep check; otherwise validates the magic
    bytes only.  This keeps the importer usable without extra dependencies.
    """

    image_module = _pil_image()
    if image_module is not None:
        try:
            with image_module.open(path) as img:
                img.verify()
            with image_module.open(path) as img:
                width, height = img.size
            return True, int(width), int(height)
        except Exception:
            return False, 0, 0

    try:
        header = Path(path).read_bytes()[:16]
    except OSError:
        return False, 0, 0
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return True, 0, 0
    if header.startswith(b"\xff\xd8\xff"):
        return True, 0, 0
    if header.startswith((b"GIF87a", b"GIF89a")):
        return True, 0, 0
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return True, 0, 0
    return False, 0, 0


def _pil_image() -> Any:
    try:
        from PIL import Image  # type: ignore[import-not-found]

        return Image
    except Exception:
        return None


def _mime_for(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


def _image_data_url(path: Path) -> str | None:
    """Return a data URL, downscaling only when the file is very large."""

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None

    mime = _mime_for(path)
    if len(raw) > MAX_INLINE_IMAGE_BYTES:
        image_module = _pil_image()
        if image_module is None:
            return None
        try:
            with image_module.open(io.BytesIO(raw)) as img:
                img.seek(0)
                frame = img.convert("RGB") if img.mode not in {"RGB", "L"} else img.copy()
                frame.thumbnail((1024, 1024))
                buffer = io.BytesIO()
                frame.save(buffer, format="JPEG", quality=85)
                raw = buffer.getvalue()
                mime = "image/jpeg"
        except Exception:
            return None

    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


@contextmanager
def _temporary_timeout(llm: Any, timeout: float) -> Iterator[None]:
    """Clamp the LLM client's per-request timeout, then restore it."""

    original = getattr(llm, "timeout", None)
    if original is None:
        yield
        return
    try:
        llm.timeout = min(float(original), float(timeout))
    except (TypeError, ValueError):
        yield
        return
    try:
        yield
    finally:
        llm.timeout = original


def _llm_available(llm: Any) -> bool:
    if llm is None:
        return False
    available = getattr(llm, "available", True)
    try:
        return bool(available)
    except Exception:
        return True


def _resolve_cache_path(llm: Any, cache_path: str | Path | None) -> Path:
    if cache_path is not None:
        return Path(cache_path)
    return _default_data_dir(llm) / "stickers" / "tag_cache.json"


def _default_data_dir(source: Any) -> Path:
    config = getattr(source, "config", None)
    if config is not None:
        try:
            return Path(config.data_dir)
        except Exception:
            pass
    return Path(__file__).resolve().parents[2] / "data"


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def _save_cache(path: Path, cache: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError:
        pass


def _parse_json_object(text: str) -> dict[str, Any] | None:
    value = (text or "").strip()
    if not value:
        return None
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else value
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(value[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_payload(
    raw: dict[str, Any],
    path: Path,
    ocr: str,
    *,
    vision_used: bool,
    needs_review: bool,
) -> dict[str, Any]:
    caption = str(raw.get("caption") or "").strip()[:80]
    if not caption:
        caption = _humanize_stem(path)[:40]

    description = str(raw.get("description") or "").strip()[:500]

    raw_tags = raw.get("tags")
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for item in raw_tags:
            text = str(item).strip()[:24]
            if text and text not in tags:
                tags.append(text)
            if len(tags) >= MAX_TAGS:
                break

    raw_emotion = raw.get("emotion")
    emotion_raw: dict[str, Any] = raw_emotion if isinstance(raw_emotion, dict) else {}
    primary = str(emotion_raw.get("primary") or "").strip()[:30]
    emotion_intensity = _clamp_int(emotion_raw.get("intensity"), 1, 5, 3 if primary else 2)

    raw_intents = raw.get("intents")
    intents: list[str] = []
    if isinstance(raw_intents, list):
        for item in raw_intents:
            text = str(item).strip().lower()
            if text in INTENT_KEYWORDS and text not in intents:
                intents.append(text)
            if len(intents) >= MAX_INTENTS:
                break

    has_text = _as_bool(raw.get("has_text")) or bool(ocr.strip())
    text_content = str(raw.get("text_content") or ocr or "").strip()[:500]

    return {
        "caption": caption,
        "description": description,
        "tags": tags,
        "emotion": {"primary": primary, "intensity": emotion_intensity},
        "intents": intents,
        "intensity": _clamp_int(raw.get("intensity"), 1, 5, emotion_intensity),
        "style": str(raw.get("style") or "").strip()[:40],
        "has_text": has_text,
        "is_base": _as_bool(raw.get("is_base")),
        "text_content": text_content,
        "vision_used": bool(vision_used),
        "needs_review": bool(needs_review),
    }


def _fallback_result(path: Path, ocr: str, *, vision_used: bool) -> dict[str, Any]:
    """Conservative, filename/OCR-only result used when vision is unavailable."""

    stem_tags = _filename_tags(path)
    lookup = f"{path.stem} {ocr}".lower()

    intents: list[str] = []
    for intent in INTENT_TAXONOMY:
        if intent in lookup:
            intents.append(intent)
    for intent, words in INTENT_KEYWORDS.items():
        if intent not in intents and any(word in f"{path.stem} {ocr}" for word in words):
            intents.append(intent)
        if len(intents) >= MAX_INTENTS:
            break

    caption = ""
    if ocr.strip():
        caption = " ".join(ocr.split())[:30]
    if not caption:
        caption = _humanize_stem(path)[:40]

    return {
        "caption": caption,
        "description": "未使用视觉模型，仅依据文件名/OCR 生成，建议人工复核。",
        "tags": stem_tags[:MAX_TAGS],
        "emotion": {"primary": "", "intensity": 2},
        "intents": intents[:MAX_INTENTS],
        "intensity": 2,
        "style": "unknown",
        "has_text": bool(ocr.strip()),
        "is_base": _template_hint(path, {}),
        "text_content": ocr[:500],
        "vision_used": bool(vision_used),
        "needs_review": True,
    }


def _filename_tags(path: Path) -> list[str]:
    tokens: list[str] = []
    for token in re.split(r"[^0-9A-Za-z一-鿿]+", path.stem):
        text = token.strip().lower()
        if not text or text.isdigit():
            continue
        if text in {path.suffix.lower().lstrip("."), "img", "image"}:
            continue
        if text not in tokens:
            tokens.append(text)
        if len(tokens) >= MAX_TAGS:
            break
    return tokens


def _humanize_stem(path: Path) -> str:
    text = re.sub(r"[_\-.]+", " ", path.stem).strip()
    return text or path.name


def _clean_template_hint(path: Path, result: dict[str, Any]) -> bool:
    """A clean template if the name/style says so; never inferred from pixels."""

    return _template_hint(path, result)


def _template_hint(path: Path, result: dict[str, Any]) -> bool:
    haystack = f"{path.stem} {result.get('style', '')}".lower()
    return any(hint in haystack for hint in _TEMPLATE_HINTS)


def _base_destination(base_dir: Path, image: ImageFile) -> Path:
    destination = base_dir / image.path.name
    if destination.exists() and sha256_file(destination) != image.sha256:
        destination = base_dir / f"{image.path.stem}-{image.sha256[:8]}{image.path.suffix}"
    return destination


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _merge_index(index_file: Path, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    try:
        existing = json.loads(index_file.read_text(encoding="utf-8"))
        if isinstance(existing, dict) and isinstance(existing.get("images"), list):
            for item in existing["images"]:
                if isinstance(item, dict) and item.get("id"):
                    merged[str(item["id"])] = item
    except (OSError, json.JSONDecodeError):
        pass
    for entry in entries:
        merged[str(entry["id"])] = entry
    return sorted(merged.values(), key=lambda item: str(item.get("file", "")))


def _write_index(
    index_file: Path,
    entries: list[dict[str, Any]],
    *,
    source_root: Path,
    base_root: Path,
    model: str,
    counts: dict[str, Any],
) -> None:
    payload = {
        "generated_at": time.time(),
        "generated_at_iso": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source_root),
        "base_dir": str(base_root),
        "model": model,
        "total": len(entries),
        "counts": dict(counts),
        "images": entries,
    }
    index_file.parent.mkdir(parents=True, exist_ok=True)
    temp = index_file.with_name(index_file.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp, index_file)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是", "有"}
    return False


def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
