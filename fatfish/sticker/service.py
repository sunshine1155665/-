"""Sticker generation: one ``send_sticker`` action -> one ModelScope LoRA job.

The library/retrieval era is over.  Stickers are no longer selected from a
local collection with vector search; every request is rendered fresh by the
``whalegirl-sticker-v1`` LoRA on ModelScope's turbo base.  The submit is
synchronous and fast; polling and download happen in :meth:`poll_jobs`, driven
by ``Runtime.tick``, and the finished PNG is queued as an image message so the
current reply never waits for the render.

Job rows live in ``sticker_jobs`` only.  The generation prompt never enters
conversation turns, memory or RAG -- same isolation rule as ``photo_jobs``.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..db import Database
from ..photo.modelscope import (
    DEFAULT_BASE_URL,
    ModelScopePhotoClient,
    ModelScopePhotoError,
)

# Fixed anchor: identity + sticker style.  The variable part is the model's
# natural-language need, appended after it.  Keep this in sync with the LoRA
# training captions (work/lora-sticker-train, trigger whalegirl_sticker).
ANCHOR = (
    "chibi, 表情包贴纸，厚白色描边，扁平明亮配色，纯色背景，"
    "蓝色渐变长卷发少女，蓝眼睛，呆毛，鲸鱼耳鳍，白色女仆发饰，蓝色蝴蝶结，"
    "深蓝色女仆裙，白色围裙绣鲸鱼，身后一条鲸鱼尾巴，单人"
)
DEFAULT_MODEL = "Qwen/Qwen-Image-2.1-Turbo"
DEFAULT_TRIGGER = "whalegirl_sticker"
MAX_NEED = 200
# Prompt length guard.  ModelScope caps prompts at 2000 chars; the anchor plus
# a long need is nowhere near it, but a runaway model rewrite could be.
MAX_PROMPT = 1500


@dataclass(slots=True)
class Sticker:
    """Legacy ``stickers`` row, kept only for adapter lookups.

    The QQ adapter can still resolve an id against the old table (rows imported
    before the switch); new generations never write there.
    """

    id: str
    file_path: str
    caption: str
    source: str = "library"


@dataclass(slots=True)
class StickerDecision:
    """Outcome of one ``send_sticker`` action.

    ``pending`` means a generation job was accepted; delivery happens later
    through the outbox.  ``send`` stays False because nothing is sent in-turn
    any more -- it exists so old callers can keep reading the field without a
    KeyError.
    """

    pending: bool
    reason: str
    job_id: str | None = None
    caption: str = ""
    send: bool = False

    @property
    def generated(self) -> bool:
        return self.pending


class StickerService:
    def __init__(
        self,
        db: Database,
        config: Config,
        client: Any | None = None,
    ):
        self.db = db
        self.config = config
        self.enabled = bool(config.get("sticker.generation_enabled", True))
        self.model = str(config.get("sticker.generation_model") or DEFAULT_MODEL).strip()
        raw_loras = config.get("sticker.generation_loras", []) or []
        if isinstance(raw_loras, str):
            raw_loras = [raw_loras]
        self.loras = [str(item).strip() for item in raw_loras if str(item).strip()]
        self.trigger = str(
            config.get("sticker.generation_trigger") or DEFAULT_TRIGGER
        ).strip()
        self.negative = str(config.get("sticker.generation_negative") or "").strip()
        self.steps = max(1, min(100, int(config.get("sticker.generation_steps", 8))))
        self.guidance = float(config.get("sticker.generation_guidance", 1.0))
        self.size = str(config.get("sticker.generation_size") or "1024x1024").strip()
        self.max_per_10m = max(
            0, int(config.get("sticker.generation_max_per_10m", 3))
        )
        self.max_pending = max(
            1, int(config.get("sticker.generation_max_pending", 1))
        )
        self.timeout = max(
            30.0, float(config.get("sticker.modelscope_timeout_seconds", 600.0))
        )
        self.poll_seconds = max(
            0.5, float(config.get("sticker.modelscope_poll_seconds", 3.0))
        )
        base_url = str(
            config.get("sticker.modelscope_base_url") or DEFAULT_BASE_URL
        ).strip()
        key_env = str(
            config.get("sticker.modelscope_api_key_env") or "MODELSCOPE_API_KEY"
        ).strip()
        api_key = os.environ.get(key_env, "").strip()
        self.output_dir = config.data_dir / "stickers" / "generated"
        if client is not None:
            self.client = client
        elif api_key and self.model and self.loras:
            self.client = ModelScopePhotoClient(
                base_url=base_url,
                api_key=api_key,
                model=self.model,
                timeout=self.timeout,
                poll_seconds=self.poll_seconds,
                size=self.size,
                extra_params={
                    "loras": list(self.loras),
                    "steps": self.steps,
                    "guidance": self.guidance,
                },
            )
        else:
            self.client = None

    # ------------------------------------------------------------------ config
    @property
    def available(self) -> bool:
        return bool(
            self.enabled
            and self.client is not None
            and getattr(self.client, "available", True)
            and self.model
        )

    def build_prompt(self, need: str) -> str:
        """Anchor + the model's need; the trigger word appears exactly once."""
        text = str(need or "").strip()[:MAX_NEED]
        prompt = f"{self.trigger}, {ANCHOR}"
        if text:
            prompt = f"{prompt}，{text}"
        return prompt[:MAX_PROMPT]

    # ------------------------------------------------------------------ limits
    def _count(self, sql: str, params: tuple[Any, ...]) -> int:
        try:
            row = self.db.query_one(sql, params)
        except Exception:  # noqa: BLE001 - a missing table must not kill a turn
            return 0
        if row is None:
            return 0
        try:
            return int(row["n"])
        except (KeyError, TypeError, ValueError):
            return 0

    def _recent_count(self, now: float) -> int:
        if self.max_per_10m <= 0:
            return 0
        return self._count(
            "SELECT COUNT(*) AS n FROM sticker_jobs WHERE created_at >= ?",
            (now - 600.0,),
        )

    def _pending_count(self) -> int:
        return self._count(
            "SELECT COUNT(*) AS n FROM sticker_jobs "
            "WHERE status IN ('waiting','running')",
            (),
        )

    # ------------------------------------------------------------------ submit
    def decide(
        self,
        need: str,
        *,
        session_id: str | None = None,
        force: bool = False,
        user_text: str | None = None,
        context_text: str | None = None,
        intensity: float | None = None,
        now: float | None = None,
    ) -> StickerDecision:
        """Submit one generation job (or refuse).  Never blocks on the render.

        ``force``/``user_text``/``context_text``/``intensity`` are accepted for
        call-site compatibility; generation no longer ranks candidates, so they
        do not change the prompt.  The model's ``need`` is already its own
        rewrite and is the best description we have.
        """
        if not self.available:
            return StickerDecision(False, "generation_unavailable")
        now = time.time() if now is None else now
        if self._pending_count() >= self.max_pending:
            return StickerDecision(False, "busy")
        if self.max_per_10m and self._recent_count(now) >= self.max_per_10m:
            return StickerDecision(False, "rate_limited")
        caption = str(need or "").strip()[:MAX_NEED]
        if not caption:
            return StickerDecision(False, "missing_need")
        prompt = self.build_prompt(caption)
        job_id = uuid.uuid4().hex[:16]
        try:
            payload = self.client.build_payload(
                prompt,
                self.negative,
                seed=None,
                width=_side(self.size),
                height=_side(self.size),
            )
            task_id = self.client.submit(payload)
        except ModelScopePhotoError as exc:
            return StickerDecision(False, f"generation_failed:{exc.error_text}")
        except Exception as exc:  # noqa: BLE001 - one bad submit must not kill a turn
            return StickerDecision(
                False, f"generation_failed:{type(exc).__name__}"
            )
        self.db.execute(
            "INSERT INTO sticker_jobs(id, session_id, created_at, status, "
            "provider, task_id, request_text, updated_at, prompt, caption) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                str(session_id or "")[:120],
                now,
                "waiting",
                "modelscope",
                task_id,
                caption,
                now,
                prompt,
                caption,
            ),
        )
        return StickerDecision(True, "generation_queued", job_id=job_id, caption=caption)

    # ------------------------------------------------------------------ poll
    def poll_jobs(
        self,
        *,
        now: float | None = None,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Poll due jobs once; download successes and mark failures."""
        now = time.time() if now is None else now
        if not self.available:
            return []
        try:
            rows = self.db.query(
                "SELECT id, session_id, created_at, task_id, caption "
                "FROM sticker_jobs WHERE status IN ('waiting','running') "
                "ORDER BY created_at LIMIT ?",
                (max(1, int(limit)),),
            )
        except Exception:  # noqa: BLE001
            return []
        completed: list[dict[str, Any]] = []
        for row in rows:
            job_id = str(row["id"])
            session_id = str(row["session_id"] or "")
            task_id = str(row["task_id"] or "").strip()
            created_at = float(row["created_at"] or now)
            if not task_id:
                self._finish(job_id, "failed", error="missing_task_id")
                completed.append(
                    {"ok": False, "job_id": job_id, "session_id": session_id,
                     "error": "missing_task_id"}
                )
                continue
            if now - created_at > self.timeout + 30.0:
                self._finish(job_id, "failed", error="modelscope_timeout")
                completed.append(
                    {"ok": False, "job_id": job_id, "session_id": session_id,
                     "error": "modelscope_timeout"}
                )
                continue
            try:
                result = self.client.poll(task_id)
                if result.get("status") != "succeeded":
                    self.db.execute(
                        "UPDATE sticker_jobs SET updated_at=? WHERE id=?",
                        (now, job_id),
                    )
                    continue
                url = str(result.get("url") or "").strip()
                if not url:
                    raise ModelScopePhotoError(
                        "modelscope_no_image", "task succeeded without url"
                    )
                raw = self.client.download(url)
                self.output_dir.mkdir(parents=True, exist_ok=True)
                target = self.output_dir / f"{job_id}{_image_extension(raw)}"
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(raw)
                os.replace(tmp, target)
                self._finish(job_id, "done", image_path=str(target), now=now)
                completed.append(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "session_id": session_id,
                        "path": str(target),
                        "caption": str(row["caption"] or ""),
                    }
                )
            except ModelScopePhotoError as exc:
                self._finish(job_id, "failed", error=exc.error_text, now=now)
                completed.append(
                    {"ok": False, "job_id": job_id, "session_id": session_id,
                     "error": exc.error_text}
                )
            except Exception as exc:  # noqa: BLE001
                detail = f"{type(exc).__name__}:{str(exc)[:160]}"
                self._finish(job_id, "failed", error=detail, now=now)
                completed.append(
                    {"ok": False, "job_id": job_id, "session_id": session_id,
                     "error": detail}
                )
        return completed

    def _finish(
        self, job_id: str, status: str, *, error: str = "", image_path: str = "",
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        self.db.execute(
            "UPDATE sticker_jobs SET status=?, error=?, image_path=?, "
            "updated_at=?, finished_at=? WHERE id=?",
            (
                status,
                error or None,
                image_path or None,
                now,
                now if status in ("done", "failed") else None,
                job_id,
            ),
        )

    # ------------------------------------------------------------------ views
    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            rows = self.db.query(
                "SELECT id, session_id, created_at, finished_at, status, "
                "task_id, request_text, image_path, error FROM sticker_jobs "
                "ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            )
        except Exception:  # noqa: BLE001
            return []
        return [dict(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            row = self.db.query_one(
                "SELECT * FROM sticker_jobs WHERE id=?", (str(job_id),)
            )
        except Exception:  # noqa: BLE001
            return None
        return dict(row) if row else None

    # ------------------------------------------------------- legacy lookups
    def get(self, sticker_id: str) -> Sticker | None:
        """Resolve an old library row (adapter compatibility only)."""
        try:
            row = self.db.query_one(
                "SELECT id, file_path, caption, source FROM stickers WHERE id=?",
                (str(sticker_id),),
            )
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        return Sticker(
            id=str(row["id"]),
            file_path=str(row["file_path"] or ""),
            caption=str(row["caption"] or ""),
            source=str(row["source"] or ""),
        )

    def add_sticker(
        self,
        file_path: str,
        *,
        caption: str = "",
        sticker_id: str | None = None,
        source: str = "library",
        **_: Any,
    ) -> Sticker:
        """Seed a legacy row (tests/adapters).  No embedding is computed.

        Kept intentionally tiny: nothing in the runtime writes here any more.
        """
        sid = sticker_id or uuid.uuid4().hex
        now = time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO stickers(id, file_path, caption, tags_json, "
            "emotion_json, identity_json, content_json, intent_json, intensity, "
            "style, source, usage_count, embedding_text, active, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL,1,?,?)",
            (
                sid,
                str(file_path),
                caption,
                "[]",
                "{}",
                "{}",
                "{}",
                "[]",
                2.0,
                "",
                source,
                now,
                now,
            ),
        )
        return Sticker(id=sid, file_path=str(file_path), caption=caption, source=source)


def _side(size: str) -> int:
    """``"1024x1024"`` -> 1024 (payload width/height are informational)."""
    try:
        return int(str(size).lower().split("x", 1)[0])
    except (TypeError, ValueError):
        return 1024


def _image_extension(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".png"
