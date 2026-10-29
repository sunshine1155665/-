"""Offline tests for the NapCat QR MCP tool (scripts/napcat_qr_mcp.py)."""

from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import scripts.napcat_qr_mcp as mcp


class TokenFileTests(unittest.TestCase):
    def test_read_webui_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "webui.json"
            path.write_text(json.dumps({"token": "webui-secret"}), encoding="utf-8")
            self.assertEqual(mcp.read_webui_token(path), "webui-secret")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(mcp.read_webui_token(path), "")
            self.assertEqual(mcp.read_webui_token(Path(tmp) / "missing"), "")


class FakeRunner:
    def __init__(self, running: bool = False, returncode: int = 0):
        self.running = running
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        stdout = "true" if self.running else "false"
        return SimpleNamespace(returncode=self.returncode, stdout=stdout, stderr="")


class NapCatClientTests(unittest.TestCase):
    def make_client(self, qr_path: Path, runner=None):
        return mcp.NapCatClient(
            "webui-token",
            qr_path=qr_path,
            runner=runner or FakeRunner(),
            sleep=lambda _s: None,
        )

    def test_stale_credential_relogins_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_client(Path(tmp) / "qrcode.png")
            responses = [
                {"data": {"Credential": "c1"}},
                {"code": -1, "message": "Unauthorized"},
                {"data": {"Credential": "c2"}},
                {"code": 0, "data": {"isLogin": True, "isOffline": False}},
            ]
            with mock.patch.object(mcp, "post_json", side_effect=responses) as post:
                status = client.status()
            self.assertTrue(client.online(status))
            self.assertEqual(post.call_count, 4)

    def test_status_online(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_client(Path(tmp) / "qrcode.png")
            responses = [
                {"data": {"Credential": "cred"}},
                {"code": 0, "data": {"isLogin": True, "isOffline": False}},
            ]
            with mock.patch.object(mcp, "post_json", side_effect=responses):
                status = client.status()
            self.assertTrue(client.online(status))

    def test_qr_short_circuits_when_online(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_client(Path(tmp) / "qrcode.png")
            client.status = lambda: {"isLogin": True, "isOffline": False}
            client.ensure_running = lambda: None
            result = client.qr()
            self.assertTrue(result["online"])
            self.assertIsNone(result["png"])

    def test_qr_refreshes_and_reads_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qr = Path(tmp) / "qrcode.png"
            qr.write_bytes(b"old")
            client = self.make_client(qr)
            client.status = lambda: {"isLogin": False, "isOffline": True}
            client.ensure_running = lambda: None

            def refresh(path, payload=None):
                qr.write_bytes(b"new-qr-png")
                return {"code": 0}

            client._call = refresh  # type: ignore[method-assign]
            result = client.qr()
            self.assertFalse(result["online"])
            self.assertEqual(result["png"], b"new-qr-png")

    def test_qr_recognises_already_logged_in_refresh_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_client(Path(tmp) / "qrcode.png")
            client.status = lambda: {"isLogin": False, "isOffline": True}
            client.ensure_running = lambda: None
            client._call = lambda path, payload=None: {  # type: ignore[method-assign]
                "code": -1,
                "message": "QQ Is Logined",
            }
            result = client.qr()
            self.assertTrue(result["online"])

    def test_qr_refresh_error_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_client(Path(tmp) / "qrcode.png")
            client.status = lambda: {"isLogin": False, "isOffline": True}
            client.ensure_running = lambda: None
            client._call = lambda path, payload=None: {  # type: ignore[method-assign]
                "code": -1,
                "message": "boom",
            }
            with self.assertRaises(RuntimeError):
                client.qr()

    def test_status_payload_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_client(Path(tmp) / "qrcode.png")
            client.status = lambda: (_ for _ in ()).throw(RuntimeError("down"))
            payload = client.status_payload()
            self.assertFalse(payload["reachable"])
            self.assertIn("down", payload["error"])

    def test_ensure_running_starts_supervisor(self) -> None:
        runner = FakeRunner(running=False)
        with tempfile.TemporaryDirectory() as tmp:
            client = self.make_client(Path(tmp) / "qrcode.png", runner=runner)
            attempts = {"n": 0}

            def status():
                attempts["n"] += 1
                if attempts["n"] < 2:
                    raise RuntimeError("not up")
                return {"isLogin": False}

            client.status = status
            client.ensure_running()
            self.assertTrue(
                any("systemctl" in " ".join(call) for call in runner.calls)
            )


class MCPAppTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, online: bool = False, png: bytes = b""):
            self._online = online
            self._png = png

        def status_payload(self):
            return {"reachable": True, "online": self._online}

        def qr(self):
            return {
                "online": self._online,
                "png": None if self._online else self._png,
                "status": {"loginPhase": "ready" if self._online else "waiting_qrcode"},
            }

    def app(self, **kwargs):
        return mcp.MCPApp(self.FakeClient(**kwargs))

    def test_initialize_and_tools_list(self) -> None:
        app = self.app()
        response, session = app.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26"},
            }
        )
        self.assertIsNotNone(session)
        self.assertEqual(response["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", response["result"]["capabilities"])
        tools = app.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0]
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertEqual(names, {mcp.TOOL_QR, mcp.TOOL_STATUS})

    def test_tools_call_status(self) -> None:
        app = self.app(online=True)
        response, _ = app.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": mcp.TOOL_STATUS, "arguments": {}},
            }
        )
        self.assertFalse(response["result"]["isError"])
        self.assertIn("text", response["result"]["content"][0]["type"])

    def test_tools_call_qr_online(self) -> None:
        app = self.app(online=True)
        response, _ = app.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": mcp.TOOL_QR, "arguments": {}},
            }
        )
        self.assertIn("已在线", response["result"]["content"][0]["text"])

    def test_tools_call_qr_returns_image(self) -> None:
        app = self.app(online=False, png=b"png-bytes")
        response, _ = app.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": mcp.TOOL_QR, "arguments": {}},
            }
        )
        content = response["result"]["content"]
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(
            base64.b64decode(content[1]["data"]), b"png-bytes"
        )

    def test_tool_exception_is_iserror(self) -> None:
        class Boom:
            def status_payload(self):
                raise RuntimeError("boom")

            def qr(self):
                raise RuntimeError("boom")

        app = mcp.MCPApp(Boom())
        response, _ = app.handle(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": mcp.TOOL_QR, "arguments": {}},
            }
        )
        self.assertTrue(response["result"]["isError"])

    def test_unknown_method_and_notification(self) -> None:
        app = self.app()
        response, _ = app.handle({"jsonrpc": "2.0", "id": 7, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)
        self.assertEqual(
            app.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            (None, None),
        )

    def test_batch(self) -> None:
        app = self.app()
        response, _ = app.handle_batch(
            [
                {"jsonrpc": "2.0", "id": 8, "method": "ping"},
                {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
            ]
        )
        self.assertIsInstance(response, list)
        self.assertEqual(len(response), 2)


class AuthTests(unittest.TestCase):
    def test_bearer_token(self) -> None:
        handler = object.__new__(mcp.MCPHandler)
        handler.headers = {"Authorization": "Bearer secret"}
        handler.server = SimpleNamespace(auth_token="secret")
        self.assertTrue(handler._authorized())
        handler.headers = {"Authorization": "Bearer wrong"}
        self.assertFalse(handler._authorized())
        handler.headers = {}
        self.assertFalse(handler._authorized())


if __name__ == "__main__":
    unittest.main()
