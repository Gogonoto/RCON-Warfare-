"""
Стиль интерфейса: плоский тёмный «графит + люминофор» (секция 8 скилла
tactical-ui-dearpygui) + моноширинный шрифт с кириллицей + иконка вьюпорта.

ВАЖНО (DPG 2.x): цвета/стили темы кладутся ТОЛЬКО в `add_theme_component`,
а не в саму тему — `add_theme_color(parent=theme)` молча не применяется
(несовместимый родитель). Дефект «дефолтной радуги» v13 лечится именно здесь.

Шрифт ОБЯЗАТЕЛЕН: встроенный шрифт Dear ImGui не содержит кириллицы. Основной
гарнитурой ставится DejaVuSansMono (забандлен в assets): моноширина даёт
«приборную» посадку цифр в телеметрии и таблицах. Заголовки секций вяжутся
на DejaVuSansMono-Bold через `bind_item_font` — так строится иерархия веса.

Цвета карты задаются явными RGBA в слоях `maprender.py` и сюда не попадают:
тема отвечает только за хром виджетов.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

import dearpygui.dearpygui as dpg

log = logging.getLogger(__name__)

ASSETS = os.path.join(os.path.dirname(__file__), "assets")

_FONT_CANDIDATES = [
    os.path.join(ASSETS, "DejaVuSansMono.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    os.path.join(ASSETS, "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/courier.ttf",
]
_BOLD_CANDIDATES = [
    os.path.join(ASSETS, "DejaVuSansMono-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]

FONT_SIZE = 14.0
BOLD_FONT_SIZE = 14.0

# ---------------------------------------------------------------------------
#  Палитра: графитовая подложка, люминофорно-зелёный акцент,
#  функциональные цвета (cyan/amber/red) — без «радуги» дефолта DPG
# ---------------------------------------------------------------------------
BG: Tuple[int, int, int, int] = (12, 15, 13, 255)
BG_PANEL = (16, 20, 17, 255)
BG_RAISED = (21, 26, 22, 255)
BG_FRAME = (24, 30, 25, 255)
BG_FRAME_H = (32, 40, 33, 255)
BG_FRAME_A = (41, 52, 42, 255)
BORDER = (47, 59, 49, 255)
BORDER_SOFT = (32, 40, 33, 255)
TITLE = (17, 21, 18, 255)
TITLE_ACT = (25, 33, 27, 255)
TEXT = (211, 224, 211, 255)
TEXT_DIM = (126, 142, 126, 255)
ACCENT = (112, 214, 134, 255)
ACCENT_HI = (158, 240, 176, 255)
CYAN = (102, 196, 224, 255)
AMBER = (226, 180, 94, 255)
RED = (226, 104, 94, 255)

#: цвета для текста журнала/статусов вне темы
LOG_OK = ACCENT
LOG_WARN = AMBER
LOG_ERR = RED


def find_font() -> Optional[str]:
    for path in _FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def find_bold_font() -> Optional[str]:
    for path in _BOLD_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def bind() -> Optional[str]:
    """Привязать тему и кириллический моноширинный шрифт.

    Возвращает путь основного шрифта (None — если не найден).
    """
    theme = dpg.add_theme(tag="c2_theme")
    comp = dpg.add_theme_component(parent=theme)

    pairs: List[tuple] = [
        # подложки
        (dpg.mvThemeCol_WindowBg, BG),
        (dpg.mvThemeCol_ChildBg, BG_PANEL),
        (dpg.mvThemeCol_PopupBg, (18, 23, 19, 248)),
        (dpg.mvThemeCol_MenuBarBg, BG_RAISED),
        (dpg.mvThemeCol_ModalWindowDimBg, (0, 0, 0, 140)),
        # рамки
        (dpg.mvThemeCol_Border, BORDER),
        (dpg.mvThemeCol_BorderShadow, (0, 0, 0, 0)),
        (dpg.mvThemeCol_Separator, BORDER_SOFT),
        (dpg.mvThemeCol_SeparatorActive, ACCENT),
        (dpg.mvThemeCol_SeparatorHovered, BORDER),
        # текст
        (dpg.mvThemeCol_Text, TEXT),
        (dpg.mvThemeCol_TextDisabled, TEXT_DIM),
        (dpg.mvThemeCol_TextSelectedBg, (52, 84, 58, 160)),
        # заголовки окон
        (dpg.mvThemeCol_TitleBg, TITLE),
        (dpg.mvThemeCol_TitleBgActive, TITLE_ACT),
        (dpg.mvThemeCol_TitleBgCollapsed, (13, 16, 14, 255)),
        # кнопки
        (dpg.mvThemeCol_Button, BG_RAISED),
        (dpg.mvThemeCol_ButtonHovered, BG_FRAME_H),
        (dpg.mvThemeCol_ButtonActive, BG_FRAME_A),
        # рамки ввода / combo / слайдеры
        (dpg.mvThemeCol_FrameBg, BG_FRAME),
        (dpg.mvThemeCol_FrameBgHovered, BG_FRAME_H),
        (dpg.mvThemeCol_FrameBgActive, BG_FRAME_A),
        # списки / заголовки секций
        (dpg.mvThemeCol_Header, (26, 33, 27, 255)),
        (dpg.mvThemeCol_HeaderHovered, BG_FRAME_H),
        (dpg.mvThemeCol_HeaderActive, BG_FRAME_A),
        # акценты управления
        (dpg.mvThemeCol_CheckMark, ACCENT),
        (dpg.mvThemeCol_SliderGrab, ACCENT),
        (dpg.mvThemeCol_SliderGrabActive, ACCENT_HI),
        (dpg.mvThemeCol_ResizeGrip, BORDER),
        (dpg.mvThemeCol_ResizeGripHovered, BORDER),
        (dpg.mvThemeCol_ResizeGripActive, ACCENT),
        # скролл
        (dpg.mvThemeCol_ScrollbarBg, (10, 13, 11, 255)),
        (dpg.mvThemeCol_ScrollbarGrab, (41, 52, 43, 255)),
        (dpg.mvThemeCol_ScrollbarGrabHovered, (56, 71, 58, 255)),
        (dpg.mvThemeCol_ScrollbarGrabActive, (74, 94, 77, 255)),
        # прогресс-бары (PlotHistogram!) — дефолтно-синие в v13 были бедой
        (dpg.mvThemeCol_PlotHistogram, ACCENT),
        (dpg.mvThemeCol_PlotHistogramHovered, ACCENT_HI),
        (dpg.mvThemeCol_PlotLines, ACCENT),
        # вкладки / навигация
        (dpg.mvThemeCol_Tab, BG_RAISED),
        (dpg.mvThemeCol_TabHovered, BG_FRAME_H),
        (dpg.mvThemeCol_TabActive, BG_FRAME_A),
        (dpg.mvThemeCol_TabUnfocused, BG_RAISED),
        (dpg.mvThemeCol_TabUnfocusedActive, BG_PANEL),
        (dpg.mvThemeCol_NavHighlight, ACCENT),
        # drag-n-drop
        (dpg.mvThemeCol_DragDropTarget, ACCENT),
    ]
    for target, color in pairs:
        dpg.add_theme_color(target, list(color),
                            category=dpg.mvThemeCat_Core, parent=comp)

    styles: List[tuple] = [
        (dpg.mvStyleVar_WindowPadding, (10, 8)),
        (dpg.mvStyleVar_WindowRounding, 6),
        (dpg.mvStyleVar_WindowBorderSize, 1),
        (dpg.mvStyleVar_WindowTitleAlign, (0.5, 0.5)),
        (dpg.mvStyleVar_ChildRounding, 5),
        (dpg.mvStyleVar_ChildBorderSize, 1),
        (dpg.mvStyleVar_PopupRounding, 5),
        (dpg.mvStyleVar_PopupBorderSize, 1),
        (dpg.mvStyleVar_FramePadding, (7, 5)),
        (dpg.mvStyleVar_FrameRounding, 4),
        (dpg.mvStyleVar_FrameBorderSize, 1),
        (dpg.mvStyleVar_ItemSpacing, (8, 6)),
        (dpg.mvStyleVar_ItemInnerSpacing, (5, 4)),
        (dpg.mvStyleVar_IndentSpacing, 16),
        (dpg.mvStyleVar_ScrollbarSize, 11),
        (dpg.mvStyleVar_ScrollbarRounding, 6),
        (dpg.mvStyleVar_GrabMinSize, 9),
        (dpg.mvStyleVar_GrabRounding, 3),
        (dpg.mvStyleVar_TabRounding, 4),
        (dpg.mvStyleVar_TabBorderSize, 0),
        (dpg.mvStyleVar_ButtonTextAlign, (0.5, 0.5)),
        (dpg.mvStyleVar_SelectableTextAlign, (0.0, 0.5)),
    ]
    for target, value in styles:
        if isinstance(value, tuple):
            dpg.add_theme_style(target, value[0], value[1],
                                category=dpg.mvThemeCat_Core, parent=comp)
        else:
            dpg.add_theme_style(target, value, category=dpg.mvThemeCat_Core,
                                parent=comp)
    dpg.bind_theme(theme)

    font_path = find_font()
    if font_path:
        try:
            with dpg.font_registry(tag="font_registry"):
                font = dpg.add_font(font_path, int(FONT_SIZE),
                                    tag="font_regular")
                bold_path = find_bold_font()
                if bold_path:
                    dpg.add_font(bold_path, int(BOLD_FONT_SIZE),
                                 tag="font_bold")
            dpg.bind_font(font)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось привязать шрифт %s: %s", font_path, exc)
            font_path = None
    else:
        log.warning("Шрифт с кириллицей не найден — текст может отображаться "
                    "как «?»")
    dpg.set_viewport_clear_color(list(BG)[:3] + [255])
    return font_path


def bind_bold(item: str) -> None:
    """Жирный моно для заголовков (иерархия веса)."""
    try:
        if dpg.does_item_exist("font_bold") and dpg.does_item_exist(item):
            dpg.bind_item_font(item, "font_bold")
    except Exception:  # noqa: BLE001
        pass


def load_icon(viewport_ready: bool = True) -> bool:
    """Иконка приложения: assets/icon.png (256) и icon32.png — в иконки окна.

    Генерируется tools/make_icon.py в стиле интерфейса (графит + радарное
    кольцо + люминофорный силуэт). DPG принимает сырой float-RGBA из
    `load_image` напрямую (проверено на 2.3.1), PIL не нужен.
    """
    if not viewport_ready:
        return False
    ok = False
    for path, setter in ((os.path.join(ASSETS, "icon32.png"),
                          dpg.set_viewport_small_icon),
                         (os.path.join(ASSETS, "icon.png"),
                          dpg.set_viewport_large_icon)):
        if not os.path.isfile(path):
            continue
        try:
            _w, _h, _c, data = dpg.load_image(path)
            setter(data)
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.warning("Иконка %s не загружена: %s", path, exc)
    return ok
