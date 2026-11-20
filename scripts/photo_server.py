#!/usr/bin/env python
"""家里那台 GPU 机的拍照端点（T-0068 第二段）。

云端没有显卡，也没有 ComfyUI。云上的 ``fatfish`` 用 ``photo.mode = "remote"``
把「画一张」POST 到这里，家里用 ``PhotoService`` 走完 ComfyUI（含显存租约、
自启、TTS 卸载），把 png 原样回传；云上再落自己的 ``data/photos`` 和自己的
``photo_jobs`` 行——QQ / Web 的发送链路一行不用改。

    python scripts/photo_server.py --port 9946 --token-env PHOTO_SERVER_TOKEN

在外网（tailnet）上暴露，只走 tailscale serve，不碰公网：

    tailscale serve --bg --https=9446 http://127.0.0.1:9946

协议（与 ``scripts/sing_server.py`` 同一套约定，两边客户端形状一致）
----
``GET /health``
    ``{"ok": true, "busy": bool, "mode": "local", "comfyui_url": "..."}``
    不需要 token：它只说「进程活着 / 手上有没有活」，不含任何内容。
    云端 ``fatfish remote`` 探的就是这个地址——**没有它，photo 能力位永远是红的**。

``POST /photo``
    请求 ``{"prompt": "…", "negative": "…"?, "caption": "…"?, "job_id": "…"?,
    "width": 1024?, "height": 1024?, "seed": 42?}``，鉴权头 ``X-Token``。
    * ``200 image/png``：png 字节；响应头带 ``X-Job-Id`` / ``X-Elapsed-Ms``。
    * ``401``：token 不匹配。
    * ``409``：正在拍上一张（同一时刻只允许一个 job；与唱歌共用显卡时也会落到这里，
      因为 ``PhotoService`` 抢不到 ``data/run/vram.lock`` 时返回 ``vram_busy``）。
    * ``504``：画图超时（``--timeout``，默认 150s；云端等 180s，先让家里到点）。
    * ``503``：本机没启用 / ComfyUI 起不来 / 模型缺失。
    * ``500``：其它渲染失败；JSON 里的 ``error`` 是 ``PhotoService`` 的错误码，
      ``message`` 是给用户看的那一句（``friendly_photo_error``）。

显存交接仍然由 ``PhotoService`` 负责：抢 ``data/run/vram.lock`` → 必要时卸载 TTS
→ 画图 → ``finally`` 里放锁。本文件只把这件事包成 HTTP，不重复实现一遍。

本文件是标准库 only；渲染层可注入（``LocalPhotographer(service_factory=...)``），
所以没有 GPU 的机器也能跑完所有协议测试，不会碰真的 ``vram.lock`` 或 ComfyUI。
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
if str(ROOT) not in sys.path:  # runnable as `python scripts/photo_server.py`
    sys.path.insert(0, str(ROOT))

from fatfish.config import load_config  # noqa: E402
from fatfish.db import Database  # noqa: E402
from fatfish.photo.service import (  # noqa: E402
    PhotoPrompt,
    PhotoService,
    friendly_photo_error,
)

log = logging.getLogger("photo_server")

DEFAULT_PORT = 9946
#: 云端 ``photo.remote_timeout_seconds = 180``；家里先到点，好把「画超时」这个
#: 明确结论回给云端，而不是让云端先断、只剩一句含糊的 remote_timeout。
DEFAULT_TIMEOUT = 150.0
MAX_BODY_BYTES = 32 * 1024
MIN_SIDE = 64
MAX_SIDE = 2048
#: Session id written to the home-side ``photo_jobs`` row, so the ledger says
#: where the request came from without leaking the cloud session id.
REMOTE_SESSION_ID = "photo-server"


@dataclass
class RenderOutcome:
    ok: bool
    png: bytes = b""
    error: str = ""
    job_id: str = ""
    caption: str = ""
    elapsed_ms: float = 0.0


def http_status_for(code: str) -> int:
    """Map a ``PhotoService`` error code to an HTTP status the cloud understands.

    Mirrors ``sing_server.http_status_for``: busy-ish codes are 409 so the cloud
    can tell "wait a moment" apart from "broken", and timeouts are 504.
    """
    code = str(code or "")
    if code == "unauthorized":
        return 401
    if code in {"busy", "sing_busy", "photo_busy", "vram_busy", "vram_conflict_tts"}:
        return 409
    if code in {"comfyui_timeout"} or code.startswith("render_timeout"):
        return 504
    if code == "disabled":
        return 503
    if code.startswith(
        (
            "comfyui_not_running",
            "comfyui_start_",
            "comfyui_spawn_error",
            "missing_assets",
            "paths_missing",
        )
    ):
        return 503
    return 500


class LocalPhotographer:
    """``PhotoService`` wrapped as "one call in, one png out".

    A fresh service (and therefore a fresh SQLite connection) is built **inside
    the request thread** and dropped afterwards: ``sqlite3`` connections are
    bound to the thread that created them, so sharing one ``Database`` across
    ``ThreadingHTTPServer`` workers would fail every write with
    ``ProgrammingError``.  One picture per connection is cheap enough.
    """

    def __init__(
        self,
        config: Any,
        *,
        timeout: float | None = None,
        service_factory: Callable[[], Any] | None = None,
    ):
        self.config = config
        self.timeout = float(timeout) if timeout else None
        self._factory = service_factory

    def _service(self) -> Any:
        if self._factory is not None:
            return self._factory()
        db = Database(self.config.db_path)
        db.initialize()
        return PhotoService(self.config, db)

    @staticmethod
    def _close(service: Any) -> None:
        """One picture owns one SQLite connection; close it, don't leak handles."""
        db = getattr(service, "db", None)
        close = getattr(db, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # 已经关了 / 另一个线程关了
                pass

    def render(
        self,
        *,
        positive: str,
        negative: str = "",
        caption: str = "",
        seed: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> RenderOutcome:
        started = time.time()
        if not positive.strip():
            return RenderOutcome(ok=False, error="prompt_missing")
        service = self._service()
        if self.timeout:
            service.timeout = max(10.0, self.timeout)
        if width:
            service.width = max(MIN_SIDE, min(MAX_SIDE, int(width)))
        if height:
            service.height = max(MIN_SIDE, min(MAX_SIDE, int(height)))
        prompt = PhotoPrompt(
            positive=positive.strip(),
            negative=str(negative or "").strip(),
            caption=str(caption or "").strip() or "给你画好啦",
            source="remote",
        )
        try:
            result = service.generate(
                "",
                prompt=prompt,
                session_id=REMOTE_SESSION_ID,
                seed=seed,
            )
        except Exception:
            # ``generate`` is documented never to raise; if it ever does, the
            # request thread still has to answer with one sentence.
            log.exception("generate crashed")
            return RenderOutcome(
                ok=False, error="server_error:generate", elapsed_ms=(time.time() - started) * 1000
            )
        finally:
            self._close(service)
        elapsed = (time.time() - started) * 1000
        if not result.ok:
            # 摘要行由 ``PhotoApp`` 统一打（gate 拒绝的那张没有 service 结果，
            # 也得留痕），这里不再重复。
            return RenderOutcome(
                ok=False,
                error=str(result.error or "render_failed"),
                job_id=result.job_id,
                elapsed_ms=elapsed,
            )
        try:
            png = Path(str(result.path or "")).read_bytes()
        except OSError:
            log.warning("job=%s png unreadable: %s", result.job_id, result.path)
            return RenderOutcome(
                ok=False,
                error="photo_storage_error",
                job_id=result.job_id,
                elapsed_ms=elapsed,
            )
        return RenderOutcome(
            ok=True,
            png=png,
            job_id=result.job_id,
            caption=result.caption or prompt.caption,
            elapsed_ms=elapsed,
        )

    # -- observability
    def health(self, *, busy: bool) -> dict[str, Any]:
        # 不碰 ``data/run/vram.lock``：那是跑图时的租约，健康检查只回答
        # 「进程活着 / 手上有没有活」，不该去 acquire 一下（会创建/触碰运行态文件）。
        getter = getattr(self.config, "get", None)
        mode = "local"
        url = ""
        if callable(getter):
            mode = str(getter("photo.mode", "local") or "local")
            url = str(getter("photo.comfyui_url", "") or "")
        return {"ok": True, "busy": bool(busy), "mode": mode, "comfyui_url": url}


class PhotoApp:
    """One-in-flight gate plus the JSON/png shaping.  Transport-agnostic."""

    def __init__(
        self,
        backend: LocalPhotographer,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        emit_json: bool = False,
    ):
        self.backend = backend
        self.timeout = float(timeout)
        self.emit_json = bool(emit_json)
        self._gate = threading.Lock()
        self.last: dict[str, Any] = {}

    @property
    def busy(self) -> bool:
        return self._gate.locked()

    def render(self, **kwargs: Any) -> RenderOutcome:
        if not self._gate.acquire(blocking=False):
            # 被拒的这一张也要留痕：云端日志里能看到「409 是因为上一张还没画完」，
            # 而不是一片空白。
            return self._record(RenderOutcome(ok=False, error="busy"))
        try:
            outcome = self.backend.render(**kwargs)
        except Exception as exc:  # a bad request must not kill the server
            log.exception("render crashed: %s", type(exc).__name__)
            outcome = RenderOutcome(ok=False, error=f"server_error:{type(exc).__name__}")
        finally:
            # 无论成功、失败还是抛异常，闸门都要放回去：下一次拍照不能被上一张
            # 的异常永久堵死。
            self._gate.release()
        return self._record(outcome)

    def health(self) -> dict[str, Any]:
        return self.backend.health(busy=self.busy)

    # -- summary line
    def _record(self, outcome: RenderOutcome) -> RenderOutcome:
        """One line per job, so ``grep``/``jq`` on the home log can tell what happened."""
        self.last = {
            "at": round(time.time(), 3),
            "ok": outcome.ok,
            "error": outcome.error,
            "job_id": outcome.job_id,
            "elapsed_ms": round(outcome.elapsed_ms, 1),
            "bytes": len(outcome.png),
        }
        if self.emit_json:
            log.info("summary %s", json.dumps({"event": "photo_job", **self.last}, ensure_ascii=False))
        else:
            log.info(
                "job=%s ok=%s bytes=%d elapsed=%.0fms error=%s",
                outcome.job_id or "-",
                outcome.ok,
                len(outcome.png),
                outcome.elapsed_ms,
                outcome.error or "-",
            )
        return outcome


class PhotoRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "fatfish-photo/1.0"
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
        if path != "/photo":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self._authorized():
            log.warning("POST /photo 401 (X-Token mismatch)")
            self._send_json(
                401,
                {
                    "ok": False,
                    "error": "unauthorized",
                    "message": "拍照服务要 token，X-Token 不对。",
                },
            )
            return
        if problem:
            self._send_json(400, problem)
            return
        positive = str(payload.get("prompt") or payload.get("positive") or "").strip()
        if not positive:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "prompt_missing",
                    "message": "没给要画什么，这次先不拍了。",
                },
            )
            return
        seed: int | None = None
        if payload.get("seed") is not None:
            try:
                seed = int(payload["seed"])
            except (TypeError, ValueError):
                self._send_json(
                    400,
                    {"ok": False, "error": "bad_seed", "message": "seed 不是整数。"},
                )
                return
        outcome = self.server.app.render(
            positive=positive,
            negative=str(payload.get("negative") or ""),
            caption=str(payload.get("caption") or ""),
            seed=seed,
            width=_int_or_none(payload.get("width")),
            height=_int_or_none(payload.get("height")),
        )
        if outcome.ok:
            self._send_png(outcome)
            return
        code = outcome.error or "render_failed"
        self._send_json(
            http_status_for(code),
            {
                "ok": False,
                "error": code,
                "message": friendly_photo_error(code),
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
                "message": "请求太大了，这次先不拍了。",
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
                "message": "请求不是 JSON，这次先不拍了。",
            }
        if not isinstance(data, dict):
            return {}, {
                "ok": False,
                "error": "bad_json",
                "message": "请求不是 JSON 对象，这次先不拍了。",
            }
        return data, None

    def _send_png(self, outcome: RenderOutcome) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(outcome.png)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Job-Id", outcome.job_id or "")
        self.send_header("X-Elapsed-Ms", str(int(outcome.elapsed_ms)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(outcome.png)

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


class PhotoHTTPServer(http.server.ThreadingHTTPServer):
    """Threaded so ``/health`` still answers while a picture is rendering."""

    daemon_threads = True
    # Windows SO_REUSEADDR lets a *second* listener bind a busy port; the same
    # trap documented in fatfish/process_lock.py.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], app: PhotoApp, *, token: str = ""):
        self.app = app
        self.token = token
        super().__init__(address, PhotoRequestHandler)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def make_server(
    host: str, port: int, app: PhotoApp, *, token: str = ""
) -> PhotoHTTPServer:
    return PhotoHTTPServer((host, port), app, token=token)


def build_backend(config: Any, *, timeout: float | None = None) -> LocalPhotographer:
    return LocalPhotographer(config, timeout=timeout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="家里那台 GPU 机的拍照端点")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认只听本机，由 tailscale serve 转发）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default="", help="X-Token 值；不给则读 --token-env 指的环境变量")
    parser.add_argument("--token-env", default="PHOTO_SERVER_TOKEN")
    parser.add_argument("--allow-anonymous", action="store_true", help="显式关掉 token 鉴权（不推荐）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单张图渲染上限（秒）")
    parser.add_argument(
        "--emit-json",
        action="store_true",
        help="每张图打一行 JSON 摘要（event=photo_job），方便云端把家里日志直接 jq/grep",
    )
    parser.add_argument("--profile", default=None, help="配置 profile（默认 FATFISH_PROFILE 或 low）")
    parser.add_argument("--config", default=None, help="额外配置文件（例如 config/cloud.local.toml）")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[photo] %(asctime)s %(levelname)s %(message)s",
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
    # 家里这台不该再往别处转发：那会变成「云端 → 家里 → 云端」的环。
    if str(config.get("photo.mode", "local") or "local").strip().lower() == "remote":
        print(
            "拒绝启动：photo.mode = \"remote\"。photo_server 只住家里，"
            "请用 low.toml（mode = \"local\"）启动。",
            file=sys.stderr,
        )
        return 2
    # Fail fast (schema migration, read-only data dir) here instead of inside
    # the first picture, and never keep this connection: workers open their own.
    bootstrap = Database(config.db_path)
    try:
        bootstrap.initialize()
    finally:
        bootstrap.close()
    backend = build_backend(config, timeout=args.timeout)
    app = PhotoApp(backend, timeout=args.timeout, emit_json=args.emit_json)
    httpd = make_server(args.host, args.port, app, token=token)
    log.info(
        "listening on http://%s:%d (token=%s, timeout=%.0fs, emit_json=%s)",
        args.host,
        args.port,
        "on" if token else "OFF",
        args.timeout,
        "on" if args.emit_json else "off",
    )
    log.info(
        "expose it with: tailscale serve --bg --https=9446 http://127.0.0.1:%d",
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
