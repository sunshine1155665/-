"""The optional urllib3 keep-alive path must actually reuse a connection."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fatfish.llm import LLMClient
from tests.test_owner import make_env

try:
    import urllib3  # noqa: F401
except ImportError:  # pragma: no cover
    urllib3 = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    ports: list[int] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length", "0") or 0)
        self.rfile.read(length)
        type(self).ports.append(self.client_address[1])
        body = json.dumps(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@unittest.skipIf(urllib3 is None, "urllib3 is not installed")
class KeepAliveTests(unittest.TestCase):
    def test_second_chat_reuses_the_tcp_connection(self) -> None:
        _Handler.ports = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp:
            config, db, _ = make_env(Path(tmp))
            try:
                config._data.setdefault("llm", {})["keepalive"] = True
                client = LLMClient(config, db)
                client.api_key = "test-key"
                client.base_url = f"http://127.0.0.1:{server.server_port}/v1"
                client.timeout = 5.0
                with mock.patch(
                    "fatfish.llm._proxies_configured", return_value=False
                ):
                    first = client.chat([{"role": "user", "content": "hi"}])
                    second = client.chat([{"role": "user", "content": "again"}])
                self.assertEqual("ok", first.text)
                self.assertEqual("ok", second.text)
                self.assertEqual(2, len(_Handler.ports))
                self.assertEqual(
                    _Handler.ports[0],
                    _Handler.ports[1],
                    "the second request should reuse the keep-alive connection",
                )
            finally:
                db.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
