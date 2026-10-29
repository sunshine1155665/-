---
title: 运行时与人格
type: branch
status: active
tags: [runtime, context, llm, actions, wakeup, life, relationship]
updated: 2026-11-01
---

# 运行时与人格

> 上级：[系统架构](../overview/architecture.md) · 相关：[架构](../overview/architecture.md) · [记忆](memory.md) · [安全](security.md) · [平台](platforms.md)

## 职责

单轮编排：安全过滤 → 认主 → 召回 → 提示词 → 一次 LLM 调用 → 解析 `{messages, actions}` → 本地执行 → TTS → outbox → 发送。

### 空回合兜底（2026-10-31 隔离修复）

LLM 偶尔返回空 plan（`{"messages": [], "actions": []}`）或空文本。旧行为有两条坑：

1. `parse_turn_plan` 把空 plan 当成纯文本，原样返回 JSON 字符串当回复；
2. 真正的兜底 `_fallback_reply` 写死「主人哥哥」，老师/家人/朋友会话命中时会被叫成主人（2026-10-31 杨老师会话「哼」→ 回了「主人哥哥慢慢说」）。

现在：空 plan 解析成 `messages=[]`，交给 **role-aware** 的 `_fallback_reply(text, role=, name=)`——owner 仍是主人哥哥；friend/family/teacher 用绑定显示名（杨老师/妈妈/姐姐）；未绑定私聊/群聊用事件里的 `sender_name`；都没有才用中性「你」。每次兜底记 `metrics.key='fallback_reply'`（带 session/role），便于日后发现模型空回合。`tests/test_fallback_reply.py` + `test_agent_loop.TeacherFallbackIsolationTests` 覆盖。

## 关键代码

| 文件 | 作用 | 入口 |
|---|---|---|
| `fatfish/runtime.py` | 编排核心 | `Runtime.handle_user_message` / `execute_actions` / `tick` |
| `fatfish/context.py` | 提示词装配（人格/历史/记忆/表情/工具 schema） | `ContextAssembler.build` |
| `fatfish/llm.py` | OpenAI 兼容客户端 | `LLMClient` |
| `fatfish/style.py` | emoji/颜文字限额与严肃场景 | `StyleController` |
| `fatfish/textfmt.py` | 去舞台提示/时间戳 | `strip_stage_directions` |
| `fatfish/state.py` | 6 状态机 | `StateMachine` |
| `fatfish/wakeup/scheduler.py` | 定时唤醒 + 主动闸门 | `WakeupScheduler` / `ProactiveGate` |
| `fatfish/continue_policy.py` | 继续对话三信号 + 撒娇阶梯（T-0029） | `ContinuePolicy.should_arm` |
| `fatfish/life/engine.py` | 精力/心情/日计划 | `LifeEngine.tick` |
| `fatfish/relationship.py` | 早安/睡前/久未联系 | `RelationshipRituals` |
| `fatfish/growth.py` | 成长记录 | `append_growth` |

## 动作

10 个白名单动作 + 拍照旁路（`post_qzone` / `share_with_owner` / `share_to_friend` 见 ADR-0004/0005/0006；`sing` 见 ADR-0010）：

| 动作 | 信任 | 上限 | 写 |
|---|---|---|---|
| send_sticker | GROUP | 1 | |
| send_voice | PRIVATE | 1 | |
| schedule_wakeup | PRIVATE | 1 | ✅ |
| remember | PRIVATE | 2 | ✅ |
| open_loop | PRIVATE | 2 | ✅ |
| close_loop | PRIVATE | 2 | ✅ |
| post_qzone | PRIVATE | 1 | ✅（审批/渴望闸门） |
| share_with_owner | PRIVATE | 2 | ✅ |
| share_to_friend | PRIVATE | 2 | ✅ |
| sing | PRIVATE | 1 | ✅（vram 租约 + TTS 交接） |
| search_memory | PRIVATE | 1 | （T-0042，证据留本地） |
| read_qzone | PRIVATE | 1 | （T-0042，证据留本地） |
| make_photo | PRIVATE | 1 | （T-0042，vram 租约，证据留本地） |
| search_videos | PRIVATE | 1 | （T-0042，证据留本地） |

拍照由 `runtime.py:463 wants_photo()` 触发，不进动作表，仅私聊。

### 语音轮的转录保真（T-0056）

`send_voice` 合成的是 `args.text or reply`，而 `[qq].send_text_with_voice` 默认 `false`——语音发出后 `platforms/qq.py` 会把同一段文字整段抑制掉，他**只听到、没看到字**。

所以写 `conversation_turns` 时走 `runtime.py::_transcript_reply`：动作成功合成且 `spoken != reply` 时，历史存**实际念出去的那句**；`send_text_with_voice=true` 时文字也发了，就仍存完整 `reply`。此前一律存 `reply`，等于让她记住一句从没送达的话（「我刚才不是说了吗」）。

**不加 `[语音]` 之类的模态标记**：assistant 历史是 `{"messages": [...]}` 信封，往里塞任何东西都会被她学进输出风格——时间戳就踩过这个坑（见 `history_messages` 注释）。她要判断「刚才是说还是打字」，看 `runtime_context`，不看历史。

## 人格

`PERSONA_VERSION=5`，人设存 `persona.core_json`；`context.py` 拼稳定前缀（可哈希命中）+ 动态段。默认 LLM 为 `opencode_go / deepseek-v4.1-flash`。

「不计较钱」（T-0043）：稳定前缀里插一段固定块 `【不计较钱】`（`MONEY_BLOCK`，`【关系】` 与 `【语气示例】` 之间），让她不为日常小钱劝主人省。放前缀而非 `DEFAULT_PERSONA`，因为线上人设读库；`PERSONA_VERSION` 保持 5——`ensure_default_persona` 在版本落后时整块重写 `core_json`，会抹掉主人手改的人设。

## 对话中 agentic 行为（T-0027）

设计取舍见 [ADR-0007](../adr/0007-agent-loop.md)：单轮计划不变，agentic 感来自回合外的系统行为。两块能力：

### 实时取材旁路（私聊限定，LLM 调用前）

- **B站浏览**：`wants_bili_browse()`（`runtime.py`）——强词 `bilibili/哔哩哔哩/小破站` 单独命中；「b站」需配意图词（看看/刷刷/有什么…）；「新视频+有什么」组合也命中。命中即 `BilibiliCollector.browse_once(limit=chat.live_bili_browse_limit)`，新入库标题+简介经 `wrap_untrusted(EXTERNAL)` 注入 `extra_evidence`，失败回落本地库存（memory_items source_type='bilibili'）。
- **QQ空间实时刷新**：`wants_qzone_review()` 命中且文本含「现在/实时/最新/刚刚」→ 注入前先 `fetch_and_ingest(manual=True)` 一次（QzoneError 容错，失败用库存）。
- **冷却**：同会话同采集器 ≥`chat.live_fetch_cooldown_minutes`（默认 5 分钟），meta 水位 `live_fetch_at:<session>:<kind>`，先写水位再抓（防慢网紧循环）；群聊不触发；旁路是系统行为，不进 TOOL_SCHEMA。

### 继续对话策略：撒娇阶梯（T-0029，ADR-0009；取代早期「每轮必挂」）

「她要不要回来接着说」由三信号显式决策（`fatfish/continue_policy.py` 纯引擎，零 schema 变更）：

- **挂梯条件**：该会话有 7 日内开过/动过的 open loop，或回合末自报 desire ≥0.5；都没命中 → 不挂（沉默路径）。LLM 在动作 JSON 顶层捎带 `continue:{thread_open,desire}` 与 `user_wants`（chat_stop/busy/topic_change）标签——同 post_qzone 的 desire 标尺，诚实打分宁低勿高，不加 LLM 调用；入站另有停聊关键词快路径（先清后设，同条消息自身可再设）。
- **阶梯**：间隔 +1/+10/+30 分钟统一适用；棒数 = 渴望 <0.5→1 / 0.5–0.8→2 / ≥0.8→3（硬顶 3）。payload 带 rung/max_rungs/hook/loop_id（origin="coax"）。
- **独立车道**：不进 ProactiveGate 90 分钟池、不占 `wakeup.max_per_day`，自有每会话每日总帽 `chat.coax_max_rungs_per_day`（默认 6 棒已投递）；投递台账写 `kind='continue_policy' action='rung_delivered'`，**不写** `proactive_message/sent`（那正是闸门的计数行）。
- **续写回合**：fire 时 session 原样复用跑一个真 LLM 回合：coax 棒注入 `chat.coax_prompt`（必须挂在 hook 具体话题上，不换题、不重复原话，可选【沉默】）+ 上轮摘要证据（wrap_untrusted）；她显式 `schedule_wakeup(intent='self_continue')`（origin="explicit"）仍走旧 self_continue 提示词，同一车道。提示词轮与沉默轮都标记 ephemeral，不进她的历史；`allow_voice=False`。投递走 outbox `proactive_message`（文字+可选表情包图片），QQ 适配器按 session 原路投递；web 会话在 fire 时即丢弃（`not_deliverable`）。
- **全梯生死**：任一棒触发前对方任何回复 → cancel-on-reply 杀全梯；她可选沉默 = 提前收梯（不耗棒、不 mute，台账记 silence）；跑完没回 → 该话题当日 mute loop；安静时段触发的棒顺延到 08:00 且棒数保留（排程与处理双重顺延，顺延同步推 expires_at）；LLM 不可用 → 顺延 `chat.coax_rung_llm_retry_minutes`（默认 5 分钟）重试一次不耗棒，再失败才放弃。
- **停聊压制（跨通道）**：`chat_stop`（持续到对方下次开口）/`busy`（默认 `chat.busy_minutes`=120）flag 生效期间，该会话所有主动出站静默——阶梯棒、早晚安仪式等非 self_continue 唤醒（process 时取消+台账 suppressed）、share_with_owner / share_to_friend（动作执行点直接拒绝 `error='stop_flag'`）、已入队的聊天类 outbox 投递（defer 到 flag 解除）。
- **预算**：唤醒行仍走 `wakeup.self_continue_max_per_day`（默认 60）独立计数；`chat.self_continue_enabled=false` 仍是总开关。
- **台账**：全生命周期进 `decision_log(kind='continue_policy')`：ladder_armed / ladder_skipped / cancelled_by_reply / rung_scheduled / rung_fired / rung_delivered / rung_deferred / rung_cancelled / ladder_exhausted / quiet_deferred / stop_flag_set / suppressed / rung_skipped。
- **配置**：`[chat] continue_policy_enabled / coax_intervals_minutes / coax_desire_low / coax_desire_high / coax_max_rungs_per_day / coax_rung_llm_retry_minutes / busy_minutes`，全默认零配置可用（low.toml 显式化基准；standard/remote 用代码默认）。

## 待办与分享候选（agenda）

提示词动态尾部的常驻小节（T-0018），让她每一轮都看得见自己的约定和想分享的东西：

- **待办与约定** = `open_loops` 表 `status='open'` 的行（复用 `open_loop` / `close_loop` 动作，无新动作）。
- **待分享候选** = `qzone_posts` 表 `status='draft'` 的行（`post_qzone` 起草即入列；审批 approved/rejected/posted 后自动消失）。
- **位置**：`ContextAssembler.build` 的 turn_tail 最前——history 之后、`[本轮检索证据]` 之前，只进动态尾部，不碰稳定前缀缓存。原动态段里的 `[未闭合话题]` 小节由本模块取代（同一张表，避免双重渲染与双重查询）。
- **限额**：每节 ≤`agenda_max_items`（默认 8）条、每条截断到 `agenda_item_chars`（默认 80）字；待办带 due 标注（当天=「今日到期」，其他日期=「MM-DD到期」），候选带 `(草稿#{id})` 便于引用。两节全空则整块不渲染。
- **可见性**：沿用记忆摘要的门控——owner/friend 可见，陌生人与群成员不渲染且不查库。
- **过期治理**：夜间归档 `finalize_nightly` 调 `nightly.expire_open_loops`，把创建超过 `agenda_expire_days`（默认 14）天仍未 close 的 open loop 标 `expired`（终态，不再渲染）；分享候选无过期，审批流转即出口。
- **性能**：`context.agenda_enabled=false` 时零额外查询；开启时固定两条 SQL（open loops 一条、draft 一条，无 N+1）。

## 大肥鱼间聊天（T-0041）

只对一个配置里绑定的 QQ 账号生效（`[ai_chat].peer_uin`，**留空即整条链路关闭**）：
`session_id == "qq:private:<peer_uin>"` 才会进入该模式，其他账号、群聊、web 一律不启用；
不用关键词触发，也不猜收件人。

- 代码：`fatfish/ai_chat/`（`settings.py` 读配置、`engine.py` 纯数学、`service.py` 状态/审计/投递闸门）、
  `runtime.maybe_ai_chat_turn`（主动轮）、`context.py` 的 `[大肥鱼间聊天]` 段、`qq.py` 的 `ai_chat_message` kind。
- 渴望值：该会话每轮 LLM 顶层输出 `ai_chat.desire`（0–1，缺失按 0）；`desire >= 当前阈值` 才允许主动开口。
- 阈值：发一条成功 `+threshold_step`（默认 0.12），随时间**线性衰减**，
  夹在 `[threshold_floor, threshold_ceiling]`（默认 0.15–0.95）。
- **50 条不是硬限流**：`decay_messages_per_hour = 50` 只是衰减基准，
  派生 `threshold_decay_per_minute = threshold_step × 50 / 60 = 0.1 / 分钟`，
  即「每小时抹掉 50 条消息抬升的量」，稳态可持续约 50 条/小时、短时爆发约 7 条（0.15→0.95）。
  代码里没有「到 50 条就拒发」的分支；`hard_cap_per_hour` 默认 0 = 完全不拦，只作手动止血开关。
- 审计：`ai_chat_messages`（schema v11）+ `decision_log(kind='ai_chat')`；已发条数只用于统计与兜底硬帽。
- 该会话不启用撒娇梯（两套主动机制会抢同一份 token 与水位）。

## 自主动作与三域阈值（T-0042）

渴望阈值有三个**互相独立**的域：数学同源（一套 `fatfish/desire/engine.py`），但配置段、
`schema_meta` 状态键、计数、水位全部分开——一个域用得多，不会抬高另一个域的门槛。

| 域 | 配置段 | 状态键 | 驱动什么 |
|---|---|---|---|
| AI 之间 | `[ai_chat]` | `ai_chat:state:<peer_uin>` | 主动跟另一个大肥鱼开口（T-0041） |
| 对主人 | `[owner_chat]`（占位，`enabled=false`，**代码未接**） | `owner_chat:state:<ident>` | 目前仍由 `[chat].coax_desire_low/high` 撒娇梯 + `[activity]` 预算决定 |
| 自己做事 | `[self_action]` | `self_action:state:global` | 下面四个自主动作 |

- 代码：`fatfish/desire/`（`engine.py` 纯数学、`settings.py` 读任意段、`state.py` 定键约定）、
  `fatfish/self_action/service.py`（状态/闸门/记账）、`fatfish/self_action/explicit.py`
  （「主人明确指使」判定）、`runtime.execute_actions` 里的 `SELF_ACTION_TOOLS` 闸门、
  `context.py::_self_action_block` 的证据注入。
- 四个动作：`search_memory`、`read_qzone`（只读）、`make_photo`、`search_videos`（有代价）。
  全部**仅私聊**、每轮各 1 次，`write=False`——不产生对外副作用。
- 闸门顺序：`policy.validate` → **`explicit`（主人明确指使，直接放行）** → `enabled` → 静默时段
  → `daily_cap` → `desire >= 当前阈值` → 本轮 `max_events_per_turn`。缺失 `desire` 按 0 → 不执行。
  被拒写 `decision_log(kind='self_action')`。
- **显式豁免**（`fatfish/self_action/explicit.py`）：主人原话明确让她做这件事（「你去搜一下X」
  「翻翻你以前的记录」「给我画一张猫」）时，四道闸门全部让路，本轮预算也不计——前四道防的是
  **她自己的**冲动，不是**他的**指令。判据**只取主人原话**（`execute_actions(user_text=...)`，
  自生成回合该值为空串），模型在 args 里自报的 flag 一律不看，所以门不能自己开。
  豁免不免记账：成功执行照写 evidence、照抬阈值。放行也留一行审计
  （`outcome='explicit_request'`），半夜为什么在搜，台账里查得到。
  匹配是启发式（按工具分四组正则、先剔否定片段）；**漏判只回落阈值路径**，不会更糟。
- 阈值：`[self_action]` 默认 `initial 0.35 / step 0.10 / floor 0.20 / ceiling 0.90 / decay 30 次每小时
  = 0.05/分钟`；成功执行一次抬一档，**失败不抬**。`hard_cap_per_hour=0` 只软限，`daily_cap=12` 是兜底。
  `enabled` 默认 **true**（2026-10-26 boss 拍板；关掉要显式写 `false`）。
- 结果不在当轮出现：动作在回复之后执行，结果写 `self_action_evidence`（原文，未转义），
  **下一轮**由 `_self_action_block` 取未消费的前 3 条、`wrap_untrusted` 包装后注入，随即标记
  `consumed_at`——同一条只出现一次；超过 24 小时的不再注入。
- 失败也留痕：生图失败会写 `source=photo_failure` 的 evidence，下一轮她照实说没画成，
  不会编造「我给你画了一张」。
- 显卡：`make_photo` 完全委托给 `_take_photo`/`PhotoService`，`vram.lock`、卸 TTS、起停 ComfyUI
  都在服务内部，runtime 这一层不碰。

## 多会话漫游（ADR-0014 P0，2026-11-01）

- 代码：`fatfish/roam/`（`service.py` 生命周期、`summary.py` 确定性总结、`intent.py` 主人指令、
  `model.py` 线程卡/trip/摘要）；schema v14 的 `session_threads` / `roam_trips` /
  `conversation_turns.trip_id`。
- 房间：只允许已绑定的 family / teacher / friend 私聊；owner 是 home，不走 trip；群聊不纳入。
- 出门：主人说「去看看妈妈，问她身体」→ 确定性解析（不依赖模型）或模型调 `roam_visit`；
  校验房间/预算/冷却后写 trip（`entering → active`），给目标排一条 `roam_message` 开场。
- 在场：目标入站重置 20 分钟 idle；主人入站立即 `owner_preempt` 归位并带 recap；
  目标 `chat_stop` 立即离开并封房到当天结束。其他房间来信只加 `unread_count`。
- 归位：`tick()` 先跑 `maybe_roam_return`（idle / max 60 分钟 / 安静时段），写确定性
  summary + key_facts 到线程卡与 `memory_items(origin_scope='roam', audience='owner')`；
  `notify_return=true` 时排 `roam_notice`（安静时段顺延到安静结束）。
- prompt：`attention_block` 固定在 `past_timeline` 之后、`history` 之前；目标房间只看自己的
  线程卡 + 本次目的，主人会话看 `[漫游回顾]`。
- 重启：`Runtime.reconcile_roam()` 把 `entering/active` 标 `interrupted` 并补确定性摘要，
  不补发开场、不补跟进；QQ adapter 的 `run_forever` 启动时调用一次。
- P1 未做：评分调度、`ask` 提案/批准、CLI/晨报、Web 面板、群聊。

## 已知问题

- 首字延迟（历史记录 LLM 37s）未做预算治理。
- `voice.provider="edge"` 是无效配置（代码只读 `voice.enabled`）。
- 生活状态是否真的影响输出，未验证。

## 配置

`config/low.toml`（基准）、`standard.toml`（独显）、`remote.toml`（瘦客户端）、`tts25-live.toml`（实验覆盖，端口有冲突）。
