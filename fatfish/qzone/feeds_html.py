"""feeds3_html_more response parser: JSONP + JS object literal + embedded HTML.

The friend-feed CGI behind ``ic2.qzone.qq.com`` answers with
``_Callback({...})`` whose body is a JS object literal (bare keys, single
quoted strings) and whose feed items embed an HTML snippet per entry. None of
that is strict JSON, so this module owns the lenient parse:

* ``strip_jsonp``        -- unwrap ``callback(...)`` envelopes
* ``loose_json_loads``   -- JS object literal -> Python object (stdlib only)
* ``parse_feeds3``       -- raw response text -> normalized feed item dicts
* ``parse_feeds3_payload`` -- already-unwrapped envelope dict -> item dicts

Tolerance contract: structural garbage raises :class:`Feeds3ParseError` so the
caller can fall back to the legacy mobile-list endpoint; per-item field gaps
degrade to empty values and never raise. The 2026-10-25 probe showed the stream
also carries injected ads (``uin='0'``, ids prefixed ``advertisement``) and
trailing ``null`` placeholders -- both are skipped: a companion must not
memorize advertising. Fixtures in the tests are fully synthetic.
"""

from __future__ import annotations

import html as html_module
import json
import re
from typing import Any

MAX_FEED_ITEMS = 50
MAX_CONTENT_CHARS = 4000
MAX_MEDIA = 9
# A bare response can exceed 200KB; the HTML text extraction stays linear and
# this hard cap keeps a hostile body from being re-scanned forever.
MAX_HTML_CHARS = 512 * 1024

_JSONP_RE = re.compile(r"^\s*[_$A-Za-z][_$A-Za-z0-9]*\s*\((.*)\)\s*;?\s*$", re.S)
_BARE_KEY_RE = re.compile(r"[ \t\r\n]*:")
_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")
_JS_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v",
    "0": "\0", "/": "/", "'": "'", '"': '"', "\\": "\\",
}


class Feeds3ParseError(ValueError):
    """The feeds3 envelope itself was unusable (caller should fall back)."""


# ------------------------------------------------------------ JS -> literal
def strip_jsonp(text: str) -> str:
    """Remove a ``callback(...)`` wrapper; pass bare JSON through untouched."""
    stripped = text.strip().lstrip("\ufeff")
    match = _JSONP_RE.match(stripped)
    return match.group(1) if match else stripped


def _decode_js_string(s: str, start: int) -> tuple[str, int]:
    """Decode a single/double quoted JS string starting at ``start``.

    Returns ``(decoded, index_after_closing_quote)``. Unknown escapes resolve
    to the escaped character itself (JS semantics); ``\\xNN``/``\\uXXXX`` are
    numeric escapes. Raises :class:`Feeds3ParseError` when unterminated.
    """
    quote = s[start]
    out: list[str] = []
    i = start + 1
    n = len(s)
    while i < n:
        char = s[i]
        if char == quote:
            return "".join(out), i + 1
        if char == "\\" and i + 1 < n:
            esc = s[i + 1]
            if esc == "u":
                try:
                    out.append(chr(int(s[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            elif esc == "x":
                try:
                    out.append(chr(int(s[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            out.append(_JS_ESCAPES.get(esc, esc))
            i += 2
            continue
        out.append(char)
        i += 1
    raise Feeds3ParseError("unterminated string literal")


def loose_json_loads(text: str) -> Any:
    """Parse JSON or a JS object literal (bare keys, single quotes).

    Strict JSON takes the fast path; anything else goes through a
    string-aware rewrite that quotes bare keys and re-encodes strings, so a
    comma/colon inside feed HTML can never corrupt the scan.
    """
    stripped = text.strip().lstrip("\ufeff")
    if not stripped:
        raise Feeds3ParseError("empty body")
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    if len(stripped) > MAX_HTML_CHARS:
        raise Feeds3ParseError("body too large for lenient parse")
    try:
        return json.loads(_js_to_json(stripped))
    except ValueError as exc:
        raise Feeds3ParseError(f"unparsable JS literal: {exc}") from exc


def _js_to_json(s: str) -> str:
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        char = s[i]
        if char in "\"'":
            decoded, nxt = _decode_js_string(s, i)
            out.append(json.dumps(decoded, ensure_ascii=False))
            i = nxt
            continue
        if char in "{,":
            if char == ",":
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                if j < n and s[j] in "}]":
                    i += 1  # JS tolerates a trailing comma; JSON does not
                    continue
            out.append(char)
            i += 1
            j = i
            while j < n and s[j] in " \t\r\n":
                j += 1
            k = j
            while k < n and s[k] in _IDENT_CHARS:
                k += 1
            # Bare identifier in key position (``main:{...}``, ``key :''``).
            if k > j and _BARE_KEY_RE.match(s, k):
                out.append(s[i:j])
                out.append('"' + s[j:k] + '"')
                i = k
            continue
        if char in _IDENT_CHARS and not char.isdigit():
            k = i
            while k < n and s[k] in _IDENT_CHARS:
                k += 1
            word = s[i:k]
            if word == "undefined":
                out.append("null")
            elif word in ("NaN", "Infinity"):
                out.append("null")
            else:
                out.append(word)
            i = k
            continue
        out.append(char)
        i += 1
    return "".join(out)


# --------------------------------------------------------------- HTML bits
def _find_div_blocks(html: str, cls_token: str) -> list[str]:
    """Nesting-aware extraction of ``<div>`` blocks whose class has the token."""
    blocks: list[str] = []
    for opener in re.finditer(r"<div\b[^>]*>", html):
        attrs = opener.group(0)
        cls_match = re.search(r'class\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', attrs)
        classes = (cls_match.group(1) or cls_match.group(2)) if cls_match else ""
        if cls_token not in classes.split():
            continue
        depth = 1
        pos = opener.end()
        for token in re.finditer(r"<div\b[^>]*>|</div\s*>", html[pos:]):
            depth += 1 if token.group(0).startswith("<div") else -1
            if depth == 0:
                blocks.append(html[opener.start():pos + token.end()])
                break
    return blocks


def _tag_text(block: str) -> str:
    """Visible text of an HTML block: <br> becomes a newline."""
    text = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", block)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _tag_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(
        r"([a-zA-Z_:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag
    ):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        attrs[match.group(1).lower()] = value or ""
    return attrs


def _feed_data_attrs(html: str) -> dict[str, str]:
    """Attributes of the machine-readable ``<i name='feed_data' ...>`` element.

    It carries ``data-tid``/``data-uin``/``data-abstime``/``data-origtid`` even
    when the JSON part omits fields. A drift fallback accepts any tag holding
    ``data-tid``.
    """
    tag_match = re.search(r"<i\b[^>]*\bname\s*=\s*['\"]feed_data['\"][^>]*>", html)
    if not tag_match:
        tag_match = re.search(r"<[a-zA-Z]+\b[^>]*\bdata-tid\s*=\s*['\"][^'\"]+['\"][^>]*>", html)
    if not tag_match:
        return {}
    return _tag_attrs(tag_match.group(0))


def _nick_from_html(html: str) -> str:
    """Nickname fallback: the anchor text of the ``f-name`` link."""
    anchor = re.search(
        r"<a\b[^>]*\bclass\s*=\s*['\"][^'\"]*\bf-name\b[^'\"]*['\"][^>]*>(.*?)</a\s*>",
        html,
        re.S,
    )
    if not anchor:
        return ""
    return _tag_text(anchor.group(1))


def _media_from_html(html: str) -> list[str]:
    """Picture URLs: the ``<img src>`` inside ``img-item`` anchors."""
    urls: list[str] = []
    for anchor in re.finditer(
        r"<a\b[^>]*\bclass\s*=\s*['\"][^'\"]*\bimg-item\b[^'\"]*['\"][^>]*>(.*?)</a\s*>",
        html,
        re.S,
    ):
        img = re.search(r"<img\b[^>]*\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", anchor.group(1))
        if not img:
            continue
        url = (img.group(1) if img.group(1) is not None else img.group(2) or "").strip()
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= MAX_MEDIA:
            break
    return urls


def _content_from_html(html: str) -> str:
    """Feed text: ``f-info`` blocks first, ``txt-box`` blocks as fallback."""
    blocks = _find_div_blocks(html, "f-info") or _find_div_blocks(html, "txt-box")
    texts = [text for text in (_tag_text(block) for block in blocks[:4]) if text]
    if not texts:
        return ""
    content = "\n".join(texts)
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "…"
    return content


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) > limit:
        text = text[:limit] + "…"
    return text.strip()


# ------------------------------------------------------------ normalization
def _normalize_feed_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One envelope entry -> collector item dict; ``None`` to skip.

    Skips: placeholders (``None``/non-dict handled upstream), ads (ids prefixed
    ``advertisement`` or author uin ``0``) and anything without an id.
    """
    html = str(raw.get("html") or "")
    attrs = _feed_data_attrs(html)
    external_id = _clean_text(raw.get("key") or raw.get("feedid"), 128)
    if not external_id:
        external_id = _clean_text(attrs.get("data-key") or attrs.get("data-tid") or attrs.get("data-fkey"), 128)
    if not external_id or external_id.startswith("advertisement"):
        return None
    author_uin = _clean_text(raw.get("uin") or attrs.get("data-uin"), 32)
    if author_uin == "0":
        return None
    author_name = _clean_text(
        raw.get("nickname") or raw.get("nick") or _nick_from_html(html), 64
    )
    published = _as_int(raw.get("abstime") or attrs.get("data-abstime"))
    reduced_raw = {k: v for k, v in raw.items() if k != "html"}
    return {
        "external_id": external_id,
        "author_uin": author_uin,
        "author_name": author_name,
        "content": _content_from_html(html),
        "media": _media_from_html(html),
        "published_at": float(published) if published else None,
        "url": f"https://user.qzone.qq.com/{author_uin}" if author_uin else "",
        # The HTML snippet can be 50KB; raw_json is capped at 20KB downstream,
        # so the reduced structured dict (sans html) is what gets persisted.
        "raw": reduced_raw,
    }


def parse_feeds3_payload(payload: Any) -> list[dict[str, Any]]:
    """Normalize an unwrapped feeds3 envelope dict into feed item dicts.

    The observed shape is ``{code: 0, data: {main: {...}, data: [<entry>|null]}}``.
    Raises :class:`Feeds3ParseError` when the envelope has no usable item list
    (endpoint drift) so the caller can fall back; an empty list is legitimate.
    """
    if not isinstance(payload, dict):
        raise Feeds3ParseError("feeds3 envelope is not an object")
    code = payload.get("code")
    if isinstance(code, int) and code != 0:
        raise Feeds3ParseError(f"feeds3 error envelope code={code}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise Feeds3ParseError("feeds3 envelope has no data object")
    main = data.get("main") if isinstance(data.get("main"), dict) else {}
    items: Any = None
    for candidate in (data.get("data"), main.get("data"), main.get("list")):
        if isinstance(candidate, list):
            items = candidate
            break
    if items is None:
        raise Feeds3ParseError("feeds3 envelope has no item list")
    normalized: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue  # trailing null placeholders
        item = _normalize_feed_item(raw)
        if item is None:
            continue
        normalized.append(item)
        if len(normalized) >= MAX_FEED_ITEMS:
            break
    return normalized


def parse_feeds3(raw_text: bytes | str) -> list[dict[str, Any]]:
    """Full pipeline: raw response body (JSONP or bare) -> feed item dicts.

    HTML login walls, unparsable bodies and error envelopes raise
    :class:`Feeds3ParseError`; everything else returns the normalized list.
    """
    text = (
        raw_text.decode("utf-8", "replace")
        if isinstance(raw_text, (bytes, bytearray))
        else str(raw_text)
    )
    stripped = text.strip()
    if not stripped:
        raise Feeds3ParseError("empty body")
    if stripped.startswith("<"):
        raise Feeds3ParseError("html login wall")
    payload = loose_json_loads(strip_jsonp(stripped))
    return parse_feeds3_payload(payload)
