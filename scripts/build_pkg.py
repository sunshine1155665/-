#!/usr/bin/env python3
"""Build the fatfish wheel + sdist offline and prove the wheel actually works.

Why not ``python -m build`` / ``pip wheel``: neither is guaranteed to be
installed, and both want network unless told not to.  This script drives the
PEP 517 backend that is already on the box (``setuptools.build_meta``) in
process, so it needs nothing beyond the standard library + setuptools + wheel.

The source tree is staged into a scratch directory before building, so the
working tree never gets a ``build/`` or ``*.egg-info/`` dropped into it.

Usage (run from anywhere):

    python scripts/build_pkg.py            # build, print name/size/sha256
    python scripts/build_pkg.py --check    # + assert the data files are inside
    python scripts/build_pkg.py --smoke    # + install the wheel in a throwaway
                                           #   venv and create a real database
    python scripts/build_pkg.py --check --smoke
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Resources that must travel inside the distribution.  Kept in sync with
# pyproject ``[tool.setuptools.package-data]`` by tests/test_packaging.py.
REQUIRED_WHEEL_MEMBERS = (
    "fatfish/schema.sql",
    "fatfish/lexicon/seed.json",
)

_SMOKE_CODE = r"""
import json, pathlib, sys

db_path = pathlib.Path(sys.argv[1])
import fatfish

pkg_dir = pathlib.Path(fatfish.__file__).resolve().parent
schema = pkg_dir / "schema.sql"
seed = pkg_dir / "lexicon" / "seed.json"
if not schema.is_file():
    raise SystemExit(f"missing packaged schema: {schema}")
if not seed.is_file():
    raise SystemExit(f"missing packaged seed: {seed}")

from fatfish.db import Database
from fatfish.lexicon import load_seed_entries

db = Database(db_path)
created = db.ensure_schema()
tables = {
    row[0]
    for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
}
if created is not True:
    raise SystemExit(f"ensure_schema() should have initialised a fresh db, got {created!r}")
if "memory_items" not in tables or "schema_meta" not in tables:
    raise SystemExit(f"schema incomplete, tables={sorted(tables)[:8]}")
entries = load_seed_entries()
if not entries:
    raise SystemExit("seed.json loaded but empty")

print(
    json.dumps(
        {
            "version": fatfish.__version__,
            "package_dir": str(pkg_dir),
            "ensure_schema": created,
            "tables": len(tables),
            "seed_entries": len(entries),
        }
    )
)
"""


def _version() -> str:
    sys.path.insert(0, str(ROOT))
    try:
        import fatfish
    finally:
        sys.path.pop(0)
    return fatfish.__version__


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage(dest: Path) -> None:
    """Copy just what the build needs into ``dest`` (no build leftovers in git)."""
    shutil.copy2(ROOT / "pyproject.toml", dest / "pyproject.toml")
    for extra in ("README.md", "LICENSE"):
        source = ROOT / extra
        if source.is_file():
            shutil.copy2(source, dest / extra)
    shutil.copytree(
        ROOT / "fatfish",
        dest / "fatfish",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def build(scratrch: Path) -> list[Path]:
    """Build wheel + sdist into ``scratrch`` and return the artefact paths."""
    try:
        from setuptools import build_meta
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SystemExit(f"setuptools is required to build: {exc}")

    outdir = scratrch / "out"
    stage = scratrch / "src"
    outdir.mkdir(parents=True, exist_ok=True)
    stage.mkdir(parents=True, exist_ok=True)
    _stage(stage)

    cwd = os.getcwd()
    os.chdir(stage)
    log = io.StringIO()
    try:
        # setuptools narrates every copied file; keep it for failures only.
        with contextlib.redirect_stdout(log):
            wheel_name = build_meta.build_wheel(str(outdir))
            sdist_name = build_meta.build_sdist(str(outdir))
    except Exception:
        print(log.getvalue()[-4000:], file=sys.stderr)
        raise
    finally:
        os.chdir(cwd)
    return [outdir / wheel_name, outdir / sdist_name]


def check(artefacts: list[Path]) -> list[str]:
    """Return a list of human-readable problems (empty == good)."""
    problems: list[str] = []
    for artefact in artefacts:
        if artefact.suffix == ".whl":
            with zipfile.ZipFile(artefact) as archive:
                names = set(archive.namelist())
            for member in REQUIRED_WHEEL_MEMBERS:
                if member not in names:
                    problems.append(f"wheel is missing {member}")
        elif artefact.name.endswith(".tar.gz"):
            import tarfile

            with tarfile.open(artefact) as archive:
                names = {name.split("/", 1)[-1] for name in archive.getnames()}
            for member in REQUIRED_WHEEL_MEMBERS:
                if member not in names:
                    problems.append(f"sdist is missing {member}")
        else:  # pragma: no cover - defensive
            problems.append(f"unexpected artefact type: {artefact.name}")
    return problems


def smoke(wheel: Path, workdir: Path) -> dict[str, object]:
    """Install ``wheel`` into a fresh venv (offline) and create a database."""
    venv_dir = workdir / "venv"
    venv_python = (
        venv_dir / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else venv_dir / "bin" / "python"
    )
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)], check=True, capture_output=True
    )
    if not venv_python.is_file():  # pragma: no cover - broken interpreter
        raise SystemExit(f"venv does not provide a python: {venv_python}")

    install = subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--disable-pip-version-check",
            "--no-input",
            str(wheel),
        ],
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        raise SystemExit(
            "offline pip install failed:\n"
            f"{install.stdout[-2000:]}\n{install.stderr[-2000:]}"
        )

    # Run from a directory with no fatfish/ in it, so the installed copy is
    # what gets imported (never the source tree).
    sandbox = workdir / "run"
    sandbox.mkdir(parents=True, exist_ok=True)
    db_path = sandbox / "smoke.db"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    run = subprocess.run(
        [str(venv_python), "-I", "-c", _SMOKE_CODE, str(db_path)],
        cwd=str(sandbox),
        capture_output=True,
        text=True,
        env=env,
    )
    if run.returncode != 0:
        raise SystemExit(
            "wheel smoke test failed:\n"
            f"{run.stdout[-2000:]}\n{run.stderr[-2000:]}"
        )
    payload = json.loads(run.stdout.strip().splitlines()[-1])
    if payload["version"] != _version():
        raise SystemExit(
            f"installed version {payload['version']!r} != source {_version()!r}"
        )
    return payload


def _publish(artefacts: list[Path], target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    published = []
    for artefact in artefacts:
        destination = target / artefact.name
        shutil.copy2(artefact, destination)
        published.append(destination)
    return published


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="verify data files are inside the artefacts"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="install the wheel into a throwaway venv and create a database",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=None,
        help="output directory (default: dist/pkg/<version>)",
    )
    args = parser.parse_args(argv)

    version = _version()
    target = (args.dist_dir or ROOT / "dist" / "pkg" / version).resolve()

    # Scratch space stays inside dist/ (git-ignored): short, space-free path and
    # no worktree pollution if the build dies half way.
    scratch_base = ROOT / "dist" / "_build"
    scratch_base.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="pkg-", dir=str(scratch_base)))
    failures = 0
    try:
        artefacts = _publish(build(scratch), target)
        for artefact in artefacts:
            size = artefact.stat().st_size
            print(f"{artefact.name}\t{size} bytes\tsha256:{_sha256(artefact)}")
        print(f"python {sys.version.split()[0]}, build dir {target}")

        if args.check:
            problems = check(artefacts)
            if problems:
                failures += 1
                for problem in problems:
                    print(f"CHECK FAIL: {problem}", file=sys.stderr)
            else:
                print("CHECK OK: " + ", ".join(REQUIRED_WHEEL_MEMBERS) + " present")

        if args.smoke:
            wheel = next(a for a in artefacts if a.suffix == ".whl")
            payload = smoke(wheel, scratch)
            print(
                "SMOKE OK: installed fatfish "
                f"{payload['version']} in a clean venv, ensure_schema() created "
                f"{payload['tables']} tables, {payload['seed_entries']} seed entries"
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            scratch_base.rmdir()  # only when empty; leftovers mean keep for debugging
        except OSError:
            pass
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
