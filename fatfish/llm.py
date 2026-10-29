"""OpenAI-compatible LLM client with OpenCode Go header adaptation.

OpenCode Go endpoints (official docs):
  OpenAI Chat Completions: https://opencode.ai/inference/go/openai/v1/chat/completions
  OpenAI Responses:        https://opencode.ai/inference/go/openai/v1/responses
  Anthropic Messages:      https://opencode.ai/inference/go/anthropic/v1/messages
  Models:                  https://opencode.ai/inference/go/v1/models

Required / recommended headers:
  Authorization: Bearer $FATFISH_OPENCODE_KEY (falls back to OPENCODE_API_KEY)
  User-Agent:    fatfish/0.1.0
  x-opencode-session: stable per conversation

The client intentionally implements the OpenAI-compatible paths only. Some
models (GPT/Grok) use the Responses API; set api_style = "responses" for those.
Anthropic/Google-native models use different endpoints and are not handled here.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

try:  # optional: keep-alive is a latency optimization, not a requirement
    import urllib3
except ImportError:  # pragma: no cover - exercised only in minimal installs
    urllib3 = None  # type: ignore[assignment]

from . import __version__
from .config import Config, env_secret
from .db import Database

_SESSION_SAFE = re.compile(r"[^a-zA-Z0-9._:-]+")
_REASONING_EFFORTS = {"low", "medium", "high"}

# ``urllib.request`` opens a fresh TCP+TLS connection per call.  A Runtime is
# created for every chat turn, so an instance-level pool would die with it;
# urllib3's PoolManager is process-wide and thread-safe.  It is optional: the
# core stays dependency-free and falls back to urllib when it is not installed
# or when a system proxy is configured (urllib handles proxies for us).
_POOL: Any = None
_POOL_LOCK = threading.Lock()


def _http_pool() -> Any:
    global _POOL
    if urllib3 is None:
        return None
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = urllib3.PoolManager(num_pools=8, maxsize=4, block=False)
        return _POOL


def _proxies_configured() -> bool:
    try:
        return bool(urllib.request.getproxies())
    except Exception:
        return False


def normalize_reasoning_effort(value: Any) -> str:
    """Normalize ``llm.reasoning_effort`` to low/medium/high or "".

    Empty string and "off" disable the field; unknown values fall back to
    "low" so a typo never drops the reasoning hint silently.
    """
    text = str(value if value is not None else "").strip().lower()
    if not text or text == "off":
        return ""
    return text if text in _REASONING_EFFORTS else "low"


def vision_content(text: str, images: list[str]) -> list[dict[str, Any]]:
    """OpenAI-compatible multimodal ``content`` array (text first, then images).

    ``images`` are data URLs (``data:image/jpeg;base64,...``). Only callers
    that actually hold image bytes build this shape; the pure-text path keeps
    sending a plain string so prompt caching and existing byte-for-byte
    request bodies are untouched.
    """
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": str(text)})
    for url in images or []:
        parts.append({"type": "image_url", "image_url": {"url": str(url)}})
    return parts


@dataclass(slots=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True)
class LLMResult:
    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: float = 0.0
    model: str = ""
    raw_usage: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.provider = str(config.get("llm.provider", "opencode_go")).strip().lower()
        self.base_url = str(
            config.get("llm.base_url", "https://opencode.ai/inference/go/openai/v1")
        ).rstrip("/")
        self.model = str(config.get("llm.model", "deepseek-v4.1-flash"))
        self.api_style = str(config.get("llm.api_style", "chat_completions")).strip().lower()
        self.api_key_env = str(config.get("llm.api_key_env", "OPENCODE_API_KEY"))
        self.api_key = env_secret(self.api_key_env)
        # Backward compatibility: older installs only exported OPENCODE_API_KEY.
        if not self.api_key and self.api_key_env != "OPENCODE_API_KEY":
            self.api_key = env_secret("OPENCODE_API_KEY")
        # Single-sourced from the package version: pinning a literal here is
        # how low ended up on 0.2.0 while standard/remote drifted to 0.1.0.
        self.user_agent = str(
            config.get("llm.user_agent") or f"fatfish/{__version__}"
        )
        self.org_id = config.get("llm.org_id") or None
        self.extra_headers = config.get("llm.headers", {}) or {}
        self.send_session_header = bool(config.get("llm.send_session_header", True))
        self.timeout = float(config.get("llm.timeout_seconds", 90))
        self.enabled = bool(config.get("llm.enabled", True))
        self.keepalive = bool(config.get("llm.keepalive", True))

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.api_key and self.base_url)

    def headers(self, session_id: str | None = None) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
            "user-agent": self.user_agent,
        }
        if self.send_session_header and session_id:
            safe = _SESSION_SAFE.sub("-", str(session_id)).strip("-")
            if safe:
                headers["x-opencode-session"] = safe[:128]
        if self.org_id:
            headers["x-opencode-org-id"] = str(self.org_id)
        for key, value in dict(self.extra_headers).items():
            headers[str(key).strip().lower()] = str(value)
        return headers

    def build_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Build the request URL and body. ``messages`` content is a plain
        string for text turns, or an OpenAI multimodal array (see
        :func:`vision_content`) when the current user message carries images;
        both are serialized as-is."""
        max_tokens = int(max_tokens or self.config.get("llm.max_output_tokens", 800))
        temperature = float(
            self.config.get("llm.temperature", 0.8) if temperature is None else temperature
        )
        effort = normalize_reasoning_effort(
            self.config.get("llm.reasoning_effort", "low")
        )
        if self.api_style == "responses":
            payload: dict[str, Any] = {
                "model": self.model,
                "input": messages,
                "max_output_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
            if effort:
                payload["reasoning"] = {"effort": effort}
            return f"{self.base_url}/responses", payload
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if effort:
            payload["reasoning_effort"] = effort
        return f"{self.base_url}/chat/completions", payload

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> LLMResult | None:
        """One completion. ``timeout`` overrides ``llm.timeout_seconds`` for
        callers that must not outlive their slot (the wakeup action decision
        runs inside the 2-second tick loop, so it passes a tight budget)."""
        if not self.available:
            return None
        timeout_s = float(self.timeout if timeout is None else timeout)
        url, payload = self.build_payload(
            messages, max_tokens=max_tokens, temperature=temperature
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self.headers(session_id)
        pool = _http_pool() if self.keepalive else None
        use_pool = pool is not None and not _proxies_configured()
        started = time.time()
        if use_pool:
            try:
                response = pool.request(
                    "POST",
                    url,
                    body=body,
                    headers=headers,
                    timeout=timeout_s,
                    # No automatic retry of a POST (a completion costs money
                    # and may already have run), but keep urllib's redirect
                    # behaviour for gateways that move http -> https.
                    retries=urllib3.Retry(
                        total=5, connect=0, read=0, redirect=5, status=0
                    ),
                    redirect=True,
                    preload_content=True,
                )
                status = int(response.status)
                raw = bytes(response.data)
            except Exception as exc:  # urllib3 wraps TLS/connection errors
                self._metric("llm_error", 1.0, {"error": type(exc).__name__})
                return None
            if status >= 400:
                self._metric(
                    "llm_error",
                    1.0,
                    {
                        "status": status,
                        "body": raw[:500].decode("utf-8", errors="ignore"),
                    },
                )
                return None
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._metric("llm_error", 1.0, {"error": type(exc).__name__})
                return None
        else:
            req = urllib.request.Request(
                url, data=body, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                error_body = ""
                try:
                    error_body = exc.read().decode("utf-8", errors="ignore")[:500]
                except Exception:
                    pass
                self._metric(
                    "llm_error", 1.0, {"status": exc.code, "body": error_body}
                )
                return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._metric("llm_error", 1.0, {"error": type(exc).__name__})
                return None
        latency = (time.time() - started) * 1000
        text = self._extract_text(data)
        usage_raw = data.get("usage") or {}
        usage = self._parse_usage(usage_raw)
        self._record_usage(usage, latency)
        return LLMResult(
            text=text,
            usage=usage,
            latency_ms=latency,
            model=self.model,
            raw_usage=usage_raw if isinstance(usage_raw, dict) else {},
        )

    def _extract_text(self, data: dict[str, Any]) -> str:
        if self.api_style == "responses":
            if isinstance(data.get("output_text"), str):
                return data["output_text"]
            parts: list[str] = []
            for item in data.get("output", []) or []:
                if not isinstance(item, dict):
                    continue
                for content in item.get("content", []) or []:
                    if isinstance(content, dict) and content.get("type") in {
                        "output_text",
                        "text",
                    }:
                        parts.append(str(content.get("text", "")))
            return "".join(parts).strip()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            ).strip()
        return ""

    @staticmethod
    def _parse_usage(raw: dict[str, Any]) -> LLMUsage:
        prompt = int(
            raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0
        )
        completion = int(
            raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0
        )
        total = int(raw.get("total_tokens", prompt + completion) or 0)
        details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
        cache_hit = int(
            raw.get("prompt_cache_hit_tokens", 0)
            or raw.get("cache_hit_tokens", 0)
            or raw.get("cache_read_input_tokens", 0)
            or (details.get("cached_tokens", 0) if isinstance(details, dict) else 0)
            or 0
        )
        cache_miss_raw = raw.get("prompt_cache_miss_tokens", None)
        if cache_miss_raw is None:
            cache_miss = max(0, prompt - cache_hit)
        else:
            cache_miss = int(cache_miss_raw or 0)
        return LLMUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
            total_tokens=total,
        )

    def _record_usage(self, usage: LLMUsage, latency_ms: float) -> None:
        now = time.time()
        rows = [
            ("llm_prompt_tokens", usage.prompt_tokens),
            ("llm_completion_tokens", usage.completion_tokens),
            ("llm_cache_hit_tokens", usage.cache_hit_tokens),
            ("llm_cache_miss_tokens", usage.cache_miss_tokens),
            ("llm_latency_ms", latency_ms),
        ]
        for key, value in rows:
            self.db.execute(
                "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
                (
                    now,
                    key,
                    float(value),
                    json.dumps({"model": self.model, "provider": self.provider}, ensure_ascii=False),
                ),
            )
        total = usage.cache_hit_tokens + usage.cache_miss_tokens
        if total > 0:
            self.db.execute(
                "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
                (
                    now,
                    "llm_cache_hit_rate",
                    usage.cache_hit_tokens / total,
                    json.dumps({"model": self.model, "provider": self.provider}, ensure_ascii=False),
                ),
            )
        # A long prompt with zero reuse means the invariant block got polluted
        # or the gateway stopped honouring the cache. Record it loudly instead
        # of letting it hide in a 0.0 average.
        if usage.prompt_tokens >= 1200 and usage.cache_hit_tokens == 0:
            self.db.execute(
                "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
                (
                    now,
                    "llm_cache_cold",
                    float(usage.prompt_tokens),
                    json.dumps(
                        {"model": self.model, "provider": self.provider, "prompt_tokens": usage.prompt_tokens},
                        ensure_ascii=False,
                    ),
                ),
            )

    def _metric(self, key: str, value: float, tags: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
            (time.time(), key, float(value), json.dumps(tags, ensure_ascii=False)[:2000]),
        )
