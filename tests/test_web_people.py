from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fatfish.config import Config, load_config
from fatfish.db import Database
from fatfish.memory.entities import record_mention
from fatfish.web.server import COOKIE_NAME, create_server

TOKEN = "test-token-people"
UIN = "1234567890"
UIN_MASKED = "123****7890"


def _json(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


class WebPeopleTests(unittest.TestCase):
    """Web「人物」面板 API：/api/people + add/update/merge（T-0057 P2）。

    与 test_web.py 同一套脚手架：临时目录 + FATFISH_DATA_DIR/FATFISH_DB
    环境变量 + create_server + cookie token。离线、不读真实 data/。
    """

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
                "FATFISH_DB": str(cls.data_dir / "people-web.db"),
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
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {"X-Fatfish-Request": "1"} if csrf else {}
        return self._request("POST", path, body=payload, headers=headers, cookie=cookie)

    def _get_json(self, path: str) -> tuple[int, dict]:
        status, _headers, raw = self._request("GET", path)
        return status, _json(raw)

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        status, _headers, raw = self._post(path, payload)
        return status, _json(raw)

    def _add_person(self, **payload: object) -> dict:
        status, body = self._post_json("/api/people/add", payload)
        self.assertEqual(200, status, body)
        self.assertTrue(body["ok"])
        return body["person"]

    # --------------------------------------------------------------- tests
    def test_people_unauthorized(self) -> None:
        status, _headers, raw = self._request("GET", "/api/people", cookie=False)
        self.assertEqual(401, status)
        payload = _json(raw)
        self.assertFalse(payload["ok"])
        self.assertEqual("unauthorized", payload["error"])
        self.assertNotIn(TOKEN.encode(), raw)

        status, _headers, raw = self._post(
            "/api/people/add", {"name": "不该进来的"}, cookie=False
        )
        self.assertEqual(401, status)
        self.assertFalse(_json(raw)["ok"])

    def test_people_empty_list(self) -> None:
        # 用一个必然无命中的查询验证「空列表」的响应形状（用例按字母序执行，
        # 其它用例可能已经建了卡，所以这里不依赖全库为空）。
        status, body = self._get_json("/api/people?query=%E7%BB%9D%E6%97%A0%E4%BB%85%E6%9C%89xyz")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(0, body["count"])
        self.assertEqual([], body["people"])

    def test_add_then_list_masks_uin(self) -> None:
        status, body = self._post_json(
            "/api/people/add",
            {
                "name": "小美",
                "uin": UIN,
                "aliases": ["美美"],
                "relation": "朋友",
                "remark": "爱喝汽水",
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["created"])
        person = body["person"]
        self.assertEqual("小美", person["name"])
        self.assertEqual(UIN_MASKED, person["uin_masked"])
        self.assertNotIn("uin", person)
        self.assertEqual(["美美"], person["aliases"])
        self.assertEqual("朋友", person["relation"])
        self.assertEqual("爱喝汽水", person["remark"])

        status, _headers, raw = self._request("GET", "/api/people?query=%E5%B0%8F%E7%BE%8E")
        payload = _json(raw)
        self.assertEqual(200, status)
        self.assertEqual(1, payload["count"])
        card = payload["people"][0]
        self.assertEqual(person["id"], card["id"])
        self.assertEqual(UIN_MASKED, card["uin_masked"])
        for key in (
            "id",
            "name",
            "aliases",
            "relation",
            "remark",
            "mentions",
            "last_seen_at",
            "merged_from",
            "uin_masked",
        ):
            self.assertIn(key, card)
        # uin 的打码版本在响应里；完整 uin 只以卡片 id（person:qq:<uin>）的
        # 形式存在——那是数据层的键，界面与 CLI 同样原样展示 id。
        status, payload = self._get_json(f"/api/people?query={UIN}")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["count"])
        self.assertEqual(UIN_MASKED, payload["people"][0]["uin_masked"])
        self.assertNotIn("uin", payload["people"][0])

    def test_update_rename_keeps_old_name_as_alias(self) -> None:
        person = self._add_person(name="阿黄")
        status, body = self._post_json(
            "/api/people/update", {"id": person["id"], "name": "大黄"}
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        card = body["person"]
        self.assertEqual("大黄", card["name"])
        self.assertIn("阿黄", card["aliases"])

        # 旧名字仍然可达（别名也能命中同一张卡）。
        status, payload = self._get_json("/api/people?query=%E9%98%BF%E9%BB%84")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["count"])
        self.assertEqual("大黄", payload["people"][0]["name"])

        # 加 / 删别名。
        status, body = self._post_json(
            "/api/people/update",
            {"id": person["id"], "aliases_add": ["小黄"], "aliases_remove": ["美美"]},
        )
        self.assertEqual(200, status)
        self.assertIn("小黄", body["person"]["aliases"])

    def test_merge_folds_mentions_and_list(self) -> None:
        target = self._add_person(name="小明", uin="1111111111")
        source = self._add_person(name="明明", uin="2222222222")

        # 合并前给「明明」记一条 mention：mention 永不移动，合并后由家族折算。
        db = Database(self.config.db_path)
        try:
            self.assertTrue(
                record_mention(db, source["id"], "session", "sess-people-1", "明明")
            )
            db.conn.commit()
        finally:
            db.close()

        # target 用 uin 当选择器，顺带覆盖 resolve_person_selector 的 uin 路径。
        status, body = self._post_json(
            "/api/people/merge", {"source": source["id"], "target": "1111111111"}
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["merged"])
        merged = body["person"]
        self.assertEqual(target["id"], merged["id"])
        self.assertEqual("小明", merged["name"])
        self.assertIn("明明", merged["aliases"])
        self.assertEqual(1, merged["mentions"])
        # merged_from 只以掩码形态出网；raw id 是回传句柄，仅存于卡片主键。
        self.assertIn("person:qq:222****2222", merged["merged_from"])
        self.assertNotIn(source["id"], merged["merged_from"])

        status, payload = self._get_json("/api/people?query=%E5%B0%8F%E6%98%8E")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["count"])
        card = payload["people"][0]
        self.assertEqual(target["id"], card["id"])
        self.assertEqual(1, card["mentions"])
        self.assertIn("person:qq:222****2222", card["merged_from"])
        self.assertNotIn(source["id"], card["merged_from"])

        # 同一张卡合并返回 merged=False，不报错。
        status, body = self._post_json(
            "/api/people/merge", {"source": "小明", "target": "1111111111"}
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertFalse(body["merged"])

    def test_add_requires_name_or_uin(self) -> None:
        status, body = self._post_json("/api/people/add", {})
        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid_request", body["error"])

        status, body = self._post_json("/api/people/add", {"name": "   "})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])

        status, body = self._post_json("/api/people/add", {"aliases": ["不是字符串"]})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])

    def test_update_unknown_id_returns_404(self) -> None:
        status, body = self._post_json(
            "/api/people/update", {"id": "person:qq:9999999999", "name": "不存在"}
        )
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])
        self.assertEqual("not_found", body["error"])

    def test_merge_unknown_selector_returns_400(self) -> None:
        status, body = self._post_json(
            "/api/people/merge", {"source": "查无此人", "target": "也没有"}
        )
        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid_request", body["error"])

        # 选择器是 uin 时，错误信息里不允许出现 uin 原文。
        ghost_uin = "9988776655"
        status, _headers, raw = self._post(
            "/api/people/merge", {"source": ghost_uin, "target": "小美"}
        )
        self.assertEqual(400, status)
        self.assertNotIn(ghost_uin.encode(), raw)

        # 缺 source / target 也是 400。
        status, body = self._post_json("/api/people/merge", {"source": "小美"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", body["error"])


if __name__ == "__main__":
    unittest.main()
