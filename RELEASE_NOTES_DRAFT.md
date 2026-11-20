# 发布说明（2026-11-20）

> 公开发布树。源码可见、个人非商用；模型/权重按 README 链接自行下载。

## 本版包含

- **聊天 + 人设 + 记忆**：SQLite schema v17；事件/细节/实体/人物卡；多会话漫游。
- **语音**：IndexTTS-2.5 本地 TTS（9880），六档语气；参考音由 `assets/` 播种。
- **QQ**：NapCat / OneBot v11 本地通道；官方 QQ Bot adapter（云端入口）。
- **表情包**：魔搭 LoRA 现生成（`sticker_jobs` 异步补发）；无 key 时友好降级。
- **拍照**：魔搭 ModelScope API-Inference 默认；ComfyUI 本机模式保留。
- **唱歌**：YuE2 全曲 → Demucs → RVC 换声；renderer `soulx/song_yue2_rvc.py`。
- **平台接入**：QQ 私聊/群、Web、CLI、B 站只读、QQ 空间、漫游、桌宠（桌宠壳不在本发布树）。
- **云端在线层**：`deploy/linux/`；QQ/Web/记忆常驻，GPU 能力回家里。

## 本版移除 / 不包含

- 旧 SoulX `song_v2.py` 唱歌链及其 15 件闭包、`finalize_song`、旧 melody/prosody/legato/reverb 测试。
- 旧表情包库检索（intent/emotion/lexicon/importer/generator）、`fetch_stickers.py`、库图片资产。
- 根目录 `staging_*.py`、`legacy/`、一次性研发报告、真实数据/模型/权重。
- 桌宠 `pet-shell/`（本地能力，未随本发布树分发）。

## 验证

- 发布树全量离线测试：**2,515 条，0 失败，3 skipped**（2026-11-20 实测，约 3.6 分钟）。源码树全量 2,680 条 0 失败。
- 测试不联网、不需要模型、不读真实 `data/fatfish.db`。
- 冒烟：`python -m fatfish --help`、`scripts/install_assets.py --dry-run`、`scripts/make_app.py --no-zip`。
- 文档索引：`python scripts/docs_index.py --check`（34 篇）。

## 升级提示

- 旧库从 schema v14/v15/v16 自动迁移到 v17；`Database.migrate()` 幂等。
- 旧 SoulX 配置键（`sing.soulx_root`、`sing.deps_path`、`sing.script=soulx/song_v2.py`）不再使用；改 `sing.script=soulx/song_yue2_rvc.py`，并用环境变量 `YUE2_ROOT` / `RVC_ROOT` / `RVC_PTH` / `RVC_INDEX` 指向外部安装。
- 旧表情包库配置（`embedding_*`、`intent_*`、`similarity_*`）不再使用；新配置是 `[sticker] generation_*` + `MODELSCOPE_API_KEY`。

## 许可

源码见 [LICENSE](LICENSE)；第三方组件与模型见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。权重/资产不随仓分发。
