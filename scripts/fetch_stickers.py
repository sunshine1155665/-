"""Fetch the sticker pack into ``data/stickers/`` (idempotent).

The sticker images are not committed to the repo; they live in an upstream
archive.  Run this once after cloning and the sticker features (sticker
retrieval, auto-reply) have material to work with.  Without it, the sticker
features degrade gracefully (``missing_assets``), so this is optional.

Stdlib only.  Usage::

    python scripts/fetch_stickers.py            # download + extract
    python scripts/fetch_stickers.py --dry-run  # show the plan only

TODO(boss): 待补来源链接。把 STICKER_URL 换成真实的 release zip / 归档地址
（来源仓库与许可证登记在 THIRD_PARTY_NOTICES.md）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "data" / "stickers"

# TODO(boss): 待补链接（表情包库来源 URL）。
STICKER_URL = ""  # e.g. "https://github.com/<owner>/<repo>/releases/download/v1/stickers.zip"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fetch_stickers.py",
        description="Download the sticker pack into data/stickers/ (idempotent).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show the plan without downloading"
    )
    parser.add_argument(
        "--url", default=STICKER_URL, help="override the sticker archive URL"
    )
    args = parser.parse_args(argv)

    if not args.url:
        print(
            "error: sticker source URL is not configured yet (TODO(boss)).\n"
            "Set scripts/fetch_stickers.py STICKER_URL or pass --url <zip>.\n"
            "Until then the sticker features run in degraded mode (no images).",
            file=sys.stderr,
        )
        return 2

    if DEST.is_dir() and any(DEST.iterdir()):
        print(f"skip (exists): {DEST} already has content.")
        return 0

    if args.dry_run:
        print(f"dry-run: would download {args.url} -> {DEST}")
        return 0

    DEST.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fatfish-stickers-") as tmp:
        archive = Path(tmp) / "stickers.zip"
        print(f"downloading {args.url} ...")
        with urllib.request.urlopen(args.url) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        print(f"extracting into {DEST} ...")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(DEST)
    print("done: sticker pack installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
