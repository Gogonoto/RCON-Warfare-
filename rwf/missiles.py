"""
Блочные ракеты: УР/ПТУР как мини-юнит с собственной блочной моделью.

Закрывает «добавь в подвесы ракеты из блоков»: запущенная ракета — это не
одиночный fireball, а маленький летающий объект из нескольких блоков,
построенный той же механикой, что и техника (`BlockModel` + дифф-обновление).
В полёте она видна на карте силуэтом ракеты и в мире — цепочкой блоков,
доворачивает к цели с ограниченной угловой скоростью и при сближении
детонирует, нанося урон через `CombatSystem.resolve_blast` (то есть может
подбить другую технику).

Жизненный цикл: пуск -> наведение -> попадание/самоликвидация -> уборка блоков.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import mc
from .combat import CombatSystem
from .geometry import yaw_to
from .model import BlockModel, get_blueprint
from .rcon import CommandQueue, Priority
from .weapons import Target, distance

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]


@dataclass
class MissileSpec:
    label: str = "УР"
    speed: float = 90.0            # м/с
    turn_rate: float = 55.0        # град/с
    ttl: float = 14.0              # с до самоликвидации
    damage: float = 90.0
    blast_radius: float = 12.0
    blueprint: str = "missile_small"
    arming_distance: float = 15.0  # не взводиться ближе к носителю


class MissileUnit:
    """Одна летящая блочная ракета."""

    kind = "missile"

    def __init__(self, mid: int, pos: Vec3, velocity: Vec3, target: Target,
                 spec: Optional[MissileSpec] = None, owner_id: Optional[int] = None,
                 owner_label: str = "", weapon_key: str = "",
                 homing_unit: Optional[Any] = None):
        self.id = mid
        self.spec = spec or MissileSpec()
        self.pos = pos
        v = velocity
        norm = math.sqrt(sum(c * c for c in v)) or 1.0
        self.dir = (v[0] / norm, v[1] / norm, v[2] / norm)
        self.target = target
        # Живая цель (ПЗРК/УР по юниту): каждый тик обновляем Target по нему,
        # иначе ракета летела бы в точку, где цель была в момент пуска.
        self.homing_unit = homing_unit
        self.owner_id = owner_id
        self.owner_label = owner_label
        self.weapon_key = weapon_key
        self.age = 0.0
        self.alive = True
        self.distance_flown = 0.0
        self.model = BlockModel(get_blueprint(self.spec.blueprint))
        self._last_sync: Optional[Tuple[float, ...]] = None
        self.tag = f"rwf_msl_{mid}"

    def _refresh_target(self) -> None:
        u = self.homing_unit
        if u is None or not getattr(u, "alive", False):
            return
        self.target = Target(pos=tuple(u.pos),
                             vel=tuple(u.velocity()) if hasattr(u, "velocity")
                             else (0.0, 0.0, 0.0),
                             name=getattr(getattr(u, "spec", None), "label", ""),
                             unit_id=getattr(u, "id", None))

    # ------------------------------------------------------------- движение
    @property
    def yaw(self) -> float:
        dx, _dy, dz = self.dir
        return yaw_to(dx, dz)   # P1.1: единая формула в geometry.py

    @property
    def pitch(self) -> float:
        dx, dy, dz = self.dir
        horiz = math.hypot(dx, dz) or 1e-6
        return math.degrees(math.atan2(-dy, horiz))

    def _steer(self, dt: float) -> None:
        """Довернуть `dir` к цели с ограничением по угловой скорости."""
        tx, ty, tz = self.target.pos
        want = (tx - self.pos[0], ty - self.pos[1], tz - self.pos[2])
        wn = math.sqrt(sum(c * c for c in want)) or 1e-6
        want = (want[0] / wn, want[1] / wn, want[2] / wn)
        dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(self.dir, want))))
        angle = math.acos(dot)
        max_turn = math.radians(self.spec.turn_rate) * dt
        if angle <= 1e-6:
            return
        t = 1.0 if angle <= max_turn else max_turn / angle
        new = tuple(a + (w - a) * t for a, w in zip(self.dir, want))
        n = math.sqrt(sum(c * c for c in new)) or 1.0
        self.dir = (new[0] / n, new[1] / n, new[2] / n)

    def step(self, dt: float) -> str:
        """Один тик. Возвращает 'fly' | 'hit' | 'expired'."""
        if not self.alive:
            return "expired"
        self.age += dt
        self._refresh_target()
        self._steer(dt)
        step = self.spec.speed * dt
        self.pos = (self.pos[0] + self.dir[0] * step,
                    self.pos[1] + self.dir[1] * step,
                    self.pos[2] + self.dir[2] * step)
        self.distance_flown += step
        # Не взводиться сразу после пуска (не сбить собственный носитель)
        if self.distance_flown < self.spec.arming_distance:
            return "fly"
        if distance(self.pos, self.target.pos) <= max(4.0, self.spec.speed * dt * 1.2):
            self.alive = False
            return "hit"
        if self.age >= self.spec.ttl:
            self.alive = False
            return "expired"
        return "fly"

    # ---------------------------------------------------------------- модель
    def model_commands(self, queue: CommandQueue) -> int:
        key = (round(self.pos[0], 1), round(self.pos[1], 1), round(self.pos[2], 1),
               round(self.yaw, 2))
        if key == self._last_sync:
            return 0
        self._last_sync = key
        writes, clears = self.model.sync(self.pos, self.yaw, self.pitch, 0.0)
        n = 0
        for (x, y, z) in clears:
            queue.submit(mc.setblock(x, y, z, "minecraft:air"), Priority.VISUAL,
                         key=f"msl{self.id}:{x},{y},{z}")
            n += 1
        for (x, y, z), block in writes:
            queue.submit(mc.setblock(x, y, z, block), Priority.VISUAL,
                         key=f"msl{self.id}:{x},{y},{z}")
            n += 1
        return n

    def despawn_commands(self, queue: CommandQueue) -> int:
        n = 0
        for (x, y, z) in self.model.clear():
            queue.submit(mc.setblock(x, y, z, "minecraft:air"), Priority.CRITICAL,
                         key=f"msl{self.id}:{x},{y},{z}")
            n += 1
        return n

    # -------------------------------------------------------------- снимок
    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": 10000 + self.id,        # не пересекаться с id юнитов
            "missile_id": self.id,
            "kind": "missile", "label": self.spec.label,
            "is_bot": True, "alive": self.alive,
            "pos": tuple(self.pos), "yaw": self.yaw, "pitch": self.pitch,
            "roll": 0.0, "speed": self.spec.speed,
            "status": "fly", "health_pct": 100.0,
            "target": self.target.name or "",
            "owner": self.owner_label,
        }


class MissileManager:
    """Владеет активными блочными ракетами: тик, модель, детонация, уборка."""

    def __init__(self, queue: CommandQueue, combat: CombatSystem,
                 get_units=None, enabled: bool = True):
        self.queue = queue
        self.combat = combat
        self.get_units = get_units or (lambda: [])
        self.enabled = enabled
        self.missiles: List[MissileUnit] = []
        self._next_id = 1
        self.launched = 0
        self.hits = 0
        self.expired = 0

    def launch(self, pos: Vec3, velocity: Vec3, target: Target,
               spec: Optional[MissileSpec] = None, owner_id: Optional[int] = None,
               owner_label: str = "", weapon_key: str = "",
               homing_unit: Optional[Any] = None) -> MissileUnit:
        m = MissileUnit(self._next_id, pos, velocity, target, spec,
                        owner_id, owner_label, weapon_key, homing_unit)
        self._next_id += 1
        self.missiles.append(m)
        self.launched += 1
        m.model_commands(self.queue)      # сразу показать ракету в мире
        log.info("Пуск блочной ракеты #%d (%s) по %s", m.id,
                 owner_label or "-", target.name or "точке")
        return m

    def step(self, dt: float) -> List[Dict[str, Any]]:
        """Тик всех ракет. Возвращает события попаданий для лога/эффектов."""
        if not self.missiles:
            return []
        events: List[Dict[str, Any]] = []
        units = self.get_units()
        survivors: List[MissileUnit] = []
        for m in self.missiles:
            result = m.step(dt)
            if result == "fly":
                m.model_commands(self.queue)
                survivors.append(m)
                continue
            # попадание или самоликвидация
            m.despawn_commands(self.queue)
            if result == "hit":
                self.hits += 1
                blast = self.combat.resolve_blast(
                    units, m.pos, m.spec.blast_radius, m.spec.damage,
                    source_kind=f"ракета {m.weapon_key or ''}".strip(),
                    source_id=m.owner_id, skip=m.owner_id)
                self.queue.submit(mc.instant_explosion(m.pos), Priority.CRITICAL)
                events.append({"kind": "hit", "missile": m.id,
                               "pos": m.pos, "destroyed": [e.target_id for e in blast]})
            else:
                self.expired += 1
                self.queue.submit(mc.instant_explosion(m.pos), Priority.NORMAL)
                events.append({"kind": "expired", "missile": m.id, "pos": m.pos})
        self.missiles = survivors
        return events

    def snapshot_entries(self) -> List[Dict[str, Any]]:
        return [m.snapshot() for m in self.missiles]

    def clear_all(self) -> None:
        for m in list(self.missiles):
            m.despawn_commands(self.queue)
        self.missiles.clear()

    def stats(self) -> Dict[str, Any]:
        return {"active": len(self.missiles), "launched": self.launched,
                "hits": self.hits, "expired": self.expired}
