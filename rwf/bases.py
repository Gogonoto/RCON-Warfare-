"""
Военные базы: аэродром, авианосец, наземная база.

Закрывает «нет баз»: техника больше не спавнится «над точкой карты» вслепую —
у каждой базы есть взлётные/стояночные точки (pads), своя ориентация и набор
услуг (заправка, перезарядка, ремонт). Возврат (RTB) ведёт на ближайшую
дружественную базу подходящего типа и обслуживается на ней.

Типы:
* `airport`  — аэродром: взлётная полоса, принимает самолёты;
* `carrier`  — авианосец: палуба на воде из блоков, принимает самолёты,
               вертолёты и БПЛА, **умеет плыть** (`move_to`) и принимает
               технику на посадку через унифицированную `RecoveryNode`;
* `ground`   — военная база: вертолётные площадки, принимает наземную
               технику, вертолёты и БПЛА.

ЕДИНАЯ КОНВЕНЦИЯ КУРСА (важно!): `heading` базы — это yaw Minecraft, как
у юнитов: 0 = юг (+Z), 90 = запад (−X). Векторы:
    forward = (−sin h, cos h),  right = (cos h, sin h).
Смещения стоянок хранятся в ЛОКАЛЬНЫХ координатах (fwd, right) и пересчитываются
в мировые с текущим курсом базы — поэтому палуба авианосца может поворачивать,
а стоянки остаются на своих местах относительно корабля.

Унифицированная нода восстановления `RecoveryNode` описывает, КАК принимать
летающую технику: точка касания, посадочный курс, длина и высота захода,
ограничения скорости и окно захвата. Одна и та же структура используется
движком (проверка касания), ИИ (маршрут возврата) и UI (отображение захода) —
аэродром, авианосец и вертолётная площадка отличаются только параметрами.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

Vec2 = Tuple[float, float]

KIND_AIRPORT = "airport"
KIND_CARRIER = "carrier"
KIND_GROUND = "ground"

BASE_LABELS = {KIND_AIRPORT: "Аэродром", KIND_CARRIER: "Авианосец",
               KIND_GROUND: "Военная база"}

#: какие типы техники принимает база
ACCEPTS = {
    KIND_AIRPORT: {"aircraft", "transport"},
    KIND_CARRIER: {"aircraft", "transport", "helicopter", "drone"},
    KIND_GROUND: {"tank", "truck", "apc", "helicopter", "drone"},
}

#: уровень моря и высота палубы авианосца (блоки; юниты стоят на y=65)
SEA_LEVEL = 63.0
CARRIER_DECK_Y = 65.0

#: габариты палубы авианосца в блоках (длина вдоль курса × ширина)
CARRIER_DECK_LEN = 24
CARRIER_DECK_W = 10

# ---------------------------------------------------------------------------
#  Экономика базы (ECO-01…)
#
#  Очки снабжения — единая «валюта» базы: заправка, боезапас и ремонт стоят
#  очков. Очки КОПЯТСЯ (регенерация) и не уходят в ноль навсегда: ниже
#  `SUPPLY_FLOOR` база не опускается, но и обслужить машину дороже остатка
#  не может — получается дефицит, который видно оператору.
#
#  Авианосец своей генерации НЕ имеет (`supply_regen = 0`): его снабжает
#  транспортник, который привозит груз и передаёт его на борт
#  (`Base.deliver_cargo`). Так появляется настоящая логистика.
# ---------------------------------------------------------------------------
#: сколько очков даёт единица ресурса
COST_FUEL = 0.35          # за 1 ед. топлива
COST_AMMO = 1.6           # за 1 выстрел
COST_REPAIR = 1.1         # за 1 ед. прочности
#: неустранимый остаток: база никогда не «схлопывается» в ноль
SUPPLY_FLOOR = 5.0
#: очки за тонну доставленного груза (логистика авианосца)
SUPPLY_PER_TON = 26.0

#: прочность и регенерация по типу базы
BASE_HEALTH = {KIND_AIRPORT: 2400.0, KIND_CARRIER: 3600.0,
               KIND_GROUND: 1800.0}
BASE_SUPPLY = {KIND_AIRPORT: 420.0, KIND_CARRIER: 300.0,
               KIND_GROUND: 300.0}
BASE_REGEN = {KIND_AIRPORT: 2.2, KIND_CARRIER: 0.0, KIND_GROUND: 1.6}


def forward_vec(heading_deg: float) -> Vec2:
    """Вектор «вперёд» для yaw Minecraft: 0 = +Z, 90 = −X."""
    h = math.radians(heading_deg)
    return (-math.sin(h), math.cos(h))


def right_vec(heading_deg: float) -> Vec2:
    """Вектор «вправо» для yaw Minecraft."""
    h = math.radians(heading_deg)
    return (math.cos(h), math.sin(h))


def yaw_to(dx: float, dz: float) -> float:
    """Yaw Minecraft, соответствующий направлению (dx, dz)."""
    return math.degrees(math.atan2(-dx, dz)) % 360.0


def angle_diff(target: float, current: float) -> float:
    """Кратчайшая разница курсов в градусах, [-180, 180]."""
    d = (target - current + 180.0) % 360.0 - 180.0
    return d


# ---------------------------------------------------------------------------
#  Унифицированная нода восстановления (приёма техники)
# ---------------------------------------------------------------------------
@dataclass
class RecoveryNode:
    """Как база принимает летающую технику: единый контракт для всех типов.

    Точка касания (x, z, y) и посадочный курс задают финиш захода;
    `approach_point()` — начало финальной прямой. Движок ловит касание по
    окну захвата (along/cross/alt), ИИ строит по ноде маршрут посадки.
    """
    base_id: int
    base_name: str
    kind: str                          # 'runway' | 'deck' | 'helipad'
    x: float
    z: float
    y: float                           # высота касания (мировой Y)
    heading: float                     # посадочный курс (yaw Minecraft)
    approach_dist: float               # длина финальной прямой, м
    approach_alt: float                # высота на начале прямой (над точкой), м
    max_touchdown_speed: float         # м/с
    #: окно захвата: вдоль курса назад от точки / поперёк / по высоте, м
    capture_along: float = 150.0
    capture_cross: float = 30.0
    capture_alt: float = 10.0

    def approach_point(self) -> Tuple[float, float, float]:
        """Начало финальной прямой: (x, z, абсолютная высота)."""
        fx, fz = forward_vec(self.heading)
        return (self.x - fx * self.approach_dist,
                self.z - fz * self.approach_dist,
                self.y + self.approach_alt)

    def to_dict(self) -> Dict[str, Any]:
        return {"base_id": self.base_id, "base_name": self.base_name,
                "kind": self.kind, "x": self.x, "z": self.z, "y": self.y,
                "heading": self.heading,
                "approach_dist": self.approach_dist,
                "approach_alt": self.approach_alt,
                "max_touchdown_speed": self.max_touchdown_speed}


@dataclass
class BasePad:
    """Взлётная/стояночная точка базы.

    Смещение хранится в локальных осях базы (fwd — вдоль курса, right —
    поперёк): при повороте/движении базы мировые координаты пересчитываются.
    """
    index: int
    fwd: float = 0.0
    right: float = 0.0
    heading_rel: float = 0.0           # курс стоянки относительно курса базы
    occupied_by: Optional[int] = None

    def world(self, base: "Base") -> Tuple[float, float, float]:
        """(x, z, yaw) стоянки в мировых координатах при текущем курсе базы."""
        fx, fz = forward_vec(base.heading)
        rx, rz = right_vec(base.heading)
        return (base.x + fx * self.fwd + rx * self.right,
                base.z + fz * self.fwd + rz * self.right,
                (base.heading + self.heading_rel) % 360.0)

    def offset_world(self, base: "Base") -> Vec2:
        x, z, _h = self.world(base)
        return (x - base.x, z - base.z)

    def to_dict(self, base: "Base") -> Dict[str, Any]:
        ox, oz = self.offset_world(base)
        return {"index": self.index, "offset": [ox, oz],
                "fwd": self.fwd, "right": self.right,
                "heading": (base.heading + self.heading_rel) % 360.0,
                "occupied_by": self.occupied_by}


@dataclass
class Base:
    id: int
    name: str
    kind: str
    x: float
    z: float
    heading: float = 0.0               # yaw Minecraft: 0 = +Z
    radius: float = 120.0
    faction: str = "blue"
    services: Tuple[str, ...] = ("refuel", "rearm", "repair")
    pads: List[BasePad] = field(default_factory=list)
    movable: bool = False
    move_target: Optional[Vec2] = None
    move_speed: float = 3.0            # м/с для авианосца
    turn_rate: float = 4.0             # град/с поворота палубы к курсу движения
    created: float = field(default_factory=time.time)

    # --- живучесть базы (BASE-HP): базу можно уничтожить -------------------
    health: Optional[float] = None
    health_max: Optional[float] = None
    destroyed: bool = False

    # --- экономика: очки снабжения -----------------------------------------
    supply: Optional[float] = None
    supply_max: Optional[float] = None
    supply_regen: Optional[float] = None
    supply_spent: float = 0.0          # сколько всего потрачено (статистика)
    last_shortage: float = 0.0         # момент последнего отказа по дефициту

    def __post_init__(self):
        if not self.pads:
            self.pads = self._default_pads()
        if self.health_max is None:
            self.health_max = BASE_HEALTH.get(self.kind, 1800.0)
        if self.health is None:
            self.health = self.health_max
        if self.supply_max is None:
            self.supply_max = BASE_SUPPLY.get(self.kind, 300.0)
        if self.supply is None:
            self.supply = self.supply_max
        if self.supply_regen is None:
            self.supply_regen = BASE_REGEN.get(self.kind, 1.0)

    # ------------------------------------------------------------ стоянки
    def _default_pads(self) -> List[BasePad]:
        """Раскладка стоянок по типу базы (локальные координаты)."""
        if self.kind == KIND_CARRIER:
            # палуба: четыре места у правого борта, вдоль оси корабля
            return [BasePad(index=i, fwd=f, right=3.0)
                    for i, f in enumerate((-9.0, -3.0, 3.0, 9.0))]
        if self.kind == KIND_GROUND:
            # вертолётные площадки по углам
            return [BasePad(index=i, fwd=fz * 14.0, right=rx * 14.0)
                    for i, (fz, rx) in enumerate(((1, 1), (1, -1),
                                                  (-1, 1), (-1, -1)))]
        # аэродром: стоянки вдоль оси полосы
        count = 6
        spacing = 26.0
        return [BasePad(index=i, fwd=(i - (count - 1) / 2.0) * spacing,
                        right=12.0)
                for i in range(count)]

    def free_pad(self) -> Optional[BasePad]:
        if self.destroyed:
            return None                 # снесённая база не принимает технику
        for pad in self.pads:
            if pad.occupied_by is None:
                return pad
        return None

    def take_pad(self, uid: int) -> Optional[BasePad]:
        pad = self.free_pad()
        if pad is not None:
            pad.occupied_by = uid
        return pad

    def release_pad(self, uid: int) -> None:
        for pad in self.pads:
            if pad.occupied_by == uid:
                pad.occupied_by = None

    def pad_world(self, pad: BasePad) -> Tuple[float, float, float]:
        return pad.world(self)

    def free_pads(self) -> int:
        return sum(1 for p in self.pads if p.occupied_by is None)

    # ------------------------------------------------------------- услуги
    def accepts(self, unit_kind: str) -> bool:
        return unit_kind in ACCEPTS.get(self.kind, set())

    def provides(self, service: str) -> bool:
        return service in self.services

    # --------------------------------------------------------- живучесть
    @property
    def alive(self) -> bool:
        return not self.destroyed and self.health > 0.0

    @property
    def health_pct(self) -> float:
        return 100.0 * self.health / max(1.0, self.health_max)

    def take_damage(self, amount: float, source: str = "") -> bool:
        """Урон базе. True — база уничтожена (заказчик: базу можно снести)."""
        if self.destroyed or amount <= 0:
            return False
        self.health = max(0.0, self.health - float(amount))
        if self.health <= 0.0:
            self.destroyed = True
            log.warning("База #%d %s УНИЧТОЖЕНА (%s)", self.id, self.name,
                        source or "—")
            return True
        return False

    def repair_base(self, amount: Optional[float] = None) -> None:
        self.health = (self.health_max if amount is None
                       else min(self.health_max, self.health + amount))

    # ---------------------------------------------------------- снабжение
    @property
    def supply_pct(self) -> float:
        return 100.0 * self.supply / max(1.0, self.supply_max)

    def regen_supply(self, dt: float) -> float:
        """Накопление очков снабжения. Возвращает, сколько добавилось."""
        if self.destroyed or self.supply_regen <= 0:
            return 0.0
        before = self.supply
        self.supply = min(self.supply_max, self.supply + self.supply_regen * dt)
        return self.supply - before

    def can_afford(self, cost: float) -> bool:
        """Хватит ли очков: тратить можно всё, кроме неснижаемого остатка."""
        return (self.supply - cost) >= SUPPLY_FLOOR - 1e-6

    def spend(self, cost: float) -> float:
        """Списать очки. Возвращает реально списанное (может быть меньше)."""
        cost = max(0.0, float(cost))
        available = max(0.0, self.supply - SUPPLY_FLOOR)
        paid = min(cost, available)
        self.supply -= paid
        self.supply_spent += paid
        if paid < cost - 1e-6:
            self.last_shortage = time.time()
        return paid

    def deliver_cargo(self, tons: float) -> float:
        """Логистика: груз транспортника превращается в очки снабжения.

        Единственный способ пополнить авианосец (у него `supply_regen = 0`).
        Возвращает, сколько очков зачислено.
        """
        if self.destroyed or tons <= 0:
            return 0.0
        gained = min(tons * SUPPLY_PER_TON, self.supply_max - self.supply)
        self.supply += max(0.0, gained)
        return max(0.0, gained)

    def service_cost(self, unit) -> Dict[str, float]:
        """Сколько очков стоит полное обслуживание этой машины."""
        need_fuel = max(0.0, unit.spec.fuel_max - unit.fuel)
        need_ammo = sum(max(0, m.ammo_max - m.ammo) for m in unit.mounts)
        need_hp = max(0.0, unit.max_health - unit.health)
        return {"refuel": need_fuel * COST_FUEL,
                "rearm": need_ammo * COST_AMMO,
                "repair": need_hp * COST_REPAIR}

    # -------------------------------------------- нода приёма (RecoveryNode)
    def recovery_node(self, unit_kind: str) -> Optional[RecoveryNode]:
        """Унифицированная нода посадки для типа техники, либо None."""
        if self.destroyed or not self.accepts(unit_kind):
            return None
        fx, fz = forward_vec(self.heading)
        if self.kind == KIND_CARRIER:
            # касание чуть позади середины палубы, курс — вдоль корабля
            tx, tz = self.x - fx * 4.0, self.z - fz * 4.0
            if unit_kind == "aircraft":
                return RecoveryNode(self.id, self.name, "deck", tx, tz,
                                    CARRIER_DECK_Y, self.heading,
                                    approach_dist=500.0, approach_alt=45.0,
                                    max_touchdown_speed=95.0,
                                    capture_along=160.0, capture_cross=22.0,
                                    capture_alt=12.0)
            return RecoveryNode(self.id, self.name, "deck", tx, tz,
                                CARRIER_DECK_Y, self.heading,
                                approach_dist=180.0, approach_alt=25.0,
                                max_touchdown_speed=10.0,
                                capture_along=60.0, capture_cross=20.0,
                                capture_alt=10.0)
        if self.kind == KIND_AIRPORT:
            tx, tz = self.x - fx * 20.0, self.z - fz * 20.0
            return RecoveryNode(self.id, self.name, "runway", tx, tz,
                                self.ground_y(tx, tz), self.heading,
                                approach_dist=600.0, approach_alt=55.0,
                                max_touchdown_speed=95.0,
                                capture_along=200.0, capture_cross=28.0,
                                capture_alt=12.0)
        # наземная база: вертолётная площадка в центре (только винтокрылые)
        if unit_kind not in ("helicopter", "drone"):
            return None
        return RecoveryNode(self.id, self.name, "helipad", self.x, self.z,
                            self.ground_y(self.x, self.z), self.heading,
                            approach_dist=150.0, approach_alt=20.0,
                            max_touchdown_speed=8.0,
                            capture_along=40.0, capture_cross=18.0,
                            capture_alt=8.0)

    def ground_y(self, x: float, z: float) -> float:
        """Высота поверхности в точке базы (без доступа к рельефу — море)."""
        return SEA_LEVEL + 1.0

    def contains_point(self, x: float, z: float, margin: float = 0.0) -> bool:
        """Точка в габаритах базы (круг радиуса radius)."""
        return self.distance_to(x, z) <= self.radius + margin

    # --------------------------------------------------------- перемещение
    def move_to(self, target: Vec2) -> None:
        if not self.movable:
            log.warning("База %s не может перемещаться", self.name)
            return
        self.move_target = (float(target[0]), float(target[1]))

    @property
    def moving(self) -> bool:
        return self.movable and self.move_target is not None

    @property
    def arrive_radius(self) -> float:
        """Радиус прибытия: корабль не может циркулировать вокруг точки.

        Берём половину радиуса циркуляции (v/ω) с минимумом 6 м: в этой
        зоне цель считается достигнутой, иначе авианосец вечно ходил бы
        кругами мимо станции.
        """
        turn_r = self.move_speed / max(1e-6, math.radians(self.turn_rate))
        return max(6.0, turn_r * 0.6)

    def step(self, dt: float) -> float:
        """Один тик движения. Возвращает пройденное расстояние, м.

        Авианосец идёт к цели и плавно доворачивает палубу на курс движения:
        стоянки и RecoveryNode пересчитываются от текущих (x, z, heading).
        """
        if not self.moving:
            return 0.0
        assert self.move_target is not None
        tx, tz = self.move_target
        dx, dz = tx - self.x, tz - self.z
        d = math.hypot(dx, dz)
        if d < self.arrive_radius:
            self.move_target = None
            return 0.0
        # доворот носа на курс движения
        want = yaw_to(dx, dz)
        diff = angle_diff(want, self.heading)
        max_turn = self.turn_rate * dt
        if abs(diff) <= max_turn:
            self.heading = want
        else:
            self.heading = (self.heading + math.copysign(max_turn, diff)) % 360.0
        step = min(d, self.move_speed * dt)
        fx, fz = forward_vec(self.heading)
        self.x += fx * step
        self.z += fz * step
        return step

    # -------------------------------------------------------------- снимок
    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind,
                "x": self.x, "z": self.z, "heading": self.heading,
                "radius": self.radius, "faction": self.faction,
                "services": list(self.services),
                "pads": [p.to_dict(self) for p in self.pads],
                "movable": self.movable, "moving": self.moving,
                "move_target": list(self.move_target) if self.move_target else None,
                "move_speed": self.move_speed, "free_pads": self.free_pads(),
                "health": round(self.health, 1),
                "health_max": round(self.health_max, 1),
                "health_pct": round(self.health_pct, 1),
                "destroyed": self.destroyed,
                "supply": round(self.supply, 1),
                "supply_max": round(self.supply_max, 1),
                "supply_pct": round(self.supply_pct, 1),
                "supply_regen": self.supply_regen,
                "shortage": (time.time() - self.last_shortage) < 6.0
                if self.last_shortage else False}

    def distance_to(self, x: float, z: float) -> float:
        return math.hypot(self.x - x, self.z - z)


class BaseManager:
    """Реестр баз и подбор точки запуска/возврата."""

    def __init__(self):
        self.bases: Dict[int, Base] = {}
        self._next_id = 1

    def add(self, name: str, kind: str, x: float, z: float, heading: float = 0.0,
            radius: float = 120.0, faction: str = "blue",
            services: Sequence[str] = ("refuel", "rearm", "repair"),
            movable: Optional[bool] = None,
            health: Optional[float] = None) -> Base:
        if kind not in ACCEPTS:
            raise ValueError(f"Неизвестный тип базы {kind!r}. "
                             f"Доступны: {', '.join(sorted(ACCEPTS))}")
        base = Base(id=self._next_id, name=name, kind=kind, x=x, z=z,
                    heading=heading % 360.0, radius=radius, faction=faction,
                    services=tuple(services),
                    movable=(kind == KIND_CARRIER) if movable is None else movable,
                    health=health, health_max=health)
        self._next_id += 1
        self.bases[base.id] = base
        log.info("База #%d %s (%s) в (%.0f, %.0f), курс %.0f°", base.id, name,
                 BASE_LABELS[kind], x, z, base.heading)
        return base

    def remove(self, base_id: int) -> bool:
        return self.bases.pop(base_id, None) is not None

    def get(self, base_id: int) -> Optional[Base]:
        return self.bases.get(base_id)

    def all(self) -> List[Base]:
        return list(self.bases.values())

    def nearest(self, x: float, z: float, kind: Optional[str] = None,
                unit_kind: Optional[str] = None,
                faction: Optional[str] = None) -> Optional[Base]:
        best, best_d = None, float("inf")
        for base in self.bases.values():
            if base.destroyed:
                continue                # мёртвая база не цель и не убежище
            if kind and base.kind != kind:
                continue
            if unit_kind and not base.accepts(unit_kind):
                continue
            if faction and base.faction != faction:
                continue
            d = base.distance_to(x, z)
            if d < best_d:
                best, best_d = base, d
        return best

    def spawn_point(self, base: Base, uid: int,
                    altitude: float) -> Tuple[float, float, float]:
        """Точка взлёта: свободная стоянка базы, курс = курс стоянки."""
        if base.destroyed:
            raise ValueError(f"База «{base.name}» уничтожена — запуск невозможен")
        pad = base.take_pad(uid)
        if pad is None:
            # все стоянки заняты — взлёт с торца полосы (позади центра)
            fx, fz = forward_vec(base.heading)
            return (base.x - fx * base.radius, base.z - fz * base.radius,
                    base.heading)
        x, z, heading = base.pad_world(pad)
        return (x, z, heading)

    def release(self, base: Base, uid: int) -> None:
        base.release_pad(uid)

    def service_at(self, base: Base, unit, pay: bool = True) -> List[str]:
        """Обслужить юнит на базе; вернуть список выполненных услуг.

        ECO-02: каждая услуга СПИСЫВАЕТ очки снабжения базы. Если очков не
        хватает — услуга оказывается частично (топливо/ремонт — пропорцио-
        нально, боезапас — только при полной оплате), а база помечается как
        «дефицит». Так ресурсы становятся дефицитом, а не декорацией.
        """
        done: List[str] = []
        if base.destroyed:
            return done
        costs = base.service_cost(unit) if pay else {}

        if base.provides("refuel"):
            need = max(0.0, unit.spec.fuel_max - unit.fuel)
            if need > 0:
                paid = base.spend(costs.get("refuel", 0.0)) if pay else 1.0
                frac = 1.0 if not pay or costs.get("refuel", 0.0) <= 0 else \
                    min(1.0, paid / max(1e-6, costs["refuel"]))
                unit.fuel = min(unit.spec.fuel_max, unit.fuel + need * frac)
                done.append("заправка" if frac > 0.99 else
                            f"заправка {frac * 100:.0f}%")
        if base.provides("rearm"):
            need = sum(max(0, m.ammo_max - m.ammo) for m in unit.mounts)
            if need > 0:
                cost = costs.get("rearm", 0.0)
                if not pay or base.can_afford(cost):
                    base.spend(cost) if pay else None
                    for m in unit.mounts:
                        m.reload()
                    done.append("боезапас")
        if base.provides("repair"):
            need = max(0.0, unit.max_health - unit.health)
            if need > 0:
                paid = base.spend(costs.get("repair", 0.0)) if pay else 1.0
                frac = 1.0 if not pay or costs.get("repair", 0.0) <= 0 else \
                    min(1.0, paid / max(1e-6, costs["repair"]))
                unit.repair(need * frac)
                done.append("ремонт" if frac > 0.99 else f"ремонт {frac * 100:.0f}%")
        return done

    def step(self, dt: float) -> Dict[int, float]:
        """Тик движения всех баз. Возвращает {base_id: пройдено м}."""
        moved: Dict[int, float] = {}
        for base in self.bases.values():
            d = base.step(dt)
            if d > 0.0:
                moved[base.id] = d
        return moved

    def step_supply(self, dt: float) -> Dict[int, float]:
        """Тик экономики: накопление очков снабжения (ECO-01)."""
        gained: Dict[int, float] = {}
        for base in self.bases.values():
            g = base.regen_supply(dt)
            if g > 0.0:
                gained[base.id] = g
        return gained

    def snapshot(self) -> List[Dict[str, Any]]:
        return [b.to_dict() for b in self.bases.values()]
