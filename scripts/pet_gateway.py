#!/usr/bin/env python3
"""Home-side persistent WebSocket hub for the local whale-girl bridge.

Two kinds of peers, one process:

* ``/``      — the cloud qqbot's long-lived link.  It sends ``state`` /
               ``bubble`` events and receives ``chat`` frames from the local
               pet shell.
* ``/local`` — the future desktop pet shell (or ``scripts/pet_client.py``).
               It sends chat/interaction frames and receives state/bubbles.

Heavy media (TTS audio, songs, photos) does **not** go through this socket; it
stays on the existing HTTP/Tailscale paths.  The gateway is deliberately tiny
and standard-library + ``websockets`` only.

Run (from home_stack_start.ps1):
    python scripts/pet_gateway.py --host 127.0.0.1 --port 9950

Tailscale Serve exposes it as:
    tailscale serve --bg --https=9447 http://127.0.0.1:9950
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

try:
    import websockets
except ImportError:  # pragma: no cover - startup error is the useful signal
    print("missing dependency: websockets", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("pet_gateway")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9950
CAPABILITIES = ["chat", "state", "pet"]
LOCAL_PATH = "/local"


def _header(ws: Any, name: str) -> str:
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            return str(headers.get(name, "") or "")
        except Exception:
            pass
    legacy = getattr(ws, "request_headers", None)
    if legacy is not None:
        try:
            return str(legacy.get(name, "") or "")
        except Exception:
            pass
    return ""


def _path(ws: Any) -> str:
    request = getattr(ws, "request", None)
    value = getattr(request, "path", "") if request is not None else ""
    return str(value or "/")


def _query_token(ws: Any) -> str:
    """The Electron renderer cannot set WS headers, so /local accepts ?token=."""
    path = _path(ws)
    try:
        values = parse_qs(urlsplit(path).query)
    except ValueError:
        return ""
    return str((values.get("token") or [""])[0] or "")


class Hub:
    def __init__(self, token: str):
        self.token = token
        self.cloud: Any = None
        self.locals: set[Any] = set()
        self.last_state: dict[str, Any] = {"type": "state", "state": "idle"}
        self.started_at = time.time()

    async def send_cloud(self, payload: dict[str, Any]) -> bool:
        ws = self.cloud
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def broadcast(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False)
        dead: list[Any] = []
        for ws in list(self.locals):
            try:
                await ws.send(raw)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.locals.discard(ws)

    async def cloud_handler(self, ws: Any) -> None:
        self.cloud = ws
        await ws.send(
            json.dumps(
                {
                    "type": "hello_ack",
                    "role": "home",
                    "capabilities": CAPABILITIES,
                    "version": 1,
                    "ts": time.time(),
                },
                ensure_ascii=False,
            )
        )
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                kind = str(msg.get("type") or "")
                if kind == "ping":
                    await ws.send(json.dumps({"type": "pong", "ts": time.time()}))
                elif kind in {"state", "bubble", "typing"}:
                    if kind == "state":
                        self.last_state = msg
                    await self.broadcast(msg)
                elif kind == "hello":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello_ack",
                                "role": "home",
                                "capabilities": CAPABILITIES,
                                "echo": msg.get("device") or "",
                                "ts": time.time(),
                            },
                            ensure_ascii=False,
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            log.info("cloud link closed: %s", exc)
        finally:
            if self.cloud is ws:
                self.cloud = None
            log.info("cloud link disconnected")

    async def local_handler(self, ws: Any) -> None:
        self.locals.add(ws)
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "hello_ack",
                        "role": "home",
                        "capabilities": CAPABILITIES,
                        "cloud_connected": self.cloud is not None,
                        "ts": time.time(),
                    },
                    ensure_ascii=False,
                )
            )
            if self.last_state:
                await ws.send(json.dumps(self.last_state, ensure_ascii=False))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    await ws.send(json.dumps({"type": "error", "error": "bad_json"}))
                    continue
                kind = str(msg.get("type") or "")
                if kind == "ping":
                    await ws.send(json.dumps({"type": "pong", "ts": time.time()}))
                    continue
                if kind in {"chat", "interrupt", "interaction"}:
                    if self.cloud is None:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "error": "cloud_offline",
                                    "id": msg.get("id") or "",
                                }
                            )
                        )
                        continue
                    if kind == "chat":
                        msg.setdefault("session", "desktop:owner")
                    await self.send_cloud(msg)
                    await ws.send(
                        json.dumps(
                            {
                                "type": "ack",
                                "id": msg.get("id") or "",
                                "channel": kind,
                                "ts": time.time(),
                            }
                        )
                    )
                else:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "ack",
                                "id": msg.get("id") or "",
                                "channel": kind,
                                "ts": time.time(),
                            }
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            log.info("local pet closed: %s", exc)
        finally:
            self.locals.discard(ws)
            log.info("local pet disconnected")


def make_handler(hub: Hub):
    async def handler(ws: Any) -> None:
        supplied = _header(ws, "X-Pet-Token")
        path = _path(ws)
        if not supplied and path.startswith(LOCAL_PATH):
            supplied = _query_token(ws)
        if hub.token and supplied != hub.token:
            log.warning("pet gateway rejected connection (bad token)")
            await ws.close(code=4401, reason="unauthorized")
            return
        path = _path(ws)
        peer = getattr(ws, "remote_address", None)
        if path.startswith(LOCAL_PATH):
            log.info("local pet connected: %s", peer)
            await hub.local_handler(ws)
        else:
            log.info("cloud link connected: %s", peer)
            await hub.cloud_handler(ws)

    return handler


async def serve(host: str, port: int, token: str) -> None:
    hub = Hub(token)
    async with websockets.serve(
        make_handler(hub),
        host,
        port,
        ping_interval=20,
        ping_timeout=20,
        max_size=256 * 1024,
        close_timeout=5,
    ):
        log.info("pet gateway listening on ws://%s:%d", host, port)
        stop = asyncio.get_running_loop().create_future()

        def _stop(*_args: Any) -> None:
            if not stop.done():
                stop.set_result(None)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, _stop)
            except (NotImplementedError, RuntimeError):
                pass
        await stop


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="home-side persistent pet WebSocket hub")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default="")
    ap.add_argument("--token-env", default="PET_GATEWAY_TOKEN")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = str(args.token or os.environ.get(args.token_env, "") or "").strip()
    if not token:
        log.error("PET_GATEWAY_TOKEN 为空；拒绝无鉴权启动")
        return 2
    try:
        asyncio.run(serve(args.host, args.port, token))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
