"""Local text-embedding HTTP service (bge-base-zh-v1.5 on CPU).

Why a separate process: ``fatfish`` core is deliberately dependency-free
(``pyproject.toml`` has ``dependencies = []``), so it cannot import torch. This
follows the same pattern the project already uses for speech: a local service
owns the model, the core talks HTTP to it.

Why CPU: the GPU has 8 GB and CosyVoice already takes ~7.7 GB of it. bge-base
is 102M params and embeds one short query in tens of milliseconds on CPU, so
there is nothing to gain from competing for VRAM.

Endpoints::

    GET  /health  -> {"ok": true, "loaded": bool, "dim": 768, "model": "..."}
    POST /embed   -> {"texts": ["..."]} -> {"ok": true, "dim": 768, "vectors": [[...]]}

Run with the CosyVoice virtualenv (it has torch + transformers)::

    .venv-cosyvoice\\Scripts\\python.exe scripts\\embedding_server.py

Self-check without starting a server::

    .venv-cosyvoice\\Scripts\\python.exe scripts\\embedding_server.py --self-test
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("FATFISH_DATA_DIR") or (ROOT / "data"))
MODEL_DIR = ROOT / "models" / "bge-base-zh-v1.5"

# The script is normally launched from the CosyVoice virtualenv, which does not
# have the project installed.  ROOT on sys.path is enough for the stdlib-only
# lock helper (fatfish/__init__.py is intentionally import-light).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from fatfish.process_lock import acquire, lock_path  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9881
MAX_BODY_BYTES = 256 * 1024
MAX_TEXTS = 256
MAX_TEXT_CHARS = 512
BATCH_SIZE = 16


class EmbeddingModel:
    """bge-style encoder: mean pooling over the last hidden state, L2 normalised."""

    def __init__(self, model_dir: Path) -> None:
        import torch
        from transformers import AutoConfig, AutoModel, AutoTokenizer

        self._torch = torch
        config = AutoConfig.from_pretrained(str(model_dir), local_files_only=True)
        self.dim = int(config.hidden_size)
        self.label = f"{model_dir.name}@{self.dim}"
        self._tok = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        self._model = AutoModel.from_pretrained(str(model_dir), local_files_only=True)
        self._model.eval()

    def embed(self, texts: list[str]) -> list[list[float]]:
        torch = self._torch
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            enc = self._tok(
                texts[start : start + BATCH_SIZE],
                padding=True,
                truncation=True,
                max_length=128,
                return_tensors="pt",
            )
            with torch.no_grad():
                hidden = self._model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).float()
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-6)
            for vec in pooled.tolist():
                norm = math.sqrt(sum(v * v for v in vec))
                out.append([v / norm for v in vec] if norm else vec)
        return out


class Service:
    """Holds at most one model; loads lazily on the first request."""

    def __init__(self, model_dir: Path, *, lazy: bool = True) -> None:
        self.model_dir = Path(model_dir)
        self.lazy = bool(lazy)
        self._lock = threading.Lock()
        self._model: EmbeddingModel | None = None
        self.loads = 0
        self.requests = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_load_seconds = 0.0

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _ensure_loaded(self) -> EmbeddingModel:
        if self._model is not None:
            return self._model
        started = time.perf_counter()
        model = EmbeddingModel(self.model_dir)
        self._model = model
        self.loads += 1
        self.last_load_seconds = time.perf_counter() - started
        logging.info("model loaded in %.1fs (%s)", self.last_load_seconds, model.label)
        return model

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            model = self._ensure_loaded()
            return model.embed(texts)

    def load(self) -> None:
        """Load eagerly (honours ``--no-lazy-load``)."""
        with self._lock:
            self._ensure_loaded()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "loaded": self.loaded,
            "dim": self._model.dim if self._model else 0,
            "model": self._model.label if self._model else self.model_dir.name,
            "loads": self.loads,
            "requests": self.requests,
            "errors": self.errors,
            "last_error": self.last_error,
            "load_seconds": round(self.last_load_seconds, 3),
        }


def build_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FatfishEmbed/1.0"
        timeout = 30

        def log_message(self, fmt: str, *args: Any) -> None:
            # Request bodies carry user text; never put them in access logs.
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path.split("?")[0] != "/health":
                self._send(404, {"ok": False, "error": "not_found"})
                return
            self._send(200, service.health())

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            if self.path.split("?")[0] != "/embed":
                self._send(404, {"ok": False, "error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(400, {"ok": False, "error": "bad_body_size"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"ok": False, "error": "bad_json"})
                return
            raw_texts = payload.get("texts") if isinstance(payload, dict) else None
            if not isinstance(raw_texts, list) or not raw_texts:
                self._send(400, {"ok": False, "error": "texts_required"})
                return
            if len(raw_texts) > MAX_TEXTS:
                self._send(400, {"ok": False, "error": "too_many_texts", "limit": MAX_TEXTS})
                return
            texts: list[str] = []
            for item in raw_texts:
                value = str(item or "").strip()[:MAX_TEXT_CHARS]
                texts.append(value)
            service.requests += 1
            try:
                vectors = service.embed(texts)
            except Exception as exc:  # noqa: BLE001 - report, never traceback
                service.errors += 1
                service.last_error = f"{type(exc).__name__}: {exc}"[:200]
                logging.exception("embed failed")
                self._send(503, {"ok": False, "error": "model_error"})
                return
            self._send(
                200,
                {
                    "ok": True,
                    "dim": len(vectors[0]) if vectors else 0,
                    "model": service._model.label if service._model else "",
                    "vectors": vectors,
                },
            )

    return Handler


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second process bind the same port,
    # putting two embedding models in RAM and silently splitting requests.
    # Refuse address reuse there; on POSIX the default is already safe.
    allow_reuse_address = os.name != "nt"


def self_test(model_dir: Path) -> int:
    """Load the model, embed three texts and print the cosine matrix."""
    model = EmbeddingModel(Path(model_dir))
    texts = ["想被摸摸头、求安慰", "有点烦，想被摸摸头", "哈哈大笑停不下来"]
    vectors = model.embed(texts)
    print(f"model={model.label} dim={model.dim} texts={len(texts)}")
    for i, left in enumerate(texts):
        for j, right in enumerate(texts):
            if j <= i:
                continue
            dot = sum(a * b for a, b in zip(vectors[i], vectors[j]))
            print(f"  cos({left!r}, {right!r}) = {dot:.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="local text embedding service")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=str(MODEL_DIR))
    parser.add_argument("--no-lazy-load", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("USE_TF", "0")

    model_dir = Path(args.model)
    if not model_dir.is_dir():
        print(f"[embed] missing model dir: {model_dir}", file=sys.stderr)
        return 1

    # A second embedding model costs ~1 GB of resident RAM.  The port check is
    # not enough on Windows (SO_REUSEADDR), and a --self-test also loads a full
    # model, so both paths take the same lock.
    lock = acquire(lock_path(DATA_DIR, "embedding"))
    if lock is None:
        print(
            "[embed] another embedding server (or a self-test) already holds "
            f"{lock_path(DATA_DIR, 'embedding')}; not starting a second copy.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.self_test:
            return self_test(model_dir)

        service = Service(model_dir, lazy=not args.no_lazy_load)
        if args.no_lazy_load:
            # Explicit --no-lazy-load used to be silently ignored.  Honour it:
            # the model is loaded once before health checks can see the server.
            service.load()
        try:
            server = _ThreadingServer((args.host, args.port), build_handler(service))
        except OSError as exc:
            print(
                f"[embed] cannot bind {args.host}:{args.port}: {exc}. "
                "Another process is using this port; not starting a second server.",
                file=sys.stderr,
            )
            return 2
        print(f"[embed] {model_dir.name} on http://{args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
