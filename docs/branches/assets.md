---
title: 资产地图
type: branch
status: active
tags: [assets, models, data, archive, disk, cleanup]
updated: 2026-10-24
---

# 资产地图

> 上级：[系统架构](../overview/architecture.md) · 相关：[运维](ops.md) · [语音](voice.md) · [唱歌](singing.md)

## 磁盘总览（实测）

| 路径 | 大小 | 说明 |
|---|---|---|
| `models/` | 10,857MB | IndexTTS-2.5 10,466 + bge 391 |
| `vendor/` | ~2.8GB | napcat 2.8G + index-tts-main 41M（旧 SoulX-Singer/singdeps 已归档到 E 盘） |
| `.venv-cosyvoice/` | 5,100MB | torch 供体（`.venv-soulx` 依赖） |
| `.venv-indextts/` | 1,034MB | 产线 TTS 环境 |
| `.venv-soulx/` | 117MB | 唱歌环境 |
| `data/` | 195MB | soulx 69.6 + stickers 49.5 + voice demo 28.6 + audio 12.6 + photos 3.7 |
| `dist/` | 5.3MB | 语音基准包 + 清理报告 |
| 外部归档盘（本地路径不入库） | ~55GB | 归档七类：清理产物 + 旧模型 + 旧仓库 + 音频等 |

## 绝不能删

| 资产 | 原因 |
|---|---|
| `models/IndexTTS-2.5` | 产线语音，重下 10GB |
| `data/whale_voice_demo_v2/refs/cand_home_up1.wav` | 音色身份，TTS 与唱歌共用 |
| `data/soulx/prompt_v6.json` | 唱歌参考元数据，重建要重新对齐 |
| `.venv-cosyvoice` | 被 `.venv-soulx` 借 torch，删了唱歌崩 |
| `.venv-indextts` | 产线 TTS 环境 |
| `YUE2_ROOT` / `RVC_ROOT`（外部） | 产线唱歌的 YuE2 / RVC 环境与权重；不随仓分发 |
| `data/fatfish.db` | 全部对话/记忆/认主 |
| `fatfish/kernel.json` | 身份内核 |

## 重要数据资产

| 路径 | 内容 |
|---|---|
| `data/soulx/prompt_v6.json` | 28 音节 / 5.54s / F0 55–73（产线参考元数据） |
| `data/soulx/persona_song/report.json` | 默认歌歌词/底稿（产线） |
| `data/soulx/melody_*`、`song_*`、`cand_*` | 旧 SoulX 时代 take，产线不读；归档候选 |
| `data/whale_voice_demo_v2/` | 93 文件 / 28.6MB，参考音与 demo |
| `data/audio_8d_scenes_v6/` | 8D 原型 20 文件（未接线） |
| `data/stickers/` | 旧库资产，运行时不读；归档候选 |
| `data/photos/` | 产线拍照产物 |
| `data/backups/` | 手工备份（含夜间归档前快照） |

## 可清理（需确认）

| 路径 | 建议 |
|---|---|
| `data/soulx/flywheel/`、`modes_p1/p2/`、`nursery/`、`prosody/`、`vocal_ab/` | 实验音频归档到 E，保留 report |
| `data/audio/` | TTS 缓存，可清 |
| `data/run/*.pid` | 无进程残留 |
| `dist/cleanup-*` | 一次性报告 |
| 根目录研发脚本 `staging_*.py` | 已在公开发布版移除，只保留运行时 `soulx/` |

## 归档

- 外部归档盘（本地路径不入库）：7 类，含 MANIFEST。
- 外部归档盘：13 个参考仓库。
- 外部归档盘 `08_docs_20261024/`：旧文档（本体系重组前）。
- 旧文档原文保留在主开发仓 git 历史中（未随发布版分发）。
