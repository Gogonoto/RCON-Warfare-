"""
Проекция STATE -> виджеты (секции 3 и 6 скилла tactical-ui-dearpygui).

Единственное место, где данные пишутся в виджеты: `set_value`/`configure_item`
по ТЕГАМ. Вызывается из главного цикла каждый кадр; «тяжёлые» блоки (журнал,
списки, таблицы) пересобираются только когда изменилась соответствующая
ревизия в STATE — остальное стоит микросекунды (I4/I5).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import dearpygui.dearpygui as dpg

from ..unitstate import describe
from . import build as B
from . import state as st
from . import theme as T


def _unit_extra(state: Dict[str, Any], uid: Any) -> Dict[str, Any]:
    """Дополнение к снимку юнита для точного состояния (посадка/стоянка)."""
    out: Dict[str, Any] = {}
    rec = (state.get("recovery") or {}).get(uid)
    if rec:
        out["recovery"] = True
    park = (state.get("parked") or {}).get(uid)
    if park:
        out["parked"] = True
    rs = state.get("route_status") or {}
    if state.get("selection") == uid and rs.get("name"):
        out["route"] = True
    ai = state.get("ai_status") or {}
    if state.get("selection") == uid and ai.get("ai"):
        out["ai"] = ai.get("ai")
    return out

_LABEL_TO_ACTION: Dict[str, str] = {}


def _action_labels() -> Dict[str, str]:
    if not _LABEL_TO_ACTION:
        from ..routes import ACTION_LABELS
        for k, v in ACTION_LABELS.items():
            _LABEL_TO_ACTION[v] = k
    return _LABEL_TO_ACTION


def _action_label(action: str) -> str:
    from ..routes import ACTION_LABELS
    return ACTION_LABELS.get(action, action)


# ---------------------------------------------------------------------------
def project_state_to_widgets(state: Dict[str, Any], facade) -> None:
    sig = state.setdefault("_sig", {})
    _project_statusline(state, sig)
    _project_hotbar(state, sig)
    _project_ai_targets(state, sig)
    _project_units(state, sig)
    _project_bases(state, sig)
    _project_console(state, sig, facade)
    _project_presets(state, sig, facade)
    _project_progress(state, sig)
    _project_planner(state, sig)
    _project_players(state, sig)
    _project_log(state, sig)
    _project_library(state, sig)


# ------------------------------------------------------------- статус-строка
def _project_statusline(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    """Живые показатели — в заголовок окна карты (v15).

    Отдельной верхней полосы больше нет (жалоба «огромная пустая полоса
    сверху»): соединение, координаты курсора, число юнитов, fps и прогресс
    скана печатаются в label окна карты. Обновляем только при изменении
    текста — `configure_item(label=...)` дёргает ImGui на перерисовку
    заголовка, поэтому держим его за подписью (I5).
    """
    conn = state["connection"]
    if conn["connected"]:
        conn_txt = f"● {conn.get('description') or 'в сети'}"
    elif conn.get("busy"):
        conn_txt = "…подключение…"
    else:
        conn_txt = "○ не подключено"

    frame = state.get("frame") or {}
    units = frame.get("units") or {}
    mouse = state.get("mouse") or {}
    scan = state.get("scan") or {}
    coords = (f"X {mouse.get('wx', 0.0):.0f} Z {mouse.get('wz', 0.0):.0f}"
              if mouse.get("inside") else "X — Z —")
    scan_txt = ""
    if scan.get("active"):
        pct = scan["done"] / max(1, scan["total"])
        scan_txt = f"  ·  скан {pct * 100:.0f}%"
    label = (f"ТАКТИЧЕСКАЯ КАРТА   {conn_txt}   юнитов {len(units)}"
             f"   {coords}   {dpg.get_frame_rate():.0f} fps{scan_txt}")
    if sig.get("map_label") != label:
        sig["map_label"] = label
        if dpg.does_item_exist("win_map"):
            dpg.configure_item("win_map", label=label)

    # дубль индикатора соединения в секции СВЯЗЬ (цветом)
    if conn["connected"]:
        text, color = conn_txt, (120, 230, 140, 255)
    elif conn.get("busy"):
        text, color = conn_txt, (230, 210, 120, 255)
    else:
        text, color = conn_txt, (190, 110, 110, 255)
    if sig.get("conn") != (text, color):
        sig["conn"] = (text, color)
        if dpg.does_item_exist("txt_conn_status"):
            dpg.set_value("txt_conn_status", text)
            dpg.configure_item("txt_conn_status", color=color)
    if sig.get("scan") != scan_txt:
        sig["scan"] = scan_txt
        if dpg.does_item_exist("txt_scan_status"):
            dpg.set_value("txt_scan_status", scan_txt.strip())


def _project_topbar(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    conn = state["connection"]
    if conn["connected"]:
        text = f"● {conn['description'] or 'подключено'}"
        color = (120, 230, 140, 255)
    elif conn.get("busy"):
        text = "…подключение…"
        color = (230, 210, 120, 255)
    else:
        text = "○ не подключено"
        color = (190, 110, 110, 255)
    if sig.get("conn") != (text, color):
        sig["conn"] = (text, color)
        if dpg.does_item_exist("txt_conn_status"):
            dpg.set_value("txt_conn_status", text)
            dpg.configure_item("txt_conn_status", color=color)

    frame = state.get("frame") or {}
    units = frame.get("units") or {}
    mouse = state.get("mouse") or {}
    scan = state.get("scan") or {}
    coords = (f"X {mouse.get('wx', 0.0):7.0f}  Z {mouse.get('wz', 0.0):7.0f}"
              if mouse.get("inside") else "координаты: —")
    status = (f"юнитов {len(units)}  ·  {coords}  ·  "
              f"{dpg.get_frame_rate():.0f} fps")
    if dpg.does_item_exist("txt_top_status"):
        dpg.set_value("txt_top_status", status)
    if scan.get("active"):
        pct = (scan["done"] / max(1, scan["total"]))
        txt = (f"· скан рельефа {scan['done']}/{scan['total']} "
               f"({pct * 100:.0f}%)")
    else:
        txt = ""
    if sig.get("scan") != txt:
        sig["scan"] = txt
        if dpg.does_item_exist("txt_scan_status"):
            dpg.set_value("txt_scan_status", txt)


# ------------------------------------------------------------------- хотбар
def _project_hotbar(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    """Подсветка активного инструмента и состояний «следить»/«звук»."""
    key = (state.get("tool", "select"), bool(state.get("follow")),
           bool(getattr(state.get("_facade_settings", None),
                        "sound_enabled", True)))
    if sig.get("hotbar") == key:
        return
    sig["hotbar"] = key
    B._paint_hotbar(state)


# ------------------------------------------------- список целей автопилота
def _project_ai_targets(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    """Наполняет `cmb_ai_target`: игроки и техника, без ручного ввода имени.

    Заказчик: «таргетинг целей через выпадающий список (Players / Tech),
    а не ручной ввод». Список grouped префиксами — DPG не умеет настоящие
    группы в combo, поэтому разделители сделаны текстом.
    """
    frame = state.get("frame") or {}
    players = sorted((frame.get("players") or {}))
    units = frame.get("units") or {}
    items = [B.AI_TARGET_NONE]
    if players:
        items.append("— ИГРОКИ —")
        items.extend(f"игрок: {n}" for n in players)
    if units:
        items.append("— ТЕХНИКА —")
        items.extend(f"#{uid} {u.get('label', u.get('kind', ''))}"
                     for uid, u in sorted(units.items()))
    key = tuple(items)
    if sig.get("ai_targets") == key:
        return
    sig["ai_targets"] = key
    tag = "cmb_ai_target"
    if not dpg.does_item_exist(tag):
        return
    cur = dpg.get_value(tag)
    dpg.configure_item(tag, items=items)
    dpg.set_value(tag, cur if cur in items else B.AI_TARGET_NONE)


# ------------------------------------------------------------------ пресеты
def _project_presets(state: Dict[str, Any], sig: Dict[str, Any],
                     facade) -> None:
    """Список пресетов в «Запуске» синхронен с каталогом (сохранения/удаления)."""
    variant = state.get("launch", {}).get("variant") or "attacker"
    try:
        names = tuple(facade.preset_names(variant))
    except Exception:  # noqa: BLE001
        return
    if sig.get("presets") == names:
        return
    sig["presets"] = names
    B._refresh_preset_combo(variant)


# ----------------------------------------------------------------- прогресс
def _project_progress(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    pr = state.get("progress") or {}
    key = (pr.get("score"), pr.get("rank"), tuple(pr.get("unlocked") or ()))
    if sig.get("progress") == key:
        return
    sig["progress"] = key
    if not pr:
        return
    _set("txt_score", f"{pr.get('score', 0)}")
    _set("txt_score_session", f"сессия: {pr.get('session', 0):+d}")
    _set("txt_rank", str(pr.get("rank", "—")))
    frac = float(pr.get("rank_frac", 0.0))
    if dpg.does_item_exist("bar_rank"):
        dpg.set_value("bar_rank", max(0.0, min(1.0, frac)))
        dpg.configure_item("bar_rank",
                           overlay=f"{frac * 100:.0f}%")
    lo, hi = int(pr.get("rank_lo", 0)), int(pr.get("rank_hi", 0))
    _set("txt_rank_next", f"{pr.get('score', 0)} / {hi} очков до следующего "
                          f"звания (порог текущего {lo})")
    achs = pr.get("achievements") or []
    for i in range(10):
        tag = f"txt_ach_{i}"
        if not dpg.does_item_exist(tag):
            continue
        if i < len(achs):
            a = achs[i]
            mark = "✓" if a.get("done") else "·"
            dpg.configure_item(tag, show=True,
                               color=T.ACCENT if a.get("done") else T.TEXT_DIM)
            dpg.set_value(tag, f"{mark} {a.get('name', '')} — {a.get('hint', '')}")
        else:
            dpg.configure_item(tag, show=False)


# ------------------------------------------------------------------- юниты
def _units_sig(state: Dict[str, Any]) -> Tuple:
    frame = state.get("frame") or {}
    units = frame.get("units") or {}
    return (state.get("frame_rev"), state.get("selection"),
            tuple(sorted((uid, u.get("status"), round(u.get("fuel_pct", 0)),
                          u.get("ammo", 0), round(u.get("health_pct", 0)))
                         for uid, u in units.items())))


def _project_units(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    key = _units_sig(state)
    if sig.get("units") == key:
        return
    sig["units"] = key
    frame = state.get("frame") or {}
    units = frame.get("units") or {}
    sel = state.get("selection")
    items = sorted(units.items(),
                   key=lambda kv: (kv[0] != sel, kv[1].get("kind", ""), kv[0]))
    row_uids: Dict[int, Optional[int]] = {}
    for i in range(st.MAX_UNIT_ROWS):
        tag_row = f"u_row_{i}"
        if not dpg.does_item_exist(tag_row):
            continue
        if i < len(items):
            uid, u = items[i]
            row_uids[i] = uid
            dpg.configure_item(tag_row, show=True)
            bot = "·бот " if u.get("is_bot") else ""
            sel_mark = "▶ " if uid == sel else ""
            dpg.configure_item(
                f"u_name_{i}",
                label=f"{sel_mark}#{uid} {bot}"
                      f"{u.get('label', u.get('kind', ''))}")
            try:
                dpg.set_table_row_color(
                    tag_row, (40, 56, 42, 255) if uid == sel
                    else (18, 23, 19, 255))
            except Exception:  # noqa: BLE001
                pass
            B.redraw_unit_icon(i, u.get("kind", "aircraft"), uid == sel)
            _key, label, color, _alarm = describe(u, _unit_extra(state, uid))
            dpg.configure_item(f"u_status_{i}", label=label)
            try:
                dpg.configure_item(f"u_status_{i}", color=color)
            except Exception:  # noqa: BLE001 - selectable цвет не берёт
                pass
            dpg.set_value(f"u_fuel_{i}", f"{u.get('fuel_pct', 0):.0f}%")
            dpg.set_value(f"u_ammo_{i}",
                          f"{u.get('ammo', 0)}/{u.get('ammo_max', 0)}")
            hp = u.get("health_pct", 100.0)
            dpg.set_value(f"u_hp_{i}", f"{hp:.0f}%")
            color = ((255, 120, 110, 255) if hp < 40 else
                     (240, 210, 130, 255) if hp < 75 else
                     (190, 225, 190, 255))
            dpg.configure_item(f"u_hp_{i}", color=color)
        else:
            row_uids[i] = None
            dpg.configure_item(tag_row, show=False)
    state["_row_uids"] = row_uids


# --------------------------------------------------------------------- базы
def _bases_sig(state: Dict[str, Any]) -> Tuple:
    return tuple((b["id"], b["name"], b["kind"], b["free_pads"],
                  bool(b.get("moving")),
                  round(float(b.get("health_pct", 100.0))),
                  round(float(b.get("supply", 0.0))),
                  bool(b.get("destroyed")))
                 for b in state.get("bases") or [])


def _base_label(b: Dict[str, Any]) -> str:
    from ..bases import BASE_LABELS
    kind = BASE_LABELS.get(b["kind"], b["kind"])
    return f"#{b['id']} {b['name']} ({kind})"


def _project_bases(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    key = _bases_sig(state)
    if sig.get("bases") == key:
        return
    sig["bases"] = key
    bases = state.get("bases") or []
    labels = [_base_label(b) for b in bases]
    combo_items = ["— (в центр карты)"] + labels
    if dpg.does_item_exist("cmb_launch_base"):
        cur = dpg.get_value("cmb_launch_base")
        dpg.configure_item("cmb_launch_base", items=combo_items)
        dpg.set_value("cmb_launch_base",
                      cur if cur in combo_items else combo_items[0])
    for i in range(6):
        tag = f"base_row_{i}"
        if not dpg.does_item_exist(tag):
            continue
        if i < len(bases):
            b = bases[i]
            dpg.configure_item(tag, show=True)
            moving = " →" if b.get("moving") else ""
            sel_mark = "▶ " if state.get("selected_base") == b["id"] else ""
            name = f"{sel_mark}#{b['id']} {b['name']}{moving}"
            if len(name) > 22:
                name = name[:21] + "…"
            from ..bases import BASE_LABELS
            dpg.configure_item(f"txt_base_{i}", label=name)
            try:
                dpg.set_table_row_color(
                    tag, (40, 56, 42, 255)
                    if state.get("selected_base") == b["id"]
                    else (18, 23, 19, 255))
            except Exception:  # noqa: BLE001
                pass
            dpg.configure_item(f"txt_base_cap_{i}",
                               label=f"{b['free_pads']}/{len(b['pads'])}")
            hp = float(b.get("health_pct", 100.0))
            dpg.configure_item(
                f"txt_base_hp_{i}",
                label=("снесена" if b.get("destroyed") else f"{hp:.0f}%"))
            try:
                dpg.set_table_row_color(
                    tag, (70, 34, 32, 255) if b.get("destroyed")
                    else (40, 56, 42, 255)
                    if state.get("selected_base") == b["id"]
                    else (18, 23, 19, 255))
            except Exception:  # noqa: BLE001
                pass
            # ячейки таблицы — selectable: их значение bool, текст задаётся
            # через label (set_value(str) DPG отвергает с «Must be bool»)
            sup = float(b.get("supply", 0.0))
            dpg.configure_item(f"txt_base_sup_{i}", label=f"{sup:.0f}")
            B.redraw_base_icon(i, b["kind"],
                               state.get("selected_base") == b["id"])
        else:
            dpg.configure_item(tag, show=False)


# -------------------------------------------------------------------- пульт
def _console_items(state: Dict[str, Any]) -> List[Tuple[str, tuple]]:
    """Список объектов пульта: техника, игроки, базы (UX-14)."""
    out: List[Tuple[str, tuple]] = []
    frame = state.get("frame") or {}
    for uid, u in sorted((frame.get("units") or {}).items()):
        out.append((f"#{uid} {u.get('label', '')} "
                    f"[{u.get('kind', '')}]", ("unit", uid)))
    for name in sorted((frame.get("players") or {})):
        out.append((f"игрок: {name}", ("player", name)))
    for b in state.get("bases") or []:
        out.append((f"#{b['id']} {b['name']} [база]", ("base", b["id"])))
    return out


def _project_console(state: Dict[str, Any], sig: Dict[str, Any],
                     facade) -> None:
    # --- список объектов ---------------------------------------------------
    items = _console_items(state)
    labels = ["—"] + [l for l, _ in items]
    if sig.get("console_items") != tuple(labels):
        sig["console_items"] = tuple(labels)
        state["_console_map"] = items
        if dpg.does_item_exist("cmb_console_obj"):
            cur = dpg.get_value("cmb_console_obj")
            dpg.configure_item("cmb_console_obj", items=labels)
            dpg.set_value("cmb_console_obj",
                          cur if cur in labels else "—")
    # --- синхронизация выбора: selection -> console_obj ---------------------
    obj = state.get("console_obj")
    sel = state.get("selection")
    if sel is not None and obj != ("unit", sel):
        obj = ("unit", sel)
        state["console_obj"] = obj
    if sig.get("console_obj") != obj and dpg.does_item_exist("cmb_console_obj"):
        sig["console_obj"] = obj
        label = next((l for l, o in items if o == obj), "—")
        dpg.set_value("cmb_console_obj", label)

    cons: Dict[str, Any] = state.get("console") or {}
    is_unit = bool(obj and obj[0] == "unit" and cons
                   and cons.get("id") == obj[1])
    frame = state.get("frame") or {}

    if is_unit:
        _console_unit(state, cons, sig)
    elif obj and obj[0] == "player":
        _console_player(state, frame, obj[1], sig)
    elif obj and obj[0] == "base":
        _console_base(state, obj[1], sig)
    else:
        _console_empty(sig)


def _set_param(i: int, key: str, value: str, color=None) -> None:
    if dpg.does_item_exist(f"telp_k_{i}"):
        dpg.set_value(f"telp_k_{i}", key)
        dpg.set_value(f"telp_v_{i}", value)
        if color is not None:
            dpg.configure_item(f"telp_v_{i}", color=color)


def _clear_params_from(i: int) -> None:
    for j in range(i, B.TEL_PARAM_ROWS):
        if dpg.does_item_exist(f"telp_k_{j}"):
            dpg.set_value(f"telp_k_{j}", "")
            dpg.set_value(f"telp_v_{j}", "")


def _paint_pic(kind: str, silhouette: Optional[str] = None,
               label: str = "") -> None:
    """Портрет объекта в пульте: силуэт техники / иконка игрока / базы."""
    from .. import uicons
    from ..icons import Lod, unit_icon
    from ..maprender import UnitsLayer
    from . import iconwidget as iw
    tag = "tel_pic"
    if not dpg.does_item_exist(tag):
        return
    prims: List[Dict[str, Any]] = []
    try:
        cw, ch = dpg.get_item_rect_size(tag)
    except Exception:  # noqa: BLE001
        cw, ch = B.obj_client_w(), 92.0
    cx, cy = cw / 2.0, ch / 2.0
    if silhouette:
        fill, outline = UnitsLayer.COLORS.get(silhouette,
                                              ("#ffffff", "#888888"))
        lod = Lod(size_px=min(34.0, ch * 0.36), detail=2, alpha=255,
                  show_label=False, show_rotors=True, show_blades=True)
        prims = unit_icon(silhouette, cx, cy, 0.0, lod, fill, outline)
    else:
        name = {"player": "user", "airport": "plane", "carrier": "ship",
                "ground": "home", "base": "home"}.get(kind, "target")
        prims = uicons.prims(name, cx, cy, min(52.0, ch * 0.6), T.TEXT_DIM, 1.6)
    # тонкая рамка-«экран прибора»: силуэт читается как картинка, а не как пятно
    prims.append({"type": "rect", "x": 0.5, "y": 0.5, "w": cw - 1.0,
                  "h": ch - 1.0, "fill": None, "outline": "#222c23",
                  "width": 1, "alpha": 200})
    iw.paint_raw(tag, prims)
    if dpg.does_item_exist("tel_pic_label"):
        dpg.set_value("tel_pic_label", label)


def _console_unit(state: Dict[str, Any], cons: Dict[str, Any],
                  sig: Dict[str, Any]) -> None:
    kind = str(cons.get("kind", "") or "")
    ground = bool(cons.get("ground")) or kind in ("tank", "truck", "apc")
    cargo_max = float(cons.get("cargo_max", 0.0) or 0.0)
    transport = cargo_max > 0 or kind in ("transport", "truck")

    _key, state_label, state_color, alarm = describe(
        cons, {"recovery": bool(cons.get("recovery")),
               "parked": bool(cons.get("parked")),
               "route": bool((state.get("route_status") or {}).get("name")),
               "ai": (state.get("ai_status") or {}).get("ai")})

    # Набор параметров зависит от типа машины: у наземной техники нет
    # высоты/вертикальной скорости/перегрузки, у истребителя нет груза —
    # раньше всё это выводилось пустыми строками («ненужная фигня»).
    rows: List[Tuple[str, str, Any]] = [
        ("СОСТОЯНИЕ", state_label, state_color),
        ("СКОРОСТЬ", f"{cons.get('speed', 0):.1f} м/с", None),
    ]
    if ground:
        rows.append(("УКЛОН", f"{cons.get('slope', 0.0):+.0f}°", None))
        rows.append(("ГРУНТ", str(cons.get("ground_kind") or "—"), None))
    else:
        rows.append(("ВЫСОТА", f"{cons.get('altitude', 0):.0f} м", None))
        rows.append(("ВЕРТ.СК.", f"{cons.get('vs', 0):+.1f} м/с", None))
        rows.append(("ПЕРЕГРУЗКА", f"{cons.get('g_load', 1.0):.2f} g", None))
    rows.append(("КУРС", f"{cons.get('heading', 0):.0f}°", None))
    if transport:
        rows.append(("ГРУЗ", f"{cons.get('cargo', 0.0):.1f} / {cargo_max:.1f} т",
                     None))
    mode = "БОТ" if cons.get("is_bot") else "РУЧНОЙ"
    if cons.get("parked"):
        mode = f"СТОЯНКА · {cons['parked'].get('base_name', '')}"
    elif cons.get("recovery"):
        mode = f"ПОСАДКА · {cons['recovery'].get('base_name', '')}"
    rows.append(("РЕЖИМ", mode, None))
    rs = state.get("route_status") or {}
    rows.append(("МАРШРУТ",
                 (f"{rs.get('name', '')} {rs.get('current', 0)}/"
                  f"{rs.get('total', 0)}") if rs.get("name") else "—", None))
    nb = cons.get("nearest_base")
    rows.append(("БАЗА", (f"{nb['name']} · {nb['distance']:.0f} м" if nb
                          else "—"), None))

    for i in range(B.TEL_PARAM_ROWS):
        if i < len(rows):
            _set_param(i, rows[i][0], rows[i][1], rows[i][2])
        else:
            _set_param(i, "", "")
    label = f"#{cons.get('id')} {cons.get('label', '')}"
    if dpg.does_item_exist("tel_title"):
        dpg.set_value("tel_title", label)
    if dpg.does_item_exist("cmb_console_obj"):
        dpg.configure_item("cmb_console_obj",
                           label=label if label else "объект")
    _paint_duty(cons.get("duty", "combat"), bool(cons.get("parked")),
                cons.get("fuel_pct", 100.0))
    _paint_pic(cons.get("kind", ""), cons.get("kind", ""),
               f"{cons.get('label', '')} · {cons.get('kind', '')} · "
               f"{cons.get('blueprint', '')}")
    _bar("bar_fuel", float(cons.get("fuel_pct", 0.0)) / 100.0,
         f"{cons.get('fuel', 0):.0f} ({cons.get('fuel_pct', 0):.0f}%)", True)
    _bar("bar_hull", float(cons.get("health_pct", 100.0)) / 100.0,
         f"{cons.get('health', 0):.0f} ({cons.get('health_pct', 100):.0f}%)",
         True)
    if cargo_max > 0:
        _bar("bar_cargo", float(cons.get("cargo", 0.0)) / cargo_max,
             f"{cons.get('cargo', 0.0):.1f} т", True)
    else:
        _bar("bar_cargo", 0.0, "—", False)
    _project_mounts(state, cons, sig)
    for tag in ("tel_actions", "tel_mounts", "duty_row"):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, show=True)
    _set("txt_ai_status", _ai_text(state.get("ai_status") or {}))
    if dpg.does_item_exist("txt_route_status"):
        dpg.set_value("txt_route_status",
                      _route_text(state.get("route_status") or {}))


def _paint_duty(duty: str, parked: bool, fuel_pct: float = 100.0) -> None:
    """Активный сегмент тумблера «Стоянка / В бой» + короткая подсказка.

    Подсказка объясняет, что произойдёт дальше (авто-ТО на базе или
    патруль вокруг точки взлёта), чтобы оператор не гадал.
    """
    park_active = bool(parked) or str(duty) == "park"
    if not dpg.does_item_exist("btn_duty_park"):
        return
    # активный сегмент подсвечен своей темой, пассивный — дефолтной
    for tag, active in (("btn_duty_park", park_active),
                        ("btn_duty_combat", not park_active)):
        try:
            if active:
                dpg.bind_item_theme(tag, tag + "_act_th")
            else:
                dpg.unbind_item_theme(tag)
        except Exception:  # noqa: BLE001 - unbind без темы не ошибка
            pass
    if park_active:
        hint = ("Стоянка: возврат на приписную базу, ТО автоматически "
                "по касанию")
        if float(fuel_pct) < 25.0:
            hint = f"Стоянка · топливо {float(fuel_pct):.0f}% — нужен возврат"
    else:
        hint = "В бой: маршрут оператора или патруль вокруг точки взлёта"
    if dpg.does_item_exist("txt_duty_hint"):
        dpg.set_value("txt_duty_hint", hint)


def _project_mounts(state: Dict[str, Any], cons: Dict[str, Any],
                    sig: Dict[str, Any]) -> None:
    mounts = cons.get("mounts") or []
    avail = cons.get("mount_available") or []
    uid = cons.get("id")
    mount_sig = (uid, tuple(tuple(a or []) for a in avail),
                 tuple((m.get("key"), m.get("ammo")) for m in mounts))
    if sig.get("mounts") != mount_sig:
        sig["mounts"] = mount_sig
        from ..weapons import WEAPONS
        for i in range(4):
            row = f"telm_row_{i}"
            if not dpg.does_item_exist(row):
                continue
            if i < len(mounts):
                keys = list(avail[i]) if i < len(avail) else []
                items = ["—"] + [WEAPONS[k]["label"] for k in keys
                                 if k in WEAPONS]
                cur = mounts[i].get("key")
                cur_label = WEAPONS[cur]["label"] if cur in WEAPONS else "—"
                dpg.configure_item(f"cmb_wpn_{i}", items=items, show=True)
                dpg.set_value(f"cmb_wpn_{i}",
                              cur_label if cur_label in items else "—")
                dpg.set_value(f"txt_ammo_{i}",
                              f"{mounts[i].get('ammo', 0)}/"
                              f"{mounts[i].get('ammo_max', 0)}")
                dpg.configure_item(row, show=True)
            else:
                dpg.configure_item(row, show=False)
    else:
        for i, m in enumerate(mounts[:4]):
            if dpg.does_item_exist(f"txt_ammo_{i}"):
                dpg.set_value(f"txt_ammo_{i}",
                              f"{m.get('ammo', 0)}/{m.get('ammo_max', 0)}")


def _console_player(state: Dict[str, Any], frame: Dict[str, Any],
                    name: str, sig: Dict[str, Any]) -> None:
    rec = (frame.get("players") or {}).get(name) or {}
    pos = rec.get("pos") or (0, 0, 0)
    mp = (state.get("manpads") or {}).get(name) or {}
    lock = mp.get("lock")
    cd = mp.get("cooldown") or 0.0
    _set_param(0, "ИГРОК", name)
    _set_param(1, "КООРД", f"{pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}")
    if lock:
        _set_param(2, "ПЗРК", f"захват: {lock.get('label')} "
                              f"#{lock.get('unit_id')} "
                              f"({lock.get('distance'):.0f} м)",
                   color=(150, 255, 160, 255))
    elif cd > 0:
        _set_param(2, "ПЗРК", f"перезарядка {cd:.0f} с",
                   color=(240, 210, 130, 255))
    else:
        _set_param(2, "ПЗРК", "—")
    _clear_params_from(3)
    if dpg.does_item_exist("tel_title"):
        dpg.set_value("tel_title", f"игрок {name}")
    _paint_pic("player", None, f"игрок {name}")
    for tag in ("bar_row_fuel", "bar_row_hull", "bar_row_cargo",
                "tel_mounts", "tel_actions", "duty_row"):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, show=False)
    _set("txt_ai_status", "")


def _console_base(state: Dict[str, Any], base_id: int,
                  sig: Dict[str, Any]) -> None:
    b = next((x for x in state.get("bases") or [] if x["id"] == base_id), None)
    if b is None:
        _console_empty(sig)
        return
    from ..bases import BASE_LABELS
    hp = float(b.get("health", 0.0) or 0.0)
    hp_max = float(b.get("health_max", 1.0) or 1.0)
    supply = float(b.get("supply", 0.0) or 0.0)
    regen = float(b.get("supply_regen", 0.0) or 0.0)
    hp_color = ((255, 120, 110, 255) if hp / hp_max < 0.35 else
                (240, 210, 130, 255) if hp / hp_max < 0.75 else
                (190, 225, 190, 255))
    rows = [
        ("ТИП", BASE_LABELS.get(b["kind"], b["kind"]), None),
        ("ПРОЧНОСТЬ", f"{hp:.0f} / {hp_max:.0f}", hp_color),
        ("СНАБЖЕНИЕ", (f"{supply:.0f} / {b.get('supply_max', 0):.0f}"
                       + (f"  (+{regen:.1f}/с)" if regen > 0
                          else "  (только доставка)")),
         (240, 210, 130, 255) if supply < 40 else None),
        ("СТОЯНКИ", f"{b['free_pads']} своб. / {len(b['pads'])}", None),
        ("КООРДИНАТЫ", f"{b['x']:.0f}, {b['z']:.0f}", None),
        ("КУРС", f"{b.get('heading', 0.0):.0f}°"
                 + ("  · в движении" if b.get("moving") else ""), None),
    ]
    if b.get("destroyed"):
        rows.insert(1, ("СОСТОЯНИЕ", "УНИЧТОЖЕНА", (255, 110, 100, 255)))
    elif b.get("shortage"):
        rows.insert(1, ("СОСТОЯНИЕ", "дефицит снабжения",
                        (240, 210, 130, 255)))
    for i in range(B.TEL_PARAM_ROWS):
        if i < len(rows):
            _set_param(i, rows[i][0], rows[i][1], rows[i][2])
        else:
            _set_param(i, "", "")
    if dpg.does_item_exist("tel_title"):
        dpg.set_value("tel_title", f"#{b['id']} {b['name']}")
    _paint_pic(b["kind"], None, f"база {b['name']}")
    for tag in ("bar_row_fuel", "bar_row_hull", "bar_row_cargo",
                "tel_mounts", "tel_actions", "duty_row"):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, show=False)
    _set("txt_ai_status", "")


def _console_empty(sig: Dict[str, Any]) -> None:
    if dpg.does_item_exist("tel_title"):
        dpg.set_value("tel_title", "объект не выбран")
    for i in range(B.TEL_PARAM_ROWS):
        _set_param(i, "", "")
    _bar("bar_fuel", 0.0, "—", False)
    _bar("bar_hull", 0.0, "—", False)
    _bar("bar_cargo", 0.0, "—", False)
    for tag in ("bar_row_fuel", "bar_row_hull", "bar_row_cargo"):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, show=False)
    for i in range(4):
        if dpg.does_item_exist(f"telm_row_{i}"):
            dpg.configure_item(f"telm_row_{i}", show=False)
    for tag in ("tel_mounts", "tel_actions", "duty_row"):
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, show=False)
    if dpg.does_item_exist("txt_duty_hint"):
        dpg.set_value("txt_duty_hint", "")
    _paint_pic("none", None, "—")
    _set("txt_ai_status", "")


def _route_text(rs: Dict[str, Any]) -> str:
    if not rs or not rs.get("name"):
        return "маршрут: —"
    return (f"маршрут: {rs.get('name')} · {rs.get('current', 0)}/"
            f"{rs.get('total', 0)}" + (" · готов" if rs.get("done") else ""))


def _ai_text(ai: Dict[str, Any]) -> str:
    if not ai:
        return "ИИ: выключен"
    return (f"ИИ: {ai.get('ai')} · {ai.get('phase_label', ai.get('phase'))}"
            + (f" · цель {ai['target']}" if ai.get("target") else ""))


def _set(tag: str, value: str) -> None:
    if dpg.does_item_exist(tag):
        dpg.set_value(tag, value)


def _bar(tag: str, frac: float, overlay: str, show: bool) -> None:
    """Полоса ресурса + цифры РЯДОМ с ней.

    Заказчик: «цифры Топливо/Корпус белые на светлом фоне — нечитаемо».
    Overlay прогресс-бара в DPG рисуется цветом mvThemeCol_Text поверх
    яркого fill'а, поэтому контраст непредсказуем. Решение v15: overlay не
    используется вовсе, значение печатается отдельным текстом на тёмной
    подложке панели — контраст гарантирован при любом заполнении.
    """
    row = tag.replace("bar_", "bar_row_")
    if dpg.does_item_exist(row):
        dpg.configure_item(row, show=show)
    if dpg.does_item_exist(tag) and show:
        dpg.set_value(tag, max(0.0, min(1.0, frac)))
        try:
            dpg.configure_item(tag, overlay="")
        except Exception:  # noqa: BLE001
            pass
    txt = tag.replace("bar_", "txt_bar_")
    if dpg.does_item_exist(txt):
        dpg.set_value(txt, overlay if show else "")


# ---------------------------------------------------------------- планировщик
def _planner_sig(state: Dict[str, Any]) -> Tuple:
    pts = state["planner"]["points"]
    return tuple((round(p["x"], 1), round(p["z"], 1),
                  round(p.get("alt", 150.0), 1), p.get("action"))
                 for p in pts)


def _project_planner(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    key = _planner_sig(state)
    if sig.get("planner") == key:
        return
    sig["planner"] = key
    pts = state["planner"]["points"]
    for i in range(st.MAX_PLANNER_ROWS):
        tag = f"wp_row_{i}"
        if not dpg.does_item_exist(tag):
            continue
        if i < len(pts):
            p = pts[i]
            dpg.configure_item(tag, show=True)
            dpg.set_value(f"txt_wp_{i}",
                          f"{i + 1:>2}. {p['x']:7.0f} {p['z']:7.0f} "
                          f"h{p.get('alt', 150.0):.0f}")
            cmb = f"cmb_wp_act_{i}"
            label = _action_label(p.get("action", "navigate"))
            items = dpg.get_item_info(cmb).get("items") or []
            if label not in items:
                dpg.configure_item(cmb, items=[label] + list(items))
            dpg.set_value(cmb, label)
        else:
            dpg.configure_item(tag, show=False)
    if dpg.does_item_exist("txt_route_status"):
        dpg.set_value("txt_route_status",
                      _route_text(state.get("route_status") or {}))


# -------------------------------------------------------------------- игроки
def _project_players(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    frame = state.get("frame") or {}
    players = frame.get("players") or {}
    manpads = state.get("manpads") or {}
    key = (tuple(sorted(players)),
           tuple(sorted((n, str(m)) for n, m in manpads.items())))
    if sig.get("players") == key:
        return
    sig["players"] = key
    names = sorted(players)
    for i in range(st.MAX_PLAYER_ROWS):
        tag = f"txt_player_{i}"
        if not dpg.does_item_exist(tag):
            continue
        if i < len(names):
            name = names[i]
            p = players[name]
            pos = p.get("pos") or (0, 0, 0)
            mp = manpads.get(name) or {}
            lock = mp.get("lock")
            cd = mp.get("cooldown") or 0.0
            mp_txt = ""
            if lock:
                mp_txt = (f" · ◎ {lock.get('label')} #{lock.get('unit_id')} "
                          f"({lock.get('distance'):.0f} м)")
            elif cd > 0:
                mp_txt = f" · ⟳ {cd:.0f} с"
            dpg.configure_item(tag, show=True)
            dpg.set_value(tag,
                          f"{name}: ({pos[0]:.0f}, {pos[1]:.0f}, "
                          f"{pos[2]:.0f}){mp_txt}")
            dpg.configure_item(tag, color=(150, 255, 160, 255) if lock
                               else (205, 220, 205, 255))
        else:
            dpg.configure_item(tag, show=False)


# -------------------------------------------------------------------- журнал
def _project_log(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    rev = state.get("log_rev", 0)
    if sig.get("log") == rev:
        return
    sig["log"] = rev
    # новые записи СВЕРХУ: автопрокрутка child-окна ненадёжна между версиями
    lines = list(state["log"])[-st.MAX_LOG_ROWS:][::-1]
    for i in range(st.MAX_LOG_ROWS):
        tag = f"txt_log_{i}"
        if not dpg.does_item_exist(tag):
            continue
        if i < len(lines):
            text, level = lines[i]
            dpg.configure_item(
                tag, show=True,
                color=st.LOG_LEVEL_COLORS.get(level, (200, 214, 200, 255)))
            dpg.set_value(tag, text)
        else:
            dpg.configure_item(tag, show=False)


# ----------------------------------------------------------------- библиотека
def _project_library(state: Dict[str, Any], sig: Dict[str, Any]) -> None:
    names = state.get("route_library")
    if names is None or sig.get("lib") == tuple(names):
        return
    sig["lib"] = tuple(names)
    items = ["—"] + list(names)
    if dpg.does_item_exist("cmb_route_lib"):
        cur = dpg.get_value("cmb_route_lib")
        dpg.configure_item("cmb_route_lib", items=items)
        dpg.set_value("cmb_route_lib", cur if cur in items else "—")
