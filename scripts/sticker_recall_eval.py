"""贴纸检索验收探针：金标准问题，miss 的要看得见疼。

和 ``scripts/memory_recall_eval.py`` 同一套思路，只是对象换成贴纸库：

    # 可复现 demo（临时库，合成贴纸，不含任何真实数据）
    python scripts/sticker_recall_eval.py --demo

    # 真库，跑在临时副本上；原库只读，一个字节都不动
    python scripts/sticker_recall_eval.py --db data/fatfish.db

    # 换后端对照（比如上 B 方案前后的对比）
    python scripts/sticker_recall_eval.py --db data/fatfish.db \
        --backend http --url http://127.0.0.1:9881/embed

``--db`` 模式先把副本里的向量按 ``--backend`` 重算（真库里的向量是什么
后端都不影响评测），再对每条金标准查询打印 top-3 与命中情况。哈希后端下
还会打印非零余弦的分位数，``similarity_floor/ceil`` 的锚点值从这里挑。

退出码：``--demo`` 有 miss 就 1（期望是确定的）；``--db`` 默认只报告，
加 ``--strict`` 才按 hit@3 是否全中退出。

金标准查询的期望写成子串/意图标签：命中 = top-N 里有一张的意图标签或
索引文本包含任一 ``any``。不按 id 锚定，打标修订不会把评测搞挂。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fatfish.config import load_config  # noqa: E402
from fatfish.db import Database  # noqa: E402
from fatfish.memory.embedder import HttpEmbedder, dumps  # noqa: E402
from fatfish.sticker.service import StickerService, sticker_index_text  # noqa: E402

# 金标准：查询 -> 期望出现在 top-3 里的字样（索引文本或意图标签）。
# 词表贴着真实库的 usage_description 写，新词入库后在这里补查询。
GOLDEN: list[dict] = [
    {"q": "晚安，我去睡了", "any": ["goodnight", "晚安", "好梦", "睡"]},
    {"q": "我好委屈，呜呜呜", "any": ["委屈"]},
    {"q": "气死我了！", "any": ["生气", "气死"]},
    {"q": "抱抱我，求安慰", "any": ["求安慰", "安慰", "摸头"]},
    {"q": "哈哈哈哈笑死", "any": ["大笑", "笑死", "笑"]},
    {"q": "早安呀，起床啦", "any": ["greeting", "问好", "打招呼", "早安"]},
    {"q": "无语了真的", "any": ["无语"]},
    {"q": "对不起，我搞砸了", "any": ["道歉", "搞砸", "apology"]},
    {"q": "今天心情超好", "any": ["开心", "心情好"]},
    {"q": "肚子饿了想吃东西", "any": ["饿"]},
    {"q": "终于下班啦！", "any": ["下班", "收工"]},
    {"q": "这个模型真厉害", "any": ["夸", "好模型", "厉害"]},
    {"q": "真的惊到我了", "any": ["惊讶", "震惊", "没想到"]},
    {"q": "想你了，么么哒", "any": ["亲亲", "想你", "亲近", "flirt"]},
    {"q": "谁又涨价了，黑心", "any": ["涨价"]},
    {"q": "陪我玩会儿游戏嘛", "any": ["一起玩", "游戏"]},
    {"q": "被夸得好不好意思", "any": ["害羞", "不好意思"]},
    {"q": "困死了，想眯一会儿", "any": ["goodnight", "眯", "困"]},
    {"q": "你也太逗了吧", "any": ["好笑", "调侃", "调皮"]},
    {"q": "给你点个赞", "any": ["赞同", "点赞", "agree"]},
]

# demo 库：合成贴纸 + 确定性期望，miss 就是真回归。意图标签即期望词。
DEMO_LIBRARY: list[dict] = [
    {"caption": "困困闭眼", "usage": "该睡觉了，做个好梦", "intents": ["goodnight"]},
    {"caption": "泪汪汪", "usage": "委屈巴巴，想要抱抱", "intents": ["sad"]},
    {"caption": "气鼓鼓", "usage": "气鼓鼓，真的生气了", "intents": ["angry"]},
    {"caption": "转圈圈", "usage": "开心到转圈圈", "intents": ["happy"]},
    {"caption": "斜眼", "usage": "无语子，斜眼看你", "intents": ["tease"]},
    {"caption": "瞪眼", "usage": "瞪大眼睛，非常惊讶", "intents": ["surprise"]},
    {"caption": "捂脸", "usage": "脸红害羞，捂脸", "intents": ["shy"]},
    {"caption": "合掌", "usage": "谢谢谢谢，感恩", "intents": ["thanks"]},
    {"caption": "冒汗", "usage": "冒汗道歉，对不起", "intents": ["apology"]},
    {"caption": "撒花", "usage": "撒花庆祝胜利", "intents": ["celebrate"]},
    {"caption": "飞吻", "usage": "飞吻，想你了", "intents": ["flirt"]},
    {"caption": "挥手", "usage": "元气满满打招呼", "intents": ["greeting"]},
    {"caption": "捧碗", "usage": "肚子饿了，想吃", "intents": ["meme"]},
]

DEMO_QUERIES: list[dict] = [
    {"q": "我去睡了，晚安", "any": ["goodnight"]},
    {"q": "呜呜呜好委屈", "any": ["sad"]},
    {"q": "真的生气了！", "any": ["angry"]},
    {"q": "今天好开心呀", "any": ["happy"]},
    {"q": "真是无语了", "any": ["tease"]},
    {"q": "什么？居然是这样", "any": ["surprise"]},
    {"q": "哎呀好害羞", "any": ["shy"]},
    {"q": "谢谢你帮我", "any": ["thanks"]},
    {"q": "对不起我错了", "any": ["apology"]},
    {"q": "撒花！成功啦", "any": ["celebrate"]},
    {"q": "想你了，么么哒", "any": ["flirt"]},
    {"q": "嗨，在吗", "any": ["greeting"]},
    {"q": "肚子好饿", "any": ["meme"]},
]


def _parse_row(row) -> tuple[str, str]:
    """(id, 索引文本)：与 ``add_sticker``/``staging_reembed`` 同一原文。"""
    try:
        tags = json.loads(row["tags_json"] or "[]")
        intents = json.loads(row["intent_json"] or "[]")
        content = json.loads(row["content_json"] or "{}")
    except json.JSONDecodeError:
        tags, intents, content = [], [], {}
    text = sticker_index_text(
        row["caption"],
        [str(t) for t in tags] if isinstance(tags, list) else [],
        [str(t) for t in intents] if isinstance(intents, list) else [],
        content if isinstance(content, dict) else {},
    )
    return row["id"], text


def _index_texts(db: Database) -> dict[str, str]:
    return dict(_parse_row(row) for row in db.query(
        "SELECT id, caption, tags_json, intent_json, content_json FROM stickers WHERE active=1"
    ))


def _reembed(service: StickerService, backend: str) -> int:
    """把副本里的向量按 service 自己的 embedder 重算（宽度由构造保证一致）。

    宽度必须跟着 service 走：hash 的维度取自 memory.embedding_dim，这里
    自建一个不同维度的 embedder 就会让全部余弦静默判 0（踩过一次）。
    """
    if isinstance(service.embedder, HttpEmbedder):
        health = service.embedder.health()
        if not health.get("ok"):
            print(f"向量服务不可用：{health}", file=sys.stderr)
            return 1
    db = service.db
    rows = db.query(
        "SELECT id, caption, tags_json, intent_json, content_json FROM stickers WHERE active=1"
    )
    pairs = [_parse_row(row) for row in rows]
    with db.transaction():
        for rid, text in pairs:
            db.execute(
                "UPDATE stickers SET embedding_text=? WHERE id=?",
                (dumps(service.embedder.embed(text)), rid),
            )
    print(f"副本向量已按 {backend} 重算：{len(pairs)} 条")
    return 0


def _make_service(
    work_dir: Path, db_path: Path, backend: str, url: str
) -> tuple[StickerService, Database]:
    toml = work_dir / "eval.toml"
    sticker_lines = ['embedding_backend = "hash"'] if backend == "hash" else [
        'embedding_backend = "http"',
        f'embedding_url = "{url}"',
        "embedding_timeout_seconds = 20.0",
    ]
    toml.write_text(
        f"""
[app]
data_dir = "{(work_dir / "data").as_posix()}"

[storage]
db_path = "{db_path.as_posix()}"

[memory]
embedding_backend = "hash"
embedding_dim = 256

[voice]
enabled = false

[tts]
enabled = false

[sticker]
enabled = true
intent_threshold = 0.5
explicit_threshold = 0.20
base_generation_enabled = false
comfyui_enabled = false
{chr(10).join(sticker_lines)}
""",
        encoding="utf-8",
    )
    config = load_config("low", toml)
    config.ensure_dirs()
    db = Database(config.db_path)
    db.initialize()
    return StickerService(db, config), db


def _hit(candidate, needles: list[str], texts: dict[str, str]) -> bool:
    """命中判定与检索同源：意图标签 + 场景描述（索引原文）+ caption。"""
    hay = " ".join(
        [
            texts.get(candidate.sticker.id, ""),
            str(candidate.sticker.caption or ""),
            " ".join(candidate.sticker.intents),
        ]
    ).lower()
    return any(n.lower() in hay for n in needles)


def run_eval(
    service: StickerService,
    queries: list[dict],
    texts: dict[str, str],
) -> int:
    top1 = 0
    top3 = 0
    raw_sims: list[float] = []
    for case in queries:
        q = case["q"]
        needles = case["any"]
        cands = service.search(q, limit=3)
        for cand in cands:
            if cand.similarity > 0:
                raw_sims.append(cand.similarity)
        hit1 = bool(cands) and _hit(cands[0], needles, texts)
        hit3 = any(_hit(c, needles, texts) for c in cands)
        top1 += hit1
        top3 += hit3
        flag = "✅" if hit3 else "❌"
        note = ""
        if cands:
            best = cands[0]
            note = (
                f" -> {best.sticker.id[:8]} score={best.score:.3f} "
                f"sim={best.similarity:.3f} kw={best.keyword:.2f}"
            )
        print(f"  {flag} {q}{note}")
        if not hit3:
            for rank, cand in enumerate(cands, 1):
                print(f"      {rank}. {cand.sticker.id[:8]} {texts.get(cand.sticker.id, '')[:36]}")
    total = len(queries)
    print(f"\nhit@1 {top1}/{total}   hit@3 {top3}/{total}")
    if raw_sims:
        raw_sims.sort()
        n = len(raw_sims)
        print(
            f"非零余弦 p50={raw_sims[n // 2]:.3f} p90={raw_sims[int((n - 1) * 0.9)]:.3f} "
            f"max={raw_sims[-1]:.3f}（similarity_floor/ceil 锚点从这里挑）"
        )
    return total - top3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--demo", action="store_true", help="合成库，确定性验收")
    parser.add_argument("--db", help="真库路径（自动拷临时副本，原库只读）")
    parser.add_argument("--backend", choices=("hash", "http"), default="hash")
    parser.add_argument("--url", default="http://127.0.0.1:9881/embed")
    parser.add_argument("--strict", action="store_true", help="hit@3 不满 100%% 时退出码 1")
    args = parser.parse_args()

    load_config()  # 触达 .env；配置项用下面的临时 toml
    if args.demo:
        with tempfile.TemporaryDirectory(prefix="sticker-demo-") as td:
            work = Path(td)
            service, db = _make_service(work, work / "demo.db", "hash", "")
            try:
                for spec in DEMO_LIBRARY:
                    service.add_sticker(
                        str(work / "img.png"),
                        caption=spec["caption"],
                        intents=spec["intents"],
                        content={"usage_description": spec["usage"]},
                    )
                missed = run_eval(service, DEMO_QUERIES, _index_texts(db))
            finally:
                db.close()
        return 1 if missed else 0

    if not args.db:
        parser.error("需要 --demo 或 --db")
    source = Path(args.db)
    if not source.exists():
        print(f"库不存在：{source}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="sticker-eval-") as td:
        copy = Path(td) / "eval.db"
        # WAL 库不能只拷主文件：未 checkpoint 的页全在 -wal 里，单拷主文件
        # 会拿到旧快照（云端实测整库贴纸丢失、评测 0/20）。用 backup API
        # 才是完整的逻辑拷贝。
        src_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        dst_conn = sqlite3.connect(str(copy))
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
        service, db = _make_service(Path(td), copy, args.backend, args.url)
        try:
            if _reembed(service, args.backend) != 0:
                return 1
            missed = run_eval(service, GOLDEN, _index_texts(db))
        finally:
            db.close()
    if args.strict and missed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
