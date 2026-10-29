"""QQ 空间「免扫码」续期：借本机浏览器的登录态换新 cookie。

为什么不是再写一个扫码
----------------------
``ptlogin2`` 的扫码链路（``ptqrshow`` / ``ptqrlogin``）是腾讯风控最敏感的
一段，T-0020 实测会进冷却（见 :mod:`fatfish.qzone.qr_login`）。这里换一条
完全不同的路：

1. 起一个**独立 profile** 的 Edge/Chrome（``--user-data-dir`` 指向
   ``data/run/qzone-browser``），浏览器本身是原装正版；
2. 通过 CDP 的 ``Storage.setCookies`` 把本地已有的 cookie 灌进去；
3. 让**浏览器自己**打开 ``user.qzone.qq.com`` 做正常的会话续期——这一步在
   服务端看来就是一个普通用户打开了自己的空间，我们不发任何私有协议请求；
4. 再用 ``Storage.getCookies`` 把续期后的 cookie 读回来落盘。

失效兜底：如果旧 cookie 连浏览器都续不上，窗口会停在 QQ 登录页；用户在里面
正常登录一次（扫码/密码都行），同一段轮询会把新 cookie 收走。所以这条路
**永远不会比现在更差**：最坏情况就是回到手动登录。

安全边界
--------
* cookie 值只在本进程内传递：不打印、不写日志、不进返回值；
* 对外输出只有字段名、掩码 uin、布尔量（与 ``qr_login`` 同一套红线）；
* profile 落在 ``data/run/`` 下（运行时目录，不进备份、不进仓）。

依赖
----
Python 标准库 + 本机已安装的浏览器。CDP 走一个最小的 WebSocket 客户端
（Chrome DevTools Protocol 只需要文本帧），不引入 websocket-client。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .client import QzoneClient, QzoneError, _cfg, _clean_cookie_mapping, _data_dir
from .qr_login import mask_uin, normalize_uin

#: CDP cookie 的落点。``.qq.com`` 覆盖 ``user.qzone.qq.com``，一处就够：
#: 服务端只校验 cookie 值本身，不看它挂在哪个子域。
COOKIE_DOMAIN = ".qq.com"
COOKIE_PATH = "/"

#: 只把这些字段搬回本地文件。多带的 cookie 只会让请求头变长、让落盘文件
#: 更像一个凭据仓库，没有任何收益。
HARVEST_FIELDS = frozenset(
    {
        "uin",
        "skey",
        "p_skey",
        "p_uin",
        "luin",
        "p_luin",
        "ptcz",
        "pt4_token",
        "qzonetoken",
        "qzone_cookie",
        "qlogin_uid",
    }
)

#: 判「浏览器确实续过期了」的信号字段。
ROTATION_FIELDS = ("skey", "p_skey", "qzonetoken")

DEFAULT_PROFILE_DIRNAME = "qzone-browser"
DEFAULT_TIMEOUT_SECONDS = 300.0
POLL_INTERVAL_SECONDS = 2.0
DEVTOOLS_WAIT_SECONDS = 20.0
#: headless 下没人能登录，等这么久就该报"请开窗口"了。
HEADLESS_WAIT_SECONDS = 20.0
#: 拿不到 ``g_qzonetoken`` 时改用服务端探测，两次探测之间的间隔与上限。
PROBE_EVERY_SECONDS = 10.0
MAX_PROBES = 6
QZONE_HOME_URL = "https://user.qzone.qq.com/{uin}"

#: 常见的浏览器安装位置；``%VAR%`` 由 :func:`_expand` 展开。
#: Linux 上的路径不展开也没关系（没有 ``%``），Windows 上不存在就是了。
BROWSER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "edge": (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    ),
    "chrome": (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ),
}

#: 定位 DevTools 端口时读的文件名（Chromium 在 user-data-dir 下写）。
DEVTOOLS_PORT_FILE = "DevToolsActivePort"

#: Chrome DevTools Protocol 的文本帧 opcode。
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA


class BrowserLoginError(RuntimeError):
    """浏览器续期失败；消息里不含 cookie 值。"""


@dataclass(frozen=True)
class BrowserInfo:
    name: str
    path: Path


#: 环境变量占位符。必须一次性替换：顺序 replace 会把 ``%ProgramFiles(x86)%``
#: 先命中 ``%ProgramFiles%``，留下一个 ``(x86)%`` 尾巴。
_ENV_TOKEN_RE = re.compile(r"%([^%]+)%")


# --------------------------------------------------------------------- 发现浏览器
def _expand(value: str, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return _ENV_TOKEN_RE.sub(
        lambda match: env.get(match.group(1), match.group(0)), value
    )


def browser_candidates(env: dict[str, str] | None = None) -> list[BrowserInfo]:
    """Edge / Chrome 的候选路径，按优先级展开（不检查存在性）。"""
    found: list[BrowserInfo] = []
    for name, templates in BROWSER_CANDIDATES.items():
        for template in templates:
            expanded = _expand(template, env)
            if expanded and "%" not in expanded:
                found.append(BrowserInfo(name=name, path=Path(expanded)))
    return found


def find_browser(
    name: str | None = None,
    *,
    path: str | Path | None = None,
    env: dict[str, str] | None = None,
    exists: Callable[[Path], bool] | None = None,
) -> BrowserInfo:
    """定位一个可执行的浏览器。

    优先级：显式 ``path`` → ``name`` 对应的候选 → Edge → Chrome。
    找不到时抛 :class:`BrowserLoginError`（不是 ``FileNotFoundError``，
    调用方要把它当成"环境没准备好"而不是代码 bug）。
    """
    check = exists or (lambda p: p.is_file())
    if path:
        explicit = Path(str(path)).expanduser()
        if not check(explicit):
            raise BrowserLoginError(f"浏览器可执行文件不存在：{explicit}")
        return BrowserInfo(name=str(name or "custom"), path=explicit)
    wanted = str(name or "").strip().lower()
    candidates = browser_candidates(env)
    if wanted:
        candidates = [c for c in candidates if c.name == wanted]
        if not candidates:
            raise BrowserLoginError(f"不支持的浏览器名：{wanted}（只认 edge / chrome）")
    for candidate in candidates:
        if check(candidate.path):
            return candidate
    names = " / ".join(sorted({c.name for c in browser_candidates(env)})) or "edge / chrome"
    raise BrowserLoginError(f"没找到可用的浏览器（{names}）；用 --browser-path 显式指定")


# --------------------------------------------------------------------- 启动参数
def needs_no_sandbox(
    *,
    platform: str | None = None,
    euid: int | None = None,
) -> bool:
    """Linux 上以 root 跑 Chromium 必须加 ``--no-sandbox``（容器里很常见）。"""
    if (platform or os.name) == "nt":
        return False
    if euid is None:
        getter = getattr(os, "geteuid", None)
        euid = getter() if callable(getter) else 1
    return int(euid) == 0


def require_display(*, platform: str | None = None, env: dict[str, str] | None = None) -> None:
    """非 Windows 又不开真无头时，必须能拿到 X/Wayland display。

    qzone 会拒绝 ``--headless=new``（2026-10-29 实测），所以云服务器上跑
    免扫码续期需要 ``xvfb-run`` 之类的虚拟显示；没有就直接给出可执行的提示，
    而不是让 Chromium 在后台悄悄起不来。
    """
    if (platform or os.name) == "nt":
        return
    env = os.environ if env is None else env
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return
    raise BrowserLoginError(
        "没有图形环境：QQ 空间会拒绝无头浏览器，"
        "请用虚拟显示运行（例如 `xvfb-run -a python -m fatfish qzone refresh`）"
    )


def build_launch_args(
    browser: BrowserInfo,
    profile_dir: str | Path,
    *,
    url: str | None = None,
    headless: bool = False,
    offscreen: bool = False,
    true_headless: bool = False,
) -> list[str]:
    """组装启动参数。

    ``--remote-debugging-port=0`` 让 Chromium 自己挑端口，并把端口写进
    ``<profile>/DevToolsActivePort``——比硬编码 9222 安全（不占固定端口、
    不与用户自己的调试会话打架）。

    ``offscreen`` 是**无人值守**模式：不隐藏浏览器、只把窗口挪到屏幕外。
    2026-10-29 实测 qzone 会拒绝 ``--headless=new``（同样的 cookie，headful
    能进空间、headless 直接被打回登录页），所以"无人值守"不能等于"无头"。
    """
    args = [
        str(browser.path),
        f"--user-data-dir={Path(profile_dir)}",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
        "--disable-component-update",
    ]
    if true_headless:
        args.append("--headless=new")
    if offscreen:
        args += ["--window-position=-32000,-32000", "--window-size=1280,900"]
    if needs_no_sandbox():
        args.append("--no-sandbox")
    args.append(url or "about:blank")
    return args


def read_devtools_port(
    profile_dir: str | Path,
    *,
    timeout: float = DEVTOOLS_WAIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str] | None:
    """等 ``DevToolsActivePort`` 出现并解析成 ``(port, ws_path)``。

    返回 ``None`` 表示超时；格式坏掉也返回 ``None``（浏览器版本差异不该让
    调用方崩在解析上）。
    """
    target = Path(profile_dir) / DEVTOOLS_PORT_FILE
    deadline = clock() + max(0.0, float(timeout))
    while True:
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        if len(lines) >= 2:
            try:
                port = int(lines[0].strip())
            except ValueError:
                return None
            ws_path = lines[1].strip()
            if port > 0 and ws_path:
                return port, ws_path
            return None
        if clock() >= deadline:
            return None
        sleep(0.25)


def live_devtools(
    profile_dir: str | Path, *, timeout: float = 0.5
) -> tuple[int, str] | None:
    """已有一个**活着的**浏览器实例时返回它的 ``(port, ws_path)``。

    ``--keep-open`` 留下的浏览器可以复用：省掉一次启动（5–10 秒），而且
    那个浏览器里还留着 `skey` 这类 session cookie——等于多一份活凭据。
    端口文件存在但连不上（残留）时返回 ``None``，让调用方重新启动。
    """
    found = read_devtools_port(profile_dir, timeout=0.0)
    if found is None:
        return None
    port, ws_path = found
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            pass
    except OSError:
        return None
    return port, ws_path


# --------------------------------------------------------------------- WebSocket
class WebSocketConnection:
    """够用就好的 WebSocket 客户端（只跑 CDP 的文本帧）。

    刻意不引 ``websocket-client``：QQ 适配器那边它是可选依赖，而 QQ 空间
    这条链路必须能在裸标准库里跑起来。
    """

    def __init__(self, host: str, port: int, path: str, *, timeout: float = 15.0):
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{int(port)}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        header = self._read_until(b"\r\n\r\n")
        status_line = header.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101" not in status_line:
            self.close()
            raise BrowserLoginError(f"WebSocket 握手被拒：{status_line}")

    # -- 读
    def _read_until(self, marker: bytes, *, limit: int = 65536) -> bytes:
        while marker not in self._buffer:
            if len(self._buffer) > limit:
                raise BrowserLoginError("WebSocket 响应头过长")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise BrowserLoginError("WebSocket 连接在握手阶段被关闭")
            self._buffer += chunk
        head, _, rest = self._buffer.partition(marker)
        self._buffer = rest
        return head + marker

    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self._buffer)))
            if not chunk:
                raise BrowserLoginError("WebSocket 连接已关闭")
            self._buffer += chunk
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out

    def _read_frame(self) -> tuple[bool, int, bytes]:
        head = self._read_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        mask_key = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked and mask_key:
            payload = apply_mask(payload, mask_key)
        return fin, opcode, payload

    def recv_message(self) -> str:
        chunks: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == _OPCODE_PING:
                self._send_frame(_OPCODE_PONG, payload)
                continue
            if opcode == _OPCODE_PONG:
                continue
            if opcode == _OPCODE_CLOSE:
                raise BrowserLoginError("DevTools 关闭了 WebSocket")
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    # -- 写
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        self.sock.sendall(encode_frame(payload, opcode=opcode))

    def send_text(self, text: str) -> None:
        self._send_frame(_OPCODE_TEXT, text.encode("utf-8"))

    def close(self) -> None:
        try:
            self._send_frame(_OPCODE_CLOSE, b"")
        except Exception:  # pragma: no cover - 已经断了就算了
            pass
        try:
            self.sock.close()
        except Exception:  # pragma: no cover
            pass


def encode_frame(payload: bytes, *, opcode: int = _OPCODE_TEXT, mask_key: bytes | None = None) -> bytes:
    """把一段 payload 编成客户端帧（客户端必须加掩码）。"""
    key = mask_key if mask_key is not None else os.urandom(4)
    if len(key) != 4:
        raise ValueError("mask_key 必须是 4 字节")
    header = bytearray([0x80 | (opcode & 0x0F)])
    size = len(payload)
    if size < 126:
        header.append(0x80 | size)
    elif size < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", size)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", size)
    header += key
    return bytes(header) + apply_mask(payload, key)


def apply_mask(payload: bytes, mask_key: bytes) -> bytes:
    return bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))


# --------------------------------------------------------------------- CDP 客户端
class CdpClient:
    """极简 CDP 请求/响应关联器；事件一律忽略。"""

    def __init__(self, ws: WebSocketConnection, *, timeout: float = 15.0):
        self.ws = ws
        self.timeout = timeout
        self._next_id = 0

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        message: dict[str, Any] = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            # 扁平会话（``Target.attachToTarget {flatten:true}``）的后续命令
            # 靠这个字段路由到具体页面。
            message["sessionId"] = session_id
        self.ws.send_text(json.dumps(message))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = self.ws.recv_message()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != message_id:
                continue
            if message.get("error"):
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise BrowserLoginError(f"{method} 失败：{detail}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        raise BrowserLoginError(f"{method} 超时")

    def close(self) -> None:
        self.ws.close()


# --------------------------------------------------------------------- cookie 搬运
def cookie_params(cookies: dict[str, str]) -> list[dict[str, Any]]:
    """本地 cookie 字典 → ``Storage.setCookies`` 的参数。"""
    params: list[dict[str, Any]] = []
    for name, value in _clean_cookie_mapping(cookies).items():
        params.append(
            {
                "name": name,
                "value": value,
                "domain": COOKIE_DOMAIN,
                "path": COOKIE_PATH,
                "secure": True,
                # 真实站点下发的 skey/p_skey 就是 HttpOnly；照着设，别让页面
                # 多出"能读凭据"的能力。
                "httpOnly": True,
                "expires": -1,
            }
        )
    return params


def _in_domains(domain: str, domains: Iterable[str]) -> bool:
    normalized = str(domain or "").lstrip(".").strip().lower()
    if not normalized:
        return False
    return any(normalized == d or normalized.endswith("." + d) for d in domains)


def cookies_from_cdp(
    raw: Any,
    *,
    domains: Iterable[str] = ("qq.com",),
) -> dict[str, str]:
    """``Storage.getCookies`` 的响应 → 只含白名单字段的字典。

    同名 cookie 可能挂在多个子域（``.qq.com`` 与 ``.qzone.qq.com``），浏览器
    发请求时按"越具体越优先"排序；这里用同样的规则挑最具体的那一份。
    """
    if not isinstance(raw, list):
        return {}
    best: dict[str, tuple[int, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if not name or not value or name not in HARVEST_FIELDS:
            continue
        domain = str(item.get("domain") or "")
        if not _in_domains(domain, domains):
            continue
        rank = len(domain.lstrip("."))
        if name not in best or rank > best[name][0]:
            best[name] = (rank, value)
    return _clean_cookie_mapping({name: value for name, (_, value) in best.items()})


def harvest_is_fresh(before: dict[str, str], after: dict[str, str]) -> bool:
    """浏览器是否真的续了一次（而不是把旧 cookie 原样吐回来）。"""
    if not after.get("uin") or not after.get("skey"):
        return False
    for field in ROTATION_FIELDS:
        if after.get(field) and after.get(field) != before.get(field):
            return True
    return False


def renewed_fields(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        name
        for name, value in after.items()
        if value and value != before.get(name)
    )


# --------------------------------------------------------------------- 页面状态
#: 页面自己的说法。判定"登录态"只信页面里的 ``g_qzonetoken`` 和落地域名：
#: cookie 是不是还在，浏览器永远都会回显（我们刚灌进去），不能当证据。
LOGIN_STATE_EXPRESSION = """(() => { try {
  return JSON.stringify({
    host: location.hostname,
    token: (typeof window.g_qzonetoken === 'string' ? window.g_qzonetoken : ''),
    loginFrame: !!document.querySelector('#login_frame, iframe#login_frame'),
    ready: document.readyState
  });
} catch (e) { return JSON.stringify({ error: String(e) }); } })()"""

#: 未登录时 qzone 会把顶层导航送到 i.qq.com / ptlogin2，并渲染登录框。
_LOGIN_WALL_HOSTS = ("i.qq.com", "ptlogin2.qq.com", "xui.ptlogin2.qq.com")


def page_is_login_wall(state: Any) -> bool:
    """页面明确停在登录页（未登录）。"""
    if not isinstance(state, dict) or state.get("error"):
        return False
    host = str(state.get("host") or "").strip().lower()
    if any(host == item or host.endswith("." + item) for item in _LOGIN_WALL_HOSTS):
        return True
    return bool(state.get("loginFrame")) and not state.get("token")


def page_is_logged_in(state: Any) -> bool:
    """页面自己确证「已登录」。

    只认 ``g_qzonetoken`` 有值。当前版本的 qzone 首页**不一定**下发它
    （2026-10-29 实测登录成功也没有），所以它只是辅助信号；真正作数的是
    ``_wait_for_session`` 里的服务端探测。
    """
    if not isinstance(state, dict) or state.get("error"):
        return False
    token = state.get("token")
    return isinstance(token, str) and bool(token)


# --------------------------------------------------------------------- 主流程
class BrowserLogin:
    """用本机浏览器完成一次 QQ 空间会话续期。

    所有外部副作用（起进程、连 CDP、读时钟、sleep）都走可注入的钩子，
    测试里全是假实现，绝不真的打开窗口或联网。
    """

    def __init__(
        self,
        config: Any,
        client: QzoneClient | None = None,
        *,
        browser_name: str | None = None,
        browser_path: str | Path | None = None,
        profile_dir: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        finder: Callable[..., BrowserInfo] = find_browser,
        popen: Callable[..., Any] = subprocess.Popen,
        connector: Callable[[int, str], CdpClient] | None = None,
        live_finder: Callable[[str | Path], tuple[int, str] | None] = live_devtools,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.client = client
        self.browser_name = browser_name
        self.browser_path = browser_path
        data_dir = _data_dir(config)
        raw_profile = profile_dir or _cfg(
            config, "qzone.browser_profile", DEFAULT_PROFILE_DIRNAME
        )
        profile = Path(str(raw_profile))
        if not profile.is_absolute():
            profile = data_dir / "run" / profile
        self.profile_dir = profile.resolve()
        self.timeout = max(1.0, float(timeout))
        self._finder = finder
        self._popen = popen
        self._connector = connector or _connect_cdp
        self._live_finder = live_finder
        self._clock = clock
        self._sleep = sleep
        self.process: Any | None = None
        self.cdp: CdpClient | None = None
        #: 只关自己起的浏览器。复用一个 ``--keep-open`` 留下的实例时不能杀它。
        self.owned = False

    # -- 准备
    def _client(self) -> QzoneClient:
        if self.client is None:
            self.client = QzoneClient.from_config(self.config)
        return self.client

    def _resolve_browser(self, path_override: str | Path | None = None) -> BrowserInfo:
        return self._finder(
            self.browser_name,
            path=path_override or self.browser_path,
        )

    def _launch(
        self,
        browser: BrowserInfo,
        *,
        headless: bool,
        url: str,
        true_headless: bool = False,
    ) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # 上一次异常退出可能留下过期的端口文件，先清掉。
        try:
            (self.profile_dir / DEVTOOLS_PORT_FILE).unlink()
        except OSError:
            pass
        args = build_launch_args(
            browser,
            self.profile_dir,
            url=url,
            offscreen=headless and not true_headless,
            true_headless=true_headless,
        )
        if not true_headless:
            require_display()
        self.process = self._popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        self.owned = True

    def _connect(self) -> CdpClient:
        found = read_devtools_port(
            self.profile_dir,
            timeout=DEVTOOLS_WAIT_SECONDS,
            clock=self._clock,
            sleep=self._sleep,
        )
        if found is None:
            raise BrowserLoginError("浏览器没有暴露 DevTools 端口（启动失败或版本不支持）")
        port, ws_path = found
        self.cdp = self._connector(port, ws_path)
        return self.cdp

    def close(self) -> None:
        """收尾：先关 CDP 再关浏览器进程（只关自己起的那个）。"""
        if self.cdp is not None:
            try:
                self.cdp.close()
            except Exception:  # pragma: no cover
                pass
            self.cdp = None
        process = self.process
        self.process = None
        if process is None or not self.owned:
            self.owned = False
            return
        self.owned = False
        try:
            process.terminate()
        except Exception:  # pragma: no cover
            return
        try:
            process.wait(timeout=5)
        except Exception:  # pragma: no cover - 不配合就直接杀
            try:
                process.kill()
            except Exception:
                pass

    def __enter__(self) -> "BrowserLogin":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- 主流程
    def run(
        self,
        *,
        headless: bool = False,
        keep_open: bool = False,
        verify_online: bool = True,
        browser_path: str | Path | None = None,
        true_headless: bool = False,
    ) -> dict[str, Any]:
        """续期一次并落盘。失败也返回字典（``ok=False``），不抛异常给 CLI。

        ``headless=True`` 表示**无人值守**（窗口挪到屏幕外，不是真正的无头）。
        实测 qzone 会拒绝 ``--headless=new``，同一份 cookie headful 能进、无头
        会被打回登录页，所以无人值守必须用 offscreen。
        ``verify_online=True``（默认）会用服务端真实回包确认会话有效——
        **只有它说行，才覆盖 cookie 文件**。
        """
        client = self._client()
        before = dict(client.cookies)
        uin = normalize_uin(client.uin)
        result: dict[str, Any] = {
            "ok": False,
            "uin_masked": mask_uin(uin),
            "cookie_path": str(client.cookie_path),
            "profile": str(self.profile_dir),
            "browser": None,
            "renewed": [],
            "saved": [],
            "full_read": False,
            "online_checked": False,
            "online_ok": None,
            "reused": False,
        }
        if not uin:
            result["error"] = "no_uin"
            result["hint"] = "本地还没有 uin：先跑一次 `fatfish qzone login --qr` 或 --cookie"
            return result

        target_id = ""
        reused = False
        try:
            browser = self._resolve_browser(browser_path)
            result["browser"] = browser.name
            live = self._live_finder(self.profile_dir)
            if live is not None:
                # ``--keep-open`` 留下的实例：直接接管，省一次启动，而且
                # 里面还留着 session cookie（一份额外的活凭据）。
                self.cdp = self._connector(*live)
                cdp = self.cdp
                reused = True
            else:
                self._launch(
                    browser,
                    headless=headless,
                    url=QZONE_HOME_URL.format(uin=uin),
                    true_headless=true_headless,
                )
                cdp = self._connect()
            self._inject(cdp, before)
            target_id, session_id = self._open_home(cdp, uin)
            scan = self._wait_for_session(
                cdp,
                before,
                session_id=session_id,
                uin=uin,
                headless=headless,
                verify_online=verify_online,
            )
        except BrowserLoginError as exc:
            result["error"] = str(exc)
            return result
        except OSError as exc:  # 浏览器起不来（权限/路径）
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        finally:
            if reused and target_id:
                # 长驻实例别攒标签页；窗口本身留着。
                try:
                    self.cdp.call("Target.closeTarget", {"targetId": target_id})  # type: ignore[union-attr]
                except Exception:  # pragma: no cover
                    pass
            if not keep_open:
                self.close()

        result["reused"] = reused

        harvested = scan["harvested"]
        outcome = scan["outcome"]
        result["page"] = outcome
        result["page_token"] = bool(scan.get("page_token"))
        if not harvested:
            # 服务端的 login_required 是终局判定；其它情况按页面落点区分。
            if scan.get("verdict") == "login_required":
                result["error"] = "session_expired"
            else:
                result["error"] = (
                    "session_expired" if outcome == "login_page" else "no_session"
                )
            if scan.get("verdict"):
                result["online_checked"] = bool(verify_online)
                result["online_ok"] = scan["verdict"] == "ok"
                result["probe"] = scan.get("detail") or {}
            result["hint"] = (
                "浏览器里没建立起有效会话。"
                "跑 `fatfish qzone login --browser` 会开一个窗口，在里面正常登录一次即可；"
                "登录成功那一刻工具会自动收回 cookie，之后 refresh 都是免扫码的。"
            )
            return result

        client.cookies.update(harvested)
        result["renewed"] = renewed_fields(before, harvested)
        result["rotated"] = harvest_is_fresh(before, harvested)
        result["harvested"] = sorted(harvested)
        if verify_online:
            verdict = scan.get("verdict")
            detail = scan.get("detail") or {}
            if verdict is None:
                verdict, detail = self._probe(harvested)
            result["online_checked"] = True
            result["online_ok"] = verdict == "ok"
            if verdict == "login_required":
                # 唯一能判定"死会话"的信号：服务端明确说要登录。此时绝不落盘
                # （2026-10-29 实测踩到过：页面回显注入的 cookie，看着"有
                # skey"，服务端回 -3000 need login）。
                result["probe"] = detail
                result["error"] = "session_expired"
                result["hint"] = (
                    "服务端判定未登录，cookie 没有写入。"
                    "跑 `fatfish qzone login --browser` 手动登录一次。"
                )
                return result
            if verdict == "endpoint_error":
                # 端点自己坏了（例如 feeds3/mobile 的 -4003），跟登录态无关：
                # cookie 照存，但把"没验证成功"如实报出来。
                result["probe"] = detail
                result["warning"] = "probe_endpoint_error"
        client.save_cookies()
        result["ok"] = bool(harvested.get("uin") and harvested.get("skey"))
        result["saved"] = sorted(harvested)
        result["full_read"] = client.cookie_status()["full_read"]
        return result

    def _probe(self, cookies: dict[str, str]) -> tuple[str, dict[str, Any]]:
        """用服务端回包判定登录态，返回 ``(verdict, detail)``。

        ``verdict``：
        * ``ok`` —— 拿到了自己的说说列表（0 条也算），登录态确实有效；
        * ``login_required`` —— 服务端明确要求登录（-3000），会话是死的；
        * ``endpoint_error`` —— 端点异常（如 -4003 / 解析失败），与登录态无关。

        只用 ``emotion_cgi_msglist_v6``：它单端点、无降级，登录语义干净。
        ``fetch_feeds`` 的 mobile 兜底会把端点错误伪装成"最终失败"，不能当判据。
        """
        probe_client = QzoneClient.from_config(self.config)
        probe_client.cookies.update(cookies)
        try:
            items = probe_client.fetch_emotions(count=1)
        except QzoneError as exc:
            detail = {
                "ok": False,
                "error": str(exc),
                "code": exc.code,
                "login_required": bool(exc.login_required),
            }
            if exc.login_required or exc.code == -3000:
                return "login_required", detail
            return "endpoint_error", detail
        except Exception as exc:  # pragma: no cover - 兜底
            return "endpoint_error", {"ok": False, "error": type(exc).__name__}
        return "ok", {"ok": True, "items": len(items)}

    # -- 细步骤
    def _inject(self, cdp: CdpClient, cookies: dict[str, str]) -> None:
        """先清空再灌：profile 里可能残留上一轮登录留下的同名 cookie
        （例如 ``.qzone.qq.com`` 上的旧 `p_skey`），它们比我们灌在
        ``.qq.com`` 上的更具体、会优先生效，实测能把页面打回登录页。
        """
        try:
            cdp.call("Storage.clearCookies")
        except BrowserLoginError:  # pragma: no cover - 老版本没有这个命令
            pass
        params = cookie_params(cookies)
        if params:
            cdp.call("Storage.setCookies", {"cookies": params})

    def _open_home(self, cdp: CdpClient, uin: str) -> tuple[str, str]:
        """新开一个标签页并 attach，返回 ``(target_id, session_id)``。"""
        target = cdp.call(
            "Target.createTarget", {"url": QZONE_HOME_URL.format(uin=uin)}
        )
        target_id = str(target.get("targetId") or "")
        if not target_id:
            raise BrowserLoginError("浏览器没有返回页面 target id")
        attached = cdp.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        session_id = str(attached.get("sessionId") or "")
        if not session_id:
            raise BrowserLoginError("浏览器拒绝 attach 到页面 target")
        return target_id, session_id

    def _harvest(self, cdp: CdpClient) -> dict[str, str]:
        payload = cdp.call("Storage.getCookies", {})
        return cookies_from_cdp(payload.get("cookies"))

    def _page_state(self, cdp: CdpClient, session_id: str) -> dict[str, Any]:
        """页面自己的说法：在哪个域、有没有登录框、有没有 g_qzonetoken。"""
        try:
            result = cdp.call(
                "Runtime.evaluate",
                {"expression": LOGIN_STATE_EXPRESSION, "returnByValue": True},
                session_id=session_id,
            )
        except BrowserLoginError:
            return {}
        raw = (result.get("result") or {}).get("value")
        if not isinstance(raw, str):
            return {}
        try:
            decoded = json.loads(raw)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _wait_for_session(
        self,
        cdp: CdpClient,
        before: dict[str, str],
        *,
        session_id: str,
        uin: str,
        headless: bool,
        verify_online: bool = True,
    ) -> dict[str, Any]:
        """???**???????**????

        ?? ``{"harvested", "outcome", "verdict", "detail", "page_token"}``?

        ???????????

        1. ?????? ``ok`` ? ????????
        2. ???? ``g_qzonetoken`` ? ???????????????????
        3. ???? ``login_required`` ????? ? ??????????

        **?????????????**?2026-10-29 ???????? cookie?
        qzone ???????????? ``i.qq.com``?? ``emotion_cgi``
        ??? 200???**cookie ???????????**?????????

        ? 1 ?????????????????????????????
        """
        limit = HEADLESS_WAIT_SECONDS if headless else self.timeout
        deadline = self._clock() + limit
        next_probe_at = self._clock()
        probes = 0
        verdict: str | None = None
        detail: dict[str, Any] = {}
        while True:
            state = self._page_state(cdp, session_id)
            wall = page_is_login_wall(state)
            page_logged_in = page_is_logged_in(state)
            harvested = self._harvest(cdp)
            if harvested.get("uin") and harvested.get("skey"):
                if page_logged_in and not verify_online:
                    return {
                        "harvested": harvested,
                        "outcome": "logged_in",
                        "verdict": None,
                        "detail": {},
                        "page_token": True,
                    }
                if self._clock() >= next_probe_at and probes < MAX_PROBES:
                    probes += 1
                    next_probe_at = self._clock() + PROBE_EVERY_SECONDS
                    candidate = dict(harvested)
                    candidate.setdefault("uin", uin)
                    verdict, detail = self._probe(candidate)
                    if verdict == "ok":
                        return {
                            "harvested": candidate,
                            "outcome": "logged_in",
                            "verdict": verdict,
                            "detail": detail,
                            "page_token": page_logged_in,
                        }
                    if verdict == "endpoint_error" and page_logged_in:
                        # ??????????????????????
                        return {
                            "harvested": candidate,
                            "outcome": "logged_in",
                            "verdict": verdict,
                            "detail": detail,
                            "page_token": True,
                        }
                    if verdict == "login_required" and headless:
                        # ???????????????????????
                        return {
                            "harvested": {},
                            "outcome": "login_page",
                            "verdict": verdict,
                            "detail": detail,
                            "page_token": page_logged_in,
                        }
            if self._clock() >= deadline:
                return {
                    "harvested": {},
                    "outcome": "login_page" if wall else "timeout",
                    "verdict": verdict,
                    "detail": detail,
                    "page_token": page_logged_in,
                }
            self._sleep(POLL_INTERVAL_SECONDS)


def _connect_cdp(port: int, ws_path: str) -> CdpClient:
    return CdpClient(WebSocketConnection("127.0.0.1", port, ws_path))


# --------------------------------------------------------------------- 环境探针
def browser_available(name: str | None = None) -> bool:
    """给 ``qzone status`` 用：本机有没有可用浏览器（不启动）。"""
    try:
        find_browser(name)
    except BrowserLoginError:
        return False
    return True


def profile_size(profile_dir: str | Path) -> int:
    """profile 目录占用字节数（运维面板用；不存在返回 0）。"""
    total = 0
    root = Path(profile_dir)
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:  # pragma: no cover
            continue
    return total


def remove_profile(profile_dir: str | Path) -> bool:
    """删掉浏览器 profile（换号或清场时用；被占用会自动失败返回 False）。"""
    root = Path(profile_dir)
    if not root.exists():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()
