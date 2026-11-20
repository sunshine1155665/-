---
title: ADR 索引
type: adr
status: active
tags: [adr, decisions]
updated: 2026-11-15
---

# 决策记录（ADR）

只增不改。每条记录：背景 → 决策 → 备选 → 后果。

| 编号 | 决策 | 状态 |
|---|---|---|
| [0001](0001-indextts25.md) | 产线 TTS 用 IndexTTS-2.5 | 已采纳 |
| [0002](0002-singing-soulx.md) | 唱歌用 SoulX-Singer + 规则旋律 | 已被 0017 取代 |
| [0003](0003-memory-hierarchy.md) | 记忆改为事件-细节分层与多级标签 | 提议中 |
| [0004](0004-qzone-feed.md) | QQ空间读隐私分级 + 审批制写路径 | 已采纳 |
| [0005](0005-share-desire.md) | 分享欲模型：desire 阈值自动发布 + share_with_owner | 已采纳 |
| [0006](0006-friend-share.md) | 取消评论/点赞，改私聊分享 share_to_friend | 已采纳 |
| [0007](0007-agent-loop.md) | 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct） | 已采纳 |
| [0008](0008-catchup.md) | 上线扫未回复：掉线期间的漏答消息补回机制 | 已采纳 |
| [0009](0009-continue-policy.md) | 继续对话策略引擎：三信号 + 撒娇阶梯 | 已采纳 |
| [0010](0010-sing-action.md) | 唱歌接入：`sing` 动作、`sing_jobs` 与显存串行 | 已采纳 |
| [0011](0011-qzone-browser-renew.md) | QQ 空间免扫码续期：借本机浏览器的登录态 | 已采纳 |
| [0012](0012-online-tier.md) | 在线层与能力层分离（云主机 24h 在线） | 提议中 |
| [0013](0013-session-visit.md) | 外访会话：主人授权下的跨会话主动聊天 + 目的区 + 归位总结 | 已被 0014 取代（保留为单房间子集） |
| [0014](0014-session-roaming.md) | 多会话漫游：线程卡部分记忆 + 注意力调度 + 常驻授权 | 已采纳（P0 已实现，P1/P2 待做） |
| [0015](0015-singing-eval-determinism.md) | 唱歌评测基座：确定性采样与双层自动评审 | 历史（随旧 SoulX 删除） |
| [0016](0016-yue2-rvc-singing-chain.md) | 唱歌通用链：YuE2 全曲生成 + RVC 鲸鱼娘换声 | 已采纳 |
| [0017](0017-singing-all-yue2-rvc.md) | 唱歌统一走 YuE2→RVC，SoulX 链路删除 | 已采纳 |
| [0018](0018-singing-full-lyrics.md) | 点歌歌词升级为完整歌词（whale-lyrics 规则进 prompt） | 已采纳 |
| [0018](0018-platform-prompt-variants.md) | 平台提示词变体：按入口能力装配稳定前缀 | 已采纳 |
