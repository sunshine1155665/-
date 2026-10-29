"""免扫码续期（browser_login）全离线测试。

覆盖点：
* 浏览器定位：显式路径 / 环境变量展开 / 名字候选 / 找不到时抛
  ``BrowserLoginError``（不是 FileNotFoundError）；
* 启动参数：``--remote-debugging-port=0``、独立 ``--user-data-dir``、headless；
* ``DevToolsActivePort`` 解析（正常 / 缺失 / 坏格式 / 端口为 0）；
* WebSocket 帧编解码（掩码对称、三种长度、fin/opcode 位）与 ping→pong；
* cookie 搬运：``Storage.setCookies`` 参数形状、``Storage.getCookies`` 的
  域名过滤 + 白名单 + 同名取最具体、续期新鲜度判定；
* 主流程（假进程 / 假 CDP / 假时钟）：成功落盘、无轮换但有会话、无会话、
  本地没 uin、keep-open 不关进程；
* 输出安全：返回字典里绝不出现 cookie 值；
* CLI 契约：``login --browser`` 与 ``refresh`` 的参数与 func 绑定。

全程无网络、不启动浏览器：进程与 CDP 都是假实现。
"""

from __future__ import annotations

import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fatfish.config import load_config
from fatfish.qzone import browser_login as bl
from fatfish.qzone.browser_login import (
    BrowserInfo,
    BrowserLogin,
    BrowserLoginError,
    CdpClient,
    WebSocketConnection,
    browser_candidates,
    build_launch_args,
    cookie_params,
    cookies_from_cdp,
    encode_frame,
    find_browser,
    harvest_is_fresh,
    read_devtools_port,
    renewed_fields,
)

SECRET_UIN = "9999000003"
SECRET_SKEY = "SECRET-SKEY-VALUE"
SECRET_PSKEY = "SECRET-PSKEY-VALUE"
SECRET_NEW_SKEY = "SECRET-ROTATED-SKEY"
ALL_SECRETS = (SECRET_SKEY, SECRET_PSKEY, SECRET_NEW_SKEY)


def make_config(tmp: Path):
    data = tmp / "data"
    path = tmp / "qzone.toml"
    path.write_text(
        "\n".join(
            [
                f'[app]\ndata_dir = "{data.as_posix()}"',
                "[qzone]",
                'cookie_path = "qzone_cookies.json"',
                'owner_uin = ""',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = load_config("low", path)
    config.ensure_dirs()
    return config


def write_cookies(config, mapping: dict[str, str]) -> None:
    path = Path(config.data_dir) / "qzone_cookies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping), encoding="utf-8")


def cdp_cookie(name: str, value: str, domain: str = ".qq.com") -> dict:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": -1,
        "size": len(value),
        "httpOnly": False,
        "secure": True,
        "session": True,
        "sameSite": "Lax",
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self.killed = True


class FakeCdp:
    """按脚本依次返回 ``Storage.getCookies`` 与页面状态。"""

    LOGGED_IN = {
        "host": "user.qzone.qq.com",
        "token": "G-QZONETOKEN",
        "loginFrame": False,
        "ready": "complete",
    }
    LOGIN_WALL = {
        "host": "i.qq.com",
        "token": "",
        "loginFrame": True,
        "ready": "complete",
    }

    def __init__(
        self,
        snapshots: list[list[dict]],
        *,
        states: list[dict] | None = None,
    ) -> None:
        self.snapshots = [list(s) for s in snapshots]
        self.states = [dict(s) for s in (states or [self.LOGGED_IN])]
        self.calls: list[str] = []
        self.session_ids: list[str | None] = []
        self.set_payload: list[dict] = []
        self.cleared = False
        self.closed = False
        self.closed_targets: list[str] = []

    def call(
        self, method: str, params: dict | None = None, *, session_id: str | None = None
    ) -> dict:
        self.calls.append(method)
        self.session_ids.append(session_id)
        if method == "Storage.clearCookies":
            self.cleared = True
            return {}
        if method == "Storage.setCookies":
            self.set_payload = list((params or {}).get("cookies") or [])
            return {}
        if method == "Target.createTarget":
            return {"targetId": "FAKE-TARGET"}
        if method == "Target.attachToTarget":
            return {"sessionId": "FAKE-SESSION"}
        if method == "Target.closeTarget":
            self.closed_targets.append(str((params or {}).get("targetId") or ""))
            return {}
        if method == "Runtime.evaluate":
            state = (
                self.states.pop(0) if len(self.states) > 1 else self.states[0]
            )
            return {"result": {"value": json.dumps(state)}}
        if method == "Storage.getCookies":
            if len(self.snapshots) > 1:
                return {"cookies": self.snapshots.pop(0)}
            return {"cookies": self.snapshots[0] if self.snapshots else []}
        raise AssertionError(f"unexpected method {method}")

    def close(self) -> None:
        self.closed = True


class FakeSocket:
    """够 WebSocketConnection._read_frame 用的最小假套接字。"""

    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = incoming
        self.sent = b""

    def recv(self, size: int) -> bytes:
        chunk, self.incoming = self.incoming[:size], self.incoming[size:]
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        pass


def decode_client_frame(raw: bytes) -> tuple[bool, int, bytes]:
    fin = bool(raw[0] & 0x80)
    opcode = raw[0] & 0x0F
    masked = bool(raw[1] & 0x80)
    length = raw[1] & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack(">H", raw[offset : offset + 2])[0]
        offset += 2
    elif length == 127:
        length = struct.unpack(">Q", raw[offset : offset + 8])[0]
        offset += 8
    key = b""
    if masked:
        key = raw[offset : offset + 4]
        offset += 4
    payload = raw[offset : offset + length]
    if masked:
        payload = bl.apply_mask(payload, key)
    return fin, opcode, payload


# ------------------------------------------------------------------ 浏览器定位
class BrowserDiscoveryTests(unittest.TestCase):
    def test_env_placeholders_expand(self) -> None:
        env = {
            "ProgramFiles": r"C:\PF",
            "ProgramFiles(x86)": r"C:\PF86",
            "LocalAppData": r"C:\Users\me\AppData\Local",
        }
        candidates = browser_candidates(env)
        self.assertTrue(candidates)
        self.assertTrue(all("%" not in str(c.path) for c in candidates))

    def test_program_files_x86_is_not_eaten_by_program_files(self) -> None:
        # 回归：先替换 %ProgramFiles% 会毁掉 %ProgramFiles(x86)%，真实机器上
        # Edge 就装在那条路径下（2026-10-29 实测踩到）。
        env = {"ProgramFiles": r"C:\PF", "ProgramFiles(x86)": r"C:\PF86"}
        paths = {str(c.path) for c in browser_candidates(env)}
        self.assertIn(r"C:\PF86\Microsoft\Edge\Application\msedge.exe", paths)

    def test_finds_edge_when_file_exists(self) -> None:
        env = {"ProgramFiles(x86)": r"C:\PF86", "ProgramFiles": r"C:\PF"}
        target = Path(r"C:\PF86") / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        found = find_browser(env=env, exists=lambda p: p == target)
        self.assertEqual("edge", found.name)
        self.assertEqual(target, found.path)

    def test_prefers_requested_name(self) -> None:
        env = {"ProgramFiles": r"C:\PF", "ProgramFiles(x86)": r"C:\PF86"}
        chrome = Path(r"C:\PF") / "Google" / "Chrome" / "Application" / "chrome.exe"
        found = find_browser("chrome", env=env, exists=lambda p: p == chrome)
        self.assertEqual("chrome", found.name)

    def test_explicit_path_wins(self) -> None:
        found = find_browser(path=r"D:\tools\my-chrome.exe", exists=lambda p: True)
        self.assertEqual("custom", found.name)
        self.assertEqual(Path(r"D:\tools\my-chrome.exe"), found.path)

    def test_missing_explicit_path_raises_browser_error(self) -> None:
        with self.assertRaises(BrowserLoginError):
            find_browser(path=r"D:\nope\chrome.exe", exists=lambda p: False)

    def test_unknown_name_raises(self) -> None:
        with self.assertRaises(BrowserLoginError):
            find_browser("firefox", exists=lambda p: True)

    def test_nothing_installed_raises_with_hint(self) -> None:
        with self.assertRaises(BrowserLoginError) as ctx:
            find_browser(env={}, exists=lambda p: False)
        self.assertIn("--browser-path", str(ctx.exception))

    def test_linux_candidates_are_known(self) -> None:
        # 路径归一化跟着平台走（Windows 上 "/usr/..." 会变成 "\\usr\\..."），
        # 所以按 Path 比较而不是字符串。
        paths = {c.path for c in browser_candidates({})}
        self.assertIn(Path("/usr/bin/chromium"), paths)
        self.assertIn(Path("/usr/bin/google-chrome"), paths)

    def test_finds_linux_chromium(self) -> None:
        target = Path("/usr/bin/chromium")
        found = find_browser(env={}, exists=lambda p: p == target)
        self.assertEqual("chrome", found.name)
        self.assertEqual(target, found.path)


class LinuxRuntimeTests(unittest.TestCase):
    def test_needs_no_sandbox_only_for_linux_root(self) -> None:
        self.assertFalse(bl.needs_no_sandbox(platform="nt", euid=0))
        self.assertTrue(bl.needs_no_sandbox(platform="posix", euid=0))
        self.assertFalse(bl.needs_no_sandbox(platform="posix", euid=1000))

    def test_require_display_passes_on_windows(self) -> None:
        bl.require_display(platform="nt", env={})

    def test_require_display_needs_x_on_linux(self) -> None:
        with self.assertRaises(BrowserLoginError) as ctx:
            bl.require_display(platform="posix", env={})
        self.assertIn("xvfb-run", str(ctx.exception))
        bl.require_display(platform="posix", env={"DISPLAY": ":99"})
        bl.require_display(platform="posix", env={"WAYLAND_DISPLAY": "wayland-0"})


# ------------------------------------------------------------------ 启动参数
class LaunchArgsTests(unittest.TestCase):
    def _browser(self) -> BrowserInfo:
        return BrowserInfo(name="edge", path=Path(r"C:\PF\msedge.exe"))

    def test_uses_ephemeral_debug_port_and_private_profile(self) -> None:
        args = build_launch_args(self._browser(), r"D:\ff-local\data\run\qzone-browser")
        self.assertIn("--remote-debugging-port=0", args)
        self.assertIn(r"--user-data-dir=D:\ff-local\data\run\qzone-browser", args)
        self.assertEqual(r"C:\PF\msedge.exe", args[0])
        self.assertEqual("about:blank", args[-1])

    def test_url_replaces_blank_page(self) -> None:
        args = build_launch_args(
            self._browser(), "profile", url="https://user.qzone.qq.com/123"
        )
        self.assertEqual("https://user.qzone.qq.com/123", args[-1])

    def test_true_headless_flag(self) -> None:
        normal = build_launch_args(self._browser(), "profile")
        headless = build_launch_args(self._browser(), "profile", true_headless=True)
        self.assertNotIn("--headless=new", normal)
        self.assertIn("--headless=new", headless)

    def test_unattended_uses_offscreen_not_headless(self) -> None:
        # 回归（2026-10-29 实测）：qzone 拒绝 --headless=new，但同一份 cookie
        # 在 headful 下能进空间。所以"无人值守"必须是窗口挪到屏幕外。
        args = build_launch_args(self._browser(), "profile", offscreen=True)
        self.assertNotIn("--headless=new", args)
        self.assertIn("--window-position=-32000,-32000", args)
        self.assertIn("--window-size=1280,900", args)


# ------------------------------------------------------------------ DevTools 端口
class DevToolsPortTests(unittest.TestCase):
    def test_live_devtools_rejects_stale_file(self) -> None:
        # 端口文件在、但没人监听 → 不能复用。
        with TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / bl.DEVTOOLS_PORT_FILE).write_text(
                "1\n/devtools/browser/x\n", encoding="utf-8"
            )
            self.assertIsNone(bl.live_devtools(profile))

    def test_live_devtools_none_without_file(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertIsNone(bl.live_devtools(Path(tmp)))

    def test_reads_port_and_path(self) -> None:
        with TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / bl.DEVTOOLS_PORT_FILE).write_text(
                "54321\n/devtools/browser/abc-123\n", encoding="utf-8"
            )
            self.assertEqual(
                (54321, "/devtools/browser/abc-123"),
                read_devtools_port(profile, clock=FakeClock(), sleep=lambda _s: None),
            )

    def test_missing_file_times_out(self) -> None:
        clock = FakeClock()
        with TemporaryDirectory() as tmp:
            found = read_devtools_port(
                Path(tmp), timeout=1.0, clock=clock, sleep=clock.advance
            )
        self.assertIsNone(found)

    def test_malformed_port_is_none(self) -> None:
        with TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / bl.DEVTOOLS_PORT_FILE).write_text("not-a-port\n/x\n", encoding="utf-8")
            self.assertIsNone(
                read_devtools_port(profile, clock=FakeClock(), sleep=lambda _s: None)
            )

    def test_zero_port_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / bl.DEVTOOLS_PORT_FILE).write_text("0\n/devtools/browser/x\n", encoding="utf-8")
            self.assertIsNone(
                read_devtools_port(profile, clock=FakeClock(), sleep=lambda _s: None)
            )


# ------------------------------------------------------------------ WebSocket 帧
class FrameTests(unittest.TestCase):
    def test_short_payload_is_masked_and_round_trips(self) -> None:
        raw = encode_frame(b"hello", mask_key=b"\x01\x02\x03\x04")
        fin, opcode, payload = decode_client_frame(raw)
        self.assertTrue(fin)
        self.assertEqual(0x1, opcode)
        self.assertEqual(b"hello", payload)
        self.assertTrue(raw[1] & 0x80, "client frames must be masked")

    def test_medium_payload_uses_16_bit_length(self) -> None:
        body = b"x" * 300
        raw = encode_frame(body, mask_key=b"\x00\x00\x00\x00")
        self.assertEqual(126, raw[1] & 0x7F)
        self.assertEqual(body, decode_client_frame(raw)[2])

    def test_large_payload_uses_64_bit_length(self) -> None:
        body = b"y" * 70000
        raw = encode_frame(body, mask_key=b"\xff\xff\xff\xff")
        self.assertEqual(127, raw[1] & 0x7F)
        self.assertEqual(body, decode_client_frame(raw)[2])

    def test_mask_requires_four_bytes(self) -> None:
        with self.assertRaises(ValueError):
            encode_frame(b"x", mask_key=b"\x00\x01")

    def test_apply_mask_is_symmetric(self) -> None:
        key = b"\x11\x22\x33\x44"
        data = b"payload-bytes"
        self.assertEqual(data, bl.apply_mask(bl.apply_mask(data, key), key))

    def test_ping_is_answered_with_pong(self) -> None:
        ws = WebSocketConnection.__new__(WebSocketConnection)
        hex_payload = b"hi"
        ws.sock = FakeSocket(encode_frame(hex_payload, opcode=0x9) + encode_frame(b"ok"))
        ws._buffer = b""
        self.assertEqual("ok", ws.recv_message())
        fin, opcode, payload = decode_client_frame(ws.sock.sent)
        self.assertTrue(fin)
        self.assertEqual(0xA, opcode)
        self.assertEqual(hex_payload, payload)

    def test_close_frame_raises(self) -> None:
        ws = WebSocketConnection.__new__(WebSocketConnection)
        ws.sock = FakeSocket(encode_frame(b"", opcode=0x8))
        ws._buffer = b""
        with self.assertRaises(BrowserLoginError):
            ws.recv_message()

    def test_fragmented_message_is_joined(self) -> None:
        ws = WebSocketConnection.__new__(WebSocketConnection)
        first = encode_frame(b'{"id":1,', opcode=0x1)
        first = bytes([first[0] & 0x7F]) + first[1:]  # 清掉 FIN 位
        second = encode_frame(b'"result":{}}')
        ws.sock = FakeSocket(first + second)
        ws._buffer = b""
        self.assertEqual('{"id":1,"result":{}}', ws.recv_message())


class CdpClientTests(unittest.TestCase):
    class _FakeWs:
        def __init__(self, replies: list[str]) -> None:
            self.replies = list(replies)
            self.sent: list[str] = []

        def send_text(self, text: str) -> None:
            self.sent.append(text)

        def recv_message(self) -> str:
            return self.replies.pop(0)

        def close(self) -> None:
            pass

    def test_correlates_by_id_ignoring_events(self) -> None:
        ws = self._FakeWs(
            [
                json.dumps({"method": "Some.event", "params": {}}),
                json.dumps({"id": 1, "result": {"cookies": []}}),
            ]
        )
        cdp = CdpClient(ws)  # type: ignore[arg-type]
        self.assertEqual({"cookies": []}, cdp.call("Storage.getCookies"))
        self.assertEqual(1, json.loads(ws.sent[0])["id"])

    def test_error_reply_raises(self) -> None:
        ws = self._FakeWs([json.dumps({"id": 1, "error": {"message": "boom"}})])
        cdp = CdpClient(ws)  # type: ignore[arg-type]
        with self.assertRaises(BrowserLoginError):
            cdp.call("Storage.getCookies")


# ------------------------------------------------------------------ cookie 搬运
class CookieMappingTests(unittest.TestCase):
    def test_set_payload_shape(self) -> None:
        params = cookie_params({"uin": SECRET_UIN, "skey": SECRET_SKEY})
        by_name = {p["name"]: p for p in params}
        self.assertEqual(".qq.com", by_name["skey"]["domain"])
        self.assertEqual("/", by_name["skey"]["path"])
        self.assertTrue(by_name["skey"]["secure"])
        self.assertEqual(-1, by_name["skey"]["expires"])

    def test_set_payload_drops_unsafe_values(self) -> None:
        params = cookie_params({"skey": "bad\r\nheader: 1", "uin": SECRET_UIN})
        self.assertEqual(["uin"], [p["name"] for p in params])

    def test_get_filters_domains_and_names(self) -> None:
        raw = [
            cdp_cookie("skey", SECRET_SKEY),
            cdp_cookie("evil", "x", domain=".example.com"),
            cdp_cookie("not_whitelisted", "x", domain=".qq.com"),
            cdp_cookie("", "x"),
            cdp_cookie("uin", ""),
        ]
        self.assertEqual({"skey": SECRET_SKEY}, cookies_from_cdp(raw))

    def test_get_prefers_most_specific_domain(self) -> None:
        raw = [
            cdp_cookie("p_skey", "generic", domain=".qq.com"),
            cdp_cookie("p_skey", "specific", domain=".qzone.qq.com"),
        ]
        self.assertEqual({"p_skey": "specific"}, cookies_from_cdp(raw))

    def test_get_tolerates_non_list(self) -> None:
        self.assertEqual({}, cookies_from_cdp(None))
        self.assertEqual({}, cookies_from_cdp({"cookies": []}))

    def test_fresh_requires_new_session(self) -> None:
        before = {"uin": SECRET_UIN, "skey": SECRET_SKEY}
        self.assertFalse(harvest_is_fresh(before, {}))
        self.assertFalse(harvest_is_fresh(before, {"uin": SECRET_UIN}))
        self.assertFalse(harvest_is_fresh(before, dict(before)))
        self.assertTrue(harvest_is_fresh(before, {"uin": SECRET_UIN, "skey": SECRET_NEW_SKEY}))
        self.assertTrue(
            harvest_is_fresh(before, {"uin": SECRET_UIN, "skey": SECRET_SKEY, "qzonetoken": "t"})
        )

    def test_renewed_fields_lists_only_changes(self) -> None:
        before = {"uin": SECRET_UIN, "skey": SECRET_SKEY}
        after = {"uin": SECRET_UIN, "skey": SECRET_NEW_SKEY, "qzonetoken": "t"}
        self.assertEqual(["qzonetoken", "skey"], renewed_fields(before, after))


class PageStateTests(unittest.TestCase):
    def test_token_means_logged_in(self) -> None:
        self.assertTrue(
            bl.page_is_logged_in(
                {"host": "user.qzone.qq.com", "token": "T", "loginFrame": False, "ready": "complete"}
            )
        )

    def test_login_wall_is_not_logged_in(self) -> None:
        wall = {"host": "i.qq.com", "token": "", "loginFrame": True, "ready": "complete"}
        self.assertTrue(bl.page_is_login_wall(wall))
        self.assertFalse(bl.page_is_logged_in(wall))

    def test_ptlogin_host_is_a_wall(self) -> None:
        self.assertTrue(bl.page_is_login_wall({"host": "xui.ptlogin2.qq.com"}))

    def test_unknown_state_is_never_logged_in(self) -> None:
        self.assertFalse(bl.page_is_logged_in({}))
        self.assertFalse(bl.page_is_logged_in({"host": "user.qzone.qq.com"}))
        self.assertFalse(bl.page_is_logged_in({"error": "boom"}))
        self.assertFalse(bl.page_is_logged_in(None))

    def test_qzone_home_without_token_is_not_enough(self) -> None:
        # 2026-10-29 踩坑：死会话下页面同样停在 user.qzone.qq.com，
        # 只有 g_qzonetoken 才作数。
        self.assertFalse(
            bl.page_is_logged_in(
                {
                    "host": "user.qzone.qq.com",
                    "token": "",
                    "loginFrame": False,
                    "ready": "complete",
                }
            )
        )


# ------------------------------------------------------------------ 主流程
class BrowserLoginFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.clock = FakeClock()
        self.processes: list[FakeProcess] = []
        self.launch_args: list[list[str]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _login(self, cdp: FakeCdp, *, timeout: float = 10.0) -> BrowserLogin:
        profile = Path(self.config.data_dir) / "run" / "qzone-browser"

        def fake_popen(args, **kwargs):
            self.launch_args.append(list(args))
            profile.mkdir(parents=True, exist_ok=True)
            (profile / bl.DEVTOOLS_PORT_FILE).write_text(
                "60001\n/devtools/browser/fake\n", encoding="utf-8"
            )
            process = FakeProcess()
            self.processes.append(process)
            return process

        return BrowserLogin(
            self.config,
            browser_name="edge",
            timeout=timeout,
            finder=lambda name, path=None: BrowserInfo("edge", Path(r"C:\PF\msedge.exe")),
            popen=fake_popen,
            connector=lambda port, path: cdp,
            live_finder=lambda profile: None,
            clock=self.clock,
            sleep=self.clock.advance,
        )

    def test_success_saves_rotated_cookies(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [
                [
                    cdp_cookie("uin", SECRET_UIN),
                    cdp_cookie("skey", SECRET_NEW_SKEY),
                    cdp_cookie("p_skey", SECRET_PSKEY, domain=".qzone.qq.com"),
                    cdp_cookie("qzonetoken", "tok"),
                    cdp_cookie("other", "ignored"),
                ]
            ]
        )
        login = self._login(cdp)
        result = login.run(headless=True, verify_online=False)

        self.assertTrue(result["ok"], result)
        self.assertEqual("999****0003", result["uin_masked"])
        self.assertIn("skey", result["renewed"])
        self.assertTrue(result["rotated"])
        self.assertTrue(result["full_read"])
        saved = json.loads(
            (Path(self.config.data_dir) / "qzone_cookies.json").read_text(encoding="utf-8")
        )
        self.assertEqual(SECRET_NEW_SKEY, saved["skey"])
        self.assertEqual(SECRET_PSKEY, saved["p_skey"])
        self.assertNotIn("other", saved)
        self.assertEqual(
            [
                "Storage.clearCookies",
                "Storage.setCookies",
                "Target.createTarget",
                "Target.attachToTarget",
                "Runtime.evaluate",
                "Storage.getCookies",
            ],
            cdp.calls[:6],
        )
        self.assertEqual(["FAKE-SESSION"], cdp.session_ids[4:5])
        self.assertTrue(self.processes[0].terminated, "非 keep-open 必须关掉浏览器")

    def test_result_never_leaks_cookie_values(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]]
        )
        result = self._login(cdp).run(headless=True, verify_online=False)
        joined = json.dumps(result, ensure_ascii=False)
        for secret in ALL_SECRETS:
            self.assertNotIn(secret, joined)
        self.assertNotIn(SECRET_UIN, joined)

    def test_session_without_rotation_still_saves(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        unchanged = [cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_SKEY)]
        cdp = FakeCdp([unchanged, unchanged, unchanged, unchanged])
        result = self._login(cdp, timeout=1.0).run(headless=True, verify_online=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual([], result["renewed"])
        self.assertFalse(result["rotated"])

    def test_probe_ok_wins_even_when_page_is_walled(self) -> None:
        # 2026-10-29 实测：页面判定会抖——同一份有效 cookie 有时停 i.qq.com。
        # 服务端说行就必须算成功，否则无人值守会无谓失败。
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]],
            states=[FakeCdp.LOGIN_WALL],
        )
        login = self._login(cdp, timeout=5.0)
        login._probe = lambda cookies: ("ok", {"ok": True, "items": 0})  # type: ignore[method-assign]
        result = login.run(headless=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["online_ok"])
        self.assertFalse(result["page_token"])

    def test_login_wall_headless_fails_fast(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_SKEY)]],
            states=[FakeCdp.LOGIN_WALL],
        )
        result = self._login(cdp, timeout=90.0).run(headless=True, verify_online=False)
        self.assertFalse(result["ok"])
        self.assertEqual("session_expired", result["error"])
        self.assertEqual("login_page", result["page"])
        self.assertIn("login --browser", result["hint"])
        # headless 不该把 90 秒耗完：一次轮询就返回。
        self.assertLess(self.clock.now, 5.0)

    def test_login_wall_headful_waits_until_timeout(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_SKEY)]],
            states=[FakeCdp.LOGIN_WALL],
        )
        result = self._login(cdp, timeout=6.0).run(headless=False, verify_online=False)
        self.assertFalse(result["ok"])
        self.assertEqual("session_expired", result["error"])
        self.assertGreaterEqual(self.clock.now, 6.0, "有窗口时应当等人登录")

    def test_headful_login_happens_midway(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]],
            states=[FakeCdp.LOGIN_WALL, FakeCdp.LOGIN_WALL, FakeCdp.LOGGED_IN],
        )
        result = self._login(cdp, timeout=30.0).run(headless=False, verify_online=False)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["rotated"])

    def test_probe_failure_does_not_overwrite_cookie_file(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]]
        )
        login = self._login(cdp)
        login._probe = lambda cookies: (  # type: ignore[method-assign]
            "login_required",
            {"ok": False, "error": "qzone api error -3000: 请先登录空间", "login_required": True},
        )
        result = login.run(headless=True)
        self.assertFalse(result["ok"])
        self.assertEqual("session_expired", result["error"])
        self.assertTrue(result["online_checked"])
        self.assertFalse(result["online_ok"])
        self.assertEqual([], result["saved"], "验证失败不报告已保存")
        saved = json.loads(
            (Path(self.config.data_dir) / "qzone_cookies.json").read_text(encoding="utf-8")
        )
        self.assertEqual(SECRET_SKEY, saved["skey"], "验证失败绝不覆盖凭据")
        self.assertNotIn(SECRET_NEW_SKEY, json.dumps(result))

    def test_probe_success_saves(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]]
        )
        login = self._login(cdp)
        login._probe = lambda cookies: ("ok", {"ok": True, "items": 1})  # type: ignore[method-assign]
        result = login.run(headless=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["online_ok"])
        saved = json.loads(
            (Path(self.config.data_dir) / "qzone_cookies.json").read_text(encoding="utf-8")
        )
        self.assertEqual(SECRET_NEW_SKEY, saved["skey"])

    def test_probe_ok_without_page_token_still_wins(self) -> None:
        # 页面没给 g_qzonetoken（qzone 改版/iframe 场景），但服务端认了。
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        no_token = {
            "host": "user.qzone.qq.com",
            "token": "",
            "loginFrame": False,
            "ready": "complete",
        }
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]],
            states=[no_token],
        )
        login = self._login(cdp, timeout=5.0)
        login._probe = lambda cookies: ("ok", {"ok": True, "items": 0})  # type: ignore[method-assign]
        result = login.run(headless=True)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["page_token"])
        self.assertTrue(result["online_ok"])

    def test_probe_endpoint_error_saves_but_warns(self) -> None:
        # feeds/emotion 的 -4003 是端点问题，不该把有效会话判死。
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]]
        )
        login = self._login(cdp)
        login._probe = lambda cookies: (  # type: ignore[method-assign]
            "endpoint_error",
            {"ok": False, "error": "qzone api error -4003: 非法操作", "login_required": False},
        )
        result = login.run(headless=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual("probe_endpoint_error", result["warning"])
        self.assertFalse(result["online_ok"])
        saved = json.loads(
            (Path(self.config.data_dir) / "qzone_cookies.json").read_text(encoding="utf-8")
        )
        self.assertEqual(SECRET_NEW_SKEY, saved["skey"])

    def test_no_session_reports_hint(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp([[cdp_cookie("uin", SECRET_UIN)]], states=[{}])
        result = self._login(cdp, timeout=1.0).run(headless=False, verify_online=False)
        self.assertFalse(result["ok"])
        self.assertEqual("no_session", result["error"])
        self.assertIn("login --browser", result["hint"])

    def test_missing_uin_fails_before_launch(self) -> None:
        write_cookies(self.config, {"skey": SECRET_SKEY})
        cdp = FakeCdp([[]])
        result = self._login(cdp).run(headless=True)
        self.assertFalse(result["ok"])
        self.assertEqual("no_uin", result["error"])
        self.assertEqual([], self.launch_args, "没有 uin 就不该起浏览器")

    def test_keep_open_leaves_process_running(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]]
        )
        result = self._login(cdp).run(headless=True, keep_open=True, verify_online=False)
        self.assertTrue(result["ok"])
        self.assertFalse(self.processes[0].terminated)

    def test_launch_args_carry_profile_and_home_url(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]]
        )
        self._login(cdp).run(headless=True, verify_online=False)
        args = self.launch_args[0]
        self.assertIn("--remote-debugging-port=0", args)
        self.assertEqual(f"https://user.qzone.qq.com/{SECRET_UIN}", args[-1])
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))
        self.assertIn("--window-position=-32000,-32000", args)
        self.assertNotIn("--headless=new", args, "无人值守不能用真无头")

    def test_reuses_live_browser_instead_of_launching(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]]
        )
        login = BrowserLogin(
            self.config,
            finder=lambda name, path=None: BrowserInfo("edge", Path(r"C:\PF\msedge.exe")),
            popen=lambda *a, **k: self.fail("有活实例时不该起新浏览器"),
            connector=lambda port, path: cdp,
            live_finder=lambda profile: (60001, "/devtools/browser/live"),
            clock=self.clock,
            sleep=self.clock.advance,
        )
        result = login.run(headless=True, verify_online=False)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["reused"])
        self.assertEqual([], self.launch_args)
        self.assertEqual(["FAKE-TARGET"], cdp.closed_targets, "复用时要收掉自己的标签页")

    def test_reused_browser_is_not_killed_even_without_keep_open(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        cdp = FakeCdp(
            [[cdp_cookie("uin", SECRET_UIN), cdp_cookie("skey", SECRET_NEW_SKEY)]]
        )
        login = BrowserLogin(
            self.config,
            finder=lambda name, path=None: BrowserInfo("edge", Path(r"C:\PF\msedge.exe")),
            popen=lambda *a, **k: self.fail("不该起新浏览器"),
            connector=lambda port, path: cdp,
            live_finder=lambda profile: (60001, "/devtools/browser/live"),
            clock=self.clock,
            sleep=self.clock.advance,
        )
        login.run(headless=True, verify_online=False)
        self.assertFalse(login.owned)
        self.assertIsNone(login.process)
        # CDP 连接要关（否则进程不退），但浏览器进程本身不在我们手上。
        self.assertTrue(cdp.closed)

    def test_connector_failure_is_reported_not_raised(self) -> None:
        write_cookies(self.config, {"uin": SECRET_UIN, "skey": SECRET_SKEY})
        profile = Path(self.config.data_dir) / "run" / "qzone-browser"

        def fake_popen(args, **kwargs):
            profile.mkdir(parents=True, exist_ok=True)
            (profile / bl.DEVTOOLS_PORT_FILE).write_text("60001\n/x\n", encoding="utf-8")
            return FakeProcess()

        def boom(port, path):
            raise BrowserLoginError("handshake failed")

        login = BrowserLogin(
            self.config,
            finder=lambda name, path=None: BrowserInfo("edge", Path(r"C:\PF\msedge.exe")),
            popen=fake_popen,
            connector=boom,
            live_finder=lambda profile: None,
            clock=self.clock,
            sleep=self.clock.advance,
        )
        result = login.run(headless=True)
        self.assertFalse(result["ok"])
        self.assertIn("handshake failed", result["error"])


class OfflineProbeTests(unittest.TestCase):
    def test_browser_available_does_not_raise(self) -> None:
        # 只断言"不抛"，真实机器上有没有 Edge 不影响测试结果。
        self.assertIn(bl.browser_available(), (True, False))

    def test_profile_size_of_missing_dir_is_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(0, bl.profile_size(Path(tmp) / "nope"))


# ------------------------------------------------------------------ CLI 契约
class CliWiringTests(unittest.TestCase):
    def test_login_browser_flag_parses(self) -> None:
        from fatfish.cli import build_parser

        args = build_parser().parse_args(
            ["qzone", "login", "--browser", "--browser-name", "chrome", "--headless"]
        )
        self.assertTrue(args.browser)
        self.assertEqual("chrome", args.browser_name)
        self.assertTrue(args.headless)
        self.assertFalse(args.no_verify)

    def test_refresh_subcommand_parses(self) -> None:
        from fatfish import cli

        args = cli.build_parser().parse_args(["qzone", "refresh", "--no-verify"])
        self.assertIs(cli.cmd_qzone_refresh, args.func)
        self.assertTrue(args.no_verify)
        self.assertFalse(args.headful)
        self.assertEqual(120, args.timeout)

    def test_login_without_any_source_still_reports(self) -> None:
        # 保底：--browser 不是默认值，老的 --cookie / --qr 行为不变。
        from fatfish.cli import build_parser

        args = build_parser().parse_args(["qzone", "login", "--cookie", "uin=1; skey=2"])
        self.assertFalse(args.browser)

    def test_helpers_exist(self) -> None:
        from fatfish import cli

        self.assertTrue(callable(cli._cmd_qzone_browser))
        self.assertTrue(callable(cli._qzone_browser_status))


if __name__ == "__main__":
    unittest.main()
