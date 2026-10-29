"""Offline tests for the watch-side read APIs (T-0073).

``video_cid`` / ``video_subtitles`` / ``video_snapshot`` / ``related_videos``
are exercised against a local route table via a fake opener: no network
access, no real cookies, and the snapshot sprite JPEG itself is never
fetched (only its URL and frame index metadata are returned).
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any

from fatfish.bilibili import BilibiliClient, BilibiliError
from fatfish.config import load_config

BV = "BV1xx411c7mD"

VIEW_PATH = "/x/web-interface/view"
PLAYER_V2_PATH = "/x/player/v2"
VIDEOSHOT_PATH = "/x/player/videoshot"
RELATED_PATH = "/x/web-interface/archive/related"
CC_FILE_PATH = "/x/cc-file.json"
AI_FILE_PATH = "/x/ai-file.json"
SNAPSHOT_URL = "https://i0.hdslb.com/bfs/videoshot/sprite.jpg"


def _envelope(data: Any, code: int = 0, message: str = "ok") -> bytes:
    return json.dumps({"code": code, "message": message, "data": data}).encode("utf-8")


VIEW_DATA: dict[str, Any] = {
    "bvid": BV,
    "cid": 101,
    "pages": [
        {"page": 1, "cid": 101, "part": "第一P", "duration": 60},
        {"page": 2, "cid": 202, "part": "第二P", "duration": 45},
    ],
}

CC_TRACK = {
    "lan": "zh-CN",
    "lan_doc": "中文（人工字幕）",
    "subtitle_url": f"//aisubtitle.hdslb.com{CC_FILE_PATH}",
}
AI_TRACK = {
    "lan": "ai-zh",
    "lan_doc": "中文（自动生成）",
    "subtitle_url": f"//aisubtitle.hdslb.com{AI_FILE_PATH}",
}
CC_BODY = {
    "body": [
        {"from": 1.0, "to": 2.5, "content": "大家好"},
        {"from": 2.5, "to": 4.0, "content": "今天聊一聊鲸鱼"},
    ]
}
SNAPSHOT_DATA: dict[str, Any] = {
    "img_x_len": 10,
    "img_y_len": 10,
    "img_x_size": 160,
    "img_y_size": 90,
    "image": [SNAPSHOT_URL],
    "index": [1130, 5649, 11435],
}

RELATED_ITEMS: list[Any] = [
    {
        "bvid": "BV1rel1",
        "title": "相关一",
        "owner": {"name": "UP甲"},
        "desc": "描述一",
        "pubdate": 1700000100,
        "stat": {"view": 5000},
    },
    None,
    {"bvid": "", "title": "没有bvid"},
    {
        "bvid": "BV2rel2",
        "title": "相关二",
        "owner": {"name": "UP乙"},
        "desc": "描述二",
        "pubdate": 1700000200,
        "stat": {"view": 900},
    },
]


class _FakeResponse:
    def __init__(self, body: bytes, url: str):
        self._body = body
        self._url = url
        self.headers: dict[str, str] = {}

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOpener:
    """Offline opener dispatching on the URL path; records every request."""

    def __init__(self, routes: dict[str, Any]):
        self.routes = dict(routes)
        self.requested: list[str] = []

    def open(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        url = str(request.full_url)
        self.requested.append(url)
        path = urllib.parse.urlparse(url).path
        if path not in self.routes:
            raise AssertionError(f"unexpected request in offline test: {url}")
        payload = self.routes[path]
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload, url)


class WatchClientTestCase(unittest.TestCase):
    def make_client(
        self,
        routes: dict[str, Any] | None = None,
        *,
        bilibili_extra: str = "",
    ) -> tuple[BilibiliClient, _FakeOpener]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = (Path(tmp.name) / "data").as_posix()
        toml = f"""
[app]
data_dir = "{data_dir}"

[bilibili]
enabled = true
{bilibili_extra}
"""
        path = Path(tmp.name) / "watch.toml"
        path.write_text(toml, encoding="utf-8")
        client = BilibiliClient(load_config("low", path))
        client._throttle = lambda: None
        opener = _FakeOpener(routes or {})
        client._opener = opener
        return client, opener

    @staticmethod
    def default_routes(**overrides: Any) -> dict[str, Any]:
        routes: dict[str, Any] = {
            VIEW_PATH: _envelope(VIEW_DATA),
            PLAYER_V2_PATH: _envelope({"subtitle": {"subtitles": [AI_TRACK, CC_TRACK]}}),
            CC_FILE_PATH: json.dumps(CC_BODY, ensure_ascii=False).encode("utf-8"),
            AI_FILE_PATH: json.dumps(CC_BODY, ensure_ascii=False).encode("utf-8"),
            RELATED_PATH: _envelope(RELATED_ITEMS),
            VIDEOSHOT_PATH: _envelope(SNAPSHOT_DATA),
        }
        routes.update(overrides)
        return routes


class ViewRegressionTests(WatchClientTestCase):
    def test_view_still_normalized_without_pages(self):
        client, opener = self.make_client(self.default_routes())
        video = client.view(BV)
        self.assertEqual(video["bvid"], BV)
        self.assertNotIn("pages", video)
        self.assertIn("hot_score", video)
        self.assertEqual(len(opener.requested), 1)


class VideoCidTests(WatchClientTestCase):
    def test_video_cid_returns_pages_with_one_request(self):
        client, opener = self.make_client(self.default_routes())
        cid, pages = client.video_cid(BV)
        self.assertEqual(cid, 101)
        self.assertEqual([page["cid"] for page in pages], [101, 202])
        # Cheapest possible: exactly one raw view request, no extra fetch.
        self.assertEqual(len(opener.requested), 1)
        self.assertIn(VIEW_PATH, opener.requested[0])
        self.assertIn(f"bvid={BV}", opener.requested[0])

    def test_video_cid_page_two(self):
        client, _ = self.make_client(self.default_routes())
        cid, _pages = client.video_cid(BV, page=2)
        self.assertEqual(cid, 202)

    def test_video_cid_out_of_range_page_raises(self):
        client, _ = self.make_client(self.default_routes())
        with self.assertRaises(BilibiliError):
            client.video_cid(BV, page=3)

    def test_video_cid_single_p_falls_back_to_top_level_cid(self):
        client, _ = self.make_client({VIEW_PATH: _envelope({"bvid": BV, "cid": 777})})
        cid, pages = client.video_cid(BV)
        self.assertEqual(cid, 777)
        self.assertEqual(pages, [{"page": 1, "cid": 777}])

    def test_video_cid_without_any_cid_raises(self):
        client, _ = self.make_client({VIEW_PATH: _envelope({"bvid": BV})})
        with self.assertRaises(BilibiliError):
            client.video_cid(BV)

    def test_video_cid_empty_bvid_raises_without_request(self):
        client, opener = self.make_client(self.default_routes())
        with self.assertRaises(BilibiliError):
            client.video_cid("   ")
        self.assertEqual(opener.requested, [])


class VideoSubtitlesTests(WatchClientTestCase):
    def test_cc_track_preferred_and_protocol_relative_url_completed(self):
        client, opener = self.make_client(self.default_routes())
        result = client.video_subtitles(BV)
        self.assertEqual(result["source"], "cc")
        self.assertEqual(result["lang"], "zh-CN")
        self.assertEqual(
            result["lines"], ["[1.0-2.5] 大家好", "[2.5-4.0] 今天聊一聊鲸鱼"]
        )
        # "//host/path" must be completed to a usable https URL; the ai track
        # is never downloaded when a manual cc track exists.
        self.assertIn(f"https://aisubtitle.hdslb.com{CC_FILE_PATH}", opener.requested)
        self.assertFalse(any(AI_FILE_PATH in url for url in opener.requested))
        # cid lookup + player/v2 + subtitle file: exactly three requests.
        self.assertEqual(len(opener.requested), 3)
        self.assertIn(VIEW_PATH, opener.requested[0])
        self.assertIn(PLAYER_V2_PATH, opener.requested[1])

    def test_ai_track_used_when_no_cc(self):
        routes = self.default_routes(
            **{PLAYER_V2_PATH: _envelope({"subtitle": {"subtitles": [AI_TRACK]}})}
        )
        client, _ = self.make_client(routes)
        result = client.video_subtitles(BV)
        self.assertEqual(result["source"], "ai")
        self.assertEqual(result["lang"], "ai-zh")
        self.assertEqual(len(result["lines"]), 2)

    def test_no_tracks_is_none(self):
        routes = self.default_routes(
            **{PLAYER_V2_PATH: _envelope({"subtitle": {"subtitles": []}})}
        )
        client, _ = self.make_client(routes)
        self.assertEqual(
            client.video_subtitles(BV), {"lines": [], "source": "none", "lang": ""}
        )

    def test_track_without_url_is_skipped(self):
        routes = self.default_routes(
            **{
                PLAYER_V2_PATH: _envelope(
                    {"subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": ""}]}}
                )
            }
        )
        client, _ = self.make_client(routes)
        self.assertEqual(client.video_subtitles(BV)["source"], "none")

    def test_player_v2_risk_control_is_none_not_raise(self):
        routes = self.default_routes(
            **{
                PLAYER_V2_PATH: _envelope(
                    None, code=-412, message="请求被风控"
                )
            }
        )
        client, _ = self.make_client(routes)
        self.assertEqual(
            client.video_subtitles(BV), {"lines": [], "source": "none", "lang": ""}
        )

    def test_subtitle_download_failure_is_none(self):
        routes = self.default_routes(**{CC_FILE_PATH: b"<html>not json</html>"})
        client, _ = self.make_client(routes)
        self.assertEqual(client.video_subtitles(BV)["source"], "none")

    def test_subtitle_payload_without_body_is_none(self):
        routes = self.default_routes(
            **{CC_FILE_PATH: json.dumps({"body": None}).encode("utf-8")}
        )
        client, _ = self.make_client(routes)
        self.assertEqual(client.video_subtitles(BV)["source"], "none")

    def test_total_chars_capped_by_config(self):
        body = {
            "body": [
                {"from": 0, "to": 1, "content": "一二三四五六七八九十甲乙丙丁"},
                {"from": 1, "to": 2, "content": "第二句"},
            ]
        }
        routes = self.default_routes(
            **{CC_FILE_PATH: json.dumps(body, ensure_ascii=False).encode("utf-8")}
        )
        client, _ = self.make_client(routes, bilibili_extra="subtitle_max_chars = 12")
        result = client.video_subtitles(BV)
        self.assertEqual(result["source"], "cc")
        self.assertEqual(len(result["lines"]), 1)
        self.assertTrue(result["lines"][0].endswith("…"))
        self.assertLessEqual(sum(len(line) for line in result["lines"]), 12)

    def test_cid_param_skips_view_request(self):
        client, opener = self.make_client(self.default_routes())
        result = client.video_subtitles(BV, cid=101)
        self.assertEqual(result["source"], "cc")
        self.assertFalse(any(VIEW_PATH in url for url in opener.requested))
        player_url = next(u for u in opener.requested if PLAYER_V2_PATH in u)
        self.assertIn("cid=101", player_url)

    def test_page_selects_multi_p_cid(self):
        client, opener = self.make_client(self.default_routes())
        result = client.video_subtitles(BV, page=2)
        self.assertEqual(result["source"], "cc")
        player_url = next(u for u in opener.requested if PLAYER_V2_PATH in u)
        self.assertIn("cid=202", player_url)

    def test_empty_bvid_is_none_without_request(self):
        client, opener = self.make_client(self.default_routes())
        self.assertEqual(
            client.video_subtitles("   "),
            {"lines": [], "source": "none", "lang": ""},
        )
        self.assertEqual(opener.requested, [])

    def test_content_tags_and_newlines_collapsed(self):
        body = {"body": [{"from": 0, "to": 1, "content": "<b>加粗</b>\n换行 &amp; 符号"}]}
        routes = self.default_routes(
            **{CC_FILE_PATH: json.dumps(body, ensure_ascii=False).encode("utf-8")}
        )
        client, _ = self.make_client(routes)
        self.assertEqual(
            client.video_subtitles(BV)["lines"], ["[0.0-1.0] 加粗 换行 & 符号"]
        )

    def test_empty_content_lines_skipped(self):
        body = {
            "body": [
                {"from": 0, "to": 1, "content": "   "},
                {"from": 1, "to": 2, "content": "有内容"},
            ]
        }
        routes = self.default_routes(
            **{CC_FILE_PATH: json.dumps(body, ensure_ascii=False).encode("utf-8")}
        )
        client, _ = self.make_client(routes)
        self.assertEqual(client.video_subtitles(BV)["lines"], ["[1.0-2.0] 有内容"])


class VideoSnapshotTests(WatchClientTestCase):
    def test_snapshot_metadata_without_downloading_image(self):
        client, opener = self.make_client(self.default_routes())
        snapshot = client.video_snapshot(BV)
        self.assertEqual(
            snapshot,
            {
                "image_url": SNAPSHOT_URL,
                "img_x_len": 10,
                "img_y_len": 10,
                "index": [1130, 5649, 11435],
            },
        )
        videoshot_url = next(u for u in opener.requested if VIDEOSHOT_PATH in u)
        self.assertIn("cid=101", videoshot_url)
        self.assertIn("index=1", videoshot_url)
        # Only API requests were made; the sprite JPEG itself is never fetched.
        self.assertTrue(all("i0.hdslb.com" not in url for url in opener.requested))

    def test_snapshot_none_when_image_missing(self):
        for data in (
            {"img_x_len": 10, "img_y_len": 10, "index": [1]},
            {"image": [], "index": [1]},
            None,
        ):
            routes = self.default_routes(**{VIDEOSHOT_PATH: _envelope(data)})
            client, _ = self.make_client(routes)
            self.assertIsNone(client.video_snapshot(BV))

    def test_snapshot_none_on_risk_control(self):
        routes = self.default_routes(
            **{VIDEOSHOT_PATH: _envelope(None, code=-412, message="请求被风控")}
        )
        client, _ = self.make_client(routes)
        self.assertIsNone(client.video_snapshot(BV))

    def test_snapshot_none_on_cid_failure(self):
        routes = {VIEW_PATH: _envelope(None, code=-404, message="啥都木有")}
        client, _ = self.make_client(routes)
        self.assertIsNone(client.video_snapshot(BV))

    def test_snapshot_empty_bvid_is_none_without_request(self):
        client, opener = self.make_client(self.default_routes())
        self.assertIsNone(client.video_snapshot("   "))
        self.assertEqual(opener.requested, [])

    def test_snapshot_index_missing_is_empty_list(self):
        data = {"image": [SNAPSHOT_URL], "img_x_len": 10, "img_y_len": 10}
        routes = self.default_routes(**{VIDEOSHOT_PATH: _envelope(data)})
        client, _ = self.make_client(routes)
        snapshot = client.video_snapshot(BV)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["index"], [])


class RelatedVideosTests(WatchClientTestCase):
    def test_related_normalizes_and_skips_junk(self):
        client, opener = self.make_client(self.default_routes())
        videos = client.related_videos(BV, limit=5)
        self.assertEqual([v["bvid"] for v in videos], ["BV1rel1", "BV2rel2"])
        first = videos[0]
        self.assertEqual(first["title"], "相关一")
        self.assertEqual(first["up"], "UP甲")
        self.assertEqual(first["desc"], "描述一")
        self.assertEqual(first["url"], "https://www.bilibili.com/video/BV1rel1")
        self.assertEqual(first["published_at"], 1700000100.0)
        self.assertGreater(first["hot_score"], 0)
        self.assertEqual(len(opener.requested), 1)
        self.assertIn(RELATED_PATH, opener.requested[0])
        self.assertIn(f"bvid={BV}", opener.requested[0])

    def test_related_caps_limit(self):
        client, _ = self.make_client(self.default_routes())
        self.assertEqual(len(client.related_videos(BV, limit=1)), 1)

    def test_related_empty_bvid_no_request(self):
        client, opener = self.make_client(self.default_routes())
        self.assertEqual(client.related_videos("  "), [])
        self.assertEqual(opener.requested, [])

    def test_related_empty_payload_is_empty(self):
        client, _ = self.make_client({RELATED_PATH: _envelope([])})
        self.assertEqual(client.related_videos(BV), [])

    def test_related_api_error_raises_bilibili_error(self):
        client, _ = self.make_client(
            {RELATED_PATH: _envelope(None, code=-412, message="请求被风控")}
        )
        with self.assertRaises(BilibiliError) as ctx:
            client.related_videos(BV)
        self.assertEqual(ctx.exception.code, -412)


if __name__ == "__main__":
    unittest.main()
