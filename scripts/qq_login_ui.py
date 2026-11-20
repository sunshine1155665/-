"""Click-to-run NapCat QQ QR login with a local auto-refreshing page.

Run directly::

    python scripts/qq_login_ui.py

NapCat OneKey writes its login QR into the portable QQ tree.  NapCat
regenerates that QR about every two minutes (the console logs
``Login Error ErrType:1 ErrCode:3``), which is easy to miss.  This launcher
writes ``qrcode_view.html`` next to ``qrcode.png`` and opens the page in the
default browser.  The page reloads only the image, every two seconds, with a
cache-busting timestamp, so a refreshed QR shows up without any user action.

The launcher only starts ``scripts/start_napcat.bat`` when needed.  It never
logs in, scans, connects to OneBot, edits NapCat configuration, or prints
tokens/cookies.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NAPCAT_DIR = ROOT / "vendor" / "napcat" / "onekey" / "NapCat.52230.Shell"
DEFAULT_LAUNCHER = ROOT / "scripts" / "start_napcat.bat"
QR_RELATIVE_PATH = (
    Path("versions")
    / "9.9.33-52230"
    / "resources"
    / "app"
    / "napcat"
    / "cache"
    / "qrcode.png"
)
HTML_NAME = "qrcode_view.html"

QR_STALE_SECONDS = 300.0
QR_WAIT_SECONDS = 60.0
DEFAULT_TIMEOUT = 600.0
REFRESH_MS = 2000
WAIT_INTERVAL_SECONDS = 0.5
WATCH_INTERVAL_SECONDS = 2.0

INSTRUCTION = "用手机 QQ 扫码登录机器人账号"
LOGIN_COMMAND = "python -m fatfish qq run"
READY_MESSAGE = f"扫码后请等待 NapCat 控制台出现登录成功；然后运行 {LOGIN_COMMAND}"

# Portable NapCat runs inside QQ.exe.  The boot helpers are separate images.
PROCESS_NAMES = frozenset(
    {
        "qq.exe",
        "napcatwinbootmain.exe",
        "napcatwinboothook.exe",
    }
)


class QqLoginError(RuntimeError):
    """A problem that should be printed without a traceback."""


class QrMissingError(QqLoginError):
    """The QR image never appeared in the NapCat cache directory."""


# --------------------------------------------------------------------------- paths
def resolve_path(value: str | Path) -> Path:
    """Resolve a CLI path against the repository root, not the shell cwd."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def resolve_napcat_dir(value: str | Path | None = None) -> Path:
    """Return the portable QQ/NapCat root directory."""
    if value is None or str(value).strip() == "":
        return DEFAULT_NAPCAT_DIR
    return resolve_path(value)


def resolve_qr_path(
    napcat_dir: str | Path | None = None,
    qr_file: str | Path | None = None,
) -> Path:
    """Resolve the QR PNG path.

    ``--qr-file`` wins.  Otherwise the known OneKey layout is used; a direct
    ``<root>/cache/qrcode.png`` and any other installed version directory are
    accepted as fallbacks so a version bump does not break the launcher.
    """
    if qr_file is not None and str(qr_file).strip() != "":
        return resolve_path(qr_file)

    root = resolve_napcat_dir(napcat_dir)
    known = root / QR_RELATIVE_PATH
    direct = root / "cache" / "qrcode.png"
    for candidate in (known, direct):
        if candidate.is_file():
            return candidate

    candidates = sorted(
        root.glob("versions/*/resources/app/napcat/cache/qrcode.png")
    )
    if candidates:
        return candidates[-1]
    return known


def _qr_ready(path: Path, newer_than: float | None) -> bool:
    """True when *path* is a non-empty file, optionally modified recently."""
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size <= 0:
        return False
    if newer_than is not None and stat.st_mtime < newer_than:
        return False
    return True


def qr_is_stale(
    path: str | Path,
    *,
    now: float | None = None,
    max_age: float = QR_STALE_SECONDS,
) -> bool:
    """A missing, empty or older-than-*max_age* QR cannot be used for login."""
    try:
        stat = Path(path).stat()
    except OSError:
        return True
    if stat.st_size <= 0:
        return True
    current = time.time() if now is None else float(now)
    return (current - stat.st_mtime) > float(max_age)


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


# ------------------------------------------------------------------------- process
def _running_names_from_tasklist(text: str) -> set[str]:
    """Extract image names from ``tasklist /FO CSV /NH`` output."""
    names: set[str] = set()
    for row in csv.reader(io.StringIO(text or "")):
        if not row:
            continue
        name = row[0].strip().strip('"').lower()
        if name and name != "info:":
            names.add(name)
    return names


def is_qq_running(*, runner: Callable[..., Any] | None = None) -> bool:
    """Return True when QQ or a NapCat boot helper is already running.

    This is a read-only ``tasklist`` query.  Errors (including a missing
    ``tasklist``) are treated as "not running" so the launcher can still
    report its own timeout instead of crashing.
    """
    if os.name != "nt":
        return False

    run = runner or subprocess.run
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": 10,
    }
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if no_window:
        kwargs["creationflags"] = no_window

    try:
        completed = run(
            ["tasklist", "/FO", "CSV", "/NH"],
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    if getattr(completed, "returncode", 1) != 0:
        return False

    names = _running_names_from_tasklist(getattr(completed, "stdout", "") or "")
    return any(name in PROCESS_NAMES or name.startswith("napcat") for name in names)


def launch_napcat(
    launcher: str | Path | None = None,
    *,
    popen: Callable[..., Any] | None = None,
) -> Any:
    """Start ``start_napcat.bat`` in a new console and return immediately."""
    if os.name != "nt":
        raise QqLoginError("QQ/NapCat 登录向导只支持 Windows。")

    target = Path(launcher) if launcher is not None else DEFAULT_LAUNCHER
    if not target.is_file():
        raise QqLoginError(
            f"找不到 NapCat 启动脚本：{target}\n"
            r"请先运行 scripts\setup_napcat.ps1 安装便携 QQ/NapCat。"
        )

    spawn = popen or subprocess.Popen
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        return spawn(
            ["cmd.exe", "/c", str(target)],
            cwd=str(ROOT),
            creationflags=creationflags,
        )
    except OSError as exc:
        raise QqLoginError(f"启动 NapCat 失败：{exc}") from exc


# ---------------------------------------------------------------------------- wait
def wait_for_qr(
    path: str | Path,
    *,
    timeout: float = QR_WAIT_SECONDS,
    interval: float = WAIT_INTERVAL_SECONDS,
    newer_than: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    emit: Callable[[str], None] | None = None,
) -> bool:
    """Wait until the QR exists (and is newer than *newer_than*, if given)."""
    target = Path(path)
    deadline = clock() + max(0.0, float(timeout))
    if emit is not None:
        emit("等待 NapCat 生成二维码…")

    while True:
        if _qr_ready(target, newer_than):
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(max(0.0, float(interval)), remaining))


class QrPreparation:
    """What ``prepare_qr`` found or started."""

    __slots__ = ("qr_path", "started", "already_running", "reused")

    def __init__(
        self,
        qr_path: Path,
        *,
        started: bool,
        already_running: bool,
        reused: bool,
    ) -> None:
        self.qr_path = qr_path
        self.started = started
        self.already_running = already_running
        self.reused = reused


def prepare_qr(
    qr_path: str | Path,
    *,
    launcher: str | Path | None = None,
    wait_seconds: float = QR_WAIT_SECONDS,
    wait_interval: float = WAIT_INTERVAL_SECONDS,
    is_running: Callable[[], bool] | None = None,
    start: Callable[..., Any] | None = None,
    wait: Callable[..., bool] | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> QrPreparation:
    """Reuse a fresh QR, or start NapCat and wait for a usable QR.

    A stale/missing QR with no QQ process starts ``start_napcat.bat``.  If QQ
    is already running, no second instance is started; the existing process is
    expected to regenerate its QR.
    """
    target = Path(qr_path)
    if target.is_file() and not qr_is_stale(target, now=clock()):
        return QrPreparation(
            qr_path=target,
            started=False,
            already_running=False,
            reused=True,
        )

    running_check = is_running or is_qq_running
    start_func = start or launch_napcat
    wait_func = wait or wait_for_qr

    already_running = bool(running_check())
    started = False
    if already_running:
        emit("检测到 QQ.exe/NapCat 已在运行，不会启动第二个实例。")
    else:
        start_func(launcher)
        started = True
        emit("已启动 NapCat（新控制台窗口），等待二维码生成…")

    # A stale file may still be on disk from the last session.  Wait for a
    # rewrite instead of handing the user an expired QR.
    newer_than = clock() - 1.0 if target.is_file() else None
    if not wait_func(
        target,
        timeout=wait_seconds,
        interval=wait_interval,
        newer_than=newer_than,
        sleep=sleep,
        clock=clock,
        emit=emit,
    ):
        raise QrMissingError(
            f"等待二维码超时：未能获得可用的二维码（{target}）。\n"
            "请查看 NapCat 控制台中的报错；必要时完全退出 QQ 后重新运行 "
            r"scripts\start_napcat.bat。若尚未安装，请先运行 "
            r"scripts\setup_napcat.ps1。"
        )

    return QrPreparation(
        qr_path=target,
        started=started,
        already_running=already_running,
        reused=False,
    )


# ---------------------------------------------------------------------------- html
def image_reference(qr_path: str | Path, html_path: str | Path) -> str:
    """A browser-friendly relative reference from the page to the QR image."""
    qr = Path(qr_path)
    page = Path(html_path)
    try:
        return Path(os.path.relpath(qr, page.parent)).as_posix()
    except ValueError:  # different Windows drives
        return qr.resolve().as_uri()


def render_login_html(qr_src: str, *, generated_at: float | None = None) -> str:
    """Build the self-contained login page (no external CSS, JS or CDN)."""
    src = html.escape(str(qr_src), quote=True)
    stamp = int(time.time() if generated_at is None else generated_at)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{INSTRUCTION}</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: #eef6fc;
  color: #1f2329;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: min(94vw, 640px);
  padding: clamp(20px, 5vw, 40px);
  border-radius: 18px;
  background: #ffffff;
  box-shadow: 0 12px 40px rgba(15, 23, 42, 0.12);
  text-align: center;
}}
h1 {{ margin: 0 0 10px; font-size: clamp(1.4rem, 4.8vw, 2rem); }}
.hint {{ margin: 0 0 18px; color: #5b6673; font-size: clamp(1rem, 3vw, 1.15rem); }}
.qr {{
  display: block;
  width: min(82vw, 500px);
  height: auto;
  margin: 0 auto 18px;
  border: 10px solid #ffffff;
  border-radius: 8px;
  image-rendering: pixelated;
}}
.tip {{ margin: 8px 0; color: #4a5562; font-size: clamp(0.95rem, 2.8vw, 1.05rem); line-height: 1.7; }}
.tip.strong {{ color: #0b8fd6; font-size: clamp(1.02rem, 3.2vw, 1.2rem); font-weight: 700; }}
.tip.muted {{ color: #8a94a0; font-size: 0.9rem; }}
code {{ padding: 2px 6px; border-radius: 4px; background: #f1f4f8; font-family: Consolas, "Courier New", monospace; }}
@media (max-width: 420px) {{
  body {{ padding: 10px; }}
  .card {{ padding: 20px 14px; border-radius: 14px; }}
}}
</style>
</head>
<body>
<main class="card">
  <h1>{INSTRUCTION}</h1>
  <p class="hint">请使用要登录的机器人账号扫描下方二维码</p>
  <img id="qr" class="qr" src="{src}?t={stamp}" data-src="{src}" alt="QQ 登录二维码" width="500" height="500">
  <p class="tip strong">二维码每 2 秒自动刷新，过期了也不用管。</p>
  <p class="tip">本页面可以一直开着；NapCat 重新生成二维码后，这里会自动显示新码。</p>
  <p class="tip">扫码后请等待 NapCat 控制台出现登录成功；然后运行 <code>{LOGIN_COMMAND}</code></p>
  <p class="tip muted">二维码等同于登录凭证，请不要截图发给别人。</p>
</main>
<script>
(function () {{
  var img = document.getElementById("qr");
  if (!img) return;
  var base = img.getAttribute("data-src");
  function refresh() {{
    img.src = base + "?t=" + Date.now();
  }}
  refresh();
  window.setInterval(refresh, {REFRESH_MS});
}})();
</script>
</body>
</html>
"""


def write_html(path: str | Path, content: str) -> Path:
    """Write HTML atomically so a reload never sees half a file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, target)
    return target


def write_login_page(
    html_path: str | Path,
    qr_path: str | Path,
    *,
    generated_at: float | None = None,
) -> Path:
    qr_src = image_reference(qr_path, html_path)
    return write_html(
        html_path,
        render_login_html(qr_src, generated_at=generated_at),
    )


# --------------------------------------------------------------------------- watch
def watch_qr(
    qr_path: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = WATCH_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    emit: Callable[[str], None] = print,
) -> int:
    """Keep the launcher alive while reporting QR refreshes.

    Nothing is scanned or connected to.  The browser page refreshes itself;
    this loop only gives the user feedback and exits after *timeout*.
    """
    target = Path(qr_path)
    deadline = clock() + max(0.0, float(timeout))
    last_mtime = _mtime_ns(target)

    emit("正在监视二维码刷新；页面可以一直开着，不会自动关闭。")
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            emit(
                f"已监视 {int(max(0.0, float(timeout)))} 秒，尚未确认登录。"
                f"页面仍可继续使用；登录成功后运行 {LOGIN_COMMAND}。"
            )
            return 0

        sleep(min(max(0.0, float(interval)), remaining))

        current = _mtime_ns(target)
        if current is None:
            if last_mtime is not None:
                emit("二维码文件暂时不存在，等待 NapCat 重新生成…")
            last_mtime = None
            continue
        if last_mtime is None:
            emit("检测到二维码，页面会自动显示。")
        elif current != last_mtime:
            emit("二维码已刷新，页面会自动显示新码。")
        last_mtime = current


# ----------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qq_login_ui.py",
        description="生成 NapCat QQ 扫码登录页面，并让二维码在本机自动刷新。",
    )
    parser.add_argument(
        "--napcat-dir",
        default=None,
        help=(
            "便携 QQ/NapCat 根目录"
            f"（默认 {DEFAULT_NAPCAT_DIR}）"
        ),
    )
    parser.add_argument(
        "--qr-file",
        default=None,
        help="覆盖 qrcode.png 路径（默认按 OneKey 目录结构查找）",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器（页面仍会生成）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"监视二维码刷新的秒数（默认 {int(DEFAULT_TIMEOUT)}，即 10 分钟）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    qr_path = resolve_qr_path(args.napcat_dir, args.qr_file)
    html_path = qr_path.parent / HTML_NAME

    try:
        preparation = prepare_qr(qr_path, launcher=DEFAULT_LAUNCHER)
        write_login_page(html_path, qr_path)
    except QqLoginError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"写入登录页面失败：{exc}", file=sys.stderr)
        return 1

    if preparation.reused:
        print("使用现有二维码。")
    elif preparation.already_running:
        print("QQ.exe/NapCat 已在运行，未重复启动。")
    else:
        print("已启动 NapCat。")

    print(f"二维码图片：{qr_path}")
    print(f"登录页面：{html_path}")
    print(INSTRUCTION)
    print(READY_MESSAGE)

    if not args.no_browser:
        try:
            opened = webbrowser.open(html_path.as_uri())
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            print(f"未能自动打开浏览器，请手动打开：{html_path}")

    try:
        return watch_qr(qr_path, timeout=args.timeout)
    except KeyboardInterrupt:
        print("\n已停止监视。", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
