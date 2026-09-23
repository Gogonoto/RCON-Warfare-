"""
Навигация наземной техники по отсканированному рельефу (GROUND-01…).

Заказчик: «техника едет сквозь блоки; нужна коллизия с terrain, ограничения
угла подъёма, приоритет дорог/ровных участков». Раньше `Tank.step` лишь
брал высоту ближайшего тайла и падал в статус `stuck` при перепаде > 4 м:
машина проезжала сквозь гору, если склон был пологим, и игнорировала воду,
лес и дороги.

Здесь — чистая логика проходимости, без DPG и без RCON:

* **проходимость** (`passable`) — тайл проходим, если его тип не вода/лава/
  препятствие, а уклон до следующей точки не круче `max_climb_deg`;
* **стоимость** (`cost`) — дорога и ровные участки ДЕШЕВЛЕ, песок/снег/лес
  дороже, вода и лава — бесконечно дорого. Автопилот выбирает курс с
  минимальной стоимостью, поэтому колонна сама выходит на дорогу;
* **выбор курса** (`choose_heading`) — перебор кандидатов вокруг желаемого
  курса: сначала «в лоб», затем отклонения. Возвращает курс и признак
  «впереди стена» (`blocked`), по которому физика останавливает машину
  вместо проезда сквозь блоки;
* **коллизия** (`advance`) — шаг вперёд с проверкой: если следующая клетка
  непроходима или слишком крута, машина НЕ двигается (упирается), а её
  тангаж повторяет уклон местности, когда движение разрешено.

Типы поверхности приходят из сканера (`mc.SURFACE_KINDS`): там же добавлен
тип `road` — мощёные блоки, которые в мирах и есть дороги.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

Vec2 = Tuple[float, float]

#: непроходимое в принципе (вода/лава — техника тонет и горит)
IMPASSABLE = frozenset({"water", "lava"})
#: серьёзные препятствия: лес и постройки — объезжаем, но не «тараним»
OBSTACLES = frozenset({"leaves", "log"})

#: множитель стоимости прохода по типу поверхности (дорога дешевле всего)
SURFACE_COST: Dict[str, float] = {
    "road": 0.55,
    "stone": 0.95,
    "gravel": 1.0,
    "dirt": 1.05,
    "grass": 1.1,
    "sand": 1.75,
    "snow": 1.9,
    "leaves": 3.2,
    "log": 3.6,
    "other": 1.35,
    "water": math.inf,
    "lava": math.inf,
}
DEFAULT_SURFACE_COST = 1.35

#: предельный угол подъёма/спуска, град (по ТТХ: танк ~30°, грузовик ~22°)
MAX_CLIMB_DEG = 30.0
#: насколько круче предельного можно ехать «на пределе» (штрафная зона)
CLIMB_HARD_LIMIT_DEG = 42.0
#: шаг сетки рельефа, который считается, если сканер не дал свой
DEFAULT_STEP = 8


def surface_cost(kind: Optional[str]) -> float:
    """Стоимость прохода по тайлу этого типа (меньше = привлекательнее)."""
    if kind is None:
        return DEFAULT_SURFACE_COST
    return SURFACE_COST.get(kind, DEFAULT_SURFACE_COST)


def slope_deg(climb: float, run: float) -> float:
    """Уклон в градусах: подъём `climb` м на `run` м пути."""
    run = max(0.5, abs(run))
    return math.degrees(math.atan2(climb, run))


def passable(kind: Optional[str], slope: float = 0.0,
             max_climb_deg: float = MAX_CLIMB_DEG) -> bool:
    """Проедет ли техника: тип поверхности и уклон в норме."""
    if kind in IMPASSABLE:
        return False
    if abs(slope) > max_climb_deg:
        return False
    return True


def tile_cost(kind: Optional[str], slope: float = 0.0,
              max_climb_deg: float = MAX_CLIMB_DEG) -> float:
    """Полная стоимость клетки: поверхность + штраф за крутизну."""
    base = surface_cost(kind)
    if not math.isfinite(base):
        return math.inf
    if kind in OBSTACLES:
        base += 1.5
    limit = max(1.0, max_climb_deg)
    if abs(slope) > limit:
        return math.inf
    # крутизна штрафует квадратично: пологий склон почти бесплатен,
    # предельный — вдвое дороже ровного участка
    base *= 1.0 + 1.6 * (abs(slope) / limit) ** 2
    return base


class GroundNav:
    """Принятие решений по рельефу: что впереди, куда свернуть, можно ли ехать.

    Рельеф читается из тайловой сетки (`world.terrain`): `height_at(x, z)` и
    `get(x, z) -> (y, kind)`. Сетка дискретна (шаг 8 м по умолчанию), поэтому
    уклон считается между СОСЕДНИМИ тайлами по направлению движения, а не по
    паре «здесь-там» с плавающей привязкой — так результат не дрожит.
    """

    def __init__(self, terrain: Any, step: int = DEFAULT_STEP,
                 max_climb_deg: float = MAX_CLIMB_DEG,
                 probe_dist: Optional[float] = None,
                 probes: Tuple[float, ...] = (0.0, -12.0, 12.0, -25.0, 25.0,
                                              -40.0, 40.0, -60.0, 60.0)):
        self.terrain = terrain
        self.step = int(step or DEFAULT_STEP)
        self.max_climb_deg = float(max_climb_deg)
        #: на сколько метров вперёд смотреть (по умолчанию — шаг сетки)
        self.probe_dist = float(probe_dist if probe_dist is not None
                                else max(4.0, self.step))
        #: отклонения курса-кандидаты, град (0 = «в лоб»)
        self.probes = tuple(probes)

    # ------------------------------------------------------------- рельеф
    def snap(self, x: float, z: float) -> Tuple[int, int]:
        """Координаты тайла сетки, которому принадлежит точка."""
        st = self.step or DEFAULT_STEP
        return (int(round(x / st)) * st, int(round(z / st)) * st)

    def _tile(self, x: float, z: float) -> Optional[Tuple[int, str]]:
        get = getattr(self.terrain, "get", None)
        if get is None:
            return None
        st = self.step or DEFAULT_STEP
        bx, bz = self.snap(x, z)
        hit = get(bx, bz)
        if hit is None:                      # не отсканировано — ищем рядом
            best = None
            for dx in (0, st, -st):
                for dz in (0, st, -st):
                    cand = get(bx + dx, bz + dz)
                    if cand is not None:
                        d = abs(dx) + abs(dz)
                        if best is None or d < best[0]:
                            best = (d, cand)
            hit = best[1] if best else None
        return hit

    def height_kind(self, x: float, z: float) -> Tuple[Optional[float],
                                                       Optional[str]]:
        tile = self._tile(x, z)
        if tile is None:
            return None, None
        return float(tile[0]), tile[1]

    def next_tile(self, x: float, z: float, yaw: float,
                  max_steps: int = 8) -> Tuple[float, float, float]:
        """Первый тайл по курсу, отличный от текущего: (x, z, пройдено м).

        Идём четвертями шага сетки, потому что «+8 м от позиции» при
        банковском округлении может попасть в ТОТ ЖЕ тайл (round(0.5) == 0) —
        тогда проба смотрела бы себе под ноги и стена не обнаруживалась.
        """
        st = float(self.step or DEFAULT_STEP)
        fx, fz = -math.sin(math.radians(yaw)), math.cos(math.radians(yaw))
        base = self.snap(x, z)
        run = 0.0
        px, pz = x, z
        for _ in range(max(1, max_steps)):
            run += st * 0.25
            px, pz = x + fx * run, z + fz * run
            if self.snap(px, pz) != base:
                break
        return px, pz, run

    def slope_between(self, x0: float, z0: float, x1: float, z1: float
                      ) -> Tuple[float, Optional[str], Optional[str]]:
        """Градиент рельефа между двумя точками, отнесённый к шагу сетки.

        Делим именно на шаг тайла, а не на фактическое расстояние: иначе
        короткий шаг машины (0.5 м) превращал бы перепад в 1 блок в «стену»
        63°, а реальный уклон местности — 7°.
        """
        st = float(self.step or DEFAULT_STEP)
        y0, k0 = self.height_kind(x0, z0)
        y1, k1 = self.height_kind(x1, z1)
        if y0 is None or y1 is None:
            return 0.0, k0, k1
        return slope_deg(y1 - y0, st), k0, k1

    def slope_ahead(self, x: float, z: float, yaw: float,
                    dist: Optional[float] = None) -> Tuple[float, float,
                                                           Optional[str],
                                                           Optional[str]]:
        """Что впереди по курсу: (уклон°, стоимость, kind здесь, kind там)."""
        px, pz, _run = self.next_tile(x, z, yaw)
        slope, k0, k1 = self.slope_between(x, z, px, pz)
        return slope, tile_cost(k1, slope, self.max_climb_deg), k0, k1

    def blocked_ahead(self, x: float, z: float, yaw: float,
                      dist: Optional[float] = None) -> bool:
        """Впереди непроходимое место: вода/лава или непреодолимый уклон."""
        slope, cost, _k0, k1 = self.slope_ahead(x, z, yaw, dist)
        if k1 in IMPASSABLE:
            return True
        if not math.isfinite(cost):
            return True
        return abs(slope) > CLIMB_HARD_LIMIT_DEG

    # ------------------------------------------------------------- курс
    def choose_heading(self, x: float, z: float, want_yaw: float,
                       dist: Optional[float] = None
                       ) -> Tuple[float, float, bool]:
        """Курс с минимальной стоимостью вокруг желаемого.

        Возвращает (yaw, cost, blocked). `blocked=True` — все кандидаты
        непроходимы: физика должна остановить машину, а не ехать сквозь блоки.
        Отклонения перебираются от малых к большим, поэтому при равной
        стоимости выбирается курс, ближайший к желаемому (машина не «рыскает»).
        """
        best_yaw, best_cost = want_yaw % 360.0, math.inf
        any_pass = False
        for off in self.probes:
            yaw = (want_yaw + off) % 360.0
            slope, cost, _k0, k1 = self.slope_ahead(x, z, yaw, dist)
            if k1 in IMPASSABLE or not math.isfinite(cost):
                continue
            any_pass = True
            cost += abs(off) * 0.02        # ехать прямо дешевле, чем петлять
            if cost < best_cost - 1e-9:
                best_cost, best_yaw = cost, yaw
        if not any_pass:
            return want_yaw % 360.0, math.inf, True
        return best_yaw, best_cost, False

    def detour_heading(self, x: float, z: float, want_yaw: float,
                       dist: Optional[float] = None) -> Optional[float]:
        """Куда свернуть, чтобы объехать препятствие (None — объезда нет)."""
        yaw, _cost, blocked = self.choose_heading(x, z, want_yaw, dist)
        return None if blocked else yaw

    # ------------------------------------------------------------- шаг
    def advance(self, x: float, y: float, z: float, yaw: float,
                step_m: float, auto_detour: bool = True
                ) -> Tuple[float, float, float, float, float, bool]:
        """Продвинуть машину на `step_m` метров с проверкой коллизии.

        Возвращает (x, y, z, yaw, pitch, moved). `moved=False` — впереди
        стена/вода/неподъёмный склон: позиция НЕ меняется (коллизия), что и
        закрывает жалобу «едет сквозь блоки».

        Проверка идёт по ТАЙЛУ назначения: внутри своего тайла машина едет
        свободно, а при пересечении границы решают высота и тип следующей
        клетки. `auto_detour=True` — автопилот пробует объехать; при ручном
        управлении объезд выключен, чтобы оператор чувствовал упор машины.
        """
        if step_m <= 0:
            return x, y, z, yaw % 360.0, 0.0, False
        course = yaw % 360.0
        fx, fz = -math.sin(math.radians(course)), math.cos(math.radians(course))
        nx, nz = x + fx * step_m, z + fz * step_m
        st = float(self.step or DEFAULT_STEP)
        here, there = self.snap(x, z), self.snap(nx, nz)

        if here != there:
            slope, k0, k1 = self.slope_between(x, z, nx, nz)
            bad = (k1 in IMPASSABLE or abs(slope) > CLIMB_HARD_LIMIT_DEG
                   or abs(slope) > self.max_climb_deg * 1.35)
            if bad and auto_detour:
                alt = self.detour_heading(x, z, course)
                if alt is not None:
                    course = alt
                    fx, fz = (-math.sin(math.radians(course)),
                              math.cos(math.radians(course)))
                    nx, nz = x + fx * step_m, z + fz * step_m
                    there = self.snap(nx, nz)
                    slope, k0, k1 = self.slope_between(x, z, nx, nz)
                    bad = (there != here and (k1 in IMPASSABLE
                                              or abs(slope) > CLIMB_HARD_LIMIT_DEG))
            if there != here and bad:
                return x, y, z, course, 0.0, False

        _y0, k_here = self.height_kind(x, z)
        ny, kind = self.height_kind(nx, nz)
        if kind in IMPASSABLE or k_here in IMPASSABLE:
            return x, y, z, course, 0.0, False
        # Рельеф не отсканирован — держим высоту и идём ровно: прибавка
        # «+1 блок над поверхностью» применяется только когда она известна.
        if ny is None:
            return nx, y, nz, course, 0.0, True
        new_y = float(ny) + 1.0
        if _y0 is not None:
            slope = slope_deg(float(ny) - float(_y0), st)
            pitch = max(-35.0, min(35.0, -slope))
        else:
            pitch = 0.0
        return nx, new_y, nz, course, pitch, True

    # ------------------------------------------------------------- сводка
    def report(self, x: float, z: float, yaw: float) -> Dict[str, Any]:
        """Сводка для журнала/отладки: что видит машина впереди."""
        slope, cost, k0, k1 = self.slope_ahead(x, z, yaw)
        return {"slope": round(slope, 1), "cost": (round(cost, 2)
                                                   if math.isfinite(cost)
                                                   else None),
                "here": k0, "ahead": k1,
                "blocked": self.blocked_ahead(x, z, yaw)}


def road_bonus(kinds: List[Optional[str]]) -> float:
    """Доля «дорожных» тайлов в списке — метрика для отчётов/тестов."""
    if not kinds:
        return 0.0
    return sum(1 for k in kinds if k == "road") / len(kinds)
