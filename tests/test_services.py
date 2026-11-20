"""Tests for the backend control plane.

The point of this module is *ownership*, so the tests are mostly about who is
allowed to say what:

* a start must not block the HTTP handler (cold starts take 30-90 s);
* a second start while one is running is refused, not raced;
* nobody without the token gets in, and a rebinding-style ``Host`` is refused;
* a runtime that raises turns into a failed job, not a dead server.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from fatfish.services import (
    CONTROL_HOST,
    CONTROL_TOKEN_HEADER,
    LABELS,
    SERVICES,
    SERVICE_REGISTRY,
    ControlClient,
    ControlError,
    ControlPlane,
    get_service_manager,
    read_token,
    remove_token,
    reset_service_manager,
    token_path,
    write_token,
)


class ServiceRegistryTests(unittest.TestCase):
    """T-0030: one source of truth for the service list."""

    def test_derived_tables_match_the_registry(self) -> None:
        self.assertEqual(SERVICES, tuple(SERVICE_REGISTRY))
        self.assertEqual(LABELS, {n: i.label for n, i in SERVICE_REGISTRY.items()})

    def test_registry_fields(self) -> None:
        for name, info in SERVICE_REGISTRY.items():
            self.assertEqual(info.name, name)
            self.assertTrue(info.label)
        # the ports the rest of the stack assumes
        self.assertEqual(SERVICE_REGISTRY["tts"].port, 9880)
        self.assertEqual(SERVICE_REGISTRY["embedding"].port, 9881)
        self.assertEqual(SERVICE_REGISTRY["comfyui"].port, 8188)
        # ComfyUI is owned by the photo service, not the supervisor
        self.assertFalse(SERVICE_REGISTRY["comfyui"].supervised)
        self.assertTrue(SERVICE_REGISTRY["tts"].supervised)
        # VRAM participants (the single-8GB-card arbitration)
        self.assertEqual(
            {n for n, i in SERVICE_REGISTRY.items() if i.owns_vram},
            {"tts", "comfyui"},
        )


class FakeRuntime:
    """A runtime whose actions take time and can fail on demand."""

    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.calls: list[tuple[str, str | None]] = []
        self.fail_next: str | None = None
        self.services = [
            {"name": name, "label": name, "state": "stopped", "started_by": "none",
             "pid": None, "port": None, "healthy": False, "loaded": None,
             "assets_ok": True, "missing": [], "controllable": True, "detail": ""}
            for name in ("tts", "embedding", "comfyui", "qq")
        ]

    def status(self):
        return [dict(row) for row in self.services]

    def _record(self, action: str, name: str | None):
        if self.delay:
            time.sleep(self.delay)
        if self.fail_next == action:
            self.fail_next = None
            raise ControlError("spawn_failed", "假装启动失败", 500)
        self.calls.append((action, name))
        return {"state": "done", "detail": f"{action} ok"}

    def start(self, name):
        return self._record("start", name)

    def stop(self, name):
        return self._record("stop", name)

    def unload(self, name):
        return self._record("unload", name)

    def release(self):
        return self._record("release", None)


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.runtime = FakeRuntime()
        self.token_file = token_path(self.data)
        self.plane = ControlPlane(self.runtime, self.token_file, port=0)
        self.plane.start()
        self.client = ControlClient(self.token_file, port=self.plane.port, timeout=5.0)

    def tearDown(self) -> None:
        self.plane.stop()
        self._tmp.cleanup()

    def _wait(self, job_id: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.job(job_id)
            if job.get("state") in ("done", "failed"):
                return job
            time.sleep(0.02)
        self.fail(f"job {job_id} did not settle")

    def _raw(self, method: str, path: str, *, headers: dict | None = None) -> tuple[int, dict]:
        url = f"http://{CONTROL_HOST}:{self.plane.port}{path}"
        request = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8") or "{}"
            return exc.code, json.loads(body)

    # ---------------------------------------------------------------- happy path
    def test_status_lists_every_service(self) -> None:
        rows = self.client.status()
        self.assertEqual(
            {"tts", "embedding", "comfyui", "qq"}, {row["name"] for row in rows}
        )

    def test_start_returns_a_job_and_runs_it(self) -> None:
        job = self.client.start("tts")
        self.assertEqual("queued", job["state"])
        settled = self._wait(job["id"])
        self.assertEqual("done", settled["state"])
        self.assertIn(("start", "tts"), self.runtime.calls)

    def test_start_is_asynchronous(self) -> None:
        self.runtime.delay = 0.6
        began = time.monotonic()
        job = self.client.start("tts")
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 0.5, "HTTP 处理不能等冷启动")
        self.assertEqual("done", self._wait(job["id"], timeout=10.0)["state"])

    def test_double_start_is_refused_not_raced(self) -> None:
        self.runtime.delay = 0.4
        first = self.client.start("tts")
        with self.assertRaises(ControlError) as caught:
            self.client.start("tts")
        self.assertEqual("busy", caught.exception.code)
        self.assertEqual(409, caught.exception.status)
        self._wait(first["id"], timeout=10.0)
        self.assertEqual([("start", "tts")], self.runtime.calls)

    def test_release_calls_the_runtime_once(self) -> None:
        job = self.client.release()
        self._wait(job["id"])
        self.assertEqual([("release", None)], self.runtime.calls)

    def test_failed_action_becomes_a_failed_job(self) -> None:
        self.runtime.fail_next = "start"
        job = self.client.start("tts")
        settled = self._wait(job["id"])
        self.assertEqual("failed", settled["state"])
        self.assertIn("假装启动失败", settled["error"])
        # The plane must still answer after a failure.
        self.assertTrue(self.client.available())

    def test_unknown_service_is_404(self) -> None:
        with self.assertRaises(ControlError) as caught:
            self.client.start("rm -rf /")
        self.assertIn(caught.exception.status, (400, 404))
        self.assertEqual([], self.runtime.calls)

    # ------------------------------------------------------------------ security
    def test_no_token_is_rejected(self) -> None:
        status, _ = self._raw("GET", "/services")
        self.assertEqual(401, status)

    def test_wrong_token_is_rejected(self) -> None:
        status, _ = self._raw("GET", "/services", headers={CONTROL_TOKEN_HEADER: "nope"})
        self.assertEqual(401, status)

    def test_bearer_token_works(self) -> None:
        token = read_token(self.token_file)
        status, payload = self._raw(
            "GET", "/services", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])

    def test_foreign_host_header_is_rejected(self) -> None:
        # DNS rebinding: the browser thinks it is talking to evil.example.
        token = read_token(self.token_file)
        status, _ = self._raw(
            "GET", "/services",
            headers={CONTROL_TOKEN_HEADER: token, "Host": "evil.example"},
        )
        self.assertEqual(401, status)

    def test_browser_origin_is_rejected(self) -> None:
        token = read_token(self.token_file)
        status, _ = self._raw(
            "GET", "/services",
            headers={CONTROL_TOKEN_HEADER: token, "Origin": "http://evil.example"},
        )
        self.assertEqual(401, status)

    def test_stopping_removes_the_token(self) -> None:
        self.plane.stop()
        self.assertIsNone(read_token(self.token_file))
        self.plane = ControlPlane(self.runtime, self.token_file, port=0)
        self.plane.start()


class TokenFileTests(unittest.TestCase):
    def test_write_read_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = token_path(tmp)
            write_token(path, "abc123")
            self.assertEqual("abc123", read_token(path))
            # A token we no longer own must not be deleted by a stale stopper.
            remove_token(path, "someone-else")
            self.assertEqual("abc123", read_token(path))
            remove_token(path, "abc123")
            self.assertIsNone(read_token(path))

    def test_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_token(token_path(tmp)))
            client = ControlClient(token_path(tmp), port=1, timeout=0.2)
            self.assertFalse(client.available())


class ClientWithoutPlaneTests(unittest.TestCase):
    def test_status_raises_control_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = token_path(tmp)
            write_token(path, "deadbeef")
            client = ControlClient(path, port=1, timeout=0.2)
            with self.assertRaises((ControlError, OSError)):
                client.status()


class ServiceManagerCacheTests(unittest.TestCase):
    def test_availability_is_cached_and_can_be_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plane = ControlPlane(FakeRuntime(), token_path(tmp), port=0)
            plane.start()
            try:
                reset_service_manager()
                manager = get_service_manager(token_path(tmp))
                self.assertIsNotNone(manager)

                # The supervisor goes away, but the availability result is
                # cached.  PhotoService only cares that it *had* one; a dead
                # manager produces a failed call, not a second 1.5s probe.
                plane.stop()
                self.assertIsNotNone(get_service_manager(token_path(tmp)))

                reset_service_manager()
                self.assertIsNone(get_service_manager(token_path(tmp)))
            finally:
                reset_service_manager()
                plane.stop()


class JobHistoryTests(unittest.TestCase):
    def test_old_jobs_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime()
            plane = ControlPlane(runtime, token_path(tmp), port=0)
            plane.start()
            client = ControlClient(token_path(tmp), port=plane.port, timeout=5.0)
            try:
                seen: list[str] = []
                for _ in range(25):
                    job = client.release()
                    seen.append(job["id"])
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        if client.job(job["id"]).get("state") in ("done", "failed"):
                            break
                        time.sleep(0.02)
                self.assertIsNone(plane.job(seen[0]), "最早的 job 应该被挤掉")
                self.assertIsNotNone(plane.job(seen[-1]))
            finally:
                plane.stop()


class WebProxyTests(unittest.TestCase):
    """The web UI may only ask; it must never become a second owner."""

    @classmethod
    def setUpClass(cls) -> None:
        import os
        import threading as _threading
        from unittest.mock import patch

        from fatfish.config import Config, load_config
        from fatfish.context import ensure_default_persona
        from fatfish.db import Database
        from fatfish.web.server import create_server

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
        data = load_config("low").as_dict()
        data.setdefault("tts", {})["enabled"] = False
        cls.config = Config(data, profile="low")
        cls.config.ensure_dirs()
        db = Database(cls.config.db_path)
        db.initialize()
        ensure_default_persona(db)
        db.close()

        cls.runtime = FakeRuntime()
        cls.plane = ControlPlane(cls.runtime, token_path(cls.data_dir), port=0)
        cls.plane.start()

        cls.server = create_server(cls.config, host="127.0.0.1", port=0, token="web-token")
        cls.port = int(cls.server.server_address[1])
        cls.thread = _threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.plane.stop()
        cls._env.stop()
        cls._tmp.cleanup()

    def _call(self, method: str, path: str, body: dict | None = None):
        import http.client

        headers = {"Cookie": "fatfish_token=web-token"}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["X-Fatfish-Request"] = "1"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8") or "{}")
        finally:
            connection.close()

    def test_get_services_proxies_the_supervisor(self) -> None:
        status, payload = self._call("GET", "/api/services")
        self.assertEqual(200, status)
        self.assertTrue(payload["available"])
        self.assertEqual(
            {"tts", "embedding", "comfyui", "qq"},
            {row["name"] for row in payload["services"]},
        )
        # jobs must travel too: a 202 that later fails is only visible there.
        self.assertIn("jobs", payload)

    def test_jobs_expose_a_failure(self) -> None:
        self.runtime.fail_next = "stop"
        status, accepted = self._call("POST", "/api/services/tts/stop", {})
        self.assertEqual(202, status)
        job_id = accepted["job"]["id"]
        deadline = time.monotonic() + 5.0
        job = None
        while time.monotonic() < deadline:
            _, payload = self._call("GET", "/api/services")
            job = next((j for j in payload["jobs"] if j["id"] == job_id), None)
            if job and job["state"] in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertIsNotNone(job)
        self.assertEqual("failed", job["state"])
        self.assertIn("假装启动失败", job["error"])

    def test_post_start_returns_202_and_a_job(self) -> None:
        status, payload = self._call("POST", "/api/services/tts/start", {})
        self.assertEqual(202, status)
        self.assertTrue(payload["job"]["id"])
        self.assertEqual("queued", payload["job"]["state"])

    def test_unknown_service_is_404(self) -> None:
        status, payload = self._call("POST", "/api/services/nope/start", {})
        self.assertEqual(404, status)
        self.assertFalse(payload["ok"])

    def test_release_route(self) -> None:
        status, payload = self._call("POST", "/api/services/release", {})
        self.assertEqual(202, status)
        self.assertEqual("release", payload["job"]["action"])

    def test_no_supervisor_is_reported_not_crashed(self) -> None:
        self.plane.stop()
        try:
            status, payload = self._call("GET", "/api/services")
            self.assertEqual(200, status)
            self.assertFalse(payload["available"])
            self.assertEqual([], payload["services"])
        finally:
            self.plane = ControlPlane(self.runtime, token_path(self.data_dir), port=0)
            self.plane.start()


if __name__ == "__main__":
    unittest.main()
