from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


login_ui = _load_module("fatfish_qq_login_ui", ROOT / "scripts" / "qq_login_ui.py")
NAPCAT_INSTALLED = login_ui.DEFAULT_NAPCAT_DIR.is_dir()
WINDOWS = os.name == "nt"


def _fresh_qr(directory: Path) -> Path:
    cache = directory / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    qr = cache / "qrcode.png"
    qr.write_bytes(b"\x89PNG\r\n\x1a\nfake-qr")
    return qr


class PathResolutionTests(unittest.TestCase):
    @unittest.skipUnless(NAPCAT_INSTALLED, "NapCat OneKey is not installed")
    def test_real_repo_qr_path_exists(self) -> None:
        qr = login_ui.resolve_qr_path()
        self.assertEqual(
            login_ui.DEFAULT_NAPCAT_DIR / login_ui.QR_RELATIVE_PATH,
            qr,
        )
        self.assertEqual("qrcode.png", qr.name)
        self.assertTrue(qr.is_file(), f"QR file missing: {qr}")

    def test_qr_file_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            chosen = Path(raw) / "picked.png"
            self.assertEqual(chosen, login_ui.resolve_qr_path(qr_file=chosen))
            self.assertEqual(
                chosen,
                login_ui.resolve_qr_path(login_ui.DEFAULT_NAPCAT_DIR, chosen),
            )

    def test_relative_qr_file_resolves_against_repo(self) -> None:
        self.assertEqual(
            login_ui.ROOT / "data" / "some_qr.png",
            login_ui.resolve_qr_path(qr_file="data/some_qr.png"),
        )

    def test_other_version_directory_is_a_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = (
                root
                / "versions"
                / "9.9.99-1"
                / "resources"
                / "app"
                / "napcat"
                / "cache"
            )
            cache.mkdir(parents=True)
            qr = cache / "qrcode.png"
            qr.write_bytes(b"qr")
            self.assertEqual(qr, login_ui.resolve_qr_path(root))

    def test_stale_and_missing_detection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            qr = Path(raw) / "qrcode.png"
            now = time.time()
            self.assertTrue(login_ui.qr_is_stale(qr, now=now))

            qr.write_bytes(b"")
            self.assertTrue(login_ui.qr_is_stale(qr, now=now))

            qr.write_bytes(b"qr")
            os.utime(qr, (now - 301, now - 301))
            self.assertTrue(login_ui.qr_is_stale(qr, now=now))

            os.utime(qr, (now - 10, now - 10))
            self.assertFalse(login_ui.qr_is_stale(qr, now=now))


class HtmlTests(unittest.TestCase):
    def test_login_html_is_local_chinese_and_refreshing(self) -> None:
        page = login_ui.render_login_html(
            "qrcode.png", generated_at=1_700_000_000
        )
        self.assertIn('lang="zh-CN"', page)
        self.assertIn(login_ui.INSTRUCTION, page)
        self.assertIn("<title>用手机 QQ 扫码登录机器人账号</title>", page)
        self.assertIn('id="qr"', page)
        self.assertIn('src="qrcode.png?t=1700000000"', page)
        self.assertIn('data-src="qrcode.png"', page)
        self.assertIn("setInterval", page)
        self.assertIn("window.setInterval(refresh, 2000);", page)
        self.assertIn('base + "?t=" + Date.now()', page)
        self.assertIn("二维码每 2 秒自动刷新", page)
        self.assertIn("本页面可以一直开着", page)
        self.assertIn("NapCat 控制台出现登录成功", page)
        self.assertIn("python -m fatfish qq run", page)

        refs = re.findall(r'(?<!data-)src="([^"]*)"', page)
        self.assertEqual(1, len(refs), refs)
        self.assertTrue(refs[0].startswith("qrcode.png?t="), refs)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<link", page.lower())
        self.assertNotIn("cdn", page.lower())
        self.assertNotIn("<script src", page.lower())

    def test_write_login_page_uses_a_local_sibling_png(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            qr = _fresh_qr(tmp)
            original = qr.read_bytes()
            html_path = qr.parent / login_ui.HTML_NAME

            login_ui.write_login_page(
                html_path, qr, generated_at=1_700_000_000
            )

            text = html_path.read_text(encoding="utf-8")
            self.assertEqual(login_ui.HTML_NAME, html_path.name)
            self.assertIn('src="qrcode.png?t=1700000000"', text)
            self.assertIn('data-src="qrcode.png"', text)
            self.assertNotIn(str(tmp), text)
            self.assertEqual(original, qr.read_bytes())
            self.assertFalse(
                html_path.with_name(html_path.name + ".tmp").exists()
            )


class WaitForQrTests(unittest.TestCase):
    def test_returns_immediately_for_an_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            qr = _fresh_qr(Path(raw))
            slept: list[float] = []
            self.assertTrue(
                login_ui.wait_for_qr(
                    qr,
                    timeout=0,
                    sleep=slept.append,
                    clock=lambda: 0.0,
                )
            )
            self.assertEqual([], slept)

    def test_waits_until_the_file_appears(self) -> None:
        now = [0.0]
        created = [False]
        with tempfile.TemporaryDirectory() as raw:
            qr = Path(raw) / "cache" / "qrcode.png"

            def clock() -> float:
                return now[0]

            def sleep(seconds: float) -> None:
                now[0] += seconds
                if not created[0]:
                    qr.parent.mkdir(parents=True)
                    qr.write_bytes(b"qr")
                    created[0] = True

            self.assertTrue(
                login_ui.wait_for_qr(
                    qr, timeout=3, interval=1, sleep=sleep, clock=clock
                )
            )
            self.assertEqual(1.0, now[0])

    def test_times_out_without_creating_anything(self) -> None:
        now = [0.0]
        with tempfile.TemporaryDirectory() as raw:
            qr = Path(raw) / "cache" / "qrcode.png"
            self.assertFalse(
                login_ui.wait_for_qr(
                    qr,
                    timeout=2,
                    interval=1,
                    sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                    clock=lambda: now[0],
                )
            )
            self.assertEqual(2.0, now[0])
            self.assertFalse(qr.exists())

    def test_newer_than_ignores_an_old_file_until_it_is_rewritten(self) -> None:
        now = [0.0]
        with tempfile.TemporaryDirectory() as raw:
            qr = _fresh_qr(Path(raw))
            os.utime(qr, (100, 100))

            def sleep(seconds: float) -> None:
                now[0] += seconds
                os.utime(qr, (300, 300))

            self.assertTrue(
                login_ui.wait_for_qr(
                    qr,
                    timeout=2,
                    interval=1,
                    newer_than=250,
                    sleep=sleep,
                    clock=lambda: now[0],
                )
            )
            self.assertEqual(1.0, now[0])


class ProcessDetectionTests(unittest.TestCase):
    @unittest.skipUnless(WINDOWS, "tasklist and CREATE_NEW_CONSOLE are Windows-only")
    def test_tasklist_output_detects_qq_and_napcat(self) -> None:
        qq = SimpleNamespace(
            returncode=0,
            stdout='"QQ.exe","1234","Console","1","12,000 K"\r\n',
        )
        napcat = SimpleNamespace(
            returncode=0,
            stdout='"NapCatWinBootMain.exe","22","Console","1","1,000 K"\r\n',
        )
        other = SimpleNamespace(
            returncode=0,
            stdout='"chrome.exe","33","Console","1","1,000 K"\r\n',
        )
        failed = SimpleNamespace(returncode=1, stdout="")

        self.assertTrue(login_ui.is_qq_running(runner=lambda *a, **k: qq))
        self.assertTrue(login_ui.is_qq_running(runner=lambda *a, **k: napcat))
        self.assertFalse(login_ui.is_qq_running(runner=lambda *a, **k: other))
        self.assertFalse(login_ui.is_qq_running(runner=lambda *a, **k: failed))

    @unittest.skipUnless(WINDOWS, "CREATE_NEW_CONSOLE is Windows-only")
    def test_launch_uses_a_new_console_and_repo_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            launcher = Path(raw) / "start_napcat.bat"
            launcher.write_text("@echo off\n", encoding="utf-8")
            calls: list[tuple[list[str], dict[str, object]]] = []

            def fake_popen(args, **kwargs):
                calls.append((list(args), dict(kwargs)))
                return object()

            login_ui.launch_napcat(launcher, popen=fake_popen)

            self.assertEqual(1, len(calls))
            args, kwargs = calls[0]
            self.assertEqual("cmd.exe", args[0])
            self.assertEqual("/c", args[1])
            self.assertEqual(str(launcher), args[2])
            self.assertEqual(str(login_ui.ROOT), kwargs["cwd"])
            flags = int(kwargs["creationflags"])
            self.assertTrue(flags & login_ui.subprocess.CREATE_NEW_CONSOLE)

    @unittest.skipUnless(WINDOWS, "the launcher is Windows-only")
    def test_launch_reports_a_missing_batch_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "missing.bat"

            def no_spawn(*args, **kwargs):
                raise AssertionError("Popen must not be called")

            with self.assertRaises(login_ui.QqLoginError) as caught:
                login_ui.launch_napcat(missing, popen=no_spawn)
            self.assertIn("找不到 NapCat 启动脚本", str(caught.exception))


class PrepareQrTests(unittest.TestCase):
    def test_fresh_qr_skips_process_probe_and_launch(self) -> None:
        def boom(*args, **kwargs):
            raise AssertionError("must not be called for a fresh QR")

        with tempfile.TemporaryDirectory() as raw:
            qr = _fresh_qr(Path(raw))
            preparation = login_ui.prepare_qr(
                qr,
                is_running=boom,
                start=boom,
                wait=boom,
                emit=lambda _message: None,
            )

        self.assertTrue(preparation.reused)
        self.assertFalse(preparation.started)
        self.assertFalse(preparation.already_running)

    def test_stale_qr_with_qq_running_never_starts_a_second_instance(self) -> None:
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            qr = _fresh_qr(Path(raw))
            os.utime(qr, (time.time() - 3600, time.time() - 3600))

            def no_start(*args, **kwargs):
                raise AssertionError("must not start NapCat when QQ is running")

            preparation = login_ui.prepare_qr(
                qr,
                is_running=lambda: True,
                start=no_start,
                wait=lambda *args, **kwargs: True,
                emit=messages.append,
            )

        self.assertTrue(preparation.already_running)
        self.assertFalse(preparation.started)
        self.assertFalse(preparation.reused)
        self.assertTrue(any("不会启动第二个实例" in item for item in messages))

    def test_stale_qr_without_qq_starts_the_launcher(self) -> None:
        calls: list[object] = []
        launcher = Path("C:/fake/start_napcat.bat")
        with tempfile.TemporaryDirectory() as raw:
            qr = _fresh_qr(Path(raw))
            os.utime(qr, (time.time() - 3600, time.time() - 3600))

            def start(value):
                calls.append(value)

            def wait(*args, **kwargs):
                qr.write_bytes(b"new-qr")
                return True

            preparation = login_ui.prepare_qr(
                qr,
                launcher=launcher,
                is_running=lambda: False,
                start=start,
                wait=wait,
                emit=lambda _message: None,
            )

        self.assertEqual([launcher], calls)
        self.assertTrue(preparation.started)
        self.assertFalse(preparation.already_running)

    def test_missing_qr_starts_and_times_out_clearly(self) -> None:
        calls: list[object] = []
        with tempfile.TemporaryDirectory() as raw:
            qr = Path(raw) / "cache" / "qrcode.png"

            with self.assertRaises(login_ui.QrMissingError) as caught:
                login_ui.prepare_qr(
                    qr,
                    is_running=lambda: False,
                    start=lambda value: calls.append(value),
                    wait=lambda *args, **kwargs: False,
                    emit=lambda _message: None,
                )

        self.assertEqual(1, len(calls))
        self.assertIn("等待二维码超时", str(caught.exception))

    def test_module_level_detection_mock_prevents_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            qr = _fresh_qr(Path(raw))
            os.utime(qr, (time.time() - 3600, time.time() - 3600))

            with mock.patch.object(
                login_ui, "is_qq_running", return_value=True
            ), mock.patch.object(
                login_ui,
                "launch_napcat",
                side_effect=AssertionError("must not spawn"),
            ):
                preparation = login_ui.prepare_qr(
                    qr,
                    wait=lambda *args, **kwargs: True,
                    emit=lambda _message: None,
                )

        self.assertTrue(preparation.already_running)
        self.assertFalse(preparation.started)


class MainTests(unittest.TestCase):
    def test_main_reuses_qr_writes_page_and_exits_without_browser(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as raw:
            qr = _fresh_qr(Path(raw))
            (qr.parent / "webui.json").write_text(
                "TOKEN-SECRET-VALUE", encoding="utf-8"
            )

            with mock.patch.object(
                login_ui,
                "is_qq_running",
                side_effect=AssertionError("must not probe processes"),
            ), mock.patch.object(
                login_ui.webbrowser,
                "open",
                side_effect=AssertionError("browser is disabled"),
            ):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(
                    stderr
                ):
                    code = login_ui.main(
                        [
                            "--qr-file",
                            str(qr),
                            "--no-browser",
                            "--timeout",
                            "0",
                        ]
                    )

            html_path = qr.parent / login_ui.HTML_NAME
            html_exists = html_path.is_file()
            html_text = html_path.read_text(encoding="utf-8")
            output = stdout.getvalue()

        self.assertEqual(0, code)
        self.assertEqual("", stderr.getvalue())
        self.assertTrue(html_exists)
        self.assertIn(login_ui.INSTRUCTION, output)
        self.assertIn(f"二维码图片：{qr}", output)
        self.assertIn(login_ui.READY_MESSAGE, output)
        self.assertIn("已监视 0 秒", output)
        self.assertIn(login_ui.INSTRUCTION, html_text)
        self.assertNotIn("TOKEN-SECRET-VALUE", output)
        self.assertNotIn("TOKEN-SECRET-VALUE", html_text)
        self.assertNotIn("http://", output)
        self.assertNotIn("https://", output)


class LauncherPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.make_app = _load_module(
            "fatfish_make_app_qq_login_test", ROOT / "scripts" / "make_app.py"
        )

    def test_new_scripts_are_in_the_package_manifest(self) -> None:
        self.assertIn("qq_login_ui.py", self.make_app.SCRIPT_FILES)
        self.assertIn("start_qq_login.bat", self.make_app.SCRIPT_FILES)

    def test_vendor_stays_excluded(self) -> None:
        self.assertNotIn("vendor", self.make_app.PACKAGE_DIRS)
        self.assertFalse(
            any("vendor" in name for name in self.make_app.SCRIPT_FILES)
        )
        self.assertFalse(any("vendor" in name for name in self.make_app.ROOT_FILES))

    def test_root_launcher_exists_and_is_generated(self) -> None:
        source = ROOT / "QQ登录.bat"
        self.assertTrue(source.is_file())
        source_text = source.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("@echo off", source_text)
        self.assertIn('cd /d "%~dp0"', source_text)
        self.assertIn(r'call "scripts\start_qq_login.bat"', source_text)
        self.assertEqual(
            source_text.strip(), self.make_app.QQ_LOGIN_BAT.strip()
        )

        with tempfile.TemporaryDirectory() as raw:
            self.make_app._write_launch_scripts(Path(raw))
            generated = Path(raw) / "QQ登录.bat"
            self.assertTrue(generated.is_file())
            generated_text = generated.read_text(encoding="utf-8")
            self.assertIn(r'call "scripts\start_qq_login.bat"', generated_text)

    def test_start_script_contract(self) -> None:
        script = (ROOT / "scripts" / "start_qq_login.bat").read_text(
            encoding="utf-8"
        )
        self.assertIn("@echo off", script)
        self.assertIn("chcp 65001", script)
        self.assertIn('cd /d "%~dp0.."', script)
        self.assertIn("python --version", script)
        self.assertIn(r"python scripts\qq_login_ui.py", script)
        self.assertIn("pause", script)
        self.assertNotIn("runas", script.lower())


if __name__ == "__main__":
    unittest.main()
