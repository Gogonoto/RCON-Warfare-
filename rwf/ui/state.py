"""
STATE — единственный источник правды интерфейса (инвариант I4 скилла
`skills/tactical-ui-dearpygui.md`).

Виджеты ничего не владеют: каждый кадр `project_state_to_widgets()` проецирует
STATE в теги, а `render_map()` рисует карту из него же. Все изменения STATE
делаются ТОЛЬКО в главном потоке — в `apply_message()` при разборе MSG_Q
(инвариант I1) и в колбэках DPG (они тоже выполняются в главном потоке).

Рабочие потоки (RCON, движок, сканер) кладут в MSG_Q плоские кортежи
`("тема", payload...)` — никаких объектов GUI и никакой записи в STATE.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

#: инструменты карты (что делает левый клик). В хотбаре v14 — только
#: построение маршрутов (waypoint/wp_del); strike/base/carrier доступны
#: через контекстное меню ПКМ (context.py), но логика инструментов живёт здесь.
TOOLS = ("select", "waypoint", "wp_del", "strike", "base", "carrier")
TOOL_LABELS = {
    "select":   "Выбор",
    "waypoint": "Точка маршрута",
    "wp_del":   "Удалить точку",
    "strike":   "Зона удара",
    "base":     "Поставить базу",
    "carrier":  "Курс авианосца",
}

#: максимумы пулов виджетов (создаются один раз — инвариант I2)
MAX_UNIT_ROWS = 24
MAX_PLANNER_ROWS = 12
MAX_LOG_ROWS = 150
MAX_PLAYER_ROWS = 8

LOG_LEVEL_COLORS = {
    "": (200, 214, 200, 255),
    "green": (110, 231, 140, 255),
    "red": (255, 110, 110, 255),
    "yellow": (240, 214, 120, 255),
    "aqua": (120, 214, 231, 255),
    "cyan": (120, 214, 231, 255),
    "gray": (150, 160, 150, 255),
}


def make_state() -> Dict[str, Any]:
    """Пустой STATE до первого кадра. Вызывается один раз в bootstrap."""
    return {
        "connection": {"host": "127.0.0.1", "port": 25575, "password": "",
                       "mock": True, "connected": False, "description": "",
                       "busy": False},
        "frame": {},                    # последний снимок мира (world.snapshot)
        "bases": [],                    # снимки баз (bases.snapshot)
        "console": {},                  # обогащённый снимок выбранного юнита
        "route_status": {},
        "ai_status": {},
        "telemetry": {},
        "manpads": {},                  # имя игрока -> {lock, cooldown}
        "progress": {},                 # очки/звание/достижения (GAME-01)
        "scan": {"done": 0, "total": 0, "active": False},
        "status": {},
        "selection": None,              # uid выбранного юнита
        "selected_base": None,          # id выбранной базы
        "follow": False,
        "tool": "select",
        "hotbar": True,                   # левая панель инструментов видима
        "objpanel": True,                 # левая панель объекта видима
        "console_obj": None,              # ("unit", uid)|("player", name)|
                                          # ("base", id) — объект пульта
        "planner": {"points": [],       # [{x,z,alt,action}]
                    "selected": None},
        # --- тактическое управление (TAC-01, пункт 17 ТЗ) ------------------
        # selected_units: множественное выделение юнитов; unit_routes:
        # uid -> черновой маршрут [{x,z}] (ещё не назначенный движку);
        # tac_drag: активный перетаскиватель {"kind": unit|point|new,
        # "uid", "index", "from_index"}; tac_targets: засечённые цели по
        # uid: {"kind": player|base|unit|ground, "name", "x", "z"}.
        "selected_units": [],
        "unit_routes": {},
        "tac_drag": None,
        "tac_targets": {},
        "launch": {"base_id": None, "variant": "attacker", "altitude": 150.0,
                   "is_bot": False, "loadout": {}},
        "layers": {"grid": True, "bases": True, "zones": True, "routes": True,
                   "markers": True, "units": True, "players": True,
                   "planner": True, "hud": True, "terrain": True,
                   "icon_scale": 1.0},
        "terrain": {"tex": None,        # tag текстуры DPG (создаёт mapfacade)
                    "data": None,       # RGBA bytes
                    "w": 0, "h": 0, "world": None, "key": None,
                    "dirty": False},
        "log": deque(maxlen=500),       # (текст, уровень)
        "log_rev": 0,                   # ревизия журнала (для дешёвой проекции)
        "frame_rev": -1,
        "mouse": {"wx": 0.0, "wz": 0.0, "sx": 0.0, "sy": 0.0,
                  "inside": False},
        "strike_corner": None,          # первый угол зоны удара (инструмент)
        "docks": {"conn": True, "units": True, "console": True,
                  "planner": True, "launch": True, "players": True,
                  "log": True, "layers": False, "bases": True},
        "demo": {"spawned": False},
    }


# ---------------------------------------------------------------------------
#  Разбор входящих сообщений (единственное место мутации STATE из потока)
# ---------------------------------------------------------------------------
def apply_message(state: Dict[str, Any], msg: Tuple[Any, ...]) -> None:
    """Разобрать одно сообщение MSG_Q. Вызывается ТОЛЬКО в главном потоке."""
    if not msg:
        return
    topic = msg[0]

    if topic == "log":
        _log(state, msg[1], msg[2] if len(msg) > 2 else "")

    elif topic == "connected":
        connected, description = msg[1], msg[2]
        state["connection"]["connected"] = bool(connected)
        state["connection"]["description"] = description or ""
        state["connection"]["busy"] = False

    elif topic == "frame":
        snap = msg[1]
        state["frame"] = snap
        state["frame_rev"] = snap.get("revision", -1)

    elif topic == "terrain":
        _data, w, h, world_rect, key = msg[1], msg[2], msg[3], msg[4], msg[5]
        t = state["terrain"]
        t["data"], t["w"], t["h"], t["world"], t["key"] = _data, w, h, world_rect, key
        t["dirty"] = True

    elif topic == "terrain_clear":
        t = state["terrain"]
        t.update(data=None, w=0, h=0, world=None, key=None, dirty=True)

    elif topic == "bases":
        state["bases"] = msg[1]

    elif topic == "console":
        state["console"] = msg[2] or {}
        state["console_uid"] = msg[1]

    elif topic == "telemetry":
        state["telemetry"] = msg[2] or {}

    elif topic == "route_status":
        state["route_status"] = msg[2] or {}

    elif topic == "ai_status":
        state["ai_status"] = msg[2] or {}

    elif topic == "manpads":
        state["manpads"] = msg[1] or {}

    elif topic == "progress":
        state["progress"] = msg[1] or {}

    elif topic == "scan":
        done, total, active = msg[1], msg[2], msg[3]
        state["scan"] = {"done": done, "total": total, "active": active}

    elif topic == "status":
        state["status"] = msg[1] or {}

    elif topic == "players":
        state["players"] = msg[1]

    elif topic == "selected":
        state["selection"] = msg[1]

    elif topic == "route_library":
        state["route_library"] = msg[1]

    elif topic == "planner_loaded":
        state["planner"]["points"] = list(msg[1] or [])
        state["planner"]["selected"] = None

    else:
        # неизвестная тема — не ошибка, но видно в отладке
        _log(state, f"MSG_Q: неизвестная тема {topic!r}", "gray")


def _log(state: Dict[str, Any], text: str, level: str = "") -> None:
    log: Deque[Tuple[str, str]] = state["log"]
    log.append((str(text), str(level or "")))
    state["log_rev"] += 1


# ---------------------------------------------------------------------------
#  Планировщик маршрута (черновик живёт в STATE, не в виджетах)
# ---------------------------------------------------------------------------
def planner_add(state: Dict[str, Any], x: float, z: float,
                alt: float = 150.0, action: str = "navigate") -> None:
    pts: List[Dict[str, Any]] = state["planner"]["points"]
    pts.append({"x": float(x), "z": float(z), "alt": float(alt),
                "action": action})


def planner_remove(state: Dict[str, Any], index: int) -> None:
    pts = state["planner"]["points"]
    if 0 <= index < len(pts):
        pts.pop(index)
        sel = state["planner"]["selected"]
        if sel is not None and sel >= len(pts):
            state["planner"]["selected"] = None


def planner_move(state: Dict[str, Any], src: int, dst: int) -> bool:
    pts = state["planner"]["points"]
    if not (0 <= src < len(pts)) or not (0 <= dst < len(pts)):
        return False
    pts.insert(dst, pts.pop(src))
    return True


def planner_set_action(state: Dict[str, Any], index: int, action: str) -> None:
    pts = state["planner"]["points"]
    if 0 <= index < len(pts):
        pts[index]["action"] = action


def planner_clear(state: Dict[str, Any]) -> None:
    state["planner"]["points"] = []
    state["planner"]["selected"] = None


def planner_drag(state: Dict[str, Any], index: int, x: float, z: float) -> None:
    pts = state["planner"]["points"]
    if 0 <= index < len(pts):
        pts[index]["x"] = float(x)
        pts[index]["z"] = float(z)


# ---------------------------------------------------------------------------
#  Тактическое управление (TAC-01, пункт 17 ТЗ)
#  Черновые маршруты выделенной техники живут в STATE; facade превращает их
#  в реальные маршруты движка (route_order). Все мутации — только из
#  колбэков DPG главного потока (I1/I4).
# ---------------------------------------------------------------------------
def tac_select(state: Dict[str, Any], uid: Optional[int],
               additive: bool = False) -> None:
    """Клик по юниту: одиночный выбор либо +Shift/Ctrl к множественному."""
    sel: List[int] = state["selected_units"]
    if uid is None:
        if not additive:
            state["selected_units"] = []
            state["selection"] = None
        return
    if additive:
        if uid in sel:
            sel.remove(uid)
        else:
            sel.append(uid)
    else:
        state["selected_units"] = [uid]
        state["selection"] = uid
        return
    state["selection"] = sel[-1] if sel else None


def tac_unit_route(state: Dict[str, Any], uid: int) -> List[Dict[str, float]]:
    """Черновой маршрут юнита (создаётся при первом обращении)."""
    routes: Dict[int, List[Dict[str, float]]] = state["unit_routes"]
    return routes.setdefault(uid, [])


def tac_add_point(state: Dict[str, Any], uid: int, x: float, z: float,
                  after: Optional[int] = None) -> None:
    """Добавить точку маршрута (after=None — в конец, иначе вставить после)."""
    pts = tac_unit_route(state, uid)
    p = {"x": float(x), "z": float(z)}
    if after is None or after >= len(pts) - 1:
        pts.append(p)
    else:
        pts.insert(after + 1, p)


def tac_move_point(state: Dict[str, Any], uid: int, index: int,
                   x: float, z: float) -> None:
    pts = tac_unit_route(state, uid)
    if 0 <= index < len(pts):
        pts[index]["x"] = float(x)
        pts[index]["z"] = float(z)


def tac_remove_point(state: Dict[str, Any], uid: int, index: int) -> None:
    pts = state["unit_routes"].get(uid) or []
    if 0 <= index < len(pts):
        pts.pop(index)
        if not pts:
            state["unit_routes"].pop(uid, None)


def tac_clear_routes(state: Dict[str, Any]) -> None:
    """Сброс всех черновиков (после отправки назначенных маршрутов)."""
    state["unit_routes"] = {}


def tac_clear_route_points(state: Dict[str, Any], uid: int) -> None:
    """Очистить черновой маршрут одного юнита после назначения движку.

    Запись удаляется целиком — иначе в слое ``unit_routes`` копятся
    пустые списки на каждый юнит.
    """
    routes: Dict[int, List[Dict[str, float]]] = state.get("unit_routes") or {}
    routes.pop(int(uid), None)


def tac_set_target(state: Dict[str, Any], uid: int,
                   target: Optional[Dict[str, Any]]) -> None:
    """Цель атаки для юнита: dict(kind,name,x,z) или None (снять)."""
    if target is None:
        state["tac_targets"].pop(uid, None)
    else:
        state["tac_targets"][int(uid)] = dict(target)
