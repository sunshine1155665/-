"""Local backend control plane: who may start a service, and who may stop it.

The stack has four backends -- CosyVoice TTS (9880), the bge embedding server
(9881), ComfyUI (8188) and the QQ adapter -- and on this machine two of them
share 8 GB of VRAM, so they cannot be alive at the same time. Being able to
start and release them from the web UI is convenient; being able to start them
from *two* owners is how you get an orphan that holds a port nobody can
reclaim, because Windows does not kill grandchildren when a parent dies.

So ownership does not move: ``scripts/start_all.py`` still spawns every
backend, and the UI is only allowed to *ask it to*. This module is both halves
of that channel:

* :class:`ControlPlane` -- the HTTP server that lives inside the supervisor.
  Loopback only, token only, whitelist only, asynchronous only.
* :class:`ControlClient` -- what the web process uses to talk to it.

Everything here is stdlib, because ``start_all.py`` imports it before the
project's dependencies have been checked.

Design notes that are easy to get wrong later:

* **Actions are asynchronous.** A cold ComfyUI takes 30-90 s; a synchronous
  HTTP handler would time out and the UI would think the start failed while it
  actually succeeded. Every mutating call returns a job id immediately.
* **One job per service.** A second ``start`` while one is in flight is a 409,
  not a second spawn attempt, otherwise a double-click races two children onto
  the same port and the loser fails silently.
* **``release`` means "give the VRAM back"**, not "kill everything". Unloading
  the TTS model keeps the process (and saves the reload on the next turn);
  stopping ComfyUI is what actually frees the big block. The embedding server
  is deliberately left alone: it runs on CPU, costs little, and stopping it
  silently degrades sticker matching to keyword overlap.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 9882
CONTROL_TOKEN_NAME = "control.token"
# The token alone is not enough to connect: the plane may have been given a
# different port (tests use an ephemeral one, and a future fallback might too).
# Keeping the address in its own file means the client never has to guess.
CONTROL_ENDPOINT_NAME = "control.endpoint"
CONTROL_TOKEN_HEADER = "X-Fatfish-Control"


@dataclass(frozen=True)
class ServiceInfo:
    """One entry of the single source of truth for the service list.

    ``scripts/start_all.py`` and the control-plane API both derive their
    name/label/port tables from :data:`SERVICE_REGISTRY`, so adding a service
    (e.g. a future long-running model server) is a one-line change here
    instead of six scattered edits that drift apart.
    """

    name: str
    label: str
    port: int | None = None
    host: str = "127.0.0.1"
    # False for services another component owns and starts on demand
    # (ComfyUI is pulled up by the photo service) -- the supervisor shows
    # them but refuses to start them.
    supervised: bool = True
    # Participates in the single-8GB-card arbitration (vram.lock).
    owns_vram: bool = False


SERVICE_REGISTRY: dict[str, ServiceInfo] = {
    "tts": ServiceInfo("tts", "本地语音 TTS", 9880, owns_vram=True),
    "embedding": ServiceInfo("embedding", "向量服务", 9881),
    "comfyui": ServiceInfo("comfyui", "ComfyUI 生图", 8188, supervised=False, owns_vram=True),
    "qq": ServiceInfo("qq", "QQ 适配器", None),
}

# Whitelist, in display order. Nothing outside this tuple can be addressed by
# the API, which is also why no request value can ever reach a command line.
SERVICES = tuple(SERVICE_REGISTRY)
LABELS = {name: info.label for name, info in SERVICE_REGISTRY.items()}
SERVICE_ACTIONS = ("start", "stop", "unload")

MAX_BODY_BYTES = 8 * 1024
JOB_HISTORY = 20


class ControlError(Exception):
    """A refusal the caller is expected to show the user as-is."""

    def __init__(self, code: str, message: str = "", status: int = 400):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = status


# --------------------------------------------------------------------------- token
def token_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "run" / CONTROL_TOKEN_NAME


def write_token(path: Path, token: str) -> None:
    """Write the per-run token; ``0600`` where the OS honours it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    temp.write_text(token + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def read_token(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return raw or None


def endpoint_path(near: str | Path) -> Path:
    """The address file lives next to the token file."""
    path = Path(near)
    return path.with_name(CONTROL_ENDPOINT_NAME)


def write_endpoint(path: Path, host: str, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    temp.write_text(f"{host}:{int(port)}\n", encoding="utf-8")
    temp.replace(path)


def read_endpoint(path: Path) -> tuple[str, int] | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    host, _, port = raw.rpartition(":")
    if not host:
        return None
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return None


def remove_endpoint(path: Path, port: int | None = None) -> None:
    if port is not None:
        current = read_endpoint(path)
        if current is not None and current[1] != int(port):
            return
    try:
        path.unlink()
    except OSError:
        pass


def remove_token(path: Path, token: str | None = None) -> None:
    """Delete the token, but only if it is still ours."""
    if token is not None:
        current = read_token(path)
        if current is not None and current != token:
            return
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- runtime
class ServiceRuntime(Protocol):
    """What the control plane needs from whoever owns the processes.

    ``start``/``stop``/``unload``/``release`` may block (a cold start does) and
    are therefore always executed on a worker thread, never in the handler.
    """

    def status(self) -> list[dict[str, Any]]: ...

    def start(self, name: str) -> dict[str, Any]: ...

    def stop(self, name: str) -> dict[str, Any]: ...

    def unload(self, name: str) -> dict[str, Any]: ...

    def release(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- client
class ControlClient:
    """Thin blocking client for the control plane.

    Every method raises :class:`ControlError` on refusal and
    ``OSError``/``urllib.error.URLError`` when the plane is not running, so the
    caller can tell "the supervisor disagrees" from "there is no supervisor".
    """

    def __init__(
        self,
        token_file: str | Path,
        *,
        host: str | None = None,
        port: int | None = None,
        timeout: float = 3.0,
    ):
        self.token_file = Path(token_file)
        # ``None`` means "ask the endpoint file the supervisor wrote", which is
        # also what lets the client survive a supervisor restart on a new port.
        self._host = host
        self._port = port
        self.timeout = float(timeout)

    @property
    def host(self) -> str:
        return self._host or (self._resolve() or (CONTROL_HOST, CONTROL_PORT))[0]

    @property
    def port(self) -> int:
        if self._port is not None:
            return int(self._port)
        return (self._resolve() or (CONTROL_HOST, CONTROL_PORT))[1]

    def _resolve(self) -> tuple[str, int] | None:
        return read_endpoint(endpoint_path(self.token_file))

    # -- plumbing
    def _token(self) -> str:
        token = read_token(self.token_file)
        if not token:
            raise ControlError("no_supervisor", "控制口未启动（找不到 token）", 503)
        return token

    def _call(self, method: str, path: str, timeout: float | None = None) -> dict[str, Any]:
        url = f"http://{self.host}:{self.port}{path}"
        request = urllib.request.Request(url, method=method)
        request.add_header(CONTROL_TOKEN_HEADER, self._token())
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                raw = response.read(MAX_BODY_BYTES * 4)
        except urllib.error.HTTPError as exc:
            raise self._error_from(exc) from None
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControlError("bad_response", "控制口返回的不是 JSON", 502) from None
        if not isinstance(payload, dict):
            raise ControlError("bad_response", "控制口返回格式不对", 502)
        if payload.get("ok") is False:
            raise ControlError(
                str(payload.get("error") or "refused"),
                str(payload.get("detail") or ""),
                400,
            )
        return payload

    @staticmethod
    def _error_from(exc: urllib.error.HTTPError) -> ControlError:
        code, detail = "http_error", ""
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
            if isinstance(body, dict):
                code = str(body.get("error") or code)
                detail = str(body.get("detail") or "")
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        return ControlError(code, detail or f"控制口返回 {exc.code}", int(exc.code or 400))

    # -- api
    def available(self) -> bool:
        # ``/health`` is a constant-time liveness check.  ``/services`` builds a
        # full snapshot and probes the TTS/embedding health endpoints, which can
        # take seconds when a backend is wedged; using it here made every
        # Runtime construction pay that cost even when the supervisor was down.
        if self._host is None and self._port is None and self._resolve() is None:
            return False
        try:
            self._call("GET", "/health", timeout=0.5)
        except (ControlError, OSError, urllib.error.URLError, ValueError):
            return False
        return True

    def snapshot(self) -> dict[str, Any]:
        """Everything the panel needs in one call, jobs included.

        The jobs matter as much as the services: ``start`` returns 202 the
        moment the work is queued, so "actually it failed" only ever shows up
        here. Without them the UI would sit on a dead click for 90 seconds.

        The timeout is looser than the default because building a snapshot
        probes two ``/health`` endpoints, and a service that is up but slow
        would otherwise look like a supervisor that is not there at all.
        """
        return self._call("GET", "/services", timeout=max(self.timeout, 6.0))

    def status(self) -> list[dict[str, Any]]:
        services = self.snapshot().get("services")
        return services if isinstance(services, list) else []

    def start(self, name: str) -> dict[str, Any]:
        return self._job_of(self._call("POST", f"/services/{_name(name)}/start"))

    def stop(self, name: str) -> dict[str, Any]:
        return self._job_of(self._call("POST", f"/services/{_name(name)}/stop"))

    def unload(self, name: str) -> dict[str, Any]:
        return self._job_of(self._call("POST", f"/services/{_name(name)}/unload"))

    def release(self) -> dict[str, Any]:
        return self._job_of(self._call("POST", "/services/release"))

    @staticmethod
    def _job_of(payload: dict[str, Any]) -> dict[str, Any]:
        job = payload.get("job")
        if not isinstance(job, dict):
            raise ControlError("bad_response", "控制口没有返回 job", 502)
        return job

    def job(self, job_id: str) -> dict[str, Any]:
        payload = self._call("GET", f"/jobs/{_name(job_id)}")
        job = payload.get("job")
        return job if isinstance(job, dict) else {}

    def wait_for(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll: float = 0.2,
    ) -> dict[str, Any]:
        """Block until a queued job reaches ``done`` or ``failed``.

        Callers that must not proceed until the service is actually down
        (PhotoService before starting ComfyUI) need this; the async API alone
        only tells them the request was accepted.
        """
        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            job = self.job(job_id)
            state = str(job.get("state") or "")
            if state in ("done", "failed"):
                return job
            if time.monotonic() >= deadline:
                raise ControlError(
                    "timeout",
                    f"控制任务 {job_id} 在 {timeout:g}s 内没有完成",
                    504,
                )
            time.sleep(max(0.02, poll))

    def stop_and_wait(self, name: str, timeout: float = 45.0) -> dict[str, Any]:
        job = self.stop(name)
        return self.wait_for(str(job.get("id") or ""), timeout=timeout)

    def start_and_wait(self, name: str, timeout: float = 90.0) -> dict[str, Any]:
        job = self.start(name)
        return self.wait_for(str(job.get("id") or ""), timeout=timeout)

    def unload_and_wait(self, name: str, timeout: float = 30.0) -> dict[str, Any]:
        job = self.unload(name)
        return self.wait_for(str(job.get("id") or ""), timeout=timeout)


def _name(value: Any) -> str:
    """Reject anything that is not a plain identifier before it reaches a URL."""
    text = str(value or "").strip().lower()
    if not text or len(text) > 40 or not all(c.isalnum() or c in "-_" for c in text):
        raise ControlError("bad_name", "非法名称", 400)
    return text


def default_token_path() -> Path:
    """Token location without importing the config layer (start_all loads us early)."""
    import os

    raw = os.environ.get("FATFISH_DATA_DIR")
    if raw:
        return token_path(raw)
    root = Path(__file__).resolve().parents[1]
    return token_path(root / "data")


_MANAGER: ControlClient | None = None
_MANAGER_TOKEN: Path | None = None
_MANAGER_AVAILABLE = False
_MANAGER_CHECKED_AT = 0.0
# One failed probe per photo job is enough.  The result is cached for this long
# so a burst of photo requests (or a Runtime constructed for every chat turn)
# cannot repeatedly wait on a dead control port.  The client still re-resolves
# the endpoint file on every call, so a supervisor restart on a new port is
# picked up as soon as the cache expires.
_MANAGER_TTL_SECONDS = 30.0
_MANAGER_LOCK = threading.Lock()


def get_service_manager(token_file: str | Path | None = None) -> ControlClient | None:
    """The control client, or ``None`` when no supervisor is listening.

    ``fatfish.photo`` duck-types this hook: it tolerates a manager that knows
    nothing about ComfyUI/TTS (``_manager_call`` returns False) and keeps full
    control itself. Returning ``None`` when the plane is down keeps a web
    started on its own -- ``python -m fatfish serve`` without a supervisor --
    behaving exactly as before.

    Discovery is deliberately lazy and cached: the web process constructs a new
    ``Runtime`` for every chat turn, and a direct ``available()`` check against a
    supervisor that is not listening can block for the full connect timeout.
    ``PhotoService`` only asks for a manager when it actually needs one.
    """
    global _MANAGER, _MANAGER_TOKEN, _MANAGER_AVAILABLE, _MANAGER_CHECKED_AT
    path = Path(token_file) if token_file is not None else default_token_path()
    with _MANAGER_LOCK:
        if _MANAGER is None or _MANAGER_TOKEN != path:
            _MANAGER = ControlClient(path)
            _MANAGER_TOKEN = path
            _MANAGER_AVAILABLE = False
            _MANAGER_CHECKED_AT = 0.0
        now = time.monotonic()
        if now - _MANAGER_CHECKED_AT >= _MANAGER_TTL_SECONDS:
            _MANAGER_AVAILABLE = _MANAGER.available()
            _MANAGER_CHECKED_AT = now
        return _MANAGER if _MANAGER_AVAILABLE else None


def reset_service_manager() -> None:
    """Drop the cached client and its availability result."""
    global _MANAGER, _MANAGER_TOKEN, _MANAGER_AVAILABLE, _MANAGER_CHECKED_AT
    with _MANAGER_LOCK:
        _MANAGER = None
        _MANAGER_TOKEN = None
        _MANAGER_AVAILABLE = False
        _MANAGER_CHECKED_AT = 0.0


# --------------------------------------------------------------------------- plane
def _job_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> float:
    return round(time.time(), 3)


class _ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR permits two listeners on one port.  A second control
    # plane would then split start/stop requests between two owners.  The
    # supervisor file lock already prevents this, but the socket should not
    # depend on a different module for its safety.
    allow_reuse_address = os.name != "nt"


class ControlPlane:
    """HTTP server exposing the supervisor's services on loopback."""

    def __init__(
        self,
        runtime: ServiceRuntime,
        token_file: str | Path,
        *,
        host: str = CONTROL_HOST,
        port: int = CONTROL_PORT,
    ):
        self.runtime = runtime
        self.token_file = Path(token_file)
        self.host = host
        self.port = int(port)
        self.token = secrets.token_urlsafe(24)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_order: list[str] = []
        self._busy: dict[str, str] = {}

    # -- lifecycle
    def start(self) -> str:
        """Bind, spawn the serving thread, and publish the token. Returns the URL."""
        server = _ControlServer((self.host, self.port), _ControlHandler)
        server.control = self  # type: ignore[attr-defined]
        # ``port=0`` is allowed (tests); the real one matters to the Host check.
        self.port = int(server.server_address[1])
        write_token(self.token_file, self.token)
        write_endpoint(endpoint_path(self.token_file), self.host, self.port)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="fatfish-control", daemon=True
        )
        self._thread.start()
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except (OSError, RuntimeError):
                pass
            try:
                server.server_close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3)
        remove_token(self.token_file, self.token)
        remove_endpoint(endpoint_path(self.token_file), self.port)

    # -- jobs
    def submit(self, action: str, name: str | None = None) -> dict[str, Any]:
        """Queue one action and return its job; never runs it on this thread."""
        if action not in ("start", "stop", "unload", "release"):
            raise ControlError("bad_action", f"未知动作 {action}", 400)
        if action != "release":
            if name not in SERVICES:
                raise ControlError("unknown_service", f"未知服务 {name}", 404)
        key = name or "release"
        with self._lock:
            running = self._busy.get(key)
            if running is not None:
                raise ControlError(
                    "busy",
                    f"{LABELS.get(name or '', '释放')} 正在处理中（job {running}），请稍候",
                    409,
                )
            job_id = _job_id()
            self._jobs[job_id] = {
                "id": job_id,
                "action": action,
                "service": name,
                "state": "queued",
                "started_at": _now(),
                "finished_at": None,
                "detail": "",
                "error": "",
            }
            self._job_order.append(job_id)
            self._busy[key] = job_id
            while len(self._job_order) > JOB_HISTORY:
                self._jobs.pop(self._job_order.pop(0), None)
            # Snapshot before the worker exists: otherwise a fast action can
            # already be "done" by the time we serialise the reply and the
            # caller sees a state it cannot reason about.
            snapshot = dict(self._jobs[job_id])
        threading.Thread(
            target=self._run_job,
            args=(job_id, key, action, name),
            name=f"fatfish-control-{action}",
            daemon=True,
        ).start()
        return snapshot

    def _run_job(self, job_id: str, key: str, action: str, name: str | None) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job["state"] = "running"
        try:
            if action == "release":
                result = self.runtime.release()
            else:
                result = getattr(self.runtime, action)(name)  # type: ignore[arg-type]
            job["state"] = "done"
            job["detail"] = str((result or {}).get("detail") or (result or {}).get("state") or "")
        except ControlError as exc:
            job["state"] = "failed"
            job["error"] = exc.message or exc.code
        except Exception as exc:  # noqa: BLE001 - a job failure must not kill the plane
            job["state"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            job["finished_at"] = _now()
            with self._lock:
                if self._busy.get(key) == job_id:
                    self._busy.pop(key, None)

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._jobs[i]) for i in reversed(self._job_order)]

    def job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return dict(job) if job else None

    # -- views
    def snapshot(self) -> dict[str, Any]:
        try:
            services = self.runtime.status()
        except Exception as exc:  # noqa: BLE001 - status must always answer
            services = []
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = ""
        return {
            "ok": True,
            "control": {"host": self.host, "port": self.port},
            "services": services,
            "jobs": self.jobs(),
            "detail": detail,
        }


class _ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fatfish-control"
    sys_version = ""

    # ------------------------------------------------------------------ verbs
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - quiet
        return

    @property
    def plane(self) -> ControlPlane:
        return self.server.control  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ security
    def _host_ok(self) -> bool:
        """Reject requests addressed to some other name than our loopback.

        Without this, a page on ``evil.example`` can point its own DNS at
        127.0.0.1 and have the browser talk to us with ``Host: evil.example``
        (DNS rebinding). The token already stops it, but two independent
        reasons to say no is cheaper than one that has to be perfect.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        return host in {f"{CONTROL_HOST}:{port}", f"localhost:{port}"}

    def _authorized(self) -> bool:
        if not self._host_ok():
            return False
        # A browser always attaches Origin to a cross-site request and a same-
        # site fetch would carry our own origin; any legitimate client is
        # server-side, so the presence of Origin means "not us".
        if self.headers.get("Origin") or self.headers.get("Referer"):
            return False
        token = (self.headers.get(CONTROL_TOKEN_HEADER) or "").strip()
        if not token:
            raw = (self.headers.get("Authorization") or "").strip()
            token = raw[7:].strip() if raw[:7].lower() == "bearer " else ""
        if not token:
            return False
        try:
            return secrets.compare_digest(token.encode("utf-8"), self.plane.token.encode("utf-8"))
        except (TypeError, UnicodeEncodeError):
            return False

    # ------------------------------------------------------------------ core
    def _dispatch(self, method: str) -> None:
        try:
            if not self._authorized():
                self._drain()
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if method in ("GET", "HEAD"):
                self._handle_get(path)
            else:
                self._handle_post(path)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except ControlError as exc:
            # A refusal is normal traffic (busy, not running, unknown name) and
            # must reach the caller with its own status, not as a 500.
            try:
                self._json(exc.status, {"ok": False, "error": exc.code, "detail": exc.message})
            except OSError:
                self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - never traceback into the UI
            try:
                self._json(500, {"ok": False, "error": "internal_error", "detail": str(exc)[:200]})
            except OSError:
                self.close_connection = True

    def _handle_get(self, path: str) -> None:
        if path == "/services":
            payload = self.plane.snapshot()
            self._json(200, payload)
            return
        if path.startswith("/jobs/"):
            job = self.plane.job(path[6:])
            if job is None:
                self._json(404, {"ok": False, "error": "unknown_job"})
            else:
                self._json(200, {"ok": True, "job": job})
            return
        if path in ("/", "/health"):
            self._json(200, {"ok": True, "control": True})
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def _handle_post(self, path: str) -> None:
        self._drain()
        parts = [part for part in path.split("/") if part]
        if parts == ["services", "release"]:
            self._accept(self.plane.submit("release"))
            return
        if len(parts) == 3 and parts[0] == "services":
            name, action = parts[1], parts[2]
            if action not in SERVICE_ACTIONS:
                self._json(404, {"ok": False, "error": "unknown_action"})
                return
            if name not in SERVICES:
                self._json(404, {"ok": False, "error": "unknown_service"})
                return
            self._accept(self.plane.submit(action, name))
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def _accept(self, job: dict[str, Any]) -> None:
        self._json(202, {"ok": True, "job": job})

    # ------------------------------------------------------------------ io
    def _drain(self) -> None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return
        try:
            length = int(raw)
        except (TypeError, ValueError):
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except OSError:
            self.close_connection = True

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(body)
