#!/usr/bin/env python3
"""Tiny local static server for the Electron/Edge pet shell.

Serving the Live2D assets over HTTP avoids ``file://`` fetch restrictions in
Chromium/WebView2.  It binds loopback only and serves ``pet-shell/``.

    python scripts/pet_shell_server.py --root pet-shell --port 9951
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        # Same-origin TTS proxy: the renderer never talks to 9880 directly
        # (avoids CORS and keeps the TTS endpoint local-only).
        if urlsplit(self.path).path != "/tts":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400)
            return
        if length > 64 * 1024:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length > 0 else b"{}"
        request = urllib.request.Request(
            "http://127.0.0.1:9880/tts",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                self.send_response(200)
                self.send_header("Content-Type", response.headers.get("Content-Type", "audio/wav"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                shutil.copyfileobj(response, self.wfile, 64 * 1024)  # stream, no full buffer
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            payload = json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            ).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="pet-shell")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9951)
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    if not (root / "renderer" / "index.html").is_file():
        print(f"missing shell root: {root}", file=sys.stderr)
        return 2
    os.chdir(root)
    server = http.server.ThreadingHTTPServer(
        (args.host, args.port), lambda *a, **k: Handler(*a, directory=str(root), **k)
    )
    print(f"pet shell server on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
