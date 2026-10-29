"""Offline tests for the QQ Zone client: g_tk, cookies, response parsing.

No network, no real cookies: every fixture uses synthetic values, and the
cookie file lives in a temp data dir.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.qzone.client import (
    MAX_CONTENT_CHARS,
    QzoneClient,
    QzoneError,
    g_tk_from_skey,
    parse_cookie_string,
    parse_emotions_payload,
    parse_feeds_payload,
    parse_response_body,
)


def make_config(tmp: Path, **overrides):
    data = tmp / "data"
    lines = [
        f'[app]\ndata_dir = "{data.as_posix()}"',
        '[qzone]',
        'cookie_path = "qzone_cookies.json"',
        'owner_uin = ""',
    ]
    for key, value in overrides.items():
        lines.append(f'{key} = "{value}"')
    path = tmp / "qzone.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config = load_config("low", path)
    config.ensure_dirs()
    return config


class GtkTests(unittest.TestCase):
    def test_known_vectors(self) -> None:
        # Hand-computed from the standard 5381 algorithm: h = h*33 + code.
        self.assertEqual(g_tk_from_skey(""), 5381)
        # 'a' (97): 5381*33 + 97 = 177670
        self.assertEqual(g_tk_from_skey("a"), 177670)
        # 'b': 177670*33 + 98 = 5863208; 'c': 5863208*33 + 99 = 193485963
        self.assertEqual(g_tk_from_skey("abc"), 193485963)

    def test_result_fits_31_bits(self) -> None:
        value = g_tk_from_skey("@1234567890abcdefghij")
        self.assertGreaterEqual(value, 0)
        self.assertLess(value, 0x7FFFFFFF)


class CookieTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)
        # Keep env overrides from leaking between tests.
        self._env_backup = {
            name: os.environ.get(name)
            for name in ("QZONE_UIN", "QZONE_SKEY", "QZONE_P_SKEY", "QZONE_COOKIE")
        }
        for name in self._env_backup:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._env_backup.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def test_save_and_load_roundtrip(self) -> None:
        client = QzoneClient(self.config, cookies={"uin": "10000", "skey": "s-1", "p_skey": "p-1"})
        target = client.save_cookies()
        self.assertEqual(target, client.cookie_path)
        fresh = QzoneClient(self.config)
        loaded = fresh.load_cookies()
        self.assertEqual(loaded["uin"], "10000")
        self.assertEqual(fresh.cookies["skey"], "s-1")
        # Values never land in error messages or reports.
        report = json.dumps(fresh.cookie_status())
        self.assertNotIn("s-1", report)
        self.assertNotIn("p-1", report)

    def test_env_overrides_file(self) -> None:
        QzoneClient(self.config, cookies={"uin": "10000", "skey": "file-key"}).save_cookies()
        os.environ["QZONE_SKEY"] = "env-key"
        client = QzoneClient.from_config(self.config)
        self.assertEqual(client.cookies["skey"], "env-key")
        self.assertEqual(client.cookies["uin"], "10000")

    def test_cookie_string_login_requires_uin_and_skey(self) -> None:
        client = QzoneClient(self.config)
        with self.assertRaises(QzoneError):
            client.login_with_cookie("uin=10000")  # no skey
        with self.assertRaises(QzoneError):
            client.login_with_cookie("nonsense without equals")
        client.login_with_cookie("uin=10000; skey=s-2; p_skey=p-2")
        self.assertTrue(client.cookie_status()["full_read"])

    def test_cookie_status_missing_fields(self) -> None:
        client = QzoneClient(self.config, cookies={"uin": "10000"})
        status = client.cookie_status()
        self.assertIn("skey", status["missing"])
        self.assertFalse(status["full_read"])
        self.assertFalse(status["g_tk_ready"])

    def test_gtk_uses_p_skey_and_falls_back_to_skey(self) -> None:
        client = QzoneClient(self.config, cookies={"skey": "abc"})
        self.assertEqual(client.g_tk(), g_tk_from_skey("abc"))
        client.cookies["p_skey"] = "p"
        self.assertEqual(client.g_tk(), g_tk_from_skey("p"))
        empty = QzoneClient(self.config)
        with self.assertRaises(QzoneError):
            empty.g_tk()

    def test_unsafe_cookie_pairs_dropped(self) -> None:
        client = QzoneClient(
            self.config,
            cookies={"uin": "10000", "skey": "bad\nvalue", "ok": "fine"},
        )
        self.assertNotIn("skey", client.cookies)
        self.assertEqual(client.cookies.get("ok"), "fine")
        self.assertEqual(parse_cookie_string("a=1;\nb=2"), {"a": "1", "b": "2"})


class ParsingTests(unittest.TestCase):
    def test_normal_feed_payload(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "feedid": "f1",
                        "uin": "20001",
                        "nickname": "好友甲",
                        "content": "今天天气不错",
                        "abstime": 1761200000,
                        "pics": [{"url1": "https://img.example/1.png"}],
                    },
                    {
                        "feedinfo": {"feedid": "f2", "uin": "10000", "nickname": "主人"},
                        "nickname": "主人",
                        "content": "自己发的",
                    },
                ]
            },
        }
        items = parse_feeds_payload(payload)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["external_id"], "f1")
        self.assertEqual(items[0]["author_name"], "好友甲")
        self.assertEqual(items[0]["published_at"], 1761200000.0)
        self.assertEqual(items[0]["media"], ["https://img.example/1.png"])
        self.assertEqual(items[1]["external_id"], "f2")

    def test_bare_list_and_alt_keys(self) -> None:
        payload = [{"key": "x9", "summary": "换个字段名", "pubtime": 1761200001}]
        items = parse_feeds_payload(payload)
        self.assertEqual(items[0]["external_id"], "x9")
        self.assertEqual(items[0]["content"], "换个字段名")

    def test_missing_fields_degrade_never_raise(self) -> None:
        items = parse_feeds_payload({"code": 0, "data": {"list": [{"nickname": "无 id"}, 42, None]}})
        self.assertEqual(items, [])

    def test_overlong_content_truncated(self) -> None:
        payload = {"code": 0, "data": {"list": [{"feedid": "long", "content": "字" * 9000}]}}
        items = parse_feeds_payload(payload)
        self.assertLessEqual(len(items[0]["content"]), MAX_CONTENT_CHARS + 1)
        self.assertTrue(items[0]["content"].endswith("…"))

    def test_emotions_payload(self) -> None:
        payload = {
            "code": 0,
            "msglist": [
                {"tid": "t1", "content": "第一条说说", "created_time": 1761200000},
                {"tid": "t2", "content": "第二条", "pic": {"data": [{"url1": "u"}]}},
            ],
        }
        items = parse_emotions_payload(payload)
        self.assertEqual([i["external_id"] for i in items], ["t1", "t2"])
        self.assertEqual(items[1]["media"], ["u"])

    def test_feeds_embedded_comments_extracted(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "feedid": "f1",
                        "uin": "20001",
                        "nickname": "好友甲",
                        "content": "带评论的动态",
                        "commentlist": [
                            {"name": "评论者一", "content": "沙发"},
                            {"uin": 30002, "content": "板凳"},
                            {"name": "空文本", "content": "   "},
                            {"name": "第三条", "message": "换个字段名"},
                            {"name": "评论者四", "content": "第四条被截掉"},
                        ],
                    }
                ]
            },
        }
        items = parse_feeds_payload(payload, comments_limit=3)
        self.assertEqual(
            items[0]["comments"],
            [
                {"author": "评论者一", "text": "沙发"},
                {"author": "30002", "text": "板凳"},
                {"author": "第三条", "text": "换个字段名"},
            ],
        )

    def test_feedinfo_level_comments_found(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "vFeeds": [
                    {
                        "feedinfo": {"feedid": "vf1", "content": "嵌套动态"},
                        "commentlist": [{"nick": "路人", "content": "顶"}],
                    }
                ]
            },
        }
        items = parse_feeds_payload(payload)
        self.assertEqual(
            items[0]["comments"], [{"author": "路人", "text": "顶"}]
        )

    def test_emotions_embedded_comments_truncated(self) -> None:
        payload = {
            "code": 0,
            "msglist": [
                {
                    "tid": "t1",
                    "content": "自己的说说",
                    "commentlist": [{"name": "好友乙", "content": "字" * 300}],
                }
            ],
        }
        items = parse_emotions_payload(payload)
        comments = items[0]["comments"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "好友乙")
        self.assertLessEqual(len(comments[0]["text"]), 100)
        self.assertTrue(comments[0]["text"].endswith("…"))

    def test_embedded_comments_limit_zero_disables(self) -> None:
        payload = {
            "code": 0,
            "msglist": [
                {"tid": "t1", "content": "x", "commentlist": [{"name": "a", "content": "b"}]}
            ],
        }
        items = parse_emotions_payload(payload, comments_limit=0)
        self.assertEqual(items[0]["comments"], [])

    def test_embedded_comments_default_caps_at_five(self) -> None:
        payload = {
            "code": 0,
            "msglist": [
                {
                    "tid": "t1",
                    "content": "x",
                    "commentlist": [
                        {"name": f"u{i}", "content": "c"} for i in range(9)
                    ],
                }
            ],
        }
        items = parse_emotions_payload(payload)
        self.assertEqual(len(items[0]["comments"]), 5)

    def test_embedded_comments_absent_is_empty(self) -> None:
        payload = {"code": 0, "msglist": [{"tid": "t1", "content": "x"}]}
        items = parse_emotions_payload(payload)
        self.assertEqual(items[0]["comments"], [])

    def test_jsonp_body_unwrapped(self) -> None:
        inner = json.dumps({"code": 0, "data": {"msglist": [{"tid": "t1", "content": "hi"}]}})
        payload = parse_response_body(f"_preloadCallback({inner});")
        items = parse_emotions_payload(payload)
        self.assertEqual(items[0]["content"], "hi")

    def test_risk_control_envelope_raises_login_required(self) -> None:
        with self.assertRaises(QzoneError) as ctx:
            parse_response_body(b'{"code":-3000,"subcode":-3000,"message":"no permission"}')
        self.assertTrue(ctx.exception.login_required)

    def test_html_login_wall_raises(self) -> None:
        with self.assertRaises(QzoneError) as ctx:
            parse_response_body(b"<html><body>login</body></html>")
        self.assertTrue(ctx.exception.login_required)

    def test_empty_and_invalid_bodies(self) -> None:
        with self.assertRaises(QzoneError):
            parse_response_body(b"   ")
        with self.assertRaises(QzoneError):
            parse_response_body(b"not json at all")


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_offline_report_has_no_cookie_values(self) -> None:
        client = QzoneClient(self.config, cookies={"uin": "10000", "skey": "sec", "p_skey": "sec2"})
        report = client.doctor()
        self.assertFalse(report["online_checked"])
        self.assertTrue(report["ok"])
        self.assertNotIn("sec", json.dumps(report))

    def test_offline_report_flags_missing(self) -> None:
        client = QzoneClient(self.config, cookies={"uin": "10000"})
        report = client.doctor()
        self.assertFalse(report["ok"])
        self.assertIn("skey", report["cookie"]["missing"])

    def test_online_probe_uses_emotions_not_feeds(self) -> None:
        # 登录判定只认自己的说说端点；好友动态端点抖动不影响 ok。
        client = QzoneClient(
            self.config, cookies={"uin": "10000", "skey": "sec", "p_skey": "sec2"}
        )
        client.fetch_emotions = lambda count=None: []  # type: ignore[assignment]

        def broken_feeds(count=None):
            raise QzoneError("qzone api error -4003: 非法操作", code=-4003)

        client.fetch_feeds = broken_feeds  # type: ignore[assignment]
        report = client.doctor(online=True)
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["probe"]["ok"])
        self.assertEqual(0, report["probe"]["items"])
        self.assertFalse(report["feeds"]["ok"])
        self.assertEqual(-4003, report["feeds"]["code"])

    def test_online_probe_flags_dead_session(self) -> None:
        client = QzoneClient(
            self.config, cookies={"uin": "10000", "skey": "sec", "p_skey": "sec2"}
        )

        def dead(count=None):
            raise QzoneError(
                "qzone api error -3000: 请先登录空间", code=-3000, login_required=True
            )

        client.fetch_emotions = dead  # type: ignore[assignment]
        report = client.doctor(online=True)
        self.assertFalse(report["ok"])
        self.assertTrue(report["probe"]["login_required"])
        self.assertNotIn("sec", json.dumps(report))


class EmotionTargetTests(unittest.TestCase):
    """``fetch_emotions(uin=...)`` 能拉好友的说说（聚合 feed 的替代读法）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = make_config(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _client(self, payload):
        client = QzoneClient(
            self.config, cookies={"uin": "10000", "skey": "sec", "p_skey": "sec2"}
        )
        calls: list[dict] = []

        def fake_request(url, params=None, *a, **k):
            calls.append(dict(params or {}))
            return payload

        client.request = fake_request  # type: ignore[assignment]
        return client, calls

    def test_friend_uin_becomes_the_request_target(self) -> None:
        payload = {"code": 0, "msglist": [{"tid": "T1", "content": "好友的说说"}]}
        client, calls = self._client(payload)
        items = client.fetch_emotions(5, uin="20001")
        self.assertEqual("20001", calls[0]["uin"])
        self.assertEqual("5", calls[0]["num"])
        self.assertEqual(1, len(items))
        # 载荷没带作者 uin 时按请求的 uin 补齐，避免被当成主人自己的动态。
        self.assertEqual("20001", items[0]["author_uin"])

    def test_default_still_targets_the_cookie_account(self) -> None:
        client, calls = self._client({"code": 0, "msglist": []})
        client.fetch_emotions(1)
        self.assertEqual("10000", calls[0]["uin"])

    def test_payload_author_wins_over_the_backfill(self) -> None:
        payload = {
            "code": 0,
            "msglist": [{"tid": "T2", "content": "带作者的", "uin": "29999"}],
        }
        client, _calls = self._client(payload)
        items = client.fetch_emotions(1, uin="20001")
        self.assertEqual("29999", items[0]["author_uin"])


if __name__ == "__main__":
    unittest.main()
