"""PIL image -> base64 data URL for chat completion image_url parts."""

from __future__ import annotations

import base64
import io

from PIL import Image

from .ops import resize_max_side, to_rgb


def encode_jpeg_data_url(im: Image.Image, max_side: int = 1024, quality: int = 85) -> str:
    im = resize_max_side(to_rgb(im), max_side)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def load_image(data: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    im.load()
    return im
