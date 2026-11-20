#!/usr/bin/env python
"""家里那台 GPU 机的唱歌端点（T-0067）。

云端没有显卡，TTS / 唱歌 / 拍照都留在家里。云上的 ``fatfish`` 用
``sing.mode = "remote"`` 把一首歌 POST 到这里，家里渲染完把 wav 原样回传，
云上再落自己的 ``data/songs/<job>/full.wav`` 和自己的 ``sing_jobs`` 行——QQ
`record` 与 Web `/api/audio` 都不用改。

    python scripts/sing_server.py --port 9945 --token-env SING_SERVER_TOKEN

在外网（tailnet）上暴露，只走 tailscale serve，不碰公网：

    tailscale serve --bg --https=9445 http://127.0.0.1:9945

协议
----
``GET /health``
    ``{"ok": true, "tts_loaded": bool|null, "vram_locked": bool, "busy": bool}``
    不需要 token：它只说「进程活着 / 显卡忙不忙」，不含任何内容。

``POST /sing``
    请求 ``{"emotion": "soft", "lyrics": ["…", …]?}``，鉴权头 ``X-Token``。
    * ``200 audio/wav``：完整混音；响应头带 ``X-Job-Id`` / ``X-Tts-Unloaded``
      / ``X-Tts-Reloaded`` / ``X-Elapsed-Ms``，云上据此填 ``sing_jobs``。
    * ``401``：token 不匹配。
    * ``409``：正在唱上一首（同一时刻只允许一个 job）。
    * ``504``：渲染超时（``--timeout``，默认 600s）。
    * ``503``：本机唱歌没启用 / 资产缺失。
    * ``500``：其它渲染失败；JSON 里的 ``error`` 是 ``SingService`` 的错误码，
      ``message`` 是给用户看的那一句（``friendly_sing_error``）。

显存交接仍然由 ``SingService`` 负责：抢 ``data/run/vram.lock`` → ``POST
127.0.0.1:9880/unload`` → 渲染 → **``finally`` 里 ``POST /load``**（成功、
失败、超时都一样）。本文件只把这件事包成 HTTP，不重复实现一遍。

本文件是标准库 only；渲染进程是 ``sing.python`` + ``sing.script``，可注入
（``LocalSinger(service=...)``）以便在没有 GPU 的机器上测试。
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # runnable as `python scripts/sing_server.py`
    sys.path.insert(0, str(ROOT))

from fatfish.config import load_config  # noqa: E402
from fatfish.db import Database  # noqa: E402
from fatfish.process_lock import acquire, lock_path  # noqa: E402
from fatfish.sing import SingService, TTSHandoff, friendly_sing_error  # noqa: E402

try:
    import preflight  # noqa: E402 - same scripts/ directory
except ModuleNotFoundError:  # imported as a module in tests: scripts/ is not sys.path[0]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import preflight  # noqa: E402

log = logging.getLogger("sing_server")

DEFAULT_PORT = 9945
DEFAULT_TIMEOUT = 600.0
MAX_BODY_BYTES = 64 * 1024
#: Session id written to the home-side ``sing_jobs`` row, so the ledger says
#: where the request came from without leaking the cloud session id.
REMOTE_SESSION_ID = "sing-server"


@dataclass
class RenderOutcome:
    ok: bool
    wav: bytes = b""
    error: str = ""
    job_id: str = ""
    tts_unloaded: bool = False
    tts_reloaded: bool = False
    elapsed_ms: float = 0.0


def http_status_for(code: str) -> int:
    """Map a ``SingService`` error code to an HTTP status the cloud understands."""
    code = str(code or "")
    if code == "unauthorized":
        return 401
    if code in {"busy", "sing_busy", "vram_busy", "vram_conflict_tts"}:
        return 409
    if code == "render_timeout":
        return 504
    if code == "disabled":
        return 503
    if code.startswith(
        ("python_missing", "script_missing", "lyrics_src_missing", "missing_assets")
    ):
        return 503
    if code == "memory_not_enough":
        return 503
    return 500


class LocalSinger:
    """``SingService`` wrapped as "one call in, one wav out".

    A fresh service (and therefore a fresh SQLite connection) is built **inside
    the request thread** and dropped afterwards: ``sqlite3`` connections are
    bound to the thread that created them, so sharing one ``Database`` across
    ``ThreadingHTTPServer`` workers would fail every write with
    ``ProgrammingError``.  One song per connection is also cheap enough not to
    care.
    """

    def __init__(
        self,
        config: Any,
        *,
        timeout: float | None = None,
        service_factory: Callable[[], Any] | None = None,
        handoff: Any | None = None,
        manage_tts: bool | None = None,
    ):
        self.config = config
        self.timeout = float(timeout) if timeout else None
        self._factory = service_factory
        self.handoff = handoff if handoff is not None else TTSHandoff(config)
        # Only the real home server owns the TTS process lifecycle.  Tests and
        # injected backends pass a factory/handoff and must never touch 9880.
        self._manage_tts = (
            bool(manage_tts)
            if manage_tts is not None
            else (service_factory is None and handoff is None)
        )
        self.data_dir = Path(getattr(config, "data_dir", ".") or ".")
        self.vram_lock = lock_path(self.data_dir, "vram")

    def _service(self) -> Any:
        if self._factory is not None:
            return self._factory()
        db = Database(self.config.db_path)
        db.initialize()
        return SingService(self.config, db, tts=self.handoff)

    @staticmethod
    def _close(service: Any) -> None:
        """One render owns one SQLite connection; close it, don't leak handles."""
        db = getattr(service, "db", None)
        close = getattr(db, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # 已经关了 / 另一个线程关了
                pass

    # -- rendering
    def render(self, *, emotion: str, lyrics: Any) -> RenderOutcome:
        started = time.time()
        # RVC/singing and TTS cannot coexist on this 8 GB card.  /unload is not
        # enough: the TTS process keeps ~5.6 GiB RSS, so kill it before YuE2 and
        # bring a fresh lightweight TTS process back afterwards.
        if self._manage_tts:
            preflight.stop_tts_process(self.data_dir)
        try:
            if self._manage_tts:
                ok, notes, _data = preflight.check("sing")
                if not ok:
                    log.warning("preflight refused singing: %s", "; ".join(notes))
                    return RenderOutcome(
                        ok=False,
                        error="memory_not_enough",
                        elapsed_ms=(time.time() - started) * 1000,
                    )
            return self._render_locked(emotion=emotion, lyrics=lyrics, started=started)
        finally:
            if self._manage_tts:
                preflight.start_tts_process(self.data_dir)

    def _render_locked(self, *, emotion: str, lyrics: Any, started: float) -> RenderOutcome:
        service = self._service()
        if self.timeout:
            # The whole point of the server-side budget: the cloud client waits
            # ~30s longer, so this timeout is the one the user actually sees.
            service.timeout = max(10.0, self.timeout)
        try:
            result = service.sing(
                lyrics=lyrics, emotion=emotion, session_id=REMOTE_SESSION_ID
            )
        finally:
            self._close(service)
        elapsed = (time.time() - started) * 1000
        if not result.ok:
            log.info(
                "job=%s failed error=%s elapsed=%.0fms",
                result.job_id,
                result.error,
                elapsed,
            )
            return RenderOutcome(
                ok=False,
                error=result.error or "render_failed",
                job_id=result.job_id,
                elapsed_ms=elapsed,
            )
        try:
            wav = Path(str(result.path or "")).read_bytes()
        except OSError:
            log.warning("job=%s wav unreadable: %s", result.job_id, result.path)
            return RenderOutcome(
                ok=False,
                error="render_failed:unreadable",
                job_id=result.job_id,
                elapsed_ms=elapsed,
            )
        log.info(
            "job=%s ok bytes=%d tts_unloaded=%s tts_reloaded=%s elapsed=%.0fms",
            result.job_id,
            len(wav),
            result.tts_unloaded,
            result.tts_reloaded,
            elapsed,
        )
        return RenderOutcome(
            ok=True,
            wav=wav,
            job_id=result.job_id,
            tts_unloaded=bool(result.tts_unloaded),
            tts_reloaded=bool(result.tts_reloaded),
            elapsed_ms=elapsed,
        )

    # -- observability
    def health(self, *, busy: bool) -> dict[str, Any]:
        # ``tts_loaded`` 是一次对家里 9880 的 ``GET /health``，超时 2s：TTS 没起来
        # 时这个端点会慢 2 秒才答。别高频轮询它（云端 HealthProbe 的 TTL 是 30s，
        # 并且 tts 那条 2s 会占掉 ``remote.timeout_seconds=3`` 里的大半）。
        loaded: bool | None = None
        if self.handoff is not None:
            try:
                loaded = self.handoff.loaded()
            except Exception:  # /health 永远要能回答
                loaded = None
        return {
            "ok": True,
            "tts_loaded": loaded,
            "vram_locked": bool(busy) or self.vram_busy(),
            "busy": bool(busy),
        }

    def vram_busy(self) -> bool:
        """Is somebody else (photo/TTS) holding the card right now?

        The lease is an OS byte-range lock, not a flag in a file, so the only
        honest answer is a one-shot non-blocking acquire.  Two guards keep the
        probe itself free of side effects (photo_server skips the probe for the
        same reason; see worker-local's note in docs/branches/media.md):

        * 锁文件还不存在 = 从来没人拿过这张卡，别为了回答一次 health 轮询就把它
          创建出来（`acquire()` 会顺手建 `data/run/` 和文件）；
        * 自己在渲染时由 :meth:`health` 的 ``busy`` 短路，根本走不到这里。

        剩下的代价是 acquire/release 之间那几微秒：恰好落在窗口里的拍照会看到
        一次假的 ``vram_busy``（用户重点一次即可）。
        """
        if not self.vram_lock.exists():
            return False
        lease = acquire(self.vram_lock)
        if lease is None:
            return True
        lease.release()
        return False


class SingApp:
    """One-in-flight gate plus the JSON/wav shaping.  Transport-agnostic."""

    def __init__(self, backend: LocalSinger, *, timeout: float = DEFAULT_TIMEOUT):
        self.backend = backend
        self.timeout = float(timeout)
        self._gate = threading.Lock()
        self.last: dict[str, Any] = {}

    @property
    def busy(self) -> bool:
        return self._gate.locked()

    def render(self, *, emotion: str, lyrics: Any) -> RenderOutcome:
        if not self._gate.acquire(blocking=False):
            return RenderOutcome(ok=False, error="busy")
        try:
            outcome = self.backend.render(emotion=emotion, lyrics=lyrics)
        except Exception as exc:  # a bad song must not kill the server
            log.exception("render crashed: %s", type(exc).__name__)
            outcome = RenderOutcome(ok=False, error=f"server_error:{type(exc).__name__}")
        finally:
            self._gate.release()
        self.last = {
            "at": time.time(),
            "ok": outcome.ok,
            "error": outcome.error,
            "job_id": outcome.job_id,
            "elapsed_ms": round(outcome.elapsed_ms, 1),
            "bytes": len(outcome.wav),
        }
        return outcome

    def health(self) -> dict[str, Any]:
        return self.backend.health(busy=self.busy)


class SingRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "fatfish-sing/1.0"
    #: 一个请求体只有几百字节；半开连接的客户端不该占住一个线程。
    timeout = 30

    # -- routing
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send_json(200, self.server.app.health())
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        path = urlsplit(self.path).path.rstrip("/") or "/"
        # 先把 body 读完再决定怎么答：客户端还在发请求体时关连接，对端会收到
        # "Connection aborted"(WSAECONNABORTED)，401/404 看起来就像服务崩了。
        payload, problem = self._read_payload()
        if path != "/sing":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            log.warning("POST /sing 401 (X-Token mismatch)")
            self._send_json(
                401,
                {
                    "ok": False,
                    "error": "unauthorized",
                    "message": "唱歌服务要 token，X-Token 不对。",
                },
            )
            return
        if problem:
            self._send_json(400, problem)
            return
        emotion = str(payload.get("emotion") or "")[:32]
        lyrics = payload.get("lyrics")
        if lyrics is not None and not isinstance(lyrics, (list, str)):
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "lyrics_bad_shape",
                    "message": friendly_sing_error("lyrics_bad_shape"),
                },
            )
            return
        outcome = self.server.app.render(emotion=emotion, lyrics=lyrics)
        if outcome.ok:
            self._send_wav(outcome)
            return
        code = outcome.error or "render_failed"
        self._send_json(
            http_status_for(code),
            {
                "ok": False,
                "error": code,
                "message": friendly_sing_error(code),
                "job_id": outcome.job_id,
                "elapsed_ms": round(outcome.elapsed_ms, 1),
            },
        )

    # -- helpers
    def _authorized(self) -> bool:
        token = str(getattr(self.server, "token", "") or "")
        if not token:
            return True
        return self.headers.get("X-Token", "") == token

    def _read_payload(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return {}, {
                "ok": False,
                "error": "payload_too_large",
                "message": "请求太大了，这次先不唱了。",
            }
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}, None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}, {
                "ok": False,
                "error": "bad_json",
                "message": "请求不是 JSON，这次先不唱了。",
            }
        if not isinstance(data, dict):
            return {}, {
                "ok": False,
                "error": "bad_json",
                "message": "请求不是 JSON 对象，这次先不唱了。",
            }
        return data, None

    def _send_wav(self, outcome: RenderOutcome) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(outcome.wav)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Job-Id", outcome.job_id or "")
        self.send_header("X-Tts-Unloaded", "1" if outcome.tts_unloaded else "0")
        self.send_header("X-Tts-Reloaded", "1" if outcome.tts_reloaded else "0")
        self.send_header("X-Elapsed-Ms", str(int(outcome.elapsed_ms)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(outcome.wav)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the log one line
        log.info("%s %s", self.address_string(), fmt % args)


class SingHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded so ``/health`` still answers while a song is rendering."""

    daemon_threads = True
    # Windows SO_REUSEADDR lets a *second* listener bind a busy port; the same
    # trap documented in fatfish/process_lock.py.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], app: SingApp, *, token: str = ""):
        self.app = app
        self.token = token
        super().__init__(address, SingRequestHandler)


def make_server(
    host: str, port: int, app: SingApp, *, token: str = ""
) -> SingHTTPServer:
    return SingHTTPServer((host, port), app, token=token)


def build_backend(config: Any, *, timeout: float | None = None) -> LocalSinger:
    return LocalSinger(config, timeout=timeout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="家里那台 GPU 机的唱歌端点")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认只听本机，由 tailscale serve 转发）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default="", help="X-Token 值；不给则读 --token-env 指的环境变量")
    parser.add_argument("--token-env", default="SING_SERVER_TOKEN")
    parser.add_argument("--allow-anonymous", action="store_true", help="显式关掉 token 鉴权（不推荐）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单首渲染上限（秒）")
    parser.add_argument("--profile", default=None, help="配置 profile（默认 FATFISH_PROFILE 或 low）")
    parser.add_argument("--config", default=None, help="额外配置文件（例如 config/cloud.local.toml）")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[sing] %(asctime)s %(levelname)s %(message)s",
    )
    token = str(args.token or os.environ.get(args.token_env, "") or "")
    if not token and not args.allow_anonymous:
        print(
            f"拒绝启动：没有 token。设置环境变量 {args.token_env}，或传 --token，"
            "或显式 --allow-anonymous（只在 127.0.0.1 上勉强可以）。",
            file=sys.stderr,
        )
        return 2
    if not token and args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("拒绝启动：--allow-anonymous 只允许绑 127.0.0.1。", file=sys.stderr)
        return 2

    config = load_config(args.profile, args.config)
    # Fail fast (schema migration, read-only data dir) here instead of inside
    # the first song, and never keep this connection: workers open their own.
    bootstrap = Database(config.db_path)
    try:
        bootstrap.initialize()
    finally:
        bootstrap.close()
    backend = build_backend(config, timeout=args.timeout)
    app = SingApp(backend, timeout=args.timeout)
    httpd = make_server(args.host, args.port, app, token=token)
    log.info(
        "listening on http://%s:%d (token=%s, timeout=%.0fs)",
        args.host,
        args.port,
        "on" if token else "OFF",
        args.timeout,
    )
    log.info(
        "expose it with: tailscale serve --bg --https=9445 http://127.0.0.1:%d",
        args.port,
    )
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("bye")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
