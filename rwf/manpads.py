"""
ПЗРК: переносной зенитный ракетный комплекс игрока («Игла»/«Стингер»).

Закрывает запрос v13 «команда для захвата цели и запуска ручной ракеты»:
игрок из мира Minecraft наводит взгляд на воздушную цель и пишет

    /trigger rwf_lock       — захват ближайшей воздушной цели в конусе взгляда
    /trigger rwf_missile    — пуск ракеты по захваченной цели

Ракета — та же блочная механика (`MissileManager`/`MissileUnit`), что и УР
техники, но с профилем ПЗРК: быстрее, манёвреннее, боевая часть меньше.
Наведение — на ЖИВОЙ юнит (`homing_unit`): цель уворачивается — ракета
доворачивает за ней, а не летит в точку пуска.

Обратная связь игроку — actionbar: статус захвата, пуск, перезарядка.
Это ванильный механизм (scoreboard-триггеры), моды не требуются.

Потоки: как у `PlayerGunPoller` — собственный фоновый поток опрашивает
триггеры, команды уходят через `RCONPool`/`CommandQueue`, GUI не касается.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import mc
from .missiles import MissileManager, MissileSpec
from .rcon import CommandQueue, Priority
from .weapons import Target
from .world import World

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]

#: триггеры игрока
TRIGGER_LOCK = "rwf_lock"
TRIGGER_MISSILE = "rwf_missile"

#: тактико-технические данные ПЗРК
MANPADS_RANGE = 700.0        # м, дальность захвата
MANPADS_CONE = 25.0          # градусов, полуугол конуса захвата
MANPADS_LOCK_TTL = 8.0       # с, захват живёт без подтверждения
MANPADS_COOLDOWN = 6.0       # с между пусками одного игрока
MANPADS_KEEP_RANGE = MANPADS_RANGE * 1.3   # ракета ведёт цель и дальше захвата

#: воздушные типы, по которым работает ПЗРК
AIR_KINDS = ("aircraft", "helicopter", "drone")

MANPADS_SPEC = MissileSpec(
    label="ПЗРК «Игла»",
    speed=140.0,             # м/с — быстрее авиационных УР
    turn_rate=48.0,          # град/с
    ttl=9.0,                 # с полёта до самоликвидации
    damage=80.0,
    blast_radius=9.0,
    blueprint="missile_small",
    arming_distance=12.0,
)


@dataclass
class LockInfo:
    """Состояние захвата одного игрока."""
    player: str
    unit_id: int
    label: str
    at: float                # monotonic-время захвата
    distance: float

    def to_dict(self) -> Dict[str, Any]:
        return {"player": self.player, "unit_id": self.unit_id,
                "label": self.label, "distance": round(self.distance, 1),
                "age": round(time.monotonic() - self.at, 1)}


class ManpadsSystem:
    """Захват и пуск ПЗРК игроками. Владеет поллером триггеров."""

    def __init__(self, world: World, missiles: MissileManager,
                 pool=None, queue: Optional[CommandQueue] = None,
                 interval: float = 0.35, enabled: bool = True,
                 is_enemy: Optional[Callable[[Any], bool]] = None,
                 bus=None):
        self.world = world
        self.missiles = missiles
        self.pool = pool
        self.queue = queue
        self.bus = bus
        self.interval = max(0.1, interval)
        self.enabled = enabled
        # по умолчанию цель ПЗРК — боты (как у PlayerGunPoller)
        self.is_enemy = is_enemy or (lambda u: bool(getattr(u, "is_bot", False)))
        self.locks: Dict[str, LockInfo] = {}
        self.cooldowns: Dict[str, float] = {}     # имя -> monotonic-время пуска
        self.lock_count = 0
        self.launches = 0
        self.misses_no_lock = 0
        self.misses_cooldown = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._setup_done = False

    # ------------------------------------------------------------- поток
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="manpads",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка поллера ПЗРК")
            self._stop.wait(self.interval)

    def _ensure_objectives(self) -> None:
        if self._setup_done or self.pool is None:
            return
        try:
            self.pool.run(mc.trigger_objective(TRIGGER_LOCK))
            self.pool.run(mc.trigger_objective(TRIGGER_MISSILE))
            self._setup_done = True
        except Exception:  # noqa: BLE001
            log.warning("Не удалось создать триггеры ПЗРК")

    def _read_trigger(self, name: str, trigger: str) -> int:
        """Прочитать и сбросить триггер игрока. 0 — не нажимал."""
        if self.pool is None:
            return 0
        try:
            resp = self.pool.run(mc.trigger_get(name, trigger))
        except Exception:  # noqa: BLE001
            return 0
        value = mc.parse_score(resp) or 0
        if value:
            try:
                self.pool.run(mc.trigger_reset(name, trigger))
            except Exception:  # noqa: BLE001
                pass
        return value

    def _actionbar(self, name: str, text: str, color: str = "yellow") -> None:
        if self.queue is not None:
            self.queue.submit(mc.actionbar(name, text, color), Priority.NORMAL)
        elif self.pool is not None:
            try:
                self.pool.run(mc.actionbar(name, text, color))
            except Exception:  # noqa: BLE001
                pass

    def _fx(self, cmd: str) -> None:
        if self.queue is not None:
            self.queue.submit(cmd, Priority.NORMAL)
        elif self.pool is not None:
            try:
                self.pool.run(cmd)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ опрос
    def poll_once(self) -> List[Dict[str, Any]]:
        """Один опрос всех игроков. Возвращает события (lock/launch)."""
        if not self.enabled:
            return []
        self._ensure_objectives()
        players = self.world.get_players()
        events: List[Dict[str, Any]] = []
        for name, rec in players.items():
            if self._read_trigger(name, TRIGGER_LOCK):
                ev = self.try_lock(name, rec)
                if ev:
                    events.append(ev)
            if self._read_trigger(name, TRIGGER_MISSILE):
                ev = self.try_launch(name, rec)
                if ev:
                    events.append(ev)
        return events

    # ------------------------------------------------------------ захват
    def _look_dir(self, rec: Dict[str, Any]) -> Vec3:
        yaw = math.radians(rec.get("yaw", 0.0))
        pitch = math.radians(rec.get("pitch", 0.0))
        return (-math.sin(yaw) * math.cos(pitch),
                -math.sin(pitch),
                math.cos(yaw) * math.cos(pitch))

    def find_target(self, rec: Dict[str, Any]) -> Optional[Tuple[Any, float, float]]:
        """Лучшая воздушная цель в конусе взгляда: (unit, dist, angle)."""
        px, py, pz = rec["pos"]
        fx, fy, fz = self._look_dir(rec)
        best: Optional[Tuple[Any, float, float]] = None
        for unit in self.world.iter_units():
            if not getattr(unit, "alive", False):
                continue
            if getattr(unit, "spec", None) is None:
                continue
            if unit.spec.kind not in AIR_KINDS:
                continue
            if not self.is_enemy(unit):
                continue
            dx = unit.pos[0] - px
            dy = unit.pos[1] - py
            dz = unit.pos[2] - pz
            d = math.sqrt(dx * dx + dy * dy + dz * dz)
            if d > MANPADS_RANGE or d < 1e-3:
                continue
            cos = (dx * fx + dy * fy + dz * fz) / d
            angle = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
            if angle > MANPADS_CONE:
                continue
            # приоритет — минимальный угол к оси взгляда, затем дистанция
            score = (round(angle, 1), round(d, 1))
            if best is None or score < (round(best[2], 1), round(best[1], 1)):
                best = (unit, d, angle)
        return best

    def try_lock(self, name: str, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        found = self.find_target(rec)
        if found is None:
            self.locks.pop(name, None)
            self._actionbar(name, "✖ ПЗРК: нет цели в конусе захвата", "red")
            return None
        unit, d, _angle = found
        self.locks[name] = LockInfo(player=name, unit_id=unit.id,
                                    label=unit.spec.label,
                                    at=time.monotonic(), distance=d)
        self.lock_count += 1
        self._actionbar(name, f"◎ ЗАХВАТ: {unit.spec.label} #{unit.id} · {d:.0f} м",
                        "green")
        self._fx(mc.playsound("minecraft:block.note_block.pling", rec["pos"],
                              target=name, volume=0.8, pitch=1.6))
        if self.bus:
            self.bus.publish("manpads.lock", name, unit.id, d)
            self.bus.publish("log", f"ПЗРК: {name} захватил #{unit.id} "
                                    f"({unit.spec.label}, {d:.0f} м)", "green")
        log.info("ПЗРК: %s захватил #%s (%.0f м)", name, unit.id, d)
        return {"kind": "lock", "player": name, "unit_id": unit.id,
                "distance": d}

    def get_lock(self, name: str) -> Optional[LockInfo]:
        """Действительный захват игрока (живая цель, TTL, дальность)."""
        lock = self.locks.get(name)
        if lock is None:
            return None
        if time.monotonic() - lock.at > MANPADS_LOCK_TTL:
            self.locks.pop(name, None)
            return None
        unit = self.world.get_unit(lock.unit_id)
        if unit is None or not unit.alive:
            self.locks.pop(name, None)
            return None
        prec = self.world.get_player(name)
        if prec is not None:
            pos = prec["pos"] if isinstance(prec, dict) else prec.pos
            d = math.dist(tuple(pos), tuple(unit.pos))
            if d > MANPADS_KEEP_RANGE:
                self.locks.pop(name, None)
                return None
            lock.distance = d
        return lock

    # -------------------------------------------------------------- пуск
    def try_launch(self, name: str, rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        cd_left = self.cooldowns.get(name, 0.0) + MANPADS_COOLDOWN - now
        if cd_left > 0:
            self.misses_cooldown += 1
            self._actionbar(name, f"⟳ ПЗРК: перезарядка {cd_left:.0f} с", "yellow")
            return None
        lock = self.get_lock(name)
        if lock is None:
            self.misses_no_lock += 1
            self._actionbar(name, "✖ ПЗРК: нет захвата — /trigger rwf_lock", "red")
            return None
        unit = self.world.get_unit(lock.unit_id)
        if unit is None:
            self.locks.pop(name, None)
            self._actionbar(name, "✖ ПЗРК: цель потеряна", "red")
            return None

        fx, fy, fz = self._look_dir(rec)
        px, py, pz = rec["pos"]
        origin = (px + fx * 1.5, py + 0.6 + fy * 1.5, pz + fz * 1.5)
        target = Target(pos=tuple(unit.pos),
                        vel=tuple(unit.velocity()) if hasattr(unit, "velocity")
                        else (0.0, 0.0, 0.0),
                        name=unit.spec.label, unit_id=unit.id)
        speed = MANPADS_SPEC.speed
        missile = self.missiles.launch(
            origin, (fx * speed, fy * speed, fz * speed), target,
            MANPADS_SPEC, owner_id=None, owner_label=f"игрок {name}",
            weapon_key="manpads", homing_unit=unit)
        self.cooldowns[name] = now
        self.launches += 1
        self.locks.pop(name, None)
        self._actionbar(name, f"→ ПУСК по {unit.spec.label} #{unit.id}!", "aqua")
        self._fx(mc.playsound("minecraft:entity.firework_rocket.launch",
                              origin, target=name, volume=2.0, pitch=0.7))
        self._fx(mc.particle("minecraft:large_smoke", origin,
                             (1.0, 1.0, 1.0), 0.1, 40))
        if self.bus:
            self.bus.publish("manpads.launch", name, unit.id, missile.id)
            self.bus.publish("log",
                             f"ПЗРК: {name} → пуск по #{unit.id} "
                             f"({unit.spec.label})", "aqua")
        log.info("ПЗРК: %s пустил ракету #%s по #%s", name, missile.id, unit.id)
        return {"kind": "launch", "player": name, "unit_id": unit.id,
                "missile_id": missile.id}

    # ------------------------------------------------------------ снимок
    def snapshot(self) -> Dict[str, Any]:
        """Состояние ПЗРК по игрокам — для UI."""
        now = time.monotonic()
        out: Dict[str, Any] = {}
        for name in set(self.locks) | set(self.cooldowns):
            lock = self.locks.get(name)
            cd = self.cooldowns.get(name, 0.0) + MANPADS_COOLDOWN - now
            out[name] = {
                "lock": ({"unit_id": lock.unit_id, "label": lock.label,
                          "distance": round(lock.distance, 1)}
                         if lock else None),
                "cooldown": round(max(0.0, cd), 1),
            }
        return out

    def stats(self) -> Dict[str, Any]:
        return {"locks": self.lock_count, "launches": self.launches,
                "no_lock": self.misses_no_lock,
                "cooldown_blocked": self.misses_cooldown,
                "active_locks": len(self.locks)}
