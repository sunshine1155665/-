# 桌面壳 B / C / D 备选方案

当前 A：Electron + `pet-shell/renderer`（已提交在 `feat/desktop-pet-shell`）。
下面是换软件/换框架的三条路线，目标都是继续复用本地 TTS、`/local` WS 和鲸鱼娘模型。

## B：Tauri 2 + WebView2（推荐长期）

**做法**

- 新建 `pet-shell-tauri/`，Rust 只做窗口/托盘/WS/文件 IPC；渲染层直接复用 `pet-shell/renderer/`（HTML/CSS/PIXI Live2D）。
- 窗口：`transparent: true`、`decorations: false`、`alwaysOnTop: true`、`skipTaskbar: true`。
- 交互：把 `preload.js` 的 `pet:drag-*`、`pet:opacity`、`pet:shell-size` 改成 Tauri command/event。
- WS：Rust 或前端连 `ws://127.0.0.1:9950/local?token=...`（当前网关已支持 query token）。
- 打包 MSI，体积从 Electron 的 ~250MB 降到 ~10MB。

**优点**：轻、透明/置顶稳定、没有 Electron 的 Chromium 主进程问题。
**缺点**：需要 Rust 工具链 + WebView2；要重写约 200 行窗口桥接。
**工时**：1–2 天（渲染层基本不用改）。

**免费 token 用法**：用免费额度的编码 agent（Gemini CLI / Qwen Code / Codex CLI 等）在 `pet-shell-tauri` 分支上做移植，禁止读 `.env`、`data/`。

## C：Edge/Chrome `--app` + 现有本地服务（最快，今天可用）

**做法**

- 保留 `scripts/pet_shell_server.py`（9951）和 `pet-shell/renderer/`。
- 启动：
  ```powershell
  msedge.exe --app=http://127.0.0.1:9951/renderer/index.html?token=<token>&ws=ws://127.0.0.1:9950/local --window-size=460,760
  ```
- 用 PowerShell/Win32 `SetWindowPos(HWND_TOPMOST)` 置顶；用 `WS_EX_TOOLWINDOW` 隐藏任务栏。
- 用 Edge user-data 目录固定登录/窗口状态。

**优点**：30–60 分钟；不依赖 Electron/Rust；崩溃率最低。
**缺点**：没有真正的透明窗口、无逐像素点击穿透；不能拖动本体；只能当作稳定兜底。
**适合**：先恢复可用性，再决定是否上 B。

## D：成熟 VTuber/桌宠软件（最快获得“好看的动作”，但集成受限）

**D1 VTube Studio（免费版有水印）**

- 载入同一个 `c_0120` 模型；自带表情/热键/物理/嘴型。
- 开本地 API（WebSocket）；云端 `bubble`/情绪触发 hotkey/expression。
- TTS 音频通过 VB-Cable 等虚拟声卡送入 VTube Studio 麦克风，利用它的嘴型同步。
- 气泡/聊天窗需要另一个透明小窗（HTML/Edge）或托盘。

**D2 Warudo（Steam 免费）**

- Unity 驱动，模型/动作/表情更强；有 WebSocket API。
- 但资源占用大，和 TTS/唱歌争 GPU；不适合 2C2G 云 + 本机 8GB 显存长期挂机。

**D3 A8Chann DSH 桌宠 / Live2DViewerEX**

- 现成桌宠，但**不提供可编程控制接口**，无法接我们的云端 WS / 记忆 / 唱歌；只能当纯视觉壳，不推荐。

**优点**：动作/表情/物理成熟，几乎不用自己调。
**缺点**：控制和气泡受限；TTS 路由复杂；D2 资源重；不适合“云端控制、桌宠只做表现层”的目标。

## 建议顺序

1. 想今天稳定：**C**，30 分钟把窗口跑起来。
2. 想继续长期做自己的桌宠：**B**，复用现有 Live2D 渲染层，重写窗口桥接。
3. 只想要成熟动作、不介意软件限制：**D1**，但聊天/气泡仍需外挂。

## 与 git 的配合

- 当前分支：`feat/desktop-pet-shell`
- 已提交：
  - `5898dd7 feat(desktop-pet): live2d shell, owner surface, window controls`
  - `e5a612b fix(desktop-pet): resize whole shell window for zoom`
- `pet-shell/assets/`（模型和 vendor）和 `node_modules/` 被 `.gitignore` 排除；换机时按 `NOTICE.md` 重新获取，**不要把 Cubism Core 提交到公开仓库**。
- 换编码 agent 时只给它 `pet-shell/` + `scripts/pet_*` + `fatfish/pet_link.py`，不要给 `.env`/`data/`。
