from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fatfish.config import load_config
from fatfish.context import ContextAssembler
from fatfish.db import Database
from fatfish.lexicon import (
    PROMPT_HEADER,
    LexiconService,
    load_seed_entries,
    make_id,
    mine,
)
from fatfish.memory import MemoryService
from fatfish.web.server import COOKIE_NAME, create_server

TOKEN = "lexicon-test-token"


def make_env(tmp: Path, name: str = "lexicon.db"):
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / name).as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 128
"""
    path = tmp / "test.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return config, db, path


def insert_turn(db: Database, role: str, content: str, *, at: float | None = None) -> None:
    db.execute(
        "INSERT INTO conversation_turns("
        "session_id, role, content, created_at, token_count, used_refs_json, ephemeral"
        ") VALUES (?,?,?,?,?,?,?)",
        ("s", role, content, time.time() if at is None else at, 0, "[]", 0),
    )


class LexiconServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.config_path = make_env(self.tmp)
        self.lexicon = LexiconService(self.db, self.config)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    # ------------------------------------------------------------------ seed
    def test_seed_corpus_is_valid(self) -> None:
        entries = load_seed_entries()
        self.assertGreaterEqual(len(entries), 80)
        self.assertLessEqual(len(entries), 120)
        keys = {(entry["kind"], entry["content"]) for entry in entries}
        self.assertEqual(len(entries), len(keys))
        for entry in entries:
            self.assertIn(entry["kind"], {"expression", "jargon"})
            self.assertIn(entry["status"], {"candidate", "approved"})
            self.assertIn(
                entry["style"], {"soft", "tsundere", "playful", "comfort", "serious"}
            )
            self.assertGreaterEqual(entry["confidence"], 0.6)
            self.assertLessEqual(entry["confidence"], 0.95)
            self.assertTrue(entry["content"].strip())
            self.assertTrue(entry["meaning"].strip())
            self.assertIsInstance(entry["tags"], list)
            self.assertIsInstance(entry["examples"], list)
            self.assertLessEqual(len(entry["examples"]), 2)
            haystack = " ".join(
                [entry["content"], entry["meaning"], entry["situation"]]
                + entry["tags"]
                + entry["examples"]
            )
            self.assertNotRegex(haystack, r"https?://|\[CQ:|@\w+")

    def test_seed_is_idempotent(self) -> None:
        seed_count = len(load_seed_entries())
        self.assertEqual(seed_count, self.lexicon.stats()["total"])
        again = LexiconService(self.db, self.config)
        self.assertEqual(0, again.seed_if_empty())
        self.assertEqual(seed_count, again.stats()["total"])
        row = self.db.query_one(
            "SELECT id FROM lexicon_entries WHERE kind='expression' AND content='贴贴'"
        )
        self.assertIsNotNone(row)
        self.assertEqual(make_id("expression", "贴贴"), row["id"])

    # ------------------------------------------------------------------ CRUD
    def test_add_review_list_and_stats(self) -> None:
        item = self.lexicon.add(
            "expression",
            "测试暗号",
            meaning="测试用的表达",
            situation="单元测试时",
            style="soft",
            tags=["测试", "暗号"],
            examples=["这是一句测试暗号"],
            status="candidate",
        )
        self.assertEqual(make_id("expression", "测试暗号"), item["id"])
        self.assertEqual(item["id"], make_id("expression", "测试暗号"))
        self.assertEqual("candidate", item["status"])
        self.assertEqual(["测试", "暗号"], item["tags"])
        self.assertEqual(["这是一句测试暗号"], item["examples"])
        self.assertEqual(1, len(self.lexicon.list(query="测试暗号")))

        duplicate = self.lexicon.add("expression", "测试暗号", meaning="不应覆盖")
        self.assertEqual(item["id"], duplicate["id"])
        self.assertEqual("测试用的表达", duplicate["meaning"])

        queried = self.lexicon.list(status="candidate", query="测试暗号", limit=10)
        self.assertEqual(1, len(queried))
        self.assertEqual(item["id"], queried[0]["id"])

        reviewed = self.lexicon.review(
            item["id"], "approved", meaning="审核后的意思", style="comfort"
        )
        self.assertIsNotNone(reviewed)
        assert reviewed is not None
        self.assertEqual("approved", reviewed["status"])
        self.assertEqual("审核后的意思", reviewed["meaning"])
        self.assertEqual("comfort", reviewed["style"])
        self.assertIsNotNone(reviewed["reviewed_at"])

        stats = self.lexicon.stats()
        self.assertGreaterEqual(stats["approved"], 1)
        self.assertGreaterEqual(stats["total"], len(load_seed_entries()) + 1)
        self.assertIn("by_status", stats)
        self.assertIn("by_kind", stats)
        self.assertIn("by_source", stats)

        with self.assertRaises(ValueError):
            self.lexicon.review(item["id"], "bogus")
        with self.assertRaises(ValueError):
            self.lexicon.review(item["id"], "approved", style="bogus")
        with self.assertRaises(ValueError):
            self.lexicon.list(status="bogus")
        with self.assertRaises(ValueError):
            self.lexicon.add("bogus", "x")
        with self.assertRaises(ValueError):
            self.lexicon.add("expression", "")
        self.assertIsNone(self.lexicon.review("missing-entry", "approved"))

    def test_approved_for_context_caps_and_status(self) -> None:
        self.db.execute("UPDATE lexicon_entries SET status='disabled'")
        approved_ids = []
        for index in range(8):
            item = self.lexicon.add(
                "expression",
                f"测试安慰{index}",
                meaning="陪伴和安慰",
                situation="对方难过需要安慰时",
                style="comfort",
                tags=["安慰", "测试"],
                status="approved",
            )
            approved_ids.append(item["id"])
        candidate = self.lexicon.add(
            "expression", "候选安慰", meaning="候选", tags=["安慰"], status="candidate"
        )
        disabled = self.lexicon.add(
            "expression", "禁用安慰", meaning="禁用", tags=["安慰"], status="disabled"
        )
        empty_id = "empty-content-entry"
        now = time.time()
        self.db.execute(
            "INSERT INTO lexicon_entries("
            "id, kind, content, meaning, situation, style, tags_json, examples_json, "
            "source, confidence, status, usage_count, created_at, updated_at, reviewed_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                empty_id, "expression", "", "空内容", "", "", "[]", "[]",
                "test", 0.9, "approved", 0, now, now, now,
            ),
        )

        selected = self.lexicon.approved_for_context("今天很难过，需要安慰", limit=6)
        self.assertEqual(6, len(selected))
        self.assertTrue(all(entry["status"] == "approved" for entry in selected))
        self.assertTrue(all(entry["content"] for entry in selected))
        selected_ids = {entry["id"] for entry in selected}
        self.assertNotIn(candidate["id"], selected_ids)
        self.assertNotIn(disabled["id"], selected_ids)
        self.assertNotIn(empty_id, selected_ids)
        self.assertTrue(selected_ids.issubset(set(approved_ids)))

        first_ids = [entry["id"] for entry in selected]
        second_ids = [
            entry["id"]
            for entry in self.lexicon.approved_for_context("今天很难过，需要安慰", limit=6)
        ]
        self.assertEqual(first_ids, second_ids)

        block = self.lexicon.format_for_prompt(selected)
        self.assertTrue(block.startswith(PROMPT_HEADER))
        self.assertLessEqual(len(block), 400)
        self.assertNotIn("候选安慰", block)
        self.assertNotIn("禁用安慰", block)

        before = self.lexicon.get(first_ids[0])
        assert before is not None
        self.assertEqual(1, self.lexicon.record_usage([first_ids[0], first_ids[0]]))
        after = self.lexicon.get(first_ids[0])
        assert after is not None
        self.assertEqual(before["usage_count"] + 1, after["usage_count"])

        for index in range(12):
            self.lexicon.add(
                "expression",
                f"超长测试{index}",
                meaning="很长的说明",
                situation="这是一个很长很长的使用场景说明" * 8,
                style="comfort",
                tags=["测试"],
                status="approved",
            )
        broad = self.lexicon.approved_for_context("", limit=20)
        self.assertLessEqual(len(broad), 20)
        long_block = self.lexicon.format_for_prompt(broad)
        self.assertLessEqual(len(long_block), 400)

    # ----------------------------------------------------------------- mining
    def test_miner_inserts_only_candidates_and_dedupes(self) -> None:
        now = time.time()
        insert_turn(self.db, "user", "这个菠萝披萨真的太怪了", at=now - 60)
        insert_turn(self.db, "assistant", "又是菠萝披萨，菠萝披萨真的绝了", at=now - 50)
        insert_turn(self.db, "user", "菠萝披萨，我绷不住了啊", at=now - 40)
        insert_turn(
            self.db,
            "user",
            "看 https://example.com 加QQ 123456789 @某人 这个菠萝披萨真奇怪",
            at=now - 30,
        )
        insert_turn(self.db, "system", "系统提示词" * 20, at=now - 20)
        insert_turn(self.db, "user", "「月亮掉线了」是什么鬼", at=now - 10)
        insert_turn(
            self.db,
            "assistant",
            "月亮掉线了月亮掉线了月亮掉线了",
            at=now - 5,
        )
        insert_turn(self.db, "user", "陈年旧梗" * 8, at=now - 40 * 86400)

        result = mine(self.lexicon, days=14, limit_turns=100, min_count=2)
        self.assertEqual(
            {"scanned", "candidates", "added", "skipped", "duplicates", "truncated"},
            set(result),
        )
        self.assertEqual(6, result["scanned"])
        self.assertGreaterEqual(result["added"], 2)
        self.assertGreaterEqual(result["candidates"], result["added"])
        self.assertGreaterEqual(result["skipped"], 0)

        mined = self.db.query(
            "SELECT * FROM lexicon_entries WHERE source='miner' ORDER BY content"
        )
        self.assertTrue(mined)
        contents = {row["content"] for row in mined}
        self.assertIn("菠萝披萨", contents)
        self.assertIn("月亮掉线了", contents)
        self.assertNotIn("陈年旧梗", contents)
        for row in mined:
            self.assertEqual("jargon", row["kind"])
            self.assertEqual("candidate", row["status"])
            self.assertEqual("", row["meaning"])
            self.assertGreaterEqual(float(row["confidence"]), 0.3)
            self.assertLessEqual(float(row["confidence"]), 0.9)
            baggage = str(row["situation"] or "") + str(row["examples_json"] or "")
            self.assertNotRegex(baggage, r"\d|https?://|@\w+|\[CQ:")
            self.assertLessEqual(len(json.loads(row["examples_json"] or "[]")), 2)

        count_before = len(mined)
        again = mine(self.lexicon, days=14, limit_turns=100, min_count=2)
        self.assertEqual(0, again["added"])
        self.assertGreaterEqual(again["skipped"], 1)
        self.assertGreaterEqual(again["duplicates"], 1)
        count_after = self.db.query_one(
            "SELECT COUNT(*) AS n FROM lexicon_entries WHERE source='miner'"
        )
        self.assertEqual(count_before, int(count_after["n"]))

    # ---------------------------------------------------------------- context
    def test_context_injects_approved_into_dynamic_only(self) -> None:
        self.db.execute("UPDATE lexicon_entries SET status='disabled'")
        approved = self.lexicon.add(
            "expression",
            "测试暗号",
            meaning="只有动态提示能看到的暗号",
            situation="对方需要安慰时",
            style="comfort",
            tags=["安慰", "测试"],
            status="approved",
        )
        candidate = self.lexicon.add(
            "expression", "候选暗号", meaning="候选", tags=["安慰"], status="candidate"
        )
        disabled = self.lexicon.add(
            "expression", "禁用暗号", meaning="禁用", tags=["安慰"], status="disabled"
        )
        memory = MemoryService(self.db, self.config)
        assembler = ContextAssembler(
            self.db, self.config, memory, lexicon=self.lexicon
        )
        plain = ContextAssembler(self.db, self.config, memory)

        bundle = assembler.build("s", "今天好累，需要一点安慰", now=time.time())
        by_name = {section.name: section.text for section in bundle.sections}
        self.assertIn(PROMPT_HEADER, by_name["runtime_context"])
        self.assertIn("测试暗号", by_name["runtime_context"])
        self.assertNotIn("测试暗号", by_name["stable_prefix"])
        self.assertNotIn(PROMPT_HEADER, by_name["stable_prefix"])
        self.assertNotIn("候选暗号", bundle.text())
        self.assertNotIn("禁用暗号", bundle.text())
        self.assertEqual(plain.stable_prefix(), bundle.sections[0].text)

        used = self.lexicon.get(approved["id"])
        assert used is not None
        self.assertEqual(1, used["usage_count"])
        self.assertEqual(0, self.lexicon.get(candidate["id"])["usage_count"])  # type: ignore[index]
        self.assertEqual(0, self.lexicon.get(disabled["id"])["usage_count"])  # type: ignore[index]

        self.lexicon.review(approved["id"], "disabled")
        cleared = assembler.build("s", "今天好累，需要一点安慰", now=time.time())
        cleared_dynamic = {
            section.name: section.text for section in cleared.sections
        }["runtime_context"]
        self.assertNotIn(PROMPT_HEADER, cleared_dynamic)


class LexiconApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        data_dir = cls.tmp / "data"
        data_dir.mkdir(parents=True)
        cls._env = patch.dict(
            os.environ,
            {
                "FATFISH_DATA_DIR": str(data_dir),
                "FATFISH_DB": str(data_dir / "api.db"),
                "OPENCODE_API_KEY": "",
            },
        )
        cls._env.start()
        cls.config = load_config("low")
        cls.config.ensure_dirs()
        db = Database(cls.config.db_path)
        db.initialize()
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

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        request_headers = dict(headers or {})
        request_headers.setdefault("Cookie", f"{COOKIE_NAME}={TOKEN}")
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request(method, path, body=data, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def _get(self, path: str) -> tuple[int, dict]:
        status, raw = self._request("GET", path)
        return status, json.loads(raw.decode("utf-8"))

    def _post(self, path: str, payload: dict, *, csrf: bool = True) -> tuple[int, dict]:
        headers = {"X-Fatfish-Request": "1"} if csrf else {}
        status, raw = self._request("POST", path, body=payload, headers=headers)
        return status, json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------ tests
    def test_list_and_stats(self) -> None:
        status, payload = self._get("/api/lexicon?limit=5")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertLessEqual(payload["count"], 5)
        self.assertLessEqual(len(payload["items"]), 5)
        self.assertGreaterEqual(payload["stats"]["total"], 80)
        item = payload["items"][0]
        for key in (
            "id", "kind", "content", "meaning", "situation", "style", "tags",
            "examples", "source", "confidence", "status", "usage_count",
        ):
            self.assertIn(key, item)
        self.assertNotIn("db_path", item)

        status, payload = self._get("/api/lexicon?status=approved&limit=10")
        self.assertEqual(200, status)
        self.assertTrue(all(entry["status"] == "approved" for entry in payload["items"]))

        status, payload = self._get("/api/lexicon?status=bogus")
        self.assertEqual(400, status)
        self.assertEqual("invalid_status", payload["error"])

        status, payload = self._get("/api/lexicon/stats")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["stats"]["total"], 80)

    def test_add_review_and_validation(self) -> None:
        status, payload = self._post(
            "/api/lexicon",
            {
                "kind": "expression",
                "content": "测试接口暗号",
                "meaning": "接口测试",
                "situation": "单元测试时",
                "style": "soft",
                "tags": ["测试"],
                "examples": ["接口暗号"],
            },
        )
        self.assertEqual(200, status, payload)
        item = payload["item"]
        self.assertEqual("candidate", item["status"])
        self.assertEqual(make_id("expression", "测试接口暗号"), item["id"])

        status, payload = self._post(
            "/api/lexicon/review",
            {
                "id": item["id"],
                "status": "approved",
                "meaning": "审核后的接口含义",
                "style": "comfort",
            },
        )
        self.assertEqual(200, status, payload)
        self.assertEqual("approved", payload["item"]["status"])
        self.assertEqual("审核后的接口含义", payload["item"]["meaning"])

        status, payload = self._post(
            "/api/lexicon/review", {"id": item["id"], "status": "bogus"}
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_status", payload["error"])

        status, payload = self._post(
            "/api/lexicon/review", {"id": "missing-id", "status": "approved"}
        )
        self.assertEqual(404, status)

        status, payload = self._post(
            "/api/lexicon", {"kind": "bogus", "content": "x"}
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_kind", payload["error"])

        status, payload = self._post(
            "/api/lexicon",
            {"kind": "expression", "content": "坏标签", "tags": [1, 2]},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_tags", payload["error"])

        status, payload = self._post(
            "/api/lexicon/review", {"id": item["id"], "status": "approved"}, csrf=False
        )
        self.assertEqual(403, status)

        status, payload = self._get(
            "/api/lexicon?query=%E6%B5%8B%E8%AF%95%E6%8E%A5%E5%8F%A3%E6%9A%97%E5%8F%B7"
        )
        self.assertEqual(200, status)
        self.assertEqual(1, payload["count"])
        self.assertEqual("approved", payload["items"][0]["status"])

    def test_mine_endpoint(self) -> None:
        db = Database(self.config.db_path)
        db.initialize()
        now = time.time()
        for index, text in enumerate(
            ["海盐柠檬茶真的好喝", "今天也想喝海盐柠檬茶", "海盐柠檬茶，绝了"]
        ):
            db.execute(
                "INSERT INTO conversation_turns("
                "session_id, role, content, created_at, token_count, used_refs_json, ephemeral"
                ") VALUES (?,?,?,?,?,?,?)",
                ("api", "user", text, now - 100 + index, 0, "[]", 0),
            )
        db.close()

        status, payload = self._post(
            "/api/lexicon/mine", {"days": 14, "min_count": 2}
        )
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["scanned"], 3)
        self.assertGreaterEqual(payload["added"], 1)

        status, payload = self._get(
            "/api/lexicon?status=candidate&query=%E6%B5%B7%E7%9B%90%E6%9F%A0%E6%AA%AC%E8%8C%B6"
        )
        self.assertEqual(200, status)
        self.assertGreaterEqual(payload["count"], 1)
        self.assertEqual("miner", payload["items"][0]["source"])
        self.assertEqual("candidate", payload["items"][0]["status"])

        status, payload = self._post(
            "/api/lexicon/mine", {"days": 14, "min_count": 2}
        )
        self.assertEqual(200, status)
        self.assertEqual(0, payload["added"])


if __name__ == "__main__":
    unittest.main()
