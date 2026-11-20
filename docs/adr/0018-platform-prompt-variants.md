---
title: ADR-0018 平台提示词变体：按入口能力装配稳定前缀
type: adr
status: accepted
tags: [adr, prompt, context, platform, capability, cache, desktop, qq]
updated: 2026-11-15
---

# ADR-0018：平台提示词变体（platform prompt variants）

> **boss 拍板（2026-11-15）**：
> 1. moss 生图输出上限 **10000** token（含思考强度）；常规回复上限 **4000**；**夜间日报也一起放开**。
> 2. 增加截断观测（`finish_reason`）。
> 3. **降低成本描述、下调渴望阈值、不设每日数额限制**。
> 4. 生图**只用云端**（ModelScope API-Inference）。
> 5. 桌面**自动念**（气泡由本地 TTS 读出），允许据此改提示词。
> 6. 群聊最小集**不保留 voice**。
> 7. 漫游房间**全能力**。
> 8. **首版全上**四个变体。

## 0. 一句话

同一个「大肥鱼」内核，按入口能力装配不同的**稳定前缀**：桌面窗口拿到「全能力 − 发语音 + 桌面规则」，QQ 群只拿聊天 + 表情包 + 群规则，漫游房间拿全能力 + 漫游规则；运行时再按进程能力（QQ adapter 能否读历史、拍照/唱歌服务是否在线）做一次过滤。**提示词承诺的动作 = 运行时真正允许的动作**，前缀缓存按变体隔离。

## 1. 背景

- 固定前缀 ≈9.5k 字符（≈4.3k token），其中工具说明 6.4k 字符（≈2.9k token）占 2/3。
- 不是每个入口都能执行全部 19 个动作：群聊被 policy 拒绝写操作/语音，官方 QQ Bot 没有历史消息接口，桌面壳自己用本地 TTS 念气泡（再发语音是隐藏的第二通道）。
- 模型看到自己不能用的动作会先承诺再被拒，浪费 token 也伤体验；群聊每轮还背着一整份主人私聊的工具清单。
- 前缀是整条 message 级缓存键，变体必须**逐字节稳定**，否则缓存命中率归零。

## 2. 决策

### 2.1 三层解析

```text
入口 platform（adapter 显式传 / session 兜底推断 / 默认 full）
  → variant（full / desktop / qq_group / roam_room）
  → capabilities（VARIANT_CAPABILITIES）
  → runtime filter（qq_history / photo / sing 可用性）
  → stable_prefix(capabilities) 逐字节固定
```

- `Runtime.handle_user_message(..., platform=None)`：QQ Bot adapter 传 `qq_private` / `qq_group`；桌宠链路传 `desktop`；CLI/旧调用不传时按 session 形状推断，活跃漫游 trip 升级为 `roam_room`。
- 未知入口一律 `full`，行为与改造前完全一致。

### 2.2 变体与能力

| variant | 入口 | 能力 |
|---|---|---|
| `full` | QQ 私聊（NapCat/官方 Bot）、CLI、未知 | 全部动作（含 `send_voice`） |
| `desktop` | 桌宠 `desktop:owner` | 全能力 **− send_voice** + `rules.desktop`（气泡会被本地 TTS 念出） |
| `qq_group` | QQ 群 | `chat` + `sticker` + `rules.group`（无 voice、无记忆写入、无分享/空间/读聊天/漫游/媒体/拍照/唱歌） |
| `roam_room` | 漫游中的目标房间 | 全能力 + `rules.roam` |

能力清单与动作映射在 `fatfish/prompt_variants.py`；提示词块在 `fatfish/prompt_blocks.py`。

### 2.3 执行侧对齐

`PolicyEngine.validate(..., allowed=...)` 增加能力门，顺序在 **trust/安全校验之后**（群聊 audition 仍报 `group_not_allowed`，不会把"不被允许"说成"平台不支持"）。`execute_actions` 由 runtime 传入本回合的允许动作集，越权动作返回 `unsupported_on_platform` 且不写历史。

### 2.4 运行时能力过滤

`filter_capabilities()` 在构建前缀前去掉当前进程无法兑现的能力：

| 能力 | 条件 |
|---|---|
| `qq.read` / `qq.talk` | `runtime.qq` 必须是实现了 `fetch_friend_history` / `fetch_group_history` / `fetch_forward_messages` / `fetch_group_members` 的 adapter（NapCat/OneBot） |
| `photo` | `photo.enabled` |
| `sing` | `sing.enabled` |

> **官方腾讯 QQ Bot 接口现状（2026-11-15 核对代码）**：官方 API 只有事件推送 + 发消息（`/v2/users/*/messages`、`/v2/groups/*/messages`），**没有拉取历史消息的端点**。`read_chat` / `talk_to` / 上线补回都建立在 NapCat(OneBot) 的 `get_friend_msg_history` 等扩展上。`platforms/qqbot.py` 的官方 adapter 明确"no read_chat back-reference"；用官方 Bot 时 `runtime.qq=None`，上述能力会被过滤出提示词（不会承诺做不到的事）。要用读聊天，必须走 NapCat 适配器。

### 2.5 缓存与观测

- `PromptBundle.variant` 记录本回合变体；结果 `context.variant` 可直接在日志里看。
- prefix hash 按 `variant + capability_signature` 分键写 meta（`last_stable_prefix_hash:<key>`），不同变体互不误报缓存断裂。
- `prompt_prefix_hit` metric 的 tags 带 `variant`；`llm_cache_hit_tokens` / `llm_json_repair` 可按变体聚合。
- 全能力前缀（`full`）由 `tests/fixtures/stable_prefix_full.txt` golden 测试钉住，任何非预期改动都会红。

### 2.6 同批落地的输出/阈值变更

| 项 | 变更 |
|---|---|
| `llm.max_output_tokens` | 800 → **4000**（low/standard/remote；cloud 继承 low） |
| `photo.build_prompt` 输出上限 | 900 → **10000** |
| `memory.nightly_voice_max_tokens` | 500/350 → **4000** |
| `finish_reason` | `LLMResult` 暴露；`length` 时记 `llm_output_truncated` |
| 成本话术 | 删掉 make_photo/sing 的"占显卡/一两分钟"、watch 的"每天 3 条"、share 的每日上限等劝退文案 |
| 渴望阈值 | `self_action` 0.35/0.20/0.10 → **0.15/0.05/0.06**；`qzone.auto_post_desire_threshold` 0.7 → **0.45**；`chat.coax_desire_low/high` 0.5/0.8 → **0.35/0.6** |
| 每日数额限制 | self_action / watch / share / activity / roam / react 梯 / wakeup / voice 的每日上限全部归零 = **不限**（cooldown、每小时上限、安静时段、每轮动作数保留） |
| 生图 | `[photo] mode` = **modelscope**（low 与 cloud），本机 ComfyUI 路径保留可改回 |

## 3. 后果

- **收益**：群聊每轮固定前缀从 ≈9.5k 字符降到 ≈3.3k（省 ≈2.8k token）；桌面/漫游只拿自己用得上的动作；不会再出现"承诺了做不到"的动作。
- **成本**：输出上限提高、配额归零后 token 消耗上升（boss 明确可负担）；四种变体各有一份前缀缓存。
- **风险/约定**：
  - 改 `fatfish/prompt_blocks.py` 或人格内容会让 golden 测试红，属预期；更新 fixture 需人工确认。
  - persona 内容仍在 DB（`PERSONA_VERSION` 不动），变体只影响拼装。
  - 新增动作时三处同步：`ACTION_BLOCKS`/`ACTION_ORDER`、`CAPABILITY_ACTIONS`、`ACTION_SPECS`。
  - 官方 Bot 与 NapCat 的能力差异由 `filter_capabilities` 处理，新增依赖 adapter 的动作必须登记到 `RUNTIME_GATED`。

## 4. 验收

- `tests/test_prompt_variants.py`：golden 前缀、四变体内容、能力映射、runtime 过滤、能力门。
- `tests/test_config_profiles.py`：profile override 快照同步。
- 全量 unittest 基线（`python scripts/test_baseline.py`）全绿。
- 真实网关实测：常规回复 4000 / 长输出 / moss 生图 prompt 10000 的 usage 与 `finish_reason`（见 `docs/branches/platforms.md` 补记）。
