"""Serve the audition folder over Tailscale, so the audio can be heard from a
phone or another machine without uploading anything to the public internet.

    python scripts/serve_audio.py --dir data/soulx --host 100.x.y.z --port 8899

Why not a file host: the public anonymous hosts are either blocked or slow from
where the user listens (catbox/litterbox timed out, pixeldrain reset). The
machines are already on the same tailnet, and Tailscale gives a direct link, so
serving the folder from here is both faster and private.

``Range`` support is the part that matters: mobile Safari asks for a byte range
before it plays anything, and ``http.server`` answers 200 with the whole file,
which makes the audio element stall. ``parse_range`` is unit-tested.
"""

from __future__ import annotations

import argparse
import http.server
import os
import socket
import sys
from functools import partial
from urllib.parse import unquote, urlparse


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """``"bytes=100-200"`` -> ``(100, 200)``; None means "send the whole file".

    Only single ranges are handled (that is all a browser sends for audio), and
    an unsatisfiable/oddly formatted header falls back to the whole file rather
    than erroring — a 200 is always safe.
    """
    if not header or not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].split(",")[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":                      # "bytes=-500" = last 500 bytes
            n = int(end_s)
            if n <= 0:
                return None
            start, end = max(0, size - n), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Static files with byte ranges and no directory index (audio only)."""

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet-ish
        sys.stderr.write("[serve] %s\n" % (fmt % args))

    def _send_range(self, path: str, rng: tuple[int, int], size: int) -> None:
        start, end = rng
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()

    def send_head(self):
        path = self.translate_path(unquote(urlparse(self.path).path))
        if os.path.isdir(path):
            index = os.path.join(path, "index.html")
            if os.path.isfile(index):
                return super().send_head()
            self.send_error(404, "no index.html")
            return None
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "not found")
            return None
        size = os.fstat(f.fileno()).st_size
        rng = parse_range(self.headers.get("Range"), size)
        if rng is None:
            # Whole file, but advertise range support so the player can seek.
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            return f
        self._send_range(path, rng, size)
        f.seek(rng[0])
        self._remaining = rng[1] - rng[0] + 1
        return f

    def copyfile(self, source, outputfile) -> None:
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self._remaining = None


def tailscale_ip() -> str | None:
    """The machine's 100.x address, if Tailscale is up (no CLI dependency)."""
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr.startswith("100."):
                return addr
    except OSError:
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用 Tailscale 内网直接听音频")
    ap.add_argument("--dir", default="data/soulx")
    ap.add_argument("--host", default=None,
                    help="绑定地址；默认用 Tailscale 的 100.x 地址")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args(argv)

    host = args.host or tailscale_ip() or "127.0.0.1"
    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        raise SystemExit(f"no such dir: {root}")
    handler = partial(RangeHandler, directory=root)
    httpd = http.server.ThreadingHTTPServer((host, args.port), handler)
    print(f"serving {root}")
    print(f"  http://{host}:{args.port}/melody_ab.html")
    print(f"  http://{host}:{args.port}/listen.html")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
