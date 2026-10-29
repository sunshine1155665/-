"""Chinese expression / internet-slang library with a review workflow.

The library is a small, local, stdlib-only table.  Seed entries are original
and PG-13; mined entries always land in ``candidate`` status and are never
used in prompts until a human approves them.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database

SEED_PATH = Path(__file__).with_name("seed.json")

VALID_KINDS = ("expression", "jargon")
VALID_STATUSES = ("candidate", "approved", "disabled")
VALID_STYLES = ("soft", "tsundere", "playful", "comfort", "serious")

PROMPT_HEADER = "【可以偶尔用的表达（不是每句都用，宁可不贴也别硬塞）】"
PROMPT_MAX_CHARS = 400
MAX_CONTENT_CHARS = 80
MAX_TEXT_CHARS = 300
MAX_TAGS = 8
MAX_EXAMPLES = 5

_STATUS_SET = frozenset(VALID_STATUSES)
_KIND_SET = frozenset(VALID_KINDS)
_STYLE_SET = frozenset(VALID_STYLES)

_STYLE_HINTS: dict[str, tuple[str, ...]] = {
    "comfort": (
        "累", "难过", "不开心", "哭", "委屈", "烦", "崩", "焦虑", "压力",
        "失眠", "害怕", "孤单", "疼", "病", "失败", "糟糕", "内耗", "叹气",
    ),
    "soft": (
        "想你", "喜欢", "撒娇", "抱", "陪", "晚安", "早安", "摸摸", "可爱",
        "开心", "好耶", "贴贴", "亲亲", "哄",
    ),
    "tsundere": ("哼", "笨蛋", "才不", "讨厌", "不理", "嘴硬", "傲娇", "别扭"),
    "playful": ("哈哈", "笑", "离谱", "整活", "乐", "吃瓜", "6", "牛", "绝了", "抽象"),
    "serious": ("认真", "工作", "学习", "面试", "考试", "安全", "隐私", "钱", "承诺"),
}

_LIST_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def make_id(kind: str, content: str) -> str:
    """Stable, content-addressed entry id."""
    return hashlib.sha1(f"{kind}:{content}".strip().encode("utf-8")).hexdigest()[:16]


def load_seed_entries(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load and normalize ``seed.json``; raises on malformed data."""
    target = Path(path) if path is not None else SEED_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("lexicon seed must be a JSON array")
    entries = [
        _normalize_entry(item, default_source="seed", default_status="candidate")
        for item in raw
    ]
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry["kind"], entry["content"])
        if key in seen:
            raise ValueError(f"duplicate lexicon seed entry: {entry['kind']}/{entry['content']}")
        seen.add(key)
    return entries


class LexiconService:
    """CRUD, review, prompt selection and mining support for the lexicon."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.ensure_schema()
        self.seed_if_empty()

    # ---------------------------------------------------------------- schema
    def ensure_schema(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS lexicon_entries (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                meaning TEXT NOT NULL DEFAULT '',
                situation TEXT NOT NULL DEFAULT '',
                style TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]',
                examples_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'seed',
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'candidate',
                usage_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                reviewed_at REAL,
                UNIQUE(kind, content)
            )
            """
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_lexicon_status "
            "ON lexicon_entries(status, updated_at)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_lexicon_kind "
            "ON lexicon_entries(kind, updated_at)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_lexicon_updated "
            "ON lexicon_entries(updated_at)"
        )

    def seed_if_empty(self) -> int:
        """Insert the bundled corpus once.  Existing rows are never overwritten."""
        row = self.db.query_one("SELECT COUNT(*) AS n FROM lexicon_entries")
        if row and int(row["n"]) > 0:
            return 0
        entries = load_seed_entries()
        if not entries:
            return 0
        before = int(row["n"]) if row else 0
        now = time.time()
        rows = [
            (
                make_id(entry["kind"], entry["content"]),
                entry["kind"],
                entry["content"],
                entry["meaning"],
                entry["situation"],
                entry["style"],
                json.dumps(entry["tags"], ensure_ascii=False),
                json.dumps(entry["examples"], ensure_ascii=False),
                entry["source"],
                entry["confidence"],
                entry["status"],
                now,
                now,
                now if entry["status"] != "candidate" else None,
            )
            for entry in entries
        ]
        self.db.executemany(
            "INSERT OR IGNORE INTO lexicon_entries("
            "id, kind, content, meaning, situation, style, tags_json, examples_json, "
            "source, confidence, status, created_at, updated_at, reviewed_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        after = self.db.query_one("SELECT COUNT(*) AS n FROM lexicon_entries")
        return max(0, (int(after["n"]) if after else 0) - before)

    # ------------------------------------------------------------------ CRUD
    def get(self, entry_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM lexicon_entries WHERE id=?", (str(entry_id),))
        return _row_to_entry(row) if row else None

    def list(
        self,
        status: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        normalized_status = _normalize_status_filter(status)
        if normalized_status:
            clauses.append("status=?")
            params.append(normalized_status)
        text = (query or "").strip()
        if text:
            pattern = "%" + text.translate(_LIST_ESCAPE) + "%"
            clauses.append(
                "(lower(content) LIKE lower(?) ESCAPE '\\' "
                "OR lower(meaning) LIKE lower(?) ESCAPE '\\' "
                "OR lower(situation) LIKE lower(?) ESCAPE '\\' "
                "OR lower(tags_json) LIKE lower(?) ESCAPE '\\')"
            )
            params.extend([pattern, pattern, pattern, pattern])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(500, int(limit))))
        rows = self.db.query(
            f"SELECT * FROM lexicon_entries{where} ORDER BY updated_at DESC, id ASC LIMIT ?",
            params,
        )
        return [_row_to_entry(row) for row in rows]

    def add(
        self,
        kind: str,
        content: str,
        *,
        meaning: str = "",
        situation: str = "",
        style: str = "",
        tags: Sequence[str] | None = None,
        examples: Sequence[str] | None = None,
        status: str = "candidate",
        source: str = "manual",
        confidence: float = 0.5,
    ) -> dict[str, Any]:
        entry = _normalize_entry(
            {
                "kind": kind,
                "content": content,
                "meaning": meaning,
                "situation": situation,
                "style": style,
                "tags": tags if tags is not None else [],
                "examples": examples if examples is not None else [],
                "source": source,
                "confidence": confidence,
                "status": status,
            },
            default_source="manual",
            default_status="candidate",
        )
        entry_id = make_id(entry["kind"], entry["content"])
        now = time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO lexicon_entries("
            "id, kind, content, meaning, situation, style, tags_json, examples_json, "
            "source, confidence, status, created_at, updated_at, reviewed_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry_id,
                entry["kind"],
                entry["content"],
                entry["meaning"],
                entry["situation"],
                entry["style"],
                json.dumps(entry["tags"], ensure_ascii=False),
                json.dumps(entry["examples"], ensure_ascii=False),
                entry["source"],
                entry["confidence"],
                entry["status"],
                now,
                now,
                now if entry["status"] != "candidate" else None,
            ),
        )
        item = self.get(entry_id)
        if item is None:  # pragma: no cover - sqlite should have the row
            raise RuntimeError("lexicon insert failed")
        return item

    def review(
        self,
        entry_id: str,
        status: str,
        meaning: str | None = None,
        style: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in _STATUS_SET:
            raise ValueError(f"invalid status: {status}")
        updates: dict[str, Any] = {"status": status, "reviewed_at": time.time()}
        if meaning is not None:
            updates["meaning"] = _clean_text(meaning, MAX_TEXT_CHARS)
        if style is not None:
            updates["style"] = _clean_style(style)
        updates["updated_at"] = time.time()
        sets = ", ".join(f"{key}=?" for key in updates)
        cur = self.db.execute(
            f"UPDATE lexicon_entries SET {sets} WHERE id=?",
            (*updates.values(), str(entry_id)),
        )
        if cur.rowcount == 0:
            return None
        return self.get(entry_id)

    def stats(self) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT status, kind, source, COUNT(*) AS n, "
            "COALESCE(SUM(usage_count), 0) AS used "
            "FROM lexicon_entries GROUP BY status, kind, source"
        )
        by_status: dict[str, int] = {name: 0 for name in VALID_STATUSES}
        by_kind: dict[str, int] = {name: 0 for name in VALID_KINDS}
        by_source: dict[str, int] = {}
        total = 0
        usage_total = 0
        for row in rows:
            count = int(row["n"])
            total += count
            usage_total += int(row["used"] or 0)
            by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + count
            by_kind[str(row["kind"])] = by_kind.get(str(row["kind"]), 0) + count
            source = str(row["source"] or "unknown")
            by_source[source] = by_source.get(source, 0) + count
        return {
            "total": total,
            "approved": by_status.get("approved", 0),
            "candidate": by_status.get("candidate", 0),
            "disabled": by_status.get("disabled", 0),
            "expression": by_kind.get("expression", 0),
            "jargon": by_kind.get("jargon", 0),
            "by_status": by_status,
            "by_kind": by_kind,
            "by_source": by_source,
            "usage_total": usage_total,
        }

    # --------------------------------------------------------------- prompting
    def approved_for_context(
        self, situation: str, limit: int = 6
    ) -> list[dict[str, Any]]:
        """Pick approved entries for the dynamic prompt section.

        Scoring is deterministic: keyword/tag overlap with ``situation``,
        style hints, confidence, and a small penalty for frequently used
        entries.  Lines are capped so the whole block stays near 400 chars.
        """
        limit = max(0, min(20, int(limit)))
        if limit == 0:
            return []
        rows = self.db.query(
            "SELECT * FROM lexicon_entries WHERE status='approved' ORDER BY id ASC"
        )
        entries = [_row_to_entry(row) for row in rows]
        entries = [entry for entry in entries if entry["content"].strip()]
        query = (situation or "").strip()
        query_grams = _grams(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            score = _score_entry(entry, query, query_grams)
            scored.append((score, entry))
        scored.sort(
            key=lambda pair: (
                -pair[0],
                pair[1]["usage_count"],
                -pair[1]["confidence"],
                pair[1]["id"],
            )
        )

        budget = PROMPT_MAX_CHARS - len(PROMPT_HEADER) - 1
        selected: list[dict[str, Any]] = []
        used = 0
        for _score, entry in scored:
            line = _prompt_line(entry)
            if not line:
                continue
            cost = len(line) + 1
            if used + cost > budget:
                continue
            selected.append(entry)
            used += cost
            if len(selected) >= limit:
                break
        return selected

    def format_for_prompt(self, entries: Iterable[Mapping[str, Any]]) -> str:
        """Render a compact Chinese block.  Returns ``""`` when nothing fits."""
        lines = [PROMPT_HEADER]
        total = len(PROMPT_HEADER)
        for entry in entries:
            line = _prompt_line(entry)
            if not line:
                continue
            cost = len(line) + 1
            if total + cost > PROMPT_MAX_CHARS:
                continue
            lines.append(line)
            total += cost
        return "\n".join(lines) if len(lines) > 1 else ""

    def record_usage(self, ids: Iterable[str]) -> int:
        """Increment usage for the selected ids only (safe to skip)."""
        updated = 0
        seen: set[str] = set()
        for raw in ids:
            entry_id = str(raw or "").strip()
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            cur = self.db.execute(
                "UPDATE lexicon_entries SET usage_count=usage_count+1 WHERE id=?",
                (entry_id,),
            )
            updated += max(0, int(cur.rowcount))
        return updated


def _normalize_status_filter(status: str | None) -> str | None:
    if status is None:
        return None
    text = str(status).strip()
    if text in {"", "all"}:
        return None
    if text not in _STATUS_SET:
        raise ValueError(f"invalid status: {text}")
    return text


def _normalize_entry(
    raw: Mapping[str, Any],
    *,
    default_source: str,
    default_status: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("lexicon entry must be an object")
    kind = str(raw.get("kind", "")).strip()
    if kind not in _KIND_SET:
        raise ValueError(f"invalid kind: {kind or '<empty>'}")
    content = _clean_text(raw.get("content", ""), MAX_CONTENT_CHARS, required=True)
    if not content:
        raise ValueError("content must not be empty")
    status = str(raw.get("status", default_status)).strip() or default_status
    if status not in _STATUS_SET:
        raise ValueError(f"invalid status: {status}")
    style = _clean_style(raw.get("style", ""))
    source = _clean_text(raw.get("source", default_source), 40) or default_source
    try:
        confidence = float(raw.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    return {
        "kind": kind,
        "content": content,
        "meaning": _clean_text(raw.get("meaning", ""), MAX_TEXT_CHARS),
        "situation": _clean_text(raw.get("situation", ""), MAX_TEXT_CHARS),
        "style": style,
        "tags": _clean_list(raw.get("tags", ()), 20, MAX_TAGS),
        "examples": _clean_list(raw.get("examples", ()), 160, MAX_EXAMPLES),
        "source": source,
        "confidence": round(confidence, 3),
        "status": status,
    }


def _clean_style(value: Any) -> str:
    style = str(value or "").strip()
    if style and style not in _STYLE_SET:
        raise ValueError(f"invalid style: {style}")
    return style


def _clean_text(value: Any, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if required and not text:
        raise ValueError("content must not be empty")
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _clean_list(values: Any, item_limit: int, max_items: int) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ValueError("tags/examples must be a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value, item_limit)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def _row_to_entry(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "content": row["content"],
        "meaning": row["meaning"],
        "situation": row["situation"],
        "style": row["style"],
        "tags": _json_list(row["tags_json"]),
        "examples": _json_list(row["examples_json"]),
        "source": row["source"],
        "confidence": float(row["confidence"]),
        "status": row["status"],
        "usage_count": int(row["usage_count"]),
        "created_at": float(row["created_at"]),
        "updated_at": float(row["updated_at"]),
        "reviewed_at": row["reviewed_at"],
    }


def _json_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _grams(text: str) -> set[str]:
    lowered = (text or "").lower()
    chars = re.findall(r"[0-9a-z\u4e00-\u9fff]", lowered)
    grams = set(chars)
    grams.update(a + b for a, b in zip(chars, chars[1:]))
    return grams


def _score_entry(
    entry: Mapping[str, Any], query: str, query_grams: set[str]
) -> float:
    content = str(entry.get("content", ""))
    meaning = str(entry.get("meaning", ""))
    situation = str(entry.get("situation", ""))
    tags = [str(tag) for tag in entry.get("tags", [])]
    style = str(entry.get("style", ""))
    score = float(entry.get("confidence", 0.0)) * 0.5
    if query:
        if content and content in query:
            score += 6.0
        for tag in tags:
            if tag and tag in query:
                score += 3.0
        hay_grams = _grams(" ".join([content, meaning, situation, " ".join(tags)]))
        if query_grams and hay_grams:
            score += min(4.0, 0.5 * len(query_grams & hay_grams))
        for hint in _STYLE_HINTS.get(style, ()):
            if hint in query:
                score += 1.5
                break
    score -= min(int(entry.get("usage_count", 0)), 50) * 0.08
    return round(score, 6)


def _prompt_line(entry: Mapping[str, Any]) -> str:
    content = str(entry.get("content", "")).strip()
    if not content:
        return ""
    description = str(entry.get("situation") or entry.get("meaning") or "").strip()
    description = re.sub(r"\s+", " ", description)
    if len(description) > 48:
        description = description[:48].rstrip() + "…"
    if description:
        return f"- {content}：{description}"
    return f"- {content}"
