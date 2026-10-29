#!/usr/bin/env python3
"""NapCat login QR as an MCP tool (Streamable HTTP, stdlib only).

Why
---
The phone-side agent (RikkaHub) can call MCP tools over HTTPS.  When the QQ
account is kicked, the fastest recovery is: ask RikkaHub for the QR, save the
returned image to the album, scan it with QQ/TIM -- no SSH tunnel and no
desktop browser.

What it does
------------
* ``napcat_status`` -- WebUI login state (isLogin / isOffline / loginPhase)
  plus the QR file age.
* ``napcat_qr`` -- if already online, says so; otherwise starts the NapCat
  supervisor/container when needed, calls NapCat WebUI's ``RefreshQRcode``,
  waits for ``/opt/napcat/cache/qrcode.png`` to be rewritten, and returns the
  PNG as an MCP image content block.

The WebUI token is exchanged for a short-lived Credential exactly like the
WebUI front-end does: ``sha256(token + ".napcat")`` -> ``POST /api/auth/login``
-> ``Authorization: Bearer <Credential>``.  The MCP endpoint itself is guarded
by the same token (``Authorization: Bearer <FATFISH_WEB_TOKEN>``).

Run
---
    python scripts/napcat_qr_mcp.py --host 127.0.0.1 --port 9451

Then expose it over Tailscale (HTTPS, never a public port):

    tailscale serve --bg --https=9451 http://127.0.0.1:9451

RikkaHub: MCP server, transport ``streamable_http``, URL
``https://fatfish-cloud.<tailnet>.ts.net:9451/mcp``, header
``Authorization: Bearer <FATFISH_WEB_TOKEN>``.

Security notes
--------------
* The QR image is a login credential: only serve it over the tailnet and only
  with the bearer token.  Never log the token, the Credential or the QR bytes.
* The tool never logs in by password / SMS; it only asks NapCat to render the
  QR it already uses.  No account secret is stored here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

DEFAULT_WEBUI_BASE = "http://127.0.0.1:6099/api"
DEFAULT_QR_PATH = "/opt/napcat/cache/qrcode.png"
DEFAULT_ENV_PATH = "/opt/fatfish/.env"
DEFAULT_WEBUI_JSON = "/opt/napcat/config/webui.json"
QR_MAX_AGE_SECONDS = 120.0
START_WAIT_SECONDS = 30.0
QR_WAIT_SECONDS = 10.0
CREDENTIAL_TTL_SECONDS = 600.0

TOOL_QR = "napcat_qr"
TOOL_STATUS = "napcat_status"
SERVER_NAME = "fatfish-napcat-qr"
SERVER_VERSION = "0.1.0"


# --------------------------------------------------------------- small utils
def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_env_value(path: str | Path, key: str) -> str:
    """Read one ``KEY=value`` from an env file; missing file/key -> ``""``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip()
    return ""


def read_webui_token(path: str | Path) -> str:
    """NapCat WebUI token from ``webui.json`` (the 6099 login secret)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("token") or "").strip()


def post_json(
    base: str,
    path: str,
    payload: dict[str, Any],
    *,
    token: str | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("webui_unauthorized") from exc
        raise
    return json.loads(raw.decode("utf-8") or "{}")


# --------------------------------------------------------------- NapCat API
class NapCatClient:
    """The thin WebUI API client plus the QR-file wait."""

    def __init__(
        self,
        webui_token: str,
        *,
        webui_base: str = DEFAULT_WEBUI_BASE,
        qr_path: str | Path = DEFAULT_QR_PATH,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.webui_token = str(webui_token or "").strip()
        self.webui_base = str(webui_base or DEFAULT_WEBUI_BASE).rstrip("/")
        self.qr_path = Path(qr_path)
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self._credential = ""
        self._credential_at = 0.0
        self._lock = threading.Lock()

    # -- auth
    def _login(self) -> None:
        if not self.webui_token:
            raise RuntimeError("missing_webui_token")
        digest = sha256_hex(self.webui_token + ".napcat")
        response = post_json(self.webui_base, "/auth/login", {"hash": digest})
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        credential = str(data.get("Credential") or "").strip()
        if not credential:
            raise RuntimeError("webui_login_failed")
        self._credential = credential
        self._credential_at = self.clock()

    def _call(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if (
                not self._credential
                or self.clock() - self._credential_at > CREDENTIAL_TTL_SECONDS
            ):
                self._login()
            for _ in range(2):
                try:
                    response = post_json(
                        self.webui_base, path, payload or {}, token=self._credential
                    )
                except PermissionError:
                    self._login()
                    continue
                # The WebUI answers HTTP 200 with code=-1 for an expired
                # Credential; only re-login when it explicitly says so.
                if int(response.get("code", 0) or 0) == -1 and "unauthorized" in str(
                    response.get("message") or ""
                ).lower():
                    self._login()
                    continue
                return response
            raise RuntimeError("webui_unauthorized")

    def _data(self, response: dict[str, Any], action: str) -> dict[str, Any]:
        if int(response.get("code", 0) or 0) != 0:
            raise RuntimeError(
                f"{action}_failed: {str(response.get('message') or '')[:120]}"
            )
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    # -- state
    def status(self) -> dict[str, Any]:
        return self._data(self._call("/QQLogin/CheckLoginStatus", {}), "status")

    @staticmethod
    def online(status: dict[str, Any] | None) -> bool:
        state = status or {}
        return bool(state.get("isLogin")) and not bool(state.get("isOffline"))

    def _container_running(self) -> bool:
        try:
            result = self.runner(
                ["docker", "inspect", "-f", "{{.State.Running}}", "napcat"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            return False
        return (
            getattr(result, "returncode", 1) == 0
            and str(getattr(result, "stdout", "") or "").strip() == "true"
        )

    def ensure_running(self) -> None:
        """Start the supervisor/container when the WebUI is unreachable."""
        if self._container_running():
            return
        try:
            self.runner(
                ["systemctl", "start", "napcat-supervisor"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:
            pass
        deadline = self.clock() + START_WAIT_SECONDS
        while self.clock() < deadline:
            try:
                self.status()
                return
            except Exception:
                self.sleep(1.0)
        raise RuntimeError("napcat_start_timeout")

    def _qr_age(self) -> float:
        try:
            return max(0.0, self.clock() - self.qr_path.stat().st_mtime)
        except OSError:
            return float("inf")

    def qr(self) -> dict[str, Any]:
        """Return ``{"online": bool, "png": bytes|None, "status": dict}``."""
        try:
            status = self.status()
        except Exception:
            self.ensure_running()
            status = self.status()
        if self.online(status):
            return {"online": True, "png": None, "status": status}

        self.ensure_running()
        started = self.clock()
        response = self._call("/QQLogin/RefreshQRcode", {})
        if int(response.get("code", 0) or 0) != 0:
            message = str(response.get("message") or "")
            if "LOGIN" in message.upper() or "登录" in message:
                return {"online": True, "png": None, "status": self.status()}
            raise RuntimeError(f"refresh_failed: {message[:120]}")

        deadline = self.clock() + QR_WAIT_SECONDS
        while self.clock() < deadline:
            try:
                fresh = self.qr_path.stat().st_mtime >= started - 2.0
                if fresh:
                    data = self.qr_path.read_bytes()
                    if data:
                        return {"online": False, "png": data, "status": status}
            except OSError:
                pass
            self.sleep(0.4)
        raise RuntimeError("qr_file_not_updated")

    def status_payload(self) -> dict[str, Any]:
        age = self._qr_age()
        qr_age = round(age, 1) if age != float("inf") else None
        qr_fresh = age <= QR_MAX_AGE_SECONDS
        try:
            status = self.status()
        except Exception as exc:
            return {
                "reachable": False,
                "error": f"{type(exc).__name__}: {exc}"[:160],
                "qr_age_seconds": qr_age,
                "qr_fresh": qr_fresh,
            }
        return {
            "reachable": True,
            "online": self.online(status),
            "is_login": bool(status.get("isLogin")),
            "is_offline": bool(status.get("isOffline")),
            "login_phase": str(status.get("loginPhase") or ""),
            "login_error": str(status.get("loginError") or "")[:160],
            "qr_age_seconds": qr_age,
            "qr_fresh": qr_fresh,
        }


# --------------------------------------------------------------- MCP server
TOOLS: list[dict[str, Any]] = [
    {
        "name": TOOL_QR,
        "description": (
            "Get the current NapCat/QQ login QR code as an image. Use this when "
            "the bot account is offline or needs a re-scan. If already online it "
            "returns a text status instead of an image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": TOOL_STATUS,
        "description": (
            "Read NapCat credit status: online, login phase, last QR age. "
            "Use before/after napcat_qr to confirm the scan worked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def _text_content(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_content(data: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": "image/png",
    }


class MCPApp:
    """JSON-RPC methods; transport-agnostic so tests need no sockets."""

    def __init__(
        self,
        client: NapCatClient,
        *,
        protocol_version: str = "2024-11-05",
    ):
        self.client = client
        self.protocol_version = protocol_version
        self.sessions: set[str] = set()

    def _result(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _call_tool(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        if name == TOOL_STATUS:
            payload = self.client.status_payload()
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            return {"content": [_text_content(text)], "isError": False}
        if name == TOOL_QR:
            result = self.client.qr()
            if result.get("online"):
                status = result.get("status") or {}
                text = (
                    "QQ 已在线，不需要扫码。\n"
                    f"loginPhase={status.get('loginPhase') or ''}"
                )
                return {"content": [_text_content(text)], "isError": False}
            png = result.get("png") or b""
            if not png:
                return {
                    "content": [_text_content("二维码为空，请再试一次。")],
                    "isError": True,
                }
            return {
                "content": [
                    _text_content(
                        "NapCat 登录二维码（约 2 分钟有效）。"
                        "用手机 QQ/TIM 扫一扫，或保存到相册后从相册选图；"
                        "过期就再调用一次 napcat_qr。"
                    ),
                    _image_content(png),
                ],
                "isError": False,
            }
        return {
            "content": [_text_content(f"unknown tool: {name}")],
            "isError": True,
        }

    def handle(
        self, request: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Return ``(response, session_id)``; ``None`` for notifications."""
        if not isinstance(request, dict):
            return self._error(None, -32600, "invalid request"), None
        request_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}

        if method == "initialize":
            session_id = uuid.uuid4().hex
            self.sessions.add(session_id)
            return (
                self._result(
                    request_id,
                    {
                        "protocolVersion": str(
                            params.get("protocolVersion") or self.protocol_version
                        ),
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": SERVER_VERSION,
                        },
                    },
                ),
                session_id,
            )
        if method in {"notifications/initialized", "initialized"}:
            return None, None
        if method == "ping":
            return self._result(request_id, {}), None
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS}), None
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = self._call_tool(name, arguments)
            except Exception as exc:  # tool failures are results, not crashes
                result = {
                    "content": [
                        _text_content(f"{type(exc).__name__}: {exc}"[:300])
                    ],
                    "isError": True,
                }
            return self._result(request_id, result), None
        return self._error(request_id, -32601, f"method not found: {method}"), None

    def handle_batch(self, payload: Any) -> tuple[Any, str | None]:
        if isinstance(payload, list):
            responses = []
            session_id = None
            for item in payload:
                response, sid = self.handle(item)
                session_id = session_id or sid
                if response is not None:
                    responses.append(response)
            return (responses or None), session_id
        response, session_id = self.handle(payload)
        return response, session_id


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "fatfish-napcat-qr/" + SERVER_VERSION

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Keep journald clean: no headers (they carry the bearer token).
        print(f"[napcat-qr-mcp] {self.address_string()} {fmt % args}", flush=True)

    def _authorized(self) -> bool:
        expected = getattr(self.server, "auth_token", "")
        header = str(self.headers.get("Authorization") or "")
        prefix, _, supplied = header.partition(" ")
        if prefix.lower() != "bearer" or not supplied:
            return False
        return hmac.compare_digest(supplied.strip(), expected)

    def _send_json(
        self, status: int, payload: Any, *, session_id: str | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in {"/mcp", ""}:
            self._send_json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            self._send_json(400, {"error": "invalid_body"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                },
            )
            return
        app = getattr(self.server, "app")
        try:
            response, session_id = app.handle_batch(payload)
        except Exception as exc:  # never leak a traceback over HTTP
            self._send_json(
                500,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": str(exc)[:200]},
                },
            )
            return
        if response is None:
            self.send_response(202)
            self.send_header("Cache-Control", "no-store")
            if session_id:
                self.send_header("Mcp-Session-Id", session_id)
            self.end_headers()
            return
        self._send_json(200, response, session_id=session_id)

    def do_GET(self) -> None:  # noqa: N802
        # Streamable HTTP clients may probe with GET; this server has no
        # server-initiated stream, so answer cleanly instead of hanging.
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        self._send_json(405, {"error": "method_not_allowed"})


def serve(
    app: MCPApp,
    *,
    host: str,
    port: int,
    auth_token: str,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, int(port)), MCPHandler)
    server.app = app  # type: ignore[attr-defined]
    server.auth_token = auth_token  # type: ignore[attr-defined]
    return server


def build_client(
    args: argparse.Namespace, webui_token: str
) -> NapCatClient:
    return NapCatClient(
        webui_token,
        webui_base=args.webui_base,
        qr_path=args.qr_file,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NapCat QR MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9451)
    # MCP bearer (what RikkaHub sends) -- the fatfish web token.
    parser.add_argument("--token-env", default="FATFISH_WEB_TOKEN")
    parser.add_argument("--env-file", default=DEFAULT_ENV_PATH)
    # NapCat WebUI token (what /api/auth/login needs) -- from webui.json.
    parser.add_argument("--webui-token-env", default="NAPCAT_WEBUI_TOKEN")
    parser.add_argument("--webui-json", default=DEFAULT_WEBUI_JSON)
    parser.add_argument("--webui-base", default=DEFAULT_WEBUI_BASE)
    parser.add_argument("--qr-file", default=DEFAULT_QR_PATH)
    args = parser.parse_args(argv)

    auth_token = os.environ.get(args.token_env, "").strip()
    if not auth_token:
        auth_token = read_env_value(args.env_file, args.token_env)
    if not auth_token:
        print(
            f"[napcat-qr-mcp] missing {args.token_env}; set it in the "
            f"environment or {args.env_file}",
            file=sys.stderr,
        )
        return 2

    webui_token = os.environ.get(args.webui_token_env, "").strip()
    if not webui_token:
        webui_token = read_webui_token(args.webui_json)
    if not webui_token:
        print(
            f"[napcat-qr-mcp] missing NapCat WebUI token; set "
            f"{args.webui_token_env} or provide {args.webui_json}",
            file=sys.stderr,
        )
        return 2

    client = build_client(args, webui_token)
    app = MCPApp(client)
    server = serve(app, host=args.host, port=args.port, auth_token=auth_token)
    print(
        f"[napcat-qr-mcp] listening on http://{args.host}:{args.port}/mcp "
        f"(mcp auth {args.token_env}; webui token from "
        f"{args.webui_token_env} or {args.webui_json})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
