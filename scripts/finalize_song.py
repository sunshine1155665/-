"""Freeze one rendered take as the deliverable (``data/soulx/song_final``).

A take is only final when the exact settings that produced it travel with it:
the render is stochastic (two runs of the same config correlate at 0.05), so
"the good one" cannot be regenerated from the melody alone. This copies the
audio, keeps the run's report, and writes the command line + the numbers that
made it the pick, plus a hash so the file can be identified later.

    python scripts/finalize_song.py --from data/soulx/melody_v12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data/soulx/song_final"
# Everything a re-render or a mix needs; anything else is intermediate.
KEEP = ("full.wav", "vocal.wav", "report.json")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def render_command(rep: dict, out_dir: str = "data/soulx/melody_v12") -> str:
    """The exact command that produced this take, rebuilt from its report."""
    src = "data/soulx/persona_song/report.json"
    parts = [
        "python soulx/song_v2.py",
        f"--out {out_dir}",
        f"--semitones {rep.get('semitones', 0)}",
    ]
    if rep.get("native_key"):
        parts.append("--native-key")
    parts += [
        f"--emotion {rep.get('emotion', 'soft')}",
        f"--rate-scale {rep.get('rate_scale') or 1.0}",
        f"--n-steps {rep.get('n_steps') or 32}",
    ]
    # Performance knobs are spelled out even though the preset covers them: the
    # point of this file is that the numbers travel with the audio.
    for flag, key in (("--reverb", "reverb"), ("--vibrato", "vibrato"),
                      ("--vibrato-hz", "vibrato_hz"),
                      ("--drift-cents", "drift_cents"), ("--scoop", "scoop"),
                      ("--breath", "breath"), ("--air", "air"),
                      ("--legato", "legato"), ("--backing-db", "backing_db")):
        if rep.get(key) is not None:
            parts.append(f"{flag} {rep[key]}")
    parts += [f"--src {src}",
              "--prompt-wav data/whale_voice_demo_v2/refs/cand_home_up1.wav",
              "--prompt-meta data/soulx/prompt_v6.json"]
    return " ".join(parts)


def freeze(src: Path, out: Path, note: str = "") -> dict:
    if not (src / "report.json").is_file():
        raise SystemExit(f"not a render: {src}")
    rep = json.loads((src / "report.json").read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in KEEP:
        p = src / name
        if p.is_file():
            shutil.copyfile(p, out / name)
            files[name] = {"sha256_16": sha256(out / name),
                           "bytes": (out / name).stat().st_size}
    meta = {
        "version": src.name,
        "frozen_at": meta_now(),
        "note": note,
        "files": files,
        "settings": {k: rep.get(k) for k in
                     ("title", "tonic", "semitones", "native_key", "shift_mode",
                      "n_steps", "cfg", "reverb", "vibrato", "vibrato_hz",
                      "drift_cents", "gap_beats", "duration_s", "emotion",
                      "rate_scale", "legato", "scoop", "breath", "air",
                      "backing_db")},
        "command": render_command(rep, f"data/soulx/{src.name}"),
        "phrases": [{"text": p["text"], "jianpu": p["jianpu"],
                     "degrees": p["degrees"], "beat": p["beat"]}
                    for p in rep.get("phrases", [])],
    }
    (out / "SETTINGS.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "README.md").write_text(_readme(meta), encoding="utf-8")
    return meta


def meta_now() -> str:
    import datetime
    return datetime.date.today().isoformat()


def _readme(meta: dict) -> str:
    s = meta["settings"]
    lines = [
        f"# 《{s.get('title', '')}》终版（{meta['version']}）",
        "",
        f"冻结时间：{meta['frozen_at']}　时长：{s.get('duration_s')} s　"
        f"主音 MIDI：{s.get('tonic')}",
        "",
        f"> {meta['note']}" if meta.get("note") else "",
        "",
        "## 文件",
        "",
        "| 文件 | 说明 | sha256(16) |",
        "|---|---|---|",
    ]
    for name, info in meta["files"].items():
        lines.append(f"| `{name}` | "
                     + {"full.wav": "带伴奏", "vocal.wav": "纯人声",
                        "report.json": "旋律/时值/字调报表"}.get(name, name)
                     + f" | `{info['sha256_16']}` |")
    lines += [
        "",
        "## 怎么复现这一版",
        "",
        "渲染是随机的（同一配置两次波形相关只有 0.05），所以**这一条 take 本身不可复现**，"
        "下面这条命令只能复现「配置」：",
        "",
        "```bash",
        meta["command"],
        "```",
        "",
        "## 关键设置",
        "",
        "| 项 | 值 |",
        "|---|---|",
    ]
    for k, v in s.items():
        if v is None:
            continue
        lines.append(f"| `{k}` | `{v}` |")
    lines += [
        "",
        "## 旋律",
        "",
        "| # | 歌词 | 简谱 | 相对主音 |",
        "|---|---|---|---|",
    ]
    for i, p in enumerate(meta["phrases"], 1):
        lines.append(f"| {i} | {p['text']} | `{p['jianpu']}` | "
                     f"`{' '.join(str(d) for d in p['degrees'])}` |")
    lines.append("")
    return "\n".join([x for x in lines if x is not None])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把某一版冻结成终版")
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--note", default="", help="为什么选这一条")
    args = ap.parse_args(argv)
    meta = freeze(Path(args.src), Path(args.out), args.note)
    print(f"froze {meta['version']} -> {args.out}")
    for name, info in meta["files"].items():
        print(f"  {name:12} {info['bytes']:>9,} B  {info['sha256_16']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
