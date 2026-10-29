"""scripts/indextts_server.py 的音高叠加：配置驱动，默认关闭。

参考音 ``cand_home_up1.wav`` 离线制作时已经 +2 半音，服务端默认 0 = 只做
16bit/单声道归一，不再叠加；配置 >0 时按旧机制 asetrate+atempo 生成过滤串
（2026-10-25 修复 +4 音高过头）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "indextts_server.py"


def _load_server():
    spec = importlib.util.spec_from_file_location("fatfish_indextts_server", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scripts/indextts_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load_server()


class PostProcessFilterTests(unittest.TestCase):
    def test_no_overlay_when_pitch_is_zero(self) -> None:
        # 默认：参考音已含 +2，只归一容器，不得再传 -af。
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.wav"
            final = Path(tmp) / "final.wav"
            with patch.object(server.shutil, "which", return_value="ffmpeg"):
                with patch.object(server.subprocess, "run") as run:
                    server._post_process(raw, final, 22050, 0)
        args = run.call_args[0][0]
        self.assertNotIn("-af", args)
        self.assertEqual(args[args.index("-ar") + 1], "22050")
        self.assertEqual(args[args.index("-ac") + 1], "1")
        self.assertIn("pcm_s16le", args)

    def test_overlay_uses_legacy_chain_when_configured(self) -> None:
        # tts.pitch_semitones=2：原样复现旧 +2 过滤串（asetrate + atempo）。
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.wav"
            final = Path(tmp) / "final.wav"
            with patch.object(server.shutil, "which", return_value="ffmpeg"):
                with patch.object(server.subprocess, "run") as run:
                    server._post_process(raw, final, 22050, 2)
        args = run.call_args[0][0]
        self.assertEqual(
            args[args.index("-af") + 1],
            "asetrate=22050*1.122,aresample=22050,atempo=1/1.122",
        )

    def test_missing_ffmpeg_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.wav"
            final = Path(tmp) / "final.wav"
            with patch.object(server.shutil, "which", return_value=None):
                with self.assertRaises(RuntimeError) as caught:
                    server._post_process(raw, final, 22050, 0)
        self.assertEqual("ffmpeg_missing", str(caught.exception))


class ServicePitchConfigTests(unittest.TestCase):
    def test_health_reports_pitch_and_filter(self) -> None:
        # /health 不加载模型也能读：默认 0 + 空 post_filter；配置 2 时报旧串。
        with tempfile.TemporaryDirectory() as tmp:
            default = server.IndexTTSService(ref_dir=Path(tmp))
            try:
                self.assertEqual(default.pitch_semitones, 0)
                self.assertEqual(default.health()["pitch_semitones"], 0)
                self.assertEqual(default.health()["post_filter"], "")
            finally:
                default.shutdown()
        with tempfile.TemporaryDirectory() as tmp:
            shifted = server.IndexTTSService(ref_dir=Path(tmp), pitch_semitones=2)
            try:
                self.assertEqual(shifted.health()["pitch_semitones"], 2)
                self.assertIn("1.122", shifted.health()["post_filter"])
            finally:
                shifted.shutdown()

    def test_negative_pitch_is_clamped_to_zero(self) -> None:
        # 机制只做升调；负值不该悄悄把人声压低。
        with tempfile.TemporaryDirectory() as tmp:
            service = server.IndexTTSService(ref_dir=Path(tmp), pitch_semitones=-2)
            try:
                self.assertEqual(service.pitch_semitones, 0)
            finally:
                service.shutdown()


class CliFlagTests(unittest.TestCase):
    def test_help_advertises_pitch_semitones(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--pitch-semitones", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
