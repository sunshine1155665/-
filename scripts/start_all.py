"""One-click supervisor for the local fatfish stack.

Starts the optional local soft-voice TTS and the local embedding service, then
the web UI and waits for its login URL, then the QQ adapter when NapCat is
ready.  Everything is a child of this process, so Ctrl+C stops all of them and
no orphan server is left behind.

Stdlib only.  Optional dependencies and services are detected, never required:

* ``websocket-client`` for the QQ adapter
* a running NapCat on ``127.0.0.1:3001`` plus ``QQ_ACCESS_TOKEN`` in ``.env``
* the local TTS engine selected by ``tts.engine`` (only when
  ``tts.enabled = true``):
  - ``indextts25``: ``.venv-indextts`` + ``models/IndexTTS-2.5`` + the v6
    reference ``cand_home_up1.wav`` (2.5 + free-form 8-D vectors);
  - ``cosyvoice``: ``.venv-cosyvoice`` + ``models/Fun-CosyVoice3-0.5B`` + a
    soft reference.
* ``.venv-cosyvoice`` + ``models/bge-base-zh-v1.5`` for the embedding service
  (only when ``memory.embedding_backend`` is an HTTP backend)

The printed web URL contains a per-run login token.  Tokens and cookies are
never written to the log files (the token in the URL is redacted in logs).
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import os
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# stdlib-only and dependency-free by design, so it is safe to import before the
# optional components have been checked. Keeping the import here (rather than
# inside run()) means a broken package shows up at once instead of after
# somebody has already waited for the TTS model to load.
from fatfish.services import (  # noqa: E402
    CONTROL_HOST,
    CONTROL_PORT,
    CONTROL_TOKEN_NAME,
    ControlError,
    ControlPlane,
    SERVICE_REGISTRY,
)

MIN_PYTHON = (3, 11)
WEB_HOST = "127.0.0.1"
NAPCAT_HOST = "127.0.0.1"
NAPCAT_PORT = 3001
# The single source for names/labels/ports is fatfish.services.SERVICE_REGISTRY;
# these constants just unpack it for the many local uses.
TTS_HOST = SERVICE_REGISTRY["tts"].host
TTS_PORT = SERVICE_REGISTRY["tts"].port or 9880
EMBED_HOST = SERVICE_REGISTRY["embedding"].host
EMBED_PORT = SERVICE_REGISTRY["embedding"].port or 9881
COMFY_HOST = SERVICE_REGISTRY["comfyui"].host
COMFY_PORT = SERVICE_REGISTRY["comfyui"].port or 8188
# How long to wait for the embedding model before letting the web server run
# anyway. Loading takes ~9s; without the wait the first turns fall back to
# hashing, whose 256-dim vectors score 0 against the stored 768-dim ones.
EMBED_READY_TIMEOUT = 25.0
# The TTS server binds its port before the model is resident (it loads on the
# first request), so this only waits for the HTTP surface, not for the weights.
TTS_READY_TIMEOUT = 60.0
STARTUP_TIMEOUT = 20.0
STOP_TIMEOUT = 8.0
WEB_BANNER_RE = re.compile(r"fatfish web:\s+(http://\S+)")
TOKEN_LINE_RE = re.compile(r"^\s*token\s*:", re.IGNORECASE)
WAV_TOKEN_RE = re.compile(r"""[^\s"']+\.wav""", re.IGNORECASE)

SOFT_SERVER = ROOT / "scripts" / "cosyvoice_soft_server.py"
SOFT_MODEL = ROOT / "models" / "Fun-CosyVoice3-0.5B"
INDEXTTS_SERVER = ROOT / "scripts" / "indextts_server.py"
INDEXTTS_MODEL = ROOT / "models" / "IndexTTS-2.5"
INDEXTTS_REF_DIR = ROOT / "data" / "whale_voice_demo_v2" / "refs"
EMBED_SERVER = ROOT / "scripts" / "embedding_server.py"
EMBED_MODEL = ROOT / "models" / "bge-base-zh-v1.5"
# Backends that mean "ask the local service". Kept in step with
# fatfish.memory.embedder.HTTP_BACKENDS, duplicated here so this supervisor
# stays stdlib-only and importable before the package is installed.
EMBED_BACKENDS = frozenset({"http", "service", "bge", "remote"})
SOFT_REF_ENV = ("COSYVOICE_SOFT_REF", "FATFISH_SOFT_REF")
SOFT_REF_CANDIDATES = (
    "data/whale_voice_demo_v2/refs/ref_base_x2.wav",
    "data/voices/soft/ref.wav",
    "data/voices/whale_girl_soft/ref.wav",
    "data/voices/whale_soft/ref.wav",
    "data/soft_ref.wav",
    "data/refs/soft_ref.wav",
    "data/refs_soft/ref.wav",
    "assets/soft_ref.wav",
)


# --------------------------------------------------------------------------- output
def say(message: str = "") -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"[fatfish] {message}", flush=True)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def redact(text: str, secrets_: tuple[str, ...] | list[str] = ()) -> str:
    """Replace sensitive fragments before writing a line to disk or screen."""
    for secret in secrets_:
        if secret:
            text = text.replace(secret, "<token>")
    return text


# --------------------------------------------------------------------------- helpers
def parse_web_banner(line: str) -> str | None:
    """Return the login URL from a server banner line, or ``None``."""
    match = WEB_BANNER_RE.search(line)
    return match.group(1) if match else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="start_all.py",
        description="一键启动大肥鱼：Web + QQ（可选）+ 本地软声 TTS（可选）。按 Ctrl+C 停止全部。",
    )
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--no-qq", action="store_true", help="不启动 QQ 适配器")
    parser.add_argument("--web-only", action="store_true", help="只启动 Web，不启动 QQ/TTS")
    parser.add_argument("--no-tts", action="store_true", help="不启动本地 CosyVoice 软声服务")
    parser.add_argument(
        "--no-embedding", action="store_true", help="不启动本地向量服务（贴纸退回哈希匹配）"
    )
    parser.add_argument(
        "--no-control",
        action="store_true",
        help="不开放后端控制口（Web 里的「服务」面板会显示不可用）",
    )
    parser.add_argument(
        "--port",
        type=_port_number,
        default=0,
        help="Web 端口（默认 0 = 自动选择空闲端口）",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="配置 profile：low / standard / remote（默认读 FATFISH_PROFILE，否则 low）",
    )
    return parser


def _port_number(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid port: {value!r}") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def _python() -> str:
    return sys.executable or "python"


def tts_command(
    python: Path,
    server: Path,
    *,
    idle_unload_seconds: float | None = None,
    warmup: bool = False,
    pitch_semitones: int | None = None,
) -> list[str]:
    command = [str(python), str(server), "--host", TTS_HOST, "--port", str(TTS_PORT)]
    if idle_unload_seconds is not None:
        command.extend(["--idle-unload-seconds", f"{float(idle_unload_seconds):g}"])
    if warmup:
        command.append("--warmup")
    if pitch_semitones is not None:
        command.extend(["--pitch-semitones", str(max(0, int(pitch_semitones)))])
    return command


def embedding_command(python: Path) -> list[str]:
    return [
        str(python), str(EMBED_SERVER),
        "--host", EMBED_HOST, "--port", str(EMBED_PORT),
    ]


def qq_command(profile: str) -> list[str]:
    return [_python(), "-m", "fatfish", "--profile", profile, "qq", "run"]


def http_health(url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    """GET a JSON health endpoint; ``None`` when it cannot be read."""
    import json as _json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = _json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


def http_post(url: str, timeout: float = 30.0) -> dict[str, Any] | None:
    import json as _json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = _json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


def wait_for_health(url: str, *, timeout: float, want_loaded: bool = True) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = http_health(url, timeout=1.5)
        if payload and payload.get("ok") and (payload.get("loaded") or not want_loaded):
            return True
        time.sleep(0.4)
    return False


def pid_on_port(port: int, host: str = "127.0.0.1") -> int | None:
    """Best-effort owner of a listening TCP port.

    Needed because two of these backends are not started by the supervisor:
    ComfyUI belongs to ``PhotoService`` and, on a bad day, a previous manual
    run holds 9881 with no pid file. Releasing VRAM means stopping *whoever*
    owns the port, so the port is the only handle that always exists.
    """
    if not port_open(host, port):
        return None
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        needle = f":{int(port)}"
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local, state, pid = parts[1], parts[3].upper(), parts[4]
            # LISTENING only: a client connection to the port has its own row
            # with an ephemeral local port and would name the wrong process.
            if not local.endswith(needle) or state != "LISTENING":
                continue
            try:
                value = int(pid)
            except ValueError:
                continue
            if value > 0 and value != os.getpid():
                return value
        return None
    try:
        import psutil  # type: ignore

        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                return int(conn.pid) if conn.pid else None
    except Exception:
        return None
    return None


def kill_tree(pid: int) -> bool:
    """Kill a process and its children.

    ``terminate()`` is not enough on Windows: CosyVoice spawns dataloader
    workers and ComfyUI spawns its own python, and neither dies with the
    parent. ``/T`` is the only thing that reliably frees the port.
    """
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _load_config(profile: str | None) -> Any:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from fatfish.config import load_config  # imported late: Python <3.11 has no tomllib

    # Importing fatfish.config also loads .env; load_config(None) then honours
    # FATFISH_PROFILE before falling back to "low".
    return load_config(profile)


def _child_env(profile: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["FATFISH_PROFILE"] = profile
    env.pop("FATFISH_WEB_TOKEN", None)
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT) + ((os.pathsep + current) if current else "")
    return env


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, OverflowError, ValueError):
        return False


def _interrupted_exit(code: int | None) -> bool:
    """True for exit codes a Ctrl+C/Ctrl+Break gives a console child."""
    if code is None:
        return False
    return code in {-2, -15, 130, 3221225786, -1073741510}


# --------------------------------------------------------------------------- pid files
def pid_alive(pid: int) -> bool:
    """Best-effort liveness check that never signals the process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        for row in csv.reader(io.StringIO(result.stdout or "")):
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    token = raw.split()[0] if raw else ""
    try:
        pid = int(token)
    except ValueError:
        return None
    return pid if pid > 0 else None


def write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(f"{pid}\n", encoding="ascii")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass


def remove_pid(path: Path, pid: int | None = None) -> None:
    if pid is not None and read_pid(path) != pid:
        return
    try:
        path.unlink()
    except OSError:
        pass


def clean_stale_pid_files(run_dir: Path) -> list[Path]:
    """Delete ``*.pid`` files whose process is gone; return removed paths."""
    removed: list[Path] = []
    if not run_dir.is_dir():
        return removed
    for path in sorted(run_dir.glob("*.pid")):
        pid = read_pid(path)
        if pid is not None and pid_alive(pid):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def acquire_supervisor_lock(path: Path) -> Any | None:
    """Hold an exclusive OS lock for this supervisor's lifetime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, ImportError):
        try:
            handle.close()  # type: ignore[possibly-undefined]
        except (OSError, UnboundLocalError):
            pass
        return None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode("ascii"))
        handle.flush()
    except OSError:
        pass
    return handle


def release_supervisor_lock(handle: Any | None) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ImportError):
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- child process
class ManagedChild:
    """A child process whose output is logged, masked and optionally collected."""

    def __init__(
        self,
        name: str,
        command: list[str],
        log_path: Path,
        *,
        profile: str = "low",
        secrets_: tuple[str, ...] = (),
    ):
        self.name = name
        self.command = command
        self.log_path = log_path
        self.profile = profile
        self.secrets = tuple(item for item in secrets_ if item)
        self.process: subprocess.Popen[str] | None = None
        self.lines: deque[str] = deque(maxlen=60)
        self.queue: queue.Queue[str | None] = queue.Queue()
        self._collecting = True
        self._thread: threading.Thread | None = None
        self._log: TextIO | None = None
        self._log_lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def poll(self) -> int | None:
        return self.process.poll() if self.process is not None else None

    @property
    def returncode(self) -> int | None:
        return self.process.returncode if self.process is not None else None

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("a", encoding="utf-8", errors="replace")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        command = redact(" ".join(self.command), self.secrets)
        self._write_log(f"=== {stamp} start {self.name}: {command} ===")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=str(ROOT),
                env=_child_env(self.profile),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError:
            with self._log_lock:
                if self._log is not None:
                    try:
                        self._log.close()
                    except OSError:
                        pass
                    self._log = None
            raise
        self._thread = threading.Thread(
            target=self._pump, name=f"fatfish-{self.name}", daemon=True
        )
        self._thread.start()

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for raw in self.process.stdout:
                line = raw.rstrip("\r\n")
                self.lines.append(line)
                self._write_log(line)
                if self._should_echo(line):
                    print(redact(line, self.secrets), flush=True)
                if self._collecting:
                    self.queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            if self._collecting:
                self.queue.put(None)

    def _should_echo(self, line: str) -> bool:
        if WEB_BANNER_RE.search(line):
            return False
        return not TOKEN_LINE_RE.match(line)

    def _write_log(self, line: str) -> None:
        with self._log_lock:
            if self._log is None:
                return
            stamp = time.strftime("%H:%M:%S")
            try:
                self._log.write(f"[{stamp}] {redact(line, self.secrets)}\n")
                self._log.flush()
            except (OSError, ValueError):
                pass

    def stop_collecting(self) -> None:
        self._collecting = False

    def stop(self, timeout: float = STOP_TIMEOUT, *, tree: bool = False) -> None:
        self.stop_collecting()
        process = self.process
        if process is not None and process.poll() is None:
            if tree and os.name == "nt" and process.pid:
                # Windows does not kill grandchildren.  CosyVoice's dataloader
                # workers are separate processes and keep their CUDA context
                # alive after the parent's terminate(), so the VRAM never comes
                # back.  taskkill /T /F is the only reliable tree kill here.
                kill_tree(process.pid)
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            else:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        code = process.returncode if process is not None else None
        self._write_log(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} stop {self.name} (exit={code}) ===")
        with self._log_lock:
            if self._log is not None:
                try:
                    self._log.close()
                except OSError:
                    pass
                self._log = None


def wait_for_web_url(child: ManagedChild, timeout: float = STARTUP_TIMEOUT) -> str | None:
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = child.queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if child.poll() is not None:
                    return None
                continue
            if line is None:
                return None
            url = parse_web_banner(line)
            if url:
                return url
    finally:
        child.stop_collecting()


# --------------------------------------------------------------------------- services
def _qq_access_token(config: Any) -> str:
    env_name = str(config.get("qq.access_token_env", "QQ_ACCESS_TOKEN"))
    for name in (env_name, "QQ_ACCESS_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return str(config.get("qq.access_token") or "").strip()


def _find_other_process(needles: tuple[str, ...]) -> list[int]:
    """Best-effort scan for an already running adapter (requires psutil)."""
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    found: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid == os.getpid():
                continue
            command = " ".join(proc.info.get("cmdline") or [])
            if command and all(needle in command for needle in needles):
                found.append(pid)
        except Exception:
            continue
    return found


def _bili_cookie_path(config: Any) -> Path:
    raw = str(config.get("bilibili.cookie_path", "bilibili_cookies.json"))
    path = Path(raw)
    if not path.is_absolute():
        path = config.data_dir / path
    return path.resolve()


def _resolve_reference(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def find_soft_reference(config: Any) -> Path | None:
    """Locate the soft-voice reference without printing its path contents."""
    for name in SOFT_REF_ENV:
        raw = os.environ.get(name, "").strip()
        if raw:
            path = _resolve_reference(raw)
            if path.is_file():
                return path
    for key in ("tts.soft_ref_audio", "tts.soft_ref", "tts.ref_audio_path"):
        raw = str(config.get(key) or "").strip()
        if raw:
            path = _resolve_reference(raw)
            if path.is_file():
                return path
    for relative in SOFT_REF_CANDIDATES:
        path = ROOT / relative
        if path.is_file():
            return path
    for script in (
        ROOT / "scripts" / "start_tts_soft.bat",
        ROOT / "scripts" / "cosyvoice_soft_server.py",
    ):
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in WAV_TOKEN_RE.findall(text):
            path = _resolve_reference(raw.strip("\"'"))
            if path.is_file():
                return path
    for pattern in ("data/*soft*.wav", "data/voices/**/*.wav", "data/refs_soft/**/*.wav"):
        for path in sorted(ROOT.glob(pattern)):
            lowered = path.as_posix().lower()
            if path.is_file() and ("soft" in lowered or "ref" in path.name.lower()):
                return path
    return None


def tts_engine(config: Any) -> str:
    """Which local engine owns ``tts.base_url``.

    Canonical names are ``cosyvoice`` and ``indextts25``.  ``indextts`` is kept
    as a legacy alias so an old config still boots the 2.5 service; the TTS
    cache is namespaced separately (``tts.cache_version``), so a 2.0 WAV is
    never replayed as if it were 2.5.
    """
    raw = str(config.get("tts.engine", "cosyvoice") or "cosyvoice").strip().lower()
    if raw in ("indextts", "indextts25"):
        return "indextts25"
    if raw == "cosyvoice":
        return "cosyvoice"
    return "cosyvoice"


def tts_idle_unload_seconds(config: Any) -> float | None:
    """Read ``tts.idle_unload_seconds``; ``0`` means keep the model resident."""
    raw = config.get("tts.idle_unload_seconds", None)
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def tts_pitch_semitones(config: Any) -> int:
    """Read ``tts.pitch_semitones``; 0 = no pitch overlay.

    The v6 reference ``cand_home_up1.wav`` was produced offline already +2
    semitones up, so the overlay must stay off by default — stacking the old
    +2 on top of it was the +4 pitch bug (2026-10-25).  A value >0 re-enables
    the asetrate+atempo shift for a reference at the original pitch.
    """
    raw = config.get("tts.pitch_semitones", 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def tts_warmup(config: Any) -> bool:
    """Only IndexTTS has a ``--warmup`` flag; CosyVoice keeps its own policy."""
    return bool(config.get("tts.warmup", False)) and tts_engine(config) == "indextts25"


def soft_tts_components(config: Any) -> tuple[list[str], Path | None, Path, Path]:
    if tts_engine(config) == "indextts25":
        python = ROOT / ".venv-indextts" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        missing: list[str] = []
        if not INDEXTTS_SERVER.is_file():
            missing.append("scripts/indextts_server.py")
        if not python.is_file():
            missing.append(str(python.relative_to(ROOT)))
        if not INDEXTTS_MODEL.is_dir():
            missing.append("models/IndexTTS-2.5")
        reference = INDEXTTS_REF_DIR / "cand_home_up1.wav"
        if not reference.is_file():
            missing.append("data/whale_voice_demo_v2/refs/cand_home_up1.wav")
        return missing, None, python, INDEXTTS_SERVER

    python = ROOT / ".venv-cosyvoice" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    missing: list[str] = []
    if not SOFT_SERVER.is_file():
        missing.append("scripts/cosyvoice_soft_server.py")
    if not python.is_file():
        missing.append(str(python.relative_to(ROOT)))
    if not SOFT_MODEL.is_dir():
        missing.append("models/Fun-CosyVoice3-0.5B")
    reference = find_soft_reference(config)
    if reference is None:
        missing.append("软声参考音频（设置 COSYVOICE_SOFT_REF 或放到 data/voices/soft/ref.wav）")
    return missing, reference, python, SOFT_SERVER


def start_tts(
    config: Any,
    *,
    profile: str,
    disabled: bool,
    run_dir: Path,
    logs_dir: Path,
) -> tuple[ManagedChild | None, Path | None]:
    if disabled:
        say("[TTS] 已跳过（--web-only / --no-tts）")
        return None, None
    if not bool(config.get("tts.enabled", False)):
        say("[TTS] 未启用（tts.enabled=false），不启动本地模型")
        return None, None
    mode = str(config.get("tts.mode", "http_json"))
    base_url = str(config.get("tts.base_url") or "").strip()
    command = str(config.get("tts.command") or "").strip()
    if not base_url and not command:
        warn("[TTS] tts.enabled=true 但 base_url/command 为空；语音调用会失败。")
    if mode != "http_json":
        say(f"[TTS] mode={mode}，本地软声服务不适用，跳过")
        return None, None
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1") or (
            parsed.port or (443 if parsed.scheme == "https" else 80)
        ) != TTS_PORT:
            say("[TTS] base_url 指向外部服务，跳过本地软声服务")
            return None, None
    if base_url or command:
        say(f"[TTS] 已启用（mode={mode}），本地软声服务按需启动")
    pid_path = run_dir / "tts.pid"
    for candidate in (pid_path, run_dir / "tts_soft.pid"):
        existing = read_pid(candidate)
        if existing and pid_alive(existing):
            say(f"[TTS] 已有本地服务在运行（PID {existing}），跳过")
            return None, None
    if port_open(TTS_HOST, TTS_PORT):
        # Reusing a service on 9880 is fine only when it is the engine the
        # config expects.  Otherwise the UI would report "running" while the
        # client keeps sending IndexTTS vectors to CosyVoice (or vice versa).
        health = http_health(f"http://{TTS_HOST}:{TTS_PORT}/health")
        running_engine = str((health or {}).get("engine") or "").strip().lower()
        wanted_engine = tts_engine(config)
        if running_engine and running_engine != wanted_engine:
            warn(
                f"[TTS] 9880 上运行的是 {running_engine}，但 tts.engine={wanted_engine}；"
                "不复用。请先停掉旧服务，或把 tts.engine 改成与它一致。"
            )
            return None, None
        say(f"[TTS] 127.0.0.1:{TTS_PORT} 已有服务，复用")
        return None, None
    missing, _reference, python, server = soft_tts_components(config)
    if missing:
        warn("[TTS] tts.enabled=true，但本地软声服务缺组件：")
        for item in missing:
            say(f"      - {item}")
        say("[TTS] Web/QQ 继续启动；语音会失败或退回其他后端。")
        return None, None
    child = ManagedChild(
        "tts",
        tts_command(
            python,
            server,
            idle_unload_seconds=tts_idle_unload_seconds(config),
            warmup=tts_warmup(config),
            pitch_semitones=tts_pitch_semitones(config),
        ),
        logs_dir / "tts.log",
        profile=profile,
    )
    try:
        child.start()
    except OSError as exc:
        warn(f"[TTS] 启动失败：{exc}；Web/QQ 继续。")
        return None, None
    if child.pid:
        write_pid(pid_path, child.pid)
    say("[TTS] 本地软声服务启动中（模型加载不阻塞 Web）")
    return child, pid_path


def embedding_components(config: Any) -> tuple[list[str], Path]:
    """Return (missing pieces, interpreter) for the local embedding service."""
    # CosyVoice's venv is the one that reliably has torch + transformers on this
    # machine, and the embedding server needs exactly those two.
    python = ROOT / ".venv-cosyvoice" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    missing: list[str] = []
    if not EMBED_SERVER.is_file():
        missing.append("scripts/embedding_server.py")
    if not python.is_file():
        missing.append(str(python.relative_to(ROOT)))
    if not EMBED_MODEL.is_dir():
        missing.append("models/bge-base-zh-v1.5")
    return missing, python


def embedding_enabled(config: Any) -> bool:
    # Sticker retrieval is gone; the embedding server now only exists for the
    # memory backend (hash on every shipped profile, so 9881 stays down unless
    # a profile explicitly switches memory to an HTTP backend).
    backend = str(config.get("memory.embedding_backend", "hash")).strip().lower()
    return backend in EMBED_BACKENDS


def _warm_embedding() -> bool:
    """Ask the lazy embedding server to load its model now.

    The service loads on the first ``/embed``, not at bind time, so merely
    polling ``/health`` used to burn the whole 25 s budget and then start Web
    anyway -- and the first turn still saw a cold model.  A background POST
    gives the server the trigger while the health loop keeps polling.
    """
    import json as _json
    import urllib.error
    import urllib.request

    body = _json.dumps({"texts": ["warmup"]}).encode("utf-8")
    request = urllib.request.Request(
        f"http://{EMBED_HOST}:{EMBED_PORT}/embed",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return True


def wait_for_embedding(timeout: float = EMBED_READY_TIMEOUT) -> bool:
    """Poll ``/health`` until the model reports loaded."""
    import json as _json
    import urllib.error
    import urllib.request

    url = f"http://{EMBED_HOST}:{EMBED_PORT}/health"
    deadline = time.monotonic() + timeout
    triggered = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                payload = _json.loads(response.read().decode("utf-8"))
            if payload.get("ok") and payload.get("loaded"):
                return True
            if payload.get("ok") and not triggered:
                triggered = True
                threading.Thread(
                    target=_warm_embedding, name="fatfish-embed-warmup", daemon=True
                ).start()
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.4)
    return False


def start_embedding(
    config: Any,
    *,
    profile: str,
    disabled: bool,
    run_dir: Path,
    logs_dir: Path,
) -> tuple[ManagedChild | None, Path | None]:
    if disabled:
        say("[EMB] 已跳过（--web-only / --no-embedding）")
        return None, None
    if not embedding_enabled(config):
        say("[EMB] 未启用（memory.embedding_backend=hash），用哈希向量")
        return None, None
    pid_path = run_dir / "embedding.pid"
    existing = read_pid(pid_path)
    if existing and pid_alive(existing):
        say(f"[EMB] 已有向量服务在运行（PID {existing}），跳过")
        return None, None
    if port_open(EMBED_HOST, EMBED_PORT):
        say(f"[EMB] 127.0.0.1:{EMBED_PORT} 已有服务，复用")
        return None, None
    missing, python = embedding_components(config)
    if missing:
        warn("[EMB] 已配置真向量，但缺组件：")
        for item in missing:
            say(f"      - {item}")
        say("[EMB] Web 继续启动；贴纸会退回哈希匹配（不会崩，但排序明显变差）。")
        return None, None
    child = ManagedChild(
        "embedding",
        embedding_command(python),
        logs_dir / "embedding.log",
        profile=profile,
    )
    try:
        child.start()
    except OSError as exc:
        warn(f"[EMB] 启动失败：{exc}；Web/QQ 继续。")
        return None, None
    if child.pid:
        write_pid(pid_path, child.pid)
    if wait_for_embedding():
        say(f"[EMB] 向量服务就绪（PID {child.pid}，{EMBED_MODEL.name}）")
    else:
        warn(
            f"[EMB] {int(EMBED_READY_TIMEOUT)} 秒内未就绪，Web 先启动；"
            "模型加载完成后自动可用。"
        )
    return child, pid_path


def start_web(
    profile: str,
    port: int,
    *,
    run_dir: Path,
    logs_dir: Path,
) -> tuple[ManagedChild, str | None, str, Path]:
    token = secrets.token_urlsafe(24)
    command = [
        _python(),
        "-m",
        "fatfish",
        "--profile",
        profile,
        "serve",
        "--host",
        WEB_HOST,
        "--port",
        str(port),
        "--token",
        token,
    ]
    child = ManagedChild(
        "web", command, logs_dir / "web.log", profile=profile, secrets_=(token,)
    )
    child.start()
    pid_path = run_dir / "web.pid"
    if child.pid:
        write_pid(pid_path, child.pid)
    url = wait_for_web_url(child)
    return child, url, token, pid_path


def start_qq(
    config: Any,
    profile: str,
    *,
    disabled: bool,
    websocket_ready: bool,
    run_dir: Path,
    logs_dir: Path,
) -> tuple[ManagedChild | None, Path | None]:
    if disabled:
        say("[QQ] 已跳过")
        return None, None
    token = _qq_access_token(config)
    if not token:
        say("[QQ] 未配置：QQ 扫码登录后把 QQ_ACCESS_TOKEN 写进 .env，再重新双击一键启动")
        return None, None
    napcat_ready = port_open(NAPCAT_HOST, NAPCAT_PORT)
    if not napcat_ready:
        # The adapter retries with backoff, so start it now and let it connect
        # once the QR login finishes instead of skipping QQ for this whole run.
        say("[QQ] NapCat 尚未登录：适配器先后台等待，扫码后会自动连上")
    if not websocket_ready:
        say('[QQ] 缺少 websocket-client，跳过。安装：pip install "websocket-client>=1.7"')
        return None, None
    pid_path = run_dir / "qq.pid"
    existing = read_pid(pid_path)
    if existing and pid_alive(existing):
        say(f"[QQ] 已有适配器在运行（PID {existing}），跳过")
        return None, None
    others = _find_other_process(("fatfish", "qq", "run"))
    if others:
        say(f"[QQ] 检测到已有适配器进程（PID {others[0]}），跳过")
        return None, None
    child = ManagedChild(
        "qq",
        qq_command(profile),
        logs_dir / "qq.log",
        profile=profile,
    )
    try:
        child.start()
    except OSError as exc:
        warn(f"[QQ] 启动失败：{exc}；Web 继续。")
        return None, None
    if child.pid:
        write_pid(pid_path, child.pid)
    if napcat_ready:
        say(f"[QQ] 已连接 NapCat（PID {child.pid}）")
    else:
        say(f"[QQ] 适配器已启动（PID {child.pid}），等待扫码登录…")
    return child, pid_path


class ServiceRuntime:
    """The single owner of the backend children.

    Boot and the UI buttons call the same methods, so there is exactly one code
    path that can spawn or stop a backend. The only difference is how a refusal
    is handled: boot prints a warning and carries on, the UI gets a 4xx.

    Ownership rules that this class exists to enforce:

    * a backend started elsewhere (a previous manual run, or ``PhotoService``
      for ComfyUI) is reported as ``external`` and never silently claimed;
    * ``start`` refuses when the port is already held, instead of racing a
      second child onto it and letting the loser fail invisibly;
    * ``release`` frees VRAM -- it unloads the TTS model but keeps its process,
      stops ComfyUI (the big block), and deliberately leaves the embedding
      server alone, because that one is CPU-only and stopping it quietly
      degrades sticker matching.
    """

    NAMES = tuple(SERVICE_REGISTRY)

    def __init__(
        self,
        config: Any,
        profile: str,
        *,
        run_dir: Path,
        logs_dir: Path,
        websocket_ready: bool = True,
    ):
        self.config = config
        self.profile = profile
        self.run_dir = run_dir
        self.logs_dir = logs_dir
        self.websocket_ready = websocket_ready
        self._lock = threading.RLock()
        self.children: dict[str, ManagedChild | None] = {
            name: None for name in SERVICE_REGISTRY if SERVICE_REGISTRY[name].supervised
        }
        self.pid_paths: dict[str, Path | None] = {name: None for name in self.children}
        self.notes: dict[str, str] = {}
        self._assets_cache: dict[str, tuple[float, list[str]]] = {}

    # ------------------------------------------------------------- plumbing
    def adopt(self, name: str, child: ManagedChild | None, pid_path: Path | None) -> None:
        """Take over a child started by the boot sequence."""
        with self._lock:
            self.children[name] = child
            self.pid_paths[name] = pid_path

    def child(self, name: str) -> ManagedChild | None:
        return self.children.get(name)

    def _assets(self, name: str) -> list[str]:
        """Missing components, cached: ``find_soft_reference`` globs the disk."""
        cached = self._assets_cache.get(name)
        if cached and time.monotonic() - cached[0] < 20.0:
            return cached[1]
        if name == "tts":
            missing, _ref, _python_, _server = soft_tts_components(self.config)
        elif name == "embedding":
            missing, _python_ = embedding_components(self.config)
        else:
            missing = []
        self._assets_cache[name] = (time.monotonic(), missing)
        return missing

    def _pid_from_file(self, name: str) -> int | None:
        pid_path = self.pid_paths.get(name) or (self.run_dir / f"{name}.pid")
        pid = read_pid(pid_path)
        return pid if pid and pid_alive(pid) else None

    def _port_of(self, name: str) -> int | None:
        info = SERVICE_REGISTRY.get(name)
        return info.port if info else None

    def _health(self, name: str) -> dict[str, Any] | None:
        if name == "tts":
            return http_health(f"http://{TTS_HOST}:{TTS_PORT}/health")
        if name == "embedding":
            return http_health(f"http://{EMBED_HOST}:{EMBED_PORT}/health")
        return None

    # --------------------------------------------------------------- status
    def status(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in self.NAMES:
            port = self._port_of(name)
            child = self.children.get(name)
            alive = child is not None and child.poll() is None
            port_up = bool(port) and port_open(COMFY_HOST, port)
            pid = child.pid if alive and child is not None else self._pid_from_file(name)
            if alive:
                started_by = "supervisor"
                state = "running" if (not port or port_up) else "starting"
            elif port_up:
                started_by = "external"
                state = "external"
                pid = pid or pid_on_port(port or 0)
            else:
                started_by = "none"
                state = "stopped"
            health = self._health(name) if state in ("running", "external") else None
            missing = self._assets(name) if name in ("tts", "embedding") else []
            rows.append(
                {
                    "name": name,
                    "label": SERVICE_REGISTRY[name].label,
                    "state": state,
                    "started_by": started_by,
                    "pid": pid,
                    "port": port,
                    "healthy": bool(health.get("ok")) if health else False,
                    "loaded": bool(health.get("loaded")) if health else None,
                    "assets_ok": not missing,
                    "missing": missing,
                    "controllable": SERVICE_REGISTRY[name].supervised,
                    "detail": self.notes.get(name, ""),
                }
            )
        return rows

    # ---------------------------------------------------------------- start
    def start(self, name: str) -> dict[str, Any]:
        info = SERVICE_REGISTRY.get(name)
        if info is None:
            raise ControlError("unknown_service", f"未知服务 {name}", 404)
        if not info.supervised:
            raise ControlError(
                "not_owned",
                f"{info.label} 由拍照服务按需拉起；这里只能停止或释放显存",
                409,
            )
        with self._lock:
            if self.children.get(name) is not None and self.children[name].poll() is None:
                raise ControlError("already_running", "已经在运行", 409)
            if name != "qq":
                # Check configuration before the port: "this build has no local
                # TTS" is a better answer than "something else holds 9880",
                # which would invite the user to kill an unrelated process.
                self._check_enabled(name)
            port = self._port_of(name)
            if port and port_open(TTS_HOST, port):
                raise ControlError(
                    "external_owner",
                    f"端口 {port} 已被其他进程占用（不是一键启动拉起的）。"
                    "先点「停止」把它收掉，再从这里启动。",
                    409,
                )
            if name == "tts":
                child = self._spawn_tts()
            elif name == "embedding":
                child = self._spawn_embedding()
            else:
                child = self._spawn_qq()
            self.notes.pop(name, None)
            return {"state": "running", "pid": child.pid, "detail": f"PID {child.pid}"}

    def _check_enabled(self, name: str) -> None:
        if name == "tts":
            if not bool(self.config.get("tts.enabled", False)):
                raise ControlError("disabled", "tts.enabled=false，本地语音没启用", 409)
            if str(self.config.get("tts.mode", "http_json")) != "http_json":
                raise ControlError("disabled", "tts.mode 不是 http_json", 409)
        elif name == "embedding":
            if not embedding_enabled(self.config):
                raise ControlError(
                    "disabled",
                    "memory.embedding_backend 不是 HTTP 后端，用哈希向量就够",
                    409,
                )

    def _assert_child_alive(
        self,
        name: str,
        child: ManagedChild,
        pid_path: Path | None,
    ) -> None:
        """Reject a spawn that lost the port race and exited immediately.

        The pre-spawn port check is only a hint: two simultaneous starts can
        both pass it.  The loser then exits after ``child.start()`` (its bind
        or single-instance lock fails) while the winner serves the port, so a
        health probe would happily succeed against the other process.  Checking
        our own child's exit code is what makes the race visible.
        """
        if child.poll() is None:
            return
        if pid_path is not None:
            remove_pid(pid_path, child.pid)
        self.children[name] = None
        self.pid_paths[name] = None
        raise ControlError(
            "spawn_failed",
            f"{name} 启动后立即退出（code {child.returncode}）；"
            "端口或单实例锁可能已被另一个进程抢先占用。",
            500,
        )

    def _spawn_tts(self) -> ManagedChild:
        self._check_enabled("tts")
        missing, _reference, python, server = soft_tts_components(self.config)
        if missing:
            raise ControlError("missing_assets", "缺组件：" + "、".join(missing), 409)
        child = ManagedChild(
            "tts",
            tts_command(
                python,
                server,
                idle_unload_seconds=tts_idle_unload_seconds(self.config),
                warmup=tts_warmup(self.config),
                pitch_semitones=tts_pitch_semitones(self.config),
            ),
            self.logs_dir / "tts.log",
            profile=self.profile,
        )
        try:
            child.start()
        except OSError as exc:
            raise ControlError("spawn_failed", f"启动失败：{exc}", 500) from None
        pid_path = self.run_dir / "tts.pid"
        if child.pid:
            write_pid(pid_path, child.pid)
        self.children["tts"] = child
        self.pid_paths["tts"] = pid_path
        if not wait_for_health(
            f"http://{TTS_HOST}:{TTS_PORT}/health", timeout=TTS_READY_TIMEOUT, want_loaded=False
        ):
            # The process is up but the HTTP port is not answering yet; that is
            # a slow start, not a failure -- the model loads on first request.
            self.notes["tts"] = "已启动，HTTP 端口还在初始化"
        self._assert_child_alive("tts", child, pid_path)
        return child

    def _spawn_embedding(self) -> ManagedChild:
        self._check_enabled("embedding")
        missing, python = embedding_components(self.config)
        if missing:
            raise ControlError("missing_assets", "缺组件：" + "、".join(missing), 409)
        child = ManagedChild(
            "embedding", embedding_command(python), self.logs_dir / "embedding.log",
            profile=self.profile,
        )
        try:
            child.start()
        except OSError as exc:
            raise ControlError("spawn_failed", f"启动失败：{exc}", 500) from None
        pid_path = self.run_dir / "embedding.pid"
        if child.pid:
            write_pid(pid_path, child.pid)
        self.children["embedding"] = child
        self.pid_paths["embedding"] = pid_path
        if not wait_for_health(
            f"http://{EMBED_HOST}:{EMBED_PORT}/health", timeout=EMBED_READY_TIMEOUT
        ):
            self.notes["embedding"] = "已启动，模型还在加载"
        self._assert_child_alive("embedding", child, pid_path)
        return child

    def _spawn_qq(self) -> ManagedChild:
        if not _qq_access_token(self.config):
            raise ControlError(
                "not_configured",
                "没有 QQ_ACCESS_TOKEN：先扫码登录，把 token 写进 .env",
                409,
            )
        if not self.websocket_ready:
            raise ControlError(
                "missing_dependency",
                '缺少 websocket-client，安装：pip install "websocket-client>=1.7"',
                409,
            )
        child = ManagedChild(
            "qq", qq_command(self.profile), self.logs_dir / "qq.log", profile=self.profile
        )
        try:
            child.start()
        except OSError as exc:
            raise ControlError("spawn_failed", f"启动失败：{exc}", 500) from None
        pid_path = self.run_dir / "qq.pid"
        if child.pid:
            write_pid(pid_path, child.pid)
        self.children["qq"] = child
        self.pid_paths["qq"] = pid_path
        if not port_open(NAPCAT_HOST, NAPCAT_PORT):
            self.notes["qq"] = "适配器已启动，等待 NapCat 登录"
        self._assert_child_alive("qq", child, pid_path)
        return child

    # ----------------------------------------------------------------- stop
    def stop(self, name: str) -> dict[str, Any]:
        if name == "comfyui":
            return self._stop_comfyui()
        child = self.children.get(name)
        if child is not None and child.poll() is None:
            pid = child.pid
            child.stop(tree=True)
            pid_path = self.pid_paths.get(name)
            if pid_path is not None:
                remove_pid(pid_path, pid)
            if name == "tts":
                remove_pid(self.run_dir / "tts_soft.pid", pid)
            self.children[name] = None
            self.pid_paths[name] = None
            self.notes.pop(name, None)
            return {"state": "stopped", "pid": pid, "detail": "已停止"}
        # Not ours: maybe a manual run from an earlier session holds the port.
        port = self._port_of(name)
        if port:
            pid = pid_on_port(port)
            if pid and kill_tree(pid):
                remove_pid(self.run_dir / f"{name}.pid", pid)
                return {"state": "stopped", "pid": pid, "detail": "已停止（外部进程）"}
        raise ControlError("not_running", "这个服务现在没有在运行", 409)

    def _stop_comfyui(self) -> dict[str, Any]:
        pid = pid_on_port(COMFY_PORT)
        if not pid:
            raise ControlError("not_running", "ComfyUI 没有在运行", 409)
        if not kill_tree(pid):
            raise ControlError("stop_failed", "结束 ComfyUI 失败（可能需要管理员权限）", 500)
        return {"state": "stopped", "pid": pid, "detail": "已停止（由拍照服务拉起）"}

    # --------------------------------------------------------------- unload
    def unload(self, name: str) -> dict[str, Any]:
        if name != "tts":
            raise ControlError("unsupported", "只有 TTS 支持卸载显存", 400)
        if not port_open(TTS_HOST, TTS_PORT):
            raise ControlError("not_running", "TTS 没有在运行", 409)
        payload = http_post(f"http://{TTS_HOST}:{TTS_PORT}/unload")
        if payload is None:
            raise ControlError("unload_failed", "TTS 没有返回结果", 502)
        if payload.get("ok") is False:
            raise ControlError("unload_failed", str(payload.get("error") or ""), 502)
        released = bool(payload.get("unloaded"))
        return {
            "state": "running",
            "detail": "显存已释放（进程保留）" if released else "本来就没加载模型",
        }

    # -------------------------------------------------------------- release
    def release(self) -> dict[str, Any]:
        """Give the VRAM back. Best effort over every step, never raises."""
        steps: list[str] = []
        comfy_pid = pid_on_port(COMFY_PORT)
        if comfy_pid:
            steps.append(
                "ComfyUI 已停止" if kill_tree(comfy_pid) else f"ComfyUI 停止失败（PID {comfy_pid}）"
            )
        else:
            steps.append("ComfyUI 未运行")
        if port_open(TTS_HOST, TTS_PORT):
            payload = http_post(f"http://{TTS_HOST}:{TTS_PORT}/unload")
            if payload and payload.get("ok") is not False:
                steps.append("TTS 显存已释放（进程保留）" if payload.get("unloaded") else "TTS 本来未加载")
            else:
                steps.append("TTS 卸载失败")
        else:
            steps.append("TTS 未运行")
        # The embedding server is CPU-only. Stopping it would silently drop
        # sticker matching to keyword overlap, so it is left alone on purpose.
        return {"state": "done", "detail": "；".join(steps)}

    # ---------------------------------------------------------------- crash
    def poll_crashes(self) -> list[str]:
        """Report children that exited on their own, once each."""
        messages: list[str] = []
        for name, child in list(self.children.items()):
            if child is None or child.poll() is None:
                continue
            code = child.returncode
            if name == "qq":
                messages.append(f"QQ 适配器已退出（code {code}），Web 继续运行。")
            elif name == "tts":
                messages.append(f"本地 TTS 服务已退出（code {code}），Web/QQ 继续。")
            elif name == "embedding":
                messages.append(
                    f"本地向量服务已退出（code {code}），贴纸退回哈希匹配，Web/QQ 继续。"
                )
            pid = child.pid
            child.stop()
            pid_path = self.pid_paths.get(name)
            if pid_path is not None:
                remove_pid(pid_path, pid)
            if name == "tts":
                remove_pid(self.run_dir / "tts_soft.pid", pid)
            self.children[name] = None
            self.pid_paths[name] = None
        return messages

    def stop_all(self) -> None:
        for name in ("qq", "tts", "embedding"):
            child = self.children.get(name)
            if child is None:
                continue
            pid = child.pid
            try:
                child.stop(tree=True)
            except Exception:  # noqa: BLE001 - shutdown must reach every child
                pass
            pid_path = self.pid_paths.get(name)
            if pid_path is not None:
                remove_pid(pid_path, pid)
            if name == "tts":
                remove_pid(self.run_dir / "tts_soft.pid", pid)
            self.children[name] = None


def print_bili_status(config: Any) -> None:
    cookie_path = _bili_cookie_path(config)
    env_cookie = bool(
        os.environ.get("BILIBILI_COOKIE") or os.environ.get("BILIBILI_SESSDATA")
    )
    if cookie_path.is_file() and cookie_path.stat().st_size > 0:
        say(f"[B站] B站已登录（Cookie 文件：{cookie_path}）")
        say("[B站] 只读模式；需要换号时双击 B站登录.bat")
    elif env_cookie:
        say("[B站] B站已登录（环境变量 Cookie）")
    else:
        say("[B站] B站未登录：双击 B站登录.bat")


# --------------------------------------------------------------------------- main
def _print_summary(
    url: str,
    profile: str,
    web_pid_path: Path,
    logs_dir: Path,
    runtime: ServiceRuntime,
    control: ControlPlane | None = None,
) -> None:
    say("")
    say("=" * 52)
    say(" 大肥鱼已启动")
    say(f" Web:   {url}")
    say(f" 配置:  {profile}")
    qq_child = runtime.child("qq")
    tts_child = runtime.child("tts")
    emb_child = runtime.child("embedding")
    if qq_child is not None:
        say(f" QQ:    已启动（PID {qq_child.pid}）")
    if tts_child is not None:
        say(f" TTS:   本地软声服务启动中（PID {tts_child.pid}）")
    if emb_child is not None:
        say(f" 向量:  本地向量服务（PID {emb_child.pid}，端口 {EMBED_PORT}）")
    if control is not None:
        say(f" 控制:  http://{control.host}:{control.port}（Web 里点「服务」即可）")
    say(f" 日志:  {logs_dir}")
    say(f" PID:   {web_pid_path.parent}")
    say(" 停止:  在本窗口按 Ctrl+C")
    say("=" * 52)


def run(args: argparse.Namespace) -> int:
    _configure_stdio()
    os.chdir(ROOT)

    if sys.version_info < MIN_PYTHON:
        current = ".".join(str(part) for part in sys.version_info[:3])
        warn(f"需要 Python 3.11 或更新版本，当前是 {current}。")
        say("请安装 Python 3.11+：https://www.python.org/downloads/")
        say('安装时勾选 "Add python.exe to PATH"，然后重新双击 启动大肥鱼.bat。')
        return 1

    profile = args.profile or os.environ.get("FATFISH_PROFILE") or "low"
    try:
        config = _load_config(args.profile)
    except Exception as exc:  # noqa: BLE001 - a broken config must not traceback
        warn(f"读取配置失败：{exc}")
        return 1
    profile = str(getattr(config, "profile", profile) or profile)
    config.ensure_dirs()
    # Give every child the supervisor's resolved data directory.  The TTS and
    # embedding scripts are launched outside the package and only read the env
    # var, so without this a custom app.data_dir would put their single-instance
    # locks (and the shared VRAM lease) in a different directory.
    os.environ["FATFISH_DATA_DIR"] = str(config.data_dir)

    data_dir = Path(config.data_dir)
    logs_dir = data_dir / "logs"
    run_dir = data_dir / "run"
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)

    lock = acquire_supervisor_lock(run_dir / "supervisor.lock")
    if lock is None:
        pid = read_pid(run_dir / "supervisor.pid")
        warn(f"已经有一个大肥鱼在运行（PID {pid or '未知'}）。请先关闭旧窗口。")
        return 1

    web_child: ManagedChild | None = None
    web_pid_path = run_dir / "web.pid"
    supervisor_pid_path = run_dir / "supervisor.pid"
    stop_event = threading.Event()
    control: ControlPlane | None = None

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError, RuntimeError):
            pass

    web_ready = _module_available("websocket")
    qrcode_ready = _module_available("qrcode")
    runtime = ServiceRuntime(
        config,
        profile,
        run_dir=run_dir,
        logs_dir=logs_dir,
        websocket_ready=web_ready,
    )

    try:
        clean_stale_pid_files(run_dir)
        write_pid(supervisor_pid_path, os.getpid())

        say("")
        say("大肥鱼一键启动")
        say(f"目录: {ROOT}")
        say(f"配置: {profile}")
        if not web_ready:
            say('[依赖] 缺少 websocket-client：QQ 适配器不会启动。安装：pip install "websocket-client>=1.7"')
        if not qrcode_ready:
            say('[依赖] 缺少 qrcode：B站二维码不会保存 PNG（URL 仍会打印）。安装：pip install "qrcode>=7.4"')

        # The control port is what lets the web UI start/stop backends later
        # without becoming a second owner of the processes. It is optional: if
        # the port is taken the stack still runs, the panel just says so.
        if not args.no_control:
            control = ControlPlane(runtime, run_dir / CONTROL_TOKEN_NAME,
                                   host=CONTROL_HOST, port=CONTROL_PORT)
            try:
                control.start()
                say(f"[控制] 后端控制口 http://{CONTROL_HOST}:{CONTROL_PORT}（仅本机，需 token）")
            except OSError as exc:
                warn(f"[控制] 端口 {CONTROL_PORT} 已被占用，服务面板不可用：{exc}")
                control = None

        tts_child, tts_pid_path = start_tts(
            config,
            profile=profile,
            disabled=bool(args.no_tts or args.web_only),
            run_dir=run_dir,
            logs_dir=logs_dir,
        )
        runtime.adopt("tts", tts_child, tts_pid_path)

        # Before Web: the sticker service reads vectors on the first turn, and a
        # cold model would answer that turn with hash-width vectors that score 0
        # against the stored ones.
        emb_child, emb_pid_path = start_embedding(
            config,
            profile=profile,
            disabled=bool(args.no_embedding or args.web_only),
            run_dir=run_dir,
            logs_dir=logs_dir,
        )
        runtime.adopt("embedding", emb_child, emb_pid_path)

        token = ""
        try:
            web_child, url, token, web_pid_path = start_web(
                profile, args.port, run_dir=run_dir, logs_dir=logs_dir
            )
        except OSError as exc:
            warn(f"Web 服务启动失败：{exc}")
            return 1
        if not url:
            warn(f"Web 服务在 {int(STARTUP_TIMEOUT)} 秒内没有打印登录地址，正在停止。")
            for line in list(web_child.lines)[-15:]:
                say(f"  {redact(line, (token,))}")
            return 1

        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - headless environments
                pass

        qq_child, qq_pid_path = start_qq(
            config,
            profile,
            disabled=bool(args.web_only or args.no_qq),
            websocket_ready=web_ready,
            run_dir=run_dir,
            logs_dir=logs_dir,
        )
        runtime.adopt("qq", qq_child, qq_pid_path)
        print_bili_status(config)
        _print_summary(url, profile, web_pid_path, logs_dir, runtime, control)

        exit_code = 0
        while not stop_event.wait(0.5):
            if stop_event.is_set():
                break
            if web_child.poll() is not None:
                if _interrupted_exit(web_child.returncode):
                    break
                warn(f"Web 服务意外退出（code {web_child.returncode}），正在停止其他服务。")
                exit_code = web_child.returncode or 1
                break
            for message in runtime.poll_crashes():
                warn(message)

        say("")
        say("[fatfish] 正在停止…")
        return exit_code
    finally:
        if control is not None:
            # Stop answering first: a start job must not spawn a child while
            # the shutdown below is walking the same dict.
            control.stop()
        runtime.stop_all()
        if web_child is not None:
            pid = web_child.pid
            web_child.stop()
            remove_pid(web_pid_path, pid)
        remove_pid(supervisor_pid_path, os.getpid())
        release_supervisor_lock(lock)
        say("[fatfish] 已停止，没有遗留子进程。")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
