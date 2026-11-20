from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fatfish.config import Config, PROJECT_ROOT, load_config
from fatfish.db import SCHEMA_VERSION
from fatfish.context import ensure_default_persona
from fatfish.db import Database
from fatfish.memory import MemoryService
from fatfish.web.server import (
    APP_VERSION,
    COOKIE_NAME,
    FatfishHTTPServer,
    _attach_audio_url,
    create_server,
)

TOKEN = "test-token"


def _json(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


class WebServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.data_dir = cls.tmp / "data"
        cls.data_dir.mkdir(parents=True)
        cls._env = patch.dict(
            os.environ,
            {
                "FATFISH_DATA_DIR": str(cls.data_dir),
                "FATFISH_DB": str(cls.data_dir / "web.db"),
                "OPENCODE_API_KEY": "",
                "FATFISH_OPENCODE_KEY": "",
            },
        )
        cls._env.start()
        # Web tests must not depend on a live local TTS service.
        data = load_config("low").as_dict()
        data.setdefault("tts", {})["enabled"] = False
        cls.config = Config(data, profile="low")
        cls.config.ensure_dirs()
        db = Database(cls.config.db_path)
        db.initialize()
        ensure_default_persona(db)
        db.close()

        cls.server = create_server(cls.config, host="127.0.0.1", port=0, token=TOKEN)
        cls.port = int(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._env.stop()
        cls._tmp.cleanup()

    # ------------------------------------------------------------- helpers
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        cookie: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        if cookie and "Cookie" not in request_headers:
            request_headers["Cookie"] = f"{COOKIE_NAME}={TOKEN}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=data, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            header_map = {name.lower(): value for name, value in response.getheaders()}
            return response.status, header_map, raw
        finally:
            connection.close()

    def _post(
        self,
        path: str,
        payload: dict,
        *,
        csrf: bool = True,
        cookie: bool = True,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        if csrf:
            request_headers["X-Fatfish-Request"] = "1"
        return self._request(
            "POST", path, body=payload, headers=request_headers, cookie=cookie
        )

    def _get_json(self, path: str) -> tuple[int, dict[str, str], dict]:
        status, headers, raw = self._request("GET", path)
        return status, headers, _json(raw)

    # --------------------------------------------------------------- tests
    def test_unauthorized_health(self) -> None:
        status, _, raw = self._request("GET", "/api/health", cookie=False)
        self.assertEqual(401, status)
        payload = _json(raw)
        self.assertFalse(payload["ok"])
        self.assertEqual("unauthorized", payload["error"])
        self.assertNotIn(TOKEN.encode(), raw)

    def test_token_query_sets_cookie_and_redirects(self) -> None:
        status, headers, _ = self._request("GET", f"/?token={TOKEN}", cookie=False)
        self.assertEqual(303, status)
        set_cookie = headers.get("set-cookie", "")
        self.assertIn(f"{COOKIE_NAME}={TOKEN}", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Max-Age=2592000", set_cookie)
        self.assertEqual("/", headers.get("location"))

        status, _, _ = self._request(
            "GET",
            "/api/health",
            headers={"Cookie": f"{COOKIE_NAME}={TOKEN}"},
            cookie=False,
        )
        self.assertEqual(200, status)

        status, _, _ = self._request(
            "GET",
            "/api/health",
            headers={"Authorization": f"Bearer {TOKEN}"},
            cookie=False,
        )
        self.assertEqual(200, status)

    def test_api_token_query_answers_json_and_sets_cookie(self) -> None:
        status, headers, raw = self._request(
            "GET", f"/api/health?token={TOKEN}", cookie=False
        )
        self.assertEqual(200, status)
        self.assertNotIn("location", headers)
        self.assertIn(f"{COOKIE_NAME}={TOKEN}", headers.get("set-cookie", ""))
        self.assertTrue(_json(raw)["ok"])

    def test_health_and_state_shapes(self) -> None:
        status, _, health = self._get_json("/api/health")
        self.assertEqual(200, status)
        self.assertTrue(health["ok"])
        self.assertEqual("fatfish", health["app"])
        self.assertEqual(APP_VERSION, health["version"])
        self.assertEqual("low", health["profile"])
        self.assertEqual(SCHEMA_VERSION, health["db"]["schema_version"])
        for key in ("path", "schema_version", "fts5", "integrity", "size_bytes"):
            self.assertIn(key, health["db"])
        for key in ("available", "model", "provider", "key_present"):
            self.assertIn(key, health["llm"])
        for key in ("enabled", "mode", "configured"):
            self.assertIn(key, health["tts"])
        for key in ("name", "version", "hash", "intact"):
            self.assertIn(key, health["kernel"])
        self.assertIsInstance(health["server_time"], str)

        status, _, state = self._get_json("/api/state")
        self.assertEqual(200, status)
        self.assertTrue(state["ok"])
        for key in ("state", "since", "reason"):
            self.assertIn(key, state["state"])
        for key in (
            "at",
            "mood",
            "energy",
            "social",
            "phase",
            "current_activity",
            "activity_started_at",
            "planned_minutes",
            "note",
        ):
            self.assertIn(key, state["life"])
        for key in ("memory", "rag", "turns", "traces", "stickers"):
            self.assertIn(key, state["counts"])
        self.assertIsInstance(state["wakeups"], dict)
        self.assertIsInstance(state["outbox"], dict)
        self.assertEqual(SCHEMA_VERSION, state["security"]["schema_version"])
        for key in (
            "turns",
            "blocked",
            "avg_latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "cache_hit_tokens",
        ):
            self.assertIn(key, state["traces"])
        self.assertIsInstance(state["server_time"], str)

    def test_static_root(self) -> None:
        index = PROJECT_ROOT / "web" / "index.html"
        status, headers, raw = self._request("GET", "/", cookie=False)
        if index.exists():
            self.assertEqual(200, status)
            self.assertIn("text/html", headers.get("content-type", ""))
            self.assertEqual("no-store", headers.get("cache-control"))
            self.assertEqual("nosniff", headers.get("x-content-type-options"))
        else:
            self.assertEqual(404, status)
            self.assertFalse(_json(raw)["ok"])

        status, _, _ = self._request("GET", "/notes.txt", cookie=False)
        self.assertEqual(404, status)

    def test_path_traversal_blocked(self) -> None:
        paths = (
            "/..%2ffatfish/kernel.json",
            "/%2e%2e/fatfish/kernel.json",
            "/..%5c..%5cfatfish%5ckernel.json",
            "/..%2f..%2ffatfish%2fkernel.json",
        )
        for path in paths:
            status, _, raw = self._request("GET", path, cookie=False)
            self.assertIn(status, (403, 404), path)
            text = raw.decode("utf-8", errors="replace")
            self.assertNotIn("boundaries", text)
            self.assertNotIn("identity", text)
            self.assertNotIn("digest", text)

    def test_chat_requires_request_header_and_runs(self) -> None:
        status, _, raw = self._post(
            "/api/chat",
            {"message": "你好呀", "session_id": "web-test"},
            csrf=False,
        )
        self.assertEqual(403, status)
        self.assertEqual("missing_request_header", _json(raw)["error"])

        status, _, raw = self._post(
            "/api/chat",
            {"message": "你好呀，今天有点累", "session_id": "web-test"},
        )
        self.assertEqual(200, status, raw)
        payload = _json(raw)
        self.assertTrue(payload["ok"])
        result = payload["result"]
        self.assertTrue(str(result["reply"]).strip())
        self.assertIsInstance(result["messages"], list)
        tts = result.get("tts")
        if isinstance(tts, dict):
            self.assertIsNone(tts.get("audio_url"))

        status, _, traces = self._get_json("/api/traces?limit=5")
        self.assertEqual(200, status)
        self.assertTrue(traces["ok"])
        self.assertTrue(traces["items"])
        item = traces["items"][0]
        self.assertEqual("web-test", item["session_id"])
        self.assertTrue(item["user_hash"])
        self.assertIsInstance(item["actions"], list)
        self.assertIsInstance(item["result"], dict)
        self.assertNotIn("user_text", item)
        self.assertIsInstance(item["at_text"], str)

    def test_chat_rejects_invalid_messages(self) -> None:
        status, _, raw = self._post("/api/chat", {"message": ""})
        self.assertEqual(400, status)
        self.assertEqual("invalid_message", _json(raw)["error"])

        status, _, raw = self._post("/api/chat", {"message": "x" * 2001})
        self.assertEqual(400, status)
        self.assertEqual("invalid_message", _json(raw)["error"])

        status, _, raw = self._post("/api/chat", {"message": 42})
        self.assertEqual(400, status)

    def test_memory_endpoints(self) -> None:
        db = Database(self.config.db_path)
        db.initialize()
        memory = MemoryService(db, self.config)
        item = memory.add_item(
            "用户说下周有一场面试，需要准备项目故事。",
            type="episodic",
            importance=0.8,
            tags=["面试", "工作"],
            entities=["用户"],
            source_type="test",
            source_id="web-test-item",
        )
        loop_id = memory.add_open_loop("面试结束后问问结果", kind="follow_up")
        db.close()

        status, _, listing = self._get_json("/api/memory?limit=50")
        self.assertEqual(200, status)
        self.assertTrue(listing["ok"])
        self.assertGreaterEqual(listing["count"], 1)
        listed = {entry["id"]: entry for entry in listing["items"]}
        self.assertIn(item.id, listed)
        entry = listed[item.id]
        self.assertNotIn("embedding", entry)
        self.assertEqual(["面试", "工作"], entry["tags"])
        self.assertIsInstance(entry["entities"], list)
        for key in (
            "type",
            "scope",
            "session_id",
            "gist",
            "summary",
            "event_date",
            "event_time_start",
            "observed_at",
            "importance",
            "confidence",
            "tier",
            "status",
            "trust_level",
            "origin_scope",
            "source_type",
            "source_id",
            "access_count",
            "created_at",
            "updated_at",
        ):
            self.assertIn(key, entry)

        status, _, search = self._get_json("/api/memory?query=%E9%9D%A2%E8%AF%95&limit=10")
        self.assertEqual(200, status)
        self.assertTrue(search["ok"])
        self.assertEqual("面试", search["query"])
        self.assertGreaterEqual(search["count"], 1)
        hit = search["hits"][0]
        for key in ("score", "similarity", "keyword", "reason", "item"):
            self.assertIn(key, hit)
        self.assertNotIn("embedding", hit["item"])

        status, _, meta = self._get_json("/api/memory/meta?limit=24")
        self.assertEqual(200, status)
        self.assertTrue(meta["ok"])
        self.assertIsInstance(meta["items"], list)

        status, _, loops = self._get_json("/api/memory/open-loops?limit=20")
        self.assertEqual(200, status)
        self.assertTrue(loops["ok"])
        self.assertIn(loop_id, {entry["id"] for entry in loops["items"]})

    def test_memory_edit_delete_restore(self) -> None:
        db = Database(self.config.db_path)
        db.initialize()
        memory = MemoryService(db, self.config)
        item = memory.add_item(
            "原始记忆：明天去买鱼食。",
            type="episodic",
            importance=0.3,
            tags=["旧标签"],
            entities=["用户"],
            source_type="test",
            source_id="web-edit-delete",
        )
        original_updated = item.updated_at
        db.close()

        # Update only whitelisted fields; trust/origin must stay untouched.
        status, _, raw = self._post(
            "/api/memory/update",
            {
                "id": item.id,
                "gist": "更新后的要点：面试准备",
                "summary": "更新后的摘要：面试前要准备项目故事。",
                "type": "semantic",
                "importance": 0.9,
                "tags": ["面试", "准备"],
                "entities": ["用户", "公司"],
                "trust_level": "external",
                "origin_scope": "external",
            },
        )
        self.assertEqual(200, status, raw)
        updated = _json(raw)["item"]
        self.assertEqual(item.id, updated["id"])
        self.assertEqual("更新后的要点：面试准备", updated["gist"])
        self.assertEqual("更新后的摘要：面试前要准备项目故事。", updated["summary"])
        self.assertEqual("semantic", updated["type"])
        self.assertEqual(0.9, updated["importance"])
        self.assertEqual(["面试", "准备"], updated["tags"])
        self.assertEqual(["用户", "公司"], updated["entities"])
        self.assertEqual("internal", updated["trust_level"])
        self.assertEqual("self", updated["origin_scope"])
        self.assertGreaterEqual(updated["updated_at"], original_updated)

        status, _, search = self._get_json("/api/memory?query=%E9%9D%A2%E8%AF%95&limit=50")
        self.assertEqual(200, status)
        self.assertIn(item.id, {hit["item"]["id"] for hit in search["hits"]})

        # Validation failures.
        for value in (1.5, -0.1, "0.5", True):
            status, _, raw = self._post(
                "/api/memory/update", {"id": item.id, "importance": value}
            )
            self.assertEqual(400, status, value)
            self.assertEqual("invalid_importance", _json(raw)["error"])
        for bad in ("", "   ", 42, None):
            status, _, raw = self._post("/api/memory/update", {"id": bad})
            self.assertEqual(400, status, bad)
            self.assertEqual("invalid_id", _json(raw)["error"])
        status, _, raw = self._post(
            "/api/memory/update", {"id": "missing-item-id", "gist": "x"}
        )
        self.assertEqual(404, status)
        self.assertEqual("not_found", _json(raw)["error"])
        status, _, raw = self._post(
            "/api/memory/update", {"id": item.id, "tags": "面试"}
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_tags", _json(raw)["error"])

        # Soft delete hides the item from listing, "all" listing and search.
        status, _, raw = self._post("/api/memory/delete", {"id": item.id})
        self.assertEqual(200, status, raw)
        deleted = _json(raw)
        self.assertTrue(deleted["deleted"])
        self.assertFalse(deleted["hard"])
        self.assertEqual("deleted", deleted["item"]["status"])

        status, _, listing = self._get_json("/api/memory?limit=200")
        self.assertNotIn(item.id, {entry["id"] for entry in listing["items"]})
        status, _, listing = self._get_json("/api/memory?limit=200&status=all")
        self.assertNotIn(item.id, {entry["id"] for entry in listing["items"]})
        status, _, search = self._get_json("/api/memory?query=%E9%9D%A2%E8%AF%95&limit=50")
        self.assertNotIn(item.id, {hit["item"]["id"] for hit in search["hits"]})

        # Restore brings it back.
        status, _, raw = self._post("/api/memory/restore", {"id": item.id})
        self.assertEqual(200, status, raw)
        self.assertEqual("active", _json(raw)["item"]["status"])
        status, _, listing = self._get_json("/api/memory?limit=200")
        self.assertIn(item.id, {entry["id"] for entry in listing["items"]})
        status, _, search = self._get_json("/api/memory?query=%E9%9D%A2%E8%AF%95&limit=50")
        self.assertIn(item.id, {hit["item"]["id"] for hit in search["hits"]})

        # Restore/delete/update on a missing id.
        status, _, raw = self._post("/api/memory/restore", {"id": "missing-item-id"})
        self.assertEqual(404, status)
        status, _, raw = self._post(
            "/api/memory/delete", {"id": "missing-item-id", "hard": True, "confirm": True}
        )
        self.assertEqual(404, status)

        # Hard delete needs explicit confirmation, then removes row and FTS.
        status, _, raw = self._post(
            "/api/memory/delete", {"id": item.id, "hard": True}
        )
        self.assertEqual(400, status)
        self.assertEqual("confirmation_required", _json(raw)["error"])

        status, _, raw = self._post(
            "/api/memory/delete", {"id": item.id, "hard": True, "confirm": True}
        )
        self.assertEqual(200, status, raw)
        self.assertTrue(_json(raw)["hard"])

        db = Database(self.config.db_path)
        db.initialize()
        self.assertIsNone(MemoryService(db, self.config).get(item.id))
        fts = db.query_one(
            "SELECT COUNT(*) AS n FROM memory_fts WHERE id=?", (item.id,)
        )
        self.assertEqual(0, int(fts["n"]))
        db.close()

    def test_audio_endpoint(self) -> None:
        status, _, raw = self._request("GET", "/api/audio?path=../kernel.json")
        self.assertEqual(403, status)
        self.assertFalse(_json(raw)["ok"])

        status, _, _ = self._request("GET", "/api/audio?path=%2Fetc%2Fpasswd")
        self.assertEqual(403, status)

        audio_dir = self.config.data_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        wav = audio_dir / "tiny.wav"
        wav.write_bytes(
            b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"fmt " + b"\x00" * 16
            + b"data" + b"\x00" * 8
        )

        status, headers, raw = self._request("GET", "/api/audio?path=audio/tiny.wav")
        self.assertEqual(200, status)
        self.assertEqual("audio/wav", headers.get("content-type"))
        self.assertEqual("bytes", headers.get("accept-ranges"))
        self.assertEqual(wav.read_bytes(), raw)

        status, headers, raw = self._request(
            "GET", "/api/audio?path=audio/tiny.wav", headers={"Range": "bytes=0-3"}
        )
        self.assertEqual(206, status)
        self.assertEqual(f"bytes 0-3/{wav.stat().st_size}", headers.get("content-range"))
        self.assertEqual(b"RIFF", raw)

    def test_audio_url_helper(self) -> None:
        clip = self.config.data_dir / "audio" / "clip.wav"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"RIFF")

        result = {"tts": {"audio_path": str(clip)}}
        _attach_audio_url(result, self.config)
        self.assertEqual("/api/audio?path=audio/clip.wav", result["tts"]["audio_url"])

        outside = self.tmp / "outside.wav"
        outside.write_bytes(b"RIFF")
        result = {"tts": {"audio_path": str(outside)}}
        _attach_audio_url(result, self.config)
        self.assertIsNone(result["tts"]["audio_url"])

    def test_tick_and_nightly_dry_run(self) -> None:
        status, _, raw = self._post("/api/tick", {})
        self.assertEqual(200, status, raw)
        tick = _json(raw)
        self.assertTrue(tick["ok"])
        for key in ("life", "wakeups", "outbox", "rituals", "nightly"):
            self.assertIn(key, tick["result"])

        status, _, raw = self._post("/api/nightly", {"apply": False})
        self.assertEqual(200, status, raw)
        nightly = _json(raw)
        self.assertTrue(nightly["ok"])
        self.assertFalse(nightly["applied"])
        self.assertTrue(nightly["stats"]["dry_run"])

    def test_csrf_origin_mismatch(self) -> None:
        status, _, raw = self._post(
            "/api/tick", {}, headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(403, status)
        self.assertEqual("origin_mismatch", _json(raw)["error"])

    def test_non_loopback_bind_refused(self) -> None:
        with self.assertRaises(ValueError):
            create_server(self.config, host="0.0.0.0", port=0, token=TOKEN)

    def test_port_conflict_falls_back(self) -> None:
        self.assertEqual(os.name != "nt", FatfishHTTPServer.allow_reuse_address)

        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        occupied_port = int(occupied.getsockname()[1])

        server = create_server(
            self.config, host="127.0.0.1", port=occupied_port, token="t"
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            actual_port = int(server.server_address[1])
            self.assertNotEqual(occupied_port, actual_port)
            self.assertGreater(actual_port, 0)
            connection = http.client.HTTPConnection(
                "127.0.0.1", actual_port, timeout=20
            )
            try:
                connection.request(
                    "GET", "/api/health", headers={"Authorization": "Bearer t"}
                )
                response = connection.getresponse()
                raw = response.read()
            finally:
                connection.close()
            self.assertEqual(200, response.status)
            self.assertTrue(_json(raw)["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_ephemeral_port_still_works(self) -> None:
        server = create_server(self.config, host="127.0.0.1", port=0, token=TOKEN)
        port = int(server.server_address[1])
        self.assertGreater(port, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            try:
                connection.request(
                    "GET",
                    "/api/health",
                    headers={"Cookie": f"{COOKIE_NAME}={TOKEN}"},
                )
                response = connection.getresponse()
                raw = response.read()
            finally:
                connection.close()
            self.assertEqual(200, response.status)
            self.assertTrue(_json(raw)["ok"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_security_headers_on_stdlib_error(self) -> None:
        status, headers, _ = self._request("PUT", "/api/health", cookie=False)
        self.assertEqual(501, status)
        self.assertEqual("nosniff", headers.get("x-content-type-options"))
        self.assertEqual("DENY", headers.get("x-frame-options"))
        self.assertIn("frame-ancestors 'none'", headers.get("content-security-policy", ""))

    def test_token_never_in_json(self) -> None:
        requests = [
            self._request("GET", "/api/health", cookie=False),
            self._request("GET", "/api/health"),
            self._request("GET", "/api/state"),
            self._request("GET", "/api/config"),
            self._request("GET", "/api/memory?limit=5"),
            self._request("GET", "/api/memory/meta?limit=5"),
            self._request("GET", "/api/memory/open-loops?limit=5"),
            self._request("GET", "/api/traces?limit=5"),
            self._request("GET", "/api/wakeups?limit=5"),
            self._request("GET", "/api/outbox?limit=5"),
            self._request("GET", "/api/decisions?limit=5"),
            self._request("GET", "/api/audio?path=../kernel.json"),
            self._post("/api/chat", {"message": "hi"}, csrf=False),
        ]
        for status, _, raw in requests:
            self.assertNotIn(TOKEN.encode(), raw)
        self.assertEqual(401, requests[0][0])


if __name__ == "__main__":
    unittest.main()

