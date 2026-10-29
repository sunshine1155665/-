#!/usr/bin/env python
"""Generate the docs index and tag map from YAML front matter.

The docs tree is the single source of truth; README.md only holds two markers:

    <!-- DOCS-INDEX:START --> ... <!-- DOCS-INDEX:END -->
    <!-- DOCS-TAGS:START  --> ... <!-- DOCS-TAGS:END  -->

Usage::

    python scripts/docs_index.py --write   # refresh docs/README.md
    python scripts/docs_index.py --check   # exit 2 when stale
    python scripts/docs_index.py           # print to stdout

Only a tiny subset of YAML is supported on purpose: scalar ``key: value`` and
``tags: [a, b]``. That keeps the project dependency-free.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
README = DOCS / "README.md"
INDEX_START = "<!-- DOCS-INDEX:START -->"
INDEX_END = "<!-- DOCS-INDEX:END -->"
TAGS_START = "<!-- DOCS-TAGS:START -->"
TAGS_END = "<!-- DOCS-TAGS:END -->"

TYPE_ORDER = {"overview": 0, "branch": 1, "adr": 2, "index": 3}
STATUS_ICON = {
    "active": "✅ active",
    "in-progress": "🟠 in-progress",
    "draft": "🟡 draft",
    "degraded": "🟡 degraded",
    "planned": "⬜ planned",
    "deprecated": "⛔ deprecated",
    "accepted": "✅ accepted",
    "proposed": "🟠 proposed",
    "superseded": "⛔ superseded",
}


def parse_front_matter(text: str) -> dict[str, object]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict[str, object] = {}
    for raw in text[3:end].splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = value.strip("\"'")
    return meta


def collect() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(DOCS.rglob("*.md")):
        if path == README or "archive" in path.parts:
            continue
        meta = parse_front_matter(path.read_text(encoding="utf-8", errors="replace"))
        if not meta:
            continue
        rel = path.relative_to(DOCS).as_posix()
        rows.append({
            "path": rel,
            "title": str(meta.get("title") or path.stem),
            "type": str(meta.get("type") or "doc"),
            "status": str(meta.get("status") or ""),
            "tags": list(meta.get("tags") or []),  # type: ignore[arg-type]
            "updated": str(meta.get("updated") or ""),
        })
    rows.sort(key=lambda r: (
        TYPE_ORDER.get(str(r["type"]), 9),
        0 if str(r["type"]) == "adr" else 1,
        str(r["path"]),
    ))
    return rows


def render_index(rows: list[dict[str, object]]) -> str:
    out = ["| 文档 | 类型 | 状态 | 标签 | 更新 |", "|---|---|---|---|---|"]
    for r in rows:
        status = STATUS_ICON.get(str(r["status"]), str(r["status"]) or "—")
        tags = ", ".join(f"`{t}`" for t in r["tags"]) or "—"
        out.append(
            f"| [{r['title']}]({r['path']}) | {r['type']} | {status} | {tags} | {r['updated']} |"
        )
    return "\n".join(out)


def render_tags(rows: list[dict[str, object]]) -> str:
    buckets: dict[str, list[str]] = {}
    for r in rows:
        for tag in r["tags"]:  # type: ignore[union-attr]
            buckets.setdefault(str(tag), []).append(f"[{r['title']}]({r['path']})")
    out = ["| 标签 | 文档 |", "|---|---|"]
    for tag in sorted(buckets):
        out.append(f"| `{tag}` | {', '.join(buckets[tag])} |")
    return "\n".join(out)


def replace_block(text: str, start: str, end: str, body: str) -> str:
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        raise SystemExit(f"marker missing: {start}")
    return pattern.sub(f"{start}\n{body}\n{end}", text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="rewrite docs/README.md")
    ap.add_argument("--check", action="store_true", help="exit 2 when README is stale")
    args = ap.parse_args(argv)

    rows = collect()
    original = README.read_text(encoding="utf-8")
    updated = replace_block(original, INDEX_START, INDEX_END, render_index(rows))
    updated = replace_block(updated, TAGS_START, TAGS_END, render_tags(rows))

    if args.check:
        if updated != original:
            print("docs index is stale; run: python scripts/docs_index.py --write", file=sys.stderr)
            return 2
        print(f"docs index ok ({len(rows)} docs)")
        return 0
    if args.write:
        README.write_text(updated, encoding="utf-8", newline="\n")
        print(f"wrote {README.relative_to(ROOT)} ({len(rows)} docs)")
        return 0
    print(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
