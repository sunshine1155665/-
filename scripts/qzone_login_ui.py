"""Click-to-run QQ 空间 QR login with a local auto-refreshing page.

Run directly::

    python scripts/qzone_login_ui.py

Mirrors ``scripts/bili_login_ui.py`` / ``scripts/qq_login_ui.py``: the script
writes ``data/qq_login_qr.png`` and a small Chinese HTML page, opens the page
in the default browser, then polls the ptlogin2 ``ptqrlogin`` endpoint through
``fatfish.qzone.qr_login.QrLogin``.  The page refreshes the QR image every two
seconds with a cache-busting query, and an expired QR is regenerated
automatically (up to ``MAX_REFRESHES`` times) — the window never races the
expiry, the user never has to do anything but scan and confirm.

On success the check_sig redirect cookies (uin/skey/p_skey/...) are captured,
merged and saved to the configured Qzone cookie file.  Cookie values, qrsig
and the check_sig URL are never printed or rendered: only state text, field
names and a masked uin.
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

from fatfish.config import load_config  # noqa: E402
from fatfish.qzone.qr_login import QrLogin, QrLoginError  # noqa: E402

DEFAULT_QR_FILE = "data/qq_login_qr.png"
DEFAULT_HTML_FILE = "data/qzone_login.html"
DEFAULT_TIMEOUT = 600
POLL_INTERVAL_SECONDS = 3.0
MAX_REFRESHES = 5
INSTRUCTION = "QQ 空间扫码登录"
SUCCESS_MESSAGE = "登录成功，可以关闭此页面"


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
  color: #0b8fd6;
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
  <h1>{INSTRUCTION}</h1>
  <p class="hint">用手机 QQ 扫码，并在手机上点「登录确认」</p>
  <img id="qr" class="qr" src="{src}" data-src="{src}" alt="QQ 空间登录二维码" width="420" height="420">
  <p id="status" class="status">{status}</p>
  <p class="tip">打开手机 QQ，点击右上角扫一扫。</p>
  <p class="tip">二维码过期后会自动换新码，页面每 2 秒自动刷新，过期了也不用管。</p>
  <p class="tip">登录成功后本页会自动变成绿色提示，届时可以关闭。</p>
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
  window.setInterval(refresh, 2000);
}})();
</script>
</body>
</html>
"""


def render_success_html(*, fields: list[str] | None = None, uin_masked: str = "") -> str:
    """Green success page shown after the phone confirms the login."""
    extras = ""
    if fields:
        shown = ", ".join(sorted(fields))
        extras += f'<p class="tip">已保存字段：{html.escape(shown)}</p>'
    if uin_masked:
        extras += f'<p class="tip">账号：{html.escape(str(uin_masked))}</p>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{INSTRUCTION}成功</title>
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
  {extras}
  <p class="tip">登录信息已经保存在本机，不会上传到任何第三方。</p>
  <p class="tip">可以关闭这个页面，回到「QQ空间登录」窗口。</p>
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


def write_success_page(
    html_path: str | Path,
    *,
    fields: list[str] | None = None,
    uin_masked: str = "",
) -> Path:
    return write_html(
        html_path,
        render_success_html(fields=fields, uin_masked=uin_masked),
    )


# --------------------------------------------------------------------------- poll
def status_page_text(result: dict[str, Any], login: Any = None) -> str:
    """T-0021: the login page mirrors the SPECIFIC state, never a bare
    「登录失败」— WAF cooldown carries its wait, param exhaustion its meaning."""
    status = str(result.get("status") or "error")
    if status == "waf_cooldown":
        minutes = max(1, int(float(result.get("retry_after") or 0) // 60))
        tail = "，多次拦截建议换网络或用浏览器粘 cookie" if result.get("give_up") else "，急用可先用浏览器粘 cookie"
        return f"腾讯风控冷却中，{minutes} 分钟后自动重试{tail}"
    if status == "param_error":
        if result.get("in_grace"):
            return "已扫码，正在确认（风控延迟约 5 秒），请稍候…"
        return "QQ 拒绝了登录参数，正在轮换候选组合…"
    if status == "net_retry":
        message = str(result.get("message") or "").strip()
        return f"网络波动（{message}），继续等待…" if message else "网络波动，继续等待…"
    if status == "unparseable":
        return "登录服务响应异常，继续等待…"
    if status == "expired" and login is not None and getattr(login, "in_scan_grace", lambda: False)():
        return "已扫码，正在确认（风控延迟约 5 秒），请稍候…"
    return ""


def handle_poll_result(
    login: Any,
    result: dict[str, Any],
    *,
    html_path: str | Path,
    refresh_count: int,
    max_refreshes: int = MAX_REFRESHES,
    emit: Callable[[str], None] = print,
) -> tuple[int, int | None]:
    """Apply one poll result.

    Returns ``(refresh_count, exit_code)``; ``exit_code`` is ``None`` when
    polling should continue.  Never prints cookie values, qrsig or the
    check_sig URL — only the whitelisted status lines, field names and the
    masked uin.
    """
    status = str(result.get("status") or "error")

    def page_status(text: str) -> None:
        png = getattr(login, "png_path", None)
        if png is not None and text:
            write_login_page(html_path, png, status_text=text)

    if status == "waiting":
        emit("等待扫码…")
        return refresh_count, None

    if status in {"scanned", "confirming"}:
        emit("已扫码，请在手机上确认…")
        return refresh_count, None

    if status == "waf_cooldown":
        line = status_page_text(result)
        emit(line)
        page_status(line)
        if result.get("give_up"):
            emit("风控冷却未解除，已停止轮询；建议换个网络环境再试，或用浏览器粘 cookie 兜底。")
            return refresh_count, 1
        return refresh_count, None

    if status in {"net_retry", "unparseable", "param_error"}:
        # param_error here means the whole candidate ladder was walked once
        # (or sits inside the scan grace, where the ladder is frozen);
        # QrLogin keeps retrying within the timeout, so keep the window up.
        line = status_page_text(result)
        emit(line or "网络波动或参数待重试，继续等待…")
        if line:
            page_status(line)
        return refresh_count, None

    if status == "success":
        captured = dict(result.get("captured") or {})
        from fatfish.qzone.qr_login import mask_uin

        write_success_page(
            html_path,
            fields=sorted(captured),
            uin_masked=mask_uin(captured.get("uin", "")),
        )
        emit("登录成功，可以关闭此页面。")
        return refresh_count, 0

    if status == "expired":
        # T-0021: 手机确认后的 ~5s 风控窗口里，65 多为噪声——换码会作废登录。
        if getattr(login, "in_scan_grace", lambda: False)():
            line = "二维码显示过期但扫码确认进行中，宽限等待…"
            emit(line)
            page_status(line)
            return refresh_count, None
        if refresh_count >= max_refreshes:
            emit(f"二维码已连续刷新 {max_refreshes} 次仍未登录，已停止。")
            return refresh_count, 1
        qr_path = login.generate()
        write_login_page(html_path, qr_path)
        emit("二维码已过期，已自动换新码")
        return refresh_count + 1, None

    message = str(result.get("message") or "").strip()
    emit(f"登录失败：{message or '未知错误'}")
    return refresh_count, 1


def run_login(
    login: Any,
    *,
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
            result = login.poll_step()
        except QrLoginError as exc:
            emit(f"登录失败：{exc}")
            return 1

        refresh_count, exit_code = handle_poll_result(
            login,
            result,
            html_path=html_path,
            refresh_count=refresh_count,
            max_refreshes=max_refreshes,
            emit=note,
        )
        if exit_code is not None:
            return exit_code

        remaining = deadline - clock()
        if remaining > 0:
            # WAF 退避：poll_step 自己不会再打 ptqrlogin，这里同步拉长
            # 本地页轮询节奏（等满 retry_after，但不超过总剩余时间）。
            pause = float(result.get("retry_after") or 0) or interval
            sleep(min(max(0.0, pause), remaining))


# ---------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qzone_login_ui.py",
        description="生成 QQ 空间扫码登录二维码和本地页面，扫码后自动保存 Cookie。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"等待扫码的总秒数（默认 {DEFAULT_TIMEOUT}）",
    )
    parser.add_argument(
        "--max-refreshes",
        type=int,
        default=MAX_REFRESHES,
        help=f"二维码过期后自动换新码的次数上限（默认 {MAX_REFRESHES}）",
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
        login = QrLogin(config=config)
        png = login.generate()
        # generate() 决定 PNG 落点（config.data_dir）；这里若用户覆盖了
        # --qr-file，则把文件搬到用户指定的位置再写页面。
        if png != qr_path:
            qr_path.parent.mkdir(parents=True, exist_ok=True)
            qr_path.write_bytes(Path(png).read_bytes())
            login.png_path = qr_path
        write_login_page(html_path, qr_path)

        print("二维码已生成。")
        print(f"二维码图片：{qr_path}")
        print(f"登录页面：{html_path}")
        print("请用手机 QQ 扫码，并在手机上点「登录确认」。")

        if not args.no_browser:
            try:
                opened = webbrowser.open(html_path.as_uri())
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                print(f"未能自动打开浏览器，请手动打开：{html_path}")

        return run_login(
            login,
            html_path=html_path,
            timeout=args.timeout,
            max_refreshes=args.max_refreshes,
        )
    except QrLoginError as exc:
        print(f"QQ 空间接口错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消登录。", file=sys.stderr)
        return 0
    except OSError as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
