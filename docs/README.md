---
title: 文档地图
type: index
status: active
tags: [index, navigation, docs]
updated: 2026-10-25
---

# 大肥鱼文档地图

> 一句话：**总览看状态，分支看领域，ADR 看为什么，运维看怎么做。**
> 旧文档（43 篇）已归档到本机外部归档盘（路径不入库），主开发仓历史中保留全文。

## 三条阅读路径

| 你是 | 从这里开始 |
|---|---|
| 第一次接手 | [系统架构](overview/architecture.md) → [路线图](overview/roadmap.md) → [运维](branches/ops.md) |
| 改语音/唱歌 | [语音](branches/voice.md) → [唱歌](branches/singing.md) → [ADR-0002](adr/0002-singing-soulx.md) |
| 改记忆/召回 | [记忆](branches/memory.md) → [ADR-0003](adr/0003-memory-hierarchy.md) |

## 状态图例

| 标记 | 含义 |
|---|---|
| ✅ active | 已实现并在用 |
| 🟠 in-progress | 正在做，结果未定 |
| 🟡 draft / degraded | 有实现但未验收，或已知有问题 |
| ⬜ planned | 只有设计，未开工 |
| ⛔ deprecated | 已废弃，仅作记录 |

## 文档索引

<!-- DOCS-INDEX:START -->
| 文档 | 类型 | 状态 | 标签 | 更新 |
|---|---|---|---|---|
| [系统架构](overview/architecture.md) | overview | ✅ active | `architecture`, `runtime`, `dataflow`, `ports`, `modules` | 2026-11-03 |
| [项目全景地图（存放规范·功能图·节点图·代码流程）](overview/project-map.md) | overview | ✅ active | `map`, `layout`, `conventions`, `architecture`, `dataflow`, `ports`, `release` | 2026-11-03 |
| [路线图](overview/roadmap.md) | overview | ✅ active | `roadmap`, `priorities`, `singing`, `memory`, `activity`, `packaging` | 2026-10-26 |
| [活动规划（QQ 动态 / B 站观看）](branches/activity.md) | branch | ✅ active | `activity`, `qq-zone`, `bilibili`, `permissions`, `autonomy`, `scheduler`, `decision-turn` | 2026-10-25 |
| [资产地图](branches/assets.md) | branch | ✅ active | `assets`, `models`, `data`, `archive`, `disk`, `cleanup` | 2026-10-24 |
| [媒体能力（表情包 / 拍照 / 词库）](branches/media.md) | branch | ✅ active | `sticker`, `photo`, `comfyui`, `lexicon`, `media`, `vram` | 2026-10-30 |
| [记忆系统](branches/memory.md) | branch | 🟠 in-progress | `memory`, `sqlite`, `rag`, `tags`, `entities`, `people`, `nightly`, `recall` | 2026-11-01 |
| [运维手册](branches/ops.md) | branch | ✅ active | `ops`, `runbook`, `ports`, `vram`, `backup`, `nightly`, `tests`, `git`, `night-test` | 2026-10-30 |
| [平台接入](branches/platforms.md) | branch | ✅ active | `qq`, `napcat`, `onebot`, `bilibili`, `qzone`, `comments`, `web`, `cli`, `permissions` | 2026-11-01 |
| [运行时与人格](branches/runtime.md) | branch | ✅ active | `runtime`, `context`, `llm`, `actions`, `wakeup`, `life`, `relationship` | 2026-11-01 |
| [安全与权限](branches/security.md) | branch | ✅ active | `security`, `kernel`, `trust`, `policy`, `owner`, `privacy`, `injection` | 2026-11-01 |
| [旋律-歌词适配建议（mimo）](branches/singing-advice.md) | branch | 🟡 draft | `singing`, `prosody`, `melody`, `lyrics`, `mimo`, `advice` | 2026-10-25 |
| [唱歌](branches/singing.md) | branch | 🟠 in-progress | `singing`, `soulx`, `melody`, `prosody`, `lyrics`, `svs`, `gpu`, `remote` | 2026-10-30 |
| [语音与 TTS](branches/voice.md) | branch | ✅ active | `voice`, `tts`, `indextts25`, `reference-audio`, `pitch`, `gpu`, `latency` | 2026-10-25 |
| [ADR-0001 产线 TTS 选用 IndexTTS-2.5](adr/0001-indextts25.md) | adr | ✅ accepted | `adr`, `voice`, `tts`, `indextts25` | 2026-10-25 |
| [ADR-0002 唱歌选用 SoulX-Singer 与规则旋律](adr/0002-singing-soulx.md) | adr | ✅ accepted | `adr`, `singing`, `soulx`, `melody`, `svs` | 2026-10-24 |
| [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md) | adr | ✅ accepted | `adr`, `memory`, `tags`, `entities`, `rag`, `recall`, `schema`, `research`, `details`, `dream` | 2026-11-01 |
| [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md) | adr | ✅ accepted | `adr`, `qzone`, `privacy`, `approval`, `outbox`, `publish`, `schema`, `cookie` | 2026-10-24 |
| [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md) | adr | ✅ accepted | `adr`, `qzone`, `desire`, `autonomy`, `proactive`, `wakeup`, `schema`, `approval` | 2026-10-25 |
| [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md) | adr | ✅ accepted | `adr`, `qq`, `share`, `autonomy`, `outbox`, `scope`, `privacy`, `approval` | 2026-10-25 |
| [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md) | adr | ✅ accepted | `adr`, `runtime`, `autonomy`, `wakeup`, `self-continue`, `bypass`, `cost`, `scope` | 2026-10-25 |
| [ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制](adr/0008-catchup.md) | adr | ✅ accepted | `adr`, `qq`, `catchup`, `reconnect`, `quiet-hours`, `outbox`, `audit` | 2026-10-31 |
| [ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）](adr/0009-continue-policy.md) | adr | ✅ accepted | `adr`, `runtime`, `autonomy`, `wakeup`, `continue-policy`, `coax`, `desire` | 2026-10-26 |
| [ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行](adr/0010-sing-action.md) | adr | ✅ accepted | `adr`, `singing`, `soulx`, `action`, `vram`, `tts`, `schema` | 2026-10-26 |
| [ADR-0011 QQ 空间免扫码续期：借本机浏览器的登录态](adr/0011-qzone-browser-renew.md) | adr | ✅ accepted | `adr`, `qzone`, `cookie`, `browser`, `cdp`, `risk-control` | 2026-10-29 |
| [ADR-0012 在线层与能力层分离（云主机试用）](adr/0012-online-tier.md) | adr | 🟠 proposed | `adr`, `ops`, `deployment`, `cloud`, `availability`, `sqlite`, `remote-profile` | 2026-10-29 |
| [ADR-0013 外访会话：主人授权下的跨会话主动聊天](adr/0013-session-visit.md) | adr | ⛔ superseded | `adr`, `runtime`, `visit`, `cross-session`, `context`, `privacy`, `summary` | 2026-11-01 |
| [ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主](adr/0014-session-roaming.md) | adr | ✅ accepted | `adr`, `runtime`, `roaming`, `multi-session`, `attention`, `memory`, `privacy` | 2026-11-01 |
| [ADR 索引](adr/README.md) | adr | ✅ active | `adr`, `decisions` | 2026-11-01 |
| [上线启动手册（给新 agent）](runbooks/startup.md) | runbook | ✅ active | `ops`, `startup`, `napcat`, `tts`, `agent`, `runbook` | 2026-10-25 |
<!-- DOCS-INDEX:END -->

## 标签索引

<!-- DOCS-TAGS:START -->
| 标签 | 文档 |
|---|---|
| `action` | [ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行](adr/0010-sing-action.md) |
| `actions` | [运行时与人格](branches/runtime.md) |
| `activity` | [路线图](overview/roadmap.md), [活动规划（QQ 动态 / B 站观看）](branches/activity.md) |
| `adr` | [ADR-0001 产线 TTS 选用 IndexTTS-2.5](adr/0001-indextts25.md), [ADR-0002 唱歌选用 SoulX-Singer 与规则旋律](adr/0002-singing-soulx.md), [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md), [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md), [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md), [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md), [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md), [ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制](adr/0008-catchup.md), [ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）](adr/0009-continue-policy.md), [ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行](adr/0010-sing-action.md), [ADR-0011 QQ 空间免扫码续期：借本机浏览器的登录态](adr/0011-qzone-browser-renew.md), [ADR-0012 在线层与能力层分离（云主机试用）](adr/0012-online-tier.md), [ADR-0013 外访会话：主人授权下的跨会话主动聊天](adr/0013-session-visit.md), [ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主](adr/0014-session-roaming.md), [ADR 索引](adr/README.md) |
| `advice` | [旋律-歌词适配建议（mimo）](branches/singing-advice.md) |
| `agent` | [上线启动手册（给新 agent）](runbooks/startup.md) |
| `approval` | [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md), [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md), [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md) |
| `architecture` | [系统架构](overview/architecture.md), [项目全景地图（存放规范·功能图·节点图·代码流程）](overview/project-map.md) |
| `archive` | [资产地图](branches/assets.md) |
| `assets` | [资产地图](branches/assets.md) |
| `attention` | [ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主](adr/0014-session-roaming.md) |
| `audit` | [ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制](adr/0008-catchup.md) |
| `autonomy` | [活动规划（QQ 动态 / B 站观看）](branches/activity.md), [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md), [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md), [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md), [ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）](adr/0009-continue-policy.md) |
| `availability` | [ADR-0012 在线层与能力层分离（云主机试用）](adr/0012-online-tier.md) |
| `backup` | [运维手册](branches/ops.md) |
| `bilibili` | [活动规划（QQ 动态 / B 站观看）](branches/activity.md), [平台接入](branches/platforms.md) |
| `browser` | [ADR-0011 QQ 空间免扫码续期：借本机浏览器的登录态](adr/0011-qzone-browser-renew.md) |
| `bypass` | [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md) |
| `catchup` | [ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制](adr/0008-catchup.md) |
| `cdp` | [ADR-0011 QQ 空间免扫码续期：借本机浏览器的登录态](adr/0011-qzone-browser-renew.md) |
| `cleanup` | [资产地图](branches/assets.md) |
| `cli` | [平台接入](branches/platforms.md) |
| `cloud` | [ADR-0012 在线层与能力层分离（云主机试用）](adr/0012-online-tier.md) |
| `coax` | [ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）](adr/0009-continue-policy.md) |
| `comfyui` | [媒体能力（表情包 / 拍照 / 词库）](branches/media.md) |
| `comments` | [平台接入](branches/platforms.md) |
| `context` | [运行时与人格](branches/runtime.md), [ADR-0013 外访会话：主人授权下的跨会话主动聊天](adr/0013-session-visit.md) |
| `continue-policy` | [ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）](adr/0009-continue-policy.md) |
| `conventions` | [项目全景地图（存放规范·功能图·节点图·代码流程）](overview/project-map.md) |
| `cookie` | [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md), [ADR-0011 QQ 空间免扫码续期：借本机浏览器的登录态](adr/0011-qzone-browser-renew.md) |
| `cost` | [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md) |
| `cross-session` | [ADR-0013 外访会话：主人授权下的跨会话主动聊天](adr/0013-session-visit.md) |
| `data` | [资产地图](branches/assets.md) |
| `dataflow` | [系统架构](overview/architecture.md), [项目全景地图（存放规范·功能图·节点图·代码流程）](overview/project-map.md) |
| `decision-turn` | [活动规划（QQ 动态 / B 站观看）](branches/activity.md) |
| `decisions` | [ADR 索引](adr/README.md) |
| `deployment` | [ADR-0012 在线层与能力层分离（云主机试用）](adr/0012-online-tier.md) |
| `desire` | [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md), [ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）](adr/0009-continue-policy.md) |
| `details` | [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md) |
| `disk` | [资产地图](branches/assets.md) |
| `dream` | [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md) |
| `entities` | [记忆系统](branches/memory.md), [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md) |
| `git` | [运维手册](branches/ops.md) |
| `gpu` | [唱歌](branches/singing.md), [语音与 TTS](branches/voice.md) |
| `indextts25` | [语音与 TTS](branches/voice.md), [ADR-0001 产线 TTS 选用 IndexTTS-2.5](adr/0001-indextts25.md) |
| `injection` | [安全与权限](branches/security.md) |
| `kernel` | [安全与权限](branches/security.md) |
| `latency` | [语音与 TTS](branches/voice.md) |
| `layout` | [项目全景地图（存放规范·功能图·节点图·代码流程）](overview/project-map.md) |
| `lexicon` | [媒体能力（表情包 / 拍照 / 词库）](branches/media.md) |
| `life` | [运行时与人格](branches/runtime.md) |
| `llm` | [运行时与人格](branches/runtime.md) |
| `lyrics` | [旋律-歌词适配建议（mimo）](branches/singing-advice.md), [唱歌](branches/singing.md) |
| `map` | [项目全景地图（存放规范·功能图·节点图·代码流程）](overview/project-map.md) |
| `media` | [媒体能力（表情包 / 拍照 / 词库）](branches/media.md) |
| `melody` | [旋律-歌词适配建议（mimo）](branches/singing-advice.md), [唱歌](branches/singing.md), [ADR-0002 唱歌选用 SoulX-Singer 与规则旋律](adr/0002-singing-soulx.md) |
| `memory` | [路线图](overview/roadmap.md), [记忆系统](branches/memory.md), [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md), [ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主](adr/0014-session-roaming.md) |
| `mimo` | [旋律-歌词适配建议（mimo）](branches/singing-advice.md) |
| `models` | [资产地图](branches/assets.md) |
| `modules` | [系统架构](overview/architecture.md) |
| `multi-session` | [ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主](adr/0014-session-roaming.md) |
| `napcat` | [平台接入](branches/platforms.md), [上线启动手册（给新 agent）](runbooks/startup.md) |
| `night-test` | [运维手册](branches/ops.md) |
| `nightly` | [记忆系统](branches/memory.md), [运维手册](branches/ops.md) |
| `onebot` | [平台接入](branches/platforms.md) |
| `ops` | [运维手册](branches/ops.md), [ADR-0012 在线层与能力层分离（云主机试用）](adr/0012-online-tier.md), [上线启动手册（给新 agent）](runbooks/startup.md) |
| `outbox` | [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md), [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md), [ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制](adr/0008-catchup.md) |
| `owner` | [安全与权限](branches/security.md) |
| `packaging` | [路线图](overview/roadmap.md) |
| `people` | [记忆系统](branches/memory.md) |
| `permissions` | [活动规划（QQ 动态 / B 站观看）](branches/activity.md), [平台接入](branches/platforms.md) |
| `photo` | [媒体能力（表情包 / 拍照 / 词库）](branches/media.md) |
| `pitch` | [语音与 TTS](branches/voice.md) |
| `policy` | [安全与权限](branches/security.md) |
| `ports` | [系统架构](overview/architecture.md), [项目全景地图（存放规范·功能图·节点图·代码流程）](overview/project-map.md), [运维手册](branches/ops.md) |
| `priorities` | [路线图](overview/roadmap.md) |
| `privacy` | [安全与权限](branches/security.md), [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md), [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md), [ADR-0013 外访会话：主人授权下的跨会话主动聊天](adr/0013-session-visit.md), [ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主](adr/0014-session-roaming.md) |
| `proactive` | [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md) |
| `prosody` | [旋律-歌词适配建议（mimo）](branches/singing-advice.md), [唱歌](branches/singing.md) |
| `publish` | [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md) |
| `qq` | [平台接入](branches/platforms.md), [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md), [ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制](adr/0008-catchup.md) |
| `qq-zone` | [活动规划（QQ 动态 / B 站观看）](branches/activity.md) |
| `quiet-hours` | [ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制](adr/0008-catchup.md) |
| `qzone` | [平台接入](branches/platforms.md), [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md), [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md), [ADR-0011 QQ 空间免扫码续期：借本机浏览器的登录态](adr/0011-qzone-browser-renew.md) |
| `rag` | [记忆系统](branches/memory.md), [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md) |
| `recall` | [记忆系统](branches/memory.md), [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md) |
| `reconnect` | [ADR-0008 上线扫未回复：掉线期间的漏答消息补回机制](adr/0008-catchup.md) |
| `reference-audio` | [语音与 TTS](branches/voice.md) |
| `relationship` | [运行时与人格](branches/runtime.md) |
| `release` | [项目全景地图（存放规范·功能图·节点图·代码流程）](overview/project-map.md) |
| `remote` | [唱歌](branches/singing.md) |
| `remote-profile` | [ADR-0012 在线层与能力层分离（云主机试用）](adr/0012-online-tier.md) |
| `research` | [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md) |
| `risk-control` | [ADR-0011 QQ 空间免扫码续期：借本机浏览器的登录态](adr/0011-qzone-browser-renew.md) |
| `roadmap` | [路线图](overview/roadmap.md) |
| `roaming` | [ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主](adr/0014-session-roaming.md) |
| `runbook` | [运维手册](branches/ops.md), [上线启动手册（给新 agent）](runbooks/startup.md) |
| `runtime` | [系统架构](overview/architecture.md), [运行时与人格](branches/runtime.md), [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md), [ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）](adr/0009-continue-policy.md), [ADR-0013 外访会话：主人授权下的跨会话主动聊天](adr/0013-session-visit.md), [ADR-0014 多会话漫游：线程记忆、注意力调度与受控自主](adr/0014-session-roaming.md) |
| `scheduler` | [活动规划（QQ 动态 / B 站观看）](branches/activity.md) |
| `schema` | [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md), [ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径](adr/0004-qzone-feed.md), [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md), [ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行](adr/0010-sing-action.md) |
| `scope` | [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md), [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md) |
| `security` | [安全与权限](branches/security.md) |
| `self-continue` | [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md) |
| `share` | [ADR-0006 取消评论/点赞，改私聊分享 share_to_friend](adr/0006-friend-share.md) |
| `singing` | [路线图](overview/roadmap.md), [旋律-歌词适配建议（mimo）](branches/singing-advice.md), [唱歌](branches/singing.md), [ADR-0002 唱歌选用 SoulX-Singer 与规则旋律](adr/0002-singing-soulx.md), [ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行](adr/0010-sing-action.md) |
| `soulx` | [唱歌](branches/singing.md), [ADR-0002 唱歌选用 SoulX-Singer 与规则旋律](adr/0002-singing-soulx.md), [ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行](adr/0010-sing-action.md) |
| `sqlite` | [记忆系统](branches/memory.md), [ADR-0012 在线层与能力层分离（云主机试用）](adr/0012-online-tier.md) |
| `startup` | [上线启动手册（给新 agent）](runbooks/startup.md) |
| `sticker` | [媒体能力（表情包 / 拍照 / 词库）](branches/media.md) |
| `summary` | [ADR-0013 外访会话：主人授权下的跨会话主动聊天](adr/0013-session-visit.md) |
| `svs` | [唱歌](branches/singing.md), [ADR-0002 唱歌选用 SoulX-Singer 与规则旋律](adr/0002-singing-soulx.md) |
| `tags` | [记忆系统](branches/memory.md), [ADR-0003 记忆改为事件-细节分层与多级标签](adr/0003-memory-hierarchy.md) |
| `tests` | [运维手册](branches/ops.md) |
| `trust` | [安全与权限](branches/security.md) |
| `tts` | [语音与 TTS](branches/voice.md), [ADR-0001 产线 TTS 选用 IndexTTS-2.5](adr/0001-indextts25.md), [ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行](adr/0010-sing-action.md), [上线启动手册（给新 agent）](runbooks/startup.md) |
| `visit` | [ADR-0013 外访会话：主人授权下的跨会话主动聊天](adr/0013-session-visit.md) |
| `voice` | [语音与 TTS](branches/voice.md), [ADR-0001 产线 TTS 选用 IndexTTS-2.5](adr/0001-indextts25.md) |
| `vram` | [媒体能力（表情包 / 拍照 / 词库）](branches/media.md), [运维手册](branches/ops.md), [ADR-0010 唱歌接入：sing 动作、sing_jobs 与显存串行](adr/0010-sing-action.md) |
| `wakeup` | [运行时与人格](branches/runtime.md), [ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享](adr/0005-share-desire.md), [ADR-0007 对话中 agentic 行为：实时取材旁路 + 单轮计划上的异步续写（不做 ReAct）](adr/0007-agent-loop.md), [ADR-0009 继续对话策略引擎：三信号 + 撒娇阶梯（取代 T-0027 的每轮必挂）](adr/0009-continue-policy.md) |
| `web` | [平台接入](branches/platforms.md) |
<!-- DOCS-TAGS:END -->

## 标签体系

| 维度 | 标签 |
|---|---|
| 类型 | `overview` `branch` `adr` `index` |
| 领域 | `voice` `singing` `memory` `runtime` `platforms` `media` `security` `ops` `assets` `activity` |
| 主题 | `tts` `soulx` `melody` `prosody` `rag` `tags` `qq` `bilibili` `web` `sticker` `photo` `lexicon` `gpu` `vram` `latency` `permissions` |
| 状态 | 用 front matter 的 `status:`，不要塞进 tags |

## 维护规则

1. 新文档必须带 front matter：`title/type/status/tags/updated`。
2. 改动后运行 `python scripts/docs_index.py --write`，CI/提交前用 `--check`。
3. 「当前整体状态」写在 [系统架构](overview/architecture.md)；分支文档只写本领域事实。
4. 决策只进 `adr/`，只增不改；推翻旧决策就新增一条并标注 supersedes。
5. 不再新增 `handover-*.md`。里程碑只更新总览与对应分支。

## 归档

| 位置 | 内容 |
|---|---|
| 本机外部归档盘（路径不入库） | 重组前 43 篇 + MANIFEST；模型/音频/旧仓库等七类资产 |
| 主开发仓 git 历史 | 重组前全部文档的版本快照 |
