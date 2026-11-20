# Agent Board 接入说明（ZCode / OpenCode / WorkBuddy 共用一块看板）

三个工具同时开着，但互相看不见对方在做什么。这里放一块**文件型看板**，谁接了什么、干到哪一步、
卡在哪，都落在 `.agent-board/`。抢单靠文件原子锁，不需要服务、不需要联网。

本项目只服务一个仓库：`<PROJECT_ROOT>`。看板数据不入库（见 `.gitignore`）。

## 角色分工（2026-10-24 定）

| 工具 | 角色 | 能不能领单 | 备注 |
|---|---|---|---|
| **ZCode 主对话** | **调度员** | 不领单，只 `add` / 看板 / 验收 | 用 `/dispatch`，只拆单不动手 |
| **ZCode 子对话** | **执行者** | 能，`--agent zcode-<主题>` | 用 `/worker`，一对话一单 |
| OpenCode | 审计 / 协调 | 不领（软约束） | 只 `list` / `show` / `feed` / `comment` |
| WorkBuddy | 只读旁观 | **不能（已冻结）** | `claim` 会被系统拒绝 |

派单权只在 ZCode：额度免费，重活、长上下文、机械重构、补测试都往那边放。
模型统一 **start-plan / GLM-5.3-Flash**；不要用 max variant 跑本仓的活。

解冻 WorkBuddy（确实要它干活时）：`board.py unblock workbuddy --agent boss`。

## 目录

```
scripts/agent_board/
├── board.py            # 看板本体（CLI，纯标准库，Windows 可用）
├── board_mcp.py        # MCP stdio server，暴露 board_* 工具
├── board_overview.py   # 人类视角总览
├── gpu.py              # 显存/内存闸门（复用 data/run/vram.lock）
├── wire.py             # 一键接入 / 检查 / 撤销
└── SKILL.md            # 上游 skill 原文（wire.py 改写后分发）
```

上游：<https://github.com/zheyu-Julie-Liu/zcode-agent-board>（MIT）。更新时替换 `.py` + `SKILL.md`，再跑 `wire.py`。

## 接了什么

| 工具 | MCP 注册位置 | skill | 身份 |
|---|---|---|---|
| OpenCode | `<PROJECT_ROOT>\opencode.jsonc`（项目级） | `.opencode/skills/agent-board/` | `opencode` |
| WorkBuddy | `~/.workbuddy/mcp.json` → `mcpServers.agent-board` | `~/.workbuddy/skills/agent-board/` | `workbuddy` |
| ZCode | `~/.zcode/cli/config.json` → `mcp.servers.agent-board` | `~/.zcode/skills/agent-board/` | `zcode` |

ZCode 另有三个斜杠命令（`~/.zcode/commands/`）：

| 命令 | 在哪用 | 干什么 |
|---|---|---|
| `/dispatch` | 主对话 | 读 AGENTS.md 优先级 → 拆单进看板（只派不做） |
| `/worker` | 子对话 | 领一单 → 干活 → `done` 写结果 |
| `/board` | 任意 | 总览 + 流水，整理成人话 |

```bash
python scripts/agent_board/wire.py            # 写入/更新（幂等，改前自动 .bak）
python scripts/agent_board/wire.py --status   # 只检查
python scripts/agent_board/wire.py --unwire   # 从 WorkBuddy/ZCode 摘掉 MCP 注册
```

改完配置：OpenCode `/reload` 即生效；WorkBuddy / ZCode 需新开对话（或在命令面板重载）。

## 日常用法

```bash
python scripts/agent_board/board.py list                     # 看板（默认隐藏已完成）
python scripts/agent_board/board.py list --all --json        # 全量 JSON
python scripts/agent_board/board.py add "标题" -d "目标/涉及文件/验收/禁止" -p 1 --agent zcode
python scripts/agent_board/board.py claim --agent zcode-记忆   # 自动挑最高优先级
python scripts/agent_board/board.py claim T-0007 --agent zcode-记忆
python scripts/agent_board/board.py comment T-0007 "卡在 X" --agent zcode-记忆
python scripts/agent_board/board.py done T-0007 -r "改了什么 + 怎么验证 + 风险" --agent zcode-记忆
python scripts/agent_board/board.py feed -n 20               # 谁最近干了什么
python scripts/agent_board/board.py steal T-0007 --agent zcode-记忆   # 原认领者超时 30 分钟才可接管
```

MCP 已连上时优先用工具（`board_list` / `board_claim` / `board_done` …），身份参数与 CLI 保持一致。

## 显存 / 内存闸门

卡是 RTX 5060 Laptop，**8151 MiB**；TTS(9880) / ComfyUI(8188) / SoulX **不能同时驻留**。
产品侧已用 `data/run/vram.lock` 做租约（`fatfish/process_lock.py`），`gpu.py` 复用**同一把锁**，
所以 agent 的活和拍照/唱歌不会互相踩。

```bash
python scripts/agent_board/gpu.py status                  # 显存 + 内存 + 当前持有者
python scripts/agent_board/gpu.py run --agent zcode-唱歌 --wait 600 -- python -m soulx.song_yue2_rvc
python scripts/agent_board/gpu.py run --agent zcode-拍照 --unload-tts --min-free-mb 7000 -- <命令>
```

纪律：

1. **会加载模型的活必须走 `run`**（TTS 常驻、SoulX、ComfyUI、embedding、训练、评测）。
   `run` 拿不到锁就排队；进程退出锁自动释放，崩了也不会卡死。
2. **纯 CPU 不用抢**：单测、文档、SQLite、`docs_index`。但别同时开超过 2 个重 CPU 进程
   （686 条测试约 301s，跑两份就顶到内存）。
3. **先卸再跑**：需要整卡时加 `--unload-tts`；跑完默认不自动 `/load`，让服务按需加载。
4. **内存**：三个 Electron 客户端（ZCode/WorkBuddy/OpenCode）+ 多份 venv 已占不少；
   32 GiB 机器上建议**同时最多 2 个子对话在跑**，其余排队。
5. 显存泄漏排查：`gpu.py status` 看 `data/run/vram.holder.json` 的记录（谁、什么活、从什么时候）。

## 本项目约定

1. **一单一人**：`claim` 抢到锁才动手；不同时改同一批文件。跨工具的活拆成任务，别并行改同一目录。
2. **文件隔离**：派单写明涉及目录；改 `fatfish/` 时各自开 worktree
   （`git worktree add ../fatfish-task-<name> -b task/<name>`）。
3. **不碰**：`models/`、`vendor/`、`data/`（真实库与音频）、`.venv-*`、`<COMFYUI_ROOT>`。
   派这类任务要先问人。
4. **验收写进 `done -r`**：跑过哪条测试、结果如何，一句话说清，别的 agent 才敢接着做。
5. **`boss` 身份归用户**：agent 不得冒用；用户口述的任务由当前 agent 代为落板。

## 排错

- **看板找不到**：`board.py whoami` 看 `看板目录` 是否是 `<PROJECT_ROOT>\.agent-board`；不是就传 `--root "<PROJECT_ROOT>"`。
- **MCP 连不上**：先手动握手：
  ```powershell
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' |
    & "D:\develop\python\python.exe" scripts\agent_board\board_mcp.py
  ```
  没输出就检查 python 绝对路径（配置里用的是 `D:\develop\python\python.exe`）。
- **claim 被拒**：`board.py whoami` 看是否在冻结名单；`unblock <身份> --agent boss` 解冻。
- **抢不到显存**：`gpu.py status` 看持有者；持有进程已死但 holder 文件残留时，租约本身会随句柄释放，直接重试即可。
- **锁卡住**：`.agent-board/locks/T-XXXX.lock` 的修改时间超过 1800 秒即可被 `steal`；不要手删任务 JSON。
