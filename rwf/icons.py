"""
Силуэты техники и LOD для карты.

Закрывает жалобы:
* «вертолёт не выглядит как вертолёт» — вместо одинаковой стрелки у каждого
  типа свой силуэт ВИД СВЕРХУ: самолёт (фюзеляж + стреловидное крыло),
  вертолёт (корпус + хвостовая балка + несущий и рулевой винты), БПЛА
  (квадрат-крест с четырьмя роторами), танк (корпус + гусеницы + башня + ствол).
* «изменение размеров иконок и их тяжести при удалении» — LOD: размер иконок
  следует за зумом в заданных пределах, при отдалении силуэт упрощается
  (пропадают винты/гусеницы/подписи) и становится полупрозрачным («легче»),
  чтобы дальние юниты не перекрывали собой карту.

Локальная система координат силуэта: `lf` — вперёд (нос), `lr` — вправо,
нормированы примерно к [-1, 1]. Поворот на экране по yaw выполняется здесь же,
поэтому слои карты просто вызывают `unit_icon(...)`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

Pt = Tuple[float, float]          # (lr, lf) локально
Prim = Dict[str, object]


# ---------------------------------------------------------------------------
#  Силуэты
# ---------------------------------------------------------------------------
@dataclass
class Silhouette:
    """Набор полигонов/линий/окружностей вида сверху."""
    name: str
    hull: List[List[Pt]] = field(default_factory=list)      # основные полигоны
    detail: List[List[Pt]] = field(default_factory=list)    # видно только вблизи
    lines: List[Tuple[Pt, Pt]] = field(default_factory=list)   # ствол, балка
    detail_lines: List[Tuple[Pt, Pt]] = field(default_factory=list)
    rotors: List[Tuple[Pt, float]] = field(default_factory=list)   # (центр, радиус)
    rotor_blades: int = 0            # число лопастей несущего винта
    turret: Optional[Tuple[Pt, float]] = None               # (центр, радиус) башни
    label_dy: float = -1.6           # смещение подписи вверх (в локальных ед.)


SILHOUETTES: Dict[str, Silhouette] = {
    # --- самолёт: фюзеляж, стреловидное крыло, стабилизатор ---------------
    "aircraft": Silhouette(
        name="Самолёт",
        hull=[
            [(-0.14, -0.95), (0.14, -0.95), (0.18, 0.45), (0.0, 1.05), (-0.18, 0.45)],
            [(-1.05, -0.30), (-0.16, 0.10), (-0.16, -0.30), (-0.95, -0.55)],
            [(1.05, -0.30), (0.16, 0.10), (0.16, -0.30), (0.95, -0.55)],
        ],
        detail=[
            [(-0.55, -0.98), (-0.14, -0.72), (-0.14, -0.95)],
            [(0.55, -0.98), (0.14, -0.72), (0.14, -0.95)],
        ],
        lines=[],
        label_dy=-1.5,
    ),
    # --- вертолёт: корпус, хвостовая балка, несущий и рулевой винты --------
    "helicopter": Silhouette(
        name="Вертолёт",
        hull=[
            [(-0.30, -0.45), (0.30, -0.45), (0.34, 0.35), (0.0, 0.80), (-0.34, 0.35)],
            [(-0.07, -0.45), (0.07, -0.45), (0.06, -1.15), (-0.06, -1.15)],
        ],
        detail=[
            [(-0.16, -1.28), (0.16, -1.28), (0.16, -1.02), (-0.16, -1.02)],  # киль
        ],
        lines=[],
        detail_lines=[
            ((-0.06, -1.15), (0.06, -1.15)),      # рулевой винт (линия)
        ],
        rotors=[((0.0, 0.15), 1.15)],            # несущий винт: втулка+ометаемая зона
        rotor_blades=4,
        label_dy=-1.7,
    ),
    # --- БПЛА: квадрат-крест с четырьмя роторами ---------------------------
    "drone": Silhouette(
        name="БПЛА",
        hull=[
            [(-0.22, -0.22), (0.22, -0.22), (0.22, 0.22), (-0.22, 0.22)],
        ],
        detail=[
            [(-0.62, -0.62), (-0.30, -0.30), (-0.42, -0.18), (-0.74, -0.50)],
            [(0.62, -0.62), (0.30, -0.30), (0.42, -0.18), (0.74, -0.50)],
            [(-0.62, 0.62), (-0.30, 0.30), (-0.42, 0.18), (-0.74, 0.50)],
            [(0.62, 0.62), (0.30, 0.30), (0.42, 0.18), (0.74, 0.50)],
        ],
        lines=[],
        rotors=[((-0.62, -0.62), 0.34), ((0.62, -0.62), 0.34),
                ((-0.62, 0.62), 0.34), ((0.62, 0.62), 0.34)],
        rotor_blades=0,
        label_dy=-1.3,
    ),
    # --- танк: корпус, гусеницы, башня, ствол ------------------------------
    "tank": Silhouette(
        name="Танк",
        hull=[
            [(-0.42, -0.85), (0.42, -0.85), (0.46, 0.62), (-0.46, 0.62)],
        ],
        detail=[
            [(-0.66, -0.90), (-0.46, -0.90), (-0.46, 0.70), (-0.66, 0.70)],
            [(0.46, -0.90), (0.66, -0.90), (0.66, 0.70), (0.46, 0.70)],
        ],
        lines=[((0.0, 0.15), (0.0, 1.25))],       # ствол
        detail_lines=[],
        turret=((0.0, 0.10), 0.34),
        label_dy=-1.5,
    ),
    # --- транспортник: прямое высокорасположенное крыло, два винта --------
    "transport": Silhouette(
        name="Транспортник",
        hull=[
            [(-0.16, -1.00), (0.16, -1.00), (0.20, 0.55), (0.0, 1.10),
             (-0.20, 0.55)],
            [(-1.10, 0.05), (1.10, 0.05), (1.10, 0.30), (-1.10, 0.30)],
            [(-0.62, -0.95), (0.62, -0.95), (0.62, -0.72), (-0.62, -0.72)],
        ],
        detail=[],
        lines=[],
        detail_lines=[],
        rotors=[((-0.42, 0.18), 0.22), ((0.42, 0.18), 0.22)],
        rotor_blades=2,
        label_dy=-1.6,
    ),
    # --- грузовик: кабина + кузов ------------------------------------------
    "truck": Silhouette(
        name="Грузовик",
        hull=[
            [(-0.30, -0.95), (0.30, -0.95), (0.30, 0.10), (-0.30, 0.10)],
            [(-0.26, 0.18), (0.26, 0.18), (0.28, 0.80), (-0.28, 0.80)],
        ],
        detail=[
            [(-0.42, -0.80), (-0.32, -0.80), (-0.32, 0.70), (-0.42, 0.70)],
            [(0.32, -0.80), (0.42, -0.80), (0.42, 0.70), (0.32, 0.70)],
        ],
        lines=[],
        label_dy=-1.4,
    ),
    # --- БТР: длинный корпус, восемь колёс, башенка ------------------------
    "apc": Silhouette(
        name="БТР",
        hull=[
            [(-0.34, -0.90), (0.34, -0.90), (0.40, 0.55), (0.0, 1.00),
             (-0.40, 0.55)],
        ],
        detail=[
            [(-0.50, -0.75), (-0.38, -0.75), (-0.38, 0.60), (-0.50, 0.60)],
            [(0.38, -0.75), (0.50, -0.75), (0.50, 0.60), (0.38, 0.60)],
        ],
        lines=[((0.0, 0.10), (0.0, 0.72))],      # ствол пушки
        turret=((0.0, 0.05), 0.24),
        label_dy=-1.4,
    ),
    # --- ракета / боеприпас (для блочных ракет): вытянутый ромб -----------
    "missile": Silhouette(
        name="Ракета",
        hull=[[(0.0, 1.0), (0.18, 0.0), (0.0, -0.7), (-0.18, 0.0)]],
        detail=[
            [(-0.34, -0.55), (0.0, -0.30), (0.34, -0.55), (0.0, -0.72)],
        ],
        lines=[],
        label_dy=-1.2,
    ),
}

DEFAULT_SILHOUETTE = "aircraft"


# ---------------------------------------------------------------------------
#  LOD
# ---------------------------------------------------------------------------
@dataclass
class Lod:
    """Уровень детализации и «вес» иконки при данном зуме."""
    size_px: float          # характерный радиус иконки в пикселях
    detail: int             # 0 = точка, 1 = силуэт, 2 = силуэт + лопасти/мелочи
    alpha: int              # 0..255, «тяжесть»: вдали иконка легче
    show_label: bool
    show_rotors: bool       # ометаемый диск несущего винта
    show_blades: bool       # сами лопасти (только крупно)

    @property
    def clickable(self) -> bool:
        return self.size_px >= 3.0


#: характерный габарит техники в метрах (размах / длина) для расчёта размера
CRAFT_SPAN_M = 14.0


def lod_for(meters_per_pixel: float, user_scale: float = 1.0,
            min_px: float = 8.0, max_px: float = 34.0) -> Lod:
    """Переводит масштаб карты в размер/детальность/«вес» иконки.

    Иконка держит физический габарит юнита (размах ~14 м), но НЕ уменьшается
    ниже `min_px`: на тактической карте знак обязан оставаться читаемым на любом
    зуме. «Тяжесть» при отдалении передаётся иначе — падает непрозрачность,
    исчезают подписи и мелкие детали (лопасти, киль, гусеницы), силуэт
    упрощается. Так рой дальних юнитов не забивает карту, но и не превращается
    в невидимую пыль.

    Пороги под типовые zoom'ы:
      mpp≈0.3 (±120 м)  -> крупно: лопасти, подписи, полный силуэт;
      mpp≈1.0 (±400 м)  -> силуэт + диск винта + подпись;
      mpp≈2   (±800 м)  -> силуэт + диск винта, без подписи, слегка бледнее;
      mpp≈8   (±3200 м) -> силуэт, заметно бледнее, без подписей и винта.
    """
    mpp = max(1e-6, meters_per_pixel)
    natural = (CRAFT_SPAN_M / mpp) * 0.5 * user_scale
    size = max(min_px, min(max_px, natural))
    detail = 2 if size >= 16.0 else 1
    show_label = mpp <= 1.6
    show_rotors = mpp <= 4.0
    show_blades = size >= 16.0
    # «Вес»: чем дальше, тем прозрачнее, но не исчезает совсем
    alpha = int(max(140, min(255, 255 - (mpp - 1.0) * 22)))
    return Lod(size_px=size, detail=detail, alpha=alpha, show_label=show_label,
               show_rotors=show_rotors, show_blades=show_blades)


# ---------------------------------------------------------------------------
#  Отрисовка
# ---------------------------------------------------------------------------
def _rotate(lr: float, lf: float, sy: float, cy: float) -> Tuple[float, float]:
    """Локальные (вправо, вперёд) -> экранные смещения при yaw."""
    return (lr * cy + lf * (-sy), lr * sy + lf * cy)


def unit_icon(kind: str, sx: float, sy: float, yaw_deg: float, lod: Lod,
              fill: str, outline: str, alpha: Optional[int] = None,
              rotor_phase: float = 0.0) -> List[Prim]:
    """Примитивы силуэта юнита на экране."""
    a = lod.alpha if alpha is None else alpha
    sil = SILHOUETTES.get(kind, SILHOUETTES[DEFAULT_SILHOUETTE])
    r = math.radians(yaw_deg)
    sy_, cy_ = math.sin(r), math.cos(r)
    s = lod.size_px
    prims: List[Prim] = []

    def P(lr: float, lf: float) -> Tuple[float, float]:
        dx, dy = _rotate(lr, lf, sy_, cy_)
        return (sx + dx * s, sy - dy * s)      # локальный «вперёд» = вверх экрана

    if lod.detail == 0:
        prims.append({"type": "circle", "x": sx, "y": sy, "r": max(2.0, s * 0.5),
                      "fill": fill, "outline": outline, "width": 1, "alpha": a})
        return prims

    polys = list(sil.hull)
    if lod.detail >= 2:
        polys += sil.detail
    for poly in polys:
        prims.append({"type": "poly", "points": [P(lr, lf) for lr, lf in poly],
                      "fill": fill, "outline": outline, "width": 1, "alpha": a})

    for (p0, p1) in sil.lines:
        x0, y0 = P(*p0); x1, y1 = P(*p1)
        prims.append({"type": "line", "x1": x0, "y1": y0, "x2": x1, "y2": y1,
                      "color": outline, "width": max(1.0, s * 0.12), "alpha": a})
    if lod.detail >= 2:
        for (p0, p1) in sil.detail_lines:
            x0, y0 = P(*p0); x1, y1 = P(*p1)
            prims.append({"type": "line", "x1": x0, "y1": y0, "x2": x1, "y2": y1,
                          "color": outline, "width": 1, "alpha": a})

    # Башня танка
    if sil.turret and lod.detail >= 1:
        (cx, cy), cr = sil.turret
        px, py = P(cx, cy)
        prims.append({"type": "circle", "x": px, "y": py, "r": cr * s,
                      "fill": outline, "outline": outline, "width": 1, "alpha": a})

    # Несущий винт: ометаемый диск всегда (когда виден силуэт), лопасти крупно
    if sil.rotors and lod.show_rotors:
        for (cx, cy), cr in sil.rotors:
            px, py = P(cx, cy)
            prims.append({"type": "circle", "x": px, "y": py, "r": cr * s,
                          "fill": None, "outline": outline, "width": 1,
                          "alpha": max(60, a // 2), "dash": not lod.show_blades})
            if sil.rotor_blades and lod.show_blades:
                for b in range(sil.rotor_blades):
                    ang = rotor_phase + b * (2 * math.pi / sil.rotor_blades)
                    bx, by = math.cos(ang) * cr, math.sin(ang) * cr
                    x0, y0 = P(cx, cy)
                    x1, y1 = P(cx + bx, cy + by)
                    prims.append({"type": "line", "x1": x0, "y1": y0,
                                  "x2": x1, "y2": y1, "color": outline,
                                  "width": max(1.0, s * 0.10), "alpha": a})
            elif sil.rotor_blades:
                # Средний LOD: одна линия-«размах» вместо лопастей
                x0, y0 = P(cx - cr, cy)
                x1, y1 = P(cx + cr, cy)
                prims.append({"type": "line", "x1": x0, "y1": y0, "x2": x1, "y2": y1,
                              "color": outline, "width": 1, "alpha": max(60, a // 2)})
    return prims


def icon_label(kind: str, sx: float, sy: float, lod: Lod, text: str,
               color: str) -> List[Prim]:
    if not lod.show_label:
        return []
    sil = SILHOUETTES.get(kind, SILHOUETTES[DEFAULT_SILHOUETTE])
    return [{"type": "text", "x": sx, "y": sy + sil.label_dy * lod.size_px,
             "text": text, "color": color, "anchor": "center", "size": 8,
             "alpha": lod.alpha}]


def silhouette_name(kind: str) -> str:
    return SILHOUETTES.get(kind, SILHOUETTES[DEFAULT_SILHOUETTE]).name
