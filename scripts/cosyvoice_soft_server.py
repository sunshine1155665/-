"""Local CosyVoice3 HTTP service using the soft canonical reference.

The service keeps at most one fp32 model in memory and exposes:

  GET  /health  -> JSON status (works even while the model is unloaded)
  POST /tts     -> JSON {"text", "voice", "format", "emotion"} -> audio/wav
  POST /load    -> load the model now (local clients only)
  POST /unload  -> release the model now (local clients only)

The model can be loaded lazily on the first ``POST /tts`` and is released again
after ``--idle-unload-seconds`` without requests.  Unloading drops the Python
model reference, collects garbage and calls ``torch.cuda.empty_cache()``.  The
CUDA context and any ONNX Runtime arenas owned by the process stay resident;
that baseline is why ``nvidia-smi`` does not return to zero after an unload.

It matches the existing ``fatfish.tts.client.TTSClient`` ``http_json`` contract:
raw ``audio/wav`` bytes are returned, so the runtime can save the file directly.
No reference switching happens per emotion: the soft ``ref_base_x2`` reference
and the fixed zero-shot prompt are always used.

Run with the CosyVoice virtualenv::

    .venv-cosyvoice\\Scripts\\python.exe scripts\\cosyvoice_soft_server.py

Verification without starting a server::

    .venv-cosyvoice\\Scripts\\python.exe scripts\\cosyvoice_soft_server.py --self-test
"""

from __future__ import annotations

import argparse
import array
import contextlib
import gc
import io
import ipaddress
import json
import logging
import math
import os
import socket
import sys
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("FATFISH_DATA_DIR") or (ROOT / "data"))
MODEL_DIR = ROOT / "models" / "Fun-CosyVoice3-0.5B"

# The CosyVoice virtualenv does not install the project package, but the lock
# helper is stdlib-only; ROOT on sys.path is enough.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from fatfish.process_lock import acquire, held, lock_path  # noqa: E402
COSY_DIR = ROOT / "repos" / "CosyVoice"
REF_PATH = ROOT / "data" / "whale_voice_demo_v2" / "refs" / "ref_base_x2.wav"
PID_PATH = ROOT / "data" / "run" / "tts_soft.pid"
SMOKE_PATH = ROOT / "data" / "tts_soft_smoke.wav"

# Same prompt as scripts/voice_consistency_v2.py variant F: the seed text is
# repeated because the reference itself is ref_base repeated twice (~8.85 s).
_SEED_TEXT = "你好呀，我是大肥鱼。今天海面很安静，我在这里等你。"
PROMPT_TEXT = (
    "You are a helpful assistant.<|endofprompt|>"
    + _SEED_TEXT
    + " "
    + _SEED_TEXT
)
WARMUP_TEXT = "你好呀"
SELF_TEST_TEXT = "你好呀，我是大肥鱼。"

SEED = 42
SPEED = 1.0
SAMPLE_RATE = 24000
PEAK = 0.9

MAX_BODY_BYTES = 16 * 1024
MAX_TEXT_CHARS = 300
# instruct 是一句固定措辞（最长的官方原话不到 40 字），200 只是防滥用。
MAX_INSTRUCT_CHARS = 200
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9880
DEFAULT_IDLE_UNLOAD_SECONDS = 300.0

# Returns (model, torch_module, device).  Called under the service lock.
ModelFactory = Callable[[], tuple[Any, Any, str]]


@dataclass(slots=True)
class SynthesisResult:
    wav: bytes
    duration_s: float
    latency_s: float
    sample_rate: int


def _iso_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return (
        datetime.fromtimestamp(float(value), timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# 语速只在很窄的范围内有意义：低于 0.8 会拖成念课文，高于 1.3 开始吞字。
# 越界不报错，夹住就好 —— 语音失败比"没那么快"更糟。
MIN_SPEED = 0.8
MAX_SPEED = 1.3

# CosyVoice3 的 LLM 里有一句硬断言（llm.py:479）：
#   assert 151646 in text, '<|endofprompt|> not detected ...'
# 也就是「拼给模型的 text+prompt_text 里必须出现 <|endofprompt|> 这个 token」。
# zero_shot 天然满足（PROMPT_TEXT 就带），instruct2 不满足 —— instruct 字符串
# 整个充当 prompt_text，官方示例是 `'用四川话说这句话<|endofprompt|>'`，
# 我们原来只发措辞、不带这一截。后果不是"语气不生效"，是**线程里断言失败**：
# llm_job 死掉 -> 音频 token 一个都没生成 -> token2wav 在空张量上 interpolate
# -> 「Input and output sizes should be greater than 0」-> HTTP 503。
# 也就是 lively 这一档语气在真机上必然失败。所以把它收在适配层：
# 任何调用方都不用知道这个 token，补齐由我们负责。
ENDOFPROMPT = "<|endofprompt|>"


def _normalize_instruct(value: Any) -> str:
    """把 instruct 规范成 CosyVoice3 认的样子（结尾补 <|endofprompt|>）。

    已经带了就不重复补：重复出现不会有语法错，但会把 prompt 拉长、
    挤占 LLM 的上下文预算。空字符串保持空 —— 那表示"没有语气"。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.endswith(ENDOFPROMPT):
        return text
    return text + ENDOFPROMPT


def _clamp_speed(value: Any) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return SPEED
    if math.isnan(rate) or math.isinf(rate):
        return SPEED
    return max(MIN_SPEED, min(MAX_SPEED, rate))


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


def _iter_floats(value: Any) -> Iterator[float]:
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_floats(item)
    else:
        yield float(value)


def _chunk_samples(chunk: Any) -> list[float]:
    """Turn a torch/numpy/list chunk into a flat list of floats."""
    if hasattr(chunk, "detach"):
        chunk = chunk.detach().cpu().float()
    if hasattr(chunk, "dim") and chunk.dim() == 2:
        chunk = chunk.squeeze(0)
    elif hasattr(chunk, "ndim") and getattr(chunk, "ndim", 0) == 2:
        chunk = chunk.squeeze(0)
    if hasattr(chunk, "tolist"):
        chunk = chunk.tolist()
    return list(_iter_floats(chunk))


def encode_wav(samples: list[float], sample_rate: int = SAMPLE_RATE) -> bytes:
    """Peak-normalize to 0.9 and encode mono 16-bit PCM WAV."""
    if not samples:
        raise ValueError("empty_audio")
    peak = max(abs(value) for value in samples)
    scale = (PEAK / peak) if peak > 0 else 0.0
    pcm = array.array(
        "h",
        (
            int(round(max(-1.0, min(1.0, value * scale)) * 32767.0))
            for value in samples
        ),
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


class SoftTTSService:
    """Model holder, counters, lazy loading and the single inference lock.

    Threading model:

    * ``self.lock`` serializes model load, unload and synthesis.  A request
      that arrives while the model is loading waits here and then uses the
      freshly loaded model instead of failing.
    * ``self._stats_lock`` protects counters and idle timestamps and is never
      held while calling into the model or torch.
    * the idle watchdog is a daemon ``threading.Timer`` rescheduled after each
      request.  It re-checks in-flight work and idle time under both locks
      before unloading, so it cannot interrupt a running synthesis.
    """

    def __init__(
        self,
        model: Any = None,
        *,
        model_factory: ModelFactory | None = None,
        ref: Path | str = REF_PATH,
        prompt_text: str = PROMPT_TEXT,
        model_label: str | None = None,
        torch_module: Any = None,
        device: str = "cpu",
        load_seconds: float = 0.0,
        speed: float = SPEED,
        sample_rate: int | None = None,
        lazy_load: bool = False,
        idle_unload_seconds: float = DEFAULT_IDLE_UNLOAD_SECONDS,
        clock: Callable[[], float] | None = None,
        instruct_enabled: bool = True,
        text_frontend: bool = True,
        vram_guard: Callable[[], Any] | None = None,
    ) -> None:
        self.model = model
        self._model_factory = model_factory
        self.ref = Path(ref)
        self.prompt_text = prompt_text
        self.model_label = str(model_label or getattr(model, "model_path", MODEL_DIR))
        self.torch = torch_module
        self.device = device
        self.load_seconds = float(load_seconds)
        self.speed = float(speed)
        # 语气：走 inference_instruct2。关掉就只剩 zero-shot 一条参考音，
        # 也就是接这个之前的行为 —— 留一条命令行退路，别让一个参数把服务锁死。
        self.instruct_enabled = bool(instruct_enabled)
        # 断句：交给模型自己的文本前端（按标点断、把「～」「……」念顺），
        # 而不是调用方按 20 字硬切完再拼。默认开，可用 --no-text-frontend 关。
        self.text_frontend = bool(text_frontend)
        # A context manager factory held only while the weights are being
        # allocated.  PhotoService holds the same lease for the whole ComfyUI
        # job, so a /tts arriving mid-photo fails fast instead of allocating a
        # second model and taking the card down with an OOM.
        self._vram_guard = vram_guard
        self._sample_rate_override = sample_rate
        self.sample_rate = int(
            sample_rate or getattr(model, "sample_rate", SAMPLE_RATE) or SAMPLE_RATE
        )
        self.lazy_load = bool(lazy_load)
        self.idle_unload_seconds = max(0.0, float(idle_unload_seconds))
        self.lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._timer_lock = threading.Lock()
        self._idle_timer: threading.Timer | None = None
        self._closed = False
        self._in_flight = 0
        self.requests = 0
        self.errors = 0
        self.loads = 1 if model is not None else 0
        self.unloads = 0
        self.last_request_at: float | None = None
        self.last_load_at: float | None = time.time() if model is not None else None
        self.last_unload_at: float | None = None
        self.last_unload_reason: str | None = None
        self._last_request_mono = self._clock()

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def note_request(self) -> None:
        with self._stats_lock:
            self.requests += 1

    def begin_request(self) -> None:
        """Count a request and mark the service busy before it waits for a lock."""
        now = time.time()
        mono = self._clock()
        with self._stats_lock:
            self.requests += 1
            self._in_flight += 1
            self.last_request_at = now
            self._last_request_mono = mono

    def end_request(self) -> None:
        with self._stats_lock:
            if self._in_flight > 0:
                self._in_flight -= 1
            self.last_request_at = time.time()
            self._last_request_mono = self._clock()
        if self.idle_unload_seconds > 0 and self.model is not None:
            self._schedule_idle_unload()

    def note_error(self) -> None:
        with self._stats_lock:
            self.errors += 1

    def health(self) -> dict[str, Any]:
        with self._stats_lock:
            requests = int(self.requests)
            errors = int(self.errors)
            loads = int(self.loads)
            unloads = int(self.unloads)
            last_request_at = self.last_request_at
            last_load_at = self.last_load_at
            last_unload_at = self.last_unload_at
            last_unload_reason = self.last_unload_reason
            in_flight = int(self._in_flight)
        return {
            # ok describes the HTTP service, not the model: an unloaded model
            # is the normal steady state with idle unloading enabled.
            "ok": True,
            "engine": "cosyvoice",
            "model": self.model_label,
            "ref": str(self.ref),
            "loaded": self.model is not None,
            "device": self.device,
            "sample_rate": self.sample_rate,
            "requests": requests,
            "errors": errors,
            "loads": loads,
            "unloads": unloads,
            "load_seconds": round(float(self.load_seconds), 3),
            "last_request_at": _iso_timestamp(last_request_at),
            "last_load_at": _iso_timestamp(last_load_at),
            "last_unload_at": _iso_timestamp(last_unload_at),
            "last_unload_reason": last_unload_reason,
            "idle_unload_seconds": float(self.idle_unload_seconds),
            "lazy_load": self.lazy_load,
            "in_flight": in_flight,
            # 客户端据此判断「这个服务到底会不会听 instruct」，而不是猜。
            "instruct": self.instruct_enabled,
            "text_frontend": self.text_frontend,
            "speed": float(self.speed),
        }

    def _release_cuda_cache(self) -> None:
        """Best-effort cleanup after dropping the model reference."""
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

    def _ensure_loaded_locked(self) -> float:
        """Load through the factory if needed. Callers must hold ``self.lock``."""
        if self.model is not None:
            return self.load_seconds
        factory = self._model_factory
        if factory is None:
            raise RuntimeError("model_not_loaded")
        started = time.perf_counter()
        try:
            with contextlib.ExitStack() as stack:
                if self._vram_guard is not None:
                    stack.enter_context(self._vram_guard())
                model, torch_module, device = factory()
        except Exception:
            self._release_cuda_cache()
            raise
        load_seconds = time.perf_counter() - started
        if model is None:
            self._release_cuda_cache()
            raise RuntimeError("model_factory_returned_none")
        self.model = model
        if torch_module is not None:
            self.torch = torch_module
        if device:
            self.device = str(device)
        self.load_seconds = load_seconds
        if self._sample_rate_override is None:
            self.sample_rate = int(
                getattr(model, "sample_rate", SAMPLE_RATE) or SAMPLE_RATE
            )
        with self._stats_lock:
            self.loads += 1
            self.last_load_at = time.time()
        return load_seconds

    def load(self) -> float:
        """Load the model now; return the load duration in seconds."""
        with self.lock:
            seconds = self._ensure_loaded_locked()
        # A manual load is not a request, so without this the model would stay
        # resident forever if no /tts followed.
        self._schedule_idle_unload()
        return seconds

    def _unload_locked(self, reason: str) -> bool:
        if self.model is None:
            self._release_cuda_cache()
            return False
        self.model = None
        self._release_cuda_cache()
        with self._stats_lock:
            self.unloads += 1
            self.last_unload_at = time.time()
            self.last_unload_reason = reason
        return True

    def unload(self) -> bool:
        """Release the model now; return True if a loaded model was released."""
        with self.lock:
            return self._unload_locked("manual")

    def start_idle_watchdog(self) -> None:
        if self.idle_unload_seconds > 0:
            self._schedule_idle_unload(self.idle_unload_seconds)

    def _schedule_idle_unload(self, delay: float | None = None) -> None:
        if self.idle_unload_seconds <= 0:
            return
        wait = self.idle_unload_seconds if delay is None else max(0.01, float(delay))
        with self._timer_lock:
            if self._closed:
                return
            previous = self._idle_timer
            timer = threading.Timer(wait, self._idle_tick)
            timer.daemon = True
            self._idle_timer = timer
            timer.start()
        if previous is not None:
            previous.cancel()

    def _idle_tick(self) -> None:
        if self._closed or self.idle_unload_seconds <= 0:
            return
        with self._stats_lock:
            in_flight = int(self._in_flight)
            last_mono = float(self._last_request_mono)
        idle_for = self._clock() - last_mono
        if in_flight > 0 or idle_for < self.idle_unload_seconds:
            if in_flight > 0:
                wait = self.idle_unload_seconds
            else:
                wait = max(0.01, self.idle_unload_seconds - idle_for)
            self._schedule_idle_unload(wait)
            return

        reschedule = False
        with self.lock:
            with self._stats_lock:
                in_flight = int(self._in_flight)
                last_mono = float(self._last_request_mono)
            if self.model is None:
                return
            if in_flight > 0 or (self._clock() - last_mono) < self.idle_unload_seconds:
                # A request arrived between the first check and this lock.
                reschedule = True
            else:
                self._unload_locked("idle")
        if reschedule:
            with self._stats_lock:
                idle_for = self._clock() - float(self._last_request_mono)
            self._schedule_idle_unload(max(0.01, self.idle_unload_seconds - idle_for))

    def close(self) -> None:
        """Stop the idle watchdog (does not unload; used on shutdown and tests)."""
        with self._timer_lock:
            self._closed = True
            timer = self._idle_timer
            self._idle_timer = None
        if timer is not None:
            timer.cancel()

    def synthesize(
        self,
        text: str,
        *,
        instruct: str | None = None,
        text_frontend: bool | None = None,
        speed: float | None = None,
    ) -> SynthesisResult:
        """Synthesize one utterance. Callers must hold ``self.lock``.

        If the model was unloaded, it is loaded again here while the lock is
        held, so concurrent requests share one load and are never dropped.

        ``instruct`` switches to ``inference_instruct2``: that is the only path
        in CosyVoice3 that changes *how* a sentence is said (tone, pace) rather
        than just what it says. Everything else about the voice -- reference
        audio, prompt text, seed -- stays identical, so two calls with different
        instruct are still the same speaker.

        ``text_frontend`` lets the model's own normaliser split the sentence and
        read ``～``/``……``/``、`` properly. With it off, the caller's hard 20-char
        chunks are what the model sees, which is why clipped-sounding joins
        happen at chunk boundaries.
        """
        self._ensure_loaded_locked()
        if self.model is None:
            raise RuntimeError("model_not_loaded")
        # 补齐 <|endofprompt|>：不补的话真机上一定 503，不是"语气不生效"而已。
        instruct = _normalize_instruct(instruct)
        use_instruct = bool(instruct) and self.instruct_enabled
        frontend = self.text_frontend if text_frontend is None else bool(text_frontend)
        rate = _clamp_speed(self.speed if speed is None else speed)
        started = time.perf_counter()
        context: Any = contextlib.nullcontext()
        if self.torch is not None:
            self.torch.manual_seed(SEED)
            context = self.torch.inference_mode()
        chunks: list[Any] = []
        with context:
            if use_instruct:
                stream = self.model.inference_instruct2(
                    text,
                    str(instruct),
                    str(self.ref),
                    stream=False,
                    speed=rate,
                    text_frontend=frontend,
                )
            else:
                stream = self.model.inference_zero_shot(
                    text,
                    self.prompt_text,
                    str(self.ref),
                    stream=False,
                    speed=rate,
                    text_frontend=frontend,
                )
            for out in stream:
                chunks.append(out["tts_speech"])
        samples: list[float] = []
        for chunk in chunks:
            samples.extend(_chunk_samples(chunk))
        wav = encode_wav(samples, self.sample_rate)
        latency = time.perf_counter() - started
        return SynthesisResult(
            wav=wav,
            duration_s=len(samples) / float(self.sample_rate),
            latency_s=latency,
            sample_rate=self.sample_rate,
        )


def create_app(
    model: Any = None,
    *,
    model_factory: ModelFactory | None = None,
    ref: Path | str = REF_PATH,
    prompt_text: str = PROMPT_TEXT,
    model_label: str | None = None,
    torch_module: Any = None,
    device: str = "cpu",
    load_seconds: float = 0.0,
    speed: float = SPEED,
    sample_rate: int | None = None,
    lazy_load: bool = False,
    idle_unload_seconds: float = DEFAULT_IDLE_UNLOAD_SECONDS,
    clock: Callable[[], float] | None = None,
    instruct_enabled: bool = True,
    text_frontend: bool = True,
    vram_guard: Callable[[], Any] | None = None,
) -> SoftTTSService:
    """Build the service state.

    Tests inject a fake ``model`` directly, or a fake ``model_factory`` for
    lazy-load/unload tests; no unit test imports torch or touches the GPU.
    """
    return SoftTTSService(
        model,
        model_factory=model_factory,
        ref=ref,
        prompt_text=prompt_text,
        model_label=model_label,
        torch_module=torch_module,
        device=device,
        load_seconds=load_seconds,
        speed=speed,
        sample_rate=sample_rate,
        lazy_load=lazy_load,
        idle_unload_seconds=idle_unload_seconds,
        clock=clock,
        instruct_enabled=instruct_enabled,
        text_frontend=text_frontend,
        vram_guard=vram_guard,
    )


def build_handler(service: SoftTTSService) -> type[BaseHTTPRequestHandler]:
    """Return a request handler bound to ``service`` (the second test seam)."""

    class TTSHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "FatfishTTSSoft/1.0"
        sys_version = ""
        timeout = 60

        def log_message(self, fmt: str, *args: Any) -> None:
            # Intentionally silent: request bodies contain user text and must
            # never reach access logs. Startup messages are printed by main().
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
                # The runtime may time out or cancel; never print a traceback
                # and never leak request text through an error dump.
                pass

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._respond(status, raw, "application/json; charset=utf-8")

        def _model_error(self, exc: Exception) -> None:
            service.note_error()
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
                # Control endpoints can force GPU allocations/releases and are
                # therefore limited to loopback clients.
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

            # /tts is always counted, including validation failures, and the
            # in-flight marker keeps the idle watchdog away while it runs.
            service.begin_request()
            try:
                self._handle_tts()
            finally:
                service.end_request()

        def _handle_tts(self) -> None:
            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header) if length_header is not None else -1
            except (TypeError, ValueError):
                length = -1
            if length < 0:
                service.note_error()
                self._json(400, {"ok": False, "error": "content_length_required"})
                return
            if length > MAX_BODY_BYTES:
                service.note_error()
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
                service.note_error()
                self._json(400, {"ok": False, "error": "incomplete_body"})
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                service.note_error()
                self._json(400, {"ok": False, "error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                service.note_error()
                self._json(400, {"ok": False, "error": "json_object_required"})
                return

            text = payload.get("text")
            if not isinstance(text, str):
                service.note_error()
                self._json(400, {"ok": False, "error": "text_required"})
                return
            text = text.strip()
            if not text:
                service.note_error()
                self._json(400, {"ok": False, "error": "text_empty"})
                return
            if len(text) > MAX_TEXT_CHARS:
                service.note_error()
                self._json(
                    400,
                    {
                        "ok": False,
                        "error": "text_too_long",
                        "limit": MAX_TEXT_CHARS,
                    },
                )
                return

            # 语气与断句是**两个**独立的东西，别混成一个参数：
            #   instruct      —— 走 inference_instruct2，唯一能改变"怎么说"的通道
            #   text_frontend —— 让模型自己的前端按标点断句、把「～」「……」念顺
            #   speed         —— 窄幅夹取，越界不报错（语音失败比"没那么快"更糟）
            # voice/format 仍然只为接口契约而收下：单说话人、固定 WAV。
            instruct_raw = payload.get("instruct")
            instruct: str | None = None
            if instruct_raw is not None:
                if not isinstance(instruct_raw, str):
                    service.note_error()
                    self._json(400, {"ok": False, "error": "instruct_must_be_string"})
                    return
                instruct = instruct_raw.strip()
                if len(instruct) > MAX_INSTRUCT_CHARS:
                    service.note_error()
                    self._json(
                        400,
                        {
                            "ok": False,
                            "error": "instruct_too_long",
                            "limit": MAX_INSTRUCT_CHARS,
                        },
                    )
                    return
                if not instruct:
                    instruct = None

            frontend_raw = payload.get("text_frontend")
            frontend: bool | None
            if frontend_raw is None:
                frontend = None
            elif isinstance(frontend_raw, bool):
                frontend = frontend_raw
            else:
                service.note_error()
                self._json(
                    400, {"ok": False, "error": "text_frontend_must_be_boolean"}
                )
                return

            try:
                with service.lock:
                    result = service.synthesize(
                        text,
                        instruct=instruct,
                        text_frontend=frontend,
                        speed=payload.get("speed"),
                    )
            except Exception as exc:  # noqa: BLE001 - convert all model failures
                self._model_error(exc)
                return

            self._respond(200, result.wav, "audio/wav")

    TTSHandler.service = service  # type: ignore[attr-defined]
    return TTSHandler


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would allow two servers to bind the same port.
    allow_reuse_address = False


def make_server(host: str, port: int, service: SoftTTSService) -> ThreadingHTTPServer:
    return _ThreadingServer((host, int(port)), build_handler(service))


def port_in_use(host: str, port: int) -> bool:
    """Return True if the address already has a listener.

    A plain connect is tried first for a fast positive. When the occupant has
    a full accept backlog the connect can time out, so an exclusive bind probe
    is used as the authoritative check.
    """
    target = host
    if host in ("", "0.0.0.0"):
        target = "127.0.0.1"
    elif host == "::":
        target = "::1"
    try:
        with socket.create_connection((target, int(port)), timeout=0.35):
            return True
    except OSError:
        pass

    bind_host = host or "127.0.0.1"
    try:
        infos = socket.getaddrinfo(
            bind_host, int(port), type=socket.SOCK_STREAM
        )
    except OSError:
        return True
    for family, sock_type, proto, _, sockaddr in infos:
        try:
            with socket.socket(family, sock_type, proto) as probe:
                # No SO_REUSEADDR: binding an occupied port must fail.
                probe.bind(sockaddr)
        except OSError:
            continue
        else:
            return False
    return True


def _quiet_library_logs() -> None:
    """CosyVoice logs the synthesized text at INFO/WARNING on the root logger."""
    root = logging.getLogger()
    root.setLevel(logging.ERROR)
    for name in ("cosyvoice", "modelscope", "wetext"):
        logging.getLogger(name).setLevel(logging.ERROR)


def load_model(
    model_dir: Path | str = MODEL_DIR, cosy_dir: Path | str = COSY_DIR
) -> tuple[Any, Any, str]:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    cosy = Path(cosy_dir)
    for path in (cosy / "third_party" / "Matcha-TTS", cosy):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
    from cosyvoice.cli.cosyvoice import AutoModel  # type: ignore[import-not-found]
    import torch  # type: ignore[import-not-found]

    _quiet_library_logs()
    model = AutoModel(model_dir=str(model_dir), fp16=False)
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    return model, torch, device


def check_paths(model_dir: Path | str, ref: Path | str) -> list[str]:
    missing: list[str] = []
    model_path = Path(model_dir)
    ref_path = Path(ref)
    if not model_path.is_dir():
        missing.append(str(model_path))
    elif not (model_path / "llm.pt").is_file():
        missing.append(str(model_path / "llm.pt"))
    if not ref_path.is_file():
        missing.append(str(ref_path))
    return missing


def write_pid_file(path: Path | str = PID_PATH, pid: int | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(pid or os.getpid()), encoding="ascii")


def remove_pid_file(path: Path | str = PID_PATH, pid: int | None = None) -> None:
    target = Path(path)
    try:
        if target.read_text(encoding="ascii").strip() == str(pid or os.getpid()):
            target.unlink()
    except OSError:
        pass


def _vram_guard() -> Any:
    """The shared TTS/ComfyUI VRAM lease.

    PhotoService holds it for the whole photo job; a TTS load attempts to take
    it for the duration of the weight allocation.  ``held`` raises ``LockBusy``
    after 0.5 s, which the handler turns into a 503 so the caller can fall back
    to text instead of letting both models allocate and OOM.
    """
    return held(lock_path(DATA_DIR, "vram"), timeout=0.5)


def _service_from_args(
    args: argparse.Namespace,
    model: Any,
    torch_module: Any,
    device: str,
    load_seconds: float,
    *,
    model_factory: ModelFactory | None = None,
    lazy_load: bool = False,
) -> SoftTTSService:
    return create_app(
        model=model,
        model_factory=model_factory,
        ref=args.ref,
        model_label=str(args.model),
        torch_module=torch_module,
        device=device,
        load_seconds=load_seconds,
        lazy_load=lazy_load,
        idle_unload_seconds=float(args.idle_unload_seconds),
        instruct_enabled=bool(getattr(args, "instruct", True)),
        text_frontend=bool(getattr(args, "text_frontend", True)),
        vram_guard=_vram_guard,
    )


def _guarded(name: str, fn: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> int:
    """Run a model-owning entry point under the single-TTS lock."""
    lock = acquire(lock_path(DATA_DIR, "tts"))
    if lock is None:
        print(
            f"[tts-soft] another TTS process already holds "
            f"{lock_path(DATA_DIR, 'tts')}; not starting a second {name}.",
            file=sys.stderr,
        )
        return 2
    try:
        return fn(args)
    finally:
        lock.release()


def _run_self_test_locked(args: argparse.Namespace) -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    missing = check_paths(args.model, args.ref)
    if missing:
        print("[tts-soft] missing required path(s): " + ", ".join(missing), file=sys.stderr)
        return 2
    print("[tts-soft] self-test loading model (fp32) ...", flush=True)
    started = time.perf_counter()
    try:
        with _vram_guard():
            model, torch_module, device = load_model(args.model)
        load_seconds = time.perf_counter() - started
        service = _service_from_args(args, model, torch_module, device, load_seconds)
        print(
            f"[tts-soft] model loaded in {load_seconds:.2f}s device={device}",
            flush=True,
        )
        with service.lock:
            result = service.synthesize(SELF_TEST_TEXT)
    except Exception as exc:  # noqa: BLE001 - report model/CUDA failures verbatim
        print(f"[tts-soft] self-test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    SMOKE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SMOKE_PATH.write_bytes(result.wav)
    valid = False
    frames = sample_rate = channels = 0
    try:
        with wave.open(io.BytesIO(result.wav), "rb") as handle:
            channels = handle.getnchannels()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            valid = frames > 0 and sample_rate == service.sample_rate and channels == 1
    except (wave.Error, EOFError):
        valid = False
    print(
        "[tts-soft] self-test "
        f"load_s={load_seconds:.2f} latency_s={result.latency_s:.2f} "
        f"audio_s={result.duration_s:.2f}",
        flush=True,
    )
    print(
        f"[tts-soft] self-test wav={SMOKE_PATH} bytes={len(result.wav)} "
        f"frames={frames} rate={sample_rate} channels={channels} valid={valid}",
        flush=True,
    )
    return 0 if valid else 1


def _run_server_locked(args: argparse.Namespace) -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    missing = check_paths(args.model, args.ref)
    if missing:
        print("[tts-soft] missing required path(s): " + ", ".join(missing), file=sys.stderr)
        return 2
    if port_in_use(args.host, args.port):
        print(
            f"[tts-soft] port {args.host}:{args.port} is already in use; "
            "a TTS server is probably running. Not starting a second server.",
            file=sys.stderr,
        )
        return 2

    if args.lazy_load:
        print(
            "[tts-soft] lazy load enabled; the model will load on the first /tts "
            f"request and unload after {args.idle_unload_seconds:g}s idle.",
            flush=True,
        )
        service = _service_from_args(
            args,
            None,
            None,
            "unknown",
            0.0,
            model_factory=lambda: load_model(args.model),
            lazy_load=True,
        )
        load_seconds = 0.0
        device = "unloaded"
    else:
        print("[tts-soft] loading model (fp32) ...", flush=True)
        started = time.perf_counter()
        try:
            with _vram_guard():
                model, torch_module, device = load_model(args.model)
            load_seconds = time.perf_counter() - started
            service = _service_from_args(
                args, model, torch_module, device, load_seconds
            )
        except Exception as exc:  # noqa: BLE001 - model load failures are fatal
            print(
                f"[tts-soft] failed to load model: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1

        if not args.no_warmup:
            print(
                f"[tts-soft] model loaded in {load_seconds:.2f}s; warming up ...",
                flush=True,
            )
            warm_started = time.perf_counter()
            try:
                with service.lock:
                    service.synthesize(WARMUP_TEXT)
                print(
                    f"[tts-soft] warmup done in "
                    f"{time.perf_counter() - warm_started:.2f}s",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - keep serving and report
                print(
                    f"[tts-soft] warmup failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    try:
        httpd = make_server(args.host, args.port, service)
    except OSError as exc:
        service.close()
        print(
            f"[tts-soft] cannot bind {args.host}:{args.port}: {exc}. "
            "Another process is using this port; not starting a second server.",
            file=sys.stderr,
        )
        return 2

    service.start_idle_watchdog()
    write_pid_file(PID_PATH)
    url_host = "127.0.0.1" if args.host in ("", "0.0.0.0") else args.host
    mode = "lazy" if args.lazy_load else "warm"
    print(
        f"[tts-soft] ready at http://{url_host}:{args.port}/tts "
        f"(health: http://{url_host}:{args.port}/health) "
        f"mode={mode} load={load_seconds:.2f}s device={device} "
        f"idle_unload={args.idle_unload_seconds:g}s ref={Path(args.ref).name}",
        flush=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[tts-soft] Ctrl+C received, stopping ...", flush=True)
    finally:
        service.close()
        httpd.server_close()
        remove_pid_file(PID_PATH)
    return 0


def run_self_test(args: argparse.Namespace) -> int:
    return _guarded("self-test", _run_self_test_locked, args)


def run_server(args: argparse.Namespace) -> int:
    return _guarded("server", _run_server_locked, args)


def _non_negative_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be >= 0 (0 disables)")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local CosyVoice3 TTS server (soft ref_base_x2 voice)."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--model", default=str(MODEL_DIR))
    parser.add_argument("--ref", default=str(REF_PATH))
    parser.add_argument(
        "--lazy-load",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "load the model on the first /tts request instead of at startup "
            "(default: on; use --no-lazy-load for the old warm-start behaviour)"
        ),
    )
    parser.add_argument(
        "--idle-unload-seconds",
        type=_non_negative_seconds,
        default=DEFAULT_IDLE_UNLOAD_SECONDS,
        metavar="SECONDS",
        help=(
            "unload the model after this many seconds without requests; "
            f"0 disables (default: {DEFAULT_IDLE_UNLOAD_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="skip the startup warmup request (only applies with --no-lazy-load)",
    )
    parser.add_argument(
        "--instruct",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "honour the per-request 'instruct' field via inference_instruct2 "
            "(default: on). --no-instruct pins every request to zero-shot, which "
            "is exactly the pre-instruct behaviour and the fallback if a tone "
            "sounds wrong."
        ),
    )
    parser.add_argument(
        "--text-frontend",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "let the model normalise and split text by punctuation "
            "(default: on). --no-text-frontend restores raw 20-char chunks."
        ),
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="load the model, synthesize one smoke line, then exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test(args)
    return run_server(args)


if __name__ == "__main__":
    raise SystemExit(main())
