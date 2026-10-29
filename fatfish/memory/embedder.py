"""Embedding backends.

``HashEmbedder`` is a dependency-free fallback: signed hashing of characters and
bigrams into a fixed number of buckets. It behaves like a cosine similarity but
it only rewards literal overlap, so it is a lexical matcher wearing a vector's
clothes -- fine for smoke tests, weak for "users mean 委屈 but typed 难过".

``HttpEmbedder`` talks to a local embedding service (``scripts/embedding_server.py``)
that owns a real model, because the core is deliberately dependency-free and
cannot import torch itself. When the service is unavailable it falls back to
hashing instead of raising: a degraded ranking must never break a turn.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import urllib.error
import urllib.request
from typing import Protocol

_CJK = re.compile(r"[\u4e00-\u9fff]")
_TOKEN = re.compile(r"[a-zA-Z0-9_]+")

HTTP_BACKENDS = frozenset({"http", "service", "bge", "remote"})
_hash_backends = frozenset({"hash", "local", "none"})


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


def _tokens(text: str) -> list[str]:
    text = (text or "").lower()
    tokens: list[str] = []
    for match in _TOKEN.finditer(text):
        tokens.append(match.group(0))
    chars = _CJK.findall(text)
    tokens.extend(chars)
    tokens.extend("".join(pair) for pair in zip(chars, chars[1:]))
    return [t for t in tokens if t.strip()]


def _signed_bucket(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    bucket = value % dim
    sign = 1.0 if (value >> 63) & 1 == 0 else -1.0
    return bucket, sign


class HashEmbedder:
    """Deterministic hashing embedding; handles Chinese characters and bigrams."""

    def __init__(self, dim: int = 256):
        self.dim = max(32, int(dim))

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        counts: dict[str, int] = {}
        for token in _tokens(text):
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            bucket, sign = _signed_bucket(token, self.dim)
            vec[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class HttpEmbedder:
    """Vectors from a local embedding service, with hashing as the safety net.

    ``dim`` is learned from the first successful response, because the service
    decides it (bge-base is 768). Callers must not assume a fixed width: stored
    vectors of an older width simply score 0 instead of being silently truncated
    by :func:`cosine`.
    """

    def __init__(
        self,
        url: str,
        dim: int = 0,
        *,
        timeout: float = 10.0,
        fallback: "Embedder | None" = None,
    ) -> None:
        self.url = str(url)
        self.dim = int(dim or 0)
        self.timeout = float(timeout)
        self._fallback = fallback if fallback is not None else HashEmbedder(256)
        self.failures = 0
        self.last_error: str | None = None

    @property
    def degraded(self) -> bool:
        return self.failures > 0

    def embed(self, text: str) -> list[float]:
        try:
            vector = self.embed_many([text])[0]
        except Exception as exc:  # noqa: BLE001 - never break a turn over vectors
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:160]
            return self._fallback.embed(text)
        return vector

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"texts": [str(t or "") for t in texts]}, ensure_ascii=False)
        request = urllib.request.Request(
            self.url,
            data=payload.encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        vectors = body.get("vectors") if isinstance(body, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("bad_embedding_response")
        out = [[float(x) for x in vec] for vec in vectors]
        if out and out[0]:
            self.dim = len(out[0])
        self.failures = 0
        return out

    def health(self) -> dict:
        """Best-effort status probe; never raises."""
        try:
            url = self.url.rsplit("/", 1)[0] + "/health"
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:160]}


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    if len(a) != n:
        a = a[:n]
    if len(b) != n:
        b = b[:n]
    # One pass instead of three separate comprehensions.  This sits in the
    # innermost loop of every memory/RAG search (up to 10k vectors per turn),
    # so the saved indexing and generator overhead is measurable.
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / math.sqrt(norm_a * norm_b)))


def dumps(vec: list[float] | None) -> str | None:
    if vec is None:
        return None
    return json.dumps([round(v, 6) for v in vec], separators=(",", ":"))


def pack_embedding(vec: list[float] | None) -> bytes | None:
    """Compact little-endian float32 encoding.

    JSON costs ~1.4KB per 256-dim vector and ~7.2KB per 768-dim one.  The BLOB
    is 1KB / 3KB and needs no number parsing.  SQLite stores BLOBs in a TEXT
    column without complaint, so this needs no schema change; ``loads`` reads
    both formats, which keeps old rows and old backups usable.
    """
    if vec is None:
        return None
    return struct.pack(f"<{len(vec)}f", *(float(v) for v in vec))


def loads(raw: str | bytes | memoryview | None) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, (bytes, bytearray)):
        size = len(raw) // 4
        if size <= 0:
            return None
        return list(struct.unpack(f"<{size}f", bytes(raw[: size * 4])))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list):
        return None
    # ``json.loads`` already returns floats for the numbers we wrote.  The old
    # list comprehension re-wrapped every element on every search; validate the
    # common shape cheaply and only convert if something unexpected (e.g. a
    # hand-edited string) is present.
    if value and not isinstance(value[0], (int, float)):
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            return None
    return value


def build_embedder(
    backend: str,
    dim: int,
    *,
    url: str | None = None,
    timeout: float = 10.0,
) -> Embedder:
    key = str(backend or "").strip().lower()
    if key in HTTP_BACKENDS:
        if not url:
            raise ValueError(
                f"embedding backend {key!r} needs a url (e.g. "
                "memory.embedding_url = 'http://127.0.0.1:9881/embed')"
            )
        return HttpEmbedder(str(url), dim, timeout=timeout)
    if key in _hash_backends or not key:
        return HashEmbedder(dim)
    raise ValueError(
        f"unsupported embedding backend: {backend!r}. "
        "Use 'hash', or 'http' plus a local scripts/embedding_server.py."
    )
