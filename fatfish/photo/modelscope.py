"""ModelScope API-Inference image generation for ``[photo] mode = "modelscope"``.

The app itself stays dependency-free: this is a small stdlib (urllib) client for
the OpenAI-compatible async image endpoint.  ModelScope accepts a LoRA repo id
directly in ``model`` (it loads the LoRA on top of its declared base model), so
``sunny1155665/whalegirl-moss-v2`` is all the photo backend needs to render the
whale-girl character in the cloud.

Flow (verified 2026-11-12):
1. ``POST /v1/images/generations`` with ``X-ModelScope-Async-Mode: true`` -> task_id
2. ``GET /v1/tasks/{task_id}`` with ``X-ModelScope-Task-Type: image_generation``
3. On ``SUCCEED`` download the first URL in ``output_images``

The submit response itself may already say ``SUCCEED`` while the task is still
queued; always poll the task id and never trust the submit status.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .. import __version__

DEFAULT_BASE_URL = "https://api-inference.modelscope.cn/v1"
DEFAULT_MODEL = "sunny1155665/whalegirl-moss-v2"
DEFAULT_TIMEOUT = 300.0
DEFAULT_POLL_SECONDS = 3.0
DEFAULT_SIZE = "1024x1024"
_MIN_IMAGE_BYTES = 64

_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
)


class ModelScopePhotoError(RuntimeError):
    """A failure already mapped to a user-facing ``friendly_photo_error`` code."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    @property
    def error_text(self) -> str:
        return f"{self.code}:{self.detail}" if self.detail else self.code


def _looks_like_image(body: bytes) -> bool:
    if len(body) < _MIN_IMAGE_BYTES:
        return False
    if any(body.startswith(magic) for magic in _IMAGE_MAGIC):
        return True
    # WebP: RIFF....WEBP
    return len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP"


def _extract_images(data: dict[str, Any]) -> list[str]:
    """ModelScope has used both ``output_images`` and nested ``outputs.images``."""
    raw = data.get("output_images")
    urls: list[str] = []
    if isinstance(raw, str):
        urls = [raw]
    elif isinstance(raw, list):
        urls = [str(item) for item in raw if isinstance(item, str)]
    if not urls:
        outputs = data.get("outputs")
        if isinstance(outputs, dict):
            nested = outputs.get("images") or []
            for item in nested:
                if isinstance(item, str):
                    urls.append(item)
                elif isinstance(item, dict) and item.get("url"):
                    urls.append(str(item["url"]))
    return [url for url in urls if url]


class ModelScopePhotoClient:
    """One async image generation through ModelScope API-Inference."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        size: str = DEFAULT_SIZE,
        extra_params: dict[str, Any] | None = None,
    ):
        self.base_url = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or DEFAULT_MODEL).strip()
        self.timeout = max(10.0, float(timeout))
        self.poll_seconds = max(0.5, float(poll_seconds))
        self.size = str(size or DEFAULT_SIZE).strip()
        self.extra_params = dict(extra_params or {})

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    # -- request helpers -------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
            "user-agent": f"fatfish/{__version__}",
        }
        headers.update(extra or {})
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        auth: bool = True,
    ) -> bytes:
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=self._headers(headers) if auth else {"user-agent": f"fatfish/{__version__}"},
        )
        timeout_s = float(self.timeout if timeout is None else timeout)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            self._raise_for_status(int(exc.code), detail)
            raise ModelScopePhotoError("modelscope_failed", f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelScopePhotoError(
                "modelscope_timeout", type(exc).__name__
            ) from exc

    @staticmethod
    def _raise_for_status(status: int, detail: str) -> None:
        if status in (401, 403):
            raise ModelScopePhotoError("modelscope_unauthorized", detail)
        if status == 429:
            raise ModelScopePhotoError("modelscope_rate_limited", detail)
        lowered = (detail or "").lower()
        if status == 400 and "no provider supported" in lowered:
            raise ModelScopePhotoError("modelscope_model_unavailable", detail)
        if status >= 400:
            raise ModelScopePhotoError("modelscope_failed", f"HTTP {status}: {detail}")

    def _json(self, url: str, *, headers: dict[str, str] | None = None,
              timeout: float | None = None) -> dict[str, Any]:
        raw = self._request("GET", url, headers=headers, timeout=timeout)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelScopePhotoError("modelscope_failed", "bad_json") from exc
        if not isinstance(data, dict):
            raise ModelScopePhotoError("modelscope_failed", "bad_json")
        return data

    # -- public API ------------------------------------------------------
    def build_payload(
        self,
        positive: str,
        negative: str = "",
        *,
        seed: int | None = None,
        width: int = 1024,
        height: int = 1024,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": str(positive or ""),
            "n": 1,
            "size": self.size or f"{int(width)}x{int(height)}",
        }
        if negative:
            payload["negative_prompt"] = str(negative)
        if seed is not None:
            payload["seed"] = int(seed)
        for key, value in self.extra_params.items():
            payload.setdefault(str(key), value)
        return payload

    def submit(self, payload: dict[str, Any]) -> str:
        raw = self._request(
            "POST",
            f"{self.base_url}/images/generations",
            payload=payload,
            headers={"x-modelscope-async-mode": "true"},
            timeout=min(self.timeout, 60.0),
        )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelScopePhotoError("modelscope_failed", "bad_json") from exc
        if not isinstance(data, dict):
            raise ModelScopePhotoError("modelscope_failed", "bad_json")
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise ModelScopePhotoError(
                "modelscope_failed",
                str(data.get("errors") or data.get("message") or "no_task_id")[:300],
            )
        return task_id

    def poll(self, task_id: str) -> dict[str, Any]:
        """One non-blocking task poll.

        Returns ``{"status": "pending"}`` while queued/running and
        ``{"status": "succeeded", "url": "..."}`` on success.  A platform
        failure raises :class:`ModelScopePhotoError` so the caller can mark the
        job failed without a second network request.
        """
        data = self._json(
            f"{self.base_url}/tasks/{task_id}",
            headers={"x-modelscope-task-type": "image_generation"},
            timeout=min(self.timeout, 60.0),
        )
        status = str(data.get("task_status") or "").upper()
        if status == "SUCCEED":
            images = _extract_images(data)
            if not images:
                raise ModelScopePhotoError(
                    "modelscope_no_image",
                    json.dumps(data.get("outputs"), ensure_ascii=False)[:300],
                )
            return {"status": "succeeded", "url": images[0]}
        if status in {"FAILED", "CANCELED"}:
            detail = data.get("errors") or data.get("message") or status
            raise ModelScopePhotoError(
                "modelscope_failed",
                json.dumps(detail, ensure_ascii=False)[:300],
            )
        return {"status": "pending"}

    def wait(self, task_id: str) -> str:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            result = self.poll(task_id)
            if result.get("status") == "succeeded":
                return str(result["url"])
            time.sleep(self.poll_seconds)
        raise ModelScopePhotoError("modelscope_timeout", "task_poll")

    def download(self, url: str) -> bytes:
        # The image lives on Alibaba OSS: never leak the API token to another
        # host, so this GET goes out with no Authorization header.
        raw = self._request("GET", url, timeout=min(self.timeout, 120.0), auth=False)
        if not _looks_like_image(raw):
            raise ModelScopePhotoError(
                "modelscope_bad_image", f"{len(raw)} bytes"
            )
        return raw

    def generate(
        self,
        positive: str,
        negative: str = "",
        *,
        seed: int | None = None,
        width: int = 1024,
        height: int = 1024,
    ) -> bytes:
        if not self.available:
            raise ModelScopePhotoError("modelscope_not_configured")
        payload = self.build_payload(
            positive, negative, seed=seed, width=width, height=height
        )
        task_id = self.submit(payload)
        url = self.wait(task_id)
        return self.download(url)
