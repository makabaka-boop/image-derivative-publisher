"""Image decoding / validation / derivative rendering helpers."""

from __future__ import annotations

import io

from PIL import Image

from .db import ALLOWED_FORMATS


class BadImageError(ValueError):
    """Raised when uploaded bytes are not a decodable PNG/JPEG image."""


def decode_image(data: bytes) -> Image.Image:
    """Decode bytes into a fully-loaded PIL image, PNG/JPEG only."""
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        img.load()
    except Exception as exc:  # Pillow raises many exception types here
        raise BadImageError(f"undecodable image: {exc}") from exc
    if fmt not in ALLOWED_FORMATS:
        raise BadImageError(f"unsupported image format: {fmt!r}")
    return img


def validate_crop(x: int, y: int, w: int, h: int, width: int, height: int) -> str | None:
    """Return an error message if the crop rectangle escapes the image."""
    if x < 0 or y < 0:
        return "crop origin must be >= 0"
    if w < 1 or h < 1:
        return "crop width/height must be >= 1"
    if x + w > width or y + h > height:
        return f"crop rectangle ({x},{y} {w}x{h}) exceeds image bounds {width}x{height}"
    return None


def render_derivative(img: Image.Image, crop: tuple[int, int, int, int], size: int) -> Image.Image:
    """Crop and resize so the longest edge equals ``size`` pixels."""
    x, y, w, h = crop
    region = img.crop((x, y, x + w, y + h))
    scale = size / max(w, h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    return region.resize((new_w, new_h), Image.LANCZOS)


def save_webp(img: Image.Image, path: str) -> None:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
    img.save(path, format="WEBP", quality=90, method=4)
