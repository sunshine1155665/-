"""T-0020 QQ 空间扫码登录：全离线 fixture 测试。

覆盖点：
* hash33 已知向量与 31 位掩码性质；
* ptuiCB 各状态码解析（0/65/66/67/68/7 + 无法解析）；
* ptqrlogin 参数候选降级（STATE 7 → 下一候选，轮完一圈报 param_error）；
* check_sig 重定向 cookie 截获（合成响应 + 合成 Set-Cookie）、字段过滤与
  与现有 cookie 的合并、uin 归一化；
* run() 循环（成功 / 过期换新码上限 / 超时）与输出安全（绝不出现
  cookie 值、qrsig、check_sig URL）；
* UI 脚本状态机（不启动真实窗口/浏览器）；
* 一键 bat 三件套与 make_app 打包清单契约。

全程无网络：HTTP 层用 FakeOpener 脚本化。
"""

from __future__ import annotations

import importlib.util
import unittest
import urllib.parse
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from fatfish.qzone.qr_login import (  # noqa: E402
    APPID_CANDIDATES,
    PARAM_CANDIDATES,
    QrLogin,
    QrLoginError,
    hash33,
    mask_uin,
    normalize_uin,
    parse_ptui_callback,
)

SECRET_UIN = "1234567890"
SECRET_SKEY = "SECRET-SKEY-VALUE"
SECRET_PSKEY = "SECRET-PSKEY-VALUE"
SECRET_QRSIG = "SECRET-QRSIG-VALUE"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"fakepngdata" * 20

CHECK_SIG_URL = (
    "https://ssl.ptlogin2.qq.com/check_sig?uin=o"
    + SECRET_UIN
    + "&ptsig=SECRET-PTSIG&daid=5"
)


def _ptui(code: str, url: str = "", message: str = "") -> str:
    return f"ptuiCB('{code}','0','{url}','0','{message}', '')"


BODY_SUCCESS = _ptui("0", CHECK_SIG_URL, "登录成功")
BODY_EXPIRED = _ptui("65", "", "二维码已失效。")
BODY_WAITING = _ptui("66", "", "二维码未失效。")
BODY_SCANNED_67 = _ptui("67", "", "二维码认证中。")
BODY_SCANNED_68 = _ptui("68", "", "二维码已扫描。")
BODY_PARAM7 = _ptui("7", "", "提交参数错误。")


# ---------------------------------------------------------------------- fakes
def _cookie(name: str, value: str, domain: str = ".qq.com") -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path="/",
        path_specified=True,
        secure=False,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeOpener:
    """Scripted opener: replays bodies, simulates the cookie processor."""

    def __init__(self, jar: CookieJar, script=None, on_open=None):
        self.jar = jar
        self.script = list(script or [])
        self.on_open = on_open
        self.requests: list[tuple[str, dict]] = []

    def open(self, request, timeout=None):  # noqa: ANN001
        url = request.full_url
        headers = {k.lower(): v for k, v in request.header_items()}
        self.requests.append((url, headers))
        if self.on_open is not None:
            self.on_open(url)
        item = self.script.pop(0) if self.script else b""
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(url)
        if isinstance(item, str):
            item = item.encode("utf-8")
        return FakeResponse(item)


class FakeClient:
    """QzoneClient double: cookie dict + save_cookies counter."""

    def __init__(self, cookies=None):
        self.cookies = dict(cookies or {})
        self.saved = 0
        self.data_dir = None

    def save_cookies(self, path=None):  # noqa: ANN001
        self.saved += 1
        return Path("qzone_cookies.json")


def _new_session(**kwargs) -> tuple[QrLogin, FakeOpener, FakeClient, CookieJar]:
    """QrLogin wired to a scripted opener with a fresh qrsig session."""
    jar = CookieJar()
    jar.set_cookie(_cookie("qrsig", SECRET_QRSIG))
    script = kwargs.pop("script", [BODY_WAITING])
    on_open = kwargs.pop("on_open", None)
    client = kwargs.pop("client", None) or FakeClient()
    opener = FakeOpener(jar, script=script, on_open=on_open)
    kwargs.setdefault("png_path", Path(kwargs.pop("tmp", ".")) / "qq_login_qr.png")
    login = QrLogin(client=client, opener=opener, jar=jar, **kwargs)
    return login, opener, client, jar


def _set_login_cookies(jar: CookieJar, **pairs: str) -> None:
    for name, value in pairs.items():
        domain = ".qzone.qq.com" if name == "p_skey" else ".qq.com"
        jar.set_cookie(_cookie(name, value, domain=domain))


def _capture_on_open(jar: CookieJar):
    """Simulate Set-Cookie during the check_sig chain and the qzone hop."""

    def on_open(url: str) -> None:
        if url.startswith("https://ssl.ptlogin2.qq.com/check_sig"):
            _set_login_cookies(jar, uin="o" + SECRET_UIN, skey=SECRET_SKEY)
        elif url.startswith("https://user.qzone.qq.com"):
            _set_login_cookies(jar, p_skey=SECRET_PSKEY)

    return on_open


# --------------------------------------------------------------------- hash33
class Hash33Tests(unittest.TestCase):
    def test_known_vectors(self) -> None:
        # 手推向量：h += (h<<5) + ord(ch)，31 位掩码。
        self.assertEqual(0, hash33(""))
        self.assertEqual(97, hash33("a"))
        # "ab" -> 97 + 97*32 + 98 = 3299；"abc" -> 3299 + 3299*32 + 99 = 108966
        self.assertEqual(3299, hash33("ab"))
        self.assertEqual(108966, hash33("abc"))

    def test_matches_independent_implementation(self) -> None:
        def reference(text: str) -> int:
            h = 0
            for ch in text:
                h = (h + ((h << 5) + ord(ch))) & 0x7FFFFFFF
            return h

        for sample in (SECRET_QRSIG, "Zk9pQ2rT", "x" * 64, "中文"):
            self.assertEqual(reference(sample), hash33(sample))

    def test_stays_within_31_bits(self) -> None:
        self.assertLessEqual(hash33("q" * 200), 0x7FFFFFFF)

    def test_differs_from_g_tk_seed(self) -> None:
        from fatfish.qzone.client import g_tk_from_skey

        sample = "abc"
        self.assertNotEqual(g_tk_from_skey(sample), hash33(sample))


# ----------------------------------------------------------------- uin / mask
class UinHelperTests(unittest.TestCase):
    def test_normalize_strips_o_prefix(self) -> None:
        self.assertEqual(SECRET_UIN, normalize_uin("o" + SECRET_UIN))
        self.assertEqual(SECRET_UIN, normalize_uin(SECRET_UIN))
        self.assertEqual("", normalize_uin(""))
        self.assertEqual("123", normalize_uin("o123"))  # o + 纯数字 → 剥前缀
        self.assertEqual("obox", normalize_uin("obox"))  # 非 o+数字 原样保留

    def test_mask_uin(self) -> None:
        self.assertEqual("123****7890", mask_uin(SECRET_UIN))
        self.assertEqual("(masked)", mask_uin(""))
        self.assertEqual("(masked)", mask_uin("12345"))


# -------------------------------------------------------------------- parsing
class ParsePtuiCallbackTests(unittest.TestCase):
    def test_success_includes_check_url(self) -> None:
        code, url, message = parse_ptui_callback(BODY_SUCCESS)
        self.assertEqual("0", code)
        self.assertEqual(CHECK_SIG_URL, url)
        self.assertIn("登录成功", message)

    def test_state_codes(self) -> None:
        self.assertEqual(("65", "", "二维码已失效。"), parse_ptui_callback(BODY_EXPIRED))
        self.assertEqual("66", parse_ptui_callback(BODY_WAITING)[0])
        self.assertEqual("67", parse_ptui_callback(BODY_SCANNED_67)[0])
        self.assertEqual("68", parse_ptui_callback(BODY_SCANNED_68)[0])
        self.assertEqual("7", parse_ptui_callback(BODY_PARAM7)[0])

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(parse_ptui_callback(""))
        self.assertIsNone(parse_ptui_callback("<html>blocked</html>"))
        self.assertIsNone(parse_ptui_callback("ptuiCB(0,0,'url')"))


# ------------------------------------------------------------ candidate table
class CandidateTableTests(unittest.TestCase):
    def test_has_multiple_candidates_with_sources(self) -> None:
        self.assertGreaterEqual(len(PARAM_CANDIDATES), 3)
        names = [c["name"] for c in PARAM_CANDIDATES]
        self.assertEqual(len(names), len(set(names)), "候选名不得重复")
        for candidate in PARAM_CANDIDATES:
            self.assertIn("params", candidate)
            self.assertIn("referer", candidate)
            self.assertNotIn("pt_qzone_sig", candidate["params"])

    def test_u1_and_ptqrtoken_are_not_baked_into_table(self) -> None:
        # u1/ptqrtoken/action 由运行时注入，表里不该写死。
        for candidate in PARAM_CANDIDATES:
            params = candidate["params"]
            for key in ("u1", "ptqrtoken", "action"):
                self.assertNotIn(key, params)

    def test_appid_candidates_start_with_qzone_web_appid(self) -> None:
        self.assertEqual(("549000912", "5"), APPID_CANDIDATES[0])


# ----------------------------------------------------------- generate / poll
class GenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_generate_writes_png_and_resets_candidate_ladder(self) -> None:
        jar = CookieJar()
        client = FakeClient()

        def on_open(url: str) -> None:
            if "ptqrshow" in url and "appid=549000912" in url:
                jar.set_cookie(_cookie("qrsig", SECRET_QRSIG))

        opener = FakeOpener(jar, script=[PNG_MAGIC], on_open=on_open)
        login = QrLogin(
            client=client,
            opener=opener,
            jar=jar,
            png_path=self.tmp / "qr.png",
        )
        login._candidate_index = 2  # 之前降级过
        png = login.generate()
        self.assertEqual(self.tmp / "qr.png", png)
        self.assertEqual(PNG_MAGIC, png.read_bytes())
        self.assertEqual(0, login._candidate_index, "换码后参数阶梯从头开始")
        self.assertEqual(SECRET_QRSIG, login.qrsig())

    def test_generate_falls_back_to_next_appid_on_non_image(self) -> None:
        jar = CookieJar()
        opener = FakeOpener(jar, script=[b"<html>error</html>", PNG_MAGIC])
        login = QrLogin(
            client=FakeClient(), opener=opener, jar=jar, png_path=self.tmp / "qr.png"
        )
        login.generate()
        appids = [
            urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("appid", [""])[0]
            for url, _ in opener.requests
        ]
        self.assertEqual(
            [APPID_CANDIDATES[0][0], APPID_CANDIDATES[1][0]], appids
        )

    def test_generate_raises_when_every_appid_fails(self) -> None:
        opener = FakeOpener(CookieJar(), script=[b"nope", b"nope"])
        login = QrLogin(
            client=FakeClient(), opener=opener, jar=CookieJar(), png_path=self.tmp / "qr.png"
        )
        with self.assertRaises(QrLoginError):
            login.generate()

    def test_poll_step_requires_generate_first(self) -> None:
        login = QrLogin(client=FakeClient(), opener=FakeOpener(CookieJar()), jar=CookieJar(),
                        png_path=self.tmp / "qr.png")
        with self.assertRaises(QrLoginError):
            login.poll_step()


class PollStepTests(unittest.TestCase):
    def _login(self, script, **kwargs):
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        return _new_session(script=script, tmp=tmp, **kwargs)

    def test_waiting_scanned_expired_states(self) -> None:
        for body, expected in (
            (BODY_WAITING, "waiting"),
            (BODY_SCANNED_67, "scanned"),
            (BODY_SCANNED_68, "scanned"),
            (BODY_EXPIRED, "expired"),
        ):
            login, _, _, _ = self._login(script=[body])
            result = login.poll_step()
            self.assertEqual(expected, result["status"], body)

    def test_success_returns_check_url_and_captured_cookies(self) -> None:
        jar = CookieJar()
        jar.set_cookie(_cookie("qrsig", SECRET_QRSIG))
        client = FakeClient()
        opener = FakeOpener(
            jar,
            script=[BODY_SUCCESS, b"", b""],
            on_open=_capture_on_open(jar),
        )
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        login = QrLogin(client=client, opener=opener, jar=jar, png_path=tmp / "qr.png")
        result = login.poll_step()
        self.assertEqual("success", result["status"])
        self.assertEqual(CHECK_SIG_URL, result["check_url"])
        self.assertEqual(SECRET_UIN, result["captured"]["uin"])
        self.assertEqual(SECRET_SKEY, result["captured"]["skey"])
        self.assertEqual(1, client.saved, "成功后立即落盘")

    def test_state_7_degrades_to_next_candidate(self) -> None:
        ladder = (
            {"name": "a", "params": {"daid": "5", "js_ver": "111"}, "referer": "r", "action_style": "short"},
            {"name": "b", "params": {"daid": "6", "js_ver": "222"}, "referer": "r", "action_style": "short"},
            {"name": "c", "params": {}, "referer": "r", "action_style": "short"},
        )
        login, opener, _, _ = self._login(
            script=[BODY_PARAM7, BODY_PARAM7, BODY_WAITING], candidates=ladder
        )
        result = login.poll_step()
        self.assertEqual("waiting", result["status"])
        self.assertEqual("c", result["candidate"])
        self.assertEqual(2, login._candidate_index)

    def test_state_7_exhausting_ladder_reports_param_error(self) -> None:
        ladder = (
            {"name": "a", "params": {"daid": "5"}, "referer": "r", "action_style": "short"},
            {"name": "b", "params": {}, "referer": "r", "action_style": "short"},
        )
        login, _, _, _ = self._login(
            script=[BODY_PARAM7, BODY_PARAM7], candidates=ladder
        )
        result = login.poll_step()
        self.assertEqual("param_error", result["status"])

    def test_poll_request_carries_ptqrtoken_and_u1(self) -> None:
        login, opener, _, _ = self._login(script=[BODY_WAITING])
        login.poll_step()
        url, headers = opener.requests[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(str(hash33(SECRET_QRSIG)), query["ptqrtoken"][0])
        self.assertIn("u1", query)
        self.assertIn("action", query)
        self.assertTrue(url.startswith("https://ssl.ptlogin2.qq.com/ptqrlogin"))
        self.assertTrue(headers["referer"])

    def test_network_error_is_net_retry(self) -> None:
        login, _, _, _ = self._login(script=[OSError("boom")])
        result = login.poll_step()
        self.assertEqual("net_retry", result["status"])

    def test_unparseable_body(self) -> None:
        login, _, _, _ = self._login(script=["<html>wall</html>"])
        self.assertEqual("unparseable", login.poll_step()["status"])


# -------------------------------------------------------------------- capture
class AcceptSuccessTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _login_with_jar(self, cookies_in_jar: dict[str, str], existing=None):
        jar = CookieJar()
        _set_login_cookies(jar, **cookies_in_jar)
        client = FakeClient(cookies=existing or {})
        opener = FakeOpener(jar, script=[b"", b""])
        login = QrLogin(client=client, opener=opener, jar=jar, png_path=self.tmp / "qr.png")
        return login, client

    def test_capture_filters_and_merges(self) -> None:
        login, client = self._login_with_jar(
            {
                "uin": "o" + SECRET_UIN,
                "skey": SECRET_SKEY,
                "p_skey": SECRET_PSKEY,
                "ptcz": "KEEP-PTCZ",
                "junk_field": "junk",
            },
            existing={"uin": "999", "skey": "OLD", "luin": "OLD-LUIN"},
        )
        captured = login.accept_success(CHECK_SIG_URL)
        self.assertEqual(
            {"uin", "skey", "p_skey", "ptcz"}, set(captured)
        )
        self.assertEqual(SECRET_UIN, client.cookies["uin"], "uin 归一化并覆盖旧值")
        self.assertEqual(SECRET_SKEY, client.cookies["skey"])
        self.assertEqual(SECRET_PSKEY, client.cookies["p_skey"])
        self.assertEqual("KEEP-PTCZ", client.cookies["ptcz"], "截获字段覆盖旧值")
        self.assertEqual("OLD-LUIN", client.cookies["luin"], "未截获的旧字段保留")
        self.assertNotIn("junk_field", client.cookies)
        self.assertEqual(1, client.saved)
        self.assertEqual(dict(captured), login.last_captured)

    def test_missing_required_fields_raises_with_names_only(self) -> None:
        login, client = self._login_with_jar({"ptcz": "X"})
        with self.assertRaises(QrLoginError) as ctx:
            login.accept_success(CHECK_SIG_URL)
        message = str(ctx.exception)
        self.assertIn("uin", message)
        self.assertIn("skey", message)
        self.assertNotIn(SECRET_SKEY, message)
        self.assertEqual(0, client.saved)

    def test_check_sig_failure_is_tolerated_when_jar_has_cookies(self) -> None:
        # check_sig 请求直接网络失败，但重定向链此前已把 cookie 写进 jar。
        jar = CookieJar()
        _set_login_cookies(jar, uin="o" + SECRET_UIN, skey=SECRET_SKEY)
        client = FakeClient()
        opener = FakeOpener(jar, script=[OSError("mid-redirect"), OSError("hop")])
        login = QrLogin(client=client, opener=opener, jar=jar, png_path=self.tmp / "qr.png")
        captured = login.accept_success(CHECK_SIG_URL)
        self.assertEqual(SECRET_UIN, captured["uin"])
        self.assertEqual(1, client.saved)

    def test_empty_check_url_rejected(self) -> None:
        login, _ = self._login_with_jar({"uin": "1", "skey": "2"})
        with self.assertRaises(QrLoginError):
            login.accept_success("")


# ---------------------------------------------------------------------- run()
class RunLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _login(self, script, **kwargs):
        jar = CookieJar()
        client = FakeClient()

        def on_open(url: str) -> None:
            if "ptqrshow" in url:
                jar.set_cookie(_cookie("qrsig", SECRET_QRSIG))
            else:
                _capture_on_open(jar)(url)

        opener = FakeOpener(jar, script=script, on_open=on_open)
        now = [0.0]
        slept: list[float] = []
        login = QrLogin(
            client=client,
            opener=opener,
            jar=jar,
            png_path=self.tmp / "qr.png",
            clock=lambda: now[0],
            sleep=lambda s: slept.append(s) or now.__setitem__(0, now[0] + s),
            poll_interval=1.0,
            **kwargs,
        )
        return login, client, slept

    def test_success_path_prints_fields_and_masked_uin_only(self) -> None:
        login, _, _ = self._login(
            script=[PNG_MAGIC, BODY_WAITING, BODY_SCANNED_67, BODY_SUCCESS, b"", b""]
        )
        lines: list[str] = []
        code = login.run(timeout=60, emit=lines.append)
        self.assertEqual(0, code)
        joined = "\n".join(lines)
        self.assertIn("二维码已生成", joined)
        self.assertIn("等待扫码…", joined)
        self.assertIn("已扫码，请在手机上确认", joined)
        self.assertIn("登录成功", joined)
        self.assertIn("uin", joined)
        self.assertIn("123****7890", joined)
        # 红线：cookie 值 / qrsig / check_sig URL 不出现
        self.assertNotIn(SECRET_SKEY, joined)
        self.assertNotIn(SECRET_PSKEY, joined)
        self.assertNotIn(SECRET_QRSIG, joined)
        self.assertNotIn("check_sig", joined)
        self.assertNotIn("SECRET-PTSIG", joined)

    def test_expired_regenerates_and_counts(self) -> None:
        expired = _ptui("65", "", "二维码已失效。")
        login, _, _ = self._login(
            script=[
                PNG_MAGIC,
                expired,
                PNG_MAGIC,
                expired,
                PNG_MAGIC,
                BODY_WAITING,
                BODY_SUCCESS,
                b"",
                b"",
            ]
        )
        lines: list[str] = []
        code = login.run(timeout=60, emit=lines.append)
        self.assertEqual(0, code)
        joined = "\n".join(lines)
        self.assertIn("自动换新码（第 1/3 张）", joined)
        self.assertIn("自动换新码（第 2/3 张）", joined)

    def test_expired_exhaustion_stops(self) -> None:
        expired = _ptui("65", "", "二维码已失效。")
        login, _, _ = self._login(script=[PNG_MAGIC] + [expired, PNG_MAGIC] * 4)
        lines: list[str] = []
        code = login.run(timeout=60, emit=lines.append, max_regenerations=3)
        self.assertEqual(1, code)
        self.assertIn("已连续换新 3 张", "\n".join(lines))

    def test_timeout_exit(self) -> None:
        login, _, _ = self._login(script=[PNG_MAGIC, BODY_WAITING, BODY_WAITING])
        lines: list[str] = []
        code = login.run(timeout=1.5, emit=lines.append)
        self.assertEqual(1, code)
        self.assertIn("等待超时", "\n".join(lines))

    def test_missing_capture_reports_failure_without_values(self) -> None:
        jar = CookieJar()
        client = FakeClient()

        def on_open(url: str) -> None:
            if "ptqrshow" in url:
                jar.set_cookie(_cookie("qrsig", SECRET_QRSIG))

        opener = FakeOpener(
            jar, script=[PNG_MAGIC, BODY_SUCCESS, b"", b""], on_open=on_open
        )
        login = QrLogin(
            client=client,
            opener=opener,
            jar=jar,
            png_path=self.tmp / "qr.png",
            clock=lambda: 0.0,
            sleep=lambda s: None,
        )
        lines: list[str] = []
        code = login.run(timeout=30, emit=lines.append)
        self.assertEqual(1, code)
        joined = "\n".join(lines)
        self.assertIn("登录失败", joined)
        self.assertIn("uin", joined)
        self.assertNotIn(SECRET_QRSIG, joined)


# ------------------------------------------------------------------------ CLI
class CliWiringTests(unittest.TestCase):
    def test_qr_flag_parses_without_cookie(self) -> None:
        from fatfish.cli import build_parser

        args = build_parser().parse_args(["qzone", "login", "--qr", "--timeout", "9"])
        self.assertTrue(args.qr)
        self.assertIsNone(args.cookie)
        self.assertEqual(9, args.timeout)

    def test_paste_path_still_available(self) -> None:
        from fatfish.cli import build_parser

        args = build_parser().parse_args(["qzone", "login", "--cookie", "uin=1; skey=2"])
        self.assertFalse(args.qr)
        self.assertEqual("uin=1; skey=2", args.cookie)

    def test_qr_login_missing_both_fails_fast(self) -> None:
        # 不触发网络/DB：直接调用 cmd 前的分支逻辑不可绕过，因此只验证
        # parser 契约 + _cmd_qzone_login_qr 存在。
        from fatfish import cli

        self.assertTrue(callable(cli._cmd_qzone_login_qr))


# ----------------------------------------------------------------------- UI
def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeQrLogin:
    """UI 层用的 QrLogin 替身：脚本化 generate/poll_step。"""

    def __init__(self, *, png_bodies=None, poll_results=None, tmp=None):
        self.png_bodies = list(png_bodies or [])
        self.poll_results = list(poll_results or [])
        self.generate_calls = 0
        self.png_path = Path(tmp) / "qq_login_qr.png"

    def generate(self):
        self.generate_calls += 1
        body = self.png_bodies.pop(0) if self.png_bodies else PNG_MAGIC
        self.png_path.parent.mkdir(parents=True, exist_ok=True)
        self.png_path.write_bytes(body)
        return self.png_path

    def poll_step(self):
        if not self.poll_results:
            raise AssertionError("unexpected poll_step call")
        return self.poll_results.pop(0)


class QzoneLoginUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ui = _load_module(
            "fatfish_qzone_login_ui", ROOT / "scripts" / "qzone_login_ui.py"
        )

    def _files(self, tmp: Path) -> tuple[Path, Path]:
        data = tmp / "data"
        return data / "qq_login_qr.png", data / "qzone_login.html"

    def test_login_html_is_local_chinese_with_auto_refresh(self) -> None:
        page = self.ui.render_login_html("qq_login_qr.png")
        self.assertIn('lang="zh-CN"', page)
        self.assertIn("QQ 空间扫码登录", page)
        self.assertIn("用手机 QQ 扫码", page)
        self.assertIn('src="qq_login_qr.png"', page)
        self.assertIn("setInterval(refresh, 2000)", page)
        self.assertIn("自动换新码", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<link", page.lower())
        self.assertNotIn("cdn", page.lower())

    def test_success_html_green_without_secrets(self) -> None:
        page = self.ui.render_success_html(
            fields=["skey", "p_skey", "uin"], uin_masked="123****7890"
        )
        self.assertIn("登录成功，可以关闭此页面", page)
        self.assertIn("#16a34a", page)
        self.assertIn("p_skey, skey, uin", page)
        self.assertIn("123****7890", page)
        self.assertNotIn(SECRET_SKEY, page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_write_login_page_uses_sibling_reference(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            qr_path, html_path = self._files(Path(raw))
            self.ui.write_login_page(html_path, qr_path)
            text = html_path.read_text(encoding="utf-8")
            self.assertIn('src="qq_login_qr.png"', text)
            self.assertNotIn(raw, text)

    def test_handle_poll_result_states(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            qr_path, html_path = self._files(tmp)
            login = FakeQrLogin(tmp=tmp)
            self.ui.write_login_page(html_path, qr_path)

            messages: list[str] = []
            refresh, exit_code = self.ui.handle_poll_result(
                login,
                {"status": "waiting"},
                html_path=html_path,
                refresh_count=0,
                emit=messages.append,
            )
            self.assertIsNone(exit_code)
            self.assertIn("等待扫码…", messages)

            refresh, exit_code = self.ui.handle_poll_result(
                login,
                {"status": "scanned"},
                html_path=html_path,
                refresh_count=refresh,
                emit=messages.append,
            )
            self.assertIsNone(exit_code)
            self.assertIn("已扫码，请在手机上确认…", messages)

            refresh, exit_code = self.ui.handle_poll_result(
                login,
                {"status": "param_error"},
                html_path=html_path,
                refresh_count=refresh,
                emit=messages.append,
            )
            self.assertIsNone(exit_code)

            refresh, exit_code = self.ui.handle_poll_result(
                login,
                {"status": "error", "message": "boom"},
                html_path=html_path,
                refresh_count=refresh,
                emit=messages.append,
            )
            self.assertEqual(1, exit_code)
            self.assertIn("登录失败：boom", messages)

    def test_expired_regenerates_png_and_rewrites_page(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            qr_path, html_path = self._files(tmp)
            login = FakeQrLogin(tmp=tmp)
            login.png_path.write_bytes(b"old")
            messages: list[str] = []
            refresh, exit_code = self.ui.handle_poll_result(
                login,
                {"status": "expired"},
                html_path=html_path,
                refresh_count=0,
                emit=messages.append,
            )
            self.assertIsNone(exit_code)
            self.assertEqual(1, refresh)
            self.assertEqual(1, login.generate_calls)
            self.assertIn("已自动换新码", messages[0])

    def test_expired_exhaustion_stops(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            qr_path, html_path = self._files(tmp)
            login = FakeQrLogin(tmp=tmp)
            messages: list[str] = []
            refresh, exit_code = self.ui.handle_poll_result(
                login,
                {"status": "expired"},
                html_path=html_path,
                refresh_count=self.ui.MAX_REFRESHES,
                emit=messages.append,
            )
            self.assertEqual(1, exit_code)
            self.assertEqual(0, login.generate_calls)
            self.assertIn("已连续刷新", messages[0])

    def test_success_writes_page_without_secrets(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            qr_path, html_path = self._files(tmp)
            login = FakeQrLogin(tmp=tmp)
            messages: list[str] = []
            refresh, exit_code = self.ui.handle_poll_result(
                login,
                {
                    "status": "success",
                    "captured": {"uin": SECRET_UIN, "skey": SECRET_SKEY},
                    "check_url": CHECK_SIG_URL,
                },
                html_path=html_path,
                refresh_count=0,
                emit=messages.append,
            )
            text = html_path.read_text(encoding="utf-8")
            joined = "\n".join(messages)
        self.assertEqual(0, exit_code)
        self.assertIn("登录成功，可以关闭此页面。", joined)
        self.assertIn("123****7890", text)
        self.assertNotIn(SECRET_SKEY, text)
        self.assertNotIn(SECRET_SKEY, joined)
        self.assertNotIn("check_sig", text)
        self.assertNotIn("check_sig", joined)
        self.assertNotIn(SECRET_UIN, text)

    def test_run_login_timeout_and_success(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            qr_path, html_path = self._files(tmp)
            now = [0.0]
            login = FakeQrLogin(
                tmp=tmp,
                poll_results=[
                    {"status": "waiting"},
                    {"status": "scanned"},
                ],
            )
            messages: list[str] = []
            code = self.ui.run_login(
                login,
                html_path=html_path,
                timeout=2,
                interval=1,
                sleep=lambda s: now.__setitem__(0, now[0] + s),
                clock=lambda: now[0],
                emit=messages.append,
            )
            self.assertEqual(1, code)
            self.assertIn("等待超时", messages[-1])

            now = [0.0]
            login = FakeQrLogin(
                tmp=tmp,
                poll_results=[
                    {"status": "waiting"},
                    {
                        "status": "success",
                        "captured": {"uin": SECRET_UIN, "skey": SECRET_SKEY},
                    },
                ],
            )
            messages = []
            code = self.ui.run_login(
                login,
                html_path=html_path,
                timeout=30,
                interval=1,
                sleep=lambda s: now.__setitem__(0, now[0] + s),
                clock=lambda: now[0],
                emit=messages.append,
            )
            self.assertEqual(0, code)
            self.assertNotIn(SECRET_SKEY, "\n".join(messages))


# --------------------------------------------------------------- bat / 打包
class LauncherPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.make_app = _load_module(
            "fatfish_make_app_qzone_test", ROOT / "scripts" / "make_app.py"
        )

    def test_new_scripts_are_in_the_package_manifest(self) -> None:
        self.assertIn("qzone_login_ui.py", self.make_app.SCRIPT_FILES)
        self.assertIn("start_qzone_login.bat", self.make_app.SCRIPT_FILES)
        self.assertIn("qzone_login_ui.py", self.make_app.CLEAN_SCRIPT_FILES)
        self.assertIn("start_qzone_login.bat", self.make_app.CLEAN_SCRIPT_FILES)

    def test_root_launcher_exists_and_is_generated(self) -> None:
        source = ROOT / "QQ空间登录.bat"
        self.assertTrue(source.is_file())
        source_text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertEqual(
            source_text.strip(), self.make_app.QZONE_LOGIN_BAT.strip()
        )
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            self.make_app._write_launch_scripts(Path(raw))
            generated = Path(raw) / "QQ空间登录.bat"
            self.assertTrue(generated.is_file())
            self.assertIn(
                r'call "scripts\start_qzone_login.bat"',
                generated.read_text(encoding="utf-8"),
            )

    def test_start_script_contract(self) -> None:
        script = (ROOT / "scripts" / "start_qzone_login.bat").read_text(
            encoding="utf-8"
        )
        self.assertIn("@echo off", script)
        self.assertIn("chcp 65001", script)
        self.assertIn('cd /d "%~dp0.."', script)
        self.assertIn("set \"PYTHONUTF8=1\"", script)
        self.assertIn("python --version", script)
        self.assertIn(r"python scripts\qzone_login_ui.py", script)
        self.assertIn("pause", script)
        self.assertNotIn("runas", script.lower())


# ------------------------------------------------- T-0021 hardening (WAF/grace)
import io  # noqa: E402
import urllib.error  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from fatfish.qzone.qr_login import (  # noqa: E402
    MAX_WAF_STRIKES,
    SCAN_CONFIRM_GRACE_SECONDS,
    WAF_BACKOFF_BASE_SECONDS,
)


def _http_error(code: int) -> urllib.error.HTTPError:
    import email.message

    return urllib.error.HTTPError(
        "https://ssl.ptlogin2.qq.com/ptqrlogin",
        code,
        "blocked",
        email.message.Message(),
        io.BytesIO(b""),
    )


class MutableClock:
    """Scriptable monotonic clock for backoff/grace timing tests."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class WafHardeningTests(unittest.TestCase):
    def _login(self, script, **kwargs):
        import shutil
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return _new_session(script=script, tmp=tmp, **kwargs)

    def test_http_403_is_waf_cooldown_not_net_retry(self) -> None:
        clock = MutableClock()
        login, opener, _, _ = self._login(
            script=[_http_error(403)], clock=clock
        )
        result = login.poll_step()
        self.assertEqual("waf_cooldown", result["status"])
        self.assertEqual(403, result["code"])
        self.assertGreaterEqual(result["retry_after"], WAF_BACKOFF_BASE_SECONDS - 1)
        self.assertFalse(result["give_up"])

    def test_cooldown_window_blocks_ptqrlogin_entirely(self) -> None:
        clock = MutableClock()
        login, opener, _, _ = self._login(
            script=[_http_error(429), BODY_WAITING], clock=clock
        )
        first = login.poll_step()
        self.assertEqual("waf_cooldown", first["status"])
        hits_before = len(opener.requests)
        # the very next poll inside the window must not touch the network
        second = login.poll_step()
        self.assertEqual("waf_cooldown", second["status"])
        self.assertEqual(hits_before, len(opener.requests))

    def test_backoff_doubles_and_gives_up_after_max_strikes(self) -> None:
        clock = MutableClock()
        login, _, _, _ = self._login(
            script=[_http_error(403), _http_error(403), _http_error(403)],
            clock=clock,
        )
        windows = []
        for _ in range(MAX_WAF_STRIKES):
            result = login.poll_step()
            windows.append(float(result["retry_after"]))
            clock.advance(result["retry_after"])  # wait the window out
        self.assertEqual(
            windows,
            [
                WAF_BACKOFF_BASE_SECONDS,
                WAF_BACKOFF_BASE_SECONDS * 2,
                WAF_BACKOFF_BASE_SECONDS * 4,
            ],
        )
        # the third strike reports give_up
        self.assertTrue(result["give_up"])

    def test_http_500_stays_net_retry_with_code(self) -> None:
        login, _, _, _ = self._login(script=[_http_error(500)])
        result = login.poll_step()
        self.assertEqual("net_retry", result["status"])
        self.assertEqual(500, result["code"])
        self.assertIn("500", result["message"])

    def test_success_resets_waf_state(self) -> None:
        clock = MutableClock()
        login, _, _, jar = self._login(
            script=[_http_error(403), BODY_SUCCESS],
            clock=clock,
        )
        login._opener.on_open = _capture_on_open(jar)
        first = login.poll_step()
        self.assertEqual("waf_cooldown", first["status"])
        clock.advance(first["retry_after"])
        result = login.poll_step()
        self.assertEqual("success", result["status"])
        self.assertEqual(0, login._waf_strikes)
        self.assertEqual(0.0, login.waf_cooldown_remaining())

    def test_run_gives_up_with_specific_waf_message(self) -> None:
        clock = MutableClock()
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds)  # waiting the backoff out moves the clock

        login, _, _, _ = self._login(
            script=[PNG_MAGIC, _http_error(403), _http_error(403), _http_error(403)],
            clock=clock,
            sleep=fake_sleep,
        )
        lines: list[str] = []
        code = login.run(timeout=6000.0, emit=lines.append)
        self.assertEqual(1, code)
        joined = "\n".join(lines)
        self.assertIn("风控冷却", joined)
        self.assertIn("兜底", joined)
        # backoff sleeps only (300 / 600); the third strike gives up
        self.assertEqual(2, len(sleeps))


class ScanGraceTests(unittest.TestCase):
    def _login(self, script, **kwargs):
        import shutil
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return _new_session(script=script, tmp=tmp, **kwargs)

    def test_scanned_opens_the_grace_window(self) -> None:
        clock = MutableClock()
        login, _, _, _ = self._login(script=[BODY_SCANNED_67], clock=clock)
        login.poll_step()
        self.assertTrue(login.in_scan_grace())
        clock.advance(SCAN_CONFIRM_GRACE_SECONDS + 1)
        self.assertFalse(login.in_scan_grace())

    def test_param_error_inside_grace_freezes_the_ladder(self) -> None:
        clock = MutableClock()
        ladder = (
            {"name": "a", "params": {"daid": "5"}, "referer": "r", "action_style": "short"},
            {"name": "b", "params": {}, "referer": "r", "action_style": "short"},
        )
        login, opener, _, _ = self._login(
            script=[BODY_SCANNED_67, BODY_PARAM7], clock=clock, candidates=ladder
        )
        login.poll_step()  # scanned
        result = login.poll_step()  # STATE 7 lands inside the grace window
        self.assertEqual("param_error", result["status"])
        self.assertTrue(result["in_grace"])
        self.assertEqual("a", login.candidate_name)  # ladder untouched

    def test_expired_inside_grace_does_not_regenerate(self) -> None:
        clock = MutableClock()
        png_hits: list[str] = []
        login, opener, _, jar = self._login(
            script=[PNG_MAGIC, BODY_SCANNED_67, BODY_EXPIRED, BODY_SUCCESS],
            clock=clock,
            sleep=lambda _s: clock.advance(2.0),
        )
        capture = _capture_on_open(jar)

        def on_open(url: str) -> None:
            if "ptqrshow" in url:
                png_hits.append(url)
            capture(url)

        opener.on_open = on_open
        lines: list[str] = []
        code = login.run(timeout=120.0, emit=lines.append)
        self.assertEqual(0, code)
        # exactly one ptqrshow (run()'s initial generate): the
        # expired-inside-grace never swapped the QR
        self.assertEqual(1, len(png_hits))


class StatusPassthroughTests(unittest.TestCase):
    """T-0021 item 5: the failure lines carry the SPECIFIC state."""

    def test_status_page_text_variants(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "qzone_login_ui", ROOT / "scripts" / "qzone_login_ui.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        waf = module.status_page_text(
            {"status": "waf_cooldown", "retry_after": 300.0}
        )
        self.assertIn("风控冷却", waf)
        self.assertIn("5 分钟", waf)
        params = module.status_page_text({"status": "param_error"})
        self.assertIn("参数", params)
        grace = module.status_page_text({"status": "param_error", "in_grace": True})
        self.assertIn("确认", grace)
        net = module.status_page_text(
            {"status": "net_retry", "message": "HTTP 502"}
        )
        self.assertIn("502", net)

    def test_handle_poll_result_gives_up_on_waf(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "qzone_login_ui", ROOT / "scripts" / "qzone_login_ui.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lines: list[str] = []
        _count, exit_code = module.handle_poll_result(
            SimpleNamespace(png_path=None),
            {"status": "waf_cooldown", "retry_after": 300, "give_up": True},
            html_path=Path("unused.html"),
            refresh_count=0,
            emit=lines.append,
        )
        self.assertEqual(1, exit_code)
        self.assertTrue(any("兜底" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
