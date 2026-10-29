---
title: ADR-0006 取消评论/点赞，改私聊分享 share_to_friend
type: adr
status: accepted
tags: [adr, qq, share, autonomy, outbox, scope, privacy, approval]
updated: 2026-10-25
---

# ADR-0006：取消评论/点赞，改私聊分享 share_to_friend

> **boss 授权记录（2026-10-25）**：T-0012 的方向由 boss 两次收口——v2 曾拍板「评论欲阈值」（desire ≥0.8 自动评论好友动态），实现完成且全量测试通过后，boss 复盘「评论欲阈值」时改主意：**不允许在动态下评论，点赞也取消**，改为「私聊分享」：她能主动私聊的对象只有主人（已有 `share_with_owner`）和好友位绑定的姐姐（新 `share_to_friend`），别无他人。本 ADR 记录最终态；v2 实现已按指令全部丢弃（未提交，不入库）。

## 背景

T-0012 原始规格是审批制评论/点赞（v1），后升级为 desire 模型（v2，与 ADR-0005 同构）：范围硬限「我」与「姐姐」、阈值 0.8、防刷屏三闸。实现已完成（schema v10 + `qzone_interactions` + 客户端候选端点 + Web/CLI 审批），1090 条测试全绿。但评论/点赞是**挂在别人空间里的公开互动**：社交成本高、风控面未知、说错话不可撤回，且「评论欲」本质是把她的社交表达外包给一个阈值。boss 最终选择更保守也更有分寸的形态：想说的话，**私聊发给你想说的那个人**——目标天然被 `owner_bindings` 收口，语气与场合由她自己掌握，随时可以不发。

## 决策

### 1. 评论/点赞整体取消

- 不做 `comment_qzone` / `like_qzone`；不新增任何空间动态写端点。QQ 空间对外写能力维持 ADR-0004/0005 的「发说说」一条。
- v2 的全部产物（`qzone_interactions` 表、schema v10、interactions 服务、候选端点、审批面、配置键）已丢弃；schema 停在 **v9**，无迁移负担。

### 2. share_to_friend：主动私聊分享给姐姐

- 新 ActionSpec：`min_trust=PRIVATE`、`max_calls=2`、`allow_group=False`、`write=True`、必填 `text`；可选 `args.desire`（0-1，仅记录进审计，不驱动任何行为）。
- **目标解析在服务端**：查 `owner_bindings` `role='friend'` 的绑定行，从 `session_id`（形如 `qq:private:<uin>`）解析好友 uin。无绑定、或绑定会话无法解析出 QQ 号（如 web 会话）→ 返回 `{ok: false, error: 'no_friend_bound'}`，**绝不猜测目标**；LLM 传入的任何收件人参数一律忽略。
- 投递：`outbox.enqueue(kind='friend_share', payload={'text': 原话, 'friend_uin': uin}, idempotency_key='friend_share:'+hash(text+uin+当日))`——同一天同一段话对同一个人只排一次队；QQ 适配器 `send_outbox_item` 新增 `friend_share` 分支，用既有 `send_private_text` 原话私聊投递，payload 缺 `friend_uin` 是 handler 错误而非猜测理由。
- 闸门：安静时段 23:30–08:00（`share.quiet_hours_start/end`，深夜顺延到 08:00）；每日上限 `share.max_to_friend_per_day` 默认 **2**（0=不限），计数走 `decision_log`（kind `friend_share`、action `share`、outcome `queued` 的当日行数）——账本即计数器，重启不丢。
- 内容筛查与 `post_qzone` 草稿同款：命中凭证形态或 `secret_notes` 原文 → 拒发（`sensitive_content`），审计行只记长度等元数据，**敏感文本本身不进 decision_log**。
- 审计：每次尝试都落 `log_decision(kind='friend_share')`（queued / duplicate / no_friend_bound / quota_exceeded / sensitive_content），`at` 用当轮时钟（`log_decision` 新增可选 `at` 参数，生产路径不变仍为墙钟）。

### 3. 主动私聊的总边界（提示词 + 服务端双层）

TOOL_SCHEMA 明示：**她只能主动私聊两个人**——主人哥哥（`share_with_owner`，走 wakeup 通道）与姐姐（`share_to_friend`，走 outbox 直投），其他任何人绝不主动私聊。服务端没有「第三个收件人」的路径：`share_to_friend` 的收件人只来自绑定行，`share_with_owner` 只回到原会话，群聊被 trust 矩阵挡住。

### 4. 配置

三套 profile（low/standard/remote）新增 `[share]`：`max_to_friend_per_day=2`、`quiet_hours_start="23:30"`、`quiet_hours_end="08:00"`。绑谁由 `owner_bindings` 决定（CLI/认主流程），配置只管频率，不管目标——目标没有任何配置绕过。

## 备选

- **保留 v2 评论/点赞（已实现、已测试）**：沉没成本不是理由。公开互动面（风控、不可撤回、说错话的代价）与「有分寸的伴侣」人设冲突；私聊分享覆盖了同一需求（对在乎的人表达）而风险面小一个量级。放弃，v2 实现丢弃。
- **share_to_friend 走 wakeup 通道（与 share_with_owner 完全同构）**：主人通道走 wakeup 是因为要回到**原会话**继续对话；对姐姐是单向主动开场，直接 outbox 原话投递更简单，安静时段/每日上限由本 ADR 的闸门承担。放弃 wakeup。
- **允许配置多个可私聊好友**：boss 明示只有两个人。`owner_bindings` 好友位只有一个槽位，天然满足；未来真要放宽必须改本 ADR。

## 后果

- QQ 空间动态下将永远沉默（不评不赞），表达欲全部经由聊天/说说/私聊三条已审计通道。
- `decision_log` 现在承担 friend_share 的每日计数职责；`log_decision` 的可选 `at` 参数是唯一共享代码改动，其余全部是增量。
- 测试基线：`tests/test_share_to_friend.py`（动作矩阵、无绑定拒绝、范围不可达、内容闸、配额、安静时段、原话投递、主人通道不受影响）。
- 已知风险：投递用的是**入队时**绑定的 uin——若主人在这期间换绑，消息仍发给原姐姐（入队即同意的语义，已在 QQ 适配器分支注释说明）；同文幂等按「自然日 + 同人同文」算，跨天重复会再发。
