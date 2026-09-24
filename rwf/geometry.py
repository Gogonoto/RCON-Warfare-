"""
Единый источник правды для плоской геометрии и углов (P1.1).

Формулы курсов/векторов живут ТОЛЬКО здесь. `bases.py`, `units.py`,
`routes.py`, `ai.py`, `maprender.py` делают реэкспорт — исторически эти
функция были написаны в `bases.py` и оттуда импортировались тестами
(`test_carrier.py` импортирует `angle_diff, forward_vec, right_vec, yaw_to`
из `rwf.bases`), поэтому реэкспорт обязателен и не удаляется.

Соглашение о знаках — Minecraft-угол `yaw`:
    0°   = +Z (юг),  90° = −X (запад),  180° = −Z,  270° = +X.
Это зеркало относительно математического полярного угла; путаница знаков
давала дефекты вида «авианосец разворачивается против курса» (BASE), поэтому
вывод формул задокументирован прямо в докстрингах.
"""
from __future__ import annotations

import math
from typing import Tuple

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


def clamp(value: float, lo: float, hi: float) -> float:
    """Зажать значение в отрезок [lo, hi]."""
    return lo if value < lo else hi if value > hi else value


def wrap_deg(angle: float) -> float:
    """Привести угол к диапазону [0, 360)."""
    return angle % 360.0


def normalize_deg(angle: float) -> float:
    """Привести угол к диапазону [-180, 180)."""
    return (angle + 180.0) % 360.0 - 180.0


def forward_vec(heading_deg: float) -> Vec2:
    """Вектор «вперёд» для yaw Minecraft: 0 = +Z, 90 = −X.

    Единичная длина в плоскости XZ; y не возвращается — вертикаль это
    задача тангажа (`forward_vector` в units.py).
    """
    h = math.radians(heading_deg)
    return (-math.sin(h), math.cos(h))


def right_vec(heading_deg: float) -> Vec2:
    """Вектор «вправо» для yaw Minecraft (правая нормаль к forward_vec)."""
    h = math.radians(heading_deg)
    return (math.cos(h), math.sin(h))


def yaw_to(dx: float, dz: float) -> float:
    """Yaw Minecraft, соответствующий направлению (dx, dz).

    Обратная к `forward_vec` операция: `yaw_to(*forward_vec(y)[::-1]) == y`.
    Аргументы — приращения (dx, dz), порядок именно такой.
    """
    return math.degrees(math.atan2(-dx, dz)) % 360.0


def angle_diff(target: float, current: float) -> float:
    """Кратчайшая разница курсов в градусах, [-180, 180].

    Положительна — крутиться «по возрастанию yaw» (влево по Minecraft).
    Напрямую используется рулевыми законами техники и ИИ; второй реализации
    быть не должно (§4.10: не создавать второй источник правды).
    """
    d = (target - current + 180.0) % 360.0 - 180.0
    return d


def distance(p: Vec3, q: Vec3) -> float:
    """Евклидова дистанция между двумя точками пространства."""
    return math.sqrt((p[0] - q[0]) ** 2
                     + (p[1] - q[1]) ** 2
                     + (p[2] - q[2]) ** 2)


def lerp(a: float, b: float, t: float) -> float:
    """Линейная интерполяция: a при t=0, b при t=1."""
    return a + (b - a) * t
