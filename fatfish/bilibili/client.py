"""Stdlib-only Bilibili API client: read-only browsing plus QR/cookie login.

Design notes
------------
* No third-party HTTP stack: ``urllib`` + ``http.cookiejar``.
* Login is explicit. Nothing here comments, likes, follows or sends DMs.
* Requests are throttled to at least one second apart by default.
* Cookies are secrets: they are never printed or put into decision logs.

Endpoints used (all public web endpoints):

* ``/x/web-interface/nav``                      login state + WBI keys
* ``/x/passport-login/web/qrcode/generate|poll`` QR login
* ``/x/web-interface/wbi/search/type``          search (WBI signed)
* ``/x/web-interface/popular``                  popular feed
* ``/x/web-interface/view``                     one video
* ``/x/tag/archive/tags``                       optional tags
* ``/x/v2/reply/wbi/main``                      video comments (WBI signed)
* ``/x/player/v2``                              subtitle tracks (cc / ai)
* ``/x/player/videoshot``                       videoshot sprite metadata
* ``/x/web-interface/archive/related``          related videos
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import http.cookies
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.bilibili.com"
PASSPORT_BASE = "https://passport.bilibili.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"
WBI_KEYS_TTL_SECONDS = 6 * 3600
MIN_REQUEST_INTERVAL = 1.0

REPLY_MAIN_PATH = "/x/v2/reply/wbi/main"
# Comment text per line is hard-capped: comments are third-party UGC and the
# highest injection-risk surface, so each line stays short and bounded.
MAX_COMMENT_CHARS = 150
# The WBI reply endpoint answers at most 20 replies per page; we take one page
# and never paginate (fewer requests beat completeness on the risk-control
# surface, mirroring the Qzone zero-extra-request rule).
MAX_REPLIES_PER_PAGE = 20

# Standard public BV <-> AV conversion (bilibili-API-collect reference):
# base58 over the payload digits after swapping 3<->9 and 4<->7, masked to 51
# bits and XORed. Verified against av170001=BV17x411w7KC and av2=BV1xx411c7mD.
BVID_ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
BVID_INDEX = {char: idx for idx, char in enumerate(BVID_ALPHABET)}
BVID_XOR_CODE = 23442827791579
BVID_MASK_CODE = 2251799813685247

# Standard WBI mixin-key permutation.
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

QR_STATUS = {
    0: "success",
    86101: "waiting",
    86090: "scanned",
    86038: "expired",
}

# The QR success URL may only carry cross-domain routing metadata when login
# completes on a sub-site; the real credentials then arrive as Set-Cookie
# headers. Keep a whitelist so those routing fields are never written to the
# cookie file.
NON_COOKIE_POLL_KEYS = frozenset({"first_domain", "gourl", "ticket"})
LOGIN_COOKIE_NAMES = frozenset(
    {
        "SESSDATA",
        "bili_jct",
        "DedeUserID",
        "DedeUserID__ckMd5",
        "sid",
        "refresh_token",
        "buvid3",
        "buvid4",
        "b_nut",
        "buvid_fp",
        "buvid_fp_plain",
        "_uuid",
        "rpdid",
        "bsource",
        "fingerprint",
        "fingerprint3",
        "CURRENT_FNVAL",
        "CURRENT_QUALITY",
        "home_feed_column",
        "header_theme_version",
        "theme_style",
        "LIVE_BUVID",
        "PVID",
        "hit-dyn-v2",
        "browser_resolution",
    }
)
_COOKIE_ATTRIBUTE_NAMES = frozenset(
    str(name).lower()
    for name in getattr(http.cookies.Morsel, "_reserved", {})
)


class BilibiliError(RuntimeError):
    """Bilibili API/network failure with the remote code when available."""

    def __init__(self, message: str, *, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class BilibiliClient:
    def __init__(self, config: Any, cookies: dict[str, str] | None = None):
        self.config = config
        self.data_dir = _data_dir(config)
        raw_path = str(_cfg(config, "bilibili.cookie_path", "bilibili_cookies.json"))
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.data_dir / path
        self.cookie_path = path.resolve()
        self.user_agent = str(_cfg(config, "bilibili.user_agent", DEFAULT_USER_AGENT))
        self.request_interval = max(
            MIN_REQUEST_INTERVAL,
            float(_cfg(config, "bilibili.request_interval_seconds", 1.0)),
        )
        self.timeout = max(1.0, float(_cfg(config, "bilibili.timeout_seconds", 10.0)))
        self.max_videos_per_day = int(_cfg(config, "bilibili.max_videos_per_day", 8))
        # Comments-per-video read limit; 0 disables comment reading entirely
        # (video_comments then returns [] without sending any request).
        self.comments_per_video = max(
            0, int(_cfg(config, "bilibili.comments_per_video", 10) or 0)
        )
        # Total character budget across the subtitle lines returned by
        # video_subtitles(); clamped so a budget is always defined.
        self.subtitle_max_chars = max(
            1, int(_cfg(config, "bilibili.subtitle_max_chars", 20000) or 0)
        )

        self.cookies: dict[str, str] = _clean_cookie_mapping(cookies or {})
        self._last_request_at = 0.0
        self._wbi_cache: dict[str, str] | None = None
        self._wbi_cached_at = 0.0
        self.last_set_cookie_headers: list[str] = []
        self.last_response_url: str | None = None
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    # ---------------------------------------------------------------- loading
    @classmethod
    def from_config(cls, config: Any, cookies: dict[str, str] | None = None) -> "BilibiliClient":
        """Env cookies win; otherwise load the JSON cookie file."""
        merged: dict[str, str] = {}
        raw_cookie = (os.environ.get("BILIBILI_COOKIE") or "").strip()
        if raw_cookie:
            merged.update(parse_cookie_string(raw_cookie))
        sessdata = (os.environ.get("BILIBILI_SESSDATA") or "").strip()
        bili_jct = (os.environ.get("BILIBILI_BILI_JCT") or "").strip()
        dede = (os.environ.get("BILIBILI_DEDEUSERID") or "").strip()
        if sessdata:
            merged.setdefault("SESSDATA", sessdata)
        if bili_jct:
            merged.setdefault("bili_jct", bili_jct)
        if dede:
            merged.setdefault("DedeUserID", dede)

        if cookies:
            merged.update(_clean_cookie_mapping(cookies))
        merged = _clean_cookie_mapping(merged)
        if not merged.get("SESSDATA"):
            loader = cls(config)
            for key, value in loader.load_cookies().items():
                merged.setdefault(key, value)
        return cls(config, cookies=merged)

    def load_cookies(self, path: str | Path | None = None) -> dict[str, str]:
        """Load a cookie file, ignoring files without a usable SESSDATA.

        Older/broken files (for example a saved QR poll payload containing
        ``first_domain``/``gourl``/``ticket``) must not make callers believe a
        login is present. Such files are left untouched and will be overwritten
        on the next successful QR login.
        """
        target = Path(path) if path else self.cookie_path
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        loaded = _clean_cookie_mapping(raw)
        if not loaded.get("SESSDATA"):
            return {}
        self.cookies.update(loaded)
        return loaded

    def save_cookies(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.cookie_path
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = _clean_cookie_mapping(self.cookies)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            try:
                os.chmod(target, 0o600)
            except OSError:  # pragma: no cover - platform dependent
                pass
        return target

    def login_with_cookie(self, cookie_string: str) -> dict[str, str]:
        parsed = _clean_cookie_mapping(parse_cookie_string(cookie_string))
        if not parsed:
            raise BilibiliError("empty or invalid cookie string")
        if not parsed.get("SESSDATA"):
            raise BilibiliError("cookie string is missing SESSDATA")
        self.cookies.update(parsed)
        self.save_cookies()
        return dict(self.cookies)

    @property
    def has_login_cookie(self) -> bool:
        return bool(self.cookies.get("SESSDATA"))

    @property
    def logged_in(self) -> bool:
        return self.has_login_cookie

    # ----------------------------------------------------------------- request
    def request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = _resolve_url(path)
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None},
                quote_via=urllib.parse.quote,
            )
            if query:
                url = f"{url}{'&' if '?' in url else '?'}{query}"
        request = urllib.request.Request(
            url, headers=self._request_headers(), method="GET"
        )
        self._throttle()
        self.last_set_cookie_headers = []
        self.last_response_url = None
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
                self.last_set_cookie_headers = _header_values(
                    getattr(response, "headers", None), "Set-Cookie"
                )
                geturl = getattr(response, "geturl", None)
                if callable(geturl):
                    self.last_response_url = str(geturl())
        except urllib.error.HTTPError as exc:
            self.last_set_cookie_headers = _header_values(
                getattr(exc, "headers", None), "Set-Cookie"
            )
            body = exc.read() if hasattr(exc, "read") else b""
            payload = _try_json(body)
            if isinstance(payload, dict) and payload.get("code") is not None:
                raise BilibiliError(
                    f"bilibili http {exc.code}: {payload.get('message') or 'request failed'}",
                    code=int(payload.get("code") or 0),
                    data=payload,
                ) from exc
            raise BilibiliError(f"bilibili http {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise BilibiliError(f"bilibili network error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise BilibiliError("bilibili request timed out") from exc
        payload = _try_json(body)
        if not isinstance(payload, dict):
            raise BilibiliError("bilibili returned invalid JSON")
        code = int(payload.get("code") or 0)
        if code != 0:
            raise BilibiliError(
                f"bilibili api error {code}: {payload.get('message') or 'unknown error'}",
                code=code,
                data=payload,
            )
        return payload.get("data")

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self.request_interval - (now - self._last_request_at)
        if self._last_request_at and wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Referer": REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        cookie_header = self._cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    def _download_json(self, url: str) -> Any:
        """GET a bare-JSON URL with the same headers/throttle as request().

        Unlike :meth:`request` the parsed body is returned as-is: subtitle
        files are plain JSON documents without the ``code/data`` envelope,
        so envelope unwrapping would discard them.
        """
        request = urllib.request.Request(
            url, headers=self._request_headers(), method="GET"
        )
        self._throttle()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise BilibiliError(f"bilibili http {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise BilibiliError(f"bilibili network error: {exc.reason}") from exc
        except TimeoutError as exc:
            raise BilibiliError("bilibili request timed out") from exc
        payload = _try_json(body)
        if payload is None:
            raise BilibiliError("bilibili returned invalid JSON")
        return payload

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    def _cookies_from_jar(self) -> dict[str, str]:
        """Cookies urllib's cookie processor captured (including redirects)."""
        cookies: dict[str, str] = {}
        for cookie in self.jar:
            domain = str(getattr(cookie, "domain", "") or "").lower()
            if domain and "bilibili.com" not in domain and "biligame.com" not in domain:
                continue
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "")
            if name and value:
                cookies[name] = value
        return _clean_cookie_mapping(cookies)

    # --------------------------------------------------------------------- nav
    def nav(self) -> dict[str, Any]:
        """Login state and WBI image keys. Anonymous access is not an error.

        The endpoint may answer ``-101`` (not logged in) while still returning
        ``data.wbi_img``; those keys are required for anonymous WBI search, so
        they are preserved.
        """
        try:
            data = _unwrap(self.request("/x/web-interface/nav"))
        except BilibiliError as exc:
            if exc.code in {-101, -400}:
                payload = exc.data if isinstance(exc.data, dict) else {}
                nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                return {
                    "is_login": bool(nested.get("isLogin")),
                    "isLogin": bool(nested.get("isLogin")),
                    "mid": nested.get("mid"),
                    "uname": nested.get("uname") or "",
                    "wbi_img": nested.get("wbi_img"),
                }
            raise
        data = data or {}
        level_info = data.get("level_info") or {}
        return {
            "is_login": bool(data.get("isLogin")),
            "isLogin": bool(data.get("isLogin")),
            "mid": data.get("mid"),
            "uname": data.get("uname") or "",
            "level": level_info.get("current_level"),
            "vip": bool(data.get("vipStatus")),
            "wbi_img": data.get("wbi_img"),
        }

    # ---------------------------------------------------------------- QR login
    def qr_generate(self) -> dict[str, Any]:
        data = _unwrap(
            self.request(f"{PASSPORT_BASE}/x/passport-login/web/qrcode/generate")
        ) or {}
        url = str(data.get("url") or "")
        key = str(data.get("qrcode_key") or "")
        if not url or not key:
            raise BilibiliError("bilibili QR generate returned an incomplete response")
        return {"url": url, "qrcode_key": key}

    def qr_poll(self, qrcode_key: str) -> dict[str, Any]:
        data = _unwrap(
            self.request(
                f"{PASSPORT_BASE}/x/passport-login/web/qrcode/poll",
                {"qrcode_key": qrcode_key},
            )
        ) or {}
        raw_code = data.get("code", 0)
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = -1
        status = QR_STATUS.get(code, "error")
        message = str(data.get("message") or _default_qr_message(status))
        parsed_cookies: dict[str, str] = {}
        if status == "success":
            parsed_cookies = parse_login_cookies(
                str(data.get("url") or ""),
                set_cookie_headers=self.last_set_cookie_headers,
                jar_cookies=self._cookies_from_jar(),
            )
            if parsed_cookies:
                self.cookies.update(parsed_cookies)
                # Never persist routing metadata or a partial set without the
                # actual session cookie; the next successful poll overwrites
                # any stale/broken file.
                if parsed_cookies.get("SESSDATA"):
                    self.save_cookies()
        return {"status": status, "message": message, "cookies": parsed_cookies}

    # -------------------------------------------------------------------- WBI
    def wbi_keys(self, *, force: bool = False) -> dict[str, str]:
        now = time.time()
        if (
            not force
            and self._wbi_cache
            and now - self._wbi_cached_at < WBI_KEYS_TTL_SECONDS
        ):
            return dict(self._wbi_cache)
        nav = self.nav()
        wbi_img = nav.get("wbi_img") or {}
        img_key = _basename_key(str(wbi_img.get("img_url") or ""))
        sub_key = _basename_key(str(wbi_img.get("sub_url") or ""))
        if not img_key or not sub_key:
            raise BilibiliError("bilibili WBI keys are unavailable")
        raw = img_key + sub_key
        mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]
        self._wbi_cache = {
            "img_key": img_key,
            "sub_key": sub_key,
            "mixin_key": mixin_key,
        }
        self._wbi_cached_at = now
        return dict(self._wbi_cache)

    def sign(self, params: dict[str, Any], *, wts: int | None = None) -> dict[str, str]:
        """Return signed params (``wts`` + ``w_rid``) for WBI endpoints."""
        mixin_key = self.wbi_keys()["mixin_key"]
        timestamp = int(time.time()) if wts is None else int(wts)
        filtered: dict[str, str] = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            text = re.sub(r"[!'()*]", "", str(value))
            filtered[str(key)] = text
        filtered["wts"] = str(timestamp)
        query = urllib.parse.urlencode(
            sorted(filtered.items()),
            quote_via=urllib.parse.quote,
        )
        filtered["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return filtered

    # ------------------------------------------------------------- read-only
    def search(self, keyword: str, limit: int = 20) -> list[dict[str, Any]]:
        keyword = str(keyword or "").strip()
        if not keyword:
            return []
        limit = max(1, int(limit))
        params = self.sign(
            {
                "search_type": "video",
                "keyword": keyword,
                "page": 1,
                "page_size": min(50, limit),
            }
        )
        data = _unwrap(
            self.request(f"{API_BASE}/x/web-interface/wbi/search/type", params)
        ) or {}
        items = data.get("result") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        videos: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or str(item.get("type") or "video") != "video":
                continue
            normalized = _normalize_video(item)
            if normalized["bvid"]:
                videos.append(normalized)
            if len(videos) >= limit:
                break
        return videos

    def popular(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        data = _unwrap(
            self.request(
                f"{API_BASE}/x/web-interface/popular",
                {"ps": min(50, limit), "pn": 1},
            )
        ) or {}
        items = data.get("list") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        videos: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_video(item)
            if normalized["bvid"]:
                videos.append(normalized)
            if len(videos) >= limit:
                break
        return videos

    def view(self, bvid: str) -> dict[str, Any]:
        return _normalize_video(self._view_raw(bvid))

    def _view_raw(self, bvid: str) -> dict[str, Any]:
        """Raw ``/x/web-interface/view`` payload (keeps ``pages``/``cid``).

        ``_normalize_video`` drops the page list, but the player endpoints
        (subtitles / videoshot) key on ``cid``, so callers that need it read
        the raw data here — one request, shared with :meth:`view`.
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            raise BilibiliError("bvid is required")
        data = _unwrap(
            self.request(f"{API_BASE}/x/web-interface/view", {"bvid": bvid})
        ) or {}
        if not isinstance(data, dict):
            raise BilibiliError("bilibili view returned an invalid response")
        return data

    def tags(self, bvid: str) -> list[str]:
        bvid = str(bvid or "").strip()
        if not bvid:
            return []
        data = _unwrap(
            self.request(f"{API_BASE}/x/tag/archive/tags", {"bvid": bvid})
        ) or []
        names: list[str] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("tag_name"):
                    names.append(str(item["tag_name"]))
        return names

    def video_comments(self, bvid: str, limit: int | None = None) -> list[str]:
        """Read one video's comments (read-only, one signed request).

        Returns ``"用户: 内容"`` lines in reply order, each content truncated
        to :data:`MAX_COMMENT_CHARS`. ``limit=None`` resolves the config key
        ``bilibili.comments_per_video``; a resolved limit of 0 means the
        feature is switched off: no request is made and ``[]`` is returned.
        The reply endpoint is WBI signed like ``search`` and throttled like
        every other request; only the first page is taken (see
        :data:`MAX_REPLIES_PER_PAGE`) — comments are additive context, not
        worth extra risk-control exposure.

        Raises :class:`BilibiliError` on API/network failure and
        :class:`ValueError` when ``bvid`` is not a valid BV id; callers that
        merely want best-effort comments should catch both.
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return []
        resolved = self.comments_per_video if limit is None else int(limit)
        resolved = max(0, int(resolved))
        if resolved == 0:
            return []
        aid = bvid_to_aid(bvid)
        params = self.sign(
            {
                "oid": aid,
                "type": 1,
                "mode": 3,
                "ps": min(MAX_REPLIES_PER_PAGE, resolved),
                "pn": 1,
            }
        )
        data = _unwrap(self.request(f"{API_BASE}{REPLY_MAIN_PATH}", params)) or {}
        replies = data.get("replies") if isinstance(data, dict) else None
        comments: list[str] = []
        if isinstance(replies, list):
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                member = (
                    reply.get("member") if isinstance(reply.get("member"), dict) else {}
                )
                user = str(member.get("uname") or "").strip() or "匿名"
                content = (
                    reply.get("content")
                    if isinstance(reply.get("content"), dict)
                    else {}
                )
                text = _clean_html(str(content.get("message") or ""))
                if not text:
                    continue
                comments.append(f"{user}: {_truncate(text, MAX_COMMENT_CHARS)}")
                if len(comments) >= resolved:
                    break
        return comments

    def video_cid(self, bvid: str, page: int = 1) -> tuple[int, list[dict[str, Any]]]:
        """Resolve ``(cid, pages)`` for one P of a video.

        The player endpoints (subtitles / videoshot) key on ``cid`` while
        :meth:`view` normalizes the page list away, so this reads the raw
        view payload directly — one request, no extra metadata fetch.
        ``pages`` is the raw page list (``cid``/``part``/``duration``...);
        single-P answers without one fall back to the top-level ``cid``.

        Raises :class:`BilibiliError` for an empty bvid, API failure or an
        out-of-range page — a missing cid has no useful silent fallback.
        """
        data = self._view_raw(bvid)
        raw_pages = data.get("pages")
        pages: list[dict[str, Any]] = (
            [item for item in raw_pages if isinstance(item, dict)]
            if isinstance(raw_pages, list)
            else []
        )
        if not pages:
            cid = _int_or(data.get("cid"), 0)
            if cid <= 0:
                raise BilibiliError(f"bilibili view returned no pages for {bvid!r}")
            return cid, [{"page": 1, "cid": cid}]
        try:
            wanted = max(1, int(page))
        except (TypeError, ValueError):
            raise BilibiliError(f"invalid page: {page!r}") from None
        for index, item in enumerate(pages):
            if _int_or(item.get("page"), index + 1) != wanted:
                continue
            cid = _int_or(item.get("cid"), 0)
            if cid <= 0:
                break
            return cid, pages
        raise BilibiliError(f"page {wanted} out of range for {bvid!r}")

    def video_subtitles(
        self, bvid: str, page: int = 1, cid: int | None = None
    ) -> dict[str, Any]:
        """Read one video's subtitle lines (manual cc first, then ai-).

        Returns ``{"lines": ["[起-止] 文本", ...], "source": "cc"|"ai"|"none",
        "lang": ...}``; the lines together are capped to
        ``bilibili.subtitle_max_chars`` (default 20000) characters. The
        subtitle file URL is completed from ``//host`` to ``https://host``
        and fetched like any other request. Best-effort and never raises:
        without a login cookie the player endpoint often hides subtitle
        tracks, and every other failure (risk control, no usable track,
        dead subtitle URL, malformed payload) degrades to ``source="none"``.
        Callers that must distinguish a missing video from missing subtitles
        should call :meth:`video_cid` first; passing ``cid`` from it skips
        the extra view lookup request.
        """
        def none_result() -> dict[str, Any]:
            return {"lines": [], "source": "none", "lang": ""}

        bvid = str(bvid or "").strip()
        if not bvid:
            return none_result()
        try:
            if cid is None:
                cid, _pages = self.video_cid(bvid, page)
            resolved = _int_or(cid, 0)
            if resolved <= 0:
                return none_result()
            data = _unwrap(
                self.request(
                    f"{API_BASE}/x/player/v2", {"bvid": bvid, "cid": resolved}
                )
            )
        except BilibiliError:
            return none_result()
        subtitle = data.get("subtitle") if isinstance(data, dict) else None
        tracks = subtitle.get("subtitles") if isinstance(subtitle, dict) else None
        if not isinstance(tracks, list):
            return none_result()
        usable: list[tuple[dict[str, Any], str, str]] = []
        for track in tracks:
            if not isinstance(track, dict):
                continue
            url = _absolute_https(str(track.get("subtitle_url") or ""))
            if not url.startswith("http"):
                continue
            manual = not str(track.get("lan") or "").startswith("ai-")
            usable.append((track, "cc" if manual else "ai", url))
        manual_tracks = [item for item in usable if item[1] == "cc"]
        chosen = manual_tracks[:1] or usable[:1]
        if not chosen:
            return none_result()
        track, source, subtitle_url = chosen[0]
        try:
            payload = self._download_json(subtitle_url)
        except BilibiliError:
            return none_result()
        body = payload.get("body") if isinstance(payload, dict) else payload
        if not isinstance(body, list):
            return none_result()
        lines: list[str] = []
        for item in body:
            if not isinstance(item, dict):
                continue
            text = " ".join(_clean_html(str(item.get("content") or "")).split())
            if not text:
                continue
            lines.append(
                f"[{_format_ts(item.get('from'))}-{_format_ts(item.get('to'))}] {text}"
            )
        if not lines:
            return none_result()
        return {
            "lines": _cap_lines(lines, self.subtitle_max_chars),
            "source": source,
            "lang": str(track.get("lan") or ""),
        }

    def video_snapshot(
        self, bvid: str, page: int = 1, cid: int | None = None
    ) -> dict[str, Any] | None:
        """Official videoshot sprite (contact sheet) metadata, or ``None``.

        The player endpoint answers with a 10x10 sprite JPEG URL plus the
        frame index; that URL and metadata is all we return — the image
        itself is never downloaded here (the cloud box has no ffmpeg and
        does not need one). Best-effort like :meth:`video_subtitles`:
        empty bvid, API error, risk control or a missing image all degrade
        to ``None``. Passing ``cid`` from :meth:`video_cid` skips the extra
        view lookup request.
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return None
        try:
            if cid is None:
                cid, _pages = self.video_cid(bvid, page)
            resolved = _int_or(cid, 0)
            if resolved <= 0:
                return None
            data = _unwrap(
                self.request(
                    f"{API_BASE}/x/player/videoshot",
                    {"bvid": bvid, "cid": resolved, "index": 1},
                )
            )
        except BilibiliError:
            return None
        if not isinstance(data, dict):
            return None
        images = data.get("image")
        image_url = ""
        if isinstance(images, list):
            for candidate in images:
                text = str(candidate or "").strip()
                if text:
                    image_url = text
                    break
        elif isinstance(images, str):
            image_url = images.strip()
        if not image_url:
            return None
        raw_index = data.get("index")
        index = (
            [_int_or(item, 0) for item in raw_index if isinstance(item, (int, float))]
            if isinstance(raw_index, list)
            else []
        )
        return {
            "image_url": image_url,
            "img_x_len": _int_or(data.get("img_x_len"), 0),
            "img_y_len": _int_or(data.get("img_y_len"), 0),
            "index": index,
        }

    def related_videos(self, bvid: str, limit: int = 5) -> list[dict[str, Any]]:
        """Related videos for one archive, normalized like search/popular.

        Raises :class:`BilibiliError` on API failure, like the other list
        endpoints; an empty bvid returns ``[]`` without a request.
        """
        bvid = str(bvid or "").strip()
        if not bvid:
            return []
        limit = max(1, int(limit))
        data = _unwrap(
            self.request(
                f"{API_BASE}/x/web-interface/archive/related", {"bvid": bvid}
            )
        )
        if not isinstance(data, list):
            return []
        videos: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_video(item)
            if normalized["bvid"]:
                videos.append(normalized)
            if len(videos) >= limit:
                break
        return videos

    def browse(self, limit: int = 5, keyword: str | None = None) -> list[dict[str, Any]]:
        """Popular feed, or search results when ``keyword`` is provided."""
        limit = max(1, int(limit))
        videos = self.search(keyword, limit) if keyword else self.popular(limit)
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for video in videos:
            bvid = str(video.get("bvid") or "")
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            result.append(video)
            if len(result) >= limit:
                break
        return result


# --------------------------------------------------------------------- helpers
def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(dotted, default)
    if isinstance(config, dict):
        current: Any = config
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current
    return default


def _data_dir(config: Any) -> Path:
    value = getattr(config, "data_dir", None)
    if value is not None:
        return Path(value)
    raw = _cfg(config, "app.data_dir", "data")
    return Path(str(raw))


def _resolve_url(path: str) -> str:
    value = str(path or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/"):
        return API_BASE + value
    return f"{API_BASE}/{value}"


def _unwrap(payload: Any) -> Any:
    """Accept either a full API envelope or its ``data`` payload."""
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload.get("data")
    return payload


def _try_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8", "replace") if body else "null")
    except (TypeError, ValueError):
        return None


def _basename_key(url: str) -> str:
    if not url:
        return ""
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, marking the cut."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _int_or(value: Any, default: int) -> int:
    """Best-effort int coercion; ``0`` doubles as the "missing" sentinel."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_ts(value: Any) -> str:
    """Subtitle timestamps as ``12.3`` seconds; junk becomes ``0.0``."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "0.0"


def _absolute_https(url: str) -> str:
    """Complete protocol-relative ``//host/path`` URLs to ``https://``."""
    url = url.strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _cap_lines(lines: list[str], budget: int) -> list[str]:
    """Cap the joined subtitle text to ``budget`` characters.

    Lines are kept whole while they fit; the first line that overflows is
    cut (marked with an ellipsis) and the rest are dropped.
    """
    capped: list[str] = []
    total = 0
    for line in lines:
        remaining = budget - total
        if remaining <= 0:
            break
        if len(line) <= remaining:
            capped.append(line)
            total += len(line)
        else:
            capped.append(_truncate(line, remaining))
            break
    return capped


def bvid_to_aid(bvid: str) -> int:
    """Standard public BV→AV conversion, stdlib-only.

    Reference: bilibili-API-collect (XOR/mask over a base58 payload after
    swapping digits 3↔9 and 4↔7). Verified against av170001=BV17x411w7KC and
    av2=BV1xx411c7mD. The reply API takes a numeric ``oid``, so without this
    conversion every comment fetch would cost an extra ``view`` request.
    """
    raw = str(bvid or "").strip()
    if len(raw) != 12 or not raw.startswith("BV1"):
        raise ValueError(f"invalid bvid: {bvid!r}")
    chars = list(raw)
    for source, target in ((3, 9), (4, 7)):
        chars[source], chars[target] = chars[target], chars[source]
    value = 0
    for char in chars[3:]:
        index = BVID_INDEX.get(char)
        if index is None:
            raise ValueError(f"invalid bvid: {bvid!r}")
        value = value * 58 + index
    return (value & BVID_MASK_CODE) ^ BVID_XOR_CODE


_UNSAFE_COOKIE_CHARS = frozenset("\r\n\x00;")


def _cookie_text_is_safe(value: str) -> bool:
    """Reject control characters, newlines and semicolons."""
    for char in value:
        if char in _UNSAFE_COOKIE_CHARS or ord(char) < 0x20 or ord(char) == 0x7F:
            return False
    return True


def _clean_cookie_mapping(cookies: dict | None) -> dict[str, str]:
    """Return only usable session cookies from an arbitrary mapping.

    QR-poll payloads and loosely parsed headers can carry routing metadata
    (``first_domain``/``gourl``/``ticket``) or HTTP cookie attributes
    (``path``/``domain``/``expires``/...). Persisting those makes a broken
    file look like a login, so they are dropped here. Keys and values must be
    non-empty strings without header-injection characters.
    """
    if not cookies:
        return {}
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in cookies.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        key = raw_key.strip()
        value = raw_value.strip()
        if not key or not value:
            continue
        if key in NON_COOKIE_POLL_KEYS:
            continue
        if key.lower() in _COOKIE_ATTRIBUTE_NAMES:
            continue
        if not _cookie_text_is_safe(key) or not _cookie_text_is_safe(value):
            continue
        cleaned[key] = value
    return cleaned


def _header_values(headers: Any, name: str) -> list[str]:
    """Collect every value of a header, comparing names case-insensitively."""
    if headers is None:
        return []
    wanted = str(name or "").strip().lower()
    if not wanted:
        return []
    raw_values: list[Any] = []
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all(name)
        if values is not None:
            if isinstance(values, str):
                raw_values = [values]
            else:
                raw_values = list(values)
    else:
        getter = getattr(headers, "get", None)
        value: Any = None
        if callable(getter):
            value = getter(name)
            if value is None:
                value = getter(wanted)
        if value is None:
            items = getattr(headers, "items", None)
            if callable(items):
                for key, candidate in items():
                    if str(key).strip().lower() == wanted:
                        value = candidate
                        break
        if value is not None:
            raw_values = [value]
    values_out: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip()
        if text:
            values_out.append(text)
    return values_out


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    """Parse ``k=v; k=v`` (also accepts newlines) into a cookie dict."""
    cookies: dict[str, str] = {}
    raw = str(cookie_string or "").replace("\n", ";")
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            cookies[key] = value
    return cookies


def parse_cookie_string_from_url(url: str) -> dict[str, str]:
    """QR success returns a URL whose query carries the login cookies."""
    if not url:
        return {}
    query = urllib.parse.urlparse(url).query
    if not query:
        return {}
    parsed = urllib.parse.parse_qs(query, keep_blank_values=False)
    cookies: dict[str, str] = {}
    for key, values in parsed.items():
        if values and values[0]:
            cookies[str(key)] = str(values[0])
    return cookies


def parse_login_cookies(
    url: str,
    *,
    set_cookie_headers: list[str] | None = None,
    jar_cookies: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge login cookies from the QR success URL, headers and the jar.

    Later sources win: the URL query and ``Set-Cookie`` values override the
    cookie jar when the same name appears in both. Routing metadata and cookie
    attributes are removed by :func:`_clean_cookie_mapping`.
    """
    merged: dict[str, str] = {}
    if jar_cookies:
        merged.update(_clean_cookie_mapping(jar_cookies))
    merged.update(_clean_cookie_mapping(parse_cookie_string_from_url(str(url or ""))))

    headers = set_cookie_headers or []
    if isinstance(headers, str):
        headers = [headers]
    for header in headers:
        text = str(header or "").strip()
        if not text:
            continue
        parsed = http.cookies.SimpleCookie()
        try:
            parsed.load(text)
        except http.cookies.CookieError:
            continue
        for key, morsel in parsed.items():
            value = morsel.value
            if isinstance(value, str) and value:
                merged[str(key)] = value
    return _clean_cookie_mapping(merged)


def _default_qr_message(status: str) -> str:
    return {
        "waiting": "waiting for scan",
        "scanned": "scanned, confirm on your phone",
        "expired": "QR code expired",
        "success": "login success",
    }.get(status, "unknown QR status")


def _normalize_video(item: dict[str, Any]) -> dict[str, Any]:
    bvid = str(item.get("bvid") or "").strip()
    title = _clean_html(str(item.get("title") or ""))
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    up = str(owner.get("name") or item.get("author") or "").strip()
    desc = _clean_html(str(item.get("desc") or item.get("description") or ""))
    pubdate = item.get("pubdate") or item.get("senddate") or item.get("created")
    try:
        published_at: float | None = float(pubdate) if pubdate else None
    except (TypeError, ValueError):
        published_at = None
    topics = _topics(item)
    stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
    play = stat.get("view") if stat else item.get("play")
    try:
        play_count = int(play or 0)
    except (TypeError, ValueError):
        play_count = 0
    hot_score = round(min(1.0, math.log10(max(1, play_count)) / 7.0), 4)
    return {
        "bvid": bvid,
        "title": title,
        "up": up,
        "desc": desc,
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "published_at": published_at,
        "topics": topics,
        "hot_score": hot_score,
    }


def _topics(item: dict[str, Any]) -> list[str]:
    raw = item.get("tag")
    topics: list[str] = []
    if isinstance(raw, str) and raw.strip():
        topics.extend(part.strip() for part in raw.split(",") if part.strip())
    elif isinstance(raw, list):
        topics.extend(str(part).strip() for part in raw if str(part).strip())
    if not topics:
        tname = str(item.get("tname") or "").strip()
        if tname:
            topics.append(tname)
    return topics


def _clean_html(text: str) -> str:
    import html

    cleaned = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(cleaned).strip()
