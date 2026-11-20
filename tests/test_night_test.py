"""Offline tests for the night-test driver (T-0015).

Covers: profile file, session id format, owner binding insert/cleanup, memory
visibility for the night role, qzone friend evidence at profile level, token
accounting from turn_traces, ledger, brakes (stop file / window end /
consecutive failures / per-wake budget), --dry-run and --once, prepare/finalize
git snapshots against a temp repo, and the JSON-repair retry path. No network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fatfish.clock import from_local, to_local
from fatfish.config import load_config
from fatfish.context import ContextAssembler, ensure_default_persona
from fatfish.db import Database
from fatfish.llm import LLMResult
from fatfish.memory import MemoryService
from fatfish.owner import OwnerRegistry
from fatfish.photo.service import wants_photo
from fatfish.qzone import QzoneCollector
from fatfish.runtime import (
    QZONE_REVIEW_NEGATIVE,
    Runtime,
    _owes_sticker,
    _wants_sticker,
    wants_qzone_review,
)
from fatfish.security import PolicyEngine, TrustLevel, trust_for_session
from fatfish.trace import TraceRecorder
from fatfish.voice import VoiceDecision

from scripts.night_test import (  # noqa: E402
    DEFAULT_WAKE_TEXT,
    NightTestDriver,
    _apply_night_overrides,
    night_test_date,
    normalize_date,
)

PROFILE_PATH = ROOT / "config" / "night-test.toml"
OWNER_SESSION = "qq:private:10001"
FRIEND_SESSION = "qq:private:10002"
NIGHT_SESSION = "night-20261025-w01"


def ts(*args: int) -> float:
    """Timestamp for a local Asia/Shanghai wall-clock time."""
    return from_local(datetime(*args), "Asia/Shanghai")


def make_config(tmp: Path, *, ledger_name: str = "night-test-{date}.jsonl"):
    data_dir = (tmp / "data").as_posix()
    logs_dir = (tmp / "logs").as_posix()
    run_dir = (tmp / "run").as_posix()
    toml = f"""
[app]
data_dir = "{data_dir}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(tmp / "data" / "night.db").as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128

[llm]
enabled = true

[qzone]
enabled = true
read_only = false
require_approval = true
poll_interval_minutes = 30
friends_content_to_llm = true
owner_uin = "10000"
review_max_items = 10

[relationship]
owner_display_name = "主人哥哥"

[night_test]
interval_minutes = 2
jitter_minutes = 0
start = "03:00"
end = "08:00"
wake_token_budget = 1000000
bilibili_videos_per_wake = 1
review_items_per_wake = 10
session_prefix = "night"
ledger_path = "{logs_dir}/{ledger_name}"
stop_file = "{run_dir}/night-test.stop"
consecutive_failure_limit = 3
wake_text = "{DEFAULT_WAKE_TEXT}"
"""
    path = tmp / "night.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    return config


def make_db(config) -> Database:
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return db


class FakeQzone:
    def __init__(self, *, fail: bool = False, new: int = 2):
        self.calls = 0
        self.fail = fail
        self.new = new

    def fetch_and_ingest(self, client=None, *, manual: bool = False, now=None):
        self.calls += 1
        if not manual:
            raise AssertionError("driver must poll with manual=True")
        if self.fail:
            raise RuntimeError("qzone down")
        return {"ok": True, "fetched": 5, "new": self.new, "promoted": self.new, "errors": []}


class FakeBili:
    def __init__(self, *, fail: bool = False, cards: int = 1):
        self.calls = 0
        self.fail = fail
        self.cards = cards

    def browse_once(self, limit: int = 5, keyword=None, client=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("bili down")
        return [{"bvid": f"BV{i}"} for i in range(self.cards)]


class FakeRuntime:
    """Stand-in runtime that writes real turn_traces rows for accounting."""

    def __init__(self, db: Database, *, tokens: int = 0, fail: bool = False):
        self.db = db
        self.tokens = tokens
        self.fail = fail
        self.calls = 0
        self.last_session = ""
        self.last_text = ""

    def handle_user_message(self, session_id: str, text: str, **kwargs):
        self.calls += 1
        self.last_session = session_id
        self.last_text = text
        if self.fail:
            raise RuntimeError("llm exploded")
        TraceRecorder(self.db).record(
            session_id,
            scope="private",
            trust="private",
            user_text=text,
            prompt_tokens=self.tokens,
            completion_tokens=5,
            model="fake",
        )
        return {"actions": [], "security": {"trace_id": "fake-trace"}}


class FakeClock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def make_driver(
    config,
    db: Database,
    *,
    date: str = "2026-10-25",
    clock: FakeClock | None = None,
    qzone: FakeQzone | None = None,
    bili: FakeBili | None = None,
    runtime=None,
    out: list[str] | None = None,
    git_runner=None,
) -> tuple[NightTestDriver, FakeClock, list[str]]:
    # 默认时钟放在 2020 年：TraceRecorder 写的是真实 time.time()（2026 年），
    # 早于 since 才不会被 tokens_since 过滤掉；真实夜间运行两者都是真实时钟。
    clock = clock or FakeClock(ts(2020, 1, 1, 3, 0))
    lines = out if out is not None else []
    driver = NightTestDriver(
        config,
        date=date,
        db=db,
        qzone=qzone or FakeQzone(),
        bili=bili or FakeBili(),
        runtime=runtime,
        repo_root=ROOT,
        clock=clock,
        sleep=clock.advance,
        jitter=lambda: 0.0,
        git_runner=git_runner,
        out=lines.append,
    )
    return driver, clock, lines


def read_ledger(config, date: str = "2026-10-25") -> list[dict]:
    path = NightTestDriver(config, date=date).ledger_path_for(date)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class ProfileFileTests(unittest.TestCase):
    """The shipped config/night-test.toml must carry the approved values."""

    def setUp(self) -> None:
        self.assertTrue(PROFILE_PATH.exists(), PROFILE_PATH)
        with PROFILE_PATH.open("rb") as fh:
            self.data = tomllib.load(fh)

    def test_qzone_section(self) -> None:
        qzone = self.data["qzone"]
        self.assertTrue(qzone["enabled"])
        self.assertFalse(qzone["read_only"])
        self.assertTrue(qzone["require_approval"])
        self.assertEqual(qzone["poll_interval_minutes"], 30)
        self.assertTrue(qzone["friends_content_to_llm"])

    def test_night_test_section(self) -> None:
        night = self.data["night_test"]
        self.assertEqual(night["interval_minutes"], 30)
        self.assertEqual(night["jitter_minutes"], 3)
        self.assertEqual(night["start"], "03:00")
        self.assertEqual(night["end"], "08:00")
        self.assertEqual(night["wake_token_budget"], 1000000)
        self.assertEqual(night["bilibili_videos_per_wake"], 1)
        self.assertEqual(night["review_items_per_wake"], 10)
        self.assertEqual(night["session_prefix"], "night")
        self.assertEqual(night["consecutive_failure_limit"], 3)
        self.assertIn("{date}", night["ledger_path"])
        self.assertTrue(night["stop_file"].endswith(".stop"))

    def test_wake_text_hits_bypass_and_avoids_senders(self) -> None:
        text = self.data["night_test"]["wake_text"]
        self.assertTrue(wants_qzone_review(text), text)
        self.assertFalse(_wants_sticker(text))
        self.assertFalse(_owes_sticker(text))
        self.assertFalse(wants_photo(text))
        self.assertFalse(any(word in text for word in QZONE_REVIEW_NEGATIVE))

    def test_review_override_applied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            config = make_config(tmp)
            self.assertEqual(_apply_night_overrides(config).get("qzone.review_max_items"), 10)
            config._data["night_test"]["review_items_per_wake"] = 7
            self.assertEqual(_apply_night_overrides(config).get("qzone.review_max_items"), 7)


class NightDateTests(unittest.TestCase):
    def test_date_rule_lands_on_window_end_morning(self) -> None:
        # 窗口 03:00-08:00：22:30 预备 → 次日晨；窗口内 → 当日晨；08:30 收尾 → 当日。
        self.assertEqual(night_test_date(ts(2026, 10, 24, 22, 30)), "2026-10-25")
        self.assertEqual(night_test_date(ts(2026, 10, 25, 3, 30)), "2026-10-25")
        self.assertEqual(night_test_date(ts(2026, 10, 25, 8, 30)), "2026-10-25")
        # 傍晚提前准备 → 即将到来的那一夜的结束晨。
        self.assertEqual(night_test_date(ts(2026, 10, 25, 20, 0)), "2026-10-26")

    def test_date_override(self) -> None:
        self.assertEqual(normalize_date("20261025"), "2026-10-25")
        self.assertEqual(normalize_date("2026-10-25"), "2026-10-25")
        self.assertIsNone(normalize_date(None))
        with self.assertRaises(ValueError):
            normalize_date("25-10-2026")


class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.db = make_db(self.config)
        self.driver, _, _ = make_driver(self.config, self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_in_window(self) -> None:
        self.assertTrue(self.driver._in_window(ts(2026, 10, 25, 3, 0)))
        self.assertTrue(self.driver._in_window(ts(2026, 10, 25, 7, 59)))
        self.assertFalse(self.driver._in_window(ts(2026, 10, 25, 2, 59)))
        self.assertFalse(self.driver._in_window(ts(2026, 10, 25, 8, 0)))
        self.assertFalse(self.driver._in_window(ts(2026, 10, 25, 12, 0)))

    def test_window_end_moment(self) -> None:
        end = to_local(self.driver._window_end_moment(ts(2026, 10, 24, 22, 40)), "Asia/Shanghai")
        self.assertEqual((end.day, end.hour), (25, 8))
        end2 = to_local(self.driver._window_end_moment(ts(2026, 10, 25, 5, 0)), "Asia/Shanghai")
        self.assertEqual((end2.day, end2.hour), (25, 8))

    def test_next_start_moment(self) -> None:
        nxt = to_local(self.driver._next_start_moment(ts(2026, 10, 24, 22, 40)), "Asia/Shanghai")
        self.assertEqual((nxt.day, nxt.hour), (25, 3))
        # 窗口已开始后的下一次开始是明天。
        nxt2 = to_local(self.driver._next_start_moment(ts(2026, 10, 25, 5, 0)), "Asia/Shanghai")
        self.assertEqual((nxt2.day, nxt2.hour), (26, 3))


class SessionIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.db = make_db(self.config)
        owner = OwnerRegistry(self.db, self.config)
        owner.bind(OWNER_SESSION, display_name="主人哥哥")
        owner.bind_friend(FRIEND_SESSION, display_name="姐姐")
        self.driver, _, _ = make_driver(self.config, self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_session_id_format(self) -> None:
        self.assertEqual(self.driver.session_id_for(1, "2026-10-25"), "night-20261025-w01")
        self.assertEqual(self.driver.session_id_for(10, "2026-10-25"), "night-20261025-w10")
        self.assertEqual(self.driver.session_id_for(11, "2026-10-25"), "night-20261025-w11")

    def test_trust_is_private_for_night_sessions(self) -> None:
        self.assertEqual(trust_for_session(NIGHT_SESSION), TrustLevel.PRIVATE)
        self.assertEqual(PolicyEngine().trust_for(NIGHT_SESSION), TrustLevel.PRIVATE)

    def test_binding_inserts_without_touching_existing_rows(self) -> None:
        before = {
            row["session_id"]: dict(row)
            for row in self.db.query("SELECT * FROM owner_bindings")
        }
        self.assertEqual(sorted(before), sorted([OWNER_SESSION, FRIEND_SESSION]))

        role = self.driver.ensure_session_binding(NIGHT_SESSION)
        self.assertEqual(role, "owner")

        rows = {
            row["session_id"]: dict(row)
            for row in self.db.query("SELECT * FROM owner_bindings")
        }
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[NIGHT_SESSION]["role"], "owner")
        self.assertEqual(rows[NIGHT_SESSION]["source"], "night-test")
        self.assertEqual(rows[NIGHT_SESSION]["display_name"], "主人哥哥")
        # 既有两行一个字段都不能变。
        for sid in (OWNER_SESSION, FRIEND_SESSION):
            self.assertEqual(rows[sid], before[sid])
        self.assertEqual(OwnerRegistry(self.db, self.config).role_of(NIGHT_SESSION), "owner")

    def test_binding_idempotent(self) -> None:
        self.driver.ensure_session_binding(NIGHT_SESSION)
        self.driver.ensure_session_binding(NIGHT_SESSION)
        self.assertEqual(
            self.db.query_one("SELECT COUNT(*) AS n FROM owner_bindings")["n"], 3
        )

    def test_remove_bindings_keeps_existing_rows(self) -> None:
        self.driver.ensure_session_binding(NIGHT_SESSION)
        removed = self.driver.remove_session_bindings()
        self.assertEqual(removed, 1)
        rows = {row["session_id"] for row in self.db.query("SELECT * FROM owner_bindings")}
        self.assertEqual(rows, {OWNER_SESSION, FRIEND_SESSION})
        self.assertIsNone(OwnerRegistry(self.db, self.config).role_of(NIGHT_SESSION))


class MemoryVisibilityTests(unittest.TestCase):
    """核实点 1：夜间会话必须拿得到能看记忆的角色。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.db = make_db(self.config)
        self.memory = MemoryService(self.db, self.config)
        self.memory.add_item(
            "主人哥哥书架第三层摆着一只紫水晶企鹅。",
            type="episodic",
            importance=0.9,
            tags=["书架"],
        )
        self.assembler = ContextAssembler(self.db, self.config, self.memory)
        self.driver, _, _ = make_driver(self.config, self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _bundle_text(self) -> str:
        role = OwnerRegistry(self.db, self.config).role_of(NIGHT_SESSION)
        bundle = self.assembler.build(NIGHT_SESSION, "在吗", role=role, now=ts(2026, 10, 25, 4, 0))
        return bundle.text()

    def test_without_binding_memory_is_hidden(self) -> None:
        self.assertNotIn("紫水晶企鹅", self._bundle_text())

    def test_with_night_binding_memory_visible(self) -> None:
        self.driver.ensure_session_binding(NIGHT_SESSION)
        text = self._bundle_text()
        self.assertIn("紫水晶企鹅", text)
        self.assertIn("短期记忆摘要", text)


class FriendEvidenceProfileTests(unittest.TestCase):
    """核实点 2：night-test.toml（friends_content_to_llm=true）下旁路证据含好友动态。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _night_profile_config(self):
        """真实 night-test.toml + 临时路径覆盖（绝不指向真实 data/）。"""
        with PROFILE_PATH.open("rb") as fh:
            merged = tomllib.load(fh)
        merged.setdefault("app", {})["data_dir"] = (self.tmp / "data").as_posix()
        merged.setdefault("storage", {})["db_path"] = (
            self.tmp / "data" / "profile.db"
        ).as_posix()
        merged.setdefault("memory", {}).update(
            {"embedding_backend": "hash", "embedding_dim": 128}
        )
        night = merged.setdefault("night_test", {})
        night["ledger_path"] = (self.tmp / "logs" / "nt-{date}.jsonl").as_posix()
        night["stop_file"] = (self.tmp / "run" / "nt.stop").as_posix()
        path = self.tmp / "profile-override.toml"
        path.write_text(_toml_dumps(merged), encoding="utf-8")
        config = load_config("low", path)
        config.ensure_dirs()
        return config

    def test_profile_flags_loaded(self) -> None:
        config = self._night_profile_config()
        self.assertTrue(config.get("qzone.friends_content_to_llm"))
        self.assertTrue(config.get("qzone.enabled"))
        self.assertFalse(config.get("qzone.read_only"))
        self.assertTrue(config.get("qzone.require_approval"))
        self.assertEqual(config.get("night_test.start"), "03:00")
        self.assertEqual(config.get("night_test.end"), "08:00")

    def test_bypass_prompt_contains_friend_dynamics(self) -> None:
        config = self._night_profile_config()
        db = make_db(config)
        try:
            memory = MemoryService(db, config)
            collector = QzoneCollector(memory, config)
            stored = collector.store_items(
                [
                    {
                        "external_id": "own1",
                        "author_uin": "10000",
                        "author_name": "主人",
                        "content": "主人今天去爬山了",
                        "media": [],
                        "published_at": ts(2026, 10, 25, 3, 10),
                        "url": "",
                        "raw": {},
                    },
                    {
                        "external_id": "fr1",
                        "author_uin": "20001",
                        "author_name": "好友甲",
                        "content": "好友甲在吃火锅",
                        "media": [],
                        "published_at": ts(2026, 10, 25, 3, 20),
                        "url": "",
                        "raw": {},
                    },
                ]
            )
            collector.ingest_into_memory(stored)

            runtime = Runtime(db, config)
            prompts: list[str] = []
            runtime.llm.api_key = "fake"

            def chat(messages, *args, **kwargs):
                prompts.append("\n".join(str(m.get("content", "")) for m in messages))
                return LLMResult(text='{"messages":["看到了哦。"]}', model="fake-model")

            runtime.llm.chat = chat
            runtime.voice.decide = lambda *a, **k: VoiceDecision("text_only", 0.1, ["test"])

            driver, _, _ = make_driver(config, db, runtime=runtime, date="2026-10-25")
            driver.run_wake(wake_no=1, date_iso="2026-10-25", dry_run=False)
            self.assertTrue(prompts)
            self.assertIn("qzone_feed", prompts[0])
            self.assertIn("主人今天去爬山了", prompts[0])
            # friends_content_to_llm=true：好友动态原文必须进提示词（wrap_untrusted 包裹）。
            self.assertIn("好友甲", prompts[0])
            self.assertIn("吃火锅", prompts[0])
        finally:
            db.close()


class TokenAccountingTests(unittest.TestCase):
    """核实点 3：每轮 token 从 turn_traces 记账并可超限判定。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.db = make_db(self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_tokens_since_sums_prompt_and_completion(self) -> None:
        recorder = TraceRecorder(self.db)
        recorder.record(
            NIGHT_SESSION, scope="private", trust="private", user_text="a",
            prompt_tokens=900, completion_tokens=100,
        )
        recorder.record(
            NIGHT_SESSION, scope="private", trust="private", user_text="b",
            prompt_tokens=1, completion_tokens=2,
        )
        recorder.record(
            "other-session", scope="private", trust="private", user_text="c",
            prompt_tokens=5000, completion_tokens=5000,
        )
        driver, _, _ = make_driver(self.config, self.db)
        usage = driver.tokens_since(NIGHT_SESSION, since=0.0)
        self.assertEqual(usage["tokens"], 1003)
        self.assertEqual(usage["turns"], 2)

    def test_over_budget_flagged_and_skipped_in_dry_run(self) -> None:
        runtime = FakeRuntime(self.db, tokens=2_000_000)
        driver, _, _ = make_driver(self.config, self.db, runtime=runtime)
        row = driver.run_wake(wake_no=1, date_iso="2026-10-25", dry_run=False)
        self.assertTrue(row["over_budget"])
        self.assertEqual(row["tokens"], 2_000_005)
        dry = driver.run_wake(wake_no=2, date_iso="2026-10-25", dry_run=True)
        self.assertFalse(dry["over_budget"])
        self.assertEqual(dry["tokens"], 0)


class DriverLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.db = make_db(self.config)
        OwnerRegistry(self.db, self.config).bind(OWNER_SESSION)
        OwnerRegistry(self.db, self.config).bind_friend(FRIEND_SESSION)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_dry_run_once_skips_llm_turn(self) -> None:
        qzone, bili = FakeQzone(new=3), FakeBili(cards=1)
        runtime = FakeRuntime(self.db)
        driver, _, _ = make_driver(
            self.config, self.db, qzone=qzone, bili=bili, runtime=runtime
        )
        result = driver.run(dry_run=True, once=True)
        self.assertEqual(result["stop_reason"], "once")
        self.assertEqual(result["wakes"], 1)
        self.assertEqual(runtime.calls, 0)
        self.assertEqual(qzone.calls, 1)
        self.assertEqual(bili.calls, 1)
        rows = read_ledger(self.config)
        wakes = [row for row in rows if row.get("event") == "wake"]
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes[0]["mode"], "dry-run")
        self.assertEqual(wakes[0]["qzone_new"], 3)
        self.assertEqual(wakes[0]["bili_new"], 1)
        self.assertEqual(wakes[0]["tokens"], 0)
        self.assertEqual(wakes[0]["session"], "night-20261025-w01")
        self.assertEqual(rows[-1].get("event"), "stop")
        # dry-run 不建会话绑定。
        self.assertEqual(
            self.db.query_one("SELECT COUNT(*) AS n FROM owner_bindings")["n"], 2
        )

    def test_live_once_writes_ledger_and_cleans_binding(self) -> None:
        runtime = FakeRuntime(self.db, tokens=150)
        driver, _, _ = make_driver(self.config, self.db, runtime=runtime)
        result = driver.run(dry_run=False, once=True)
        self.assertEqual(result["wakes"], 1)
        self.assertEqual(runtime.last_session, "night-20261025-w01")
        self.assertIn("空间里", runtime.last_text)
        rows = read_ledger(self.config)
        wakes = [row for row in rows if row.get("event") == "wake"]
        self.assertEqual(wakes[0]["tokens"], 155)
        self.assertEqual(wakes[0]["trace_turns"], 1)
        self.assertFalse(wakes[0]["over_budget"])
        self.assertEqual(wakes[0]["mode"], "live")
        # run 结束后夜间绑定被清理，原有绑定不动。
        bindings = {
            row["session_id"] for row in self.db.query("SELECT * FROM owner_bindings")
        }
        self.assertEqual(bindings, {OWNER_SESSION, FRIEND_SESSION})

    def test_once_does_not_wait_for_window(self) -> None:
        # 22:40 的 pre-flight 冒烟必须立刻跑，不能睡到 03:00。
        runtime = FakeRuntime(self.db, tokens=10)
        clock = FakeClock(ts(2026, 10, 24, 22, 40))
        driver, _, _ = make_driver(
            self.config, self.db, clock=clock, runtime=runtime, date="2026-10-25"
        )
        result = driver.run(dry_run=True, once=True)
        self.assertEqual(result["wakes"], 1)
        self.assertLess(clock.now - ts(2026, 10, 24, 22, 40), 60)

    def test_wake_numbering_resumes_from_ledger(self) -> None:
        driver, _, _ = make_driver(self.config, self.db)
        driver.run(once=True)
        driver.run(once=True)
        rows = read_ledger(self.config)
        wakes = [row["wake"] for row in rows if row.get("event") == "wake"]
        self.assertEqual(wakes, [1, 2])
        # 预置一个“第 5 轮”的旧台账，续号从 6 开始。
        path = driver.ledger_path_for("2026-10-25")
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text(
            "\n".join(lines + [json.dumps({"event": "wake", "wake": 5})]) + "\n",
            encoding="utf-8",
        )
        driver.run(once=True)
        rows = read_ledger(self.config)
        wakes = [row["wake"] for row in rows if row.get("event") == "wake"]
        self.assertEqual(wakes[-1], 6)

    def test_stop_file_brakes_before_any_wake(self) -> None:
        driver, _, _ = make_driver(self.config, self.db)
        driver.stop_file.parent.mkdir(parents=True, exist_ok=True)
        driver.stop_file.write_text("stop", encoding="utf-8")
        result = driver.run(dry_run=True, once=False)
        self.assertEqual(result["stop_reason"], "stop_file")
        self.assertEqual(result["wakes"], 0)
        stops = [row for row in read_ledger(self.config) if row.get("event") == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]["reason"], "stop_file")

    def test_window_end_brakes_after_wakes(self) -> None:
        # 07:50 进窗，2 分钟一轮：应该唤醒几轮后在 08:00 出窗硬停。
        clock = FakeClock(ts(2026, 10, 25, 7, 50))
        runtime = FakeRuntime(self.db, tokens=5)
        driver, _, _ = make_driver(self.config, self.db, clock=clock, runtime=runtime)
        result = driver.run(dry_run=False, once=False)
        self.assertEqual(result["stop_reason"], "window_end")
        self.assertGreaterEqual(result["wakes"], 2)
        rows = [row for row in read_ledger(self.config) if row.get("event") == "wake"]
        for row in rows:
            self.assertLess(row["ts"], ts(2026, 10, 25, 8, 0) + 1)

    def test_consecutive_failures_brake(self) -> None:
        qzone, bili = FakeQzone(fail=True), FakeBili(fail=True)
        runtime = FakeRuntime(self.db, fail=True)
        driver, _, _ = make_driver(
            self.config, self.db, qzone=qzone, bili=bili, runtime=runtime
        )
        result = driver.run(dry_run=False, once=False)
        self.assertEqual(result["stop_reason"], "consecutive_failures")
        self.assertEqual(result["wakes"], 3)
        rows = [row for row in read_ledger(self.config) if row.get("event") == "wake"]
        self.assertTrue(all(row["failed"] for row in rows))
        for row in rows:
            self.assertTrue(any(e.startswith("qzone:") for e in row["errors"]))
            self.assertTrue(any(e.startswith("bilibili:") for e in row["errors"]))
            self.assertTrue(any(e.startswith("turn:") for e in row["errors"]))

    def test_partial_failure_is_not_hard_failure(self) -> None:
        # qzone 挂、bili 正常、轮次正常：不算一轮失败，循环继续到窗口结束。
        clock = FakeClock(ts(2026, 10, 25, 7, 40))
        driver, _, _ = make_driver(
            self.config, self.db, qzone=FakeQzone(fail=True), bili=FakeBili(cards=1),
            runtime=FakeRuntime(self.db, tokens=5), clock=clock,
        )
        result = driver.run(dry_run=False, once=False)
        self.assertEqual(result["stop_reason"], "window_end")
        rows = [row for row in read_ledger(self.config) if row.get("event") == "wake"]
        self.assertTrue(all(not row["failed"] for row in rows))
        self.assertTrue(
            all(any(e.startswith("qzone:") for e in row["errors"]) for row in rows)
        )

    def test_waits_for_window_start_when_launched_early(self) -> None:
        clock = FakeClock(ts(2026, 10, 24, 22, 40))
        driver, _, _ = make_driver(self.config, self.db, clock=clock, date="2026-10-25")
        original = clock.advance

        def advance_and_stop(seconds: float) -> None:
            original(seconds)
            if clock.now > ts(2026, 10, 24, 23, 0):
                driver.stop_file.parent.mkdir(parents=True, exist_ok=True)
                driver.stop_file.write_text("stop", encoding="utf-8")

        driver._sleep = advance_and_stop
        result = driver.run(dry_run=True, once=False)
        self.assertEqual(result["stop_reason"], "stop_file")
        self.assertEqual(result["wakes"], 0)
        wakes = [row for row in read_ledger(self.config) if row.get("event") == "wake"]
        self.assertEqual(wakes, [])


class SummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.db = make_db(self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_summary_reads_ledger_and_db(self) -> None:
        runtime = FakeRuntime(self.db, tokens=100)
        clock = FakeClock(ts(2020, 1, 1, 3, 0))
        driver, _, lines = make_driver(
            self.config, self.db, runtime=runtime, clock=clock, date="2026-10-25"
        )
        driver.run(once=True)
        # 台账里的 wake ts 是 FakeClock 的时刻；草稿与记忆都落在同一时刻才落窗。
        wake_ts = ts(2020, 1, 1, 3, 0)
        self.db.execute(
            "INSERT INTO qzone_posts(content, media_json, status, created_by, session_id, "
            "idempotency_key, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "主人哥哥早安，昨晚看到好友甲在吃火锅。",
                "[]",
                "draft",
                "llm",
                NIGHT_SESSION,
                "k1",
                wake_ts,
            ),
        )
        MemoryService(self.db, self.config).add_item(
            "夜里刷到好友甲吃火锅。", type="episodic", importance=0.3, observed_at=wake_ts
        )
        payload = driver.summary()
        self.assertFalse(payload["ledger_missing"])
        self.assertEqual(payload["wake_count"], 1)
        self.assertEqual(payload["tokens"]["total"], 105)
        self.assertEqual(payload["qzone_new_total"], 2)
        self.assertEqual(payload["drafts_created_total"], 0)  # 本轮模型没起草
        self.assertEqual(len(payload["drafts"]), 1)
        self.assertGreater(payload["drafts"][0]["id"], 0)
        self.assertEqual(payload["drafts"][0]["status"], "draft")
        self.assertIn("吃火锅", payload["drafts"][0]["content"])
        self.assertEqual(payload["memory_new_in_window"], 1)
        self.assertTrue(lines and lines[0].startswith("{"))

    def test_summary_without_ledger_falls_back_to_window(self) -> None:
        driver, _, _ = make_driver(self.config, self.db)
        payload = driver.summary()
        self.assertTrue(payload["ledger_missing"])
        self.assertEqual(payload["wake_count"], 0)
        self.assertEqual(payload["drafts"], [])


@unittest.skipIf(shutil.which("git") is None, "git not available")
class SnapshotGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.db = make_db(self.config)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        base = ["git", "-C", str(self.repo)]
        subprocess.run([*base, "init", "-q"], check=True)
        subprocess.run([*base, "config", "user.email", "test@example.com"], check=True)
        subprocess.run([*base, "config", "user.name", "tester"], check=True)
        # 让 config.db_path / backup_dir 都落在这个临时仓库里。
        (self.repo / "data").mkdir()
        shutil.copy(self.config.db_path, self.repo / "data" / "night.db")
        self.config._data["storage"]["db_path"] = (self.repo / "data" / "night.db").as_posix()
        self.config._data["app"]["data_dir"] = (self.repo / "data").as_posix()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def test_prepare_and_finalize_snapshot(self) -> None:
        driver, _, _ = make_driver(self.config, self.db, date="2026-10-25")
        driver.repo_root = self.repo
        driver.ensure_session_binding(NIGHT_SESSION)

        prepared = driver.prepare()
        self.assertTrue(prepared["ok"])
        backup = Path(prepared["backup"])
        self.assertTrue(backup.exists())
        self.assertEqual(backup.name, "fatfish-before-nighttest-20261025.db")
        self.assertIn("pre-nighttest-20261025", self._git("tag"))
        self.assertIn(
            "chore(backup): pre-nighttest snapshot 20261025",
            self._git("log", "-1", "--format=%s"),
        )
        self.assertIn(backup.name, self._git("ls-files"))

        finalized = driver.finalize()
        self.assertTrue(finalized["ok"])
        self.assertIn("post-nighttest-20261025", self._git("tag"))
        self.assertEqual(finalized["bindings_removed"], 1)
        self.assertEqual(
            self.db.query_one(
                "SELECT COUNT(*) AS n FROM owner_bindings WHERE source='night-test'"
            )["n"],
            0,
        )

    def test_prepare_fails_when_db_missing(self) -> None:
        self.config._data["storage"]["db_path"] = (self.tmp / "missing.db").as_posix()
        # Driver deep-copies the config, so build it after the mutation.
        driver, _, _ = make_driver(self.config, self.db, date="2026-10-25")
        driver.repo_root = self.repo
        with self.assertRaises(FileNotFoundError):
            driver.prepare()


class JsonRepairSurvivalTests(unittest.TestCase):
    """核实点 3 的驱动侧：JSON 解析失败→修复重试的路径不能炸驱动。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        self.db = make_db(self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_repair_retry_keeps_wake_alive(self) -> None:
        runtime = Runtime(self.db, self.config)
        runtime.llm.api_key = "fake"
        calls: list[list[dict]] = []

        def chat(messages, *args, **kwargs):
            calls.append(list(messages))
            if len(calls) == 1:
                return LLMResult(
                    text="嗯……本鲸鱼娘看看再说。（这轮忘了 JSON 信封）", model="fake"
                )
            return LLMResult(
                text=(
                    '{"messages":["嗯，看完了。"],'
                    '"actions":[{"tool":"remember","args":{"text":"夜里翻了下动态"}}]}'
                ),
                model="fake",
            )

        runtime.llm.chat = chat
        runtime.voice.decide = lambda *a, **k: VoiceDecision("text_only", 0.1, ["test"])

        driver, _, _ = make_driver(
            self.config, self.db,
            qzone=FakeQzone(), bili=FakeBili(), runtime=runtime,
        )
        result = driver.run(dry_run=False, once=True)
        self.assertEqual(result["wakes"], 1)
        self.assertEqual(len(calls), 2)  # 主调用 + JSON 修复重试
        rows = [row for row in read_ledger(self.config) if row.get("event") == "wake"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "night-20261025-w01")
        self.assertFalse(rows[0]["failed"])
        # remember 动作真的落了库，台账里也记了 tool 名。
        self.assertIn("remember", rows[0]["actions"])
        self.assertEqual(
            self.db.query_one(
                "SELECT COUNT(*) AS n FROM memory_items WHERE source_type='model_action'"
            )["n"],
            1,
        )
        # 台账只统计到 turn_traces 里的主调用；修复重试只进 metrics（已知漏计）。
        self.assertEqual(rows[0]["trace_turns"], 1)


def _toml_dumps(data: dict) -> str:
    """Tiny TOML writer for flat tables of scalars (test-only)."""
    lines: list[str] = []
    for section, values in data.items():
        if isinstance(values, dict):
            lines.append(f"[{section}]")
            for key, value in values.items():
                lines.append(_toml_value(key, value))
            lines.append("")
    return "\n".join(lines)


def _toml_value(key: str, value) -> str:
    if isinstance(value, bool):
        return f"{key} = {'true' if value else 'false'}"
    if isinstance(value, int):
        return f"{key} = {value}"
    return f'{key} = "{value}"'


if __name__ == "__main__":
    unittest.main()
