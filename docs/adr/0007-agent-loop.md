---
title: ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）
type: adr
status: accepted
tags: [adr, runtime, autonomy, wakeup, self-continue, bypass, cost, scope]
updated: 2026-10-25
---

# ADR-0007：对话中 agentic 行为——实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）

> **boss 授权记录（2026-10-25）**：T-0027 由 boss 提出并两次收口。v1 是「关键词触发 + 她说稍等才挂唤醒」的省电捷径；v2（本 ADR 记录的最终态）升级为「**每轮对话结束必挂 +60s 续写**」，token 成本 boss 明示接受，同时保留她的沉默权与 cancel-on-reply。

## 背景

之前的她是单轮应答机：一句话进、一个 JSON 计划出，回合结束即失联。要在对话里表现出 agentic 行为——「你一说我就真的去看B站/看空间」，以及「我说了稍等，60 秒后我真的回来说后续」——有两条路：

1. **ReAct 式工具循环**：LLM 在一轮里多计划、多执行、循环观察，直到它自己决定结束。
2. **单轮计划 + 异步续写**：保持一回合一次 LLM 调用；「继续」这件事交给唤醒调度器，下一拍由她带着上一轮的钩子醒来自己决定说什么（或者沉默）。

现有架构里已经有三块现成地基：动作旁路先例（`wants_photo` / `wants_qzone_review`，T-0006 定型的 wrap_untrusted(EXTERNAL) 证据注入）、唤醒调度器（`WakeupScheduler`：min_delay、链深、安静时段顺延、每日配额）与主动投递链（`ProactiveGate` → outbox `proactive_message` → QQ 适配器按 session 原路投递，T-0023/T-0012）。

## 决策

### 1. 不做 ReAct，选单轮计划 + 异步续写

- **成本与时延可预算**：QQ 聊天的第一要务是回得快。ReAct 每多一跳就多几秒首字延迟和一份 token，且循环终止时机不可控；异步续写把「下一跳」推到 60 秒后的独立回合，用户感知是「她想了一会儿又来了」，恰好是想要的拟人效果。
- **风险面小**：ReAct 循环一旦失控（模型反复调用工具）没有天然的刹车；异步续写的每一跳都要重新过 ProactiveGate（安静时段、90 分钟最小间隔、每日 proactive_message 配额）和调度器链深硬顶，**刹车是结构性的，不是提示词性的**。
- **可测**：每个机制都是确定性的纯函数或 DB 状态机（关键词命中、水位冷却、唤醒挂/取消/触发/沉默），离线可断言；ReAct 的「循环行为」只能靠端到端黑盒测。
- **架构不变**：单轮 JSON 计划（`parse_turn_plan`）仍是唯一回合形态；agentic 感来自系统在回合外做的事，不来自回合内的新协议。TOOL_SCHEMA 只给 `schedule_wakeup` 增加 `intent=self_continue`，不新增动作。

### 2. 每轮必挂 +60s，沉默是一等公民

- 任何一次 assistant 回合完成（私聊 QQ 会话、LLM 可用）自动挂 `intent='self_continue'` 唤醒，+60s = 调度器 min_delay。她不回话不挂（群聊/web/CLI 会话、LLM 不可用、本回合已有自己的唤醒如 share_with_owner）。
- 续写回合提示词明确给出合法输出：接着说 / 撒娇 / 补一句 / 发表情包 / **沉默**（约定输出`【沉默】`）。沉默=只花一次判定的 token，不产出不投递；标记轮标记 ephemeral，不进她的历史。LLM 不可用时同样按沉默处理，绝不发兜底话术。
- **cancel-on-reply**：用户 60 秒内先回话 → 入站处理开头取消同会话 pending 的 self_continue（用户回复本身就是新回合，它自己会挂新的）。
- **链深**：自动续写 depth=1 语义上不自动续挂；她显式 `schedule_wakeup(intent='self_continue')` 可再挂一层，调度器对 self_continue 硬顶 `wakeup.self_continue_max_chain_depth=2`，超限是该动作失败而非整轮失败。

### 3. 预算与账本

- `wakeup.max_per_day`（默认 5）**不再计入 self_continue**：否则活跃对话一个上午就吃光全部唤醒额度，饿死 ritual / share_with_owner。self_continue 走独立配额 `wakeup.self_continue_max_per_day`（默认 60，主要约束 DB 行数）。
- 真正花钱的续写 LLM 回合在 fire 时先过 ProactiveGate：安静时段顺延（23:30–08:00 顺延到 08:00）、距上次 proactive_message 90 分钟内取消（`too_soon`）、每日 `wakeup.max_per_day` 条封顶。**投递与 ritual 主动消息共用同一个配额池**，这是「续写不能变成轰炸」的结构保证。
- 挂起 / 取消 / gate 拦截 / 触发 / 沉默 / 投递全量进 `decision_log(kind='self_continue')`；投递行同时记 `decision_log(kind='proactive_message', action='sent')`（ProactiveGate 的计数来源）。晨报可直接统计「她今天自己续聊几次、说了什么」。

### 4. 实时取材旁路（第一期，照抄 qzone review 模式）

- `wants_bili_browse(text)` 关键词旁路：私聊命中「看看B站/刷刷B站/有什么新视频」→ LLM 前执行一次 `BilibiliCollector.browse_once`（联网 5-15s，与拍照旁路同级时延），新入库标题+简介经 `wrap_untrusted(EXTERNAL)` 注入 `extra_evidence`。TOOL_SCHEMA 不加动作——旁路是系统行为。
- `wants_qzone_review` 实时刷新升级：文本含「现在/实时/最新/刚刚」时注入前先 `fetch_and_ingest(manual=True)` 一次（QzoneError 容错，失败回落本地库存）。
- 护栏：同会话同采集器冷却 ≥5 分钟（meta 水位 `live_fetch_at:<session>:<kind>`，**先写水位再抓**，防慢网变成紧循环）；群聊一律不触发；客户端自带节流/熔断不变。

### 5. 范围

web 会话**不挂也不投**（现状是投递被静默丢弃，本期更进一步：根本不产生为丢弃而花的 LLM 回合）；群聊不触发自动续写。QQ 适配器后续若要给 web 前端推送主动消息，属前端工作，不在本期。

## 备选

- **ReAct 工具循环**（见决策 1，否决理由：时延/成本/失控面/可测性）。
- **只在她说「稍等」时挂唤醒**（v1 捷径）：省 token，但续写变成关键词驱动的罕见事件，「对话中活着」的感觉出不来；boss v2 拍板放弃。
- **给 web 会话也挂唤醒**：产出必然被投递层丢弃，纯烧 token；否决。
- **续写走独立投递类型**：ProactiveGate 配额就无法天然覆盖续写，防轰炸要另造闸门；复用 `proactive_message` 让旧闸门直接生效。

## 后果

- 每轮对话多一行 wakeups 记录 + 每 60 秒静默期多一次（可能沉默的）LLM 判定，成本 boss 明示接受；独立配额与 ProactiveGate 兜底防止放大。
- `decision_log` 新增 self_continue 全生命周期行，晨报/夜间归档无需 schema 变更（kind 过滤即可）。
- 续写与 ritual 共享主动消息配额：续写活跃的日子，早安/睡前类 ritual 触发可能被 `too_soon`/`daily_limit` 推迟——这是有意的取舍，必要时调 `wakeup.proactive_min_interval_minutes`。
- 无 schema 变更（复用 wakeups/meta/decision_log/outbox），SCHEMA_VERSION 不动。
- 已知留白：续写回合的 TTS/表情包投递只支持文字+图片（outbox media 原路），语音续写留待前端。
