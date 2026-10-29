"""``scripts/indextts_server.py`` must not clip IndexTTS-2's int16-range output."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

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


class _FakeTensor:
    def __init__(self, data: np.ndarray):
        self.data = data

    def detach(self):
        return self

    def cpu(self):
        return self

    def float(self):
        return self

    def numpy(self):
        return self.data


def _roundtrip(source: np.ndarray) -> np.ndarray:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "out.wav"
        server._save_pcm_wav(str(path), _FakeTensor(source[None, :]), 22050)
        with wave.open(str(path), "rb") as wav:
            data = np.frombuffer(
                wav.readframes(wav.getnframes()), dtype="<i2"
            ).astype(np.float32) / 32768.0
    return data


class IndexttsSaveTests(unittest.TestCase):
    def test_int16_range_is_scaled_before_pcm16(self) -> None:
        # IndexTTS2.infer() returns 32767 * wav.  Writing that straight to a
        # PCM16 file clips almost every sample and sounds like electric noise;
        # the service divides by 32767 first.
        source = (np.sin(np.linspace(0, 6.28, 2000)) * 20000).astype(np.float32)
        data = _roundtrip(source)
        self.assertLessEqual(float(np.max(np.abs(data))), 0.999)
        self.assertGreater(float(np.max(np.abs(data))), 0.5)
        self.assertGreater(float(np.sqrt(np.mean(data**2))), 0.1)

    def test_full_scale_maps_to_unit_without_double_scaling(self) -> None:
        # Full-scale int16 input must land at ±1.0: scaled exactly once, no clip.
        source = (np.sin(np.linspace(0, 6.28, 2000)) * 32767.0).astype(np.float32)
        data = _roundtrip(source)
        self.assertAlmostEqual(float(np.max(np.abs(data))), 1.0, places=2)
        self.assertLessEqual(float(np.max(np.abs(data))), 1.0)


if __name__ == "__main__":
    unittest.main()
