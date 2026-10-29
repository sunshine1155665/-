"""Threaded local HTTP server for the fatfish web UI and JSON API.

Design notes
------------
* Every API request opens its own :class:`~fatfish.db.Database`.  sqlite3
  connections are bound to the thread that created them, so a single shared
  ``Database`` cannot be used by a threaded HTTP server.  ``initialize()`` is
  idempotent and WAL mode keeps concurrent readers happy.
* ``/api/chat``, ``/api/tick`` and ``/api/nightly`` mutate shared state and are
  serialized by one module-level lock.  Read-only endpoints never take it.
* The listener binds loopback by default.  Binding a public interface requires
  ``allow_remote=True`` explicitly.
* The access token may arrive as ``?token=`` (first page load), a cookie, or a
  bearer header.  Query strings are never written to the log.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from ..clock import to_local
from ..config import PROJECT_ROOT, Config
from ..db import Database
from ..life import LifeEngine
from ..lexicon import VALID_KINDS, VALID_STATUSES, LexiconService, mine
from ..llm import LLMClient
from ..memory import MemoryService
from ..memory.entities import (
    create_person,
    list_people,
    merge_entities,
    resolve_person_selector,
    update_person,
)
from ..nightly import run_nightly
from ..outbox import Outbox
from ..qzone import QzoneCollector, QzonePostService
from ..qzone.qr_login import mask_uin, mask_uin_text
from ..runtime import Runtime
from ..security import KernelError, load_kernel
from ..services import (
    SERVICE_ACTIONS,
    SERVICES,
    ControlClient,
    ControlError,
    token_path as control_token_path,
)
from ..state import StateMachine
from ..trace import TraceRecorder
from ..tts import TTSClient
from ..wakeup import WakeupScheduler

APP_VERSION = "0.2.0"

STATIC_ROOT = PROJECT_ROOT / "web"
STATIC_SUFFIXES = {
    ".html",
    ".js",
    ".css",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".ico",
    ".woff2",
}
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

AUDIO_SUFFIXES = {".wav", ".mp3", ".ogg", ".flac", ".m4a"}
AUDIO_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
}

MAX_BODY_BYTES = 65536
MAX_MESSAGE_CHARS = 2000
MAX_SESSION_CHARS = 128
LOCK_TIMEOUT_SECONDS = 5.0

COOKIE_NAME = "fatfish_token"
COOKIE_MAX_AGE = 2592000

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)

_SESSION_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]+")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")

# Chat / tick / nightly mutate agent, life, memory and outbox state.
_TURN_LOCK = threading.Lock()


class PortRangeExhausted(OSError):
    """No port in the requested fallback window could be bound."""

    def __init__(self, first_port: int, last_port: int) -> None:
        self.first_port = first_port
        self.last_port = last_port
        super().__init__(
            errno.EADDRINUSE,
            f"no free port in {first_port}..{last_port}",
        )

    def __str__(self) -> str:
        return f"no free port in {self.first_port}..{self.last_port}"


class FatfishHTTPServer(ThreadingHTTPServer):
    """Threaded server carrying the config, token and binding policy."""

    daemon_threads = True
    # Windows silently lets a second listener share a port when SO_REUSEADDR
    # is set, so a stale or unrelated server keeps receiving requests.  Refuse
    # address reuse there and rely on create_server's port fallback instead.
    allow_reuse_address = os.name != "nt"

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler] = None,  # type: ignore[assignment]
        *,
        config: Config,
        token: str,
        token_source: str,
        allow_remote: bool,
    ):
        self.config = config
        self.token = token
        self.token_source = token_source
        self.allow_remote = allow_remote
        # The supervisor owns every backend process; this client may only ask
        # it to act. When no supervisor is running the panel reports itself as
        # unavailable instead of pretending to be one.
        self.services = ControlClient(control_token_path(config.data_dir))
        host = str(server_address[0])
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, RequestHandlerClass or FatfishRequestHandler)


class FatfishRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"fatfish/{APP_VERSION}"
    sys_version = ""

    # ------------------------------------------------------------------ verbs
    def handle_one_request(self) -> None:
        self._security_sent = False
        super().handle_one_request()

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    # ------------------------------------------------------------------ core
    @property
    def app(self) -> FatfishHTTPServer:
        return self.server  # type: ignore[return-value]

    def _dispatch(self, method: str) -> None:
        self._req_started = time.perf_counter()
        self._set_cookie_token: str | None = None
        self._security_sent = False
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path or "/")
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            is_api = path == "/api" or path.startswith("/api/")

            if is_api:
                if not self._authorized(params):
                    self._drain_body()
                    self._error(401, "unauthorized", "missing or invalid token")
                    return
                self._capture_query_token(params)
                if method == "POST":
                    self._handle_api_post(path, params)
                elif method == "GET":
                    self._handle_api_get(path, params)
                else:
                    self._error(405, "method_not_allowed", "method not allowed")
                return

            if method not in {"GET", "HEAD"}:
                self._error(405, "method_not_allowed", "method not allowed")
                return
            # A valid ?token= on a static request sets the cookie and then
            # redirects so the token does not linger in the address bar.
            if self._capture_query_token(params):
                target = urllib.parse.quote(path or "/", safe="/")
                self._send_bytes(
                    303,
                    b"",
                    "text/plain; charset=utf-8",
                    extra_headers=[("Location", target)],
                )
                return
            self._serve_static(path, head=method == "HEAD")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                self._error(500, "internal_error", "internal error")
            except Exception:
                self.close_connection = True

    # ------------------------------------------------------------------ auth
    def _authorized(self, params: dict[str, list[str]]) -> bool:
        for candidate in params.get("token", []):
            if self._token_matches(candidate):
                return True
        cookie_token = self._cookie_token()
        if cookie_token is not None and self._token_matches(cookie_token):
            return True
        bearer = self._bearer_token()
        if bearer is not None and self._token_matches(bearer):
            return True
        return False

    def _capture_query_token(self, params: dict[str, list[str]]) -> bool:
        for candidate in params.get("token", []):
            if self._token_matches(candidate):
                self._set_cookie_token = self.app.token
                return True
        return False

    def _token_matches(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        try:
            return secrets.compare_digest(
                str(candidate).encode("utf-8"), str(self.app.token).encode("utf-8")
            )
        except (TypeError, UnicodeEncodeError):
            return False

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _bearer_token(self) -> str | None:
        raw = self.headers.get("Authorization", "")
        if raw[:7].lower() != "bearer ":
            return None
        return raw[7:].strip()

    # ------------------------------------------------------------------ csrf
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        return parsed.netloc.lower() == host

    # ------------------------------------------------------------------ body
    def _drain_body(self) -> None:
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

    def _read_json_body(self) -> dict[str, Any] | None:
        """Return a dict, or ``None`` after sending a 4xx response."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            if self.headers.get("Transfer-Encoding"):
                self._error(400, "invalid_content_length", "Content-Length required")
            else:
                self._error(411, "length_required", "Content-Length required")
            return None
        try:
            length = int(raw)
        except (TypeError, ValueError):
            self._error(400, "invalid_content_length", "invalid Content-Length")
            return None
        if length < 0:
            self._error(400, "invalid_content_length", "invalid Content-Length")
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._error(413, "payload_too_large", "request body too large")
            return None
        data = self.rfile.read(length) if length else b""
        if not data.strip():
            return {}
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid_json", "request body must be valid JSON")
            return None
        if not isinstance(payload, dict):
            self._error(400, "invalid_json", "request body must be a JSON object")
            return None
        return payload

    # ------------------------------------------------------------------ api
    def _handle_api_post(self, path: str, params: dict[str, list[str]]) -> None:
        if not self._origin_ok():
            self._drain_body()
            self._error(403, "origin_mismatch", "cross-origin request rejected")
            return
        if self.headers.get("X-Fatfish-Request") != "1":
            self._drain_body()
            self._error(403, "missing_request_header", "missing X-Fatfish-Request header")
            return

        if path == "/api/chat":
            payload = self._read_json_body()
            if payload is not None:
                self._api_chat(payload)
        elif path == "/api/chat/stream":
            payload = self._read_json_body()
            if payload is not None:
                self._api_chat_stream(payload)
        elif path == "/api/tick":
            payload = self._read_json_body()
            if payload is not None:
                self._api_tick()
        elif path == "/api/nightly":
            payload = self._read_json_body()
            if payload is not None:
                self._api_nightly(payload)
        elif path == "/api/lexicon/mine":
            payload = self._read_json_body()
            if payload is not None:
                self._api_lexicon_mine(payload)
        elif path == "/api/memory/update":
            payload = self._read_json_body()
            if payload is not None:
                self._api_memory_update(payload)
        elif path == "/api/memory/delete":
            payload = self._read_json_body()
            if payload is not None:
                self._api_memory_delete(payload)
        elif path == "/api/memory/restore":
            payload = self._read_json_body()
            if payload is not None:
                self._api_memory_restore(payload)
        elif path == "/api/lexicon/review":
            payload = self._read_json_body()
            if payload is not None:
                self._api_lexicon_review(payload)
        elif path == "/api/qzone/review":
            payload = self._read_json_body()
            if payload is not None:
                self._api_qzone_review(payload)
        elif path == "/api/people/add":
            payload = self._read_json_body()
            if payload is not None:
                self._api_people_add(payload)
        elif path == "/api/people/update":
            payload = self._read_json_body()
            if payload is not None:
                self._api_people_update(payload)
        elif path == "/api/people/merge":
            payload = self._read_json_body()
            if payload is not None:
                self._api_people_merge(payload)
        elif path == "/api/lexicon":
            payload = self._read_json_body()
            if payload is not None:
                self._api_lexicon_add(payload)
        elif path.startswith("/api/services/"):
            payload = self._read_json_body()
            if payload is not None:
                self._api_services_action(path, payload)
        else:
            self._drain_body()
            self._error(404, "not_found", "unknown endpoint")

    def _handle_api_get(self, path: str, params: dict[str, list[str]]) -> None:
        if path == "/api/health":
            self._api_health(params)
        elif path == "/api/state":
            self._api_state()
        elif path == "/api/memory":
            self._api_memory(params)
        elif path == "/api/memory/meta":
            self._api_memory_meta(params)
        elif path == "/api/memory/open-loops":
            self._api_memory_open_loops(params)
        elif path == "/api/lexicon":
            self._api_lexicon(params)
        elif path == "/api/lexicon/stats":
            self._api_lexicon_stats()
        elif path == "/api/traces":
            self._api_traces(params)
        elif path == "/api/wakeups":
            self._api_wakeups(params)
        elif path == "/api/outbox":
            self._api_outbox(params)
        elif path == "/api/decisions":
            self._api_decisions(params)
        elif path == "/api/qzone/feed":
            self._api_qzone_feed(params)
        elif path == "/api/qzone/posts":
            self._api_qzone_posts(params)
        elif path == "/api/people":
            self._api_people(params)
        elif path == "/api/config":
            self._api_config()
        elif path == "/api/audio":
            self._api_audio(params)
        elif path == "/api/services":
            self._api_services()
        else:
            self._error(404, "not_found", "unknown endpoint")

    @contextmanager
    def _db_session(self) -> Iterator[Database]:
        db = Database(self.app.config.db_path)
        try:
            # One indexed lookup instead of re-running the whole schema on
            # every HTTP request; initialize() only fires for a new/old DB.
            db.ensure_schema()
            yield db
        finally:
            db.close()

    def _api_health(self, params: dict[str, list[str]]) -> None:
        config = self.app.config
        full = str(_first_param(params, "full", "0") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        with self._db_session() as db:
            health = db.health_check(full=full)
            llm = LLMClient(config=config, db=db)
            tts = TTSClient(config, db).status()
            try:
                kernel = load_kernel(config)
                kernel_info = {
                    "name": kernel.name,
                    "version": kernel.version,
                    "hash": kernel.digest,
                    "intact": kernel.verify_file(),
                }
            except (KernelError, OSError, ValueError):
                kernel_info = {"name": "", "version": 0, "hash": "", "intact": False}
            payload = {
                "ok": True,
                "app": "fatfish",
                "version": APP_VERSION,
                "profile": config.profile,
                "db": {
                    "path": health["path"],
                    "schema_version": health["schema_version"],
                    "fts5": health["fts5"],
                    "integrity": health["integrity"],
                    "size_bytes": health["size_bytes"],
                },
                "llm": {
                    "available": llm.available,
                    "model": llm.model,
                    "provider": llm.provider,
                    "key_present": bool(llm.api_key),
                },
                "tts": {
                    "enabled": bool(tts.get("enabled")),
                    "mode": str(tts.get("mode", "")),
                    "configured": bool(tts.get("configured")),
                },
                "kernel": kernel_info,
                "server_time": _server_time(config),
            }
        self._send_json(200, payload)

    def _api_state(self) -> None:
        config = self.app.config
        with self._db_session() as db:
            state = StateMachine(db).snapshot()
            life = asdict(LifeEngine(db, config).state())
            wakeups = WakeupScheduler(db, config).stats()
            outbox = Outbox(db).stats()
            traces = TraceRecorder(db).summary(limit=100)
            traces = {
                "turns": 0,
                "blocked": 0,
                "avg_latency_ms": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit_tokens": 0,
                **traces,
            }
            kernel_hash = db.get_meta("identity_kernel_hash") or ""
            try:
                kernel = load_kernel(config)
                kernel_ok = kernel.verify_file()
                kernel_hash = kernel_hash or kernel.digest
            except (KernelError, OSError, ValueError):
                kernel_ok = False
            payload = {
                "ok": True,
                "state": state,
                "life": life,
                "counts": {
                    "memory": _count(db, "memory_items"),
                    "rag": _count(db, "rag_chunks"),
                    "turns": _count(db, "conversation_turns"),
                    "traces": _count(db, "turn_traces"),
                    "stickers": _count(db, "stickers"),
                },
                "wakeups": wakeups,
                "outbox": outbox,
                "security": {
                    "kernel_hash": kernel_hash,
                    "kernel_ok": kernel_ok,
                    "schema_version": db.get_meta("schema_version", "0"),
                },
                "traces": traces,
                "server_time": _server_time(config),
            }
        self._send_json(200, payload)

    def _api_memory(self, params: dict[str, list[str]]) -> None:
        limit = _int_param(params, "limit", 20)
        query = (_first_param(params, "query") or "").strip()
        with self._db_session() as db:
            memory = MemoryService(db, self.app.config)
            if query:
                hits = memory.search(query, limit=limit)
                payload = {
                    "ok": True,
                    "count": len(hits),
                    "query": query,
                    "hits": [
                        {
                            "score": hit.score,
                            "similarity": hit.similarity,
                            "keyword": hit.keyword,
                            "reason": hit.reason,
                            "item": _memory_item(hit.item),
                        }
                        for hit in hits
                    ],
                }
            else:
                status = _first_param(params, "status", "active")
                if status in {None, "", "all"}:
                    status = None
                items = memory.list_items(limit, status=status)
                payload = {
                    "ok": True,
                    "count": len(items),
                    "items": [_memory_item(item) for item in items],
                }
        self._send_json(200, payload)

    def _api_memory_meta(self, params: dict[str, list[str]]) -> None:
        limit = _int_param(params, "limit", 24)
        with self._db_session() as db:
            items = MemoryService(db, self.app.config).meta_directory(limit=limit)
        self._send_json(200, {"ok": True, "items": items})

    def _api_memory_open_loops(self, params: dict[str, list[str]]) -> None:
        limit = _int_param(params, "limit", 20)
        with self._db_session() as db:
            items = MemoryService(db, self.app.config).list_open_loops(limit=limit)
        self._send_json(200, {"ok": True, "items": items})

    def _api_qzone_feed(self, params: dict[str, list[str]]) -> None:
        """Stored QQ Zone dynamics, read-only and behind the usual token gate.

        Local display only: nothing here is sent to the LLM, so the response
        is not bound by ``qzone.friends_content_to_llm`` (opt in with
        ``?friend=1``). ``raw_json`` is deliberately absent from the payload.
        """
        limit = _int_param(params, "limit", 20)
        include_friend = str(_first_param(params, "friend", "0") or "").lower() in (
            "1",
            "true",
            "yes",
        )
        with self._db_session() as db:
            collector = QzoneCollector(
                MemoryService(db, self.app.config), self.app.config
            )
            items = collector.recent_items(limit=limit, include_friend=include_friend)
        self._send_json(200, {"ok": True, "count": len(items), "items": items})

    def _qzone_post_service(self, db: Database) -> QzonePostService:
        """Write-path service wired to a fresh per-request outbox."""
        return QzonePostService(
            db,
            self.app.config,
            outbox=Outbox(
                db,
                lease_seconds=float(self.app.config.get("outbox.lease_seconds", 60)),
                max_attempts=int(self.app.config.get("outbox.max_attempts", 5)),
            ),
        )

    def _api_qzone_posts(self, params: dict[str, list[str]]) -> None:
        """Draft queue + recent publish ledger + quota/write-path status.

        Same token gate as every other API endpoint; content was already
        screened at draft time, and ``raw`` payloads never exist on this table.
        """
        limit = _int_param(params, "limit", 50, low=1, high=200)
        status_filter = _first_param(params, "status")
        with self._db_session() as db:
            service = self._qzone_post_service(db)
            try:
                posts = service.list_posts(status=status_filter, limit=limit)
                quota = service.quota_status()
                write_status = service.status()
            except ValueError as exc:
                self._error(400, "invalid_status", str(exc))
                return
        drafts = [item for item in posts if item["status"] == "draft"]
        self._send_json(
            200,
            {
                "ok": True,
                "count": len(posts),
                "drafts": drafts,
                "items": posts,
                "quota": quota,
                "write": write_status,
            },
        )

    def _api_qzone_review(self, payload: dict[str, Any]) -> None:
        """Approve or reject one draft (mirrors /api/lexicon/review)."""
        raw_id = payload.get("id")
        post_id: int | None = None
        if isinstance(raw_id, bool):
            post_id = None
        elif isinstance(raw_id, int):
            post_id = raw_id
        elif isinstance(raw_id, str) and raw_id.strip().lstrip("-").isdigit():
            post_id = int(raw_id.strip())
        if post_id is None or post_id <= 0:
            self._error(400, "invalid_id", "id is required")
            return
        decision = payload.get("decision")
        if decision not in ("approved", "rejected"):
            self._error(400, "invalid_decision", "decision must be approved or rejected")
            return
        with self._db_session() as db:
            service = self._qzone_post_service(db)
            try:
                outcome = service.review_post(post_id, str(decision))
            except ValueError as exc:
                self._error(400, "invalid_decision", str(exc))
                return
        if outcome is None:
            self._error(404, "not_found", "qzone post not found")
            return
        if not outcome.get("ok"):
            self._send_json(
                200,
                {
                    "ok": False,
                    "error": str(outcome.get("error") or "review_failed"),
                    "quota": outcome.get("quota"),
                },
            )
            return
        self._send_json(200, {"ok": True, "post": outcome.get("post")})

    def _api_memory_update(self, payload: dict[str, Any]) -> None:
        item_id = _memory_id(payload)
        if item_id is None:
            self._error(400, "invalid_id", "id is required")
            return
        fields: dict[str, Any] = {}
        for name in ("gist", "summary", "type"):
            if name in payload:
                value = payload[name]
                if not isinstance(value, str):
                    self._error(400, f"invalid_{name}", f"{name} must be a string")
                    return
                fields[name] = value
        if "importance" in payload:
            value = payload["importance"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                self._error(
                    400,
                    "invalid_importance",
                    "importance must be a number between 0 and 1",
                )
                return
            if not 0.0 <= float(value) <= 1.0:
                self._error(
                    400,
                    "invalid_importance",
                    "importance must be between 0 and 1",
                )
                return
            fields["importance"] = float(value)
        for name in ("tags", "entities"):
            if name in payload:
                value = payload[name]
                if not isinstance(value, list) or not all(
                    isinstance(entry, str) for entry in value
                ):
                    self._error(
                        400, f"invalid_{name}", f"{name} must be a list of strings"
                    )
                    return
                fields[name] = value
        with self._db_session() as db:
            try:
                item = MemoryService(db, self.app.config).update_item_fields(
                    item_id, fields
                )
            except ValueError as exc:
                self._error(400, "invalid_request", str(exc))
                return
        if item is None:
            self._error(404, "not_found", "memory item not found")
            return
        self._send_json(200, {"ok": True, "item": _memory_item(item)})

    def _api_memory_delete(self, payload: dict[str, Any]) -> None:
        item_id = _memory_id(payload)
        if item_id is None:
            self._error(400, "invalid_id", "id is required")
            return
        hard = payload.get("hard", False)
        if not isinstance(hard, bool):
            self._error(400, "invalid_hard", "hard must be a boolean")
            return
        if hard and payload.get("confirm") is not True:
            self._error(
                400,
                "confirmation_required",
                "hard delete requires confirm=true",
            )
            return
        with self._db_session() as db:
            item = MemoryService(db, self.app.config).delete_item(item_id, hard=hard)
        if item is None:
            self._error(404, "not_found", "memory item not found")
            return
        self._send_json(
            200,
            {"ok": True, "deleted": True, "hard": hard, "item": _memory_item(item)},
        )

    def _api_memory_restore(self, payload: dict[str, Any]) -> None:
        item_id = _memory_id(payload)
        if item_id is None:
            self._error(400, "invalid_id", "id is required")
            return
        with self._db_session() as db:
            item = MemoryService(db, self.app.config).restore_item(item_id)
        if item is None:
            self._error(404, "not_found", "memory item not found")
            return
        self._send_json(200, {"ok": True, "item": _memory_item(item)})

    # ------------------------------------------------- people (T-0057 P2)
    def _api_people(self, params: dict[str, list[str]]) -> None:
        """Live person cards for the web panel; the uin is always masked.

        Empty ``query`` returns every card; a digit-only query also matches
        the exact uin (``list_people``).  No full-uin option exists on
        purpose: the account id is local-only.
        """
        limit = _int_param(params, "limit", 50, low=1, high=200)
        query = (_first_param(params, "query") or "").strip()
        with self._db_session() as db:
            cards = list_people(db, query=query, limit=limit)
        people = [_person_card_public(card) for card in cards]
        self._send_json(200, {"ok": True, "count": len(people), "people": people})

    def _api_people_add(self, payload: dict[str, Any]) -> None:
        """Add one person card (``create_person`` semantics, ``source="web"``)."""
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            self._error(400, "invalid_request", "name must be a string")
            return
        uin = payload.get("uin")
        if uin is not None and not isinstance(uin, str):
            self._error(400, "invalid_request", "uin must be a string")
            return
        aliases, error = _person_alias_value(payload, "aliases")
        if error:
            self._error(400, "invalid_request", error)
            return
        relation, remark, error = _person_relation_remark(payload)
        if error:
            self._error(400, "invalid_request", error)
            return
        with self._db_session() as db:
            try:
                result = create_person(
                    db,
                    name=str(name or ""),
                    uin=str(uin or ""),
                    aliases=tuple(aliases or ()),
                    relation=relation,
                    remark=remark,
                    source="web",
                )
            except ValueError as exc:
                self._error(400, "invalid_request", str(exc))
                return
        self._send_json(
            200,
            {
                "ok": True,
                "created": bool(result.get("created")),
                "person": _person_card_public(result.get("entity") or {}),
                "merged": [str(item) for item in result.get("merged") or []],
                "merge_candidates": [
                    _person_card_public(candidate)
                    for candidate in result.get("merge_candidates") or []
                ],
            },
        )

    def _api_people_update(self, payload: dict[str, Any]) -> None:
        """Rename / re-alias / re-annotate one person card."""
        raw_id = payload.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            self._error(400, "invalid_id", "id is required")
            return
        entity_id = raw_id.strip()
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            self._error(400, "invalid_request", "name must be a string")
            return
        aliases_add, error = _person_alias_value(payload, "aliases_add")
        if error:
            self._error(400, "invalid_request", error)
            return
        aliases_remove, error = _person_alias_value(payload, "aliases_remove")
        if error:
            self._error(400, "invalid_request", error)
            return
        relation, remark, error = _person_relation_remark(payload)
        if error:
            self._error(400, "invalid_request", error)
            return
        with self._db_session() as db:
            try:
                result = update_person(
                    db,
                    entity_id,
                    name=name,
                    aliases_add=tuple(aliases_add or ()),
                    aliases_remove=tuple(aliases_remove or ()),
                    relation=relation,
                    remark=remark,
                )
            except ValueError as exc:
                if str(exc) == "entity_not_found":
                    self._error(404, "not_found", "person not found")
                else:
                    self._error(400, "invalid_request", str(exc))
                return
        self._send_json(
            200,
            {"ok": True, "person": _person_card_public(result.get("entity") or {})},
        )

    def _api_people_merge(self, payload: dict[str, Any]) -> None:
        """Merge ``source`` into ``target``; both accept id / uin / name / alias."""
        source_ref = payload.get("source")
        target_ref = payload.get("target")
        if not isinstance(source_ref, str) or not source_ref.strip():
            self._error(400, "invalid_request", "source is required")
            return
        if not isinstance(target_ref, str) or not target_ref.strip():
            self._error(400, "invalid_request", "target is required")
            return
        with self._db_session() as db:
            try:
                source = resolve_person_selector(db, source_ref)
                target = resolve_person_selector(db, target_ref)
            except ValueError:
                # The selector text may be a uin; the error must not repeat it.
                self._error(
                    400,
                    "invalid_request",
                    "没有找到对应的人物，或该名字指向多个人",
                )
                return
            try:
                result = merge_entities(db, source, target)
            except ValueError as exc:
                self._error(400, "invalid_request", str(exc))
                return
        self._send_json(
            200,
            {
                "ok": True,
                "merged": bool(result.get("merged")),
                "person": _person_card_public(result.get("entity") or {}),
            },
        )

    def _api_lexicon(self, params: dict[str, list[str]]) -> None:
        limit = _int_param(params, "limit", 50, low=1, high=200)
        status = _first_param(params, "status")
        if status in {None, "", "all"}:
            status = None
        elif status not in VALID_STATUSES:
            self._error(
                400,
                "invalid_status",
                "status must be candidate, approved or disabled",
            )
            return
        query = (_first_param(params, "query") or "").strip()[:100] or None
        with self._db_session() as db:
            lexicon = LexiconService(db, self.app.config)
            items = lexicon.list(status=status, query=query, limit=limit)
            stats = lexicon.stats()
        self._send_json(
            200,
            {"ok": True, "count": len(items), "items": items, "stats": stats},
        )

    def _api_lexicon_stats(self) -> None:
        with self._db_session() as db:
            stats = LexiconService(db, self.app.config).stats()
        self._send_json(200, {"ok": True, "stats": stats})

    def _api_traces(self, params: dict[str, list[str]]) -> None:
        limit = _int_param(params, "limit", 30)
        tz_name = self.app.config.timezone_name
        with self._db_session() as db:
            rows = TraceRecorder(db).last(limit=limit)
            items = []
            for row in rows:
                item = dict(row)
                item["at_text"] = _ts_text(row["at"], tz_name)
                item["actions"] = _json_load(item.get("actions_json"), [])
                item["result"] = _json_load(item.get("result_json"), {})
                items.append(item)
        self._send_json(200, {"ok": True, "summary": _trace_summary(items), "items": items})

    def _api_wakeups(self, params: dict[str, list[str]]) -> None:
        limit = _int_param(params, "limit", 30)
        status = _first_param(params, "status", "pending")
        if status in {"", "all"}:
            status = None
        tz_name = self.app.config.timezone_name
        with self._db_session() as db:
            wakeups = WakeupScheduler(db, self.app.config).list(status, limit=limit)
            items = []
            for wakeup in wakeups:
                item = asdict(wakeup)
                item["fire_at_text"] = _ts_text(wakeup.fire_at, tz_name)
                item["created_at_text"] = _ts_text(wakeup.created_at, tz_name)
                items.append(item)
        self._send_json(200, {"ok": True, "items": items})

    def _api_outbox(self, params: dict[str, list[str]]) -> None:
        limit = _int_param(params, "limit", 30)
        with self._db_session() as db:
            rows = db.query(
                "SELECT * FROM outbox "
                "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC "
                "LIMIT ?",
                (limit,),
            )
            items = [
                {
                    "id": int(row["id"]),
                    "kind": row["kind"],
                    "status": row["status"],
                    "attempts": int(row["attempts"] or 0),
                    "max_attempts": int(row["max_attempts"] or 0),
                    "available_at": float(row["available_at"] or 0),
                    "created_at": float(row["created_at"] or 0),
                    "last_error": row["last_error"],
                    "payload": _json_load(row["payload_json"], {}),
                }
                for row in rows
            ]
        self._send_json(200, {"ok": True, "items": items})

    def _api_decisions(self, params: dict[str, list[str]]) -> None:
        limit = _int_param(params, "limit", 30)
        tz_name = self.app.config.timezone_name
        with self._db_session() as db:
            rows = db.query("SELECT * FROM decision_log ORDER BY at DESC LIMIT ?", (limit,))
            items = []
            for row in rows:
                items.append(
                    {
                        "id": int(row["id"]),
                        "at": float(row["at"]),
                        "at_text": _ts_text(row["at"], tz_name),
                        "kind": row["kind"],
                        "action": row["action"],
                        "outcome": row["outcome"],
                        "latency_ms": float(row["latency_ms"] or 0.0),
                        "model": row["model"],
                        "session_id": row["session_id"],
                        "output": _json_load(row["output_json"], {}),
                    }
                )
        self._send_json(200, {"ok": True, "items": items})

    def _api_config(self) -> None:
        config = self.app.config
        with self._db_session() as db:
            tts = TTSClient(config, db).status()
        payload = {
            "ok": True,
            "profile": config.profile,
            "llm": {
                "provider": str(config.get("llm.provider", "")),
                "model": str(config.get("llm.model", "")),
                "base_url": str(config.get("llm.base_url", "")),
                "api_style": str(config.get("llm.api_style", "")),
            },
            "tts": {
                "enabled": bool(tts.get("enabled")),
                "mode": str(tts.get("mode", "")),
                "base_url": str(tts.get("base_url", "")),
                "configured": bool(tts.get("configured")),
            },
            "security": {
                "block_hostile_inputs": bool(config.get("security.block_hostile_inputs", True)),
                "redact_outbound": bool(config.get("security.redact_outbound", True)),
            },
            "paths": {
                "data_dir": str(config.data_dir),
                "db_path": str(config.db_path),
            },
            "features": {"web": True, "group_chat": False},
        }
        self._send_json(200, payload)

    # ------------------------------------------------------------------ services
    def _api_services(self) -> None:
        """Backend inventory, proxied from the supervisor.

        The web process never spawns a backend itself: two owners of the same
        port end up with an orphan that Ctrl+C cannot reclaim on Windows. So
        this endpoint reports what the supervisor says, and says so plainly
        when there is no supervisor to ask.
        """
        client: ControlClient = self.app.services
        try:
            payload = client.snapshot()
        except ControlError as exc:
            self._send_json(
                200,
                {"ok": True, "available": False, "services": [], "jobs": [],
                 "error": exc.code, "detail": exc.message},
            )
            return
        except (OSError, ValueError) as exc:
            self._send_json(
                200,
                {"ok": True, "available": False, "services": [], "jobs": [],
                 "error": "no_supervisor",
                 "detail": f"没有检测到一键启动在运行（{exc}）"},
            )
            return
        services = payload.get("services")
        jobs = payload.get("jobs")
        self._send_json(
            200,
            {
                "ok": True,
                "available": True,
                "services": services if isinstance(services, list) else [],
                "jobs": jobs if isinstance(jobs, list) else [],
            },
        )

    def _api_services_action(self, path: str, _payload: dict[str, Any]) -> None:
        parts = [part for part in path.split("/") if part]
        # /api/services/<name>/<action>  or  /api/services/release
        if parts == ["api", "services", "release"]:
            name, action = None, "release"
        elif len(parts) == 4 and parts[0] == "api" and parts[1] == "services":
            name, action = parts[2], parts[3]
        else:
            self._error(404, "not_found", "unknown endpoint")
            return
        if action not in SERVICE_ACTIONS and action != "release":
            self._error(404, "unknown_action", f"unknown action {action}")
            return
        if name is not None and name not in SERVICES:
            self._error(404, "unknown_service", f"unknown service {name}")
            return
        client: ControlClient = self.app.services
        try:
            if action == "release":
                job = client.release()
            elif action == "start":
                job = client.start(name or "")
            elif action == "stop":
                job = client.stop(name or "")
            else:
                job = client.unload(name or "")
        except ControlError as exc:
            self._error(exc.status, exc.code, exc.message)
            return
        except (OSError, ValueError) as exc:
            self._error(503, "no_supervisor", f"控制口没有响应：{exc}")
            return
        self._send_json(202, {"ok": True, "job": job})

    # ------------------------------------------------------------------ turns
    def _api_chat(self, payload: dict[str, Any]) -> None:
        raw_message = payload.get("message")
        if not isinstance(raw_message, str):
            self._error(400, "invalid_message", "message must be a string")
            return
        message = raw_message.strip()
        if not 1 <= len(message) <= MAX_MESSAGE_CHARS:
            self._error(
                400,
                "invalid_message",
                f"message must be 1..{MAX_MESSAGE_CHARS} characters",
            )
            return
        session_id = _sanitize_session_id(payload.get("session_id"))
        is_group = bool(payload.get("is_group", False))

        if not _TURN_LOCK.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            self._error(503, "busy", "another turn is already running")
            return
        try:
            config = self.app.config
            with self._db_session() as db:
                runtime = Runtime(db, config)
                result = runtime.handle_user_message(
                    session_id, message, is_group=is_group
                )
                _attach_audio_url(result, config)
            self._send_json(200, {"ok": True, "result": result})
        finally:
            _TURN_LOCK.release()

    def _api_chat_stream(self, payload: dict[str, Any]) -> None:
        """Same turn as ``/api/chat``, but emits one NDJSON line per segment.

        The LLM reply is still produced in one piece; the gain is that the
        first sentence is synthesized and sent while the rest are still being
        generated.  The response body is a sequence of JSON lines:

            {"type": "segment", "text": ..., "audio_url": ...}
            {"type": "done", "result": {...}}
            {"type": "error", "error": ...}
        """
        raw_message = payload.get("message")
        if not isinstance(raw_message, str):
            self._error(400, "invalid_message", "message must be a string")
            return
        message = raw_message.strip()
        if not 1 <= len(message) <= MAX_MESSAGE_CHARS:
            self._error(
                400,
                "invalid_message",
                f"message must be 1..{MAX_MESSAGE_CHARS} characters",
            )
            return
        session_id = _sanitize_session_id(payload.get("session_id"))
        is_group = bool(payload.get("is_group", False))

        if not _TURN_LOCK.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            self._error(503, "busy", "another turn is already running")
            return

        config = self.app.config
        events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        finished = threading.Event()

        def voice_stream(segment: str, result: Any) -> None:
            ok = bool(getattr(result, "ok", False))
            audio_url = None
            if ok:
                audio_url = _audio_url_for(getattr(result, "audio_path", None), config)
            events.put(
                {
                    "type": "segment",
                    "text": segment,
                    "ok": ok,
                    "audio_url": audio_url,
                    "cached": bool(getattr(result, "cached", False)),
                    "error": getattr(result, "error", None),
                }
            )

        def worker() -> None:
            try:
                with self._db_session() as db:
                    runtime = Runtime(db, config)
                    result = runtime.handle_user_message(
                        session_id,
                        message,
                        is_group=is_group,
                        voice_stream=voice_stream,
                    )
                    _attach_audio_url(result, config)
                    events.put({"type": "done", "result": result})
            except Exception as exc:  # noqa: BLE001 - report, never kill the stream
                events.put(
                    {
                        "type": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            finally:
                events.put(None)
                finished.set()

        threading.Thread(target=worker, name="chat-stream", daemon=True).start()

        self.close_connection = True
        writable = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                item = events.get()
                if item is None:
                    break
                if not writable:
                    continue
                line = json.dumps(item, ensure_ascii=False, default=str) + "\n"
                try:
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    writable = False
        finally:
            # Keep the turn lock until the worker is really done, even if the
            # listener closed the tab and stopped reading.
            finished.wait(timeout=300)
            _TURN_LOCK.release()

    def _api_tick(self) -> None:
        if not _TURN_LOCK.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            self._error(503, "busy", "another turn is already running")
            return
        try:
            with self._db_session() as db:
                result = Runtime(db, self.app.config).tick()
            self._send_json(200, {"ok": True, "result": result})
        finally:
            _TURN_LOCK.release()

    def _api_nightly(self, payload: dict[str, Any]) -> None:
        apply = bool(payload.get("apply", False))
        if not _TURN_LOCK.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            self._error(503, "busy", "another turn is already running")
            return
        try:
            config = self.app.config
            with self._db_session() as db:
                memory = MemoryService(db, config)
                if apply:
                    stats = run_nightly(db, config, memory, LifeEngine(db, config))
                else:
                    stats = memory.compact(dry_run=True)
            self._send_json(200, {"ok": True, "applied": apply, "stats": stats})
        finally:
            _TURN_LOCK.release()

    # ---------------------------------------------------------------- lexicon
    def _api_lexicon_mine(self, payload: dict[str, Any]) -> None:
        days = _body_int(payload, "days", 14, 1, 365)
        limit_turns = _body_int(payload, "limit_turns", 800, 1, 2000)
        min_count = _body_int(payload, "min_count", 3, 1, 50)
        with self._db_session() as db:
            result = mine(
                LexiconService(db, self.app.config),
                days=days,
                limit_turns=limit_turns,
                min_count=min_count,
            )
        self._send_json(200, {"ok": True, **result})

    def _api_lexicon_review(self, payload: dict[str, Any]) -> None:
        entry_id = payload.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            self._error(400, "invalid_id", "id is required")
            return
        status = payload.get("status")
        if status not in VALID_STATUSES:
            self._error(
                400,
                "invalid_status",
                "status must be candidate, approved or disabled",
            )
            return
        meaning = payload.get("meaning")
        if meaning is not None and not isinstance(meaning, str):
            self._error(400, "invalid_meaning", "meaning must be a string")
            return
        style = payload.get("style")
        if style is not None and not isinstance(style, str):
            self._error(400, "invalid_style", "style must be a string")
            return
        with self._db_session() as db:
            try:
                item = LexiconService(db, self.app.config).review(
                    entry_id.strip(), str(status), meaning=meaning, style=style
                )
            except ValueError as exc:
                self._error(400, "invalid_request", str(exc))
                return
        if item is None:
            self._error(404, "not_found", "lexicon entry not found")
            return
        self._send_json(200, {"ok": True, "item": item})

    def _api_lexicon_add(self, payload: dict[str, Any]) -> None:
        kind = payload.get("kind")
        content = payload.get("content")
        if not isinstance(kind, str) or kind not in VALID_KINDS:
            self._error(400, "invalid_kind", "kind must be expression or jargon")
            return
        if not isinstance(content, str) or not content.strip():
            self._error(400, "invalid_content", "content is required")
            return
        status = payload.get("status", "candidate")
        if not isinstance(status, str) or status not in VALID_STATUSES:
            self._error(
                400,
                "invalid_status",
                "status must be candidate, approved or disabled",
            )
            return
        for name in ("meaning", "situation", "style"):
            value = payload.get(name, "")
            if value is not None and not isinstance(value, str):
                self._error(400, f"invalid_{name}", f"{name} must be a string")
                return
        lists: dict[str, list[str]] = {}
        for name in ("tags", "examples"):
            value = payload.get(name, [])
            if value is None:
                value = []
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                self._error(400, f"invalid_{name}", f"{name} must be a list of strings")
                return
            lists[name] = value
        with self._db_session() as db:
            try:
                item = LexiconService(db, self.app.config).add(
                    kind,
                    content,
                    meaning=str(payload.get("meaning") or ""),
                    situation=str(payload.get("situation") or ""),
                    style=str(payload.get("style") or ""),
                    tags=lists["tags"],
                    examples=lists["examples"],
                    status=status,
                )
            except ValueError as exc:
                self._error(400, "invalid_request", str(exc))
                return
        self._send_json(200, {"ok": True, "item": item})

    # ------------------------------------------------------------------ audio
    def _api_audio(self, params: dict[str, list[str]]) -> None:
        raw = _first_param(params, "path")
        if not raw:
            self._error(400, "missing_path", "path is required")
            return
        if "\x00" in raw or _looks_absolute(raw):
            self._error(403, "forbidden", "path must be relative to the data directory")
            return
        config = self.app.config
        data_dir = config.data_dir.resolve()
        try:
            candidate = (data_dir / raw).resolve()
        except (OSError, RuntimeError):
            self._error(403, "forbidden", "invalid path")
            return
        if not candidate.is_relative_to(data_dir):
            self._error(403, "forbidden", "path escapes the data directory")
            return
        suffix = candidate.suffix.lower()
        if suffix not in AUDIO_SUFFIXES:
            self._error(403, "forbidden", "unsupported audio type")
            return
        if not candidate.is_file():
            self._error(404, "not_found", "audio file not found")
            return

        size = candidate.stat().st_size
        range_header = self.headers.get("Range")
        parsed_range = _parse_byte_range(range_header, size)
        if parsed_range == "invalid":
            self._send_json(
                416,
                {"ok": False, "error": "range_not_satisfiable", "message": "invalid range"},
                extra_headers=[("Content-Range", f"bytes */{size}")],
            )
            return

        start: int | None = None
        end: int | None = None
        if isinstance(parsed_range, tuple):
            start, end = parsed_range
        try:
            with candidate.open("rb") as handle:
                if start is None:
                    body = handle.read()
                else:
                    handle.seek(start)
                    body = handle.read(end - start + 1)
        except OSError:
            self._error(404, "not_found", "audio file not found")
            return

        headers: list[tuple[str, str]] = [("Accept-Ranges", "bytes")]
        status = 200
        if start is not None and end is not None:
            status = 206
            headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
        self._send_bytes(
            status,
            body,
            AUDIO_CONTENT_TYPES[suffix],
            extra_headers=headers,
        )

    # ------------------------------------------------------------------ static
    def _serve_static(self, path: str, *, head: bool = False) -> None:
        candidate = _resolve_static_path(path)
        if candidate is None:
            self._error(404, "not_found", "file not found")
            return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._error(404, "not_found", "file not found")
            return
        self._send_bytes(
            200,
            body,
            STATIC_CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream"),
            head=head,
        )

    # ------------------------------------------------------------------ output
    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _error(self, status: int, code: str, message: str = "") -> None:
        self._send_json(
            status,
            {"ok": False, "error": code, "message": message or code},
        )

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
        head: bool = False,
    ) -> None:
        self.send_response(status)
        self._send_security_headers()
        self.send_header("Cache-Control", "no-store")
        cookie_token = getattr(self, "_set_cookie_token", None)
        if cookie_token:
            self.send_header("Set-Cookie", self._cookie_header(cookie_token))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.end_headers()
        if not head and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _send_security_headers(self) -> None:
        self._security_sent = True
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)

    def end_headers(self) -> None:
        # Cover responses produced by stdlib error paths (malformed request
        # lines, unsupported methods, etc.) as well as our own handlers.
        if not getattr(self, "_security_sent", False):
            self._send_security_headers()
        super().end_headers()

    def _cookie_header(self, token: str) -> str:
        jar = SimpleCookie()
        jar[COOKIE_NAME] = token
        morsel = jar[COOKIE_NAME]
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        morsel["path"] = "/"
        morsel["max-age"] = str(COOKIE_MAX_AGE)
        return morsel.OutputString()

    # ------------------------------------------------------------------ logs
    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            text = fmt % args
        except Exception:
            text = str(fmt)
        text = text.replace("\r", " ").replace("\n", " ")
        if "?" in text:
            text = text.split("?", 1)[0]
        sys.stderr.write(f"[web] {self.address_string()} {text}\n")

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        started = getattr(self, "_req_started", None)
        elapsed = int((time.perf_counter() - started) * 1000) if started else 0
        path = (getattr(self, "path", "") or "").split("?", 1)[0]
        path = path.replace("\r", "").replace("\n", "")
        method = getattr(self, "command", "") or ""
        sys.stderr.write(
            f"[web] {self.address_string()} {method} {path} {code} {elapsed}ms\n"
        )


# ---------------------------------------------------------------------- helpers
def _is_loopback_host(host: str) -> bool:
    raw = (host or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    if raw.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def _resolve_token(config: Config, token: str | None) -> tuple[str, str]:
    explicit = str(token).strip() if token else ""
    if explicit:
        return explicit, "arg"
    env_token = (os.environ.get("FATFISH_WEB_TOKEN") or "").strip()
    if env_token:
        return env_token, "env"
    configured = config.get("web.token")
    configured = str(configured).strip() if configured else ""
    if configured:
        return configured, "config"
    return secrets.token_urlsafe(24), "generated"


def _first_param(
    params: dict[str, list[str]], name: str, default: str | None = None
) -> str | None:
    values = params.get(name)
    return values[0] if values else default


def _int_param(
    params: dict[str, list[str]],
    name: str,
    default: int,
    *,
    low: int = 1,
    high: int = 200,
) -> int:
    raw = _first_param(params, name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _body_int(
    payload: dict[str, Any], name: str, default: int, low: int, high: int
) -> int:
    raw = payload.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _count(db: Database, table: str) -> int:
    row = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
    return int(row["n"]) if row else 0


def _json_load(raw: Any, fallback: Any) -> Any:
    if raw is None:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _ts_text(ts: Any, tz_name: str) -> str:
    try:
        return to_local(float(ts), tz_name).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _server_time(config: Config) -> str:
    return to_local(time.time(), config.timezone_name).isoformat(timespec="seconds")


def _trace_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {
            "turns": 0,
            "blocked": 0,
            "avg_latency_ms": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_hit_tokens": 0,
        }
    count = len(items)
    return {
        "turns": count,
        "blocked": sum(1 for item in items if int(item.get("blocked") or 0)),
        "avg_latency_ms": round(
            sum(float(item.get("latency_ms") or 0.0) for item in items) / count, 1
        ),
        "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in items),
        "completion_tokens": sum(
            int(item.get("completion_tokens") or 0) for item in items
        ),
        "cache_hit_tokens": sum(
            int(item.get("cache_hit_tokens") or 0) for item in items
        ),
    }


def _memory_id(payload: dict[str, Any]) -> str | None:
    raw = payload.get("id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _memory_item(item: Any) -> dict[str, Any]:
    return {
        "id": item.id,
        "type": item.type,
        "scope": item.scope,
        "session_id": item.session_id,
        "gist": item.gist,
        "summary": item.summary,
        "event_date": item.event_date,
        "event_time_start": item.event_time_start,
        "observed_at": item.observed_at,
        "importance": item.importance,
        "confidence": item.confidence,
        "tier": item.tier,
        "status": item.status,
        "trust_level": item.trust_level,
        "origin_scope": item.origin_scope,
        "source_type": item.source_type,
        "source_id": item.source_id,
        "tags": item.tags,
        "entities": item.entities,
        "access_count": item.access_count,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _sanitize_session_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "web"
    cleaned = _SESSION_UNSAFE.sub("-", value.strip())[:MAX_SESSION_CHARS]
    return cleaned or "web"


def _person_card_public(card: dict[str, Any]) -> dict[str, Any]:
    """Web view of one person card; the uin only ever leaves as a mask.

    ``id`` stays raw on purpose: it is the handle the panel sends back to
    ``/api/people/update`` / ``/api/people/merge`` and the UI never renders
    it.  ``merged_from`` ids are masked — the UI only shows a count.
    """
    data = card if isinstance(card, dict) else {}
    uin = str(data.get("uin") or "")
    aliases = data.get("aliases")
    merged_from = data.get("merged_from")
    return {
        "id": str(data.get("id") or ""),
        "name": str(data.get("name") or ""),
        "aliases": [str(alias) for alias in aliases] if isinstance(aliases, list) else [],
        "relation": str(data.get("relation") or ""),
        "remark": str(data.get("remark") or ""),
        "mentions": int(data.get("mentions") or 0),
        "last_seen_at": data.get("last_seen_at"),
        "merged_from": (
            [mask_uin_text(str(item)) for item in merged_from]
            if isinstance(merged_from, list)
            else []
        ),
        "uin_masked": mask_uin(uin) if uin else "",
    }


def _person_alias_value(
    payload: dict[str, Any], name: str
) -> tuple[list[str] | None, str | None]:
    """Read an optional ``[str]`` field; ``(None, None)`` when absent."""
    raw = payload.get(name)
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        return None, f"{name} must be a list of strings"
    return [str(item).strip() for item in raw if str(item).strip()], None


def _person_relation_remark(
    payload: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Read optional ``relation`` / ``remark``; ``None`` leaves the field alone."""
    fields: list[str | None] = []
    for key in ("relation", "remark"):
        if key not in payload or payload[key] is None:
            fields.append(None)
            continue
        value = payload[key]
        if not isinstance(value, str):
            return None, None, f"{key} must be a string or null"
        fields.append(value)
    return fields[0], fields[1], None


def _looks_absolute(raw: str) -> bool:
    if raw.startswith(("/", "\\")):
        return True
    if _DRIVE_PATH.match(raw):
        return True
    try:
        return Path(raw).is_absolute()
    except (OSError, ValueError):
        return True


def _resolve_static_path(raw_path: str) -> Path | None:
    try:
        decoded = urllib.parse.unquote(raw_path)
    except (UnicodeDecodeError, ValueError):
        return None
    if "\x00" in decoded:
        return None
    normalized = decoded.replace("\\", "/")
    if normalized.startswith("//") or _DRIVE_PATH.match(normalized):
        return None
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." or _DRIVE_PATH.match(part) for part in parts):
        return None
    rel = Path(*parts) if parts else Path("index.html")
    if rel.is_absolute():
        return None
    root = STATIC_ROOT.resolve()
    try:
        candidate = (root / rel).resolve()
    except (OSError, RuntimeError):
        return None
    if not candidate.is_relative_to(root):
        return None
    if candidate.is_dir():
        return None
    if candidate.suffix.lower() not in STATIC_SUFFIXES:
        return None
    return candidate


def _parse_byte_range(
    header: str | None, size: int
) -> tuple[int, int] | str | None:
    """Return ``(start, end)``, ``"invalid"`` or ``None`` (serve full body)."""
    if not header:
        return None
    raw = header.strip()
    if not raw.lower().startswith("bytes="):
        return None
    spec = raw[6:].strip()
    if "," in spec or "-" not in spec:
        return "invalid"
    start_text, end_text = spec.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0 or size <= 0:
                return "invalid"
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            if start < 0 or start >= size:
                return "invalid"
            end = int(end_text) if end_text else size - 1
            if end < start:
                return "invalid"
            end = min(end, size - 1)
    except ValueError:
        return "invalid"
    return start, end


def _audio_url_for(raw: object, config: Config) -> str | None:
    """Map an on-disk audio path to a same-origin ``/api/audio`` URL."""
    if not raw:
        return None
    data_dir = config.data_dir.resolve()
    path = Path(str(raw))
    if not path.is_absolute():
        path = data_dir / path
    try:
        candidate = path.resolve()
    except (OSError, RuntimeError):
        return None
    if not candidate.is_relative_to(data_dir):
        return None
    if candidate.suffix.lower() not in AUDIO_SUFFIXES:
        return None
    if not candidate.is_file():
        return None
    relative = candidate.relative_to(data_dir).as_posix()
    return "/api/audio?path=" + urllib.parse.quote(relative, safe="/")


def _attach_audio_url(result: dict[str, Any], config: Config) -> None:
    """Add a same-origin ``audio_url`` to a runtime result when possible."""
    tts = result.get("tts")
    if isinstance(tts, dict):
        tts["audio_url"] = _audio_url_for(tts.get("audio_path"), config)
        segments = tts.get("segments")
        if isinstance(segments, list):
            for item in segments:
                if isinstance(item, dict):
                    item["audio_url"] = _audio_url_for(item.get("audio_path"), config)
    sing = result.get("sing")
    if isinstance(sing, dict):
        sing["audio_url"] = _audio_url_for(sing.get("path"), config)


def _is_address_in_use(exc: OSError) -> bool:
    """True for EADDRINUSE, including the Windows WinError 10048 form."""
    return (
        exc.errno == errno.EADDRINUSE
        or exc.errno == 10048
        or getattr(exc, "winerror", None) == 10048
    )


def create_server(
    config: Config,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    *,
    allow_remote: bool = False,
    port_fallback: bool = True,
    max_fallback: int = 20,
) -> FatfishHTTPServer:
    """Create (but do not start) the HTTP server.

    ``port == 0`` asks the OS for an ephemeral port and is never adjusted.
    With ``port_fallback`` the server tries the requested port and then the
    following ``max_fallback - 1`` ports (20 ports by default) whenever a
    bind fails with address-in-use.  The chosen port is exposed as
    ``server.server_address[1]``.
    """
    host = str(host).strip() or "127.0.0.1"
    if not allow_remote and not _is_loopback_host(host):
        raise ValueError(
            f"refusing to bind non-loopback host {host!r}; pass allow_remote=True "
            "or --allow-remote to override"
        )
    resolved_token, token_source = _resolve_token(config, token)
    requested = int(port)

    def bind(candidate: int) -> FatfishHTTPServer:
        return FatfishHTTPServer(
            (host, candidate),
            config=config,
            token=resolved_token,
            token_source=token_source,
            allow_remote=allow_remote,
        )

    if requested == 0 or not port_fallback:
        return bind(requested)

    attempts = max(1, int(max_fallback))
    last_error: OSError | None = None
    for candidate in range(requested, requested + attempts):
        try:
            return bind(candidate)
        except OSError as exc:
            if not _is_address_in_use(exc):
                raise
            last_error = exc
    raise PortRangeExhausted(requested, requested + attempts - 1) from last_error


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in str(host) else str(host)


def run_server(
    config: Config,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    open_browser: bool = False,
    *,
    allow_remote: bool = False,
) -> None:
    """Create the server, print the login URL and serve until Ctrl+C."""
    server = create_server(
        config,
        host=host,
        port=port,
        token=token,
        allow_remote=allow_remote,
    )
    bound_host, bound_port = server.server_address[:2]
    display_host = _url_host(str(bound_host))
    actual_port = int(bound_port)
    requested_port = int(port)
    if requested_port and actual_port != requested_port:
        print(
            f"[web] port {requested_port} is busy; using {actual_port}",
            file=sys.stderr,
            flush=True,
        )
    url = (
        f"http://{display_host}:{actual_port}/?token="
        f"{urllib.parse.quote(server.token, safe='')}"
    )
    print(f"fatfish web: {url}", flush=True)
    if server.token_source in {"arg", "generated"}:
        print(f"token: {server.token}", flush=True)
    print(f"bound: {display_host}:{actual_port}; press Ctrl+C to stop", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
