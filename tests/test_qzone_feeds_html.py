"""Offline tests for the feeds3_html_more parser (fatfish/qzone/feeds_html.py).

Every fixture here is synthetic: fake uins, fake ids, placeholder text. No
real response content ever enters the repository (the captured sample lives in
a gitignored temp path and is only used for structural research).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fatfish.config import load_config
from fatfish.qzone import feeds_html
from fatfish.qzone.client import QzoneClient, QzoneError, parse_response_body

TID_A = "a" * 24
TID_B = "b" * 24
UIN_OWNER = "10000"
UIN_FRIEND = "20001"


def synth_html(
    key: str,
    uin: str,
    nick: str = "",
    lines: tuple[str, ...] = ("合成动态正文",),
    pic_urls: tuple[str, ...] = (),
    *,
    omit_f_name: bool = False,
) -> str:
    """A feed entry HTML block with the observed structure, synthetic values."""
    nick_anchor = (
        "" if omit_f_name else f"<a target='_blank' class='f-name q_namecard '>{nick}</a>"
    )
    img_anchors = "".join(
        f"<a class='img-item ' data-cmd='qz_popup' href='https://s.example/{i}'>"
        f"<img src='{url}' style='height:420px;'></a>"
        for i, url in enumerate(pic_urls)
    )
    body = "<br>".join(lines)
    return (
        "<li class='f-single f-s-s' id='feed_{key}_syn'>"
        "<div class='f-single-head f-aside'>"
        "<div class='user-info'><div class='f-nick'>{nick_anchor}</div></div>"
        "</div>"
        "<div class='f-single-content f-wrap'>"
        "<div class='f-item f-s-i' data-key='{key}' data-functype='func_friend_feed'>"
        "<div class='f-info'>{body}</div>"
        "<div class='qz_summary wupfeed' id='sum_{key}'>"
        "<i class='none' name='feed_data' data-fkey='{key}' data-tid='{key}' "
        "data-uin='{uin}' data-abstime='1792000000' data-feedstype='100'></i>"
        "<div class='f-ct '><div class='txt-box '></div>"
        "<div class='img-box '>{img_anchors}</div></div>"
        "</div></div></div></li>"
    ).format(key=key, uin=uin, nick_anchor=nick_anchor, body=body, img_anchors=img_anchors)


def synth_item(key: str, uin: str, nick: str = "好友甲", **kwargs) -> dict:
    """Envelope entry in the observed shape: structured fields + html snippet."""
    html = synth_html(key, uin, **kwargs)
    item = {
        "ver": "1",
        "appid": "311",
        "key": key,
        "feedno": "1",
        "abstime": "1792000000",
        "uin": uin,
        "nickname": nick,
        "typeid": "0",
        "html": html,
    }
    if kwargs.get("omit_f_name"):
        item.pop("nickname")
    return item


def synth_body(items: list, *, code: int = 0, js_wrapped: bool = True) -> str:
    """The _Callback envelope: strict-JSON item literals inside a JS skeleton."""
    item_literal = ",".join(
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in items
    )
    head = (
        '_Callback({\n\t"code":%d,\n\t"subcode":0,\n\t"message":"",\n\t"default":0,\n'
        "\t\"data\":{\nmain:{attach:'',searchtype :'',hasMoreFeeds:true,"
        "pagenum:'1',begintime:'2026-10-24'},\ndata:[%s]\n}}\n);" % (code, item_literal)
    )
    if js_wrapped:
        return head
    return head[len("_Callback(") : -len(");")]


class LooseJsonTests(unittest.TestCase):
    def test_strict_json_fast_path(self) -> None:
        self.assertEqual(feeds_html.loose_json_loads('{"a": 1}'), {"a": 1})

    def test_bare_keys_single_quotes_undefined(self) -> None:
        literal = "{a:1, b :'x', c:true, d:undefined, e:[1,2,], f:{g:null, h:'it\\'s'}}"
        value = feeds_html.loose_json_loads(literal)
        self.assertEqual(value["a"], 1)
        self.assertEqual(value["b"], "x")
        self.assertIs(value["c"], True)
        self.assertIsNone(value["d"])
        self.assertEqual(value["e"], [1, 2])  # trailing comma tolerated
        self.assertEqual(value["f"]["h"], "it's")

    def test_double_quotes_inside_single_quoted_string(self) -> None:
        value = feeds_html.loose_json_loads("{a:'say \"hi\" now'}")
        self.assertEqual(value["a"], 'say "hi" now')

    def test_broken_literal_raises(self) -> None:
        with self.assertRaises(feeds_html.Feeds3ParseError):
            feeds_html.loose_json_loads("{a:unknown_bare_value}")


class ParseFeeds3Tests(unittest.TestCase):
    def test_full_pipeline_jsonp_html_items(self) -> None:
        body = synth_body(
            [
                synth_item(
                    TID_A,
                    UIN_FRIEND,
                    nick="好友甲",
                    lines=("第一行", "第二行"),
                    pic_urls=("https://pic.example/a1.jpg", "https://pic.example/a2.jpg"),
                ),
                synth_item(TID_B, UIN_OWNER, nick="主人"),
                "null",  # trailing placeholder, as seen in the wild
            ]
        )
        items = feeds_html.parse_feeds3(body)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first["external_id"], TID_A)
        self.assertEqual(first["author_uin"], UIN_FRIEND)
        self.assertEqual(first["author_name"], "好友甲")
        self.assertEqual(first["content"], "第一行\n第二行")
        self.assertEqual(
            first["media"], ["https://pic.example/a1.jpg", "https://pic.example/a2.jpg"]
        )
        self.assertEqual(first["published_at"], 1792000000.0)
        self.assertEqual(first["url"], f"https://user.qzone.qq.com/{UIN_FRIEND}")
        # raw keeps the structured fields but drops the bulky html snippet.
        self.assertNotIn("html", first["raw"])
        self.assertEqual(first["raw"]["key"], TID_A)
        second = items[1]
        self.assertEqual(second["external_id"], TID_B)
        self.assertEqual(second["media"], [])

    def test_bare_body_without_jsonp_wrapper(self) -> None:
        items = feeds_html.parse_feeds3(
            synth_body([synth_item(TID_A, UIN_FRIEND)], js_wrapped=False)
        )
        self.assertEqual([i["external_id"] for i in items], [TID_A])

    def test_html_fallbacks_for_missing_fields(self) -> None:
        # No structured nickname/uin/abstime: the HTML snippet must supply them.
        item = {
            "key": TID_B,
            "html": synth_html(
                TID_B, "0", nick="HTML昵称", omit_f_name=True
            ).replace("data-uin='0'", "data-uin='20002'").replace("data-abstime='1792000000'", "data-abstime='1792000100'"),
        }
        items = feeds_html.parse_feeds3(synth_body([item]))
        self.assertEqual(len(items), 1)
        entry = items[0]
        self.assertEqual(entry["author_uin"], "20002")
        self.assertEqual(entry["published_at"], 1792000100.0)
        self.assertEqual(entry["author_name"], "")  # no nickname anywhere: degrade

    def test_ads_and_idless_items_skipped(self) -> None:
        ad = synth_item("advertisement_outlink_0", "0", nick="广告")
        ad["uin"] = "0"
        no_id = {"nickname": "无 id", "html": synth_html("", UIN_FRIEND)}
        items = feeds_html.parse_feeds3(
            synth_body([ad, no_id, synth_item(TID_A, UIN_FRIEND)])
        )
        self.assertEqual([i["external_id"] for i in items], [TID_A])

    def test_empty_item_list_is_legitimate(self) -> None:
        self.assertEqual(feeds_html.parse_feeds3(synth_body([])), [])

    def test_unusable_bodies_raise_feeds3_error(self) -> None:
        with self.assertRaises(feeds_html.Feeds3ParseError):
            feeds_html.parse_feeds3("<html><body>login wall</body></html>")
        with self.assertRaises(feeds_html.Feeds3ParseError):
            feeds_html.parse_feeds3(synth_body([], code=-4003))
        with self.assertRaises(feeds_html.Feeds3ParseError):
            feeds_html.parse_feeds3("_Callback({code:0,data:{main:{}}});")
        with self.assertRaises(feeds_html.Feeds3ParseError):
            feeds_html.parse_feeds3("_Callback({broken")
        with self.assertRaises(feeds_html.Feeds3ParseError):
            feeds_html.parse_feeds3("   ")


class ClientIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        data = self.tmp / "data"
        toml = (
            f'[app]\ndata_dir = "{data.as_posix()}"\n'
            '[qzone]\ncookie_path = "qzone_cookies.json"\n'
        )
        path = self.tmp / "qzone.toml"
        path.write_text(toml, encoding="utf-8")
        self.config = load_config("low", path)
        self.config.ensure_dirs()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _client(self) -> QzoneClient:
        return QzoneClient(
            self.config, cookies={"uin": UIN_OWNER, "skey": "sec", "p_skey": "sec2"}
        )

    def test_parse_response_body_accepts_feeds3_js_literal(self) -> None:
        payload = parse_response_body(synth_body([synth_item(TID_A, UIN_FRIEND)]))
        items = feeds_html.parse_feeds3_payload(payload)
        self.assertEqual([i["external_id"] for i in items], [TID_A])

    def test_parse_response_body_error_envelope_still_raises(self) -> None:
        with self.assertRaises(QzoneError):
            parse_response_body("_Callback({code:-4003,subcode:0,message:'no'});")

    def _stub_request(self, client: QzoneClient, responses: dict[str, object]) -> list[str]:
        """Replace request() with a URL-dispatching fake; returns called URLs."""
        called: list[str] = []

        def fake_request(url, params=None):
            called.append(url)
            for prefix, response in responses.items():
                if url.startswith(prefix):
                    if isinstance(response, Exception):
                        raise response
                    if isinstance(response, str):
                        # Mirror client.request: parse the raw body first.
                        return parse_response_body(response)
                    return response
            raise QzoneError(f"unexpected url {url}")

        client.request = fake_request  # type: ignore[method-assign]
        return called

    def test_fetch_feeds_prefers_feeds3(self) -> None:
        client = self._client()
        feeds3_body = synth_body([synth_item(TID_A, UIN_FRIEND)])
        called = self._stub_request(client, {client_module_prefix(True): feeds3_body})
        items = client.fetch_feeds()
        self.assertEqual([i["external_id"] for i in items], [TID_A])
        self.assertEqual(len(called), 1)
        self.assertIn("feeds3_html_more", called[0])

    def test_fetch_feeds_falls_back_to_mobile_list(self) -> None:
        client = self._client()
        mobile_payload = {
            "code": 0,
            "data": {"list": [{"feedid": "f9", "uin": UIN_FRIEND, "nickname": "好友甲"}]},
        }
        called = self._stub_request(
            client,
            {
                client_module_prefix(True): QzoneError("qzone http 403: blocked", code=403),
                client_module_prefix(False): mobile_payload,
            },
        )
        items = client.fetch_feeds()
        self.assertEqual([i["external_id"] for i in items], ["f9"])
        self.assertEqual(len(called), 2)
        self.assertIn("feeds3_html_more", called[0])
        self.assertIn("mobile.qzone.qq.com/list", called[1])

    def test_fetch_feeds_falls_back_on_parse_error(self) -> None:
        client = self._client()
        mobile_payload = {
            "code": 0,
            "data": {"list": [{"feedid": "f8", "uin": UIN_FRIEND}]},
        }
        called = self._stub_request(
            client,
            {
                # Envelope parses but has no item list -> endpoint drift.
                client_module_prefix(True): {"code": 0, "data": {"main": {}}},
                client_module_prefix(False): mobile_payload,
            },
        )
        items = client.fetch_feeds()
        self.assertEqual([i["external_id"] for i in items], ["f8"])
        self.assertEqual(len(called), 2)


def client_module_prefix(feeds3: bool) -> str:
    from fatfish.qzone import client as client_module

    return client_module.FEEDS3_HTML_URL if feeds3 else client_module.MOBILE_LIST_URL


if __name__ == "__main__":
    unittest.main()
