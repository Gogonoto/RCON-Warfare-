"""
Карта: трансформация, слои, растр рельефа.

Модуль намеренно НЕ знает про Qt: он производит список примитивов
(`{'type': 'line'|'rect'|'circle'|'poly'|'text', ...}`) и байтовый буфер RGB
для рельефа. Qt-сторона (`ui/mapview.py`) только переносит их на сцену.
Это даёт тестируемость без дисплея и headless-скриншоты.

Закрытые дефекты наброска
------------------------
* **MAP-01/MAP-02** — рельеф рисовался отдельными `QGraphicsItem` на каждый
  тайл и пересоздавался каждые 300 мс: 10 000 тайлов = 10 000 item'ов на кадр
  = гарантированный фриз. Здесь рельеф растеризуется в ОДИН буфер фиксированного
  размера (по умолчанию 512×512), который Qt масштабирует под вьюпорт;
  перерисовка — только когда изменился рельеф или вид.
* **MAP-06** — `set_center()` сбрасывал `follow`, а рендер тут же включал его
  обратно, поэтому ручное панорамирование откатывалось. Режимы разделены:
  `follow` — явный флаг, панорама его выключает, «F» — включает.
* **MAP-05** — зум центрируется на курсоре (`zoom_at`), а не «уезжает».
* **MAP-07** — маршрут бота рисуется cyan-пунктиром, как и было заявлено.
* **MAP-08** — силуэт юнита симметричный (в наброске были перепутаны знаки).
* **MAP-09** — тайлы центрируются, а не рисуются от левого верхнего угла.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Vec2 = Tuple[float, float]

#: цвета рельефа по типу блока
BLOCK_PALETTE: Dict[str, Tuple[int, int, int]] = {
    "water": (28, 62, 116),
    "lava": (176, 78, 20),
    "grass": (50, 92, 42),
    "sand": (132, 120, 82),
    "snow": (148, 158, 166),
    "stone": (56, 64, 58),
    "leaves": (34, 70, 34),
    "log": (66, 48, 30),
    "dirt": (76, 58, 40),
    "gravel": (78, 80, 76),
    "road": (96, 98, 100),
    "other": (44, 52, 46),
}
UNKNOWN_TILE = (18, 22, 19)


def shade(rgb: Tuple[int, int, int], y: float, y0: float, y1: float) -> Tuple[int, int, int]:
    """Оттенок по высоте: низины темнее, вершины светлее."""
    span = max(1.0, y1 - y0)
    t = max(0.0, min(1.0, (y - y0) / span))
    f = 0.55 + 0.55 * t
    return tuple(int(min(255, c * f)) for c in rgb)   # type: ignore[return-value]


# ---------------------------------------------------------------------------
#  Трансформация
# ---------------------------------------------------------------------------
class MapTransform:
    """Мировые координаты (X, Z) ↔ экранные (px).

    Экранная Y направлена ВНИЗ, мировая Z — на юг, поэтому знаки совпадают:
    +Z вниз по экрану. Центр сцены — центр обзора.
    """

    def __init__(self, size: Vec2 = (900.0, 900.0), view_radius: float = 400.0,
                 center: Vec2 = (0.0, 0.0), min_radius: float = 25.0,
                 max_radius: float = 6000.0):
        self.size = (float(size[0]), float(size[1]))
        self.min_radius = min_radius
        self.max_radius = max_radius
        self.view_radius = float(view_radius)
        self.center = (float(center[0]), float(center[1]))
        self.follow = True
        self._recalc()

    def _recalc(self) -> None:
        # Масштаб по меньшей из сторон, чтобы карта вписывалась в неквадратный вьюпорт
        self.scale = min(self.size[0], self.size[1]) / (2.0 * self.view_radius)
        self.cx = self.size[0] / 2.0
        self.cy = self.size[1] / 2.0

    # ------------------------------------------------------------- размеры
    def resize(self, w: float, h: float) -> None:
        self.size = (max(10.0, float(w)), max(10.0, float(h)))
        self._recalc()

    # -------------------------------------------------------------- центр
    def set_center(self, x: float, z: float, keep_follow: bool = True) -> None:
        """Установить центр. `keep_follow=False` — при ручном панорамировании."""
        self.center = (float(x), float(z))
        if not keep_follow:
            self.follow = False

    def pan(self, dx: float, dz: float) -> None:
        """Сдвинуть центр в мировых координатах; отключает слежение."""
        self.center = (self.center[0] + dx, self.center[1] + dz)
        self.follow = False

    def pan_screen(self, dpx: float, dpy: float) -> None:
        """Сдвинуть на величину в пикселях (для WASD)."""
        self.pan(dpx / self.scale, dpy / self.scale)

    def set_view_radius(self, r: float) -> None:
        self.view_radius = max(self.min_radius, min(self.max_radius, float(r)))
        self._recalc()

    def zoom_by(self, factor: float) -> None:
        self.set_view_radius(self.view_radius * factor)

    def zoom_at(self, factor: float, sx: float, sy: float) -> None:
        """Зум с центром в точке экрана (MAP-05): мировая точка под курсором
        остаётся под курсором."""
        wx, wz = self.to_world(sx, sy)
        self.set_view_radius(self.view_radius * factor)
        nx, ny = self.to_screen(wx, wz)
        self.center = (self.center[0] + (nx - sx) / self.scale,
                       self.center[1] + (ny - sy) / self.scale)

    # ---------------------------------------------------------- преобразования
    def to_screen(self, wx: float, wz: float) -> Vec2:
        return (self.cx + (wx - self.center[0]) * self.scale,
                self.cy + (wz - self.center[1]) * self.scale)

    def to_world(self, sx: float, sy: float) -> Vec2:
        if self.scale <= 0:
            return self.center
        return (self.center[0] + (sx - self.cx) / self.scale,
                self.center[1] + (sy - self.cy) / self.scale)

    def in_bounds(self, sx: float, sy: float, margin: float = 0.0) -> bool:
        return (-margin <= sx <= self.size[0] + margin
                and -margin <= sy <= self.size[1] + margin)

    def visible_world_rect(self) -> Tuple[float, float, float, float]:
        """Мировой прямоугольник, видимый сейчас (для отсечения слоёв)."""
        x0, z0 = self.to_world(0.0, 0.0)
        x1, z1 = self.to_world(self.size[0], self.size[1])
        return (x0, z0, x1, z1)

    def meters_per_pixel(self) -> float:
        return 1.0 / self.scale if self.scale else 1.0


# ---------------------------------------------------------------------------
#  Растр рельефа
# ---------------------------------------------------------------------------
class TerrainRaster:
    """Рельеф в ОДИН RGB-буфер в пространстве тайлов (MAP-01/MAP-02).

    Ключевая идея: картинка строится не под текущий вид, а под сетку тайлов —
    то есть пересчитывается только когда изменился САМ рельеф, а не камера.
    Масштабирует и сдвигает её уже Qt (на C++), поэтому панорамирование и зум
    бесплатны.

    Для сравнения: растеризация под размер вида стоила ~400 мс на кадр, и
    любое движение карты требовало пересчёта. Здесь — ~5 мс один раз на скан.
    """

    #: предел пикселей в растре; крупнее — увеличиваем шаг выборки
    MAX_PIXELS = 1_000_000

    def __init__(self, size: int = 512):
        # `size` оставлен для совместимости интерфейса: теперь это верхняя
        # граница стороны растра при слишком плотной сетке тайлов.
        self.size = max(32, int(size))
        self._cache_key: Optional[Tuple[Any, ...]] = None
        self._cache: Optional[Tuple[bytes, int, int, Tuple[float, float, float, float]]] = None

    def render(self, grid) -> Optional[Tuple[bytes, int, int,
                                             Tuple[float, float, float, float]]]:
        """(rgb_bytes, w, h, world_rect) либо None, если рельефа нет.

        `grid` — живой `TerrainGrid` или словарь-снимок `terrain_snapshot()`.
        """
        if isinstance(grid, dict):
            count = len(grid.get("tiles") or {})
            step = grid.get("step", 8) or 8
            bounds = grid.get("bounds") or (0, 0, 0, 0)
            y_range = grid.get("y_range") or (0, 0)
            getter = (grid.get("tiles") or {}).get
            key = (count, step, bounds, y_range)
        else:
            count = grid.count()
            step = grid.step or 8
            bounds = grid.bounds()
            y_range = grid.y_range()
            getter = lambda k: grid.get(k[0], k[1])      # noqa: E731
            key = grid.descriptor()
        if count == 0:
            self._cache = None
            self._cache_key = None
            return None
        if key == self._cache_key and self._cache is not None:
            return self._cache

        x0, z0, x1, z1 = bounds
        # Число узлов сетки по каждой оси
        nx = (x1 - x0) // step + 1
        nz = (z1 - z0) // step + 1
        # Слишком крупная сетка — прореживаем, иначе растр не собрать за разумное время
        mul = 1
        while nx * nz > self.MAX_PIXELS and mul < 16:
            mul *= 2
        sx_step = step * mul
        xs = list(range(x0, x1 + 1, sx_step))
        zs = list(range(z0, z1 + 1, sx_step))
        w, h = len(xs), len(zs)
        if w == 0 or h == 0:
            return None

        y0, y1 = y_range
        buf = bytearray(w * h * 3)
        for i in range(0, len(buf), 3):
            buf[i:i + 3] = bytes(UNKNOWN_TILE)
        for iy, bz in enumerate(zs):
            row = iy * w * 3
            for ix, bx in enumerate(xs):
                tile = getter((bx, bz))
                if tile is None:
                    tile = (getter((bx + sx_step, bz)) or getter((bx - sx_step, bz))
                            or getter((bx, bz + sx_step)) or getter((bx, bz - sx_step)))
                    if tile is None:
                        continue
                y, kind = tile
                rgb = shade(BLOCK_PALETTE.get(kind, BLOCK_PALETTE["other"]), y, y0, y1)
                off = row + ix * 3
                buf[off], buf[off + 1], buf[off + 2] = rgb

        half = sx_step * 0.5
        world = (x0 - half, z0 - half, x1 + half, z1 + half)
        self._cache = (bytes(buf), w, h, world)
        self._cache_key = key
        return self._cache

    def invalidate(self) -> None:
        self._cache = None
        self._cache_key = None


# ---------------------------------------------------------------------------
#  Слои
# ---------------------------------------------------------------------------
class Layer:
    z = 0
    enabled = True

    def render(self, snap: Dict[str, Any], tf: MapTransform) -> List[Dict[str, Any]]:
        raise NotImplementedError


class GridLayer(Layer):
    """Сетка чанков (16 блоков) с подписями координат."""
    z = 20
    CHUNK = 16

    def __init__(self, label_every: int = 4):
        self.label_every = max(1, label_every)

    def render(self, snap, tf):
        prims: List[Dict[str, Any]] = []
        step = self.CHUNK * tf.scale
        if step < 6:
            return prims
        x0, z0, x1, z1 = tf.visible_world_rect()
        cx = int(math.floor(x0 / self.CHUNK)) * self.CHUNK
        cz = int(math.floor(z0 / self.CHUNK)) * self.CHUNK
        color = "#16281c"
        major = "#234630"
        i = 0
        while cx <= x1:
            sx, _ = tf.to_screen(cx, 0.0)
            c = major if (cx // self.CHUNK) % self.label_every == 0 else color
            prims.append({"type": "line", "x1": sx, "y1": 0.0, "x2": sx,
                          "y2": tf.size[1], "color": c, "width": 1})
            if c is major and step * self.label_every > 30:
                prims.append({"type": "text", "x": sx + 3, "y": 4,
                              "text": str(cx), "color": "#41704c",
                              "anchor": "nw", "size": 8})
            cx += self.CHUNK
            i += 1
            if i > 4000:
                break
        i = 0
        while cz <= z1:
            _, sy = tf.to_screen(0.0, cz)
            c = major if (cz // self.CHUNK) % self.label_every == 0 else color
            prims.append({"type": "line", "x1": 0.0, "y1": sy, "x2": tf.size[0],
                          "y2": sy, "color": c, "width": 1})
            if c is major and step * self.label_every > 30:
                prims.append({"type": "text", "x": 4, "y": sy + 3, "text": str(cz),
                              "color": "#3d6b48", "anchor": "nw", "size": 8})
            cz += self.CHUNK
            i += 1
            if i > 4000:
                break
        return prims


class ZonesLayer(Layer):
    """Зона удара, точка запуска, waypoint, база."""
    z = 30

    def render(self, snap, tf):
        prims: List[Dict[str, Any]] = []
        zone = snap.get("strike_zone")
        if zone:
            x1, z1, x2, z2 = zone
            sx1, sy1 = tf.to_screen(x1, z1)
            sx2, sy2 = tf.to_screen(x2, z2)
            prims.append({"type": "rect", "x": min(sx1, sx2), "y": min(sy1, sy2),
                          "w": abs(sx2 - sx1), "h": abs(sy2 - sy1),
                          "fill": "#ff334422", "outline": "#ff3344", "width": 2,
                          "dash": True})
            prims.append({"type": "text", "x": min(sx1, sx2) + 4, "y": min(sy1, sy2) + 4,
                          "text": "ЗОНА УДАРА", "color": "#ff6666",
                          "anchor": "nw", "size": 9})
        for key, color, label, shape in (
                ("launch_point", "#33ccff", "ТОЧКА ЗАПУСКА", "triangle"),
                ("base", "#44ff88", "БАЗА", "square"),
                ("waypoint", "#ffdd44", "WAYPOINT", "cross")):
            pt = snap.get(key)
            if not pt:
                continue
            sx, sy = tf.to_screen(pt[0], pt[1])
            if shape == "triangle":
                prims.append({"type": "poly",
                              "points": [(sx, sy - 11), (sx + 9, sy + 7), (sx - 9, sy + 7)],
                              "outline": color, "width": 2})
            elif shape == "square":
                prims.append({"type": "rect", "x": sx - 8, "y": sy - 8, "w": 16,
                              "h": 16, "outline": color, "width": 2})
            else:
                prims.append({"type": "line", "x1": sx - 7, "y1": sy, "x2": sx + 7,
                              "y2": sy, "color": color, "width": 2})
                prims.append({"type": "line", "x1": sx, "y1": sy - 7, "x2": sx,
                              "y2": sy + 7, "color": color, "width": 2})
            prims.append({"type": "text", "x": sx, "y": sy - 16, "text": label,
                          "color": color, "anchor": "center", "size": 8})
        return prims


class RouteLayer(Layer):
    """Маршруты: сегменты по действию, шевроны направления, «бриллианты»
    точек, бегущий пунктир активного участка, тусклые пройденные участки."""
    z = 35

    def __init__(self):
        from .routes import ACTION_COLORS, BOT_ROUTE_COLOR
        self.colors = ACTION_COLORS
        self.bot_color = BOT_ROUTE_COLOR
        self._t = 0.0

    @staticmethod
    def _chevrons(x1, y1, x2, y2, color, alpha):
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length < 48.0:
            return []
        ux, uy = dx / length, dy / length
        out = []
        n = int(length // 52)
        for j in range(1, n + 1):
            t = j / (n + 1.0)
            cx, cy = x1 + dx * t, y1 + dy * t
            # две ветви стрелки, отклонённые назад от направления
            for sx_, sy_ in ((-ux * 5 - uy * 3.4, -uy * 5 + ux * 3.4),
                             (-ux * 5 + uy * 3.4, -uy * 5 - ux * 3.4)):
                out.append({"type": "line", "x1": cx, "y1": cy,
                            "x2": cx + sx_, "y2": cy + sy_, "color": color,
                            "width": 1.6, "alpha": alpha})
        return out

    def render(self, snap, tf):
        self._t += 0.033
        prims: List[Dict[str, Any]] = []
        units = snap.get("units", {})
        for uid, route in (snap.get("routes") or {}).items():
            wps = route.get("waypoints") or []
            if not wps:
                continue
            is_bot = route.get("owner_kind") == "bot"
            idx = route.get("current_idx", 0)
            unit = units.get(uid)
            if unit:
                px, pz = unit["pos"][0], unit["pos"][2]
            else:
                px, pz = wps[0]["x"], wps[0]["z"]

            for i, wp in enumerate(wps):
                wx, wz = wp["x"], wp["z"]
                sx1, sy1 = tf.to_screen(px, pz)
                sx2, sy2 = tf.to_screen(wx, wz)
                if not (tf.in_bounds(sx1, sy1, 200) or tf.in_bounds(sx2, sy2, 200)):
                    px, pz = wx, wz
                    continue
                color = self.bot_color if is_bot else \
                    self.colors.get(wp.get("action"), "#dddddd")
                done = bool(wp.get("reached")) or i < idx
                active = i == idx and not done
                if is_bot:
                    prims.append({"type": "line", "x1": sx1, "y1": sy1,
                                  "x2": sx2, "y2": sy2, "color": color,
                                  "dash": True, "width": 1.2, "alpha": 150})
                elif done:
                    prims.append({"type": "line", "x1": sx1, "y1": sy1,
                                  "x2": sx2, "y2": sy2, "color": color,
                                  "width": 1.4, "alpha": 80})
                elif active:
                    prims.append({"type": "line", "x1": sx1, "y1": sy1,
                                  "x2": sx2, "y2": sy2, "color": color,
                                  "width": 3.0, "dash": True,
                                  "dash_offset": self._t * 42.0, "alpha": 255})
                else:
                    prims.append({"type": "line", "x1": sx1, "y1": sy1,
                                  "x2": sx2, "y2": sy2, "color": color,
                                  "width": 2.0, "alpha": 215})
                if not done:
                    prims.extend(self._chevrons(sx1, sy1, sx2, sy2, color,
                                                120 if is_bot else 160))
                # --- точка маршрута: ромб с номером -----------------------
                edge = "#7d8f7d" if done else color
                r = 8.0 if active else 6.5
                prims.append({"type": "poly", "closed": True,
                              "points": [(sx2, sy2 - r), (sx2 + r, sy2),
                                         (sx2, sy2 + r), (sx2 - r, sy2)],
                              "fill": "#0c130c", "outline": edge,
                              "width": 2.2 if active else 1.6,
                              "alpha": 110 if done else 255})
                if active:
                    pulse = 11.0 + 1.6 * math.sin(self._t * 5.0)
                    prims.append({"type": "circle", "x": sx2, "y": sy2,
                                  "r": pulse, "outline": edge, "width": 1,
                                  "alpha": 110})
                prims.append({"type": "text", "x": sx2, "y": sy2,
                              "text": str(i + 1),
                              "color": "#889988" if done else "#ffffff",
                              "anchor": "center", "size": 8})
                act = wp.get("action")
                if act and act != "navigate" and not is_bot:
                    prims.append({"type": "text", "x": sx2, "y": sy2 + 15,
                                  "text": str(act).upper(), "color": color,
                                  "anchor": "center", "size": 7,
                                  "alpha": 200})
                px, pz = wx, wz
        return prims


class MarkersLayer(Layer):
    """Метки событий: взрывы, попадания, аварии, обслуживание, разведка."""
    z = 40
    COLORS = {"bomb": "#ff8844", "nuke": "#ff2222", "impact": "#ffaa33",
              "drop": "#ffcc44",
              "rocket": "#ffcc55", "crash": "#ff4444", "kamikaze": "#ff00aa",
              "service": "#44ff88", "recon": "#ffdd44", "drone": "#ff44ff"}

    def render(self, snap, tf):
        prims: List[Dict[str, Any]] = []
        for m in snap.get("markers", []):
            sx, sy = tf.to_screen(m["x"], m["z"])
            if not tf.in_bounds(sx, sy, 20):
                continue
            color = self.COLORS.get(m.get("kind", ""), "#ff8844")
            kind = m.get("kind", "")
            if kind in ("crash", "kamikaze"):
                prims.append({"type": "line", "x1": sx - 7, "y1": sy - 7,
                              "x2": sx + 7, "y2": sy + 7, "color": color, "width": 2})
                prims.append({"type": "line", "x1": sx - 7, "y1": sy + 7,
                              "x2": sx + 7, "y2": sy - 7, "color": color, "width": 2})
            elif kind == "service":
                prims.append({"type": "rect", "x": sx - 6, "y": sy - 6, "w": 12,
                              "h": 12, "outline": color, "width": 2})
            else:
                prims.append({"type": "circle", "x": sx, "y": sy, "r": 5,
                              "fill": None, "outline": color, "width": 2})
            text = m.get("text") or kind
            prims.append({"type": "text", "x": sx + 9, "y": sy - 5, "text": str(text),
                          "color": color, "anchor": "w", "size": 7})
        return prims


class UnitsLayer(Layer):
    """Силуэты техники с LOD: свой профиль у каждого типа, размер и «вес»
    зависят от зума (при отдалении иконки упрощаются и бледнеют)."""
    z = 50
    COLORS = {
        "aircraft": ("#ffdd44", "#a87f00"),
        "helicopter": ("#66ddff", "#1c7fa8"),
        "drone": ("#ff77ff", "#a03aa0"),
        "tank": ("#a8c47a", "#5a7040"),
        "transport": ("#ffb366", "#a86a2c"),
        "truck": ("#d8c08a", "#8a744a"),
        "apc": ("#b8d0a0", "#6a8050"),
        "missile": ("#ff9955", "#a8541c"),
    }
    BOT_COLORS = {
        "aircraft": ("#22e6ff", "#0d7f96"),
        "helicopter": ("#5cffb0", "#1f8f5c"),
        "drone": ("#a8f0ff", "#3f8fa8"),
        "tank": ("#b8ffc0", "#4f8f57"),
        "transport": ("#ffd0a0", "#b07840"),
        "truck": ("#f0e0b0", "#9a8454"),
        "apc": ("#d0f0c0", "#7a9060"),
        "missile": ("#ffbb77", "#a86a2c"),
    }

    def __init__(self, icon_scale: float = 1.0):
        self.icon_scale = max(0.3, min(4.0, icon_scale))
        self._t = 0.0

    def render(self, snap, tf):
        from .icons import icon_label, lod_for, unit_icon
        self._t += 0.05
        prims: List[Dict[str, Any]] = []
        mpp = tf.meters_per_pixel()
        lod = lod_for(mpp, self.icon_scale)
        for uid, u in (snap.get("units") or {}).items():
            x, y, z = u["pos"]
            sx, sy = tf.to_screen(x, z)
            if not tf.in_bounds(sx, sy, 60):
                continue
            kind = u.get("kind", "aircraft")
            palette = self.BOT_COLORS if u.get("is_bot") else self.COLORS
            fill, outline = palette.get(kind, ("#ffffff", "#888888"))
            yaw = u.get("yaw", 0.0)
            # Фаза несущего винта крутится, когда вертолёт в воздухе
            phase = self._t * (6.0 if u.get("status") in ("flying", "hover") else 0.6)
            prims.extend(unit_icon(kind, sx, sy, yaw, lod, fill, outline,
                                   rotor_phase=phase))
            if not u.get("alive", True):
                prims.append({"type": "line", "x1": sx - lod.size_px, "y1": sy - lod.size_px,
                              "x2": sx + lod.size_px, "y2": sy + lod.size_px,
                              "color": "#ff4444", "width": 2})
                prims.append({"type": "line", "x1": sx - lod.size_px, "y1": sy + lod.size_px,
                              "x2": sx + lod.size_px, "y2": sy - lod.size_px,
                              "color": "#ff4444", "width": 2})
            elif u.get("health_pct", 100) < 60:
                # Индикатор повреждений — дуга под иконкой
                frac = max(0.0, min(1.0, u.get("health_pct", 100) / 100.0))
                w = lod.size_px * 1.6
                prims.append({"type": "line", "x1": sx - w / 2, "y1": sy + lod.size_px + 4,
                              "x2": sx - w / 2 + w * frac, "y2": sy + lod.size_px + 4,
                              "color": "#ff6644" if frac < 0.35 else "#e0c050",
                              "width": 2, "alpha": lod.alpha})
            # рамка выделения: уголки + кольцо
            if snap.get("selection") == uid:
                half = lod.size_px + 7.0
                leg = 6.0
                for cx, cy, dx, dy in ((sx - half, sy - half, 1, 1),
                                       (sx + half, sy - half, -1, 1),
                                       (sx - half, sy + half, 1, -1),
                                       (sx + half, sy + half, -1, -1)):
                    prims.append({"type": "line", "x1": cx, "y1": cy,
                                  "x2": cx + dx * leg, "y2": cy,
                                  "color": "#ffffff", "width": 1.6,
                                  "alpha": 225})
                    prims.append({"type": "line", "x1": cx, "y1": cy,
                                  "x2": cx, "y2": cy + dy * leg,
                                  "color": "#ffffff", "width": 1.6,
                                  "alpha": 225})
                prims.append({"type": "circle", "x": sx, "y": sy,
                              "r": half + 3.0, "outline": "#ffffff",
                              "width": 1, "alpha": 55})
            # вектор путевой скорости
            spd = float(u.get("speed", 0.0) or 0.0)
            if u.get("alive", True) and spd > 2.0:
                yr = math.radians(yaw)
                ln = min(30.0, 6.0 + spd * 0.5)
                vx = -math.sin(yr) * (lod.size_px + 3.0)
                vy = math.cos(yr) * (lod.size_px + 3.0)
                prims.append({"type": "line", "x1": sx + vx, "y1": sy + vy,
                              "x2": sx + vx * (1 + ln / (lod.size_px + 3.0)),
                              "y2": sy + vy * (1 + ln / (lod.size_px + 3.0)),
                              "color": fill, "width": 1.5, "alpha": 110})
            label = ("🤖 " if u.get("is_bot") else "") + f"#{uid} {u.get('label', '')}"
            prims.extend(icon_label(kind, sx, sy, lod, label, fill))
            status = u.get("status", "")
            if lod.show_label and status and status not in ("flying", "idle", "spawned"):
                prims.append({"type": "text", "x": sx, "y": sy + lod.size_px + 12,
                              "text": status, "color": "#ff8866",
                              "anchor": "center", "size": 7, "alpha": lod.alpha})
        return prims


class PlayersLayer(Layer):
    """Игроки: зелёные маркеры с ником и вектором взгляда."""
    z = 60

    def render(self, snap, tf):
        prims: List[Dict[str, Any]] = []
        for name, p in (snap.get("players") or {}).items():
            pos = p["pos"]
            sx, sy = tf.to_screen(pos[0], pos[2])
            if not tf.in_bounds(sx, sy, 40):
                continue
            yaw = math.radians(p.get("yaw", 0.0))
            prims.append({"type": "line", "x1": sx, "y1": sy,
                          "x2": sx - math.sin(yaw) * 14, "y2": sy + math.cos(yaw) * 14,
                          "color": "#2a8f2a", "width": 2})
            prims.append({"type": "circle", "x": sx, "y": sy, "r": 5,
                          "fill": "#33ff33", "outline": "#0a5c0a", "width": 1})
            prims.append({"type": "text", "x": sx + 9, "y": sy - 6, "text": name,
                          "color": "#88ff88", "anchor": "w", "size": 9})
        return prims


class BaseLayer(Layer):
    """Базы: ВПП с разметкой, палуба авианосца с надстройкой и кильватером,
    наземный гарнизон с уголками-скобами. Ориентация, радиус, стоянки."""
    z = 25
    KIND_COLORS = {"airport": "#7fd8ff", "carrier": "#66a8ff", "ground": "#c8e07f"}
    KIND_ICONS = {"airport": "plane", "carrier": "ship", "ground": "home"}

    # ---------------------------------------------------------------- ВПП
    def _airport(self, prims, base, sx, sy, tf, hx, hz, px_, pz_, color):
        L, W = 96.0 * tf.scale, 16.0 * tf.scale
        if L < 14:                      # слишком мелко: только ось и торцы
            prims.append({"type": "line", "x1": sx - hx * L / 2,
                          "y1": sy - hz * L / 2, "x2": sx + hx * L / 2,
                          "y2": sy + hz * L / 2, "color": color, "width": 2,
                          "alpha": 200})
            return
        hw, hl = W / 2.0, L / 2.0
        corners = [(sx + hx * hl + px_ * hw, sy + hz * hl + pz_ * hw),
                   (sx + hx * hl - px_ * hw, sy + hz * hl - pz_ * hw),
                   (sx - hx * hl - px_ * hw, sy - hz * hl - pz_ * hw),
                   (sx - hx * hl + px_ * hw, sy - hz * hl + pz_ * hw)]
        # перрон вокруг полосы
        ow, ol = hw + 5 * tf.scale, hl + 7 * tf.scale
        apron = [(sx + hx * ol + px_ * ow, sy + hz * ol + pz_ * ow),
                 (sx + hx * ol - px_ * ow, sy + hz * ol - pz_ * ow),
                 (sx - hx * ol - px_ * ow, sy - hz * ol - pz_ * ow),
                 (sx - hx * ol + px_ * ow, sy - hz * ol + pz_ * ow)]
        prims.append({"type": "poly", "points": apron, "closed": True,
                      "fill": "#161c17", "outline": color, "width": 1,
                      "alpha": 70})
        # полотно полосы
        prims.append({"type": "poly", "points": corners, "closed": True,
                      "fill": "#242b31", "outline": color, "width": 1.6,
                      "alpha": 235})
        # осевая линия
        ax0, ay0 = sx - hx * (hl - 10 * tf.scale), sy - hz * (hl - 10 * tf.scale)
        ax1, ay1 = sx + hx * (hl - 10 * tf.scale), sy + hz * (hl - 10 * tf.scale)
        prims.append({"type": "line", "x1": ax0, "y1": ay0, "x2": ax1,
                      "y2": ay1, "color": "#dfe7ec", "width": max(1.0, 0.7 * tf.scale),
                      "dash": True, "alpha": 150})
        # пороги: «клавиши» поперёк полосы
        for e in (1.0, -1.0):
            for k in (-6.0, -2.0, 2.0, 6.0):
                bx0 = sx + hx * e * (hl - 1.5 * tf.scale) + px_ * k * tf.scale
                by0 = sy + hz * e * (hl - 1.5 * tf.scale) + pz_ * k * tf.scale
                bx1 = sx + hx * e * (hl - 9.0 * tf.scale) + px_ * k * tf.scale
                by1 = sy + hz * e * (hl - 9.0 * tf.scale) + pz_ * k * tf.scale
                prims.append({"type": "line", "x1": bx0, "y1": by0, "x2": bx1,
                              "y2": by1, "color": "#dfe7ec",
                              "width": max(1.0, 1.1 * tf.scale), "alpha": 190})
        # номер полосы: курс/10 с обоих торцов
        hdg = float(base.get("heading", 0.0))
        n1 = int(hdg // 10) % 36 or 36
        n2 = (n1 + 18) % 36 or 36
        for e, num in ((1.0, n1), (-1.0, n2)):
            tx = sx + hx * e * (hl + 11 * tf.scale)
            ty = sy + hz * e * (hl + 11 * tf.scale)
            prims.append({"type": "text", "x": tx, "y": ty, "text": f"{num:02d}",
                          "color": color, "anchor": "center", "size": 8,
                          "alpha": 210})

    # ------------------------------------------------------------- авианосец
    def _carrier(self, prims, base, sx, sy, tf, hx, hz, px_, pz_, color):
        L, W = 72.0, 20.0            # метры; масштаб ниже
        sc = tf.scale

        def pt(fwd: float, stb: float) -> Tuple[float, float]:
            return (sx + hx * fwd * sc + px_ * stb * sc,
                    sy + hz * fwd * sc + pz_ * stb * sc)

        hull = [pt(L / 2 + 9, 0), pt(L / 2 - 5, W / 2), pt(-L / 2 + 5, W / 2),
                pt(-L / 2, W / 2 - 4), pt(-L / 2, -(W / 2 - 4)),
                pt(-L / 2 + 5, -W / 2), pt(L / 2 - 5, -W / 2)]
        if L * sc < 16:              # далеко: просто вытянутая марка
            a, b = pt(-L / 2, 0), pt(L / 2, 0)
            prims.append({"type": "line", "x1": a[0], "y1": a[1],
                          "x2": b[0], "y2": b[1], "color": color, "width": 3,
                          "alpha": 220})
            return
        prims.append({"type": "poly", "points": hull, "closed": True,
                      "fill": "#27313b", "outline": color, "width": 2,
                      "alpha": 240})
        # угловая посадочная дорожка
        d0, d1 = pt(-L / 2 + 6, W * 0.22), pt(L / 2 - 16, -W * 0.16)
        prims.append({"type": "line", "x1": d0[0], "y1": d0[1], "x2": d1[0],
                      "y2": d1[1], "color": "#e6eef4", "width": max(1.0, 0.8 * sc),
                      "dash": True, "alpha": 140})
        # осевая в носовой части
        c0, c1 = pt(L / 2 - 14, 0), pt(L / 2 + 4, 0)
        prims.append({"type": "line", "x1": c0[0], "y1": c0[1], "x2": c1[0],
                      "y2": c1[1], "color": "#e6eef4", "width": max(1.0, 0.7 * sc),
                      "dash": True, "alpha": 120})
        # надстройка («остров») по правому борту + мачта
        isl = [pt(-10, -W / 2 + 1.5), pt(2, -W / 2 + 1.5),
               pt(2, -W / 2 + 6.5), pt(-10, -W / 2 + 6.5)]
        prims.append({"type": "poly", "points": isl, "closed": True,
                      "fill": color, "outline": None, "alpha": 210})
        m0, m1 = pt(-4, -W / 2 + 1.5), pt(-4, -W / 2 - 2.5)
        prims.append({"type": "line", "x1": m0[0], "y1": m0[1], "x2": m1[0],
                      "y2": m1[1], "color": color, "width": 1, "alpha": 160})
        # стрелка курса впереди носа
        tip = pt(L / 2 + 9, 0)
        for stb in (4.0, -4.0):
            a = pt(L / 2 + 15, 0)
            b = pt(L / 2 + 10, stb)
            prims.append({"type": "line", "x1": b[0], "y1": b[1], "x2": a[0],
                          "y2": a[1], "color": "#bfe2ff", "width": 1.6,
                          "alpha": 190})
        # кильватер, если движется
        if base.get("moving"):
            for stb in (W / 2 - 4, -(W / 2 - 4)):
                w0 = pt(-L / 2, stb)
                w1 = pt(-L / 2 - 18, stb * 1.5)
                prims.append({"type": "line", "x1": w0[0], "y1": w0[1],
                              "x2": w1[0], "y2": w1[1], "color": "#9fc6e8",
                              "width": 1.4, "alpha": 80})
            for d in (10.0, 17.0, 24.0):
                q0 = pt(-L / 2 - d, (W / 2) * (1.0 - d / 60.0))
                q1 = pt(-L / 2 - d, -(W / 2) * (1.0 - d / 60.0))
                prims.append({"type": "line", "x1": q0[0], "y1": q0[1],
                              "x2": q1[0], "y2": q1[1], "color": "#9fc6e8",
                              "width": 1, "alpha": 55})

    # --------------------------------------------------------------- наземная
    @staticmethod
    def _ground(prims, sx, sy, color):
        r = 13.0
        prims.append({"type": "rect", "x": sx - r, "y": sy - r, "w": 2 * r,
                      "h": 2 * r, "fill": "#2c3a22", "outline": color,
                      "width": 2, "alpha": 225})
        g, leg = r + 5.0, 7.0
        for cx, cy, dx, dy in ((sx - g, sy - g, 1, 1), (sx + g, sy - g, -1, 1),
                               (sx - g, sy + g, 1, -1), (sx + g, sy + g, -1, -1)):
            prims.append({"type": "line", "x1": cx, "y1": cy,
                          "x2": cx + dx * leg, "y2": cy, "color": color,
                          "width": 2, "alpha": 200})
            prims.append({"type": "line", "x1": cx, "y1": cy, "x2": cx,
                          "y2": cy + dy * leg, "color": color, "width": 2,
                          "alpha": 200})
        prims.append({"type": "line", "x1": sx - r * 0.5, "y1": sy,
                      "x2": sx + r * 0.5, "y2": sy, "color": color, "width": 2})
        prims.append({"type": "line", "x1": sx, "y1": sy - r * 0.5, "x2": sx,
                      "y2": sy + r * 0.5, "color": color, "width": 2})

    def render(self, snap, tf):
        from . import uicons
        prims: List[Dict[str, Any]] = []
        sel_base = snap.get("selected_base")
        for base in snap.get("bases") or []:
            bx, bz = base["x"], base["z"]
            sx, sy = tf.to_screen(bx, bz)
            if not tf.in_bounds(sx, sy, 240):
                continue
            color = self.KIND_COLORS.get(base["kind"], "#cccccc")
            heading = math.radians(base.get("heading", 0.0))
            hx, hz = -math.sin(heading), math.cos(heading)
            px_, pz_ = math.cos(heading), math.sin(heading)
            kind = base["kind"]

            # радиус действия: пунктирное кольцо с рисками
            r_px = max(6.0, base.get("radius", 120.0) * tf.scale)
            prims.append({"type": "circle", "x": sx, "y": sy, "r": r_px,
                          "fill": None, "outline": color, "width": 1,
                          "dash": True, "alpha": 62})
            for ang in (45, 135, 225, 315):
                a = math.radians(ang)
                prims.append({"type": "line",
                              "x1": sx + math.cos(a) * (r_px - 4),
                              "y1": sy + math.sin(a) * (r_px - 4),
                              "x2": sx + math.cos(a) * (r_px + 4),
                              "y2": sy + math.sin(a) * (r_px + 4),
                              "color": color, "width": 1, "alpha": 110})

            if kind == "airport":
                self._airport(prims, base, sx, sy, tf, hx, hz, px_, pz_, color)
            elif kind == "carrier":
                self._carrier(prims, base, sx, sy, tf, hx, hz, px_, pz_, color)
            else:
                self._ground(prims, sx, sy, color)

            # стоянки: круг с точкой, занятая — янтарная
            for pad in base.get("pads") or []:
                ox, oz = pad["offset"]
                psx, psy = tf.to_screen(bx + ox, bz + oz)
                occupied = pad.get("occupied_by") is not None
                pr = max(3.0, 6.0 * tf.scale)
                prims.append({"type": "circle", "x": psx, "y": psy, "r": pr,
                              "fill": "#ff8855" if occupied else None,
                              "outline": "#ffb27f" if occupied else color,
                              "width": 1.4,
                              "alpha": 235 if occupied else 130})
                if occupied:
                    prims.append({"type": "circle", "x": psx, "y": psy,
                                  "r": max(1.0, pr * 0.35), "fill": "#2a1408",
                                  "outline": None})

            # рамка выделения базы
            if sel_base == base.get("id"):
                half = max(18.0, min(r_px * 0.45, 46.0))
                leg = 8.0
                for cx, cy, dx, dy in ((sx - half, sy - half, 1, 1),
                                       (sx + half, sy - half, -1, 1),
                                       (sx - half, sy + half, 1, -1),
                                       (sx + half, sy + half, -1, -1)):
                    prims.append({"type": "line", "x1": cx, "y1": cy,
                                  "x2": cx + dx * leg, "y2": cy,
                                  "color": "#ffffff", "width": 2, "alpha": 215})
                    prims.append({"type": "line", "x1": cx, "y1": cy,
                                  "x2": cx, "y2": cy + dy * leg,
                                  "color": "#ffffff", "width": 2, "alpha": 215})

            # подпись: иконка типа + имя
            name = str(base.get("name", ""))
            tsize = 13.0
            tw = len(name) * tsize * 0.60
            ly = sy - max(20.0, r_px * 0.30) - 16.0
            total = 15.0 + 4.0 + tw
            x0 = sx - total / 2.0
            prims.extend(uicons.prims(self.KIND_ICONS.get(kind, "target"),
                                      x0 + 7.0, ly + tsize * 0.55, 14.0,
                                      color, 1.5))
            prims.append({"type": "text", "x": x0 + 15.0, "y": ly,
                          "text": name, "color": color, "anchor": "nw",
                          "size": 9})
        return prims


class PlannerLayer(Layer):
    """Черновик маршрута, который сейчас рисует пользователь."""
    z = 65

    def __init__(self):
        self.points: List[Tuple[float, float, float, str]] = []   # x, z, alt, action
        self.cursor: Optional[Vec2] = None
        self.colors: Dict[str, str] = {}

    def set_colors(self, colors: Dict[str, str]) -> None:
        self.colors = colors

    def render(self, snap, tf):
        prims: List[Dict[str, Any]] = []
        prev: Optional[Vec2] = None
        if self.points:
            first = self.points[0]
            prev = tf.to_screen(first[0], first[1])
        for i, (x, z, _alt, action) in enumerate(self.points):
            sx, sy = tf.to_screen(x, z)
            color = self.colors.get(action, "#ffffff")
            if prev is not None and i > 0:
                prims.append({"type": "line", "x1": prev[0], "y1": prev[1],
                              "x2": sx, "y2": sy, "color": color, "width": 2,
                              "dash": True})
            prims.append({"type": "circle", "x": sx, "y": sy, "r": 7,
                          "fill": "#101810", "outline": color, "width": 2})
            prims.append({"type": "text", "x": sx, "y": sy, "text": str(i + 1),
                          "color": "#ffffff", "anchor": "center", "size": 9})
            prev = (sx, sy)
        if self.cursor is not None and prev is not None:
            cx, cy = tf.to_screen(*self.cursor)
            prims.append({"type": "line", "x1": prev[0], "y1": prev[1],
                          "x2": cx, "y2": cy, "color": "#ffffff", "width": 1,
                          "dash": True, "alpha": 120})
        return prims


class HudLayer(Layer):
    """HUD: виньетка по кромке, рамка с уголками, компас с рисками и
    стрелкой севера, контрастная масштабная линейка, служебная строка."""
    z = 90

    def render(self, snap, tf):
        prims: List[Dict[str, Any]] = []
        w, h = tf.size

        # --- виньетка: полосы по кромке дают «стеклянную» глубину ----------
        band = 20.0
        for x, y, ww, hh, al in ((0, 0, w, band, 40), (0, h - band, w, band, 40),
                                 (0, 0, band, h, 26), (w - band, 0, band, h, 26)):
            prims.append({"type": "rect", "x": x, "y": y, "w": ww, "h": hh,
                          "fill": "#000000", "outline": None, "alpha": al})
        prims.append({"type": "rect", "x": 0.5, "y": 0.5, "w": w - 1,
                      "h": h - 1, "fill": None, "outline": "#3a5a42",
                      "width": 1, "alpha": 130})
        # уголки-скобы по углам
        leg = 22.0
        for cx, cy, dx, dy in ((7, 7, 1, 1), (w - 7, 7, -1, 1),
                               (7, h - 7, 1, -1), (w - 7, h - 7, -1, -1)):
            prims.append({"type": "line", "x1": cx, "y1": cy,
                          "x2": cx + dx * leg, "y2": cy, "color": "#79c98a",
                          "width": 2, "alpha": 200})
            prims.append({"type": "line", "x1": cx, "y1": cy, "x2": cx,
                          "y2": cy + dy * leg, "color": "#79c98a", "width": 2,
                          "alpha": 200})
        # перекрестие в центре обзора
        ccx, ccy = w / 2.0, h / 2.0
        for dx, dy in ((1, 0), (0, 1)):
            prims.append({"type": "line", "x1": ccx - dx * 6, "y1": ccy - dy * 6,
                          "x2": ccx + dx * 6, "y2": ccy + dy * 6,
                          "color": "#8fbf9f", "width": 1, "alpha": 60})

        # --- масштабная линейка: круглое число метров, контрастные доли ----
        # HUD-03: длина линейки ограничена половиной ширины карты, иначе
        # полоса и её подпись наезжали на служебную строку и уголки рамки.
        target_px = 110.0
        meters = target_px / tf.scale if tf.scale else 100.0
        steps = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)
        for step in steps:
            if step >= meters:
                meters = step
                break
        px = meters * tf.scale
        if px > w * 0.5:
            prev = [st for st in steps if st * tf.scale <= w * 0.5]
            meters = prev[-1] if prev else steps[0]
            px = meters * tf.scale
        x, y = 22.0, h - 28.0
        prims.append({"type": "rect", "x": x, "y": y - 3, "w": px / 2.0,
                      "h": 6, "fill": "#c8dcc8", "outline": None, "alpha": 210})
        prims.append({"type": "rect", "x": x + px / 2.0, "y": y - 3,
                      "w": px / 2.0, "h": 6, "fill": None,
                      "outline": "#c8dcc8", "width": 1, "alpha": 210})
        for dx in (0.0, px / 2.0, px):
            prims.append({"type": "line", "x1": x + dx, "y1": y - 6,
                          "x2": x + dx, "y2": y + 6, "color": "#c8dcc8",
                          "width": 1.5, "alpha": 230})
        lbl_x = x + px + 10
        if lbl_x + 46 > w:                      # подпись не за край карты
            lbl_x = max(22.0, w - 56.0)
        prims.append({"type": "text", "x": lbl_x, "y": y - 5,
                      "text": f"{meters} м", "color": "#d5e6d5",
                      "anchor": "w", "size": 9})

        # --- компас: кольцо с рисками, стрелка севера, буквы сторон ---------
        cx, cy, r = w - 40.0, 46.0, 24.0
        prims.append({"type": "circle", "x": cx, "y": cy, "r": r,
                      "fill": "#0b120b", "outline": "#3d6b48", "width": 1.4,
                      "alpha": 235})
        for deg in range(0, 360, 30):
            a = math.radians(deg)
            r0 = r - (5 if deg % 90 else 8)
            prims.append({"type": "line",
                          "x1": cx + math.cos(a) * r0,
                          "y1": cy + math.sin(a) * r0,
                          "x2": cx + math.cos(a) * (r - 1.5),
                          "y2": cy + math.sin(a) * (r - 1.5),
                          "color": "#7fa37f", "width": 1.2, "alpha": 190})
        # стрелка: северная половина красная, южная — серая
        prims.append({"type": "poly", "closed": True,
                      "points": [(cx, cy - r + 7), (cx - 3.5, cy + 2),
                                 (cx + 3.5, cy + 2)],
                      "fill": "#e0665c", "outline": None, "alpha": 230})
        prims.append({"type": "poly", "closed": True,
                      "points": [(cx, cy + r - 7), (cx - 3.5, cy - 2),
                                 (cx + 3.5, cy - 2)],
                      "fill": "#9fbf9f", "outline": None, "alpha": 130})
        prims.append({"type": "circle", "x": cx, "y": cy, "r": 1.8,
                      "fill": "#e8f2e8", "outline": None})
        for label, ang in (("С", 180.0), ("В", 270.0), ("Ю", 0.0), ("З", 90.0)):
            a = math.radians(ang)
            tx = cx - math.sin(a) * (r + 9)
            ty = cy + math.cos(a) * (r + 9)
            prims.append({"type": "text", "x": tx, "y": ty, "text": label,
                          "color": "#ff8877" if label == "С" else "#9fbf9f",
                          "anchor": "center", "size": 8})

        # --- служебная строка ----------------------------------------------
        info = []
        if snap.get("scanning"):
            done, total = snap.get("scan_progress", (0, 1))
            info.append(f"скан {done}/{total}")
        info.append(f"{len(snap.get('units') or {})} юнитов")
        info.append(f"{len(snap.get('players') or {})} игроков")
        info.append(f"{snap.get('terrain_tiles', 0)} тайлов")
        # HUD-04: служебная строка перенесена ВВЕРХ слева — внизу она
        # сталкивалась с масштабной линейкой и уголками рамки.
        prims.append({"type": "text", "x": 40.0, "y": 24.0,
                      "text": "  ·  ".join(info), "color": "#8fb38f",
                      "anchor": "nw", "size": 9})
        cx0, cz0, cx1, cz1 = tf.visible_world_rect()
        prims.append({"type": "text", "x": 40.0, "y": 38.0,
                      "text": f"центр {tf.center[0]:.0f}, {tf.center[1]:.0f}   "
                             f"обзор ±{tf.view_radius:.0f} м",
                      "color": "#678767", "anchor": "nw", "size": 8})
        return prims


# ---------------------------------------------------------------------------
#  Рендерер
# ---------------------------------------------------------------------------
class MapRenderer:
    """Собирает примитивы всех слоёв по снимку мира."""

    def __init__(self, size: Vec2 = (900.0, 900.0), view_radius: float = 400.0,
                 raster_size: int = 512):
        self.transform = MapTransform(size=size, view_radius=view_radius)
        self.raster = TerrainRaster(raster_size)
        self.terrain_layer_enabled = True
        self.planner = PlannerLayer()
        self.units_layer = UnitsLayer()
        self.base_layer = BaseLayer()
        self.layers: List[Layer] = [
            GridLayer(), self.base_layer, ZonesLayer(), RouteLayer(),
            MarkersLayer(), self.units_layer, PlayersLayer(), self.planner,
            HudLayer(),
        ]
        self._by_name: Dict[str, Layer] = {
            "grid": self.layers[0], "bases": self.layers[1],
            "zones": self.layers[2], "routes": self.layers[3],
            "markers": self.layers[4], "units": self.layers[5],
            "players": self.layers[6], "planner": self.planner,
            "hud": self.layers[8],
        }
        self.planner.set_colors({})

    # ------------------------------------------------------------- слои
    def layer(self, name: str) -> Layer:
        return self._by_name[name]

    def layer_enabled(self, name: str) -> bool:
        return bool(self.layer(name).enabled)

    def set_layer_enabled(self, name: str, enabled: bool) -> None:
        if name in self._by_name:
            self._by_name[name].enabled = enabled

    def set_action_colors(self, colors: Dict[str, str]) -> None:
        self.planner.set_colors(colors)

    def follow_unit(self, snap: Dict[str, Any], prefer_uid: Optional[int] = None) -> None:
        """Центрировать на выбранном юните, иначе на первом, иначе на игроке."""
        if not self.transform.follow:
            return
        units = snap.get("units") or {}
        target = None
        if prefer_uid is not None and prefer_uid in units:
            target = units[prefer_uid]["pos"]
        elif units:
            target = next(iter(units.values()))["pos"]
        else:
            players = snap.get("players") or {}
            if players:
                target = next(iter(players.values()))["pos"]
        if target is not None:
            self.transform.set_center(target[0], target[2], keep_follow=True)

    # -------------------------------------------------------------- кадр
    def render(self, snap: Dict[str, Any],
               terrain: Optional[Dict[str, Any]] = None,
               follow_uid: Optional[int] = None) -> List[Dict[str, Any]]:
        self.follow_unit(snap, follow_uid)
        tf = self.transform
        prims: List[Dict[str, Any]] = []
        for layer in sorted(self.layers, key=lambda l: l.z):
            if not layer.enabled:
                continue
            try:
                prims.extend(layer.render(snap, tf))
            except Exception:  # noqa: BLE001 - слой не должен ронять карту
                continue
        return prims

    def terrain_image(self, grid) -> Optional[Tuple[bytes, int, int,
                                                     Tuple[float, float, float, float]]]:
        """Растр рельефа в пространстве тайлов + мировой прямоугольник, который
        он покрывает. Не зависит от камеры — кэш сбрасывается только при
        изменении рельефа."""
        if not self.terrain_layer_enabled:
            return None
        return self.raster.render(grid)

    # --------------------------------------------------------- convenience
    def screen_to_world(self, sx: float, sy: float) -> Vec2:
        return self.transform.to_world(sx, sy)

    def world_to_screen(self, wx: float, wz: float) -> Vec2:
        return self.transform.to_screen(wx, wz)
