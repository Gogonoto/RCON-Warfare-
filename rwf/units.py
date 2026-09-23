"""
Техника: характеристики, физика полёта/движения, интеграция с моделью.

Семантика управления — ЕДИНАЯ и абсолютная
------------------------------------------
Главный дефект наброска (UNIT-01): `apply_controls(pitch=...)` в одном месте
интерпретировался как «прибавить к текущему тангажу», в другом — как «вот
столько держать». И то и другое происходило одновременно, поэтому самолёт был
неуправляем.

Здесь все значения управления — **целевые абсолютные величины**:

    unit.set_target(pitch=-10.0)     # держать тангаж −10° (нос вверх)
    unit.set_target(roll=30.0)       # держать крен 30° (правое крыло вниз)
    unit.set_target(heading=270.0)   # автопилот: выйти на курс 270°
    unit.set_target(throttle=0.8)    # 80% тяги
    unit.trim(dpitch=-2.0)           # сдвинуть ЦЕЛЬ на 2° (ручной режим)

Физика приводит текущие значения к целевым с ограниченной скоростью
(`pitch_rate`, `roll_rate`), поэтому появляется инерция, а крен больше не
затухает сам по себе (UNIT-02). Все производные умножаются на `dt` (UNIT-03).

Вираж считается как согласованный разворот: n = 1/cos(крен),
ω = g·tan(крен)/V. Отсюда же берётся реальная перегрузка (UNIT-10) и
ограничение по `max_g`.
"""
from __future__ import annotations

import logging
import math
import time
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import mc
from .model import BLUEPRINTS, BlockModel, Blueprint, get_blueprint
from .rcon import CommandQueue, Priority
from .unitstate import BURN_HP_PCT
from .weapons import WEAPON_CATEGORIES, WeaponMount, build_mounts
from .world import World

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]

GRAVITY = 15.0            # м/с² — эффективная гравитация сущностей Minecraft
SEA_LEVEL = 63.0
Loadout = Sequence[Tuple[str, str, Optional[str]]]


# ---------------------------------------------------------------------------
#  Характеристики
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UnitSpec:
    """Паспорт техники. Неизменяемый и общий для класса; экземпляр получает
    копию через `replace()`, если что-то настраивается индивидуально
    (закрывает UNIT-15 — общий изменяемый объект на все экземпляры)."""
    kind: str = "unit"
    label: str = "Юнит"
    blueprint: str = "su25"

    # --- скорость (блоки/с = м/с) ---
    max_speed: float = 40.0
    min_speed: float = 12.0
    cruise_speed: float = 25.0
    accel: float = 6.0              # как быстро набирается целевая скорость

    # --- высоты ---
    max_altitude: float = 250.0
    min_altitude: float = 15.0
    service_ceiling: float = 300.0

    # --- управляемость ---
    can_hover: bool = False
    ground_unit: bool = False
    max_pitch: float = 45.0
    max_roll: float = 55.0
    pitch_rate: float = 25.0        # град/с
    roll_rate: float = 45.0         # град/с
    yaw_rate: float = 0.0           # прямое рысканье (вертолёт/танк), град/с
    turn_rate: float = 45.0         # ограничение рысканья, град/с
    climb_rate: float = 12.0        # м/с для вертолётных
    hover_throttle: float = 0.5

    # --- самолётные ---
    stall_speed: float = 10.0
    max_g: float = 5.0

    # --- ресурс ---
    fuel_max: float = 240.0         # секунд полного газа
    fuel_idle: float = 0.25         # доля расхода на малом газу
    cargo_max: float = 0.0          # т полезной нагрузки (транспортники/truck)
    # --- живучесть ---
    health: float = 100.0           # очки прочности
    armor: float = 0.0              # 0..80, процент гашения урона

    # --- наземная техника ---
    max_climb_deg: float = 30.0     # предельный угол подъёма, град (GROUND-02)

    # --- автопилот ---
    heading_rate_gain: float = 0.9  # желаемая угловая скорость (град/с) на градус ошибки
    heading_max_rate: float = 35.0  # предел скорости разворота автопилота, град/с
    waypoint_radius: float = 25.0   # м: ближе — точка считается достигнутой

    @property
    def idle_speed(self) -> float:
        """Скорость установившегося полёта на малом газе.

        Намеренно ниже `stall_speed`: в наброске целевая скорость при нулевой
        тяге равнялась `min_speed`, из-за чего самолёт с убранным газом
        **разгонялся** и никогда не сваливался.
        """
        if self.can_hover or self.ground_unit:
            return 0.0
        return max(0.0, self.stall_speed * 0.6)

    def copy(self, **changes: Any) -> "UnitSpec":
        return replace(self, **changes)


# ---------------------------------------------------------------------------
#  Управление
# ---------------------------------------------------------------------------
@dataclass
class Controls:
    """Целевые величины. Всё — абсолютные значения, не дельты."""
    roll: float = 0.0                    # град, + = правое крыло вниз
    pitch: float = 0.0                   # град, − = нос вверх (соглашение MC)
    heading: Optional[float] = None      # град: автопилот по курсу
    yaw_rate: float = 0.0                # град/с: прямое рысканье
    throttle: float = 0.7                # 0..1

    def clamped(self, spec: UnitSpec) -> "Controls":
        return Controls(
            roll=max(-spec.max_roll, min(spec.max_roll, self.roll)),
            pitch=max(-spec.max_pitch, min(spec.max_pitch, self.pitch)),
            heading=None if self.heading is None else self.heading % 360.0,
            yaw_rate=max(-spec.turn_rate, min(spec.turn_rate, self.yaw_rate)),
            throttle=max(0.0, min(1.0, self.throttle)),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {"roll": self.roll, "pitch": self.pitch,
                "heading": self.heading, "yaw_rate": self.yaw_rate,
                "throttle": self.throttle}


# ---------------------------------------------------------------------------
#  Базовый юнит
# ---------------------------------------------------------------------------
class Unit:
    """Общий контракт для всей техники.

    Все типы обязаны иметь одинаковый набор полей (`fuel`, `g_load`, `stalled`,
    `ammo_total`, ...), чтобы общий код не проверял их через `hasattr`
    (закрывает UNIT-17).
    """

    SPEC: UnitSpec = UnitSpec()
    LOADOUT: Loadout = ()
    AVAILABLE: Dict[str, Sequence[str]] = {}

    def __init__(self, tag: str, is_bot: bool = False,
                 spec: Optional[UnitSpec] = None,
                 blueprint: Optional[str] = None):
        self.id: Optional[int] = None
        self.tag = tag
        self.is_bot = is_bot
        self.faction = "red" if is_bot else "blue"
        self.base_id: Optional[int] = None
        self.spec: UnitSpec = spec or self.SPEC

        # --- состояние ---
        self.pos: Vec3 = (0.0, self.spec.min_altitude, 0.0)
        self.yaw = 0.0
        self.pitch = 0.0
        self.roll = 0.0
        self.speed = self.spec.cruise_speed
        self.alive = True
        self.status = "idle"
        self.crashed_reason: str = ""

        # --- управление ---
        self.controls = Controls(throttle=0.7 if not self.spec.ground_unit else 0.0)
        self._lock = threading.RLock()

        # --- ресурс и живучесть ---
        self.fuel = self.spec.fuel_max
        self.cargo = 0.0
        self.health = self.spec.health
        self.max_health = self.spec.health
        self.last_damage_at = 0.0
        self.last_damage_source = ""
        self.kills = 0
        self.g_load = 1.0
        self.stalled = False
        self.airborne = not self.spec.ground_unit
        self.vs = 0.0                 # вертикальная скорость, м/с (+ вверх)
        self.burning = False          # горит после тяжёлых повреждений
        #: режим службы: 'park' («Стоянка») или 'combat' («В бой») — UX-15
        self.duty = "combat"
        self.flight_time = 0.0
        self.distance_flown = 0.0

        # --- оружие ---
        self.mounts: List[WeaponMount] = build_mounts(self.LOADOUT)

        # --- модель ---
        self.model = BlockModel(get_blueprint(blueprint or self.spec.blueprint))
        self._last_model_sync: Optional[Tuple[float, float, float, float]] = None

    # ------------------------------------------------------------ управление
    def set_target(self, roll: Optional[float] = None, pitch: Optional[float] = None,
                   heading: Optional[float] = None, yaw_rate: Optional[float] = None,
                   throttle: Optional[float] = None, speed: Optional[float] = None
                   ) -> None:
        """Задать целевые величины (абсолютные). `None` — не трогать."""
        with self._lock:
            c = self.controls
            if roll is not None:
                c.roll = roll
            if pitch is not None:
                c.pitch = pitch
            if heading is not None:
                c.heading = heading % 360.0
            if yaw_rate is not None:
                c.yaw_rate = yaw_rate
            if throttle is not None:
                c.throttle = max(0.0, min(1.0, throttle))
            if speed is not None:
                # Прямое задание скорости: подбираем тягу (для вертолётных).
                self.speed = max(0.0, min(self.spec.max_speed, speed))
                if self.spec.can_hover and self.spec.max_speed > 0:
                    c.throttle = 0.0

    def trim(self, droll: float = 0.0, dpitch: float = 0.0, dyaw: float = 0.0,
             dthrottle: float = 0.0) -> None:
        """Сдвинуть целевые величины — ручной режим управления из UI."""
        with self._lock:
            c = self.controls
            c.roll += droll
            c.pitch += dpitch
            c.yaw_rate += dyaw
            c.throttle = max(0.0, min(1.0, c.throttle + dthrottle))
            if droll or dpitch:
                c.heading = None          # ручное управление снимает автопилот

    def level(self) -> None:
        """Выровнять: крен/тангаж в ноль, курс держать текущий."""
        with self._lock:
            self.controls.roll = 0.0
            self.controls.pitch = 0.0
            self.controls.yaw_rate = 0.0

    def controls_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self.controls.as_dict()

    # --------------------------------------------------------------- векторы
    @staticmethod
    def forward_vector(yaw: float, pitch: float = 0.0) -> Vec3:
        """Вектор «вперёд». yaw 0 = юг (+Z), yaw 90 = запад (−X)."""
        yr, pr = math.radians(yaw), math.radians(pitch)
        return (-math.sin(yr) * math.cos(pr), -math.sin(pr),
                math.cos(yr) * math.cos(pr))

    @staticmethod
    def right_vector(yaw: float) -> Vec3:
        yr = math.radians(yaw)
        return (math.cos(yr), 0.0, math.sin(yr))

    def forward(self) -> Vec3:
        return self.forward_vector(self.yaw, self.pitch)

    def velocity(self) -> Vec3:
        """Скорость в м/с (для баллистики и наследования TNT)."""
        fx, fy, fz = self.forward()
        return (fx * self.speed, fy * self.speed, fz * self.speed)

    def muzzle_pos(self) -> Vec3:
        """Точка вылета снаряда — чуть впереди и ниже центра."""
        fx, fy, fz = self.forward()
        x, y, z = self.pos
        return (x + fx * 3.0, y + fy * 3.0 - 1.0, z + fz * 3.0)

    # --------------------------------------------------------------- физика
    def step(self, dt: float, world: World) -> None:
        """Один тик симуляции. Переопределяется типом техники."""
        raise NotImplementedError

    # ------------------------------------------------------------ ресурс
    def _consume_fuel(self, dt: float, throttle: float) -> None:
        spec = self.spec
        if spec.fuel_max <= 0:
            return
        rate = spec.fuel_idle + (1.0 - spec.fuel_idle) * throttle
        self.fuel = max(0.0, self.fuel - dt * rate)
        if self.fuel <= 0.0:
            with self._lock:
                self.controls.throttle = 0.0
            self.status = "fuel_out" if self.status != "crashed" else self.status

    @property
    def health_pct(self) -> float:
        return 100.0 * self.health / max(1.0, self.max_health)

    def take_damage(self, amount: float, source_kind: str = "",
                    source_id: Optional[int] = None,
                    world: Optional[World] = None) -> bool:
        """Получить урон. Броня гасит часть. Возвращает True, если юнит уничтожен.

        Используется и для попаданий оружия другой техники, и для огня из игры,
        и для столкновений — единая точка, чтобы поражение было одинаковым.
        """
        if not self.alive or amount <= 0:
            return False
        reduction = min(0.8, max(0.0, self.spec.armor / 100.0))
        self.health -= amount * (1.0 - reduction)
        self.last_damage_at = time.time()
        self.last_damage_source = source_kind or "?"
        if self.health <= 0.0:
            self.health = 0.0
            self.destroy(source_kind, source_id, world)
            return True
        if self.health_pct <= BURN_HP_PCT:
            self.burning = True        # дым/огонь в мире и статус «Горит»
        if self.status in ("flying", "idle", "spawned", "moving"):
            self.status = "damaged"
        return False

    def repair(self, amount: Optional[float] = None) -> None:
        self.health = self.max_health if amount is None else \
            min(self.max_health, self.health + amount)
        if self.health_pct > BURN_HP_PCT:
            self.burning = False       # потушили — статус снимается
        if self.health > 0 and self.status == "damaged":
            self.status = "flying" if not self.spec.ground_unit else "idle"

    def destroy(self, source_kind: str = "", source_id: Optional[int] = None,
                world: Optional[World] = None) -> None:
        """Уничтожение: юнит больше не боеспособен, помечается для уборки."""
        if not self.alive:
            return
        self.alive = False
        self.health = 0.0
        self.status = "destroyed"
        self.crashed_reason = (f"сбит: {source_kind}" if source_kind
                               else "уничтожен")
        if world is not None:
            world.add_marker(self.pos[0], self.pos[2], "crash", ttl=180.0,
                             text=f"#{self.id} {self.crashed_reason}")
        log.info("Юнит #%s уничтожен (%s, источник %s)", self.id,
                 self.crashed_reason, source_id if source_id is not None else "-")

    def crash(self, reason: str, world: Optional[World] = None) -> None:
        if not self.alive:
            return
        self.alive = False
        self.status = "crashed"
        self.crashed_reason = reason
        if world is not None:
            world.add_marker(self.pos[0], self.pos[2], "crash", ttl=120.0,
                             text=reason)
        log.info("Юнит #%s (%s) потерян: %s", self.id, self.spec.label, reason)

    # --------------------------------------------------------------- модель
    def model_commands(self, force: bool = False) -> List[str]:
        """Список команд перерисовки модели (дифф — только изменившиеся блоки).

        Пустой список, если позиция и углы не изменились: ноль команд на сервер.
        """
        key = self._model_key()
        if not force and key == self._last_model_sync:
            return []
        writes, clears = self.model.sync(self.pos, self.yaw, self.pitch, self.roll)
        self._last_model_sync = key
        if not writes and not clears:
            return []
        cmds = [mc.setblock(x, y, z, "minecraft:air") for (x, y, z) in clears]
        cmds += [mc.setblock(x, y, z, block) for (x, y, z), block in writes]
        return cmds

    def _model_key(self) -> Tuple[int, int, int, int, int]:
        """Ключ кадра модели в ЦЕЛОЧИСЛЕННЫХ блоках.

        Блоки ставятся в целые координаты, поэтому пока юнит не сдвинулся
        хотя бы на полблока и не повернулся на 4°, раскладка клеток не меняется —
        проекцию можно не пересчитывать вовсе.
        """
        return (int(round(self.pos[0])), int(round(self.pos[1])),
                int(round(self.pos[2])), int(round(self.yaw / 4.0)),
                int(round(self.pitch / 4.0)))

    def _cell_key(self, x: int, y: int, z: int) -> str:
        return f"m{self.id}:{x},{y},{z}"

    def sync_model(self, queue: CommandQueue, force: bool = False,
                   priority: int = Priority.VISUAL) -> int:
        """Отправить перерисовку в очередь. Возвращает число поставленных команд.

        Ключ слияния — **позиция блока**, а не юнит: если команда для той же
        клетки ещё не ушла на сервер, она отбрасывается в пользу новой. Рисовать
        промежуточные кадры бессмысленно, а при быстрой перерисовке это
        главное средство снижения нагрузки. Общий ключ на юнит был бы
        ошибкой: от залпа блоков остался бы один.
        """
        key0 = self._model_key()
        if not force and key0 == self._last_model_sync:
            return 0
        writes, clears = self.model.sync(self.pos, self.yaw, self.pitch, self.roll)
        self._last_model_sync = key0
        sent = 0
        for (x, y, z) in clears:
            queue.submit(mc.setblock(x, y, z, "minecraft:air"), priority,
                         key=self._cell_key(x, y, z))
            sent += 1
        for (x, y, z), block in writes:
            queue.submit(mc.setblock(x, y, z, block), priority,
                         key=self._cell_key(x, y, z))
            sent += 1
        return sent

    def despawn_commands(self) -> List[str]:
        """Убрать модель целиком. Чужие блоки не трогаются (UNIT-12)."""
        return [mc.setblock(x, y, z, "minecraft:air")
                for (x, y, z) in self.model.clear()]

    def despawn(self, queue: CommandQueue) -> int:
        """Снять модель, используя те же ключи слияния, что и при отрисовке.

        Это критично: снятие идёт приоритетом CRITICAL, а отрисовка — VISUAL.
        Без общего ключа команда «убрать блок» обгоняла в очереди ещё не
        отправленную команду «поставить блок» для той же клетки, и блок
        оставался в мире навсегда (инверсия приоритетов).
        """
        sent = 0
        for (x, y, z) in self.model.clear():
            queue.submit(mc.setblock(x, y, z, "minecraft:air"), Priority.CRITICAL,
                         key=self._cell_key(x, y, z))
            sent += 1
        return sent

    # ------------------------------------------------------------- служебное
    @property
    def ammo_total(self) -> int:
        return sum(m.ammo for m in self.mounts)

    @property
    def ammo_max(self) -> int:
        return sum(m.ammo_max for m in self.mounts)

    def mounts_by_category(self, category: str) -> List[WeaponMount]:
        return [m for m in self.mounts if m.category == category]

    def altitude_agl(self, world: World) -> float:
        """Высота над уровнем земли (по данным сканера)."""
        h = world.terrain.height_at(self.pos[0], self.pos[2])
        ground = float(h) if h is not None else SEA_LEVEL
        return self.pos[1] - ground

    def telemetry(self) -> Dict[str, Any]:
        return {
            "speed": round(self.speed, 1),
            "altitude": round(self.pos[1], 1),
            "heading": round(self.yaw % 360.0, 1),
            "pitch": round(self.pitch, 1),
            "roll": round(self.roll, 1),
            "g_load": round(self.g_load, 2),
            "stalled": self.stalled,
            "fuel": round(self.fuel, 1),
            "fuel_pct": round(100.0 * self.fuel / max(1.0, self.spec.fuel_max), 1),
            "ammo": self.ammo_total, "ammo_max": self.ammo_max,
            "status": self.status, "alive": self.alive,
            "flight_time": round(self.flight_time, 1),
            "distance": round(self.distance_flown, 1),
            "vs": round(self.vs, 1),
        }

    def snapshot(self) -> Dict[str, Any]:
        """Снимок для UI. Все структуры — копии (контракт `World.snapshot`)."""
        return {
            "id": self.id, "kind": self.spec.kind, "label": self.spec.label,
            "tag": self.tag, "is_bot": self.is_bot, "blueprint": self.spec.blueprint,
            "ground": bool(self.spec.ground_unit), "base_id": self.base_id,
            "burning": bool(self.burning), "kills": self.kills,
            "duty": self.duty,
            "pos": tuple(self.pos), "yaw": self.yaw, "pitch": self.pitch,
            "roll": self.roll, "speed": self.speed,
            "status": self.status, "alive": self.alive,
            "health": round(self.health, 1),
            "health_pct": round(self.health_pct, 1),
            "armor": self.spec.armor,
            "cargo": round(self.cargo, 1),
            "cargo_max": self.spec.cargo_max,
            "controls": self.controls_snapshot(),
            "mounts": [m.snapshot() for m in self.mounts],
            **self.telemetry(),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<{type(self).__name__} #{self.id} {self.spec.label} "
                f"{self.status} {'bot' if self.is_bot else 'player'}>")


# ---------------------------------------------------------------------------
#  Самолёт
# ---------------------------------------------------------------------------
class Aircraft(Unit):
    """Аэродинамическая модель: вираж креном, сваливание, перегрузка, топливо."""

    def step(self, dt: float, world: World) -> None:
        if not self.alive or dt <= 0:
            return
        spec = self.spec
        with self._lock:
            target = self.controls.clamped(spec)

        v = max(1.0, self.speed)
        # В сваливании эффективность рулей падает (UNIT-09)
        authority = 0.35 if self.stalled else 1.0

        # --- 1. Целевое положение рулей ---------------------------------
        # Важно: цель по крену определяется ЕДИНООБРАЗНО и применяется один
        # раз. В наброске ручной крен и автопилот курса правили `self.roll`
        # по очереди в пределах одного тика и гасили друг друга.
        want_roll = target.roll
        want_pitch = target.pitch
        if self.stalled:
            # Нос самопроизвольно опускается — так сваливание можно вывести
            want_pitch = max(target.pitch, 12.0)

        if target.heading is not None:
            # Автопилот физический, а не пропорциональный по крену:
            #   ошибка курса -> желаемая угловая скорость -> требуемый крен.
            # Крен, дающий ω при скорости V: φ = atan(ω·V/g). Сходится без
            # раскачки и без подбора коэффициента демпфирования.
            err = _angle_diff(target.heading, self.yaw)
            omega_des = max(-spec.heading_max_rate,
                            min(spec.heading_max_rate,
                                err * spec.heading_rate_gain))
            phi_des = math.degrees(math.atan(math.radians(omega_des) * v / GRAVITY))
            want_roll = max(-spec.max_roll, min(spec.max_roll, phi_des))

        self.pitch = _approach(self.pitch, want_pitch, spec.pitch_rate * authority, dt)
        self.roll = _approach(self.roll, want_roll, spec.roll_rate * authority, dt)

        # --- 2. Рысканье -------------------------------------------------
        if target.heading is None and abs(target.yaw_rate) > 1e-6:
            self.yaw = (self.yaw + target.yaw_rate * dt) % 360.0

        # --- 3. Согласованный вираж и перегрузка -------------------------
        # n = 1/cos(крен); ω = g·tan(крен)/V. Отсюда же реальная перегрузка
        # и её ограничение по паспорту планера (UNIT-10).
        roll_rad = math.radians(self.roll)
        cos_r = max(0.2, math.cos(roll_rad))
        n_load = 1.0 / cos_r
        if n_load > spec.max_g:
            max_bank = math.degrees(math.acos(1.0 / spec.max_g))
            self.roll = math.copysign(min(abs(self.roll), max_bank), self.roll)
            roll_rad = math.radians(self.roll)
            n_load = spec.max_g
        self.g_load = n_load
        omega = math.degrees(GRAVITY * math.tan(roll_rad) / v)     # град/с
        self.yaw = (self.yaw + omega * dt) % 360.0

        # --- 4. Скорость: тяга + составляющая силы тяжести ---------------
        # Ограничение по `accel` (м/с²) делает разгон инерционным и
        # независимым от частоты тика (UNIT-03).
        thrust_speed = (spec.idle_speed
                        + (spec.max_speed - spec.idle_speed) * target.throttle)
        dv = max(-spec.accel * dt, min(spec.accel * dt, thrust_speed - self.speed))
        dv += -GRAVITY * math.sin(math.radians(self.pitch)) * dt
        self.speed = max(0.0, min(spec.max_speed * 1.35, self.speed + dv))

        # --- 5. Сваливание ----------------------------------------------
        self.stalled = self.speed < spec.stall_speed

        # --- 6. Интегрирование позиции ----------------------------------
        fx, fy, fz = self.forward_vector(self.yaw, self.pitch)
        sink = (4.0 + (spec.stall_speed - self.speed) * 1.5) if self.stalled else 0.0
        move = self.speed * dt
        new_x = self.pos[0] + fx * move
        new_z = self.pos[2] + fz * move
        new_y = self.pos[1] + fy * move - sink * dt
        self.distance_flown += math.hypot(fx * move, fz * move)
        self.flight_time += dt
        self.vs = (new_y - self.pos[1]) / max(1e-6, dt)

        # --- 7. Потолок, эксплуатационный минимум и земля ----------------
        ground = world.terrain.height_at(new_x, new_z)
        ground_y = (float(ground) + 1.0) if ground is not None else SEA_LEVEL
        if new_y > spec.service_ceiling:
            new_y = spec.service_ceiling
            self.pitch = max(self.pitch, 0.0)
        if new_y <= ground_y:
            self.pos = (new_x, ground_y, new_z)
            self.crash("столкновение с землёй", world)
            return
        if new_y < spec.min_altitude:
            # Ниже эксплуатационного минимума — не авария, а выравнивание:
            # ИИ не должно «зарываться» в землю на выходе из пикирования.
            new_y = spec.min_altitude
            self.pitch = min(self.pitch, 0.0)
            if self.status == "flying":
                self.status = "low_altitude"
        self.pos = (new_x, new_y, new_z)
        self._update_status()

        # --- 8. Топливо ---------------------------------------------------
        self._consume_fuel(dt, target.throttle)

    def _update_status(self) -> None:
        """Статус отражает самое важное текущее состояние."""
        if not self.alive:
            self.status = "crashed"
        elif self.stalled:
            self.status = "stall"
        elif self.g_load >= self.spec.max_g - 1e-3:
            self.status = "g_limit"
        elif self.fuel <= 0.0:
            self.status = "fuel_out"
        else:
            self.status = "flying"


# ---------------------------------------------------------------------------
#  Вертолёт
# ---------------------------------------------------------------------------
class Helicopter(Unit):
    """Несущий винт: шаг/крен дают скорость, РУВ (throttle) — вертикальную.

    `throttle = hover_throttle` — висение; больше — набор высоты, меньше —
    снижение. Тангаж > 0 (нос вниз) — движение вперёд (UNIT-19: реальное
    зависание вместо отсутствующего).
    """

    def step(self, dt: float, world: World) -> None:
        if not self.alive or dt <= 0:
            return
        spec = self.spec
        with self._lock:
            target = self.controls.clamped(spec)

        authority = 1.0 if self.fuel > 0 else 0.0
        self.pitch = _approach(self.pitch, target.pitch, spec.pitch_rate * authority, dt)
        self.roll = _approach(self.roll, target.roll, spec.roll_rate * authority, dt)

        # --- рысканье: автопилот по курсу или прямая команда
        if target.heading is not None:
            err = _angle_diff(target.heading, self.yaw)
            rate = max(-spec.turn_rate, min(spec.turn_rate, err * 2.0))
            self.yaw = (self.yaw + rate * dt) % 360.0
        elif abs(target.yaw_rate) > 1e-6:
            self.yaw = (self.yaw + target.yaw_rate * dt) % 360.0

        # --- скорость: продольная от тангажа, поперечная от крена
        fwd_frac = self.pitch / spec.max_pitch if spec.max_pitch else 0.0
        lat_frac = self.roll / spec.max_roll if spec.max_roll else 0.0
        v_fwd = spec.max_speed * max(-0.5, min(1.0, fwd_frac))
        v_lat = spec.max_speed * 0.7 * max(-1.0, min(1.0, lat_frac))
        self.speed = math.hypot(v_fwd, v_lat)

        v_up = (target.throttle - spec.hover_throttle) * 2.0 * spec.climb_rate
        self.g_load = 1.0 + v_up * 0.05
        self.stalled = False

        yr = math.radians(self.yaw)
        fx, fz = -math.sin(yr), math.cos(yr)
        rx, rz = math.cos(yr), math.sin(yr)
        dx = (fx * v_fwd + rx * v_lat) * dt
        dz = (fz * v_fwd + rz * v_lat) * dt
        dy = v_up * dt
        self.vs = v_up
        self.distance_flown += math.hypot(dx, dz)
        self.flight_time += dt

        new_x, new_z = self.pos[0] + dx, self.pos[2] + dz
        new_y = self.pos[1] + dy
        ground = world.terrain.height_at(new_x, new_z)
        ground_y = (float(ground) + 1.0) if ground is not None else SEA_LEVEL
        if new_y < spec.min_altitude and new_y > ground_y:
            new_y = spec.min_altitude
            v_up = max(0.0, v_up)
        if new_y <= ground_y:
            new_y = ground_y
            sink_rate = -min(0.0, v_up)          # запоминаем ДО обнуления
            self.pos = (new_x, new_y, new_z)
            if self.speed > spec.max_speed * 0.7 or sink_rate > spec.climb_rate * 0.7:
                self.crash("жёсткая посадка", world)
                return
            self.status = "landed"
        else:
            self.status = "flying"
        if new_y > spec.service_ceiling:
            new_y = spec.service_ceiling
        self.pos = (new_x, new_y, new_z)
        # Висение — не «нулевой расход»: несущий винт работает постоянно.
        load = 0.55 + 0.45 * min(1.0, abs(target.throttle - spec.hover_throttle) * 2.0)
        self._consume_fuel(dt, load)
        self._update_status_heli()

    def _update_status_heli(self) -> None:
        if not self.alive:
            self.status = "crashed"
        elif self.fuel <= 0.0:
            self.status = "fuel_out"


# ---------------------------------------------------------------------------
#  БПЛА
# ---------------------------------------------------------------------------
class Drone(Helicopter):
    """БПЛА — та же схема управления, что у вертолёта, но легче и медленнее."""


# ---------------------------------------------------------------------------
#  Наземная техника
# ---------------------------------------------------------------------------
class Tank(Unit):
    """Наземная техника: коллизия с рельефом, уклон, приоритет дорог.

    GROUND-01…04. До переработки машина брала высоту ближайшего тайла и
    ехала дальше, из-за чего **проезжала сквозь блоки**: пологий склон любой
    крутизны считался проходимым, а вода и лес вообще не учитывались. Теперь
    шаг идёт через `GroundNav.advance()`: он проверяет клетку впереди и либо
    двигает машину (с тангажом по реальному уклону), либо сообщает «упёрся».

    Что даёт навигатор:
    * **коллизия** — впереди стена/вода/лава/неподъёмный склон → позиция не
      меняется, машина упирается (статус `stuck`), а не проходит насквозь;
    * **ограничение угла подъёма** — `spec.max_climb_deg` (у танка выше, чем
      у грузовика): сверх предела не едем даже на малом газу;
    * **приоритет дорог и ровных участков** — автопилот/маршрут получает
      скорректированный курс: навигатор перебирает отклонения и выбирает
      минимальную стоимость клетки, где дорога дешевле пересечёнки, а крутизна
      штрафуется квадратично. При РУЧНОМ управлении объезд выключен — оператор
      чувствует упор машины, а не «умное» подруливание.
    """

    #: предельный угол подъёма по умолчанию, град (паспорт может переопределить)
    DEFAULT_MAX_CLIMB = 30.0

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._nav: Optional[Any] = None
        self._nav_terrain: Optional[Any] = None
        self.blocked = False           # упёрся в препятствие на прошлом тике
        self.ground_kind: str = ""     # тип поверхности под машиной (для UI)
        self.slope: float = 0.0        # текущий уклон, град

    # ------------------------------------------------------------- навигатор
    @property
    def max_climb_deg(self) -> float:
        return float(getattr(self.spec, "max_climb_deg", self.DEFAULT_MAX_CLIMB))

    def nav(self, world: World):
        """Ленивый `GroundNav` на рельефе мира (один на юнит, кэшится)."""
        terrain = getattr(world, "terrain", None)
        if self._nav is None or self._nav_terrain is not terrain:
            from .groundnav import GroundNav
            self._nav = GroundNav(terrain,
                                  step=int(getattr(terrain, "step", 8) or 8),
                                  max_climb_deg=self.max_climb_deg)
            self._nav_terrain = terrain
        return self._nav

    def step(self, dt: float, world: World) -> None:
        if not self.alive or dt <= 0:
            return
        spec = self.spec
        with self._lock:
            target = self.controls.clamped(spec)

        nav = self.nav(world)

        # --- поворот: желаемый курс корректируется рельефом ----------------
        want_yaw = self.yaw
        if target.heading is not None:
            # Автопилот: навигатор может увести курс в сторону, если прямо по
            # желаемому направлению стена/вода/круто. Это и есть «приоритет
            # дорог и ровных участков» — колонна сама выходит на дорогу.
            course, _cost, blocked = nav.choose_heading(self.pos[0],
                                                        self.pos[2],
                                                        target.heading)
            if not blocked:
                want_yaw = course
            err = _angle_diff(want_yaw, self.yaw)
            rate = max(-spec.turn_rate, min(spec.turn_rate, err * 3.0))
        else:
            rate = target.yaw_rate
        self.yaw = (self.yaw + rate * dt) % 360.0

        # --- скорость ------------------------------------------------------
        want = spec.max_speed * target.throttle
        # поверхность тормозит: песок/снег/лес медленнее асфальта
        _y_here, kind_here = nav.height_kind(self.pos[0], self.pos[2])
        self.ground_kind = kind_here or ""
        want *= SURFACE_SPEED.get(self.ground_kind, 1.0)
        self.speed = _approach(self.speed, want, spec.accel, dt)
        self.g_load = 1.0
        self.stalled = False
        self.vs = 0.0

        # --- шаг с коллизией ----------------------------------------------
        step_m = self.speed * dt
        nx, ny, nz, course, pitch, moved = nav.advance(
            self.pos[0], self.pos[1], self.pos[2], self.yaw, step_m,
            auto_detour=target.heading is not None)

        here_y, _k0 = nav.height_kind(self.pos[0], self.pos[2])
        if moved:
            self.distance_flown += math.hypot(nx - self.pos[0],
                                              nz - self.pos[2])
            self.flight_time += dt
            self.pos = (nx, ny, nz)
            self.pitch = _approach(self.pitch, pitch, 40.0, dt)
            self.slope = -pitch
            self.blocked = False
            self.roll = _approach(self.roll, 0.0, 30.0, dt)
            self._consume_fuel(dt, target.throttle)
            if self.status in ("stuck",):
                self.status = "moving"
            if self.status != "stuck":
                self.status = "moving" if self.speed > 0.5 else "idle"
            return

        # --- упёрся: коллизия. Позиция НЕ меняется (не проезжаем сквозь) ---
        self.speed = 0.0
        self.blocked = True
        self.pitch = _approach(self.pitch, 0.0, 40.0, dt)
        if self.status not in ("crashed", "destroyed"):
            self.status = "stuck"
        self._consume_fuel(dt, 0.0)


#: как поверхность влияет на скорость наземной техники (GROUND-04:
#: дорога быстрее пересечёнки — поэтому автопилоту выгодно выходить на неё)
SURFACE_SPEED: Dict[str, float] = {
    "road": 1.15,
    "stone": 1.0,
    "gravel": 0.95,
    "dirt": 0.9,
    "grass": 0.88,
    "other": 0.85,
    "sand": 0.62,
    "snow": 0.55,
    "leaves": 0.4,
    "log": 0.35,
}


# ---------------------------------------------------------------------------
#  Вспомогательные функции
# ---------------------------------------------------------------------------
def _approach(current: float, target: float, rate: float, dt: float) -> float:
    """Привести значение к цели с ограниченной скоростью изменения."""
    delta = target - current
    limit = rate * dt
    if abs(delta) <= limit:
        return target
    return current + math.copysign(limit, delta)


def _angle_diff(target: float, current: float) -> float:
    """Разница курсов в пределах −180..+180 (знак = куда крутить)."""
    return (target - current + 540.0) % 360.0 - 180.0


# ---------------------------------------------------------------------------
#  Варианты техники
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UnitVariant:
    """Вариант техники: паспорт, загрузка, доступное вооружение, описание."""
    key: str
    kind: str
    label: str
    description: str
    spec: UnitSpec
    loadout: Loadout
    available: Dict[str, Sequence[str]]
    blueprint: str

    def make_spec(self, **changes: Any) -> UnitSpec:
        return self.spec.copy(**changes)


def _v(key: str, kind: str, label: str, description: str, spec: UnitSpec,
       loadout: Loadout, available: Dict[str, Sequence[str]]) -> UnitVariant:
    return UnitVariant(key, kind, label, description, spec, loadout, available,
                       spec.blueprint)


VARIANTS: Dict[str, UnitVariant] = {
    # ---------------------------------------------------------------- самолёты
    "attacker": _v(
        "attacker", "aircraft", "Штурмовик Су-25",
        "Низковысотный, тяжёлое вооружение, перегрузка до 4.5",
        UnitSpec(kind="aircraft", label="Су-25", blueprint="su25",
                 max_speed=42.0, min_speed=13.0, cruise_speed=26.0, accel=7.0,
                 max_altitude=250.0, min_altitude=15.0, service_ceiling=280.0,
                 max_pitch=50.0, max_roll=60.0, pitch_rate=28.0, roll_rate=55.0,
                 stall_speed=9.0, max_g=4.5, fuel_max=240.0,
                 health=120.0, armor=20.0),
        [("bomb", "Бомбоотсек", "fab500"),
         ("rocket", "Левое крыло", "s8"),
         ("rocket", "Правое крыло", "s8"),
         ("missile", "ПТУР левый", "agm"),
         ("missile", "ПТУР правый", "agm"),
         ("cannon", "Курсовая ГШ-23", "gsh23")],
        {"bomb": ["fab100", "fab500", "fab1500", "fab5000", "kab500"],
         "rocket": ["s5", "s8", "s13", "s25"],
         "missile": ["agm", "kh29", "vikhr"],
         "cannon": ["gsh23", "2a42"]}),

    "fighter": _v(
        "fighter", "aircraft", "Истребитель МиГ-29",
        "Скоростной и высотный, воздух-воздух, перегрузка до 9",
        UnitSpec(kind="aircraft", label="МиГ-29", blueprint="mig29",
                 max_speed=68.0, min_speed=18.0, cruise_speed=40.0, accel=11.0,
                 max_altitude=320.0, min_altitude=40.0, service_ceiling=380.0,
                 max_pitch=70.0, max_roll=75.0, pitch_rate=40.0, roll_rate=90.0,
                 stall_speed=13.0, max_g=9.0, fuel_max=200.0,
                 health=90.0, armor=5.0),
        [("missile", "Левое крыло", "r73"),
         ("missile", "Правое крыло", "r73"),
         ("missile", "Левый подкрылок", "r27"),
         ("missile", "Правый подкрылок", "r27"),
         ("cannon", "Курсовая ГШ-23", "gsh23")],
        {"missile": ["r73", "r27", "agm"],
         "cannon": ["gsh23", "m61"]}),

    "bomber": _v(
        "bomber", "aircraft", "Бомбардировщик Ту-95",
        "Высотный, много бомб, перегрузка до 2.5",
        UnitSpec(kind="aircraft", label="Ту-95", blueprint="tu95",
                 max_speed=34.0, min_speed=14.0, cruise_speed=24.0, accel=3.5,
                 max_altitude=300.0, min_altitude=80.0, service_ceiling=340.0,
                 max_pitch=25.0, max_roll=35.0, pitch_rate=12.0, roll_rate=22.0,
                 stall_speed=10.0, max_g=2.5, fuel_max=420.0,
                 health=160.0, armor=10.0),
        [("bomb", "Отсек №1", "fab1500"),
         ("bomb", "Отсек №2", "fab1500"),
         ("bomb", "Отсек №3", "fab500"),
         ("bomb", "Отсек №4", "fab500"),
         ("bomb", "Отсек №5", "fab5000"),
         ("bomb", "Отсек №6", "nuke"),
         ("mg", "Кормовая установка", "m2")],
        {"bomb": ["fab100", "fab500", "fab1500", "fab5000", "nuke"],
         "mg": ["m2", "pkm"]}),

    # -------------------------------------------------------------- вертолёты
    "attack_heli": _v(
        "attack_heli", "helicopter", "Вертолёт Ка-52",
        "Висение, НАР и ПТУР, пушка в носу",
        UnitSpec(kind="helicopter", label="Ка-52", blueprint="ka52",
                 max_speed=28.0, min_speed=0.0, cruise_speed=18.0, accel=6.0,
                 max_altitude=220.0, min_altitude=5.0, service_ceiling=260.0,
                 can_hover=True, max_pitch=20.0, max_roll=25.0,
                 pitch_rate=18.0, roll_rate=22.0, yaw_rate=45.0, turn_rate=45.0,
                 climb_rate=10.0, fuel_max=300.0, waypoint_radius=12.0,
                 health=110.0, armor=15.0),
        [("rocket", "Левый блок", "s8"),
         ("rocket", "Правый блок", "s8"),
         ("missile", "ПТУР левый", "agm"),
         ("missile", "ПТУР правый", "agm"),
         ("cannon", "Носовая 2А42", "2a42"),
         ("mg", "Пулемёт", "pkm")],
        {"rocket": ["s5", "s8", "s13", "s25"],
         "missile": ["agm", "kh29", "vikhr"],
         "cannon": ["2a42", "gsh23"],
         "mg": ["pkm", "yakt", "ags17"]}),

    # ------------------------------------------------------------------ БПЛА
    "recon_drone": _v(
        "recon_drone", "drone", "БПЛА Орлан-10",
        "Разведчик: висение, малая заметность, лёгкая подвеска",
        UnitSpec(kind="drone", label="Орлан-10", blueprint="orlan",
                 max_speed=22.0, min_speed=0.0, cruise_speed=14.0, accel=5.0,
                 max_altitude=260.0, min_altitude=10.0, service_ceiling=300.0,
                 can_hover=True, max_pitch=15.0, max_roll=20.0,
                 pitch_rate=14.0, roll_rate=16.0, yaw_rate=60.0, turn_rate=60.0,
                 climb_rate=6.0, fuel_max=600.0, waypoint_radius=8.0,
                 health=60.0, armor=0.0),
        [("bomb", "Подвес 1", "fab100"),
         ("bomb", "Подвес 2", None),
         ("missile", "Подвес 3", "agm")],
        {"bomb": ["fab100"], "missile": ["agm", "vikhr"]}),

    "kamikaze_drone": _v(
        "kamikaze_drone", "drone", "БПЛА Ланцет",
        "Одноразовый: большая БЧ, нет возврата",
        UnitSpec(kind="drone", label="Ланцет", blueprint="orlan",
                 max_speed=34.0, min_speed=0.0, cruise_speed=26.0, accel=9.0,
                 max_altitude=240.0, min_altitude=10.0, service_ceiling=280.0,
                 can_hover=True, max_pitch=25.0, max_roll=30.0,
                 pitch_rate=25.0, roll_rate=30.0, yaw_rate=70.0, turn_rate=70.0,
                 climb_rate=8.0, fuel_max=180.0, waypoint_radius=6.0,
                 health=50.0, armor=0.0),
        [("bomb", "Боевая часть", "fab500")],
        {"bomb": ["fab500", "fab1500"]}),

    "gunship": _v(
        "gunship", "helicopter", "Вертолёт Ми-24",
        "Ударный транспортник: десант, НАР и ПТУР, висение",
        UnitSpec(kind="helicopter", label="Ми-24", blueprint="ka52",
                 max_speed=30.0, min_speed=0.0, cruise_speed=20.0, accel=6.5,
                 max_altitude=230.0, min_altitude=5.0, service_ceiling=270.0,
                 can_hover=True, max_pitch=22.0, max_roll=28.0,
                 pitch_rate=20.0, roll_rate=24.0, yaw_rate=40.0, turn_rate=40.0,
                 climb_rate=9.0, fuel_max=340.0, waypoint_radius=12.0,
                 cargo_max=1.5, health=130.0, armor=20.0),
        [("rocket", "Левый блок", "s8"),
         ("rocket", "Правый блок", "s8"),
         ("missile", "ПТУР левый", "vikhr"),
         ("missile", "ПТУР правый", "vikhr"),
         ("mg", "Носовая установка", "yakt")],
        {"rocket": ["s5", "s8", "s13", "s25"],
         "missile": ["vikhr", "agm"],
         "mg": ["yakt", "pkm"]}),

    # ------------------------------------------------- транспорт и наземные
    "transport": _v(
        "transport", "transport", "Транспортник Ан-26",
        "Военно-транспортный: десант и грузы, сброс с воздуха",
        UnitSpec(kind="transport", label="Ан-26", blueprint="an26",
                 max_speed=32.0, min_speed=13.0, cruise_speed=24.0, accel=4.0,
                 max_altitude=260.0, min_altitude=40.0, service_ceiling=300.0,
                 max_pitch=25.0, max_roll=30.0, pitch_rate=14.0, roll_rate=26.0,
                 stall_speed=11.0, max_g=2.0, fuel_max=380.0, cargo_max=5.0,
                 health=140.0, armor=5.0),
        [("mg", "Кормовая установка", "pkm")],
        {"mg": ["pkm", "m2", "yakt"]}),

    "truck": _v(
        "truck", "truck", "Грузовик Урал-4320",
        "Снабжение: боезапас и топливо колоннам, без вооружения",
        UnitSpec(kind="truck", label="Урал-4320", blueprint="ural",
                 max_speed=12.0, min_speed=0.0, cruise_speed=8.0, accel=2.5,
                 max_altitude=200.0, min_altitude=-60.0, service_ceiling=200.0,
                 ground_unit=True, max_pitch=0.0, max_roll=0.0,
                 pitch_rate=0.0, roll_rate=0.0, turn_rate=28.0,
                 fuel_max=300.0, fuel_idle=0.12, waypoint_radius=5.0,
                 cargo_max=5.0, health=80.0, armor=0.0),
        [],
        {}),

    "apc": _v(
        "apc", "apc", "БТР-82А",
        "Пехота и огневая поддержка: пушка 2А72 и пулемёт",
        UnitSpec(kind="apc", label="БТР-82А", blueprint="btr82",
                 max_speed=16.0, min_speed=0.0, cruise_speed=11.0, accel=3.5,
                 max_altitude=200.0, min_altitude=-60.0, service_ceiling=200.0,
                 ground_unit=True, max_pitch=0.0, max_roll=0.0,
                 pitch_rate=0.0, roll_rate=0.0, turn_rate=45.0,
                 fuel_max=360.0, fuel_idle=0.12, waypoint_radius=5.0,
                 cargo_max=1.0, health=120.0, armor=30.0),
        [("cannon", "Башенка", "2a42"),
         ("mg", "Спаренный", "pkm")],
        {"cannon": ["2a42"], "mg": ["pkm", "m2"]}),

    "ifv": _v(
        "ifv", "apc", "БМП-3",
        "Боевая машина пехоты: пушка 2А70, ПТУР и десант",
        UnitSpec(kind="apc", label="БМП-3", blueprint="btr82",
                 max_speed=18.0, min_speed=0.0, cruise_speed=12.0, accel=4.0,
                 max_altitude=200.0, min_altitude=-60.0, service_ceiling=200.0,
                 ground_unit=True, max_pitch=0.0, max_roll=0.0,
                 pitch_rate=0.0, roll_rate=0.0, turn_rate=42.0,
                 fuel_max=400.0, fuel_idle=0.13, waypoint_radius=5.0,
                 cargo_max=1.0, health=150.0, armor=35.0,
                 max_climb_deg=28.0),
        [("cannon", "Башня", "2a42"),
         ("missile", "ПТУР", "kornet"),
         ("mg", "Спаренный", "pkm")],
        {"cannon": ["2a42"], "missile": ["kornet", "agm"],
         "mg": ["pkm", "m2", "ags17"]}),

    # ------------------------------------------------------------------ танки
    "mbt": _v(
        "mbt", "tank", "Танк Т-72",
        "Основной боевой танк: пушка и пулемёт, привязка к рельефу",
        UnitSpec(kind="tank", label="Т-72", blueprint="t72",
                 max_speed=14.0, min_speed=0.0, cruise_speed=9.0, accel=3.0,
                 max_altitude=200.0, min_altitude=-60.0, service_ceiling=200.0,
                 ground_unit=True, max_pitch=0.0, max_roll=0.0,
                 pitch_rate=0.0, roll_rate=0.0, turn_rate=40.0,
                 fuel_max=480.0, fuel_idle=0.15, waypoint_radius=6.0,
                 health=260.0, armor=55.0),
        [("cannon", "Башня", "2a42"),
         ("mg", "Спаренный", "pkm"),
         ("missile", "ПТУР", "agm")],
        {"cannon": ["2a42", "gsh23"], "mg": ["pkm", "m2", "ags17"],
         "missile": ["agm", "kornet"]}),
}


KIND_CLASSES: Dict[str, type] = {
    "aircraft": Aircraft,
    "helicopter": Helicopter,
    "drone": Drone,
    "tank": Tank,
    "transport": Aircraft,
    "truck": Tank,
    "apc": Tank,
}


def variant_keys(kind: Optional[str] = None) -> List[str]:
    return [k for k, v in VARIANTS.items() if kind is None or v.kind == kind]


def build_unit(variant_key: str, tag: str, is_bot: bool = False,
               pos: Optional[Vec3] = None, heading: Optional[float] = None,
               speed: Optional[float] = None, throttle: Optional[float] = None,
               fuel: Optional[float] = None, faction: Optional[str] = None) -> Unit:
    """Создать юнит по ключу варианта.

    `speed=0` обрабатывается корректно: в наброске было `if speed:`, из-за
    чего нулевая скорость молча игнорировалась (UNIT-16).
    """
    try:
        variant = VARIANTS[variant_key]
    except KeyError:
        raise KeyError(f"Неизвестный вариант {variant_key!r}. "
                       f"Доступны: {', '.join(sorted(VARIANTS))}") from None
    cls = KIND_CLASSES[variant.kind]
    unit = cls(tag, is_bot=is_bot, spec=variant.make_spec())
    unit.LOADOUT = variant.loadout
    unit.AVAILABLE = dict(variant.available)
    unit.mounts = build_mounts(variant.loadout)
    if pos is not None:
        unit.pos = (float(pos[0]), float(pos[1]), float(pos[2]))
    if heading is not None:
        unit.yaw = heading % 360.0
    if speed is not None:
        unit.speed = max(0.0, min(variant.spec.max_speed, float(speed)))
    if throttle is not None:
        unit.controls.throttle = max(0.0, min(1.0, float(throttle)))
    if fuel is not None:
        unit.fuel = max(0.0, float(fuel))
    if faction is not None:
        unit.faction = faction
    unit.status = "spawned"
    return unit
