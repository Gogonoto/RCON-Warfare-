"""
Построение статического интерфейса (секции 1-4 скилла tactical-ui-dearpygui).

ВСЕ виджеты создаются ОДИН раз здесь (I2) и адресуются СТРОКОВЫМИ ТЕГАМИ (I3).
Каждый кадр `project.py` проецирует STATE в эти теги через set_value/configure;
колбэки не содержат логики — они мутируют STATE (главный поток) или кладут
команду в OUT_Q через `facade.send` (I1/I5).

Компоновка v14 (по разбору кадра заказчиком):
* сверху — узкая СТАТУС-СТРОКА (без кнопок): соединение, координаты, fps;
* СЛЕВА — скрываемый ХОТБАР только с инструментами построения маршрутов
  (иконки без подписей): точка, удаление точки, назначить, очистить;
* карта во всё оставшееся место; снизу — ПУЛЬТ любого объекта (техника,
  игрок, база) с силуэтом и ТАБЛИЦАМИ параметров/подвесов;
* справа — стек СЕКЦИЙ: кликабельный холст-заголовок (шеврон + иконка +
  название + линия) и тело. «Запуск» и «Базы» РАЗДЕЛЕНЫ, списки — таблицы
  с колонками и заголовками (жалоба «параметры лежат в куче»).
* массовые интеракции вынесены в КОНТЕКСТНОЕ МЕНЮ по ПКМ (context.py).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

import logging

import dearpygui.dearpygui as dpg

log = logging.getLogger(__name__)

from .. import uicons
from ..bases import BASE_LABELS, KIND_AIRPORT, KIND_CARRIER, KIND_GROUND
from ..routes import ACTION_LABELS, Action
from ..units import VARIANTS
from . import iconwidget as iw
from . import state as st
from . import theme as T
from .mapfacade import TEX_REGISTRY, MapFacade

# ---------------------------------------------------------------------------
#  Геометрия раскладки (единственное место)
# ---------------------------------------------------------------------------
PAD = 6
TOOL_W = 34          # ширина хотбара-иконко rail
OBJ_W = 306          # ширина левой панели объекта (под размер силуэта)

#: режимы левого клика (инструменты хотбара). Порядок = порядок иконок.
HOTBAR_MODES = (("select", "pointer", "Выбор объекта (клик по карте)"),
                ("waypoint", "map_pin", "Точка маршрута (клик по карте)"),
                ("wp_del", "pin_x", "Удалить точку (клик по точке)"),
                ("strike", "crosshair", "Зона удара (два клика по карте)"),
                ("base", "home", "Поставить базу (клик по карте)"),
                ("carrier", "anchor", "Курс авианосца (клик по карте)"))
#: разовые действия хотбара
HOTBAR_ACTIONS = (("assign", "route", "Назначить черновик маршрута выбранному"),
                  ("clear", "trash", "Очистить черновик и маршрут"),
                  ("scan", "scan", "Сканировать рельеф вокруг центра карты"),
                  ("center", "target", "Центрировать карту на выбранном"),
                  ("follow", "eye", "Следить за выбранным объектом"),
                  ("sound", "volume", "Звук вкл/выкл"))

#: иконки секций правого дока
SECTION_ICONS = {"conn": "plug", "launch": "plane_takeoff", "bases": "home",
                 "units": "users", "ai": "cpu", "progress": "gauge",
                 "planner": "route",
                 "players": "radio", "layers": "layers",
                 "settings": "sliders", "log": "terminal", "view": "eye"}

_SECTION_OPEN: Dict[str, bool] = {}
_SECTION_META: Dict[str, tuple] = {}
_TOOL_STATE: Dict[str, Any] = {}


def tcol(text: str, width: int, tag: Optional[str] = None,
         color: Optional[tuple] = None, show: bool = True) -> str:
    """Текст в колонке фиксированной ширины (add_text в DPG 2.x без width)."""
    with dpg.group(width=width, show=show):
        kw: Dict[str, Any] = {}
        if color is not None:
            kw["color"] = color
        if tag:
            kw["tag"] = tag
        dpg.add_text(text, **kw)
        return kw.get("tag", "")


def right_w() -> int:
    return int(_TOOL_STATE.get("settings_ui_right_w", 444))


def layout() -> None:
    """Разложить окна под клиентский размер вьюпорта.

    Сетка v15 (жалоба «огромная пустая полоса сверху»): верхней статус-строки
    больше нет — карта и панели занимают всю высоту окна, а живые показатели
    (соединение, координаты, fps, скан) уехали в заголовок окна карты.
    Слева направо: хотбар-иконки | панель объекта | карта | правые секции.
    """
    try:
        vw = dpg.get_viewport_client_width() or dpg.get_viewport_width()
        vh = dpg.get_viewport_client_height() or dpg.get_viewport_height()
    except Exception:  # noqa: BLE001
        vw, vh = dpg.get_viewport_width(), dpg.get_viewport_height()
    rw = right_w()
    hotbar = bool(_TOOL_STATE.get("hotbar", True))
    obj = bool(_TOOL_STATE.get("objpanel", True))
    h = max(240.0, vh - 2 * PAD)

    x = PAD
    if hotbar:
        _place("win_tools", x, PAD, TOOL_W, h)
        _place("win_tools_tab", -400, -400, 0, 0)
        x += TOOL_W + PAD
    else:
        _place("win_tools", -400, -400, 0, 0)
        _place("win_tools_tab", x, PAD, 20, 64)
        x += 20 + PAD
    if obj:
        _place("win_obj", x, PAD, OBJ_W, h)
        _place("win_obj_tab", -400, -400, 0, 0)
        x += OBJ_W + PAD
    else:
        _place("win_obj", -400, -400, 0, 0)
        _place("win_obj_tab", x, PAD, 20, 64)
        x += 20 + PAD

    map_w = max(320.0, vw - rw - x - PAD)
    _place("win_map", x, PAD, map_w, h)
    _place("win_right", vw - rw - PAD, PAD, rw, h)


def obj_client_w() -> int:
    """Рабочая ширина панели объекта (минус рамка окна и отступы)."""
    return max(120, OBJ_W - 20)


def _place(tag: str, x: float, y: float, w: float, h: float) -> None:
    if not dpg.does_item_exist(tag):
        return
    dpg.configure_item(tag, pos=(int(x), int(y)), width=int(w), height=int(h))


def pen_client_size() -> tuple:
    """Размер пера карты: окно минус рамка и заголовок."""
    w, h = dpg.get_item_rect_size("win_map")
    return (max(64.0, w - 12.0), max(64.0, h - 34.0))


def bind_state(state: Dict[str, Any]) -> None:
    _TOOL_STATE.clear()
    _TOOL_STATE.update(state)


# ---------------------------------------------------------------------------
#  Построение
# ---------------------------------------------------------------------------
def build_static_ui(state: Dict[str, Any], facade, map_ui: MapFacade,
                    on_exit: Optional[Callable[[], None]] = None) -> None:
    """Создать все постоянные элементы. Вызывается один раз до цикла."""
    bind_state(state)
    _TOOL_STATE["facade"] = facade
    _TOOL_STATE["on_exit"] = on_exit
    _TOOL_STATE["hotbar"] = bool(state.get("hotbar", True))
    _TOOL_STATE["objpanel"] = bool(state.get("objpanel", True))
    dpg.add_texture_registry(tag=TEX_REGISTRY)

    _build_hotbar()
    _build_object_panel(state, facade)
    _build_map_window(state, facade, map_ui)
    _build_right_docks(state, facade)
    _build_launch_and_dialogs(state, facade)
    layout()
    _paint_hotbar(state)


# ------------------------------------------------------------- хотбар (rail)
def _tip(parent: str, text: str) -> None:
    """Тултип вместо подписи: заказчик просил убрать мелкий текст-подсказки.

    `add_tooltip` — НЕ контекст-менеджер в DPG 2.x: вызов `dpg.tooltip(...)`
    внутри активного контейнера роняет стек контейнеров («No container to
    pop»). Поэтому создаём тултип функцией с явным `parent=`, а текст
    добавляем внутрь него вторым вызовом. Ошибка в одном тултипе не должна
    валить всю сборку интерфейса.
    """
    if not dpg.does_item_exist(parent):
        return
    # ВАЖНО: тултип на контейнере (таблица, строка таблицы, окно, child)
    # роняет DPG насмерть — это abort в нативном слое, try/except его не
    # берёт. Поэтому вешаем подсказки только на листовые виджеты.
    try:
        kind = dpg.get_item_type(parent)
    except Exception:  # noqa: BLE001
        return
    if kind in _TIP_UNSAFE:
        log.debug("Тултип пропущен: %s — контейнер %s", parent, kind)
        return
    try:
        tip = dpg.add_tooltip(parent, delay=0.3)
        dpg.add_text(text, parent=tip, wrap=250, color=T.TEXT)
    except Exception:  # noqa: BLE001
        log.debug("Тултип для %s не создан", parent, exc_info=True)


#: типы-контейнеры, на которых add_tooltip приводит к падению процесса
_TIP_UNSAFE = frozenset({
    "mvAppItemType::mvTable", "mvAppItemType::mvTableRow",
    "mvAppItemType::mvTableColumn", "mvAppItemType::mvWindowAppItem",
    "mvAppItemType::mvChildWindow", "mvAppItemType::mvGroup",
    "mvAppItemType::mvTooltip", "mvAppItemType::mvMenuBar",
})


def _build_hotbar() -> None:
    """Вертикальный rail иконок слева.

    v15: наложения исправлены жёстким порядком — каждый ребёнок создаётся с
    явным `parent=` и uniforme шагом, никаких вложенных контекстов. Иконок
    больше (6 режимов + 6 действий), все подписи уехали в тултипы.
    """
    col = "hotbar_col"
    with dpg.window(tag="win_tools", no_title_bar=True, no_move=True,
                    no_resize=True, no_scrollbar=True):
        dpg.add_group(tag=col, horizontal=False)
        dpg.add_spacer(height=4, parent=col)
        for key, icon, tip in HOTBAR_MODES:
            tag = f"ico_tool_{key}"
            iw.icon_canvas(tag, col, icon, 20, T.TEXT_DIM, 1.7,
                           callback=_cb_tool, user_data=key)
            _tip(tag, tip)
            dpg.add_spacer(height=8, parent=col)
        _rail_sep(col, "hotbar_sep")
        for key, icon, tip in HOTBAR_ACTIONS:
            tag = f"ico_act_{key}"
            iw.icon_canvas(tag, col, icon, 20, T.TEXT_DIM, 1.7,
                           callback=_cb_hotbar_action, user_data=key)
            _tip(tag, tip)
            dpg.add_spacer(height=8, parent=col)
        _rail_sep(col, "hotbar_sep2")
        iw.icon_canvas("ico_hot_hide", col, "chevron_left", 18, T.TEXT_DIM,
                       1.7, callback=_cb_hotbar_hide)
        _tip("ico_hot_hide", "Скрыть хотбар")
    with dpg.window(tag="win_tools_tab", no_title_bar=True, no_move=True,
                    no_resize=True, no_scrollbar=True, show=False):
        iw.icon_canvas("ico_hot_show", "win_tools_tab", "chevron_right", 16,
                       T.ACCENT, 1.8, callback=_cb_hotbar_show)
        _tip("ico_hot_show", "Показать хотбар")


def _rail_sep(col: str, tag: str) -> None:
    """Тонкий разделитель rail'а: drawlist фиксированной высоты, без наложений."""
    dpg.add_spacer(height=3, parent=col)
    dpg.add_drawlist(width=TOOL_W - 10, height=2, tag=tag, parent=col)
    dpg.draw_line((1, 1), (TOOL_W - 11, 1), color=T.BORDER, thickness=1,
                  parent=tag)
    dpg.add_spacer(height=5, parent=col)


def _cb_hotbar_hide(sender=None, app_data=None, user_data=None) -> None:
    _TOOL_STATE["hotbar"] = False
    _TOOL_STATE["facade"].sound_play("ui")
    dpg.configure_item("win_tools_tab", show=True)
    if dpg.does_item_exist("chk_view_hotbar"):
        dpg.set_value("chk_view_hotbar", False)
    layout()


def _cb_hotbar_show(sender=None, app_data=None, user_data=None) -> None:
    _TOOL_STATE["hotbar"] = True
    _TOOL_STATE["facade"].sound_play("ui")
    dpg.configure_item("win_tools_tab", show=False)
    if dpg.does_item_exist("chk_view_hotbar"):
        dpg.set_value("chk_view_hotbar", True)
    layout()


def _cb_hotbar_action(sender, app_data, user_data) -> None:
    """Разовые действия хотбара: без логики в колбэке, только STATE/OUT_Q."""
    state, facade = _TOOL_STATE, _TOOL_STATE["facade"]
    facade.sound_play("ui")
    if user_data == "assign":
        pts = state["planner"]["points"]
        facade.send("assign_route_points", None, list(pts))
    elif user_data == "clear":
        st.planner_clear(state)
        facade.send("clear_route", None)
    elif user_data == "scan":
        cx, cz = facade.renderer.transform.center
        facade.send("scan", cx, cz, 300, 8)
    elif user_data == "center":
        facade.center_on_selection()
    elif user_data == "follow":
        follow = not bool(state.get("follow"))
        state["follow"] = follow
        facade.follow_uid = state.get("selection") if follow else None
        if dpg.does_item_exist("chk_follow"):
            dpg.set_value("chk_follow", follow)
    elif user_data == "sound":
        if dpg.does_item_exist("chk_sound"):
            _cb_sound_toggle()
    _paint_hotbar(state)


def _paint_hotbar(state) -> None:
    """Активный инструмент и состояния «следить/звук» — цветом иконки."""
    from . import iconwidget as _iw
    tool = state.get("tool", "select")
    for key, _icon, _tip_txt in HOTBAR_MODES:
        tag = f"ico_tool_{key}"
        if dpg.does_item_exist(tag):
            _iw.redraw(tag, color=T.ACCENT if key == tool else T.TEXT_DIM)
    if dpg.does_item_exist("ico_act_follow"):
        _iw.redraw("ico_act_follow",
                   color=T.ACCENT if state.get("follow") else T.TEXT_DIM)
    if dpg.does_item_exist("ico_act_sound"):
        on = bool(_TOOL_STATE["facade"].settings.sound_enabled)
        _iw.redraw("ico_act_sound", name="volume" if on else "volume_x",
                   color=T.ACCENT if on else T.TEXT_DIM)


def _cb_tool(sender, app_data, user_data) -> None:
    """Переключение режима левого клика: инструмент хотбара или 'select'."""
    cur = _TOOL_STATE.get("tool", "select")
    _TOOL_STATE["tool"] = "select" if cur == user_data else user_data
    _TOOL_STATE["facade"].sound_play("ui")
    _paint_hotbar(_TOOL_STATE)


# --------------------------------------------------------------------- карта
def _build_map_window(state: Dict[str, Any], facade, map_ui: MapFacade) -> None:
    """Окно карты на всю высоту: статус-строка переехала в его заголовок.

    Заказчик: «убрать огромную пустую полосу сверху». Отдельного окна
    `win_top` больше нет — соединение, координаты курсора, число юнитов, fps
    и прогресс скана пишет в label этого окна `project._project_statusline`
    (обновление текста заголовка стоит микросекунды и не плодит виджеты).
    """
    with dpg.window(tag="win_map", label="ТАКТИЧЕСКАЯ КАРТА", no_move=True,
                    no_resize=True, no_scrollbar=True):
        map_ui.create("win_map", 800.0, 600.0)


# ------------------------------------------------------- панель объекта (слева)
TEL_PARAM_ROWS = 10
BAR_W = 132


def _bar_theme(tag: str, color: tuple) -> None:
    """Тема прогресс-бара: тёмный жёлоб, яркий fill, без «радуги»."""
    th = dpg.add_theme(tag=tag + "_th")
    comp = dpg.add_theme_component(parent=th)
    dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, list(color),
                        category=dpg.mvThemeCat_Core, parent=comp)
    dpg.add_theme_color(dpg.mvThemeCol_PlotHistogramHovered,
                        [min(255, c + 40) for c in color[:3]] + [255],
                        category=dpg.mvThemeCat_Core, parent=comp)
    dpg.add_theme_color(dpg.mvThemeCol_FrameBg, [14, 18, 15, 255],
                        category=dpg.mvThemeCat_Core, parent=comp)
    dpg.add_theme_color(dpg.mvThemeCol_Border, list(T.BORDER),
                        category=dpg.mvThemeCat_Core, parent=comp)
    dpg.bind_item_theme(tag, th)


def _group_label(text: str, icon: Optional[str] = None,
                 color=None) -> None:
    """Мелкий заголовок группы внутри секций правого дока."""
    with dpg.group(horizontal=True) as g:
        if icon:
            iw.icon_canvas(f"ico_lbl_{icon}_{text}", g,
                           icon, 13, color or T.TEXT_DIM, 1.5)
        lbl = dpg.add_text(text, color=color or T.TEXT_DIM)
        T.bind_bold(lbl)


def _obj_header(text: str, icon: Optional[str] = None) -> None:
    """Заголовок блока панели объекта: тонкая линия + мелкая иконка."""
    w = obj_client_w()
    tag = "objhd_" + text.replace(" ", "_")
    dpg.add_drawlist(width=w, height=16, tag=tag)
    prims: list = [{"type": "line", "x1": 0, "y1": 13.5, "x2": w, "y2": 13.5,
                    "color": "#2c3a2e", "width": 1, "alpha": 220}]
    if icon:
        prims.extend(uicons.prims(icon, 7.0, 8.0, 11.0, T.TEXT_DIM, 1.5))
        prims.append({"type": "text", "x": 15, "y": 3, "text": text,
                      "color": T.TEXT_DIM, "anchor": "nw", "size": 11})
    else:
        prims.append({"type": "text", "x": 1, "y": 3, "text": text,
                      "color": T.TEXT_DIM, "anchor": "nw", "size": 11})
    iw.paint_raw(tag, prims)


def _build_object_panel(state: Dict[str, Any], facade) -> None:
    """Компактная панель объекта в ЛЕВОЙ колонке (v15).

    Жалобы заказчика, которые она закрывает:
    * «сильно уменьшить место, подогнать под размер иконки» — ширина панели
      равна ширине силуэта (OBJ_W), всё в одну колонку, без пустых полей;
    * «убрать/уменьшить заголовок "Пульт объекта"» — окна без title bar,
      сверху только имя объекта;
    * «чекбокс "Следить" — в угол» — он в правой части шапки;
    * «компактная таблица параметров без лишних отступов» — таблица 2×N
      с no_pad_innerX и без вертикальных границ;
    * «цифры Топливо/Корпус белые на светлом фоне» — цифры вынесены ИЗ бара
      в отдельный текст на тёмной подложке (контраст гарантирован);
    * «повысить читаемость таблицы подвесов» — строки с фоном, имя оружия
      светлым, боезапас моноширинно справа, огонь отдельной иконкой.
    """
    w = obj_client_w()
    with dpg.window(tag="win_obj", no_title_bar=True, no_move=True,
                    no_resize=True, no_scrollbar=True):
        with dpg.child_window(tag="obj_scroll", height=-1,
                              horizontal_scrollbar=False):
            # --- шапка: имя объекта + «Следить» в углу -------------------
            with dpg.group(horizontal=True, tag="obj_hdr"):
                iw.icon_canvas("ico_obj", "obj_hdr", "gauge", 15, T.ACCENT, 1.6)
                dpg.add_text("ОБЪЕКТ", tag="tel_title", color=T.TEXT)
                T.bind_bold("tel_title")
                dpg.add_spacer(width=6)
                dpg.add_checkbox(label="Следить", tag="chk_follow",
                                 default_value=False, callback=_cb_follow)
            _tip("chk_follow", "Центрировать карту на выбранном объекте")
            dpg.add_combo(["—"], width=w - 2, tag="cmb_console_obj",
                          default_value="—", callback=_cb_console_obj)
            _tip("cmb_console_obj",
                 "Объект пульта: техника, игрок или база")

            # --- силуэт: размер задаёт ширину всей панели -----------------
            dpg.add_drawlist(width=w - 2, height=92, tag="tel_pic")
            dpg.add_text("—", tag="tel_pic_label", color=T.TEXT_DIM,
                         wrap=w - 2)

            # --- параметры: компактная таблица 2 колонки ------------------
            _obj_header("ПАРАМЕТРЫ", "sliders")
            with dpg.table(header_row=False, borders_innerV=False,
                           borders_innerH=False, borders_outerH=False,
                           borders_outerV=False, row_background=True,
                           no_pad_innerX=True, pad_outerX=False,
                           policy=dpg.mvTable_SizingFixedFit,
                           tag="tel_params", width=w - 2):
                dpg.add_table_column(width_fixed=True,
                                     init_width_or_weight=104)
                dpg.add_table_column(width_fixed=True,
                                     init_width_or_weight=w - 110)
                for i in range(TEL_PARAM_ROWS):
                    with dpg.table_row(tag=f"telp_row_{i}"):
                        dpg.add_text("—", tag=f"telp_k_{i}", color=T.TEXT_DIM)
                        dpg.add_text("—", tag=f"telp_v_{i}")

            # --- ресурс: цифры ВЫНЕСЕНЫ из бара (читаемость) --------------
            _obj_header("РЕСУРС", "droplet")
            for key, label, color in (("fuel", "ТОПЛ", T.ACCENT),
                                      ("hull", "КОРП", T.AMBER),
                                      ("cargo", "ГРУЗ", T.CYAN)):
                with dpg.group(horizontal=True, tag=f"bar_row_{key}"):
                    tcol(label, 34, color=T.TEXT_DIM)
                    dpg.add_progress_bar(default_value=0.0, width=BAR_W,
                                         height=13, tag=f"bar_{key}")
                    tcol("—", max(60, w - BAR_W - 42), tag=f"txt_bar_{key}",
                         color=color)
                _bar_theme(f"bar_{key}", color)

            # --- подвесы ---------------------------------------------------
            _obj_header("ПОДВЕСЫ", "box")
            with dpg.table(header_row=True, borders_innerV=False,
                           borders_innerH=True, borders_outerH=False,
                           borders_outerV=False, row_background=True,
                           no_pad_innerX=True, pad_outerX=False,
                           policy=dpg.mvTable_SizingFixedFit,
                           tag="tel_mounts", width=w - 2):
                dpg.add_table_column(label="№", width_fixed=True,
                                     init_width_or_weight=18)
                dpg.add_table_column(label="ОРУЖИЕ", width_fixed=True,
                                     init_width_or_weight=w - 116)
                dpg.add_table_column(label="Б/К", width_fixed=True,
                                     init_width_or_weight=52)
                dpg.add_table_column(label="", width_fixed=True,
                                     init_width_or_weight=24)
                for i in range(4):
                    with dpg.table_row(tag=f"telm_row_{i}"):
                        dpg.add_text(f"{i + 1}", tag=f"telm_n_{i}",
                                     color=T.TEXT_DIM)
                        dpg.add_combo(["—"], tag=f"cmb_wpn_{i}",
                                      width=w - 122, user_data=i,
                                      callback=_cb_load_weapon,
                                      default_value="—")
                        dpg.add_text("—", tag=f"txt_ammo_{i}")
                        iw.icon_canvas(f"ico_fire_{i}", f"telm_row_{i}",
                                       "zap", 14, T.RED, 1.6,
                                       callback=_cb_fire_mount, user_data=i)
                    _tip(f"cmb_wpn_{i}", "Сменить оружие на этом подвесе")
                    _tip(f"ico_fire_{i}", "Выстрел из этого подвеса")

            # --- управление: сетка 3×3 без «Паузы» и ручного ТО ------------
            _obj_header("УПРАВЛЕНИЕ", "zap")
            bw = (w - 8) // 3
            with dpg.group(horizontal=True, tag="tel_actions"):
                dpg.add_button(label="ОГОНЬ", width=bw, height=26,
                               tag="btn_fire",
                               callback=lambda: facade.send("fire_selected"))
                dpg.add_button(label="Выровнять", width=bw, height=26,
                               callback=lambda: facade.send("level"))
                dpg.add_button(label="Снять", width=bw, height=26,
                               callback=_cb_despawn)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Центр", width=bw, height=24,
                               callback=lambda: facade.center_on_selection())
                dpg.add_button(label="Маршрут", width=bw, height=24,
                               callback=lambda: _cb_hotbar_action(None, None,
                                                                  "assign"))
                dpg.add_button(label="Очистить", width=bw, height=24,
                               callback=lambda: _cb_hotbar_action(None, None,
                                                                  "clear"))
            _tip("btn_fire", "Огонь из всех готовых подвесов по текущей цели")
            _tip("tel_actions",
                 "Заправка, снаряжение и ТО выполняются автоматически на базе "
                 "при возврате — ручных кнопок больше нет")
            # сегментный тумблер режима службы
            with dpg.group(horizontal=True, tag="duty_row"):
                dpg.add_button(label="СТОЯНКА", width=(w - 6) // 2, height=26,
                               tag="btn_duty_park", callback=_cb_duty_park)
                dpg.add_button(label="В БОЙ", width=(w - 6) // 2, height=26,
                               tag="btn_duty_combat", callback=_cb_duty_combat)
            _tip("btn_duty_park",
                 "Стоянка: автовозврат на приписную базу и автоматическое ТО "
                 "по касанию")
            _tip("btn_duty_combat",
                 "В бой: автовзлёт со стоянки и дальше маршрут оператора или "
                 "патруль вокруг точки взлёта")
            dpg.add_text("", tag="txt_duty_hint", color=T.TEXT_DIM, wrap=w - 2)
    with dpg.window(tag="win_obj_tab", no_title_bar=True, no_move=True,
                    no_resize=True, no_scrollbar=True, show=False):
        iw.icon_canvas("ico_obj_show", "win_obj_tab", "chevron_right", 16,
                       T.ACCENT, 1.8, callback=_cb_obj_show)
        _tip("ico_obj_show", "Показать панель объекта")
    _build_duty_theme()


def _build_duty_theme() -> None:
    """Темы сегментов тумблера «Стоянка / В бой»: активный подсвечен."""
    for tag, rgb in (("btn_duty_park", (58, 74, 60, 255)),
                     ("btn_duty_combat", (120, 46, 40, 255))):
        th = dpg.add_theme(tag=tag + "_act_th")
        comp = dpg.add_theme_component(parent=th)
        dpg.add_theme_color(dpg.mvThemeCol_Button, list(rgb),
                            category=dpg.mvThemeCat_Core, parent=comp)
        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,
                            [min(255, c + 22) for c in rgb[:3]] + [255],
                            category=dpg.mvThemeCat_Core, parent=comp)
        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, list(rgb),
                            category=dpg.mvThemeCat_Core, parent=comp)
        dpg.add_theme_color(dpg.mvThemeCol_Text, (240, 248, 240, 255),
                            category=dpg.mvThemeCat_Core, parent=comp)
        dpg.bind_item_theme(tag, th)
    # «ОГОНЬ» — красный, как просил заказчик
    th = dpg.add_theme(tag="btn_fire_th")
    comp = dpg.add_theme_component(parent=th)
    dpg.add_theme_color(dpg.mvThemeCol_Button, (150, 42, 36, 255),
                        category=dpg.mvThemeCat_Core, parent=comp)
    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (186, 54, 46, 255),
                        category=dpg.mvThemeCat_Core, parent=comp)
    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (120, 32, 28, 255),
                        category=dpg.mvThemeCat_Core, parent=comp)
    dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 240, 238, 255),
                        category=dpg.mvThemeCat_Core, parent=comp)
    dpg.bind_item_theme("btn_fire", th)


def _cb_duty_park(sender=None, app_data=None, user_data=None) -> None:
    _TOOL_STATE["facade"].sound_play("ui")
    _TOOL_STATE["facade"].send("set_duty", None, "park")


def _cb_duty_combat(sender=None, app_data=None, user_data=None) -> None:
    _TOOL_STATE["facade"].sound_play("ui")
    _TOOL_STATE["facade"].send("set_duty", None, "combat")


def _cb_obj_show(sender=None, app_data=None, user_data=None) -> None:
    _TOOL_STATE["objpanel"] = True
    _TOOL_STATE["facade"].sound_play("ui")
    dpg.configure_item("win_obj_tab", show=False)
    if dpg.does_item_exist("chk_view_obj"):
        dpg.set_value("chk_view_obj", True)
    layout()


def _cb_obj_hide(sender=None, app_data=None, user_data=None) -> None:
    _TOOL_STATE["objpanel"] = False
    _TOOL_STATE["facade"].sound_play("ui")
    dpg.configure_item("win_obj_tab", show=True)
    if dpg.does_item_exist("chk_view_obj"):
        dpg.set_value("chk_view_obj", False)
    layout()


def _cb_console_obj(sender, app_data, user_data) -> None:
    """Пульт принимает любой объект: техника, игрок, база (UX-14)."""
    mapping: List[tuple] = _TOOL_STATE.get("_console_map", [])
    label = app_data or "—"
    obj = next((o for l, o in mapping if l == label), None)
    _TOOL_STATE["console_obj"] = obj
    facade = _TOOL_STATE["facade"]
    facade.sound_play("ui")
    if obj and obj[0] == "unit":
        _TOOL_STATE["selection"] = obj[1]
        facade.send("select", obj[1])
    elif obj and obj[0] == "base":
        _TOOL_STATE["selected_base"] = obj[1]
    elif obj is None:
        _TOOL_STATE["selection"] = None


def _cb_despawn(sender, app_data, user_data) -> None:
    uid = _TOOL_STATE.get("selection")
    if uid is not None:
        _TOOL_STATE["facade"].send("despawn", uid, False)


def _cb_load_weapon(sender, app_data, user_data) -> None:
    uid = _TOOL_STATE.get("selection")
    if uid is None:
        return
    key: Optional[str] = None
    if app_data not in ("—", "", None):
        from ..weapons import WEAPONS
        key = next((k for k, w in WEAPONS.items()
                    if w["label"] == app_data), None)
    _TOOL_STATE["facade"].send("load_weapon", uid, int(user_data), key)


def _cb_fire_mount(sender, app_data, user_data) -> None:
    _TOOL_STATE["facade"].send("fire_selected", None, int(user_data))


#: префиксы пунктов списка целей -> как их понимает фасад
AI_TARGET_NONE = "— (ближайший игрок)"


def _cb_ai_on(sender, app_data, user_data) -> None:
    """Включить автопилот: цель берётся из выпадающего списка (UX-15)."""
    tpl = dpg.get_value("cmb_ai")
    label = dpg.get_value("cmb_ai_target") or AI_TARGET_NONE
    target = "" if label == AI_TARGET_NONE else str(label)
    _TOOL_STATE["facade"].send("set_ai", None, tpl, target)


def _cb_refuel(sender, app_data, user_data) -> None:
    uid = _TOOL_STATE.get("selection")
    if uid is not None:
        _TOOL_STATE["facade"].send("refuel", uid)


def _cb_rearm(sender, app_data, user_data) -> None:
    uid = _TOOL_STATE.get("selection")
    if uid is not None:
        _TOOL_STATE["facade"].send("rearm", uid)


def _cb_follow(sender, app_data, user_data) -> None:
    _TOOL_STATE["follow"] = bool(app_data)
    if app_data and _TOOL_STATE.get("selection") is not None:
        _TOOL_STATE["facade"].follow_uid = _TOOL_STATE["selection"]
    else:
        _TOOL_STATE["facade"].follow_uid = None


# ---------------------------------------------------------- пресеты запуска
def _build_launch_and_dialogs(state: Dict[str, Any], facade) -> None:
    """Окна, которых нет в доках: редактор пресетов и диалог создания базы.

    Созются один раз (I2) и показываются по требованию; редактор — модальный,
    в пол-экрана, поверх всего (заказчик: «редактор пресетов — отдельное окно
    в пол-экрана поверх всего»).
    """
    _build_preset_editor(facade)
    _build_base_dialog()


def _build_preset_editor(facade) -> None:
    variants = [(k, v.label) for k, v in VARIANTS.items()]
    with dpg.window(tag="win_preset_editor", label="РЕДАКТОР ПРЕСЕТА",
                    modal=True, show=False, width=760, height=520,
                    no_resize=False, no_collapse=True):
        with dpg.group(horizontal=True):
            tcol("Имя", 60, color=T.TEXT_DIM)
            dpg.add_input_text(tag="pe_name", width=320,
                               hint="название пресета")
            dpg.add_button(label="Сохранить", width=110, height=22,
                           callback=_cb_pe_save)
            dpg.add_button(label="Сохранить как…", width=130, height=22,
                           callback=_cb_pe_save_as)
        with dpg.group(horizontal=True):
            tcol("Техника", 60, color=T.TEXT_DIM)
            dpg.add_combo([lbl for _, lbl in variants], width=260,
                          tag="pe_variant", default_value=variants[0][1],
                          callback=_cb_pe_variant)
            tcol("Высота", 60, color=T.TEXT_DIM)
            dpg.add_input_float(tag="pe_alt", width=90, default_value=150.0,
                                format="%.0f")
            dpg.add_checkbox(label="бот", tag="pe_bot", default_value=False)
        with dpg.group(horizontal=True):
            tcol("ИИ", 60, color=T.TEXT_DIM)
            dpg.add_combo(["—"] + [t for t, _ in facade.ai_templates()],
                          width=260, tag="pe_ai", default_value="—")
            dpg.add_button(label="Удалить пресет", width=130, height=22,
                           callback=_cb_pe_delete)
            dpg.add_button(label="Закрыть", width=90, height=22,
                           callback=lambda: dpg.configure_item(
                               "win_preset_editor", show=False))
        dpg.add_separator()
        with dpg.group(horizontal=True):
            # --- слоты подвесов: приёмники перетаскивания ----------------
            with dpg.group(tag="pe_slots"):
                _group_label("ПОДВЕСЫ ПРЕСЕТА (перетащи оружие справа)",
                             "box", T.TEXT_DIM)
                for i in range(4):
                    with dpg.group(horizontal=True, tag=f"pe_slot_row_{i}"):
                        tcol(f"{i + 1}", 20, color=T.TEXT_DIM)
                        dpg.add_button(label="— пусто —", width=250,
                                       height=24, tag=f"pe_slot_{i}",
                                       user_data=i,
                                       callback=_cb_pe_slot_click,
                                       drop_callback=_cb_pe_slot_drop)
                        dpg.add_button(label="✕", width=26, height=24,
                                       user_data=i,
                                       callback=_cb_pe_slot_clear)
                        _tip(f"pe_slot_{i}",
                             "Перетащи оружие из списка справа или кликни, "
                             "чтобы положить выбранное в палитре")
            dpg.add_spacer(width=14)
            # --- палитра оружия: источники перетаскивания ----------------
            with dpg.child_window(tag="pe_palette_win", width=330,
                                  height=330):
                _group_label("АРСЕНАЛ (тяни на слот или кликни)", "zap",
                             T.TEXT_DIM)
                with dpg.group(tag="pe_palette"):
                    pass            # наполняет project._project_pe_palette
        dpg.add_separator()
        dpg.add_text("", tag="pe_note", color=T.TEXT_DIM, wrap=700)


def _cb_pe_variant(sender, app_data, user_data) -> None:
    """Смена техники в редакторе: слоты очищаются под её категории."""
    from ..units import build_unit
    key = _VARIANT_BY_LABEL.get(app_data or "", "attacker")
    _TOOL_STATE["pe_variant"] = key
    try:
        u = build_unit(key, "proto")
        cats = [m.category for m in u.mounts]
    except Exception:  # noqa: BLE001
        cats = []
    for i in range(4):
        tag = f"pe_slot_{i}"
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, label="— пусто —")
    _TOOL_STATE["pe_loadout"] = {}
    _refresh_pe_palette(key)


def _refresh_pe_palette(variant_key: str) -> None:
    """Пересобрать палитру оружия под подвесы выбранной машины."""
    from ..weapons import WEAPONS
    parent = "pe_palette"
    if not dpg.does_item_exist(parent):
        return
    dpg.delete_item(parent, children_only=True)
    items, allowed = variant_palette(variant_key)
    _TOOL_STATE["pe_allowed"] = allowed
    if not items:
        dpg.add_text("у этой машины нет подвесов", parent=parent,
                     color=T.TEXT_DIM)
    for key, label in items:
        b = dpg.add_button(label=f"{label}", width=300, height=20,
                           parent=parent, user_data=key,
                           callback=_cb_pe_palette_click,
                           drag_callback=_cb_pe_palette_drag)
        # в DPG 2.x данные payload передаются через drag_data (не data!)
        dpg.add_drag_payload(parent=b, payload_type="rwf_wpn", drag_data=key)
        _tip(b, f"{label} · перетащи на подходящий слот или кликни, затем "
                f"кликни слот")


def _cb_pe_palette_drag(sender, app_data, user_data) -> None:
    """Начало перетаскивания: payload уже создан ребёнком кнопки."""


def _cb_pe_palette_click(sender, app_data, user_data) -> None:
    """Клик по оружию = «выбрано в палитре» (альтернатива drag&drop)."""
    _TOOL_STATE["pe_picked"] = user_data
    _TOOL_STATE["facade"].sound_play("ui")
    if dpg.does_item_exist("pe_note"):
        dpg.set_value("pe_note",
                      f"Выбрано в палитре: {user_data}. Кликни слот слева, "
                      f"чтобы положить, или перетащи мышью.")


def _pe_put(slot: int, key: str) -> None:
    """Положить оружие в слот с проверкой по категории узла."""
    from ..weapons import WEAPONS
    allowed = (_TOOL_STATE.get("pe_allowed") or {}).get(slot)
    if allowed is not None and key not in allowed:
        if dpg.does_item_exist("pe_note"):
            dpg.set_value("pe_note",
                          f"«{WEAPONS.get(key, {}).get('label', key)}» не "
                          f"подходит к узлу {slot + 1} этой машины")
        return
    loadout = _TOOL_STATE.setdefault("pe_loadout", {})
    loadout[slot] = key
    tag = f"pe_slot_{slot}"
    if dpg.does_item_exist(tag):
        dpg.configure_item(tag, label=WEAPONS.get(key, {}).get("label", key))


def _cb_pe_slot_drop(sender, app_data, user_data) -> None:
    """Приём перетаскивания: app_data — drag_data payload (ключ оружия)."""
    key = app_data if isinstance(app_data, str) else None
    if key:
        _pe_put(int(user_data), key)
        _TOOL_STATE["facade"].sound_play("ui")


def _cb_pe_slot_click(sender, app_data, user_data) -> None:
    picked = _TOOL_STATE.get("pe_picked")
    if picked:
        _pe_put(int(user_data), picked)
        _TOOL_STATE["facade"].sound_play("ui")


def _cb_pe_slot_clear(sender, app_data, user_data) -> None:
    _TOOL_STATE.setdefault("pe_loadout", {}).pop(int(user_data), None)
    dpg.configure_item(f"pe_slot_{int(user_data)}", label="— пусто —")


def _pe_collect() -> "Preset":
    from ..presets import Preset
    key = _TOOL_STATE.get("pe_variant", "attacker")
    return Preset(name=(dpg.get_value("pe_name") or "").strip() or "preset",
                  variant=key,
                  loadout=dict(_TOOL_STATE.get("pe_loadout") or {}),
                  altitude=float(dpg.get_value("pe_alt") or 150.0),
                  is_bot=bool(dpg.get_value("pe_bot")),
                  ai="" if dpg.get_value("pe_ai") == "—"
                  else str(dpg.get_value("pe_ai")))


def _cb_pe_save(sender, app_data, user_data) -> None:
    facade = _TOOL_STATE["facade"]
    preset = _pe_collect()
    facade.sound_play("ui")
    if facade.save_preset(preset):
        dpg.configure_item("win_preset_editor", show=False)


def _cb_pe_save_as(sender, app_data, user_data) -> None:
    _cb_pe_save(sender, app_data, user_data)


def _cb_pe_delete(sender, app_data, user_data) -> None:
    facade = _TOOL_STATE["facade"]
    name = (dpg.get_value("pe_name") or "").strip()
    facade.sound_play("ui")
    if name:
        facade.delete_preset(name)
        dpg.configure_item("win_preset_editor", show=False)


def open_preset_editor(facade, preset_name: Optional[str] = None,
                       variant: Optional[str] = None) -> None:
    """Открыть редактор (пол-экрана, поверх всего) и наполнить пресетом."""
    from ..presets import Preset
    _TOOL_STATE["facade"] = facade
    preset: Optional[Preset] = None
    if preset_name:
        preset = facade.get_preset(preset_name)
    variant = variant or (preset.variant if preset else None) or "attacker"
    if dpg.does_item_exist("pe_variant"):
        label = next((lbl for k, lbl in
                      ((k, v.label) for k, v in VARIANTS.items())
                      if k == variant), VARIANTS["attacker"].label)
        dpg.set_value("pe_variant", label)
    _TOOL_STATE["pe_variant"] = variant
    _TOOL_STATE["pe_loadout"] = dict(preset.loadout) if preset else {}
    _TOOL_STATE["pe_picked"] = None
    if dpg.does_item_exist("pe_name"):
        dpg.set_value("pe_name", preset.name if preset else "")
    if dpg.does_item_exist("pe_alt"):
        dpg.set_value("pe_alt", preset.altitude if preset else 150.0)
    if dpg.does_item_exist("pe_bot"):
        dpg.set_value("pe_bot", preset.is_bot if preset else False)
    if dpg.does_item_exist("pe_ai"):
        dpg.set_value("pe_ai", (preset.ai if preset and preset.ai else "—"))
    for i in range(4):
        key = (preset.loadout.get(i) if preset else None)
        dpg.configure_item(f"pe_slot_{i}",
                           label=_weapon_label(key) if key else "— пусто —")
    _refresh_pe_palette(variant)
    dpg.configure_item("win_preset_editor", show=True)
    _center_modal("win_preset_editor")


def _weapon_label(key: Optional[str]) -> str:
    from ..weapons import WEAPONS
    if not key:
        return "— пусто —"
    return WEAPONS.get(key, {}).get("label", key)


def _center_modal(tag: str, fallback: Tuple[int, int] = (760, 520)) -> None:
    """Поставить модальное окно по центру вьюпорта.

    `get_item_rect_size` до первой отрисовки возвращает нули, поэтому размер
    берём из конфигурации окна, а уже потом — из фактического rect.
    """
    try:
        vw = dpg.get_viewport_client_width() or dpg.get_viewport_width()
        vh = dpg.get_viewport_client_height() or dpg.get_viewport_height()
        info = dpg.get_item_info(tag) or {}
        w = int(info.get("width") or 0) or fallback[0]
        h = int(info.get("height") or 0) or fallback[1]
        rect = dpg.get_item_rect_size(tag)
        if rect and rect[0] > 40 and rect[1] > 40:
            w, h = int(rect[0]), int(rect[1])
        dpg.set_item_pos(tag, (max(8, (vw - w) // 2), max(8, (vh - h) // 2)))
    except Exception:  # noqa: BLE001
        log.debug("не удалось центрировать %s", tag, exc_info=True)


# ------------------------------------------------------------- диалог базы
def _build_base_dialog() -> None:
    from ..bases import BASE_LABELS, KIND_AIRPORT
    kinds = [BASE_LABELS[KIND_AIRPORT], BASE_LABELS["carrier"],
             BASE_LABELS["ground"]]
    with dpg.window(tag="win_base_dialog", label="НОВАЯ БАЗА", modal=True,
                    show=False, width=430, height=330, no_collapse=True):
        with dpg.group(horizontal=True):
            tcol("Имя", 70, color=T.TEXT_DIM)
            dpg.add_input_text(tag="bd_name", width=250, hint="название базы")
        with dpg.group(horizontal=True):
            tcol("Тип", 70, color=T.TEXT_DIM)
            dpg.add_combo(kinds, width=250, tag="bd_kind",
                          default_value=kinds[0])
        with dpg.group(horizontal=True):
            tcol("X", 70, color=T.TEXT_DIM)
            dpg.add_input_float(tag="bd_x", width=110, default_value=0.0,
                                format="%.0f")
            tcol("Z", 24, color=T.TEXT_DIM)
            dpg.add_input_float(tag="bd_z", width=110, default_value=0.0,
                                format="%.0f")
        with dpg.group(horizontal=True):
            tcol("Курс", 70, color=T.TEXT_DIM)
            dpg.add_input_float(tag="bd_heading", width=110, default_value=0.0,
                                format="%.0f")
            tcol("HP", 30, color=T.TEXT_DIM)
            dpg.add_input_float(tag="bd_health", width=110,
                                default_value=0.0, format="%.0f")
        _tip("bd_health", "Прочность базы: 0 = значение по умолчанию для "
                          "типа. Базу с прочностью можно уничтожить огнём")
        with dpg.group(horizontal=True):
            dpg.add_button(label="В центр карты", width=130, height=22,
                           callback=_cb_bd_center)
            dpg.add_button(label="Создать", width=100, height=22,
                           callback=_cb_bd_create)
            dpg.add_button(label="Отмена", width=90, height=22,
                           callback=lambda: dpg.configure_item(
                               "win_base_dialog", show=False))
        dpg.add_text("Локацию можно задать числами или кнопкой «В центр "
                     "карты» — куда сейчас смотрит камера.",
                     color=T.TEXT_DIM, wrap=390)


def open_base_dialog(facade) -> None:
    _TOOL_STATE["facade"] = facade
    dpg.configure_item("win_base_dialog", show=True)
    _center_modal("win_base_dialog")


def _cb_bd_center(sender, app_data, user_data) -> None:
    facade = _TOOL_STATE["facade"]
    cx, cz = facade.renderer.transform.center
    dpg.set_value("bd_x", float(cx))
    dpg.set_value("bd_z", float(cz))


def _cb_bd_create(sender, app_data, user_data) -> None:
    from ..bases import BASE_LABELS
    facade = _TOOL_STATE["facade"]
    facade.sound_play("ui")
    kind_label = dpg.get_value("bd_kind")
    kind = next((k for k, v in BASE_LABELS.items() if v == kind_label),
                "airport")
    health = float(dpg.get_value("bd_health") or 0.0)
    name = (dpg.get_value("bd_name") or "").strip() or f"База {kind_label}"
    if facade.create_base_dialog(name, kind, float(dpg.get_value("bd_x")),
                                 float(dpg.get_value("bd_z")),
                                 float(dpg.get_value("bd_heading")),
                                 health or None):
        dpg.configure_item("win_base_dialog", show=False)


# --------------------------------------------------------------------- доки
def _build_right_docks(state: Dict[str, Any], facade) -> None:
    with dpg.window(tag="win_right", label="ПАНЕЛИ", no_move=True,
                    no_resize=True):
        with dpg.child_window(tag="right_scroll", height=-1,
                              horizontal_scrollbar=False):
            _section("conn", "СВЯЗЬ", state, facade)
            _section("launch", "ЗАПУСК", state, facade)
            _section("bases", "БАЗЫ", state, facade)
            _section("units", "ЮНИТЫ", state, facade)
            _section("ai", "АВТОПИЛОТ И ЦЕЛЬ", state, facade)
            _section("progress", "ПРОГРЕСС ОПЕРАТОРА", state, facade)
            _section("planner", "ПЛАНИРОВЩИК МАРШРУТА", state, facade)
            _section("players", "ИГРОКИ И ПЗРК", state, facade)
            _section("layers", "СЛОИ КАРТЫ", state, facade)
            _section("settings", "НАСТРОЙКИ", state, facade)
            _section("log", "ЖУРНАЛ", state, facade)
            _section("view", "ВИД", state, facade)


_SEC_W = right_w() - 34


def _section_header_prims(key: str, title: str, opened: bool) -> list:
    prims: list = []
    w, h = _SEC_W, 24.0
    prims.append({"type": "rect", "x": 0, "y": 0, "w": w, "h": h - 2,
                  "fill": "#1a211b", "outline": None,
                  "alpha": 160 if opened else 80})
    prims.append({"type": "rect", "x": 0, "y": 0, "w": 2.5, "h": h - 2,
                  "fill": "#70d686", "outline": None,
                  "alpha": 230 if opened else 90})
    prims.append({"type": "line", "x1": 0, "y1": h - 1.5, "x2": w,
                  "y2": h - 1.5, "color": "#2c3a2e", "width": 1,
                  "alpha": 220})
    prims.extend(uicons.prims("chevron_down" if opened else "chevron_right",
                              13, h / 2 - 1, 12, T.TEXT_DIM, 1.8))
    prims.extend(uicons.prims(SECTION_ICONS.get(key, "target"), 32,
                              h / 2 - 1, 15,
                              T.ACCENT if opened else T.TEXT_DIM, 1.6))
    prims.append({"type": "text", "x": 46, "y": 4, "text": title,
                  "color": T.TEXT if opened else T.TEXT_DIM,
                  "anchor": "nw", "size": 14})
    return prims


def _paint_section_header(key: str) -> None:
    title, opened = _SECTION_META[key][0], _SECTION_OPEN.get(key, True)
    iw.paint_raw(f"dock_hd_{key}",
                 _section_header_prims(key, title, opened))


def _cb_section_toggle(sender, app_data, user_data) -> None:
    key = user_data
    _TOOL_STATE["facade"].sound_play("ui")
    set_section_open(key, not _SECTION_OPEN.get(key, True))


def set_section_open(key: str, opened: bool) -> None:
    _SECTION_OPEN[key] = bool(opened)
    body = f"dock_{key}"
    if dpg.does_item_exist(body):
        dpg.configure_item(body, show=bool(opened))
    _paint_section_header(key)


def _section(key: str, title: str, state: Dict[str, Any], facade) -> None:
    opened = bool(state["docks"].get(key, True))
    _SECTION_OPEN[key] = opened
    _SECTION_META[key] = (title, )
    dpg.add_drawlist(width=_SEC_W, height=24, tag=f"dock_hd_{key}")
    with dpg.item_handler_registry(tag=f"dock_hd_{key}_hits"):
        dpg.add_item_clicked_handler(callback=_cb_section_toggle,
                                     user_data=key)
    dpg.bind_item_handler_registry(f"dock_hd_{key}", f"dock_hd_{key}_hits")
    with dpg.group(tag=f"dock_{key}", show=opened, horizontal=True):
        dpg.add_spacer(width=10)
        with dpg.group():
            _SECTION_BUILDERS[key](state, facade)
    dpg.add_spacer(height=4)
    _paint_section_header(key)


_SECTION_BUILDERS: Dict[str, Callable] = {}


def _section_builder(key: str):
    def deco(fn):
        _SECTION_BUILDERS[key] = fn
        return fn
    return deco


@_section_builder("conn")
def _dock_connection(state: Dict[str, Any], facade) -> None:
    with dpg.group(horizontal=True):
        tcol("Хост", 44, color=T.TEXT_DIM)
        dpg.add_input_text(tag="in_host", width=150,
                           default_value=facade.cfg.rcon.host)
        tcol("Порт", 34, color=T.TEXT_DIM)
        dpg.add_input_int(tag="in_port", width=80,
                          default_value=facade.cfg.rcon.port)
    with dpg.group(horizontal=True):
        tcol("Пароль", 44, color=T.TEXT_DIM)
        dpg.add_input_text(tag="in_pass", width=150, password=True,
                           default_value=facade.cfg.rcon.password)
        dpg.add_checkbox(label="Имитатор", tag="chk_mock",
                         default_value=bool(facade.cfg.rcon.mock))
    with dpg.group(horizontal=True) as gr:
        iw.icon_canvas("ico_conn", gr, "plug", 14, T.ACCENT, 1.6,
                       callback=_cb_connect)
        dpg.add_button(label="Подключить", width=110, height=22,
                       callback=_cb_connect)
        dpg.add_button(label="Отключить", width=92, height=22,
                       callback=lambda: facade.send("disconnect"))
    with dpg.group(horizontal=True):
        dpg.add_text("○ не подключено", tag="txt_conn_status",
                     color=(190, 110, 110, 255))
        dpg.add_text("", tag="txt_scan_status", color=T.CYAN)


def _cb_connect(sender, app_data, user_data) -> None:
    f = _TOOL_STATE["facade"]
    f.sound_play("ui")
    f.send("connect", dpg.get_value("in_host"), dpg.get_value("in_port"),
           dpg.get_value("in_pass"), dpg.get_value("chk_mock"))


@_section_builder("launch")
def _dock_launch(state: Dict[str, Any], facade) -> None:
    """Запуск в v15: Тип транспорта → Пресет → База → Старт.

    Заказчик: «основной экран = только пресеты». Сборка машины вручную
    (высота, бот, три комбобокса подвесов) ушла в РЕДАКТОР ПРЕСЕТОВ —
    отдельное модальное окно в пол-экрана. Здесь остаются четыре выбора.
    """
    variants = [(k, v.label) for k, v in VARIANTS.items()]
    with dpg.group(horizontal=True):
        tcol("Тип", 52, color=T.TEXT_DIM)
        dpg.add_combo([lbl for _, lbl in variants], width=right_w() - 96,
                      tag="cmb_launch_variant", default_value=variants[0][1],
                      callback=_cb_variant_change)
    with dpg.group(horizontal=True):
        tcol("Пресет", 52, color=T.TEXT_DIM)
        dpg.add_combo(["—"], width=right_w() - 120, tag="cmb_launch_preset",
                      default_value="—")
        iw.icon_canvas("ico_pe_edit", f"cmb_launch_preset_row"
                       if dpg.does_item_exist("cmb_launch_preset_row")
                       else "dock_launch", "sliders", 15, T.ACCENT, 1.6,
                       callback=_cb_open_pe)
    _tip("ico_pe_edit", "Редактор пресетов: окно в пол-экрана, вооружение "
                        "перетаскиванием, сохранение рецепта машины")
    with dpg.group(horizontal=True):
        tcol("База", 52, color=T.TEXT_DIM)
        dpg.add_combo(["—"], width=right_w() - 96, tag="cmb_launch_base",
                      default_value="—")
    with dpg.group(horizontal=True) as gr:
        iw.icon_canvas("ico_launch", gr, "plane_takeoff", 15, T.ACCENT, 1.6,
                       callback=_cb_launch)
        dpg.add_button(label="СТАРТ", width=right_w() - 110, height=26,
                       callback=_cb_launch)
    _tip("cmb_launch_preset",
         "Готовый рецепт машины: вариант, подвесы, высота, бот и шаблон ИИ. "
         "Пресеты лежат файлами в каталоге presets/")


@_section_builder("bases")
def _dock_bases(state: Dict[str, Any], facade) -> None:
    with dpg.group(horizontal=True):
        tcol("Имя", 40, color=T.TEXT_DIM)
        dpg.add_input_text(tag="in_base_name", width=120, hint="название")
        dpg.add_combo([BASE_LABELS[KIND_AIRPORT], BASE_LABELS[KIND_CARRIER],
                       BASE_LABELS[KIND_GROUND]], width=110,
                      tag="cmb_base_kind", default_value=BASE_LABELS[KIND_AIRPORT])
        dpg.add_button(label="+", width=26, height=22, tag="btn_base_add",
                       callback=lambda: open_base_dialog(
                           _TOOL_STATE["facade"]))
    _tip("btn_base_add", "Создать базу: окно выбора типа, локации, курса и "
                         "прочности (HP)")
    # Мелкие текстовые подсказки убраны по просьбе заказчика: вместо них
    # тултипы на самих элементах (наведение мыши).
    with dpg.table(header_row=True, borders_innerV=True, borders_innerH=True,
                   row_background=True, policy=dpg.mvTable_SizingFixedFit,
                   tag="bases_table", width=_SEC_W - 16):
        dpg.add_table_column(label="", width_fixed=True,
                             init_width_or_weight=20)
        dpg.add_table_column(label="БАЗА", width_fixed=True,
                             init_width_or_weight=150)
        dpg.add_table_column(label="КОРП", width_fixed=True,
                             init_width_or_weight=52)
        dpg.add_table_column(label="СНАБ", width_fixed=True,
                             init_width_or_weight=46)
        dpg.add_table_column(label="МЕСТ", width_fixed=True,
                             init_width_or_weight=44)
        for i in range(6):
            with dpg.table_row(tag=f"base_row_{i}"):
                iw.icon_canvas(f"b_ico_{i}", f"base_row_{i}", "home",
                               14, T.TEXT_DIM, 1.5, callback=_cb_base_select,
                               user_data=i)
                dpg.add_selectable(label="—", tag=f"txt_base_{i}",
                                   user_data=i, callback=_cb_base_select)
                dpg.add_selectable(label="—", tag=f"txt_base_hp_{i}",
                                   user_data=i, callback=_cb_base_select)
                dpg.add_selectable(label="—", tag=f"txt_base_sup_{i}",
                                   user_data=i, callback=_cb_base_select)
                dpg.add_selectable(label="—", tag=f"txt_base_cap_{i}",
                                   user_data=i, callback=_cb_base_select)
            dpg.configure_item(f"base_row_{i}", show=False)
            _tip(f"b_ico_{i}",
                 "ЛКМ — выбрать и открыть в панели объекта · ПКМ — действия "
                 "(центр карты, курс авианосца, ремонт)")
    # Пояснения к колонкам — тултип на ЗАГОЛОВКЕ секции (drawlist, листовой
    # виджет): на самой таблице add_tooltip роняет DPG насмерть.
    _tip("dock_hd_bases",
         "КОРП — прочность базы, её можно уничтожить. СНАБ — очки снабжения: "
         "копятся и тратятся на заправку, боезапас и ремонт. У авианосца "
         "своей генерации нет — снабжение привозит транспортник")
    _tip("dock_hd_units",
         "ЛКМ — выбрать и открыть в панели объекта, ПКМ — действия. Статусы "
         "человекочитаемые: Стоит, Маневрирует, Отстреливается, Падает, Горит")


@_section_builder("units")
def _dock_units(state: Dict[str, Any], facade) -> None:
    with dpg.table(header_row=True, borders_innerV=True, borders_innerH=True,
                   row_background=True, policy=dpg.mvTable_SizingStretchSame,
                   tag="units_table"):
        dpg.add_table_column(label="", width_fixed=True,
                             init_width_or_weight=22)
        dpg.add_table_column(label="ЮНИТ")
        dpg.add_table_column(label="СТАТУС", width_fixed=True,
                             init_width_or_weight=74)
        dpg.add_table_column(label="ТОПЛ", width_fixed=True,
                             init_width_or_weight=44)
        dpg.add_table_column(label="Б/К", width_fixed=True,
                             init_width_or_weight=52)
        dpg.add_table_column(label="КОРП", width_fixed=True,
                             init_width_or_weight=44)
        for i in range(st.MAX_UNIT_ROWS):
            with dpg.table_row(tag=f"u_row_{i}"):
                iw.icon_canvas(f"u_ico_{i}", f"u_row_{i}", "plane",
                               16, T.TEXT_DIM, 1.5, callback=_cb_unit_select,
                               user_data=i)
                dpg.add_selectable(label="—", tag=f"u_name_{i}", user_data=i,
                                   callback=_cb_unit_select)
                dpg.add_selectable(label="—", tag=f"u_status_{i}",
                                   user_data=i, callback=_cb_unit_select)
                dpg.add_text("—", tag=f"u_fuel_{i}")
                dpg.add_text("—", tag=f"u_ammo_{i}")
                dpg.add_text("—", tag=f"u_hp_{i}")
            dpg.configure_item(f"u_row_{i}", show=False)



def redraw_unit_icon(row: int, kind: str, selected: bool) -> None:
    """Силуэт техники в строке списка юнитов (как на карте, но мини)."""
    from ..icons import Lod, unit_icon
    from ..maprender import UnitsLayer
    fill, outline = UnitsLayer.COLORS.get(kind, ("#ffffff", "#888888"))
    if selected:
        outline = "#ffffff"
    lod = Lod(size_px=6.4, detail=2, alpha=255, show_label=False,
              show_rotors=True, show_blades=False)
    iw.paint_raw(f"u_ico_{row}",
                 unit_icon(kind, 8.0, 8.5, 0.0, lod, fill, outline))


def redraw_base_icon(row: int, kind: str, selected: bool) -> None:
    """Иконка типа базы в строке списка баз."""
    name = {"airport": "plane", "carrier": "ship",
            "ground": "home"}.get(kind, "home")
    color = T.ACCENT if selected else T.TEXT_DIM
    iw.paint_raw(f"b_ico_{row}", uicons.prims(name, 7.0, 7.0, 13.0, color,
                                              1.5))


def _cb_unit_select(sender, app_data, user_data) -> None:
    uid = _TOOL_STATE.get("_row_uids", {}).get(int(user_data))
    if uid is not None:
        _TOOL_STATE["selection"] = uid
        _TOOL_STATE["console_obj"] = ("unit", uid)
        _TOOL_STATE["facade"].sound_play("ui")
        _TOOL_STATE["facade"].send("select", uid)
        if _TOOL_STATE.get("follow"):
            _TOOL_STATE["facade"].follow_uid = uid


def _cb_base_select(sender, app_data, user_data) -> None:
    i = int(user_data)
    bases = _TOOL_STATE.get("bases") or []
    if 0 <= i < len(bases):
        _TOOL_STATE["selected_base"] = bases[i]["id"]
        _TOOL_STATE["console_obj"] = ("base", bases[i]["id"])
        _TOOL_STATE["facade"].sound_play("ui")


@_section_builder("planner")
def _dock_planner(state: Dict[str, Any], facade) -> None:
    with dpg.group(horizontal=True):
        tcol("Высота", 52, color=T.TEXT_DIM)
        dpg.add_input_float(tag="in_wp_alt", width=76, default_value=150.0,
                            format="%.0f")
        dpg.add_text("Точка маршрута → ЛКМ по карте", color=T.TEXT_DIM)
    actions = [ACTION_LABELS[a] for a in Action.ALL]
    for i in range(st.MAX_PLANNER_ROWS):
        with dpg.group(horizontal=True, tag=f"wp_row_{i}"):
            tcol(f"{i + 1:>2}. —", 132, tag=f"txt_wp_{i}")
            dpg.add_combo(actions, width=118, tag=f"cmb_wp_act_{i}",
                          user_data=i, callback=_cb_wp_action,
                          default_value=actions[0])
            iw.icon_canvas(f"ico_wp_up_{i}", f"wp_row_{i}",
                           "arrow_up", 14, T.TEXT_DIM, 1.6,
                           callback=_cb_wp_up, user_data=i)
            iw.icon_canvas(f"ico_wp_dn_{i}", f"wp_row_{i}",
                           "arrow_down", 14, T.TEXT_DIM, 1.6,
                           callback=_cb_wp_dn, user_data=i)
            iw.icon_canvas(f"ico_wp_del_{i}", f"wp_row_{i}", "x", 14,
                           T.RED, 1.7, callback=_cb_wp_del, user_data=i)
        dpg.configure_item(f"wp_row_{i}", show=False)
    dpg.add_text("", tag="txt_route_status", color=T.TEXT_DIM)
    with dpg.group(horizontal=True) as gsave:
        tcol("Имя", 52, color=T.TEXT_DIM)
        dpg.add_input_text(tag="in_route_name", width=120, hint="имя маршрута")
        iw.icon_canvas("ico_route_save", gsave, "save", 14,
                       T.ACCENT, 1.6, callback=_cb_save_route)
    with dpg.group(horizontal=True):
        dpg.add_combo([], width=150, tag="cmb_route_lib", default_value="—")
        dpg.add_button(label="Загрузить", width=80, height=20,
                       callback=_cb_load_route)
        dpg.add_button(label="Удалить", width=64, height=20,
                       callback=_cb_del_route)


_LABEL_TO_ACTION = {v: k for k, v in ACTION_LABELS.items()}


def _wp_points() -> List[Dict[str, Any]]:
    return _TOOL_STATE["planner"]["points"]


def _cb_wp_action(sender, app_data, user_data) -> None:
    i = int(user_data)
    pts = _wp_points()
    if 0 <= i < len(pts):
        pts[i]["action"] = _LABEL_TO_ACTION.get(app_data, Action.NAVIGATE)


def _cb_wp_up(sender, app_data, user_data) -> None:
    st.planner_move(_TOOL_STATE, int(user_data), int(user_data) - 1)


def _cb_wp_dn(sender, app_data, user_data) -> None:
    st.planner_move(_TOOL_STATE, int(user_data), int(user_data) + 1)


def _cb_wp_del(sender, app_data, user_data) -> None:
    st.planner_remove(_TOOL_STATE, int(user_data))


def _cb_save_route(sender, app_data, user_data) -> None:
    name = (dpg.get_value("in_route_name") or "").strip()
    _TOOL_STATE["facade"].send("save_route", name, list(_wp_points()))
    _TOOL_STATE["facade"].send("library_names")


def _cb_load_route(sender, app_data, user_data) -> None:
    name = dpg.get_value("cmb_route_lib")
    if name and name != "—":
        _TOOL_STATE["facade"].send("load_route", name)


def _cb_del_route(sender, app_data, user_data) -> None:
    name = dpg.get_value("cmb_route_lib")
    if name and name != "—":
        _TOOL_STATE["facade"].send("delete_route", name)


_VARIANT_BY_LABEL = {v.label: k for k, v in VARIANTS.items()}
_KIND_BY_LABEL = {v: k for k, v in BASE_LABELS.items()}


def _cb_variant_change(sender, app_data, user_data) -> None:
    """Смена типа техники: список пресетов фильтруется под вариант."""
    key = _VARIANT_BY_LABEL.get(app_data or "", "attacker")
    _TOOL_STATE["launch"]["variant"] = key
    _refresh_preset_combo(key)
    facade = _TOOL_STATE.get("facade")
    if facade is not None:
        facade.sound_play("ui")


def _refresh_preset_combo(variant_key: Optional[str]) -> None:
    """Наполнить комбобокс пресетов (только подходящие к варианту)."""
    facade = _TOOL_STATE.get("facade")
    tag = "cmb_launch_preset"
    if facade is None or not dpg.does_item_exist(tag):
        return
    names = facade.preset_names(variant_key)
    items = ["— без пресета —"] + names
    cur = dpg.get_value(tag)
    dpg.configure_item(tag, items=items)
    dpg.set_value(tag, cur if cur in items else
                  (names[0] if names else items[0]))


def _cb_open_pe(sender, app_data, user_data) -> None:
    """Открыть редактор пресетов (модальное окно в пол-экрана)."""
    facade = _TOOL_STATE["facade"]
    facade.sound_play("ui")
    name = dpg.get_value("cmb_launch_preset")
    variant = _TOOL_STATE["launch"].get("variant")
    open_preset_editor(facade,
                       preset_name=name if name not in ("—", "— без пресета —",
                                                        "", None) else None,
                       variant=variant)


def cat_keys_for(category: str) -> List[str]:
    """Ключи оружия категории (в WEAPONS поле называется «cat»)."""
    from ..weapons import WEAPONS
    return [k for k, v in WEAPONS.items()
            if (v.get("cat") or v.get("category")) == category]


def variant_palette(variant_key: str) -> Tuple[List[Tuple[str, str]],
                                               Dict[int, List[str]]]:
    """Палитра редактора: (все доступные оружия, {слот: допустимые ключи}).

    Источник правды — `UnitVariant.available` (категория -> ключи) и категории
    подвесов варианта: в слот нельзя положить то, что узел не потянет.
    """
    from ..weapons import WEAPONS
    from ..units import build_unit
    allowed: Dict[int, List[str]] = {}
    items: List[Tuple[str, str]] = []
    seen: set = set()
    try:
        u = build_unit(variant_key, "proto")
        cats = [m.category for m in u.mounts]
        avail = dict(getattr(u, "AVAILABLE", {}) or {})
    except Exception:  # noqa: BLE001
        return items, allowed
    for i, cat in enumerate(cats[:4]):
        allowed[i] = list(avail.get(cat) or cat_keys_for(cat))
    for cat in dict.fromkeys(cats):
        for key in (avail.get(cat) or cat_keys_for(cat)):
            if key in seen:
                continue
            seen.add(key)
            items.append((key, WEAPONS.get(key, {}).get("label", key)))
    return items, allowed


def _cb_launch(sender, app_data, user_data) -> None:
    """СТАРТ: Тип → Пресет → База. Пресет задаёт всё, кроме базы."""
    s = _TOOL_STATE
    facade = s["facade"]
    facade.sound_play("ui")
    base_label = dpg.get_value("cmb_launch_base")
    bases = s.get("bases") or []
    base = next((b for b in bases if _base_label(b) == base_label), None)
    preset_name = dpg.get_value("cmb_launch_preset")
    if preset_name and preset_name != "— без пресета —":
        if base is None:
            cx, cz = facade.renderer.transform.center
            preset = facade.get_preset(preset_name)
            if preset is None:
                return
            facade.send("spawn", preset.variant, cx, cz, preset.altitude,
                        preset.is_bot)
        else:
            facade.send("launch_preset", preset_name, base["id"])
        return
    # без пресета — минимальный запуск текущего типа без вооружения
    variant = s["launch"].get("variant") or "attacker"
    if base is None:
        cx, cz = facade.renderer.transform.center
        facade.send("spawn", variant, cx, cz, 150.0, False)
    else:
        facade.send("launch_from_base", base["id"], variant, 150.0, False, {})


def _base_label(b: Dict[str, Any]) -> str:
    kind = BASE_LABELS.get(b["kind"], b["kind"])
    return f"#{b['id']} {b['name']} ({kind})"


@_section_builder("progress")
def _dock_progress(state: Dict[str, Any], facade) -> None:
    """Очки, звание и достижения оператора (GAME-01)."""
    with dpg.group(horizontal=True):
        tcol("Очки", 56, color=T.TEXT_DIM)
        dpg.add_text("0", tag="txt_score", color=T.ACCENT)
        T.bind_bold("txt_score")
        dpg.add_text("", tag="txt_score_session", color=T.TEXT_DIM)
    with dpg.group(horizontal=True):
        tcol("Звание", 56, color=T.TEXT_DIM)
        dpg.add_text("—", tag="txt_rank", color=T.AMBER)
    dpg.add_progress_bar(default_value=0.0, width=_SEC_W - 24, height=12,
                         tag="bar_rank")
    _bar_theme("bar_rank", T.AMBER)
    dpg.add_text("", tag="txt_rank_next", color=T.TEXT_DIM, wrap=_SEC_W - 24)
    _group_label("ДОСТИЖЕНИЯ", "target", T.TEXT_DIM)
    for i in range(10):
        dpg.add_text("", tag=f"txt_ach_{i}", show=False, wrap=_SEC_W - 24)
    _tip("txt_score",
         "Очки за результат: сбитая машина, посадка, доставка снабжения, "
         "снесённая база, выполненный маршрут. Потеря своей техники очки "
         "снимает. Звание и достижения сохраняются между сессиями")


@_section_builder("ai")
def _dock_ai(state: Dict[str, Any], facade) -> None:
    """Автопилот выбранного объекта.

    v15: настройки ИИ убраны из хотбара/пульта сюда, а цель выбирается
    ВЫПАДАЮЩИМ списком (игроки и техника) вместо ручного ввода имени —
    опечататься в нике больше нельзя.
    """
    tpls = facade.ai_templates()
    with dpg.group(horizontal=True):
        tcol("Шаблон", 56, color=T.TEXT_DIM)
        dpg.add_combo([t for t, _ in tpls], width=_SEC_W - 120, tag="cmb_ai",
                      default_value=(tpls or [("patrol", "")])[0][0])
    with dpg.group(horizontal=True):
        tcol("Цель", 56, color=T.TEXT_DIM)
        dpg.add_combo(["— (ближайший игрок)"], width=_SEC_W - 120,
                      tag="cmb_ai_target", default_value="— (ближайший игрок)")
    _tip("cmb_ai_target", "Цель автопилота: игроки или техника из списка")
    with dpg.group(horizontal=True) as gr:
        iw.icon_canvas("ico_ai2", gr, "cpu", 14, T.CYAN, 1.6)
        dpg.add_button(label="Включить", width=96, height=22,
                       callback=_cb_ai_on)
        dpg.add_button(label="Выключить", width=96, height=22,
                       callback=lambda: facade.send("clear_ai", None))
    dpg.add_text("", tag="txt_ai_status", color=T.CYAN, wrap=_SEC_W - 20)


@_section_builder("players")
def _dock_players(state: Dict[str, Any], facade) -> None:
    _tip(f"dock_hd_players",
         "Игрок в игре вводит /trigger rwf_lock — захват цели ПЗРК, "
         "/trigger rwf_missile — пуск. Здесь видно, кто сейчас в захвате")
    for i in range(st.MAX_PLAYER_ROWS):
        dpg.add_text("—", tag=f"txt_player_{i}", show=False,
                     wrap=right_w() - 56)


@_section_builder("layers")
def _dock_layers(state: Dict[str, Any], facade) -> None:
    labels = {"grid": "Сетка", "terrain": "Рельеф", "bases": "Базы",
              "zones": "Зоны и метки", "routes": "Маршруты",
              "markers": "События", "units": "Техника",
              "players": "Игроки", "planner": "Черновик",
              "hud": "HUD (компас/линейка)"}
    keys = list(labels)
    half = (len(keys) + 1) // 2
    with dpg.group(horizontal=True):
        with dpg.group():
            for key in keys[:half]:
                dpg.add_checkbox(label=labels[key], tag=f"chk_layer_{key}",
                                 default_value=bool(state["layers"].get(key, True)),
                                 user_data=key, callback=_cb_layer)
        with dpg.group():
            for key in keys[half:]:
                dpg.add_checkbox(label=labels[key], tag=f"chk_layer_{key}",
                                 default_value=bool(state["layers"].get(key, True)),
                                 user_data=key, callback=_cb_layer)
    with dpg.group(horizontal=True):
        tcol("Иконки", 52, color=T.TEXT_DIM)
        dpg.add_slider_float(tag="sld_icon_scale", width=170,
                             default_value=1.0, min_value=0.5,
                             max_value=2.5, callback=_cb_icon_scale)


def _cb_layer(sender, app_data, user_data) -> None:
    _TOOL_STATE["layers"][user_data] = bool(app_data)


def _cb_icon_scale(sender, app_data, user_data) -> None:
    _TOOL_STATE["layers"]["icon_scale"] = float(app_data)


@_section_builder("settings")
def _dock_settings(state: Dict[str, Any], facade) -> None:
    with dpg.group(horizontal=True) as gsnd:
        iw.icon_canvas("ico_snd", gsnd, "volume", 15,
                       T.ACCENT, 1.6, callback=_cb_sound_toggle)
        dpg.add_checkbox(label="Звук", tag="chk_sound",
                         default_value=bool(facade.settings.sound_enabled),
                         callback=_cb_sound_toggle)
        dpg.add_slider_float(tag="sld_volume", width=130, min_value=0.0,
                             max_value=1.0,
                             default_value=facade.settings.volume,
                             format="%.1f", callback=_cb_volume)
        tcol("", 34, tag="txt_volume", color=T.TEXT_DIM)
    with dpg.group(horizontal=True):
        ev = facade.settings.get("sound.events", {}) or {}
        for i, name in enumerate(("ui", "fire", "explosion", "spawn",
                                  "landing", "lock", "alarm")):
            dpg.add_checkbox(label=name, tag=f"chk_ev_{name}",
                             default_value=bool(ev.get(name, True)),
                             user_data=name, callback=_cb_sound_event)
    with dpg.group(horizontal=True):
        dpg.add_button(label="Сохранить настройки", width=170, height=22,
                       callback=_cb_settings_save)
    dpg.add_text(str(facade.settings.path), color=T.TEXT_DIM,
                 wrap=right_w() - 60)


def _cb_sound_toggle(sender=None, app_data=None, user_data=None) -> None:
    val = bool(dpg.get_value("chk_sound"))
    iw.redraw("ico_snd", name="volume" if val else "volume_x",
              color=T.ACCENT if val else T.TEXT_DIM)
    _TOOL_STATE["facade"].set_setting("sound.enabled", val)
    if val:
        _TOOL_STATE["facade"].sound_play("ui")


def _cb_volume(sender, app_data, user_data) -> None:
    vol = float(app_data)
    _TOOL_STATE["facade"].set_setting("sound.volume", vol)
    if dpg.does_item_exist("txt_volume"):
        dpg.set_value("txt_volume", f"{vol * 100:.0f}%")


def _cb_sound_event(sender, app_data, user_data) -> None:
    _TOOL_STATE["facade"].set_setting(f"sound.events.{user_data}",
                                      bool(app_data))


def _cb_settings_save(sender, app_data, user_data) -> None:
    ok = _TOOL_STATE["facade"].settings.save()
    _TOOL_STATE["facade"].log_ui(
        "Настройки сохранены" if ok else "Не удалось сохранить настройки",
        "green" if ok else "red")


@_section_builder("log")
def _dock_log(state: Dict[str, Any], facade) -> None:
    with dpg.child_window(tag="log_scroll", height=180):
        for i in range(st.MAX_LOG_ROWS):
            dpg.add_text("", tag=f"txt_log_{i}", show=False,
                         wrap=right_w() - 66)


@_section_builder("view")
def _dock_view(state: Dict[str, Any], facade) -> None:
    for key, label in (("conn", "Связь"), ("launch", "Запуск"),
                       ("bases", "Базы"), ("units", "Юниты"),
                       ("ai", "Автопилот и цель"),
                       ("planner", "Планировщик"), ("players", "Игроки и ПЗРК"),
                       ("layers", "Слои карты"), ("settings", "Настройки"),
                       ("log", "Журнал")):
        dpg.add_checkbox(label=label, tag=f"chk_view_{key}",
                         default_value=bool(state["docks"].get(key, True)),
                         user_data=key, callback=_cb_view_toggle)
    with dpg.group(horizontal=True):
        dpg.add_checkbox(label="Панель объекта", tag="chk_view_obj",
                         default_value=bool(state.get("objpanel", True)),
                         callback=_cb_obj_toggle)
        dpg.add_checkbox(label="Хотбар", tag="chk_view_hotbar",
                         default_value=bool(state.get("hotbar", True)),
                         callback=_cb_hotbar_toggle)
    with dpg.group(horizontal=True) as gr:
        iw.icon_canvas("ico_exit", gr, "power", 14, T.RED, 1.6,
                       callback=_cb_exit)
        dpg.add_button(label="Выйти из программы", width=150, height=22,
                       callback=_cb_exit)


def _cb_view_toggle(sender, app_data, user_data) -> None:
    _TOOL_STATE["docks"][user_data] = bool(app_data)
    set_section_open(user_data, bool(app_data))


def _cb_obj_toggle(sender, app_data, user_data) -> None:
    """Скрыть/показать левую панель объекта (место отдаётся карте)."""
    _TOOL_STATE["objpanel"] = bool(app_data)
    dpg.configure_item("win_obj_tab", show=not bool(app_data))
    layout()


def _cb_hotbar_toggle(sender, app_data, user_data) -> None:
    _TOOL_STATE["hotbar"] = bool(app_data)
    dpg.configure_item("win_tools_tab", show=not bool(app_data))
    layout()


def _cb_exit(sender=None, app_data=None, user_data=None) -> None:
    on_exit = _TOOL_STATE.get("on_exit")
    if on_exit:
        on_exit()
    else:
        dpg.stop_dearpygui()
