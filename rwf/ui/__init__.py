"""
Интерфейс RCON Warfare на Dear PyGui.

Порт с PySide6 по скиллу `skills/tactical-ui-dearpygui.md` (решение v13):
один поток GUI, STATE — единственный источник правды, обмен с ядром через
очереди MSG_Q/OUT_Q, карта — фасад `mapfacade.render()`.

Структура (роутинг по секциям скилла — см. заголовки модулей):
    app.py        — главный цикл (секции 1, 7)
    state.py      — STATE и редуктор сообщений (секция 2)
    build.py      — статические виджеты/доки (секции 3, 4)
    project.py    — проекция STATE -> виджеты (секции 3, 6)
    mapfacade.py  — фасад карты: перо, рельеф, хит-тесты (секция 5)
    painter.py    — интерпретатор примитивов maprender (без dpg)
    facade.py     — управление ядром, рабочие потоки (секция 7, без dpg)
    theme.py      — стиль, шрифт с кириллицей, иконка (секция 8)
"""
from __future__ import annotations

from typing import Optional


def run(cfg=None, headless_frames: int = 0,
        screenshot: Optional[str] = None) -> int:
    from ..config import AppConfig
    from .app import run as _run
    return _run(cfg or AppConfig(), headless_frames=headless_frames,
                screenshot=screenshot)
