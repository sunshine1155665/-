from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start_all.py"


def _load_start_all():
    spec = importlib.util.spec_from_file_location("fatfish_start_all", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scripts/start_all.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


start_all = _load_start_all()


class StartAllUnitTests(unittest.TestCase):
    def test_parse_web_banner(self) -> None:
        self.assertEqual(
            "http://127.0.0.1:8765/?token=abc",
            start_all.parse_web_banner(
                "fatfish web: http://127.0.0.1:8765/?token=abc"
            ),
        )
        self.assertEqual(
            "http://[::1]:9000/?token=a%2Fb",
            start_all.parse_web_banner(
                "  fatfish web: http://[::1]:9000/?token=a%2Fb  "
            ),
        )
        self.assertIsNone(start_all.parse_web_banner("bound: 127.0.0.1:8765"))
        self.assertIsNone(start_all.parse_web_banner("fatfish web: https://x/y"))
        self.assertIsNone(start_all.parse_web_banner(""))

    def test_redact_hides_token(self) -> None:
        token = "secret-token-value"
        line = f"fatfish web: http://127.0.0.1:1/?token={token}"
        masked = start_all.redact(line, (token,))
        self.assertNotIn(token, masked)
        self.assertIn("<token>", masked)
        self.assertEqual("plain", start_all.redact("plain", ()))
        self.assertEqual("plain", start_all.redact("plain", ("",)))

    def test_interrupted_exit(self) -> None:
        for code in (-2, -15, 130, 3221225786, -1073741510):
            self.assertTrue(start_all._interrupted_exit(code), code)
        for code in (None, 0, 1, 2):
            self.assertFalse(start_all._interrupted_exit(code), code)

    def test_pid_helpers_and_stale_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fatfish-pid-test-") as tmp:
            run_dir = Path(tmp)
            live = run_dir / "live.pid"
            dead = run_dir / "dead.pid"
            junk = run_dir / "junk.pid"

            start_all.write_pid(live, os.getpid())
            start_all.write_pid(dead, 99999999)
            junk.write_text("not-a-pid\n", encoding="ascii")

            self.assertEqual(os.getpid(), start_all.read_pid(live))
            self.assertEqual(99999999, start_all.read_pid(dead))
            self.assertIsNone(start_all.read_pid(junk))
            self.assertTrue(start_all.pid_alive(os.getpid()))
            self.assertFalse(start_all.pid_alive(99999999))
            self.assertFalse(start_all.pid_alive(0))
            self.assertFalse(start_all.pid_alive(-1))

            removed = {path.name for path in start_all.clean_stale_pid_files(run_dir)}
            self.assertEqual({"dead.pid", "junk.pid"}, removed)
            self.assertTrue(live.is_file())
            self.assertFalse(dead.exists())
            self.assertFalse(junk.exists())

            start_all.remove_pid(live, os.getpid())
            self.assertFalse(live.exists())

    def test_lock_helper_acquire_and_release(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fatfish-lock-test-") as tmp:
            path = Path(tmp) / "supervisor.lock"
            first = start_all.acquire_supervisor_lock(path)
            self.assertIsNotNone(first)
            start_all.release_supervisor_lock(first)
            second = start_all.acquire_supervisor_lock(path)
            self.assertIsNotNone(second)
            start_all.release_supervisor_lock(second)

    def test_tts_engine_reads_config_and_defaults(self) -> None:
        class Config:
            def __init__(self, values: dict):
                self.values = values

            def get(self, key, default=None):
                return self.values.get(key, default)

        self.assertEqual("cosyvoice", start_all.tts_engine(Config({})))
        self.assertEqual(
            "indextts25", start_all.tts_engine(Config({"tts.engine": "IndexTTS25"}))
        )
        # 旧配置里的 "indextts" 当作 2.5 的别名，仍然能启动。
        self.assertEqual(
            "indextts25", start_all.tts_engine(Config({"tts.engine": "indextts"}))
        )
        # 拼错/未来值不能把启动器卡死；回退到旧引擎是刻意的。
        self.assertEqual(
            "cosyvoice", start_all.tts_engine(Config({"tts.engine": "bogus"}))
        )

    def test_tts_command_passes_keepalive_flags(self) -> None:
        command = start_all.tts_command(
            Path("python"),
            Path("server.py"),
            idle_unload_seconds=0,
            warmup=True,
        )
        self.assertIn("--idle-unload-seconds", command)
        self.assertEqual("0", command[command.index("--idle-unload-seconds") + 1])
        self.assertIn("--warmup", command)

        plain = start_all.tts_command(Path("python"), Path("server.py"))
        self.assertNotIn("--idle-unload-seconds", plain)
        self.assertNotIn("--warmup", plain)

    def test_tts_pitch_semitones_reads_config(self) -> None:
        class Config:
            def __init__(self, values: dict):
                self.values = values

            def get(self, key, default=None):
                return self.values.get(key, default)

        # 参考音 cand_home_up1.wav 已含 +2 半音：默认/负值/垃圾值都必须落在
        # 0（不叠加）；只有显式配置 >0 才允许叠加。
        self.assertEqual(0, start_all.tts_pitch_semitones(Config({})))
        self.assertEqual(
            0, start_all.tts_pitch_semitones(Config({"tts.pitch_semitones": -2}))
        )
        self.assertEqual(
            0, start_all.tts_pitch_semitones(Config({"tts.pitch_semitones": "x"}))
        )
        self.assertEqual(
            2, start_all.tts_pitch_semitones(Config({"tts.pitch_semitones": 2}))
        )

    def test_tts_command_passes_pitch_flag(self) -> None:
        command = start_all.tts_command(
            Path("python"), Path("server.py"), pitch_semitones=0
        )
        self.assertIn("--pitch-semitones", command)
        self.assertEqual("0", command[command.index("--pitch-semitones") + 1])

        clamped = start_all.tts_command(
            Path("python"), Path("server.py"), pitch_semitones=-1
        )
        self.assertEqual("0", clamped[clamped.index("--pitch-semitones") + 1])

        plain = start_all.tts_command(Path("python"), Path("server.py"))
        self.assertNotIn("--pitch-semitones", plain)

    def test_warmup_is_indextts_only(self) -> None:
        class Config:
            def __init__(self, values: dict):
                self.values = values

            def get(self, key, default=None):
                return self.values.get(key, default)

        self.assertTrue(
            start_all.tts_warmup(
                Config({"tts.engine": "indextts", "tts.warmup": True})
            )
        )
        self.assertFalse(
            start_all.tts_warmup(
                Config({"tts.engine": "cosyvoice", "tts.warmup": True})
            )
        )
        self.assertEqual(
            0.0,
            start_all.tts_idle_unload_seconds(
                Config({"tts.idle_unload_seconds": 0})
            ),
        )
        self.assertIsNone(
            start_all.tts_idle_unload_seconds(Config({}))
        )

    @patch.object(start_all, "port_open", return_value=True)
    @patch.object(
        start_all,
        "http_health",
        return_value={"ok": True, "engine": "cosyvoice"},
    )
    def test_start_tts_refuses_engine_mismatch(self, _health, _port) -> None:
        class Config:
            def get(self, key, default=None):
                return {
                    "tts.enabled": True,
                    "tts.mode": "http_json",
                    "tts.engine": "indextts",
                    "tts.base_url": "http://127.0.0.1:9880/tts",
                }.get(key, default)

        child, pid_path = start_all.start_tts(
            Config(),
            profile="low",
            disabled=False,
            run_dir=Path(tempfile.mkdtemp()),
            logs_dir=Path(tempfile.mkdtemp()),
        )
        self.assertIsNone(child)
        self.assertIsNone(pid_path)

    def test_parse_args_flags(self) -> None:
        args = start_all.parse_args(
            ["--web-only", "--no-browser", "--no-tts", "--port", "0", "--profile", "low"]
        )
        self.assertTrue(args.web_only)
        self.assertTrue(args.no_browser)
        self.assertTrue(args.no_tts)
        self.assertEqual(0, args.port)
        self.assertEqual("low", args.profile)

        defaults = start_all.parse_args([])
        self.assertFalse(defaults.web_only)
        self.assertFalse(defaults.no_qq)
        self.assertFalse(defaults.no_tts)
        self.assertIsNone(defaults.profile)
        self.assertEqual(0, defaults.port)

    def test_help_runs_without_importing_runtime(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        for flag in (
            "--no-browser",
            "--no-qq",
            "--web-only",
            "--no-tts",
            "--no-embedding",
            "--port",
            "--profile",
        ):
            self.assertIn(flag, result.stdout)
        self.assertNotIn("Traceback", result.stderr)


class EmbeddingServiceTests(unittest.TestCase):
    """向量服务只在「后端要 HTTP」时才该被拉起来。

    误拉起来的代价不是崩溃，是悄悄变了行为：哈希配置下起一个 768 维服务，
    库里的 256 维向量就全判 0 分了。
    """

    class FakeConfig:
        def __init__(self, values: dict):
            self.values = values

        def get(self, key, default=None):
            return self.values.get(key, default)

    def test_hash_backend_does_not_start_the_service(self) -> None:
        config = self.FakeConfig({"memory.embedding_backend": "hash"})
        self.assertFalse(start_all.embedding_enabled(config))

    def test_http_backend_starts_it(self) -> None:
        config = self.FakeConfig({"memory.embedding_backend": "http"})
        self.assertTrue(start_all.embedding_enabled(config))

    def test_sticker_key_no_longer_starts_it(self) -> None:
        # Sticker retrieval is gone; a leftover sticker.* key must not spawn 9881.
        config = self.FakeConfig({"sticker.embedding_backend": "http"})
        self.assertFalse(start_all.embedding_enabled(config))

    def test_missing_pieces_are_listed_not_raised(self) -> None:
        config = self.FakeConfig({"memory.embedding_backend": "http"})
        missing, python = start_all.embedding_components(config)
        self.assertIsInstance(missing, list)
        self.assertIn("python", str(python).lower())

    def test_skipped_when_asked(self) -> None:
        config = self.FakeConfig({"memory.embedding_backend": "http"})
        with tempfile.TemporaryDirectory() as tmp:
            child, pid_path = start_all.start_embedding(
                config,
                profile="low",
                disabled=True,
                run_dir=Path(tmp),
                logs_dir=Path(tmp),
            )
        self.assertIsNone(child)
        self.assertIsNone(pid_path)


class ServiceRuntimeTests(unittest.TestCase):
    """控制面只能开到 ServiceRuntime 上，它才是进程的所有者。

    这些测试刻意不拉起任何进程：它们检查的是「拒绝」和「如实汇报」，
    而这两件事恰恰是原来那套「谁都能 spawn」最容易出错的地方。
    """

    class FakeConfig:
        def __init__(self, values: dict):
            self.values = values

        def get(self, key, default=None):
            return self.values.get(key, default)

    def _runtime(self, values: dict, tmp: str) -> "start_all.ServiceRuntime":
        root = Path(tmp)
        return start_all.ServiceRuntime(
            self.FakeConfig(values),
            "low",
            run_dir=root / "run",
            logs_dir=root / "logs",
            websocket_ready=False,
        )

    def test_status_lists_every_service_with_the_expected_shape(self) -> None:
        # 端口探测要打桩：本机可能真的开着 9881，那样测的就不是代码了。
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime({"tts.enabled": False}, tmp)
            with patch.object(start_all, "port_open", return_value=False):
                rows = runtime.status()
        self.assertEqual(
            {"tts", "embedding", "comfyui", "qq"}, {row["name"] for row in rows}
        )
        for row in rows:
            self.assertIn(
                row["state"], {"running", "starting", "external", "stopped", "failed"}
            )
            self.assertIn("assets_ok", row)
            self.assertIn("controllable", row)
            self.assertFalse(row["healthy"])
        comfy = next(row for row in rows if row["name"] == "comfyui")
        self.assertFalse(comfy["controllable"], "ComfyUI 归拍照服务所有")

    def test_comfyui_cannot_be_started_from_here(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime({}, tmp)
            with self.assertRaises(start_all.ControlError) as caught:
                runtime.start("comfyui")
        self.assertEqual("not_owned", caught.exception.code)

    def test_disabled_tts_is_refused_not_spawned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime({"tts.enabled": False}, tmp)
            with self.assertRaises(start_all.ControlError) as caught:
                runtime.start("tts")
        self.assertEqual("disabled", caught.exception.code)
        self.assertIsNone(runtime.child("tts"))

    def test_hash_backend_does_not_start_the_embedding_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime({"memory.embedding_backend": "hash"}, tmp)
            with self.assertRaises(start_all.ControlError) as caught:
                runtime.start("embedding")
        self.assertEqual("disabled", caught.exception.code)

    def test_qq_without_token_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime({"qq.access_token": ""}, tmp)
            with self.assertRaises(start_all.ControlError):
                runtime.start("qq")

    def test_unload_only_applies_to_tts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime({}, tmp)
            with self.assertRaises(start_all.ControlError) as caught:
                runtime.unload("embedding")
        self.assertEqual("unsupported", caught.exception.code)

    def test_stopping_something_that_never_ran_is_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime({}, tmp)
            # Port 0 is never listened on, so this cannot hit a real service.
            runtime._port_of = lambda name: 0  # type: ignore[method-assign]
            with self.assertRaises(start_all.ControlError) as caught:
                runtime.stop("qq")
        self.assertEqual("not_running", caught.exception.code)

    def test_release_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime({}, tmp)
            with patch.object(start_all, "pid_on_port", return_value=None), patch.object(
                start_all, "port_open", return_value=False
            ):
                result = runtime.release()
        self.assertEqual("done", result["state"])
        self.assertIn("ComfyUI", result["detail"])
        self.assertIn("未运行", result["detail"])


if __name__ == "__main__":
    unittest.main()
