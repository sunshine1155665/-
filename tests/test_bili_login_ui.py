from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET = "SECRET-SESSDATA-VALUE"

try:
    import qrcode  # noqa: F401
    from PIL import Image  # noqa: F401

    HAVE_QRCODE = True
except Exception:  # pragma: no cover - depends on the optional extra
    HAVE_QRCODE = False


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


login_ui = _load_module("fatfish_bili_login_ui", ROOT / "scripts" / "bili_login_ui.py")


class FakeClient:
    """Client double: queued QR payloads and poll results, no network."""

    def __init__(self, *, qr_payloads=None, poll_results=None):
        self.qr_payloads = list(qr_payloads or [])
        self.poll_results = list(poll_results or [])
        self.qr_calls = 0
        self.polled_keys: list[str] = []

    def qr_generate(self):
        self.qr_calls += 1
        if not self.qr_payloads:
            raise AssertionError("unexpected qr_generate call")
        return self.qr_payloads.pop(0)

    def qr_poll(self, qrcode_key: str):
        self.polled_keys.append(qrcode_key)
        if not self.poll_results:
            raise AssertionError("unexpected qr_poll call")
        return self.poll_results.pop(0)


def _files(tmp: Path) -> tuple[Path, Path]:
    data = tmp / "data"
    return data / "bilibili_login.png", data / "bilibili_login.html"


class QrAndHtmlTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_QRCODE, "qrcode/Pillow not installed")
    def test_write_qr_png_creates_a_real_png(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            qr_path, _ = _files(Path(raw))
            login_ui.write_qr_png("https://example.com/qr-token", qr_path)
            self.assertTrue(qr_path.is_file())
            payload = qr_path.read_bytes()
            self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(payload), 400)

    def test_login_html_is_local_and_chinese(self) -> None:
        page = login_ui.render_login_html("bilibili_login.png")
        self.assertIn('lang="zh-CN"', page)
        self.assertIn("用 B 站手机客户端扫码", page)
        self.assertIn('src="bilibili_login.png"', page)
        self.assertIn("setInterval(refresh, 2000)", page)
        self.assertIn('?t=" + Date.now()', page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<link", page.lower())
        self.assertNotIn("cdn", page.lower())

    def test_success_html_is_green_and_local(self) -> None:
        page = login_ui.render_success_html()
        self.assertIn("登录成功，可以关闭此页面", page)
        self.assertIn("#16a34a", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<link", page.lower())

    def test_write_login_page_uses_a_sibling_png_reference(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            qr_path, html_path = _files(Path(raw))
            login_ui.write_login_page(html_path, qr_path)
            text = html_path.read_text(encoding="utf-8")
            self.assertIn('src="bilibili_login.png"', text)
            self.assertNotIn(str(Path(raw)), text)


class PollHandlingTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_QRCODE, "qrcode/Pillow not installed")
    def test_expired_regenerates_png_html_and_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            qr_path, html_path = _files(Path(raw))
            login_ui.write_qr_png("https://example.com/old", qr_path)
            login_ui.write_login_page(html_path, qr_path)
            old_digest = hashlib.sha256(qr_path.read_bytes()).hexdigest()

            client = FakeClient(
                qr_payloads=[
                    {"url": "https://example.com/new", "qrcode_key": "key-2"}
                ]
            )
            messages: list[str] = []
            refresh_count, key, exit_code = login_ui.handle_poll_result(
                client,
                {"status": "expired", "message": "expired"},
                qrcode_key="key-1",
                qr_path=qr_path,
                html_path=html_path,
                refresh_count=0,
                emit=messages.append,
            )

            self.assertEqual(1, refresh_count)
            self.assertEqual("key-2", key)
            self.assertIsNone(exit_code)
            self.assertEqual(1, client.qr_calls)
            self.assertEqual(1, len(messages))
            self.assertIn("二维码已刷新", messages)
            new_digest = hashlib.sha256(qr_path.read_bytes()).hexdigest()
            self.assertNotEqual(old_digest, new_digest)
            text = html_path.read_text(encoding="utf-8")
            self.assertIn('src="bilibili_login.png"', text)
            self.assertIn("用 B 站手机客户端扫码", text)

    def test_refresh_limit_exits_without_generating(self) -> None:
        client = FakeClient()
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            qr_path, html_path = _files(Path(raw))
            refresh_count, key, exit_code = login_ui.handle_poll_result(
                client,
                {"status": "expired", "message": "expired"},
                qrcode_key="key-1",
                qr_path=qr_path,
                html_path=html_path,
                refresh_count=login_ui.MAX_REFRESHES,
                emit=messages.append,
            )
        self.assertEqual(login_ui.MAX_REFRESHES, refresh_count)
        self.assertEqual("key-1", key)
        self.assertEqual(1, exit_code)
        self.assertEqual(0, client.qr_calls)
        self.assertIn("已连续刷新", messages[0])

    def test_success_rewrites_green_html_without_secrets(self) -> None:
        client = FakeClient()
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            qr_path, html_path = _files(Path(raw))
            login_ui.write_login_page(html_path, qr_path)
            refresh_count, key, exit_code = login_ui.handle_poll_result(
                client,
                {
                    "status": "success",
                    "message": "login success",
                    "cookies": {"SESSDATA": SECRET, "bili_jct": SECRET},
                },
                qrcode_key="key-1",
                qr_path=qr_path,
                html_path=html_path,
                refresh_count=0,
                emit=messages.append,
            )
            text = html_path.read_text(encoding="utf-8")

        self.assertEqual(0, refresh_count)
        self.assertEqual("key-1", key)
        self.assertEqual(0, exit_code)
        self.assertIn("登录成功，可以关闭此页面", text)
        self.assertIn("#16a34a", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("SESSDATA", text)
        self.assertNotIn("<script", text.lower())
        joined = "\n".join(messages)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn("SESSDATA", joined)

    def test_poll_output_never_contains_cookies_or_headers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            qr_path, html_path = _files(Path(raw))
            client = FakeClient()
            messages: list[str] = []

            waiting = login_ui.handle_poll_result(
                client,
                {
                    "status": "waiting",
                    "cookies": {"SESSDATA": SECRET},
                    "headers": {"Cookie": f"SESSDATA={SECRET}"},
                },
                qrcode_key="key-1",
                qr_path=qr_path,
                html_path=html_path,
                refresh_count=0,
                emit=messages.append,
            )
            scanned = login_ui.handle_poll_result(
                client,
                {
                    "status": "scanned",
                    "cookies": {"SESSDATA": SECRET},
                    "headers": {"Cookie": f"SESSDATA={SECRET}"},
                },
                qrcode_key="key-1",
                qr_path=qr_path,
                html_path=html_path,
                refresh_count=0,
                emit=messages.append,
            )
            error = login_ui.handle_poll_result(
                client,
                {
                    "status": "error",
                    "message": "boom",
                    "cookies": {"SESSDATA": SECRET},
                    "headers": {"Cookie": f"SESSDATA={SECRET}"},
                },
                qrcode_key="key-1",
                qr_path=qr_path,
                html_path=html_path,
                refresh_count=0,
                emit=messages.append,
            )

        self.assertIsNone(waiting[2])
        self.assertIsNone(scanned[2])
        self.assertEqual(1, error[2])
        joined = "\n".join(messages)
        self.assertIn("等待扫码…", joined)
        self.assertIn("已扫码，请在手机上确认…", joined)
        self.assertIn("登录失败：boom", joined)
        self.assertNotIn(SECRET, joined)
        self.assertNotIn("SESSDATA", joined)
        self.assertNotIn("Cookie", joined)


class RunLoginTests(unittest.TestCase):
    def test_run_login_times_out_without_scanning(self) -> None:
        now = [0.0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        client = FakeClient(
            poll_results=[
                {"status": "waiting", "cookies": {"SESSDATA": SECRET}},
                {"status": "waiting", "cookies": {"SESSDATA": SECRET}},
            ]
        )
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            qr_path, html_path = _files(Path(raw))
            code = login_ui.run_login(
                client,
                "key-1",
                qr_path=qr_path,
                html_path=html_path,
                timeout=4,
                interval=2,
                sleep=sleep,
                clock=clock,
                emit=messages.append,
            )

        self.assertEqual(1, code)
        self.assertEqual(["key-1", "key-1"], client.polled_keys)
        self.assertEqual(4.0, now[0])
        self.assertIn("等待超时", messages[-1])
        self.assertNotIn(SECRET, "\n".join(messages))

    def test_run_login_returns_success_code(self) -> None:
        client = FakeClient(
            poll_results=[
                {"status": "scanned", "cookies": {"SESSDATA": SECRET}},
                {"status": "success", "cookies": {"SESSDATA": SECRET}},
            ]
        )
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            qr_path, html_path = _files(Path(raw))
            now = [0.0]
            code = login_ui.run_login(
                client,
                "key-1",
                qr_path=qr_path,
                html_path=html_path,
                timeout=30,
                interval=2,
                sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                clock=lambda: now[0],
                emit=messages.append,
            )
            text = html_path.read_text(encoding="utf-8")

        self.assertEqual(0, code)
        self.assertIn("登录成功，可以关闭此页面", text)
        self.assertNotIn(SECRET, "\n".join(messages))


class LauncherPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.make_app = _load_module(
            "fatfish_make_app_login_test", ROOT / "scripts" / "make_app.py"
        )

    def test_new_scripts_are_in_the_package_manifest(self) -> None:
        self.assertIn("bili_login_ui.py", self.make_app.SCRIPT_FILES)
        self.assertIn("start_bili_login.bat", self.make_app.SCRIPT_FILES)

    def test_vendor_stays_excluded(self) -> None:
        self.assertNotIn("vendor", self.make_app.PACKAGE_DIRS)
        self.assertFalse(
            any("vendor" in name for name in self.make_app.SCRIPT_FILES)
        )
        self.assertFalse(any("vendor" in name for name in self.make_app.ROOT_FILES))

    def test_root_launcher_exists_and_is_generated(self) -> None:
        source = ROOT / "B站登录.bat"
        self.assertTrue(source.is_file())
        source_text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("@echo off", source_text)
        self.assertIn('cd /d "%~dp0"', source_text)
        self.assertIn(r'call "scripts\start_bili_login.bat"', source_text)
        self.assertEqual(
            source_text.strip(), self.make_app.BILI_LOGIN_BAT.strip()
        )

        with tempfile.TemporaryDirectory() as raw:
            self.make_app._write_launch_scripts(Path(raw))
            generated = Path(raw) / "B站登录.bat"
            self.assertTrue(generated.is_file())
            generated_text = generated.read_text(encoding="utf-8")
            self.assertIn(r'call "scripts\start_bili_login.bat"', generated_text)

    def test_start_script_contract(self) -> None:
        script = (ROOT / "scripts" / "start_bili_login.bat").read_text(
            encoding="utf-8"
        )
        self.assertIn("@echo off", script)
        self.assertIn("chcp 65001", script)
        self.assertIn('cd /d "%~dp0.."', script)
        self.assertIn("python --version", script)
        self.assertIn(r"python scripts\bili_login_ui.py", script)
        self.assertIn("pause", script)
        self.assertNotIn("runas", script.lower())


if __name__ == "__main__":
    unittest.main()
