"""Seed ``data/`` with the shipped voice/song assets (first start, once).

The repo ships a small set of pre-made assets under ``assets/`` (a voice
reference clip and the settled song prompt/melody).  ``data/`` is git-ignored,
so a fresh clone is empty; run this script once before the first start and it
copies each asset to the path the product expects.  Idempotent: an existing
destination file is left untouched and just reported.

Stdlib only.  Usage::

    python scripts/install_assets.py            # copy what is missing
    python scripts/install_assets.py --dry-run  # show the plan, change nothing
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# (source under assets/, destination under data/)
PLAN = (
    ("voice/cand_home_up1.wav", "whale_voice_demo_v2/refs/cand_home_up1.wav"),
    ("song/prompt_v6.json", "soulx/prompt_v6.json"),
    ("song/persona_song.json", "soulx/persona_song/report.json"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install_assets.py",
        description="Copy the shipped assets/ into data/ (idempotent).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be copied without writing anything",
    )
    args = parser.parse_args(argv)

    data_dir = ROOT / "data"
    missing_sources = [src for src, _ in PLAN if not (ASSETS / src).is_file()]
    if missing_sources:
        for src in missing_sources:
            print(f"error: missing asset file: assets/{src}", file=sys.stderr)
        return 2

    actions: list[tuple[Path, Path]] = []
    for src, dst in PLAN:
        source = ASSETS / src
        target = data_dir / dst
        if target.is_file():
            print(f"skip (exists): data/{dst}")
        else:
            actions.append((source, target))
            print(f"copy : assets/{src} -> data/{dst}")

    if args.dry_run:
        print(f"dry-run: {len(actions)} file(s) would be copied.")
        return 0

    for source, target in actions:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    print(f"done: {len(actions)} file(s) copied, data/ is ready for first start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
