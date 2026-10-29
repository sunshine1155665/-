# 鲸鱼娘·大肥鱼（fatfish）

本地优先的单用户 AI 伴侣 / 虚拟主播：她在 QQ 和本机 Web 里陪你聊天，会发**中文情感语音**（IndexTTS-2.5）、发表情包、**拍照**（ComfyUI）、**唱歌**（SoulX-Singer）、记事、主动找你、读 B 站与 QQ 空间；主人一句话也能让她**代看 QQ 群聊/私聊、找指定的人搭话、看指定的 B 站视频**，她还会在多个会话间**漫游串场**（ADR-0014）。应用本体只用 **Python 标准库 + SQLite**，无前端构建，Windows 优先（Linux 可跑纯聊天/Web）。

- 形态：本机 Web UI + CLI + QQ（NapCat / OneBot v11）；可选本地 GPU 引擎
- 许可：个人非商用源码可见许可（见 [LICENSE](LICENSE)），第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- 详细文档：[`docs/README.md`](docs/README.md)（总览 / 分支 / ADR）

## 快速开始（三步）

> 目标平台 Windows 10/11 + Python 3.11。纯聊天 + Web 只需第 1、2 步。

**① 装 Python 3.11**

从 [python.org](https://www.python.org/downloads/) 安装，勾选 **Add python.exe to PATH**。

**② 拉代码 + 首次准备**

```bash
git clone https://github.com/sunshine1155665/-.git fatfish && cd fatfish
python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt   # 可选依赖（QQ/B站/系统信息）
python scripts/install_assets.py        # 把 assets/ 里的参考音频与唱歌底稿播种到 data/（首启前跑一次）
copy .env.example .env                  # 然后编辑 .env，至少填一个 LLM key
```

**③ 配置 + 启动**

```bash
python -m fatfish init                  # 初始化 data/ 与数据库（幂等）
python -m fatfish doctor                # 体检：Python/依赖/显存/配置
```

- **纯聊天 + Web（无需显卡）**：`python -m fatfish serve --open`，浏览器自动打开本机 Web。
- **全家桶（QQ + 语音 + 向量）**：双击 `启动大肥鱼.bat`（内部跑 `scripts/start_all.py`），它会按序拉起 NapCat 检测、TTS（9880）、向量服务（9881）、控制面（9882）与本机 Web（随机端口，URL 带一次性登录 token 打到日志和浏览器）。
- QQ 收发需要先装 NapCat（下文），并把 QQ 号发给机器人；语音/拍照/唱歌需要对应引擎与显卡（下文）。

> 更省事：把本仓库交给你的 AI 编码助手（ZCode / Cursor 等），让它按 [AGENTS.md](AGENTS.md) 完成部署与验收；人工三步走见上。

## 环境变量（.env）

| 变量 | 必填 | 说明 |
|---|---|---|
| `FATFISH_OPENCODE_KEY` | 聊天必填 | LLM API key（OpenCode Go）；旧名 `OPENCODE_API_KEY` 兼容 |
| `QQ_ACCESS_TOKEN` | 接 QQ 时 | NapCat 正向 WebSocket 鉴权 token |
| `QZONE_FRIEND_UINS` | 可选 | 逗号分隔的好友 QQ 号列表（feeds 回退通道；走 env 是为了不把真实 QQ 号写进配置） |
| `BILIBILI_SESSDATA` / `BILIBILI_BILI_JCT` / `BILIBILI_COOKIE` | 可选 | B 站只读登录（也可用 `B站登录.bat` 扫码） |
| `FATFISH_PROFILE` / `FATFISH_DATA_DIR` / `FATFISH_DB` | 可选 | profile（默认 low）/ 数据目录 / 数据库路径覆盖 |

本机路径与个性化值写进 `config.local.toml`（已被 gitignore），启动时 `python -m fatfish --config config.local.toml serve`；需要填哪些键见 [`config/example.toml`](config/example.toml)。

## 模型与引擎安装（可选，按需）

主仓只放代码；模型走「链接分发、用户自行下载」：GitHub 放代码，HuggingFace / ModelScope 放权重。国内网络可在 `hf-mirror.com` 设 `HF_ENDPOINT` 走镜像。一键获取脚本 `scripts/fetch_models.py --engine tts|sing|embed` 在规划中（<!-- TODO(boss): 待补链接 --> 落地前请按下表手动下载）。

### IndexTTS-2.5（语音，~11 GB，显存 ~6 GB）

- 来源：HuggingFace `IndexTeam/IndexTTS-2.5`（ModelScope 备选同名仓库）<!-- TODO(boss): 待补链接 -->
- 放到：`models/IndexTTS-2.5/`
- 运行环境：`.venv-indextts`（装引擎仓库 requirements + `scripts/indextts_server.py` 头部注释所列依赖）
- 装好后 `python scripts/preflight.py --for tts` 自检，再跑 `启动大肥鱼.bat` 或单独起 `scripts/indextts_server.py`。参考音频已由 `install_assets.py` 播种到 `data/whale_voice_demo_v2/refs/`。

### SoulX-Singer（唱歌，~5.3 GB，显存 ~5.5 GB）

- 来源：`git clone https://github.com/Soul-AILab/SoulX-Singer vendor/SoulX-Singer`（Apache-2.0）；权重 `hf download Soul-AILab/SoulX-Singer --local-dir vendor/SoulX-Singer/pretrained_models`（子目录形状以仓库 README 为准）<!-- TODO(boss): 待核实权重落位 -->
- 运行环境：`.venv-soulx`（torch + 引擎 requirements + `soundfile`/`librosa`/`pyworld`）
- 配置：`config/low.toml [sing]` 已指向 `soulx/song_v2.py` 与 `vendor/SoulX-Singer`；要点歌先 `python scripts/preflight.py --for svs --unload-tts`（唱歌与 TTS 共用 8 GB 卡，必须串行）。

### bge-base-zh-v1.5（向量，391 MB，CPU 即可）

- 来源：HuggingFace `BAAI/bge-base-zh-v1.5` <!-- TODO(boss): 待补链接 -->
- 放到：`models/bge-base-zh-v1.5/`
- 运行环境：`.venv-cosyvoice`（torch + transformers，`scripts/embedding_server.py` 跑在 9881）。表情包检索与记忆向量召回用它；没有它自动退到内置 hash 向量（功能可用，语义相似度变弱）。

### NapCat（QQ 通道）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_napcat.ps1
```

自动下载 NapCat OneKey（pin 在 v4.18.41）+ 便携 QQ 到 `vendor/napcat`，并种好 `ws://127.0.0.1:3001` 配置；token 从 `.env` 的 `QQ_ACCESS_TOKEN` 读。首次登录在 `http://127.0.0.1:6099` WebUI 扫码。

### ComfyUI + 生图模型（拍照）

ComfyUI 建议独立安装（自带 venv，默认端口 8188），把四个路径填进 `config.local.toml [photo]`（见 `config/example.toml`）：`comfyui_python / comfyui_main / comfyui_cwd / models_root`。生图模型：anima-turbo（unet/clip/vae 文件名见配置）；鲸鱼娘人物 LoRA `deepseek_whalegirl_v1.safetensors` 来源 <!-- TODO(boss): 待补链接 -->（AI 模型站 LoRA 多限制再分发，故走链接自备；也可换成任意人物 LoRA，改 `[photo] lora` 即可）。

### 表情包库

图片不入主仓：`python scripts/fetch_stickers.py`（来源 <!-- TODO(boss): 待补链接 -->）。不装也能跑——表情包功能自动降级为「无图可用」。

## 无引擎也能跑

- 没有显卡 / 没下模型：纯聊天 + Web + 记忆 + 词库全部可用（LLM 走 API）；TTS/拍照/点歌在缺资产时通过 `missing_assets` 友好降级，不会崩。
- 完全离线（无 LLM key）：`python -m fatfish demo` 提供无 key 冒烟；聊天走内置本地兜底回复。
- 全量测试不联网、不需要模型：`python -m unittest discover -s tests -p "test_*.py"`（约 6–7 分钟）。

## 目录

| 路径 | 说明 |
|---|---|
| `fatfish/` | 应用代码（零第三方依赖） |
| `soulx/` | 唱歌运行时链路（由 SoulX-Singer 子进程执行） |
| `scripts/` | 启动/服务/运维脚本（`start_all.py`、`preflight.py`、`install_assets.py`…） |
| `web/` | 原生前端（无构建） |
| `tests/` | unittest 全量套件（离线） |
| `config/` | profile（`low` 为 base；`example.toml` 列出全部可填键） |
| `assets/` | 随仓分发的复刻资产（参考音频 + 唱歌底稿） |
| `docs/` | 总览 / 分支 / ADR / runbook |
| `data/`、`models/`、`vendor/` | 本地资产（git 忽略，由脚本/用户填充） |

## 许可证与致谢

本项目源码：[Fatfish Personal Use License](LICENSE)（个人非商用）。
各上游组件（IndexTTS-2.5 / index-tts、SoulX-Singer、bge-base-zh-v1.5、ComfyUI、NapCat 等）许可证各不相同，逐个登记在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)——分发方式均为「链接 + 用户自行下载」，请遵守各上游条款。复刻参考音频 `assets/voice/cand_home_up1.wav` 为项目自制录音素材，非开源件，随仓分发仅用于语音克隆参考。
