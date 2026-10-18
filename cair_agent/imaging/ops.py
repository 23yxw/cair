"""Deterministic image operations: resize, crop/zoom, contact sheet, side-by-side."""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

ROI = Tuple[float, float, float, float]  # normalized x0, y0, x1, y1


def _font(size: int = 14):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def to_rgb(im: Image.Image) -> Image.Image:
    return im if im.mode == "RGB" else im.convert("RGB")


def resize_max_side(im: Image.Image, max_side: int) -> Image.Image:
    """Downscale so the longest side <= max_side. Never upscales."""
    w, h = im.size
    s = max(w, h)
    if s <= max_side:
        return im
    scale = max_side / s
    return im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def crop_zoom(im: Image.Image, roi: Optional[ROI], scale: float = 2.0, max_side: int = 1024) -> Image.Image:
    """Crop a normalized ROI (clipped to the image) and upscale by `scale`, capped at max_side."""
    w, h = im.size
    if roi is None:
        roi = (0.25, 0.25, 0.75, 0.75)
    x0, y0, x1, y1 = roi
    x0, x1 = sorted((min(max(x0, 0.0), 1.0), min(max(x1, 0.0), 1.0)))
    y0, y1 = sorted((min(max(y0, 0.0), 1.0), min(max(y1, 0.0), 1.0)))
    box = (int(x0 * w), int(y0 * h), max(int(x0 * w) + 1, int(x1 * w)), max(int(y0 * h) + 1, int(y1 * h)))
    crop = im.crop(box)
    cw, ch = crop.size
    target = (round(cw * scale), round(ch * scale))
    crop = crop.resize(target, Image.LANCZOS)
    return resize_max_side(crop, max_side)


def label_image(im: Image.Image, text: str) -> Image.Image:
    """Burn a small label into the top-left corner (used for contact sheet tiles)."""
    im = to_rgb(im).copy()
    d = ImageDraw.Draw(im)
    f = _font(max(12, im.size[0] // 24))
    bbox = d.textbbox((0, 0), text, font=f)
    pad = 3
    d.rectangle((0, 0, bbox[2] + 2 * pad, bbox[3] + 2 * pad), fill=(0, 0, 0))
    d.text((pad, pad), text, fill=(255, 255, 0), font=f)
    return im


def contact_sheet(images: Sequence[Image.Image], labels: Sequence[str], cols: int = 4,
                  tile: int = 256) -> Image.Image:
    """Grid of thumbnails with burned-in labels (e.g. slice numbers)."""
    assert len(images) == len(labels)
    n = len(images)
    cols = max(1, min(cols, n))
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile, rows * tile), (20, 20, 20))
    for i, (im, lab) in enumerate(zip(images, labels)):
        t = to_rgb(im).copy()
        t.thumbnail((tile, tile), Image.LANCZOS)
        t = label_image(t, lab)
        x = (i % cols) * tile + (tile - t.size[0]) // 2
        y = (i // cols) * tile + (tile - t.size[1]) // 2
        sheet.paste(t, (x, y))
    return sheet


def side_by_side(images: Sequence[Image.Image], labels: Sequence[str], height: int = 512) -> Image.Image:
    """Horizontal strip, each image scaled to a common height, with labels."""
    tiles: List[Image.Image] = []
    for im, lab in zip(images, labels):
        t = to_rgb(im)
        w, h = t.size
        t = t.resize((max(1, round(w * height / h)), height), Image.LANCZOS)
        tiles.append(label_image(t, lab))
    total_w = sum(t.size[0] for t in tiles) + 4 * (len(tiles) - 1)
    out = Image.new("RGB", (max(1, total_w), height), (20, 20, 20))
    x = 0
    for t in tiles:
        out.paste(t, (x, 0))
        x += t.size[0] + 4
    return out
