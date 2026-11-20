---
title: ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）
type: adr
status: accepted
tags: [adr, runtime, autonomy, wakeup, continue-policy, coax, desire]
updated: 2026-10-26
---

# ADR-0009：继续对话策略引擎——三信号 + 撒娇阶梯

> **boss 拍板记录（2026-10-25）**：把「她要不要继续说话」从 self_continue 的隐式沉默判断升级为显式三信号策略引擎。boss 明确决策：①阶梯无最小间隔（不进 ProactiveGate 90 分钟池）；②她主动提话题没回，与对话没回同一处理，都进撒娇阶梯；③三棒间隔 1/10/30 分钟，统一适用、无例外（否决了「新话题算第一棒、从 10 分钟起」的微调）；④渴望决定棒数 1–3。衔接 [ADR-0007](0007-agent-loop.md)（单轮计划 + 异步续写不变，只升级「要不要挂」的决策）。

## 背景

ADR-0007 v2 的规则是「每个私聊回合结束必挂 +60s 续写」，由她在续写回合自己决定说话或【沉默】。跑起来后暴露两个问题：

1. **盲挂**：不管话题有没有说完、她想不想说，每个回合都烧一次续写 LLM 调用；大多数回合的话题其实已经聊完，靠她在提示词里选沉默兜底——判断发生在花钱之后。
2. **一次即止**：60 秒没等到回复就永远安静了；「我还想跟你把这件事说完」的撒娇感没有梯度。

## 决策

把「要不要回来」改成回合末的显式决策（`fatfish/continue_policy.py`，纯引擎、零 schema 变更、离线可单测）：

### 三信号（谁决定挂梯）

| 信号 | 事实源 | 落地 |
|---|---|---|
| 事情完整性 thread_open | `open_loops` 表（7 日内开过/动过，loop 为准） | LLM 回合末动作 JSON 顶层捎带 `continue:{thread_open,desire}` 标签（同 post_qzone 的 desire 标尺，诚实打分宁低勿高），不加 LLM 调用 |
| 她的渴望 desire | 同上标签的自评 | ≥0.5 才独立构成挂梯理由 |
| 对方意愿 user_wants | 入站关键词快路径 + LLM 分类 | `chat_stop`（别聊了类，持续到对方下次开口）/ `busy`（在忙类，默认 120 分钟 TTL）/ `topic_change`（换题，该话题当日进黑名单，不清 loop 数据）；存 `schema_meta` 带 TTL |

任一停聊 flag 生效 → 该会话**所有**主动出站静默：阶梯、早晚安仪式、share_with_owner / share_to_friend、activity 轮分享、已入队的聊天类 outbox 投递。

### 阶梯（挂了之后）

- 间隔 **+1/+10/+30 分钟**，统一适用、无例外；棒数 = desire <0.5→1、0.5–0.8→2、≥0.8→3（硬顶 3；loop 开着但没报渴望按 1 棒）。
- **独立车道**：不占 `wakeup.max_per_day`、不进 ProactiveGate 90 分钟共享池（投递台账写 `kind='continue_policy' action='rung_delivered'`，不写 `proactive_message/sent`——后者正是闸门的计数行）；自有每会话每日总帽（默认 6 棒已投递）。
- **cancel-on-reply**：任一棒触发前对方任何回复 → 杀全梯（复用 self_continue 的 intent 级取消）。
- **安静时段**：23:30–08:00 触发的棒顺延到 08:00 且棒数保留（排程时与处理时双重顺延，顺延时同步推 expires_at）。
- **她的沉默** = 提前收梯（台账记 silence 便于观察误判率），不消耗当日棒数、不 mute loop。
- **跑完没回** → 该话题当日不再挂梯（mute 当日 loop）。
- **LLM 不可用** → 棒顺延 5 分钟重试一次，不消耗棒数；再失败才放弃。
- 撒娇棒提示词（`chat.coax_prompt`）必须挂在具体话题钩子上，`allow_voice=False`，文字+表情包。

### 取代关系（不双发）

`_schedule_self_continue` 的「必挂」逻辑被 `continue_policy.should_arm` 取代：无信号即不挂（现状沉默路径）；有信号挂第 1 棒（payload 带 rung/max_rungs/hook/loop_id，origin="coax"）。续写回合机制（唤醒/ephemeral 回合/沉默标记/outbox 投递）原样复用；`chat.self_continue_enabled=false` 仍是总开关；她显式 `schedule_wakeup intent=self_continue`（origin="explicit"）走老提示词、同一车道。

## 配置

全部带默认、零配置可用；`config/low.toml [chat]` 显式化基准值（`continue_policy_enabled` / `coax_intervals_minutes` / `coax_desire_low` / `coax_desire_high` / `coax_max_rungs_per_day` / `coax_rung_llm_retry_minutes` / `busy_minutes`）。standard/remote 用代码默认（同值），不在各 profile 重复——profile 间复制粘贴漂移是已登记的架构债（config 消漂移专项处理）。`coax_prompt` 同 `self_continue_prompt` 先例留在 runtime 代码里。

## 台账

`decision_log kind='continue_policy'`，actions：`ladder_armed / ladder_skipped / cancelled_by_reply / rung_scheduled / rung_fired / rung_delivered / rung_deferred / rung_cancelled / ladder_exhausted / quiet_deferred / stop_flag_set / suppressed / rung_skipped`。跑两周真实数据再调阈值（渴望分档、每日帽、busy 时长）。

## 后果

- **正**：判断前移到花钱之前；撒娇有梯度（1→10→30 分钟）；对方明确表态后全通道安静，不再有「说了别聊还分享」的事故面。
- **负**：LLM 需要新学会两个顶层标签（TOOL_SCHEMA 已说明，缺省=不挂，保守方向失败）；台账新增一种 kind，晨报统计要跟进。
- **不做**：不加 LLM 调用、零 schema 变更（SCHEMA_VERSION 保持 9）、不动 ProactiveGate 本身（它继续管仪式/分享的共享池）。

## 验收

全量回归基线 1158 + 新增 35（`tests/test_continue_policy.py`：引擎全组合 / parse 标签 / runtime 接线）全绿；`test_agent_loop.py` 6 处语义变更点同步改写（必挂→信号挂、llm_down→顺延、90 分钟池→绕过+仪式仍受管、台账 kind 迁移）。docs_index 通过。
