"""
Человекочитаемое состояние техники (UX-15).

Заказчик: «убрать статус spawned, показывать реальные состояния — Стоит,
Маневрирует, Отстреливается, Падает, Горит». Внутренние статусы юнита
(`flying`, `idle`, `spawned`, `damaged`, `stall`, ...) — это технические
флаги физики, оператору они ничего не говорят. Этот модуль — ЕДИНСТВЕННАЯ
точка перевода «техника -> слово для человека», им пользуются и список
юнитов, и пульт, и подписи на карте, поэтому расхождения между панелями
исключены.

Решение принимается по снимку юнита (`Unit.snapshot()`) и необязательному
дополнению из пульта (маршрут, заход на посадку, стоянка). Порядок проверок
= приоритет: критичное важнее повседневного («Падает» важнее «Маневрирует»,
«Горит» важнее «Отстреливается»).

Модуль НЕ импортирует dpg и не знает про UI-теги — его можно тестировать
на словарях (секция 2 скилла tactical-ui-dearpygui: чистые функции домена).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
#  Словарь состояний: ключ -> (подпись, цвет RGBA, признак тревоги)
# ---------------------------------------------------------------------------
_OK = (176, 214, 176, 255)
_DIM = (150, 162, 150, 255)
_CYAN = (120, 214, 231, 255)
_AMBER = (240, 205, 110, 255)
_ORANGE = (255, 166, 84, 255)
_RED = (255, 110, 100, 255)

STATES: Dict[str, Tuple[str, Tuple[int, int, int, int], bool]] = {
    "destroyed":  ("Уничтожен",        _RED,    True),
    "crashed":    ("Разбился",         _RED,    True),
    "falling":    ("Падает",           _RED,    True),
    "burning":    ("Горит",            _ORANGE, True),
    "stall":      ("Сваливание",       _ORANGE, True),
    "fuel_out":   ("Без топлива",      _AMBER,  True),
    "stuck":      ("Застрял",          _AMBER,  True),
    "firing":     ("Отстреливается",   _AMBER,  False),
    "maneuver":   ("Маневрирует",      _CYAN,   False),
    "landing":    ("Заходит на посадку", _CYAN, False),
    "takingoff":  ("Взлетает",         _CYAN,   False),
    "cruise":     ("В полёте",         _OK,     False),
    "route":      ("Идёт по маршруту", _OK,     False),
    "patrol":     ("Патрулирует",      _OK,     False),
    "rolling":    ("Едет",             _OK,     False),
    "parked":     ("Стоит на базе",    _DIM,    False),
    "standby":    ("Стоит",            _DIM,    False),
    "unknown":    ("—",                _DIM,    False),
}

#: технические статусы, которые точно означают «не подвижен»
_STILL = {"idle", "spawned", "parked", "landed", "serviced", "standby"}

#: порог вертикальной скорости, м/с: ниже — считаем падением
FALL_VS = -6.0
#: порог «горим»: доля прочности
BURN_HP_PCT = 40.0
#: окно «только что стрелял», с
FIRE_WINDOW = 2.0
#: перегрузка/крен, при которых полёт считается манёвром
MANEUVER_G = 1.35
MANEUVER_ROLL = 18.0
MANEUVER_PITCH = 12.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def unit_state(snap: Dict[str, Any],
               extra: Optional[Dict[str, Any]] = None) -> str:
    """Ключ состояния юнита по его снимку.

    `extra` — необязательное обогащение из пульта/движка:
    `{"recovery": bool, "parked": bool, "route": bool, "ai": str}`.
    """
    extra = extra or {}
    status = str(snap.get("status", "") or "")
    alive = bool(snap.get("alive", True))
    hp = _num(snap.get("health_pct", 100.0), 100.0)
    speed = _num(snap.get("speed", 0.0))
    vs = _num(snap.get("vs", 0.0))
    roll = abs(_num(snap.get("roll", 0.0)))
    pitch = abs(_num(snap.get("pitch", 0.0)))
    g_load = _num(snap.get("g_load", 1.0), 1.0)
    ground = bool(snap.get("ground", False)) or status in ("moving",)
    kind = str(snap.get("kind", "") or "")
    if kind in ("tank", "truck", "apc"):
        ground = True

    # --- 1. гибель ---------------------------------------------------------
    if not alive or status in ("destroyed", "crashed"):
        return "destroyed" if status == "destroyed" or not alive else "crashed"

    # --- 2. падение: быстро теряет высоту или свалился ---------------------
    if status == "stall" and vs <= 0.0:
        return "falling" if vs < FALL_VS else "stall"
    if vs < FALL_VS and not ground:
        return "falling"

    # --- 3. горит: критические повреждения ---------------------------------
    if hp <= BURN_HP_PCT and (status == "damaged" or hp < BURN_HP_PCT):
        return "burning"

    # --- 4. нет топлива / застрял ------------------------------------------
    if status == "fuel_out":
        return "fuel_out"
    if status == "stuck":
        return "stuck"

    # --- 5. ведёт огонь (приоритет выше манёвра: это главное для оператора)
    if _firing(snap):
        return "firing"

    # --- 6. стоянка / неподвижность ----------------------------------------
    if extra.get("parked") or status == "parked":
        return "parked"
    if status in _STILL and speed < 0.6:
        return "standby"
    if status == "landed" and speed < 0.6:
        return "standby"

    # --- 7. заход на посадку / взлёт ---------------------------------------
    if extra.get("recovery") or status == "recovery":
        return "landing"
    if extra.get("takingoff") or status == "takingoff":
        return "takingoff"

    # --- 8. наземная техника -----------------------------------------------
    if ground:
        return "rolling" if speed > 0.6 else "standby"

    # --- 9. манёвр ----------------------------------------------------------
    if (g_load >= MANEUVER_G or roll >= MANEUVER_ROLL
            or pitch >= MANEUVER_PITCH or status == "g_limit"):
        return "maneuver"

    # --- 10. повседневное ---------------------------------------------------
    if extra.get("ai") in ("patrol", "recon"):
        return "patrol"
    if extra.get("route"):
        return "route"
    return "cruise"


def _firing(snap: Dict[str, Any]) -> bool:
    """Юнит стрелял в последние `FIRE_WINDOW` секунд.

    Смотрим на `since_fire` подвесов (добавлено в `WeaponMount.snapshot`):
    по нему же видно, что залп был только что, даже если боезапас кончился.
    """
    mounts = snap.get("mounts") or []
    for m in mounts:
        if not isinstance(m, dict):
            continue
        since = m.get("since_fire")
        if since is None:
            continue
        if _num(since, 1e9) <= FIRE_WINDOW and m.get("fired_total", 0):
            return True
    return False


def state_label(key: str) -> str:
    return STATES.get(key, STATES["unknown"])[0]


def state_color(key: str) -> Tuple[int, int, int, int]:
    return STATES.get(key, STATES["unknown"])[1]


def state_alarm(key: str) -> bool:
    """True для состояний, которые стоит подсвечивать/озвучивать."""
    return STATES.get(key, STATES["unknown"])[2]


def describe(snap: Dict[str, Any],
             extra: Optional[Dict[str, Any]] = None) -> Tuple[str, str,
                                                             Tuple[int, ...],
                                                             bool]:
    """(ключ, подпись, цвет, тревога) — удобным кортежем для проекции."""
    key = unit_state(snap, extra)
    label, color, alarm = STATES.get(key, STATES["unknown"])
    return key, label, color, alarm


#: обратная совместимость: старые технические статусы -> человекочитаемые
LEGACY_MAP: Dict[str, str] = {
    "spawned": "standby",
    "idle": "standby",
    "flying": "cruise",
    "moving": "rolling",
    "damaged": "burning",
    "parked": "parked",
    "landed": "standby",
    "serviced": "parked",
    "stall": "stall",
    "g_limit": "maneuver",
    "low_altitude": "cruise",
    "fuel_out": "fuel_out",
    "stuck": "stuck",
    "crashed": "crashed",
    "destroyed": "destroyed",
    "kamikaze": "firing",
}


def legacy_to_state(status: str) -> str:
    """Грубый перевод одного технического статуса (без телеметрии)."""
    return LEGACY_MAP.get(str(status or ""), "unknown")
