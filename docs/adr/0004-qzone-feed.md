---
title: ADR-0004 QQ空间动态：读路径隐私分级 + 审批制写路径
type: adr
status: accepted
tags: [adr, qzone, privacy, approval, outbox, publish, schema, cookie]
updated: 2026-10-24
---

# ADR-0004：QQ空间动态——读路径隐私分级 + 审批制写路径

> **实现进度（2026-10-24）**：读路径（T-0006，schema v7）与写路径（T-0007，schema v8）均已落地：`fatfish/qzone/{client,collector,posts}.py`、`qzone_posts` 表、`post_qzone` 动作、Web/CLI 审批流、outbox `qzone_post` 内建 handler、出站脱敏补 qzone cookie 字段。候选端点未实测——真实可达性留给 `fatfish qzone doctor` 与真实 cookie 验证。

## 背景

QQ 空间是主人自己的社交面，也是最容易被「她说了不该说的话」烧掉的出口：动态是公开的、可转发的、有主人实名与社交关系绑定的。读与写的风险完全不对称——

- **读**（刷好友动态）：错的只是「她知道了什么」，内容进本地库，有隐私闸门兜底；
- **写**（发说说）：错的会永久留在他人的时间线上，截图、转发都收不回来。

因此写路径的设计目标是：**LLM 可以起草，但永远不能直接发布；每一次发布都要有主人的明确决定，且全程可审计。**

## 决策

### 1. 独立 cookie HTTP 客户端，而非 NapCat

NapCat（OneBot v11）只实现了消息收发，**没有 Qzone 的 OneBot action**（无 `send_qzone_*`）；QQ 空间的 CGI（`emotion_cgi_*`、`app_publish_qzonetoken.fcgi`）走的是 user.qzone.qq.com 的 cookie + g_tk 通道，与 OneBot 的 WebSocket 协议是两个世界。

所以沿用 T-0006 读路径的 `QzoneClient`（纯 `urllib`，cookie JSON 文件 + 环境变量覆盖 + 指数退避），在其上补：

- `fetch_qzonetoken()`——`app_publish_qzonetoken.fcgi`（候选端点）；
- `publish_text()`——`emotion_cgi_publish_v6` POST（候选端点），`_Callback` JSONP 容错解析；
- `post_form()`——与 GET 共用限速/退避/Cookie 头。

**风险**：两个候选端点都是社区流传形态，字段可能漂移。所有解析都是容错的、fixture 测试的；真实可达性由 `fatfish qzone doctor`（--online）与首次真实发布验证，不进测试套件。失败走 outbox 重试（≤3 次），终态 `failed` 落 decision_log，不会反复打扰腾讯风控。

### 2. 审批制发布（默认必须人工批准）

状态机（`qzone_posts.status`）：

```
draft ──批准──> approved ──handler──> posted
  │                │
  │                └─重试耗尽──> failed
  ├──拒绝──> rejected
  └──敏感内容命中──> blocked（永不发布）
```

- **唯一入口**：`post_qzone` 动作（policy：`min_trust=PRIVATE`、`max_calls=1`、`write=True`、`required_text='content'`）只创建 `draft`，不碰网络；
- **唯一出口**：`review_post(id, approved|rejected)`（Web `/api/qzone/review`、CLI `fatfish qzone approve/reject`）把 draft 送进 outbox；
- **执行**：outbox `qzone_post` handler 是唯一触网者，且只发布 `status='approved'` 的行——status 检查就是重复投递保护，lease 恢复后重放不会发两条；
- `require_approval=false` 是显式 opt-out：草稿直接入 outbox，但**仍受配额与安静时段约束**；`read_only=true`（默认）时无论怎么配都不发。

### 3. 隐私分级延续读路径

读路径的边界（`author_scope` → `origin_scope='qzone'/'qzone_friend'`，好友内容默认不进 LLM）不变。写路径在其上加一道**出站闸**：

- `_CREDENTIAL_PATTERNS` 补 qzone cookie 字段（`skey` / `p_skey` / `p_uin` / `luin` / `qzonetoken` / `qzone_cookie` 的赋值形态），与 SESSDATA 等同一套正负例测试；
- 草稿命中凭证模式或 `secret_notes` 原文 → 落 `blocked` 行（存**脱敏后**文本，原始值不落库）+ decision_log，动作返回失败。

### 4. 配额与熔断

- `qzone.max_posts_per_day = 1`（activity.md 规定）；**批准即占当日名额**（`approved` 与 `posted` 都计入），防止一天批准两份草稿排队超发；配额超限拒绝时 draft 保留（主人可以明天再批），不静默删除；
- 安静时段 23:30–08:00（`qzone.quiet_hours_start/end`）：免审路径入 outbox 时 `available_at` 顺延到时段结束；审批路径不等待主人，但 handler 沿用 outbox 的可用时间语义；
- 指数退避由 `QzoneClient` 统一承担，handler 失败走 `mark_failed`（1 首发 + 3 重试，终态 dead/failed），不引入新的重试实现。

### 5. schema v8：`qzone_posts`

`id`、`content`、`media_json`（本期预留，图片上传明确不做）、`status`、`created_by`、`session_id`、`trace_id`、`idempotency_key`（UNIQUE）、`post_result_json`（远端 tid/url）、`error`、`created_at`、`decided_at`、`posted_at`。迁移是 photo_jobs 式的表级 fallback（v7 原地升级），幂等，`test_p0` 有 v7→v8 用例。

## 备选

- **NapCat 扩展**：等上游支持 Qzone action 再接。放弃：无排期，且审批流与传输层无关，后端换 NapCat 时只需换 `publish_text` 实现。
- **直接发布 + 事后撤回**：撤回依赖另一组未验证端点，且截图不可撤。放弃。
- **白名单关键词自动发布**（如主人说「发出去吧」就跳过审批）：把「同意」压进一句话太脆，主人可能对空气说话。放弃——审批留在 Web/CLI 的显式按钮上。

## 后果

- 主人多一个日常动作：草稿箱里有东西要点一下。这是设计要付的价格，不是缺陷。
- 候选端点漂移时表现为 `failed` 终态 + decision_log，有明确故障信号，不会静默半发。
- 图片说说、QQ 评论/点赞/私信仍不做（activity.md 边界）；`media_json` 只预留。
