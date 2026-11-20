# THIRD_PARTY_NOTICES —— 第三方组件登记

本仓库采用「代码入库、权重/资产链接分发」的方式：主仓不打包任何第三方模型权重或图片资产，用户按 README 自行下载。因此分发合规的关键是逐个登记来源与许可证。**使用前请阅读并遵守各上游许可证条款。**

| 组件 | 用途 | 来源 | 版本 / revision | 许可证 | 备注 |
|---|---|---|---|---|---|
| IndexTTS-2.5（模型权重） | 中文情感 TTS | HuggingFace `IndexTeam/IndexTTS-2.5`（ModelScope 备选同名仓库）<!-- TODO(boss): 待补链接 --> | 未固定（<!-- TODO(boss): 待补版本 -->） | 以 HF 模型页标注为准（上游为 bilibili 系模型协议） | ~11 GB；**限制商用再分发**，链接分发规避再打包 |
| index-tts（引擎代码） | TTS 服务封装基座 | https://github.com/index-tts/index-tts | 本地快照 `vendor/index-tts-main`（revision 未记录，<!-- TODO(boss): 待补 -->） | **bilibili Model Use License Agreement**（见 vendor 内 LICENSE 原文）：对月活 >1 亿或年营收 >10 亿元人民币的主体需另行授权；不得用于改进其它 AI 模型（非商用模型除外）；必须保留版权与协议副本 | 代码不出现在本仓（用户自建环境），此处登记以备查阅 |
| YuE2（模型 + 引擎） | 唱歌全曲生成 | 上游 YuE2 仓库/权重（用户自行安装）<!-- TODO(boss): 待补链接 --> | 未固定 | 以模型页标注为准 | 通过 `YUE2_ROOT` 指向本地安装 |
| RVC（代码 + 权重） | 唱歌换声 | 上游 RVC 仓库/权重（用户自行安装）<!-- TODO(boss): 待补链接 --> | `v10g_e30`（本地训练） | 以 RVC 仓库 LICENSE 为准 | 通过 `RVC_ROOT` / `RVC_PTH` / `RVC_INDEX` 指向本地 |
| bge-base-zh-v1.5（向量模型） | 记忆语义向量（可选） | HuggingFace `BAAI/bge-base-zh-v1.5`<!-- TODO(boss): 待补链接 --> | 未固定 | MIT（以 HF 页面标注为准，<!-- TODO(boss): 待核实 -->） | 391 MB，CPU 即可 |
| ComfyUI | 生图引擎（外部安装） | https://github.com/comfyanonymous/ComfyUI | 用户自选 | GPL-3.0（以官方仓库 LICENSE 为准，<!-- TODO(boss): 待核实 -->） | 独立部署，通过 HTTP 8188 调用 |
| anima-turbo 生图模型 | 拍照底模（unet/clip/vae） | ComfyUI 模型站（随 ComfyUI 生态分发）<!-- TODO(boss): 待补链接 --> | 未固定 | 以模型页标注为准（<!-- TODO(boss): 待补 -->） | 文件名见 `config/example.toml [photo]` |
| 鲸鱼娘人物 LoRA | 拍照人物形象 | <!-- TODO(boss): 待补链接（civitai/HuggingFace 等） --> | `deepseek_whalegirl_v1.safetensors` | 第三方作品，**多限制再分发**——本仓只记文件名与来源，用户自备 | 可换成任意人物 LoRA，改 `[photo] lora` |
| NapCat（NapCatQQ） | QQ / OneBot v11 适配器 | https://github.com/NapNeko/NapCatQQ | OneKey **v4.18.41**（`scripts/setup_napcat.ps1` pin） | 以官方仓库 LICENSE 为准（<!-- TODO(boss): 待核实 -->） | 随附便携 QQ 9.9.33-52230，QQ 客户端版权归腾讯 |
| 魔搭表情包 LoRA | 表情包现生成 | ModelScope `sunny1155665/whalegirl-sticker-v1` | 本地训练，6000 步 | **CC BY-NC-SA 4.0** | 需署名「溟月 / 上善无形」「女仆鲸鱼娘 / ZipZipPipe」；非商业、相同方式共享 |
| MOSS-Image / Qwen-Image | 拍照/表情包底模 | 魔搭 API-Inference 模型 | 用户配置的 model id | 以魔搭模型页标注为准 | 通过 `MODELSCOPE_API_KEY` 调用 |
| Python 生态依赖 | 运行时 | PyPI（`requirements.txt`：psutil、websocket-client、qrcode、edge-tts；唱歌/向量 venv 另装 torch、transformers、soundfile、librosa、pyworld 等） | 见 requirements 与各 venv | 均为各自 PyPI 标注的开源许可证 | 应用本体 `fatfish/` 零第三方依赖 |

## 本仓自制、非开源件

- `assets/voice/cand_home_up1.wav`（约 255 KB）：项目自制录音素材，**非开源件**，经作者确认随公开仓分发，仅作语音克隆参考音使用。
- `assets/song/prompt_v6.json`、`assets/song/persona_song.json`：项目自制的人设唱歌底稿（歌词为人设创作，非真实聊天记录）。

## 许可证义务提醒

- 若再分发本项目，需同时满足上表各许可证：保留各组件的版权声明与协议文本（尤其 index-tts 的 bilibili 协议、YuE2/RVC 与魔搭 LoRA 的条款）。
- 模型权重一律不随本仓分发，由使用者从上表来源自行下载，适用各来源的许可条款。
