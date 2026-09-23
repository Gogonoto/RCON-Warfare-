"""
Разрушения: что остаётся на месте уничтоженной техники (WRECK-01…).

Заказчик: «при уничтожении техники — обломки падают, кратер, макет сгоревшей
техники (уголь + железные обломки), зона горит (fire-эффекты)». Раньше гибель
машины выглядела как один `instant_explosion` и маркер на карте: мир оставался
чистым, и следов боя не было.

Теперь гибель — это четырёхтактное событие:

1. **Обломки падают** — несколько `falling_block` с разной скоростью: они
   реально летят и разбиваются о землю, поэтому разброс каждый раз разный.
2. **Кратер** — воронка в грунте (`mc.crater`), радиус зависит от типа машины.
3. **Сгоревший макет** — остов из угля и железа на месте машины: чертёж юнита
   проецируется в точку падения, материалы заменяются на обгоревшие, а часть
   клеток «отваливается» (детерминированно от uid — картинка стабильна между
   перезапусками и тестируема).
4. **Зона горит** — блоки огня и партиклы дыма/пламени `burn_time` секунд,
   после чего огонь гасится (`setblock air`), а остов остаётся как шрам.

Бюджет команд ограничен сверху (`MAX_*`): авария не должна выдавливать из
очереди RCON полезные команды (инвариант I5 — тяжёлое вне кадра, а здесь ещё
и вне бюджета).

Модуль НЕ знает про DPG: он выдаёт списки команд и снимки сайтов, поэтому
тестируется без контекста Dear PyGui.
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import mc
from .model import Blueprint, get_blueprint, project
from .rcon import CommandQueue, Priority

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]

# ---------------------------------------------------------------------------
#  Бюджет и материалы
# ---------------------------------------------------------------------------
MAX_HULL_CELLS = 46        # блоков в остове (чертёж самолёта ~60-90)
MAX_DEBRIS = 9             # падающих обломков
MAX_FIRE_CELLS = 14        # блоков огня в зоне
BURN_TIME = 45.0           # секунд горения зоны
SMOKE_INTERVAL = 2.5       # как часто докидывать партиклы дыма/огня
SCAR_TTL = 900.0           # сколько остов живёт на карте (маркер), с

#: материалы сгоревшего остова: уголь, закопчённый камень, железо, тление
BURNT_MATERIALS: Tuple[str, ...] = (
    "minecraft:coal_block",
    "minecraft:polished_blackstone",
    "minecraft:iron_bars",
    "minecraft:iron_block",
    "minecraft:magma_block",
)
#: чем заменяем «стеклянные»/хрупкие блоки оригинала
FRAGILE = {"minecraft:glass", "minecraft:glass_pane", "minecraft:ice",
           "minecraft:glowstone"}

#: радиус кратера по типу техники
CRATER_RADIUS = {"aircraft": 4, "transport": 5, "helicopter": 3,
                 "drone": 2, "tank": 3, "truck": 2, "apc": 3, "unit": 3}
#: сколько обломков разбрасывает взрыв
DEBRIS_COUNT = {"aircraft": 9, "transport": 8, "helicopter": 6, "drone": 3,
                "tank": 5, "truck": 4, "apc": 5, "unit": 5}


@dataclass
class WreckSite:
    """След аварии: где, что горело и какие блоки поставлены."""
    id: int
    x: float
    y: float
    z: float
    kind: str = "unit"                  # тип техники (для иконки на карте)
    label: str = ""                     # человекочитаемое имя машины
    reason: str = ""                    # почему погибла
    unit_id: Optional[int] = None
    created: float = field(default_factory=time.time)
    burn_until: float = 0.0             # момент, когда огонь гаснет
    hull: Dict[Tuple[int, int, int], str] = field(default_factory=dict)
    fire: List[Tuple[int, int, int]] = field(default_factory=list)
    extinguished: bool = False
    last_smoke: float = 0.0

    @property
    def burning(self) -> bool:
        return (not self.extinguished) and time.time() < self.burn_until

    def age(self, now: Optional[float] = None) -> float:
        return (now or time.time()) - self.created

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "x": self.x, "y": self.y, "z": self.z,
                "kind": self.kind, "label": self.label, "reason": self.reason,
                "unit_id": self.unit_id, "created": self.created,
                "burning": self.burning, "age": round(self.age(), 1),
                "hull_blocks": len(self.hull), "fire_blocks": len(self.fire)}


# ---------------------------------------------------------------------------
#  Построение остова
# ---------------------------------------------------------------------------
def burnt_block(block: str, x: int, y: int, z: int) -> str:
    """Материал сгоревшего остова: детерминированно от координат.

    Один и тот же остов при пересоздании выглядит одинаково — важно и для
    тестов, и для того, чтобы «шрам» не менялся при повторной отрисовке.
    """
    if block in FRAGILE:
        return "minecraft:iron_bars"
    h = (x * 73856093) ^ (y * 19349663) ^ (z * 83492791)
    return BURNT_MATERIALS[abs(h) % len(BURNT_MATERIALS)]


def burnt_hull(blueprint: Blueprint, pos: Sequence[float], yaw: float,
               pitch: float = 0.0, roll: float = 0.0,
               keep: float = 0.55, seed: int = 0,
               max_cells: int = MAX_HULL_CELLS
               ) -> Dict[Tuple[int, int, int], str]:
    """Остов сгоревшей машины: проекция чертежа с «отвалившимися» клетками.

    `keep` — доля клеток, которая пережила взрыв. Отсев детерминирован
    (`random.Random(seed)` по uid юнита), поэтому результат воспроизводим.
    Машина «проседает» в землю на блок: взрыв прижимает остов к воронке.
    """
    projected = project(blueprint, (pos[0], pos[1] - 1.0, pos[2]), yaw,
                        pitch, roll)
    rng = random.Random(seed)
    items = sorted(projected.items())
    rng.shuffle(items)
    out: Dict[Tuple[int, int, int], str] = {}
    for (x, y, z), block in items:
        if len(out) >= max_cells:
            break
        if rng.random() > keep:
            continue                      # клетку оторвало взрывом
        out[(x, y, z)] = burnt_block(block, x, y, z)
    return out


def fire_cells(hull: Dict[Tuple[int, int, int], str], seed: int = 0,
               max_cells: int = MAX_FIRE_CELLS) -> List[Tuple[int, int, int]]:
    """Клетки огня: над блоками остова и по краю воронки."""
    if not hull:
        return []
    rng = random.Random(seed + 17)
    keys = sorted(hull)
    rng.shuffle(keys)
    out: List[Tuple[int, int, int]] = []
    seen = set()
    for (x, y, z) in keys:
        cell = (x, y + 1, z)
        if cell in hull or cell in seen:
            continue
        seen.add(cell)
        out.append(cell)
        if len(out) >= max_cells:
            break
    return out


def debris_commands(pos: Vec3, kind: str, seed: int = 0,
                    nbt: mc.NbtDialect = mc.LEGACY_NBT) -> List[str]:
    """Падающие обломки: летят в разные стороны и разбиваются о землю."""
    rng = random.Random(seed + 91)
    count = DEBRIS_COUNT.get(kind, 5)
    cmds: List[str] = []
    for _ in range(count):
        ang = rng.uniform(0.0, math.tau)
        spd = rng.uniform(0.25, 1.05)
        motion = (math.cos(ang) * spd, rng.uniform(0.55, 1.35),
                  math.sin(ang) * spd)
        block = rng.choice(("minecraft:coal_block", "minecraft:iron_block",
                            "minecraft:polished_blackstone",
                            "minecraft:iron_bars"))
        origin = (pos[0] + rng.uniform(-1.5, 1.5),
                  pos[1] + rng.uniform(0.5, 2.5),
                  pos[2] + rng.uniform(-1.5, 1.5))
        cmds.append(mc.falling_block(origin, block, motion, nbt))
    return cmds


# ---------------------------------------------------------------------------
#  Менеджер
# ---------------------------------------------------------------------------
class WreckageManager:
    """Реестр мест аварий: команды в мир, тик огня, снимок для карты."""

    def __init__(self, queue: Optional[CommandQueue] = None,
                 world: Optional[Any] = None,
                 nbt: mc.NbtDialect = mc.LEGACY_NBT,
                 burn_time: float = BURN_TIME,
                 scar_ttl: float = SCAR_TTL,
                 limit: int = 64):
        self.queue = queue
        self.world = world
        self.nbt = nbt
        self.burn_time = float(burn_time)
        self.scar_ttl = float(scar_ttl)
        self.limit = int(limit)
        self._sites: Dict[int, WreckSite] = {}
        self._next_id = 1
        self.stats = {"sites": 0, "hull_blocks": 0, "debris": 0,
                      "fire_cells": 0, "craters": 0, "extinguished": 0}

    # ------------------------------------------------------------- создание
    def create(self, pos: Vec3, kind: str = "unit", label: str = "",
               reason: str = "", blueprint: Optional[str] = None,
               yaw: float = 0.0, pitch: float = 0.0, roll: float = 0.0,
               unit_id: Optional[int] = None,
               ground_y: Optional[float] = None) -> WreckSite:
        """Создать место аварии и отправить команды в мир.

        `ground_y` — высота поверхности (для авиации: остов должен лежать на
        земле, а не висеть в воздухе на высоте гибели).
        """
        now = time.time()
        site = WreckSite(id=self._next_id, x=float(pos[0]),
                         y=float(pos[1]), z=float(pos[2]), kind=kind,
                         label=label, reason=reason, unit_id=unit_id,
                         created=now, burn_until=now + self.burn_time)
        self._next_id += 1
        self._sites[site.id] = site
        if len(self._sites) > self.limit:      # старые шрамы чистим первыми
            oldest = sorted(self._sites, key=lambda i: self._sites[i].created)
            for sid in oldest[:len(self._sites) - self.limit]:
                self.remove(sid)

        x, y, z = site.x, site.y, site.z
        if ground_y is not None:
            y = float(ground_y)
            site.y = y
        seed = int(unit_id if unit_id is not None else site.id)
        cmds: List[str] = []

        # 1. кратер
        radius = CRATER_RADIUS.get(kind, 3)
        cmds.extend(mc.crater(int(round(x)), int(round(y)), int(round(z)),
                              radius, destroy=True))
        self.stats["craters"] += 1

        # 2. сгоревший остов
        bp: Optional[Blueprint] = None
        if blueprint:
            try:
                bp = get_blueprint(blueprint)
            except (KeyError, ValueError):
                bp = None
        hull: Dict[Tuple[int, int, int], str] = {}
        if bp is not None:
            hull = burnt_hull(bp, (x, y, z), yaw, pitch, roll, seed=seed)
        if not hull:                            # нет чертежа — куча обломков
            hull = _rubble(x, y, z, seed)
        site.hull = hull
        for (bx, by, bz), block in sorted(hull.items()):
            cmds.append(mc.setblock(bx, by, bz, block))
        self.stats["hull_blocks"] += len(hull)

        # 3. падающие обломки
        debris = debris_commands((x, y + 1.0, z), kind, seed=seed,
                                 nbt=self.nbt)
        cmds.extend(debris)
        self.stats["debris"] += len(debris)

        # 4. зона горит
        site.fire = fire_cells(hull, seed=seed)
        for (fx, fy, fz) in site.fire:
            cmds.append(mc.fire_layer(fx, fy, fz))
        self.stats["fire_cells"] += len(site.fire)
        cmds.append(mc.particle("minecraft:explosion_emitter", (x, y + 1.0, z),
                                (0.5, 0.5, 0.5), 0.0, 1))
        cmds.append(mc.playsound("minecraft:entity.generic.explode",
                                 (x, y, z), volume=1.4, pitch=0.55))

        self._submit(cmds, Priority.CRITICAL)
        site.last_smoke = now

        # 5. след на карте
        if self.world is not None:
            try:
                self.world.add_marker(x, z, "wreck", ttl=self.scar_ttl,
                                      text=f"обломки: {label or kind}")
            except Exception:  # noqa: BLE001
                log.exception("Не удалось поставить маркер обломков")
        self.stats["sites"] += 1
        log.info("Авария #%d (%s) в (%.0f, %.0f): остов %d блоков, огонь %d",
                 site.id, label or kind, x, z, len(hull), len(site.fire))
        return site

    def create_from_unit(self, unit, reason: str = "",
                         ground_y: Optional[float] = None) -> Optional[WreckSite]:
        """Удобная обёртка: сайт аварии прямо из объекта юнита."""
        spec = getattr(unit, "spec", None)
        return self.create(tuple(unit.pos), kind=getattr(spec, "kind", "unit"),
                           label=getattr(spec, "label", ""), reason=reason
                           or getattr(unit, "crashed_reason", ""),
                           blueprint=getattr(spec, "blueprint", None),
                           yaw=getattr(unit, "yaw", 0.0),
                           pitch=getattr(unit, "pitch", 0.0),
                           roll=getattr(unit, "roll", 0.0),
                           unit_id=getattr(unit, "id", None),
                           ground_y=ground_y)

    # ---------------------------------------------------------------- тик
    def step(self, dt: float, now: Optional[float] = None) -> int:
        """Тик: дым/пламя, пока горит; погасить и снять огонь по таймеру.

        Возвращает число отправленных команд (для статистики движка).
        """
        now = now if now is not None else time.time()
        sent = 0
        for site in list(self._sites.values()):
            if site.extinguished:
                continue
            if now >= site.burn_until:
                sent += self.extinguish(site.id)
                continue
            if now - site.last_smoke >= SMOKE_INTERVAL:
                site.last_smoke = now
                cmds = [mc.particle("minecraft:large_smoke",
                                    (site.x, site.y + 1.5, site.z),
                                    (2.2, 1.6, 2.2), 0.02, 26),
                        mc.particle("minecraft:flame",
                                    (site.x, site.y + 0.8, site.z),
                                    (1.6, 0.9, 1.6), 0.03, 14),
                        mc.particle("minecraft:campfire_cosy_smoke",
                                    (site.x, site.y + 2.0, site.z),
                                    (0.6, 1.2, 0.6), 0.01, 6)]
                self._submit(cmds, Priority.VISUAL)
                sent += len(cmds)
        return sent

    def extinguish(self, site_id: int) -> int:
        """Погасить зону: снять блоки огня, остов остаётся шрамом."""
        site = self._sites.get(site_id)
        if site is None or site.extinguished:
            return 0
        site.extinguished = True
        cmds = [mc.setblock(x, y, z, "minecraft:air") for (x, y, z) in site.fire]
        cmds.append(mc.particle("minecraft:large_smoke",
                                (site.x, site.y + 1.0, site.z),
                                (2.5, 1.5, 2.5), 0.01, 40))
        self._submit(cmds, Priority.NORMAL)
        self.stats["extinguished"] += 1
        log.info("Авария #%d: огонь погашен (остов %d блоков остался)",
                 site_id, len(site.hull))
        return len(cmds)

    def remove(self, site_id: int) -> int:
        """Полностью убрать след: остов и огонь снимаются с карты мира."""
        site = self._sites.pop(site_id, None)
        if site is None:
            return 0
        cmds = [mc.setblock(x, y, z, "minecraft:air")
                for (x, y, z) in sorted(set(site.hull) | set(site.fire))]
        self._submit(cmds, Priority.NORMAL)
        return len(cmds)

    def prune(self, max_age: Optional[float] = None) -> int:
        """Убрать давно остывшие шрамы (экономим блоки и память)."""
        limit = self.scar_ttl if max_age is None else float(max_age)
        n = 0
        for sid in [s.id for s in self._sites.values()
                    if s.age() > limit and s.extinguished]:
            n += 1 if self.remove(sid) else 0
        return n

    def clear(self) -> int:
        n = 0
        for sid in list(self._sites):
            self.remove(sid)
            n += 1
        return n

    # ----------------------------------------------------------- снимки
    def get(self, site_id: int) -> Optional[WreckSite]:
        return self._sites.get(site_id)

    def sites(self) -> List[WreckSite]:
        return sorted(self._sites.values(), key=lambda s: s.created)

    def burning_sites(self) -> List[WreckSite]:
        return [s for s in self._sites.values() if s.burning]

    def snapshot(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.sites()]

    def nearest(self, x: float, z: float,
                max_dist: float = 1e9) -> Optional[WreckSite]:
        best, best_d = None, max_dist
        for site in self._sites.values():
            d = math.hypot(site.x - x, site.z - z)
            if d < best_d:
                best, best_d = site, d
        return best

    # ----------------------------------------------------------- служба
    def _submit(self, cmds: Sequence[str], priority: int) -> None:
        if self.queue is None:
            return
        for cmd in cmds:
            self.queue.submit(cmd, priority)


def _rubble(x: float, y: float, z: float, seed: int) -> Dict[Tuple[int, int,
                                                                 int], str]:
    """Куча обломков, когда чертёж неизвестен: уголь + железо в воронке."""
    rng = random.Random(seed + 5)
    out: Dict[Tuple[int, int, int], str] = {}
    ix, iy, iz = int(round(x)), int(round(y)), int(round(z))
    for _ in range(18):
        dx = rng.randint(-2, 2)
        dz = rng.randint(-2, 2)
        dy = rng.randint(0, 1)
        cell = (ix + dx, iy + dy, iz + dz)
        if cell in out:
            continue
        out[cell] = rng.choice(BURNT_MATERIALS)
    return out
