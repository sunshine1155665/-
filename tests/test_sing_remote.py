"""T-0067 云端唱歌：`scripts/sing_server.py` + `SingService(mode="remote")`。

全部 mock：没有 GPU、没有 9880、没有 YuE2/RVC、没有真 subprocess。
HTTP 那一段用真的 ``http.server``（随机端口 + 127.0.0.1），因为要验的是
「401 / 409 / 504 / 200 audio/wav」这些协议细节，而不是函数返回值。
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.config import load_config  # noqa: E402
from fatfish.db import Database  # noqa: E402
from fatfish.process_lock import acquire, lock_path  # noqa: E402
from fatfish.sing import SingResult, SingService, friendly_sing_error  # noqa: E402
from fatfish.sing.service import RemoteSingerClient  # noqa: E402
from scripts.sing_server import (  # noqa: E402
    LocalSinger,
    RenderOutcome,
    SingApp,
    make_server,
)

WAV_BYTES = b"RIFF" + b"\x00" * 40 + b"WAVEfmt " + b"\x00" * 8
LYRICS = [
    "窗外下着小雨呀",
    "我在等你回家呀",
    "热汤已经煮好了",
    "快过来抱抱我吧",
]

# 服务端的每行日志（含 401 与崩溃栈）在测试里只会污染输出。
logging.getLogger("sing_server").setLevel(logging.CRITICAL)


# --------------------------------------------------------------------------- #
# 共用桩
# --------------------------------------------------------------------------- #
class FakeTTS:
    """``SingService`` 用到的 TTS 交接面。"""

    def __init__(self, *, loaded: bool = True, reachable: bool = True):
        self.loaded_value = loaded
        self._reachable = reachable
        self.calls: list[str] = []

    def reachable(self) -> bool:
        return self._reachable

    def loaded(self) -> bool | None:
        return self.loaded_value

    def unload(self) -> bool:
        self.calls.append("unload")
        self.loaded_value = False
        return True

    def load(self) -> bool:
        self.calls.append("load")
        self.loaded_value = True
        return True


class FakeRemote:
    """``RemoteSingerClient`` 的替身：只记 call，不做网络。"""

    def __init__(
        self,
        *,
        reachable: bool = True,
        status: int = 200,
        body: bytes = WAV_BYTES,
        headers: dict[str, str] | None = None,
        raises: BaseException | None = None,
    ):
        self._reachable = reachable
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.raises = raises
        self.probes = 0
        self.posts: list[dict[str, Any]] = []

    def reachable(self) -> bool:
        self.probes += 1
        return self._reachable

    def post(self, payload: dict[str, Any]):
        self.posts.append(payload)
        if self.raises is not None:
            raise self.raises
        return self.status, self.body, self.headers


class FakeRunner:
    """本地渲染桩：写 wav / 返回错误 / 直接抛异常，三选一。"""

    def __init__(self, *, ok: bool = True, error: str = "", raises: BaseException | None = None):
        self.ok = ok
        self.error = error
        self.raises = raises
        self.requests: list[Any] = []

    def __call__(self, request):
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if not self.ok:
            return False, self.error
        request.output_dir.mkdir(parents=True, exist_ok=True)
        (request.output_dir / "full.wav").write_bytes(WAV_BYTES)
        return True, ""


class FakeBackend:
    """``LocalSinger`` 的替身，给 HTTP 层用。"""

    def __init__(
        self,
        outcome: RenderOutcome | None = None,
        *,
        raises: BaseException | None = None,
        release: threading.Event | None = None,
    ):
        self.outcome = outcome
        self.raises = raises
        self.release = release
        self.entered = threading.Event()
        self.calls: list[tuple[str, Any]] = []

    def render(self, *, emotion: str, lyrics: Any) -> RenderOutcome:
        self.calls.append((emotion, lyrics))
        self.entered.set()
        if self.release is not None:
            self.release.wait(5.0)
        if self.raises is not None:
            raise self.raises
        return self.outcome or RenderOutcome(
            ok=True,
            wav=WAV_BYTES,
            job_id="job-remote-1",
            tts_unloaded=True,
            tts_reloaded=True,
            elapsed_ms=1234,
        )

    def health(self, *, busy: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "tts_loaded": True,
            "vram_locked": bool(busy),
            "busy": bool(busy),
        }


def make_env(tmp: Path, *, mode: str = "remote", remote_url: str = "http://127.0.0.1:9") -> tuple[Any, Any]:
    """一份最小的 sing 配置 + 空库。"""
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    python = tmp / "python.exe"
    python.write_bytes(b"")
    script = tmp / "song_yue2_rvc.py"
    script.write_text("# fake renderer\n", encoding="utf-8")
    default_src = tmp / "default.json"
    default_src.write_text(
        json.dumps(
            {
                "title": "等你回家",
                "phrases": [{"text": t} for t in LYRICS],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'sing.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[voice]
enabled = false

[tts]
enabled = false
base_url = ""

[sticker]
enabled = false

[sing]
enabled = true
mode = "{mode}"
remote_url = "{remote_url}"
python = "{python.as_posix()}"
script = "{script.as_posix()}"
soulx_root = ""
deps_path = ""
default_src = "{default_src.as_posix()}"
prompt_wav = ""
prompt_meta = ""
output_dir = "songs"
timeout_seconds = 5
emotion = "soft"
semitones = 3
min_free_vram_mb = 6800
unload_tts = true
reload_tts = true
vram_wait_seconds = 1
"""
    path = tmp / "sing_remote.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db


# --------------------------------------------------------------------------- #
# SingService：remote 模式
# --------------------------------------------------------------------------- #
class SingServiceRemoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.addCleanup(self.db.close)
        self.tts = FakeTTS()
        self.remote = FakeRemote()
        self.service = SingService(
            self.config,
            self.db,
            tts=self.tts,
            vram_free=lambda: 0,
            remote_client=self.remote,
        )

    def _job(self, job_id: str):
        return self.db.query_one("SELECT * FROM sing_jobs WHERE id=?", (job_id,))

    def _last_metric(self):
        return self.db.query_one(
            "SELECT * FROM metrics WHERE key='sing_error' ORDER BY id DESC LIMIT 1"
        )

    # -- 正常路径
    def test_remote_success_saves_the_wav_and_records_the_job(self) -> None:
        self.remote.headers = {"X-Tts-Unloaded": "1", "X-Tts-Reloaded": "1"}
        result = self.service.sing(
            lyrics=LYRICS, emotion="genki", session_id="qq:private:1", now=100.0
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.emotion, "genki")
        self.assertEqual(result.title, "你点的歌")
        wav = Path(result.path or "")
        self.assertTrue(wav.is_file())
        self.assertEqual(wav.read_bytes(), WAV_BYTES)
        self.assertEqual(wav.parent.name, result.job_id)
        self.assertEqual(wav.parent.parent, self.config.data_dir / "songs")
        row = self._job(result.job_id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["audio_path"], result.path)
        self.assertEqual(json.loads(row["lyrics_json"]), LYRICS)
        self.assertTrue(result.tts_unloaded)
        self.assertTrue(result.tts_reloaded)
        self.assertEqual(self.remote.posts[0]["lyrics"], LYRICS)
        self.assertEqual(self.remote.posts[0]["emotion"], "genki")

    def test_remote_success_does_not_touch_the_local_tts(self) -> None:
        self.remote.headers = {"X-Tts-Unloaded": "1"}
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertTrue(result.ok, result.error)
        # 家里的 TTS 由 sing_server 负责收尾，云上不该去 POST 9880。
        self.assertEqual(self.tts.calls, [])

    def test_remote_mode_does_not_require_local_assets(self) -> None:
        Path(str(self.config.get("sing.script"))).unlink()
        Path(str(self.config.get("sing.python"))).unlink()
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.service.asset_report(), {})

    # -- 远端可答但失败
    def test_remote_409_busy_only_returns_text_and_a_metric(self) -> None:
        self.remote.status = 409
        self.remote.body = b'{"ok": false, "error": "busy"}'
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "busy")
        self.assertIsNone(result.path)
        self.assertFalse((self.config.data_dir / "songs" / result.job_id).exists())
        row = self._job(result.job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "busy")
        metric = self._last_metric()
        self.assertIsNotNone(metric)
        tags = json.loads(metric["tags_json"])
        self.assertEqual(tags["mode"], "remote")
        self.assertEqual(tags["error"], "busy")
        self.assertEqual(tags["friendly"], friendly_sing_error("busy"))
        self.assertIn("上一首", tags["friendly"])

    def test_remote_500_keeps_the_remote_error_code(self) -> None:
        self.remote.status = 500
        self.remote.body = json.dumps(
            {"ok": False, "error": "render_failed:exit_1"}, ensure_ascii=False
        ).encode("utf-8")
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "render_failed:exit_1")
        self.assertEqual(self._job(result.job_id)["status"], "failed")

    def test_remote_500_without_json_falls_back_to_the_status(self) -> None:
        self.remote.status = 502
        self.remote.body = b"<html>bad gateway</html>"
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "remote_error:502")

    def test_remote_401_is_reported_not_swallowed(self) -> None:
        self.remote.status = 401
        self.remote.body = b'{"ok": false, "error": "unauthorized"}'
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unauthorized")
        self.assertTrue(self._job(result.job_id)["error"])

    def test_remote_504_is_reported_as_a_timeout(self) -> None:
        self.remote.status = 504
        self.remote.body = b'{"ok": false, "error": "render_timeout"}'
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertEqual(result.error, "render_timeout")
        self.assertIn("超时", friendly_sing_error(result.error))

    def test_200_with_a_non_riff_body_is_rejected(self) -> None:
        self.remote.body = b"not a wav at all"
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "remote_bad_audio")

    # -- 远端根本不可达
    def test_unreachable_home_fails_fast_and_never_posts(self) -> None:
        self.remote._reachable = False
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "home_offline")
        self.assertEqual(self.remote.posts, [])
        # 关键：不是等满 remote_timeout 才回一句文字。
        self.assertLess(result.elapsed_ms, 1000.0)
        self.assertIn("没连上", friendly_sing_error(result.error))
        self.assertEqual(self._last_metric()["key"], "sing_error")

    def test_connection_error_is_home_offline(self) -> None:
        self.remote.raises = urllib.error.URLError("connection refused")
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertEqual(result.error, "home_offline")

    def test_socket_timeout_is_remote_timeout(self) -> None:
        self.remote.raises = TimeoutError("timed out")
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertEqual(result.error, "remote_timeout")

    def test_urlerror_wrapping_a_timeout_is_remote_timeout(self) -> None:
        self.remote.raises = urllib.error.URLError(TimeoutError("timed out"))
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertEqual(result.error, "remote_timeout")

    def test_remote_without_a_url_is_reported_not_attempted(self) -> None:
        service = SingService(self.config, self.db, tts=self.tts, vram_free=lambda: 0)
        service.remote_client = None
        result = service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "remote_not_configured")

    def test_remote_client_reads_the_token_from_the_environment(self) -> None:
        import os

        self.config._data["sing"]["remote_url"] = "http://home:9445"
        os.environ["SING_SERVER_TOKEN"] = "tok-123"
        try:
            service = SingService(self.config, self.db)
        finally:
            os.environ.pop("SING_SERVER_TOKEN", None)
        self.assertIsInstance(service.remote_client, RemoteSingerClient)
        self.assertEqual(service.remote_client.token, "tok-123")
        self.assertEqual(service.remote_client.base_url, "http://home:9445")


class SingServiceLocalFinallyTests(unittest.TestCase):
    """渲染抛异常时，finally 仍然要把 TTS POST /load 回来（最重要的一条）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp, mode="local")
        self.addCleanup(self.db.close)
        self.tts = FakeTTS()

    def test_runner_exception_still_reloads_tts(self) -> None:
        runner = FakeRunner(raises=RuntimeError("CUDA out of memory"))
        service = SingService(
            self.config,
            self.db,
            runner=runner,
            tts=self.tts,
            vram_free=lambda: 100,  # 必须先把 TTS 卸掉
        )
        result = service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertIn("RuntimeError", result.error)
        self.assertEqual(self.tts.calls, ["unload", "load"])
        self.assertTrue(result.tts_unloaded)
        self.assertTrue(result.tts_reloaded)
        row = self.db.query_one("SELECT status, error FROM sing_jobs WHERE id=?", (result.job_id,))
        self.assertEqual(row["status"], "failed")


# --------------------------------------------------------------------------- #
# scripts/sing_server.py：HTTP 层
# --------------------------------------------------------------------------- #
class SingServerCase(unittest.TestCase):
    token = "s3cret"
    backend: FakeBackend

    def make_backend(self) -> FakeBackend:
        return FakeBackend()

    def setUp(self) -> None:
        self.backend = self.make_backend()
        self.app = SingApp(self.backend, timeout=420)
        self.httpd = make_server("127.0.0.1", 0, self.app, token=self.token)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.daemon = True
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    # -- HTTP helpers（绕开系统代理，别让环境变量把 127.0.0.1 也代理走）
    def _opener(self):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self, path: str):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="GET")
        try:
            with self._opener().open(request, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def post(self, payload: dict[str, Any], *, token: str | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Token"] = token
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/sing", data=body, method="POST", headers=headers
        )
        try:
            with self._opener().open(request, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()


class SingServerProtocolTests(SingServerCase):
    def test_health_reports_the_four_fields(self) -> None:
        status, _, body = self.get("/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"ok", "tts_loaded", "vram_locked", "busy"})
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["busy"])

    def test_health_does_not_need_a_token(self) -> None:
        status, _, _ = self.get("/health")
        self.assertEqual(status, 200)

    def test_wrong_token_is_401_and_never_reaches_the_renderer(self) -> None:
        status, _, body = self.post({"emotion": "soft"}, token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "unauthorized")
        self.assertEqual(self.backend.calls, [])

    def test_missing_token_is_401(self) -> None:
        status, _, _ = self.post({"emotion": "soft"})
        self.assertEqual(status, 401)

    def test_success_returns_wav_bytes_and_handoff_headers(self) -> None:
        status, headers, body = self.post({"emotion": "genki", "lyrics": LYRICS}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/wav")
        self.assertEqual(headers.get("X-Tts-Unloaded"), "1")
        self.assertEqual(headers.get("X-Tts-Reloaded"), "1")
        self.assertEqual(headers.get("X-Job-Id"), "job-remote-1")
        self.assertEqual(body, WAV_BYTES)
        self.assertEqual(self.backend.calls, [("genki", LYRICS)])

    def test_unknown_path_is_404(self) -> None:
        self.assertEqual(self.get("/nope")[0], 404)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/nope",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-Token": self.token},
        )
        try:
            with self._opener().open(request, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 404)

    def test_bad_json_is_400(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/sing",
            data=b"{not json",
            method="POST",
            headers={"Content-Type": "application/json", "X-Token": self.token},
        )
        try:
            with self._opener().open(request, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        self.assertEqual(status, 400)

    def test_render_exception_is_500_and_the_server_stays_up(self) -> None:
        self.backend.raises = RuntimeError("boom")
        status, _, body = self.post({"emotion": "soft"}, token=self.token)
        self.assertEqual(status, 500)
        self.assertIn("server_error", json.loads(body)["error"])
        self.assertEqual(self.get("/health")[0], 200)
        self.assertFalse(json.loads(self.get("/health")[2])["busy"])

    def test_second_request_while_rendering_is_409(self) -> None:
        release = threading.Event()
        self.backend.release = release
        results: list[int] = []

        def first() -> None:
            results.append(self.post({"emotion": "soft"}, token=self.token)[0])

        worker = threading.Thread(target=first)
        worker.start()
        self.assertTrue(self.backend.entered.wait(5.0))
        try:
            status, _, body = self.post({"emotion": "sad"}, token=self.token)
            self.assertEqual(status, 409)
            self.assertEqual(json.loads(body)["error"], "busy")
            health = json.loads(self.get("/health")[2])
            self.assertTrue(health["busy"])
            self.assertTrue(health["vram_locked"])
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(results, [200])
        self.assertEqual(len(self.backend.calls), 1)


class SingServerStatusMappingTests(SingServerCase):
    def make_backend(self) -> FakeBackend:
        return FakeBackend(RenderOutcome(ok=False, error="render_timeout", job_id="j1"))

    def test_timeout_maps_to_504(self) -> None:
        status, _, body = self.post({"emotion": "soft"}, token=self.token)
        self.assertEqual(status, 504)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "render_timeout")
        self.assertIn("超时", payload["message"])

    def test_busy_maps_to_409(self) -> None:
        self.backend.outcome = RenderOutcome(ok=False, error="busy")
        status, _, _ = self.post({"emotion": "soft"}, token=self.token)
        self.assertEqual(status, 409)

    def test_disabled_maps_to_503(self) -> None:
        self.backend.outcome = RenderOutcome(ok=False, error="disabled")
        status, _, body = self.post({"emotion": "soft"}, token=self.token)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "disabled")

    def test_generic_failure_maps_to_500(self) -> None:
        self.backend.outcome = RenderOutcome(ok=False, error="render_failed:exit_1")
        status, _, _ = self.post({"emotion": "soft"}, token=self.token)
        self.assertEqual(status, 500)


class SingServerLocalSingerTests(SingServerCase):
    """HTTP → LocalSinger → 真 SingService（假的渲染器/TTS/显存）。

    LocalSinger 在**请求线程**里建自己的 SQLite 连接（sqlite 有线程亲和性），
    所以这里注入的是工厂；断言用的 ``self.db`` 是主线程那条只读连接，WAL
    下能读到请求线程提交的行。
    """

    token = "s3cret"

    def make_backend(self) -> LocalSinger:  # type: ignore[override]
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp, mode="local")
        self.addCleanup(self.db.close)
        self.tts = FakeTTS()
        self.runner = FakeRunner()

        def factory() -> SingService:
            # 这个连接在请求线程里创建、在请求线程里被 GC，Windows 才不会因为
            # 句柄没释放而删不掉临时目录。
            db = Database(self.config.db_path)
            return SingService(
                self.config,
                db,
                runner=self.runner,
                tts=self.tts,
                vram_free=lambda: 100,  # 低显存 → 必须走 unload/load
            )

        return LocalSinger(
            self.config, timeout=42, service_factory=factory, handoff=self.tts
        )

    def test_http_song_writes_data_songs_and_returns_tts(self) -> None:
        status, headers, body = self.post(
            {"emotion": "soft", "lyrics": LYRICS}, token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/wav")
        self.assertEqual(body, WAV_BYTES)
        self.assertEqual(self.tts.calls, ["unload", "load"])
        self.assertEqual(headers.get("X-Tts-Unloaded"), "1")
        self.assertEqual(headers.get("X-Tts-Reloaded"), "1")
        job_id = headers.get("X-Job-Id")
        self.assertTrue(job_id)
        wav = self.config.data_dir / "songs" / str(job_id) / "full.wav"
        self.assertTrue(wav.is_file())
        self.assertEqual(wav.read_bytes(), WAV_BYTES)
        row = self.db.query_one(
            "SELECT status, elapsed_ms, session_id FROM sing_jobs WHERE id=?", (job_id,)
        )
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["session_id"], "sing-server")

    def test_render_exception_still_reloads_tts_over_http(self) -> None:
        self.runner.raises = RuntimeError("CUDA out of memory")
        status, _, body = self.post({"emotion": "soft"}, token=self.token)
        self.assertEqual(status, 500)
        payload = json.loads(body)
        self.assertIn("render_failed", payload["error"])
        # 最有价值的一条：就算渲染炸了，家里 TTS 也必须被 POST /load 还回去。
        self.assertEqual(self.tts.calls, ["unload", "load"])
        self.assertEqual(self.get("/health")[0], 200)


class FakeService:
    """只有 ``LocalSinger`` 关心的两个属性：``timeout`` 和 ``sing()``。"""

    def __init__(self, wav_path: Path):
        self.timeout = 300.0
        self.wav_path = wav_path
        self.calls: list[dict[str, Any]] = []

    def sing(self, **kwargs):
        self.calls.append(kwargs)
        return SingResult(
            True,
            path=str(self.wav_path),
            job_id="job-x",
            tts_unloaded=True,
            tts_reloaded=True,
        )


class LocalSingerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp, mode="local")
        self.addCleanup(self.db.close)
        self.wav = self.tmp / "full.wav"
        self.wav.write_bytes(WAV_BYTES)

    def test_timeout_flag_reaches_the_service(self) -> None:
        service = FakeService(self.wav)
        backend = LocalSinger(self.config, timeout=42, service_factory=lambda: service)
        outcome = backend.render(emotion="genki", lyrics=LYRICS)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(service.timeout, 42.0)
        self.assertEqual(outcome.wav, WAV_BYTES)
        self.assertEqual(service.calls[0]["session_id"], "sing-server")
        self.assertEqual(service.calls[0]["emotion"], "genki")

    def test_missing_wav_is_a_failure_not_an_exception(self) -> None:
        self.wav.unlink()
        backend = LocalSinger(
            self.config, service_factory=lambda: FakeService(self.wav)
        )
        outcome = backend.render(emotion="soft", lyrics=None)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "render_failed:unreadable")

    def test_health_reports_the_four_fields_without_creating_the_lock(self) -> None:
        backend = LocalSinger(
            self.config, service_factory=lambda: FakeService(self.wav), handoff=FakeTTS()
        )
        payload = backend.health(busy=False)
        self.assertEqual(set(payload), {"ok", "tts_loaded", "vram_locked", "busy"})
        self.assertEqual(payload["tts_loaded"], True)
        self.assertFalse(payload["vram_locked"])
        # /health 是只读端点：显卡锁文件没被创建（photo_server 也这么做的）。
        self.assertFalse(backend.vram_lock.exists())

    def test_health_sees_a_foreign_vram_lease(self) -> None:
        backend = LocalSinger(
            self.config, service_factory=lambda: FakeService(self.wav), handoff=FakeTTS()
        )
        lease = acquire(lock_path(self.config.data_dir, "vram"))
        self.assertIsNotNone(lease)
        try:
            self.assertTrue(backend.health(busy=False)["vram_locked"])
        finally:
            lease.release()
        self.assertFalse(backend.health(busy=False)["vram_locked"])


if __name__ == "__main__":
    unittest.main()
