from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
import os
import sys

from fatfish.clock import parse_when, to_local
from fatfish.config import Config, load_config
from fatfish.context import ContextAssembler, PERSONA_VERSION, ensure_default_persona
from fatfish.db import Database
from fatfish.life import LifeEngine
from fatfish.llm import LLMClient
from fatfish.memory import MemoryService, RecallGate
from fatfish.memory.embedder import dumps
from fatfish.outbox import Outbox
from fatfish.runtime import Runtime, parse_turn_plan
from fatfish.relationship import RelationshipRituals, proactive_text_for_reason
from fatfish.state import CHATTING, IDLE, StateMachine
from fatfish.sticker import StickerService
from fatfish.style import StyleController, count_emoji, count_kaomoji, is_serious
from fatfish.tts import TTSClient
from fatfish.tts.client import _render_template
from fatfish.voice import VoicePolicy
from fatfish.wakeup import ProactiveGate, WakeupScheduler


def make_env(tmp: Path):
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'test.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128

[voice]
enabled = true
min_score = 0.62
text_with_voice_min = 0.45
max_per_hour = 10
max_per_day = 50

[sticker]
enabled = true
# The suite runs offline, so the sticker index must use the hash embedder. Its
# cosines spread over 0-0.4, while production uses bge with a calibrated 0.50
# threshold (see config/low.toml). Pinning the hash-scale value keeps this test
# about the service's ranking/gating logic instead of production tuning.
embedding_backend = "hash"
intent_threshold = 0.38
comfyui_enabled = false

[wakeup]
max_per_day = 10
max_chain_depth = 3

[style]
# 生产配置（config/low.toml）现在是「不限量」（-1），因为主人要 emoji 正常用。
# 这里把上限钉回 1/1 并保留黑名单，测的是限流机制本身，不受生产调参影响。
max_emoji_per_message = 1
max_kaomoji_per_message = 1
emoji_blacklist = ["😅", "🙏", "👍", "🙂", "😊", "🤝"]
"""
    config_path = tmp / "test.toml"
    config_path.write_text(toml, encoding="utf-8")
    config = load_config("low", config_path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db = make_env(self.tmp)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_outbox_idempotency(self) -> None:
        outbox = Outbox(self.db)
        first, created1 = outbox.enqueue("send", {"text": "hi"}, idempotency_key="k1")
        second, created2 = outbox.enqueue("send", {"text": "hi"}, idempotency_key="k1")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first, second)
        claimed = outbox.claim(limit=10)
        self.assertEqual(len(claimed), 1)
        outbox.mark_done(claimed[0].id)
        self.assertEqual(outbox.stats().get("done"), 1)

    def test_state_transitions(self) -> None:
        sm = StateMachine(self.db)
        self.assertEqual(sm.current().state, IDLE)
        sm.transition(CHATTING, "user")
        self.assertEqual(sm.current().state, CHATTING)
        with self.assertRaises(ValueError):
            sm.transition("browsing", "illegal")

    def test_memory_timeline_search_and_compaction(self) -> None:
        memory = MemoryService(self.db, self.config)
        item = memory.add_item(
            "用户喜欢喝美式咖啡，不加糖。",
            type="preference",
            importance=0.8,
            event_time="2026-10-18",
            tags=["咖啡", "偏好"],
            entities=["用户"],
        )
        self.assertIsNotNone(item.event_time_start)
        self.assertEqual(item.time_granularity, "day")
        hits = memory.search("咖啡")
        self.assertTrue(hits)
        self.assertIn("咖啡", hits[0].item.summary)
        meta = memory.meta_directory()
        self.assertTrue(any(m["name"] == "咖啡" for m in meta))

        old = time.time() - 400 * 86400
        memory.add_item(
            "很久以前的一次普通闲聊，没有特别的信息。",
            type="episodic",
            importance=0.2,
            observed_at=old,
        )
        stats = memory.compact(dry_run=False, now=time.time())
        self.assertGreaterEqual(stats["checked"], 2)
        self.assertGreaterEqual(stats["archived"], 1)

    def test_memory_search_shortlist_keeps_exact_hit(self) -> None:
        memory = MemoryService(self.db, self.config)
        for i in range(40):
            memory.add_item(
                f"批量记忆第 {i} 号，都是高重要性的填充内容。",
                type="episodic",
                gist=f"批量记忆 {i}",
                importance=0.9,
            )
        target = memory.add_item(
            "唯一标记 zxqv7391 的那件事，重要性很低。",
            type="episodic",
            gist="唯一标记 zxqv7391",
            importance=0.2,
        )
        self.assertGreater(memory.candidate_limit, 0)
        memory.candidate_limit = 5
        shortlist = memory._shortlist_ids("zxqv7391", ["status='active'"], [], 5)
        self.assertLessEqual(len(shortlist), 5)
        self.assertEqual(shortlist[0], target.id)
        hits = memory.search("zxqv7391", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].item.id, target.id)
        memory.candidate_limit = 0
        full = memory.search("zxqv7391", limit=5)
        self.assertEqual(full[0].item.id, target.id)

    def test_memory_embedding_is_blob_and_reads_legacy_json(self) -> None:
        memory = MemoryService(self.db, self.config)
        item = memory.add_item(
            "向量存储格式检查的独特内容 blobfmt7721。",
            type="episodic",
            gist="blobfmt7721",
        )
        row = self.db.query_one(
            "SELECT typeof(embedding) AS t, length(embedding) AS n "
            "FROM memory_items WHERE id=?",
            (item.id,),
        )
        self.assertEqual(row["t"], "blob")
        self.assertEqual(int(row["n"]), len(item.embedding or []) * 4)
        hits = memory.search("blobfmt7721")
        self.assertTrue(hits)
        self.assertEqual(hits[0].item.id, item.id)
        # A pre-migration row still stored as JSON must keep working.
        self.db.execute(
            "UPDATE memory_items SET embedding=? WHERE id=?",
            (dumps(item.embedding), item.id),
        )
        legacy = memory.search("blobfmt7721")
        self.assertTrue(legacy)
        self.assertEqual(legacy[0].item.id, item.id)

    def test_memory_edit_and_delete_filters(self) -> None:
        memory = MemoryService(self.db, self.config)
        item = memory.add_item(
            "用户喜欢喝美式咖啡，不加糖。",
            type="preference",
            importance=0.4,
            tags=["咖啡"],
            entities=["用户"],
        )
        old_updated = item.updated_at

        edited = memory.update_item_fields(
            item.id,
            {
                "gist": "咖啡偏好",
                "summary": "用户喜欢喝美式咖啡，最近改喝冰美式。",
                "type": "preference",
                "importance": 0.9,
                "tags": ["咖啡", "冰美式", "咖啡"],
                "entities": ["用户"],
                "trust_level": "external",
                "origin_scope": "external",
                "unknown_field": "ignored",
            },
        )
        self.assertIsNotNone(edited)
        self.assertEqual("咖啡偏好", edited.gist)
        self.assertEqual("用户喜欢喝美式咖啡，最近改喝冰美式。", edited.summary)
        self.assertEqual(0.9, edited.importance)
        self.assertEqual(["咖啡", "冰美式"], edited.tags)
        self.assertEqual(["用户"], edited.entities)
        self.assertEqual("internal", edited.trust_level)
        self.assertEqual("self", edited.origin_scope)
        self.assertGreaterEqual(edited.updated_at, old_updated)

        # The embedding is refreshed, so semantic search sees new content.
        self.assertIn(item.id, {hit.item.id for hit in memory.search("冰美式")})
        self.assertIn(item.id, {entry.id for entry in memory.search_fts("冰美式")})

        for bad_fields in (
            {"importance": 1.5},
            {"importance": -0.1},
            {"importance": "0.5"},
            {"importance": True},
            {"gist": 123},
            {"type": ""},
            {"tags": "咖啡"},
            {"entities": [1]},
        ):
            with self.assertRaises(ValueError, msg=bad_fields):
                memory.update_item_fields(item.id, bad_fields)
        self.assertIsNone(memory.update_item_fields("missing-id", {"gist": "x"}))

        # Soft delete: hidden from every listing/search path, row still there.
        deleted = memory.delete_item(item.id)
        self.assertIsNotNone(deleted)
        self.assertEqual("deleted", deleted.status)
        self.assertIsNotNone(memory.get(item.id))
        self.assertNotIn(item.id, {entry.id for entry in memory.list_items(limit=100)})
        self.assertNotIn(
            item.id, {entry.id for entry in memory.list_items(limit=100, status=None)}
        )
        self.assertNotIn(item.id, {hit.item.id for hit in memory.search("咖啡", limit=20)})
        self.assertNotIn(
            item.id,
            {
                hit.item.id
                for hit in memory.search("咖啡", limit=20, include_forgotten=True)
            },
        )
        self.assertEqual([], memory.search_fts("咖啡"))

        # Restore.
        restored = memory.restore_item(item.id)
        self.assertIsNotNone(restored)
        self.assertEqual("active", restored.status)
        self.assertIn(item.id, {hit.item.id for hit in memory.search("咖啡", limit=20)})
        self.assertIn(item.id, {entry.id for entry in memory.search_fts("咖啡")})

        # Hard delete removes both the row and the FTS entry.
        removed = memory.delete_item(item.id, hard=True)
        self.assertIsNotNone(removed)
        self.assertIsNone(memory.get(item.id))
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM memory_fts WHERE id=?", (item.id,)
        )
        self.assertEqual(0, int(row["n"]))
        self.assertEqual([], memory.search_fts("咖啡"))

    def test_recall_is_ephemeral(self) -> None:
        memory = MemoryService(self.db, self.config)
        memory.add_item("用户最近在准备面试。", type="relationship", importance=0.8, tags=["面试"])
        memory.add_rag_chunk(
            "bilibili",
            "BV1",
            "面试前要查公司、准备项目故事、深呼吸。",
            metadata={"title": "面试准备"},
        )
        result = memory.recall("面试准备")
        self.assertTrue(result.ephemeral)
        self.assertTrue(result.refs)
        self.assertIn("面试", result.ephemeral_text)
        turns = self.db.query("SELECT COUNT(*) AS n FROM conversation_turns")
        self.assertEqual(int(turns[0]["n"]), 0)

    def test_wakeup_quiet_hours(self) -> None:
        wake = WakeupScheduler(self.db, self.config)
        now = parse_when("2026-10-20 23:45", self.config.timezone_name)
        self.assertEqual(parse_when("+9h30m", self.config.timezone_name, now=now) - now, 34200)
        shifted = wake.schedule("+10m", intent="check_user", reason="test", now=now)
        local_shifted = to_local(shifted.fire_at, self.config.timezone_name)
        self.assertEqual(local_shifted.hour, 8)
        self.assertEqual(local_shifted.minute, 0)

        urgent = wake.schedule("+10m", intent="check_user", reason="urgent", urgent=True, now=now)
        local_urgent = to_local(urgent.fire_at, self.config.timezone_name)
        self.assertEqual(local_urgent.hour, 23)
        gate = ProactiveGate(self.db, self.config)
        ok, reason = gate.should_act(urgent, now=urgent.fire_at)
        self.assertFalse(ok)
        self.assertEqual(reason, "quiet_hours")

    def test_emoji_style_control(self) -> None:
        style = StyleController(self.config)
        polished = style.polish(["开心😭😉😅", "👍收到", "（尾巴晃了一下）好耶"])
        self.assertLessEqual(count_emoji(polished[0]), 1)
        self.assertNotIn("😅", polished[0])
        self.assertNotIn("👍", polished[1])
        self.assertIn("（尾巴晃了一下）", polished[2])

        serious = style.polish(["别难过😭（抱抱）"], serious=is_serious("别难过，认真点"))
        self.assertEqual(count_emoji(serious[0]), 0)
        self.assertIn("（抱抱）", serious[0])

        kao = style.polish(["(≧▽≦) 好耶 (๑•̀ㅂ•́)و✧"], serious=False)
        self.assertLessEqual(count_kaomoji(kao[0]), 1)
        self.assertIn("好耶", kao[0])

    def test_emoji_budget_minus_one_means_unlimited(self) -> None:
        class _Cfg:
            def __init__(self, values):
                self._values = values

            def get(self, key, default=None):
                return self._values.get(key, default)

        unlimited = StyleController(
            _Cfg(
                {
                    "style.max_emoji_per_message": -1,
                    "style.max_kaomoji_per_message": -1,
                    "style.emoji_blacklist": [],
                }
            )
        )
        text = "开心😭😉🎉😅👍 好耶（≧▽≦）（๑•̀ㅂ•́）"
        polished = unlimited.polish([text])
        self.assertEqual(count_emoji(polished[0]), count_emoji(text))
        self.assertGreater(count_emoji(polished[0]), 2)
        self.assertGreaterEqual(count_kaomoji(polished[0]), 2)

        # 0 仍然是「全删」，和 -1 不是同一个意思。
        strip_all = StyleController(
            _Cfg(
                {
                    "style.max_emoji_per_message": 0,
                    "style.max_kaomoji_per_message": 0,
                    "style.emoji_blacklist": [],
                }
            )
        )
        self.assertEqual(count_emoji(strip_all.polish([text])[0]), 0)

        # 严肃场合不管上限多少都全删。
        serious = unlimited.polish([text], serious=True)
        self.assertEqual(count_emoji(serious[0]), 0)

    def test_voice_policy(self) -> None:
        voice = VoicePolicy(self.db, self.config)
        self.assertEqual(voice.decide("晚安，做个好梦", session_id="s").mode, "voice_only")
        self.assertEqual(
            voice.decide("这是安装步骤：先安装 Python，然后运行命令", session_id="s").mode,
            "text_only",
        )
        self.assertEqual(
            voice.decide("晚安", session_id="s", is_group=True).mode, "text_only"
        )

    def test_sticker_service(self) -> None:
        stickers = StickerService(self.db, self.config)
        stickers.add_sticker(
            "sticker.png",
            caption="鲸鱼娘摊手，一脸无奈又好笑",
            tags=["无奈", "好笑"],
            intents=["tease", "meme"],
            intensity=2,
        )
        decision = stickers.decide("想表达一种无奈又好笑、轻轻的吐槽")
        self.assertTrue(decision.send)
        self.assertIsNotNone(decision.sticker)
        none = stickers.decide("恭喜发财，大吉大利")
        self.assertFalse(none.send)

    def test_context_prefix_hit(self) -> None:
        memory = MemoryService(self.db, self.config)
        assembler = ContextAssembler(self.db, self.config, memory)
        now = time.time()
        first = assembler.build("s", "你好", now=now)
        second = assembler.build("s", "你好", now=now + 120)
        self.assertEqual(first.stable_prefix_hash, second.stable_prefix_hash)
        self.assertFalse(first.prefix_hit)
        self.assertTrue(second.prefix_hit)
        self.assertNotEqual(first.dynamic_hash, second.dynamic_hash)

    def test_opencode_go_headers(self) -> None:
        previous = os.environ.get("OPENCODE_API_KEY")
        previous_dedicated = os.environ.get("FATFISH_OPENCODE_KEY")
        os.environ["OPENCODE_API_KEY"] = "test-key"
        os.environ["FATFISH_OPENCODE_KEY"] = "test-key"
        try:
            client = LLMClient(self.config, self.db)
            headers = client.headers("session/with spaces")
            self.assertEqual(headers["authorization"], "Bearer test-key")
            self.assertEqual(client.api_key_env, "FATFISH_OPENCODE_KEY")
            self.assertEqual(headers["user-agent"], "fatfish/0.2.0")
            self.assertEqual(headers["x-opencode-session"], "session-with-spaces")
            url, payload = client.build_payload([{"role": "user", "content": "hi"}])
            self.assertTrue(url.endswith("/chat/completions"))
            self.assertEqual(payload["model"], "deepseek-v4.1-flash")

            client.api_style = "responses"
            url2, payload2 = client.build_payload([{"role": "user", "content": "hi"}])
            self.assertTrue(url2.endswith("/responses"))
            self.assertIn("input", payload2)

            usage = LLMClient._parse_usage(
                {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 80},
                }
            )
            self.assertEqual(usage.cache_hit_tokens, 80)
            self.assertEqual(usage.cache_miss_tokens, 20)
        finally:
            if previous is None:
                os.environ.pop("OPENCODE_API_KEY", None)
            else:
                os.environ["OPENCODE_API_KEY"] = previous
            if previous_dedicated is None:
                os.environ.pop("FATFISH_OPENCODE_KEY", None)
            else:
                os.environ["FATFISH_OPENCODE_KEY"] = previous_dedicated

    def test_turn_plan_parse(self) -> None:
        plan = parse_turn_plan(
            '{"reply":"好呀","actions":[{"tool":"remember","args":{"text":"喜欢咖啡"}},'
            '{"tool":"schedule_wakeup","args":{"when":"+30m","reason":"跟进"}}]}'
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan.reply, "好呀")
        self.assertEqual([a.tool for a in plan.actions], ["remember", "schedule_wakeup"])

        multi = parse_turn_plan(
            '{"messages":["哼，鱼片","才不是大肥鱼","白饭还有吗"],"actions":[]}'
        )
        self.assertEqual(len(multi.messages), 3)
        self.assertIn("白饭", multi.reply)

        newline_split = parse_turn_plan('{"reply":"第一条\\n第二条","actions":[]}')
        self.assertEqual(len(newline_split.messages), 2)

        sticker_only = parse_turn_plan(
            '{"messages":[],"actions":[{"tool":"send_sticker","args":{"need":"无奈"}}]}'
        )
        self.assertEqual(sticker_only.messages, [])
        self.assertEqual(len(sticker_only.actions), 1)

        abuse = parse_turn_plan(
            '{"messages":["' + "很长" * 200 + '","二","三","四","五","@all 集合"],"actions":[]}'
        )
        self.assertLessEqual(len(abuse.messages), 4)
        self.assertTrue(all(len(m) <= 120 for m in abuse.messages))
        self.assertNotIn("@all", "\n".join(abuse.messages))

        fenced = parse_turn_plan('```json\n{"reply":"嗯","actions":[]}\n```')
        self.assertEqual(fenced.reply, "嗯")

        plain = parse_turn_plan("就是普通文本")
        self.assertEqual(plain.reply, "就是普通文本")
        self.assertEqual(plain.actions, [])

    def test_persona_migration(self) -> None:
        row = self.db.query_one("SELECT version, core_json FROM persona WHERE id='default'")
        self.assertEqual(int(row["version"]), PERSONA_VERSION)
        self.assertIn("大肥鱼", row["core_json"])
        self.assertIn("主人哥哥", row["core_json"])
        self.assertIn("本鲸鱼娘", row["core_json"])
        # 主人 2026-10-30 要求「emoji 不用设限制」：prompt 里不该再出现硬性条数/黑名单。
        self.assertNotIn("客服 emoji", row["core_json"])
        self.assertNotIn("emoji 默认最多一个", row["core_json"])
        # 主人 2026-10-30 要求：任何人问主人哥哥的作息，一律撒娇装不知道（v7）。
        core = json.loads(row["core_json"])
        self.assertIn("主人哥哥的作息", core["boundaries"][5])
        self.assertTrue(any("秘密" in ex or "不告诉你" in ex for ex in core["examples"]))

    def test_recall_gate_filters(self) -> None:
        memory = MemoryService(self.db, self.config)
        memory.add_item(
            "用户明天要面试，很紧张。",
            type="relationship",
            importance=0.85,
            tags=["面试"],
        )
        memory.add_item(
            "用户喜欢喝美式咖啡。",
            type="preference",
            importance=0.5,
            tags=["咖啡"],
        )
        hits = memory.search("面试准备", limit=10)
        gate = RecallGate(self.db, self.config)
        result = gate.select("面试准备", hits, [], context_text="用户明天要面试")
        self.assertTrue(result.memory_hits)
        joined = " ".join(h.item.summary for h in result.memory_hits)
        self.assertIn("面试", joined)
        self.assertNotIn("咖啡", joined)

    def test_execute_multiple_actions(self) -> None:
        runtime = Runtime(self.db, self.config)
        now = parse_when("2026-10-20 12:00", self.config.timezone_name)
        plan = parse_turn_plan(
            '{"reply":"好","actions":['
            '{"tool":"remember","args":{"text":"用户喜欢美式咖啡","type":"preference","importance":0.8}},'
            '{"tool":"schedule_wakeup","args":{"when":"+2h","intent":"check_user","reason":"测试"}},'
            '{"tool":"open_loop","args":{"text":"问面试"}}'
            ']}'
        )
        results = runtime.execute_actions(
            plan.actions, session_id="s", reply=plan.reply, now=now
        )
        self.assertTrue(all(r["ok"] for r in results), results)
        hits = runtime.memory.search("美式")
        self.assertTrue(hits)
        pending = runtime.wakeup.list("pending")
        self.assertTrue(any(w.reason == "测试" for w in pending))
        loops = runtime.memory.list_open_loops()
        self.assertTrue(any("面试" in loop["text"] for loop in loops))

    def test_tts_disabled_and_command(self) -> None:
        disabled_data = self.config.as_dict()
        disabled_data["tts"] = {**disabled_data.get("tts", {}), "enabled": False}
        disabled = TTSClient(
            Config(disabled_data, profile="low"), self.db
        ).synthesize("你好")
        self.assertFalse(disabled.ok)
        self.assertEqual(disabled.error, "disabled")

        data = self.config.as_dict()
        data["tts"] = {
            "enabled": True,
            "mode": "command",
            "format": "wav",
            "command": [
                sys.executable,
                "-c",
                "import sys; open(sys.argv[1],'wb').write(b'RIFFtest')",
                "{out}",
            ],
        }
        cfg = Config(data, profile="low")
        cfg.ensure_dirs()
        result = TTSClient(cfg, self.db).synthesize("你好")
        self.assertTrue(result.ok, result)
        self.assertTrue(Path(result.audio_path).exists())

        gsv_data = self.config.as_dict()
        gsv_data["tts"] = {
            "enabled": True,
            "mode": "gpt_sovits",
            "base_url": "http://127.0.0.1:9880/tts",
            "ref_audio_path": "D:/voice/ref.wav",
            "text_lang": "zh",
            "prompt_lang": "zh",
            "media_type": "wav",
        }
        gsv = TTSClient(Config(gsv_data, profile="low"), self.db)
        self.assertTrue(gsv.status()["configured"])
        self.assertEqual(gsv.status()["mode"], "gpt_sovits")

        rendered = _render_template(
            {"tts_text": "{text}", "spk_id": "{voice}", "instruct_text": "{emotion}"},
            {"text": "你好", "voice": "v1", "emotion": "软萌"},
        )
        self.assertEqual(rendered["tts_text"], "你好")
        self.assertEqual(rendered["instruct_text"], "软萌")

    def test_relationship_rituals(self) -> None:
        now = parse_when("2026-10-20 09:00", self.config.timezone_name)
        old = now - 4 * 86400
        self.db.execute(
            "INSERT INTO conversation_turns(session_id, role, content, created_at, token_count) "
            "VALUES ('s', 'user', 'hi', ?, 1)",
            (old,),
        )
        wake = WakeupScheduler(self.db, self.config)
        rituals = RelationshipRituals(self.db, self.config, wake)
        scheduled = rituals.tick(now=now)
        reasons = [item.get("reason", "") for item in scheduled]
        self.assertTrue(any("早安" in r for r in reasons), reasons)
        self.assertTrue(any("天没理" in r for r in reasons), reasons)
        pending = wake.list("pending")
        self.assertTrue(any("早安" in w.reason for w in pending))

        night = parse_when("2026-10-20 23:00", self.config.timezone_name)
        rituals.tick(now=night)
        pending = wake.list("pending")
        self.assertTrue(any("晚安" in w.reason for w in pending))
        self.assertIn("累不累", proactive_text_for_reason("晚安：问主人哥哥今天累不累") or "")

    def test_proactive_outbox(self) -> None:
        runtime = Runtime(self.db, self.config)
        now = parse_when("2026-10-20 09:00", self.config.timezone_name)
        wakeup = runtime.wakeup.schedule(
            "+1m", intent="check_user", reason="早安：问主人哥哥今天怎么样", now=now
        )
        results = runtime.process_due_wakeups(now=now + 120)
        self.assertTrue(any(r.get("acted") for r in results), results)
        row = self.db.query_one(
            "SELECT payload_json FROM outbox WHERE kind='proactive_message' ORDER BY id DESC LIMIT 1"
        )
        self.assertIsNotNone(row)
        self.assertIn("早安", row["payload_json"])

    def test_life_engine(self) -> None:
        life = LifeEngine(self.db, self.config)
        state = life.tick(now=time.time())
        self.assertIn(state.current_activity, {"browsing", "browse_bilibili", "read", "rest", "write_notes", "sleeping"})
        plan = life.plan_day(day="2026-10-20")
        self.assertGreaterEqual(len(plan), 5)


if __name__ == "__main__":
    unittest.main()
