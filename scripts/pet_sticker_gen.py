#!/usr/bin/env python
"""Generate outfit/prop stickers for the pet shell via ModelScope image models.

Pipeline (per sticker sheet):
  1. text-to-image one sheet: several objects on a pure white background
  2. slice the sheet into cells (``--grid 2x2``)
  3. cut out each cell to a transparent PNG
     - ``auto`` (default): use the image alpha when the model already
       produced one (Qwen native RGBA), otherwise rembg
  4. trim to content, pad square, resize, save to ``pet-shell/assets/stickers/<scene>/<cell>.png``

Run with the tools venv (PIL + rembg live there, the app stays stdlib-only):

    pet-shell/tools/venv/Scripts/python.exe scripts/pet_sticker_gen.py --preset work
    pet-shell/tools/venv/Scripts/python.exe scripts/pet_sticker_gen.py --list

The MODELSCOPE_API_KEY comes from the environment or the repo ``.env``
(``KEY=VALUE`` lines); it is never printed.  Free quota is ~2000 calls/day,
each sheet costs one generation, so retries are cheap but bounded.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from fatfish.photo.modelscope import ModelScopePhotoClient  # noqa: E402

STICKER_ROOT = os.path.join(REPO_ROOT, "pet-shell", "assets", "stickers")
CACHE_DIR = os.path.join(REPO_ROOT, "pet-shell", "tools", "cache")

DEFAULT_MODEL = "Qwen/Qwen-Image"
STICKER_NEGATIVE = (
    "photo, realistic, clutter, text, watermark, signature, frame, border, "
    "multiple copies of the same object, overlapping objects, shadows on the ground"
)

# One preset = one sheet of related props for a scene.  Cell names must match
# the renderer's sticker config in pet-shell/renderer/renderer.js (SCENES).
PRESETS: dict[str, dict] = {
    "headset2": {
        "scene": "music",
        "grid": (2, 2),
        "cells": ["headphones", "hpcat", "hpwide", "hptune"],
        "worn": ["headphones", "hpcat"],
        "prompt": (
            "A sticker sheet of four separate kawaii cartoon headphones, arranged in a "
            "strict 2x2 grid on a pure solid white background, evenly spaced with wide "
            "margins, never touching or overlapping, no text. All four are drawn straight "
            "from the front, chibi proportion: the headband arch is WIDE and FLAT, and the "
            "two round ear cups sit at the FAR LEFT and FAR RIGHT ENDS of the band, spread "
            "very wide apart so the cups reach the outer edges of the artwork. "
            "Top-left: deep-blue headphones with a small whale-tail motif on top of the band. "
            "Top-right: the same deep-blue headphones but with two cute cat ears on top of the band. "
            "Bottom-left: light-blue headphones with sleepy closed-eye cups. "
            "Bottom-right: deep-blue compact on-ear headphones with cups touching the band ends. "
            "Thick clean outlines, soft pastel blue palette, flat sticker art style."
        ),
    },
    "work": {
        "scene": "work",
        "grid": (2, 2),
        "cells": ["headphones", "mug", "books", "deskwhale"],
        "worn": ["headphones"],
        "prompt": (
            "A sticker sheet of four separate kawaii cartoon objects, arranged in a "
            "strict 2x2 grid on a pure solid white background, evenly spaced with "
            "wide margins, never touching or overlapping, no text. "
            "Top-left: blue over-ear headphones with a small whale-tail motif. "
            "Top-right: a warm ceramic mug of hot cocoa with a tiny whale drawn on it. "
            "Bottom-left: a small neat stack of three books with a blue ribbon bookmark. "
            "Bottom-right: a chubby light-blue baby whale plush toy. "
            "Thick clean outlines, soft pastel blue palette, flat sticker art style."
        ),
    },
    "meal": {
        "scene": "meal",
        "grid": (2, 2),
        "cells": ["bib", "chopsticks", "ricebowl", "apple"],
        "worn": ["bib"],
        "prompt": (
            "A sticker sheet of four separate kawaii cartoon objects, arranged in a "
            "strict 2x2 grid on a pure solid white background, evenly spaced with "
            "wide margins, never touching or overlapping, no text. "
            "Top-left: a baby-blue meal bib with a tiny whale embroidered on it. "
            "Top-right: cute wooden chopsticks tied with a blue ribbon. "
            "Bottom-left: a small bowl of white rice with a smiley face made of nori. "
            "Bottom-right: a shiny red apple with a green leaf. "
            "Thick clean outlines, soft pastel palette, flat sticker art style."
        ),
    },
    "night": {
        "scene": "night",
        "grid": (2, 2),
        "cells": ["nightcap", "moon", "pillow", "starlamp"],
        "worn": ["nightcap"],
        "prompt": (
            "A sticker sheet of four separate kawaii cartoon objects, arranged in a "
            "strict 2x2 grid on a pure solid white background, evenly spaced with "
            "wide margins, never touching or overlapping, no text. "
            "Top-left: a navy-blue nightcap with a golden crescent-moon pattern and a pompom. "
            "Top-right: a sleepy golden crescent moon with closed eyes and tiny stars. "
            "Bottom-left: a small fluffy pillow in pale blue with a whale silhouette. "
            "Bottom-right: a tiny star-shaped night light glowing softly. "
            "Thick clean outlines, dark blue and gold palette, flat sticker art style."
        ),
    },
    "devil": {
        "scene": "devil",
        "grid": (2, 2),
        "cells": ["trident", "batwings", "flame", "impwhale"],
        "prompt": (
            "A sticker sheet of four separate kawaii cartoon objects, arranged in a "
            "strict 2x2 grid on a pure solid white background, evenly spaced with "
            "wide margins, never touching or overlapping, no text. "
            "Top-left: a cute mini devil trident with rounded heart-shaped tips. "
            "Top-right: a pair of small chubby dark-purple bat wings. "
            "Bottom-left: a friendly tiny orange flame with a smiley face. "
            "Bottom-right: a chubby baby whale with tiny devil horns and a cheeky grin. "
            "Thick clean outlines, purple and orange palette, flat sticker art style."
        ),
    },
    "clingy": {
        "scene": "clingy",
        "grid": (2, 2),
        "cells": ["balloon", "fishplush", "pawheart", "bowribbon"],
        "prompt": (
            "A sticker sheet of four separate kawaii cartoon objects, arranged in a "
            "strict 2x2 grid on a pure solid white background, evenly spaced with "
            "wide margins, never touching or overlapping, no text. "
            "Top-left: a heart-shaped pink balloon on a string. "
            "Top-right: a soft light-blue whale plush toy with sleepy eyes. "
            "Bottom-left: a fluffy white cat paw holding a small red heart. "
            "Bottom-right: a big pastel pink hair bow ribbon. "
            "Thick clean outlines, pink and blue pastel palette, flat sticker art style."
        ),
    },
    "happy": {
        "scene": "happy",
        "grid": (2, 2),
        "cells": ["crown", "confetti", "medal", "partywhale"],
        "worn": ["crown"],
        "prompt": (
            "A sticker sheet of four separate kawaii cartoon objects, arranged in a "
            "strict 2x2 grid on a pure solid white background, evenly spaced with "
            "wide margins, never touching or overlapping, no text. "
            "Top-left: a small golden flower crown of daisies. "
            "Top-right: a burst of colorful confetti and streamers. "
            "Bottom-left: a golden prize medal with a star and blue ribbon. "
            "Bottom-right: a chubby baby whale wearing a tiny party hat, celebrating. "
            "Thick clean outlines, bright cheerful palette, flat sticker art style."
        ),
    },
}


def load_env_keys(path: str | None = None) -> None:
    """Load KEY=VALUE pairs from the repo .env into os.environ (never printed)."""
    path = path or os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip()


def parse_grid(text: str) -> tuple[int, int]:
    cols, _, rows = text.lower().partition("x")
    cols_i, rows_i = int(cols), int(rows or 1)
    if cols_i < 1 or rows_i < 1 or cols_i * rows_i > 12:
        raise ValueError(f"bad grid: {text!r}")
    return cols_i, rows_i


def cell_boxes(width: int, height: int, cols: int, rows: int) -> list[tuple[int, int, int, int]]:
    """Pixel boxes for each grid cell, in row-major order."""
    boxes = []
    cw, ch = width // cols, height // rows
    for r in range(rows):
        for c in range(cols):
            boxes.append((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
    return boxes


def submit_sheet(client: ModelScopePhotoClient, prompt: str, size: str, retries: int) -> str:
    width_text, _, height_text = size.lower().partition("x")
    width, height = int(width_text), int(height_text or width_text)
    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            payload = client.build_payload(prompt, STICKER_NEGATIVE, width=width, height=height)
            task_id = client.submit(payload)
            url = client.wait(task_id)
            return url
        except Exception as exc:  # noqa: BLE001 - bounded retry for flaky quota
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"  attempt {attempt}/{retries} failed: {last_error[:200]}")
    raise SystemExit(f"generation failed after {retries} attempts: {last_error[:300]}")


def download(url: str) -> bytes:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def cutout_cell(img, mode: str, session=None):
    """Return an RGBA cutout of one cell image. ``mode``: auto|alpha|rembg|none."""
    has_alpha = img.mode == "RGBA" and img.getchannel("A").getextrema()[0] < 250
    if mode in ("auto", "alpha") and has_alpha:
        return img
    if mode == "alpha":
        if img.mode != "RGBA":
            raise SystemExit("--cutout alpha but the model returned no alpha channel")
        return img
    if mode == "none":
        return img.convert("RGBA")
    if session is None:
        raise SystemExit("rembg requested but the session was not initialised")
    from rembg import remove

    return remove(img.convert("RGBA"), session=session)


def keep_main_component(img, center_bias: bool = True):
    """Keep only one connected alpha blob per cell.

    rembg happily keeps several objects from a sheet cell (a neighbour's edge,
    or the whole white card as 'background object'), which shows up on the pet
    as ghost overlays.  Keep the blob that covers the cell centre when
    possible, otherwise the largest blob.  numpy BFS via roll-dilation: fast
    enough at 512px and dependency-free beyond numpy (already in the tools venv
    for onnxruntime).
    """
    import numpy as np

    alpha = np.asarray(img.getchannel("A"), dtype=np.uint8)
    mask = alpha > 64
    h, w = mask.shape
    if not mask.any():
        return img

    def flood(seed_mask: np.ndarray) -> np.ndarray:
        comp = np.zeros_like(mask, dtype=bool)
        frontier = seed_mask & mask
        comp[frontier] = True
        while True:
            grown = (
                np.roll(comp, 1, 0) | np.roll(comp, -1, 0)
                | np.roll(comp, 1, 1) | np.roll(comp, -1, 1)
            ) & mask
            grown |= comp
            if (grown == comp).all():
                return comp
            comp = grown

    if center_bias:
        seed = np.zeros_like(mask, dtype=bool)
        seed[h // 2 - 2 : h // 2 + 3, w // 2 - 2 : w // 2 + 3] = True
        comp = flood(seed)
    else:
        comp = None
    if comp is None or not comp.any():
        # fall back to the largest component anywhere (sparse seeds suffice:
        # roll-flood expands each seed to its full blob)
        visited = np.zeros_like(mask, dtype=bool)
        best = None
        best_size = 0
        ys, xs = np.nonzero(mask)
        for y, x in zip(ys[::16], xs[::16]):
            if visited[y, x]:
                continue
            seed = np.zeros_like(mask, dtype=bool)
            seed[y, x] = True
            c = flood(seed)
            visited |= c
            size = int(c.sum())
            if size > best_size:
                best_size, best = size, c
        comp = best if best is not None else mask
    cleaned = np.where(comp, alpha, 0).astype(np.uint8)
    from PIL import Image

    out = img.copy()
    out.putalpha(Image.fromarray(cleaned, "L"))
    return out


def soften_edges(img, erode: int = 2, blur: float = 1.2):
    """Kill the rembg hard fringe: erode alpha, feather it, de-white edges."""
    from PIL import Image, ImageFilter

    alpha = img.getchannel("A")
    for _ in range(erode):
        alpha = alpha.filter(ImageFilter.MinFilter(3))
    alpha = alpha.filter(ImageFilter.GaussianBlur(blur))
    out = img.copy()
    out.putalpha(alpha)

    # de-white soft edge pixels: pull their colour toward the solid interior
    import numpy as np

    arr = np.array(out, dtype=np.float32)
    solid = arr[..., 3] > 220
    if solid.any():
        mean_rgb = arr[..., :3][solid].mean(axis=0)
        soft = (arr[..., 3] > 20) & ~solid
        weight = 0.45
        arr[..., :3][soft] = (
            arr[..., :3][soft] * (1 - weight) + mean_rgb[None, :] * weight
        )
        out = Image.fromarray(arr.astype(np.uint8), "RGBA")
    return out


def strip_white_border(img):
    """Remove the sticker-style white outline: flood near-white pixels that are
    connected to the image border.  Interior whites (eye highlights, patterns)
    are not border-connected and survive.  Used for *worn* accessories so they
    read as painted onto the character instead of a sticker slapped on top.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    arr = np.array(img.convert("RGBA"), dtype=np.uint8)
    rgb = arr[..., :3].astype(int)
    alpha = arr[..., 3].astype(int)
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    # passable: the white sticker card plus the drop-shadow ring rembg kept
    # (gray, often semi-transparent); both connect border -> object outline.
    near_white = ((mx - mn < 30) & (mn > 170)) | (alpha < 210)
    border = np.zeros_like(near_white, dtype=bool)
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
    comp = frontier = border & near_white
    while True:
        grown = (
            np.roll(comp, 1, 0) | np.roll(comp, -1, 0)
            | np.roll(comp, 1, 1) | np.roll(comp, -1, 1)
        ) & near_white
        grown |= comp
        if (grown == comp).all():
            break
        comp = grown
    arr[..., 3][comp] = 0
    out = Image.fromarray(arr, "RGBA")
    alpha = out.getchannel("A").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.0))
    out.putalpha(alpha)
    return out


def trim_and_square(img, out_size: int = 512, threshold: int = 8):
    """Trim transparent margins, pad to square, resize."""
    from PIL import Image

    bbox = img.getchannel("A").point(lambda a: 255 if a > threshold else 0).getbbox()
    if bbox:
        img = img.crop(bbox)
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    if side != out_size:
        canvas = canvas.resize((out_size, out_size), 2)  # Image.LANCZOS
    return canvas


def main() -> None:  # noqa: C901 - script main
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preset", help="preset name (see --list)")
    parser.add_argument("--list", action="store_true", help="list presets and exit")
    parser.add_argument("--cells", default="", help="comma list of cell names to keep (default: all)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="ModelScope image model id")
    parser.add_argument("--size", default="1024x1024", help="generation size")
    parser.add_argument("--grid", default="", help="override grid, e.g. 2x2")
    parser.add_argument("--prompt", default="", help="override sheet prompt")
    parser.add_argument("--name", default="", help="override output file name (single cell mode)")
    parser.add_argument("--cutout", default="auto", choices=["auto", "alpha", "rembg", "none"])
    parser.add_argument("--out", default="", help="override output dir")
    parser.add_argument("--sheet", default="", help="reuse a cached sheet PNG instead of generating")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--no-save-sheet", action="store_true", help="do not keep the raw sheet in cache")
    parser.add_argument("--no-refine", action="store_true", help="skip component cleanup and edge feathering")
    args = parser.parse_args()

    if args.list:
        for name, spec in PRESETS.items():
            print(f"{name:8s} scene={spec['scene']:8s} grid={'x'.join(map(str, spec['grid']))} cells={','.join(spec['cells'])}")
        return

    if not args.preset:
        parser.error("--preset is required (or --list)")
    if args.preset not in PRESETS:
        parser.error(f"unknown preset {args.preset!r}; use --list")

    spec = PRESETS[args.preset]
    grid = parse_grid(args.grid) if args.grid else spec["grid"]
    prompt = args.prompt or spec["prompt"]
    if len(spec["cells"]) > grid[0] * grid[1]:
        raise SystemExit("preset has more cells than the grid holds")

    load_env_keys()
    api_key = os.environ.get("MODELSCOPE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("MODELSCOPE_API_KEY missing (env or .env)")

    session = None
    if args.cutout in ("auto", "rembg"):
        try:
            from rembg import new_session

            session = new_session("u2net")
        except Exception as exc:  # noqa: BLE001
            if args.cutout == "rembg":
                raise SystemExit(f"rembg unavailable in this interpreter: {exc}")
            print("  rembg unavailable, falling back to alpha/none cutout")
            args.cutout = "alpha" if args.model.lower().endswith(("2509", "2512")) else "none"

    from PIL import Image

    client = ModelScopePhotoClient(api_key=api_key, model=args.model, timeout=600, poll_seconds=5)
    if args.sheet:
        with open(args.sheet, "rb") as fh:
            raw = fh.read()
        print(f"sheet reused: {args.sheet}")
    else:
        print(f"model={args.model} size={args.size} grid={grid[0]}x{grid[1]} preset={args.preset}")
        url = submit_sheet(client, prompt, args.size, max(1, args.retries))
        raw = download(url)
        os.makedirs(CACHE_DIR, exist_ok=True)
        sheet_path = os.path.join(CACHE_DIR, f"sheet_{args.preset}_{args.model.replace('/', '_')}_{args.size}.png")
        if not args.no_save_sheet:
            with open(sheet_path, "wb") as fh:
                fh.write(raw)
            print(f"sheet saved: {sheet_path}")

    sheet = Image.open(io.BytesIO(raw))
    print(f"sheet: {sheet.format} {sheet.mode} {sheet.size}")

    out_dir = args.out or os.path.join(STICKER_ROOT, spec["scene"])
    os.makedirs(out_dir, exist_ok=True)
    keep = [c for c in args.cells.split(",") if c] or spec["cells"]

    for index, (box, name) in enumerate(zip(cell_boxes(sheet.width, sheet.height, grid[0], grid[1]), spec["cells"])):
        if name not in keep:
            continue
        cell = sheet.crop(box)
        cut = cutout_cell(cell, args.cutout, session=session)
        if not args.no_refine:
            cut = keep_main_component(cut, center_bias=True)
            cut = strip_white_border(cut)  # worn AND floating: white sticker cards look odd on the desktop
        if args.name and len(keep) == 1:
            name = args.name
        final = trim_and_square(cut)
        if not args.no_refine:
            final = soften_edges(final)
        out_path = os.path.join(out_dir, f"{name}.png")
        final.save(out_path)
        alpha_lo, alpha_hi = final.getchannel("A").getextrema()
        print(f"  cell[{index}] {name}: {out_path} (alpha {alpha_lo}-{alpha_hi})")

    print("done.")


if __name__ == "__main__":
    main()
