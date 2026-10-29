"""CLI `fatfish memory person` (T-0057)."""

from __future__ import annotations

import contextlib
import gc
import io
import json
import tempfile
import unittest
from pathlib import Path

from fatfish import cli
from fatfish.config import load_config
from fatfish.context import ensure_default_persona
from fatfish.db import Database

OWNER_SESSION = "qq:private:123456789"


def make_env(tmp: Path) -> tuple[object, Database, Path]:
    data = tmp / "data"
    toml = f"""
[app]
data_dir = "{data.as_posix()}"
timezone = "Asia/Shanghai"

[storage]
db_path = "{(data / 'people-cli.db').as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 64

[tts]
enabled = false
"""
    path = tmp / "people.toml"
    path.write_text(toml, encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    ensure_default_persona(db)
    return config, db, path


class PersonCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config, self.db, self.config_path = make_env(self.tmp)

    def tearDown(self) -> None:
        # CLI 命令经 _bootstrap 打开的 Database 不会主动关闭（产品行为），
        # Windows 上文件句柄要等 GC 才释放；先回收再关自己的连接，
        # 否则 TemporaryDirectory 清理会撞 WinError 32。
        gc.collect()
        self.db.close()
        self._tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["--config", str(self.config_path), *argv])
        return code, json.loads(buffer.getvalue())

    def test_add_masks_the_uin_by_default(self) -> None:
        code, payload = self._run(
            "memory", "person", "add", "小明", "--uin", "123456789"
        )
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["created"])
        # 卡片主键本身含 uin，默认输出里也要一并掩掉。
        self.assertEqual("person:qq:123****6789", payload["entity"]["id"])
        self.assertEqual("123****6789", payload["entity"]["uin"])
        self.assertTrue(payload["entity"]["uin_masked"])

    def test_add_full_uin_is_opt_in(self) -> None:
        _, payload = self._run(
            "memory", "person", "add", "小明", "--uin", "123456789", "--full-uin"
        )
        self.assertEqual("123456789", payload["entity"]["uin"])
        self.assertEqual("person:qq:123456789", payload["entity"]["id"])
        self.assertNotIn("uin_masked", payload["entity"])

    def test_add_accepts_relation_alias_and_remark(self) -> None:
        _, payload = self._run(
            "memory",
            "person",
            "add",
            "小明",
            "--uin",
            "123456789",
            "--alias",
            "阿明",
            "--relation",
            "同学",
            "--remark",
            "实验室",
        )
        card = payload["entity"]
        self.assertIn("阿明", card["aliases"])
        self.assertEqual("同学", card["relation"])
        self.assertEqual("实验室", card["remark"])

    def test_add_is_idempotent(self) -> None:
        first = self._run("memory", "person", "add", "小明", "--uin", "123456789")[1]
        second = self._run("memory", "person", "add", "小明", "--uin", "123456789")[1]
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])

    def test_list_and_show(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "123456789")
        code, listing = self._run("memory", "person", "list")
        self.assertEqual(0, code)
        self.assertEqual(1, listing["count"])
        self.assertEqual("小明", listing["people"][0]["name"])
        code, shown = self._run("memory", "person", "show", "小明")
        self.assertEqual(0, code)
        self.assertEqual("person:qq:123****6789", shown["entity"]["id"])

    def test_show_accepts_an_alias(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "123456789", "--alias", "阿明")
        code, shown = self._run("memory", "person", "show", "阿明")
        self.assertEqual(0, code)
        self.assertEqual("person:qq:123****6789", shown["entity"]["id"])

    def test_show_accepts_an_uin(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "123456789")
        code, shown = self._run("memory", "person", "show", "123456789")
        self.assertEqual(0, code)
        self.assertEqual("person:qq:123****6789", shown["entity"]["id"])

    def test_show_unknown_selector_fails_cleanly(self) -> None:
        code, payload = self._run("memory", "person", "show", "查无此人")
        self.assertEqual(2, code)
        self.assertFalse(payload["ok"])
        self.assertIn("person_not_found", payload["error"])

    def test_show_ambiguous_name_fails_cleanly(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "111111111")
        self._run("memory", "person", "add", "小明", "--uin", "222222222")
        code, payload = self._run("memory", "person", "show", "小明")
        self.assertEqual(2, code)
        self.assertIn("ambiguous_selector", payload["error"])

    def test_rename_keeps_the_old_name(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "123456789")
        code, payload = self._run("memory", "person", "rename", "小明", "明明")
        self.assertEqual(0, code)
        self.assertEqual("明明", payload["entity"]["name"])
        self.assertIn("小明", payload["entity"]["aliases"])
        # The old name still selects the card.
        code, shown = self._run("memory", "person", "show", "小明")
        self.assertEqual(0, code)
        self.assertEqual("person:qq:123****6789", shown["entity"]["id"])

    def test_alias_add_and_remove(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "123456789")
        self._run("memory", "person", "alias", "小明", "add", "阿明")
        code, payload = self._run("memory", "person", "alias", "小明", "remove", "阿明")
        self.assertEqual(0, code)
        self.assertNotIn("阿明", payload["entity"]["aliases"])

    def test_merge_reports_one_card(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "111111111")
        self._run("memory", "person", "add", "明明", "--uin", "222222222")
        code, payload = self._run("memory", "person", "merge", "明明", "小明")
        self.assertEqual(0, code)
        self.assertTrue(payload["merged"])
        self.assertEqual("person:qq:111****1111", payload["entity"]["id"])
        listing = self._run("memory", "person", "list")[1]
        self.assertEqual(1, listing["count"])

    def test_merge_unknown_target_fails_cleanly(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "111111111")
        code, payload = self._run("memory", "person", "merge", "小明", "查无此人")
        self.assertEqual(2, code)
        self.assertFalse(payload["ok"])

    def test_duplicates_lists_pairs(self) -> None:
        self._run("memory", "person", "add", "小明", "--uin", "111111111")
        self._run("memory", "person", "add", "小明", "--uin", "222222222")
        code, payload = self._run("memory", "person", "duplicates")
        self.assertEqual(0, code)
        self.assertEqual(1, payload["count"])
        self.assertEqual("小明", payload["pairs"][0]["shared"])
        self.assertIn("****", payload["pairs"][0]["a"]["uin"])

    def test_sync_mirrors_the_owner_binding(self) -> None:
        self._run("owner", "bind", "--session", OWNER_SESSION, "--name", "阿主")
        code, payload = self._run("memory", "person", "sync")
        self.assertEqual(0, code)
        self.assertEqual("123****6789", payload["roles"]["owner"]["uin"])
        self.assertEqual("阿主", payload["roles"]["owner"]["name"])
        shown = self._run("memory", "person", "show", "阿主", "--full-uin")[1]
        self.assertEqual("person:owner", shown["entity"]["id"])
        self.assertEqual("123456789", shown["entity"]["uin"])


if __name__ == "__main__":
    unittest.main()
