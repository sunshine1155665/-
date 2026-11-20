---
title: 活动规划（QQ 动态 / B 站观看）
type: branch
status: active
tags: [activity, qq-zone, bilibili, permissions, autonomy, scheduler, decision-turn]
updated: 2026-11-18
---

# 活动规划（QQ 动态 / B 站观看）

> 上级：[系统架构](../overview/architecture.md) · 相关：[平台](platforms.md) · [安全](security.md) · [运行时](runtime.md) · [路线图](../overview/roadmap.md)

## 需求（用户 2026-10-24 指定）

之后要给她：

1. **发 QQ 动态**（说说/配图）；
2. **在 B 站看视频**（浏览、挑选、记住）；
3. 设计合适的**活动方式**，让她像一个有自己生活的人，而不是只会回消息的应答机。

## 现状

- 已有：生活状态 `life/engine.py`、定时唤醒 `wakeup/scheduler.py`、主动闸门 `ProactiveGate`、B 站只读客户端与 collector、拍照/表情包生成。
- **QQ 动态发布已实施（2026-10-24）**：NapCat 无 Qzone action（P0 调查结论，ADR-0004），改走独立 cookie HTTP 客户端；`post_qzone` 动作 → 草稿 → 主人审批（Web/CLI）→ outbox 发布，发布失败重试 ≤3 次后 `failed`。图片上传不做（`media_json` 预留）。
- **私聊分享已实施（2026-10-25，ADR-0006）**：`share_to_friend` 主动私聊好友位绑定的姐姐（outbox 原话投递、每天 ≤2 条、深夜顺延）；评论/点赞取消——她对外的公开互动面只剩说说一条，对人的表达全走私聊/聊天。
- **分享欲模型已实施（2026-10-25，ADR-0005）**：`post_qzone` 加 `desire` 0-1 自评，≥阈值（默认 0.7）跳过审批直接发布、不限次（`max_posts_per_day=0` = 不限制）；新增 `share_with_owner` 动作——90 秒后主动唤醒找主人聊天分享（走 ProactiveGate，安静时段顺延、每日 ≤12 次）；观后感/碎碎念默认聊天里说，说说留给分享欲强的时刻。
- **30 分钟自主决策轮已实施（2026-10-25，T-0025）**：见下文「自主决策轮（已实施）」。
- 没有：B 站“观看”语义（目前只有 ingest）、`activities` 表（决策轮用 `decision_log` kind=`activity_turn` + JSONL 台账代替，见下）。

## 设计原则

1. **只读优先**：B 站观看先做只读；QQ 动态是写操作，必须单独授权 + 频率上限。
2. **活动 ≠ 推送**：她的活动先进入她自己的“今日经历”，只有值得分享的才发动态。
3. **可审计**：每次活动写 `activities` 表（时间、类型、输入、结果、是否外发）。
4. **失败降级**：任何活动失败不影响聊天与语音。
5. **安静时段**：23:30–08:00 不主动外发。

## 权限模型（建议）

| 能力 | 类型 | 默认 | 闸门 |
|---|---|---|---|
| B 站浏览/观看 | 读 | 允许 | 每小时 ≤ 3 次，内容进记忆 |
| B 站收藏 | 写（站内） | 需授权 | 每天 ≤ 2 次 |
| QQ 动态发布 | 写（对外） | 需授权 | 每天 ≤ 1 条；图片需审核 |
| QQ 评论/点赞 | 写（对外公开区） | **不做**（2026-10-25 boss 拍板，ADR-0006；v2 评论欲实现已弃） | — |
| QQ 私聊分享 | 写（对外私聊） | 需绑定 + 配额 | 仅两个收件人：主人（`share_with_owner`）与好友位姐姐（`share_to_friend`，每天 ≤2 条，深夜顺延，ADR-0006） |

实现上不要直接暴露给 LLM 当工具；用**后台活动调度器 + 独立策略**，LLM 只负责“要不要分享、怎么说”。

## 数据模型（建议）

```sql
CREATE TABLE activities (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,            -- browse_video | post_moment | collect | ...
  started_at REAL, ended_at REAL,
  status TEXT,                   -- planned | running | done | failed | skipped
  input_json TEXT, result_json TEXT,
  share_intent REAL,             -- 0-1，值不值得外发
  shared_ref TEXT,               -- outbox id / post id
  error TEXT
);
```

配合 `permissions` 配置节（每类活动的开关、日/时上限、需人工确认）。

## 活动节律（草案）

| 时段 | 活动 | 说明 |
|---|---|---|
| 09:00 | 看今日计划/天气 | 决定心情与话题 |
| 12:30 | B 站看 1 个短视频 | 只读，进 finding 卡 |
| 15:00 | 整理/生成表情包 | 已有能力 |
| 19:30 | 挑一件今天的事发动态 | 需 share_intent 过阈值 |
| 22:30 | 写今日小结 | 进 episode |

## 自主决策轮（已实施，T-0025，2026-10-25）

**两层分离：采集是确定性的，决策交给她自己。**

### 采集层（不进决策、不调 LLM）

- QQ 空间轮询：`maybe_poll_qzone`，`[qzone].poll_interval_minutes`（现 30 分钟，运行时下限 30）。
- B 站入库：`maybe_browse_bilibili`（`runtime.py`），每 `activity.bili_browse_interval_minutes`（默认 120 分钟）`browse_once(limit=activity.bili_browse_limit)`；meta 水位写在抓取之前（与 qzone 轮询同款防紧循环），`[bilibili].enabled` 关着时整段跳过。
- 安静时段（23:30–08:00）采集照常跑，只有决策轮停。

### 决策层（每 30 分钟一次 LLM 决策）

`Runtime.maybe_activity_turn` 挂在 `tick()` 末尾（B 站采集之后，保证本轮决策能看到刚入库的内容），结构与 `maybe_poll_qzone` 同款：meta 水位（`last_activity_turn_at`）+ 间隔 + 安静时段跳过（**不推水位**，08:00 第一个 tick 立即出轮）+ 异常兜底。

- 一轮 = 一次 `handle_user_message`，会话 `activity-YYYYMMDD-HHMM`；owner 绑定同夜测模式——`owner_bindings` 临时 INSERT `source='activity'` 行，`finally` 里整批清理，绝不复用 `bind()`（那会替换真实主人绑定）。
- 提示词 = `activity.decision_prompt`（规则，config 可整段覆盖）+ 素材菜单（runtime 模板化拼装，经 `wrap_untrusted` 作为证据进轮，与「看看空间动态」旁路同一信任面；好友内容默认不给 LLM，只给条数）。菜单含：新帖（作者/时间/摘要）、B 站新入库视频、距上次主动分享、待审批草稿数、life 状态、当前时刻。**决策轮不合成语音**（`allow_voice=False`），回复只进记忆/台账，不打扰真人。
- 她的动作就是现有动作：`share_with_owner` / `share_to_friend` / `post_qzone` / `remember`，全部闸门（渴望值阈值、每日配额、安静时段顺延、敏感内容拦截）自动生效。`share_with_owner` 的 wakeup 在轮后**改挂到主人的真实 QQ 会话**（`UPDATE wakeups SET session_id`，仅 pending 行）——活动会话是临时的，不改挂的话 proactive_message 会在投递层被 `not_qq_session` 静默丢弃；ProactiveGate/每日唤醒上限在触发时照常拦。
- **2026-11-18：活动轮可自发发图/表情包**。`make_photo` / `send_sticker` 在活动沙盒里被视为 proactive：`proactive_media=True`、过 `[proactive_media]` 闸门，提交前用 `_media_target_session()` 把 job 重定向到 owner 的 `qq:private:*`，否则 QQ poller 会跳过 `activity-*`。普通聊天和 self-continue 同样走这套闸门。
- **活动轮上下文（缓存安全）**：活动轮仍用临时 `activity-*` 会话，只在 `extra_evidence` 里注入一段只读的「主人最近 6 轮」摘要。`extra_evidence` 落在 `turn_tail`（`cacheable=False`），不碰 stable prefix / history；活动会话每轮本来就是新 session（history 为空），所以 stable prefix 的缓存命中不受影响。若改成 owner session + transient 模式，则 history 也可缓存，但需要额外屏蔽 owner 副作用，收益不值。
- 回复以「休息」开头 = 这轮休息（`activity.idle_marker` 可调），台账记 `rest`。
- 白天硬顶 `activity.max_llm_turns_per_day`（默认 48）；计数走 `decision_log`（kind=`activity_turn`，action=`decide`），跳过不计。
- **省电捷径**：新帖=0 且 B 站新入库=0 且距上次主动分享 <`activity.share_recency_hours`（默认 6h）→ 整轮跳过不调 LLM。规格书原文的「B 站距上次浏览<间隔」被新入库计数吸收：采集层自管节奏，刚入库的视频必然让计数>0，不会漏聊。
- 台账：`data/logs/activity-YYYYMMDD.jsonl`（相对 `data_dir`，**不是**仓库根——测试的临时数据目录因此天然隔离），每轮一行 `ts/decision/session/menu/actions/tokens/...`，跳过轮也记一行 `skipped`；晨报汇总读它。
- LLM 不可用时整轮 skip（fallback 文案不配当「她的决定」），水位照推防重试风暴。

### 测试

`tests/test_activity.py` 26 条：闸门/水位/安静时段、菜单数据与好友内容隐私面、省电捷径三种打破方式、每日配额、绑定生命周期、share wakeup 改挂、share_to_friend 出箱、台账、`maybe_browse_bilibili` 采集闸门、`handle_user_message` 新参数（`extra_evidence` / `allow_voice`）、决策提示词不触发「空间动态」旁路与拍照旁路。

## 唤醒后动作决策（已实施，T-0072，2026-10-30）

改造前「闹钟到点」只意味着一件事：发一条主动消息。于是「到点了」和「想找人说话」被绑死——既不能安静地去刷会儿 B 站，也不能什么都不做。现在两者拆开：过了 `ProactiveGate` 之后，先花一次极小的 LLM 调用决定这一次做什么。

### 决策流程

```
wakeups.due()
  ├─ intent == self_continue → _finish_self_continue_wakeup（独立通道，未改动）
  ├─ _suppressed_by_stop_flag → 跳过
  ├─ gate.should_act（due / 非静默 / 距上次主动消息 ≥90min / 当日 ≤12 次）
  │     └─ 不过 → cancelled/expired，结束
  ├─ decide_action = false → 直接走老路径（模板文案入 outbox），结束
  ├─ payload.text 非空（"到点我喊你"）→ 承诺优先：原话入 outbox，不花决策
  └─ 决策：build_prompt(上下文) → llm.chat(max_tokens=24, temperature=0)
        ├─ chat   → fire + complete + 模板文案入 outbox（老路径）
        ├─ browse → fire + complete + maybe_browse_bilibili 采集一次，不发消息
        └─ silent → fire + complete，只记日志
```

决策结果一律落 `decision_log`：`kind="wakeup_action"`，`output_json` 含 `{wakeup_id, intent, reason, action, why, model}`，`action` 就是 `chat|browse|silent`，`outcome` 是实际结果（`queued|duplicate|browsed|browse_skipped|browse_failed|silent|empty`）。`why` 区分 `model` / `promise_text` / `llm_unavailable` / `no_response` / `unparseable` / `error:<类型>`。

### 决策输入（~300 token，不含记忆召回）

固定规则（`fatfish/wakeup/decide.py` 的 `DECIDE_PROMPT_TEMPLATE`）+ 一段上下文：当前时刻与是否静默时段、这次闹钟的原因、距上次互动多久、今天已主动找过几次（含上限）、B 站「上次采集时间 / 是否有新视频 / 这轮能不能刷」、最近 4 轮对话（每轮截 40 字）。不塞人物卡、不塞记忆召回——这一轮要的是「要不要出声」，不是「说什么」。

### 配置项

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `wakeup.decide_action` | `true` | 关掉 = 回到改造前：不调 LLM、不写 `wakeup_action` 行，模板文案照发 |
| `wakeup.decide_timeout_seconds` | `8` | 决策调用自己的超时预算（覆盖 `llm.timeout_seconds`） |
| `wakeup.decide_max_tokens` | `24` | 只够吐一个词 |

### 超时与回退

决策跑在 2 秒一 tick 的主循环里，所以**不能**等 `llm.timeout_seconds`（默认 90 秒）。做法是给这一次调用单独设 8 秒预算（`LLMClient.chat(timeout=...)`，落到 HTTP 层），超时/异常/无响应/读不懂/LLM 不可用一律**当场回退到老模板路径**，消息照发。

为什么不落成「这一轮记 pending、下一轮取结果」：`Database` 的连接是 `check_same_thread=True`，工作线程复用不了它，token 计量会丢；而把「已过闸的消息」压在内存里等下一 tick，进程重启或线程被回收就真的吞掉一条她许过的消息。8 秒有界同步（且只有过闸的唤醒才会花，≤12 次/天）比这两种代价都小，也与 `maybe_activity_turn` / `maybe_ai_chat_turn` 在 tick 里同步跑 LLM 轮的既有形态一致。

### browse 与主动消息额度的关系

`browse` 走既有采集层 `maybe_browse_bilibili`：它自己管间隔水位（`activity.bili_browse_interval_minutes`，下限 30 分钟）、bvid 去重与异常兜底，所以同一个 wakeup 最多触发一次采集，失败不重试。browse **不写** `proactive_message`，因此既不占 90 分钟的主动消息池，也不占当日额度——「她安静地看了会儿视频」不该花掉一次「主动联系」。

没得刷时（B 站没开、或距上次采集不足间隔）决策上下文会明确写「不要选 browse」；模型仍选了就降级成**什么都不做**（`browse_skipped`），而不是改成 chat：她选 browse 的语义是「这轮别打扰主人」，把它翻成主动消息恰恰是最坏的降级。

### 承诺优先（T-0031 回归点）

`share_with_owner` 之类 payload 带 `text` 的唤醒（"到点我喊你"）**永远**直接发那条原话：不花决策调用，也不给它被改成 `silent`/`browse` 的机会。`tests/test_wakeup_decide.py` 里有两条测试钉死这一点（模型答 silent / browse 都必须照发）。

### 测试

`tests/test_wakeup_decide.py` 24 条：chat 入 outbox、browse 不发言且采集一次、browse 不占额度、silent 只留审计、承诺文案优先（两种）、`decide_action=false` 行为不变（两条）、超时/异常/无响应/读不懂/不可用五种回退、browse 三种降级、决策输入规模与信号、`decision_log` 字段完整性、`parse_action` 的解析边界、`self_continue` 通道不经过决策。

## 分阶段

1. **P0 可行性**：✅ NapCat 无 OneBot Qzone action，QQ 空间走独立 cookie 客户端（ADR-0004）；B 站观看数据获取方式待确认。
2. **P1 只读活动**：B 站观看 → finding 卡 → 记忆；活动表与调度器；不做外发。
3. **P2 对外发布**：✅ 已实施（2026-10-24 审批制；2026-10-25 起叠加分享欲阈值自动发布，ADR-0005）——desire ≥ 阈值直发（`decided_by='desire-threshold'` 审计）或草稿 → 人工确认（Web 审批卡 / CLI `qzone approve|reject`）→ outbox 发布。候选端点未实测，真实发布待 doctor/live 验证。图片说说不做；评论/点赞明确不做（2026-10-25 boss 拍板，ADR-0006），对好友的表达走 `share_to_friend` 私聊分享。
4. **P3 自主节律**：✅ 已落地（2026-10-25）——`share_with_owner` 主动分享通道（ADR-0005）+ 30 分钟自主决策轮（T-0025，上文）：采集层持续入库，决策层由她自己选分享/聊B站/休息；`activities` 表仍是建议稿，当前用 `decision_log` kind=`activity_turn` + JSONL 台账审计。敏感内容拦截已有（草稿层）。

## 待用户确认

- ~~QQ 动态默认「先草稿后确认」~~ 已拍板（2026-10-25）：desire 阈值自动发布 + 不限次（ADR-0005，boss 授权）；阈值 0.7 跑一段后按 decision_log 校准。
- B 站观看是否需要登录态（收藏/历史）？目前只读 Cookie。
- 动态内容边界：是否允许发她的照片（ComfyUI 生成）、是否允许提到主人。
