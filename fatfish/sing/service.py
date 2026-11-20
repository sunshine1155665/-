"""On-demand singing: one YuE2→RVC render per request, serialized on the 8 GB card.

Design constraints
------------------
* The application itself stays stdlib-only: the singing models live in
  ``.venv-soulx`` / ``.venv-train`` and are invoked as subprocesses
  (``soulx/song_yue2_rvc.py`` by default).
* One song at a time.  A second concurrent request returns ``busy`` instead of
  queueing unbounded GPU work.  The lease is process-wide and, via a file lock
  in ``data/run``, shared between the web and QQ processes.
* TTS and the singer cannot both hold the card.  Before rendering we take the same
  ``vram.lock`` the photo service uses, cooperatively unload TTS and, once the
  song is written, reload it in ``finally`` (also on failure/timeout).
* The lyrics never come from the network here.  The model may pass a complete
  song (4–16 sung lines, optional section tags); invalid lyrics fall back to
  the shipped persona song instead of failing the turn, and the reason is
  recorded on the job row.
* No arbitrary shell: the only process spawned is the configured python +
  script pair.

Two modes (T-0067)
------------------
``sing.mode = "local"`` (default) renders here.  ``"remote"`` means this
process runs on the cloud host, which has no GPU: the song is POSTed to the
home ``scripts/sing_server.py``, which owns the card and the TTS handoff.  The
cloud still writes its own ``sing_jobs`` row and its own ``data/songs/<id>``
copy, so QQ/Web delivery is unchanged.

A remote failure must not cost the user 600 seconds of silence.  Before the
POST we do a 2s TCP probe: unreachable -> ``home_offline`` immediately (text
only), same idea as the TTS capability probe.  The render itself is bounded by
``sing.remote_timeout_seconds``, which sits ~30s above the server's own render
timeout so the server's JSON/504 arrives first.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..config import Config, env_secret
from ..db import Database
from ..process_lock import acquire, lock_path

# ``Runtime`` is built per chat turn in the web process, so an instance lock is
# not enough: two turns could both start a render.  The module lock covers this
# process, the file lock covers the QQ process as well.
_PROCESS_JOB_LOCK = threading.Lock()

DEFAULT_EMOTIONS = ("soft", "genki", "sad")
#: Server-side render budget (scripts/sing_server.py --timeout).  YuE2→RVC
#: takes ~5-6 minutes; 10 minutes is the "something hung" line.
DEFAULT_REMOTE_TIMEOUT = 600.0
#: The client waits a little longer than the server so a server-side timeout
#: arrives as JSON instead of a client-side socket timeout.
REMOTE_TIMEOUT_GRACE = 30.0
#: TCP-only probe before uploading the request.
DEFAULT_REMOTE_CONNECT_TIMEOUT = 2.0
_MIN_WAV_BYTES = 44
_MIN_LINE_CHARS = 4
_MAX_LINE_CHARS = 16
#: ADR-0018: ``lyrics`` is a *complete* song now, not a fixed four-liner.
#: 4–16 sung lines matches what YuE2 full generation handles well (the 16-line
#: 2:07 birthday song rendered fine; more risks truncation).
_MIN_SUNG_LINES = 4
_MAX_SUNG_LINES = 16
#: Section tags the model may use to structure a full song.  Anything else in
#: brackets is treated as a sung line (and will fail the length/count checks).
SECTION_TAGS = frozenset(
    {"verse", "chorus", "bridge", "intro", "outro", "hook", "refrain",
     "pre-chorus", "prechorus"}
)
_ANSI_RE = None


@dataclass
class RenderRequest:
    """Everything the subprocess needs; the runner is injectable for tests."""

    command: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path
    output_dir: Path
    timeout: float


@dataclass
class SingResult:
    ok: bool
    path: str | None = None
    emotion: str = "soft"
    job_id: str = ""
    elapsed_ms: float = 0.0
    error: str = ""
    lyrics: list[str] = field(default_factory=list)
    lyrics_error: str = ""
    tts_unloaded: bool = False
    tts_reloaded: bool = False
    title: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "emotion": self.emotion,
            "job_id": self.job_id,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "lyrics": list(self.lyrics),
            "lyrics_error": self.lyrics_error,
            "tts_unloaded": self.tts_unloaded,
            "tts_reloaded": self.tts_reloaded,
            "title": self.title,
        }


def friendly_sing_error(code: str) -> str:
    """One Chinese sentence for every failure the service can report."""
    code = str(code or "").strip()
    if code == "disabled":
        return "本鲸鱼娘现在嗓子还没开（唱歌功能没启用）。"
    if code in {"busy", "sing_busy"}:
        return "本鲸鱼娘正在唱上一首呢，等一小会儿再点歌好不好？"
    if code == "vram_busy":
        return "显卡正忙着呢（TTS 正在加载或另一首在唱），等一下再唱给你听。"
    if code == "vram_conflict_tts":
        return "显存不够开嗓：TTS 还占着显卡，先卸载 TTS 再点歌吧。"
    if code.startswith("python_missing"):
        return "唱歌的运行环境没装好（.venv-soulx 找不到）。"
    if code.startswith("script_missing"):
        return "唱歌脚本不在（soulx/song_yue2_rvc.py）。"
    if code.startswith("lyrics_src_missing"):
        return "歌词底稿不在，这次唱不了。"
    if code.startswith("missing_assets"):
        return "唱歌要用的模型/资源缺了，先去检查 renderer 配置。"
    if code == "render_timeout":
        return "这首歌录到一半超时了，等下再点一次。"
    if code == "memory_not_enough":
        return "现在内存/显存有点紧，先不唱了，等会儿再点一次。"
    if code.startswith("render_failed"):
        return "这次没唱好，等下再点一次。"
    if code == "sing_storage_error":
        return "唱歌记录没存下来，这次先不唱了。"
    # -- 云端（T-0067）：云主机自己不唱，歌在家里那台 GPU 机上渲染。
    if code == "home_offline":
        return "家里那台唱歌的机器这会儿没连上，等我回到家再唱给你听好不好？"
    if code == "remote_not_configured":
        return "云端还没配好唱歌的地址（sing.remote_url），这次先不唱了。"
    if code == "remote_timeout":
        return "家里那边唱太久没回音，这次先算了，等下再点一次。"
    if code == "remote_bad_audio":
        return "家里传回来的歌声是坏的，这次先不唱了。"
    if code.startswith("remote_error"):
        return "家里唱歌的服务出了点岔子，等下再点一次。"
    return "这次没唱成，等下再点一次。"


def is_section_tag(line: str) -> bool:
    """``"[Chorus]"`` -> True; anything else in brackets is a sung line."""
    text = line.strip()
    if not (text.startswith("[") and text.endswith("]") and len(text) > 2):
        return False
    return text[1:-1].strip().lower().replace(" ", "") in SECTION_TAGS


def clean_lyrics(raw: Any) -> tuple[list[str], str]:
    """Return ``(lines, error)``; empty lines mean "sing the default song".

    ADR-0018: ``lyrics`` carries a *complete* song — 4–16 sung lines of 4–16
    characters each, optionally structured with ``[Verse]``/``[Chorus]`` tags.
    The tags are kept in the returned lines (the renderer forwards them to
    YuE2 verbatim) but do not count towards the sung-line budget.
    """
    if raw is None or raw == "" or raw == []:
        return [], ""
    if isinstance(raw, str):
        items = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return [], "lyrics_bad_shape"
    lines = [" ".join(str(item or "").split()) for item in items]
    lines = [line for line in lines if line]
    sung = [line for line in lines if not is_section_tag(line)]
    if not _MIN_SUNG_LINES <= len(sung) <= _MAX_SUNG_LINES:
        return [], "lyrics_bad_count"
    if any(
        len(line) < _MIN_LINE_CHARS or len(line) > _MAX_LINE_CHARS
        for line in sung
    ):
        return [], "lyrics_bad_length"
    return lines, ""


class TTSHandoff:
    """The cooperative TTS handoff contract (POST /unload, POST /load).

    Public because ``scripts/sing_server.py`` asks the same endpoint for
    ``/health`` (``loaded``) without going through a second copy of the URL
    parsing.
    """

    def __init__(self, config: Config):
        raw = str(config.get("tts.base_url", "") or "").strip()
        parts = urlsplit(raw if "://" in raw else "http://127.0.0.1:9880/tts")
        scheme = parts.scheme or "http"
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 9880
        self.base_url = f"{scheme}://{host}:{port}"

    def reachable(self) -> bool:
        parts = urlsplit(self.base_url)
        try:
            with socket.create_connection(
                (parts.hostname or "127.0.0.1", parts.port or 80), timeout=1.0
            ):
                return True
        except OSError:
            return False

    def loaded(self) -> bool | None:
        try:
            data = self._json("GET", "/health", timeout=2.0)
        except OSError:
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("loaded")
        return value if isinstance(value, bool) else None

    def unload(self) -> bool:
        try:
            data = self._json("POST", "/unload", {}, timeout=15.0)
        except OSError:
            return False
        return isinstance(data, dict) and data.get("ok") is not False

    def load(self) -> bool:
        try:
            # IndexTTS cold load measured ~71s; a short timeout would abort the
            # HTTP call while the server keeps loading (and keep the reload
            # flag false even though the model does come back).
            data = self._json("POST", "/load", {}, timeout=120.0)
        except OSError:
            return False
        return isinstance(data, dict) and data.get("ok") is not False

    def _json(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
        *, timeout: float,
    ) -> Any:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OSError(str(exc)) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {}


#: Backwards-compatible alias: older code (and tests) import the private name.
_TTSBridge = TTSHandoff


class RemoteSingerClient:
    """Transport to ``scripts/sing_server.py`` on the home machine.

    Two methods, both replaceable in tests: :meth:`reachable` is the cheap
    "is the box awake" probe, :meth:`post` is the one long request.  Nothing
    here knows about wav files or jobs -- :class:`SingService` decides what a
    status code means.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = DEFAULT_REMOTE_TIMEOUT,
        connect_timeout: float = DEFAULT_REMOTE_CONNECT_TIMEOUT,
    ):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "")
        self.timeout = max(5.0, float(timeout))
        self.connect_timeout = max(0.5, float(connect_timeout))

    def _endpoint(self) -> str:
        return self.base_url + "/sing"

    def _host_port(self) -> tuple[str, int] | None:
        parts = urlsplit(self.base_url)
        if not parts.hostname:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.hostname, port

    def reachable(self) -> bool:
        """TCP probe with a short timeout.

        DNS for a Tailscale MagicDNS name can fail outright (``OSError``),
        which is exactly the "home is off" answer we want, not an exception.
        """
        target = self._host_port()
        if target is None:
            return False
        try:
            with socket.create_connection(target, timeout=self.connect_timeout):
                return True
        except OSError:
            return False

    def post(self, payload: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
        """POST ``/sing``.  HTTP errors come back as ``(code, body, headers)``."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "audio/wav, application/json",
        }
        if self.token:
            headers["X-Token"] = self.token
        request = urllib.request.Request(
            self._endpoint(), data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers or {})
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except OSError:
                raw = b""
            return exc.code, raw, dict(exc.headers or {})


class SingService:
    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        runner: Callable[[RenderRequest], tuple[bool, str]] | None = None,
        tts: Any | None = None,
        vram_free: Callable[[], int | None] | None = None,
        remote_client: Any | None = None,
    ):
        self.config = config
        self.db = db
        self.enabled = bool(config.get("sing.enabled", False))
        self.mode = str(config.get("sing.mode", "local") or "local").strip().lower()
        if self.mode not in {"local", "remote"}:
            self.mode = "local"
        self.remote_url = str(config.get("sing.remote_url", "") or "").strip()
        self.remote_timeout = max(
            30.0,
            float(
                config.get("sing.remote_timeout_seconds", DEFAULT_REMOTE_TIMEOUT)
                or DEFAULT_REMOTE_TIMEOUT
            )
            + REMOTE_TIMEOUT_GRACE,
        )
        # Tailscale can be DERP-relayed: a raw TCP connect may take >2s even
        # though the home service is healthy.  Keep the probe generous so it
        # reports "offline" only when the box is really unreachable.
        self.remote_connect_timeout = max(
            1.0,
            float(
                config.get(
                    "sing.remote_connect_timeout_seconds",
                    DEFAULT_REMOTE_CONNECT_TIMEOUT,
                )
                or DEFAULT_REMOTE_CONNECT_TIMEOUT
            ),
        )
        token_env = str(config.get("sing.remote_token_env", "SING_SERVER_TOKEN") or "")
        self.remote_client = remote_client
        if self.remote_client is None and self.mode == "remote" and self.remote_url:
            self.remote_client = RemoteSingerClient(
                self.remote_url,
                token=(env_secret(token_env) or "") if token_env else "",
                timeout=self.remote_timeout,
                connect_timeout=self.remote_connect_timeout,
            )
        self.emotion = str(config.get("sing.emotion", "soft") or "soft")
        self.semitones = float(config.get("sing.semitones", 0.0))
        self.timeout = max(10.0, float(config.get("sing.timeout_seconds", 720)))
        self.min_free_vram_mb = max(
            0, int(config.get("sing.min_free_vram_mb", 5600))
        )
        self.unload_tts = bool(config.get("sing.unload_tts", True))
        self.reload_tts = bool(config.get("sing.reload_tts", True))
        self.vram_wait_seconds = max(
            0.0, float(config.get("sing.vram_wait_seconds", 20.0))
        )
        self._root = Path(__file__).resolve().parents[2]
        self.python = self._path(config.get("sing.python", ""))
        self.script = self._path(config.get("sing.script", ""))
        self.soulx_root = self._path(config.get("sing.soulx_root", ""))
        self.deps_path = self._path(config.get("sing.deps_path", ""))
        self.prompt_wav = self._path(config.get("sing.prompt_wav", ""))
        self.prompt_meta = self._path(config.get("sing.prompt_meta", ""))
        self.default_src = self._path(config.get("sing.default_src", ""))
        self.default_title = "等你回家"
        if self.default_src and self.default_src.is_file():
            try:
                data = json.loads(self.default_src.read_text(encoding="utf-8"))
                self.default_title = str(data.get("title") or self.default_title)
            except (OSError, ValueError):
                pass
        output = str(config.get("sing.output_dir", "songs") or "songs")
        output_path = Path(output)
        if not output_path.is_absolute():
            output_path = config.data_dir / output_path
        self.output_dir = output_path
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = config.data_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._runner = runner or _run_process
        self._tts = tts if tts is not None else _TTSBridge(config)
        self._vram_free = vram_free or _gpu_free_mb
        self._lock = threading.Lock()
        self._sing_lock_path = lock_path(config.data_dir, "sing")
        self._vram_lock_path = lock_path(config.data_dir, "vram")
        self._tts_unloaded_this_job = False
        self._tts_reloaded = False

    # ------------------------------------------------------------- public API
    def status(self) -> dict[str, Any]:
        assets = self.asset_report()
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "remote_url": self.remote_url,
            "emotion": self.emotion,
            "output_dir": str(self.output_dir),
            "python": str(self.python or ""),
            "script": str(self.script or ""),
            "assets": assets,
            "assets_ok": all(assets.values()) if assets else True,
        }

    def asset_report(self) -> dict[str, bool]:
        # Remote mode has no local renderer: python/script/vendor all live on
        # the home machine, so reporting them missing would be a false alarm.
        if self.mode == "remote":
            return {}
        report: dict[str, bool] = {
            "python": bool(self.python) and self.python.is_file(),
            "script": bool(self.script) and self.script.is_file(),
            "default_src": bool(self.default_src) and self.default_src.is_file(),
        }
        for name, path in (
            ("prompt_wav", self.prompt_wav),
            ("prompt_meta", self.prompt_meta),
            ("soulx_root", self.soulx_root),
            ("deps_path", self.deps_path),
        ):
            if path:
                report[name] = path.exists()
        return report

    def sing(
        self,
        *,
        lyrics: Any = None,
        emotion: str = "",
        session_id: str = "",
        now: float | None = None,
    ) -> SingResult:
        """Render one song.  Never raises; failures come back as codes."""
        started = time.time()
        now = time.time() if now is None else now
        if not self.enabled:
            return SingResult(False, error="disabled")
        if not self._lock.acquire(blocking=False):
            return SingResult(False, error="busy")
        if not _PROCESS_JOB_LOCK.acquire(blocking=False):
            self._lock.release()
            return SingResult(False, error="busy")
        process_lock = acquire(self._sing_lock_path)
        if process_lock is None:
            _PROCESS_JOB_LOCK.release()
            self._lock.release()
            return SingResult(False, error="sing_busy")

        job_id = uuid.uuid4().hex[:16]
        job_dir = self.output_dir / job_id
        chosen_emotion = self._clean_emotion(emotion)
        lines, lyrics_error = clean_lyrics(lyrics)
        result: SingResult | None = None
        vram_lock = None
        self._tts_unloaded_this_job = False
        self._tts_reloaded = False
        try:
            if self.mode != "remote":
                missing = self._missing_assets(custom_lyrics=bool(lines))
                if missing:
                    self._insert_job(
                        job_id, session_id, now, chosen_emotion, lines, status="failed"
                    )
                    self._update_job(job_id, status="failed", error=missing)
                    return self._finish(
                        SingResult(
                            False,
                            emotion=chosen_emotion,
                            job_id=job_id,
                            error=missing,
                            lyrics=lines,
                            lyrics_error=lyrics_error,
                            elapsed_ms=(time.time() - started) * 1000,
                        )
                    )
            try:
                self._insert_job(job_id, session_id, now, chosen_emotion, lines)
            except sqlite3.Error:
                return self._finish(
                    SingResult(
                        False,
                        emotion=chosen_emotion,
                        job_id=job_id,
                        error="sing_storage_error",
                        lyrics=lines,
                        lyrics_error=lyrics_error,
                        elapsed_ms=(time.time() - started) * 1000,
                    )
                )

            if self.mode == "remote":
                result = self._render_remote(
                    job_id=job_id,
                    job_dir=job_dir,
                    emotion=chosen_emotion,
                    lines=lines,
                    lyrics_error=lyrics_error,
                )
                # The home side owns the card and the TTS handoff; report what
                # *it* says instead of our own (never touched) local bridge.
                self._tts_unloaded_this_job = result.tts_unloaded
                self._tts_reloaded = result.tts_reloaded
            else:
                job_dir.mkdir(parents=True, exist_ok=True)
                vram_lock = acquire(self._vram_lock_path)
                if vram_lock is None:
                    result = SingResult(
                        False,
                        emotion=chosen_emotion,
                        job_id=job_id,
                        error="vram_busy",
                        lyrics=lines,
                        lyrics_error=lyrics_error,
                    )
                elif not self._ensure_vram():
                    result = SingResult(
                        False,
                        emotion=chosen_emotion,
                        job_id=job_id,
                        error="vram_conflict_tts",
                        lyrics=lines,
                        lyrics_error=lyrics_error,
                    )
                else:
                    src = self.default_src
                    if lines:
                        src = job_dir / "lyrics.json"
                        # ``lyrics`` keeps section tags for the renderer; the
                        # 4-line default song only ever has ``phrases``.
                        src.write_text(
                            json.dumps(
                                {
                                    "title": "点歌",
                                    "lyrics": lines,
                                    "phrases": [
                                        {"text": line}
                                        for line in lines
                                        if not is_section_tag(line)
                                    ],
                                },
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                    request = RenderRequest(
                        command=self._command(chosen_emotion, job_dir, src),
                        cwd=self._root,
                        env=self._env(),
                        log_path=self.log_dir / f"sing-{job_id}.log",
                        output_dir=job_dir,
                        timeout=self.timeout,
                    )
                    ok, error = self._runner(request)
                    audio = job_dir / "full.wav"
                    if ok and not audio.is_file():
                        ok, error = False, "render_failed:no_output"
                    if ok:
                        result = SingResult(
                            True,
                            path=str(audio),
                            emotion=chosen_emotion,
                            job_id=job_id,
                            lyrics=lines,
                            lyrics_error=lyrics_error,
                            title=("你点的歌" if lines else self.default_title),
                        )
                    else:
                        result = SingResult(
                            False,
                            emotion=chosen_emotion,
                            job_id=job_id,
                            error=error or "render_failed",
                            lyrics=lines,
                            lyrics_error=lyrics_error,
                        )
        except Exception as exc:  # one bad job must not kill the runtime
            result = SingResult(
                False,
                emotion=chosen_emotion,
                job_id=job_id,
                error=f"render_failed:{type(exc).__name__}",
                lyrics=lines,
                lyrics_error=lyrics_error,
            )
        finally:
            if vram_lock is not None:
                vram_lock.release()
                vram_lock = None
            # Remote mode must never reload the *local* TTS: on the cloud host
            # there is no card to reload, and the home server already did it.
            if (
                self.mode != "remote"
                and self._tts_unloaded_this_job
                and self.reload_tts
            ):
                try:
                    self._tts_reloaded = bool(self._tts.load())
                except Exception:
                    self._tts_reloaded = False
            process_lock.release()
            _PROCESS_JOB_LOCK.release()
            self._lock.release()

        if result is None:  # defensive: the try block always assigns one
            result = SingResult(
                False,
                emotion=chosen_emotion,
                job_id=job_id,
                error="render_failed",
                lyrics=lines,
                lyrics_error=lyrics_error,
            )
        result.elapsed_ms = (time.time() - started) * 1000
        result.tts_unloaded = self._tts_unloaded_this_job
        result.tts_reloaded = self._tts_reloaded
        if result.ok:
            self._update_job(
                job_id,
                status="done",
                audio_path=result.path,
                tts_unloaded=result.tts_unloaded,
                elapsed_ms=result.elapsed_ms,
            )
        else:
            self._update_job(
                job_id,
                status="failed",
                error=result.error,
                tts_unloaded=result.tts_unloaded,
                elapsed_ms=result.elapsed_ms,
            )
        return result

    # ----------------------------------------------------------- internals
    def _render_remote(
        self,
        *,
        job_id: str,
        job_dir: Path,
        emotion: str,
        lines: list[str],
        lyrics_error: str,
    ) -> SingResult:
        """One POST to ``scripts/sing_server.py``; the wav lands in data/songs.

        Order matters: probe first (2s), then a bounded POST.  The user's
        message went out before the render started, so every failure here just
        adds one sentence via :func:`friendly_sing_error` -- no 450s silence.
        """
        def fail(code: str) -> SingResult:
            return SingResult(
                False,
                emotion=emotion,
                job_id=job_id,
                error=code,
                lyrics=lines,
                lyrics_error=lyrics_error,
            )

        client = self.remote_client
        if client is None:
            return fail("remote_not_configured")
        try:
            reachable = bool(client.reachable())
        except Exception:
            reachable = False
        if not reachable:
            return fail("home_offline")

        payload: dict[str, Any] = {"emotion": emotion}
        if lines:
            payload["lyrics"] = list(lines)
        try:
            status, body, headers = client.post(payload)
        except TimeoutError:
            return fail("remote_timeout")
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                return fail("remote_timeout")
            return fail("home_offline")

        headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
        if status == 200:
            if len(body or b"") <= _MIN_WAV_BYTES or not body.startswith(b"RIFF"):
                return fail("remote_bad_audio")
            try:
                job_dir.mkdir(parents=True, exist_ok=True)
                audio = job_dir / "full.wav"
                audio.write_bytes(body)
            except OSError:
                return fail("sing_storage_error")
            return SingResult(
                True,
                path=str(audio),
                emotion=emotion,
                job_id=job_id,
                lyrics=lines,
                lyrics_error=lyrics_error,
                title=("你点的歌" if lines else self.default_title),
                tts_unloaded=headers.get("x-tts-unloaded", "") == "1",
                tts_reloaded=headers.get("x-tts-reloaded", "") == "1",
            )

        # Prefer the home side's own code ("busy", "render_timeout", ...): the
        # friendly sentence already knows how to explain those.
        code = ""
        if body:
            try:
                data = json.loads(body.decode("utf-8", errors="replace"))
            except ValueError:
                data = None
            if isinstance(data, dict):
                code = str(data.get("error") or "").strip()[:120]
        return fail(code or f"remote_error:{status}")

    def _finish(self, result: SingResult) -> SingResult:
        """Bookkeeping for early returns that happen before the job loop."""
        result.tts_unloaded = self._tts_unloaded_this_job
        result.tts_reloaded = self._tts_reloaded
        return result

    def _path(self, raw: Any) -> Path | None:
        text = str(raw or "").strip()
        if not text:
            return None
        path = Path(text)
        return path if path.is_absolute() else (self._root / path)

    def _clean_emotion(self, raw: Any) -> str:
        value = str(raw or "").strip().lower()
        return value if value in DEFAULT_EMOTIONS else self.emotion

    def _missing_assets(self, *, custom_lyrics: bool) -> str:
        if not self.python or not self.python.is_file():
            return "python_missing"
        if not self.script or not self.script.is_file():
            return "script_missing"
        if not custom_lyrics and (
            not self.default_src or not self.default_src.is_file()
        ):
            return "lyrics_src_missing"
        missing = []
        for name, path in (
            ("prompt_wav", self.prompt_wav),
            ("prompt_meta", self.prompt_meta),
        ):
            if path and not path.is_file():
                missing.append(name)
        if missing:
            return "missing_assets:" + ",".join(missing)
        return ""

    def _command(self, emotion: str, job_dir: Path, src: Path) -> list[str]:
        command = [
            str(self.python),
            str(self.script),
            "--emotion",
            emotion,
            "--out",
            str(job_dir),
            "--src",
            str(src),
            "--semitones",
            f"{self.semitones:g}",
        ]
        if self.prompt_wav:
            command += ["--prompt-wav", str(self.prompt_wav)]
        if self.prompt_meta:
            command += ["--prompt-meta", str(self.prompt_meta)]
        return command

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONUTF8", "1")
        if self.soulx_root:
            env["SOULX_ROOT"] = str(self.soulx_root)
        if self.deps_path:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{self.deps_path}{os.pathsep}{existing}" if existing
                else str(self.deps_path)
            )
        return env

    def _ensure_vram(self) -> bool:
        """Make room for the singer, cooperatively unloading TTS if needed."""
        free = self._vram_free()
        if free is not None and free >= self.min_free_vram_mb:
            return True
        if not self.unload_tts:
            return free is None
        if not bool(self._tts.reachable()):
            return free is None
        if not bool(self._tts.unload()):
            return free is None
        self._tts_unloaded_this_job = True
        return self._wait_for_vram()

    def _wait_for_vram(self) -> bool:
        deadline = time.time() + self.vram_wait_seconds
        while True:
            free = self._vram_free()
            if free is not None and free >= self.min_free_vram_mb:
                return True
            loaded = self._tts.loaded()
            if loaded is False:
                return True
            if time.time() >= deadline:
                return free is None and loaded is None
            time.sleep(min(0.25, self.vram_wait_seconds or 0.25))

    # --------------------------------------------------------- persistence
    def _insert_job(
        self,
        job_id: str,
        session_id: str,
        now: float,
        emotion: str,
        lyrics: list[str],
        *,
        status: str = "running",
    ) -> None:
        self.db.execute(
            "INSERT INTO sing_jobs(id, session_id, created_at, status, emotion, "
            "lyrics_json) VALUES (?,?,?,?,?,?)",
            (
                job_id,
                session_id[:120],
                float(now),
                status,
                emotion,
                json.dumps(lyrics, ensure_ascii=False),
            ),
        )

    def _update_job(
        self,
        job_id: str,
        *,
        status: str,
        audio_path: str | None = None,
        error: str | None = None,
        tts_unloaded: bool = False,
        elapsed_ms: float = 0.0,
    ) -> None:
        try:
            self.db.execute(
                "UPDATE sing_jobs SET status=?, audio_path=?, error=?, "
                "finished_at=?, tts_unloaded=?, elapsed_ms=? WHERE id=?",
                (
                    status,
                    audio_path,
                    (error or "")[:500],
                    time.time(),
                    1 if tts_unloaded else 0,
                    float(elapsed_ms),
                    job_id,
                ),
            )
        except sqlite3.Error:
            pass
        if status == "failed" and error:
            self._metric_sing_error(error)

    def _metric_sing_error(self, code: str) -> None:
        """One ``metrics`` row per failed song: the code *and* the sentence.

        The user only ever sees the friendly line (``runtime`` appends it to
        the reply), so the raw code would otherwise be invisible on the cloud
        host, where ``data/logs`` is not where anyone looks.
        """
        try:
            self.db.execute(
                "INSERT INTO metrics(at, key, value, tags_json) VALUES (?,?,?,?)",
                (
                    time.time(),
                    "sing_error",
                    1.0,
                    json.dumps(
                        {
                            "mode": self.mode,
                            "error": str(code)[:500],
                            "friendly": friendly_sing_error(code),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
        except sqlite3.Error:
            pass


def _run_process(request: RenderRequest) -> tuple[bool, str]:
    """Default runner: one subprocess, log to a file, hard timeout."""
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    request.output_dir.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        handle = request.log_path.open("wb", buffering=0)
        proc = subprocess.Popen(
            request.command,
            cwd=str(request.cwd),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=request.env,
            creationflags=creationflags,
        )
        try:
            code = proc.wait(timeout=request.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return False, "render_timeout"
    except (OSError, ValueError) as exc:
        return False, f"render_failed:{type(exc).__name__}"
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
    if code == 0:
        return True, ""
    tail = _log_tail(request.log_path, 300)
    return False, "render_failed:" + (tail or f"exit_{code}")


def _log_tail(path: Path, limit: int) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data[-max(1, limit * 4):].decode("utf-8", errors="ignore")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[-limit:]


def _gpu_free_mb() -> int | None:
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    values = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            values.append(int(line))
    return min(values) if values else None
