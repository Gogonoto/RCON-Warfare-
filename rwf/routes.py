"""
Маршруты: точки, действия, исполнитель.

Главный дефект наброска и как он закрыт
---------------------------------------
**ROUTE-01/02.** Точка считалась достигнутой при `dist_h <= 3`. Самолёт на
60 м/с при тике 0.5 с пролетает 30 блоков за тик, то есть физически не может
попасть в окно 3 блока — маршрут не выполнялся ВООБЩЕ.

Здесь достижение определяется по-разному для двух режимов (выбор заказчика —
настраивается у каждой точки):

* `fly` — пролёт: точка пройдена, когда юнит пересёк перпендикулярную
  плоскость через неё (проекция вектора «точка − позиция» на направление
  движения стала отрицательной). Работает на любой скорости.
* `precise` — точный выход: расстояние до точки меньше радиуса (и по высоте
  тоже). Естественно для вертолёта, БПЛА и танка.

Плюс предохранитель: если точка не достигнута за `waypoint_timeout` секунд,
маршрут переходит дальше с пометкой `skipped` — зависнуть навсегда нельзя.

Остальные закрытые дефекты:
* **ROUTE-03** — таймер действия увеличивается во всех ветках, действие без
  подходящего подвеса помечается пропущенным, а не зависает;
* **ROUTE-04** — сброс бомб выполняется ДО точки, на расчётной дистанции
  (V·t_падения), и учитывает `count`;
* **ROUTE-05** — обстрел ограничен длительностью и боезапасом;
* **ROUTE-06** — камикадзе реально пикирует, взрывается на цели, пробивает
  шахту и уничтожает юнит;
* **ROUTE-07** — никакого `rcon.run()` в тике: всё через `WeaponSystem`;
* **ROUTE-08** — «удержание» для самолёта значит вираж, а не остановку в воздухе;
* **ROUTE-09** — `action_done` ставится только если действие состоялось;
* **ROUTE-10** — навигация задаёт ЦЕЛИ (курс/тангаж) один раз за тик.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import mc
# П1.1: углы/векторы — только из geometry.py (единственный источник правды).
from .geometry import angle_diff, forward_vec, right_vec, yaw_to  # noqa: F401
from .config import AppConfig
from .events import EventBus
from .weapons import PendingEffect, Target, WeaponSystem
from .world import World

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]
GRAVITY = 15.0
SEA_LEVEL = 63.0


# ---------------------------------------------------------------------------
#  Действия
# ---------------------------------------------------------------------------
class Action:
    NAVIGATE = "navigate"
    BOMB = "bomb"
    STRAFE = "strafe"
    MISSILE = "missile"
    KAMIKAZE = "kamikaze"
    DROP = "drop"
    RTB = "rtb"
    HOLD = "hold"
    RECON = "recon"

    #: какие категории подвесов использует действие
    CATEGORIES: Dict[str, Tuple[str, ...]] = {
        BOMB: ("bomb",),
        STRAFE: ("cannon", "mg"),
        MISSILE: ("missile",),
        KAMIKAZE: ("bomb",),
    }

    ALL: Tuple[str, ...] = (NAVIGATE, BOMB, STRAFE, MISSILE, KAMIKAZE,
                            DROP, RTB, HOLD, RECON)


ACTION_LABELS: Dict[str, str] = {
    Action.NAVIGATE: "Перелёт",
    Action.BOMB: "Сброс бомб",
    Action.STRAFE: "Обстрел",
    Action.MISSILE: "Ракетный удар",
    Action.KAMIKAZE: "Камикадзе",
    Action.DROP: "Сброс груза",
    Action.RTB: "Возврат на базу",
    Action.HOLD: "Удержание",
    Action.RECON: "Разведка",
}

#: цвета линии маршрута на карте — по действию участка
ACTION_COLORS: Dict[str, str] = {
    Action.NAVIGATE: "#d8e6d8",
    Action.BOMB: "#ff8844",
    Action.STRAFE: "#ff3344",
    Action.MISSILE: "#cc44ff",
    Action.KAMIKAZE: "#ff00aa",
    Action.DROP: "#ffcc44",
    Action.RTB: "#44ff88",
    Action.HOLD: "#8888ff",
    Action.RECON: "#ffdd44",
}

BOT_ROUTE_COLOR = "#22e6ff"        # cyan — требование к маршруту бота


# ---------------------------------------------------------------------------
#  Точка маршрута
# ---------------------------------------------------------------------------
@dataclass
class Waypoint:
    x: float
    z: float
    altitude: float = 150.0
    action: str = Action.NAVIGATE
    # --- параметры действия ---
    count: int = 1                 # сколько подвесов/снарядов использовать
    duration: float = 3.0          # для STRAFE/HOLD/RECON, секунд
    target_name: str = ""          # цель-игрок, если задана (иначе — точка)
    #: Высота ЦЕЛИ для бомб/ракет. `None` — взять рельеф под точкой (или
    #: уровень моря). Это НЕ `altitude`: `altitude` задаёт высоту полёта
    #: юнита, а бомбы падают вниз, поэтому цель обязана быть ниже.
    target_altitude: Optional[float] = None
    # --- способ прохождения ---
    pass_mode: str = "auto"        # 'auto' | 'fly' | 'precise'
    radius: float = 0.0            # 0 = подобрать по типу юнита
    speed: Optional[float] = None  # ограничение скорости на участке
    note: str = ""
    # --- состояние исполнения ---
    reached: bool = False
    action_done: bool = False
    skipped: bool = False

    PASS_MODES = ("auto", "fly", "precise")

    def __post_init__(self):
        if self.pass_mode not in self.PASS_MODES:
            raise ValueError(
                f"pass_mode должен быть одним из {self.PASS_MODES}, "
                f"получено {self.pass_mode!r}")
        if self.action not in Action.ALL:
            raise ValueError(f"Неизвестное действие {self.action!r}. "
                             f"Доступны: {', '.join(Action.ALL)}")

    def resolved_mode(self, unit) -> str:
        """'auto' -> 'fly' для самолёта, 'precise' для вертолётных и наземных."""
        if self.pass_mode != "auto":
            return self.pass_mode
        spec = unit.spec
        if spec.ground_unit:
            return "precise"
        return "precise" if spec.can_hover else "fly"

    def resolved_radius(self, unit) -> float:
        if self.radius > 0:
            return self.radius
        spec = unit.spec
        if spec.ground_unit:
            return 4.0
        if spec.can_hover:
            return 6.0
        # Самолёт: радиус не меньше пути за один тик, иначе «пролёт» не поймать
        return max(spec.waypoint_radius, spec.max_speed * 0.5)

    def pos(self) -> Tuple[float, float]:
        return (self.x, self.z)

    def pos3(self) -> Vec3:
        return (self.x, self.altitude, self.z)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Waypoint":
        known = {f for f in cls.__dataclass_fields__}     # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def copy(self) -> "Waypoint":
        return Waypoint(**{k: v for k, v in asdict(self).items()})


# ---------------------------------------------------------------------------
#  Маршрут
# ---------------------------------------------------------------------------
class Route:
    """Список точек + состояние исполнения.

    Потокобезопасен: `snapshot()` снимает копию под своей блокировкой, потому
    что UI читает маршрут, пока исполнитель его меняет (контракт WORLD-02).
    """

    def __init__(self, waypoints: Optional[Sequence[Waypoint]] = None,
                 unit_id: Optional[int] = None, loop: bool = False,
                 owner_kind: str = "player", name: str = ""):
        self._lock = threading.RLock()
        self.waypoints: List[Waypoint] = list(waypoints or [])
        self.unit_id = unit_id
        self.current_idx = 0
        self.loop = loop
        self.owner_kind = owner_kind          # 'player' | 'bot'
        self.name = name
        self.done = False
        self.created = time.time()
        self.cycles = 0
        self._progress: List[str] = []        # журнал исполнения для отладки

    # ------------------------------------------------------------ навигация
    def current(self) -> Optional[Waypoint]:
        with self._lock:
            if 0 <= self.current_idx < len(self.waypoints):
                return self.waypoints[self.current_idx]
            return None

    def previous(self) -> Optional[Waypoint]:
        with self._lock:
            i = self.current_idx - 1
            return self.waypoints[i] if 0 <= i < len(self.waypoints) else None

    def next(self) -> Optional[Waypoint]:
        with self._lock:
            i = self.current_idx + 1
            return self.waypoints[i] if i < len(self.waypoints) else None

    def __len__(self) -> int:
        with self._lock:
            return len(self.waypoints)

    def add(self, wp: Waypoint) -> "Route":
        with self._lock:
            self.waypoints.append(wp)
            return self

    def insert(self, index: int, wp: Waypoint) -> None:
        with self._lock:
            self.waypoints.insert(max(0, min(index, len(self.waypoints))), wp)
            if index <= self.current_idx:
                self.current_idx += 1

    def remove(self, index: int) -> Optional[Waypoint]:
        with self._lock:
            if not (0 <= index < len(self.waypoints)):
                return None
            wp = self.waypoints.pop(index)
            if index < self.current_idx:
                self.current_idx -= 1
            elif index == self.current_idx:
                self.current_idx = min(self.current_idx, max(0, len(self.waypoints) - 1))
            if not self.waypoints:
                self.done = True
            return wp

    def move(self, src: int, dst: int) -> bool:
        with self._lock:
            if not (0 <= src < len(self.waypoints)) or not (0 <= dst < len(self.waypoints)):
                return False
            wp = self.waypoints.pop(src)
            self.waypoints.insert(dst, wp)
            return True

    def replace_waypoints(self, waypoints: Sequence[Waypoint],
                          keep_progress: bool = True) -> None:
        """Заменить точки, сохранив текущий индекс и флаги пройденных.

        Нужно ИИ: при перепланировании на середине захода нельзя сбрасывать
        прогресс в ноль — иначе бот вечно летит к первой точке (AI-01).
        """
        with self._lock:
            old = {i: (wp.reached, wp.action_done)
                   for i, wp in enumerate(self.waypoints)}
            self.waypoints = list(waypoints)
            if keep_progress:
                self.current_idx = min(self.current_idx,
                                       max(0, len(self.waypoints) - 1))
                for i, (reached, done) in old.items():
                    if i < self.current_idx and i < len(self.waypoints):
                        self.waypoints[i].reached = reached
                        self.waypoints[i].action_done = done
            else:
                self.current_idx = 0
            self.done = not self.waypoints

    def advance(self) -> bool:
        """Перейти к следующей точке. False — маршрут завершён."""
        with self._lock:
            self.current_idx += 1
            if self.current_idx >= len(self.waypoints):
                if self.loop and self.waypoints:
                    self.current_idx = 0
                    self.cycles += 1
                    self.reset_flags()
                    return True
                self.current_idx = len(self.waypoints)
                self.done = True
                return False
            return True

    def reset_flags(self) -> None:
        with self._lock:
            for wp in self.waypoints:
                wp.reached = False
                wp.action_done = False
                wp.skipped = False

    def restart(self) -> None:
        with self._lock:
            self.current_idx = 0
            self.done = False
            self.reset_flags()

    def note(self, message: str) -> None:
        with self._lock:
            self._progress.append(message)
            if len(self._progress) > 200:
                del self._progress[:100]

    @property
    def progress_log(self) -> List[str]:
        with self._lock:
            return list(self._progress)

    # -------------------------------------------------------------- снимки
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "unit_id": self.unit_id,
                "current_idx": self.current_idx,
                "loop": self.loop,
                "owner_kind": self.owner_kind,
                "name": self.name,
                "done": self.done,
                "cycles": self.cycles,
                "created": self.created,
                "waypoints": [wp.to_dict() for wp in self.waypoints],
            }

    def copy(self) -> "Route":
        with self._lock:
            return Route([wp.copy() for wp in self.waypoints], self.unit_id,
                         self.loop, self.owner_kind, self.name)

    # ----------------------------------------------------------- импорт/экспорт
    def to_dict(self) -> Dict[str, Any]:
        """Формат сохранения в JSON (без состояния исполнения)."""
        with self._lock:
            return {
                "format": "rwf.route",
                "version": 1,
                "name": self.name,
                "loop": self.loop,
                "owner_kind": self.owner_kind,
                "waypoints": [
                    {k: v for k, v in wp.to_dict().items()
                     if k not in ("reached", "action_done", "skipped")}
                    for wp in self.waypoints
                ],
            }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Route":
        fmt = data.get("format")
        if fmt != "rwf.route":
            raise ValueError(f"Это не файл маршрута RCON Warfare "
                             f"(format={fmt!r})")
        version = int(data.get("version", 1))
        if version > 1:
            raise ValueError(f"Формат маршрута v{version} не поддерживается "
                             f"(ожидается v1)")
        wps = [Waypoint.from_dict(w) for w in data.get("waypoints", [])]
        return cls(wps, None, bool(data.get("loop", False)),
                   data.get("owner_kind", "player"), data.get("name", ""))

    def __repr__(self) -> str:  # pragma: no cover
        with self._lock:
            cur = self.current_idx
            total = len(self.waypoints)
        return (f"<Route {self.name or 'без имени'}: {cur + 1}/{total} "
                f"{'цикл' if self.loop else ''} {self.owner_kind}>")


# ---------------------------------------------------------------------------
#  Исполнитель маршрута
# ---------------------------------------------------------------------------
class RouteExecutor:
    """Ведёт юнит по маршруту и выполняет действия в точках.

    Порядок работы за тик
    ---------------------
    1. `_fly_to()` — перевести рули на точку (курс, тангаж/шаг, тяга).
    2. `_pre_action()` — действия, которые надо выполнить **ДО** пролёта точки:
       сброс бомб на расчётной дистанции, пуск УР на дальности, начало
       обстрела при входе в зону огня. Без этого шага бомбы всегда падали бы
       позади цели (ROUTE-04).
    3. Проверка достижения (`fly` — пролёт плоскости точки, `precise` — радиус).
    4. `_run_action()` — поведение после достижения: обстрел продолжается на
       пролёте, удержание и разведка крутят вираж, остальные завершаются сразу.

    Не блокируется на сети: огонь идёт через `WeaponSystem`, команды — через
    очередь (ROUTE-07). Один экземпляр на юнит, живёт в потоке движка.
    """

    #: окно обстрела: насколько заранее начинать стрелять до точки
    STRAFE_ENGAGE = 160.0
    #: доля дальности УР, на которой уже можно пускать
    MISSILE_LAUNCH_FRACTION = 0.85

    def __init__(self, unit, world: World, weapons: WeaponSystem,
                 cfg: Optional[AppConfig] = None, bus: Optional[EventBus] = None,
                 waypoint_timeout: float = 90.0):
        self.unit = unit
        self.world = world
        self.weapons = weapons
        self.cfg = cfg or AppConfig()
        self.bus = bus if bus is not None else world.bus
        self.waypoint_timeout = waypoint_timeout

        self.state = "navigate"          # navigate | action | finished | idle
        self.action_timer = 0.0
        self.wp_timer = 0.0
        self.prev_pos: Optional[Tuple[float, float]] = None
        self.last_action_report = ""
        self.strafe_started = False

    # ------------------------------------------------------------------ тик
    def tick(self, route: Optional[Route], dt: float) -> str:
        if route is None:
            self.state = "idle"
            return self.state
        if route.done or not self.unit.alive:
            if self.state != "finished":
                self.state = "finished"
                self._release_controls()
            return self.state

        wp = route.current()
        if wp is None:
            route.done = True
            self.state = "finished"
            return self.state

        if wp.action == Action.KAMIKAZE:
            return self._kamikaze(route, wp, dt)

        if self.state == "action":
            self._run_action(route, wp, dt)
        else:
            self._navigate(route, wp, dt)
        return self.state

    # ------------------------------------------------------------- полёт
    def _fly_to(self, wp: Waypoint) -> None:
        """Перевести органы управления на точку. Без проверки достижения."""
        unit, spec = self.unit, self.unit.spec
        tx, tz = wp.x, wp.z
        px, py, pz = unit.pos
        dx, dz = tx - px, tz - pz
        dist_h = math.hypot(dx, dz)
        alt_err = wp.altitude - py
        heading = yaw_to(dx, dz)   # P1.1: единая формула в geometry.py

        throttle = None
        if wp.speed is not None:
            throttle = max(0.0, min(1.0, wp.speed / max(1.0, spec.max_speed)))

        if spec.ground_unit:
            unit.set_target(heading=heading,
                            throttle=1.0 if dist_h > 2.0 else 0.0)
            return

        if spec.can_hover:
            # Вертолётным: тангаж — ПОСТУПАТЕЛЬНОЕ движение, шаг — высота.
            # В наброске тангаж использовался для высоты, поэтому вертолёт
            # вместо набора высоты летел вперёд и не мог точно выйти в точку.
            pitch = 0.0 if dist_h < 3.0 else max(0.0, min(spec.max_pitch * 0.8,
                                                          dist_h * 0.6))
            climb = max(-1.0, min(1.0, alt_err / 12.0))
            hover = spec.hover_throttle
            thr = hover + climb * (1.0 - hover) * 0.95
            unit.set_target(heading=heading, pitch=pitch, roll=0.0,
                            throttle=throttle if throttle is not None
                            else max(0.0, min(1.0, thr)))
            return

        # Самолёт: курс автопилотом, тангаж — на набор/снижение к высоте точки
        limit = min(spec.max_pitch * 0.6, 25.0)
        want_pitch = 0.0
        if dist_h > 1.0:
            want_pitch = -math.degrees(math.atan2(alt_err, dist_h))  # тангаж к высоте точки — не yaw, остаётся локально
        else:
            want_pitch = -limit if alt_err > 0 else limit
        want_pitch = max(-limit, min(limit, want_pitch))
        unit.set_target(heading=heading, pitch=want_pitch, throttle=throttle)

    def _navigate(self, route: Route, wp: Waypoint, dt: float) -> None:
        unit = self.unit
        self.wp_timer += dt

        if self.wp_timer > self.waypoint_timeout:
            wp.skipped = True
            route.note(f"Точка {route.current_idx + 1} пропущена по таймауту "
                       f"({self.wp_timer:.0f} с)")
            log.warning("Юнит #%s: точка %d пропущена по таймауту", unit.id,
                        route.current_idx + 1)
            self._on_arrived(route, wp)
            self.prev_pos = (unit.pos[0], unit.pos[2])
            return

        self._fly_to(wp)

        px, _py, pz = unit.pos
        dist_h = math.hypot(wp.x - px, wp.z - pz)
        self._pre_action(route, wp, dist_h)

        mode = wp.resolved_mode(unit)
        radius = wp.resolved_radius(unit)
        alt_err = wp.altitude - unit.pos[1]
        if self._arrived(wp, mode, radius, dist_h, alt_err):
            self._on_arrived(route, wp)
        self.prev_pos = (px, pz)

    def _arrived(self, wp: Waypoint, mode: str, radius: float,
                 dist_h: float, alt_err: float) -> bool:
        """Проверка достижения точки (ROUTE-01/02).

        `fly` — пересечение плоскости точки: проекция вектора «юнит → точка»
        на направление движения стала неположительной. Работает на любой
        скорости, в отличие от окна фиксированного радиуса.
        """
        if mode == "precise":
            # Допуск по высоте отдельный и более мягкий: вертолёт снижается
            # медленно, и требовать от него точности в 6 м нельзя.
            return dist_h <= radius and abs(alt_err) <= max(radius * 2.0, 25.0)
        if dist_h <= radius:
            return True
        if self.prev_pos is None:
            return False
        mx = self.unit.pos[0] - self.prev_pos[0]
        mz = self.unit.pos[2] - self.prev_pos[1]
        seg = math.hypot(mx, mz)
        if seg < 1e-6:
            return False
        to_x = wp.x - self.unit.pos[0]
        to_z = wp.z - self.unit.pos[2]
        return (to_x * mx + to_z * mz) / seg <= 0.0

    def _on_arrived(self, route: Route, wp: Waypoint) -> None:
        wp.reached = True
        self.wp_timer = 0.0
        self.action_timer = 0.0
        self.state = "action"
        if wp.action == Action.NAVIGATE:
            self._finish_action(route, wp, ok=True, report="перелёт")
        elif wp.action == Action.RTB:
            self._do_rtb(route, wp)
        elif wp.action == Action.DROP:
            self._do_drop(route, wp)
        elif wp.action in (Action.BOMB, Action.MISSILE) and wp.action_done:
            self._finish_action(route, wp, ok=True, report="выполнено на подходе")
        elif wp.action in (Action.BOMB, Action.MISSILE, Action.STRAFE):
            self.last_action_report = ""        # дорабатываем в _run_action
        else:
            self.last_action_report = ""

    # --------------------------------------------- действия ДО пролёта точки
    def _pre_action(self, route: Route, wp: Waypoint, dist_h: float) -> None:
        """Бомбы и ракеты выпускаются на подходе, а не после пролёта цели."""
        if wp.action_done or wp.action not in (Action.BOMB, Action.MISSILE,
                                               Action.STRAFE):
            return
        unit = self.unit
        target = self._target_for(wp)

        if wp.action == Action.BOMB:
            release = self._bomb_release_distance(target)
            if dist_h <= max(release, 8.0):
                self._release_bombs(route, wp, target)

        elif wp.action == Action.MISSILE:
            rng = self._missile_range(unit)
            if rng <= 0:
                wp.skipped = True
                self._finish_action(route, wp, ok=False, report="нет УР")
                return
            if dist_h <= rng * self.MISSILE_LAUNCH_FRACTION:
                self._launch_missiles(route, wp, target)

        elif wp.action == Action.STRAFE:
            engage = min(self.STRAFE_ENGAGE, unit.speed * max(1.0, wp.duration))
            if dist_h <= max(engage, 20.0):
                self.strafe_started = True

    def _bomb_release_distance(self, target: Target) -> float:
        """Дистанция сброса: V · t_падения (бомбам нужно время долететь)."""
        unit = self.unit
        height = max(1.0, unit.pos[1] - target.pos[1])
        g = max(1.0, self.cfg.combat.gravity_effective)
        t_fall = math.sqrt(2.0 * height / g)
        return unit.speed * t_fall

    def _missile_range(self, unit) -> float:
        from .ai import WEAPON_RANGE
        ranges = [WEAPON_RANGE.get(m.key, 0.0)
                  for m in unit.mounts if m.category == "missile" and m.ammo > 0]
        return max(ranges) if ranges else 0.0

    def _release_bombs(self, route: Route, wp: Waypoint, target: Target) -> None:
        unit = self.unit
        mounts = [m for m in unit.mounts if m.category == "bomb" and m.ready()]
        if not mounts:
            if not any(m.category == "bomb" and m.ammo > 0 for m in unit.mounts):
                wp.skipped = True
                self._finish_action(route, wp, ok=False, report="нет бомб")
            return                                    # подвесы на паузе — ждём тик
        fired = 0
        for mount in mounts[:max(1, wp.count)]:
            if self.weapons.fire(unit, mount, target):
                fired += 1
        if fired:
            wp.action_done = True
            self.last_action_report = f"сброшено подвесов: {fired}"
            route.note(f"{route.current_idx + 1}: сброс бомб — {fired} подвес(ов)")
            self.state = "action"
            self.action_timer = 0.0

    def _launch_missiles(self, route: Route, wp: Waypoint, target: Target) -> None:
        unit = self.unit
        mounts = [m for m in unit.mounts if m.category == "missile" and m.ready()]
        if not mounts:
            if not any(m.category == "missile" and m.ammo > 0 for m in unit.mounts):
                wp.skipped = True
                self._finish_action(route, wp, ok=False, report="нет УР")
            return
        fired = 0
        for mount in mounts[:max(1, wp.count)]:
            if self.weapons.fire(unit, mount, target):
                fired += 1
        if fired:
            wp.action_done = True
            self.last_action_report = f"пущено УР: {fired}"
            route.note(f"{route.current_idx + 1}: пуск УР — {fired}")
            self.state = "action"
            self.action_timer = 0.0

    # ------------------------------------------------------- после достижения
    def _run_action(self, route: Route, wp: Waypoint, dt: float) -> None:
        """Диспетчер действий на точке (P2.2).

        Таймер растёт в ЛЮБОЙ ветке — действие не может зависнуть (ROUTE-03);
        общий предохранитель — в конце, он действует и на ветки без своего
        лимита. Новые действия добавляются в `_ACTION_HANDLERS`, а не через
        elif здесь.
        """
        self.action_timer += dt
        unit = self.unit
        if not unit.alive:
            return
        handler = self._ACTION_HANDLERS.get(wp.action, self._act_none)
        # функции в _ACTION_HANDLERS — простые (не bound-методы), self передаём явно
        handler(self, route, wp, dt)
        self._guard_action_timeout(route, wp)

    def _guard_action_timeout(self, route: Route, wp: Waypoint) -> None:
        """Общий предохранитель: никакое действие не длится вечно."""
        act = wp.action
        if self.state == "action" and \
                self.action_timer > max(wp.duration, 1.0) * 4.0 + 12.0:
            route.note(f"Действие {ACTION_LABELS.get(act, act)} прервано по таймауту")
            self._finish_action(route, wp, ok=wp.action_done, report="таймаут")

    # --- обработчики (бывшие ветки elif; логика перенесена дословно) -------
    def _act_bomb(self, route: Route, wp: Waypoint, dt: float) -> None:
        if wp.action_done:
            self._finish_action(route, wp, ok=True,
                                report=self.last_action_report or "сброс выполнен")
        else:
            # Не сбросили на подходе (не успели/нет цели) — пробуем сейчас
            target = self._target_for(wp)
            self._release_bombs(route, wp, target)
            if self.action_timer > 3.0 and not wp.action_done:
                wp.skipped = True
                self._finish_action(route, wp, ok=False, report="сброс не удался")

    def _act_missile(self, route: Route, wp: Waypoint, dt: float) -> None:
        if wp.action_done:
            self._finish_action(route, wp, ok=True,
                                report=self.last_action_report or "пуск выполнен")
        else:
            target = self._target_for(wp)
            self._launch_missiles(route, wp, target)
            if self.action_timer > 4.0 and not wp.action_done:
                wp.skipped = True
                self._finish_action(route, wp, ok=False, report="пуск не удался")

    def _act_strafe(self, route: Route, wp: Waypoint, dt: float) -> None:
        # Обстрел идёт на пролёте: продолжаем лететь к точке и стрелять
        unit = self.unit
        self._fly_to(wp)
        target = self._target_for(wp)
        cats = Action.CATEGORIES[Action.STRAFE]
        if self.action_timer <= wp.duration and self.unit.alive:
            shots = 0
            for mount in unit.mounts:
                if mount.category in cats and shots < max(1, wp.count):
                    if self.weapons.fire(unit, mount, target):
                        shots += 1
                        wp.action_done = True
            if shots == 0 and not any(m.category in cats and m.ammo > 0
                                      for m in unit.mounts):
                wp.skipped = True
                self._finish_action(route, wp, ok=False, report="нет боезапаса")
                return
        if self.action_timer >= wp.duration:
            report = "обстрел завершён" if wp.action_done else "обстрел не удался"
            self._finish_action(route, wp, ok=wp.action_done, report=report)

    def _act_hold(self, route: Route, wp: Waypoint, dt: float) -> None:
        self._do_hold(route, wp, dt)

    def _act_recon(self, route: Route, wp: Waypoint, dt: float) -> None:
        self._do_recon(route, wp, dt)

    def _act_none(self, route: Route, wp: Waypoint, dt: float) -> None:
        self._finish_action(route, wp, ok=True, report="нет действия")

    #: таблица диспетчера P2.2: действие → обработчик
    _ACTION_HANDLERS = {
        Action.BOMB: _act_bomb,
        Action.MISSILE: _act_missile,
        Action.STRAFE: _act_strafe,
        Action.HOLD: _act_hold,
        Action.RECON: _act_recon,
    }

    def _target_for(self, wp: Waypoint) -> Target:
        """Цель действия: названный игрок (живая позиция), иначе сама точка.

        `World.get_player()` возвращает `PlayerRecord`, а `get_players()` —
        словари-снимки. Формы разные намеренно: запись нужна логике, снимок —
        интерфейсу. Перепутать их легко, поэтому доступ только через атрибуты.
        """
        if wp.target_name:
            rec = self.world.get_player(wp.target_name)
            if rec is not None:
                return Target(pos=rec.pos, vel=rec.vel, name=wp.target_name)
        # Без названной цели прицеливаемся в ЗЕМЛЮ под точкой: `altitude` —
        # это высота полёта юнита, и если целиться в неё, дистанция сброса
        # получится нулевой и бомбы упадут позади цели.
        alt = wp.target_altitude
        if alt is None:
            h = self.world.terrain.height_at(wp.x, wp.z)
            alt = float(h) if h is not None else SEA_LEVEL
        return Target(pos=(wp.x, alt, wp.z), name="")

    def _do_hold(self, route: Route, wp: Waypoint, dt: float) -> None:
        """Удержание. Самолёт уходит в вираж: остановка в воздухе невозможна
        (ROUTE-08), вертолётные висят, наземные стоят."""
        unit, spec = self.unit, self.unit.spec
        if spec.can_hover:
            unit.set_target(pitch=0.0, roll=0.0, yaw_rate=0.0,
                            throttle=spec.hover_throttle)
        elif spec.ground_unit:
            unit.set_target(throttle=0.0, yaw_rate=0.0)
        else:
            unit.set_target(heading=(unit.yaw + 30.0) % 360.0, pitch=0.0)
        wp.action_done = True
        if self.action_timer >= wp.duration:
            self._finish_action(route, wp, ok=True,
                                report=f"удержание {self.action_timer:.0f} с")

    def _do_recon(self, route: Route, wp: Waypoint, dt: float) -> None:
        """Разведка: вираж над точкой и метки по площади."""
        self._do_hold(route, wp, dt)
        if int(self.action_timer * 4) % 8 == 0 and self.action_timer > 0.2:
            self.world.add_marker(wp.x, wp.z, "recon", ttl=180.0,
                                  text=f"разведка #{self.unit.id}")
        if self.action_timer >= wp.duration and self.state == "action":
            self._finish_action(route, wp, ok=True, report="разведка выполнена")

    def _do_drop(self, route: Route, wp: Waypoint) -> None:
        """Десантирование груза в точке (TRANSPORT-02)."""
        unit = self.unit
        if unit.cargo <= 0.0:
            wp.skipped = True
            self._finish_action(route, wp, ok=False, report="нет груза")
            return
        dropped = unit.cargo
        unit.cargo = 0.0
        self.world.add_marker(wp.x, wp.z, "drop", ttl=120.0,
                              text=f"груз {dropped:.0f}т #{unit.id}")
        log.info("Юнит #%s: сброшено %s т груза в (%s, %s)",
                 unit.id, dropped, wp.x, wp.z)
        self._finish_action(route, wp, ok=True,
                            report=f"сброшено {dropped:.0f} т")

    def _do_rtb(self, route: Route, wp: Waypoint) -> None:
        """Возврат на базу с дозаправкой и перезарядкой (ROUTE-15)."""
        base = self.world.base or self.world.launch_point
        if base is None:
            route.note("RTB: база не задана, возврат отменён")
            self._finish_action(route, wp, ok=False, report="база не задана")
            return
        self._service_unit()
        self._finish_action(route, wp, ok=True, report="обслужен на базе")

    def _service_unit(self) -> None:
        unit = self.unit
        unit.fuel = unit.spec.fuel_max
        unit.cargo = unit.spec.cargo_max
        for m in unit.mounts:
            m.reload()
        unit.status = "serviced"
        self.world.add_marker(unit.pos[0], unit.pos[2], "service", ttl=45.0,
                              text=f"обслужен #{unit.id}")
        log.info("Юнит #%s обслужен: топливо и боезапас восполнены", unit.id)

    # ------------------------------------------------------------- камикадзе
    def _kamikaze(self, route: Route, wp: Waypoint, dt: float) -> str:
        """Таран: пикирование на цель, взрыв, шахта в пещеру, гибель юнита."""
        unit, spec = self.unit, self.unit.spec
        target = self._target_for(wp)
        tx, ty, tz = target.pos
        px, py, pz = unit.pos
        dist_h = math.hypot(tx - px, tz - pz)
        dist_v = py - ty
        heading = yaw_to(tx - px, tz - pz)   # P1.1: единая формула в geometry.py

        if spec.ground_unit:
            unit.set_target(heading=heading, throttle=1.0)
        elif spec.can_hover:
            # Вертолётным/БПЛА: тангаж — вперёд на цель, шаг — снижение
            pitch = 0.0 if dist_h < 3.0 else max(0.0, min(spec.max_pitch,
                                                          dist_h * 0.8))
            climb = max(-1.0, min(1.0, -dist_v / 15.0))   # dist_v>0 -> снижаемся
            hover = spec.hover_throttle
            unit.set_target(heading=heading, pitch=pitch, roll=0.0,
                            throttle=max(0.0, min(1.0, hover + climb * hover)))
        else:
            pitch = max(-spec.max_pitch, min(spec.max_pitch,
                                             math.degrees(math.atan2(dist_v,
                                                                     max(1.0, dist_h)))))
            unit.set_target(heading=heading, pitch=pitch, throttle=1.0)

        # Подрыв: либо вплотную к цели, либо при касании поверхности.
        # Поверхность берётся с тем же запасом, что и в физике юнита, иначе
        # step() успеет записать «столкновение с землёй» раньше подрыва.
        hit_dist = max(3.0, unit.speed * dt * 1.5)
        if dist_h <= hit_dist and abs(dist_v) <= max(8.0, hit_dist):
            self._detonate_on_target(target)
            return self.state
        ground = self.world.terrain.height_at(px, pz)
        ground_y = (float(ground) if ground is not None else SEA_LEVEL) + 1.0
        if py <= ground_y + max(2.0, unit.speed * dt):
            # Цель под землёй — взрываемся на поверхности, до неё докопает шахта
            self._detonate_on_target(target)
        return self.state

    def _detonate_on_target(self, target: Target) -> None:
        unit = self.unit
        pos = unit.pos
        cfg = self.cfg.combat
        self.weapons.effects.append(PendingEffect(
            delay=0.0, commands=[mc.instant_explosion(pos)],
            kind="kamikaze", label=f"камикадзе #{unit.id}"))
        bomb = next((m for m in unit.mounts
                     if m.category == "bomb" and m.ammo > 0), None)
        if bomb is not None:
            self.weapons.fire(unit, bomb, target)
        tunnel = (self.weapons._tunnel_to(pos, target.pos, cfg.tunnel_radius or 2)
                  if cfg.tunnel_enabled and cfg.destructive_confirmed else [])
        self.weapons.effects.append(PendingEffect(
            delay=0.05, commands=tunnel, kind="tunnel", label="шахта камикадзе"))
        unit.alive = False
        unit.status = "kamikaze"
        unit.crashed_reason = "камикадзе"
        self.world.add_marker(pos[0], pos[2], "kamikaze", ttl=90.0,
                              text=f"#{unit.id}")
        self.state = "finished"
        log.info("Юнит #%s: камикадзе по цели (%.0f, %.0f)", unit.id,
                 target.pos[0], target.pos[2])

    # --------------------------------------------------------------- служебное
    def _finish_action(self, route: Route, wp: Waypoint, ok: bool,
                       report: str) -> None:
        if ok:
            wp.action_done = True
        self.last_action_report = report
        route.note(f"{route.current_idx + 1}: "
                   f"{ACTION_LABELS.get(wp.action, wp.action)} — {report}")
        self.state = "navigate"
        self.action_timer = 0.0
        self.wp_timer = 0.0
        self.strafe_started = False
        route.advance()

    def _release_controls(self) -> None:
        """Маршрут завершён: снять автопилот, но не ронять юнит."""
        spec = self.unit.spec
        if spec.can_hover:
            self.unit.set_target(pitch=0.0, roll=0.0, yaw_rate=0.0,
                                 throttle=spec.hover_throttle)
        elif spec.ground_unit:
            self.unit.set_target(throttle=0.0, yaw_rate=0.0)
        else:
            self.unit.set_target(heading=self.unit.yaw, pitch=0.0, roll=0.0,
                                 throttle=0.7)

    def status(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "action_timer": round(self.action_timer, 2),
            "wp_timer": round(self.wp_timer, 2),
            "last_report": self.last_action_report,
            "strafe_started": self.strafe_started,
        }
