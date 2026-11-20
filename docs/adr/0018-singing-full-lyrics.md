---
title: ADR-0018 点歌歌词升级为完整歌词（whale-lyrics 规则进 prompt）
type: adr
status: accepted
tags: [adr, singing, lyrics, prompt, yue2]
updated: 2026-11-15
---

# ADR-0018：点歌歌词升级为完整歌词（whale-lyrics 规则进 prompt）

## 背景

ADR-0017 把唱歌统一到 YuE2→RVC 后，`sing` 动作的歌词仍是「可选四句、每句 5–12 字」，
由聊天模型即兴写；`skills/whale-lyrics` 的写词规则（意图/锚点/四拍/长度曲线/韵脚/
语气词预算）只用在离线扩曲库，没进产品。boss 决定：唱歌意向出现时**保留原对话上下文**
加载 skill，产出**完整歌词**；多套歌词候选在**歌词阶段**定稿，**只生成一次歌曲**。

## 决策

1. **歌词契约升级（`sing.lyrics`）**：完整歌词——4–16 句正文、每句 4–16 字，
   可用 `[Verse]`/`[Chorus]`/`[Bridge]`/`[Intro]`/`[Outro]`/`[Hook]`/`[Refrain]`/
   `[Pre-Chorus]` 分段。段标签保留并原样传给 renderer，但不计入正文句数。
2. **校验**（`fatfish/sing/service.py`）：`clean_lyrics` 按上述规则校验；
   错误码 `lyrics_need_four_lines` → `lyrics_bad_count`（`lyrics_bad_length` 不变）。
   不合格照旧回退默认歌《等你回家》，原因记 `sing_jobs.lyrics_error`。
3. **renderer**（`soulx/song_yue2_rvc.py`）：优先读 `lyrics.json` 的
   `lyrics`（带段标签）并**原样**拼进 YuE2 request；无标签的词（含四句默认歌）
   自动包一层 `[Verse]`。上界 16 句——16 行 2:07 的长版生日实测可渲染，更多会有
   截断风险。
4. **写词规则进 prompt**（`fatfish/prompt_blocks.py`）：`sing` 块教模型按
   whale-lyrics 的核心规则写完整歌词——一句话意图、可看见的锚点、句句接得住、
   句长有变化、语气词 ≤2、开口音韵脚、末段回到锚点/对方。**不起新进程、不另建
   agent**：写词发生在带完整对话上下文的同一次生成里。
5. **仍然**：lyrics 可选；不写唱默认歌；候选筛选由模型在写词时完成，运行时
   每次 `sing` 只渲染一首。

## 备选与放弃理由

| 方案 | 结论 |
|---|---|
| 单独起一个歌词 agent 进程 | 不采用：丢上下文、多一个活动部件；boss 明确要保留原上下文 |
| 维持固定四句 | 不采用：boss 要完整歌词 |
| 运行时跑完整 lyrics_lint 门 | 暂缓：`clean_lyrics` 已挡结构问题；lint 留作离线打磨工具 |

## 后果

**正面**

- 点歌从「四句小样」变成「完整一首歌」，与 6 情境/长版生日的成品形态一致。
- 写词规则随 prompt 生效于本地与云端（同一份 prompt），无新进程/新依赖。
- 16 句上界有实测支撑（长版生日 16 行 2:07 渲染成功）。

**负面 / 风险**

- 歌更长（约 1–2 分钟音频），YuE2 耗时波动大（实测 161–575s）；超时已按
  ADR-0017 放宽（本地 720s / 服务端与云端 900s）。
- 完整歌词的语义连贯依赖模型自觉；结构之外的音乐性问题暂无自动门，靠试听回收。

## 关联

- [唱歌分支](../branches/singing.md)（「完整歌词」节）
- [ADR-0016](0016-yue2-rvc-singing-chain.md)、[ADR-0017](0017-singing-all-yue2-rvc.md)
- `skills/whale-lyrics/`（离线写词规则与 lint；本条取其规则进 prompt）
