"""
Иконочные холсты для виджетов: vector-icon -> drawlist малого размера.

Кнопки/заголовки DPG не принимают картинки, поэтому иконка живёт РОДОМ
drawlist рядом с виджетом (или вместо заголовка секции) и кликается своим
item-хендлером с тем же колбэком. Все холсты учитываются в реестре модуля,
`redraw()` перекрашивает/меняет иконку на лету (активный инструмент,
шеврон секции, тип юнита в строке списка).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

import dearpygui.dearpygui as dpg

from .. import uicons
from . import painter
from .mapfacade import DpgEmit

log = logging.getLogger(__name__)

Color = Tuple[int, int, int, int]

#: tag -> (name, size, color, width)
_REGISTRY: Dict[str, Tuple[str, float, Color, float]] = {}


def _paint(tag: str) -> None:
    rec = _REGISTRY.get(tag)
    if rec is None or not dpg.does_item_exist(tag):
        return
    name, size, color, width = rec
    dpg.delete_item(tag, children_only=True)
    emit = DpgEmit(tag)
    painter.paint(emit, uicons.prims(name, size / 2.0, size / 2.0,
                                     size * 0.92, color, width))


def icon_canvas(tag: str, parent: str, name: str, size: float = 16.0,
                color: Color = (126, 142, 126, 255), width: float = 1.6,
                callback: Optional[Callable] = None,
                user_data: Any = None) -> str:
    """Создать холст с иконкой; `callback` — кликабельность как у кнопки."""
    if dpg.does_item_exist(tag):
        dpg.delete_item(tag)
    dpg.add_drawlist(width=size, height=size, parent=parent, tag=tag)
    _REGISTRY[tag] = (name, size, color, width)
    _paint(tag)
    if callback is not None:
        hits = tag + "_hits"
        if dpg.does_item_exist(hits):
            dpg.delete_item(hits)
        with dpg.item_handler_registry(tag=hits):
            dpg.add_item_clicked_handler(callback=callback,
                                         user_data=user_data)
        dpg.bind_item_handler_registry(tag, hits)
    return tag


def redraw(tag: str, name: Optional[str] = None,
           color: Optional[Color] = None) -> None:
    rec = _REGISTRY.get(tag)
    if rec is None:
        return
    n, s, c, w = rec
    _REGISTRY[tag] = (name or n, s, color or c, w)
    _paint(tag)


def paint_raw(tag: str, prims) -> None:
    """Перерисовать холст произвольными примитивами painter'а
    (силуэты техники в строках списка юнитов)."""
    if not dpg.does_item_exist(tag):
        return
    dpg.delete_item(tag, children_only=True)
    painter.paint(DpgEmit(tag), prims)
