"""Cloud-side persistent WebSocket link to the home pet gateway.

The cloud QQ bot process owns this tiny daemon thread.  It dials the home
gateway over the tailnet and keeps one WSS connection alive, with heartbeats
and exponential reconnect.  It is a transport only: chat turns still run in
the cloud runtime; this link exists so future pet/local-shell events do not
pay a TLS handshake per request.

Configuration (environment, from `/opt/fatfish/.env`):
    PET_GATEWAY_URL=wss://desktop-....ts.net:9447
    PET_GATEWAY_TOKEN=<shared secret>
"""
from __future__ import annotations

import json
import logging
import os
import queue
import ssl
import threading
import time
from typing import Any

log = logging.getLogger("fatfish.pet_link")

try:
    import websocket  # websocket-client; already installed for the QQ gateway
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore

DEFAULT_PING_SECONDS = 20.0
MAX_BACKOFF_SECONDS = 30.0


class PetLink:
    def __init__(self, url: str, token: str, *, name: str = "cloud"):
        self.url = str(url or "").strip()
        self.token = str(token or "").strip()
        self.name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self.connected = False
        self.last_error = ""
        self.connects = 0
        self.messages = 0
        self.last_message_at = 0.0
        self._inbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self._send_lock = threading.Lock()

    # -- inbound queue (drained by the qqbot main thread, never the socket thread)
    def take_inbound(self) -> dict[str, Any] | None:
        try:
            return self._inbound.get_nowait()
        except queue.Empty:
            return None

    def send_event(self, payload: dict[str, Any]) -> bool:
        """Best-effort send from any thread; websocket-client is multithreaded."""
        ws = self._ws
        if ws is None or not self.connected:
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:  # noqa: BLE001 - reconnect loop owns recovery
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    @classmethod
    def from_env(cls) -> "PetLink | None":
        url = str(os.environ.get("PET_GATEWAY_URL", "") or "").strip()
        token = str(os.environ.get("PET_GATEWAY_TOKEN", "") or "").strip()
        if not url or not token:
            log.info("pet link disabled (PET_GATEWAY_URL/TOKEN not set)")
            return None
        return cls(url, token)

    # -- lifecycle
    def start(self) -> None:
        if not self.url or not self.token:
            return
        if websocket is None:
            log.warning("pet link disabled (websocket-client missing)")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="pet-link")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.url and self.token),
            "connected": self.connected,
            "url": self.url,
            "connects": self.connects,
            "messages": self.messages,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
        }

    # -- transport
    def _connect(self) -> Any:
        header = [f"X-Pet-Token: {self.token}"]
        sslopt = {}
        if self.url.startswith("wss://"):
            sslopt = {"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True}
        return websocket.create_connection(
            self.url,
            header=header,
            timeout=10.0,
            enable_multithread=True,
            sslopt=sslopt,
        )

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = self._connect()
                self._ws = ws
                self.connected = True
                self.connects += 1
                self.last_error = ""
                log.info("pet link connected: %s", self.url)
                self._send(
                    ws,
                    {
                        "type": "hello",
                        "role": "cloud",
                        "device": self.name,
                        "capabilities": ["chat", "state", "tts", "sing", "photo"],
                        "ts": time.time(),
                    },
                )
                ws.settimeout(1.0)
                last_ping = time.monotonic()
                backoff = 1.0
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except Exception as exc:
                        # websocket-client raises WebSocketTimeoutException for
                        # its recv timeout; a plain socket timeout is also
                        # benign here.  Distinguish nothing for now: any
                        # exception means "check stop and ping".
                        if type(exc).__name__ not in {
                            "WebSocketTimeoutException",
                            "TimeoutError",
                            "socket.timeout",
                        }:
                            raise
                        raw = ""
                    if raw:
                        self.messages += 1
                        self.last_message_at = time.time()
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            msg = {"type": "raw"}
                        log.info("pet link <- %s", str(msg)[:300])
                        kind = str(msg.get("type") or "")
                        if kind == "chat":
                            self._inbound.put(msg)
                    now = time.monotonic()
                    if now - last_ping >= DEFAULT_PING_SECONDS:
                        self._send(ws, {"type": "ping", "ts": time.time()})
                        last_ping = now
            except Exception as exc:  # noqa: BLE001 - reconnect loop
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("pet link disconnected: %s", self.last_error)
            finally:
                self.connected = False
                ws = self._ws
                self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(MAX_BACKOFF_SECONDS, backoff * 2.0)

    @staticmethod
    def _send(ws: Any, payload: dict[str, Any]) -> None:
        ws.send(json.dumps(payload, ensure_ascii=False))
