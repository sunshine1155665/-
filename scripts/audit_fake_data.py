#!/usr/bin/env python
"""Read-only inventory of fake data / dirty references in the fatfish-lab tree.

The script never mutates the repository: it walks text files, classifies hits
into five buckets (absolute paths, real-data/secret references, network calls,
test fakes, scattered root files) and prints a summary.  Output files are only
written when ``--out DIR`` is given.

Usage::

    python scripts/audit_fake_data.py                      # stdout summary
    python scripts/audit_fake_data.py --out audit_out      # + json & md files
    python scripts/audit_fake_data.py --out audit_out --json
    python scripts/audit_fake_data.py --json               # json on stdout

Zero third-party dependencies, Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Directories that are never scanned (large assets, real data, generated code).
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".agent-board", ".opencode", ".workbuddy",
    "models", "vendor", "data", "dist", "build", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea",
    ".venv", "venv", "env", "site-packages", "audit_out",
}
SKIP_DIR_PREFIXES = (".venv-", "venv-", "staging_soulx", "backups")

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".jsonl", ".js", ".mjs", ".ts", ".html",
    ".css", ".sql", ".toml", ".ini", ".cfg", ".yml", ".yaml", ".bat", ".ps1",
    ".sh", ".example", ".gitignore", ".gitattributes", ".env",
}
MAX_BYTES = 2_000_000

CATEGORY_TITLES = {
    "a": "a) 硬编码绝对路径",
    "b": "b) 真实数据 / 密钥引用",
    "c": "c) 网络调用",
    "d": "d) 测试假数据",
    "e": "e) 根目录散落文件",
}

# ---------------------------------------------------------------- patterns ---

WINDOWS_ABS = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'`)]*")
POSIX_ABS = re.compile(r"(?<![\w.])/(?:home|tmp|Users|mnt|opt)/[\w.@/+-]*")
ABS_ANY = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")

REAL_DB = re.compile(r"data/fatfish\.db")
QZONE_COOKIES = re.compile(r"qzone_cookies\.json")
DOTENV = re.compile(r"(?<![\w])\.env(?![\w.])")
SECRET_LITERAL = re.compile(
    r"(?i)\b(token|cookie|passwd|password|secret|api[_-]?key|access[_-]?key|authorization)\b"
    r"\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']"
)
PLACEHOLDER = re.compile(
    r"(?i)^(<|\$\{|xxx|your[_-]?|changeme|placeholder|example|dummy|fake|none|null|test[-_]?token)"
)

NET_IMPORT = re.compile(
    r"\b(?:import|from)\s+(requests|websocket|websockets|httpx|aiohttp|urllib3)\b"
    r"|urllib\.request"
    r"|http\.client"
    r"|\bsocket\.(?:create_connection|connect)\b"
)
EXTERNAL_URL = re.compile(r"https?://(?!127\.0\.0\.1|localhost|0\.0\.0\.0)[\w.-]+")
LOCAL_HINT = re.compile(r"127\.0\.0\.1|localhost|0\.0\.0\.0|::1")
MOCK_HINT = re.compile(r"mock\.patch|monkeypatch|patch\(")
QUOTED_FAKE = re.compile(r"[\"'][^\"']*fake")

# marker -> human label.  Only applied inside tests/.
FAKE_MARKERS = {
    "张三": "假人名",
    "李四": "假人名",
    "王五": "假人名",
    "测试用户": "假用户",
    "测试消息": "假聊天",
    "测试一下": "假聊天",
    "假数据": "显式标注",
    "假人设": "显式标注",
    "假聊天": "显式标注",
    "lorem": "占位文本",
    "ipsum": "占位文本",
    "dummy": "占位命名",
    "fake": "fake 命名",
    "example.com": "示例域名",
    "example.org": "示例域名",
    "test@example": "示例邮箱",
    "13800138000": "示例手机号",
    "1380013800": "示例手机号",
    "fake_audio": "假音频",
    "dummy.wav": "假音频",
    "silence.wav": "假音频",
    "empty.wav": "假音频",
}

DETAIL_CAP = 12  # findings printed per category in the stdout summary


@dataclass
class Finding:
    path: str
    line: int
    category: str
    kind: str
    snippet: str
    suggestion: str


@dataclass
class Audit:
    findings: list[Finding] = field(default_factory=list)
    root_files: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    counts_by_category: dict[str, int] = field(default_factory=dict)
    counts_by_kind: dict[str, int] = field(default_factory=dict)
    marker_counts: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    scanned_files: int = 0


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - defensive
            return None


def iter_text_files(root: Path, skip_abs: set[Path]):
    for dirpath, dirnames, filenames in os_walk(root):
        here = Path(dirpath)
        kept = []
        for name in dirnames:
            if name in SKIP_DIRS or name.startswith(SKIP_DIR_PREFIXES):
                continue
            if (here / name).resolve() in skip_abs:
                continue
            kept.append(name)
        dirnames[:] = sorted(kept)
        for name in sorted(filenames):
            path = here / name
            if path.resolve() in skip_abs:
                continue
            if name.startswith("fake-data-audit-") and here.name == "reports":
                # the generated report quotes hit snippets; scanning it would
                # inflate every count and make the numbers self-referential.
                continue
            suffix = path.suffix.lower()
            if suffix not in TEXT_SUFFIXES and name not in {
                ".gitignore", ".gitattributes", ".env", ".env.example",
                ".cosyvoice-freeze.txt",
            }:
                continue
            yield path


def os_walk(root: Path):
    """Thin wrapper so the module has a single import surface for os.walk."""
    import os

    return os.walk(root)


def rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_test_path(relpath: str) -> bool:
    return relpath.startswith("tests/")


def is_doc_path(relpath: str) -> bool:
    return relpath.startswith("docs/") or relpath.endswith(".md")


def snippet_of(line: str, limit: int = 120) -> str:
    text = line.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def classify_abs(relpath: str, snippet: str) -> tuple[str, str]:
    if is_test_path(relpath):
        return ("测试内绝对路径", "移入 tests 夹具（tmp_path / 环境变量 / 相对路径）")
    if relpath in {".env.example", "config/low.toml", "config/standard.toml", "config/remote.toml"}:
        return ("本机路径配置项", "待人工确认（配置示例/本机配置：确认是否随仓库分发）")
    if is_doc_path(relpath):
        return ("文档示例路径", "保留（文档示例；确认不含真实私有路径）")
    if relpath.startswith("scripts/agent_board/"):
        return ("看板脚本固定项目根", "待人工确认（看板脚本硬编码 D:\\fatfish-lab 属既定设计）")
    return ("源码/脚本绝对路径", "待人工确认（改为项目相对路径或 config 项）")


def classify_secret(relpath: str, value: str) -> str:
    if PLACEHOLDER.search(value.strip("\"'")):
        return "占位/假值，保留（确认非真实凭据）"
    if is_test_path(relpath):
        return "移入 tests 夹具（改用常量或环境变量，勿用真实值）"
    return "待人工确认（疑似真实凭据：外移 .env 并轮换）"


def classify_net(relpath: str, line: str) -> tuple[str, str]:
    local = bool(LOCAL_HINT.search(line))
    mocked = bool(MOCK_HINT.search(line))
    if is_test_path(relpath):
        if mocked:
            return ("测试内网络（已 mock）", "保留（已 mock，不直连）")
        if local:
            return ("测试内本地回环", "保留（仅打 127.0.0.1 / localhost）")
        return ("测试内网络调用", "移入 tests 夹具（改为本地 stub 或回环服务）")
    if relpath.startswith("fatfish/"):
        if local:
            return ("产品内本地服务调用", "保留（本地 HTTP 服务，属产品设计）")
        return ("产品内外网调用", "保留（确认 host 可配置且在白名单内）")
    return ("脚本内网络调用", "待人工确认（一次性脚本，确认是否仍需要）")


def scan_file(path: Path, root: Path, audit: Audit) -> None:
    text = read_text(path)
    if text is None:
        return
    audit.scanned_files += 1
    relpath = rel(path, root)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        snip = snippet_of(line)

        # a) hardcoded absolute paths -------------------------------------
        for match in ABS_ANY.finditer(line):
            kind, suggestion = classify_abs(relpath, snip)
            audit.findings.append(Finding(relpath, lineno, "a", kind, snip, suggestion))
            break
        else:
            posix = POSIX_ABS.search(line)
            if posix:
                kind, suggestion = classify_abs(relpath, snip)
                audit.findings.append(
                    Finding(relpath, lineno, "a", kind + "（POSIX）", snip, suggestion)
                )

        # b) real data / secrets ------------------------------------------
        if REAL_DB.search(line):
            suggestion = (
                "保留（文档/代码引用；测试禁止连真实库）"
                if is_doc_path(relpath)
                else "待人工确认（测试须用 tmp 库；运行时路径应走 config）"
            )
            audit.findings.append(
                Finding(relpath, lineno, "b", "真实数据库引用", snip, suggestion)
            )
        if QZONE_COOKIES.search(line):
            suggestion = "保留（运行时凭据路径）" if not is_test_path(relpath) else "移入 tests 夹具"
            audit.findings.append(
                Finding(relpath, lineno, "b", "QQ空间 Cookie 文件", snip, suggestion)
            )
        if DOTENV.search(line):
            suggestion = (
                "保留（.env 约定；确认 .gitignore 覆盖）"
                if not is_test_path(relpath)
                else "移入 tests 夹具（测试不应读真实 .env）"
            )
            audit.findings.append(
                Finding(relpath, lineno, "b", ".env 引用", snip, suggestion)
            )
        for match in SECRET_LITERAL.finditer(line):
            value = match.group(2)
            audit.findings.append(
                Finding(
                    relpath, lineno, "b", f"疑似密钥字面量（{match.group(1)}）",
                    snip, classify_secret(relpath, value),
                )
            )

        # c) network calls -------------------------------------------------
        if NET_IMPORT.search(line):
            kind, suggestion = classify_net(relpath, line)
            audit.findings.append(Finding(relpath, lineno, "c", kind, snip, suggestion))
        elif is_test_path(relpath):
            url = EXTERNAL_URL.search(line)
            if url:
                audit.findings.append(
                    Finding(
                        relpath, lineno, "c", "测试内外网 host 字面量", snip,
                        "移入 tests 夹具（改用 example.com 以外的本地 stub）",
                    )
                )

        # d) test fake data ------------------------------------------------
        if is_test_path(relpath):
            lowered = line.lower()
            for marker, label in FAKE_MARKERS.items():
                if marker == "fake" and not QUOTED_FAKE.search(lowered):
                    # bare ``fake_urlopen`` helpers are fixtures, not payloads
                    continue
                if marker.lower() in lowered:
                    audit.marker_counts[marker] = audit.marker_counts.get(marker, 0) + 1
                    audit.findings.append(
                        Finding(
                            relpath, lineno, "d", f"测试假数据（{label}：{marker}）",
                            snip, "保留（属测试夹具）/ 若外泄到产品则待人工确认",
                        )
                    )


def collect_root_files(root: Path, audit: Audit) -> None:
    """Category e: files scattered in the repo root."""
    staging_py: list[dict[str, object]] = []
    staging_other: list[dict[str, object]] = []
    root_md: list[dict[str, object]] = []
    reference_cache: dict[str, list[str]] = {}

    def referenced_by(name: str) -> list[str]:
        """Return files mentioning ``name``; ``import ...`` hits are flagged."""
        if name in reference_cache:
            return reference_cache[name]
        hits: list[str] = []
        stem = Path(name).stem
        import_re = re.compile(rf"^\s*(?:import|from)\s+{re.escape(stem)}\b", re.M)
        for base in ("tests", "docs", "fatfish", "scripts"):
            folder = root / base
            if not folder.is_dir():
                continue
            for dirpath, dirnames, filenames in os_walk(folder):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for filename in filenames:
                    path = Path(dirpath) / filename
                    if path.suffix.lower() not in {".py", ".md", ".txt", ".json"}:
                        continue
                    body = read_text(path)
                    if not body or name not in body:
                        continue
                    marker = "import:" if path.suffix == ".py" and import_re.search(body) else "mention:"
                    hits.append(marker + rel(path, root))
        reference_cache[name] = hits
        return hits

    for path in sorted(root.iterdir()):
        name = path.name
        if path.is_dir():
            if name.startswith("staging_") and not name.startswith("staging_tmp"):
                staging_other.append({
                    "path": name + "/",
                    "kind": "staging 资产目录",
                    "suggestion": "保留（模型/样例资产，勿删；如需瘦身另开卡）",
                })
            continue
        if name.startswith("staging_"):
            refs = referenced_by(name) if path.suffix == ".py" else []
            if path.suffix == ".py":
                imported = [r for r in refs if r.startswith("import:")]
                mentioned = [r for r in refs if r.startswith("mention:")]
                if imported:
                    suggestion = "待人工确认（被产品/测试代码 import：先迁移引用再归档）"
                elif mentioned:
                    suggestion = "待人工确认（仅被注释/文档提及：先改文案再归档）"
                else:
                    suggestion = "待人工确认（未被引用：可归档到 staging/legacy/，勿直接删）"
                staging_py.append({
                    "path": name,
                    "size": path.stat().st_size,
                    "referenced_by": refs[:5],
                    "suggestion": suggestion,
                })
            else:
                staging_other.append({
                    "path": name,
                    "size": path.stat().st_size,
                    "kind": "staging 规格/文本",
                    "suggestion": "待人工确认（规格类：移入 docs/ 或 staging/ 归档）",
                })
            continue
        if path.suffix.lower() == ".md":
            if name in {"README.md", "AGENTS.md"}:
                continue
            root_md.append({
                "path": name,
                "size": path.stat().st_size,
                "suggestion": "待人工确认（报告类：归档进 docs/reports/，需 boss 确认）",
            })

    audit.root_files = {
        "staging_py": staging_py,
        "staging_other": staging_other,
        "root_md": root_md,
    }
    count = len(staging_py) + len(staging_other) + len(root_md)
    audit.counts_by_category["e"] = count
    audit.counts_by_kind["根目录 staging_*.py"] = len(staging_py)
    audit.counts_by_kind["根目录 staging_* 其他"] = len(staging_other)
    audit.counts_by_kind["根目录散落 *.md"] = len(root_md)
    for item in staging_py + staging_other + root_md:
        audit.findings.append(
            Finding(
                str(item["path"]), 1, "e", str(item.get("kind") or "根目录 staging 脚本"),
                f"{item['path']} ({item.get('size', '-')} B)",
                str(item["suggestion"]),
            )
        )


def run(root: Path, out_dir: Path | None) -> Audit:
    audit = Audit()
    skip_abs = {out_dir.resolve()} if out_dir and out_dir.exists() else set()
    for path in iter_text_files(root, skip_abs):
        scan_file(path, root, audit)
    collect_root_files(root, audit)

    counts: dict[str, int] = {"a": 0, "b": 0, "c": 0, "d": 0}
    for finding in audit.findings:
        if finding.category in counts:
            counts[finding.category] += 1
        audit.counts_by_kind[finding.category + ":" + finding.kind] = (
            audit.counts_by_kind.get(finding.category + ":" + finding.kind, 0) + 1
        )
    counts["e"] = audit.counts_by_category.get("e", 0)
    audit.counts_by_category = counts
    return audit


# ------------------------------------------------------------------ output ---


def fmt_finding(finding: Finding) -> str:
    return (
        f"  - {finding.path}:{finding.line} [{finding.kind}] {finding.snippet}\n"
        f"    → {finding.suggestion}"
    )


def render_summary(audit: Audit, root: Path) -> str:
    lines = [
        "假数据 / 脏引用盘点（只读扫描，未做任何改动）",
        f"仓库根：{root}",
        f"扫描文本文件：{audit.scanned_files} 个",
        "排除目录：" + ", ".join(
            sorted(list(SKIP_DIRS)) + [p + "*" for p in SKIP_DIR_PREFIXES]
        ),
        "",
        "各类命中数量：",
    ]
    for key in ("a", "b", "c", "d", "e"):
        lines.append(f"  {CATEGORY_TITLES[key]}：{audit.counts_by_category.get(key, 0)} 条")

    for key in ("a", "b", "c", "d"):
        items = [f for f in audit.findings if f.category == key]
        if not items:
            continue
        lines.append("")
        lines.append(f"{CATEGORY_TITLES[key]}（前 {min(len(items), DETAIL_CAP)} / {len(items)} 条）")
        lines.extend(fmt_finding(f) for f in items[:DETAIL_CAP])
        if len(items) > DETAIL_CAP:
            lines.append(f"  … 其余 {len(items) - DETAIL_CAP} 条见 --json 输出")

    lines.append("")
    lines.append(f"{CATEGORY_TITLES['e']}（{audit.counts_by_category.get('e', 0)} 项）")
    for group, title in (
        ("staging_py", "根目录 staging_*.py"),
        ("staging_other", "根目录 staging_* 其他"),
        ("root_md", "根目录散落 *.md"),
    ):
        rows = audit.root_files.get(group) or []
        lines.append(f"  {title}：{len(rows)} 个")
        for row in rows[:DETAIL_CAP]:
            lines.append(f"    - {row['path']} → {row['suggestion']}")
        if len(rows) > DETAIL_CAP:
            lines.append(f"    … 其余 {len(rows) - DETAIL_CAP} 个见 --json 输出")
    lines.append("")
    lines.append("提示：本脚本只读，不做任何删除/移动/重命名；清理动作需人工逐条确认。")
    return "\n".join(lines)


def to_payload(audit: Audit, root: Path) -> dict[str, object]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "scanned_files": audit.scanned_files,
        "excluded_dirs": sorted(SKIP_DIRS | {p + "*" for p in SKIP_DIR_PREFIXES}),
        "counts": audit.counts_by_category,
        "counts_by_kind": audit.counts_by_kind,
        "marker_counts": audit.marker_counts,
        "root_files": audit.root_files,
        "findings": [
            {
                "path": f.path, "line": f.line, "category": f.category,
                "kind": f.kind, "snippet": f.snippet, "suggestion": f.suggestion,
            }
            for f in audit.findings
        ],
        "notice": "只读盘点结果；脚本不执行任何删除/移动/重命名。",
    }


def render_markdown(audit: Audit, root: Path) -> str:
    lines = [
        "# 假数据 / 脏引用扫描结果（自动生成）",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 仓库根：`{root.as_posix()}`",
        f"- 扫描文本文件：{audit.scanned_files}",
        "- 排除目录：" + "、".join(f"`{d}`" for d in sorted(SKIP_DIRS)),
        "",
        "## 命中数量",
        "",
        "| 类别 | 数量 |",
        "|---|---|",
    ]
    for key in ("a", "b", "c", "d", "e"):
        lines.append(f"| {CATEGORY_TITLES[key]} | {audit.counts_by_category.get(key, 0)} |")
    for key in ("a", "b", "c", "d"):
        items = [f for f in audit.findings if f.category == key]
        lines += ["", f"## {CATEGORY_TITLES[key]}（{len(items)} 条）", ""]
        if not items:
            lines.append("（无）")
            continue
        lines += ["| 位置 | 类型 | 片段 | 建议动作 |", "|---|---|---|---|"]
        for f in items[:200]:
            snippet = f.snippet.replace("|", "\\|")
            lines.append(f"| `{f.path}:{f.line}` | {f.kind} | `{snippet}` | {f.suggestion} |")
        if len(items) > 200:
            lines.append(f"| … | 其余 {len(items) - 200} 条 | 见 JSON | — |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读盘点仓库里的假数据/脏引用")
    parser.add_argument("--out", type=Path, help="输出目录（不传则只打 stdout 摘要）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（配合 --out 写文件）")
    parser.add_argument("--md", action="store_true", help="输出 Markdown（配合 --out 写文件）")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    out_dir = args.out.resolve() if args.out else None
    audit = run(root, out_dir)

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json = args.json or not args.md
        write_md = args.md or not args.json
        if write_json:
            target = out_dir / "audit_fake_data.json"
            target.write_text(
                json.dumps(to_payload(audit, root), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8", newline="\n",
            )
            print(f"wrote {target}", file=sys.stderr)
        if write_md:
            target = out_dir / "audit_fake_data.md"
            target.write_text(render_markdown(audit, root), encoding="utf-8", newline="\n")
            print(f"wrote {target}", file=sys.stderr)

    if args.json and not out_dir:
        print(json.dumps(to_payload(audit, root), ensure_ascii=False, indent=2))
    else:
        print(render_summary(audit, root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
