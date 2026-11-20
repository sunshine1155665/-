---
title: ADR-0003 记忆改为事件-细节分层与多级标签
type: adr
status: accepted
tags: [adr, memory, tags, entities, rag, recall, schema, research, details, dream]
updated: 2026-11-01
---

# ADR-0003：记忆改为事件-细节分层与多级标签

> **实现进度（2026-10-24）**：schema v5 已落地 `event_details` / `detail_fts` / `archive_runs`；日归档已抽取 99 条带原话的 detail，召回改为「先细节、后记忆、按需回原文」。`memory_nodes` 节点树、月/年做梦仍是未做部分。
>
> **实现进度（2026-10-24，二）**：schema v6 实体/标签五表（`tags` / `tag_links` / `entities` / `entity_mentions` / `entity_edges`）+ 称呼归一抽取/回填 + 中文时间窗解析与召回预过滤已落地（规则版，731 测试全绿）。未做：`memory_nodes` 节点树、LLM 细节抽取、月/年做梦（按 §4.6 等数据）；真实库 backfill 待人工执行。

## 背景

用户反馈：问「几个月前那件事」时，她只能确认事件存在，说不出人物、地点、原话、情绪。

2026-10-24 夜间归档首跑后的实测证据（研发期巡检脚本，未随发布版分发）：

- 5 个 episode、17 条 `memory_items`；
- daily memory 的 summary 只有 20–47 字；
- `tags_json` 只有 `["archive","2026-10-20"]` 这类扁平值，`entities_json` **0/17 全空**；
- 4 个 session 各生成一条 gist 为「2026-10-20 的日常」的记忆，无法区分；
- 测试 session（`qq:private:verify-*`、`web:devcheck`）被写进真实记忆；
- 召回实测只返回摘要/gist，没有后续对话、原话、人物或情绪。

根因不是向量检索不准，而是**归档阶段把细节压掉了**：episode 摘要 + 一条 daily memory 就是全部可召回内容。换更强的 embedding 也检索不出不存在的东西。

## 决策

采用四层记忆模型：

```
episode（天 / session）
  └─ event（一件有边界的事）
       └─ detail（一句一事实：人、地、物、原话、情绪、变化）
            └─ evidence（turn_id + 逐字引用 + 字符区间）
```

`memory_items` 从「唯一真相」降级为**语义指针 / 长期画像**；细节放在 `event_details`，原话以 `conversation_turns` 为准。

### 1. 标签与实体（防爆炸）

```sql
CREATE TABLE tags (
  id INTEGER PRIMARY KEY,
  namespace TEXT NOT NULL CHECK(namespace IN ('domain','relation','emotion','time','topic')),
  name TEXT NOT NULL,
  parent_id INTEGER REFERENCES tags(id),
  aliases_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'candidate',      -- candidate|active|merged
  usage_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  UNIQUE(namespace, name)
);

CREATE TABLE tag_links (
  tag_id INTEGER NOT NULL REFERENCES tags(id),
  target_type TEXT NOT NULL,                     -- episode|event|detail|memory
  target_id TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL DEFAULT 'extract',
  created_at REAL NOT NULL,
  PRIMARY KEY (tag_id, target_type, target_id)
);

CREATE TABLE entities (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                            -- person|place|org|object|pet|topic
  canonical_name TEXT NOT NULL,
  aliases_json TEXT NOT NULL DEFAULT '[]',
  attrs_json TEXT NOT NULL DEFAULT '{}',
  first_seen_at REAL, last_seen_at REAL,
  merged_into TEXT REFERENCES entities(id),
  created_at REAL NOT NULL
);

CREATE TABLE entity_mentions (
  id INTEGER PRIMARY KEY,
  entity_id TEXT NOT NULL REFERENCES entities(id),
  source_type TEXT NOT NULL, source_id TEXT NOT NULL,
  surface TEXT NOT NULL,                         -- 原文里的称呼
  turn_id INTEGER REFERENCES conversation_turns(id),
  char_start INTEGER, char_end INTEGER,
  confidence REAL NOT NULL DEFAULT 0.8,
  created_at REAL NOT NULL
);

CREATE TABLE entity_edges (
  id INTEGER PRIMARY KEY,
  src_id TEXT NOT NULL REFERENCES entities(id),
  dst_id TEXT NOT NULL REFERENCES entities(id),
  relation TEXT NOT NULL,
  valid_from REAL, valid_to REAL,
  confidence REAL NOT NULL DEFAULT 0.7,
  evidence_detail_id TEXT,
  created_at REAL NOT NULL
);
```

### 2. 细节层与证据链

```sql
CREATE TABLE event_details (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES conversation_events(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL CHECK(kind IN ('fact','quote','emotion','change','decision','plan','setting')),
  subject TEXT NOT NULL DEFAULT '',
  predicate TEXT NOT NULL DEFAULT '',
  object TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,                            -- 一句一事实，可独立阅读
  speaker TEXT,
  quote_turn_id INTEGER REFERENCES conversation_turns(id),
  quote_text TEXT,                               -- 逐字摘录，禁止改写
  quote_hash TEXT,
  occurred_at REAL,
  time_granularity TEXT, time_confidence REAL,
  valid_from REAL, valid_to REAL,                -- 偏好/住址/承诺会过期
  emotion TEXT, emotion_intensity REAL,
  importance REAL NOT NULL DEFAULT 0.4,
  confidence REAL NOT NULL DEFAULT 0.7,
  source TEXT NOT NULL DEFAULT 'nightly',
  detail_hash TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX ux_detail_hash ON event_details(detail_hash);
CREATE INDEX idx_detail_event ON event_details(event_id, seq);
CREATE INDEX idx_detail_time ON event_details(occurred_at);

CREATE VIRTUAL TABLE detail_fts USING fts5(
  detail_id UNINDEXED, text_seg, quote_text, tokenize='unicode61'
);

CREATE TABLE evidence_refs (
  id INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL,                     -- memory|detail|event|episode
  target_id TEXT NOT NULL,
  turn_id INTEGER NOT NULL REFERENCES conversation_turns(id),
  quote TEXT NOT NULL,
  char_start INTEGER, char_end INTEGER,
  content_hash TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(target_type, target_id, turn_id, content_hash)
);

CREATE TABLE archive_runs (
  id TEXT PRIMARY KEY,                           -- date:session:event_key
  event_id TEXT, content_hash TEXT NOT NULL,
  detail_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
```

`memory_items` 新增：`detail_id`（指向最重要的一条 detail）、`salience`（排序用，不删除）。

### 3. 写入（nightly）

1. **测试 session 过滤**：`verify-*`、`devcheck`、`test-*`、`live-tts25*` 不归档。
2. **event 粒度 + 幂等**：沿用现有 `ep:<date>:<session>` upsert 与 `source_id` 去重；新增 `archive_runs` 记录 `date:session:event_key`，重跑跳过；turn 集合 Jaccard>0.6 视为同一 event。
3. **确定性预抽**（无 LLM）：引号句、第一人称情绪词、时间词、人名/地名候选、问答对，保留 `turn_id + char_start/char_end`。
4. **单 event 一次结构化抽取**：严格 JSON schema，返回 `details[]`；每条必须带 `turn_id`、`kind`、10–120 字 `text`，可选 `quote`。
   - `quote` 必须是该 turn 的**精确子串**，服务端 `in` 校验，不匹配即丢弃。
   - `tags` 只能从受控词表选；`entities` 走实体消歧。
5. **实体消歧**：别名精确匹配 → 称呼归一（「主人哥哥」→ owner）→ 同 kind 且 cosine>0.88 进合并候选；不确定先标 `provisional`，后续 `merged_into`，**不删 mention**。
6. **标签治理**：新标签先 `candidate`，累计 ≥2 次才 `active`；领域标签活跃数控制在 ~30；同义词写 `aliases_json`。
7. **细节去重**：`detail_hash = sha1(event_id|kind|规范化 text|quote_turn_id)`，唯一索引。
8. **现有数据回填**：对 62 轮/5 episode 跑一次 event→details；17 条 memory 通过 `detail_id` 挂接；不改历史摘要。

### 4. 召回（由粗到细）

| 步 | 动作 | 方式 |
|---|---|---|
| 0 | 解析时间词/实体/领域 | 规则 + 可选小模型 JSON |
| 1 | 时间窗筛 episode | SQL：`end_at>=t0 AND start_at<=t1` |
| 2 | episode → event 排序 | 实体命中 + 时间接近 + importance，top 3 |
| 3 | event → **全部 details** | 外键按 `seq` 展开，禁止只取 summary |
| 4 | 细节精排 | `detail_fts MATCH` + detail 向量（先过滤后 ANN） |
| 5 | 回原话 | `quote_turn_id` join `conversation_turns` |
| 6 | 「后来呢」 | 1 跳 `entity_edges` / `memory_links` |

**融合用 RRF**（`1/(60+rank)` 相加），不要直接把 cosine、BM25、importance 加权求和——量纲不可比。

**硬约束**：召回结果组装成 `EvidencePack{event, details[], quotes[]}`；`details` 为空时禁止确定性回答，只能答「只记得大概」或反问。摘要只做标题/索引，不进事实段。

### 5. 层级、显著性、衰减

- T0 `conversation_turns`：不可变，永不删。
- T1 `event_details`：永久，不参与二次摘要。
- T2 event 摘要 / T3 episode 摘要：可再生，365 天无访问可 `archived`。
- T4 `memory_items(type='semantic'/'profile')`：人格与长期偏好。
- `salience = importance · exp(−Δdays/(30·stability)) + 0.3·log(1+access_count)`，只影响排序。
- 提升：detail 被 ≥3 次命中，或同 `subject+predicate` 在 ≥2 个 event 重复，或 importance≥0.7 → 提升为 T4。
- 矛盾：新 fact 写入时给旧 fact 设 `valid_to=now, superseded_by=新 id`；**绝不 UPDATE 原话**。

### 6. 评估

| 指标 | 定义 | 目标 |
|---|---|---|
| detail_hit@5 | 答案细节是否在 top5 | ≥80% |
| quote_hit@5 | 是否带回原话 | ≥60% |
| time_precision | 时间过滤后无关日期比例 | ≥90% |
| entity_coverage | 有实体标注的 memory 占比 | ≥70% |
| 人工抽检 | 20 条「几个月前」问题，是否答出人/地/事/情绪 | ≥15/20 |

## 4.5 分层节点与父/子路由（2026-10-24 补充）

> 用户提案：晚上“做梦”时总结当天对话，整理成结构化记忆分级入库；召回时，若上级相似度最高就召回该上级对应的数据，若下级最高就直接用下级。
>
> **结论：可行，但要把“召回上级”实现为“用上级定位后下钻”，而不是用上级摘要回答。**

### 4.5.1 节点树

所有可检索对象统一成一种节点，用 `level` 分级、`parent_id` 组树：

| level | 内容 | 可变性 | 作用 |
|---|---|---|---|
| 0 | 原始 turn | 不可变 | 证据，永不删 |
| 1 | detail（一句一事实 + 原话） | 永久 | 精确回答 |
| 2 | event（一件事） | 可再生 | 定位一件事 |
| 3 | day / session（一天） | 可再生 | 定位“那几天” |
| 4 | semantic / profile（长期偏好、人物设定） | 稳定 | 人格与长期关系 |

```sql
CREATE TABLE memory_nodes (
  id TEXT PRIMARY KEY,
  level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 4),
  kind TEXT NOT NULL,                 -- turn|detail|event|day|semantic
  parent_id TEXT REFERENCES memory_nodes(id),
  root_id TEXT,                       -- 顶层 day 节点，便于整棵树操作
  title TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,                 -- 检索用文本（detail 用事实，event/day 用摘要）
  quote TEXT,                         -- 仅 level 1：逐字原话
  turn_id INTEGER REFERENCES conversation_turns(id),
  time_start REAL, time_end REAL,
  importance REAL NOT NULL DEFAULT 0.4,
  salience REAL NOT NULL DEFAULT 0,
  confidence REAL NOT NULL DEFAULT 0.7,
  status TEXT NOT NULL DEFAULT 'active',   -- active|archived|superseded
  source_type TEXT, source_id TEXT,        -- 指回 episodes / events / details
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX idx_node_parent ON memory_nodes(parent_id);
CREATE INDEX idx_node_level_time ON memory_nodes(level, time_start, time_end);

-- 向量单独存，换模型时只重建这张表（贴纸库吃过宽度不一致的亏）
CREATE TABLE memory_embeddings (
  node_id TEXT NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (node_id, model)
);
```

层级用 `parent_id` + SQLite 递归 CTE 即可（深度 ≤5），不必上 closure table。

### 4.5.2 “做梦”（夜间巩固）

每天固定时间（建议 03:00，安静时段）跑一次，按 `date + session` 幂等：

1. **重建事件**：turn → event（已有 `conversation_events`），跳过测试 session。
2. **抽细节**：每个 event 一次结构化抽取，产出 level 1 节点；`quote` 必须是原 turn 的精确子串，不匹配即丢弃。
3. **建事件节点**：level 2，text 用“谁 + 做了什么 + 结果”，不写空泛形容；挂到当天 level 3。
4. **建日节点**：level 3，按 session 一条 + 跨 session 一条日汇总（解决 4 条同名「日常」）。
5. **巩固长期记忆**：重复出现 ≥2 次的事实/偏好、importance≥0.7 的细节 → level 4；矛盾时旧节点 `valid_to=now`、`superseded_by=新 id`，**不改原话**。
6. **算显著性**：`salience = importance·exp(−Δdays/(30·stability)) + 0.3·log(1+access_count)`，只影响排序，不删除。
7. **向量化**：level 1/2/3 都写 embedding；把“可检索文本”写进 `text`，不要把原话和摘要混在一起。
8. **做梦报告**：新增/合并/废弃了多少条、发现哪些矛盾。报告是给人看的，**不进入事实召回**。

关键约束：做梦只能“整理”，不能“编造”。所有 level 1/2 的断言都必须有 `turn_id` 证据；没有证据的叙述只能放进报告。

### 4.5.3 召回路由

不要把所有层级的 cosine 放在一起取 argmax——不同长度的文本，余弦分布不可比。正确做法是**分层召回 + 路由**：

```text
1. 解析 query：时间词 → [t0,t1]；实体/称呼；是否“具体事实”查询
2. 分层硬过滤 + 召回：
     L3/L2: 时间窗 + 实体命中，各取 top 5
     L1   : 时间窗 + 实体 + FTS，取 top 12
3. 层内排序（向量 + FTS 用 RRF 融合），得到每层 rank
4. 路由：
     若 query 是“具体事实/原话”型，且 L1 最佳明显胜出（margin ≥ δ）
         → 叶子优先：直接用该 detail，附 1 行父级上下文
     否则若 L2/L3 最佳胜出
         → 父级定位：取该节点，下钻其子节点，重排后取 top 6 details
     否则（分数接近）
         → 默认下钻：先父级定位，再子级重排（宁可多给上下文）
5. 默认只返回 details + 1 行路径；quote 不默认加载
6. 按需展开原文（expand），触发条件见下表
7. 组装 EvidencePack{path, details[], quotes[]}；details 为空则只能答“只记得大概”
```

**先细节、后原文（符合人的回忆过程）**：

人的回忆通常是先想起“发生了什么”，被追问才重构原话。所以默认召回单位是 **detail**，不是原始 turn：

| 情况 | 返回 |
|---|---|
| 普通提问 | detail 文本 + 时间/人物/事件路径；不回原话 |
| 用户明确要细节（“原话怎么说”“具体点”“后来呢”） | 追加 `quote` + 原始 turn |
| 同一 detail 在 10 轮内第二次命中 | 自动升级为原文（渐进披露） |
| detail 置信度低 / 与长期记忆冲突 / 用户质疑 | 回原文核对 |
| 只有父级、没有 detail | 只答“只记得大概”，或反问 |

这样既省上下文，又避免一上来就灌原始对话；原文只在需要时通过 `quote_turn_id` 拉取。

**为什么父级命中要下钻**：父级是索引，不是事实。直接用摘要回答就是现在的毛病——只能说“那天聊了 8 件事”，说不出聊了什么。

**为什么叶子命中也要带父级**：叶子是“一句事实”，缺时间/地点/人物关系。回 1 行路径即可：`2026-07-14 > 赶稿那晚 > 「今天赶稿好累」`。

**跨层比较的替代方案**：

- 层内用 RRF（`Σ 1/(60+rank)`），不要跨层直接加 cosine；
- 需要跨层决策时，用“层内第一名 + 层先验 + 查询类型”做路由，而不是比较原始分数；
- 父级摘要短而泛，容易匹配一切：给 level 2/3 加长度惩罚或固定先验，避免它压过精确叶子。

**展开预算**：

- 父级最多下钻 8 条 details，按 query 相关度 + salience 重排；
- 叶子模式附父级摘要 1 行 + 相邻 detail 最多 2 条；
- 总注入 ≤ 1200 tokens，超了先丢低分 detail，不丢原话。

### 4.5.4 例子

query：「几个月前她赶稿很累，后来我们聊了什么？」（合成示例）

```text
时间解析 → 2026-07 前后
L3 命中：2026-07-14 的日常（0.52）
L2 命中：赶稿那晚（0.61）   ← 父级胜出
L1 命中：「今天赶稿好累…」（0.58）
路由：父级胜出 → 下钻“赶稿那晚”
  details: 赶稿 / 累 / 不想动 / 主人安慰 / 聊到晚饭
  quotes : 「今天赶稿好累，一点都不想动了……」
回答：时间 + 事件 + 3 条细节 + 1 句原话
```

容量与检索成本实测（D 盘 232.9GB 可用、5000 条 768 维时单次搜索 606ms；时间预过滤后 4ms）见 [记忆分支 · 容量与性能](../branches/memory.md#容量与性能实测2026-10-24)。

### 4.6 多级做梦：日 / 月 / 年

用户提案：每天做梦一次，每月再做梦一次，每年再做梦一次。

**结论：可行，而且应该做，但必须遵守“低层存细节、高层只做索引”的规则。** 月/年节点不能取代日节点，只能指向它们。

| 级别 | 节点 | 输入 | 输出 | 频率 |
|---|---|---|---|---|
| 3 | day | 当天的 turns / events / details | 当天时间线 + 3–8 个事件 + 未闭环 | 每天 03:00 |
| 4 | month | 该月所有 day 节点 + 事件/事实 | 主题、重复事件、变化、关系弧、未闭环 | 每月 1 日 03:30 |
| 5 | year | 该年 12 个 month 节点 | 里程碑、长期变化、关系阶段、年度关键词 | 每年 1 月 1 日 04:00 |
| 6 | semantic | 跨周期的重复事实/偏好 | 人物设定、长期偏好、承诺 | 随日/月提升 |

**实现约束：**

- `period_key` 唯一（`2026-10-24` / `2026-10` / `2026`），重跑幂等；输入内容哈希不变则跳过。
- 高层节点的每一条断言都必须带 `child_node_ids`；没有子节点证据就不写。
- 月/年不是“把日摘要再摘要一遍”：要从 day 节点下的事件与事实聚合，必要时回到 detail 重排；摘要只做导航。
- 跨月事件按开始时间归属，但保留跨月链接；不要复制成两条事实。
- 数据不足时跳过（例如当月 < 5 个事件），避免生成空洞的“这个月很平静”。

**输出结构（结构化压缩事件 + 事实，不是散文）：**

```json
{
  "period": "2026-10",
  "level": 4,
  "themes": [{"label": "工作压力", "weight": 0.7, "node_ids": ["..."]}],
  "events": [{"title": "赶稿那晚", "date": "2026-10-23",
              "summary": "主人赶稿到很晚，聊到晚饭和休息。",
              "detail_ids": ["..."], "quote_ids": ["..."]}],
  "facts": [{"subject": "主人", "predicate": "最近", "object": "工作压力大",
             "valid_from": 1759..., "confidence": 0.7, "evidence": ["..."]}],
  "changes": [{"from": "常熬夜", "to": "开始早睡", "since": "2026-10-20",
               "evidence": ["..."]}],
  "open_loops": [{"text": "等主人汇报面试结果", "due_at": null}],
  "stats": {"days": 8, "events": 12, "details": 37}
}
```

年节点同样结构，`themes` 换成“关系阶段 / 年度关键词 / 重要里程碑”。**散文版“梦话”可以生成给人看，但不进召回、不作为事实。**

**效果评估：**

| 维度 | 日 | 月 | 年 |
|---|---|---|---|
| 解决什么 | 当天细节 | “这个月怎么样” | “我们这一年” |
| 召回收益 | 高 | 中高 | 中 |
| 失真风险 | 低 | 中 | 高 |
| LLM 成本 | 每天 1 次 | 每月 1 次 | 每年 1 次 |

最大风险是 **summary-of-summary 漂移**：月节点如果只读日摘要，会像传话游戏一样丢细节。所以月/年必须能下钻到 event/detail，且回答“具体发生了什么”时永远回低层。

**建议顺序：** 先把日做梦 + detail 层做扎实，积累 2–4 周数据后再开月做梦；有 6–12 个月数据再开年做梦。不要用测试数据跑月/年，否则会生成看似合理但无证据的长期记忆。

## 最小可行改造（只改 3 处）

1. **加 `event_details` + `memory_nodes` + `detail_fts` + 召回强制展开**（含 `quote_turn_id` 回链）。这是「细节丢失」的正解，收益最大；节点树同时解决父/子路由。
2. **加查询时间解析 + 分层召回路由**：先按时间窗硬过滤，再按「父级定位 → 下钻 / 叶子直取」组装 EvidencePack。直接解决「几个月前那件事」这类最高频失败查询。
3. **加 `entities`/`entity_mentions` + 标签 namespace 归一**。解决 `entities_json` 全空、实体消歧、跨事件反查与去重。

三处都不引入新服务、不改动历史 62 轮数据，可在一次迁移内完成。

## 备选方案（GitHub 调研，2026-10-24）

| 项目 | 数据模型 | SQLite | 结论 |
|---|---|---|---|
| [Mem0](https://github.com/mem0ai/mem0) 67.7k | 抽取事实 + metadata + 向量 | 需外置向量库 | 排序强，但会改写原文；不整体引入 |
| [Letta/MemGPT](https://github.com/letta-ai/letta) 25.2k | core blocks / recall / archival | 需 Postgres | 分层思想可借 |
| [Graphiti (Zep)](https://github.com/getzep/graphiti) 32.2k | episode→entity→edge，双时间 | 需 Neo4j 等 | **双时间 + 原文回链**最值得借 |
| [Cognee](https://github.com/topoteretes/cognee) 32.7k | DataPoint 图 + 溯源 | 默认 SQLite | 架构可参考，依赖偏重 |
| [Memobase](https://github.com/memodb-io/memobase) 2.9k | profile + event_gist + chat blob | 需 Postgres/Redis | **event→gist→原文**骨架可借 |
| [A-MEM](https://github.com/agiresearch/A-mem) 1.2k | Zettelkasten 笔记 + 链接 | ChromaDB | 会演化重写旧笔记，不适合原话 |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) 4.3k | Entity→Observation→Relation | Markdown+SQLite | 证明「原子事实」可行 |
| [Memori](https://github.com/MemoriLabs/Memori) 17.1k | SQL 原生 messages + facts | SQLite 一等公民 | 工程形态最接近，可读源码 |
| [HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG) 4.1k | OpenIE 图 + PageRank | 需图+向量 | 多跳召回可借，成本高 |
| [GraphRAG](https://github.com/microsoft/graphrag) 36.4k | text_unit→entity→community report | 否 | 多分辨率摘要可借，索引成本过高 |
| [LlamaIndex](https://github.com/run-llama/llama_index) 52.6k | Memory blocks | 可 SQLite | 通用框架，无现成细节层 |
| [Memobase/Supermemory/Zep 云] | 托管 | 否 | 不符合本地优先 |

> star 数为调研时 GitHub API 实测；功能以官方 README 为准。

**调研结论**：没有能原样嵌入现有 schema 的方案。最值得借的三个骨架是 Graphiti（双时间 + 原文回链）、Memobase（event→gist→原文）、GraphRAG（多分辨率但保留 text_unit）。综合成上面的 SQLite 原生方案。

## 公认的坑

1. **过度压缩**：summary-of-summary 不可逆丢细节。摘要只存指针，T0/T1 不可变。
2. **标签爆炸**：自由标签几个月到几千个 → 检索崩塌。封闭词表 + candidate + 合并别名；实体不进标签表。
3. **时间漂移**：把 `event_time` 写成归档时间，或「上周」按 now 解析。以 `turn.created_at` 为锚，写入时绝对化。
4. **向量与标签冲突**：ANN 先召回「语义很像但时间不对」的事。必须先硬过滤再向量，融合用 RRF。
5. **错误合并**：仅靠 embedding 阈值去重会把「两次生日」合成一次。事件身份靠 turn 区间 + 日期 + 实体集合。

## 后果

**正面**：能回答细节；标签可过滤；原话可回溯；跨 session 可汇总；时间窗查询准确。

**负面**：schema 与 nightly 改动大；标签/实体需要治理；细节抽取初期准确率有限，需要人工抽检。

## 迁移计划

1. `schema v5` 建表（幂等）。
2. 回填现有 5 episode / 17 memory 的 details。
3. 双写一周，对比 detail_hit@5 / quote_hit@5。
4. 切换默认召回；保留旧向量检索作为兜底。

## 2026-10-26 补记：召回准确性与缺失话术（不改决策）

§4.5.3 的「先细节、后记忆」和 §6 的评估闭环在实测里暴露了两个落地缺口，本轮补齐，**不改表结构、不改分层决策**：

1. **同事件上下文**：查询命中「鲸鲸今年多大了」时，答案「我记不清具体几岁」是同一 event 的另一条 detail，词面完全不相交。`expand_event_context` 在已命中 detail 后带出同 event 最近 2 条兄弟（全局上限 4，标记 `context=True`），提示词用 `[同事件补充]` 明确区分「不是独立命中」。
2. **内容 token 守卫**：coverage 会被称呼和虚词 bigram 稀释——「主人哥哥答应姐姐看空间」曾把「主人哥哥喜欢喝冰美式」也召回（主人/人哥/哥哥 三个名字 bigram 就够门槛）。现在称呼（主人/哥哥/姐姐/鲸鲸…）与虚词/代词 bigram（姐是/是不/不是/她的…）不计入内容命中；查询里有内容 token 时至少命中一个，`min_score=0` 仍是显式关门模式。
3. **记忆问题专用提示**：`wants_memory_check` 识别「记得/上次/那次/你说过/答应…」；有证据时要求具体到人/时间/事/原话（禁止「好像/大概/记不清」），无证据时用撒娇/卖萌话术（「主人哥哥你耍我！本鲸鱼娘根本就不知道那东西嘛」），跨人私聊诚实说「不方便转述」而不是谎称没有。`format_recall(memory_first=True)` 与 `[记忆核查·直接依据]` 把事实摘要放到细节噪声和长人格之前。
4. **隐私兜底**：如果模型仍用「没这条」否认一条来自别人 private session 的记忆，`runtime.memory_privacy_reply` 改成「确实提过，但不方便转述」，不撒谎也不泄露。
5. **评估闭环（§6 的第一步）**：研发期评测脚本（未随发布版分发）支持种子事实演示库、只读副本库与仅召回三种模式；已知题看答案关键词（忽略空白/标点），观点题要内容事实 + 第一人称态度，未知题看俏皮措辞且排除系统腔。2026-10-26 合成数据三组（基础、B 站、生活化）全部通过，演示库 10/10。
6. **跨 session 自主活动**：夜班/活动轮的 assistant 原话在独立 session，`_recent_activity_evidence` 在问「昨晚/凌晨在干什么」时读最近 36 小时的活动记录并按时间线注入；块放在用户消息前，避免当前生活状态顶替回忆。
7. **B 站本地回顾**：`wants_bili_review` 对「上次在 B 站看的视频/怎么看/哪个最有意思」读取本地标题/UP（不联网），并注入「先报标题、基于简介/评论说态度」的规则；实时浏览仍走 `wants_bili_browse`。

仍留：真实库实体回填（T-0009）未执行、细节层无向量排序、月/年节点未开。

8. **测试数据读取隔离（2026-10-26）**：真实库里仍有夜测/验证/演示行，直接删除需要人工确认。读取路径先隔离：`fatfish/memory/sessions.py` 统一识别 `verify-`/`devcheck`/`test-`/`night-`/`demo-`/`smoke-`/`live-tts25` session 与 `BV_TEST`/`BV_DEMO`；`search_details`、`memory.search`、`search_rag`、短摘要/元目录和 `_bili_stock_evidence` 全部跳过。夜班活动仍从 `conversation_turns` 读，不受影响。存量清理仍列为人工待办。

9. **同日多 session 日汇总（2026-10-26）**：夜间归档在逐 session 的 `episode` 之外再写一条 `memory_items(source_type='day_summary', source_id=YYYY-MM-DD)`，按「主人哥哥 → 姐姐 → 群聊 → 她的活动（合并一行）」排序，排除测试/夜测 session；同一天重跑原地刷新，不新增行。问题「昨天/今天发生了什么」由 `_day_summary_evidence` 按 `parse_time_window` 取对应日期的汇总，提示词用 `[日汇总（跨会话）]`。逐 session 台账保持不变。

10. **群聊召回隔离（2026-10-26，当日被第 11 条推翻）**：审计发现群聊回合的 `[本轮检索证据]` 会带出主人私聊 memory_items。修复在 runtime 的 group 分支：不召回 `memory_items`，`event_details` 只查当前群 session（`search_details(session_id=...)`），RAG 额外排除 `qzone_friend`；群聊自己的 transcript/timeline 不变；群聊也不启用 `[记忆核查]`（其示例使用主人称呼）。反向同样隔离：私聊/好友位的 recall、details、短期摘要、元目录排除群聊记忆，日汇总群聊只记轮数不写内容。

11. **QQ 记忆共享 + B 站私下分享（2026-10-26，boss 决定）**：QQ 内**不做**群聊/私聊记忆隔离——主人自己控制她加入哪些群，群聊与私聊共享 `memory_items`、`event_details`、RAG、短期摘要和元目录；群聊也照常启用 `[记忆核查]`，并同样称呼主人/主人哥哥。测试/夜测/演示数据的读取过滤（第 8 条）继续保留，属于数据卫生，不是群聊隔离。B 站内容不做记忆隔离；采集器本来就是只读（不评论/不点赞/不私信），观后感只在私聊讲给主人，群聊不展开 B 站链接或讨论。

## 2026-11-01 补记：entity_edges 写入与 1 跳关联补充（不改决策）

§4 召回表第 6 行「后来呢 → 1 跳 entity_edges」落地，思路取自 ActionRAG
（arXiv:2610.32752）的「初始相似度分可以被局部图结构修正」，取其最便宜版本。
不改表结构（`entity_edges` v6 已建、`entity_mentions` 复用）、不改分层决策：

1. **写侧**（`fatfish/memory/edges.py`）：nightly 与 backfill 对每条 detail，
   把文本/原话里共同出现的实体两两写 `entity_edges(relation='co_mention',
   evidence_detail_id=detail)`，并写 detail 级 `entity_mentions`（此前只有
   memory/event/episode 三级）。边是证据不是结论：同一对实体在几个 detail
   里同现就留几行，幂等键 `(src, dst, relation, evidence_detail_id)`，方向按
   实体 id 排序。边置信度 = 两端 mention 置信较小值（群聊同现 0.4）。
2. **读侧**：直接命中的 detail 的实体沿 1 跳边扩散，邻居实体的 detail 以
   `via_graph=True` + `graph_via="锚点 ↔ 邻居"` 带出，渲染为
   `[关联补充]`（不是独立命中）。锚点实体优先读 detail 级 mention，缺失时按
   文本兜底识别（真实库回填前也能用）。实体合并后旧 id 的边与 mention 经
   家族折叠仍可达，`graph_via` 显示存活卡名。
3. **收口**：预算（总 3 / 每邻居 2）、最低置信 0.5（群聊同现的 0.4 默认进不来，
   宁漏记不错归）、时间窗与召回共用、测试 session 一步不进、T-0073 viewer 闸门
   在补充之后统一过滤。配置：`memory.graph_expand{,_max,_per_neighbor,_min_confidence}`。
4. **台账**：runtime `memory.detail_graph` 计数图补充条数；CLI `memory edges`
   查看边，`memory backfill` 现在同时回填边。
5. **测试**：`tests/test_memory_edges.py` 34 例（匹配守卫 / 幂等 / 家族折叠 /
   预算排序 / 时间窗 / 隔离 / 闸门 / 开关）。

仍留：detail 层无向量排序、LLM 结构化抽取（subject/predicate/object 字段仍空）、
月/年做梦、真实库实体与边回填待人工执行。
