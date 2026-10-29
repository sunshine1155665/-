---
title: ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制
type: adr
status: accepted
tags: [adr, qq, catchup, reconnect, quiet-hours, outbox, audit]
updated: 2026-10-31
---

# ADR-0008：上线扫未回复——掉线期间的漏答消息补回机制

> **boss 授权记录（2026-10-25）**：T-0028 由 boss 直接下令：「她经常掉线（NapCat 重启/掉登录），每次上线时按顺序读取主人和姐姐的对话，找出没回复的消息并补上。」范围硬限主人和姐姐两个会话。
>
> **2026-10-31 追加（boss 口径「接口改为人物卡里的」）**：扫描范围从「主人 + 姐姐」扩到**人物卡名册**——`entities kind='person'` 且带 QQ uin 的**已知**活卡（老师、家人等；`relation` 非空或 `source in (cli,binding,manual)`，自动建卡的私聊陌生人默认跳过，`catchup_include_unknown_cards=true` 才纳入），owner 先、friend 次、其余按名册顺序，`qq.catchup_max_people` 默认 20 封顶。owner/friend 两个**槽位**仍以 `owner_bindings` 为同意点（人物卡是它的镜像，卡 uin 只作无绑定时的兜底）；`qzone.owner_uin` / `qzone_cookies.json` 是 QZone 的**发帖账号**（机器人号），不再作为补回的 owner 来源——2026-10-31 实测 cookie 优先会把 owner 目标解析成机器人自己，真正的主人会话整轮漏扫。

## 背景

她的 QQ 链路有三类掉线场景：NapCat 进程重启、掉登录后重扫码、以及 fatfish 侧 WebSocket 断开。重连后 NTQQ 会把大部分离线消息以普通事件补投进回合流（走正常回复路径），但**不保证全量**——尤其是 fatfish 侧断线而 NapCat 存活的窗口，消息对 fatfish 完全不可见。结果是「主人/姐姐发过话，她永远不知道」，直到对方追问。

现有的判定素材：本地 `conversation_turns`（QQ 会话 `qq:private:<uin>`，追加式回合流）+ NapCat 的非标准扩展 action `get_friend_msg_history`（已在 `vendor/napcat/NapCat.Shell/napcat.mjs` 验证存在：payload `{user_id, message_seq?, count=20, reverse_order?}`，响应 `{messages: []}`）。

## 决策

### 1. 触发：连接稳定后一次，水位防风暴

- `run_forever` 连接成功处**武装**一次 catch-up；连接稳定 `qq.catchup_delay_seconds`（默认 10s）后的 tick 触发。延迟是刻意的：NTQQ 补投的离线消息在登录后几秒内以普通事件到达并被正常回复，先让它们走完，扫描才不会双重回答。
- 每会话 meta 水位 `catchup_last_scan_at:<session>`（`schema_meta`）**先写后判**（与 `maybe_poll_qzone` 同型：崩溃不能把下次重连变成紧循环重扫）；`qq.catchup_min_interval_seconds`（默认 300s）节流 connect 风暴（NapCat 反复重连时每个会话最多 5 分钟一扫）。
- 同一次武装只触发一次（pending 标志），重连重新武装。

### 2. 判定：本地流序为主，历史拉取为增强

- **主判定**：会话的最后一条永久 `user` 回合按流 id 新于最后一条 `assistant` 回合 ⇒ 未回复。用流 id 而非时间戳：回合流追加有序，天然免疫事件补投与回复之间的时钟偏差。
- **增强判定**（`qq.catchup_fetch_history=true` 默认开）：`get_friend_msg_history` 拉 ≤`qq.catchup_max_messages`（默认 20）条，diff 出「晚于她最后回复、且不匹配近 40 条本地 user 回合（内容相同且时间差 ≤300s）」的行 = 真正漏掉的。±300s 去重窗口恰好吸收 NTQQ 补投的时钟滞后，防止「补投事件已回复 + 历史又拉到」双重回答；她的 catch-up 回复本身是 assistant 回合，会推进 cutoff，天然幂等。
- action 不存在/失败/超时 ⇒ 一律降级为纯本地流判定（记录 `history_degraded`），**绝不硬依赖**。无时间戳的行跳过（无法判定新旧）。

### 3. 补回：一个联系人一个回合，走正常回复路径

- 顺序=主人先、姐姐后；每联系人**一个** catch-up 回合（session 原样复用），错过的消息按时间顺序合成一份 transcript。
- 沿用 T-0027 self_continue 的双通道注入：固定提示词「你掉线错过了一些消息，按顺序看完再回复，不要跳过任何一条」走 `ephemeral_input=True`（系统指令，不作为她的「用户说过的话」进历史）；transcript 经 `wrap_untrusted(kind='catchup_messages', trust=EXTERNAL)` 走 `extra_evidence`——与 QQ 空间/B 站旁路同一信任面，漏掉的消息里哪怕带注入式措辞也只是一段被转义的素材，不构成指令。
- 她的回复是**对既有消息的响应**：走普通 `qq_send` outbox 原路送达，不碰 `proactive_message` 配额（90 分钟间隔/每日上限），也不挂 self_continue +60s（`suppress_self_continue=True`——补回是应答不是新话头）。语音/表情包/图片走既有媒体路径。
- LLM 不可用 ⇒ 沉默（self_continue 先例：不产出兜底话术，回合照常审计）。

### 4. 安静时段：顺延到 08:00，而不是静默丢弃

- `qq.catchup_respect_quiet_hours=true` 默认遵守（沿用 `memory.quiet_start/quiet_end`，单一来源）。深夜扫描发现未回复 ⇒ 不跑回合，入 outbox `qq_catchup` 项（`available_at = 下一安静结束点 08:00`，幂等键 `catchup-retry:<session>:<日>`，同日不重复）。
- 次日到点由在岗适配器的 handler **重扫**该会话（`force=True` 绕过 connect 节流，判定本身幂等）：夜里到早上之间已经回复过的自然判 `nothing_new`。适配器未连接 ⇒ handler 返回失败交回 outbox 重试；即使 outbox 行最终耗尽重试次数，下次成功连接的全量重扫仍会兜底。

### 5. 边界与审计

- **扫描范围（2026-10-31 起）**：主人 → 姐姐 → **人物卡名册**（`entities kind='person'` 且带 QQ uin 的**已知**活卡：`relation` 非空或 `source in (cli,binding,manual)`；`qq.catchup_from_cards` 默认开，`qq.catchup_max_people` 默认 20，`qq.catchup_include_unknown_cards` 默认 false）。主人/姐姐槽位以 `owner_bindings role='owner'/'friend'` 的 `qq:private:<uin>` 为准（单槽、同意点），**无任何绑定**时才回落到 `person:owner`/`person:friend` 卡的 uin；绑定存在但为非 QQ（web）会话时按“不可达”处理，绝不复活旧卡里的 QQ uin。`qzone.owner_uin` 与 `qzone_cookies.json` 是 QZone 发帖账号（机器人号），**不再参与**补回。同一会话只扫一次，owner/friend 优先。
- **登录态硬门**：catch-up 回合与重试 handler 都先验证 `client.connected`；NapCat 掉线时不产生无法投递的回复。
- 审计：`decision_log kind='catchup'`，每次全扫一条 `scanned`（含 trigger、目标、总漏答数——晨报可统计「补了几条」），每目标一条具体结果（`replied` / `nothing_new` / `skipped_quiet` / `skipped_offline` / error）。
- `OneBotClient.call_action(action, params, on_event=...)`：echo 关联的请求-响应原语；轮询响应期间到达的推送事件经 `on_event` 重新派发进 `handle_event`，不因拉历史而丢实时消息。 NapCat 扩展 action 仅此一处使用，失败面收敛在 catch-up 内。

## 备选与取舍

- **扫描即时触发（无 10s 延迟）**：会与 NTQQ 补投赛跑——扫描先跑发现「无事」，补投事件随后到达被正常回复，看似无损；但一旦补投晚于扫描且历史拉取也不可用，本可当轮补回的消息要等下次重连。延迟 + 流序判定 + 内容去重三层防重，成本只是 10 秒。
- **漏掉的消息直接作为 user 回合写进流**：历史完整，但消息原文要过 `sanitize_inbound`，注入式措辞会把整个补回回合打成 hostile；且回合流里出现一段「不是当场说的话」会污染后续上下文召回。证据通道（wrap_untrusted）是既有旁路的成熟做法。
- **深夜直接回复、只是延迟投递（available_at=08:00）**：省一次重扫，但回复是凌晨口吻、且重扫能顺带覆盖夜里新到的消息；outbox 重扫方案让「是否还需要补回」始终用最新状态判定。
- **把 catch-up 做成 runtime 的 tick 轮询**：runtime 不知道连接何时建立，水位语义会变成「周期巡检」而非「上线一次」；触发点属于平台适配器的连接生命周期，放 `platforms/qq.py` 内聚。

## 后果

- 掉线期间的漏答有了结构性兜底；每次上线的补回量可在晨报里统计（`decision_log kind='catchup' action='scanned'`）。
- 新增 config 键（均有默认，零配置可用）：`qq.catchup_enabled` / `catchup_delay_seconds` / `catchup_min_interval_seconds` / `catchup_max_messages` / `catchup_fetch_history` / `catchup_from_cards` / `catchup_max_people` / `catchup_include_unknown_cards` / `catchup_respect_quiet_hours` / `catchup_prompt`。
- 补回的历史图片走**同一条视觉直读通道**：`qq.vision_enabled=true` 时按 `data.url` 下载（10s、单张 ≤5MB、单轮最多 4 张），以内联 data URL 随补回回合给 LLM；失败/未开启时诚实降级为 `[图片]` 占位。图片本体不落库。
- 无 schema 变更（水位复用 `schema_meta`）；`tests/test_catchup.py` 80 条离线 fixture 覆盖判定/顺序/水位/去重/注入/顺延/降级/人物卡目标/陌生人卡过滤/历史图片/echo 关联/历史拉取期间的事件重派发。
- 已知边界：回复出队后投递彻底失败（outbox 耗尽重试）会被误判为已答（assistant 回合已在）——概率低，靠 outbox 五次重试 + 用户侧追问兜底；`get_friend_msg_history` 的真实响应形状待实机回填（fixture 按 NapCat 源码 action 定义构造）。
