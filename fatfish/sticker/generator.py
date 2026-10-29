"""Generate a meme from a textless base image when no library match fits.

The chat model supplies the intent; this module only decides whether a base
image is appropriate and how to render a short caption. It never invents a
long paragraph, and it never touches the conversation history.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
from collections import deque
from pathlib import Path
from collections.abc import Sequence
from typing import Any

try:  # 可选依赖：只有「库里没有就现画一张」需要 Pillow
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont
except ImportError:  # pragma: no cover - 纯文字/无贴纸部署不装 Pillow
    Image = ImageDraw = ImageEnhance = ImageFont = None  # type: ignore[assignment]

from ..config import Config
from ..db import Database

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)
_FALLBACK_CAPTIONS = {
    "happy": "好耶",
    "comfort": "抱抱",
    "tease": "就这？",
    "confused": "无语",
    "angry": "哼",
    "greeting": "在吗",
    "shy": "才不是",
    "sad": "呜呜",
    "celebrate": "好耶",
    "flirt": "贴贴",
    "goodnight": "晚安好梦",
    "apology": "对不起嘛",
    "thanks": "谢谢啦",
    "meme": "本鲸鱼娘来啦",
}


class BaseStickerGenerator:
    def __init__(self, db: Database, config: Config, service: Any, llm: Any = None):
        self.db = db
        self.config = config
        self.service = service
        self.llm = llm
        self.enabled = bool(config.get("sticker.base_generation_enabled", True))
        self.max_chars = max(2, min(12, int(config.get("sticker.base_generation_max_chars", 12))))
        self.output_dir = config.data_dir / "stickers" / "generated"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = config.data_dir / "stickers" / "generated_cache.json"
        self.cache = self._load_cache()
        self._recent: deque[float] = deque()
        self.max_per_10m = max(0, int(config.get("sticker.base_generation_max_per_10m", 3)))

    # ---------------------------------------------------------------- bases
    def _load_cache(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(
                json.dumps(self.cache, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _base_stickers(self) -> list[Any]:
        rows = self.db.query(
            "SELECT id, content_json FROM stickers WHERE active=1"
        )
        result = []
        for row in rows:
            try:
                meta = json.loads(row["content_json"] or "{}")
            except ValueError:
                continue
            if not isinstance(meta, dict) or not meta.get("is_base"):
                continue
            sticker = self.service.get(row["id"])
            if sticker:
                result.append(sticker)
        return result

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", (text or "").lower())
            if token.strip()
        }

    def select_base(self, need: str, *, preferred_intents: Sequence[str] | None = None) -> Any | None:
        need_tokens = self._tokens(need)
        wants = self.service.classify_intents(need)
        if preferred_intents:
            # The user's own words outrank the model's paraphrase: mixing both
            # lets "撒娇" pull the tie back towards a pouting base.
            intent_set = set(preferred_intents)
        else:
            intent_set = {name for name, score in wants if score >= 0.3}
        best = None
        best_score = -1.0
        for sticker in self._base_stickers():
            hay = f"{sticker.caption} {' '.join(sticker.tags)} {' '.join(sticker.intents)}".lower()
            overlap = len(need_tokens & self._tokens(hay)) / max(1, len(need_tokens))
            intent_hit = len(intent_set & set(sticker.intents)) / max(1, len(intent_set or {"meme"}))
            score = 0.55 * intent_hit + 0.35 * overlap + 0.10 * float(sticker.intensity) / 5.0
            if score > best_score:
                best, best_score = sticker, score
        return best

    # -------------------------------------------------------------- caption
    def _fallback_caption(self, need: str, intents: Sequence[str] | None = None) -> str:
        ordered = [name for name in (intents or ())]
        ordered += [
            name for name, score in self.service.classify_intents(need) if score >= 0.3
        ]
        for name in ordered:
            if name in _FALLBACK_CAPTIONS:
                return _FALLBACK_CAPTIONS[name]
        text = re.sub(r"\s+", "", need or "")
        if text and "表情" not in text and "图" not in text:
            return text[: self.max_chars]
        return _FALLBACK_CAPTIONS["meme"]

    def _llm_caption(
        self,
        base: Any,
        need: str,
        intents: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        if self.llm is None:
            try:
                from ..llm import LLMClient

                self.llm = LLMClient(self.db, self.config)
            except Exception:
                self.llm = None
        if self.llm is None or not getattr(self.llm, "available", False):
            return None
        try:
            raw = Path(base.file_path).read_bytes()
        except OSError:
            return None
        suffix = Path(base.file_path).suffix.lower().lstrip(".") or "png"
        data_url = f"data:image/{suffix};base64,{base64.b64encode(raw).decode('ascii')}"
        prompt = (
            "你是鲸鱼娘的表情包文案导演。看这张无字底图，结合需求，只返回 JSON："
            '{"caption":"2-8个中文字符","placement":"top|bottom|middle","filter":"none|warm|cool|contrast","effect":"none|outline|shadow"}。'
            "caption 必须短、口语、和画面情绪一致；不要解释、不要长句、不要真实姓名/隐私。"
            "文案要紧贴场景：场景是睡前的就是晚安/睡吧一类，不要写和场景无关的傲娇台词。"
            f"\n场景：{'、'.join(sorted(intents)) if intents else '未指定'}"
            f"\n需求：{need}"
        )
        try:
            result = self.llm.chat(
                [
                    {"role": "system", "content": "只输出 JSON，不要 Markdown。"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    },
                ],
                max_tokens=1500,
                temperature=0.4,
                session_id="sticker-base-caption",
            )
            text = (result.text if result else "").strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            caption = str(data.get("caption") or "").strip()[: self.max_chars]
            if not caption:
                return None
            return {
                "caption": caption,
                "placement": str(data.get("placement") or "bottom").lower(),
                "filter": str(data.get("filter") or "none").lower(),
                "effect": str(data.get("effect") or "outline").lower(),
            }
        except Exception:
            return None

    # --------------------------------------------------------------- render
    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        for candidate in _FONT_CANDIDATES:
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _wrap(self, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
        lines: list[str] = []
        current = ""
        for char in text:
            candidate = current + char
            if font.getbbox(candidate)[2] <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = char
        if current:
            lines.append(current)
        return lines[:2]

    def _render(self, base: Any, caption: str, style: dict[str, Any], digest: str) -> Path:
        if Image is None:
            raise RuntimeError(
                "贴纸现画需要 Pillow：pip install pillow（纯文字部署可以不装）"
            )
        with Image.open(base.file_path) as image:
            image.seek(0)
            frame = image.convert("RGBA")
        width, height = frame.size
        if width > 1400 or height > 1400:
            scale = 1400 / max(width, height)
            frame = frame.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.LANCZOS)
            width, height = frame.size

        filter_name = str(style.get("filter") or "none").lower()
        if filter_name == "warm":
            frame = ImageEnhance.Color(frame).enhance(1.12)
            frame = ImageEnhance.Brightness(frame).enhance(1.04)
        elif filter_name == "cool":
            frame = ImageEnhance.Color(frame).enhance(0.92)
            frame = ImageEnhance.Brightness(frame).enhance(1.02)
        elif filter_name == "contrast":
            frame = ImageEnhance.Contrast(frame).enhance(1.12)

        font_size = max(24, min(72, int(width / max(4, len(caption) + 1))))
        font = self._font(font_size)
        lines = self._wrap(caption, font, int(width * 0.86))
        line_height = int(font_size * 1.15)
        block_h = line_height * len(lines)
        placement = str(style.get("placement") or "bottom").lower()
        if placement == "top":
            y0 = int(height * 0.06)
        elif placement == "middle":
            y0 = (height - block_h) // 2
        else:
            y0 = height - block_h - int(height * 0.06)

        overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        pad = max(10, font_size // 3)
        max_line = max(font.getbbox(line)[2] for line in lines)
        x0 = (width - max_line) // 2
        draw.rounded_rectangle(
            (x0 - pad, y0 - pad, x0 + max_line + pad, y0 + block_h + pad),
            radius=pad,
            fill=(2, 12, 20, 170),
        )
        frame = Image.alpha_composite(frame, overlay)
        draw = ImageDraw.Draw(frame)
        for index, line in enumerate(lines):
            bbox = font.getbbox(line)
            line_w = bbox[2] - bbox[0]
            x = (width - line_w) // 2
            y = y0 + index * line_height
            draw.text(
                (x, y),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=max(2, font_size // 14),
                stroke_fill=(2, 10, 16, 255),
            )
        target = self.output_dir / f"{digest}.png"
        frame.convert("RGB").save(target, "PNG")
        return target

    # ---------------------------------------------------------------- public
    def generate(
        self,
        need: str,
        context: dict[str, Any] | None = None,
        *,
        intents: Sequence[str] | None = None,
    ) -> Any | None:
        if not self.enabled:
            return None
        now = time.time()
        while self._recent and now - self._recent[0] > 600:
            self._recent.popleft()
        if self.max_per_10m and len(self._recent) >= self.max_per_10m:
            return None
        # ``intents`` comes from the user's own words; without it a paraphrase
        # like "带一点撒娇" picks a shy base for a bedtime request.
        base = self.select_base(need, preferred_intents=intents)
        if base is None:
            return None
        cache_key = f"{base.id}|{need[:80]}"
        cached = self.cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("path") and Path(cached["path"]).is_file():
            return self.service.get(cached.get("sticker_id") or "") or None
        style = self._llm_caption(base, need, intents) or {
            "caption": self._fallback_caption(need, intents),
            "placement": "bottom",
            "filter": "none",
            "effect": "outline",
        }
        caption = str(style.get("caption") or self._fallback_caption(need, intents))[: self.max_chars]
        digest = hashlib.sha256(f"{base.id}|{caption}|{style}".encode("utf-8")).hexdigest()[:16]
        path = self._render(base, caption, style, digest)
        classified = [name for name, _ in self.service.classify_intents(need)[:3]]
        ordered = [name for name in (intents or ()) if name != "meme"]
        intents_out = ordered + [name for name in classified if name not in ordered]
        sticker = self.service.add_sticker(
            str(path),
            caption=caption,
            tags=["generated", "base", "鲸鱼娘"],
            intents=intents_out[:3] or ["meme"],
            content={
                "generated": True,
                "base_id": base.id,
                "text": caption,
                "style": style,
                "need": need,
            },
            source="generated",
            sticker_id=digest,
        )
        self._recent.append(now)
        self.cache[cache_key] = {"path": str(path), "sticker_id": sticker.id}
        self._save_cache()
        return sticker
