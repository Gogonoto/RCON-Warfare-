# ------------------------------------------------------------- хотбар (rail)
def _tip(parent: str, text: str) -> None:
    """Тултип вместо подписи: заказчик просил убрать мелкий текст-подсказки."""
    with dpg.tooltip(parent=parent, delay=0.25):
        t = dpg.add_text(text, wrap=240)
        try:
            dpg.configure_item(t, color=T.TEXT)
        except Exception:  # noqa: BLE001
            pass


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


# ------------------------------------------------------- панель объекта (слева)
TEL_PARAM_ROWS = 10
BAR_W = 132


def _bar_theme(tag: str, color: tuple) -> None:
    """Тема прогресс-бара: тёмный жёлоб, яркий填 fill, без «радуги»."""
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
                    dpg.add_text("—", tag=f"txt_bar_{key}", color=color,
                                 width=w - BAR_W - 40)
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
