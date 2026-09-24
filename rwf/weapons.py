"""
Оружие: каталог, подвесы с боезапасом, баллистика, эффекты поражения.

Закрытые дефекты наброска
-------------------------
* **UNIT-07** — боезапас расходовался только на бумаге: `ammo` нигде не
  уменьшался, огонь был бесконечным.
* **WPN-01** — потеряна исходная фича проекта: пробивка шахты до уровня цели
  в пещере. Поля `tunnel_r`/`crater_r` лежали в каталоге неиспользованными.
* **WPN-02** — `fireball` спавнился с полем `direction`, которого у этой
  сущности нет; параметр молча игнорировался.
* **WPN-03** — у `fireball` не обнулялось `power` (внутреннее ускорение),
  из-за чего снаряд улетал не по вектору `Motion`.
* **WPN-04** — бомбы сбрасывались без упреждения: при скорости 20 м/с и высоте
  100 м промах составляет ~70 блоков.
* **WPN-05** — «управляемые» ракеты не наводились вообще.
* **WPN-08** — скорострельность `rate` лежала в каталоге, а `can_fire()` для
  ракет и УР использовала хардкод 0.8 с.
* **WPN-09** — все бомбы залпа спавнились в одной точке и сливались в один взрыв.

Единицы измерения
-----------------
Поле `Motion` в NBT сущности — **блоки за тик**, а не за секунду. 1 блок/тик =
20 м/с. В каталоге `speed` указан в блоках/тик (как его ожидает Minecraft),
а `speed_mps` — производное значение для интерфейса и баллистики.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import mc
from .config import CombatConfig
from .geometry import distance as _geo_distance
from .events import TOPIC_WEAPON_FIRED, EventBus
from .rcon import CommandQueue, Priority

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]
TICKS_PER_SECOND = 20.0


# ---------------------------------------------------------------------------
#  Каталог
# ---------------------------------------------------------------------------
WEAPON_CATEGORIES: Dict[str, str] = {
    "bomb": "Бомба (свободное падение)",
    "rocket": "НАР (неуправляемая)",
    "missile": "УР (управляемая)",
    "cannon": "Пушка",
    "mg": "Пулемёт",
}

#: cat — категория, label — название для UI, count — боезапас подвеса,
#: speed — блоки/тик (NBT Motion), rate — выстрелов/с для автоматического
#: огня, burst — длина очереди, tnt — число боеприпасов в залпе,
#: fuse — тики взрывателя, crater_r/tunnel_r — радиусы поражения,
#: power — сила взрыва снаряда, cooldown — пауза между пусками.
WEAPONS: Dict[str, Dict[str, object]] = {
    # --- бомбы ------------------------------------------------------------
    "fab100": {"cat": "bomb", "label": "ФАБ-100", "count": 8, "tnt": 4,
               "fuse": 0, "crater_r": 2, "tunnel_r": 1, "power": 2,
               "cooldown": 0.6, "drop_speed": 0.0},
    "fab500": {"cat": "bomb", "label": "ФАБ-500", "count": 4, "tnt": 4,
               "fuse": 0, "crater_r": 3, "tunnel_r": 1, "power": 4,
               "cooldown": 0.8, "drop_speed": 0.0},
    "fab1500": {"cat": "bomb", "label": "ФАБ-1500", "count": 2, "tnt": 6,
                "fuse": 0, "crater_r": 5, "tunnel_r": 2, "power": 6,
                "cooldown": 1.2, "drop_speed": 0.0},
    "fab5000": {"cat": "bomb", "label": "ФАБ-5000", "count": 1, "tnt": 8,
                "fuse": 0, "crater_r": 7, "tunnel_r": 3, "power": 9,
                "cooldown": 2.0, "drop_speed": 0.0},
    "nuke": {"cat": "bomb", "label": "Ядерная", "count": 1, "tnt": 6,
             "fuse": 0, "crater_r": 14, "tunnel_r": 5, "power": 12,
             "cooldown": 5.0, "drop_speed": 0.0},
    # --- НАР --------------------------------------------------------------
    "s5": {"cat": "rocket", "label": "С-5", "entity": "minecraft:fireball",
           "count": 8, "speed": 2.0, "burst": 4, "rate": 4.0, "power": 1,
           "spread": 0.10, "cooldown": 0.9},
    "s8": {"cat": "rocket", "label": "С-8", "entity": "minecraft:fireball",
           "count": 6, "speed": 2.4, "burst": 3, "rate": 3.0, "power": 2,
           "spread": 0.14, "cooldown": 1.1},
    "s13": {"cat": "rocket", "label": "С-13", "entity": "minecraft:fireball",
            "count": 3, "speed": 2.2, "burst": 1, "rate": 1.2, "power": 4,
            "spread": 0.05, "cooldown": 1.6},
    # --- УР ---------------------------------------------------------------
    "r73": {"cat": "missile", "label": "Р-73 (В-В)", "entity": "minecraft:arrow",
            "count": 4, "speed": 2.6, "burst": 1, "rate": 1.0, "power": 3,
            "cooldown": 1.4, "guided": True, "range": 400.0},
    "r27": {"cat": "missile", "label": "Р-27 (В-В)", "entity": "minecraft:arrow",
            "count": 2, "speed": 2.2, "burst": 1, "rate": 0.6, "power": 4,
            "cooldown": 2.2, "guided": True, "range": 800.0},
    "agm": {"cat": "missile", "label": "AGM (В-З)", "entity": "minecraft:arrow",
            "count": 2, "speed": 2.4, "burst": 1, "rate": 0.8, "power": 5,
            "cooldown": 1.8, "guided": True, "range": 600.0},
    "kh29": {"cat": "missile", "label": "Х-29 (В-З)", "entity": "minecraft:arrow",
             "count": 1, "speed": 2.0, "burst": 1, "rate": 0.5, "power": 8,
             "cooldown": 3.0, "guided": True, "range": 900.0, "tunnel_r": 3,
             "crater_r": 6},
    # --- пополнение арсенала v15 ------------------------------------------
    "kab500": {"cat": "bomb", "label": "КАБ-500С (корр.)", "count": 4, "tnt": 5,
               "fuse": 0, "crater_r": 2, "tunnel_r": 1, "power": 6,
               "cooldown": 1.0, "drop_speed": 0.0},
    "s25": {"cat": "rocket", "label": "С-25", "entity": "minecraft:arrow",
            "count": 2, "speed": 2.8, "burst": 1, "rate": 0.5, "power": 6,
            "spread": 0.02, "cooldown": 2.2, "crater_r": 4},
    "vikhr": {"cat": "missile", "label": "ПТУР Вихрь", "entity": "minecraft:arrow",
              "count": 4, "speed": 2.5, "burst": 1, "rate": 0.8, "power": 5,
              "cooldown": 2.4, "guided": True, "range": 900.0, "tunnel_r": 2},
    "kornet": {"cat": "missile", "label": "ПТУР Корнет", "entity": "minecraft:arrow",
               "count": 2, "speed": 2.3, "burst": 1, "rate": 0.6, "power": 6,
               "cooldown": 3.2, "guided": True, "range": 700.0, "tunnel_r": 2,
               "crater_r": 3},
    "ags17": {"cat": "mg", "label": "АГС-17", "entity": "minecraft:arrow",
              "count": 60, "speed": 2.2, "burst": 3, "rate": 4.0, "power": 1,
              "spread": 0.09, "cooldown": 0.25},
    # --- пушки ------------------------------------------------------------
    "gsh23": {"cat": "cannon", "label": "ГШ-23", "entity": "minecraft:small_fireball",
              "count": 120, "speed": 3.2, "burst": 4, "rate": 8.0, "power": 1,
              "spread": 0.03, "cooldown": 0.12},
    "2a42": {"cat": "cannon", "label": "2А42", "entity": "minecraft:small_fireball",
             "count": 90, "speed": 3.4, "burst": 3, "rate": 6.0, "power": 1,
             "spread": 0.02, "cooldown": 0.16},
    "m61": {"cat": "cannon", "label": "M61 Вулкан", "entity": "minecraft:small_fireball",
            "count": 200, "speed": 3.8, "burst": 6, "rate": 12.0, "power": 1,
            "spread": 0.04, "cooldown": 0.08},
    # --- пулемёты ---------------------------------------------------------
    "pkm": {"cat": "mg", "label": "ПКМ", "entity": "minecraft:arrow",
            "count": 200, "speed": 3.0, "burst": 4, "rate": 6.0, "power": 0,
            "spread": 0.03, "cooldown": 0.15},
    "m2": {"cat": "mg", "label": "M2 Браунинг", "entity": "minecraft:arrow",
           "count": 250, "speed": 3.4, "burst": 5, "rate": 8.0, "power": 0,
           "spread": 0.03, "cooldown": 0.12},
    "yakt": {"cat": "mg", "label": "ЯкБ-12.7", "entity": "minecraft:arrow",
             "count": 180, "speed": 3.2, "burst": 5, "rate": 7.0, "power": 0,
             "spread": 0.04, "cooldown": 0.13},
}


def weapon(key: str) -> Dict[str, object]:
    try:
        return WEAPONS[key]
    except KeyError:
        raise KeyError(f"Неизвестное оружие {key!r}. "
                       f"Доступно: {', '.join(sorted(WEAPONS))}") from None


def available_for(category: str) -> List[str]:
    return sorted(k for k, v in WEAPONS.items() if v["cat"] == category)


def speed_mps(key: str) -> float:
    """Скорость снаряда в м/с (в NBT Motion — блоки/тик)."""
    return float(WEAPONS[key].get("speed", 0.0)) * TICKS_PER_SECOND


# ---------------------------------------------------------------------------
#  Подвес
# ---------------------------------------------------------------------------
class WeaponMount:
    """Точка подвески: категория, слот, оружие, боезапас, темп."""

    __slots__ = ("category", "slot", "key", "ammo", "ammo_max", "last_fire",
                 "fired_total", "jammed")

    def __init__(self, category: str, slot: str, key: Optional[str] = None):
        if category not in WEAPON_CATEGORIES:
            raise ValueError(f"Неизвестная категория подвеса: {category!r}")
        self.category = category
        self.slot = slot
        self.key: Optional[str] = None
        self.ammo = 0
        self.ammo_max = 0
        self.last_fire = 0.0
        self.fired_total = 0
        self.jammed = False
        if key:
            self.load(key)

    # ------------------------------------------------------------- оружие
    def load(self, key: Optional[str]) -> None:
        """Зарядить подвес. `None` — разрядить."""
        if key is None:
            self.key = None
            self.ammo = self.ammo_max = 0
            return
        spec = weapon(key)
        if spec["cat"] != self.category:
            raise ValueError(
                f"{spec['label']} ({spec['cat']}) нельзя поставить на подвес "
                f"категории {self.category!r} ({self.slot})")
        self.key = key
        self.ammo_max = int(spec.get("count", 1))
        self.ammo = self.ammo_max

    def reload(self) -> None:
        self.ammo = self.ammo_max
        self.jammed = False

    @property
    def spec(self) -> Optional[Dict[str, object]]:
        return WEAPONS.get(self.key) if self.key else None

    @property
    def label(self) -> str:
        spec = self.spec
        return spec["label"] if spec else "— пусто —"   # type: ignore[return-value]

    @property
    def cooldown(self) -> float:
        spec = self.spec
        if not spec:
            return 1.0
        if "cooldown" in spec:
            return float(spec["cooldown"])
        rate = float(spec.get("rate", 1.0))
        return 1.0 / rate if rate > 0 else 1.0

    def ready(self, now: Optional[float] = None) -> bool:
        """Готов к выстрелу: заряжен, не заклинил, выдержана пауза."""
        now = time.monotonic() if now is None else now
        return bool(self.key) and self.ammo > 0 and not self.jammed \
            and (now - self.last_fire) >= self.cooldown

    def consume(self, n: int = 1) -> int:
        """Списать боезапас. Возвращает, сколько реально израсходовано."""
        used = max(0, min(n, self.ammo))
        self.ammo -= used
        self.fired_total += used
        return used

    def mark_fired(self, now: Optional[float] = None) -> None:
        self.last_fire = time.monotonic() if now is None else now

    def snapshot(self) -> Dict[str, object]:
        now = time.monotonic()
        #: `since_fire` — сколько секунд назад был выстрел (None — не стрелял).
        #: По нему UI показывает состояние «Отстреливается» без обращения
        #: к движку (rwf/unitstate.py).
        since = (now - self.last_fire) if self.last_fire > 0.0 else None
        return {
            "category": self.category, "slot": self.slot, "key": self.key,
            "label": self.label, "ammo": self.ammo, "ammo_max": self.ammo_max,
            "cooldown": round(self.cooldown, 3), "ready": self.ready(),
            "fired_total": self.fired_total, "jammed": self.jammed,
            "since_fire": round(since, 2) if since is not None else None,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Mount {self.slot}: {self.label} {self.ammo}/{self.ammo_max}>"


def build_mounts(loadout: Sequence[Tuple[str, str, Optional[str]]]) -> List[WeaponMount]:
    return [WeaponMount(cat, slot, key) for cat, slot, key in loadout]


# ---------------------------------------------------------------------------
#  Цель и упреждение
# ---------------------------------------------------------------------------
@dataclass
class Target:
    """Цель: позиция, скорость (для упреждения), имя и (опц.) юнит-цель."""
    pos: Vec3
    vel: Vec3 = (0.0, 0.0, 0.0)
    name: str = ""
    unit_id: Optional[int] = None

    def predicted(self, dt: float) -> Vec3:
        return (self.pos[0] + self.vel[0] * dt,
                self.pos[1] + self.vel[1] * dt,
                self.pos[2] + self.vel[2] * dt)


def distance(a: Vec3, b: Vec3) -> float:
    # P1.1: единый источник правды — geometry.distance; оставляем имя в
    # модуле (его импортируют missiles.py/ai.py и тесты) как тонкую обёртку.
    return _geo_distance(a, b)


def solve_lead(shooter: Vec3, target: Vec3, target_vel: Vec3,
               proj_speed: float) -> float:
    """Время полёта до точки встречи (решение квадратного уравнения).

    |T + V·t − S| = v·t.  При нулевой скорости цели возвращает просто
    дистанцию / скорость.
    """
    dx, dy, dz = target[0] - shooter[0], target[1] - shooter[1], target[2] - shooter[2]
    vx, vy, vz = target_vel
    a = vx * vx + vy * vy + vz * vz - proj_speed * proj_speed
    b = 2.0 * (dx * vx + dy * vy + dz * vz)
    c = dx * dx + dy * dy + dz * dz
    if abs(a) < 1e-6:
        return math.sqrt(c) / proj_speed if proj_speed > 0 else 0.0
    disc = b * b - 4 * a * c
    if disc < 0:
        return math.sqrt(c) / proj_speed if proj_speed > 0 else 0.0
    root = math.sqrt(disc)
    t1, t2 = (-b - root) / (2 * a), (-b + root) / (2 * a)
    candidates = [t for t in (t1, t2) if t > 0]
    return min(candidates) if candidates else math.sqrt(c) / max(proj_speed, 1e-6)


def segment_distance(p: Vec3, a: Vec3, b: Vec3) -> float:
    """Расстояние от точки до отрезка a-b.

    Нужно для попадания быстрой ракетой: за один тик она пролетает 10-15
    блоков, поэтому проверка «дистанция до цели < радиуса» на концах отрезка
    промахивается — цель оказывается между кадрами.
    """
    abx, aby, abz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    apx, apy, apz = p[0] - a[0], p[1] - a[1], p[2] - a[2]
    ab2 = abx * abx + aby * aby + abz * abz
    if ab2 < 1e-9:
        return math.sqrt(apx * apx + apy * apy + apz * apz)
    t = max(0.0, min(1.0, (apx * abx + apy * aby + apz * abz) / ab2))
    cx = apx - abx * t
    cy = apy - aby * t
    cz = apz - abz * t
    return math.sqrt(cx * cx + cy * cy + cz * cz)


def unit_vector(a: Vec3, b: Vec3) -> Optional[Vec3]:
    dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    d = math.sqrt(dx * dx + dy * dy + dz * dz)
    if d < 1e-6:
        return None
    return (dx / d, dy / d, dz / d)


# ---------------------------------------------------------------------------
#  Отложенные эффекты
# ---------------------------------------------------------------------------
@dataclass
class PendingEffect:
    """Эффект, который сработает позже (взрыв бомбы после падения).

    `delay` — в секундах СИМУЛЯЦИИ, а не настенного времени: если движок
    поставлен на паузу, воронка должна появляться вместе с бомбой, а не
    «по часам».
    """
    delay: float
    commands: List[str] = field(default_factory=list)
    kind: str = "impact"
    label: str = ""


# ---------------------------------------------------------------------------
#  Управляемая ракета
# ---------------------------------------------------------------------------
class GuidedMissile:
    """УР с реальным наведением.

    Ванильный `fireball` летит сам и наводиться не умеет, поэтому ракета
    реализуется стрелой с `NoGravity:1b`, вектор которой пересчитывается
    каждый тик в сторону цели. Стоимость — 1-2 команды на тик, разворот
    ограничен `turn_rate`, поэтому ракета может и промахнуться по манёвренной
    цели (WPN-05).
    """

    __slots__ = ("mid", "tag", "weapon_key", "pos", "vel", "target", "age",
                 "ttl", "turn_rate", "hit_radius", "power", "alive",
                 "crater_r", "tunnel_r", "owner_tag")

    def __init__(self, mid: int, weapon_key: str, pos: Vec3, vel: Vec3,
                 target: Target, cfg: CombatConfig, owner_tag: str = ""):
        spec = weapon(weapon_key)
        self.mid = mid
        self.tag = f"rwf_msl_{mid}"
        self.weapon_key = weapon_key
        self.pos = pos
        self.vel = vel
        self.target = target
        self.age = 0.0                     # секунды симуляции
        self.ttl = cfg.missile_max_time
        self.turn_rate = cfg.missile_turn_rate
        self.hit_radius = cfg.missile_hit_radius
        self.power = int(spec.get("power", 3))
        self.crater_r = int(spec.get("crater_r", 2))
        self.tunnel_r = int(spec.get("tunnel_r", 0))
        self.owner_tag = owner_tag
        self.alive = True

    @property
    def speed(self) -> float:
        return math.sqrt(sum(c * c for c in self.vel))

    def steer(self, dt: float, target_pos: Vec3) -> None:
        """Довернуть вектор скорости в сторону цели, не превышая turn_rate."""
        cur = unit_vector((0.0, 0.0, 0.0), self.vel)
        want = unit_vector(self.pos, target_pos)
        if cur is None or want is None:
            return
        max_angle = math.radians(self.turn_rate) * dt
        dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(cur, want))))
        angle = math.acos(dot)
        if angle <= 1e-6:
            return
        t = 1.0 if angle <= max_angle else max_angle / angle
        new = tuple(c + (w - c) * t for c, w in zip(cur, want))
        norm = math.sqrt(sum(c * c for c in new)) or 1.0
        sp = self.speed
        self.vel = (new[0] / norm * sp, new[1] / norm * sp, new[2] / norm * sp)

    def update(self, dt: float, target_pos: Vec3) -> Tuple[List[str], bool]:
        """Шаг ракеты. Возвращает (команды, детонировала ли).

        `vel` хранится в блоках/тик (ровно то, что ждёт NBT `Motion`),
        поэтому за dt секунд ракета проходит vel * dt * 20 блоков.
        """
        self.age += dt
        self.steer(dt, target_pos)
        prev = self.pos
        ticks = dt * TICKS_PER_SECOND
        self.pos = (self.pos[0] + self.vel[0] * ticks,
                    self.pos[1] + self.vel[1] * ticks,
                    self.pos[2] + self.vel[2] * ticks)
        cmds = [
            f"data merge entity {mc.by_tag(self.tag)} "
            f"{{Motion:{mc.LEGACY_NBT.vec(self.vel)},Pos:{mc.LEGACY_NBT.veci(self.pos)}}}",
            mc.particle("minecraft:campfire_cosy_smoke", self.pos,
                        (0.1, 0.1, 0.1), 0.0, 3),
        ]
        if segment_distance(target_pos, prev, self.pos) <= self.hit_radius:
            return cmds, True
        if self.age > self.ttl:
            return cmds, True
        return cmds, False

    def detonate_commands(self, cfg: CombatConfig) -> List[str]:
        out = [mc.instant_explosion(self.pos), mc.kill_tag(self.tag)]
        if self.crater_r and cfg.crater_enabled:
            out += mc.crater(int(self.pos[0]), int(self.pos[1]), int(self.pos[2]),
                             self.crater_r, destroy=cfg.drop_blocks)
        return out

    def spawn_commands(self) -> List[str]:
        spec = weapon(self.weapon_key)
        entity = str(spec.get("entity", "minecraft:arrow"))
        nbt = (f"{{NoGravity:1b,Silent:1b,Invulnerable:1b,PersistenceRequired:1b,"
               f"{mc.LEGACY_NBT.tags}:[\"{self.tag}\"],"
               f"Motion:{mc.LEGACY_NBT.vec(self.vel)},"
               f"Pos:{mc.LEGACY_NBT.veci(self.pos)},"
               f"damage:{self.power * 3}.0d}}")
        return [f"summon {entity} {mc.fmt_pos(*self.pos)} {nbt}"]


# ---------------------------------------------------------------------------
#  Система вооружения
# ---------------------------------------------------------------------------
class WeaponSystem:
    """Стрельба, расход боезапаса, отложенные эффекты, наведение УР.

    Команды не отправляются напрямую: всё уходит в `CommandQueue` с нужным
    приоритетом, поэтому физический тик и GUI не блокируются на сети.
    """

    def __init__(self, cfg: CombatConfig, queue: CommandQueue, world,
                 bus: Optional[EventBus] = None,
                 caps: Optional[mc.ServerCaps] = None,
                 combat=None, missiles=None):
        self.cfg = cfg
        self.queue = queue
        self.world = world
        self.bus = bus if bus is not None else world.bus
        self.caps = caps or mc.ServerCaps()
        self.combat = combat                  # CombatSystem: урон технике
        self.missile_manager = missiles       # MissileManager: блочные ракеты
        # (self.missiles — список старых управляемых ракет, не трогать)
        self.missiles: List[GuidedMissile] = []
        self.effects: List[PendingEffect] = []
        # (due, pos, radius, damage, source_kind, source_id, skip) — урон технике
        self._pending_blasts: List[tuple] = []
        self._next_mid = 1
        self.shots = 0
        self.missiles_launched = 0
        self.missiles_hit = 0
        self.ammo_spent = 0
        self.blocked_destructive = 0

    # ------------------------------------------------------------- залпы
    def fire(self, unit, mount: WeaponMount, target: Optional[Target] = None,
             now: Optional[float] = None) -> bool:
        """Выстрел из подвеса. False — не готов или нет боезапаса."""
        now = time.monotonic() if now is None else now
        if not mount.ready(now):
            return False
        spec = mount.spec
        if not spec or not mount.key:
            return False
        cat = str(spec["cat"])
        handler = {
            "bomb": self._fire_bomb,
            "rocket": self._fire_rocket,
            "missile": self._fire_missile,
            "cannon": self._fire_gun,
            "mg": self._fire_gun,
        }.get(cat)
        if handler is None:
            return False
        ok = handler(unit, mount, spec, target or self._default_target(unit), now)
        if ok:
            mount.mark_fired(now)
            self.shots += 1
            self.bus.publish(TOPIC_WEAPON_FIRED, unit.id, mount.key)
        return ok

    def fire_category(self, unit, category: str,
                      target: Optional[Target] = None) -> int:
        """Залп из всех готовых подвесов категории."""
        n = 0
        for mount in unit.mounts:
            if mount.category == category and self.fire(unit, mount, target):
                n += 1
        return n

    def fire_ready(self, unit, target: Optional[Target] = None,
                   categories: Optional[Iterable[str]] = None) -> int:
        """Огонь из всего готового. `categories` — ограничить набор."""
        cats = set(categories) if categories else None
        n = 0
        for mount in unit.mounts:
            if cats and mount.category not in cats:
                continue
            if self.fire(unit, mount, target):
                n += 1
        return n

    def _default_target(self, unit) -> Optional[Target]:
        """Цель по умолчанию — ближайший игрок (для ботов и турелей)."""
        players = self.world.get_players()
        if not players:
            return None
        best, best_d = None, float("inf")
        for name, rec in players.items():
            d = distance(unit.pos, rec["pos"])
            if d < best_d:
                best, best_d = Target(tuple(rec["pos"]), name=name), d
        return best

    # --------------------------------------------------------------- бомбы
    # ------------------------------------------------------------- урон
    @staticmethod
    def _damage_for(spec: Dict) -> float:
        """Ориентировочный урон попадания по технике."""
        cat = spec.get("cat")
        power = float(spec.get("power", 1) or 1)
        if cat == "bomb":
            return 40.0 + power * 12.0
        if cat == "rocket":
            return 18.0 + power * 8.0
        if cat == "missile":
            return 45.0 + power * 10.0
        if cat == "cannon":
            return 6.0 + power * 2.0
        return 3.0 + power * 1.0        # пулемёт

    def _schedule_blast(self, pos: Vec3, radius: float, damage: float,
                        source_kind: str, source_id: Optional[int],
                        skip: Optional[int], delay: float) -> None:
        """Отложенный урон технике в точке (когда долетит снаряд/бомба)."""
        if self.combat is None:
            return
        self._pending_blasts.append((time.monotonic() + max(0.0, delay), pos,
                                     radius, damage, source_kind, source_id, skip))

    def _process_blasts(self) -> None:
        if not self._pending_blasts or self.combat is None:
            return
        now = time.monotonic()
        due = [b for b in self._pending_blasts if b[0] <= now]
        if not due:
            return
        self._pending_blasts = [b for b in self._pending_blasts if b[0] > now]
        units = self.world.iter_units()
        for (_d, pos, radius, damage, kind, sid, skip) in due:
            self.combat.resolve_blast(units, pos, radius, damage, kind, sid, skip)

    def _fire_bomb(self, unit, mount: WeaponMount, spec: Dict,
                   target: Optional[Target], now: float) -> bool:
        count = int(spec.get("tnt", 1))
        used = mount.consume(count)
        if used <= 0:
            mount.ammo = 0
            return False
        self.ammo_spent += used
        count = min(used, self.cfg.max_commands_per_volley)

        x, y, z = unit.pos
        vx, vy, vz = unit.velocity()                 # м/с
        height = max(1.0, y - self._ground_below(unit))
        t_fall = math.sqrt(2.0 * height / max(1.0, self.cfg.gravity_effective))
        fuse = max(4, int(t_fall * TICKS_PER_SECOND))

        cmds: List[str] = []
        impacts: List[Vec3] = []
        for i in range(count):
            # Бомбовый «веер»: каждая следующая уходит назад по курсу, чтобы
            # снаряды легли цепочкой, а не слились в один взрыв (WPN-09).
            back = i * self.cfg.bomb_interval
            bx = x - vx * back
            by = y - 1.0
            bz = z - vz * back
            motion = (vx / TICKS_PER_SECOND, -0.05, vz / TICKS_PER_SECOND)
            cmds.append(mc.summon_tnt((bx, by, bz), fuse=fuse, motion=motion,
                                      nbt=self.caps.nbt))
            ix = bx + vx * t_fall
            iz = bz + vz * t_fall
            impacts.append((ix, by - height, iz))

        self._submit(cmds, Priority.CRITICAL, f"bomb:{unit.id}")
        self._schedule_impact(unit, mount, spec, impacts, target, t_fall)
        # Урон технике в точке падения (может подбить и своего при ошибке)
        main = impacts[len(impacts) // 2]
        self._schedule_blast(main, float(spec.get("crater_r", 3)) * 3.0 + 6.0,
                             self._damage_for(spec), f"бомба {mount.key}",
                             unit.id, unit.id, t_fall)
        self._fx(unit, "minecraft:entity.generic.explode", (x, y - 2, z), 0.6, 1.4)
        return True

    def _schedule_impact(self, unit, mount: WeaponMount, spec: Dict,
                         impacts: Sequence[Vec3], target: Optional[Target],
                         t_fall: float) -> None:
        """Эффекты поражения — в момент падения, а не в момент сброса."""
        cmds: List[str] = []
        crater_r = int(spec.get("crater_r", 3))
        tunnel_r = int(spec.get("tunnel_r", 0))
        main = impacts[len(impacts) // 2]
        aim = target.pos if target else None

        if self.cfg.crater_enabled and crater_r > 0:
            cmds += mc.crater(int(main[0]), int(main[1]), int(main[2]),
                              crater_r, destroy=self.cfg.drop_blocks)
        if self.cfg.tunnel_enabled and tunnel_r > 0 and aim:
            cmds += self._tunnel_to(main, aim, tunnel_r)

        if cmds:
            self.effects.append(PendingEffect(
                delay=max(0.0, t_fall), commands=cmds, kind="impact",
                label=f"{spec['label']} → ({main[0]:.0f}, {main[2]:.0f})"))

    def _tunnel_to(self, impact: Vec3, aim: Vec3, radius: int) -> List[str]:
        """Пробить шахту от точки удара до цели в пещере (WPN-01).

        Копается только если цель действительно ниже точки удара — иначе
        каждая бомбёжка портила бы поверхность.
        """
        if self.cfg.tunnel_only_underground and aim[1] >= impact[1] - 3:
            return []
        depth = min(self.cfg.tunnel_max_depth, int(impact[1] - aim[1]) + 2)
        if depth <= 1:
            return []
        if self.cfg.confirm_destructive and not self.cfg.destructive_confirmed:
            self.blocked_destructive += 1
            log.info("Пробивка шахты заблокирована: требуется подтверждение")
            return []
        return mc.tunnel_down(int(aim[0]), int(aim[2]),
                              y_from=int(impact[1]), y_to=int(aim[1]),
                              radius=radius, destroy=self.cfg.drop_blocks)

    # -------------------------------------------------------------- ракеты
    def _fire_rocket(self, unit, mount: WeaponMount, spec: Dict,
                     target: Optional[Target], now: float) -> bool:
        burst = int(spec.get("burst", 1))
        used = mount.consume(burst)
        if used <= 0:
            return False
        self.ammo_spent += used
        used = min(used, self.cfg.max_commands_per_volley)

        origin, direction = self._aim(unit, target, speed_mps(mount.key or "s5"))
        spread = float(spec.get("spread", 0.1))
        entity = str(spec.get("entity", "minecraft:fireball"))
        power = int(spec.get("power", 1))
        speed = float(spec["speed"]) * self.cfg.muzzle_speed_scale
        cmds: List[str] = []
        for i in range(used):
            d = self._spread(direction, i, used, spread)
            v = (d[0] * speed, d[1] * speed, d[2] * speed)
            pos = (origin[0] + d[0] * 3.0, origin[1] + d[1] * 3.0,
                   origin[2] + d[2] * 3.0)
            cmds.append(mc.summon_projectile(entity, pos, v, power=power,
                                             nbt=self.caps.nbt))
        self._submit(cmds, Priority.NORMAL, f"rocket:{unit.id}")
        self._fx(unit, "minecraft:entity.firework_rocket.launch", origin, 1.0, 1.0)
        self.world.add_marker(origin[0], origin[2], "rocket", ttl=15.0)
        if target is not None and target.unit_id is not None and self.combat:
            d = distance(unit.pos, target.pos)
            speed = max(10.0, speed_mps(mount.key or "s8"))
            self._schedule_blast(target.pos, 8.0, self._damage_for(spec),
                                 f"НАР {mount.key}", unit.id, target.unit_id,
                                 d / speed)
        return True

    def _fire_missile(self, unit, mount: WeaponMount, spec: Dict,
                      target: Optional[Target], now: float) -> bool:
        if target is None:
            # УР без цели не пускаем: расходовать боезапас впустую нельзя.
            log.debug("УР %s: нет цели, пуск отменён", mount.label)
            return False
        rng = float(spec.get("range", 500.0))
        if distance(unit.pos, target.pos) > rng:
            return False
        if not self.cfg.guided_missiles or len(self.missiles) >= self.cfg.max_tracked_missiles:
            return self._fire_rocket(unit, mount, spec, target, now)
        used = mount.consume(1)
        if used <= 0:
            return False
        self.ammo_spent += used
        origin, direction = self._aim(unit, target, speed_mps(mount.key or "agm"))
        speed = float(spec["speed"]) * self.cfg.muzzle_speed_scale
        # speed в каталоге — блоки/тик; для блочной ракеты переводим в м/с
        vel_mps = speed * 20.0
        vel = (direction[0] * speed, direction[1] * speed, direction[2] * speed)
        pos = (origin[0] + direction[0] * 3.0, origin[1] + direction[1] * 3.0,
               origin[2] + direction[2] * 3.0)
        if self.missile_manager is not None and \
                getattr(self.cfg, "use_block_missiles", True):
            self._launch_block_missile(unit, mount, spec, target, origin,
                                       direction, pos, vel_mps)
        else:
            self._launch_guided_missile(unit, mount, pos, vel, target)
        # Инвариант P2.3: боезапас списывается РОВНО ОДИН раз на пуск —
        # только через consume(1) выше. Ни один из путей пуска (_launch_*)
        # не имеет права трогать mount.ammo (регресс WPN-08).
        mount.mark_fired()
        self.missiles_launched += 1
        self._fx(unit, "minecraft:entity.firework_rocket.launch", origin, 1.2, 0.8)
        return True

    def _launch_block_missile(self, unit, mount: WeaponMount, spec: Dict,
                              target: Target, origin: Vec3, dirv: Vec3,
                              pos: Vec3, vel_mps: float) -> None:
        """Пуск через MissileManager (блочная ракета, основной путь v13+)."""
        from .missiles import MissileSpec
        mspec = MissileSpec(label=str(spec.get("label", "УР")),
                            speed=max(40.0, min(220.0, vel_mps)),
                            damage=self._damage_for(spec),
                            blast_radius=float(spec.get("crater_r", 3)) * 2.5 + 6.0)
        homing = (self.world.get_unit(target.unit_id)
                  if target.unit_id is not None else None)
        self.missile_manager.launch(pos, (dirv[0] * vel_mps,
                                         dirv[1] * vel_mps,
                                         dirv[2] * vel_mps),
                                    target, mspec, owner_id=unit.id,
                                    owner_label=unit.spec.label,
                                    weapon_key=mount.key or "agm",
                                    homing_unit=homing)

    def _launch_guided_missile(self, unit, mount: WeaponMount, pos: Vec3,
                               vel: Vec3, target: Target) -> None:
        """Fallback: легковесная GuidedMissile (менеджер недоступен /
        use_block_missiles=False). Команды спавна уходят CRITICAL-приоритетом."""
        msl = GuidedMissile(self._next_mid, mount.key or "agm", pos, vel,
                            target, self.cfg, owner_tag=unit.tag)
        self._next_mid += 1
        self.missiles.append(msl)
        self._submit(msl.spawn_commands(), Priority.CRITICAL, None)

    def _fire_gun(self, unit, mount: WeaponMount, spec: Dict,
                  target: Optional[Target], now: float) -> bool:
        # Если цель — юнит, планируем прямой урон по времени полёта снаряда
        if target is not None and target.unit_id is not None and self.combat:
            d = distance(unit.pos, target.pos)
            speed = max(10.0, speed_mps(mount.key or "pkm"))
            self._schedule_blast(target.pos, 6.0, self._damage_for(spec),
                                 f"{mount.category} {mount.key}", unit.id,
                                 target.unit_id, d / speed)
        burst = int(spec.get("burst", 3))
        used = mount.consume(burst)
        if used <= 0:
            return False
        self.ammo_spent += used
        origin, direction = self._aim(unit, target, speed_mps(mount.key or "pkm"))
        spread = float(spec.get("spread", 0.03))
        entity = str(spec.get("entity", "minecraft:arrow"))
        power = int(spec.get("power", 0))
        speed = float(spec["speed"]) * self.cfg.muzzle_speed_scale
        cmds: List[str] = []
        for i in range(used):
            d = self._spread(direction, i, used, spread)
            pos = (origin[0] + d[0] * 2.5, origin[1] + d[1] * 2.5,
                   origin[2] + d[2] * 2.5)
            cmds.append(mc.summon_projectile(entity, pos,
                                             (d[0] * speed, d[1] * speed, d[2] * speed),
                                             power=power or None,
                                             nbt=self.caps.nbt))
        self._submit(cmds, Priority.NORMAL, f"gun:{unit.id}:{mount.slot}")
        self._fx(unit, "minecraft:entity.blaze.shoot", origin, 0.7, 1.6)
        return True

    # -------------------------------------------------------------- прицел
    def _aim(self, unit, target: Optional[Target],
             proj_speed: float) -> Tuple[Vec3, Vec3]:
        """Точка вылета и направление. С упреждением, если цель задана."""
        origin = unit.muzzle_pos()
        if target is None:
            return origin, unit.forward()
        t = solve_lead(origin, target.pos, target.vel, max(1.0, proj_speed))
        point = target.predicted(min(t, 8.0))
        direction = unit_vector(origin, point)
        if direction is None:
            direction = unit.forward()
        return origin, direction

    @staticmethod
    def _spread(direction: Vec3, index: int, total: int, amount: float) -> Vec3:
        """Развести залп веером симметрично относительно направления."""
        if total <= 1 or amount <= 0:
            return direction
        dx, dy, dz = direction
        px, pz = -dz, dx                      # перпендикуляр в горизонтали
        norm = math.hypot(px, pz) or 1.0
        px, pz = px / norm, pz / norm
        off = (index - (total - 1) / 2.0) * amount
        out = (dx + px * off, dy + off * 0.25, dz + pz * off)
        n = math.sqrt(sum(c * c for c in out)) or 1.0
        return (out[0] / n, out[1] / n, out[2] / n)

    def _ground_below(self, unit) -> float:
        """Высота земли под юнитом: из скана рельефа, иначе уровень моря."""
        h = self.world.terrain.height_at(unit.pos[0], unit.pos[2])
        if h is not None:
            return float(h)
        return 63.0

    # --------------------------------------------------------------- тик
    def update(self, dt: float, resolver=None) -> None:
        """Обновить ракеты, отложенные эффекты и урон по технике."""
        self._process_blasts()
        # --- эффекты по расписанию (время симуляции) ---------------------
        if self.effects:
            for eff in self.effects:
                eff.delay -= dt
            due = [e for e in self.effects if e.delay <= 0.0]
            if due:
                self.effects = [e for e in self.effects if e.delay > 0.0]
                for eff in due:
                    self._submit(eff.commands, Priority.CRITICAL, None)
                    log.debug("Эффект %s: %d команд", eff.kind, len(eff.commands))

        # --- ракеты ------------------------------------------------------
        if not self.missiles:
            return
        survivors: List[GuidedMissile] = []
        for msl in self.missiles:
            tpos = self._missile_target_pos(msl, resolver)
            cmds, boom = msl.update(dt, tpos)
            if boom:
                self.missiles_hit += 1
                cmds += msl.detonate_commands(self.cfg)
                self.world.add_marker(msl.pos[0], msl.pos[2], "impact", ttl=30.0)
                self._fx_at(msl.pos, "minecraft:entity.generic.explode", 1.0, 0.9)
            else:
                survivors.append(msl)
            self._submit(cmds, Priority.NORMAL, f"msl:{msl.mid}")
        self.missiles = survivors

    def _missile_target_pos(self, msl: GuidedMissile, resolver) -> Vec3:
        """Актуальная позиция цели (цель движется — ракета доводится)."""
        if resolver is not None:
            pos = resolver(msl.target)
            if pos is not None:
                return pos
        if msl.target.name:
            rec = self.world.get_player(msl.target.name)
            if rec is not None:
                return rec.pos
        return msl.target.pos

    # ------------------------------------------------------------- служебное
    def _submit(self, cmds: List[str], priority: int, key: Optional[str]) -> None:
        """Отправить пачку команд в очередь.

        Ключ слияния имеет смысл только для одиночной команды: пачка залпа
        должна уйти целиком, иначе от неё останется один снаряд.
        """
        if not cmds:
            return
        if key and len(cmds) == 1:
            self.queue.submit(cmds[0], priority, key=key)
            return
        for c in cmds:
            self.queue.submit(c, priority)

    def _fx(self, unit, sound: str, pos: Vec3, volume: float = 1.0,
            pitch: float = 1.0) -> None:
        self._fx_at(pos, sound, volume, pitch)

    def _fx_at(self, pos: Vec3, sound: str, volume: float, pitch: float) -> None:
        self.queue.submit(mc.playsound(sound, pos, volume=volume, pitch=pitch),
                          Priority.VISUAL, key=f"snd:{int(pos[0])}:{int(pos[2])}")

    def stats(self) -> Dict[str, float]:
        return {
            "shots": self.shots, "ammo_spent": self.ammo_spent,
            "missiles_active": len(self.missiles),
            "missiles_launched": self.missiles_launched,
            "missiles_hit": self.missiles_hit,
            "pending_effects": len(self.effects),
            "blocked_destructive": self.blocked_destructive,
        }
