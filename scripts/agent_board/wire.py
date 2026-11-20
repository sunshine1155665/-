#!/usr/bin/env python3
"""把 Agent Board（任务看板）接入本机三个工具：ZCode / OpenCode / WorkBuddy。

只服务一个项目：D:\\fatfish-lab。看板数据在 <项目根>\\.agent-board\\。

用法：
    python scripts/agent_board/wire.py            # 写入/更新三处配置（幂等，自动备份）
    python scripts/agent_board/wire.py --status    # 只检查当前接了什么
    python scripts/agent_board/wire.py --unwire    # 从 WorkBuddy / ZCode 配置里移除（备份保留）

设计说明：
- 脚本本体在 scripts/agent_board/，三处只做“注册 + skill 副本”，不复制 board.py。
- MCP 一律走 stdio，命令用绝对路径的 python.exe，避免 GUI 进程 PATH 不同。
- 身份分别设为 opencode / workbuddy / zcode，看板 feed 可直接看出谁在干活。
- 配置里同时写 env；skill 里仍要求显式传 --root，env 失效也不会指错板。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

PROJECT = Path(r"<PROJECT_ROOT>")
SCRIPTS = PROJECT / "scripts" / "agent_board"
BOARD = SCRIPTS / "board.py"
BOARD_MCP = SCRIPTS / "board_mcp.py"
OVERVIEW = SCRIPTS / "board_overview.py"
SKILL_SRC = SCRIPTS / "SKILL.md"

PYTHON = Path(sys.executable)
HOME = Path(os.path.expanduser("~"))

WORKBUDDY_CFG = HOME / ".workbuddy" / "mcp.json"
ZCODE_CFG = HOME / ".zcode" / "cli" / "config.json"
OPENCODE_CFG = PROJECT / "opencode.jsonc"

SKILL_TARGETS = {
    "opencode": PROJECT / ".opencode" / "skills" / "agent-board" / "SKILL.md",
    "workbuddy": HOME / ".workbuddy" / "skills" / "agent-board" / "SKILL.md",
    "zcode": HOME / ".zcode" / "skills" / "agent-board" / "SKILL.md",
}

SERVER_NAME = "agent-board"
STAMP = time.strftime("%Y%m%d-%H%M%S")

LOCAL_NOTES = {
    "zcode": """
## 本机共享看板（已接入）· 你的角色：调度员 + 执行者

- 看板根：`D:\\fatfish-lab`（唯一项目，所有命令固定 `--root "D:\\fatfish-lab"`）
- **主对话只派单，不动手**：用 `/dispatch` 把任务写进看板；写代码在**新开的子对话**里跑 `/worker`
- 身份：派单用 `--agent zcode`；子对话认领用 `--agent zcode-<主题>`（同一对话内保持一致）
- 模型：本仓重活统一走 **start-plan / GLM-5.3-Flash**（额度免费；不要切到 max variant）
- 另外两个工具只读：OpenCode=审计协调、WorkBuddy=已冻结（claim 会被拒）
- GPU 活必须先拿租约：`python scripts/agent_board/gpu.py run --agent <身份> -- <命令>`
- 抢单靠 `.agent-board/locks/` 原子创建；`feed` 看谁在做什么
""",
    "opencode": """
## 本机共享看板（已接入）· 你的角色：只读审计 / 协调

- 看板根：`D:\\fatfish-lab`（所有命令固定 `--root "D:\\fatfish-lab"`）
- **不要 claim 任务**：派单权在 ZCode 主对话，执行在 ZCode 子对话（额度免费）
- 你可以：`list` / `show` / `feed` / `comment`（提要求、报风险），不要 `done`/`steal`
- 需要新任务：`comment` 给 `boss` 或写进汇报，由用户决定是否交给 ZCode
- 需要跑 GPU 活时同样要先 `python scripts/agent_board/gpu.py run --agent opencode -- <命令>`
""",
    "workbuddy": """
## 本机共享看板（已接入）· 你的角色：只读旁观（已冻结）

- 看板根：`D:\\fatfish-lab`（所有命令固定 `--root "D:\\fatfish-lab"`）
- **claim 会被拒绝**（用户指示：任务只派给 ZCode）；`list` / `show` / `feed` 仍可读
- 不要建任务、不要抢单；需要变更请 `comment` 留言，由 ZCode 主对话派单
- 需要跑 GPU 活时同样要先 `python scripts/agent_board/gpu.py run --agent workbuddy -- <命令>`
""",
}

ZCODE_COMMANDS = HOME / ".zcode" / "commands"

ZCODE_CMD_FILES = {
    "board.md": """---
description: 看板总览：待办 / 在飞 / 谁在做什么
---

按顺序运行，然后把输出整理成人话（谁在做什么、卡在哪、需要人拍板什么）：

```bash
python "D:\\fatfish-lab\\scripts\\agent_board\\board_overview.py" --root "D:\\fatfish-lab" --todo
python "D:\\fatfish-lab\\scripts\\agent_board\\board.py" --root "D:\\fatfish-lab" list --all
python "D:\\fatfish-lab\\scripts\\agent_board\\board.py" --root "D:\\fatfish-lab" feed -n 15
```

$ARGUMENTS
""",
    "dispatch.md": """---
description: 只派单不动手：把当前优先级拆成看板任务
---

你是本项目的**调度员**，不是执行者。这条命令下**禁止**编辑 `fatfish/`、`tests/`、`docs/` 里的任何文件。

1. 读 `AGENTS.md` 的「当前优先级」与 `docs/overview/roadmap.md`；需要细节再读对应 `docs/branches/*.md`。
2. 看看板现状，已有同主题待办不要重复建（先 `show`，再决定补留言还是新建）：
   `python "D:\\fatfish-lab\\scripts\\agent_board\\board.py" --root "D:\\fatfish-lab" list`
3. 每单拆到 **≤30 分钟、单文件或单目录可验收**，描述必须含四段，缺一段就不许建：
   - 目标（一句话，可验收）
   - 涉及文件（具体路径）
   - 验收命令（可直接复制执行）
   - 禁止事项（不能碰的目录、不能改的接口）
4. 只派给 ZCode：`--agent zcode`。不要给 `workbuddy`（已冻结）或 `opencode`（只读）派单。
5. 优先级：P0 仅线上故障；P1 = 当前三项优先级；P2 = 可延后。
6. 建完按 P1→P2 列出「任务号 + 标题」，并说明**建议开几个子对话、每个领哪一单**（子对话里跑 `/worker`）。
7. 只回答"派了什么、为什么这么拆"，**不要顺手把活干了**。用户明确说"你自己做"时才切换成执行者。

$ARGUMENTS
""",
    "worker.md": """---
description: 领一单并干完（子对话专用）
---

你是执行者，只做**一单**。模型用 **start-plan 的 GLM-5.3-Flash**，不要切 max variant（本仓的活不需要）。

1. 认领（不带 ID 自动挑最高优先级；已被抢会明确报错）：
   `python "D:\\fatfish-lab\\scripts\\agent_board\\board.py" --root "D:\\fatfish-lab" claim --agent zcode-<主题>`
2. `show <任务号>` 读全说明和留言。**没有验收命令就先 `comment` 问，不要猜**。
3. 按 `AGENTS.md` 干活：从 `master` 拉 worktree/分支 → 先写测试 → 实现 →
   `.venv-indextts\\Scripts\\python.exe -m unittest discover -s tests -p "test_*.py"`（基线 686 条，必须全绿）。
4. 会加载模型的活必须拿显存租约：
   `python "D:\\fatfish-lab\\scripts\\agent_board\\gpu.py" run --agent zcode-<主题> -- <真实命令>`
   纯 CPU（单测/文档/SQLite）不用抢锁。
5. 完成：`board.py done <任务号> -r "改了什么 / 如何验证 / 已知风险"`。
   做不完就 `comment` 说明卡点 + `release` 放回待办池，别硬撑。
6. 回复给：diff 摘要 + 验收输出 + 风险，不要贴大段代码。

$ARGUMENTS
""",
}



def build_skill(tool: str) -> str:
    text = SKILL_SRC.read_text(encoding="utf-8")
    text = text.replace("python3 scripts/board_overview.py",
                        'python "D:\\fatfish-lab\\scripts\\agent_board\\board_overview.py" --root "D:\\fatfish-lab"')
    text = text.replace("python3 scripts/board.py",
                        'python "D:\\fatfish-lab\\scripts\\agent_board\\board.py" --root "D:\\fatfish-lab"')
    text = text.replace("python3 ", "python ")
    text = text.replace("> `scripts/board.py` 相对本 SKILL.md 所在目录；零第三方依赖，直接用 python3 运行。",
                        "> 脚本在 `D:\\fatfish-lab\\scripts\\agent_board\\`；零第三方依赖，Windows 上直接 `python` 运行。")
    text = text.replace("> `scripts/board.py` 相对本 SKILL.md 所在目录；零第三方依赖，直接用 python 运行。",
                        "> 脚本在 `D:\\fatfish-lab\\scripts\\agent_board\\`；零第三方依赖，Windows 上直接 `python` 运行。")
    text = re.sub(r"\n## 本机共享看板（已接入）.*\Z", "\n", text, flags=re.S)
    return text.rstrip() + "\n" + LOCAL_NOTES[tool]


BACKUP_DIR = PROJECT / ".agent-board" / "backups"


def _write_with_backup(dest: Path, text: str, tag: str) -> bool:
    """内容相同就不动；要改就把旧文件备份到 .agent-board/backups/（已 gitignore）。"""
    if dest.exists():
        try:
            if dest.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dest, BACKUP_DIR / f"{tag}-{STAMP}{dest.suffix}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return True


def install_skills() -> list[str]:
    out = []
    for tool, dest in SKILL_TARGETS.items():
        changed = _write_with_backup(dest, build_skill(tool), f"skill-{tool}")
        out.append(f"{dest}{'' if changed else '（未变）'}")
    return out


DEFAULT_BOARD_CONFIG = {
    "project_name": "fatfish-lab",
    "stale_seconds": 1800,
    "notify_mentions": ["boss"],
    "dispatch": {
        "owner": "zcode",
        "executor": "zcode-<主题>",
        "observers": ["opencode", "workbuddy"],
        "blocked_from_claim": ["workbuddy"],
        "model": "start-plan / GLM-5.3-Flash",
        "rule": "主对话只派单；执行在新开的子对话里；重活与长上下文都走 ZCode（额度免费）",
        "gpu": {
            "serialized": True,
            "lock": "data/run/vram.lock",
            "wrapper": "scripts/agent_board/gpu.py run --agent <身份> -- <命令>",
        },
    },
}


def ensure_board_config() -> str:
    """看板目录被 gitignore，新机器上要能重建派单策略。"""
    path = PROJECT / ".agent-board" / "config.json"
    if path.exists():
        return f"{path}（已存在）"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_BOARD_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def install_zcode_commands() -> list[str]:
    out = []
    ZCODE_COMMANDS.mkdir(parents=True, exist_ok=True)
    for name, body in ZCODE_CMD_FILES.items():
        dest = ZCODE_COMMANDS / name
        changed = _write_with_backup(dest, body, f"zcode-{name}")
        out.append(f"{dest}{'' if changed else '（未变）'}")
    return out


def _strip_jsonc(raw: str) -> str:
    """去掉 // 行注释与尾随逗号，让 .jsonc（OpenCode 配置）也能用 json 解析。

    只处理字符串外的注释，避免误伤 "https://..." 这类内容。
    """
    out = []
    i = 0
    in_str = False
    while i < len(raw):
        ch = raw[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < len(raw):
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < len(raw) and raw[i + 1] == "/":
            while i < len(raw) and raw[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    text = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def load_json(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig")
    return json.loads(_strip_jsonc(raw))


def save_json(path: Path, data: dict) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak-agent-board-{STAMP}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mcp_entry(tool: str, extra: dict | None = None) -> dict:
    entry = {
        "type": "stdio",
        "command": str(PYTHON),
        "args": [str(BOARD_MCP)],
        "env": {
            "AGENT_BOARD_ROOT": str(PROJECT),
            "AGENT_BOARD_AGENT": tool,
        },
    }
    if extra:
        entry.update(extra)
    return entry


def wire_workbuddy() -> str:
    data = load_json(WORKBUDDY_CFG) if WORKBUDDY_CFG.exists() else {}
    servers = data.setdefault("mcpServers", {})
    entry = mcp_entry("workbuddy")
    if servers.get(SERVER_NAME) == entry:
        return f"{WORKBUDDY_CFG} → mcpServers.{SERVER_NAME}（未变）"
    servers[SERVER_NAME] = entry
    save_json(WORKBUDDY_CFG, data)
    return f"{WORKBUDDY_CFG} → mcpServers.{SERVER_NAME}"


def wire_zcode() -> str:
    data = load_json(ZCODE_CFG) if ZCODE_CFG.exists() else {}
    servers = data.setdefault("mcp", {}).setdefault("servers", {})
    entry = mcp_entry("zcode", {"timeoutMs": 60000})
    if servers.get(SERVER_NAME) == entry:
        return f"{ZCODE_CFG} → mcp.servers.{SERVER_NAME}（未变）"
    servers[SERVER_NAME] = entry
    save_json(ZCODE_CFG, data)
    return f"{ZCODE_CFG} → mcp.servers.{SERVER_NAME}"


def unwire() -> list[str]:
    done = []
    for path, getter in (
        (WORKBUDDY_CFG, lambda d: d.get("mcpServers", {})),
        (ZCODE_CFG, lambda d: d.get("mcp", {}).get("servers", {})),
    ):
        if not path.exists():
            continue
        data = load_json(path)
        if SERVER_NAME in getter(data):
            del getter(data)[SERVER_NAME]
            save_json(path, data)
            done.append(str(path))
    return done


def status() -> int:
    ok = True
    print(f"python     : {PYTHON}  exists={PYTHON.exists()}")
    print(f"项目根     : {PROJECT}  exists={PROJECT.exists()}")
    print(f"board.py   : {BOARD}  exists={BOARD.exists()}")
    print(f"board_mcp  : {BOARD_MCP}  exists={BOARD_MCP.exists()}")
    print(f"看板数据   : {PROJECT / '.agent-board'}  exists={(PROJECT / '.agent-board').is_dir()}")

    for label, path, getter in (
        ("WorkBuddy", WORKBUDDY_CFG, lambda d: d.get("mcpServers", {})),
        ("ZCode", ZCODE_CFG, lambda d: d.get("mcp", {}).get("servers", {})),
        ("OpenCode", OPENCODE_CFG, lambda d: d.get("mcp", {}).get("servers", {})),
    ):
        try:
            data = load_json(path)
            entry = getter(data).get(SERVER_NAME)
            print(f"{label:<10} : {'已注册' if entry else '未注册'}  {path}")
            if entry:
                cmd = entry.get("command")
                args = entry.get("args")
                env = entry.get("env") or entry.get("environment")
                print(f"             cmd={cmd} args={args} env={env}")
            ok = ok and bool(entry)
        except FileNotFoundError:
            print(f"{label:<10} : 配置文件不存在  {path}")
            ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"{label:<10} : 读取失败 {exc!r}")
            ok = False

    for tool, dest in SKILL_TARGETS.items():
        print(f"skill[{tool:<9}]: {'已安装' if dest.exists() else '缺失'}  {dest}")
        ok = ok and dest.exists()

    for name in ZCODE_CMD_FILES:
        dest = ZCODE_COMMANDS / name
        print(f"zcode cmd    : {'已安装' if dest.exists() else '缺失'}  {dest}")
        ok = ok and dest.exists()

    policy = PROJECT / ".agent-board" / "config.json"
    print(f"派单策略     : {'已写入' if policy.exists() else '缺失'}  {policy}")
    ok = ok and policy.exists()

    print("\n结论：", "三处已接通" if ok else "有缺口，运行 `python scripts/agent_board/wire.py` 补齐")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="把 Agent Board 接入 ZCode / OpenCode / WorkBuddy")
    ap.add_argument("--status", action="store_true", help="只检查，不写入")
    ap.add_argument("--unwire", action="store_true", help="从 WorkBuddy / ZCode 移除注册")
    args = ap.parse_args()

    if args.status:
        return status()

    if args.unwire:
        removed = unwire()
        print("已移除：", ", ".join(removed) if removed else "（本来就没有）")
        print("skill 副本保留，可手动删除：")
        for dest in SKILL_TARGETS.values():
            print("  ", dest)
        return 0

    for f in (BOARD, BOARD_MCP, OVERVIEW, SKILL_SRC):
        if not f.exists():
            print(f"缺少文件：{f}")
            return 2

    (PROJECT / ".agent-board").mkdir(exist_ok=True)
    print("策略   →", ensure_board_config())
    for line in install_skills():
        print("skill  →", line)
    for line in install_zcode_commands():
        print("命令   →", line)
    print("MCP    →", wire_workbuddy())
    print("MCP    →", wire_zcode())
    print("OpenCode 用项目配置：", OPENCODE_CFG)
    print("\n完成。用 `--status` 复查；OpenCode 里 `/reload` 生效，WorkBuddy/ZCode 需新开对话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
