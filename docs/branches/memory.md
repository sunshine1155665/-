---
title: 记忆系统
type: branch
status: in-progress
tags: [memory, sqlite, rag, tags, entities, people, nightly, recall]
updated: 2026-11-18
---

# 记忆系统

> 上级：[系统架构](../overview/architecture.md) · 相关：[运行时](runtime.md) · [运维](ops.md) · 决策：[ADR-0003](../adr/0003-memory-hierarchy.md)

## 结论

写入链路已通，夜间归档**已首跑成功**（2026-10-24：5 个 episode / 17 条 memory_items），并已补上 **detail 层**（99 条带原话的原子细节）。召回改为「时间窗+实体预过滤 → 先细节、后记忆、按需回原文」。schema v6 实体/标签表与时间/实体预过滤已落地（规则版），**真实库回填待人工执行**（`python -m fatfish memory backfill`，实体覆盖目前 0/17）；月/年做梦仍按 ADR-0003 §4.6 等真实数据积累。

**2026-10-26 召回准确性补记**：同事件上下文扩展、称呼/虚词 token 守卫、记忆问题专用提示（有证据说具体、无证据撒娇、跨人私聊诚实拒绝）与可复跑实测脚本已落地；真实库 8 题（4 已知 / 4 未知 / 1 中性）全部通过。见下文「召回准确性」。未改 schema，实体回填仍待人工。

## 分层

| 层 | 表 | 作用 |
|---|---|---|
| 原始 | `conversation_turns` | 每轮原话（62 条） |
| 事件 | `conversation_events` | 按 45 分钟 / 话题漂移聚合（12 条） |
| **细节** | **`event_details` + `detail_fts`** | **一句一事实 + 原话 + turn 回链（99 条）** |
| 档案 | `episodes` | 每 session 每天一份时间线摘要（5 条） |
| 记忆 | `memory_items` | 事实/情节条目，带 tags/entities/重要性/时间（17 条） |
| 检索 | `rag_chunks` + FTS5 + 向量 | 检索用（目前仅 2 条 B 站内容） |

## 过段时间线（2026-10-26 起）

`episodes` 以前只写不读：`nightly` 每晚归档，`context.py` 一个字都不取，所以窗口（20~39 轮原话）之外的昨天和前天对她等于不存在。

现在 `context.py::_past_timeline_block` 会把掉出窗口的整天归档拼成一段 `[过段时间线]`，放在 `stable_prefix` 之后、历史之前（先概览、后细节），并和它们一起落在缓存区：

```
[过段时间线]
10-22 · 那天和主人哥哥聊了 8 件事
  13:57 和主人哥哥聊了几句近况
  21:14 互道晚安，约好明天再聊
（这些是你自己事后记的摘要，不是原话；要细节就用 search_memory 翻。）
```

- 取数：`MemoryService.recent_episodes(session_id, before=窗口最老时间戳, limit=3)`。`before` 保证和 `[最近对话]` 不重复；窗口为空时回落到本地零点，今日交给 `[今日时间线]`。
- 结构来自 `summary` 里已有的 `[HH:MM] 标题` 头（跳过「零散片段」和纯表情标题），口吻来自 `summary_voice`，取冒号前的总述并把「今天」换成「那天」——日记是在当天写的，这块是几天后读的。
- 上限 600 字，超了从最早的整块砍起，最近的一天永远保留。
- 变化频率 = 每晚一次（`nightly` 改写某天的归档），所以整段在缓存区里能稳定命中；代价是归档更新后的第一次请求会重置一次缓存。
- 末尾那句「不是原话」是必要的：摘要是给她自己读的索引，直接当原话引用会编出一句她没说过的话。


## 召回

`RecallGate`：相似度 0.6 + KEV 0.4（KEV 可选），过门后进提示词。`memory/service.py` 提供 FTS/向量混合检索。

召回顺序（2026-10-24 起，预过滤版）：

0. 时间窗 + 实体预过滤（`parse_time_window` + `entities_for_query`，详见下文「时间/实体预过滤」）；
1. `event_details` 细节召回（gram-LIKE，最多 6 条；`detail_fts` MATCH 分支已弃用）；
2. 同事件上下文（`expand_event_context`，标记 `context=True`）与 1 跳实体边补充（`expand_entity_neighbors`，标记 `via_graph=True`，见下文「实体边与关联补充」）；
3. `memory_items` 摘要召回（作为索引与背景）；
4. 用户明确要原话时，`recall(..., include_quotes=True)` 才带 `quote_text` 与原始 turn。

### 遗忘曲线与精确命中（2026-11-18）

排序加入「只影响排序、不删除」的时间衰减：

- `recency = 2^(-days/half_life)`；half_life 复用 `_half_life_days()`（类型 + importance + access_count），不再固定 30 天；
- 查询显式带时间窗（`parse_time_window` 解析出 `time_start/time_end`）时不再叠加 recency 惩罚，避免时间查询被二次排序；
- `status='archived'` 不再被 `search()` 硬过滤：归档行仍进候选，非精确命中乘 `memory.archived_score_multiplier=0.8` 惩罚；
- **精确命中保护**：`sim >= 0.75` 或关键词精确命中时，recency 至少为 `0.6`，不会被「新但只沾边」的记忆挤掉；
- **压缩保护**：`access_count >= 3` 或最近 60 天访问过的记忆不再归档（`_is_protected`），真正被召回的旧记忆不会因为「老」被丢掉；
- 配置在 `[memory]`：`recency_weight`、`archived_score_multiplier`、`exact_match_similarity_floor`、`exact_match_recency_floor`、`archive_min_access_count`、`archive_recent_access_days`。
- 测试：`tests/test_memory_time_weighting.py`。

主动分享的动态索引：`_record_proactive_delivery()` 在投递成功后调用 `memory.add_item(source_type='proactive_share', source_id='outbox:<id>')`，立即算 embedding 入库，不等 nightly；同一 `source_id` 自动去重。

CLI：

```bash
python -m fatfish memory details "鲸鲸"
python -m fatfish memory recall "鲸鲸今年多大了" --quotes
python -m fatfish memory entities            # 实体列表（带 mention 数）
python -m fatfish memory entities "姐姐"      # 查询文本会命中哪些实体
python -m fatfish memory edges               # 实体关系边（co_mention，带置信度与证据）
python -m fatfish memory backfill            # 对当前库幂等增量回填实体+标签+边（只新增不删除）
```

## 召回准确性（2026-10-26）

目标：**问到库里已有的记忆不含糊、不敷衍；问到没有的不编造，用撒娇/卖萌说不知道。** 本轮不改 schema，只改「取哪些证据、怎么排序、怎么说」。

| 改动 | 位置 | 作用 |
|---|---|---|
| 同事件上下文 | `details.expand_event_context` + `MemoryService.recall_details` | 命中提问句时把同一 event 的最近 2 条兄弟细节一起带出（全局最多 4 条），标记 `context=True`；解决「命中『鲸鲸今年多大了』却看不到『记不清具体几岁』」 |
| 称呼/虚词守卫 | `details._NAME_BIGRAMS` / `_FUNCTION_CHARS` / `_FUNCTION_BIGRAMS` | 只共享「主人/人哥/哥哥」或「姐是/是不/不是」不算内容命中；同一人 ≠ 同一事件。`min_score=0` 仍是显式关门模式 |
| 记忆问题识别 | `fatfish/memory/intent.py` | `wants_memory_check`（记得/上次/那次/你说过/答应…；提醒句「记得吃饭」不误判）；`wants_verbatim`（原话/怎么说的） |
| 证据排序 | `format_recall(memory_first=True)` | 记忆问题把 `[记忆召回]` 摘要放在 `[细节召回]` 前面；普通聊天仍细节优先 |
| 直接依据 | `ContextAssembler` `[记忆核查·直接依据]` | 把 top1-2 条 gist 原样放到规则前，避免长人格/议程把答案挤出注意力 |
| 提示规则 | `MEMORY_CHECK_RULES` | 有证据：具体到人/时间/事/原话，禁止「好像/大概/记不清」；无证据：撒娇说「主人哥哥你耍我！本鲸鱼娘根本就不知道那东西嘛」；跨人私聊：诚实说「不方便转述」，不许谎称没有 |
| 隐私兜底 | `runtime.memory_privacy_reply` | 记忆问题 + 否认措辞 + 命中别人的 private 记忆 → 改成「确实提过，但不方便转述」，避免「没这条」的假否认 |
| 跨 session 自主活动 | `runtime._recent_activity_evidence` + `wants_activity_review` | 问「昨晚/凌晨在干什么」时读夜班/活动轮的 assistant 原话，按时间线注入；当前生活状态不再顶替回忆 |
| B 站本地回顾 | `wants_bili_review` + `_bili_stock_evidence` + `BILI_REVIEW_RULES` | 问上次看了什么/怎么看时注入本地标题/UP 与回答规则，不联网 |
| 唱歌记忆 | sing 成功 → `memory_items(source_type='sing')` + `_recent_sing_evidence` | 成功后写「给…唱了《title》（emotion）」；问上次唱了什么注入最近 5 条 |
| 测试数据隔离 | `fatfish/memory/sessions.py` + `search_details` / `search` / `search_rag` / `_bili_stock_evidence` | 读取路径跳过 `verify-`/`devcheck`/`test-`/`night-`/`demo-`/`smoke-`/`live-tts25` 与 `BV_TEST`/`BV_DEMO`；存量不删，夜班活动仍从 conversation_turns 读 |
| 日汇总 | `nightly._persist_day_summary` + `runtime._day_summary_evidence` | 每天跨 session 汇总（owner/friend/group/activity），`source_type='day_summary'`、`source_id=YYYY-MM-DD`，幂等；问昨天/今天发生了什么先看 `[日汇总]` |
| QQ 记忆共享 | runtime 单一路径 + `context.can_see_memory` | 2026-10-26 boss 决定：群聊/私聊共享 `memory_items`、细节、短期摘要与元目录；群聊同样启用 `[记忆核查]` 并照常称呼主人。测试数据过滤仍按上一行执行 |
| B 站私聊分享 | `_persona_hint` + `share_with_owner` | B 站采集只读（不评论/不点赞/不私信）；观后感只在私聊讲给主人，群聊不展开链接或讨论 |
| 台账 | runtime result `memory.{check,details,detail_context,memory_hits,privacy_blocked}` | 每轮可审计：是否记忆问题、命中几条、带出几条上下文、是否被隐私兜底 |

### 评测脚本（研发期内部工具）

研发期用过一个零依赖的可复跑评测脚本（临时库 + 种子事实，不出网、不读真实库），
它属于内部研发工具，未随公开发布版分发。评测目标不变：

- 基础题：答出库里已有的事实细节，未知题不编造、用俏皮措辞说不知道；
- B 站/生活化题：走生产同一条 ingest 路径种合成数据，验证「问最/哪个先选一个再给理由，问看法要有自己的态度」；
- 证据行把标题/UP 放在最前，避免长上下文截断。

评分：已知题要求回答里出现至少一个「答案里才有」的关键词（不是问题里的词；匹配忽略空白/标点，所以「第 0 天」=「第0天」）；观点题还要求出现第一人称态度（我觉得/喜欢/离谱/带劲/哼…）；未知题要求俏皮/撒娇措辞且不出现「内部信息/数据库/记录里」系统腔。退出码 1 表示有失败。

**B 站/生活化题**：`--demo` 会用生产同一条 `BilibiliCollector.ingest_video` 路径种三条视频（标题/UP/简介/评论），问题覆盖「你上次在 B 站看的视频是什么」「某条视频评论区在说什么」「你怎么看」「最有意思的是哪个」。RAG 证据行会把 `《标题》 @UP` 放在最前，避免 180 字截断丢掉标题；`[记忆核查]` 另要求「问最/哪个先选一个再给理由，问看法要有自己的态度」。

**2026-10-26 实测结果**（合成数据的临时库 + 真实 LLM，`deepseek-v4.1-flash`；下列答对要点为合成示例，展示评测覆盖什么）：

- 基础 8 题全过：约定类题答出时间约定、表情包题答出日期与互动方式、年龄题答出「记不清」并带上原话、涉及他人隐私的题诚实说「不细讲」；未知 4 题全部俏皮。
- B 站生活化 7 题全过：答出「第 0 天」视频的数据、近期视频与联动、评论区的合成原话；两道观点题都选了具体视频并给出态度。
- 生活化 7 题全过：空间只有未发布草稿（诚实说明）、昨晚活动时间线正确、能区分「没看完」和「没记录」，不编；起草的说说内容、对动态的看法均具体。
- 中性题（天气）不触发记忆核查；演示库 13/13；整合分支全量回归 OK（skipped=3）。

**实体回填副本干跑（2026-10-26）**：在只读副本库执行 `memory backfill`：mentions 与实体覆盖显著增加、标签链接新增百余条；二次回填新增 0（幂等），测试/夜测条目自动跳过。回填后副本评测 18 题 16/18（两处为代词歧义与态度词表，不是产品回归）。

已知限制：跨 session 的别人私聊记忆只回答「确实提过，但不方便转述」，不转述细节（项目硬约束：不泄露私聊）；细节层仍无向量排序；`low` 的 `decision.backend=rules`，KEV 重排未参与。

## Detail 层（已落地）

| 项 | 实现 |
|---|---|
| 表 | `event_details`（event_id / kind / text / quote_text / quote_turn_id / occurred_at / emotion / importance / detail_hash） |
| 索引 | `detail_fts`（text + quote）、event/time/session/turn 索引 |
| 幂等 | `detail_hash` 唯一 + `archive_runs` 记录 `date:session:event` |
| 抽取 | 规则版：按句读切分，一句一 detail；`quote_text` 必须是原 turn 的精确子串 |
| 分类 | fact / emotion / plan / decision / change（关键词规则） |
| 过滤 | `verify-*`、`devcheck`、`test-*`、`live-tts25*` 不入库 |
| 当前量 | 99 条 / 11 个事件（75 fact、19 emotion、5 change） |

已知限制：还没有实体抽取与语义三元组；`kind` 是关键词判断；没有 LLM 改写/补全。这些留给下一阶段（LLM 抽取仍走同一张表，服务端校验 quote 精确性）。

## 实体与标签（v6，已落地）

schema v6（`fatfish/schema.sql` 末尾 + `fatfish/db.py` `SCHEMA_VERSION=6`）按 ADR-0003 §1 新增五张表，旧库走 `ensure_schema()` 幂等升级（test_p0 有升级路径测试）。

| 模块 | 表 | 要点 |
|---|---|---|
| 实体归一（`fatfish/memory/entities.py`） | `entities` / `entity_mentions` | 确定性称呼归一：主人哥哥/主人/哥哥→`person:owner`、姐姐→`person:friend`、鲸鲸/鲸鱼娘/大肥鱼→`person:self`；「我」按 user/assistant 角色归属。mention 记 surface + turn_id + 字符区间，UNIQUE 索引幂等 |
| 标签治理（`fatfish/memory/tags.py`） | `tags` / `tag_links` | `classify_tag` 把扁平标签归入 domain/relation/emotion/time/topic 五个 namespace（日期→time、情绪词→emotion、称谓→relation、其余→topic 截 48 字）；新标签 `candidate`，usage_count≥2 自动升 `active` |
| 关系边 | `entity_edges` | 表已建，本期无写入（留后续） |

消歧守卫（定向启发式，**宁漏记不错归**，取舍写在 `entities.py` docstring）：

- **scope 消歧**：`session_scope(session_id)` 按 id 是否含 `group`（不区分大小写）判 group / direct。群聊下 owner/friend 的 mention 置信降为 0.4（含 user 的「我」），self（鲸鲸/鲸鱼娘/大肥鱼与 assistant 的「我」）保持 0.8；**群聊来源的 memory_items 不合并 `entities_json`**（mention 照写，append-only 不变）。误判 group 代价小（只降置信、不删数据），误判 direct 会把群友昵称归到主人头上，所以从宽判 group。
- **引语守卫**：「我」落在引语区间（「…」『…』“…”‘…'、"…"）内一律跳过（不分角色）——引语是转述别人的话，turn 角色定不了引语里的「我」是谁；其他称呼在引语内照常命中。未闭合引号不守卫（往漏记方向安全）。
- **前缀守卫**：哥哥/姐姐/主人 前一字为 小/大/老 则跳过（小姐姐≠姐姐）；「主人哥哥」全称豁免。这是词表启发式，不是通用分词，可按需扩词。

接线与回填：

- 夜间归档写实体（`archive_session_entities`）与标签（`sync_tags`）；
- 存量回填 `backfill_entities`（memory 级 + event/episode 级两级 mention，`entities_json` 合并 union）与 `backfill_tags`，均幂等、只新增不删除；
- **真实库 `data/fatfish.db` 尚未跑回填**，实体覆盖仍为 0/17，需人工执行 `python -m fatfish memory backfill`。

群聊误归属与引语「我」误归已由上述守卫缓解。仍留局限：**无停用词**（见下节 coverage 取舍）、开放 NER 缺；本期只做 person 类称呼（v13 起人物卡补上了 `merged_into` 合并与实体族折叠，见下节）。

## 人物卡（T-0057，v13）

### 动机

实体层此前只认识三张写死的卡（`person:owner` / `person:friend` / `person:self`）：聊天里来的人没有记录；QQ 群又是共享 session，`conversation_turns` 只有 `session_id`，v13 之前**无法回答「这句是谁说的」**。T-0057 一次补两块：

| 补什么 | 落点 |
|---|---|
| 人物卡（谁） | `entities` 表 `kind='person'` 卡 + `fatfish/memory/entities.py` 人物卡函数 |
| 逐轮发送者归属（哪句话是谁的） | `conversation_turns` 新增 `sender_id` / `sender_name`（schema v13） |

schema v12 → v13：`conversation_turns` 新增 `sender_id TEXT`、`sender_name TEXT NOT NULL DEFAULT ''`，新索引 `idx_turn_session_sender(session_id, sender_id, id)`；迁移走 `db.py` 的 `_COLUMN_MIGRATIONS` / `_INDEX_MIGRATIONS`，幂等，旧库 `ensure_schema()` 自动补列。群聊共享 session，有了逐轮归属才能回答「这句是谁说的」；assistant 轮与没有逐条 sender 的渠道（Web、system prompt）留 NULL/''。

### 卡片形态

人物卡就是一行 `kind='person'` 的 `entities` 记录，与 QQ 空间作者共用同一套键：

- 有 uin → 主键 `person:qq:<uin>`（和 QQ 空间作者同一套键，改昵称只加别名、不换卡）；
- 无 uin → `person:nick:<name>`（只在还没拿到 uin 时用）；
- `aliases_json` 收旧名与别名；`attrs_json` 存 `{"uin", "relation", "remark", "source", "role"}`（聊天自动建卡的 `source` 为 `chat`，绑定回填为 `binding`，手工建卡默认 `manual`）。

`entity_card` 输出示例（uin 为假号）：

```json
{
  "id": "person:qq:123456789",
  "kind": "person",
  "name": "小蓝",
  "aliases": ["蓝蓝"],
  "attrs": {"uin": "123456789", "relation": "同学", "remark": "", "source": "chat"},
  "uin": "123456789",
  "relation": "同学",
  "remark": "",
  "mentions": 12,
  "merged_from": []
}
```

### 合并与实体族

- `merge_entities(source, target)` 写 `merged_into`、别名并入目标；**永不自动合并**——`duplicate_candidates` 只给同名候选，合不合由人决定。
- `entity_mentions` **永不删除、不搬家**（append-only 证据）；读路径（`entity_card` / `list_people` / `_entity_event_ids`）都按「实体族」（`merged_into` 链）折叠，合并后旧 mention 照常可查。

### owner/friend 权威绑定

`owner_bindings` 是权威：`sync_binding_entities`（CLI `person sync`）把 `display_name` 与 `qq:private:<uin>` 里的 uin 回填到 `person:owner` / `person:friend` 卡。friend 的 canonical 名不再写死「姐姐」，改用 `relationship.friend_display_name`；无绑定时保留手工改名。

### 自动建卡（`[memory] person_autocreate`）

`handle_user_message` 新增 `sender_id` / `sender_name` 入参，每条用户轮次落库归属。是否自动建卡由 `[memory] person_autocreate` 控制（默认 `private`，是代码默认值，toml 未写）：

| 档 | 行为 |
|---|---|
| `off` | 只记逐轮归属，不建卡、不写 mention |
| `private`（默认） | 私聊自动建/更新卡，并写一条**会话级 mention**（`turn_id=NULL`，每会话每人一行，不随消息数膨胀——逐条归属已经在 `conversation_turns.sender_id` 上） |
| `all` | 群聊也建卡 |

未知取值按 `off` 处理（fail closed）。bookkeeping 出错绝不影响回合（`note_inbound_person` 吞异常返回 `None`）。

### CLI

```bash
python -m fatfish memory person add 小蓝 --uin 123456789 --alias 蓝蓝 --relation 同学
python -m fatfish memory person list --query 蓝
python -m fatfish memory person show 123456789       # 选择器支持 id / 姓名 / 别名 / uin
python -m fatfish memory person rename 小蓝 阿蓝      # 旧名自动转为别名
python -m fatfish memory person alias 阿蓝 add 蓝宝   # alias <选择器> add|remove <别名>
python -m fatfish memory person merge person:nick:蓝蓝 person:qq:123456789
python -m fatfish memory person duplicates           # 同名候选，只提示不自动合
python -m fatfish memory person sync                 # owner/friend 绑定回填到卡
```

Web 侧契约：`GET /api/people`、`POST /api/people/add|update|merge`（面板入口：侧栏/底栏「人物」）。

测试：`tests/test_memory_people.py`（42 例）、`tests/test_turn_sender.py`（16 例）、`tests/test_cli_people.py`（15 例）、`tests/test_web_people.py`（8 例）。

### 红线

- **QQ 号只进 `id` 和 `attrs_json`，绝不进 `canonical_name` / `aliases_json`**——`entities_for_query` 拿名字匹配查询文本，命中结果可能进提示词，uin 不能跟着进去；
- CLI 输出默认掩码 uin（`123****6789`）：`_mask_person_payload` 把 `uin` 字段和 `person:qq:<uin>` 形态的主键、`merged_from` 一起掩掉（卡片主键本身就含 uin，只掩 `uin` 字段挡不住 id 这条路），`--full-uin` 才显示原文；
- Web API：`uin` 字段与 `merged_from` 同样只出掩码；卡片 `id` 保留原文作回传句柄（`/api/people/update|merge` 要用它寻址），UI 不渲染 id；
- `sender_id` 是本地标识，只落库，不渲染进提示词 / CLI / Web 输出；
- 名册/对话者/说话人前缀只带称呼关系备注，不带 uin（T-0060）。

### 上下文接入（T-0060）

人物卡接进每轮 prompt 的三个落点（都在动态区，不碰缓存前缀）：

| 落点 | 位置 | 行为 |
|---|---|---|
| 【认识的人】名册 | `entities.roster_for_prompt` + `ContextAssembler._dynamic_text` | 查询文本实体命中优先，兜底按 (mentions desc, last_seen desc, id asc) 确定性补齐 top-10（排除 owner/friend/self 三张卡、mentions=0 不入兜底）；与记忆摘要同门：陌生人（私聊未绑定）不可见；名单为空整块不渲染；remark 截 60 字 |
| 【当前对话者】按卡 | `ContextAssembler._persona_hint` | 群聊/owner/friend 绑定档不变；未绑定私聊先按 session uin 查卡，命中则渲染「名字（关系或 联系人）」+ 原陌生档隐私护栏；未命中走原文案 |
| 说话人前缀 | `ContextAssembler.history_messages`（`_history_text` 同源） | 用户轮按 `sender_id` 查卡，命中用卡 canonical，否则用 `sender_name`，都没有则无前缀；`[时间] 称呼：内容`，时间戳仍在行首 |

测试：`tests/test_context_roster.py`（25 例）。

## 时间/实体预过滤（已落地）

`fatfish/memory/querytime.py` 的 `parse_time_window(query, tz)` 把查询里第一个中文时间词解析成 `[t0, t1]` Unix 秒硬过滤窗口：`end` 为包含语义（下一周期起点 −1s），任一侧可为 `None`（不设界），`(None, None)` 表示未命中、跳过过滤；一次只取一个表达式，按「显式日期 > 单日 > 月段 > 周 > 月 > 年 > 模糊近期」的固定优先级。

| 类型 | 表达式 | 窗口 |
|---|---|---|
| 相对日 | 今天 / 昨天 / 前天 / 大前天 / N天前 | 单个自然日 |
| 模糊近期 | 最近 / 近期（30 天）、最近几天 / 前几天（7 天） | `[now−N, ∞)` |
| 周 | 本周 / 上周 | 自然周 |
| 月 | 本月 / 上个月 / 7月 / 去年7月 / 2026-07 / 2026-07-14 / 2026年 | 自然月/日/年 |
| N个月前 | 一个月前 / 三个月前 / 两三个月前 | 以 N 为中心 ±1 月模糊窗口 |
| 几个月前 | 几个月前 / 数个月前 | **`[now−6月, now−1月]`** |
| 半个月前 | 半个月前 | `[now−30天, now−7天]` |
| 年 | 今年 / 去年 / 前年 / N年前（1≤N≤10）/ 几年前 | 自然年；N年前为 N±1 年，几年前=`[now−10年, now−2年]` |

召回链路（`fatfish/memory/service.py` + `runtime.py`）：`recall()` 与新增 `recall_details()` 自动解析时间窗并下推 SQL 硬过滤；`recall_details` 另用 `entities_for_query` 收窄 detail 检索（实体过滤无结果时自动回退不过滤，宁多勿漏）。runtime 单轮召回新增细节层：`memory.detail_top_k`（默认 4），细节**不经 RecallGate** 直接拼入召回。

细节匹配（`details.search_details`，二轮重写）：query 切 token——ASCII 串整体、CJK 串切相邻二元组 bigram、单字成 token、去重保序；候选改为单条 `LIKE ... OR ...` 扫描（`ESCAPE '\'`），**弃用 `detail_fts` MATCH 分支**（unicode61 对 CJK 无分词能力，整串一个 token，自然语句基本匹配不上），`detail_fts` 表与触发器原样保留、留给未来 gram 分词索引。打分用 coverage（命中 token 数 / query 总 token 数，latin 小写比较），过 `memory.detail_min_score`（默认 0.2，未改 toml）门槛才保留；**兜底：命中 ≥2 个不同 token 的候选始终保留**——长句虚词 bigram（的/她/那）会稀释 coverage，单个偶然共享 token（如「今天」）必须过比值，两个内容窗口不必；`min_score<=0` 关门。结果按 coverage → importance → recency 排序。

已知局限：「今年多大了」这类非事件问题也会命中「今年」窗口（无时间戳的行不受影响）；「半年前」不识别（只有「半个月前」）、星期组合（「这周三」）与未来时间词（明天/下周/明年）不在本期范围。细节召回侧，自然长句不命中已由 gram-LIKE + coverage 门槛缓解；2026-10-26 起称呼/虚词 bigram（主人/哥哥/姐是/是不/不是…）不再算内容命中——同一人 ≠ 同一事件；但仍无通用停用词表，等真实 query 再调。

## 实体边与关联补充（2026-11-01，已落地）

ADR-0003 §4 召回表第 6 行「后来呢」的最便宜实现，思路来自 ActionRAG（arXiv:2610.32752）
的「图修正初始相似度」：直接命中只有词面重叠，相关事实之间靠实体关系相连。

**写侧**（`fatfish/memory/edges.py`，nightly + backfill）：

- 一条 detail 的文本/原话里共同出现的实体两两写一条 `entity_edges`
  （`relation='co_mention'`，`evidence_detail_id` 指回该 detail）——边是证据不是结论，
  同一对实体在几个 detail 里出现就留几行；方向按实体 id 排序，不会 A-B/B-A 双写；
- 顺带写 detail 级 `entity_mentions`（`source_type='detail'`），作「detail ↔ 实体」反查索引
  （此前 mention 只有 memory/event/episode 三级）；
- 边置信度 = 两端 mention 置信的较小值：直接会话 0.8，群聊同现 0.4
  （沿用 `session_scope` 的口径）；
- 幂等：按 `(src, dst, relation, evidence_detail_id)` 查重，只增不改；
  实体合并后旧 id 上的边与 mention 经家族折叠仍可达，`graph_via` 显示存活卡的 canonical 名。

**读侧**（`MemoryService.recall_details` → `expand_entity_neighbors`）：

直接命中的 detail 取其实体（detail 级 mention 优先，缺失时按文本兜底识别——回填前的旧库也能用），
沿 1 跳边找邻居实体，取邻居的 detail 以 `via_graph=True` + `graph_via="锚点名 ↔ 邻居名"` 带出，
渲染为 `[关联补充 · 经人物关系带出的上下文，不是独立命中]`。锚点实体彼此相连不扩散；
已在直接命中/同事件补充里的行不重复。

| 配置 | 默认 | 作用 |
|---|---|---|
| `memory.graph_expand` | 开 | 总开关（0 关闭） |
| `memory.graph_expand_max` | 3 | 单轮补充总条数 |
| `memory.graph_expand_per_neighbor` | 2 | 每个邻居实体最多带几条 |
| `memory.graph_expand_min_confidence` | 0.5 | 低于此置信的边不参与（群聊同现的 0.4 默认进不来，宁漏记不错归） |

收口：时间窗与召回共用（查询解析出的窗口直接下推）；测试 session 的 detail 一步不进；
T-0073 的 viewer 闸门在补充之后统一过滤（别人 session 的邻居 detail 不外流）；
runtime 台账新增 `memory.detail_graph` 计数（`details`/`detail_context` 不含图补充）。

边界测试：`tests/test_memory_edges.py`（34 例，含实体合并家族折叠、引语/前缀守卫、
群聊低置信边、时间窗、预算与排序、测试数据隔离、viewer 闸门、开关关闭）。

## 本次首跑暴露的问题

1. **测试 session 污染**：`qq:private:verify-*` 和 `web:devcheck` 也被归档成 episode。
2. **同日 gist 撞名**：4 个 session 各生成一条「2026-10-20 的日常」，只看 gist 无法区分。
3. **标签扁平**：`tags_json` 只有 `["archive","2026-10-20"]`，`entities_json` 全空。
4. **摘要过短**：daily memory 的 summary 只有 20–47 字；事件原文虽在 `conversation_turns`，但召回不带回。
5. **缺跨 session 汇总**：同一天 4 个 session 各生成一条「2026-10-20 的日常」，没有统一的日汇总，gist 互相撞名。episode 本身按 `ep:date:session` upsert，重跑不会重复（这一点是好的）。

### 召回实测（2026-10-24，合成数据示例）

```text
$ python -m fatfish memory recall "那天她心情不好，后来我们聊了什么"
- (2026-10-21) 2026-10-21 的日常 [score=0.49]
- (2026-10-20) 2026-10-20 的日常 [score=0.46]
- (2026-10-21 11:06) （某句情绪原话摘要） [score=0.45]
- (2026-10-20) 2026-10-20 的日常 [score=0.45]
- (2026-10-20) 2026-10-20 的日常 [score=0.44]
- (2026-10-20 21:07) 鲸鲸 [score=0.40]
```

结果全是摘要/gist，没有后续对话、没有原话、没有情绪与人物。这不是排序问题，是**入库时就把细节丢了**。（下方的健康检查工具同属研发期内部脚本，未随发布版分发，量级数字为当刻快照示例。）

### 健康检查（研发期内部工具）

研发期有一个人读/机读两用的库健康巡检脚本（未随发布版分发），它回答四件事：
轮次/事件/归档量级、标签分布、实体覆盖率、测试 session 是否混入。发布版里
`python -m fatfish doctor` 覆盖其中环境与配置部分。

结论（历史审计）：**早期标签只有个位数、实体一个都没抽出来**。这比"召回排序不准"更根本。

## 已具备但未用的字段

`memory_items` 已有 `entities_json`、`tags_json`、`stability`、`tier`、`time_granularity`、`supersedes/superseded_by`、`subject`、`about_json`；schema 已为多级记忆留了位置，问题在写入与召回没用起来。

## 做梦（夜间巩固）与分层路由

用户提案（2026-10-24）已并入 ADR-0003 §4.5：

- **每晚 03:00** 按 `date + session` 幂等跑：turn → detail → event → day；每月 1 日、每年 1 月 1 日分别生成 month / year 节点；semantic 随重复事实提升。测试 session 跳过。
- **日/月/年三级做梦**：day（当天细节）、month（主题/重复/变化）、year（里程碑/关系阶段）。高层只做索引，每条断言必须带子节点证据；月/年不能替代低层，问细节要下钻。
- **月/年不要用测试数据跑**：至少积累 2–4 周（月）、6–12 个月（年）再开，避免生成无证据的长期记忆。
- 每级都建 embedding，但**不跨级直接比 cosine**（文本长度不同，分布不可比）；层内用 RRF，跨层用「查询类型 + 层先验 + margin」路由。
- **父级命中 = 定位后下钻**，不是用摘要回答；**子级命中 = detail 直取 + 1 行父级上下文**。
- **先细节、后原文**：默认返回 detail；只有用户明确要原话/细节、同一节点二次命中、置信度低或用户质疑时，才通过 `quote_turn_id` 拉原始 turn。
- 展开预算：父级最多 8 条 detail，总注入 ≤1200 tokens；没有 detail 时只能答“只记得大概”。
- 做梦只整理、不编造：level 1/2 的每条断言必须有 `turn_id` 证据；生成的“梦话”只进报告，不进事实召回。

## 容量与性能实测（2026-10-24）

设备：D 盘可用 **232.9GB**（总 701GB），E 盘 3.47TB；DB 在 D。

当前 DB：**1.45MB**（BLOB 迁移前 1.94MB），WAL 0，page 4KB，`synchronous=2`，`cache_size=2MB`，`auto_vacuum=0`。

- embedding 已从 JSON 改为 float32 BLOB：memory 向量省 26%、rag 省 17%、768 维贴纸向量省 57%；迁移用研发期脚本（未随发布版分发）；`loads()` 同时读旧 JSON 和新 BLOB。

| 组成 | 占用 | 占比 |
|---|---:|---:|
| 表情包索引与向量 | 966KB | 49.9% |
| metrics / traces / audit | 475KB | 24.5% |
| **对话与记忆** | **274KB** | **14.2%** |
| outbox / wakeups / photos | 119KB | 6.1% |
| 词库 | 65KB | 3.4% |

对话现状：**62 轮 / 1,939 字 / 平均 31.3 字/轮 / 最长 130 字**。
折算：对话数据 **4.4KB/轮**（含事件、日摘要、memory 向量、FTS 索引，不是纯文本）。

### 检索延迟（合成数据，5 次中位数）

| memory_items | 256 维（当前 hash） | 768 维（bge） |
|---:|---:|---:|
| 17（真实库） | 1.3ms | — |
| 1,000 | 54ms | 119ms |
| 5,000 | 264ms | 606ms |
| 8,000 | 262ms（被 `max_items=5000` 截断） | — |

加硬过滤后（768 维、5,000 条）：

| 查询范围 | 延迟 |
|---|---:|
| 无预过滤 | 606ms |
| 最近 24h | **4.1ms** |
| 最近 7 天 | 22ms |
| 最近 30 天 | 88ms |
| 纯 FTS5 | 1.3–1.7ms |

### 结论

1. **存储不是瓶颈**：4.4KB/轮 → 232.9GB ≈ **5,600 万轮**；按 40 轮/天 ≈ **3,900 年**。加上 detail 层（3 条/轮、768 维 BLOB）约 15KB/轮 → **1,600 万轮 / 1,100 年**。
2. **纯文本容量**：当前 31 字/轮，232.9GB 按 4.4KB/轮折算 ≈ **17.8 亿字**（含索引/摘要/向量开销；只算原始对话文本会更多，但没有实际意义）。
3. **真正的瓶颈是召回**：`search()` 在 Python 里逐行 JSON 解析 + cosine，复杂度 O(N)；`max_items_per_search=5000` 还是硬上限。
   - 5,000 条 memory ≈ 2.5–4 万轮 ≈ 40 轮/天下的 **1.7–2.7 年**；
   - 若按 detail 3 条/轮，5,000 条只够 **约 1,700 轮 ≈ 42 天**。
4. **解法**：先用时间/实体/FTS 硬过滤到 ≤200 候选，再向量精排；候选 >500 再考虑 ANN（sqlite-vec / FAISS）。24h 窗实测快 **148 倍**。
5. **已落地第一批优化**：`memory.candidate_limit=512`（`config/low.toml`）。`search()` 先用 FTS5 + LIKE + 高重要性补足到 512 条，再算 cosine；5,000 条 768 维从 **575ms → 60ms**（9.5 倍），精确命中不丢。设为 `0` 可退回全表扫描。
6. **metrics 不是记忆**：它是遥测表（LLM token/延迟/缓存、intent、TTS、唤醒等），每天晚上按 `nightly.metrics_retention_days=30` 清理；当前 2,621 行 / 475KB 属正常，不是泄漏。若要长期趋势，应做日聚合表，而不是保留原始行。

### “真正能用”的数据库

磁盘上限 ≠ 可用上限。按 100ms 检索预算（768 维）估算：

| 场景 | 可用条数 | 折算轮数 | 40 轮/天 |
|---|---:|---:|---:|
| 无预过滤，事件级（1 条/5 轮） | ~830 | ~4,150 | ~104 天 |
| 无预过滤，detail 级（3 条/轮） | ~830 | ~277 | ~7 天 |
| 200ms 预算，事件级 | ~1,650 | ~8,250 | ~206 天 |
| 短名单 512 + 时间/实体过滤 | 固定 ~60ms | 磁盘上限 ~1,600 万 | ~1,090 年 |

结论：**带时间/实体约束的检索，真正可用≈磁盘上限；无约束的全局语义检索，100ms 预算下只有约 800–1,900 条记忆。** detail 层必须预过滤，不能靠全表扫。

## 改进方向（详见 ADR-0003）

GitHub 调研（Mem0 / Letta / Graphiti / Cognee / Memobase / Memori / Basic Memory 等）的结论：没有能原样嵌入现有 schema 的方案，但可借三个骨架——**Graphiti 的双时间+原文回链、Memobase 的 event→gist→原文、GraphRAG 的多分辨率**。

最小可行改造（只改 3 处）：

1. **`event_details` + `detail_fts` + 召回强制展开**：一句一事实，带 `quote_turn_id` 回链原话。摘要只做索引，不再当事实。
2. **时间解析 + episode/event 时间窗检索**：先 SQL 硬过滤，再向量；解决「几个月前」。
3. **`entities` / `entity_mentions` + 标签 namespace 归一**：解决实体 0/17、称呼归一、跨事件反查。

配套：测试 session 过滤；`archive_runs` 幂等；事件身份用 turn 区间 + 日期 + 实体集合，不用 embedding 阈值去重；融合用 RRF。

## 资产

| 路径 | 说明 |
|---|---|
| `data/fatfish.db` | 1.45MB，40 表，schema v4（embedding 已改 float32 BLOB） |
| `data/backups/fatfish-before-nightly-20261024.db` | 本次首跑前备份 |
| `models/bge-base-zh-v1.5` | 391MB，9881 服务 |

## 跨机记忆迁移（T-0065）

把本机的「记忆」搬到云端那份库（`/opt/fatfish/data/fatfish.db`），**不搬机器状态**。
工具：`scripts/memory_bundle.py`（标准库 + sqlite3，零依赖）。

```bash
# 1) 本机导出（源库只读打开，不会被写）
python scripts/memory_bundle.py export --db data/fatfish.db --out /tmp/mem.bundle --force
# 2) 先看会写多少（目标库只读，不落盘）
python scripts/memory_bundle.py import --bundle /tmp/mem.bundle --db /opt/fatfish/data/fatfish.db --dry-run
# 3) 正式导入（幂等，可重跑）
python scripts/memory_bundle.py import --bundle /tmp/mem.bundle --db /opt/fatfish/data/fatfish.db
# 4) 校验：行数 / FTS / 引用完整性
python scripts/memory_bundle.py verify --bundle /tmp/mem.bundle --db /opt/fatfish/data/fatfish.db
```

- **bundle 形态**：单个 SQLite 文件（`bundle_meta` 记 `schema_version`、各表行数、导出时间、
  `embedding_dims`、被排除/被跳过的表），一个 scp 就能传，BLOB 与类型原样保留。
- **白名单**：`episodes` / `conversation_events` / `conversation_turns` / `memory_items` /
  `event_details` / `rag_chunks` / `lexicon_entries` / `persona`。
  **不导出** `outbox`、`wakeups`、`turn_traces`、`metrics`、`photo_jobs`、`sing_jobs`、
  `qzone_posts`、`owner_bindings` —— 这些是本机的发送队列、定时器与作业台账，搬到云端会
  重放或重复。
- **embedding 维度闸门**：导入前比对 bundle 与目标库的 BLOB 宽度（当前 1024 字节 = 256×float32）。
  不一致直接 `error:` 退出、**一行都不写**，避免半截库。
- **幂等**：按主键 `INSERT OR IGNORE`。同主键同内容算「已存在」（skip），
  第二次跑 `inserted` 全 0。
- **主键冲突**（同 id 但内容不同）默认**中止**并列出表与条数 —— `INSERT OR IGNORE` 会悄悄丢掉
  这些行；确认要放弃再加 `--accept-collisions`。真实迁移里最可能撞的是 `lexicon_entries`
  的种子条目（两台机器各自 `seed_if_empty`，时间戳不同），届时按需决定。
- **导入后重建 FTS**：`memory_fts` / `rag_fts` / `detail_fts` 由基础表整表重建，不依赖触发器是否健在。
- `lexicon_entries` 是 `LexiconService` 懒建的：目标库没有这张表时，导入会用 bundle 里的 DDL 建出来
  （`created_tables` 会写明），不改 app 自己的 schema 管理。
- **verify 的中文查询**是真实数据里取的一段 CJK：`unicode61` 把整段中文当一个 token，
  所以要整段查（`memory search` 里 MATCH + LIKE 双路的由来见上文）。
- 实测（本机库副本 → 全新 v13 空库）：`episodes 14 / conversation_events 41 /
  conversation_turns 152 / memory_items 62 / event_details 165 / rag_chunks 18 /
  lexicon_entries 138 / persona 1` 全部落地，二次导入 `inserted` 全 0，verify `ok: True`。

## 验证命令

```bash
python -m fatfish nightly --date 2026-10-20 --dry-run
python -m fatfish nightly --date 2026-10-20
python -m fatfish memory search "鲸鲸"
```

## 历史资料（已归档）

`memory-archive.md`、`memory-budget.md`（显存部分已过时）。

## 记忆隔离（T-0073，主人 2026-10-30 拍板）

口径：**只有主人能了解其他人和鲸鱼娘的对话内容；其他人只能了解主人的非涉密内容**
（黑名单制：作息、`secret_notes` 保密清单、其他人的对话内容，三样打死不出口）。

| 机制 | 位置 | 说明 |
|---|---|---|
| `memory_items.audience` 列 | `db.py _COLUMN_MIGRATIONS` | `owner`=仅主人会话（fail-closed 默认）；`open`=主人起源且非涉密，任何会话可喂 |
| 写侧打标 | `MemoryService.audience_for` | 主人会话起源：命中 `SCHEDULE_SECRET_RE`（睡觉/起床/几点/在哪/回家…）→ `owner`，否则 `open`；其他起源一律 `owner` |
| 读侧闸门 | `search / search_rag / recall_details` 的 `viewer_session/viewer_role` | 非主人观众只能看到：自己 session 的行 + 主人起源 `open` 行 + 非私聊起源的 `global` 行；**哨兵 `_UNSET`**：CLI/Web 控制台（主人本人用）不传 viewer，维持全量 |
| 原话不外流 | `recall_details` | 黑名单制共享的是**事实**不是原话——非主人观众的 event_details 只来自自己的 session |
| 角色扩展 | `owner.py` | `family` / `teacher` 两个多持有人角色（妈妈 + 两位老师可并存）；`_resolve_role` 不再把绑定一律折叠成 owner |
| persona v8 | `context.py` | 边界句：别人的私聊内容只有主人问才讲；可聊主人非涉密日常；作息/行踪装不知道 |

- 主人自己的召回**完全不变**（他看得到一切）；群聊发言按说话人角色判定（群主人不设闸，
  其他群员按陌生人闸），群公共记忆（`global` 且非私聊起源）对所有人可见。
- 漫游（ADR-0014）补充：`origin_scope='roam'` 的 summary/key_facts 只写 owner 可读；
  主人做**原话细节**召回时，已绑定家人/老师/姐姐的私聊 session 会被过滤
  （`non_owner_private_sessions`）——主人能查「聊了什么」的摘要/结构化事实，查不到别人私聊的逐字原文。
- 回填：`python scripts/backfill_memory_audience.py --db data/fatfish.db`（先 dry-run 看分类，
  加 `--apply` 写回）。改判单条：`python -m fatfish memory audience <id> open|owner`。
- 绑定：`python -m fatfish owner-role bind --session qq:private:<uin> --role family --name 妈妈`。
