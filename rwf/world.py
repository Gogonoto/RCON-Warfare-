"""
Состояние мира — единый источник правды для карты, UI и ИИ.

Что исправлено относительно наброска
------------------------------------
* **Снапшот маршрутов копируется.** Раньше `snapshot()` клал в результат живые
  объекты `Route`, и поток UI перебирал `waypoints` в тот момент, когда поток
  юнита их менял (`RuntimeError: list changed size during iteration`).
* **`set_route(uid, None)` больше не падает.** В наброске следом шло
  `route.owner_kind = ...` → `AttributeError` на `None`. Это срабатывало при
  нажатии «Hold» в HotBar.
* **Прогресс сканирования атомарный.** `world.scan_progress[0] + 1` из шести
  потоков терял инкременты; теперь счётчик под локом.
* **Игроки удаляются.** Трекер в наброске только добавлял — в списке навсегда
  оставались те, кто вышел.
* **Рельеф пишется пачками.** Один захват лока на 100 тайлов вместо 100 захватов.
* **Ревизия мира.** Каждое изменение увеличивает `revision`; UI может не
  перерисовывать карту, если ничего не поменялось (главная причина тормозов).
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .events import (
    EventBus, TOPIC_PLAYER_ADDED, TOPIC_PLAYER_REMOVED, TOPIC_ROUTE_SET,
    TOPIC_SCAN_FINISHED, TOPIC_SCAN_PROGRESS, TOPIC_UNIT_ADDED,
    TOPIC_UNIT_REMOVED, TOPIC_WORLD_CHANGED,
)

Pos3 = Tuple[float, float, float]


# ---------------------------------------------------------------------------
#  Рельеф
# ---------------------------------------------------------------------------
class TerrainGrid:
    """Тайлы рельефа: (x, z) -> (y, kind). Хранит границы и шаг сетки."""

    __slots__ = ("_tiles", "_lock", "step", "min_x", "min_z", "max_x", "max_z",
                 "min_y", "max_y")

    def __init__(self, step: int = 8):
        self._tiles: Dict[Tuple[int, int], Tuple[int, str]] = {}
        self._lock = threading.Lock()
        self.step = step
        self.min_x = self.min_z = self.max_x = self.max_z = 0
        self.min_y, self.max_y = 0, 0

    def __len__(self) -> int:
        return len(self._tiles)

    def clear(self) -> None:
        with self._lock:
            self._tiles.clear()

    def set_tile(self, x: int, z: int, y: int, kind: str) -> None:
        self.set_tiles([(x, z, y, kind)])

    def set_tiles(self, tiles: Iterable[Tuple[int, int, int, str]]) -> None:
        """Пакетная запись — в разы быстрее при сканировании."""
        batch = list(tiles)
        if not batch:
            return
        with self._lock:
            t = self._tiles
            for x, z, y, kind in batch:
                t[(x, z)] = (y, kind)
            if not self.step or self.step <= 0:
                self.step = 8
            xs = [b[0] for b in batch]
            zs = [b[1] for b in batch]
            ys = [b[2] for b in batch]
            if len(t) == len(batch):          # первая пачка — задаём границы
                self.min_x, self.max_x = min(xs), max(xs)
                self.min_z, self.max_z = min(zs), max(zs)
                self.min_y, self.max_y = min(ys), max(ys)
            else:
                self.min_x = min(self.min_x, min(xs)); self.max_x = max(self.max_x, max(xs))
                self.min_z = min(self.min_z, min(zs)); self.max_z = max(self.max_z, max(zs))
                self.min_y = min(self.min_y, min(ys)); self.max_y = max(self.max_y, max(ys))

    def get(self, x: int, z: int) -> Optional[Tuple[int, str]]:
        return self._tiles.get((x, z))

    def height_at(self, x: float, z: float) -> Optional[int]:
        """Высота ближайшего тайла — для привязки наземной техники к земле."""
        if not self._tiles:
            return None
        s = self.step or 8
        bx = int(round(x / s)) * s
        bz = int(round(z / s)) * s
        best = None
        for dx in (0, s, -s):
            for dz in (0, s, -s):
                hit = self._tiles.get((bx + dx, bz + dz))
                if hit is not None:
                    if best is None or abs(dx) + abs(dz) < best[0]:
                        best = (abs(dx) + abs(dz), hit[0])
        return best[1] if best else None

    def count(self) -> int:
        return len(self._tiles)

    def bounds(self) -> Tuple[int, int, int, int]:
        return (self.min_x, self.min_z, self.max_x, self.max_z)

    def y_range(self) -> Tuple[int, int]:
        return (self.min_y, self.max_y)

    def descriptor(self) -> Tuple[int, int, Tuple[int, int, int, int], Tuple[int, int]]:
        """Дешёвая подпись состояния — для ключа кэша растра.

        Копировать `tiles` (сотни тысяч записей) каждый кадр нельзя: карта
        обновляется 30 раз в секунду. Растр читает тайлы напрямую через
        `get()`, а инвалидируется по этой подписи.
        """
        with self._lock:
            return (len(self._tiles), self.step, self.bounds(), self.y_range())

    def items(self) -> List[Tuple[Tuple[int, int], Tuple[int, str]]]:
        with self._lock:
            return list(self._tiles.items())

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "tiles": dict(self._tiles),
                "step": self.step,
                "bounds": (self.min_x, self.min_z, self.max_x, self.max_z),
                "y_range": (self.min_y, self.max_y),
            }


# ---------------------------------------------------------------------------
#  Игрок
# ---------------------------------------------------------------------------
@dataclass
class PlayerRecord:
    name: str
    pos: Pos3 = (0.0, 64.0, 0.0)
    yaw: float = 0.0
    pitch: float = 0.0
    health: float = 20.0
    updated: float = field(default_factory=time.time)
    vel: Pos3 = (0.0, 0.0, 0.0)      # м/с, оценка по двум последним замерам

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "pos": self.pos, "yaw": self.yaw,
            "pitch": self.pitch, "health": self.health, "ts": self.updated,
            "vel": self.vel,
        }


# ---------------------------------------------------------------------------
#  Маркеры
# ---------------------------------------------------------------------------
@dataclass
class Marker:
    x: float
    z: float
    kind: str = "bomb"        # bomb | nuke | drone | impact | custom
    created: float = field(default_factory=time.time)
    ttl: float = 60.0
    text: str = ""

    def alive(self, now: Optional[float] = None) -> bool:
        if self.ttl <= 0:
            return True
        return (now or time.time()) - self.created < self.ttl

    def as_dict(self) -> Dict[str, Any]:
        return {"x": self.x, "z": self.z, "kind": self.kind,
                "created": self.created, "ttl": self.ttl, "text": self.text}


# ---------------------------------------------------------------------------
#  Мир
# ---------------------------------------------------------------------------
class World:
    """Потокобезопасное состояние мира.

    Держит: игроков, юниты, маршруты, маркеры, зоны, рельеф.
    Публикует события в `EventBus` — UI подписывается и не лезет в ядро.
    """

    def __init__(self, bus: Optional[EventBus] = None, marker_limit: int = 500):
        self._lock = threading.RLock()
        self.bus = bus or EventBus()
        self.marker_limit = marker_limit

        self.players: Dict[str, PlayerRecord] = {}
        self.units: Dict[int, Any] = {}          # uid -> Unit
        self.routes: Dict[int, Any] = {}         # uid -> Route
        self.markers: List[Marker] = []

        self.waypoint: Optional[Tuple[float, float]] = None
        self.strike_zone: Optional[Tuple[float, float, float, float]] = None
        self.launch_point: Optional[Tuple[float, float]] = None
        self.base: Optional[Tuple[float, float]] = None    # точка возврата (RTB)

        self.terrain = TerrainGrid()
        self.scan_progress: Tuple[int, int] = (0, 1)
        self.scanning = False

        self.revision = 0            # растёт при любом изменении
        self._next_unit_id = 1

    # ---------------------------------------------------------------- игроки
    def set_player(self, name: str, pos: Pos3, yaw: float = 0.0,
                   pitch: float = 0.0, health: float = 20.0,
                   vel: Optional[Pos3] = None) -> None:
        with self._lock:
            rec = self.players.get(name)
            now = time.time()
            if rec is None:
                rec = PlayerRecord(name=name)
                self.players[name] = rec
                new = True
            else:
                new = False
                # Оценка скорости по двум замерам — нужна для упреждения
                # и перехвата. Фильтр по dt защищает от деления на ноль и
                # от всплесков при редком опросе.
                dt = now - rec.updated
                if dt > 0.02:
                    vx = (pos[0] - rec.pos[0]) / dt
                    vy = (pos[1] - rec.pos[1]) / dt
                    vz = (pos[2] - rec.pos[2]) / dt
                    speed = math.sqrt(vx * vx + vy * vy + vz * vz)
                    if speed < 60.0:      # отсечение выбросов (телепорты)
                        rec.vel = (vx, vy, vz)
            rec.pos = tuple(pos)      # type: ignore[assignment]
            rec.yaw, rec.pitch, rec.health = yaw, pitch, health
            if vel is not None:
                # Явная скорость от внешнего источника имеет приоритет
                rec.vel = tuple(vel)  # type: ignore[assignment]
            rec.updated = now
            self._touch()
        if new:
            self.bus.publish(TOPIC_PLAYER_ADDED, name, rec.pos)

    def remove_player(self, name: str) -> None:
        with self._lock:
            if self.players.pop(name, None) is not None:
                self._touch()
                removed = True
            else:
                removed = False
        if removed:
            self.bus.publish(TOPIC_PLAYER_REMOVED, name)

    def sync_players(self, names: Iterable[str]) -> None:
        """Привести список игроков к фактическому (удаляет вышедших)."""
        names = set(names)
        with self._lock:
            gone = [n for n in self.players if n not in names]
        for n in gone:
            self.remove_player(n)

    def get_player(self, name: str) -> Optional[PlayerRecord]:
        with self._lock:
            rec = self.players.get(name)
            return PlayerRecord(**rec.__dict__) if rec else None

    def get_players(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {n: p.as_dict() for n, p in self.players.items()}

    def stale_players(self, max_age: float = 10.0) -> List[str]:
        now = time.time()
        with self._lock:
            return [n for n, p in self.players.items() if now - p.updated > max_age]

    # ---------------------------------------------------------------- юниты
    def add_unit(self, unit: Any) -> int:
        with self._lock:
            uid = self._next_unit_id
            self._next_unit_id += 1
            unit.id = uid
            self.units[uid] = unit
            self._touch()
        self.bus.publish(TOPIC_UNIT_ADDED, uid)
        return uid

    def remove_unit(self, uid: int) -> None:
        with self._lock:
            existed = self.units.pop(uid, None) is not None
            self.routes.pop(uid, None)
            if existed:
                self._touch()
        if existed:
            self.bus.publish(TOPIC_UNIT_REMOVED, uid)

    def get_unit(self, uid: int) -> Optional[Any]:
        with self._lock:
            return self.units.get(uid)

    def iter_units(self) -> List[Any]:
        with self._lock:
            return list(self.units.values())

    def find_units(self, kind: Optional[str] = None,
                   is_bot: Optional[bool] = None) -> List[Any]:
        with self._lock:
            out = list(self.units.values())
        if kind is not None:
            out = [u for u in out if getattr(u, "kind", None) == kind]
        if is_bot is not None:
            out = [u for u in out if bool(getattr(u, "is_bot", False)) == is_bot]
        return out

    def unit_ids(self) -> List[int]:
        with self._lock:
            return list(self.units.keys())

    # -------------------------------------------------------------- маршруты
    def set_route(self, uid: int, route: Optional[Any]) -> None:
        """Назначить маршрут юниту. `None` — очистить (без падения)."""
        with self._lock:
            if route is None:
                self.routes.pop(uid, None)
            else:
                self.routes[uid] = route
                unit = self.units.get(uid)
                if unit is not None:
                    route.owner_kind = "bot" if getattr(unit, "is_bot", False) else "player"
                    route.unit_id = uid
                self._touch()
        self.bus.publish(TOPIC_ROUTE_SET, uid)

    def get_route(self, uid: int) -> Optional[Any]:
        with self._lock:
            return self.routes.get(uid)

    def clear_routes(self) -> None:
        with self._lock:
            self.routes.clear()
            self._touch()

    # --------------------------------------------------------------- маркеры
    def add_marker(self, x: float, z: float, kind: str = "bomb",
                   ttl: float = 60.0, text: str = "") -> None:
        with self._lock:
            self.markers.append(Marker(x, z, kind, ttl=ttl, text=text))
            if len(self.markers) > self.marker_limit:
                del self.markers[: len(self.markers) - self.marker_limit]
            self._touch()

    def clear_markers(self, kind: Optional[str] = None) -> None:
        with self._lock:
            if kind is None:
                self.markers.clear()
            else:
                self.markers = [m for m in self.markers if m.kind != kind]
            self._touch()

    def prune_markers(self) -> int:
        with self._lock:
            before = len(self.markers)
            self.markers = [m for m in self.markers if m.alive()]
            removed = before - len(self.markers)
            if removed:
                self._touch()
            return removed

    # ----------------------------------------------------------------- зоны
    def set_waypoint(self, x: float, z: float) -> None:
        with self._lock:
            self.waypoint = (x, z)
            self._touch()

    def clear_waypoint(self) -> None:
        with self._lock:
            if self.waypoint is not None:
                self.waypoint = None
                self._touch()

    def set_strike_zone(self, x1: float, z1: float, x2: float, z2: float) -> None:
        with self._lock:
            self.strike_zone = (min(x1, x2), min(z1, z2), max(x1, x2), max(z1, z2))
            self._touch()

    def clear_strike_zone(self) -> None:
        with self._lock:
            if self.strike_zone is not None:
                self.strike_zone = None
                self._touch()

    def in_strike_zone(self, x: float, z: float, margin: float = 0.0) -> bool:
        with self._lock:
            if not self.strike_zone:
                return False
            x1, z1, x2, z2 = self.strike_zone
            return (x1 - margin) <= x <= (x2 + margin) and (z1 - margin) <= z <= (z2 + margin)

    def set_launch_point(self, x: float, z: float) -> None:
        with self._lock:
            self.launch_point = (x, z)
            self._touch()

    def clear_launch_point(self) -> None:
        with self._lock:
            if self.launch_point is not None:
                self.launch_point = None
                self._touch()

    def set_base(self, x: float, z: float) -> None:
        with self._lock:
            self.base = (x, z)
            self._touch()

    def clear_base(self) -> None:
        with self._lock:
            self.base = None
            self._touch()

    # --------------------------------------------------------------- сканер
    def begin_scan(self, total: int, step: int = 8) -> None:
        with self._lock:
            self.terrain.clear()
            self.terrain.step = step
            self.scan_progress = (0, max(1, total))
            self.scanning = True
            self._touch()

    def scan_add(self, count: int = 1) -> Tuple[int, int]:
        with self._lock:
            done, total = self.scan_progress
            done = min(total, done + count)
            self.scan_progress = (done, total)
            return done, total

    def set_scan_progress(self, done: int, total: int) -> None:
        """Установить прогресс напрямую (сканер знает абсолютные числа)."""
        with self._lock:
            total = max(1, int(total))
            self.scan_progress = (max(0, min(total, int(done))), total)

    def end_scan(self) -> int:
        with self._lock:
            self.scanning = False
            n = len(self.terrain)
            self._touch()
        self.bus.publish(TOPIC_SCAN_FINISHED, n)
        return n

    def publish_scan_progress(self, done: int, total: int) -> None:
        self.bus.publish(TOPIC_SCAN_PROGRESS, done, total)

    # -------------------------------------------------------------- снимок
    def _touch(self) -> None:
        self.revision += 1

    def snapshot(self, include_terrain: bool = False) -> Dict[str, Any]:
        """Снимок мира для UI. Все изменяемые структуры копируются.

        `include_terrain=False` по умолчанию: рельеф большой и меняется редко,
        карта берёт его отдельным вызовом `terrain_snapshot()`.
        """
        with self._lock:
            now = time.time()
            units = {}
            for uid, u in self.units.items():
                try:
                    units[uid] = u.snapshot()
                except Exception:  # noqa: BLE001 - юнит не должен ронять UI
                    units[uid] = {"id": uid, "error": True}
            routes = {}
            for uid, r in self.routes.items():
                try:
                    routes[uid] = r.snapshot()
                except Exception:  # noqa: BLE001
                    routes[uid] = {"waypoints": [], "error": True}
            snap = {
                "revision": self.revision,
                "players": {n: p.as_dict() for n, p in self.players.items()},
                "units": units,
                "routes": routes,
                "markers": [m.as_dict() for m in self.markers if m.alive(now)],
                "waypoint": self.waypoint,
                "strike_zone": self.strike_zone,
                "launch_point": self.launch_point,
                "base": self.base,
                "scanning": self.scanning,
                "scan_progress": self.scan_progress,
                "terrain_tiles": len(self.terrain),
            }
            if include_terrain:
                snap["terrain"] = self.terrain.snapshot()
        return snap

    def terrain_snapshot(self) -> Dict[str, Any]:
        return self.terrain.snapshot()

    def reset(self) -> None:
        with self._lock:
            self.units.clear()
            self.routes.clear()
            self.markers.clear()
            self.waypoint = None
            self.strike_zone = None
            self.launch_point = None
            self.terrain.clear()
            self.scanning = False
            self.scan_progress = (0, 1)
            self._touch()
        self.bus.publish(TOPIC_WORLD_CHANGED, self.revision)
