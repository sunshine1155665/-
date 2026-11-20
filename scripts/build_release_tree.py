#!/usr/bin/env python3
"""build_release_tree.py -- 把主工作区(fatfish-lab)同步进脱敏发布树(fatfish-release)。

可重跑的发布树组装/同步器（github-publish-plan-2026-10-30.md §四「组装清单脚本化」的落地，
规则同时编码自 T-0059 组装记录）。本文件不含任何秘密：敏感值只存在
staging/release-sync/scrub.local.toml（gitignored），由 --gen-scrub 程序化提取。

分类引擎（对发布树每个跟踪文件，以主仓快照提交为基线）：
  IN_SYNC          发布版 == transform(主仓当前版)            -> 无事可做
  SYNC             发布版 == transform(快照版)                -> 组装是机械的，安全重生成
  PAIRED_SYNC      发布版 == apply_pairs(transform(快照版))    -> 组装=机械+可推导替换对，
                   且替换对能完全解释手改 -> transform(当前版)+替换对 重生成
  KEEP_HANDEDITED  主仓快照后未动过的手改件                    -> 保留发布版
  MERGE            手改件且主仓也动了、替换对解释不了           -> 人工合并（报告给 diff）
  ADD / EXCLUDED   主仓新增文件按白名单收录/排除
  RELEASE_ONLY     发布树专属件，永不覆盖

用法：
  python scripts/build_release_tree.py --plan      # 分类报告（不写盘，报告落 staging/release-sync/）
  python scripts/build_release_tree.py --apply     # 执行同步（MERGE 项跳过）
  python scripts/build_release_tree.py --gen-scrub # 生成/刷新脱敏映射 scrub.local.toml
  python scripts/build_release_tree.py --gate      # 发布树敏感串复查（独立可跑）
选项：--dst <dir>（默认 ../fatfish-release）--snapshot <sha>（跳过自动探测）
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent.parent
DEFAULT_DST = LAB.parent / "fatfish-release"
SYNC_DIR = LAB / "staging" / "release-sync"
SCRUB_FILE = SYNC_DIR / "scrub.local.toml"
REPORT_FILE = SYNC_DIR / "plan-report.txt"

# ---------------------------------------------------------------- 路径映射

# 唱歌产线只保留 YuE2->RVC renderer；旧 SoulX 15 件是历史遗留，已从主仓删除。
# 目录仍是 soulx/ -> soulx/，这里保留映射表是为了兼容旧快照的路径推导。
SOULX_FILES = {"song_yue2_rvc"}

# 主仓路径 -> 发布树路径 的改名（其余为同名）
RENAME_MAP = {}

# 发布树专属件：永不从主仓重生成（T-0059 ⑤ 新写文档/脚本 + 规则文件）
RELEASE_ONLY = {
    "README.md", "AGENTS.md", "CONTRIBUTING.md", "LICENSE",
    "THIRD_PARTY_NOTICES.md", "RELEASE_NOTES_DRAFT.md",
    "config/example.toml", "scripts/install_assets.py",
    ".gitignore", ".gitattributes",
}

# 发布树 docs 白名单（在 T-0059 精简版基础上 + 本轮新增；pskey/云服务器清单不入）
DOCS_INCLUDE = {
    "docs/README.md",  # GENERATED：apply 后由发布树 docs_index.py --write 重生成
    "docs/adr/README.md",
    "docs/overview/architecture.md", "docs/overview/roadmap.md",
    "docs/overview/project-map.md",
    "docs/runbooks/startup.md",
    "docs/branches/activity.md", "docs/branches/assets.md", "docs/branches/media.md",
    "docs/branches/memory.md", "docs/branches/ops.md", "docs/branches/platforms.md",
    "docs/branches/runtime.md", "docs/branches/security.md",
    "docs/branches/singing.md", "docs/branches/voice.md",
}
DOCS_INCLUDE |= {f"docs/adr/{n:04d}-{s}.md" for n, s in [
    (1, "indextts25"), (2, "singing-soulx"), (3, "memory-hierarchy"), (4, "qzone-feed"),
    (5, "share-desire"), (6, "friend-share"), (7, "agent-loop"), (8, "catchup"),
    (9, "continue-policy"), (10, "sing-action"), (11, "qzone-browser-renew"),
    (12, "online-tier"), (13, "session-visit"), (14, "session-roaming"),
    (15, "singing-eval-determinism"), (16, "yue2-rvc-singing-chain"),
    (17, "singing-all-yue2-rvc"), (18, "platform-prompt-variants"),
    (18, "singing-full-lyrics"),
]}

# 发布树 scripts 白名单（T-0059 保留 30 件 + 本轮新测试依赖的 3 件）
SCRIPTS_INCLUDE = {
    "anima_photo_worker.py", "backup.bat", "bili_login_ui.py", "cosyvoice_soft_server.py",
    "docs_index.py", "doctor.bat", "embedding_server.py",
    "indextts_server.py", "init.bat", "make_app.py", "napcat_supervisor.py",
    "napcat_qr_mcp.py", "napcat_qr_setup.py", "napcat_watch.py",
    "nightly.bat", "preflight.py", "qq_login_ui.py", "qzone_login_ui.py",
    "serve_audio.py", "serve_stats.py", "setup_napcat.ps1", "start.bat", "start_all.py",
    "start_bili_login.bat", "start_napcat.bat", "start_qq_login.bat",
    "start_qzone_login.bat", "start_tts_soft.bat", "stop.bat", "stop_tts_soft.bat",
    "photo_server.py", "sing_server.py", "qqbot_service.py", "pet_gateway.py",
    "text2melory_placeholder",  # 占位防手滑，见下方过滤
    "upload_audio.py",
}
SCRIPTS_INCLUDE.discard("text2melory_placeholder")

# 主仓这些路径不进发布树（研发专用）
LAB_TESTS_EXCLUDE = {
    "tests/test_night_test.py", "tests/test_memory_bundle.py",
    "tests/test_photo_remote.py", "tests/test_sing_remote.py",
    "tests/test_napcat_watch.py",
    # Desktop shell is not shipped in the release tree; its generator scripts
    # and tests stay local-only.
    "tests/test_pet_motion_gen.py", "tests/test_pet_sticker_gen.py",
}
LAB_TOP_EXCLUDE = {
    "AGENTS.md", "README.md", "LICENSE",  # LICENSE 主仓版与发布版各自维护
    "github-publish-plan-2026-10-30.md", "memory_v6_review_2026-10-24.md",
    "表情包与服务控制面-方案分析.md", "贴纸向量替换报告.md",
    "served.html", "headers.txt", ".env", ".env.example_keep_lab",
    "requirements.txt_keep_lab",
}
LAB_DIR_EXCLUDE = {
    "staging", "staging_soulx", "data", "models", "vendor", "dist", "checkpoints",
    "work", "repos", "legacy", "pet-shell", "tasks", "skills",
    ".git", ".zcode", ".workbuddy", ".opencode", ".agent-board", ".venv-cosyvoice",
    ".venv-indextts", ".venv-soulx", "__pycache__", "web_placeholder",
}
LAB_DIR_EXCLUDE.discard("web_placeholder")

# tests/eval 等非 test_ 文件按扩展名放行
TEST_DATA_SUFFIXES = {".jsonl", ".json", ".txt"}

# ---------------------------------------------------------------- 文本处理

EOL_CRLF_SUFFIXES = {".bat", ".ps1"}


def to_rel_path(lab_rel: str) -> str | None:
    """主仓相对路径 -> 发布树相对路径；不收录返回 None。"""
    if lab_rel in RENAME_MAP:
        return RENAME_MAP[lab_rel]
    p = Path(lab_rel)
    if p.parts[0] == "soulx":
        return lab_rel
    if p.parts[0] == "staging_soulx" and p.stem in SOULX_FILES and p.suffix == ".py":
        return "soulx/" + p.name
    return lab_rel


def load_scrub() -> list[tuple[str, str]]:
    if not SCRUB_FILE.exists():
        return []
    text = SCRUB_FILE.read_text(encoding="utf-8")
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        try:
            pairs.append((json.loads(k.strip()), json.loads(v.strip())))
        except json.JSONDecodeError:
            print(f"[scrub] 无法解析行（跳过）: {line[:60]}...")
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    return pairs


def transform(text: str, rel_path: str, scrub: list[tuple[str, str]]) -> str:
    """主仓文本 -> 发布树文本：归一化 -> staging_soulx 改写 -> 脱敏替换 -> 行尾策略。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("staging_soulx", "soulx")
    for old, new in scrub:
        # 「[已脱敏]」占位对只作门禁钥匙（真实值检测），不做内容替换——
        # 否则会把上下文相关的占位约定（100.x.y.z 等）写坏。
        if new == "[已脱敏]":
            continue
        if old in text:
            text = text.replace(old, new)
    if Path(rel_path).suffix.lower() in EOL_CRLF_SUFFIXES:
        text = text.replace("\r\n", "\n")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        text = "\r\n".join(lines) + ("\r\n" if lines else "")
    return text


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class LabGit:
    def __init__(self, snapshot: str | None):
        self.snapshot = snapshot
        self._cache: dict[tuple[str, str], str | None] = {}
        self._log_cache: dict[str, list[str]] = {}

    def show(self, sha: str, lab_rel: str) -> str | None:
        key = (sha, lab_rel)
        if key not in self._cache:
            r = subprocess.run(
                ["git", "-C", str(LAB), "show", f"{sha}:{lab_rel}"],
                capture_output=True)
            self._cache[key] = r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None
        return self._cache[key]

    def snap(self, lab_rel: str) -> str | None:
        return self.show(self.snapshot, lab_rel) if self.snapshot else None

    def touching_commits(self, lab_rel: str, cap: int = 60) -> list[str]:
        """改动过该文件的提交，新→旧。"""
        if lab_rel not in self._log_cache:
            r = subprocess.run(
                ["git", "-C", str(LAB), "log", "--format=%H", "--", lab_rel],
                capture_output=True, text=True)
            self._log_cache[lab_rel] = [
                line.strip() for line in r.stdout.splitlines() if line.strip()][:cap]
        return self._log_cache[lab_rel]

    def find_assembled_commit(self, lab_rel: str, rel_now: str, scrub) -> str | None:
        """最新一个「transform(提交版)==发布版」的提交：说明该文件由它机械组装。"""
        for sha in self.touching_commits(lab_rel):
            content = self.show(sha, lab_rel)
            if content is not None and rel_now == transform(content, lab_rel, scrub):
                return sha
        return None


def detect_snapshot(rel_files: dict[str, str]) -> str:
    """按「transform(快照版)==发布版」命中率最高的提交探测快照。"""
    r = subprocess.run(
        ["git", "-C", str(LAB), "log", "--format=%H %ci",
         "--since=2026-10-29 00:00", "--until=2026-10-30 04:00"],
        capture_output=True, text=True)
    cands = [line.split()[0] for line in r.stdout.splitlines() if line.strip()]
    if not cands:
        sys.exit("快照探测失败：窗口内没有提交，用 --snapshot 手动指定")
    scrub = load_scrub()
    sample = [rp for rp in rel_files
              if rp.startswith(("fatfish/", "tests/")) and not rp.endswith(".bat")][:60]
    best, best_rate = None, -1.0
    for sha in cands:
        git = LabGit(sha)
        hit = total = 0
        for rp in sample:
            lab_rel = rel_to_lab(rp)
            if lab_rel is None:
                continue
            snap = git.snap(lab_rel)
            if snap is None:
                continue
            total += 1
            if read_text(DEFAULT_DST / rp) == transform(snap, rp, scrub):
                hit += 1
        rate = hit / total if total else 0
        print(f"  快照候选 {sha[:10]} 命中率 {hit}/{total} = {rate:.0%}")
        if rate > best_rate:
            best, best_rate = sha, rate
    print(f"快照提交 = {best}（命中率 {best_rate:.0%}）")
    return best


def rel_to_lab(rel_path: str) -> str | None:
    """发布树相对路径 -> 主仓相对路径（含改名还原）。"""
    p = Path(rel_path)
    if p.parts[0] == "soulx" and p.suffix == ".py":
        # 2026-11-20: 产线 renderer 已迁到主仓 soulx/；旧 staging_soulx 已删除。
        local = "soulx/" + p.name
        if (LAB / local).exists():
            return local
        return "staging_soulx/" + p.name
    for lab_rel, rel in RENAME_MAP.items():
        if rel == rel_path:
            return lab_rel
    return rel_path


# ---------------------------------------------------------------- 分类引擎

def classify(rel_files: dict[str, str], dst: Path, git: LabGit,
             scrub: list[tuple[str, str]]) -> dict[str, list[tuple[str, str]]]:
    """返回 bucket -> [(rel_path, note)]。rel_files: rel_path -> lab_rel。"""
    buckets: dict[str, list[tuple[str, str]]] = {}
    for rp, lab_rel in sorted(rel_files.items()):
        if rp in RELEASE_ONLY or rp == "docs/README.md":
            buckets.setdefault("RELEASE_ONLY", []).append((rp, "发布树专属/生成物"))
            continue
        rel_now = read_text(dst / rp)
        lab_now = read_text(LAB / lab_rel)
        t_now = transform(lab_now, rp, scrub)
        if rel_now == t_now:
            buckets.setdefault("IN_SYNC", []).append((rp, ""))
            continue
        snap = git.snap(lab_rel)
        if snap is not None and rel_now == transform(snap, rp, scrub):
            buckets.setdefault("SYNC", []).append((rp, "机械组装(快照)，重生成"))
            continue
        asm_sha = git.find_assembled_commit(lab_rel, rel_now, scrub)
        if asm_sha is not None:
            buckets.setdefault("SYNC", []).append((rp, f"机械组装({asm_sha[:8]})，重生成"))
            continue
        base = None
        if snap is not None:
            base = transform(snap, rp, scrub)
        else:
            # 组装时还未入库的文件：用主仓引入提交作兜底基线
            commits = git.touching_commits(lab_rel)
            if commits:
                oldest = git.show(commits[-1], lab_rel)
                if oldest is not None:
                    base = transform(oldest, rp, scrub)
        if base is None:
            buckets.setdefault("MERGE", []).append((rp, "无任何基线，需人工"))
            continue
        pairs = derive_pairs(base, rel_now)
        if pairs and apply_pairs(base, pairs) == rel_now:
            cand = apply_pairs(t_now, pairs)
            if cand != rel_now:
                buckets.setdefault("PAIRED_SYNC", []).append(
                    (rp, f"{len(pairs)} 对替换"))
                PAIRED[rp] = pairs
                continue
        merged, conflicts = three_way_merge(base, rel_now, t_now)
        if conflicts == 0:
            if merged != rel_now:
                buckets.setdefault("AUTO_MERGE", []).append((rp, "三方合并无冲突"))
                AUTO_MERGED[rp] = merged
            elif norm(t_now) == norm(base):
                buckets.setdefault("KEEP_HANDEDITED", []).append((rp, "手改件，主仓未动"))
            else:
                buckets.setdefault("KEEP_HANDEDITED", []).append((rp, "主仓增量已在发布版中"))
            continue
        buckets.setdefault("MERGE", []).append(
            (rp, f"三方合并 {conflicts} 处冲突，需人工"))
        continue

    # 主仓新增（发布树还没有的）
    for lab_rel in lab_candidates():
        rp = to_rel_path(lab_rel)
        if rp is None or (dst / rp).exists() or rp in rel_files.values():
            continue
        bucket, note = new_file_bucket(lab_rel, rp)
        buckets.setdefault(bucket, []).append((rp, f"({lab_rel}) {note}"))
    return buckets


PAIRED: dict[str, list[tuple[str, str]]] = {}
AUTO_MERGED: dict[str, str] = {}


def three_way_merge(base: str, ours: str, theirs: str) -> tuple[str, int]:
    """git merge-file 三方合并；返回 (合并文本, 冲突数)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        bp, op, tp = td_path / "base", td_path / "ours", td_path / "theirs"
        bp.write_text(base, encoding="utf-8", newline="")
        op.write_text(ours, encoding="utf-8", newline="")
        tp.write_text(theirs, encoding="utf-8", newline="")
        r = subprocess.run(
            ["git", "merge-file", "-p", "-L", "release", "-L", "base", "-L", "lab",
             str(op), str(bp), str(tp)],
            capture_output=True)
        text = r.stdout.decode("utf-8", errors="replace")
        conflicts = r.returncode if r.returncode > 0 else 0
        return text, conflicts


def norm(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def apply_pairs(text: str, pairs: list[tuple[str, str]]) -> str:
    for old, new in sorted(pairs, key=lambda kv: len(kv[0]), reverse=True):
        text = text.replace(old, new)
    return text


def derive_pairs(before: str, after: str) -> list[tuple[str, str]] | None:
    """行级 diff 推导「组装替换对」；含增删行（而非替换）时返回 None。"""
    sm = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines())
    pairs: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                pairs.append((before.splitlines()[i1 + k], after.splitlines()[j1 + k]))
        elif tag in ("delete", "insert"):
            return None
    return pairs or None


def lab_candidates() -> list[str]:
    out: list[str] = []
    for p in LAB.rglob("*"):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(LAB).as_posix()
        parts = p.relative_to(LAB).parts
        if parts[0] in LAB_DIR_EXCLUDE:
            continue
        if rel in LAB_TOP_EXCLUDE:
            continue
        if parts[0] == "scripts":
            if p.suffix == ".py" and p.name not in SCRIPTS_INCLUDE:
                continue
            if p.suffix in (".bat", ".ps1") and p.name not in SCRIPTS_INCLUDE:
                continue
            if p.suffix not in (".py", ".bat", ".ps1"):
                continue
        elif parts[0] == "tests":
            if p.name.startswith("test_") and rel in LAB_TESTS_EXCLUDE:
                continue
            if not p.name.startswith("test_") and p.suffix not in TEST_DATA_SUFFIXES:
                continue
        elif parts[0] == "docs":
            if rel not in DOCS_INCLUDE:
                continue
        elif parts[0] in ("fatfish", "web", "deploy", "config"):
            if parts[0] == "config" and p.name == "night-test.toml":
                continue
        elif parts[0] == "soulx":
            pass
        else:
            if rel not in {"pyproject.toml", "requirements.txt", ".env.example",
                           ".cosyvoice-freeze.txt", "启动大肥鱼.bat", "QQ登录.bat",
                           "QQ空间登录.bat", "QQ空间免扫码登录.bat", "B站登录.bat"}:
                continue
        out.append(rel)
    return sorted(set(out))


def new_file_bucket(lab_rel: str, rp: str) -> tuple[str, str]:
    if rp in DOCS_INCLUDE and rp != "docs/README.md":
        return "ADD", "docs 白名单"
    if Path(rp).parts[0] == "scripts":
        return "ADD", "scripts 白名单"
    if Path(rp).parts[0] in ("fatfish", "web", "deploy", "soulx"):
        return "ADD", "产品目录"
    if Path(rp).parts[0] == "tests":
        if rp in LAB_TESTS_EXCLUDE:
            return "EXCLUDED", "研发测试"
        return "ADD", "新测试"
    if Path(rp).parts[0] == "config":
        return "ADD", "config profile"
    return "EXCLUDED", "非白名单"


# ---------------------------------------------------------------- 脱敏映射生成

SCRUB_SOURCES = [
    # (主仓文件, 行锚点正则, 值形状)  —— 锚点来自 github-publish-plan §五 ② 点名位置
    ("scripts/napcat_supervisor.py", r"uin|机器人", "qq"),
    ("scripts/memory_recall_eval.py", r"uin|主人|qq|owner", "qq"),
    ("tests/test_friend_slot.py", r"uin|qq|friend|session", "qq"),
    ("tests/test_memory_pollution.py", r"uin|qq|owner|value", "qq"),
    ("scripts/serve_audio.py", r"--host|tailscale", "ip"),
    ("scripts/napcat_qr_setup.py", r"DEFAULT_PUBLIC|https?://", "ip"),
    ("fatfish/remote.py", r"endpoints 形如|base_url|http", "ip"),
    ("docs/branches/platforms.md", r"uin|机器人|napcat_supervisor", "qq"),
    ("docs/branches/ops.md", r"tailscale|ts\.net|https?://", "ip"),
    ("docs/runbooks/startup.md", r"tailscale|ts\.net|uin|qq", "both"),
]
QQ_RE = r"(?<![\d.])(\d{6,11})(?![\d.])"
IP_RE = r"(\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9-]+\.ts\.net)"
ALLOWED = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "100.x.y.z", "100.x.y.z"}


def _valid(kind: str, tok: str) -> bool:
    if tok in ALLOWED:
        return False
    if re.fullmatch(r"\d{8}", tok):  # 纯日期（YYYYMMDD）
        return False
    if kind in ("ip", "both") and "." in tok and tok.split(".")[0].isdigit():
        parts = tok.split(".")
        if all(p.isdigit() for p in parts) and not all(int(p) <= 255 for p in parts):
            return False  # 版本号（某段 >255）
    if kind == "qq" and tok.startswith("0"):
        return False
    return True


def gen_scrub(dst: Path) -> None:
    lines = ["# 由 build_release_tree.py --gen-scrub 程序化生成；本地专用，绝不提交。",
             "# 锚定行提取（github-publish-plan §五 ② 点名位置），值只作门禁钥匙。", ""]
    total = 0
    for lab_rel, anchor, kind in SCRUB_SOURCES:
        src_path = LAB / lab_rel
        if not src_path.exists():
            print(f"[gen-scrub] 主仓缺 {lab_rel}，跳过")
            continue
        found: list[str] = []
        for line in read_text(src_path).splitlines():
            if not re.search(anchor, line, re.IGNORECASE):
                continue
            pats = {"qq": [QQ_RE], "ip": [IP_RE], "both": [QQ_RE, IP_RE]}[kind]
            for pat in pats:
                for tok in re.findall(pat, line):
                    if _valid(kind, tok) and tok not in found:
                        found.append(tok)
        if not found:
            print(f"[gen-scrub] {lab_rel}: 锚点行未发现敏感值")
            continue
        lines.append(f"# {lab_rel} ({kind})")
        for tok in found:
            lines.append(f"{json.dumps(tok)} = {json.dumps('[已脱敏]')}")
            total += 1
            print(f"[gen-scrub]   {lab_rel}: 提取 1 值 (len={len(tok)})")
    SCRUB_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCRUB_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[gen-scrub] 写入 {total} 条 -> {SCRUB_FILE}")


# ---------------------------------------------------------------- 安全门

GATE_RES = [
    (r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", {"127.0.0.1", "0.0.0.0", "255.255.255.255"}),
    (r"[A-Za-z0-9-]+\.ts\.net", set()),
    (r"Comfy-Desktop", set()),
    (r"[Dd]:[\\/]+fatfish", set()),
    (r"\.workbuddy", set()),
]


def gate(dst: Path, scrub: list[tuple[str, str]]) -> int:
    hits = 0
    r = subprocess.run(["git", "-C", str(dst), "ls-files"], capture_output=True, text=True)
    files = [f for f in r.stdout.splitlines() if f.strip()]
    for f in files:
        p = dst / f
        if not p.exists() or p.suffix.lower() in {".png", ".jpg", ".wav", ".zip", ".pt", ".pth"}:
            continue
        try:
            text = read_text(p)
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"Mozilla|user_agent", line, re.IGNORECASE):
                continue  # 浏览器 UA 里的版本号会伪装成 IP
            for pat, allow in GATE_RES:
                for m in re.finditer(pat, line):
                    tok = m.group(0)
                    if tok in allow:
                        continue
                    if pat.endswith(r"\.ts\.net") and (
                            "example" in tok or "tailnet" in tok or "<" in line):
                        continue  # 占位主机名（example-tailnet / home.tailnet 等）
                    print(f"[gate] {f}:{i}: {pat}")
                    hits += 1
            for old, _ in scrub:
                if old in line:
                    print(f"[gate] {f}:{i}: 命中脱敏映射原值")
                    hits += 1
    print(f"[gate] 命中 {hits} 处" + ("（需处理）" if hits else "，通过"))
    return hits


# ---------------------------------------------------------------- 主流程

def rel_file_map(dst: Path) -> dict[str, str]:
    r = subprocess.run(["git", "-C", str(dst), "ls-files"], capture_output=True, text=True)
    mapping = {}
    for f in r.stdout.splitlines():
        f = f.strip()
        if not f:
            continue
        lab = rel_to_lab(f)
        if lab and (LAB / lab).exists():
            mapping[f] = lab
    return mapping


def write_report(buckets, dst: Path, scrub) -> None:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_FILE.open("w", encoding="utf-8") as fh:
        for name in ("MERGE", "AUTO_MERGE", "PAIRED_SYNC", "SYNC", "ADD",
                     "KEEP_HANDEDITED", "IN_SYNC", "RELEASE_ONLY", "EXCLUDED"):
            items = buckets.get(name, [])
            fh.write(f"\n== {name} ({len(items)}) ==\n")
            for rp, note in items:
                fh.write(f"  {rp}  {note}\n")
        fh.write("\n== MERGE 明细 diff（发布版 vs transform(主仓当前版)，前 40 行）==\n")
        for rp, _ in buckets.get("MERGE", []):
            lab_rel = rel_to_lab(rp)
            rel_now = read_text(dst / rp)
            t_now = transform(read_text(LAB / lab_rel), rp, scrub)
            fh.write(f"\n-- {rp} --\n")
            diff = list(difflib.unified_diff(
                rel_now.splitlines(), t_now.splitlines(),
                "release", "lab-transformed", lineterm=""))
            fh.write("\n".join(diff[:40]) + "\n")
    print(f"报告 -> {REPORT_FILE}")


def apply_changes(buckets, dst: Path, scrub) -> tuple[int, int]:
    written = skipped = 0
    for rp, _ in buckets.get("AUTO_MERGE", []):
        out = dst / rp
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(AUTO_MERGED[rp], encoding="utf-8", newline="")
        written += 1
    for bucket in ("SYNC", "PAIRED_SYNC", "ADD"):
        for rp, _ in buckets.get(bucket, []):
            lab_rel = rel_to_lab(rp)
            src = LAB / lab_rel
            if not src.exists():
                print(f"[apply] 源缺失，跳过 {rp}")
                skipped += 1
                continue
            text = transform(read_text(src), rp, scrub)
            if bucket == "PAIRED_SYNC":
                text = apply_pairs(text, PAIRED.get(rp, []))
            out = dst / rp
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8", newline="")
            written += 1
    print(f"[apply] 写入 {written} 个文件，跳过 {skipped}")
    return written, skipped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--gen-scrub", action="store_true")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--dst", type=Path, default=DEFAULT_DST)
    ap.add_argument("--snapshot", default=None)
    args = ap.parse_args()

    if args.gen_scrub:
        gen_scrub(args.dst)
        return
    scrub = load_scrub()
    if args.gate:
        sys.exit(1 if gate(args.dst, scrub) else 0)

    rel_files = rel_file_map(args.dst)
    snapshot = args.snapshot or detect_snapshot(rel_files)
    git = LabGit(snapshot)
    buckets = classify(rel_files, args.dst, git, scrub)

    for name in ("MERGE", "AUTO_MERGE", "PAIRED_SYNC", "SYNC", "ADD",
                 "KEEP_HANDEDITED", "IN_SYNC", "RELEASE_ONLY", "EXCLUDED"):
        items = buckets.get(name, [])
        print(f"{name}: {len(items)}")
        if name in ("MERGE", "AUTO_MERGE", "PAIRED_SYNC", "ADD", "KEEP_HANDEDITED"):
            for rp, note in items:
                print(f"    {rp}  {note}")

    write_report(buckets, args.dst, scrub)
    if args.apply:
        apply_changes(buckets, args.dst, scrub)
        gate(args.dst, scrub)


if __name__ == "__main__":
    main()
