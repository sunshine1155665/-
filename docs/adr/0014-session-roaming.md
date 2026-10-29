---
title: ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主
type: adr
status: accepted
tags: [adr, runtime, roaming, multi-session, attention, memory, privacy]
updated: 2026-11-01
---

# ADR-0014：多会话漫游（session roaming）

> **boss 需求原文（2026-11-01）**：「感觉不太自由。能否做在多个会话里流窜还保留部分记忆能力的？」
>
> 前身：[ADR-0013 单次外访](0013-session-visit.md)（已降级为本文的“单次外访子集”）。
>
> **boss 拍板（2026-11-01，六问六答）**：
> 1. 自主档位：**她提议、主人批准**（`autonomy="ask"`），不直接 auto。
> 2. 房间范围：**群聊不纳入**；family / teacher / 姐姐（friend）都可以。
> 3. 每日预算：**30 分钟 / 6 次**，按推荐执行。
> 4. 归位通知：**要告诉主人**（`notify_return=true`）。
> 5. 细节检索：记忆库**可以检索摘要/结构化事实**（owner-only，`origin_scope='roam'`）；**原始对话不进跨会话检索**，要细节时她可以申请再回那个房间问/查，仍受隐私协议约束。
> 6. 群聊：**不纳入**（`include_whitelisted_groups=false`）。

## 0. 一句话

在主人给出的**常驻授权**范围内，鲸鱼娘把多个已绑定会话当成“房间”自由来往：每个房间有一张持续更新的**线程卡**（目的、摘要、待办、情绪、上次说到哪），她按**注意力调度**选择下一站；离开时写回线程卡和漫游小结，回来时只带“部分记忆”（线程卡 + 摘要），不搬原始聊天。

单次外访（ADR-0013）只是“只开一个房间、一次授权、20 分钟归位”的特例。

## 1. 概念模型

| 概念 | 定义 |
|---|---|
| **Room（房间）** | 一个允许漫游的会话：owner / 已绑定 family / teacher / friend / 白名单群 |
| **Thread card（线程卡）** | 每个房间一张的持久“部分记忆”：摘要、待办、承诺、情绪、上次话题、未读数、冷却 |
| **Roam trip（漫游）** | 一次连续停留：从进入房间到离开；有 purpose、summary、起止时间 |
| **Attention block（注意力区）** | prompt 新段：当前 trip 目的 + 最近线程卡 + due 待办；位置固定在 today timeline 之后、history 之前 |
| **Attention scheduler（注意力调度）** | 定时给房间打分，决定下一站；也可由主人直接指派 |
| **Standing grant（常驻授权）** | 主人一次配置的漫游范围/预算/安静时段/暂停开关，不是每次审批 |
| **Partial memory（部分记忆）** | 线程卡 + 漫游小结 + 全局漫游日志；原始聊天仍只留在各自房间 |

设计原则：

1. **房间之间只搬摘要，不搬原文。**
2. **同一时间只“在场”一个房间**（顺序注意力，不是并行分身）。
3. **主人永远是最高优先级**：主人开口立即回 home。
4. **自由来自常驻授权 + 调度，不来自绕过白名单。**
5. **每个房间的原始历史照常加载；跨房间只带线程卡。**

## 2. 与 ADR-0013 的关系

0013 的硬约束保留为“单房间模式”：

- purpose zone 位置（today timeline 后、history 前）；
- 20 分钟无对方新消息归位；
- 归位总结；
- owner 优先、target `chat_stop`、无 uin、无秘密、无陌生人。

0014 新增：

- 多个房间、多张线程卡，而不是一个 active visit；
- 常驻授权 + 注意力调度，而不是每次由主人发起；
- 离开/回来都更新线程卡，形成“部分记忆”；
- 主人可随时暂停/收窄范围，并看到漫游日志。

## 3. 房间与授权

### 3.1 默认房间

| 房间 | 默认 | 说明 |
|---|---|---|
| owner | ✅ 永远 | home；主人优先级最高 |
| family | ✅ | 妈妈等已绑定家人 |
| teacher | ✅ | 杨老师/藤老师等已绑定老师 |
| friend | ⚠️ 可配 | 姐姐已有主动私聊车道，是否纳入漫游由主人定 |
| group | ⚠️ 默认关 | 只允许 `qq.read.groups` 白名单；默认只在被 @ 时进入，不主动刷群 |
| 陌生人 | ❌ | 永远不允许；未绑定/无 uin 直接拒绝 |

### 3.2 常驻授权（Standing grant）

主人一次配置，长期有效，直到撤销：

```toml
[roam]
enabled = true
allowed_roles = ["family", "teacher", "friend"]  # owner 永远在家，不用列；群不纳入
allowed_sessions = []                      # 额外白名单；空 = 按角色
include_whitelisted_groups = false         # boss 拍板：群聊不纳入
autonomy = "ask"                           # boss 拍板：她提议、主人批准
daily_minutes = 30                         # 每天在外总时长
max_trips_per_day = 6
max_open_threads = 6
per_thread_cooldown_minutes = 60
idle_return_minutes = 20
max_trip_minutes = 60
quiet_hours_respect = true
announce_departure = false                 # 出门时是否给主人一句
notify_return = true                       # boss 拍板：回来要告诉主人
summary_include_quotes = false
```

- `autonomy=off`：只能主人说“去看看妈妈”；
- `autonomy=ask`：她自己提议，主人批准后出门（推荐起步档）；
- `autonomy=auto`：在授权范围内自己决定（主人明确要“自由”时再开）。
- 主人可以说「别乱跑了」「只准找妈妈」「回来」「今天别去群里」——运行时映射为 pause/收窄/leave。
- 每天预算/安静时段自动生效；主人可显式 `force` 单次跨安静时段（默认不允许）。

## 4. 线程卡（部分记忆的核心）

每个房间一行 `session_threads`：

| 字段 | 含义 |
|---|---|
| `session_id` / `role` / `display_name` | 房间身份 |
| `summary` | 最近一次离开时的 1–3 句摘要 |
| `last_topic` | 上次聊到的话题 |
| `open_loops_json` | 未完成的待办/悬念（与 `open_loops` 表关联） |
| `commitments_json` | 她答应过对方/主人要做的具体事 |
| `mood` | 上次离开时的情绪（平静/担心/开心/疲惫…） |
| `unread_count` | 离开后对方发来的新消息数 |
| `last_seen_at` / `last_summary_at` | 时间锚 |
| `last_turn_id` | 摘要覆盖到哪条 turn |
| `attention_score` | 调度器算出的当前吸引力 |
| `cooldown_until` | 该房间的冷却结束时间 |
| `trip_count` / `total_seconds` | 漫游统计 |
| `status` | `open` / `idle` / `paused` / `blocked` |

线程卡就是“跨房间的部分记忆”：她回到一个房间时，prompt 里带的是这张卡 + 该房间自己的 history，而不是别的房间的原文。

## 5. 注意力区（prompt 位置）

```text
stable_prefix
past_timeline        # 今日/往日时间线
attention_block      # ← 新增：当前 trip 目的 + 最近线程卡 + due 待办
history              # 当前房间自己的历史
runtime_context
turn_tail
```

`attention_block` 规则：

- **只出现在当前房间**；主人会话里额外带“漫游回顾”，目标房间只带自己的卡 + 本次目的。
- 不出现 uin；线程卡只带称呼/关系/摘要/待办，不带对方原文。
- 内容上限 `roam.attention_max_chars`（默认 800），按 attention_score 取 top N（默认 5）。
- 单次 trip 内字节稳定，可命中缓存；进入/离开各一次 cache miss。

示例（当前在妈妈房间）：

```text
【当前注意力】
本次目的：问妈妈今天身体怎么样，周末要不要回去吃饭
从上一个房间带来：主人哥哥提到妈妈腰不舒服；不要提主人的作息和秘密
最近线程卡：
- 杨老师：上周作业已交；她问过一次你的近况（未回）
- 妈妈：上次聊到周末买菜；她担心外婆的身体
待办：主人哥哥周末是否回去，需要他定
返回：妈妈 20 分钟没有新消息，或主人叫我回去；最长 60 分钟
```

示例（回到主人房间）：

```text
【漫游回顾】
今天 20:10–20:31 去了妈妈那里：腰还是不舒服，周末想让我们回去吃饭。
今天 18:40–18:52 去了杨老师那里：作业已交，她问了你最近忙不忙（已回）。
待办：周末回不回去，需要主人定。
```

## 6. 注意力调度

每 `roam.tick_minutes`（默认 10）或每个回合结束跑一次评分：

```text
score = 100 * owner_waiting
      + 50  * owner_assigned_purpose
      + 40  * min(unread, 3)
      + 30  * due_open_loop
      + 20  * commitment_due
      + 2   * hours_since_last_visit
      + 10  * desire/curiosity
      - 100 * quiet_hours
      - 100 * cooldown_active
      - 100 * daily_budget_exhausted
```

- `owner_waiting`：主人有未回消息 → 永远回 home，不评分。
- 分数 ≥ `roam.threshold`（默认 50）且预算/冷却允许 → 可以出发。
- `autonomy=ask`：先在主人会话里说“我想去看看妈妈，因为…”，等主人确认；`auto`：直接出发（可 `announce_departure` 给主人一句）。
- 同一房间有 `cooldown`；同一时间只允许一个 trip。
- 主人可以手动指定房间/目的，跳过评分。

### 6.1 她提议、主人批准（boss 拍板：ask）

评分过线且 `autonomy="ask"` 时，**不直接出门**：

1. 写一条 `roam_trips`，`status="proposed"`，`purpose_json` 带评分依据（未读/待办/上次时间）；
2. 经 `roam_notice` 给主人发一条提案（不是开场，不发给目标）：
   ```text
   妈妈那边有 2 条未读，还欠一个“周末回不回去”的待办。
   我想去看看她，顺便问问身体怎么样。要我去吗？
   ```
3. 主人说“去吧/可以/好” → 模型调用 `roam_approve`，状态 `proposed → entering`；
   主人说“不用了/别去” → `roam_decline`，状态 `cancelled`，房间进入短 cooldown；
4. 提案 TTL `roam.proposal_ttl_minutes`（默认 30）；过期自动 `expired`，不自动重提；
5. 同一时间只允许一条 pending proposal；`roam.max_proposals_per_day`（默认 6）防刷；
6. 安静时段不提案；主人明确指派（“去看看妈妈”）时跳过提案，直接 `entering`。

提案只发给主人；目标房间在批准前**不会收到任何消息**。

## 7. 一次漫游的生命周期

```text
idle ──score/owner──> entering ──opening──> active ──idle 20m/owner/stop/max──> leaving ──summary──> idle
   │                     │                     │
   └── paused ───────────┴── failed ──────────┘
```

### 7.1 进入

1. 选定房间 + purpose（主人指派或由线程卡生成）；
2. 写 `roam_trips`（status=`entering`）；
3. 构建 attention_block；
4. 在该房间跑一个“开场/回复”回合：
   - 若有未读：先正常回复未读，而不是硬塞开场；
   - 若无未读：生成一条自然开场（“妈妈，主人让我问问你…”），经 `roam_message` outbox 发出；
5. 记录 assistant turn（带 `trip_id`），排 `roam_return` wakeup（idle 20m / max 60m）。

### 7.2 在场

- 对方入站：正常回合，加载 attention_block + 本房间 history；每条**对方用户消息**重置 idle 计时；更新 `unread_count=0`、`last_turn_id`。
- 她自己发的消息不重置 idle（防止自言自语不归位）。
- 主人入站：**立即离开**当前房间（先 summary）→ 回 home 回复主人；主人这一轮带“漫游回顾”。
- 其他房间来信：只增加 `unread_count`/线程卡，不切换当前 trip；调度器下次考虑。
- 对方 `chat_stop`/`busy`：立即离开，房间 `cooldown` 到当天结束。
- 安静时段开始 / 到达 max / 预算耗尽：离开并 summary。

### 7.3 离开与总结

1. 取本次 trip 的 turns（带 `trip_id`，最多 40 条/4000 字）；
2. 一次 LLM 总结（JSON）：
   `summary/topics/key_facts/commitments/mood/open_loops/report_to_owner/privacy_flags`；
   `key_facts` 是**结构化、非逐字**的事实（“腰还是不舒服”“周末想让我们回去吃饭”），供主人日后检索；
3. 失败 → 确定性 fallback（最后 10 条用户消息截断 + 关键词），`summary_degraded`；
4. 写回 `session_threads`（summary/topic/key_facts/loops/commitments/mood/last_seen/last_summary_at）；
5. 写 `roam_trips`（status=`returned`，summary）；
6. 写 `memory_items origin_scope='roam'`（owner 可读，目标不可见；summary + key_facts 进检索，原文不进）；
7. 主人会话准备 `roam_recap` 动态块（下一次 owner 回合注入一次）；
8. `notify_return=true` 且**不是主人打断**、不在安静时段 → 经 `roam_notice` 给主人发 1–2 句归位通知：
   ```text
   我回来了。刚和妈妈聊了 21 分钟：她腰还是不舒服，周末想让我们回去吃饭。要不要我记着？
   ```
   安静时段归位则把通知顺延到安静结束（recap 仍在下一轮）；
9. 清 active trip；恢复 continue_policy/主动车道。

### 7.4 重启/异常

- 启动时把 `entering/active` trips 标记 `interrupted`，用已有 turns 生成确定性 summary，写回线程卡；
- 不自动续访、不补发开场/跟进；
- 房间的 `unread_count` 保留，调度器下次可再考虑。

## 8. 数据模型（schema v14）

```sql
CREATE TABLE IF NOT EXISTS session_threads (
  session_id TEXT PRIMARY KEY,
  role TEXT NOT NULL DEFAULT '',
  display_name TEXT NOT NULL DEFAULT '',
  target_uin TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'idle',        -- open/idle/paused/blocked
  summary TEXT NOT NULL DEFAULT '',
  last_topic TEXT NOT NULL DEFAULT '',
  open_loops_json TEXT NOT NULL DEFAULT '[]',
  commitments_json TEXT NOT NULL DEFAULT '[]',
  key_facts_json TEXT NOT NULL DEFAULT '[]',
  mood TEXT NOT NULL DEFAULT '',
  unread_count INTEGER NOT NULL DEFAULT 0,
  attention_score REAL NOT NULL DEFAULT 0,
  last_seen_at REAL,
  last_summary_at REAL,
  last_turn_id INTEGER,
  cooldown_until REAL,
  last_trip_at REAL,
  trip_count INTEGER NOT NULL DEFAULT 0,
  total_seconds REAL NOT NULL DEFAULT 0,
  thread_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS roam_trips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id TEXT NOT NULL UNIQUE,
  thread_session TEXT NOT NULL,
  home_session TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT '',
  purpose_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'proposed',    -- proposed/entering/active/leaving/returned/cancelled/expired/interrupted
  score REAL NOT NULL DEFAULT 0,
  proposal_expires_at REAL,
  approved_at REAL,
  authorization_json TEXT NOT NULL DEFAULT '{}',
  started_at REAL,
  ended_at REAL,
  close_reason TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  summary_json TEXT NOT NULL DEFAULT '{}',
  message_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_threads_status
  ON session_threads(status, attention_score);
CREATE INDEX IF NOT EXISTS idx_trips_thread_status
  ON roam_trips(thread_session, status);
CREATE INDEX IF NOT EXISTS idx_trips_home_status
  ON roam_trips(home_session, status);
```

`conversation_turns` 新增可空 `trip_id TEXT` + `idx_turn_trip(trip_id, id)`；旧行 NULL。

- `SCHEMA_VERSION` 13 → 14（若 0013 未落地，`session_visits` 不需要建，直接 `roam_trips`）。
- `db.py` `_COLUMN_MIGRATIONS` / `_INDEX_MIGRATIONS` 幂等；`tests/test_p0.py` 版本断言同步；v13→v14 迁移测试。
- 线程卡与 trip 都是 append/update 混合：trip 只增，线程卡 upsert。

## 9. 运行时 / 适配器

| 位置 | 改动 |
|---|---|
| `runtime.py` | `roam_tick`（评分/提案）、`roam_approve`/`roam_decline`、`start_trip`、`leave_trip`、`roam_recap`、`notify_return`、owner preempt、启动 reconciliation |
| `context.py` | `attention_block`（past_timeline 后、history 前）；owner 会话的 `roam_recap` 进 runtime_context |
| `wakeup/` | intents `roam_tick` / `roam_return` / `roam_proposal_expire`；不占 self_continue 车道 |
| `outbox.py` / `platforms/qq.py` | 新 kind `roam_message`（目标房间，只允许 grant 内；幂等 `roam:<trip_id>`）+ `roam_notice`（只发 owner：提案/归位通知） |
| `memory/entities.py` | 线程卡/人物卡解析；目标必须绑定 |
| `memory/service.py` | `origin_scope='roam'` 的 summary + key_facts，owner 可检索；target 不可见；原文不跨房间 |
| `continue_policy.py` | active trip 期间 home/房间阶梯暂停，由 roam_return 接管 |
| `cli.py` | `fatfish roam status/pause/resume/threads/trips`（P1） |
| `schema.sql` / `db.py` | v14 表/列/索引 |

**开场/回复策略**：

- 有未读 → 先正常回复未读（不硬塞开场）；
- 无未读 → 生成一条自然开场，必须带“主人让我来/我来问问”的来意（不假装偶遇）；
- 同一房间 `per_thread_cooldown` 内不重复开场；对方不回只允许 1 条轻跟进（`followup_minutes`），之后等 idle 离开。

## 10. 记忆与隐私

### 10.1 谁能看到什么

| 视图 | owner | 目标房间（妈妈/老师） | 其他房间 |
|---|---|---|---|
| 当前房间 history | 自己的 | 自己的 | 自己的 |
| 线程卡 | 全部 allowed 房间 | **只有自己这张** | 只有自己这张 |
| 漫游回顾 | ✅ 全局最近 N 次 | ❌ | ❌ |
| 其他房间原文 | ❌（只有摘要） | ❌ | ❌ |
| `memory_items origin_scope='roam'` | ✅ | ❌ | ❌ |

- 目标房间的 attention_block **绝不能**带其他房间的摘要/称呼；只带“本次目的 + 自己这张卡 + 相关 due 待办”。
- 主人的 attention_block 才带“漫游回顾”。
- 目标问“你还跟谁聊了” → 按现有边界撒娇带过，不列其他房间。

### 10.2 部分记忆的边界

- 线程卡是**摘要**，不是原文；默认不贴引语（`summary_include_quotes=false`）。
- 原始历史仍按房间加载，不跨房间；她回到一个房间时“记得”的是线程卡 + 该房间原历史。
- 漫游总结写入 `memory_items`，可被主人问起时召回；目标不可见。
- 秘密清单/作息/行踪等 owner 私密信息不进入目的区/线程卡/目标房间。

### 10.3 细节检索（boss 问：记忆库能否直接检索？）

**能检索的**：

- `roam_trips.summary` / `summary_json.key_facts` / `session_threads.summary`；
- 写入 `memory_items origin_scope='roam'`，**只有 owner 的召回视图能命中**；target 房间看不到；
- 主人问“上次妈妈说什么了/身体怎么样”，走正常 `search_memory`/recall 就能命中 key_facts（结构化、非逐字）。

**不能跨会话检索的**：

- 目标房间的原始 `conversation_turns` 不进入 owner 的检索面；owner 不能直接搜“妈妈原话”；
- 这是隐私协议的硬边界：owner 可以知道“聊了什么”，不能把别人的私聊原文当公共库翻。

**要更细的细节时**：

1. 归位 summary 里的 `key_facts` 先兜住大部分“细节”（谁、什么事、结论、待办），默认不贴原话；
2. 还不够 → 她可以**申请再回那个房间**（新的 trip，purpose=“回去确认细节”），按 `autonomy="ask"` 让主人批准；她回到房间读该房间原历史后，再给主人一份新的 summary——**仍然不把原文复制到 owner 记忆**；
3. 原话引用：默认 `summary_include_quotes=false`；只有目标明确同意/主人显式授权时才在**当次对话**里转述，且不写入跨会话检索库；
4. 目标房间永远看不到其他线程卡/漫游回顾。

## 11. 与现有机制

| 机制 | 漫游期间 | 结束后 |
|---|---|---|
| `read_chat` | 不变；漫游是“去聊”，不是“去读” | 不变 |
| 主动私聊/仪式/分享 | owner 的主动车道暂停；房间开场走 roam 预算 | 恢复 |
| `continue_policy` | home/房间阶梯暂停 | 恢复 |
| 安静时段 | 不出发；active 到点离开 | 正常 |
| catchup | 房间消息正常；重启 trip 标 interrupted | 正常 |
| 人物卡/名册 | 只用于认人；房间必须绑定/白名单 | 不变 |
| ProactiveGate 90 分钟池 | 漫游不占；走 `roam.daily_minutes`/`max_trips` | 恢复 |
| 记忆召回 | 目标只看自己房间 + owner 非涉密公开事实 | summary 作为 owner 可读记忆 |

## 12. 配置（完整）

```toml
[roam]
enabled = true
allowed_roles = ["family", "teacher", "friend"]
allowed_sessions = []
include_whitelisted_groups = false
autonomy = "ask"                 # boss 拍板：她提议、主人批准
threshold = 50
tick_minutes = 10
proposal_ttl_minutes = 30        # 提案过期，不自动重提
max_proposals_per_day = 6
idle_return_minutes = 20
max_trip_minutes = 60
followup_minutes = 5
daily_minutes = 30               # boss 拍板
max_trips_per_day = 6            # boss 拍板
max_open_threads = 6
per_thread_cooldown_minutes = 60
quiet_hours_respect = true
announce_departure = false
notify_return = true             # boss 拍板：回来告诉主人
attention_max_chars = 800
summary_max_chars = 600
summary_key_facts = true         # 结构化事实进 owner 检索
summary_include_quotes = false   # 原话默认不进跨会话库
```

## 13. 台账与观测

- `decision_log kind='roam'`：`scored` / `proposed` / `approved` / `declined` / `expired` / `departed` / `opened` / `replied` / `owner_preempted` / `target_stop` / `left` / `summarized` / `summary_degraded` / `notified` / `interrupted` / `budget_exhausted` / `quiet`。
- metrics：`roam_trips`、`roam_minutes`、`roam_replies`、`roam_summary_tokens`、`roam_denied`、`roam_unread`。
- 晨报：昨天去了哪些房间、各多久、有没有待办/承诺。
- CLI（P1）：`fatfish roam status` / `threads` / `trips` / `pause` / `resume`。

## 14. 分期

| 期 | 内容 | 验收 |
|---|---|---|
| **P0** | 线程卡 + 主人指派单房间 trip + attention_block + 20m 归位 + summary/key_facts + owner recap + `notify_return` + 重启 reconciliation | 离线单测/假适配器集成；不接真实账号 |
| **P1** | 评分调度 + **`ask` 提案/批准** + 未读/due 待办 + 预算/安静/冷却 + CLI + 晨报 | 真实账号一次妈妈/老师 trip（干跑+实跑） |
| **P2** | `auto` 自主漫游 + 多房间连续切换 + Web 面板“当前房间/线程卡” | 主人观察一周后再开 auto；群聊仍不纳入 |

## 15. 验收（P0）

- 主人说「去看看妈妈，问她身体」→ 线程卡/trip/purpose 生成，进入妈妈房间；
- prompt 顺序严格 `stable_prefix → past_timeline → attention_block → history → runtime_context → turn_tail`；
- 妈妈房间只看到自己的线程卡 + 本次目的，看不到杨老师/主人原文；
- 妈妈回消息 → 正常回合，idle 重置；20 分钟无新消息 → 离开 + summary + 线程卡更新；
- 主人中途发消息 → 立即离开并回主人，主人下一轮带漫游回顾；
- 妈妈 `chat_stop` → 立即离开，当天不再去；
- **ask 提案**：评分过线 → 只给主人发提案，目标在批准前**零消息**；主人拒绝/提案过期 → 目标仍零消息；
- **归位通知**：非主人打断的 idle 归位 → 主人收到 1–2 句 `roam_notice`，下一轮仍带完整 recap；安静时段顺延；
- **记忆检索**：主人搜“妈妈身体/周末”能命中 `origin_scope='roam'` 的 summary/key_facts；搜不到妈妈原始对话；
- **细节回访**：主人要细节 → 她按 ask 流程申请再回妈妈房间；回来给新 summary，原文仍不进 owner 检索库；
- 重启 → trip interrupted，补 summary，不发开场；
- 陌生人/未绑定/群聊 → 拒绝，无外发；
- 全量测试全绿；v14 迁移幂等。

## 16. 备选与取舍

- **真正并行多会话**：拒绝。单进程、单 LLM、单人格；并行会带来消息顺序、隐私和预算混乱。用“顺序注意力 + 线程卡”实现自由。
- **每次出门都要主人审批**：0013 的做法，boss 觉得不自由；0014 改为常驻授权 + `ask/auto` 档。
- **线程卡存原文**：拒绝。跨房间只搬摘要；原文留在房间。
- **全量历史跨房间加载**：拒绝。隐私和上下文都会爆；线程卡就是“部分记忆”的边界。
- **群聊默认纳入**：拒绝。群先只读/仅 @，白名单单独开。

## 17. 风险

1. **自由度 vs 打扰**：靠每日分钟/trip 上限、每房间冷却、安静时段、对方停聊、owner 随时暂停控制。
2. **隐私**：目标房间只能看自己的线程卡；owner 只看摘要；不贴原文；秘密清单不进入任何目的区。
3. **记忆漂移**：线程卡是摘要，可能丢细节；原始历史仍在房间，主人明确问时可回到该房间查（需授权）。
4. **调度抖动**：评分必须确定性、可审计；同一 tick 不重复出发；cooldown 硬约束。
5. **状态泄漏**：重启 reconciliation 必须强制关闭 entering/active；线程卡 unread 保留但不再自动发消息。

## 18. 已拍板与剩余实现细节

**已拍板（2026-11-01）**：

| 问题 | 决定 |
|---|---|
| 自主档位 | `ask`：她提议、主人批准 |
| 房间范围 | family / teacher / friend；**群聊不纳入** |
| 每日预算 | 30 分钟 / 6 次 |
| 归位通知 | `notify_return=true`，回来告诉主人 |
| 细节检索 | 记忆库可检索 summary/key_facts（owner-only）；原文不跨会话；要细节走“回访房间” |
| 群聊 | 不纳入 |

**留给实现时调的参数（不是产品决策）**：

1. `roam.threshold` / `tick_minutes`：先 50 / 10 分钟，跑两周看误报；
2. `max_proposals_per_day`：先 6，观察提案被拒率；
3. `key_facts` 粒度：先 3–6 条/次，按主人实际检索命中率调；
4. 原话引用：默认关；若主人明确要，走“目标同意/当次转述”，不落跨会话库；
5. `auto` 升级：P2 前先看两周 `ask` 数据（提案接受率、打扰反馈）；
6. 群聊：本轮不做；将来若做，只读/@ 触发，单独白名单。

## 19. 实现状态（P0，2026-11-01）

已落地、离线可测：

| 项 | 位置 |
|---|---|
| schema v14（`session_threads` / `roam_trips` / `conversation_turns.trip_id`） | `fatfish/schema.sql`、`fatfish/db.py` |
| 线程卡 / trip 生命周期 / 归位总结 / 注意力块 / recap | `fatfish/roam/`（`service.py`、`summary.py`、`intent.py`、`model.py`） |
| prompt 位置：`stable_prefix → past_timeline → attention_block → history → runtime_context → turn_tail` | `fatfish/context.py` |
| 主人指派（「去看看妈妈，问她身体」）、`roam_visit` / `roam_return` 动作 | `fatfish/runtime.py`、`fatfish/security/policy.py`、`fatfish/context.py` |
| 目标入站重置 idle、主人抢占、`chat_stop` 当天封房、重启 reconciliation | `fatfish/runtime.py`、`fatfish/roam/service.py` |
| `roam_message` / `roam_notice` outbox 投递 | `fatfish/platforms/qq.py`、`fatfish/runtime.py` |
| owner-only 摘要/关键事实记忆 + 目标原文不进 owner 细节检索 | `fatfish/memory/service.py` |
| 配置 | `config/low.toml [roam]` |

P0 明确未做（留给 P1/P2）：评分调度、`ask` 提案/批准、预算/冷却的自动出发、CLI/晨报、Web 面板、群聊。P0 只有主人显式指派会出门；`autonomy` 配置已存在但不驱动调度。归位总结 P0 用确定性 fallback（`fatfish/roam/summary.py`）；`leave_trip(summary=...)` 已预留 LLM JSON 总结的入口，P1 再接。

## 20. 相关

- [ADR-0013 单次外访（已被本文取代）](0013-session-visit.md)
- [ADR-0007 单轮计划 + 异步续写](0007-agent-loop.md)
- [ADR-0008 上线补回](0008-catchup.md)
- [ADR-0009 继续对话策略](0009-continue-policy.md)
- [运行时分支](../branches/runtime.md) · [平台分支](../branches/platforms.md) · [安全分支](../branches/security.md)
