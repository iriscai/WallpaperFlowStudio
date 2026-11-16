"""Small PNG writer so image nodes can leave a real file without Pillow."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def write_png(path: Path, width: int, height: int, pixel) -> None:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            red, green, blue, alpha = pixel(x, y)
            raw.extend((red & 255, green & 255, blue & 255, alpha & 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def write_gradient(path: Path, width: int, height: int, seed_text: str) -> None:
    hue = zlib.crc32((seed_text or "wallpaper").encode("utf-8")) & 0xFFFFFFFF
    top = (176 + (hue & 50), 148 + ((hue >> 8) & 60), 118 + ((hue >> 16) & 50))
    bottom = (36 + ((hue >> 4) & 28), 48 + ((hue >> 12) & 36), 72 + ((hue >> 20) & 40))

    def pixel(x: int, y: int):
        ty = y / max(1, height - 1)
        tx = x / max(1, width - 1)
        red = int(top[0] * (1 - ty) + bottom[0] * ty)
        green = int(top[1] * (1 - ty) + bottom[1] * ty)
        blue = int(top[2] * (1 - tx) + bottom[2] * tx)
        return red, green, blue, 255

    write_png(path, max(8, width), max(8, height), pixel)


def fit_size(width: int, height: int, max_side: int = 360) -> tuple[int, int]:
    width = max(32, int(width or 32))
    height = max(32, int(height or 32))
    scale = min(1.0, max_side / max(width, height))
    return max(32, int(width * scale)), max(32, int(height * scale))
