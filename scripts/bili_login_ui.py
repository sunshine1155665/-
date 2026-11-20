"""Click-to-run Bilibili QR login with a local auto-refreshing page.

Run directly::

    python scripts/bili_login_ui.py

The script writes ``data/bilibili_login.png`` and a small Chinese HTML page,
opens the page in the default browser, then polls the Bilibili passport API.
The page refreshes the QR image every two seconds with a cache-busting query,
so an expired QR is replaced without asking the user to do anything.

Cookies are never printed.  On success the client saves them to its configured
cookie file (``data/bilibili_cookies.json`` by default).
"""

from __future__ import annotations

import argparse
import html
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.bilibili.client import BilibiliClient, BilibiliError  # noqa: E402
from fatfish.config import load_config  # noqa: E402

DEFAULT_QR_FILE = "data/bilibili_login.png"
DEFAULT_HTML_FILE = "data/bilibili_login.html"
DEFAULT_TIMEOUT = 600
POLL_INTERVAL_SECONDS = 2.0
MAX_REFRESHES = 5
SUCCESS_MESSAGE = "登录成功，可以关闭此页面"
QRCODE_MISSING_MESSAGE = (
    "缺少 qrcode 组件，无法生成二维码图片。请先运行：pip install qrcode"
)


class QrCodeMissingError(RuntimeError):
    """Raised when the optional ``qrcode`` dependency is unavailable."""


# --------------------------------------------------------------------------- paths
def resolve_path(value: str | Path) -> Path:
    """Resolve a CLI path against the repository root, not the shell cwd."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def image_reference(qr_path: str | Path, html_path: str | Path) -> str:
    """A browser-friendly reference from the HTML file to the QR image."""
    qr = Path(qr_path)
    page = Path(html_path)
    try:
        return Path(os.path.relpath(qr, page.parent)).as_posix()
    except ValueError:  # different Windows drives
        return qr.as_uri()


# ----------------------------------------------------------------------------- QR
def write_qr_png(url: str, path: str | Path) -> Path:
    """Render *url* as a QR PNG.  Raises ``QrCodeMissingError`` if unavailable."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=12,
            border=2,
        )
        qr.add_data(str(url))
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        image.save(str(target))
    except ImportError as exc:
        raise QrCodeMissingError(QRCODE_MISSING_MESSAGE) from exc
    return target


# --------------------------------------------------------------------------- HTML
def render_login_html(qr_src: str, *, status_text: str = "等待扫码…") -> str:
    """Build the self-contained login page (no external CSS, JS or CDN)."""
    src = html.escape(str(qr_src), quote=True)
    status = html.escape(str(status_text))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>B 站扫码登录</title>
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
  background: #f2f4f7;
  color: #1f2329;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: min(92vw, 560px);
  padding: clamp(20px, 5vw, 40px);
  border-radius: 18px;
  background: #ffffff;
  box-shadow: 0 12px 40px rgba(15, 23, 42, 0.1);
  text-align: center;
}}
h1 {{ margin: 0 0 20px; font-size: clamp(1.35rem, 4.5vw, 1.9rem); }}
.hint {{ margin: 0 0 18px; font-size: clamp(1.05rem, 3.5vw, 1.3rem); font-weight: 600; }}
.qr {{
  display: block;
  width: min(76vw, 420px);
  height: auto;
  margin: 0 auto 18px;
  border-radius: 8px;
  image-rendering: pixelated;
}}
.status {{
  margin: 10px 0 18px;
  color: #00a1d6;
  font-size: clamp(1rem, 3vw, 1.15rem);
  font-weight: 600;
}}
.tip {{ margin: 6px 0; color: #646a73; font-size: 0.95rem; line-height: 1.65; }}
@media (max-width: 420px) {{
  body {{ padding: 10px; }}
  .card {{ padding: 20px 16px; border-radius: 14px; }}
}}
</style>
</head>
<body>
<main class="card">
  <h1>B 站扫码登录</h1>
  <p class="hint">用 B 站手机客户端扫码</p>
  <img id="qr" class="qr" src="{src}" data-src="{src}" alt="B 站登录二维码" width="420" height="420">
  <p id="status" class="status">{status}</p>
  <p class="tip">打开哔哩哔哩 App，点击右上角扫一扫。</p>
  <p class="tip">二维码每 2 秒自动刷新，过期了也不用管。</p>
  <p class="tip">登录成功后本页会自动变成绿色提示，届时可以关闭。</p>
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
  window.setInterval(refresh, 2000);
  window.setTimeout(function () {{ window.location.reload(); }}, 6000);
}})();
</script>
</body>
</html>
"""


def render_success_html() -> str:
    """Green success page shown after the phone confirms the login."""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>B 站登录成功</title>
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
  background: #e8f7ee;
  color: #1f2329;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
}}
.card {{
  width: min(92vw, 560px);
  padding: clamp(24px, 6vw, 48px);
  border-top: 8px solid #16a34a;
  border-radius: 18px;
  background: #ffffff;
  box-shadow: 0 12px 40px rgba(15, 23, 42, 0.1);
  text-align: center;
}}
.check {{
  width: 84px;
  height: 84px;
  margin: 0 auto 20px;
  border-radius: 50%;
  background: #16a34a;
  color: #ffffff;
  font-size: 48px;
  line-height: 84px;
  font-weight: 700;
}}
h1 {{
  margin: 0 0 18px;
  color: #15803d;
  font-size: clamp(1.4rem, 4.5vw, 2rem);
}}
.tip {{ margin: 8px 0; color: #3f4a44; font-size: 0.98rem; line-height: 1.7; }}
</style>
</head>
<body>
<main class="card success">
  <div class="check" aria-hidden="true">✓</div>
  <h1>{SUCCESS_MESSAGE}</h1>
  <p class="tip">登录信息已经保存在本机，不会上传到任何第三方。</p>
  <p class="tip">可以回到启动窗口，或直接关闭这个页面。</p>
</main>
</body>
</html>
"""


def write_html(path: str | Path, content: str) -> Path:
    """Write HTML atomically so a browser reload never sees half a file."""
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
    status_text: str = "等待扫码…",
) -> Path:
    qr_src = image_reference(qr_path, html_path)
    return write_html(html_path, render_login_html(qr_src, status_text=status_text))


def write_success_page(html_path: str | Path) -> Path:
    return write_html(html_path, render_success_html())


# --------------------------------------------------------------------------- poll
def handle_poll_result(
    client: Any,
    result: dict[str, Any],
    *,
    qrcode_key: str,
    qr_path: str | Path,
    html_path: str | Path,
    refresh_count: int,
    max_refreshes: int = MAX_REFRESHES,
    emit: Callable[[str], None] = print,
) -> tuple[int, str, int | None]:
    """Apply one poll result.

    Returns ``(refresh_count, qrcode_key, exit_code)``.  ``exit_code`` is
    ``None`` when polling should continue.  This function never prints
    cookies, headers or the QR URL.
    """
    status = str(result.get("status") or "error")

    if status == "waiting":
        emit("等待扫码…")
        return refresh_count, qrcode_key, None

    if status == "scanned":
        emit("已扫码，请在手机上确认…")
        return refresh_count, qrcode_key, None

    if status == "success":
        write_success_page(html_path)
        emit("登录成功，可以关闭此页面。")
        return refresh_count, qrcode_key, 0

    if status == "expired":
        if refresh_count >= max_refreshes:
            emit(f"二维码已连续刷新 {max_refreshes} 次仍未登录，已停止。")
            return refresh_count, qrcode_key, 1
        qr = client.qr_generate()
        new_key = str(qr.get("qrcode_key") or "")
        qr_url = str(qr.get("url") or "")
        if not new_key or not qr_url:
            emit("刷新二维码失败：B 站接口返回不完整。")
            return refresh_count, qrcode_key, 1
        write_qr_png(qr_url, qr_path)
        write_login_page(html_path, qr_path)
        emit("二维码已刷新")
        return refresh_count + 1, new_key, None

    message = str(result.get("message") or "").strip()
    emit(f"登录失败：{message or '未知错误'}")
    return refresh_count, qrcode_key, 1


def run_login(
    client: Any,
    qrcode_key: str,
    *,
    qr_path: str | Path,
    html_path: str | Path,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = POLL_INTERVAL_SECONDS,
    max_refreshes: int = MAX_REFRESHES,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    emit: Callable[[str], None] = print,
) -> int:
    """Poll until success, error, refresh exhaustion or timeout."""
    deadline = clock() + max(0.0, float(timeout))
    refresh_count = 0
    key = qrcode_key
    last_message = ""

    def note(message: str) -> None:
        nonlocal last_message
        if message != last_message:
            emit(message)
            last_message = message

    while True:
        if deadline - clock() <= 0:
            note(f"等待超时（{int(timeout)} 秒），未完成登录。")
            return 1

        try:
            result = client.qr_poll(key)
        except BilibiliError as exc:
            emit(f"登录失败：{exc}")
            return 1

        refresh_count, key, exit_code = handle_poll_result(
            client,
            result,
            qrcode_key=key,
            qr_path=qr_path,
            html_path=html_path,
            refresh_count=refresh_count,
            max_refreshes=max_refreshes,
            emit=note,
        )
        if exit_code is not None:
            return exit_code

        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(max(0.0, float(interval)), remaining))


# ---------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bili_login_ui.py",
        description="生成 B 站扫码登录二维码和本地页面，扫码后自动保存 Cookie。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"等待扫码的总秒数（默认 {DEFAULT_TIMEOUT}）",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器（页面和图片仍会生成）",
    )
    parser.add_argument(
        "--qr-file",
        default=DEFAULT_QR_FILE,
        help=f"二维码 PNG 路径（默认 {DEFAULT_QR_FILE}）",
    )
    parser.add_argument(
        "--html-file",
        default=DEFAULT_HTML_FILE,
        help=f"登录页面路径（默认 {DEFAULT_HTML_FILE}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    qr_path = resolve_path(args.qr_file)
    html_path = resolve_path(args.html_file)

    try:
        config = load_config()
        client = BilibiliClient.from_config(config)
        qr = client.qr_generate()
        write_qr_png(str(qr.get("url") or ""), qr_path)
        write_login_page(html_path, qr_path)

        print("二维码已生成。")
        print(f"二维码图片：{qr_path}")
        print(f"登录页面：{html_path}")
        print("请用 B 站手机客户端扫码。")

        if not args.no_browser:
            try:
                opened = webbrowser.open(html_path.as_uri())
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                print(f"未能自动打开浏览器，请手动打开：{html_path}")

        return run_login(
            client,
            str(qr.get("qrcode_key") or ""),
            qr_path=qr_path,
            html_path=html_path,
            timeout=args.timeout,
        )
    except QrCodeMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except BilibiliError as exc:
        print(f"B 站接口错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消登录。", file=sys.stderr)
        return 0
    except OSError as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
