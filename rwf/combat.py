"""
Боевой урон: подбитие техники техникой и из игры.

Закрывает «возможность подбить технику из игры / другой техникой»:
* **Техника по технике**: любой взрыв (бомба, НАР, УР, блочная ракета).resolve
  урон всем юнитам в радиусе с фолл-оффом по дистанции и учётом брони.
  Уничтоженный юнит помечается, оставляет метку и попадает в кил-фид.
* **Из игры**: игрок выдаёт команду через scoreboard-триггер
  (`/trigger rwf_fire`) — поллер раз в интервал читает счёт, сбрасывает его
  и регистрирует выстрел игрока: урон ближайшей вражеской технике в конусе
  перед игроком. Это ванильный механизм, не требующий модов.

Урон централизован в `Unit.take_damage`, поэтому все источники одинаковы.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import mc
from .rcon import RCONPool
from .world import World

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]

#: триггер, через который игрок стреляет по технике из игры
TRIGGER_FIRE = "rwf_fire"
#: дальность и половина угла конуса выстрела игрока
PLAYER_GUN_RANGE = 220.0
PLAYER_GUN_CONE = 35.0          # градусов от направления взгляда
PLAYER_GUN_DAMAGE = 18.0


@dataclass
class DamageEvent:
    target_id: int
    target_label: str
    amount: float
    source_kind: str
    source_id: Optional[int]
    destroyed: bool
    at: float = field(default_factory=time.time)

    def describe(self) -> str:
        verb = "УНИЧТОЖЕН" if self.destroyed else "повреждён"
        src = f"#{self.source_id}" if self.source_id is not None else self.source_kind
        return (f"{self.target_label} #{self.target_id} {verb} "
                f"(-{self.amount:.0f}) источником {src}")


class CombatSystem:
    """Разрешение урона и лента боевых событий."""

    def __init__(self, world: World, feed_limit: int = 60,
                 on_event: Optional[Callable[[DamageEvent], None]] = None,
                 on_blast: Optional[Callable[..., None]] = None):
        self.world = world
        self.on_event = on_event
        #: callback(pos, radius, damage, source_kind) — чтобы взрыв мог
        #: задеть не только технику, но и базы/сооружения (BASE-HP)
        self.on_blast = on_blast
        self.feed: List[DamageEvent] = []
        self._limit = feed_limit
        self.total_damage = 0.0
        self.kills = 0

    # -------------------------------------------------------------- урон
    def hit(self, unit, amount: float, source_kind: str = "",
            source_id: Optional[int] = None) -> Optional[DamageEvent]:
        """Нанести урон одному юниту, записать событие."""
        if unit is None or not unit.alive:
            return None
        before = unit.alive
        destroyed = unit.take_damage(amount, source_kind, source_id, self.world)
        ev = DamageEvent(unit.id, unit.spec.label, amount, source_kind,
                         source_id, destroyed and before)
        self._push(ev)
        if ev.destroyed:
            self.kills += 1
        self.total_damage += amount
        return ev

    def resolve_blast(self, units: Sequence[Any], pos: Vec3, radius: float,
                      damage: float, source_kind: str = "",
                      source_id: Optional[int] = None,
                      skip: Optional[int] = None) -> List[DamageEvent]:
        """Взрыв в точке: урон всем юнитам в радиусе с фолл-оффом.

        `skip` — id инициатора, чтобы бомба не сбивала собственный носитель
        в момент сброса на малой высоте.
        """
        events: List[DamageEvent] = []
        if radius <= 0 or damage <= 0:
            return events
        if self.on_blast is not None:
            try:
                self.on_blast(pos, radius, damage, source_kind)
            except Exception:  # noqa: BLE001
                log.exception("Обработчик взрыва упал")
        for unit in units:
            if unit is None or not unit.alive or unit.id == skip:
                continue
            d = math.sqrt((unit.pos[0] - pos[0]) ** 2
                          + (unit.pos[2] - pos[2]) ** 2
                          + ((unit.pos[1] - pos[1]) * 0.6) ** 2)
            if d > radius:
                continue
            falloff = max(0.25, 1.0 - (d / radius) * 0.75)
            ev = self.hit(unit, damage * falloff, source_kind, source_id)
            if ev:
                events.append(ev)
        return events

    # -------------------------------------------------------------- лента
    def _push(self, ev: DamageEvent) -> None:
        self.feed.append(ev)
        if len(self.feed) > self._limit:
            del self.feed[: len(self.feed) - self._limit]
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001
                log.exception("Обработчик боевого события упал")
        log.info("БОЙ: %s", ev.describe())

    def recent(self, n: int = 10) -> List[DamageEvent]:
        return list(self.feed[-n:])

    def stats(self) -> Dict[str, Any]:
        return {"kills": self.kills, "total_damage": round(self.total_damage, 1),
                "feed": len(self.feed)}


# ---------------------------------------------------------------------------
#  Огонь из игры
# ---------------------------------------------------------------------------
class PlayerGunPoller:
    """Читает триггер `rwf_fire` у игроков и регистрирует их выстрелы.

    Игрок в игре пишет `/trigger rwf_fire` (или `add 1`). Поллер раз в
    `interval` секунд опрашивает счёт каждого игрока одной командой, при
    ненулевом значении сбрасывает его и наносит урон ближайшей вражеской
    технике в конусе перед взглядом игрока.
    """

    def __init__(self, world: World, combat: CombatSystem, pool: RCONPool,
                 interval: float = 0.5, enabled: bool = True,
                 is_enemy: Optional[Callable[[Any], bool]] = None):
        self.world = world
        self.combat = combat
        self.pool = pool
        self.interval = max(0.1, interval)
        self.enabled = enabled
        # кто считается целью для огня игроков; по умолчанию — боты
        self.is_enemy = is_enemy or (lambda u: bool(getattr(u, "is_bot", False)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.shots = 0
        self._setup_done = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="player-gun",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _ensure_objective(self) -> None:
        if self._setup_done:
            return
        try:
            self.pool.run(mc.trigger_objective(TRIGGER_FIRE))
            self._setup_done = True
        except Exception:  # noqa: BLE001
            log.warning("Не удалось создать триггер %s", TRIGGER_FIRE)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка поллера огня игроков")
            self._stop.wait(self.interval)

    def poll_once(self) -> List[DamageEvent]:
        """Один опрос всех игроков. Возвращает события урона."""
        if not self.enabled:
            return []
        self._ensure_objective()
        players = self.world.get_players()
        if not players:
            return []
        events: List[DamageEvent] = []
        for name, rec in players.items():
            try:
                resp = self.pool.run(mc.trigger_get(name, TRIGGER_FIRE))
            except Exception:  # noqa: BLE001
                continue
            value = mc.parse_score(resp)
            if not value:
                continue
            try:
                self.pool.run(mc.trigger_reset(name, TRIGGER_FIRE))
            except Exception:  # noqa: BLE001
                pass
            ev = self.fire_from_player(name, rec)
            if ev:
                events.append(ev)
        return events

    def fire_from_player(self, name: str, rec: Dict[str, Any]) -> Optional[DamageEvent]:
        """Выстрел игрока: урон ближайшей вражеской технике в конусе взгляда."""
        px, py, pz = rec["pos"]
        yaw = math.radians(rec.get("yaw", 0.0))
        pitch = math.radians(rec.get("pitch", 0.0))
        # направление взгляда в мировых координатах
        fx = -math.sin(yaw) * math.cos(pitch)
        fy = -math.sin(pitch)
        fz = math.cos(yaw) * math.cos(pitch)

        best, best_d = None, float("inf")
        for unit in self.world.iter_units():
            if not unit.alive or not self.is_enemy(unit):
                continue
            dx = unit.pos[0] - px
            dy = unit.pos[1] - py
            dz = unit.pos[2] - pz
            d = math.sqrt(dx * dx + dy * dy + dz * dz)
            if d > PLAYER_GUN_RANGE or d < 1e-3:
                continue
            cos = (dx * fx + dy * fy + dz * fz) / d
            angle = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
            if angle > PLAYER_GUN_CONE:
                continue
            if d < best_d:
                best, best_d = unit, d
        if best is None:
            return None
        self.shots += 1
        log.info("Игрок %s стрелял по #%s (%.0f м)", name, best.id, best_d)
        return self.combat.hit(best, PLAYER_GUN_DAMAGE, f"игрок {name}", None)
