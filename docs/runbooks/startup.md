---
title: 上线启动手册（给新 agent）
type: runbook
status: active
tags: [ops, startup, napcat, tts, agent, runbook]
updated: 2026-10-25
---

# 上线启动手册（给新 agent）

> 目标读者：接手运维的**新 agent**（或人）。照着做就能把大肥鱼全套服务从冷启动到在线，无需询问。
> 本手册全部步骤都在 2026-10-25 实际踩坑验证过；每一条「坑」都是当天真实发生过的故障。

## 0. 前提与关键路径

| 项 | 值 |
|---|---|
| 仓库 | `<PROJECT_ROOT>`（仓库根，所有命令 cwd 在这里；下文用相对路径） |
| 运行解释器 | 本机 Python 3.11（PATH 里的 `python`）；TTS/向量服务用 `.venv-indextts` / `.venv-cosyvoice` |
| 秘钥 | `.env`：`QQ_ACCESS_TOKEN`（NapCat WS 鉴权）、`FATFISH_OPENCODE_KEY`（她的 LLM；旧名 `OPENCODE_API_KEY` 兼容） |
| QQ 凭据 | NapCat 用 `vendor/napcat` 的 onekey 启动（首次手机扫码，之后可快速登录）；Qzone 用 `data/qzone_cookies.json`（过期重登见 §5） |
| 配置 | `config/low.toml` 是 base（`[qzone]` 已激活、`[share]`、`[activity]` 节在此）；本机个性化值放 `config.local.toml`（不入库） |
| 日志 | `data/logs/`（stack / napcat / run / qq-adapter 各一份 console 日志） |

## 1. 启动顺序（严格按此序）

### 步骤 0：清场——桌面 QQ 必须先死

NapCat 的便携 QQ 与桌面版 QQNT **互斥**（同账号族），且 QQNT 被关后会**自动重启**（有看门狗）。

```bash
cmd //c "taskkill /IM QQ.exe /F"     # Git Bash 里必须双斜杠或包 cmd /c，单斜杠会被路径转换毁掉
tasklist /FI "IMAGENAME eq QQ.exe" /NH | grep -c "QQ.exe"   # 必须输出 0 再继续
```

### 步骤 1：NapCat（她的 QQ 通道）

```bash
python -c "
import json, subprocess
uin = json.loads(open(r'data/qzone_cookies.json', encoding='utf-8').read())['uin']
subprocess.Popen(['cmd', '/c', r'scripts\start_napcat.bat', '-q', uin],
    cwd='.', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=8)"
```

- **detached 启动的输出为空 ≠ 没跑**（本手册曾因此误判两次），以端口为准。
- 验证：`Get-NetTCPConnection -State Listen -LocalPort 3001` 有监听（约 30–60 秒内出现）。
- **WS 握手成功 ≠ 已登录**：连接后发任何 action 若被立刻断开 = 账号还没登录。完成登录：
  1. 浏览器开 `http://127.0.0.1:6099`，token 见 `.../napcat/config/webui.json`（`autoLoginAccount` 已配置自动登录）；
  2. 点「快速登录」（或页面二维码手机扫）；
  3. **手机 QQ 立刻点确认——确认有时效（1–2 分钟），两边要连着做**；
  4. 登录态验证：向 3001 发 `get_login_info` 能回包（发 action 被断=仍未登录）。
- QQ 适配器（start_all 的子进程）会自动重连，无需手动。

### 步骤 2：主服务栈（TTS + 向量 + Web + QQ 适配器）

```bash
python scripts/start_all.py    # detached 启动（Start-Process），日志 data/logs/stack-console.log
```

- **Web 端口是随机的**，去日志找 `Web: http://127.0.0.1:<port>/?token=...`（浏览器会自动弹开）。
- 验证：9880（TTS 懒加载）/ 9881（bge）/ 9882（控制面）在 LISTENING；日志出现 `大肥鱼已启动` 与 `[B站] B站已登录`。

### 步骤 3：自主运行循环（她的「活着」）——⚠️ 只在无 QQ 适配器时才启动

```bash
python -m fatfish run --poll 30    # detached；⚠️ 没有 agent 子命令，用 run
```

- **不要与 QQ 适配器同时跑！** 两者都会 tick 并争抢 outbox：无 handlers 的一方会把 `proactive_message` claim 后 defer 回去（defer 不计次数），把消息**饿死**在队列里（2026-10-25 实测踩坑）。全栈模式下 **QQ 适配器本身就是完整 tick**（唤醒/outbox/轮询全含），`run` 循环只用于无 QQ 的 headless 场景。
- 此进程静默是正常的；验证 `Get-CimInstance Win32_Process` 里有 `fatfish run`。

### 步骤 4：验收清单

- [ ] 端口：3001 / 9880 / 9881 / 9882 / Web 全 LISTENING
- [ ] `python -m fatfish qq status` → `"connected": true`
- [ ] `python -m fatfish qzone doctor --offline` → `full_read: true`
- [ ] outbox 在排空（`python -m fatfish` 无直接命令，用 sqlite 查 `outbox WHERE status='pending'`）
- [ ] 给她发条 QQ 消息，看 `data/logs/stack-console.log` 出现收包与回复

## 2. 停止顺序

```
taskkill //IM QQ.exe /F          # NapCat（先停，适配器会自己退避）
# 杀掉 start_all 与 fatfish run 的 python 进程（按 CommandLine 找）
# 或直接用控制面 9882 的停止接口
```

## 3. 已知坑速查（全部实测踩过）

| 症状 | 真相 | 处置 |
|---|---|---|
| NapCat 启动脚本秒退 | 桌面 QQNT 在跑（还会自动重启） | 步骤 0 杀干净再启；启动器有守卫是故意的 |
| Git Bash 里 `taskkill /IM` 报 usage | MSYS 把 `/IM` 转成路径 | 双斜杠 `//IM` 或 `cmd //c "..."` |
| detached 启动日志 0 字节 | 控制台输出不落重定向句柄，**假象** | 以端口/进程为准 |
| WS 能连、发 action 就断 | **账号未登录**（NapCat WS 先于登录就绪） | §1 步骤 1 的 WebUI 登录流程 |
| `python -m fatfish agent` 不存在 | 子命令叫 `run` | `python -m fatfish run --poll 30` |
| Web 打不开 8765 | start_all 用**随机端口** | 日志里找 `Web: http://...` |
| 她的语音/表情包「延迟送达」 | 夜间 QQ 离线时意图入 outbox，QQ 上线后补送 | 设计如此，非 bug |
| TTS 音高偏高 | 参考音已含 +2 半音，勿再叠加 | `tts.pitch_semitones` 默认 0（T-0026 起） |

## 4. 她的自主行为现状（上线即生效）

- **说说**：desire ≥ 0.7 自动发布、不限次数；< 0.7 进 Web「QQ动态」审批箱；每次发布记 desire 到 `decision_log`。
- **主动私聊**：只有主人（`share_with_owner`）和绑定的姐姐（`share_to_friend`，每日 ≤2）；安静时段 23:30–08:00 顺延。
- **观后感**：默认在聊天里说，不进说说。
- **Qzone 轮询** 30 分钟 / **B站浏览** 120 分钟（去重，不会重复入库）。
- 审计入口：`decision_log`（kind=qzone_post / friend_share / qzone_poll）。

## 5. 内存治理（32 GB 机器实测，2026-10-25）

| 进程 | 常驻内存 | 说明 |
|---|---|---|
| TTS（indextts_server） | **~6.3 GB** | 模型常驻；`idle_unload_seconds`（现 0=永不卸载）改为 1800 可在空闲 30 分钟后释放，代价是下次语音等 ~99s 重载 |
| 向量（embedding_server） | ~1.1 GB | bge-base，CPU 常驻 |
| NapCat 便携 QQ | ~0.6 GB | |
| Web / 控制面 / 适配器 / run 循环 | 各 <150 MB | |

- 巡检命令：`powershell "Get-Process python,QQ | Sort WorkingSet64 -Desc | Select -First 6 Id,ProcessName,@{n='MB';e={[math]::Round($_.WorkingSet64/1MB)}}"`
- 富余 < 6 GB 时优先处理：TTS 空闲卸载（改 `[tts] idle_unload_seconds`）→ 重启 TTS。
- 严禁同时跑：TTS + ComfyUI（8GB 显存互斥，见 [系统架构](../overview/architecture.md)）；ComfyUI 用完即卸。

## 6. 常见维护

- **Qzone cookie 过期**：`data/qzone_cookies.json` 删除 → 浏览器登 qzone.qq.com 取 cookie → `python -m fatfish qzone login --cookie "..."`，或用 `QQ空间登录.bat` 扫码。
- **NapCat 登录态失效**：WebUI（6099）重新快速登录，或删 `napcat` config 后重跑 `scripts/setup_napcat.ps1` 扫码。
- **B站换号**：双击 `B站登录.bat`。
- **改配置**：`config/low.toml`（base）改完重启 start_all 与 run 循环生效。
