---
title: ADR-0013 外访会话：主人授权下的跨会话主动聊天
type: adr
status: superseded
tags: [adr, runtime, visit, cross-session, context, privacy, summary]
updated: 2026-11-01
---

# ADR-0013：外访会话（session visit）

> **boss 需求原文（2026-11-01）**：「加一个授权去和别人私聊的能力，允许一定程度上的迁移：去找别人聊之前，先总结目的，作为在今日时间线之后、历史上下文之前的目的区，转向其它对话去聊天，依旧正常加载上下文，但需要总结会话内容。直到 20 分钟内与她没有新对话，才回归正常。」
>
> **2026-11-01 二次口径**：boss 觉得「一次授权、一次外访」太不自由，改为**多会话漫游 + 线程记忆**，见 [ADR-0014](0014-session-roaming.md)。本 ADR 降级为 0014 的**单次外访子集**：目的区、20 分钟空闲归位、归位总结、owner 优先、目标停聊、隐私红线全部保留。

## 0. 一句话

主人**显式授权**后，鲸鱼娘可以带着一段**目的摘要**去和某个已绑定的家人/老师私聊；目标会话正常加载它自己的历史，目的区插在“今日时间线”和“历史上下文”之间；对方 20 分钟没有新消息（或主人叫她回去/到达硬上限）就**归位**，并把这次外访总结成一条主人可读的回顾，存进记忆与下一轮上下文。

这不是“跨会话读私聊”，而是“经主人授权、去和某个人当面聊一次”。

## 1. 背景与问题

现状：

- `read_chat scope=private` **只读当前会话**（隐私隔离，见 T-0072）。
- 主人在自己的会话里问“你能看看我妈吗”，鲸鱼娘无法读妈妈的私聊；她只能让主人转发（`scope=forward`）或等妈妈直接来找她。
- 人物卡/名册只解决“认人”，不解决“换到另一个会话去聊”。
- 现有的主动私聊只允许主人和姐姐两个对象（ADR-0006），家人/老师没有合法的主动开场通道。

boss 想要的是：主人授权后，她可以**主动去**找妈妈/老师聊一件事，带上目的和背景，聊完回来汇报；不是把别人的私聊内容直接摊在主人会话里。

## 2. 术语

| 术语 | 含义 |
|---|---|
| **home session** | 外访的起点，默认是主人会话；归位后回到这里 |
| **visit target** | 外访对象；必须是 `owner_bindings` 里的 `family` / `teacher`（可配置），且有人物卡 |
| **purpose** | 外访目的：主人为什么让她去、要问什么、边界是什么 |
| **purpose zone / 目的区** | prompt 里新加的一段，位置固定：`past_timeline` 之后、`history` 之前 |
| **visit summary / 外访小结** | 归位时对目标会话的总结：聊了什么、有没有承诺/待办、情绪、是否要回报主人 |
| **归位 / return** | 结束外访，回到 home 的主动/继续对话节奏；不是“退出登录” |
| **attention lease** | 外访期间的注意力租约：主动/继续对话的车道转向 target；其他会话的入站仍正常回复，但不重置外访计时 |

## 3. 决策

### 3.1 授权与范围（硬门）

- **只有主人能发起**。`visit_chat` 动作只在 owner 会话（或 owner 绑定会话）里可用；模型不能自己给自己授权。
- 目标必须同时满足：
  1. 人物卡存在且能解析出 uin；
  2. `owner_bindings` 里是该 uin 的 `family` 或 `teacher`（`visit.allowed_roles` 可配，默认这两类）；
  3. 不是 owner/friend/self；
  4. 当前没有 active visit（全局同一时间只允许一个外访）。
- 目标若是陌生人、只有昵称没有 uin、或绑定已解除 → 拒绝，不做猜测。
- 目标侧的 `chat_stop` / `busy` 停聊 flag 仍然有效：外访期间对方说“别聊了/在忙”，立即归位，当天不再主动找她。
- phase 1 只支持**主人显式发起**。phase 2 才考虑“她提议 → 主人审批”的两步授权。

### 3.2 目的区：prompt 里的精确位置

`ContextAssembler.build` 的 section 顺序改为：

```text
stable_prefix
past_timeline          # 今日/往日时间线（cacheable）
visit_purpose          # ← 新增，仅 active visit 且 session == target 时出现
history                # 目标会话自己的历史（cacheable）
runtime_context        # 动态段
turn_tail              # 当前用户消息
```

`model_messages()` 同步：`visit_purpose` 作为一条 `system` 消息插在 `past_timeline` 和 `history_messages` 之间。目的区在单次外访内**字节稳定**，可以命中缓存；外访开始/结束各造成一次 cache miss，可接受。

目的区模板（不含 uin、不含主人原话、不含秘密）：

```text
【本次外访】
授权：主人哥哥在 2026-11-01 20:10 同意（来源 turn #1234）
对象：妈妈
目的：问一下她今天身体怎么样，需不需要我们周末回去
背景摘要：主人哥哥今天提到妈妈腰不舒服；不要提主人的作息、行踪和秘密
边界：不替主人承诺；不追问她不想说的；被问主人私事就撒娇带过
返回：她 20 分钟没有新消息，或主人叫我回去；最长 60 分钟
```

规则：

- `purpose` 由主人原话 + 一段**受控的上下文摘要**组成；长度上限 `visit.purpose_max_chars`（默认 400）。
- 目的区不塞原始聊天记录、不塞 uin、不塞记忆召回；只用称呼。
- 目标的历史照常加载（她自己的会话），但**主人的历史不加载**；目标只能从目的区的摘要知道“主人让我来问什么”。
- 目的区只出现在 target 会话；主人会话永远看不到这段（主人看到的是归位后的小结）。

### 3.3 状态机

```text
pending ──approve/owner_action──> active ──idle 20m / owner / stop / max──> returning ──summary──> returned
   │                                │
   ├──deny/expire──> cancelled      ├──unbind/restart──> interrupted ──summary──> returned
   └──quiet defer──> pending        └──target stop──────> returned (summary)
```

| 状态 | 含义 | 允许的动作 |
|---|---|---|
| `pending` | 已授权、目的已生成、开场白未发 | 发开场 / 取消 / 等安静时段 |
| `active` | 开场已发，注意力在 target | 正常回合 / 重置 idle / 归位 |
| `returning` | 正在生成小结 | 只允许 summary |
| `returned` | 已归位、summary 已存 | 主人下一轮可读 recap |
| `cancelled` | 主人取消/授权过期 | — |
| `interrupted` | 进程重启/目标解绑 | 启动时补 summary 后关闭 |

- 同一时间全局只允许一个 `active`；target 可以被多次外访，但同一天有 `visit.max_per_day` 上限。
- `session_visits` 表用 `status` + `home_session` / `target_session` 索引；启动时把遗留 `active/pending` 标记为 `interrupted` 并补总结，不自动续访。

### 3.4 进入外访：开场回合

1. 主人在 owner 会话里说“你去问问妈妈今天身体怎么样”；
2. 模型调用新动作：

```json
{"tool": "visit_chat", "args": {
  "target": "妈妈",
  "purpose": "问她今天身体怎么样，需不需要周末回去",
  "max_minutes": 60,
  "return_after_minutes": 20
}}
```

3. 运行时校验授权/目标/名额/安静时段，生成 purpose + purpose_json，写 `session_visits`；
4. 在 **target 会话**里跑一个“外访开场回合”：
   - context = 目标会话的 stable/past_timeline + **purpose zone** + 目标 history + dynamic；
   - system 指令：“你受主人委托来找 {name}，目的是……先说第一句，别像客服，别一次说完”；
   - 生成 1 条消息，经新 outbox kind `visit_message` 发给 target；
   - 记录一条 assistant turn（带 `visit_id`），写 `opened_at`；
5. 排一个 `visit_return` wakeup：`fire_at = now + 20m`，payload `{visit_id}`；
6. 失败（LLM 不可用 / 发送失败 / target 不可达）→ 不进入 active，回滚为 `cancelled`，在 owner 回合里如实说明。

开场只发一次；`visit.followup_minutes`（默认 5）内对方没回，可发**一条**轻跟进（可选，默认开），之后不再催，等 idle 归位。对方一直不回 → 20 分钟后归位，summary 记 `no_reply`。

### 3.5 外访期间

- **target 的入站**：走正常 `handle_user_message(target_session)`；运行时发现该 session 有 active visit，加载 purpose zone，正常回复；每条 **target 用户消息**把 `visit_return` wakeup 重置到 `now + 20m`，并更新 `last_target_message_at`。
- **bot 自己发的消息不重置 idle**：否则她可以靠自言自语永远不归位。只有对方的回复才算“新对话”。
- **owner 的入站**：主人优先。检测到 owner 会话有消息 → 先 `return_visit(reason='owner_message')`（生成 summary、关闭 visit），再处理主人这一轮；主人的这一轮 context 里带 `visit_recap`。
- **其他会话**：正常回复，但不加载 purpose zone、不重置 visit timer；外访的注意力租约只影响主动/继续对话车道，不是硬锁。
- **主动/继续对话**：外访 active 期间，owner 的撒娇阶梯/self_continue 暂停；target 的 continue_policy 也暂停（由 visit_return 接管沉默）。归位后恢复。
- **安静时段**：开始外访默认顺延到安静结束（`visit.respect_quiet_hours=true`）；active 外访在安静开始时归位并小结，不跨夜聊。
- **硬上限**：`visit.max_minutes`（默认 60）到点必归位，即便对方还在回。
- **对方停聊**：`chat_stop` / `busy` flag 生效 → 立即归位，当天不再主动找她。
- **多目标同时来信**：target 的消息优先进入外访；其他会话正常回复但不改变 visit 状态。

### 3.6 归位与总结

触发条件（任一）：

1. target **20 分钟**没有新的用户消息（`idle_return_minutes`）；
2. owner 发来新消息（主人优先）；
3. target 触发 `chat_stop` / `busy`；
4. 到达 `max_minutes`；
5. 安静时段开始；
6. 目标解绑 / 进程重启（标记 interrupted）。

归位流程：

1. 取 `started_at..returned_at` 之间、带 `visit_id` 的 target turns（最多 40 条 / 4000 字）；
2. 一次 LLM 总结，严格 JSON：

```json
{"summary": "...", "topics": ["..."], "commitments": ["..."],
 "mood": "平静|开心|担心|疲惫|...", "open_loops": ["..."],
 "report_to_owner": "...", "privacy_flags": ["..."]}
```

3. LLM 不可用/解析失败 → 确定性 fallback：最后 10 条用户消息截断 + 关键词 topics + `summary_degraded=true`；
4. 写 `session_visits.summary` / `summary_json`，状态 `returned`；
5. 写一条 `memory_items`：`type='relationship'`、`origin_scope='visit'`、`source_type='visit'`、`source_id=visit_id`；
6. 给 owner 会话准备 `visit_recap` 动态块（下一次 owner 回合注入一次）：

```text
【外访回顾】妈妈（20:10–20:31，21 分钟）
目的：问她身体怎么样
结果：腰还是不舒服，周末想让我们回去吃饭；她没提别的
待办：主人哥哥周末要不要回去，需要你定
隐私：她提到不想让外婆知道，别外传
```

7. `visit.notify_owner=true` 时可选主动给 owner 发一条短消息；默认 **false**，等主人下次说话时自然带出；
8. 清 active 指针，恢复 owner 的主动/继续对话车道。

**总结的隐私口径**：

- 默认只给主人**高层总结**，不贴原话（`visit.summary_include_quotes=false`）；需要原话时主人在 owner 会话里明确问，且仍受 `privacy`/secret notes 约束。
- target 的原始消息**永不**进入 owner 的 history；只有 summary 进 owner 的 dynamic/memory。
- 目的区和 summary 都不含 uin；target 侧永远看不到 summary/目的区。

### 3.7 数据模型（schema v13 → v14）

新增表：

```sql
CREATE TABLE IF NOT EXISTS session_visits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  home_session TEXT NOT NULL,
  target_session TEXT NOT NULL,
  target_name TEXT NOT NULL DEFAULT '',
  target_uin TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  purpose TEXT NOT NULL DEFAULT '',
  purpose_json TEXT NOT NULL DEFAULT '{}',
  authorization_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  approved_at REAL,
  opened_at REAL,
  last_target_message_at REAL,
  last_bot_message_at REAL,
  idle_return_at REAL,
  max_until REAL,
  returned_at REAL,
  close_reason TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}',
  idempotency_key TEXT UNIQUE,
  visit_id TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_visits_home_status
  ON session_visits(home_session, status);
CREATE INDEX IF NOT EXISTS idx_visits_target_status
  ON session_visits(target_session, status);
```

`conversation_turns` 新增：

```sql
ALTER TABLE conversation_turns ADD COLUMN visit_id TEXT;
CREATE INDEX IF NOT EXISTS idx_turn_visit ON conversation_turns(visit_id, id);
```

- `SCHEMA_VERSION` 13 → 14；`db.py` 的 `_COLUMN_MIGRATIONS` / `_INDEX_MIGRATIONS` 幂等迁移；旧库自动补列，旧行 `visit_id=NULL`。
- `tests/test_p0.py` 版本断言同步；新增迁移测试（v13→v14）。
- 不建 `visit_messages` 表：turn 已经带 `visit_id`，summary 按 visit_id 取。

### 3.8 运行时 / 适配器接线

| 位置 | 改动 |
|---|---|
| `runtime.py` | 新动作 `visit_chat`（owner only）；`run_visit_opening`；`return_visit`；`visit_recap` 动态块；owner 入站 preempt；启动 reconciliation |
| `context.py` | 新 section `visit_purpose`（past_timeline 后、history 前）；`visit_recap` 进 runtime_context；`model_messages()` 顺序同步 |
| `wakeup/` | 新 intent `visit_return`；重置用 cancel+schedule（payload 带 visit_id）；不占 owner 的 self_continue 车道 |
| `outbox.py` / `platforms/qq.py` | 新 kind `visit_message`：只允许 active visit 的 target private session；payload `{visit_id, session_id, text, media}`；幂等键 `visit-open:<visit_id>`；不走 ProactiveGate 的 90 分钟池 |
| `memory/entities.py` | target 解析复用人物卡 + `owner_bindings`；名册规则不变 |
| `memory/service.py` | visit summary 的 `origin_scope='visit'`，owner 可读，family/teacher 不可见 |
| `continue_policy.py` | active visit 期间 home/target 的阶梯都暂停；归位后恢复 |
| `cli.py` | `fatfish visit list/show/end`（phase 1 可选，便于排障） |
| `schema.sql` / `db.py` | v14 表 + 列 + 索引 |

**owner 优先的具体实现**：

- `handle_user_message` 开头判断 `session_id == home_session` 且存在 active visit → 调 `return_visit(reason='owner_message', summarize=True)`；summary 可能花一次 LLM 调用，但主人这一轮仍要正常回复，不能因为总结失败而卡住。summary 失败 → fallback + `summary_degraded`，主人回合继续。
- 如果 owner 消息在 target 消息之后同一 tick 到达，按“owner 先归位”处理；target 那条仍按正常入站回复（但不再属于 visit；标记 `visit_id=None`）。

**重启 reconciliation**：

- `Runtime.__init__` / `run_forever` 启动时：`SELECT * FROM session_visits WHERE status IN ('pending','active')`；
- 标记 `interrupted`，按已有 turns 生成 deterministic summary（LLM 可用则 LLM），清 active 指针；不发任何开场/跟进消息。
- 目的：避免进程重启后拿着旧目的去和对方继续聊。

### 3.9 与现有机制的关系

| 机制 | 外访期间 | 归位后 |
|---|---|---|
| `read_chat scope=private` | 不变：仍只读当前会话；外访不是 read_chat | 不变 |
| `read_chat scope=forward` | 不变；主人仍可转发 | 不变 |
| 主动私聊（owner/friend） | owner 的主动/仪式/分享暂停；target 不走 ProactiveGate，走 visit 名额 | 恢复 |
| `continue_policy` 撒娇阶梯 | home/target 都暂停 | 恢复 |
| 安静时段 | 开始顺延；active 到点归位 | 正常 |
| `catchup` | target 消息正常；重启后 visit 标记 interrupted | 正常 |
| 记忆召回 | target 只看自己的会话 + owner 非涉密公开事实；purpose 摘要受控 | summary 作为 owner 可读记忆 |
| 人物卡/名册 | 目标必须是已绑定 family/teacher；名册规则不变 | 不变 |
| 测试 | 新增 `test_session_visit.py`（状态机/目的区/归位/总结/隐私/重启） | 全量回归 |

### 3.10 配置（全部有默认）

```toml
[visit]
enabled = true
allowed_roles = ["family", "teacher"]   # 不含 owner/friend/陌生人
idle_return_minutes = 20
max_minutes = 60
followup_minutes = 5                   # 0 = 不跟进
max_per_day = 3
min_interval_minutes = 60
respect_quiet_hours = true
notify_owner = false
purpose_max_chars = 400
summary_max_chars = 600
summary_include_quotes = false
```

### 3.11 台账与观测

- `decision_log kind='visit'` actions：`requested` / `denied` / `deferred_quiet` / `opened` / `target_replied` / `return_scheduled` / `owner_preempted` / `target_stop` / `max_reached` / `returned` / `summarized` / `summary_degraded` / `interrupted`。
- metrics：`visit_count`、`visit_duration_seconds`、`visit_replies`、`visit_summary_tokens`、`visit_denied`。
- 晨报可统计“昨天外访了几次、每次多久、有没有待办”。

### 3.12 分期

| 期 | 内容 | 验收 |
|---|---|---|
| **P0** | 表/状态机/purpose zone/owner `visit_chat`/开场/20m 归位/summary/owner recap/重启 reconciliation | 单测 + 假 LLM/QQ 适配器集成测试；不接真实账号 |
| **P1** | 安静时段/名额/owner preempt/target stop/CLI/晨报统计 | 真实账号一次妈妈外访（干跑 + 实跑） |
| **P2** | 她提议 → 主人审批；Web 面板；“外访中”状态展示 | 主人审批流测试 |

### 3.13 验收（P0）

- 主人在 owner 会话说“去问妈妈身体怎么样” → 生成 purpose、target=妈妈、发一条开场；
- target 会话的 prompt 顺序严格为 `stable → past_timeline → purpose → history → runtime_context → tail`；
- 目标的回复正常进入 target history，带 `visit_id`；purpose zone 不含 uin/主人原话；
- 20 分钟无 target 新消息 → 归位；summary 落库；owner 下一轮看到 `visit_recap`；
- owner 中途发消息 → 立即归位并正常回复，target 不再收到 visit 开场/跟进；
- 目标说“别聊了/在忙” → 立即归位，当天不再主动；
- 重启 → active 变 interrupted，补 summary，不再发消息；
- 目标不是 family/teacher 或未绑定 → `visit_chat` 拒绝，无任何外发；
- 全量测试全绿；schema v14 迁移幂等。

## 4. 备选与取舍

- **直接放开 `read_chat scope=private` 读任意绑定联系人**：拒绝。那会把别人的私聊摊到主人会话，且没有目标侧同意；外访是“去聊”，不是“去读”。
- **硬切会话/全局锁**：拒绝。其他会话的入站仍应正常回复；用 attention lease 而不是硬锁，避免主人/家人同时来信时丢消息。
- **每次 summary 都调 LLM**：不。失败必须可降级，否则归位会卡住；deterministic fallback 是硬要求。
- **归位时静默、不总结**：不满足 boss“需要总结会话内容”；summary 是外访的交付物。
- **主人消息不打断外访**：拒绝。主人优先是安全边界；外访可以下次再开。
- **把 purpose 放进 stable prefix**：拒绝。purpose 每次外访变化，会击穿缓存；放在 past_timeline 之后的独立 section，单次外访内稳定。

## 5. 风险

1. **隐私**：summary 可能带出 target 的敏感内容；默认高层总结、不贴原话、owner-only，且 target 的 `chat_stop` 立即生效。
2. **打扰**：外访是主动开场；靠主人授权 + 每日/间隔上限 + 安静时段 + 对方停聊 flag 控制。
3. **上下文膨胀**：purpose + target history + summary；purpose/summary 都有字符上限，target history 走现有窗口。
4. **状态泄漏**：进程重启/解绑可能留下 active；启动 reconciliation 必须强制关闭。
5. **模型越权**：模型可能把“名册上的人”误当成可外访对象；`visit_chat` 的 target 解析和角色白名单在运行时硬校验，模型参数不能指定 uin。
6. **缓存**：外访开始/结束各一次 prefix miss；可接受，不能为了缓存把 purpose 塞进稳定前缀。

## 6. 待 boss 拍板

1. **目标范围**：默认只允许 `family` + `teacher`；姐姐已经有主动私聊车道，是否也允许外访？（建议：先不纳入。）
2. **主人打断**：主人在外访期间发消息，是否**立即归位**？（建议：是；外访可下次再开。）
3. **总结粒度**：默认只给高层总结、不贴原话；是否允许主人明确要求原话？（建议：允许，但走 privacy/secret 检查。）
4. **归位通知**：默认不主动给主人发“我聊完了”，等主人下次说话带出；是否要主动通知？（建议：先 false，观察一周。）
5. **对方知情**：外访是主人授权、对方已绑定；是否需要开场时说明“是主人让我来问的”？（建议：目的区允许自然提及，不强制每次声明。）
6. **安静时段**：默认顺延/归位；主人是否允许 `force` 跨安静时段？（建议：默认不允许，主人显式 force 才放行。）

## 7. 相关

- [ADR-0007 单轮计划 + 异步续写](0007-agent-loop.md)
- [ADR-0008 上线补回](0008-catchup.md)
- [ADR-0009 继续对话策略](0009-continue-policy.md)
- [平台分支](../branches/platforms.md) · [运行时分支](../branches/runtime.md) · [安全分支](../branches/security.md)
