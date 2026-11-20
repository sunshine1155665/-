"""T-0042 L3: costly self actions -- ``make_photo`` and ``search_videos``.

Scope of this layer: the action runs through the existing services
(``Runtime._take_photo`` / ``BilibiliCollector.browse_once``), the result is
stored verbatim in ``self_action_evidence``, and a failure never escapes.
The desire gate and the injection into the next turn belong to L4 and are
deliberately absent here.

Offline only: fake photo service / fake collector, temp database, no network,
no ComfyUI, no TTS.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fatfish.bilibili import BilibiliError
from fatfish.config import load_config
from fatfish.context import TOOL_SCHEMA, ensure_default_persona
from fatfish.db import Database
from fatfish.runtime import ALLOWED_ACTIONS, Runtime, TurnAction, parse_turn_plan
from fatfish.security import PolicyEngine, TrustLevel
from fatfish.security.policy import ACTION_SPECS

NOW = 1_772_000_000.0
SESSION = "qq:private:1001"
SCHEMA_KEYS = ("make_photo", "search_videos")
# Anything the photo layer would touch if it tried to free the GPU itself.
FORBIDDEN_PHOTO_ATTRS = ("unload", "vram", "tts", "gpu", "release")


def make_runtime(tmp: Path) -> tuple[object, Database, Runtime]:
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        'timezone = "Asia/Shanghai"',
        f'[storage]\ndb_path = "{(data / "self_action_generate.db").as_posix()}"',
        "[memory]",
        'embedding_backend = "hash"',
        "embedding_dim = 64",
        # T-0042 L4 added the desire gate in front of these actions; this file
        # pins "always willing" so it keeps testing the branches themselves.
        "[self_action]",
        "enabled = true",
        "initial_threshold = 0.0",
        "threshold_floor = 0.0",
        "threshold_step = 0.01",
        "daily_cap = 100",
        "max_events_per_turn = 10",
        "quiet_hours = []",
        # 2026-11-18: spontaneous media gate (all three turn sources).
        "[proactive_media]",
        "photo_enabled = true",
        "photo_max_per_day = 6",
        "photo_min_interval_minutes = 45",
        "sticker_enabled = true",
        "sticker_max_per_day = 12",
        "sticker_min_interval_minutes = 20",
        "quiet_hours = []",
        # search_videos now refuses before any request when Bilibili is off;
        # this file exercises the search path, so it has to be on here.
        "[bilibili]",
        "enabled = true",
    ]
    path = tmp / "self_action_generate.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, Runtime(db, config)


class FakePhoto:
    """Duck-typed ``PhotoService``: one ``generate`` call, nothing else.

    ``__getattr__`` is a tripwire: if the action branch ever tries to unload
    TTS, read VRAM or grab the GPU lease by hand, the test fails loudly
    instead of silently succeeding on a mock.
    """

    def __init__(
        self,
        *,
        ok: bool = True,
        path: str | None = "data/photos/test.png",
        caption: str = "一张测试图",
        error: str | None = None,
        raises: Exception | None = None,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.ok = ok
        self.path = path
        self.caption = caption
        self.error = error
        self.raises = raises
        self.calls: list[dict] = []

    def generate(self, text, *, context=None, session_id=None, now=None):
        self.calls.append(
            {"text": text, "context": context, "session_id": session_id, "now": now}
        )
        if self.raises is not None:
            raise self.raises
        return {
            "ok": self.ok,
            "path": self.path,
            "caption": self.caption,
            "error": self.error,
        }

    def __getattr__(self, name: str):
        lowered = name.lower()
        if any(part in lowered for part in FORBIDDEN_PHOTO_ATTRS):
            raise AssertionError(f"make_photo must not touch photo.{name}")
        raise AttributeError(name)


class FakeBilibili:
    """Duck-typed ``BilibiliCollector.browse_once``."""

    def __init__(self, cards=None, *, raises: Exception | None = None):
        self.cards = list(cards or [])
        self.raises = raises
        self.calls: list[dict] = []

    def browse_once(self, limit=5, keyword=None, client=None):
        self.calls.append({"limit": limit, "keyword": keyword})
        if self.raises is not None:
            raise self.raises
        return list(self.cards)[:limit]


def make_card(title: str, *, up: str = "", url: str = ""):
    return SimpleNamespace(title=title, up=up, url=url)


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_runtime(self.tmp)
        self.photo = FakePhoto()
        self.bilibili = FakeBilibili()
        self.runtime.photo = self.photo
        self.runtime.bilibili = self.bilibili

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def run_action(self, tool: str, args: dict, *, trust: TrustLevel = TrustLevel.PRIVATE):
        results = self.runtime.execute_actions(
            [TurnAction(tool, args)],
            session_id=SESSION,
            reply="",
            now=NOW,
            trust=trust,
        )
        self.assertEqual(len(results), 1)
        return results[0]

    def evidence_rows(self):
        return self.db.query("SELECT * FROM self_action_evidence ORDER BY id")


# --------------------------------------------------------------------- policy
class PolicyMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyEngine()

    def test_private_allows_both(self) -> None:
        for tool, args in (
            ("make_photo", {"prompt": "海边的鲸鱼"}),
            ("search_videos", {"topic": "海洋馆"}),
        ):
            with self.subTest(tool=tool):
                decision = self.policy.validate(tool, args, trust=TrustLevel.PRIVATE)
                self.assertTrue(decision.allowed, decision.reason)

    def test_group_denied_for_both(self) -> None:
        for tool, args in (
            ("make_photo", {"prompt": "海边的鲸鱼"}),
            ("search_videos", {"topic": "海洋馆"}),
        ):
            with self.subTest(tool=tool):
                decision = self.policy.validate(tool, args, trust=TrustLevel.GROUP)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "group_not_allowed")

    def test_max_calls_enforced(self) -> None:
        for tool, args in (
            ("make_photo", {"prompt": "海边的鲸鱼"}),
            ("search_videos", {"topic": "海洋馆"}),
        ):
            with self.subTest(tool=tool):
                decision = self.policy.validate(
                    tool, args, trust=TrustLevel.PRIVATE, count=1
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "too_many_tool_calls")

    def test_missing_text_denied(self) -> None:
        for tool, args, reason in (
            ("make_photo", {}, "missing_prompt"),
            ("make_photo", {"prompt": "   "}, "missing_prompt"),
            ("make_photo", {"prompt": None}, "missing_prompt"),
            ("search_videos", {}, "missing_topic"),
            ("search_videos", {"topic": "  "}, "missing_topic"),
        ):
            with self.subTest(tool=tool, args=args):
                decision = self.policy.validate(tool, args, trust=TrustLevel.PRIVATE)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, reason)

    def test_specs_are_private_and_one_per_turn(self) -> None:
        for tool in SCHEMA_KEYS:
            with self.subTest(tool=tool):
                spec = ACTION_SPECS[tool]
                self.assertFalse(spec.allow_group)
                self.assertEqual(spec.max_calls, 1)
                self.assertEqual(spec.min_trust, TrustLevel.PRIVATE)


class RegistrationTests(unittest.TestCase):
    def test_allowed_actions_contains_both(self) -> None:
        for tool in SCHEMA_KEYS:
            self.assertIn(tool, ALLOWED_ACTIONS)

    def test_tool_schema_documents_both(self) -> None:
        for tool in SCHEMA_KEYS:
            with self.subTest(tool=tool):
                self.assertIn(f"- {tool}:", TOOL_SCHEMA)
        self.assertIn('"prompt"', TOOL_SCHEMA)
        self.assertIn('"topic"', TOOL_SCHEMA)
        self.assertIn("desire 低于当前阈值时这个动作不会执行", TOOL_SCHEMA)

    def test_parse_turn_plan_keeps_the_new_actions(self) -> None:
        raw = json.dumps(
            {
                "messages": ["嗯"],
                "actions": [
                    {"tool": "make_photo", "args": {"prompt": "鲸鱼"}},
                    {"tool": "search_videos", "args": {"topic": "海洋馆", "limit": 3}},
                ],
            },
            ensure_ascii=False,
        )
        plan = parse_turn_plan(raw)
        self.assertEqual([a.tool for a in plan.actions], list(SCHEMA_KEYS))


# --------------------------------------------------- proactive media routing
class FakePendingPhoto(FakePhoto):
    """Async ModelScope-shaped result: the job is accepted, not finished."""

    def generate(self, text, *, context=None, session_id=None, now=None):
        payload = super().generate(
            text, context=context, session_id=session_id, now=now
        )
        payload["pending"] = True
        return payload


class ProactiveMediaRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.runtime = make_runtime(self.tmp)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.db.close)
        self.runtime.owner.bind(SESSION, source="test")
        self.photo = FakePendingPhoto()
        self.runtime.photo = self.photo

    def _photo_action(self, *, session_id: str = "activity-20261118-1000"):
        return self.runtime.execute_actions(
            [TurnAction("make_photo", {"prompt": "午后的窗边"})],
            session_id=session_id,
            reply="",
            now=NOW,
            proactive_media=True,
        )

    def test_activity_photo_is_retargeted_to_owner_and_logged(self) -> None:
        result = self._photo_action()
        self.assertTrue(result[0]["ok"], result)
        self.assertTrue(result[0]["pending"])
        self.assertEqual(self.photo.calls[0]["session_id"], SESSION)
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM decision_log "
            "WHERE kind='proactive_photo' AND action='queued' AND outcome='ok'"
        )
        self.assertEqual(int(row["n"]), 1)

    def test_disabled_gate_blocks_before_generation(self) -> None:
        self.config._data["proactive_media"]["photo_enabled"] = False
        result = self._photo_action()
        self.assertFalse(result[0]["ok"])
        self.assertEqual(result[0]["error"], "proactive_media_gated")
        self.assertEqual(self.photo.calls, [])

    def test_min_interval_blocks_the_second_photo(self) -> None:
        first = self._photo_action()
        self.assertTrue(first[0]["ok"])
        second = self._photo_action()
        self.assertFalse(second[0]["ok"])
        self.assertEqual(second[0]["error"], "proactive_media_gated")
        self.assertEqual(len(self.photo.calls), 1)

    def test_legacy_sync_sticker_is_queued_as_image(self) -> None:
        # Cloud may still run the pre-async sticker service (send + file_path).
        # The runtime must bridge it into qq_send_image instead of assuming
        # decision.pending exists.
        class LegacyDecision:
            send = True
            reason = "library_match"
            score = 0.9
            emotion = 3.0
            sticker = SimpleNamespace(
                id="legacy-sticker", file_path="stickers/happy.png", caption="开心"
            )

        class LegacySticker:
            def decide(self, *args, **kwargs):
                return LegacyDecision()

            def record_usage(self, *args, **kwargs):
                return None

        self.runtime.sticker = LegacySticker()
        results = self.runtime.execute_actions(
            [TurnAction("send_sticker", {"need": "开心 转圈"})],
            session_id="activity-20261118-1000",
            reply="",
            now=NOW,
            proactive_media=True,
        )
        self.assertTrue(results[0]["ok"], results)
        row = self.db.query_one(
            "SELECT payload_json FROM outbox WHERE kind='qq_send_image' "
            "ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(row)
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["session_id"], SESSION)
        self.assertEqual(payload["path"], "stickers/happy.png")
        self.assertEqual(payload["caption"], "开心")

    def test_owner_context_is_readable_without_touching_prefix(self) -> None:
        self.runtime.memory.add_turn(SESSION, "user", "刚才聊到光刻机")
        block = self.runtime._owner_context_for_activity(now=NOW)
        self.assertIn("光刻机", block)
        self.assertIn("只读", block)


# ----------------------------------------------------------------- make_photo
class MakePhotoTests(RuntimeCase):
    def test_success_stores_evidence(self) -> None:
        result = self.run_action("make_photo", {"prompt": "海边的鲸鱼"})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["path"], "data/photos/test.png")
        self.assertEqual(result["result"]["caption"], "一张测试图")
        self.assertEqual(len(self.photo.calls), 1)
        self.assertEqual(self.photo.calls[0]["text"], "海边的鲸鱼")
        self.assertEqual(self.photo.calls[0]["session_id"], SESSION)
        self.assertEqual(self.photo.calls[0]["now"], NOW)

        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], result["result"]["evidence_id"])
        self.assertEqual(row["action"], "make_photo")
        self.assertEqual(row["source"], "photo")
        self.assertEqual(row["title"], "海边的鲸鱼")
        self.assertEqual(row["session_id"], SESSION)
        self.assertIn("我自己画了一张", row["content"])
        self.assertIn("一张测试图", row["content"])
        self.assertIn("data/photos/test.png", row["content"])
        self.assertNotIn("<untrusted_content", row["content"])

    def test_never_touches_tts_or_vram(self) -> None:
        # ``FakePhoto.__getattr__`` already trips on unload/tts/vram/gpu names;
        # this pins the second half: no lock file is taken behind her back.
        self.run_action("make_photo", {"prompt": "海边的鲸鱼"})
        self.assertFalse((Path(self.config.data_dir) / "run" / "vram.lock").exists())

    def test_payload_failure_still_stores_evidence(self) -> None:
        self.photo = FakePhoto(ok=False, path=None, caption="", error="comfyui_timeout")
        self.runtime.photo = self.photo
        result = self.run_action("make_photo", {"prompt": "海边的鲸鱼"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "comfyui_timeout")
        note = result["result"]["note"]
        self.assertIn("拍照超时", note)

        # Failure evidence is the point of this layer: next turn she must not
        # pretend she drew something.
        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "photo_failure")
        self.assertEqual(rows[0]["title"], "海边的鲸鱼")
        self.assertEqual(rows[0]["content"], note)
        self.assertEqual(rows[0]["id"], result["result"]["evidence_id"])

    def test_generate_exception_is_contained(self) -> None:
        self.photo = FakePhoto(raises=RuntimeError("comfyui died"))
        self.runtime.photo = self.photo
        result = self.run_action("make_photo", {"prompt": "海边的鲸鱼"})

        # ``_take_photo`` catches the exception itself and reports it in the
        # payload, so this surfaces as a photo error, not action_failed.
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "RuntimeError:comfyui died")
        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "photo_failure")

    def test_unexpected_error_after_photo_is_action_failed(self) -> None:
        with mock.patch.object(self.runtime, "_take_photo", side_effect=RuntimeError("boom")):
            result = self.run_action("make_photo", {"prompt": "海边的鲸鱼"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "action_failed:RuntimeError")
        self.assertEqual(self.evidence_rows(), [])

    def test_disabled_service_is_a_refusal_with_evidence(self) -> None:
        self.photo = FakePhoto(enabled=False)
        self.runtime.photo = self.photo
        result = self.run_action("make_photo", {"prompt": "海边的鲸鱼"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "disabled")
        self.assertEqual(self.photo.calls, [])
        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "photo_failure")
        self.assertIn("还没开", rows[0]["content"])

    def test_missing_prompt_is_a_refusal_not_an_exception(self) -> None:
        result = self.run_action("make_photo", {"prompt": "   "})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_prompt")
        self.assertEqual(self.photo.calls, [])
        self.assertEqual(self.evidence_rows(), [])

    def test_prompt_is_capped_at_300_chars(self) -> None:
        self.run_action("make_photo", {"prompt": "鲸" * 500})
        self.assertEqual(len(self.photo.calls[0]["text"]), 300)

    def test_group_session_is_refused_before_generating(self) -> None:
        result = self.run_action(
            "make_photo", {"prompt": "海边的鲸鱼"}, trust=TrustLevel.GROUP
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "group_not_allowed")
        self.assertEqual(self.photo.calls, [])
        self.assertEqual(self.evidence_rows(), [])

    def test_desire_is_not_read_here(self) -> None:
        # L4 owns the gate: the branch must run the same way with or without it.
        result = self.run_action("make_photo", {"prompt": "海边的鲸鱼", "desire": 0.0})
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.photo.calls), 1)


# --------------------------------------------------------------- search_videos
class SearchVideosTests(RuntimeCase):
    def test_success_stores_evidence(self) -> None:
        self.bilibili.cards = [
            make_card("深海鲸歌", up="海洋研究所", url="https://b23.tv/BV1aa"),
            make_card("水母的一天", up="", url="https://b23.tv/BV1bb"),
        ]
        result = self.run_action("search_videos", {"topic": "海洋", "limit": 3})

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["found"], 2)
        self.assertEqual(self.bilibili.calls, [{"limit": 3, "keyword": "海洋"}])

        rows = self.evidence_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], result["result"]["evidence_id"])
        self.assertEqual(row["action"], "search_videos")
        self.assertEqual(row["source"], "bilibili")
        self.assertEqual(row["title"], "海洋")
        self.assertIn("- 深海鲸歌（海洋研究所） https://b23.tv/BV1aa", row["content"])
        # No empty brackets when the uploader is unknown.
        self.assertIn("- 水母的一天 https://b23.tv/BV1bb", row["content"])
        self.assertNotIn("（）", row["content"])
        self.assertNotIn("<untrusted_content", row["content"])

    def test_no_results_is_success(self) -> None:
        self.bilibili.cards = []
        result = self.run_action("search_videos", {"topic": "不存在的主题"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["found"], 0)
        self.assertEqual(
            self.evidence_rows()[0]["content"], "（没搜到这个主题的视频）"
        )

    def test_limit_is_clamped_to_five(self) -> None:
        self.bilibili.cards = [make_card(f"视频{i}") for i in range(8)]
        result = self.run_action("search_videos", {"topic": "海洋", "limit": 99})
        self.assertTrue(result["ok"])
        self.assertEqual(self.bilibili.calls[0]["limit"], 5)
        self.assertEqual(result["result"]["found"], 5)

    def test_zero_limit_falls_back_to_default(self) -> None:
        # ``int(args.get("limit") or 5)``: 0 is falsy, so it reads as "not given".
        self.run_action("search_videos", {"topic": "海洋", "limit": 0})
        self.assertEqual(self.bilibili.calls[0]["limit"], 5)

    def test_bad_limit_falls_back_to_default(self) -> None:
        self.run_action("search_videos", {"topic": "海洋", "limit": "很多"})
        self.assertEqual(self.bilibili.calls[0]["limit"], 5)

    def test_bilibili_error_does_not_escape(self) -> None:
        self.bilibili.raises = BilibiliError("cookie expired")
        result = self.run_action("search_videos", {"topic": "海洋"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bilibili_failed:BilibiliError")
        self.assertEqual(len(self.bilibili.calls), 1)  # one attempt, no retry
        self.assertEqual(self.evidence_rows(), [])

    def test_network_error_does_not_escape(self) -> None:
        self.bilibili.raises = TimeoutError("read timed out")
        result = self.run_action("search_videos", {"topic": "海洋"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bilibili_failed:TimeoutError")

    def test_missing_topic_is_a_refusal_not_an_exception(self) -> None:
        result = self.run_action("search_videos", {"topic": "  "})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_topic")
        self.assertEqual(self.bilibili.calls, [])
        self.assertEqual(self.evidence_rows(), [])

    def test_topic_is_capped_at_100_chars(self) -> None:
        self.run_action("search_videos", {"topic": "海" * 300})
        self.assertEqual(len(self.bilibili.calls[0]["keyword"]), 100)

    def test_group_session_is_refused_before_searching(self) -> None:
        result = self.run_action(
            "search_videos", {"topic": "海洋"}, trust=TrustLevel.GROUP
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "group_not_allowed")
        self.assertEqual(self.bilibili.calls, [])
        self.assertEqual(self.evidence_rows(), [])

    def test_found_count_survives_duplicate_skips(self) -> None:
        # ``browse_once`` returns only newly ingested cards; an all-duplicate
        # search legitimately reports 0 without being an error.
        self.bilibili.cards = []
        result = self.run_action("search_videos", {"topic": "看过的主题"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["found"], 0)


if __name__ == "__main__":
    unittest.main()
