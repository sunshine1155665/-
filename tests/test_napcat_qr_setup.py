"""Offline tests for the one-click NapCat QR setup page."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import scripts.napcat_qr_setup as setup


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class EnvTests(unittest.TestCase):
    def test_read_env_ignores_comments_and_blanks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\nA=1\n\nB = two words\nC='quoted'\n",
                encoding="utf-8",
            )
            env = setup.read_env(path)
            self.assertEqual(env["A"], "1")
            self.assertEqual(env["B"], "two words")
            self.assertEqual(env["C"], "'quoted'")
            self.assertEqual(setup.read_env(Path(tmp) / "missing"), {})


class WebuiTokenTests(unittest.TestCase):
    def test_read_webui_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "webui.json"
            path.write_text(json.dumps({"token": "webui-secret"}), encoding="utf-8")
            self.assertEqual(setup.read_webui_token(path), "webui-secret")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(setup.read_webui_token(path), "")


class MCPCallTests(unittest.TestCase):
    def test_text_and_image(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "二维码在这"},
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": "cG5n",
                    },
                ],
                "isError": False,
            },
        }
        with mock.patch.object(
            setup.urllib.request, "urlopen", return_value=FakeResponse(payload)
        ):
            result = setup.mcp_call("http://x/mcp", "tok", "napcat_qr")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "二维码在这")
        self.assertEqual(result["image"], "data:image/png;base64,cG5n")

    def test_tool_error(self) -> None:
        payload = {
            "result": {
                "content": [{"type": "text", "text": "boom"}],
                "isError": True,
            }
        }
        with mock.patch.object(
            setup.urllib.request, "urlopen", return_value=FakeResponse(payload)
        ):
            result = setup.mcp_call("http://x/mcp", "tok", "napcat_qr")
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["text"])

    def test_network_error_is_friendly(self) -> None:
        with mock.patch.object(
            setup.urllib.request,
            "urlopen",
            side_effect=OSError("down"),
        ):
            result = setup.mcp_call("http://x/mcp", "tok", "napcat_qr")
        self.assertFalse(result["ok"])
        self.assertIn("调用 MCP 失败", result["text"])


class PageTests(unittest.TestCase):
    def test_render_page_contains_config_and_key(self) -> None:
        page = setup.render_page(
            key="page-key",
            token="mcp-token",
            public_mcp_url="https://example-tailnet.ts.net:9451/mcp",
            public_base="https://example-tailnet.ts.net:9452",
            webui_token="webui-secret",
            public_webui_url="https://example-tailnet.ts.net:9460/webui/",
        )
        self.assertIn("page-key", page)
        self.assertIn("mcp-token", page)
        self.assertIn("webui-secret", page)
        self.assertIn("streamable_http", page)
        self.assertIn("https://example-tailnet.ts.net:9451/mcp", page)
        self.assertIn("https://example-tailnet.ts.net:9460/webui/", page)
        self.assertIn("Authorization", page)
        self.assertIn("openWebui", page)
        self.assertIn("refreshQR", page)

    def test_config_json(self) -> None:
        config = setup._config_json("https://x/mcp", "tok")
        parsed = json.loads(config)
        server = parsed["mcpServers"]["fatfishnapcatqr"]
        self.assertEqual(server["type"], "streamable_http")
        self.assertEqual(server["url"], "https://x/mcp")
        self.assertEqual(server["headers"]["Authorization"], "Bearer tok")


class AuthTests(unittest.TestCase):
    def test_key_compare(self) -> None:
        handler = object.__new__(setup.SetupHandler)
        handler.server = SimpleNamespace(access_key="secret-key")
        self.assertTrue(handler._key_ok("secret-key"))
        self.assertFalse(handler._key_ok("wrong"))
        self.assertFalse(handler._key_ok(""))


if __name__ == "__main__":
    unittest.main()
