#!/usr/bin/env python3
"""
Генератор иконки приложения RCON Warfare (чистый Python, без зависимостей).

Рисует в «фирменном» стиле интерфейса: графитовая плашка со скруглением,
радарное кольцо с рисками, люминофорно-зелёный силуэт самолёта (тот же
полигон, что и в rwf/uicons._PLANE_TOP) и два цветных блipa-цели.
Рендер с суперсэмплингом даёт мягкое сглаживание при даунскейле; полигон
заливается сканлайном (быстро), а не поточечным point-in-polygon.

    python3 tools/make_icon.py [out256.png] [out32.png]
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path
from typing import List, Tuple

SS = 3                     # суперсэмплинг
DESIGN = 24.0              # условная сетка (как uicons)

# полигон самолёта ВИД СВЕРХУ — копия rwf/uicons._PLANE_TOP
PLANE: List[Tuple[float, float]] = [
    (12, 2.2), (13.4, 5.5), (13.4, 9.2), (21.5, 13.6), (21.5, 15.8),
    (13.4, 13.2), (13.4, 17.6), (15.6, 19.6), (15.6, 21.2), (12, 20.2),
    (8.4, 21.2), (8.4, 19.6), (10.6, 17.6), (10.6, 13.2), (2.5, 15.8),
    (2.5, 13.6), (10.6, 9.2), (10.6, 5.5),
]

BG = (20, 24, 21)
BORDER = (44, 58, 46)
RING = (61, 107, 72)
TICK = (127, 163, 127)
PLANE_FILL = (112, 214, 134)
PLANE_EDGE = (190, 250, 205)
BLIP_A = (102, 196, 224)
BLIP_B = (226, 180, 94)


def scanline_mask(n: int, pts: List[Tuple[float, float]]) -> bytearray:
    """Бинарная маска полигона в дизайн-координатах (сканлайн-заливка)."""
    mask = bytearray(n * n)
    ys = [p[1] for p in pts]
    y0, y1 = int(math.floor(min(ys) / DESIGN * n)), \
        int(math.ceil(max(ys) / DESIGN * n))
    for row in range(max(0, y0), min(n, y1 + 1)):
        y = (row + 0.5) / n * DESIGN
        xs: List[float] = []
        j = len(pts) - 1
        for i in range(len(pts)):
            xi, yi = pts[i]
            xj, yj = pts[j]
            if (yi > y) != (yj > y):
                xs.append(xi + (y - yi) / (yj - yi) * (xj - xi))
            j = i
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            a = int(math.floor(xs[k] / DESIGN * n))
            b = int(math.ceil(xs[k + 1] / DESIGN * n))
            for col in range(max(0, a), min(n, b)):
                mask[row * n + col] = 1
    return mask


def erode(mask: bytearray, n: int, px: int = 1) -> bytearray:
    out = bytearray(n * n)
    for row in range(px, n - px):
        base = row * n
        for col in range(px, n - px):
            i = base + col
            if mask[i] and mask[i - 1] and mask[i + 1] and \
                    mask[i - n] and mask[i + n]:
                out[i] = 1
    return out


def render(size: int) -> bytes:
    n = size * SS
    mask = scanline_mask(n, PLANE)
    inner = erode(mask, n, max(1, n // 300))
    raw = bytearray()
    rcx, rcy, rr = 12.0, 13.2, 8.4
    hw, rad = 11.6, 4.6
    for row in range(size):
        raw.append(0)
        for col in range(size):
            acc0 = acc1 = acc2 = acc3 = 0
            for sy in range(SS):
                y = (row * SS + sy + 0.5) / n * DESIGN
                dy = abs(y - 12.0)
                for sx in range(SS):
                    x = (col * SS + sx + 0.5) / n * DESIGN
                    dx = abs(x - 12.0)
                    a = 255
                    if dx > hw or dy > hw:
                        a = 0
                        c = BG
                    elif dx > hw - rad and dy > hw - rad and \
                            math.hypot(dx - (hw - rad),
                                       dy - (hw - rad)) > rad:
                        a = 0
                        c = BG
                    elif max(dx, dy) > hw - 0.55:
                        c = BORDER
                    else:
                        d = math.hypot(x - rcx, y - rcy)
                        idx = (row * SS + sy) * n + (col * SS + sx)
                        if mask[idx]:
                            c = PLANE_FILL if inner[idx] else PLANE_EDGE
                        elif abs(d - rr) < 0.32:
                            c = RING
                        elif rr - 0.9 < d < rr - 0.05 and \
                                abs(((math.degrees(math.atan2(y - rcy,
                                                              x - rcx))
                                      % 360.0) + 15) % 30 - 15) < 2.2:
                            c = TICK
                        else:
                            c = BG
                            for ang, bl in ((-50, BLIP_A), (165, BLIP_B)):
                                ar = math.radians(ang)
                                if math.hypot(
                                        x - (rcx + rr * math.cos(ar)),
                                        y - (rcy + rr * math.sin(ar))) < 0.62:
                                    c = bl
                                    break
                    acc0 += c[0]
                    acc1 += c[1]
                    acc2 += c[2]
                    acc3 += a
            k = SS * SS
            raw += bytes((acc0 // k, acc1 // k, acc2 // k, acc3 // k))
    return bytes(raw)


def write_png(path: Path, size: int, rgba: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(rgba, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    root = Path(__file__).resolve().parent.parent
    out256 = Path(argv[0]) if len(argv) > 0 else root / "rwf/ui/assets/icon.png"
    out32 = Path(argv[1]) if len(argv) > 1 else root / "rwf/ui/assets/icon32.png"
    for path, size in ((out256, 256), (out32, 32)):
        write_png(path, size, render(size))
        print(f"иконка: {path} ({size}px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
