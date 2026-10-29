"""Upload the current best audio to a public URL so it can be listened to away
from this machine.

The A/B page is self-contained (audio is embedded as MP3 data URIs), so one
upload gives every take plus the scores. Individual wav takes are uploaded as
MP3 too — browsers play ``audio/mpeg`` inline, while ``.wav`` from an anonymous
host arrives as ``application/octet-stream`` and gets downloaded instead.

    python scripts/upload_audio.py --page data/soulx/melody_ab.html \
        --audio data/soulx/melody_v11_s64/full.wav --hours 72

Host: litterbox (catbox's temporary host). Links expire, by design — this is for
"listen now", not for archiving. ``catbox.moe`` (permanent) resets connections
from this network, so it is not used here.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import random
import string
import sys
import urllib.error
import urllib.request
from pathlib import Path

LITTERBOX = "https://litterbox.catbox.moe/resources/internals/api.php"
HOURS = ("1h", "12h", "24h", "72h")


def _boundary() -> str:
    return "----fatfish" + "".join(
        random.choice(string.ascii_letters + string.digits) for _ in range(16))


def multipart(fields: dict[str, str], name: str, filename: str,
              data: bytes, ctype: str) -> tuple[bytes, str]:
    """Encode one file + simple fields as ``multipart/form-data``.

    Returns ``(body, content_type)``. Hand-rolled because the standard library
    has no multipart encoder and this must not pull in a dependency.
    """
    b = _boundary()
    out = bytearray()
    for key, val in fields.items():
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n".encode()
        out += str(val).encode() + b"\r\n"
    out += (f"--{b}\r\nContent-Disposition: form-data; name=\"{name}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    out += data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"


def upload(path: Path, hours: str = "72h", timeout: float = 180.0) -> str:
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body, ct = multipart({"reqtype": "fileupload", "time": hours},
                         "fileToUpload", path.name, path.read_bytes(), ctype)
    req = urllib.request.Request(LITTERBOX, data=body, method="POST",
                                 headers={"Content-Type": ct,
                                          "User-Agent": "fatfish-upload/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace").strip()
    if not text.startswith("http"):
        raise SystemExit(f"upload failed for {path.name}: {text[:200]}")
    return text


def as_mp3(src: Path, out_dir: Path) -> Path:
    """Best-effort MP3 copy (inline playback); falls back to the original wav."""
    try:
        import soundfile as sf
    except Exception:  # noqa: BLE001 - optional, only helps the browser
        return src
    if src.suffix.lower() != ".wav":
        return src
    dst = out_dir / (src.stem + ".mp3")
    try:
        y, sr = sf.read(str(src), dtype="float32", always_2d=False)
        sf.write(str(dst), y, sr, format="MP3", subtype="MPEG_LAYER_III")
    except Exception:  # noqa: BLE001 - libsndfile may lack MP3 support
        return src
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把成品音频传到临时外链")
    ap.add_argument("--page", nargs="*", default=[],
                    help="自包含的 HTML（音频内嵌），原样上传")
    ap.add_argument("--audio", nargs="*", default=[],
                    help="wav/mp3；wav 会尽量转成 mp3 以内联播放")
    ap.add_argument("--hours", choices=HOURS, default="72h")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    args = ap.parse_args(argv)

    tmp = Path(args.json).parent if args.json else Path(".")
    out: list[dict] = []
    try:
        for p in args.page:
            src = Path(p)
            url = upload(src, args.hours)
            out.append({"file": str(src), "url": url, "kind": "page"})
            print(f"{src.name} -> {url}")
        for p in args.audio:
            src = Path(p)
            up = as_mp3(src, tmp)
            url = upload(up, args.hours)
            out.append({"file": str(src), "uploaded": str(up), "url": url,
                        "kind": "audio"})
            print(f"{src.name} -> {url}")
    except urllib.error.URLError as e:  # network down / host blocked
        print(f"upload error: {e}", file=sys.stderr)
        return 2
    if args.json:
        Path(args.json).write_text(
            json.dumps({"hours": args.hours, "items": out}, ensure_ascii=False,
                       indent=1), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
