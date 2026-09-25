"""
ИИ ботов: генерация маршрутов по фазам захода.

Главное отличие от наброска
---------------------------
**AI-01.** `RouteAI.tick()` там вызывал `replan()` КАЖДЫЙ тик и подменял
маршрут новым с `current_idx = 0`. Бот физически не мог пройти дальше первой
точки: маршрут обнулялся быстрее, чем юнит до неё долетал.

Здесь перепланирование **гибридное** (выбор заказчика):

* цель сдвинулась больше чем на `replan_threshold` блоков, **или**
* прошло больше `replan_interval` секунд с последнего пересчёта, **или**
* текущий маршрут завершён.

И главное — при пересчёте точки заменяются через `Route.replace_waypoints()`,
который сохраняет `current_idx` и флаги пройденных точек. Прогресс захода не
теряется.

Фазы захода
-----------
`APPROACH → DIVE/ATTACK → ESCAPE` с собственными высотами и скоростями,
направление захода выбирается по текущему положению юнита (AI-10), чтобы не
разворачивать его «спиной вперёд». После `passes` заходов — возврат на базу
с дозаправкой и перезарядкой (AI-07).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import AppConfig
# П1.1: углы/векторы — только из geometry.py (единственный источник правды).
from .geometry import angle_diff, forward_vec, right_vec, yaw_to  # noqa: F401
from .routes import Action, Route, Waypoint
from .weapons import Target, distance, solve_lead, speed_mps
from .world import World

log = logging.getLogger(__name__)

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


class Phase:
    IDLE = "idle"
    APPROACH = "approach"
    ATTACK = "attack"
    ESCAPE = "escape"
    INTERCEPT = "intercept"
    RTB = "rtb"
    LOITER = "loiter"
    PATROL = "patrol"
    RECON = "recon"
    KAMIKAZE = "kamikaze"

    LABELS = {
        IDLE: "ожидание", APPROACH: "заход", ATTACK: "атака", ESCAPE: "отход",
        INTERCEPT: "перехват", RTB: "возврат на базу", LOITER: "ожидание цели",
        PATROL: "патруль", RECON: "разведка", KAMIKAZE: "таран",
    }


# ---------------------------------------------------------------------------
#  Профили захода
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrikeProfile:
    """Высоты, скорости и геометрия захода."""
    name: str
    stand_off: float = 200.0        # дистанция точки входа от цели
    approach_alt: float = 160.0
    attack_alt: float = 90.0
    escape_alt: float = 210.0
    escape_dist: float = 260.0
    action: str = Action.BOMB
    count: int = 1
    duration: float = 3.0
    passes: int = 3
    pass_mode: str = "fly"
    speed: Optional[float] = None

    #: дистанция, ближе которой заходить бессмысленно (надо отлететь)
    min_stand_off: float = 60.0


PROFILES: Dict[str, StrikeProfile] = {
    "strike": StrikeProfile(
        name="Штурмовик", stand_off=220.0, approach_alt=150.0, attack_alt=80.0,
        escape_alt=200.0, escape_dist=280.0, action=Action.BOMB, count=2,
        passes=3),
    "gun_run": StrikeProfile(
        name="Штурмовик (пушка)", stand_off=180.0, approach_alt=110.0,
        attack_alt=60.0, escape_alt=170.0, escape_dist=240.0,
        action=Action.STRAFE, count=2, duration=3.0, passes=4),
    "rocket_run": StrikeProfile(
        name="Штурмовик (НАР)", stand_off=300.0, approach_alt=140.0,
        attack_alt=110.0, escape_alt=200.0, escape_dist=300.0,
        action=Action.MISSILE, count=2, passes=3),
    "bomber": StrikeProfile(
        name="Бомбардировщик", stand_off=320.0, approach_alt=240.0,
        attack_alt=210.0, escape_alt=250.0, escape_dist=380.0,
        action=Action.BOMB, count=3, passes=2),
    "heli": StrikeProfile(
        name="Вертолёт", stand_off=160.0, approach_alt=70.0, attack_alt=45.0,
        escape_alt=90.0, escape_dist=180.0, action=Action.STRAFE, count=2,
        duration=4.0, passes=4, pass_mode="precise"),
    "drone": StrikeProfile(
        name="БПЛА", stand_off=140.0, approach_alt=120.0, attack_alt=90.0,
        escape_alt=150.0, escape_dist=160.0, action=Action.MISSILE, count=1,
        passes=3, pass_mode="precise"),
}


# ---------------------------------------------------------------------------
#  Базовый ИИ
# ---------------------------------------------------------------------------
class BaseAI:
    """Общая логика: выбор цели, гибридный перепланировщик, обслуживание."""

    name = "base"
    phase = Phase.IDLE

    def __init__(self, cfg: Optional[AppConfig] = None, target_name: str = "",
                 replan_threshold: float = 25.0, replan_interval: float = 2.0,
                 fuel_reserve: float = 0.18, auto_relaunch: bool = True):
        self.cfg = cfg or AppConfig()
        self.target_name = target_name
        self.replan_threshold = max(1.0, replan_threshold)
        self.replan_interval = max(0.1, replan_interval)
        self.fuel_reserve = fuel_reserve
        self.auto_relaunch = auto_relaunch

        self._plan_pos: Optional[Vec3] = None
        self._plan_time = 0.0
        self.pass_index = 0
        self.replans = 0
        self.reason = ""

    # ------------------------------------------------------------ контракт
    def build(self, unit, world: World) -> Optional[Route]:
        """Построить новый маршрут. None — строить нечего."""
        raise NotImplementedError

    def update(self, unit, world: World, dt: float) -> Optional[Route]:
        """Вызывается движком каждый тик.

        Возвращает НОВЫЙ маршрут, если его нужно назначить, или `None` —
        тогда текущий маршрут остаётся в силе (прогресс не сбрасывается).
        """
        if not unit.alive:
            return None
        now = time.monotonic()
        route = world.get_route(unit.id)
        if route is not None and not route.done and not self.should_replan(unit, world, now):
            return None

        fresh = self.build(unit, world)
        if fresh is None:
            return None
        self.replans += 1
        self._plan_time = now
        tgt = self.current_target_pos(unit, world)
        self._plan_pos = tgt

        if route is not None and not route.done and len(fresh) >= len(route):
            # Обновляем точки на месте — current_idx и флаги сохраняются
            route.replace_waypoints(fresh.waypoints)
            route.name = fresh.name
            return None
        fresh.unit_id = unit.id
        fresh.owner_kind = "bot"
        return fresh

    def should_replan(self, unit, world: World, now: float) -> bool:
        """Гибрид: порог смещения цели ИЛИ истёкший интервал."""
        if self._plan_pos is None:
            return True
        if now - self._plan_time >= self.replan_interval:
            return True
        tgt = self.current_target_pos(unit, world)
        if tgt is None:
            return False
        return distance(tgt, self._plan_pos) >= self.replan_threshold

    def on_route_finished(self, unit, world: World) -> None:
        """Маршрут завершён: следующий заход или возврат на базу."""
        self.pass_index += 1

    # --------------------------------------------------------------- цели
    def pick_target(self, unit, world: World) -> Optional[Target]:
        """Назначенная цель, иначе ближайший игрок (AI-11)."""
        players = world.get_players()
        if not players:
            self.target_name = ""
            return None
        if self.target_name and self.target_name in players:
            rec = players[self.target_name]
            return Target(pos=rec["pos"], vel=rec.get("vel", (0, 0, 0)),
                          name=self.target_name)
        # Назначенная цель пропала — берём ближайшую и запоминаем её
        best_name, best = None, None
        best_d = float("inf")
        for name, rec in players.items():
            d = distance(unit.pos, rec["pos"])
            if d < best_d:
                best_d, best_name, best = d, name, rec
        if best_name is None or best is None:
            return None
        if self.target_name and self.target_name != best_name:
            log.info("Бот #%s: цель %s пропала, переключаюсь на %s", unit.id,
                     self.target_name, best_name)
        self.target_name = best_name
        return Target(pos=best["pos"], vel=best.get("vel", (0, 0, 0)),
                      name=best_name)

    def current_target_pos(self, unit, world: World) -> Optional[Vec3]:
        t = self.pick_target(unit, world)
        return t.pos if t else None

    # ---------------------------------------------------------- обслуживание
    def needs_service(self, unit) -> bool:
        """Топливо на исходе или нечем воевать (AI-07)."""
        if unit.spec.fuel_max > 0 and unit.fuel <= unit.spec.fuel_max * self.fuel_reserve:
            self.reason = f"топливо {unit.fuel:.0f}/{unit.spec.fuel_max:.0f}"
            return True
        if not self._has_ammo(unit):
            self.reason = "нет боезапаса"
            return True
        return False

    def _has_ammo(self, unit) -> bool:
        cats = self._used_categories()
        if not cats:
            return True
        return any(m.category in cats and m.ammo > 0 for m in unit.mounts)

    def _used_categories(self) -> Tuple[str, ...]:
        return ()

    # ------------------------------------------------------- типовые маршруты
    def rtb_route(self, unit, world: World) -> Optional[Route]:
        base = world.base or world.launch_point
        if base is None:
            return None
        spec = unit.spec
        alt = 60.0 if spec.can_hover else max(120.0, spec.min_altitude + 60.0)
        # 'auto': самолёт пролетает над базой (точное зависание ему недоступно),
        # вертолёт и танк выходят в точку точно.
        wp = Waypoint(x=base[0], z=base[1], altitude=alt, action=Action.RTB,
                      pass_mode="auto",
                      radius=6.0 if spec.ground_unit else 30.0,
                      note="возврат на базу")
        route = Route([wp], unit.id, loop=False, owner_kind="bot",
                      name=f"{self.name}: RTB ({self.reason})")
        self.phase = Phase.RTB
        return route

    def loiter_route(self, unit, world: World) -> Optional[Route]:
        """Целей нет — кружим на месте, ждём появления."""
        x, _y, z = unit.pos
        alt = max(unit.spec.min_altitude + 20.0, unit.pos[1])
        wps = [Waypoint(x=x + 120.0, z=z, altitude=alt, action=Action.HOLD,
                        duration=6.0, pass_mode="fly"),
               Waypoint(x=x, z=z + 120.0, altitude=alt, action=Action.HOLD,
                        duration=6.0, pass_mode="fly")]
        self.phase = Phase.LOITER
        return Route(wps, unit.id, loop=True, owner_kind="bot",
                     name=f"{self.name}: ожидание цели")

    def status(self) -> Dict[str, Any]:
        return {
            "ai": self.name, "phase": self.phase,
            "phase_label": Phase.LABELS.get(self.phase, self.phase),
            "target": self.target_name, "pass": self.pass_index,
            "replans": self.replans, "reason": self.reason,
        }


# ---------------------------------------------------------------------------
#  Штурмовой заход по фазам
# ---------------------------------------------------------------------------
class StrikeAI(BaseAI):
    """APPROACH -> ATTACK -> ESCAPE, несколько заходов, затем RTB."""

    name = "strike"

    def __init__(self, profile: Optional[StrikeProfile] = None, **kw: Any):
        super().__init__(**kw)
        self.profile = profile or PROFILES["strike"]
        self.name = f"strike:{self.profile.name}"

    def _used_categories(self) -> Tuple[str, ...]:
        return tuple(Action.CATEGORIES.get(self.profile.action, ()))

    def build(self, unit, world: World) -> Optional[Route]:
        if self.needs_service(unit):
            rtb = self.rtb_route(unit, world)
            if rtb is not None:
                return rtb
            return self.loiter_route(unit, world)

        target = self.pick_target(unit, world)
        if target is None:
            return self.loiter_route(unit, world)

        p = self.profile
        if self.pass_index >= p.passes:
            self.reason = f"выполнено заходов: {self.pass_index}"
            rtb = self.rtb_route(unit, world)
            if rtb is not None:
                self.pass_index = 0
                return rtb
            return self.loiter_route(unit, world)

        tx, _ty, tz = target.pos
        ux, uz = self._approach_direction(unit, (tx, tz))
        stand_off = max(p.min_stand_off, p.stand_off)

        entry = (tx + ux * stand_off, tz + uz * stand_off)
        escape = (tx - ux * p.escape_dist, tz - uz * p.escape_dist)

        wps = [
            # APPROACH: выйти на рубеж захода на высоте подхода
            Waypoint(x=entry[0], z=entry[1], altitude=p.approach_alt,
                     action=Action.NAVIGATE, pass_mode=p.pass_mode,
                     speed=p.speed, note="рубеж захода"),
            # ATTACK: ударная точка на боевой высоте, цель — живой игрок
            Waypoint(x=tx, z=tz, altitude=p.attack_alt, action=p.action,
                     count=p.count, duration=p.duration, pass_mode=p.pass_mode,
                     speed=p.speed, target_name=target.name,
                     note=ACTION_LABELS_SHORT.get(p.action, p.action)),
            # ESCAPE: отход с набором высоты
            Waypoint(x=escape[0], z=escape[1], altitude=p.escape_alt,
                     action=Action.NAVIGATE, pass_mode=p.pass_mode,
                     speed=p.speed, note="отход"),
        ]
        self.phase = Phase.APPROACH
        return Route(wps, unit.id, loop=False, owner_kind="bot",
                     name=f"{self.name}: заход {self.pass_index + 1}/{p.passes}")

    def _approach_direction(self, unit, target: Vec2) -> Vec2:
        """Единичный вектор ОТ цели к рубежу захода (AI-10).

        Обычный случай: юнит далеко от цели — рубеж ставится с его стороны,
        тогда заход идёт практически без разворота.

        Юнит уже над целью: рубеж ставится ВПЕРЕДИ по курсу, на дистанции
        захода. Самолёт пролетает вперёд, доворачивает и возвращается на цель —
        это и есть настоящий повторный заход. Разворот на 180° прямо над целью
        (который получался при векторе против курса) физически неправилен:
        самолёт успевал бы проскочить цель раньше, чем выйдет на боевой курс.
        """
        dx = unit.pos[0] - target[0]
        dz = unit.pos[2] - target[1]
        d = math.hypot(dx, dz)
        if d >= 40.0:
            return (dx / d, dz / d)
        fx, _fy, fz = unit.forward()
        return (fx, fz)

    def on_route_finished(self, unit, world: World) -> None:
        super().on_route_finished(unit, world)
        log.info("Бот #%s: заход %d завершён", unit.id, self.pass_index)


ACTION_LABELS_SHORT = {
    Action.BOMB: "сброс", Action.STRAFE: "обстрел", Action.MISSILE: "пуск УР",
    Action.NAVIGATE: "перелёт", Action.KAMIKAZE: "таран", Action.HOLD: "вираж",
    Action.RECON: "разведка", Action.RTB: "возврат",
}


# ---------------------------------------------------------------------------
#  Истребитель: перехват
# ---------------------------------------------------------------------------
class FighterAI(BaseAI):
    """Перехват с упреждением: точка встречи считается по скорости цели."""

    name = "fighter"
    phase = Phase.INTERCEPT

    def __init__(self, launch_fraction: float = 0.75, **kw: Any):
        super().__init__(**kw)
        self.launch_fraction = max(0.3, min(0.95, launch_fraction))

    def _used_categories(self) -> Tuple[str, ...]:
        return ("missile", "cannon")

    def build(self, unit, world: World) -> Optional[Route]:
        if self.needs_service(unit):
            return self.rtb_route(unit, world) or self.loiter_route(unit, world)
        target = self.pick_target(unit, world)
        if target is None:
            return self.loiter_route(unit, world)

        # Дальность самой дальнобойной УР на подвесе
        ranges = [float(WEAPON_RANGE.get(m.key, 400.0))
                  for m in unit.mounts if m.category == "missile" and m.ammo > 0]
        rng = max(ranges) if ranges else 200.0
        proj_speed = speed_mps(
            next((m.key for m in unit.mounts
                  if m.category == "missile" and m.key), "r73"))

        t = solve_lead(unit.pos, target.pos, target.vel, max(10.0, proj_speed))
        meet = target.predicted(min(t, 10.0))

        # Точка пуска — на доле дальности от точки встречи к юниту
        dx, dz = unit.pos[0] - meet[0], unit.pos[2] - meet[2]
        d = math.hypot(dx, dz) or 1.0
        launch_dist = rng * self.launch_fraction
        launch = (meet[0] + dx / d * launch_dist, meet[2] + dz / d * launch_dist)

        alt = max(unit.spec.min_altitude, meet[1] + 30.0)
        wps = [
            Waypoint(x=launch[0], z=launch[1], altitude=alt,
                     action=Action.MISSILE, count=2, pass_mode="fly",
                     target_name=target.name, note="пуск УР"),
            Waypoint(x=meet[0], z=meet[2], altitude=max(unit.spec.min_altitude,
                                                        meet[1] + 10.0),
                     action=Action.STRAFE, count=1, duration=2.5,
                     pass_mode="fly", target_name=target.name, note="пушка"),
            Waypoint(x=meet[0] + dx / d * 400.0, z=meet[2] + dz / d * 400.0,
                     altitude=alt + 60.0, action=Action.NAVIGATE,
                     pass_mode="fly", note="отход"),
        ]
        self.phase = Phase.INTERCEPT
        return Route(wps, unit.id, loop=False, owner_kind="bot",
                     name=f"fighter: перехват {target.name or 'цели'}")


#: дальности УР (дублируют `WEAPONS[...]['range']`, чтобы ИИ не тянул каталог)
WEAPON_RANGE = {"r73": 400.0, "r27": 800.0, "agm": 600.0, "kh29": 900.0}


# ---------------------------------------------------------------------------
#  Камикадзе
# ---------------------------------------------------------------------------
class KamikazeAI(BaseAI):
    """Один заход: точка-цель с действием KAMIKAZE, без возврата."""

    name = "kamikaze"
    phase = Phase.KAMIKAZE

    def __init__(self, **kw: Any):
        kw.setdefault("auto_relaunch", False)
        super().__init__(**kw)

    def _used_categories(self) -> Tuple[str, ...]:
        return ()

    def needs_service(self, unit) -> bool:
        # Камикадзе одноразовый: топливо не бережём, боезапас не нужен
        return False

    def build(self, unit, world: World) -> Optional[Route]:
        target = self.pick_target(unit, world)
        if target is None:
            return self.loiter_route(unit, world)
        wp = Waypoint(x=target.pos[0], z=target.pos[2],
                      altitude=max(unit.spec.min_altitude, target.pos[1] + 2.0),
                      action=Action.KAMIKAZE, pass_mode="precise", radius=4.0,
                      target_name=target.name, note="таран")
        self.phase = Phase.KAMIKAZE
        return Route([wp], unit.id, loop=False, owner_kind="bot",
                     name=f"kamikaze: {target.name or 'цель'}")


# ---------------------------------------------------------------------------
#  Патруль и разведка
# ---------------------------------------------------------------------------
class PatrolAI(BaseAI):
    """Облёт заданных точек по кругу."""

    name = "patrol"
    phase = Phase.PATROL

    def __init__(self, waypoints: Optional[Sequence[Vec2]] = None,
                 altitude: float = 150.0, action: str = Action.NAVIGATE,
                 hold: float = 0.0, **kw: Any):
        super().__init__(**kw)
        self.waypoints: List[Vec2] = list(waypoints or [])
        self.altitude = altitude
        self.action = action
        self.hold = hold
        # Патруль не привязан к цели: пересчёт не нужен вовсе
        self.replan_interval = 1e9

    def _used_categories(self) -> Tuple[str, ...]:
        return tuple(Action.CATEGORIES.get(self.action, ()))

    def needs_service(self, unit) -> bool:
        if unit.spec.fuel_max > 0 and unit.fuel <= unit.spec.fuel_max * self.fuel_reserve:
            self.reason = "топливо"
            return True
        return False

    def build(self, unit, world: World) -> Optional[Route]:
        if self.needs_service(unit):
            rtb = self.rtb_route(unit, world)
            if rtb is not None:
                return rtb
        pts = self.waypoints
        if not pts:
            x, _y, z = unit.pos
            pts = [(x + 150, z), (x + 150, z + 150), (x, z + 150), (x, z)]
        wps = [Waypoint(x=px, z=pz, altitude=self.altitude, action=self.action,
                        duration=self.hold or 3.0, pass_mode="fly",
                        note=f"патруль {i + 1}")
               for i, (px, pz) in enumerate(pts)]
        self.phase = Phase.PATROL
        return Route(wps, unit.id, loop=True, owner_kind="bot",
                     name=f"patrol: {len(wps)} точек")

    def should_replan(self, unit, world: World, now: float) -> bool:
        # Маршрут замкнутый: пересчитываем только если его нет или он завершён
        return False


class ReconAI(PatrolAI):
    """Разведка: те же точки, но с действием RECON и долгим виражом."""

    name = "recon"
    phase = Phase.RECON

    def __init__(self, waypoints: Optional[Sequence[Vec2]] = None,
                 altitude: float = 180.0, hold: float = 12.0, **kw: Any):
        super().__init__(waypoints=waypoints, altitude=altitude,
                         action=Action.RECON, hold=hold, **kw)


# ---------------------------------------------------------------------------
#  Реестр шаблонов
# ---------------------------------------------------------------------------
AI_TEMPLATES: Dict[str, Any] = {
    "strike": StrikeAI,
    "gun_run": StrikeAI,
    "rocket_run": StrikeAI,
    "bomber": StrikeAI,
    "heli": StrikeAI,
    "drone": StrikeAI,
    "fighter": FighterAI,
    "kamikaze": KamikazeAI,
    "patrol": PatrolAI,
    "recon": ReconAI,
}

#: какой шаблон подходит какому типу техники по умолчанию
DEFAULT_AI_FOR_KIND: Dict[str, str] = {
    "aircraft": "strike",
    "helicopter": "heli",
    "drone": "drone",
    "tank": "gun_run",
}


#: человекочитаемые имена шаблонов для интерфейса
AI_LABELS: Dict[str, str] = {
    "strike": "Штурмовик: бомбовые заходы",
    "gun_run": "Штурмовик: обстрел из пушек",
    "rocket_run": "Штурмовик: пуски НАР/ПТУР",
    "bomber": "Бомбардировщик: высотный заход",
    "heli": "Вертолёт: маловысотный обстрел",
    "drone": "БПЛА: разведка и ПТУР",
    "fighter": "Истребитель: перехват цели",
    "kamikaze": "Камикадзе: одноразовый таран",
    "patrol": "Патруль: облёт точек",
    "recon": "Разведка: вираж над точками",
}


def ai_labels() -> List[Tuple[str, str]]:
    """Пары (ключ, человекочитаемое имя) для выпадающего списка в UI."""
    return [(key, AI_LABELS.get(key, PROFILES[key].name if key in PROFILES else key))
            for key in AI_TEMPLATES]


def make_ai(template: str, cfg: Optional[AppConfig] = None,
            **kw: Any) -> BaseAI:
    """Создать ИИ по имени шаблона.

    Для штурмовых шаблонов автоматически подставляется соответствующий профиль.
    """
    cls = AI_TEMPLATES.get(template)
    if cls is None:
        raise KeyError(f"Неизвестный шаблон ИИ {template!r}. "
                       f"Доступны: {', '.join(sorted(AI_TEMPLATES))}")
    if cls is StrikeAI and "profile" not in kw and template in PROFILES:
        kw["profile"] = PROFILES[template]
    return cls(cfg=cfg, **kw)


def make_ai_for_unit(unit, cfg: Optional[AppConfig] = None,
                     template: Optional[str] = None, **kw: Any) -> BaseAI:
    """Подобрать ИИ под тип техники, если шаблон не указан явно."""
    key = template or DEFAULT_AI_FOR_KIND.get(unit.spec.kind, "patrol")
    return make_ai(key, cfg=cfg, **kw)
