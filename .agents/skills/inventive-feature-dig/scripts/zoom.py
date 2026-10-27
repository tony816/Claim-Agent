"""Crop a region of a drawing or slide and enlarge it, so small parts (bosses, holes, pads) can be read.

usage: python zoom.py <image> <x0> <y0> <x1> <y1> <out.png> [scale=4]
Coordinates are pixels of the original image. Works for PNG, JPG and WEBP.
"""
from __future__ import annotations

import sys

from PIL import Image


def main(argv: list[str]) -> int:
    if len(argv) not in (7, 8):
        print(__doc__)
        return 2
    src, x0, y0, x1, y1, out = argv[1], *map(int, argv[2:6]), argv[6]
    scale = int(argv[7]) if len(argv) == 8 else 4
    im = Image.open(src).convert("RGB")
    box = (max(0, x0), max(0, y0), min(im.width, x1), min(im.height, y1))
    crop = im.crop(box)
    crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS).save(out)
    print(f"{src} {im.size} -> {out} box={box} x{scale}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
