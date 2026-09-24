"""
Движок юнитов: жизненный цикл, тик-цикл, модели, огонь.

Что исправлено
--------------
* **UI-02** — `unit.id` присваивается синхронно в `spawn()`, до запуска потока.
  В наброске id выдавался внутри потока юнита, а вызывающая сторона уже на
  следующей строке делала `controllers[unit.id]` и получала `None`.
* **UI-05/UI-06** — один фоновый поток на все юниты вместо потока на юнит,
  и остановка не блокирует вызывающего: `stop()` лишь ставит флаг,
  `join` — отдельным методом с явным таймаутом.
* **UNIT-11** — модель синхронизируется диффом и только с ограниченной
  частотой (`model_update_hz`); команды уходят в очередь с приоритетом VISUAL
  и слиянием по позиции блока.
* **UI-03** — движок не отдаёт наружу живые словари: `units()` возвращает
  снимки, а доступ к объекту идёт через `get(uid)`.
* **WPN-07** — возвращён авто-сброс при влёте в зону удара (фича из v8).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import mc
from .config import AppConfig
from .ai import BaseAI
from .bases import (CARRIER_DECK_Y, KIND_CARRIER, SUPPLY_PER_TON,
                    BaseManager, RecoveryNode, forward_vec, right_vec)
from .combat import CombatSystem, blast_falloff
from .missiles import MissileManager
from .events import (EventBus, TOPIC_UNIT_ADDED, TOPIC_UNIT_REMOVED,
                     TOPIC_UNIT_UPDATED, TOPIC_WEAPON_FIRED)
from .model import BlockModel, get_blueprint
from .rcon import CommandQueue, Priority, RCONPool
from .routes import Action, Route, RouteExecutor, Waypoint
from .units import Unit, build_unit
from .weapons import Target, WeaponSystem
from .wreckage import WreckageManager
from .world import World

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]


@dataclass
class EngineStats:
    ticks: int = 0
    spawn_calls: int = 0
    crashes: int = 0
    model_commands: int = 0
    model_skipped: int = 0
    tick_overruns: int = 0
    last_tick_ms: float = 0.0
    max_tick_ms: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ticks": self.ticks, "spawn_calls": self.spawn_calls,
            "crashes": self.crashes, "model_commands": self.model_commands,
            "model_skipped": self.model_skipped,
            "tick_overruns": self.tick_overruns,
            "last_tick_ms": round(self.last_tick_ms, 1),
            "max_tick_ms": round(self.max_tick_ms, 1),
        }


class UnitEngine:
    """Владеет всеми юнитами и крутит их в одном потоке."""

    def __init__(
        self,
        world: World,
        queue: CommandQueue,
        weapons: WeaponSystem,
        cfg: Optional[AppConfig] = None,
        bus: Optional[EventBus] = None,
        auto_strike_zone: bool = True,
    ):
        self.world = world
        self.queue = queue
        self.weapons = weapons
        self.cfg = cfg or AppConfig()
        self.bus = bus if bus is not None else world.bus
        self.auto_strike_zone = auto_strike_zone

        self._units: Dict[int, Unit] = {}
        self.fx_announce = None          # callback(ev) для игровой обратной связи
        self.combat = CombatSystem(world, on_event=self._on_combat_event,
                                   on_blast=self._on_blast)
        self.missiles = MissileManager(queue, self.combat,
                                       get_units=self.units)
        self.bases = BaseManager()
        self.wreckage = WreckageManager(queue, world)
        self._duty: Dict[int, str] = {}
        self._duty_route: Dict[int, Any] = {}
        self._duty_origin: Dict[int, Tuple[float, float]] = {}
        self._ais: Dict[int, BaseAI] = {}
        self._executors: Dict[int, RouteExecutor] = {}
        self._route_done: Dict[int, bool] = {}
        self._paused: set = set()
        self._lock = threading.RLock()

        # --- авианосец: блочная палуба и приём техники --------------------
        self._carrier_models: Dict[int, BlockModel] = {}
        self._carrier_key: Dict[int, Tuple[int, int, int]] = {}
        self._carrier_sync_at: Dict[int, float] = {}
        self.carrier_sync_interval = 0.5   # с; в тестах обнуляется
        # uid -> (RecoveryNode, base_id) — активный заход на посадку
        self._recovery: Dict[int, Tuple[RecoveryNode, int]] = {}
        # uid -> (base_id, pad_index) — припаркован на базе
        self._parked: Dict[int, Tuple[int, int]] = {}

        self.tick_dt = float(self.cfg.sim.tick)
        self.model_interval = (1.0 / self.cfg.sim.model_update_hz
                               if self.cfg.sim.model_update_hz > 0 else 0.0)
        self._last_model: Dict[int, float] = {}

        self._stop = threading.Event()
        self._pause_all = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stats = EngineStats()

    # ---------------------------------------------------------------- спавн
    def spawn(
        self,
        variant_key: str,
        pos: Vec3,
        heading: float = 0.0,
        is_bot: bool = False,
        speed: Optional[float] = None,
        throttle: Optional[float] = None,
        tag: Optional[str] = None,
        faction: Optional[str] = None,
        base_id: Optional[int] = None,
    ) -> Unit:
        """Создать юнит и сразу поставить модель в мир.

        Возвращает юнит с **уже присвоенным id** — гонки из наброска нет.
        `base_id` занимает свободную стоянку базы и запоминается на юните.
        """
        self.stats.spawn_calls += 1
        tag = tag or f"rwf_{variant_key}_{int(time.time() * 1000) % 100000}"
        unit = build_unit(variant_key, tag, is_bot=is_bot, pos=pos,
                          heading=heading, speed=speed, throttle=throttle,
                          faction=faction)
        uid = self.world.add_unit(unit)              # синхронно, до потока
        if base_id is not None:
            unit.base_id = base_id
            base = self.bases.get(base_id)
            if base is not None:
                base.take_pad(uid)
        with self._lock:
            self._units[uid] = unit
            self._executors[uid] = RouteExecutor(
                unit, self.world, self.weapons, cfg=self.cfg, bus=self.bus)
        # Первую отрисовку отправляем критическим приоритетом: модель должна
        # появиться сразу, а не ждать, пока разгребётся визуальная очередь.
        unit.sync_model(self.queue, force=True, priority=Priority.CRITICAL)
        self.bus.publish(TOPIC_UNIT_ADDED, uid)
        log.info("Спавн #%d %s в (%.0f, %.0f, %.0f)", uid, unit.spec.label,
                 pos[0], pos[1], pos[2])
        return unit

    def despawn(self, uid: int, explode: bool = False) -> bool:
        """Убрать юнит: модель снимается, чужие блоки не трогаются."""
        with self._lock:
            unit = self._units.pop(uid, None)
            self._paused.discard(uid)
            self._last_model.pop(uid, None)
            self._ais.pop(uid, None)
            self._executors.pop(uid, None)
            self._route_done.pop(uid, None)
        if unit is None:
            return False
        unit.alive = False
        if explode:
            self.queue.submit(mc.instant_explosion(unit.pos), Priority.CRITICAL)
        self._release_base_pad(unit)
        cleared = unit.despawn(self.queue)
        self.world.remove_unit(uid)
        self.bus.publish(TOPIC_UNIT_REMOVED, uid)
        log.info("Деспаун #%d %s (%d блоков модели убрано)", uid,
                 unit.spec.label, cleared)
        return True

    def get(self, uid: int) -> Optional[Unit]:
        with self._lock:
            return self._units.get(uid)

    def units(self, include_dead: bool = False) -> List[Unit]:
        with self._lock:
            out = list(self._units.values())
        return out if include_dead else [u for u in out if u.alive]

    def uids(self) -> List[int]:
        with self._lock:
            return list(self._units.keys())

    def snapshots(self) -> List[Dict[str, Any]]:
        with self._lock:
            units = list(self._units.values())
        return [u.snapshot() for u in units]

    # ------------------------------------------------------------ управление
    def set_target(self, uid: int, **kwargs: Any) -> bool:
        unit = self.get(uid)
        if unit is None:
            return False
        unit.set_target(**kwargs)
        return True

    def trim(self, uid: int, **kwargs: Any) -> bool:
        unit = self.get(uid)
        if unit is None:
            return False
        unit.trim(**kwargs)
        return True

    def level(self, uid: int) -> bool:
        unit = self.get(uid)
        if unit is None:
            return False
        unit.level()
        return True

    def pause(self, uid: int, value: bool = True) -> None:
        with self._lock:
            if value:
                self._paused.add(uid)
            else:
                self._paused.discard(uid)

    def is_paused(self, uid: int) -> bool:
        with self._lock:
            return uid in self._paused

    def pause_all(self, value: bool = True) -> None:
        if value:
            self._pause_all.set()
        else:
            self._pause_all.clear()

    # ------------------------------------------------------- маршруты и ИИ
    def assign_route(self, uid: int, route: Optional[Route]) -> bool:
        """Назначить маршрут юниту.

        ИИ при этом снимается: иначе бот-планировщик затрёт назначенный
        вручную маршрут на следующем же тике (UI-08).
        """
        unit = self.get(uid)
        if unit is None:
            return False
        if route is None:
            self.world.set_route(uid, None)
        else:
            route.unit_id = uid
            route.owner_kind = "bot" if unit.is_bot else "player"
            self.clear_ai(uid)
            self.world.set_route(uid, route)
        with self._lock:
            self._route_done[uid] = False
        return True

    def set_ai(self, uid: int, ai: Optional[BaseAI]) -> bool:
        """Включить/выключить ИИ юнита. При включении ручной маршрут снимается."""
        unit = self.get(uid)
        if unit is None:
            return False
        with self._lock:
            if ai is None:
                self._ais.pop(uid, None)
            else:
                self._ais[uid] = ai
                self._route_done[uid] = False
        if ai is not None:
            self.world.set_route(uid, None)
            log.info("Юнит #%d: включён ИИ %s", uid, ai.name)
        return True

    def clear_ai(self, uid: int) -> None:
        with self._lock:
            self._ais.pop(uid, None)

    def get_ai(self, uid: int) -> Optional[BaseAI]:
        with self._lock:
            return self._ais.get(uid)

    def ai_status(self, uid: int) -> Optional[Dict[str, Any]]:
        ai = self.get_ai(uid)
        return ai.status() if ai else None

    def executor(self, uid: int) -> Optional[RouteExecutor]:
        with self._lock:
            return self._executors.get(uid)

    def route_status(self, uid: int) -> Dict[str, Any]:
        route = self.world.get_route(uid)
        ex = self.executor(uid)
        out: Dict[str, Any] = {"has_route": route is not None}
        if route is not None:
            snap = route.snapshot()
            out.update({
                "name": snap["name"], "points": len(snap["waypoints"]),
                "current": snap["current_idx"], "done": snap["done"],
                "loop": snap["loop"], "owner": snap["owner_kind"],
                "cycles": snap["cycles"],
            })
        if ex is not None:
            out.update(ex.status())
        ai = self.get_ai(uid)
        if ai is not None:
            out["ai"] = ai.status()
        return out

    def service(self, uid: int) -> bool:
        """Дозаправка и перезарядка (вручную или по возврату на базу)."""
        ok = self.refuel(uid)
        return self.rearm(uid) and ok

    # ---------------------------------------------------------------- огонь
    def fire(self, uid: int, category: Optional[str] = None,
             mount_index: Optional[int] = None,
             target: Optional[Target] = None) -> int:
        """Выстрел. Возвращает число сработавших подвесов."""
        unit = self.get(uid)
        if unit is None or not unit.alive:
            return 0
        if mount_index is not None:
            if not (0 <= mount_index < len(unit.mounts)):
                return 0
            return 1 if self.weapons.fire(unit, unit.mounts[mount_index], target) else 0
        if category:
            return self.weapons.fire_category(unit, category, target)
        return self.weapons.fire_ready(unit, target)

    # ------------------------------------------------------------------ тик
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="unit-engine",
                                        daemon=True)
        self._thread.start()

    def stop(self, join_timeout: Optional[float] = None) -> None:
        """Остановить движок. `join_timeout=None` — не ждать (не блокировать GUI)."""
        self._stop.set()
        if join_timeout is not None and self._thread:
            self._thread.join(timeout=join_timeout)

    def join(self, timeout: float = 3.0) -> bool:
        if not self._thread:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        """Цикл реального времени (REAL-01): симуляция догоняет wall-clock.

        Набросок и ранние версии делали ОДИН тик за итерацию цикла, поэтому
        при любом overrun (сеть, GIL, сон ОС) симуляционное время отставало
        от реального и скорость/манёвренность техники зависели от частоты
        тиков и нагрузки. Здесь аккумулятор реального времени раздаёт долгие
        подшагами по `tick_dt`: период тика влияет ТОЛЬКО на гладкость
        интегрирования, но не на пройденный путь за секунду (REAL-02).
        """
        last = time.monotonic()
        acc = 0.0
        max_steps = 8
        while not self._stop.is_set():
            now = time.monotonic()
            # после сна/паузы GIL не устраиваем лавину: не больше 0.5 с долга
            acc += min(0.5, now - last)
            last = now
            started = time.monotonic()
            steps = 0
            try:
                while acc >= self.tick_dt and steps < max_steps:
                    if not self._pause_all.is_set():
                        self.tick_once(self.tick_dt)
                    acc -= self.tick_dt
                    steps += 1
                if acc >= self.tick_dt:      # не успеваем вовсе — сброс долга
                    acc = 0.0
                    self.stats.tick_overruns += 1
            except Exception:  # noqa: BLE001 - тик не должен убивать движок
                log.exception("Ошибка в тике движка")
            elapsed = time.monotonic() - started
            self.stats.last_tick_ms = elapsed * 1000.0
            self.stats.max_tick_ms = max(self.stats.max_tick_ms, elapsed * 1000.0)
            self._stop.wait(max(0.0, self.tick_dt - elapsed))

    def tick_once(self, dt: Optional[float] = None) -> None:
        """Один тик симуляции. Вызывается из потока движка либо из теста."""
        if self._pause_all.is_set():
            return
        dt = self.tick_dt if dt is None else dt
        now = time.monotonic()
        self.stats.ticks += 1

        with self._lock:
            pairs = [(uid, u) for uid, u in self._units.items() if u.alive]
            paused = set(self._paused)

        # Юниты, уничтоженные ВНЕ тика (урон из боя/игры), тоже надо убрать:
        # иначе они висят в реестре и на карте мёртвыми.
        for uid in [u for u, un in self._units.items() if not un.alive]:
            unit = self._units[uid]
            self.queue.submit(mc.instant_explosion(unit.pos), Priority.CRITICAL)
            self._make_wreck(unit)
            self._score_loss(unit)
            self._release_base_pad(unit)
            unit.despawn(self.queue)
            with self._lock:
                self._units.pop(uid, None)
                self._paused.discard(uid)
                self._ais.pop(uid, None)
                self._executors.pop(uid, None)
                self._last_model.pop(uid, None)
            self.world.remove_unit(uid)
            self.bus.publish(TOPIC_UNIT_REMOVED, uid)
            self.stats.crashes += 1

        crashed: List[int] = []
        for uid, unit in pairs:
            if uid in paused:
                continue

            # --- припаркован на базе: физика выключена, едет вместе с палубой
            if uid in self._parked:
                try:
                    self._tick_parked(uid, unit, now)
                except Exception:  # noqa: BLE001
                    log.exception("Юнит #%d: ошибка стоянки", uid)
                continue

            # --- заход на посадку: поймать касание до шага физики
            if uid in self._recovery:
                try:
                    if self._check_touchdown(uid, unit, now):
                        continue
                except Exception:  # noqa: BLE001
                    log.exception("Юнит #%d: ошибка захода на посадку", uid)

            # Порядок важен: ИИ решает, КАКОЙ маршрут нужен, исполнитель
            # переводит его в целевые значения рулей, физика их отрабатывает.
            try:
                self._tick_ai(uid, unit, dt)
                executor = self._executors.get(uid)
                if executor is not None:
                    executor.tick(self.world.get_route(uid), dt)
            except Exception:  # noqa: BLE001 - сбой маршрута не роняет юнит
                log.exception("Юнит #%d: ошибка маршрута/ИИ", uid)

            try:
                unit.step(dt, self.world)
            except Exception:  # noqa: BLE001 - один юнит не роняет остальных
                log.exception("Юнит #%d: ошибка физики", uid)
                continue

            if not unit.alive:
                crashed.append(uid)
                continue

            # --- зона удара: авто-сброс (WPN-07) ---
            if (self.auto_strike_zone and not unit.is_bot
                    and self.world.in_strike_zone(unit.pos[0], unit.pos[2])):
                self.weapons.fire_category(unit, "bomb")

            # --- модель с ограниченной частотой ---
            if self.model_interval <= 0 or \
                    now - self._last_model.get(uid, 0.0) >= self.model_interval:
                sent = unit.sync_model(self.queue)
                if sent:
                    self.stats.model_commands += sent
                    self._last_model[uid] = now
                else:
                    self.stats.model_skipped += 1

        # --- оружие: ракеты, отложенные эффекты, урон ---
        try:
            self.weapons.update(dt)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обновления оружия")

        # --- блочные ракеты: тик, модель, детонация ---
        try:
            for ev in self.missiles.step(dt):
                if ev["kind"] == "hit":
                    self.bus.publish("missile.hit", ev["missile"], ev["pos"])
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обновления ракет")

        # --- базы: авианосец плывёт, его блочная палуба едет вместе с ним ---
        try:
            self.bases.step(dt)
            self.bases.step_supply(dt)
            self._sync_carrier_models(now)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обновления баз")

        try:
            self.wreckage.step(dt)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обновления мест аварий")

        # --- погибшие: убрать модель и мир ---
        for uid in crashed:
            self.stats.crashes += 1
            unit = self.get(uid)
            if unit is not None:
                self.queue.submit(mc.instant_explosion(unit.pos), Priority.CRITICAL)
                self.queue.submit(mc.particle("minecraft:large_smoke", unit.pos,
                                              (2.0, 2.0, 2.0), 0.05, 60),
                                  Priority.NORMAL)
                self._make_wreck(unit)
                self._score_loss(unit)
                self._release_base_pad(unit)
                unit.despawn(self.queue)
            with self._lock:
                self._units.pop(uid, None)
                self._paused.discard(uid)
                self._last_model.pop(uid, None)
                self._ais.pop(uid, None)
                self._executors.pop(uid, None)
                self._route_done.pop(uid, None)
            self.world.remove_unit(uid)
            self.bus.publish(TOPIC_UNIT_REMOVED, uid)

        for uid, unit in pairs:
            if unit.alive:
                self.bus.publish(TOPIC_UNIT_UPDATED, uid)

    # ------------------------------------------------------------------
    #  Разрушения: след аварии на месте гибели машины (WRECK-01…)
    # ------------------------------------------------------------------
    def _make_wreck(self, unit) -> None:
        """Обломки, кратер, сгоревший остов и горящая зона на месте гибели.

        Для авиации остов опускается на рельеф: иначе обломки висели бы в
        воздухе на высоте гибели, что сразу читается как баг.
        """
        try:
            ground = self.world.terrain.height_at(unit.pos[0], unit.pos[2])
            gy = (float(ground) + 1.0) if ground is not None else None
            self.wreckage.create_from_unit(unit,
                                           reason=unit.crashed_reason or "",
                                           ground_y=gy)
            self.bus.publish("unit.wreck", unit.id, tuple(unit.pos))
        except Exception:  # noqa: BLE001
            log.exception("Не удалось создать место аварии для #%s",
                          getattr(unit, "id", "?"))

    def damage_bases(self, pos: Vec3, radius: float, damage: float,
                     source: str = "") -> List[int]:
        """Урон базам в зоне взрыва. Возвращает id уничтоженных баз.

        У базы есть прочность, значит её можно снести. Фолл-офф тот же, что и
        по технике, но дистанция считается от края базы (минус половина
        радиуса): территория большая, и попадание «в габариты» уже считается.
        """
        destroyed: List[int] = []
        if radius <= 0 or damage <= 0:
            return destroyed
        for base in self.bases.all():
            if base.destroyed:
                continue
            d = max(0.0, base.distance_to(pos[0], pos[2]) - base.radius * 0.5)
            if d > radius:
                continue
            falloff = blast_falloff(d, radius, floor=0.30, slope=0.70)   # P1.2: единая формула, базовые дефолты
            if base.take_damage(damage * falloff, source):
                destroyed.append(base.id)
                self._on_base_destroyed(base, source)
        return destroyed

    def _on_base_destroyed(self, base, source: str) -> None:
        """База уничтожена: взрыв, обломки, стоянки освобождаются."""
        self.queue.submit(mc.instant_explosion((base.x, self._pad_ground_y(base, base.x, base.z), base.z)),
                          Priority.CRITICAL)
        try:
            self.wreckage.create((base.x, self._pad_ground_y(base, base.x, base.z), base.z), kind="base",
                                 label=base.name, reason=source or "уничтожена")
        except Exception:  # noqa: BLE001
            log.exception("Обломки базы #%d не созданы", base.id)
        for pad in base.pads:
            uid = pad.occupied_by
            pad.occupied_by = None
            if uid is None:
                continue
            self._unpark(uid)
            unit = self.get(uid)
            if unit is not None:
                unit.take_damage(unit.health * 2.0, "взрыв базы", None,
                                 self.world)
        self.bus.publish("base.destroyed", base.id, base.name)
        self.bus.publish("score", "base_destroyed")
        self.bus.publish("log", f"БАЗА «{base.name}» УНИЧТОЖЕНА", "red")
        log.warning("База #%d «%s» уничтожена (%s)", base.id, base.name,
                    source or "—")

    # ------------------------------------------------------------------
    #  Режим службы: «Стоянка» / «В бой» (UX-15)
    # ------------------------------------------------------------------
    def duty(self, uid: int) -> str:
        """Текущий режим службы: 'park' или 'combat'."""
        return self._duty.get(uid, "combat")

    def set_duty(self, uid: int, mode: str) -> bool:
        """Переключить режим службы юнита (тумблер в пульте).

        'park' — автовозврат на приписную базу; по касанию машина уходит на
        стоянку и обслуживается (`_attach_parked` уже вызывает `service_at`),
        то есть ручные кнопки заправки/снаряжения/ТО больше не нужны.

        'combat' — автовзлёт со стоянки и дальше либо маршрут оператора
        (если он был назначен до стоянки), либо патруль вокруг точки взлёта.
        """
        unit = self.get(uid)
        if unit is None or not unit.alive:
            return False
        mode = "park" if str(mode).startswith("park") else "combat"
        self._duty[uid] = mode
        unit.duty = mode

        if mode == "park":
            if uid in self._parked:
                return True
            route = self.world.get_route(uid)
            if route is not None and not route.done:
                self._duty_route[uid] = route     # вернём, когда пошлют в бой
            ok = self.request_recovery(uid, unit.base_id)
            if not ok:
                ok = self.request_recovery(uid)
            if not ok:
                self.bus.publish("log",
                                 f"#{uid}: стоянка невозможна — нет базы со "
                                 f"свободным местом", "yellow")
            return ok

        if uid in self._parked:
            self._duty_origin[uid] = (unit.pos[0], unit.pos[2])
            self.launch_parked(uid)
        route = self._duty_route.pop(uid, None)
        if route is not None and not getattr(route, "done", True):
            self.world.set_route(uid, route)
            self.bus.publish("log", f"#{uid}: в бой, маршрут восстановлен",
                             "green")
            return True
        origin = self._duty_origin.get(uid, (unit.pos[0], unit.pos[2]))
        alt = max(120.0, unit.pos[1] + 30.0)
        r = 220.0
        pts = [(origin[0] + r, origin[1]), (origin[0], origin[1] + r),
               (origin[0] - r, origin[1]), (origin[0], origin[1] - r)]
        from .ai import PatrolAI
        self.set_ai(uid, PatrolAI(waypoints=pts, altitude=alt, cfg=self.cfg))
        self.bus.publish("log", f"#{uid}: в бой, патруль вокруг точки взлёта",
                         "green")
        return True

    def deliver_cargo(self, uid: int, base_id: Optional[int] = None) -> float:
        """Передать груз машины в снабжение базы (логистика авианосца).

        Возвращает число зачисленных очков. Авианосец своей генерации не
        имеет, поэтому без такого рейса он со временем остаётся без ТО —
        это и есть смысл логистического плеча.
        """
        unit = self.get(uid)
        if unit is None or unit.cargo <= 0.0:
            return 0.0
        base = self.bases.get(base_id) if base_id is not None else None
        if base is None:
            base = self.bases.nearest(unit.pos[0], unit.pos[2],
                                      unit_kind=unit.spec.kind)
        if base is None or base.destroyed:
            return 0.0
        tons = float(unit.cargo)
        gained = base.deliver_cargo(tons)
        # Груз списываем ровно в принятом объёме: если склад базы полон,
        # транспортник НЕ теряет тонны впустую — он может слетать на другую.
        accepted = gained / SUPPLY_PER_TON if gained > 0 else 0.0
        unit.cargo = max(0.0, tons - accepted)
        if gained <= 0:
            self.bus.publish("log",
                             f"«{base.name}»: склад полон, груз #{uid} "
                             f"не принят ({tons:.1f} т на борту)", "yellow")
            return 0.0
        if gained > 0:
            self.bus.publish("log",
                             f"#{uid} → «{base.name}»: доставлено {tons:.1f} т, "
                             f"+{gained:.0f} очков снабжения", "green")
            self.world.add_marker(base.x, base.z, "service", ttl=60.0,
                                  text=f"+{gained:.0f} снабжение")
            self.bus.publish("score", "delivery", {"tons": tons})
        return gained

    def spawn_from_base(self, base_id: int, variant: str, altitude: float,
                        is_bot: bool = False, faction: Optional[str] = None,
                        speed: Optional[float] = None,
                        throttle: Optional[float] = None) -> Optional[Unit]:
        """Запуск со свободной стоянки базы: точка и курс берутся у базы.

        Для авианосца высота не может быть ниже палубы, а самолёт уходит
        с катапульты: стартовая скорость не ниже 1.25 скорости сваливания.
        """
        base = self.bases.get(base_id)
        if base is None or base.destroyed:
            return None
        pad = base.free_pad()
        if pad is None:
            log.warning("База %s: свободных стоянок нет", base.name)
            return None
        x, z, heading = base.pad_world(pad)
        if base.kind == KIND_CARRIER:
            altitude = max(altitude, CARRIER_DECK_Y)
        unit = self.spawn(variant, (x, altitude, z), heading=heading,
                          is_bot=is_bot, faction=faction, speed=speed,
                          throttle=throttle, base_id=base_id)
        if unit is not None:
            if (base.kind == KIND_CARRIER
                    and not getattr(unit.spec, "can_hover", False)
                    and (speed is None or speed < unit.spec.stall_speed * 1.25)):
                unit.speed = max(unit.spec.stall_speed * 1.25, 55.0)
                unit.set_target(throttle=1.0)
            log.info("Запуск #%s со стоянки %d базы %s", unit.id, pad.index,
                     base.name)
        return unit

    def service_at_nearest_base(self, uid: int) -> List[str]:
        """Обслужить юнит на ближайшей подходящей базе (возврат/RTB)."""
        unit = self.get(uid)
        if unit is None:
            return []
        base = self.bases.nearest(unit.pos[0], unit.pos[2],
                                  unit_kind=unit.spec.kind)
        if base is None:
            return []
        return self.bases.service_at(base, unit)

    # ------------------------------------------------------------------
    #  Посадка на базы: унифицированная нода восстановления (RecoveryNode)
    # ------------------------------------------------------------------
    def request_recovery(self, uid: int, base_id: Optional[int] = None) -> bool:
        """Направить юнит на посадку: маршрут захода + нода восстановления.

        Работает одинаково для аэродрома (полоса), авианосца (палуба) и
        наземной базы (вертолётная площадка) — различаются только параметры
        ноды, которые даёт `Base.recovery_node`.
        """
        unit = self.get(uid)
        if unit is None or not unit.alive or uid in self._parked:
            return False
        base = self.bases.get(base_id) if base_id is not None else None
        if base is None:
            base = self.bases.nearest(unit.pos[0], unit.pos[2],
                                      unit_kind=unit.spec.kind,
                                      faction=unit.faction)
        if base is None:
            base = self.bases.nearest(unit.pos[0], unit.pos[2],
                                      unit_kind=unit.spec.kind)
        if base is None or base.free_pad() is None:
            return False
        node = base.recovery_node(unit.spec.kind)
        if node is None:
            return False
        if node.kind in ("runway", "helipad"):
            # полоса/площадка лежат на рельефе — уточняем высоту касания
            h = self.world.terrain.height_at(node.x, node.z)
            if h is not None:
                node.y = float(h) + 1.0
        ax, az, aalt = node.approach_point()
        # Если юнит уже в створе финальной прямой (например, оператор нажал
        # «Посадку» на короткой финале), точку начала захода пропускаем —
        # иначе исполнитель маршрута развернёт его назад к началу прямой.
        fx, fz = forward_vec(node.heading)
        rx, rz = right_vec(node.heading)
        dx, dz = unit.pos[0] - node.x, unit.pos[2] - node.z
        along = dx * fx + dz * fz
        cross = abs(dx * rx + dz * rz)
        on_final = (-50.0 <= along <= node.capture_along + 150.0
                    and cross <= node.capture_cross * 2.0
                    and unit.pos[1] <= node.y + node.approach_alt + 30.0)
        wps = []
        if not on_final:
            wps.append(Waypoint(x=ax, z=az, altitude=aalt,
                                action=Action.NAVIGATE, pass_mode="precise",
                                radius=50.0, note="заход на посадку"))
        wps.append(Waypoint(x=node.x, z=node.z, altitude=node.y + 2.0,
                            action=Action.RTB, pass_mode="precise",
                            radius=35.0, note="точка касания"))
        route = Route(wps, uid, loop=False, owner_kind="player",
                      name=f"Посадка: {base.name}")
        with self._lock:
            self._recovery[uid] = (node, base.id)
        self.world.set_route(uid, route)
        self.bus.publish("unit.recovery", uid, base.id)
        self.bus.publish("log", f"#{uid}: заход на посадку «{base.name}»",
                         "cyan")
        log.info("#%d заходит на посадку: %s (%s)", uid, base.name, node.kind)
        return True

    def cancel_recovery(self, uid: int) -> None:
        """Отменить заход (маршрут остаётся у оператора)."""
        with self._lock:
            self._recovery.pop(uid, None)

    def recovery_info(self, uid: int) -> Optional[Dict[str, Any]]:
        entry = self._recovery.get(uid)
        if not entry:
            return None
        node, base_id = entry
        out = node.to_dict()
        out["active_base"] = base_id
        return out

    def parked_info(self, uid: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._parked.get(uid)
        if not entry:
            return None
        base = self.bases.get(entry[0])
        return {"base_id": entry[0], "pad": entry[1],
                "base_name": base.name if base else ""}

    def _pad_ground_y(self, base, x: float, z: float) -> float:
        """Y поверхности под стоянкой: палуба авианосца либо рельеф."""
        if base.kind == KIND_CARRIER:
            return CARRIER_DECK_Y
        h = self.world.terrain.height_at(x, z)
        return float(h) + 1.0 if h is not None else base.ground_y(x, z) + 1.0

    def _touchdown_ok(self, unit: Unit, node: RecoveryNode) -> bool:
        """Юнит в окне захвата ноды: вдоль/поперёк/высота/скорость."""
        fx, fz = forward_vec(node.heading)
        rx, rz = right_vec(node.heading)
        dx = unit.pos[0] - node.x
        dz = unit.pos[2] - node.z
        along = dx * fx + dz * fz
        cross = abs(dx * rx + dz * rz)
        dalt = unit.pos[1] - node.y
        return (-20.0 <= along <= node.capture_along
                and cross <= node.capture_cross
                and -4.0 <= dalt <= node.capture_alt
                and unit.speed <= node.max_touchdown_speed)

    def _check_touchdown(self, uid: int, unit: Unit, now: float) -> bool:
        entry = self._recovery.get(uid)
        if entry is None:
            return False
        node, base_id = entry
        base = self.bases.get(base_id)
        if base is None:
            with self._lock:
                self._recovery.pop(uid, None)
            return False
        if not self._touchdown_ok(unit, node):
            return False
        pad = base.take_pad(uid)
        if pad is None:
            with self._lock:
                self._recovery.pop(uid, None)
            self.world.set_route(uid, None)
            self.bus.publish("log",
                             f"«{base.name}»: нет свободных стоянок для #{uid}",
                             "yellow")
            return False
        self._attach_parked(uid, unit, base, pad)
        return True

    def _attach_parked(self, uid: int, unit: Unit, base, pad) -> None:
        x, z, heading = pad.world(base)
        y = self._pad_ground_y(base, x, z)
        unit.pos = (x, y, z)
        unit.yaw = heading
        unit.speed = 0.0
        unit.status = "parked"
        unit.base_id = base.id
        with self._lock:
            self._recovery.pop(uid, None)
            self._parked[uid] = (base.id, pad.index)
            self._ais.pop(uid, None)          # на стоянке ИИ не рулит
        self.world.set_route(uid, None)
        done = self.bases.service_at(base, unit)
        delivered = self.deliver_cargo(uid, base.id) if unit.cargo > 0 else 0.0
        if delivered > 0:
            done.append(f"груз +{delivered:.0f}")
        unit.sync_model(self.queue, force=True)
        self.world.add_marker(x, z, "service", text=f"#{uid} на «{base.name}»")
        self.bus.publish("unit.landed", uid, base.id)
        self.bus.publish("score", "landing")
        self.bus.publish("log",
                         f"#{uid} сел на «{base.name}»"
                         + (f" ({', '.join(done)})" if done else ""), "green")
        log.info("#%d припаркован на %s (стоянка %d): %s", uid, base.name,
                 pad.index, ", ".join(done) or "—")

    def _tick_parked(self, uid: int, unit: Unit, now: float) -> None:
        """Припаркованный юнит едет вместе с базой (палуба движется)."""
        base_id, pad_index = self._parked[uid]
        base = self.bases.get(base_id)
        if base is None or not unit.alive:
            self._unpark(uid)
            return
        pad = next((p for p in base.pads if p.index == pad_index), None)
        if pad is None:
            self._unpark(uid)
            return
        x, z, heading = pad.world(base)
        y = self._pad_ground_y(base, x, z)
        moved = (abs(x - unit.pos[0]) + abs(z - unit.pos[2])
                 + abs(heading - unit.yaw))
        unit.pos = (x, y, z)
        unit.yaw = heading
        unit.speed = 0.0
        unit.status = "parked"
        if moved > 0.5 and (self.model_interval <= 0
                            or now - self._last_model.get(uid, 0.0)
                            >= self.model_interval):
            unit.sync_model(self.queue)
            self._last_model[uid] = now

    def _unpark(self, uid: int) -> None:
        with self._lock:
            entry = self._parked.pop(uid, None)
        if entry:
            base = self.bases.get(entry[0])
            if base is not None:
                base.release_pad(uid)

    def launch_parked(self, uid: int) -> bool:
        """Взлёт со стоянки: самолёт — катапульта, вертолёт/БПЛА — отрыв."""
        with self._lock:
            entry = self._parked.pop(uid, None)
        unit = self.get(uid)
        if entry is None or unit is None or not unit.alive:
            return False
        base_id, _pad_index = entry
        base = self.bases.get(base_id)
        if base is not None:
            base.release_pad(uid)
        spec = unit.spec
        if getattr(spec, "can_hover", False):
            unit.speed = 0.0
            unit.set_target(throttle=1.0, pitch=0.0, roll=0.0)
        else:
            unit.speed = max(spec.stall_speed * 1.25, 55.0)
            unit.set_target(throttle=1.0, heading=unit.yaw)
        unit.status = "flying"
        unit.sync_model(self.queue, force=True)
        self.bus.publish("unit.launched", uid, base_id)
        self.bus.publish("score", "launch")
        self.bus.publish("log",
                         f"#{uid} взлетел с «{base.name if base else 'базы'}»",
                         "green")
        log.info("#%d: взлёт со стоянки базы #%s", uid, base_id)
        return True

    # ------------------------------------------------------------------
    #  Авианосец: блочная палуба и курс
    # ------------------------------------------------------------------
    def _sync_carrier_models(self, now: float) -> None:
        """Дифф-синхронизация блочных палуб с квантованием позиции/курса.

        Квант 2 м / 3° и интервал ≥0.5 с держат поток команд в бюджете:
        на крейсерской скорости 3 м/с это ~2 перерисовки в секунду по
        ~20-40 setblock (полоса палубы), а не сотни на каждый метр.
        """
        seen = set()
        for base in self.bases.all():
            if base.kind != KIND_CARRIER:
                continue
            seen.add(base.id)
            model = self._carrier_models.get(base.id)
            if model is None:
                model = BlockModel(get_blueprint("carrier"))
                self._carrier_models[base.id] = model
            key = (int(base.x // 2), int(base.z // 2), int(base.heading // 3))
            if self._carrier_key.get(base.id) == key and model.placed_count:
                continue
            limit = max(self.carrier_sync_interval, self.model_interval * 2.0)
            if now - self._carrier_sync_at.get(base.id, 0.0) < limit:
                continue
            self._carrier_key[base.id] = key
            self._carrier_sync_at[base.id] = now
            pos = (base.x, CARRIER_DECK_Y - 1.0, base.z)
            writes, clears = model.sync(pos, base.heading)
            for (x, y, z) in clears:
                self.queue.submit(mc.setblock(x, y, z, "minecraft:air"),
                                  Priority.VISUAL,
                                  key=f"car{base.id}:{x},{y},{z}")
            for (x, y, z), block in writes:
                self.queue.submit(mc.setblock(x, y, z, block),
                                  Priority.VISUAL,
                                  key=f"car{base.id}:{x},{y},{z}")
            if writes or clears:
                self.stats.model_commands += len(writes) + len(clears)
        for bid in list(self._carrier_models):
            if bid not in seen:
                self._remove_carrier_model(bid)

    def _remove_carrier_model(self, base_id: int) -> None:
        model = self._carrier_models.pop(base_id, None)
        self._carrier_key.pop(base_id, None)
        self._carrier_sync_at.pop(base_id, None)
        if model is None:
            return
        for (x, y, z) in model.clear():
            self.queue.submit(mc.setblock(x, y, z, "minecraft:air"),
                              Priority.NORMAL, key=f"car{base_id}:{x},{y},{z}")

    def clear_base_models(self) -> None:
        """Снять все блочные палубы с карты мира (остановка/чистка)."""
        for bid in list(self._carrier_models):
            self._remove_carrier_model(bid)

    def move_base(self, base_id: int, x: float, z: float) -> bool:
        """Курс подвижной базе (авианосцу) на точку карты."""
        base = self.bases.get(base_id)
        if base is None or not base.movable:
            return False
        base.move_to((x, z))
        self.bus.publish("log",
                         f"«{base.name}»: курс на ({x:.0f}, {z:.0f})", "cyan")
        return True

    def _release_base_pad(self, unit) -> None:
        with self._lock:
            self._recovery.pop(unit.id, None)
            self._parked.pop(unit.id, None)
        base_id = getattr(unit, "base_id", None)
        if base_id is not None:
            base = self.bases.get(base_id)
            if base is not None:
                self.bases.release(base, unit.id)

    def _on_blast(self, pos, radius: float, damage: float,
                  source_kind: str = "") -> None:
        """Взрыв: урон базам (BASE-HP) и очко за попадание в зону удара."""
        try:
            self.damage_bases(tuple(pos), radius, damage, source_kind)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка урона базам от взрыва")
        try:
            if self.world.in_strike_zone(pos[0], pos[2]):
                self.bus.publish("score", "strike")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка зачёта попадания в зону")

    def _score_loss(self, unit) -> None:
        """Потеря своей машины снимает очки; гибель бота — даёт."""
        if getattr(unit, "is_bot", False):
            kind = "kill_ground" if getattr(unit.spec, "ground_unit",
                                            False) else "kill"
            self.bus.publish("score", kind)
        else:
            self.bus.publish("score",
                             "crash" if unit.status == "crashed" else "loss")

    def _on_combat_event(self, ev) -> None:
        """Боевое событие: лог + игровая обратная связь."""
        self.bus.publish("combat", ev.describe())
        if self.fx_announce is not None:
            try:
                self.fx_announce(ev)
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------- сервис
    def _tick_ai(self, uid: int, unit: Unit, dt: float) -> None:
        """Обновить ИИ и отследить завершение маршрута."""
        ai = self._ais.get(uid)
        route = self.world.get_route(uid)
        if ai is not None:
            fresh = ai.update(unit, self.world, dt)
            if fresh is not None:
                self.world.set_route(uid, fresh)
                route = fresh
                self._route_done[uid] = False

        if route is not None:
            was_done = self._route_done.get(uid, False)
            if route.done and not was_done:
                self._route_done[uid] = True
                if ai is not None:
                    ai.on_route_finished(unit, self.world)
                    # Бот остался без боезапаса/топлива и долетел до точки RTB:
                    # дальше его ведёт унифицированная нода посадки базы.
                    if (ai.needs_service(unit) and uid not in self._recovery
                            and uid not in self._parked):
                        self.request_recovery(uid)
                self.bus.publish("score", "route_done")
                log.info("Юнит #%d: маршрут %r завершён", uid, route.name)
            elif not route.done and was_done:
                self._route_done[uid] = False

    def set_tick(self, dt: float) -> None:
        self.tick_dt = max(0.02, min(2.0, float(dt)))

    def refuel(self, uid: int, amount: Optional[float] = None) -> bool:
        unit = self.get(uid)
        if unit is None:
            return False
        unit.fuel = unit.spec.fuel_max if amount is None else \
            min(unit.spec.fuel_max, unit.fuel + amount)
        return True

    def rearm(self, uid: int) -> bool:
        unit = self.get(uid)
        if unit is None:
            return False
        for m in unit.mounts:
            m.reload()
        return True

    def load_weapon(self, uid: int, slot_index: int, key: Optional[str]) -> bool:
        """Сменить оружие на подвесе (с проверкой совместимости категории)."""
        unit = self.get(uid)
        if unit is None or not (0 <= slot_index < len(unit.mounts)):
            return False
        mount = unit.mounts[slot_index]
        allowed = unit.AVAILABLE.get(mount.category, ())
        if key is not None and key not in allowed:
            log.warning("Подвес %s: %s недоступен для %s (можно: %s)",
                        mount.slot, key, unit.spec.label, ", ".join(allowed) or "—")
            return False
        try:
            mount.load(key)
        except (KeyError, ValueError) as exc:
            log.warning("Не удалось зарядить подвес: %s", exc)
            return False
        return True

    def stats_snapshot(self) -> Dict[str, Any]:
        return {
            "engine": self.stats.as_dict(),
            "units": len(self._units),
            "paused": len(self._paused),
            "ai_units": len(self._ais),
            "routes_active": len(self.world.routes),
            "weapons": self.weapons.stats(),
            "queue": self.queue.stats(),
            "world_revision": self.world.revision,
        }
