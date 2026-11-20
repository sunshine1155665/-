---
title: 安全与权限
type: branch
status: active
tags: [security, kernel, trust, policy, owner, privacy, injection]
updated: 2026-11-01
---

# 安全与权限

> 上级：[系统架构](../overview/architecture.md) · 相关：[运行时](runtime.md) · [平台](platforms.md) · [活动规划](activity.md)

## 身份内核

`fatfish/kernel.json` + DB 中 `identity_kernel_hash`。启动时算 SHA256 比对，不一致拒绝信任人格。版本号记录在 `schema_meta.identity_kernel_version`。

## 信任分级

`security/trust.py`：GROUP < PRIVATE < OWNER 等。群聊默认降级；写操作要求私聊信任。输入用 `wrap_untrusted` 包成数据信封，输出用 `redact_outbound` 脱敏。

**QQ 记忆边界（2026-10-26 boss 决定）**：QQ 内**不做**群聊/私聊记忆隔离——主人自己控制她加入哪些群，群聊与私聊共享 `memory_items`、`event_details`、短期摘要和元目录；群聊也照常启用 `[记忆核查]`，并同样称呼主人/主人哥哥。测试/夜测/演示数据的读取过滤（`sessions.py`）继续保留，属于数据卫生而非群聊隔离。B 站内容不做记忆隔离；采集器只读（不评论/不点赞/不私信），观后感只在私聊讲给主人，群聊不展开 B 站链接或讨论。

## 动作白名单

`security/policy.py:ACTION_SPECS` 现有 10 个动作，逐个限定：最低信任、每轮次数、是否允许群聊、是否写库、必填字段。**任何新能力必须先在这里登记**，否则 LLM 调用会被 `unknown_action` 拒绝。`sing`（私聊、每轮 1 次、群聊拒绝）会占显存并写 `sing_jobs`/记忆；`post_qzone`（私聊信任、每轮 1 次、写、必填 `content`）是第一个「对外发布」动作（ADR-0004）；ADR-0005 修订其发布裁决：`require_approval=false` 时由她自评的 `desire`（0-1，非法值一律按低于阈值）对照 `auto_post_desire_threshold`（默认 0.7）决定直发或留审批，每次自动发布落 `decided_by='desire-threshold'` + decision_log。`share_with_owner`（私聊信任、每轮 2 次、写、必填 `text`）只调度 wakeup、不直接发送——ProactiveGate 的安静时段/每日配额/最小间隔原样生效。

## 认主 / 好友位

`owner.py:OwnerRegistry`：单主人 + 单个好友位，绑定记录在 `owner_bindings`（当前 2 行：owner / friend）。解绑与好友位管理走 CLI。

## 保密清单

`privacy.py:SecretNotes`，存 `secret_notes` 表；提示词里作为“绝不透露”约束。

## 可靠外发

`outbox.py`：租约 + 重试 + 崩溃恢复；`max_attempts` 与 lease 在 `outbox` 表（`enqueue` 现支持按条目覆盖 `max_attempts`，qzone 发布用 1 首发 + 3 重试）。运行时内建 handler（`_builtin_outbox_handlers`）与平台传入 handlers 合并，`qzone_post` 的 handler 只发布 `status='approved'` 的草稿——状态检查就是重复投递保护。

**主动私聊的唯一出口（ADR-0006）**：`share_to_friend` 的收件人在运行时从 `owner_bindings` 好友位解析（`qq:private:<uin>`），LLM 参数无法指定收件人；无绑定/不可达绑定一律 `no_friend_bound` 拒绝。文本过凭证/secret_notes 闸后才入队（kind `friend_share`），审计行只记元数据不记原文；配额与当日计数同走 decision_log。她的主动私聊对象因此只有两个：主人（`share_with_owner`，回到原会话）与好友位姐姐（`friend_share`，绑定行解析），无第三条路径。

## 出站脱敏（qzone cookie）

`security/trust.py:_CREDENTIAL_PATTERNS` 补 QQ 空间 cookie 字段（`skey` / `p_skey` / `p_uin` / `luin` / `qzonetoken` / `qzone_cookie` 的赋值形态），与 SESSDATA 同一套正负例测试（`tests/test_qzone_policy.py`）。写路径的草稿在落库前命中凭证模式或 `secret_notes` 原文即 `blocked`，存脱敏后文本。

## 已验证

- `tests/test_p0.py`、`tests/eval/injection.jsonl`（注入评测）。
- 写路径校验矩阵与脱敏正负例：`tests/test_qzone_policy.py`、`tests/test_qzone_posts.py`。
- 分享欲模型（desire 阈值/次数不限/原话投递/安静时段）：`tests/test_qzone_posts.py`（DesireThresholdTests）、`tests/test_share_desire.py`。
- 私聊分享（收件人绑定解析/无绑定拒绝/内容闸/配额/深夜顺延/原话投递/主人通道不受影响）：`tests/test_share_to_friend.py`。
- 漫游隐私（ADR-0014）：房间只允许已绑定私聊，群/陌生人拒绝；目标房间 prompt 只带自己的线程卡；
  主人原话细节检索过滤已绑定的他人私聊 session，只保留 `origin_scope='roam'` 摘要/关键事实：`tests/test_roam.py`。
- 历史红队 20/20（2026-10-22 文档口径）。

## 缺口

- 红队语料只有注入一类，未系统化。
- QQ 动态发布已实施（`post_qzone` + 审批流，ADR-0004；desire 阈值自动发布 + `share_with_owner`，ADR-0005）；B 站互动仍是缺口。
- 新增自主面（2026-10-25）的原则边界：自动发布仅限纯文本说说（经脱敏/secret_notes 闸）、主动私聊仅限两个人——主人（wakeup 通道）与好友位姐姐（`share_to_friend`，收件人由绑定行解析，无配置绕过），且都深夜顺延；全部可被配置一键收回（`read_only=true` / `require_approval=true` / `max_posts_per_day>0` / `wakeup.max_per_day` 调低 / `share.max_to_friend_per_day=0` 或解绑好友位）。
- `doctor` 未纳入本次自动验收。

## 原则（新增权限时）

1. 只读优先；写操作必须显式授权 + 频率上限。
2. 所有外发内容可审计（`tool_audit` / `turn_traces`）。
3. 失败降级为文本回复，不阻塞聊天。
