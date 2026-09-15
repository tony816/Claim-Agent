"""Drawing normalisation: resize once at intake so every call sends the same, bounded image.

Patent drawings need legible reference numerals, so the default bound (longest side 2048 px)
is generous; anything already within the bound is passed through byte-for-byte.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

DEFAULT_MAX_SIDE = 2048
_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    mime_type: str
    sha256: str
    original_size: tuple[int, int] | None
    size: tuple[int, int] | None
    resized: bool
    note: str = ""


def normalize_image(data: bytes, mime_type: str, max_side: int = DEFAULT_MAX_SIDE) -> NormalizedImage:
    """Return the image bounded to `max_side` on its longest edge (unchanged bytes when already within)."""
    try:
        from PIL import Image
    except ImportError:  # Pillow is optional; without it the original bytes are used.
        return NormalizedImage(data, mime_type, hashlib.sha256(data).hexdigest(), None, None, False, "Pillow 미설치: 원본 전송")
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            if max_side <= 0 or max(width, height) <= max_side:
                return NormalizedImage(data, mime_type, hashlib.sha256(data).hexdigest(), (width, height), (width, height), False)
            scale = max_side / max(width, height)
            target = (max(1, round(width * scale)), max(1, round(height * scale)))
            fmt = _FORMAT.get(mime_type, "PNG")
            out = im
            if fmt == "JPEG" and out.mode not in ("RGB", "L"):
                out = out.convert("RGB")
            out = out.resize(target, Image.LANCZOS)
            buf = io.BytesIO()
            save_kwargs = {"quality": 90} if fmt in ("JPEG", "WEBP") else {"optimize": True}
            out.save(buf, format=fmt, **save_kwargs)
            blob = buf.getvalue()
            return NormalizedImage(blob, mime_type, hashlib.sha256(blob).hexdigest(), (width, height), target, True, f"{width}x{height} → {target[0]}x{target[1]}")
    except Exception as exc:  # noqa: BLE001 - never block intake on a decoder problem; send the original
        return NormalizedImage(data, mime_type, hashlib.sha256(data).hexdigest(), None, None, False, f"정규화 실패, 원본 전송: {exc}")
