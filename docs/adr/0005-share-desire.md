---
title: ADR-0005 分享欲模型：desire 阈值自动发布 + share_with_owner 主动分享
type: adr
status: accepted
tags: [adr, qzone, desire, autonomy, proactive, wakeup, schema, approval]
updated: 2026-10-25
---

# ADR-0005：分享欲模型——desire 阈值自动发布 + share_with_owner 主动分享 + 观后感路由

> **boss 授权记录（2026-10-25）**：三条行为变更由 boss 明确批准——1) 发说说加 `desire` 0-1 自评，超阈值**直接发布、不限每天次数**；2) 允许她主动唤醒找主人聊天分享（走 QQ 主动消息）；3) 观后感类内容默认只在聊天里分享，分享欲特别强才发说说。实现于 T-0023（schema v9）。

## 背景

ADR-0004 定下的审批制写路径（草稿 → 主人批准 → outbox）解决了安全，但也意味着她永远不能自己发一条说说。boss 复盘后认为过度保守：一个「有自己的生活」的伴侣，分享欲应该由她自己计量——强到忍不住的话不需要等审批，弱到随口一提的话本来就该留在聊天里。同时缺少一条「主动找主人分享」的通道：观后感、趣事只能等主人来问。

## 决策

### 1. desire 自评 + 阈值自动发布（核心）

- `post_qzone` 动作新增可选参数 `desire`（0-1 浮点）。ActionSpec 不变（私聊信任、每轮 1 次、write、必填 `content`）。
- `QzonePostService.create_post_draft(content, desire=None)`：`normalize_desire` 把非法值（非数字、越界、bool、NaN）一律归一为 `None`——**缺省/乱填按低于阈值处理，默认走审批**，模型刷分刷不出新权限。
- 当 `require_approval=false` 且 `desire ≥ qzone.auto_post_desire_threshold`（默认 0.7）：跳过审批直接入 outbox（`decided_by='desire-threshold'`）。**安静时段顺延保留**：23:30–08:00 入队的发布顺延到窗口结束（08:00），深夜的分享欲早上再兑现。
- 低于阈值 / `require_approval=true`：仍走 draft 审批箱；`require_approval=true` 时 desire 只记录不生效。这是对 ADR-0004 §2 的修订：`require_approval=false` 不再无条件直发，而是**由阈值裁决**——安全面从「开关全开/全关」变成「她自己诚实申报 + 阈值兜底」。
- 审计：每次自动发布在 `decision_log`（kind `qzone_post`，action `approved`，outcome `desire_threshold`）记录 `desire` 与 `threshold`；`qzone_posts` 新增 `desire REAL`、`decided_by TEXT`（`'owner'` | `'desire-threshold'`），事后可查每一条是怎么发出去的。

### 2. 次数不限

`qzone.max_posts_per_day` 语义扩展：**0 = 不限制**，仅 >0 时生效（`quota_status` 返回 `unlimited` 标记）。三套 profile（low/standard/remote）默认改为 0——boss 明示不限次。发布频率的实际约束由 desire 阈值（她的自我克制）+ 安静时段承担；主人随时可以设回正数恢复硬顶。

### 3. share_with_owner：主动找主人分享

- 新 ActionSpec：`min_trust=PRIVATE`、`max_calls=2`、`allow_group=False`、`write=True`、必填 `text`，可选 `args.desire`（仅记录）。
- `execute_actions` 分支：调 `WakeupScheduler.schedule(now+90s, intent='share_with_owner', payload={'text', 'desire'})`——**不直接发送**，走既有 ProactiveGate 全套约束（安静时段顺延、每日配额 `wakeup.max_per_day`、最小间隔 `proactive_min_interval_minutes`）。90s 取自调度器 60s 最小延迟之上留余量，冲动还在同一语境里。
- **原话投递**（接线结论）：`platforms/qq.py` 的 `proactive_message` handler 本来就发 `payload["text"]`，缺口在 `runtime.process_due_wakeups`——它只按 `reason` 套模板。修复：payload 里有非空 `text` 时**优先发她的原话**，否则回落 `proactive_text_for_reason` 模板。测试覆盖：原话到 QQ、模板路径不受影响、深夜顺延到 08:00 后原话完整送达。
- 三套 profile `[wakeup] max_per_day` 统一放宽到 12（找主人聊天的额度）；安静时段保留——深夜不打扰是底线。

### 4. 观后感路由（提示词层）

TOOL_SCHEMA 措辞：desire 锚点（≥0.8 忍不住必须说 / 0.6-0.8 很想说 / 0.3-0.6 有点想说 / <0.3 随口一提）+ 路由规则——**看视频/动态的观后感、碎碎念默认走 `share_with_owner` 在聊天里说，说说只留给分享欲特别强、值得挂在空间里的时刻**。低于阈值的说说稿仍进审批箱而不是自动发，阈值机制天然兜底措辞失守。

### 5. schema v9

`qzone_posts` 加列 `desire REAL`、`decided_by TEXT`，`db.py` `_ensure_column` 幂等原地升级（不重建表），`test_p0` 有 v8→v9 用例（旧行可读、新列为 NULL）。schema.sql 的建表语句同步带上两列。

### 6. 配置激活（boss 授权）

三套 profile：`[qzone] enabled=true`、`read_only=false`、`require_approval=false`、`auto_post_desire_threshold=0.7`、`max_posts_per_day=0`；`[wakeup] max_per_day=12`。注：`enabled` 同时门控读轮询与写路径（`posts.py` 落草稿前检查），规格书列出的四个键不含它，但不打开它整条写路径是死的——按「激活」的本意一并打开并在此记录，主人不同意改一行即可关回。研发期夜测档曾以 `require_approval=true` 覆盖，夜间测试仍只积草稿、绝不真发。

## 备选

- **模型外置 desire（情绪引擎算分）**：更「客观」，但引入一套新系统，且分享欲本来就是她的主观体验。放弃——自评 + 阈值 + 审计最简单，错了可从 decision_log 校准。
- **自动发布仍限次数（如每天 3 条）**：boss 明示不限次；且配额会制造「到量了再说」的机械感。放弃，保留配置项作为回退。
- **share_with_owner 直接发消息**：绕过 ProactiveGate 会打碎安静时段/限频的全部积累。放弃——复用 wakeup 通道，90 秒延迟对「想分享」无感。

## 后果

- 审批箱不再是唯一的发布口：主人看到的空间动态可能是她自己发的（`decided_by='desire-threshold'`）。Web/CLI 审批卡只对 draft 生效，审计靠 `decided_by` + decision_log。
- 阈值 0.7 的实际校准依赖观察：desire 分布、自动发布频率进 decision_log，跑一段时间后由主人调整。
- `post_qzone` 一次回复仍限 1 条、`share_with_owner` 限 2 条，plus wakeup 每日 12 次总闸——刷屏面收敛。
- 图片说说、QQ 评论/点赞/私信仍不做（`media_json` 预留不变）。
