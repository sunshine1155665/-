"""Build a copyable low-profile bundle in dist/fatfish-low."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "fatfish-low"


def ignore(_dir: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in {"__pycache__", ".pytest_cache", ".git", "data", "dist"}
        or name.endswith((".pyc", ".db", ".db-wal", ".db-shm"))
    }


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    shutil.copytree(ROOT / "fatfish", DIST / "fatfish", ignore=ignore)
    shutil.copytree(ROOT / "config", DIST / "config", ignore=ignore)
    shutil.copytree(ROOT / "scripts", DIST / "scripts", ignore=ignore)
    shutil.copytree(ROOT / "tests", DIST / "tests", ignore=ignore)
    for name in ("pyproject.toml", "README.md", ".env.example", "requirements.txt"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, DIST / name)
    (DIST / "data" / "stickers").mkdir(parents=True, exist_ok=True)
    print(f"bundle ready: {DIST}")
    print("copy this directory to the target machine, then run:")
    print("  python -m fatfish doctor")
    print("  python -m fatfish init")
    print("  python -m fatfish demo")


if __name__ == "__main__":
    main()
