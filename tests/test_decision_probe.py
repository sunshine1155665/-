from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fatfish.decision import probe as probe_module

SYSTEM_ONE_RESPONSE = {
    "model": "fake-system-one",
    "answers": {
        "is_weather": {"type": "noul", "noul": 0.87},
        "handler": {
            "type": "choice",
            "choice": "weather",
            "confidence": 0.62,
            "probabilities": {"weather": 0.62, "memory": 0.38},
        },
    },
}


class _SystemOneHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/v1/systemone":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.server.seen_payload = json.loads(raw.decode("utf-8"))
        body = json.dumps(SYSTEM_ONE_RESPONSE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        # Keep the test output quiet.
        return


class DecisionProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _SystemOneHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address[:2]
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_probe_parses_systemone_response(self) -> None:
        result = probe_module.probe(self.base_url, model="fake-model", timeout=2.0)

        self.assertTrue(result["ok"])
        self.assertIsInstance(result["latency_ms"], float)
        self.assertGreaterEqual(result["latency_ms"], 0.0)
        self.assertEqual(result["answers"]["is_weather"]["noul"], 0.87)
        self.assertEqual(result["answers"]["handler"]["choice"], "weather")

        payload = self.server.seen_payload
        self.assertEqual(payload["model"], "fake-model")
        self.assertIn("state", payload)
        self.assertEqual(payload["questions"]["is_weather"]["type"], "noul")
        self.assertEqual(payload["questions"]["handler"]["type"], "choice")

    def test_cli_prints_json_and_exits_zero(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = probe_module.main(
                ["--base-url", self.base_url, "--model", "fake-model", "--timeout", "2"]
            )

        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())
        self.assertTrue(data["ok"])
        self.assertIn("latency_ms", data)
        self.assertEqual(data["answers"]["handler"]["choice"], "weather")

    def test_connection_failure_exits_two(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"

        with self.assertRaises(probe_module.ProbeError):
            probe_module.probe(url, timeout=0.5)

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = probe_module.main(["--base-url", url, "--timeout", "0.5"])

        self.assertEqual(code, 2)
        self.assertIn("cannot reach", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
