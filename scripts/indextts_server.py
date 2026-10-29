#!/usr/bin/env python
"""Local IndexTTS-2.5 HTTP service for the fatfish TTS client.

The service keeps at most one bf16 model in memory and exposes the same
contract as ``scripts/cosyvoice_soft_server.py``:

  GET  /health  -> JSON status (works even while the model is unloaded)
  POST /tts     -> JSON {"text", "voice", "format", "emotion"} -> audio/wav
  POST /load    -> load the model now (local clients only)
  POST /unload  -> release the model now (local clients only)

IndexTTS-2.5 is the production engine: the reference clip decides *who* speaks,
while an 8-D vector decides *how*.  The tone -> vector/reference map lives in
:mod:`fatfish.tts.indextts`, so it is also usable from offline evaluation.
We deliberately do **not** use the built-in Qwen text-emotion path: it adds VRAM
and was the breathy/weak failure mode the user rejected.

Output post-process: the model speaks the v6 reference at its own pitch.  The
reference clip ``cand_home_up1.wav`` was produced offline **already +2 semitones
up**, so the service applies no pitch overlay by default
(``tts.pitch_semitones`` / ``--pitch-semitones`` defaults to 0).  A value >0
re-enables the old asetrate+atempo shift — only meaningful with a reference
recorded at the original pitch; stacking it on the v6 reference yields +4.

The model is loaded lazily on the first ``POST /tts`` and released after
``--idle-unload-seconds`` without requests.  Unloading drops the Python model
reference, collects garbage and calls ``torch.cuda.empty_cache()``.  The CUDA
context stays resident, exactly like the CosyVoice service, so ``nvidia-smi``
does not return to zero after an unload.

Run with the IndexTTS environment::

    .venv-indextts\\Scripts\\python.exe scripts\\indextts_server.py

``start_all.py`` selects this script when ``tts.engine`` is ``indextts25``.
"""

from __future__ import annotations

import argparse
import gc
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fatfish.tts.indextts import (  # noqa: E402
    DEFAULT_PITCH_SEMITONES,
    INTERVAL_SILENCE_MS,
    TONE_REFS,
    post_filter_for,
    reference_name_for,
    vector_for,
)
from fatfish.tts.instruct import normalize_tone  # noqa: E402

VENDOR = ROOT / "vendor" / "index-tts-main"
MODEL_DIR = ROOT / "models" / "IndexTTS-2.5"
REF_DIR = ROOT / "data" / "whale_voice_demo_v2" / "refs"

MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_CHARS = 300
DEFAULT_IDLE_UNLOAD_SECONDS = 300.0
SAMPLE_RATE_FALLBACK = 22050


def is_loopback_address(value: str) -> bool:
    """True for 127.0.0.0/8, ::1 and IPv4-mapped loopback addresses."""
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def _save_pcm_wav(path: str, wav: Any, sample_rate: int) -> None:
    """torchaudio 2.11 delegates saving to torchcodec, which is not installed.

    ``wav`` is PCM-scale ([-32767, 32767]) as produced by IndexTTS.  Write it
    with soundfile so the service does not depend on a second audio backend.
    """
    import soundfile as sf

    data = wav.detach().cpu().float().numpy()
    if data.ndim == 2:
        if data.shape[0] == 1:
            data = data[0]
        elif data.shape[1] == 1:
            data = data[:, 0]
        else:
            data = data.T
    data = data / 32767.0
    sf.write(path, data, int(sample_rate), subtype="PCM_16")


def _install_wetext_cache() -> None:
    """Point wetext at its local cache instead of re-downloading every load.

    The files are already under ``~/.cache/modelscope``; asking ModelScope again
    just hits its rate limit and can make a cold load fail for no reason.
    """
    wetext_dir = (
        Path.home() / ".cache" / "modelscope" / "hub" / "pengzhendong" / "wetext"
    )
    if not wetext_dir.exists():
        return
    try:
        import wetext.wetext as wetext_module  # type: ignore[import-not-found]

        wetext_module.snapshot_download = lambda *args, **kwargs: str(wetext_dir)
    except Exception:  # noqa: BLE001 - optional cache optimisation
        pass


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / float(rate) if rate else 0.0
    except (OSError, wave.Error):
        return 0.0


def _post_process(
    raw: Path, final: Path, sample_rate: int, pitch_semitones: int
) -> None:
    """Normalize to 16-bit PCM mono; shift pitch only when configured.

    The v6 reference ``cand_home_up1.wav`` already carries the +2 semitone
    identity shift, so production runs with ``pitch_semitones=0`` and this only
    normalizes the container.  ``post_filter_for`` returns the asetrate+atempo
    chain when a reference at the original pitch needs the overlay.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg_missing")
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(raw),
    ]
    filter_spec = post_filter_for(pitch_semitones, sample_rate)
    if filter_spec is not None:
        args.extend(["-af", filter_spec])
    args.extend(
        [
            "-ar", str(int(sample_rate)), "-ac", "1",
            "-c:a", "pcm_s16le", str(final),
        ]
    )
    subprocess.run(args, check=True)


class IndexTTSService:
    """Lazy model holder, one inference lock and an idle-unload watchdog."""

    def __init__(
        self,
        *,
        device: str = "cuda:0",
        fp16: bool = True,
        ref_dir: Path | str = REF_DIR,
        idle_unload_seconds: float = DEFAULT_IDLE_UNLOAD_SECONDS,
        seed: int = -1,
        pitch_semitones: int = DEFAULT_PITCH_SEMITONES,
    ) -> None:
        self.device = str(device)
        self.fp16 = bool(fp16)
        self.ref_dir = Path(ref_dir)
        self.idle_unload_seconds = max(0.0, float(idle_unload_seconds))
        self.seed = int(seed)
        # 参考音已含 +2 半音，叠加默认关闭；负值按 0 处理（机制只做升调）。
        self.pitch_semitones = max(0, int(pitch_semitones))
        self.model: Any = None
        self.torch: Any = None
        self.sample_rate = SAMPLE_RATE_FALLBACK
        self.load_seconds = 0.0
        self.requests = 0
        self.errors = 0
        self.loads = 0
        self.unloads = 0
        self.last_request_at: float | None = None
        self.last_load_at: float | None = None
        self.last_unload_at: float | None = None
        self.last_unload_reason: str | None = None
        self._last_used = time.monotonic()
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._watchdog = threading.Thread(
            target=self._idle_loop, name="indextts-idle", daemon=True
        )
        self._watchdog.start()

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def _idle_loop(self) -> None:
        interval = max(1.0, min(30.0, self.idle_unload_seconds / 2.0 or 5.0))
        while not self._stop.wait(interval):
            if self.idle_unload_seconds <= 0:
                continue
            with self.lock:
                if self.model is not None and (
                    time.monotonic() - self._last_used >= self.idle_unload_seconds
                ):
                    self._unload_locked("idle")

    def _release_cuda_cache(self) -> None:
        gc.collect()
        torch_module = self.torch
        cuda = getattr(torch_module, "cuda", None) if torch_module is not None else None
        if cuda is None:
            return
        try:
            cuda.synchronize()
        except Exception:  # noqa: BLE001 - cleanup must never mask real errors
            pass
        try:
            cuda.empty_cache()
        except Exception:  # noqa: BLE001 - cleanup must never mask real errors
            pass
        gc.collect()

    def _unload_locked(self, reason: str) -> bool:
        if self.model is None:
            return False
        self.model = None
        self._release_cuda_cache()
        self.unloads += 1
        self.last_unload_at = time.time()
        self.last_unload_reason = reason
        return True

    def _load_locked(self) -> float:
        if self.model is not None:
            return self.load_seconds
        started = time.perf_counter()
        if str(VENDOR) not in sys.path:
            sys.path.insert(0, str(VENDOR))
        _install_wetext_cache()
        import torch  # type: ignore[import-not-found]

        from indextts import infer_v2_5  # type: ignore[import-not-found]
        from indextts.infer_v2_5 import IndexTTS2  # type: ignore[import-not-found]

        self.torch = torch
        infer_v2_5.save_pcm_wav = _save_pcm_wav
        self.model = IndexTTS2(
            cfg_path=str(MODEL_DIR / "config.yaml"),
            model_dir=str(MODEL_DIR),
            # 2.5 exposes use_bf16 (2.0 used use_fp16); bf16 is the checkpoint's
            # native dtype and is stable on the 5060.
            use_bf16=self.fp16 and not self.device.startswith("cpu"),
            device=self.device,
            # The custom fused kernel is not compiled in this environment.
            use_cuda_kernel=False,
            # Vector-only: do not load QwenEmotion / the text-emotion path.
            use_qwen_emo=False,
        )
        self.sample_rate = int(
            getattr(self.model, "sampling_rate", SAMPLE_RATE_FALLBACK)
            or SAMPLE_RATE_FALLBACK
        )
        self.load_seconds = time.perf_counter() - started
        self.loads += 1
        self.last_load_at = time.time()
        self._last_used = time.monotonic()
        return self.load_seconds

    def load(self) -> float:
        with self.lock:
            return self._load_locked()

    def unload(self) -> bool:
        with self.lock:
            return self._unload_locked("request")

    def health(self) -> dict[str, Any]:
        return {
            # ok describes the HTTP service, not the model: an unloaded model is
            # the normal steady state with idle unloading enabled.
            "ok": True,
            "model": "IndexTTS-2.5",
            "engine": "indextts25",
            "loaded": self.loaded,
            "device": self.device,
            "sample_rate": self.sample_rate,
            "requests": self.requests,
            "errors": self.errors,
            "loads": self.loads,
            "unloads": self.unloads,
            "load_seconds": round(float(self.load_seconds), 3),
            "idle_unload_seconds": float(self.idle_unload_seconds),
            "lazy_load": True,
            "last_request_at": _iso_timestamp(self.last_request_at),
            "last_load_at": _iso_timestamp(self.last_load_at),
            "last_unload_at": _iso_timestamp(self.last_unload_at),
            "last_unload_reason": self.last_unload_reason,
            "ref_dir": str(self.ref_dir),
            "ref": TONE_REFS["neutral"],
            "refs": dict(TONE_REFS),
            "pitch_semitones": self.pitch_semitones,
            "post_filter": post_filter_for(self.pitch_semitones, self.sample_rate) or "",
            "interval_silence_ms": INTERVAL_SILENCE_MS,
            "seed": self.seed,
        }

    def synthesize(
        self,
        text: str,
        emotion: object = None,
        *,
        vector: list[float] | None = None,
        ref_name: str | None = None,
        interval_silence: int | None = None,
    ) -> tuple[bytes, float, str]:
        """Return ``(wav_bytes, duration_s, tone)``.

        The whole model call is serialized: IndexTTS-2.5 is not safe for two
        simultaneous generations in one process, and a second allocation would
        take the 8 GB card down.

        ``vector`` and ``ref_name`` are loopback-only staging overrides used to
        audition new prototypes without loading a second model. Production
        requests keep using the tone map in :mod:`fatfish.tts.indextts`.
        """
        tone = normalize_tone(emotion)
        if ref_name is not None:
            ref_name = str(ref_name)
            if Path(ref_name).name != ref_name:
                raise ValueError("invalid_reference_name")
            ref = self.ref_dir / ref_name
            tone = "debug"
        else:
            ref = self.ref_dir / reference_name_for(tone)
        if not ref.is_file():
            raise FileNotFoundError(f"missing_reference:{ref.name}")
        with self.lock:
            self.requests += 1
            self.last_request_at = time.time()
            try:
                self._load_locked()
                if self.seed >= 0:
                    self.torch.manual_seed(self.seed)
                handle, raw_path = tempfile.mkstemp(
                    prefix="indextts_", suffix=".wav", dir=str(ROOT / "data")
                )
                try:
                    os.close(handle)
                except OSError:
                    pass
                raw = Path(raw_path)
                raw.unlink(missing_ok=True)
                final = raw.with_name(raw.stem + "_v6.wav")
                try:
                    silence = (
                        INTERVAL_SILENCE_MS
                        if interval_silence is None
                        else max(0, min(1000, int(interval_silence)))
                    )
                    kwargs: dict[str, Any] = {
                        "spk_audio_prompt": str(ref),
                        "text": text,
                        "output_path": str(raw),
                        "interval_silence": silence,
                        "lang": "zh",
                        "verbose": False,
                    }
                    if vector is None:
                        vector = vector_for(tone)
                    if vector is not None:
                        kwargs["emo_vector"] = vector
                    self.model.infer(**kwargs)
                    if not raw.is_file() or raw.stat().st_size <= 0:
                        raise RuntimeError("empty_audio")
                    _post_process(raw, final, self.sample_rate, self.pitch_semitones)
                    if not final.is_file() or final.stat().st_size <= 0:
                        raise RuntimeError("empty_audio_after_post")
                    data = final.read_bytes()
                    duration = _wav_duration(final)
                finally:
                    raw.unlink(missing_ok=True)
                    final.unlink(missing_ok=True)
                self._last_used = time.monotonic()
                return data, duration, tone
            except Exception:
                self.errors += 1
                raise

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            self._unload_locked("shutdown")


def _warm_model(service: IndexTTSService) -> None:
    """Load the model in a daemon thread so the HTTP port can bind first."""
    try:
        seconds = service.load()
        print(f"warmup done in {seconds:.1f}s", flush=True)
    except Exception:  # noqa: BLE001 - a warmup failure must not kill the service
        traceback.print_exc()


def _iso_timestamp(value: float | None) -> str | None:
    if not value:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(value))


def build_handler(service: IndexTTSService) -> type[BaseHTTPRequestHandler]:
    class TTSHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FatfishIndexTTS/1.0"
        sys_version = ""
        timeout = 180

        def log_message(self, fmt: str, *args: Any) -> None:
            # Request bodies contain user text and must never reach access logs.
            return

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.close_connection = True
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._respond(status, raw, "application/json; charset=utf-8")

        def _model_error(self, exc: Exception) -> None:
            message = str(exc) or type(exc).__name__
            code = (
                "out_of_memory" if "out of memory" in message.lower() else "model_error"
            )
            self._json(
                503,
                {
                    "ok": False,
                    "error": code,
                    "detail": f"{type(exc).__name__}: {message[:160]}",
                },
            )

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urlsplit(self.path).path
            if path == "/health":
                self._json(200, service.health())
                return
            if path in ("/tts", "/load", "/unload"):
                self._json(405, {"ok": False, "error": "method_not_allowed"})
                return
            self._json(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urlsplit(self.path).path
            if path not in ("/tts", "/load", "/unload"):
                self.close_connection = True
                self._json(404, {"ok": False, "error": "not_found"})
                return
            if path in ("/load", "/unload"):
                if not is_loopback_address(self.client_address[0]):
                    self._json(403, {"ok": False, "error": "local_only"})
                    return
                if path == "/unload":
                    released = service.unload()
                    self._json(
                        200,
                        {
                            "ok": True,
                            "unloaded": bool(released),
                            "loaded": service.loaded,
                        },
                    )
                    return
                try:
                    load_seconds = service.load()
                except Exception as exc:  # noqa: BLE001 - report load failures
                    self._model_error(exc)
                    return
                self._json(
                    200,
                    {
                        "ok": True,
                        "loaded": service.loaded,
                        "load_seconds": round(load_seconds, 3),
                    },
                )
                return
            self._handle_tts()

        def _handle_tts(self) -> None:
            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header) if length_header is not None else -1
            except (TypeError, ValueError):
                length = -1
            if length < 0:
                self._json(400, {"ok": False, "error": "content_length_required"})
                return
            if length > MAX_BODY_BYTES:
                self.close_connection = True
                self._json(
                    413,
                    {
                        "ok": False,
                        "error": "body_too_large",
                        "limit": MAX_BODY_BYTES,
                    },
                )
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._json(400, {"ok": False, "error": "incomplete_body"})
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"ok": False, "error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"ok": False, "error": "json_object_required"})
                return
            text = payload.get("text")
            if not isinstance(text, str):
                self._json(400, {"ok": False, "error": "text_required"})
                return
            text = text.strip()
            if not text:
                self._json(400, {"ok": False, "error": "text_empty"})
                return
            if len(text) > MAX_TEXT_CHARS:
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "text_too_long",
                        "limit": MAX_TEXT_CHARS,
                    },
                )
                return

            # Staging overrides are deliberately loopback-only: they can pick a
            # different reference and an arbitrary 8-D vector without changing
            # the production tone map.
            debug = is_loopback_address(self.client_address[0])
            vector = payload.get("vector")
            ref_name = payload.get("ref")
            interval = payload.get("interval_silence")
            if (vector is not None or ref_name is not None or interval is not None) and not debug:
                self._json(403, {"ok": False, "error": "local_only"})
                return
            if vector is not None:
                if (
                    not isinstance(vector, list)
                    or len(vector) != 8
                    or any(not isinstance(value, (int, float)) for value in vector)
                ):
                    self._json(
                        400,
                        {
                            "ok": False,
                            "error": "vector_must_be_eight_numbers",
                        },
                    )
                    return
                vector = [max(0.0, min(1.0, float(value))) for value in vector]
            if ref_name is not None and (
                not isinstance(ref_name, str) or Path(ref_name).name != ref_name
            ):
                self._json(400, {"ok": False, "error": "invalid_reference_name"})
                return
            if interval is not None:
                if not isinstance(interval, (int, float)):
                    self._json(400, {"ok": False, "error": "invalid_interval_silence"})
                    return
                interval = int(interval)
            try:
                audio, duration, tone = service.synthesize(
                    text,
                    payload.get("emotion"),
                    vector=vector,
                    ref_name=ref_name,
                    interval_silence=interval,
                )
            except Exception as exc:  # noqa: BLE001 - one bad request is not fatal
                self._model_error(exc)
                return
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Fatfish-Tone", tone)
                self.send_header("X-Fatfish-Duration", f"{duration:.3f}")
                self.end_headers()
                self.wfile.write(audio)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return TTSHandler


def create_server(
    service: IndexTTSService, host: str, port: int
) -> ThreadingHTTPServer:
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return Server((host, port), build_handler(service))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IndexTTS-2.5 local HTTP service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--no-fp16", action="store_true", help="disable bf16 (CPU/debug)")
    parser.add_argument(
        "--idle-unload-seconds", type=float, default=DEFAULT_IDLE_UNLOAD_SECONDS
    )
    parser.add_argument("--ref-dir", default=str(REF_DIR))
    parser.add_argument("--seed", type=int, default=-1)
    parser.add_argument(
        "--pitch-semitones",
        type=int,
        default=DEFAULT_PITCH_SEMITONES,
        help=(
            "semitone overlay applied after synthesis (default 0: the v6 "
            "reference cand_home_up1.wav is already +2 up; >0 only for a "
            "reference recorded at the original pitch)"
        ),
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help="load the model in the background after the HTTP port is bound",
    )
    args = parser.parse_args(argv)

    service = IndexTTSService(
        device=args.device,
        fp16=not args.no_fp16,
        ref_dir=Path(args.ref_dir),
        idle_unload_seconds=args.idle_unload_seconds,
        seed=args.seed,
        pitch_semitones=args.pitch_semitones,
    )
    server = create_server(service, args.host, args.port)
    if args.warmup:
        threading.Thread(
            target=_warm_model, args=(service,), name="indextts-warmup", daemon=True
        ).start()
    print(
        "IndexTTS-2.5 service on http://%s:%d (lazy load, warmup=%s, "
        "idle unload %.0fs, pitch %+d st)"
        % (
            args.host,
            args.port,
            "on" if args.warmup else "off",
            args.idle_unload_seconds,
            args.pitch_semitones,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
