# AGENTS.md —— AI 部署与协作手册

> 读者：AI 编码助手（ZCode / Cursor / Claude Code 等）。目标：在**全新的 Windows 机器**上把「鲸鱼娘·大肥鱼」从零部署到可用，并把验收做实。每一步都有命令与期望结果，全部幂等，可重跑。
> 人类用户请看 [README.md](README.md) 的「快速开始」；本手册是给 AI 照着执行的。

## 0. 红线（先读，违反任何一条即停止并报告）

1. **不碰真实数据**：`data/` 下的数据库、cookie、音频日志是用户私有数据——不得读取内容、不得提交、不得粘贴进任何回复/日志/截图。
2. **密钥不落盘不外传**：`.env` 里的 key 只读来配置环境；不得把 key/token/cookie 写进日志、回复、issue、commit。
3. **GPU 重活先过闸门**：任何会加载模型的命令（TTS/唱歌/生图）之前先跑 `python scripts/preflight.py --for tts`（或 `--for svs`）；退出码非 0 = 不许跑。TTS / 唱歌 / ComfyUI / 向量服务**不能同时驻留**显存，渲染与全量测试一律串行。
4. **不得为绕过检查改代码**：测试失败先修环境/修代码，禁止删断言、跳过测试、改生产逻辑迁就环境。
5. **应用本体零第三方依赖**：`fatfish/` 只允许标准库 + SQLite；新依赖进 `requirements.txt`/引擎 venv，不进应用本体。
6. 不联网下载除非用户明确同意；模型文件不进 git（`data/`、`models/`、`vendor/` 均被忽略）。

## 1. 前置检查

| 项 | 要求 | 检查命令 | 期望 |
|---|---|---|---|
| OS | Windows 10/11（Linux 可跑纯聊天/Web） | — | — |
| Python | 3.11.x | `python --version` | `Python 3.11.x` |
| git | 任意近期版本 | `git --version` | 退出码 0 |
| 磁盘 | ≥ 25 GB 空闲（装全部引擎+模型） | `wmic logicaldisk get size,freespace,caption` | — |
| 显卡 | 可选。≥8 GB VRAM 才跑 TTS/唱歌/生图；无卡也能跑纯聊天+Web | `nvidia-smi` | 有则记录显存，无则跳过 GPU 章节 |

缺 Python 3.11：引导用户从 https://www.python.org/downloads/ 安装并勾选 "Add python.exe to PATH"，重开终端后再查。

## 2. 环境搭建

```bash
cd <项目根>

# ① 应用本体依赖（全部可选，按功能装；core 零依赖也能起 Web）
python -m pip install -r requirements.txt
# 国内网络慢可加清华镜像：
# python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

`requirements.txt` 内容：`psutil`（doctor 体检）、`websocket-client`（QQ 适配器）、`qrcode`（B站扫码 PNG）、`edge-tts`（备用云端语音）。期望：全部安装成功，退出码 0。

```bash
# ② 验证应用本体可用（不需要任何 key / 模型）
python -m fatfish init        # 期望：初始化 data/ 与库，退出码 0；重跑幂等
python -m fatfish doctor      # 期望：输出包含 status: OK
python -m fatfish demo        # 期望：无 key 冒烟演示通过，退出码 0
```

```bash
# ③ 引擎 venv（按需创建；哪个引擎装哪个）
python -m venv .venv-indextts    # 语音 IndexTTS-2.5
python -m venv .venv-cosyvoice   # 向量 bge（+ 可选 CosyVoice 备用引擎）
python -m venv .venv-soulx       # 唱歌 YuE2 → RVC
```

引擎 venv 内安装对应引擎仓库的 `requirements.txt`（先把引擎拉下来，见 §3），再补链路依赖：

```bash
.venv-soulx\Scripts\python -m pip install soundfile librosa pyworld
.venv-cosyvoice\Scripts\python -m pip install torch transformers numpy
```

（torch 装 CPU 版即可跑 bge；TTS/唱歌要 CUDA 版，按 pytorch.org 的选择器装。）

## 3. 模型获取（可选，按功能）

权重不进 git。一键脚本 `scripts/fetch_models.py --engine tts|sing|embed` 在规划中（<!-- TODO(boss): 待补链接 --> 提供前按下面手动做；全部幂等，已存在即跳过）。

### 3.1 语音 IndexTTS-2.5（~11 GB → `models/IndexTTS-2.5/`）

```bash
# 任选其一：HF 官方 / ModelScope 备选 / hf-mirror 镜像（国内）
huggingface-cli download IndexTeam/IndexTTS-2.5 --local-dir models/IndexTTS-2.5
# HF_ENDPOINT=https://hf-mirror.com huggingface-cli download IndexTeam/IndexTTS-2.5 --local-dir models/IndexTTS-2.5
```

期望：`models/IndexTTS-2.5/` 下出现配置与权重文件（目录大小 ~11 GB）。
参考音频不在这里：装完资产后自动就位（§5 步骤 1）。

### 3.2 唱歌 YuE2 + RVC（外部安装）

产线 renderer 是 `soulx/song_yue2_rvc.py`，运行环境 `.venv-soulx`（librosa/numpy/soundfile）。

1. 安装 YuE2 到任意目录，确保 `$YUE2_ROOT/.venv/Scripts/yue2.exe`、`$YUE2_ROOT/models/YuE2-3B`、`$YUE2_ROOT/models/YuE2-Vae` 存在。
2. 安装 RVC 到任意目录，确保 `$RVC_ROOT/.venv-train/Scripts/python.exe`、`$RVC_ROOT/convert_audio.py` 存在；准备 `RVC_PTH` / `RVC_INDEX`。
3. 把这四个路径写进 `.env`：`YUE2_ROOT`、`RVC_ROOT`、`RVC_PTH`、`RVC_INDEX`。
4. 自检：`python scripts/preflight.py --for svs --unload-tts`，再点歌。

旧 SoulX 链路已删除；不要在仓库里找 `vendor/SoulX-Singer` 或 `song_v2.py`。

### 3.3 向量 bge-base-zh-v1.5（391 MB → `models/bge-base-zh-v1.5/`）

```bash
huggingface-cli download BAAI/bge-base-zh-v1.5 --local-dir models/bge-base-zh-v1.5
```

### 3.4 NapCat（QQ 通道）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_napcat.ps1
```

期望：`vendor/napcat/` 出现 OneKey 安装目录，`webui.json` 生成；重复执行只补缺、不覆盖已有配置。首次登录：开 `http://127.0.0.1:6099`，token 在 `vendor/napcat/**/config/webui.json`，手机 QQ 扫码（1–2 分钟内确认）。

### 3.5 生图 ComfyUI + LoRA

ComfyUI 独立安装（建议 https://github.com/comfyanonymous/ComfyUI 自带 venv）。装完把 4 个路径填进 `config.local.toml [photo]`（键与写法照抄 `config/example.toml` 注释）。人物 LoRA `deepseek_whalegirl_v1.safetensors` 与表情包 LoRA 来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。表情包现在走魔搭生成，需要 `MODELSCOPE_API_KEY`。

## 4. 配置

1. **密钥（.env）**：`copy .env.example .env` 后至少填一个：

| 变量 | 何时必填 | 说明 |
|---|---|---|
| `FATFISH_OPENCODE_KEY` | 要真聊天 | LLM key；不填则聊天走内置本地兜底（`python -m fatfish demo` 可验证） |
| `QQ_ACCESS_TOKEN` | 要接 QQ | 与 NapCat 配置一致；setup_napcat.ps1 生成后回填 |
| `QZONE_FRIEND_UINS` / `BILIBILI_*` | 可选 | 见 README「环境变量」表 |

2. **本机路径（config.local.toml）**：`copy config/example.toml config.local.toml`，只保留要覆盖的键（ComfyUI 四件套、`tts.ref_audio_path` 等），启动加 `--config config.local.toml`。加载顺序：`config/low.toml` < `FATFISH_PROFILE` 选定 profile < `--config` 文件。**不要把真实路径/QQ 号写进入库的 `config/*.toml`。**
3. **profile 选择**：`low`（默认，集成显卡/无卡）、`standard`（独显）、`remote`（瘦客户端连远程）、`cloud`（在线层）；`FATFISH_PROFILE=standard` 即切换。

## 5. 首启与就绪判据

```bash
# 1) 播种复刻资产（幂等；已存在则打印 skip）
python scripts/install_assets.py
# 期望：copy/skip 三行（cand_home_up1.wav、prompt_v6.json、persona_song.json），退出码 0

# 2) 初始化 + 体检（幂等）
python -m fatfish init        # 期望退出码 0
python -m fatfish doctor      # 期望 status: OK

# 3a) 纯聊天 + Web（无显卡首选）
python -m fatfish serve --open
# 期望：控制台打印 http://127.0.0.1:<port>/?token=...；浏览器打开 Web UI

# 3b) 全家桶
启动大肥鱼.bat        # 或 python scripts/start_all.py
# 期望（按序）：NapCat 3001 检测 → TTS 9880 → 向量 9881 → 控制面 9882 → Web（随机端口）
```

就绪判据（AI 逐项验证）：

| 服务 | 判据 |
|---|---|
| Web | 日志出现 `Web: http://127.0.0.1:<port>/?token=...`，GET `/` 返回 200 且含 `<html` |
| NapCat | 端口 3001 LISTEN；`python -m fatfish qq status` 返回 `"connected": true`（未登录时连上即断，先走 §3.4 扫码） |
| TTS 9880 | `GET http://127.0.0.1:9880/health` 返回 JSON `{"ok": true, ...}` |
| 向量 9881 | `GET http://127.0.0.1:9881/health` 返回 `{"ok": true, "loaded": true, "dim": 768}` |
| 控制面 9882 | `POST http://127.0.0.1:9882/services/tts/stop` 可用（同一服务同时只允许一个 job，连发 409 busy 属正常） |

## 6. 验证清单（部署验收）

```bash
# ① 离线全量测试（不联网、不需要模型；串行约 6–7 分钟）
python -m unittest discover -s tests -p "test_*.py"
# 期望：OK（允许少量 skipped）；失败 = 未通过验收，逐个修

# ② 资产播种
python scripts/install_assets.py --dry-run    # 期望：0 file(s) would be copied（全已就位）

# ③ GPU 引擎自检（有卡才跑）
python scripts/preflight.py --for tts         # 退出码 0 = 可以跑 TTS
python scripts/preflight.py --for svs         # 退出码 0 = 可以跑唱歌

# ④ 端到端冒烟
python -m fatfish demo                        # 无 key 聊天冒烟
# 有 LLM key 时：Web 里发一句「你好」收到回复 = 部署成功
```

## 7. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| TTS 报 `missing_ref_audio_path` | 参考音频没播种 / 路径没配 | 重跑 `python scripts/install_assets.py`；自定义参考音填 `config.local.toml [tts] ref_audio_path` |
| preflight 退出码非 0 | 显存/内存不够或 9880/9881 模型常驻 | 按 `--unload-tts` 提示先卸载；或停掉占卡的进程；**绝不在红牌时强跑** |
| TTS/控制面返回 409 busy | 同一服务同时只允许一个 job | 等上一个 job 结束再发 |
| NapCat 端口在、发 action 即断 | QQ 未登录 | §3.4 的 WebUI 扫码流程 |
| 模型下不全 / 校验失败 | 网络中断 | 重新执行下载命令（huggingface-cli 幂等续传） |
| Web 打不开 8765 | start_all 用随机端口 | 日志里找 `Web: http://...` 那行 |
| `python -m fatfish agent` 不存在 | 子命令叫 `run` | `python -m fatfish run --poll 30`（且不要与 QQ 适配器同时跑，会饿死 outbox） |
| 表情包不生成 | 魔搭/生成配置 | 检查 `MODELSCOPE_API_KEY`、`sticker.generation_enabled`、`sticker_jobs.error`；不会回落旧库 |
| 测试大量失败 | 在错误目录 / 缺 Python 3.11 / 环境变量污染 | 确认 cwd=仓库根、`python --version` 是 3.11；测试必须离线，清掉代理类 env 后重跑 |

## 8. 改代码前（协作约定）

- 工作流、测试纪律、schema 变更流程见 [CONTRIBUTING.md](CONTRIBUTING.md)；架构读 `docs/overview/architecture.md`，决策读 `docs/adr/`。
- issue / PR / 截图里**不得出现**真实聊天内容、cookie、QQ 号、tailscale IP、本机绝对路径（用 `<uin>`、`<PROJECT_ROOT>` 占位）。
