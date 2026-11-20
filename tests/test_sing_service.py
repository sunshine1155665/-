"""Tests for on-demand singing (YuE2 -> RVC) and its chat/QQ/Web integrations.

No model, no GPU and no subprocess: the render step is a fake runner that
writes the wav the real command would have written.  The vram lease and TTS
handoff are exercised through fake callables so the order (unload -> render ->
reload) is asserted without touching port 9880.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.context import TOOL_SCHEMA, ensure_default_persona
from fatfish.db import SCHEMA_VERSION, Database
from fatfish.llm import LLMResult, LLMUsage
from fatfish.platforms.base import InboundMessage
from fatfish.platforms.qq import QQAdapter
from fatfish.process_lock import acquire, lock_path
from fatfish.runtime import Runtime
from fatfish.security.policy import PolicyEngine
from fatfish.security.trust import TrustLevel
from fatfish.sing import (
    SingResult,
    SingService,
    RenderRequest,
    friendly_sing_error,
)
from fatfish.web.server import _attach_audio_url, _audio_url_for

WAV_BYTES = b"RIFF" + b"\x00" * 40 + b"WAVEfmt " + b"\x00" * 8


class FakeLLM:
    available = True

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return LLMResult(
            text=self.text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            model="fake-model",
        )


class SequenceLLM(FakeLLM):
    """Returns one canned reply per call; the last one repeats."""

    def __init__(self, *texts: str):
        super().__init__(texts[-1] if texts else "")
        self.texts = list(texts)

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        text = self.texts.pop(0) if self.texts else self.text
        return LLMResult(
            text=text,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            model="fake-model",
        )


class FakeTTSBridge:
    """The slice of TTS control SingService uses."""

    def __init__(self, *, loaded: bool = True):
        self.loaded_value = loaded
        self.calls: list[str] = []

    def reachable(self) -> bool:
        return True

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


def _values(*items):
    seq = list(items)

    def next_value():
        if not seq:
            return seq[-1] if seq else None
        return seq.pop(0)

    return next_value


def make_env(tmp: Path, *, sing_extra: str = ""):
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
                "phrases": [{"text": t} for t in (
                    "本鲸鱼娘今天好想你",
                    "我数着海浪等你回来",
                    "热汤和拥抱都准备好",
                    "主人哥哥今天辛苦了",
                )],
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
{sing_extra}
"""
    path = tmp / "sing.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db


class FakeRunner:
    """Records the command; optionally writes the wav or returns an error."""

    def __init__(self, *, ok: bool = True, error: str = ""):
        self.ok = ok
        self.error = error
        self.requests: list[RenderRequest] = []

    def __call__(self, request: RenderRequest) -> tuple[bool, str]:
        self.requests.append(request)
        if not self.ok:
            return False, self.error
        request.output_dir.mkdir(parents=True, exist_ok=True)
        (request.output_dir / "full.wav").write_bytes(WAV_BYTES)
        request.output_dir.joinpath("report.json").write_text(
            json.dumps({"phrases": []}), encoding="utf-8"
        )
        return True, ""


class SingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.addCleanup(self.db.close)
        self.runner = FakeRunner()
        self.tts = FakeTTSBridge()
        self.free_mb = _values(8000)
        self.service = self._service()

    def _service(self) -> SingService:
        return SingService(
            self.config,
            self.db,
            runner=self.runner,
            tts=self.tts,
            vram_free=lambda: self.free_mb(),
        )

    def _job(self, job_id: str):
        return self.db.query_one("SELECT * FROM sing_jobs WHERE id=?", (job_id,))

    def test_disabled_returns_error_without_a_job(self) -> None:
        self.service.enabled = False
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "disabled")
        self.assertEqual(self.runner.requests, [])
        rows = self.db.query("SELECT * FROM sing_jobs")
        self.assertEqual(rows, [])

    def test_success_renders_default_song_and_records_the_job(self) -> None:
        result = self.service.sing(
            emotion="genki", session_id="qq:private:1", now=100.0
        )
        self.assertTrue(result.ok, result.error)
        self.assertTrue(Path(result.path or "").is_file())
        self.assertEqual(result.emotion, "genki")
        self.assertEqual(result.title, "等你回家")
        req = self.runner.requests[0]
        self.assertEqual(req.command[0], str(self.tmp / "python.exe"))
        self.assertEqual(req.command[1], str(self.tmp / "song_yue2_rvc.py"))
        self.assertIn("--emotion", req.command)
        self.assertEqual(req.command[req.command.index("--emotion") + 1], "genki")
        self.assertIn("--out", req.command)
        self.assertIn("--src", req.command)
        src = req.command[req.command.index("--src") + 1]
        self.assertEqual(Path(src), self.tmp / "default.json")
        self.assertEqual(result.lyrics, [])
        row = self._job(result.job_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["emotion"], "genki")
        self.assertEqual(row["audio_path"], result.path)
        self.assertEqual(json.loads(row["lyrics_json"]), [])
        self.assertGreaterEqual(float(row["elapsed_ms"]), 0.0)

    def test_custom_lyrics_are_written_and_used_as_src(self) -> None:
        lyrics = [
            "窗外下着小雨呀",
            "我在等你回家呀",
            "热汤已经煮好了",
            "快过来抱抱我吧",
        ]
        result = self.service.sing(
            lyrics=lyrics, session_id="qq:private:1", now=100.0
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.lyrics, lyrics)
        self.assertEqual(result.title, "你点的歌")
        req = self.runner.requests[0]
        src = Path(req.command[req.command.index("--src") + 1])
        self.assertEqual(src.name, "lyrics.json")
        payload = json.loads(src.read_text(encoding="utf-8"))
        self.assertEqual([p["text"] for p in payload["phrases"]], lyrics)
        self.assertEqual(payload["lyrics"], lyrics)
        row = self._job(result.job_id)
        self.assertEqual(json.loads(row["lyrics_json"]), lyrics)

    def test_full_lyrics_with_section_tags_are_kept(self) -> None:
        lyrics = [
            "[Verse]",
            "洗完头发还湿着",
            "尾巴软软拖在地上",
            "[Chorus]",
            "主人哥哥别学太晚",
            "我在这儿等你回家",
        ]
        result = self.service.sing(
            lyrics=lyrics, session_id="qq:private:1", now=100.0
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.lyrics, lyrics)
        self.assertEqual(result.lyrics_error, "")
        req = self.runner.requests[0]
        src = Path(req.command[req.command.index("--src") + 1])
        payload = json.loads(src.read_text(encoding="utf-8"))
        self.assertEqual(payload["lyrics"], lyrics)
        # phrases keeps only sung lines; the renderer forwards the tags.
        self.assertEqual(
            [p["text"] for p in payload["phrases"]],
            [line for line in lyrics if not line.startswith("[")],
        )

    def test_lyrics_count_bounds(self) -> None:
        result = self.service.sing(
            lyrics=[f"第{n}句在唱歌呀" for n in range(17)],
            session_id="qq:private:1",
            now=100.0,
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.lyrics, [])
        self.assertEqual(result.lyrics_error, "lyrics_bad_count")
        self.assertEqual(
            self.service.sing(
                lyrics=[f"第{n}句在唱歌呀" for n in range(4)],
                session_id="qq:private:1",
                now=100.0,
            ).lyrics_error,
            "",
        )

    def test_bad_lyrics_fall_back_to_the_default_song(self) -> None:
        result = self.service.sing(
            lyrics=["只有一句"], session_id="qq:private:1", now=100.0
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.lyrics, [])
        self.assertEqual(result.lyrics_error, "lyrics_bad_count")
        src = self.runner.requests[0].command
        self.assertEqual(
            Path(src[src.index("--src") + 1]), self.tmp / "default.json"
        )

    def test_render_failure_marks_the_job_failed(self) -> None:
        self.runner.ok = False
        self.runner.error = "render_timeout"
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "render_timeout")
        self.assertIn("超时", friendly_sing_error(result.error))
        row = self._job(result.job_id)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"], "render_timeout")
        self.assertIsNone(row["audio_path"])

    def test_busy_when_a_job_already_owns_the_lock(self) -> None:
        self.assertTrue(self.service._lock.acquire(blocking=False))
        try:
            result = self.service.sing(session_id="qq:private:1", now=100.0)
        finally:
            self.service._lock.release()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "busy")
        self.assertEqual(self.runner.requests, [])

    def test_vram_lease_busy_fails_before_rendering(self) -> None:
        lease = acquire(lock_path(self.config.data_dir, "vram"))
        self.assertIsNotNone(lease)
        try:
            result = self.service.sing(session_id="qq:private:1", now=100.0)
        finally:
            lease.release()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "vram_busy")
        self.assertEqual(self.runner.requests, [])

    def test_low_vram_unloads_tts_then_reloads_after_the_song(self) -> None:
        self.free_mb = _values(100, 8000)
        service = self._service()
        result = service.sing(session_id="qq:private:1", now=100.0)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.tts.calls, ["unload", "load"])
        self.assertTrue(result.tts_unloaded)
        self.assertTrue(result.tts_reloaded)

    def test_enough_vram_leaves_tts_alone(self) -> None:
        self.free_mb = _values(8000)
        service = self._service()
        result = service.sing(session_id="qq:private:1", now=100.0)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.tts.calls, [])
        self.assertFalse(result.tts_unloaded)

    def test_unload_without_enough_vram_fails_cleanly(self) -> None:
        self.free_mb = _values(100, 100, 100)
        self.config._data["sing"]["vram_wait_seconds"] = 0.05
        # The unload call succeeds but the card is still occupied by something
        # else: /health keeps saying loaded, so the wait must time out.
        self.tts.unload = lambda: (self.tts.calls.append("unload"), True)[1]
        self.tts.loaded = lambda: True
        service = self._service()
        result = service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "vram_conflict_tts")
        self.assertEqual(self.runner.requests, [])
        self.assertEqual(self.tts.calls, ["unload", "load"])

    def test_missing_assets_fail_before_spawning(self) -> None:
        Path(self.config.get("sing.script")).unlink()
        result = self.service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertIn("script_missing", result.error)
        self.assertEqual(self.runner.requests, [])

    def test_unknown_emotion_falls_back_to_the_configured_default(self) -> None:
        result = self.service.sing(
            emotion="screamo", session_id="qq:private:1", now=100.0
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.emotion, "soft")
        self.assertIn("soft", self.runner.requests[0].command)

    def test_lyrics_as_a_newline_string_are_accepted(self) -> None:
        result = self.service.sing(
            lyrics="窗外下着小雨呀\n我在等你回家呀\n热汤已经煮好了\n快过来抱抱我吧",
            session_id="qq:private:1",
            now=100.0,
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.lyrics), 4)
        self.assertEqual(result.lyrics_error, "")

    def test_runner_without_an_output_file_is_a_failure(self) -> None:
        def empty_runner(request: RenderRequest) -> tuple[bool, str]:
            self.runner.requests.append(request)
            return True, ""

        service = SingService(
            self.config,
            self.db,
            runner=empty_runner,
            tts=self.tts,
            vram_free=lambda: 8000,
        )
        result = service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "render_failed:no_output")
        row = self._job(result.job_id)
        self.assertEqual(row["status"], "failed")

    def test_unload_disabled_low_vram_fails_without_touching_tts(self) -> None:
        self.config._data["sing"]["unload_tts"] = False
        self.free_mb = _values(100)
        service = self._service()
        result = service.sing(session_id="qq:private:1", now=100.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "vram_conflict_tts")
        self.assertEqual(self.tts.calls, [])
        self.assertEqual(self.runner.requests, [])


class SingPolicyTests(unittest.TestCase):
    def test_sing_is_private_only_and_once_per_turn(self) -> None:
        policy = PolicyEngine()
        self.assertTrue(
            policy.validate(
                "sing", {"emotion": "soft"}, trust=TrustLevel.PRIVATE
            ).allowed
        )
        group = policy.validate(
            "sing", {"emotion": "soft"}, trust=TrustLevel.GROUP
        )
        self.assertFalse(group.allowed)
        self.assertEqual(group.reason, "group_not_allowed")
        twice = policy.validate("sing", {}, trust=TrustLevel.PRIVATE, count=1)
        self.assertFalse(twice.allowed)
        self.assertEqual(twice.reason, "too_many_tool_calls")

    def test_tool_schema_documents_singing(self) -> None:
        self.assertIn("sing", TOOL_SCHEMA)
        self.assertIn("emotion", TOOL_SCHEMA)
        self.assertIn("lyrics", TOOL_SCHEMA)


class SingRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.addCleanup(self.db.close)
        self.runtime = Runtime(self.db, self.config)
        self.wav = self.tmp / "song.wav"
        self.wav.write_bytes(WAV_BYTES)
        self.runtime.llm = FakeLLM(
            json.dumps(
                {
                    "messages": ["好呀，本鲸鱼娘唱给你听。"],
                    "actions": [{"tool": "sing", "args": {"emotion": "soft"}}],
                },
                ensure_ascii=False,
            )
        )

    def _stub(self, result: SingResult | None = None):
        class Stub:
            enabled = True

            def __init__(self) -> None:
                self.calls = []

            def sing(self, **kwargs):
                self.calls.append(kwargs)
                return result or SingResult(
                    True,
                    path=str(self_outer.wav),
                    emotion=kwargs.get("emotion") or "soft",
                    job_id="job1",
                    title="等你回家",
                )

        self_outer = self
        stub = Stub()
        self.runtime.sing = stub
        return stub

    def test_private_sing_returns_the_audio_payload(self) -> None:
        stub = self._stub()
        result = self.runtime.handle_user_message(
            "qq:private:1001", "唱首歌给我听", now=100.0
        )
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]["emotion"], "soft")
        self.assertTrue(result["sing"]["ok"])
        self.assertEqual(result["sing"]["path"], str(self.wav))
        self.assertEqual(
            result["actions"][0]["tool"], "sing"
        )

    def test_private_sing_writes_a_memory_item(self) -> None:
        from fatfish.owner import OwnerRegistry

        OwnerRegistry(self.db, self.config).bind("qq:private:1001", "主人哥哥")
        self._stub()
        self.runtime.handle_user_message(
            "qq:private:1001", "唱首歌给我听", now=100.0
        )
        row = self.db.query_one(
            "SELECT gist, source_type FROM memory_items "
            "WHERE source_type='sing' ORDER BY observed_at DESC LIMIT 1"
        )
        self.assertIsNotNone(row)
        self.assertIn("给主人哥哥唱了", row["gist"])
        self.assertIn("等你回家", row["gist"])

    def test_sing_review_reads_the_previous_song(self) -> None:
        self._stub()
        self.runtime.handle_user_message(
            "qq:private:1001", "唱首歌给我听", now=100.0
        )
        self.runtime.llm = FakeLLM(
            json.dumps(
                {"messages": ["上次唱的是《等你回家》。"]}, ensure_ascii=False
            )
        )
        result = self.runtime.handle_user_message(
            "qq:private:1001", "你上次唱的歌是什么？", now=200.0
        )
        sent = "\n".join(
            str(message.get("content") or "")
            for message in self.runtime.llm.calls[-1]["messages"]
        )
        self.assertIn("[最近唱过的歌", sent)
        self.assertIn("等你回家", sent)
        self.assertTrue(result["memory"]["check"])

    def test_group_never_reaches_the_singing_service(self) -> None:
        stub = self._stub()
        result = self.runtime.handle_user_message(
            "qq:group:2001", "唱首歌", is_group=True, now=100.0
        )
        self.assertEqual(stub.calls, [])
        self.assertIsNone(result.get("sing"))
        denied = [item for item in result["actions"] if item["tool"] == "sing"]
        self.assertTrue(denied and not denied[0]["ok"])
        self.assertEqual(denied[0]["error"], "group_not_allowed")

    def test_sing_failure_keeps_the_text_reply_and_explains(self) -> None:
        self._stub(SingResult(False, error="vram_conflict_tts"))
        result = self.runtime.handle_user_message(
            "qq:private:1002", "唱首歌", now=100.0
        )
        self.assertEqual(result["reply"].splitlines()[0], "好呀，本鲸鱼娘唱给你听。")
        self.assertFalse(result["sing"]["ok"])
        self.assertTrue(any("显存" in message for message in result["messages"]))

    def test_lyrics_argument_is_forwarded(self) -> None:
        self.runtime.llm = FakeLLM(
            json.dumps(
                {
                    "messages": ["唱给你听。"],
                    "actions": [
                        {
                            "tool": "sing",
                            "args": {
                                "emotion": "genki",
                                "lyrics": [
                                    "窗外下着小雨呀",
                                    "我在等你回家呀",
                                    "热汤已经煮好了",
                                    "快过来抱抱我吧",
                                ],
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        stub = self._stub()
        self.runtime.handle_user_message("qq:private:1003", "唱首下雨的歌", now=100.0)
        self.assertEqual(stub.calls[0]["emotion"], "genki")
        self.assertEqual(len(stub.calls[0]["lyrics"]), 4)

    def test_invalid_lyrics_get_one_model_rewrite_round(self) -> None:
        """ADR-0018 配套：不合格歌词先打回给模型重写，而不是默默唱默认歌。"""
        bad_lines = ["下雨了", "想你了", "回家吧"]  # 3 句正文 -> lyrics_bad_count
        fixed_lines = ["窗外下着小雨呀", "我在等你回家呀", "热汤已经煮好了", "快过来抱抱我吧"]
        self.runtime.llm = SequenceLLM(
            json.dumps(
                {
                    "messages": ["唱给你听。"],
                    "actions": [
                        {"tool": "sing", "args": {"emotion": "soft", "lyrics": bad_lines}}
                    ],
                },
                ensure_ascii=False,
            ),
            json.dumps({"lyrics": fixed_lines}, ensure_ascii=False),
        )
        stub = self._stub()
        self.runtime.handle_user_message("qq:private:1004", "唱首歌", now=100.0)
        self.assertEqual(stub.calls[0]["lyrics"], fixed_lines)
        # 重写那轮是独立的小对话：错误码和原稿都在提示里。
        rewrite = self.runtime.llm.calls[1]
        system = rewrite["messages"][0]["content"]
        self.assertIn("lyrics_bad_count", system)
        self.assertIn("下雨了", system)
        self.assertIn("4-16", system)

    def test_still_invalid_rewrite_keeps_service_fallback(self) -> None:
        bad_lines = ["下雨了", "想你了", "回家吧"]
        self.runtime.llm = SequenceLLM(
            json.dumps(
                {
                    "messages": ["唱给你听。"],
                    "actions": [
                        {"tool": "sing", "args": {"emotion": "soft", "lyrics": bad_lines}}
                    ],
                },
                ensure_ascii=False,
            ),
            "我不会写 JSON。",  # 重写轮给不出合法 JSON
        )
        stub = self._stub()
        self.runtime.handle_user_message("qq:private:1004", "唱首歌", now=100.0)
        # 原样转发：服务端 clean_lyrics 失败后落回默认歌（既有兜底不变）。
        self.assertEqual(stub.calls[0]["lyrics"], bad_lines)
        self.assertEqual(len(self.runtime.llm.calls), 2)  # 只重写一次

    def test_valid_lyrics_skip_the_rewrite_round(self) -> None:
        good_lines = ["窗外下着小雨呀", "我在等你回家呀", "热汤已经煮好了", "快过来抱抱我吧"]
        self.runtime.llm = SequenceLLM(
            json.dumps(
                {
                    "messages": ["唱给你听。"],
                    "actions": [
                        {"tool": "sing", "args": {"emotion": "soft", "lyrics": good_lines}}
                    ],
                },
                ensure_ascii=False,
            ),
            "这一轮不该被用到",
        )
        stub = self._stub()
        self.runtime.handle_user_message("qq:private:1004", "唱首歌", now=100.0)
        self.assertEqual(stub.calls[0]["lyrics"], good_lines)
        self.assertEqual(len(self.runtime.llm.calls), 1)  # 没有重写轮


class SingQQTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config, self.db = make_env(self.root)
        self.addCleanup(self.db.close)
        self.runtime = Runtime(self.db, self.config)
        self.adapter = QQAdapter(self.runtime, self.config)
        self.adapter.client = FakeOneBotClient()
        self.fake = self.adapter.client
        self.wav = self.root / "song.wav"
        self.wav.write_bytes(WAV_BYTES)

    def _message(self, *, group: bool = False) -> InboundMessage:
        return InboundMessage(
            platform="qq",
            event_id="evt-sing",
            session_id="qq:group:2" if group else "qq:private:1",
            user_id="1",
            group_id="2" if group else None,
            text="唱首歌",
            is_group=group,
        )

    def test_private_song_is_queued_and_sent_as_record(self) -> None:
        queued, errors, item_ids = self.adapter._queue_media_result(  # noqa: SLF001
            {"sing": {"ok": True, "path": str(self.wav), "emotion": "soft"}},
            target_type="private",
            target_id="1",
            event_id="evt-sing",
            is_group=False,
        )
        self.assertEqual(queued, 1)
        self.assertEqual(errors, [])
        self.assertEqual(len(item_ids), 1)
        results = self.runtime.process_outbox(handlers=self.adapter.handlers())
        self.assertEqual(results[0]["status"], "done")
        target_type, target_id, segments = self.fake.messages[0]
        self.assertEqual((target_type, target_id), ("private", "1"))
        self.assertEqual(segments[0]["type"], "record")

    def test_group_song_is_not_queued(self) -> None:
        queued, errors, _ = self.adapter._queue_media_result(  # noqa: SLF001
            {"sing": {"ok": True, "path": str(self.wav)}},
            target_type="group",
            target_id="2",
            event_id="evt-sing",
            is_group=True,
        )
        self.assertEqual(queued, 0)
        self.assertEqual(errors, [])

    def test_failed_song_is_not_queued(self) -> None:
        queued, errors, _ = self.adapter._queue_media_result(  # noqa: SLF001
            {"sing": {"ok": False, "path": None, "error": "busy"}},
            target_type="private",
            target_id="1",
            event_id="evt-sing",
            is_group=False,
        )
        self.assertEqual(queued, 0)
        self.assertEqual(errors, [])


class SingWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)
        self.addCleanup(self.db.close)
        self.song_dir = self.config.data_dir / "songs" / "job1"
        self.song_dir.mkdir(parents=True, exist_ok=True)
        self.wav = self.song_dir / "full.wav"
        self.wav.write_bytes(WAV_BYTES)

    def test_audio_url_is_mapped_for_a_song(self) -> None:
        url = _audio_url_for(str(self.wav), self.config)
        self.assertIsNotNone(url)
        self.assertIn("/api/audio?path=", url)
        self.assertIn("songs/job1/full.wav", url)

    def test_attach_audio_url_reaches_the_sing_payload(self) -> None:
        result = {"sing": {"ok": True, "path": str(self.wav)}}
        _attach_audio_url(result, self.config)
        self.assertIn("/api/audio?path=", result["sing"]["audio_url"])

    def test_audio_outside_the_data_dir_is_not_mapped(self) -> None:
        outside = self.tmp / "outside.wav"
        outside.write_bytes(WAV_BYTES)
        self.assertIsNone(_audio_url_for(str(outside), self.config))


class FakeOneBotClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, list[dict]]] = []
        self.connected = True

    def send_private_message(self, user_id, message):
        self.messages.append(("private", str(user_id), list(message)))
        return "echo-media"

    def send_group_message(self, group_id, message):
        self.messages.append(("group", str(group_id), list(message)))
        return "echo-media"


class SingSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "schema.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.db.initialize()

    def test_sing_jobs_table_exists_and_version_is_current(self) -> None:
        self.assertEqual(SCHEMA_VERSION, "17")
        tables = {
            row["name"]
            for row in self.db.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("sing_jobs", tables)
        cols = self.db.columns("sing_jobs")
        for column in (
            "id",
            "session_id",
            "created_at",
            "finished_at",
            "status",
            "emotion",
            "lyrics_json",
            "audio_path",
            "error",
            "tts_unloaded",
            "elapsed_ms",
        ):
            self.assertIn(column, cols)

    def test_a_v9_database_gains_sing_jobs_in_place(self) -> None:
        db = Database(self.path)
        db.execute("DROP TABLE sing_jobs")
        db.set_meta("schema_version", "9")
        applied = db.migrate()
        self.assertIn("sing_jobs", applied["applied"])
        self.assertEqual(db.get_meta("schema_version"), SCHEMA_VERSION)
        self.assertIn("audio_path", db.columns("sing_jobs"))
        # idempotent
        again = db.migrate()
        self.assertEqual(again["applied"], [])
        db.close()


if __name__ == "__main__":
    unittest.main()
