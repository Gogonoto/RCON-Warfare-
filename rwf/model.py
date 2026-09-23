"""
Модель техники из блоков.

Два дефекта наброска, которые здесь закрыты
-------------------------------------------
**UNIT-11 — след из блоков.** `clear_model()` вызывался для НОВОЙ позиции, а
старая не убиралась: за самолётом тянулась сплошная стена блоков через всю карту.

**UNIT-12 — разрушение мира.** `fill x-6 y-2 z-6 x+6 y+3 z+6 air replace`
стирал реальный ландшафт и постройки в объёме 13×6×13 вокруг юнита каждый тик.

Решение — **дифф-обновление по точному списку своих блоков**:

* модель помнит координаты и содержимое каждого поставленного ею блока;
* на кадр вычисляются только `writes` (чего раньше не было) и `clears`
  (чего больше нет) — при движении на 1 блок это 5-10 команд вместо 20;
* убираются исключительно те блоки, которые модель сама поставила. Чужой
  ландшафт не трогается вообще;
* координаты округляются `round()`, а не `int()`: на отрицательных X/Z
  `int(-3.7) = -3`, из-за чего модель «прыгала» на блок (UNIT-13).

Модель описывается декларативно — список клеток `(fwd, right, up, block)` в
локальной системе координат. Поворот по yaw и наклон по pitch применяются при
проекции на мир, поэтому одна и та же чертёжная запись корректно рисуется под
любым углом.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

BlockPos = Tuple[int, int, int]


@dataclass(frozen=True)
class Cell:
    """Клетка чертежа в локальных координатах.

    fwd   — вдоль курса (+ вперёд, − назад)
    right — поперёк курса (+ вправо, − влево)
    up    — вверх (+ вверх, − вниз)
    """
    fwd: float
    right: float
    up: float
    block: str


def cells(*specs: Tuple[float, float, float, str]) -> Tuple[Cell, ...]:
    return tuple(Cell(*s) for s in specs)


def cell_run(fwd_from: float, fwd_to: float, right: float, up: float,
             block: str) -> List[Cell]:
    """Ряд клеток вдоль курса (удобно для фюзеляжа)."""
    step = 1 if fwd_to >= fwd_from else -1
    return [Cell(f, right, up, block) for f in range(int(fwd_from), int(fwd_to) + step, step)]


def cell_span(fwd: float, right_from: float, right_to: float, up: float,
              block: str) -> List[Cell]:
    """Ряд клеток поперёк курса (крыло, стабилизатор)."""
    step = 1 if right_to >= right_from else -1
    return [Cell(fwd, r, up, block)
            for r in range(int(right_from), int(right_to) + step, step)]


# ---------------------------------------------------------------------------
#  Чертёж
# ---------------------------------------------------------------------------
class Blueprint:
    """Набор клеток модели + её габариты."""

    __slots__ = ("name", "cells", "length", "width", "height")

    def __init__(self, name: str, cell_list: Iterable[Cell]):
        self.name = name
        self.cells: Tuple[Cell, ...] = tuple(cell_list)
        if not self.cells:
            raise ValueError(f"Чертёж {name!r} пуст")
        f = [c.fwd for c in self.cells]
        r = [c.right for c in self.cells]
        u = [c.up for c in self.cells]
        self.length = max(f) - min(f) + 1
        self.width = max(r) - min(r) + 1
        self.height = max(u) - min(u) + 1

    def __len__(self) -> int:
        return len(self.cells)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Blueprint {self.name}: {len(self.cells)} клеток, "
                f"{self.length:.0f}×{self.width:.0f}×{self.height:.0f}>")


# ---------------------------------------------------------------------------
#  Чертёжная библиотека
# ---------------------------------------------------------------------------
# Low-poly из vanilla-блоков: силуэт должен читаться и с земли, и на карте.


def _aircraft(fuselage: str, wing: str, tip: str, glass: str,
              nose_len: int = 4, tail_len: int = 3, wing_span: int = 4,
              twin_tail: bool = False) -> List[Cell]:
    """Общий контур самолёта; варианты отличаются размерами и окраской."""
    out: List[Cell] = []
    out += cell_run(-tail_len, nose_len, 0, 0, fuselage)          # фюзеляж
    out.append(Cell(nose_len + 1, 0, 0, glass))                   # кабина
    out += cell_span(0, -wing_span, -1, 0, wing)                  # крыло
    out += cell_span(0, 1, wing_span, 0, wing)
    out.append(Cell(0, -(wing_span + 1), 0, tip))                 # законцовки
    out.append(Cell(0, wing_span + 1, 0, tip))
    out += cell_span(-tail_len, -2, 2, 0, wing)                   # стабилизатор
    if twin_tail:
        for r in (-2, 2):
            out.append(Cell(-tail_len, r, 1, fuselage))           # двухбалочный
            out.append(Cell(-tail_len, r, 2, tip))                # хвост
    else:
        out.append(Cell(-tail_len, 0, 1, fuselage))               # киль
        out.append(Cell(-tail_len, 0, 2, tip))
    out.append(Cell(-tail_len - 1, 0, 0, "minecraft:black_concrete"))  # сопло
    return out


def _helicopter() -> List[Cell]:
    out: List[Cell] = []
    out += cell_run(-2, 3, 0, 0, "minecraft:black_concrete")      # фюзеляж
    out.append(Cell(4, 0, 0, "minecraft:glass"))                  # кабина
    out += cell_run(-6, -3, 0, 0, "minecraft:iron_block")         # хв. балка
    out.append(Cell(-6, 0, 1, "minecraft:red_concrete"))          # киль
    out.append(Cell(0, 0, 1, "minecraft:gray_concrete"))          # втулка
    for d in (-3, -2, -1, 1, 2, 3):                               # несущий винт
        out.append(Cell(d, 0, 2, "minecraft:iron_bars"))
        out.append(Cell(0, d, 2, "minecraft:iron_bars"))
    out.append(Cell(-7, 0, 1, "minecraft:light_gray_concrete"))   # хв. винт
    for f in (0, 2):                                              # шасси
        out.append(Cell(f, -1, -1, "minecraft:iron_block"))
        out.append(Cell(f, 1, -1, "minecraft:iron_block"))
    return out


def _drone() -> List[Cell]:
    out: List[Cell] = [Cell(0, 0, 0, "minecraft:iron_block")]     # центр
    out.append(Cell(1, 0, 0, "minecraft:glass"))                  # камера
    out.append(Cell(-1, 0, 1, "minecraft:white_concrete"))        # антенна
    for dx, dz in ((1, 1), (1, -1), (-1, 1), (-1, -1)):           # диагонали
        out.append(Cell(dx, dz, 0, "minecraft:redstone_block"))
    for dx, dz in ((2, 2), (2, -2), (-2, 2), (-2, -2)):           # моторы
        out.append(Cell(dx, dz, 0, "minecraft:redstone_block"))
        out.append(Cell(dx, dz, 1, "minecraft:iron_bars"))        # винты
    return out


def _tank() -> List[Cell]:
    out: List[Cell] = []
    for f in range(-2, 3):                                        # корпус
        for r in (-1, 0, 1):
            out.append(Cell(f, r, 0, "minecraft:green_concrete"))
    for f in range(-2, 3):                                        # гусеницы
        for r in (-2, 2):
            out.append(Cell(f, r, -1, "minecraft:black_concrete"))
    for f in (0, 1):                                              # башня
        for r in (-1, 0, 1):
            out.append(Cell(f, r, 1, "minecraft:gray_concrete"))
    out.append(Cell(2, 0, 1, "minecraft:gray_concrete"))
    out.append(Cell(3, 0, 1, "minecraft:iron_block"))             # ствол
    out.append(Cell(4, 0, 1, "minecraft:iron_block"))
    out.append(Cell(-2, 0, 1, "minecraft:red_concrete"))          # корма
    return out


def _transport() -> List[Cell]:
    """Военно-транспортный самолёт: высоконесущее крыло с двумя ТВД."""
    fus = "minecraft:light_gray_concrete"
    wing = "minecraft:white_concrete"
    out: List[Cell] = []
    out += cell_run(-5, 6, 0, 0, fus)                             # фюзеляж
    out.append(Cell(7, 0, 0, "minecraft:glass"))                 # кабина
    out += cell_span(1, -6, -1, 1, wing)                         # крыло (верх)
    out += cell_span(1, 1, 6, 1, wing)
    out.append(Cell(1, -7, 1, wing))                             # законцовки
    out.append(Cell(1, 7, 1, wing))
    for r in (-4, 4):                                            # гондолы ТВД
        out.append(Cell(1, r, 1, "minecraft:gray_concrete"))
        out.append(Cell(2, r, 1, "minecraft:black_concrete"))    # винт
        out.append(Cell(2, r - 1 if r > 0 else r + 1, 1,
                       "minecraft:iron_bars"))
    out += cell_span(-5, -3, 3, 0, wing)                         # стабилизатор
    out.append(Cell(-5, 0, 1, fus))                              # киль
    out.append(Cell(-5, 0, 2, "minecraft:red_concrete"))
    out.append(Cell(-6, 0, 0, "minecraft:black_concrete"))       # груз. люк
    return out


def _truck() -> List[Cell]:
    """Грузовик: кабина + кузов, колёса по бортам."""
    out: List[Cell] = []
    for f in range(-3, 2):                                       # кузов
        for r in (-1, 0):
            out.append(Cell(f, r, 0, "minecraft:green_concrete"))
    for f in (2, 3):                                             # кабина
        for r in (-1, 0):
            out.append(Cell(f, r, 0, "minecraft:gray_concrete"))
            out.append(Cell(f, r, 1, "minecraft:glass"))
    for f in (-3, -1, 1, 3):                                     # колёса
        out.append(Cell(f, -2, -1, "minecraft:black_concrete"))
        out.append(Cell(f, 1, -1, "minecraft:black_concrete"))
    return out


def _apc() -> List[Cell]:
    """БТР: корпус с восьмью колёсами, башенка с пушкой."""
    out: List[Cell] = []
    for f in range(-3, 4):
        for r in (-1, 0, 1):
            out.append(Cell(f, r, 0, "minecraft:green_concrete"))
    out.append(Cell(4, 0, 0, "minecraft:light_gray_concrete"))   # нос
    for f in (-3, -2, 2, 3):                                     # колёса
        out.append(Cell(f, -2, -1, "minecraft:black_concrete"))
        out.append(Cell(f, 2, -1, "minecraft:black_concrete"))
    for f in (0, 1):                                             # башенка
        for r in (0,):
            out.append(Cell(f, r, 1, "minecraft:gray_concrete"))
    out.append(Cell(2, 0, 1, "minecraft:iron_block"))            # ствол
    return out


def _carrier() -> List[Cell]:
    """Авианосец: максимально простая палуба 24×10 + остров по правому борту.

    Ось `fwd` — вдоль курса корабля (носовая часть — +fwd). Палуба ставится
    на y уровня моря + 1, поэтому юниты стоят на CARRIER_DECK_Y = 65.
    """
    out: List[Cell] = []
    deck = "minecraft:gray_concrete"
    edge = "minecraft:light_gray_concrete"
    for f in range(-12, 12):
        for r in range(-5, 5):
            is_edge = f in (-12, 11) or r in (-5, 4)
            out.append(Cell(f, r, 0, edge if is_edge else deck))
    # осевая линия взлётно-посадочной полосы
    for f in range(-10, 10, 2):
        out.append(Cell(f, 0, 0, "minecraft:white_concrete"))
    # остров (надстройка) по правому борту
    for f in range(1, 4):
        for r in (3, 4):
            for u in range(1, 6):
                out.append(Cell(f, r, u, "minecraft:smooth_stone"))
    for f in range(1, 4):                        # остекление рубки
        out.append(Cell(f, 3, 6, "minecraft:glass"))
        out.append(Cell(f, 4, 6, "minecraft:glass"))
    out.append(Cell(2, 4, 7, "minecraft:iron_block"))       # мачта
    out.append(Cell(-12, 0, 1, "minecraft:red_concrete"))   # носовой маркер
    return out


BLUEPRINTS: Dict[str, Blueprint] = {
    # --- самолёты ---------------------------------------------------------
    "su25": Blueprint("Су-25 (штурмовик)", _aircraft(
        "minecraft:gray_concrete", "minecraft:green_concrete",
        "minecraft:red_concrete", "minecraft:glass",
        nose_len=3, tail_len=3, wing_span=4)),
    "mig29": Blueprint("МиГ-29 (истребитель)", _aircraft(
        "minecraft:light_gray_concrete", "minecraft:white_concrete",
        "minecraft:blue_concrete", "minecraft:glass",
        nose_len=5, tail_len=3, wing_span=3, twin_tail=True)),
    "tu95": Blueprint("Ту-95 (бомбардировщик)", _aircraft(
        "minecraft:iron_block", "minecraft:light_gray_concrete",
        "minecraft:red_concrete", "minecraft:glass",
        nose_len=5, tail_len=4, wing_span=7, twin_tail=True)),
    "an26": Blueprint("Ан-26 (транспортный)", _transport()),
    # --- прочая техника ---------------------------------------------------
    "ka52": Blueprint("Ка-52 (вертолёт)", _helicopter()),
    "orlan": Blueprint("Орлан (БПЛА)", _drone()),
    "t72": Blueprint("Т-72 (танк)", _tank()),
    "ural": Blueprint("Урал-4320 (грузовик)", _truck()),
    "btr82": Blueprint("БТР-82А", _apc()),
    # --- блочная ракета: несколько блоков, видно в полёте как технику ---
    "missile_small": Blueprint("Ракета (блочная)", cells(
        (0, 0, 0, "minecraft:iron_block"),
        (1, 0, 0, "minecraft:iron_block"),
        (2, 0, 0, "minecraft:redstone_block"),
        (-1, 0, 0, "minecraft:orange_concrete"),
        (-1, 0, 1, "minecraft:white_concrete"),
        (-1, 0, -1, "minecraft:white_concrete"),
        (-1, 1, 0, "minecraft:white_concrete"),
        (-1, -1, 0, "minecraft:white_concrete"),
    )),
    # --- авианосец: блочная палуба базы (ставит/двигает движок) ---------
    "carrier": Blueprint("Авианосец (палуба)", _carrier()),
}


def get_blueprint(name: str) -> Blueprint:
    try:
        return BLUEPRINTS[name]
    except KeyError:
        raise KeyError(
            f"Неизвестный чертёж {name!r}. Доступны: {', '.join(sorted(BLUEPRINTS))}"
        ) from None


# ---------------------------------------------------------------------------
#  Проекция и дифф-модель
# ---------------------------------------------------------------------------
def project(blueprint: Blueprint, pos: Sequence[float], yaw: float,
            pitch: float = 0.0, roll: float = 0.0) -> Dict[BlockPos, str]:
    """Чертёж + позиция и углы → {позиция блока: блок}.

    Соглашения Minecraft, единые для всего проекта (`Unit.forward_vector`):

    * yaw 0 = юг (+Z), yaw 90 = запад (−X), поэтому вектор «вперёд» —
      это (−sin yaw, cos yaw);
    * pitch: −90 = нос вверх, +90 = нос вниз;
    * roll > 0 — правое крыло вниз.
    """
    yr = math.radians(yaw)
    fx, fz = -math.sin(yr), math.cos(yr)        # вперёд
    rx, rz = math.cos(yr), math.sin(yr)         # вправо
    pr = math.radians(pitch)
    rr = math.radians(roll)
    px, py, pz = pos

    out: Dict[BlockPos, str] = {}
    for c in blueprint.cells:
        # наклон по тангажу: нос вверх при pitch < 0
        up = c.up - c.fwd * math.sin(pr)
        # крен: правое крыло вниз при roll > 0
        up -= c.right * math.sin(rr)   # правое крыло вниз при roll > 0
        x = px + c.fwd * fx + c.right * rx
        z = pz + c.fwd * fz + c.right * rz
        y = py + up
        key = (int(round(x)), int(round(y)), int(round(z)))
        out[key] = c.block          # при наложении побеждает последняя клетка
    return out


class BlockModel:
    """Живая модель: ставит блоки и убирает **только свои**.

    Использование:

        model = BlockModel(get_blueprint('su25'))
        writes, clears = model.sync((x, y, z), yaw, pitch)
        # -> отправить на сервер через CommandQueue

    `sync()` идемпотентна: повторный вызов с теми же параметрами вернёт
    пустые списки (ноль команд на сервер).
    """

    __slots__ = ("blueprint", "_placed", "_pos", "_yaw", "_pitch", "_roll",
                 "total_writes", "total_clears")

    def __init__(self, blueprint: Blueprint):
        self.blueprint = blueprint
        self._placed: Dict[BlockPos, str] = {}
        self._pos: Optional[Tuple[float, float, float]] = None
        self._yaw: Optional[float] = None
        self._pitch: Optional[float] = None
        self._roll: Optional[float] = None
        self.total_writes = 0
        self.total_clears = 0

    # ------------------------------------------------------------------ diff
    def sync(self, pos: Sequence[float], yaw: float, pitch: float = 0.0,
             roll: float = 0.0) -> Tuple[List[Tuple[BlockPos, str]], List[BlockPos]]:
        """Пересчитать модель под новую позицию. Возвращает (writes, clears)."""
        target = project(self.blueprint, pos, yaw, pitch, roll)
        placed = self._placed

        writes: List[Tuple[BlockPos, str]] = []
        for key, block in target.items():
            if placed.get(key) != block:
                writes.append((key, block))
        clears: List[BlockPos] = [k for k in placed if k not in target]

        self._placed = target
        self._pos = (pos[0], pos[1], pos[2])
        self._yaw, self._pitch, self._roll = yaw, pitch, roll
        self.total_writes += len(writes)
        self.total_clears += len(clears)
        return writes, clears

    def clear(self) -> List[BlockPos]:
        """Снять модель целиком (гибель, деспаун)."""
        blocks = list(self._placed)
        self._placed = {}
        self._pos = self._yaw = self._pitch = self._roll = None
        self.total_clears += len(blocks)
        return blocks

    # ------------------------------------------------------------- состояние
    @property
    def placed_count(self) -> int:
        return len(self._placed)

    @property
    def placed(self) -> Dict[BlockPos, str]:
        return dict(self._placed)

    @property
    def position(self) -> Optional[Tuple[float, float, float]]:
        return self._pos

    @property
    def bounds(self) -> Optional[Tuple[BlockPos, BlockPos]]:
        """Ограничивающий параллелепипед поставленных блоков."""
        if not self._placed:
            return None
        xs = [k[0] for k in self._placed]
        ys = [k[1] for k in self._placed]
        zs = [k[2] for k in self._placed]
        return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))

    def footprint_cells(self) -> int:
        return len(self.blueprint)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<BlockModel {self.blueprint.name}: {len(self._placed)} блоков, "
                f"writes={self.total_writes}, clears={self.total_clears}>")
