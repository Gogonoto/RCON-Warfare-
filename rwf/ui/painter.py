"""
Интерпретатор примитивов тактической карты.

`MapRenderer` (rwf/maprender.py) выдаёт список словарей-примитивов в экранных
координатах: line / rect / circle / poly / text. Этот модуль переводит их в
вызовы `emit(kind, **параметры)` — ТОЛКОВАТЕЛЯ, не знающего про Dear PyGui.
Продукционный emit (rwf/ui/mapfacade.py) зовёт dpg.draw_*; тестовый — пишет
в список. Так вся логика карты (цвета, пунктир, якоря текста) остаётся
проверяемой без GL-контекста, а dpg.draw_* не распылён по кодовой базе
(инвариант секции 5 скилла: рисование только через фасад карты).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Emit = Callable[..., None]

_COLOR_CACHE: Dict[str, Tuple[int, int, int, int]] = {}


def parse_color(spec: Any, alpha: Optional[int] = None,
                default: Tuple[int, int, int, int] = (255, 255, 255, 255)
                ) -> Optional[Tuple[int, int, int, int]]:
    """'#rgb'/'#rrggbb'/'#rrggbbaa'/кортеж -> RGBA (0..255). None -> None."""
    if spec is None:
        return None
    if isinstance(spec, (tuple, list)):
        c = tuple(int(v) for v in spec)
        if len(c) == 3:
            c = c + (255,)
        return c  # type: ignore[return-value]
    text = str(spec)
    key = text if alpha is None else f"{text}|{alpha}"
    cached = _COLOR_CACHE.get(key)
    if cached is not None:
        return cached
    out: Optional[Tuple[int, int, int, int]] = None
    if text.startswith("#"):
        h = text[1:]
        try:
            if len(h) == 3:
                out = (int(h[0] * 2, 16), int(h[1] * 2, 16),
                       int(h[2] * 2, 16), 255)
            elif len(h) == 6:
                out = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
            elif len(h) == 8:
                out = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16),
                       int(h[6:8], 16))
        except ValueError:
            out = None
    if out is None:
        out = default
    if alpha is not None:
        out = (out[0], out[1], out[2], max(0, min(255, int(alpha))))
    _COLOR_CACHE[key] = out
    return out


def dash_segments(x1: float, y1: float, x2: float, y2: float,
                  dash: float = 7.0, gap: float = 5.0, offset: float = 0.0
                  ) -> List[Tuple[float, float, float, float]]:
    """Разбить отрезок на штрихи пунктира (Qt DashLine -> сегменты).

    `offset` сдвигает фазу штрихов (анимация «бегущего» пунктира маршрута).
    """
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return [(x1, y1, x2, y2)]
    ux, uy = dx / length, dy / length
    period = dash + gap
    segs: List[Tuple[float, float, float, float]] = []
    pos = -((offset % period) if period else 0.0)
    while pos < length:
        start = max(pos, 0.0)
        end = min(pos + dash, length)
        if end > start:
            segs.append((x1 + ux * start, y1 + uy * start,
                         x1 + ux * end, y1 + uy * end))
        pos += period
    return segs or [(x1, y1, x2, y2)]


def text_size(text: str, size: float) -> Tuple[float, float]:
    """Приблизительный габарит текста в пикселях (для якорей)."""
    return (len(text) * size * 0.56, size * 1.15)


def anchor_point(text: str, x: float, y: float, size: float,
                 anchor: str) -> Tuple[float, float]:
    """Позиция верхнего левого угла текста для якоря (семантика QPainter).

    В Qt-версии drawText рисовал от базовой линии; DPG draw_text — от верхнего
    левого угла, поэтому 'nw' оставляет (x, y−h), 'center' центрирует по обеим.
    """
    w, h = text_size(text, size)
    if anchor == "center":
        return (x - w / 2.0, y - h / 2.0)
    if anchor == "w":
        return (x, y - h / 2.0)
    if anchor == "n":
        return (x - w / 2.0, y)
    if anchor == "e":
        return (x - w, y - h / 2.0)
    if anchor == "s":
        return (x - w / 2.0, y - h)
    return (x, y)          # 'nw' и прочее


def paint(emit: Emit, prims: Sequence[Dict[str, Any]]) -> int:
    """Нарисовать список примитивов через emit. Возвращает число вызовов."""
    calls = 0
    for p in prims:
        kind = p.get("type")
        alpha = p.get("alpha")
        if kind == "line":
            color = parse_color(p.get("color", "#ffffff"), alpha)
            width = max(1.0, float(p.get("width", 1)))
            segs = (dash_segments(p["x1"], p["y1"], p["x2"], p["y2"],
                                  offset=float(p.get("dash_offset", 0.0)))
                    if p.get("dash") else
                    [(p["x1"], p["y1"], p["x2"], p["y2"])])
            for (ax, ay, bx, by) in segs:
                emit("line", x1=ax, y1=ay, x2=bx, y2=by,
                     color=color, width=width)
                calls += 1
        elif kind == "rect":
            fill = parse_color(p.get("fill"), alpha)
            outline = parse_color(p.get("outline"), alpha)
            emit("rect", x=float(p["x"]), y=float(p["y"]),
                 w=float(p["w"]), h=float(p["h"]), fill=fill,
                 outline=outline, width=max(1.0, float(p.get("width", 1))),
                 dash=bool(p.get("dash")))
            calls += 1
        elif kind == "circle":
            fill = parse_color(p.get("fill"), alpha)
            outline = parse_color(p.get("outline"), alpha)
            emit("circle", x=float(p["x"]), y=float(p["y"]),
                 r=float(p.get("r", 4.0)), fill=fill, outline=outline,
                 width=max(1.0, float(p.get("width", 1))),
                 dash=bool(p.get("dash")))
            calls += 1
        elif kind == "poly":
            pts = [(float(x), float(y)) for x, y in p.get("points", [])]
            if len(pts) < 2:
                continue
            fill = parse_color(p.get("fill"), alpha)
            outline = parse_color(p.get("outline", "#ffffff"), alpha)
            emit("poly", points=pts, fill=fill, outline=outline,
                 width=max(1.0, float(p.get("width", 1))),
                 closed=bool(p.get("closed", True)))
            calls += 1
        elif kind == "text":
            text = str(p.get("text", ""))
            if not text:
                continue
            color = parse_color(p.get("color", "#ffffff"), alpha)
            size = max(6.0, float(p.get("size", 9)) * 1.6)   # pt -> px
            tx, ty = anchor_point(text, float(p["x"]), float(p["y"]), size,
                                  str(p.get("anchor", "nw")))
            emit("text", x=tx, y=ty, text=text, color=color, size=size)
            calls += 1
    return calls
