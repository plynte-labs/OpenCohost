"""Clip mosaic: tile burst frames into ONE image (vision-on-demand V1.26).

V0.7 measured a 6-frame 3x2 mosaic at 1920x720 (cells 640x360) at ~1.7k prompt
tokens and 3.3 s, with order, motion and text all read correctly, versus 3.4k
tokens for the same six frames sent as separate images (and a poor frame order
past three). The frame index goes in each cell's BOTTOM-RIGHT corner so it
never covers the top-left where titles and HUD usually sit.

Frames stay in memory: nothing here touches the disk or logs image bytes.
Pillow belongs to the optional ``vision`` extra and is imported lazily.
"""
from __future__ import annotations

import io
from typing import Any, Sequence

MOSAIC_COLS = 3
MOSAIC_ROWS = 2
CELL_SIZE = (640, 360)
MOSAIC_QUALITY = 85
MOSAIC_PREVIEW_WIDTH = 960  # three 320 px cells wide: the UI's per-frame size
_PREVIEW_QUALITY = 70
_LABEL_SIZE = 22
_LABEL_BOX = (58, 30)  # label background, inset from the cell corner
_LABEL_INSET = (4, 4)
_FONT_CANDIDATES = ("arialbd.ttf", "DejaVuSans-Bold.ttf")


def _label_font() -> Any:
    from PIL import ImageFont

    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, _LABEL_SIZE)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=_LABEL_SIZE)
    except TypeError:  # Pillow < 10.1: no sized default font
        return ImageFont.load_default()


def build_mosaic(
    jpegs: Sequence[bytes],
    *,
    cols: int = MOSAIC_COLS,
    rows: int = MOSAIC_ROWS,
    cell: tuple[int, int] = CELL_SIZE,
) -> bytes:
    """One JPEG with ``jpegs`` in time order, left to right, top to bottom.

    Each frame is fitted into its cell keeping its aspect ratio (black bars
    when it differs) and gets a ``#n`` index label bottom-right. Cells beyond
    the frames stay black. Extra frames beyond ``cols * rows`` are ignored.
    """
    from PIL import Image, ImageDraw

    cw, ch = cell
    canvas = Image.new("RGB", (cols * cw, rows * ch), (0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font = _label_font()
    box_w, box_h = _LABEL_BOX
    for index, jpeg in enumerate(list(jpegs)[: cols * rows]):
        col, row = index % cols, index // cols
        x0, y0 = col * cw, row * ch
        with Image.open(io.BytesIO(jpeg)) as src:
            frame = src.convert("RGB")
        scale = min(cw / frame.width, ch / frame.height)
        size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        frame = frame.resize(size, Image.BILINEAR)
        canvas.paste(frame, (x0 + (cw - size[0]) // 2, y0 + (ch - size[1]) // 2))
        bx = x0 + cw - box_w - _LABEL_INSET[0]
        by = y0 + ch - box_h - _LABEL_INSET[1]
        draw.rectangle([bx, by, bx + box_w - 1, by + box_h - 1], fill=(0, 0, 0))
        draw.text((bx + 6, by + 3), f"#{index + 1}", font=font, fill=(0, 255, 0))
        draw.rectangle([x0, y0, x0 + cw - 1, y0 + ch - 1], outline=(0, 255, 0))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=MOSAIC_QUALITY)
    return buf.getvalue()


def make_preview(jpeg: bytes, width: int = MOSAIC_PREVIEW_WIDTH) -> bytes:
    """A smaller JPEG of ``jpeg`` for the UI thumbnail ring (never upscales)."""
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as src:
        img = src.convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=_PREVIEW_QUALITY)
    return buf.getvalue()
