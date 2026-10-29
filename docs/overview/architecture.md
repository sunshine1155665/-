---
title: 系统架构
type: overview
status: active
tags: [architecture, runtime, dataflow, ports, modules]
updated: 2026-11-03
---

# 系统架构

> 全景导图：[project-map.md](project-map.md) · 分支：[运行时](../branches/runtime.md) · [语音](../branches/voice.md) · [记忆](../branches/memory.md) · [平台](../branches/platforms.md) · [安全](../branches/security.md) · [运维](../branches/ops.md)

## 1. 形态

本地优先的单用户应用：Python 标准库 + SQLite，本机 Web UI + CLI + QQ 适配器。应用本体不依赖第三方库；重活（TTS/向量/生图/唱歌）都在独立进程或本地 HTTP 服务里。

## 2. 分层

```
接入层   QQ/NapCat(3001)   Web(8765/随机)   CLI   B站只读   QQ空间   云端在线层(ADR-0012)
              ↓
编排层   Runtime：安全 → 认主 → 漫游判定 → 召回 → 提示词 → LLM → 旁路/动作/语音/拍照 → 发送
              ↓
能力层   记忆(含entity_edges)  语音  唱歌(sing→soulx)  表情包  拍照  词库  漫游(roam)  唤醒/续聊  成长/决策  生活
              ↓
基础层   SQLite(38表,v14)  outbox  trace  控制面(9882)  进程锁(vram.lock)  备份/归档
              ↓
模型层   TTS(9880)  向量(9881)  ComfyUI(8188)  SoulX(子进程)  KEV(可选)
```

## 3. 一次对话

| # | 步骤 | 代码 |
|---|---|---|
| 1 | 收消息 | `platforms/qq.py`、`web/server.py`、`cli.py` |
| 2 | 清洗 + 信任分级 | `security/trust.py` |
| 3 | 认主/好友位 | `owner.py` |
| 4 | 漫游入站判定（ADR-0014） | `roam/service.py:note_inbound` |
| 5 | 记忆召回 + 1 跳关联 | `memory/service.py`、`memory/edges.py`、`memory/gate.py` |
| 6 | 装配提示词 | `context.py:ContextAssembler.build` |
| 7 | 单次 LLM 调用 | `llm.py`、`runtime.py:parse_turn_plan` |
| 8 | 主人指令旁路 / 动作白名单 | `self_action/explicit.py`、`security/policy.py` |
| 9 | 拍照旁路（关键词） | `runtime.py` → `photo/service.py` |
| 10 | 语气判定 | `voice/tone.py`、`tts/indextts.py` |
| 11 | 分句 + TTS | `voice/policy.py`、`tts/segmentation.py` → 9880 |
| 12 | 可靠外发 | `outbox.py` |
| 13 | 发送 + 观测 | `platforms/qq.py`、`trace.py`、`tool_audit` |

## 4. 动作协议

LLM 只能通过白名单动作影响本地（`security/policy.py:ACTION_SPECS`，现 19 个）：`send_sticker`、`send_voice`、`schedule_wakeup`、`remember`、`open_loop`、`close_loop`、`post_qzone`、`share_with_owner`、`share_to_friend`、`sing`、`search_memory`、`read_qzone`、`read_chat`、`watch_video`、`roam_visit`、`roam_return`、`talk_to`、`make_photo`、`search_videos`。其中 `talk_to`/`read_chat`/`watch_video` 只认主人原话指令（`self_action/explicit.py` 确定性旁路 + 频控）。新能力必须同时改：动作表 → runtime 执行分支 → 发送通道 → Web API → 测试。

## 5. 数据

38 张表（另有 FTS 影子表），schema v14。核心链路：`conversation_turns` → `conversation_events` → `nightly` 生成 `episodes` / `memory_items` / `rag_chunks` → `RecallGate` 召回；人物侧 `entities`/`entity_edges`/`entity_mentions`。详见 [记忆分支](../branches/memory.md)。

## 6. 目录

| 路径 | 作用 |
|---|---|
| `fatfish/` | 应用代码（20 包 + 20 顶层模块；roam/sing/self_action/bilibili/qzone 等已入正仓） |
| `scripts/` | 服务进程、启动、运维、实验入口 |
| `web/` | 原生前端（聊天 + 人物卡 + 流式） |
| `tests/` | unittest 全量（离线），实测数见 AGENTS.md 基线 |
| `config/` | `low/standard/remote/cloud/tts25-live` profile（low 为 base） |
| `soulx/` | 唱歌链路研发区（运行时 15 件已接线 `fatfish/sing/`） |
| `docs/` | 本体系（全景导图见 project-map.md） |
| `data/`、`models/`、`vendor/` | 本地资产，已被 git 忽略 |

## 7. 改哪里

| 想改 | 先看 |
|---|---|
| 人格/提示词 | `context.py` + `persona` 表 |
| 语气/音色 | [voice](../branches/voice.md) |
| 记忆/召回 | [memory](../branches/memory.md) |
| QQ/B站/Web | [platforms](../branches/platforms.md) |
| 表情包/拍照/词库 | [media](../branches/media.md) |
| 安全/权限 | [security](../branches/security.md) |
| 启动/端口/显存 | [ops](../branches/ops.md) |
| 存放规范/发布树/全景图 | [project-map.md](project-map.md) |
