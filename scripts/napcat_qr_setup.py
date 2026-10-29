#!/usr/bin/env python3
"""One-click phone setup page: MCP bearer + NapCat login QR.

Why
---
RikkaHub (or any phone browser) needs two things to recover a kicked QQ:
the MCP bearer token and a fresh QR image.  Asking the user to SSH into the
cloud for the token defeats the "not at the computer" case.  This tiny page
serves both over the tailnet, behind a capability URL:

    https://fatfish-cloud.<tailnet>.ts.net:9452/s/<NAPCAT_QR_ACCESS_KEY>

The page shows:
  * the dedicated MCP bearer (``NAPCAT_QR_MCP_TOKEN`` -- NOT the web panel
    token), with a copy button;
  * a ready-to-paste RikkaHub ``mcpServers`` JSON snippet;
  * the live QR image, with a refresh button (calls the MCP ``napcat_qr`` tool).

Security
--------
* Served only on 127.0.0.1:9452; expose it via ``tailscale serve``, never a
  public port.
* The URL key is a capability, checked in constant time; every API call also
  carries it.  The page/token are ``Cache-Control: no-store`` and the access
  key is never written to the journal.
* The bearer shown here is scoped to the QR MCP endpoint, not the fatfish web
  panel.  Rotate both env values if the link leaks.
"""

from __future__ import annotations

import argparse
import hmac
import html
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_ENV_PATH = "/opt/fatfish/.env"
DEFAULT_MCP_URL = "http://127.0.0.1:9451/mcp"
DEFAULT_PUBLIC_MCP_URL = "https://fatfish-cloud.example-tailnet.ts.net:9451/mcp"
DEFAULT_PUBLIC_BASE = "https://fatfish-cloud.example-tailnet.ts.net:9452"
DEFAULT_WEBUI_JSON = "/opt/napcat/config/webui.json"
DEFAULT_PUBLIC_WEBUI_URL = "https://fatfish-cloud.example-tailnet.ts.net:9460/webui/"


def read_env(path: str | Path) -> dict[str, str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def read_webui_token(path: str | Path) -> str:
    """NapCat WebUI token from ``webui.json`` (the 6099 login secret)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("token") or "").strip()


def mcp_call(
    url: str,
    token: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Call one MCP tool; return ``{ok, text, image, error}``."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as exc:  # network / timeout / auth
        return {
            "ok": False,
            "text": f"调用 MCP 失败：{type(exc).__name__}: {exc}"[:300],
            "image": "",
        }
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    content = result.get("content") if isinstance(result.get("content"), list) else []
    texts: list[str] = []
    image = ""
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            texts.append(str(item.get("text") or ""))
        elif item.get("type") == "image":
            mime = str(item.get("mimeType") or "image/png")
            data_b64 = str(item.get("data") or "")
            if data_b64:
                image = f"data:{mime};base64,{data_b64}"
    if not result:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        texts.append(str(error.get("message") or "MCP 返回为空"))
    return {
        "ok": not bool(result.get("isError")),
        "text": "\n".join(t for t in texts if t).strip()[:2000],
        "image": image,
    }


def _config_json(public_mcp_url: str, token: str) -> str:
    return json.dumps(
        {
            "mcpServers": {
                "fatfishnapcatqr": {
                    "type": "streamable_http",
                    "url": public_mcp_url,
                    "headers": {"Authorization": "Bearer " + token},
                }
            }
        },
        ensure_ascii=False,
        indent=2,
    )


def render_page(
    *,
    key: str,
    token: str,
    public_mcp_url: str,
    public_base: str,
    webui_token: str = "",
    public_webui_url: str = DEFAULT_PUBLIC_WEBUI_URL,
) -> str:
    """Mobile-first one-click page.  All secrets are escaped/JSON-encoded."""
    config = _config_json(public_mcp_url, token)
    page_link = f"{public_base.rstrip('/')}/s/{key}"
    token_html = html.escape(token)
    config_html = html.escape(config)
    key_js = json.dumps(key)
    webui_token_js = json.dumps(webui_token)
    webui_base_js = json.dumps(public_webui_url)
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>NapCat 一键取码</title>
<style>
:root{{color-scheme:light dark}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:18px;background:#f5f6fa;color:#1f2430}}
.card{{background:#fff;border-radius:14px;padding:16px;margin:0 0 14px;box-shadow:0 2px 12px rgba(0,0,0,.06)}}
h1{{font-size:20px;margin:0 0 6px}} h2{{font-size:15px;margin:0 0 8px;color:#57606a}}
.muted{{color:#6b7280;font-size:13px;line-height:1.5}}
code,textarea{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}}
textarea{{width:100%;box-sizing:border-box;border:1px solid #d0d7de;border-radius:8px;padding:10px;background:#f6f8fa;color:#1f2430;resize:vertical}}
.row{{display:flex;gap:8px;align-items:center;margin-top:8px}}
button{{border:0;border-radius:10px;padding:12px 14px;font-size:15px;font-weight:600;background:#2563eb;color:#fff;flex:1}}
button.secondary{{background:#e5e7eb;color:#111827}}
button:active{{transform:scale(.99)}}
#qr{{width:min(78vw,320px);height:auto;display:block;margin:12px auto;border:1px solid #e5e7eb;border-radius:12px;background:#fff}}
#msg{{white-space:pre-wrap;font-size:14px;line-height:1.55;margin:8px 0 0}}
.badge{{display:inline-block;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:700}}
.ok{{background:#dcfce7;color:#166534}} .bad{{background:#fee2e2;color:#991b1b}} .wait{{background:#fef3c7;color:#92400e}}
.hint{{font-size:12px;color:#6b7280;margin-top:6px}}
</style>
</head>
<body>
<div class="card">
  <h1>NapCat 一键取码</h1>
  <div class="muted">RikkaHub 配置 + 当前登录二维码。仅 tailnet 内可访问。</div>
  <div class="row"><span id="badge" class="badge wait">准备中</span><span class="muted" id="phase"></span></div>
</div>

<div class="card">
  <h2>1. MCP 配置</h2>
  <div class="muted">RikkaHub → 设置 → MCP → 导入 JSON（或手动填 URL + Header）。</div>
  <textarea id="config" rows="9" readonly>{config_html}</textarea>
  <div class="row"><button onclick="copyText(document.getElementById('config').value,'配置')">复制 RikkaHub 配置</button></div>
  <div class="row"><button class="secondary" onclick="copyText('{token_html}','Token')">只复制 Token</button></div>
  <div class="hint">URL：{html.escape(public_mcp_url)}<br>Token 只用于这个 MCP 端点，不是 Web 面板 token。</div>
</div>

<div class="card">
  <h2>2. NapCat WebUI 手机登录</h2>
  <div class="muted">这是 NapCat 自己的 WebUI，token 权限比 MCP token 大；改配置/看日志时才用。点按钮会自动带 token 打开。</div>
  <div class="row"><button onclick="openWebui()">打开手机 WebUI</button></div>
  <div class="row"><button class="secondary" onclick="copyText(WEBUI_TOKEN,'WebUI Token')">复制 WebUI Token</button></div>
  <div class="hint" id="webuiUrl"></div>
</div>

<div class="card">
  <h2>3. 扫码登录</h2>
  <img id="qr" alt="NapCat 二维码" hidden>
  <div id="msg" class="muted">正在获取二维码…</div>
  <div class="row"><button onclick="refreshQR()">刷新二维码</button></div>
  <div class="hint">二维码约 2 分钟有效；过期就点一次刷新。扫码后等 5–10 秒会自动变绿。</div>
</div>

<div class="card">
  <h2>4. 配置入口</h2>
  <div class="muted">这个页面的链接本身就是钥匙，别转发；泄露了在云端换 <code>NAPCAT_QR_ACCESS_KEY</code> 与 <code>NAPCAT_QR_MCP_TOKEN</code>。</div>
  <div class="hint">{html.escape(page_link)}</div>
</div>

<script>
const KEY = {key_js};
const WEBUI_TOKEN = {webui_token_js};
const WEBUI_BASE = {webui_base_js};
function openWebui() {{
  const url = WEBUI_BASE + (WEBUI_BASE.includes('?') ? '&' : '?') +
    'token=' + encodeURIComponent(WEBUI_TOKEN);
  window.open(url, '_blank', 'noopener');
}}
document.getElementById('webuiUrl').textContent =
  WEBUI_BASE + '（按钮会自动带 token）';
async function refreshQR() {{
  const badge = document.getElementById('badge');
  const phase = document.getElementById('phase');
  const msg = document.getElementById('msg');
  const img = document.getElementById('qr');
  badge.className = 'badge wait'; badge.textContent = '获取中';
  phase.textContent = '';
  msg.textContent = '正在拉起 NapCat / 刷新二维码，最多约 30 秒…';
  try {{
    const r = await fetch('/api/qr?key=' + encodeURIComponent(KEY), {{cache:'no-store'}});
    const j = await r.json();
    const online = /已在线|already online/i.test(j.text || '');
    badge.className = 'badge ' + (online ? 'ok' : (j.ok ? 'ok' : 'bad'));
    badge.textContent = online ? '已在线' : (j.ok ? '二维码已就绪' : '失败');
    phase.textContent = online ? '无需扫码' : '';
    msg.textContent = j.text || '';
    if (j.image) {{ img.src = j.image; img.hidden = false; }} else {{ img.hidden = true; }}
  }} catch (e) {{
    badge.className = 'badge bad'; badge.textContent = '网络错误';
    msg.textContent = String(e);
  }}
}}
async function copyText(text, label) {{
  try {{
    await navigator.clipboard.writeText(text);
    alert(label + ' 已复制');
  }} catch (e) {{
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
    alert(label + ' 已复制');
  }}
}}
async function checkStatus() {{
  try {{
    const r = await fetch('/api/status?key=' + encodeURIComponent(KEY), {{cache:'no-store'}});
    const j = await r.json();
    if (!j.ok) return;
    if ((j.text || '').includes('"online": true')) {{
      const badge = document.getElementById('badge');
      badge.className = 'badge ok';
      badge.textContent = '已在线';
      document.getElementById('phase').textContent = '扫码成功';
      document.getElementById('msg').textContent = 'QQ 已在线，可以开始聊天了。';
      document.getElementById('qr').hidden = true;
    }}
  }} catch (e) {{}}
}}
refreshQR();
setInterval(checkStatus, 4000);
setInterval(() => {{
  if (!document.getElementById('qr').hidden) return;
  refreshQR();
}}, 15000);
</script>
</body>
</html>"""


class SetupHandler(BaseHTTPRequestHandler):
    server_version = "napcat-qr-setup/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Never journal the capability key in /s/<key> or query strings.
        path = str(self.path).split("?", 1)[0]
        if path.startswith("/s/"):
            path = "/s/***"
        print(f"[napcat-qr-setup] {self.address_string()} {path}", flush=True)

    def _key_ok(self, supplied: str) -> bool:
        expected = getattr(self.server, "access_key", "")
        return bool(expected) and hmac.compare_digest(
            str(supplied or ""), str(expected)
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _query_key(self) -> str:
        from urllib.parse import parse_qs, urlsplit

        return str(parse_qs(urlsplit(self.path).query).get("key", [""])[0])

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import urlsplit

        path = urlsplit(self.path).path
        if path.startswith("/s/"):
            supplied = path[3:].strip("/")
            if not self._key_ok(supplied):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            # Re-read webui.json per page load: a container recreate may rotate
            # the WebUI token without restarting this service.
            webui_token = read_webui_token(
                getattr(self.server, "webui_json", DEFAULT_WEBUI_JSON)
            ) or getattr(self.server, "webui_token", "")
            page = render_page(
                key=getattr(self.server, "access_key"),
                token=getattr(self.server, "mcp_token"),
                public_mcp_url=getattr(self.server, "public_mcp_url"),
                public_base=getattr(self.server, "public_base"),
                webui_token=webui_token,
                public_webui_url=getattr(
                    self.server, "public_webui_url", DEFAULT_PUBLIC_WEBUI_URL
                ),
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/qr":
            if not self._key_ok(self._query_key()):
                self._json(403, {"ok": False, "text": "forbidden"})
                return
            result = mcp_call(
                getattr(self.server, "mcp_url"),
                getattr(self.server, "mcp_token"),
                "napcat_qr",
            )
            self._json(200, result)
            return
        if path == "/api/status":
            if not self._key_ok(self._query_key()):
                self._json(403, {"ok": False, "text": "forbidden"})
                return
            result = mcp_call(
                getattr(self.server, "mcp_url"),
                getattr(self.server, "mcp_token"),
                "napcat_status",
                timeout=20,
            )
            self._json(200, result)
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def serve(
    *,
    host: str,
    port: int,
    access_key: str,
    mcp_token: str,
    mcp_url: str,
    public_mcp_url: str,
    public_base: str,
    webui_token: str,
    public_webui_url: str,
    webui_json: str,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, int(port)), SetupHandler)
    server.access_key = access_key  # type: ignore[attr-defined]
    server.mcp_token = mcp_token  # type: ignore[attr-defined]
    server.mcp_url = mcp_url  # type: ignore[attr-defined]
    server.public_mcp_url = public_mcp_url  # type: ignore[attr-defined]
    server.public_base = public_base  # type: ignore[attr-defined]
    server.webui_token = webui_token  # type: ignore[attr-defined]
    server.public_webui_url = public_webui_url  # type: ignore[attr-defined]
    server.webui_json = webui_json  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NapCat QR one-click setup page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9452)
    parser.add_argument("--env-file", default=DEFAULT_ENV_PATH)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--public-mcp-url", default=DEFAULT_PUBLIC_MCP_URL)
    parser.add_argument("--public-base", default=DEFAULT_PUBLIC_BASE)
    parser.add_argument("--webui-json", default=DEFAULT_WEBUI_JSON)
    parser.add_argument("--public-webui-url", default=DEFAULT_PUBLIC_WEBUI_URL)
    args = parser.parse_args(argv)

    env = read_env(args.env_file)
    token = os.environ.get("NAPCAT_QR_MCP_TOKEN", "").strip() or env.get(
        "NAPCAT_QR_MCP_TOKEN", ""
    )
    key = os.environ.get("NAPCAT_QR_ACCESS_KEY", "").strip() or env.get(
        "NAPCAT_QR_ACCESS_KEY", ""
    )
    webui_token = os.environ.get("NAPCAT_WEBUI_TOKEN", "").strip() or read_webui_token(
        args.webui_json
    )
    if not token or not key:
        print(
            "[napcat-qr-setup] missing NAPCAT_QR_MCP_TOKEN or "
            "NAPCAT_QR_ACCESS_KEY",
            flush=True,
        )
        return 2
    if not webui_token:
        print(
            f"[napcat-qr-setup] warning: no NapCat WebUI token yet "
            f"({args.webui_json}); the WebUI link will fill in after the "
            f"container writes it",
            flush=True,
        )
    server = serve(
        host=args.host,
        port=args.port,
        access_key=key,
        mcp_token=token,
        mcp_url=args.mcp_url,
        public_mcp_url=args.public_mcp_url,
        public_base=args.public_base,
        webui_token=webui_token,
        public_webui_url=args.public_webui_url,
        webui_json=args.webui_json,
    )
    print(
        f"[napcat-qr-setup] listening on http://{args.host}:{args.port} "
        f"(page /s/<key>)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
