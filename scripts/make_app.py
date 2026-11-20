"""Build the distributable fatfish app package.

This script is intentionally stdlib-only and readable.  It copies the runtime
source into ``dist/fatfish-app``, writes launch scripts and a SHA-256 manifest,
optionally creates a zip with a top-level ``fatfish-app/`` folder, and can run
an isolated verification against a temporary copy of the package.

Usage:
    python scripts/make_app.py
    python scripts/make_app.py --verify
    python scripts/make_app.py --clean --verify
    python scripts/make_app.py --dest /tmp/fatfish-app --no-zip
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

APP_NAME = "fatfish-app"
CLEAN_APP_NAME = "fatfish-oneclick"
ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = ROOT / "dist"
DEFAULT_DEST = DIST_ROOT / APP_NAME
DEFAULT_CLEAN_DEST = DIST_ROOT / CLEAN_APP_NAME

PACKAGE_DIRS = ("fatfish", "web", "config", "docs", "tests")
CLEAN_PACKAGE_DIRS = ("fatfish", "web", "config", "docs")

# Runtime/dev scripts worth shipping.  Voice-matrix experiments stay behind.
SCRIPT_FILES = (
    "start_all.py",
    "make_app.py",
    "install_assets.py",
    "backup.bat",
    "doctor.bat",
    "init.bat",
    "nightly.bat",
    "start.bat",
    "start_tts_soft.bat",
    "stop_tts_soft.bat",
    "cosyvoice_soft_server.py",
    "stop.bat",
    "setup_napcat.ps1",
    "start_napcat.bat",
    "start_bili_login.bat",
    "bili_login_ui.py",
    "start_qq_login.bat",
    "qq_login_ui.py",
    "start_qzone_login.bat",
    "qzone_login_ui.py",
    "napcat_supervisor.py",
    "napcat_watch.py",
    "napcat_qr_mcp.py",
    "napcat_qr_setup.py",
    "qqbot_service.py",
    "pet_gateway.py",
    "sing_server.py",
    "photo_server.py",
    "serve_stats.py",
)

# Subdirectories of scripts/ shipped verbatim with the full bundle.
# deploy/ holds sync_cloud.sh + its test; without it every cloud deploy
# wipes the helper and the next run has to re-scp it from /tmp.
SCRIPT_SUBDIRS = ("deploy",)

# The minimal one-click bundle ships only what a user needs to run it.
CLEAN_SCRIPT_FILES = (
    "start_all.py",
    "start_bili_login.bat",
    "bili_login_ui.py",
    "start_qq_login.bat",
    "qq_login_ui.py",
    "start_qzone_login.bat",
    "qzone_login_ui.py",
    "start_napcat.bat",
    "setup_napcat.ps1",
    "start_tts_soft.bat",
    "stop_tts_soft.bat",
    "cosyvoice_soft_server.py",
)

ROOT_FILES = (
    "README.md",
    "AGENTS.md",
    "pyproject.toml",
    "requirements.txt",
    ".env.example",
    ".gitignore",
    "LICENSE",
)

CLEAN_ROOT_FILES = (
    "README.md",
    "requirements.txt",
    ".env.example",
    "LICENSE",
)

DATA_DIRS = ("stickers", "audio", "archive", "backups", "eval", "logs", "run")

IGNORED_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    "repos",
    "models",
    "downloads",
    "notes",
    "dist",
}
IGNORED_SUFFIXES = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".zip")
IGNORED_FILES = {".env"}

START_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 goto no_python
python -m fatfish serve --open
if errorlevel 1 (
  echo.
  echo [fatfish] the web server stopped with an error.
  pause
)
exit /b 0

:no_python
echo.
echo [fatfish] Python was not found on PATH.
echo [fatfish] Install Python 3.11 or newer from https://www.python.org/downloads/
echo [fatfish] and enable "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1
"""

START_WEB_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 goto no_python
python -m fatfish serve --open
if errorlevel 1 (
  echo.
  echo [fatfish] the web server stopped with an error.
  pause
)
exit /b 0

:no_python
echo.
echo [fatfish] Python was not found on PATH.
echo [fatfish] Install Python 3.11 or newer from https://www.python.org/downloads/
echo [fatfish] and enable "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1
"""

START_ALL_BAT = r"""@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

python --version >nul 2>&1
if errorlevel 1 goto no_python

python scripts\start_all.py %*
if errorlevel 1 (
  echo.
  echo [大肥鱼] 启动失败，请把上面的信息发给维护者。
  pause
  exit /b 1
)
exit /b 0

:no_python
echo.
echo [大肥鱼] 没有找到 Python。
echo 请安装 Python 3.11 或更新版本：https://www.python.org/downloads/
echo 安装时勾选 "Add python.exe to PATH"，然后重新双击本文件。
echo.
pause
exit /b 1
"""

INIT_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
python -m fatfish init
echo.
pause
"""

DOCTOR_BAT = r"""@echo off
setlocal
cd /d "%~dp0"
python -m fatfish doctor
echo.
pause
"""

START_SH = """#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
exec python3 -m fatfish serve --open
"""

INIT_SH = """#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
exec python3 -m fatfish init
"""

DOCTOR_SH = """#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
exec python3 -m fatfish doctor
"""

BILI_LOGIN_BAT = r"""@echo off
cd /d "%~dp0"
call "scripts\start_bili_login.bat"
exit /b %errorlevel%
"""

QQ_LOGIN_BAT = r"""@echo off
cd /d "%~dp0"
call "scripts\start_qq_login.bat"
exit /b %errorlevel%
"""

QZONE_LOGIN_BAT = r"""@echo off
cd /d "%~dp0"
call "scripts\start_qzone_login.bat"
exit /b %errorlevel%
"""


class BuildError(Exception):
    """A packaging problem that should be reported without a traceback."""


class VerifyError(Exception):
    """A failed isolated check."""


# --------------------------------------------------------------------------- version
def read_version(root: Path | None = None) -> str:
    """Read ``__version__`` from ``fatfish/__init__.py`` without importing it."""
    root = root or ROOT
    init_path = root / "fatfish" / "__init__.py"
    try:
        source = init_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read {init_path}: {exc}") from exc
    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    try:
                        value = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        value = None
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    match = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", source)
    if not match:
        raise BuildError(f"no __version__ in {init_path}")
    return match.group(1).strip()


# --------------------------------------------------------------------------- build
def _ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        lowered = name.lower()
        if name in IGNORED_DIRS or name in IGNORED_FILES:
            ignored.add(name)
        elif lowered.startswith(".venv"):
            ignored.add(name)
        elif name.endswith(IGNORED_SUFFIXES):
            ignored.add(name)
    return ignored


def _iter_files(root: Path, *, include_manifest: bool = False) -> list[Path]:
    """All regular files under *root*, in stable relative-path order."""
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS or part.startswith(".venv") for part in parts):
            continue
        if path.name in IGNORED_FILES:
            continue
        if path.suffix in {".pyc", ".pyo", ".zip"}:
            continue
        if not include_manifest and path.name == "manifest.json":
            continue
        files.append(path)
    files.sort(key=lambda item: item.relative_to(root).as_posix())
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text(path: Path, text: str, *, newline: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if newline is None:
        path.write_text(text, encoding="utf-8")
    else:
        path.write_text(text, encoding="utf-8", newline=newline)


def _write_launch_scripts(dest: Path, *, clean_bundle: bool = False) -> None:
    _write_text(dest / "启动大肥鱼.bat", START_ALL_BAT, newline="\r\n")
    _write_text(dest / "start.bat", START_BAT, newline="\r\n")
    # Root-level Chinese launchers for the click-to-run QR logins.
    _write_text(dest / "B站登录.bat", BILI_LOGIN_BAT, newline="\r\n")
    _write_text(dest / "QQ登录.bat", QQ_LOGIN_BAT, newline="\r\n")
    _write_text(dest / "QQ空间登录.bat", QZONE_LOGIN_BAT, newline="\r\n")
    if clean_bundle:
        return
    _write_text(dest / "start_web.bat", START_WEB_BAT, newline="\r\n")
    _write_text(dest / "init.bat", INIT_BAT, newline="\r\n")
    _write_text(dest / "doctor.bat", DOCTOR_BAT, newline="\r\n")
    for name, text in (
        ("start.sh", START_SH),
        ("init.sh", INIT_SH),
        ("doctor.sh", DOCTOR_SH),
    ):
        target = dest / name
        _write_text(target, text, newline="\n")
        try:
            target.chmod(0o755)
        except OSError:
            pass


def _prepare_dest(dest: Path, *, clean: bool) -> None:
    resolved = dest.resolve()
    root = ROOT.resolve()
    if resolved == root or resolved in root.parents:
        raise BuildError(f"refusing to build into the project root or its parent: {dest}")
    if resolved.parent == resolved:
        raise BuildError(f"refusing to build into a filesystem root: {dest}")
    if dest.exists():
        if not clean:
            if not dest.is_dir():
                raise BuildError(f"destination exists and is not a directory: {dest}")
            return
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    dest.mkdir(parents=True, exist_ok=True)


def _copy_sources(dest: Path, *, clean_bundle: bool = False) -> None:
    package_dirs = CLEAN_PACKAGE_DIRS if clean_bundle else PACKAGE_DIRS
    script_files = CLEAN_SCRIPT_FILES if clean_bundle else SCRIPT_FILES
    root_files = CLEAN_ROOT_FILES if clean_bundle else ROOT_FILES

    for name in package_dirs:
        source = ROOT / name
        if not source.is_dir():
            raise BuildError(f"missing source directory: {source}")
        shutil.copytree(
            source,
            dest / name,
            ignore=_ignore,
            dirs_exist_ok=True,
        )

    scripts_dir = dest / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for name in script_files:
        source = ROOT / "scripts" / name
        if source.is_file():
            shutil.copy2(source, scripts_dir / name)

    for subdir in () if clean_bundle else SCRIPT_SUBDIRS:
        source = ROOT / "scripts" / subdir
        if source.is_dir():
            shutil.copytree(source, scripts_dir / subdir)

    for name in root_files:
        source = ROOT / name
        if not source.is_file():
            raise BuildError(f"missing required root file: {source}")
        shutil.copy2(source, dest / name)


def _create_data_dirs(dest: Path) -> None:
    for name in DATA_DIRS:
        target = dest / "data" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / ".gitkeep").write_text("", encoding="utf-8")


def _build_manifest(dest: Path, version: str, app_name: str = APP_NAME) -> dict[str, Any]:
    entries = []
    total = 0
    for path in _iter_files(dest):
        relative = path.relative_to(dest).as_posix()
        size = path.stat().st_size
        total += size
        entries.append({"path": relative, "sha256": _sha256(path), "size": size})
    start = (
        {"windows": "start.bat", "posix": "scripts/start_all.py"}
        if app_name == CLEAN_APP_NAME
        else {"windows": "start.bat", "posix": "start.sh"}
    )
    manifest = {
        "name": app_name,
        "version": version,
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": ">=3.11",
        "profile_default": "low",
        "entry": "fatfish/__main__.py",
        "web_entry": "web/index.html",
        "start": start,
        "one_click": {
            "windows": "启动大肥鱼.bat",
            "script": "scripts/start_all.py",
        },
        "source_readable": True,
        "encrypted": False,
        "files": entries,
    }
    _write_text(
        dest / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return manifest


def _write_zip(dest: Path, zip_path: Path, app_name: str = APP_NAME) -> None:
    zip_path = zip_path.resolve()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    files = _iter_files(dest, include_manifest=True)
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in files:
            relative = path.relative_to(dest).as_posix()
            info = zipfile.ZipInfo(f"{app_name}/{relative}", date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.suffix == ".sh" else 0o644) << 16
            archive.writestr(info, path.read_bytes())


def build(
    dest: Path,
    *,
    zip_path: Path | None = None,
    clean: bool = True,
    clean_bundle: bool = False,
) -> dict[str, Any]:
    """Build the package at *dest* and optionally a zip at *zip_path*.

    ``clean`` wipes the destination first.  ``clean_bundle`` selects the
    minimal one-click bundle (``fatfish-oneclick``) instead of the full source
    package (``fatfish-app``).
    """
    dest = Path(dest)
    version = read_version()
    app_name = CLEAN_APP_NAME if clean_bundle else APP_NAME
    _prepare_dest(dest, clean=clean)
    _copy_sources(dest, clean_bundle=clean_bundle)
    _create_data_dirs(dest)
    _write_launch_scripts(dest, clean_bundle=clean_bundle)
    _write_text(dest / "VERSION", version + "\n")
    manifest = _build_manifest(dest, version, app_name)

    if zip_path is not None:
        _write_zip(dest, Path(zip_path), app_name)

    files = _iter_files(dest, include_manifest=True)
    total_bytes = sum(path.stat().st_size for path in files)
    result = {
        "dest": str(dest.resolve()),
        "zip": str(Path(zip_path).resolve()) if zip_path is not None else None,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "version": version,
        "bundle": app_name,
        "manifest_files": len(manifest["files"]),
    }
    print(
        f"built {app_name} {version}: {result['file_count']} files, "
        f"{total_bytes / 1024:.1f} KiB -> {dest}"
    )
    return result


# --------------------------------------------------------------------------- verify
def _package_env(package: Path, temp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["FATFISH_DATA_DIR"] = str(temp / "data")
    env["FATFISH_DB"] = str(temp / "data" / "fatfish.db")
    env["FATFISH_PROFILE"] = "low"
    env["OPENCODE_API_KEY"] = ""
    # The main config reads FATFISH_OPENCODE_KEY first; clear it too or the
    # isolated chat check makes a real network LLM call and can time out.
    env["FATFISH_OPENCODE_KEY"] = ""
    env.pop("FATFISH_WEB_TOKEN", None)
    env["PYTHONPATH"] = str(package)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _record(checks: dict[str, bool], name: str, ok: bool, detail: str = "") -> None:
    checks[name] = bool(ok)
    print(f"[verify] {name}: {'ok' if ok else 'FAIL'}")
    if not ok:
        raise VerifyError(f"{name}: {detail or 'check failed'}")


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _start_server(
    python: str,
    package: Path,
    env: dict[str, str],
    extra_config: Path | None = None,
) -> tuple[subprocess.Popen[str], threading.Thread, list[str], int]:
    command = [python, "-m", "fatfish"]
    if extra_config is not None:
        # Top-level option: it must precede the ``serve`` subcommand.
        command.extend(["--config", str(extra_config)])
    command.extend(
        [
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--token",
            "verify-token",
        ]
    )
    process = subprocess.Popen(
        command,
        cwd=str(package),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: list[str] = []
    output: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                output.put(line)
        finally:
            output.put(None)

    thread = threading.Thread(target=reader, name="fatfish-verify-reader", daemon=True)
    thread.start()

    deadline = time.monotonic() + 15.0
    url = ""
    while time.monotonic() < deadline:
        try:
            line = output.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        if line is None:
            break
        text = line.rstrip("\r\n")
        lines.append(text)
        match = re.search(r"http://127\.0\.0\.1:(\d+)/\?token=verify-token", text)
        if match:
            url = match.group(0)
            break
    if not url:
        # Drain anything the reader still has, then report the startup output.
        while True:
            try:
                line = output.get_nowait()
            except queue.Empty:
                break
            if line is None:
                break
            lines.append(line.rstrip("\r\n"))
        _stop_server(process, thread)
        raise VerifyError(
            "server did not print a login URL:\n" + "\n".join(lines[-20:])
        )
    port = int(re.search(r":(\d+)/", url).group(1))  # type: ignore[union-attr]
    return process, thread, lines, port


def _stop_server(process: subprocess.Popen[str], thread: threading.Thread | None) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass
    if thread is not None:
        thread.join(timeout=5)


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    payload = None
    request_headers = dict(headers or {})
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    connection = HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _json_body(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifyError(f"invalid JSON response: {exc}") from exc
    if not isinstance(data, dict):
        raise VerifyError("JSON response is not an object")
    return data


def _short(raw: bytes, limit: int = 400) -> str:
    return raw.decode("utf-8", errors="replace")[:limit]


def verify_package(dest: Path, *, run_tests: bool = True) -> dict[str, Any]:
    """Check a built package in an isolated temporary copy.

    Raises ``SystemExit(1)`` when a check fails; returns a summary on success.
    """
    dest = Path(dest)
    if not dest.is_dir():
        raise SystemExit(f"verify failed: package directory not found: {dest}")
    try:
        version = read_version(dest)
    except BuildError as exc:
        raise SystemExit(f"verify failed: {exc}") from None
    checks: dict[str, bool] = {}
    failures: list[str] = []
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    tmp_holder = tempfile.TemporaryDirectory(prefix="fatfish-verify-")
    temp = Path(tmp_holder.name)

    try:
        package = temp / dest.name
        shutil.copytree(dest, package)
        env = _package_env(package, temp)
        python = sys.executable
        # Keep the chat check hermetic.  The low profile enables the HTTP TTS
        # client and the HTTP embedding backend, so without this the verify
        # server would talk to whatever services the developer happens to be
        # running on 9880/9881.  A cold TTS load takes ~27 s and made the 30 s
        # chat request time out; a second embedding server would also defeat
        # the point of the isolated copy.  The local fallback does not need
        # either service.
        verify_config = temp / "verify.toml"
        verify_config.write_text(
            "[tts]\nenabled = false\n\n"
            "[sticker]\nembedding_backend = \"hash\"\n\n"
            "[memory]\nembedding_backend = \"hash\"\n",
            encoding="utf-8",
        )

        # 1) doctor
        doctor = _run(
            [python, "-m", "fatfish", "doctor"],
            cwd=package,
            env=env,
            timeout=60,
        )
        doctor_out = (doctor.stdout or "") + (doctor.stderr or "")
        doctor_ok = doctor.returncode == 0 and (
            "identity_kernel" in doctor_out
            or "identity kernel" in doctor_out.lower()
            or "ok" in doctor_out.lower()
        )
        _record(
            checks,
            "doctor",
            doctor_ok,
            f"returncode={doctor.returncode}\n{doctor_out[-1000:]}",
        )
        if "status:" not in doctor_out or "OK" not in doctor_out:
            raise VerifyError("doctor did not report status: OK\n" + doctor_out[-1000:])

        # 2) init
        init = _run(
            [python, "-m", "fatfish", "init"],
            cwd=package,
            env=env,
            timeout=60,
        )
        init_out = (init.stdout or "") + (init.stderr or "")
        db_path = temp / "data" / "fatfish.db"
        _record(
            checks,
            "init",
            init.returncode == 0 and db_path.is_file(),
            f"returncode={init.returncode}, db={db_path.exists()}\n{init_out[-1000:]}",
        )

        # 3) server
        process, reader, _lines, port = _start_server(
            python, package, env, verify_config
        )
        _record(checks, "server", port > 0 and process.poll() is None, "server not running")

        auth = {"Authorization": "Bearer verify-token"}

        # health
        status, raw = _request(port, "GET", "/api/health", headers=auth)
        health = _json_body(raw) if status == 200 else {}
        _record(
            checks,
            "health",
            status == 200
            and health.get("ok") is True
            and health.get("version") == version,
            f"status={status}, version={health.get('version')!r}, want={version!r}: {_short(raw)}",
        )

        # static UI
        status, raw = _request(port, "GET", "/")
        _record(
            checks,
            "static",
            status == 200 and b"<html" in raw.lower(),
            f"status={status}: {_short(raw, 200)}",
        )

        # chat (local fallback when no API key is configured)
        status, raw = _request(
            port,
            "POST",
            "/api/chat",
            body={"message": "你好", "session_id": "verify"},
            headers={**auth, "X-Fatfish-Request": "1"},
        )
        reply = ""
        if status == 200:
            try:
                reply = str(_json_body(raw).get("result", {}).get("reply") or "")
            except (VerifyError, AttributeError):
                reply = ""
        _record(
            checks,
            "chat",
            status == 200 and bool(reply.strip()),
            f"status={status}: {_short(raw)}",
        )

        # tick
        status, raw = _request(
            port,
            "POST",
            "/api/tick",
            body={},
            headers={**auth, "X-Fatfish-Request": "1"},
        )
        _record(
            checks,
            "tick",
            status == 200 and _json_body(raw).get("ok") is True,
            f"status={status}: {_short(raw)}",
        )

        _stop_server(process, reader)
        process, reader = None, None

        # 4) the package's own test suite (full bundle only)
        if run_tests and (package / "tests").is_dir():
            # test_core/test_p0 build their own per-test TOML and temp databases.
            # FATFISH_DATA_DIR/FATFISH_DB would override those test-local paths and
            # make every test share one database, so hand the suite a clean data env.
            test_env = env.copy()
            test_env.pop("FATFISH_DATA_DIR", None)
            test_env.pop("FATFISH_DB", None)
            tests = _run(
                [python, "-m", "unittest", "discover", "-s", "tests", "-q"],
                cwd=package,
                env=test_env,
                timeout=300,
            )
            test_out = (tests.stdout or "") + (tests.stderr or "")
            _record(
                checks,
                "tests",
                tests.returncode == 0,
                f"returncode={tests.returncode}\n{test_out[-2000:]}",
            )
    except VerifyError as exc:
        failures.append(str(exc))
    except subprocess.TimeoutExpired as exc:
        failures.append(f"subprocess timed out: {exc}")
    except OSError as exc:
        failures.append(f"operating system error: {exc}")
    except Exception as exc:  # noqa: BLE001 - report, never dump a traceback to users
        failures.append(f"unexpected error: {exc!r}")
    finally:
        if process is not None:
            _stop_server(process, reader)
        tmp_holder.cleanup()

    summary = {
        "ok": not failures,
        "dest": str(dest),
        "version": version,
        "checks": checks,
        "failures": failures,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)
    return summary


# --------------------------------------------------------------------------- cli
def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    version = read_version()
    parser = argparse.ArgumentParser(
        prog="make_app.py",
        description=f"Build the {APP_NAME} source package.",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help=(
            f"destination directory (default: {_display(DEFAULT_DEST)}; "
            f"with --clean: {_display(DEFAULT_CLEAN_DEST)})"
        ),
    )
    parser.add_argument(
        "--zip",
        default=None,
        help=(
            f"zip path (default: dist/{APP_NAME}-<version>.zip; "
            f"with --clean: dist/{CLEAN_APP_NAME}-<version>.zip)"
        ),
    )
    parser.add_argument("--no-zip", action="store_true", help="do not create a zip")
    parser.add_argument(
        "--clean",
        action="store_true",
        dest="clean_bundle",
        help=(
            f"build the minimal one-click bundle ({CLEAN_APP_NAME}) without tests, "
            "skills, dev scripts or personal data"
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify the package in an isolated temporary copy after building",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="keep existing files in the destination instead of deleting it",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="with --verify, skip the package's unittest suite",
    )
    args = parser.parse_args(argv)

    app_name = CLEAN_APP_NAME if args.clean_bundle else APP_NAME
    dest = Path(args.dest) if args.dest else DIST_ROOT / app_name
    if not dest.is_absolute():
        dest = ROOT / dest

    zip_path: Path | None = None
    if not args.no_zip:
        zip_path = Path(args.zip) if args.zip else DIST_ROOT / f"{app_name}-{version}.zip"
        if not zip_path.is_absolute():
            zip_path = ROOT / zip_path

    try:
        result = build(
            dest,
            zip_path=zip_path,
            clean=not args.no_clean,
            clean_bundle=args.clean_bundle,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.verify:
        verify_package(Path(result["dest"]), run_tests=not args.skip_tests)

    print("next:")
    print(f"  cd {_display(Path(result['dest']))}")
    if os.name == "nt":
        print("  启动大肥鱼.bat")
    elif args.clean_bundle:
        print("  python scripts/start_all.py --no-browser")
    else:
        print("  ./start.sh")
    print("  python -m fatfish serve --open")
    if result["zip"]:
        print(f"zip: {_display(Path(result['zip']))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
