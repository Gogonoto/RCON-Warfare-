"""
Главный цикл RCON Warfare (канонический скелет секции 1 скилла
tactical-ui-dearpygui — контракт, который нельзя переизобретать):

    drain_messages()          # MSG_Q -> STATE (только здесь, I1)
    project_state_to_widgets()# STATE -> виджеты (I4)
    map_ui.render()           # STATE -> перо карты (секция 5)
    dpg.render_dearpygui_frame()

Порядок останова: сначала просим рабочие потоки остановиться и ждём их,
только потом destroy_context — чтобы ни один поток не полез в разобранный
контекст (секция 7).
"""
from __future__ import annotations

import logging
import os
import queue
import time
from typing import Any, Dict, Optional, Tuple

import dearpygui.dearpygui as dpg

from ..config import AppConfig
from ..settings import Settings
from . import build as B
from . import project as P
from . import theme
from .context import ContextMenu
from .facade import CoreFacade
from .mapfacade import MapFacade
from .state import apply_message, make_state

log = logging.getLogger(__name__)

#: предел сообщений на кадр: защищает кадр от лавины после скана
MAX_DRAIN_PER_FRAME = 400

#: FPS-02: окно усреднения профиля фаз кадра, кадров
PROFILE_EVERY = 30
#: FPS-02: профиль включается явно (`RWF_PROFILE=1`) — по умолчанию в консоль
#: ничего не печатается, а отметки времени в кадре даже не снимаются
PROFILE_ON = bool(os.environ.get("RWF_PROFILE"))


def _now(on: bool) -> float:
    """Отметка времени для профиля; при выключенном профиле — 0.0."""
    return time.perf_counter() if on else 0.0


def drain_messages(msg_q: queue.Queue, state: Dict[str, Any]) -> int:
    n = 0
    while n < MAX_DRAIN_PER_FRAME:
        try:
            msg = msg_q.get_nowait()
        except queue.Empty:
            return n
        try:
            apply_message(state, msg)
        except Exception:  # noqa: BLE001 - одно сообщение не роняет UI
            log.exception("MSG_Q: ошибка разбора %r", msg)
        n += 1
    return n


def _relayout(map_ui: MapFacade) -> None:
    """Пересчёт раскладки. Вызывается при смене размера вьюпорта и периодически:
    GLFW/X11 могут на переходных кадрах сообщать промежуточный client-размер,
    периодический вызов гарантирует сходимость к истинному значению."""
    B.layout()
    w, h = B.pen_client_size()
    if w > 80 and h > 80:
        map_ui.resize(w, h)


def run(cfg: Optional[AppConfig] = None, headless_frames: int = 0,
        screenshot: Optional[str] = None, autoconnect: Optional[bool] = None,
        on_frame: Optional[Any] = None,
        settings: Optional[Settings] = None) -> int:
    """Запуск интерфейса. `headless_frames>0` — отрендерить N кадров и выйти,
    `on_frame(frames, state, facade)` — хук между drain и проекцией
    (используется tools/screenshot.py для постановки демонстрации)."""
    cfg = cfg or AppConfig()
    settings = settings or Settings.load()
    state = make_state()
    state["connection"].update(host=cfg.rcon.host, port=cfg.rcon.port,
                               password=cfg.rcon.password,
                               mock=bool(cfg.rcon.mock))
    state["new_base"] = {"name": "База 1", "kind": "airport"}
    state["hotbar"] = bool(settings.get("ui.hotbar", True))
    state["settings_ui_right_w"] = int(settings.get("ui.right_w", 444))
    # UX-02/UX-03: сохранённые поведение и масштаб UI применяем до сборки
    # интерфейса — шрифты создаются сразу с нужным размером, инерция
    # карты читается из transform при первом же render().
    theme.set_ui_scale(float(settings.get("ui.scale", 1.0)))
    # TAC-01: фасад получает STATE — тактические черновики (выделение, точки
    # маршрута, засечённые цели) читаются из него при tac_apply().
    facade = CoreFacade(cfg, settings=settings, state=state)
    facade.start()

    dpg.create_context()
    dpg.create_viewport(title="RCON Warfare — тактический слой",
                        width=1600, height=900,
                        min_width=1100, min_height=680)
    dpg.setup_dearpygui()
    theme.bind()          # тема и кириллический шрифт — после setup
    theme.load_icon()

    map_ui = MapFacade(state, facade)
    # UX-02: сохранённая настройка инерции применяется к камере сразу
    map_ui.renderer.transform.inertia = bool(
        settings.get("ui.inertia", True))
    B.build_static_ui(state, facade, map_ui,
                      on_exit=lambda: dpg.stop_dearpygui())
    map_ui.ctx = ContextMenu(state, facade, map_ui)   # ПКМ-меню (UX-14)
    state["ctx"] = map_ui.ctx
    dpg.set_viewport_resize_callback(lambda s, a, u: _relayout(map_ui))
    dpg.show_viewport()
    _relayout(map_ui)

    connect_now = (cfg.rcon.mock if autoconnect is None else autoconnect)
    if connect_now:
        facade.send("connect", None, None, None, bool(cfg.rcon.mock))

    frames = 0
    last_vp: Tuple[int, int] = (0, 0)
    # --- FPS-02: профиль фаз кадра (только с RWF_PROFILE=1) -----------------
    # Какая из четырёх фаз (drain/project/map/render) съедает кадр — вопрос,
    # на который нельзя отвечать догадкой: считаем сумму времён за окно в
    # PROFILE_EVERY кадров и печатаем сводку в мс/кадр. «прочее» — разница
    # между реальным временем окна и суммой фаз (раскладка, опрос вьюпорта,
    # ожидание swap в render_dearpygui_frame). При выключенном профиле
    # отметки времени не снимаются вовсе — кадр не платит за диагностику.
    prof = {"drain": 0.0, "project": 0.0, "map": 0.0, "render": 0.0}
    prof_frames = 0
    prof_window_start = time.perf_counter()
    try:
        while dpg.is_dearpygui_running():
            vp = (dpg.get_viewport_client_width(),
                  dpg.get_viewport_client_height())
            if (vp != last_vp and vp[0] and vp[1]) or frames % 30 == 0:
                last_vp = vp
                _relayout(map_ui)
            t_drain = _now(PROFILE_ON)
            drain_messages(facade.msg_q, state)
            t_drained = _now(PROFILE_ON)
            if on_frame is not None:
                try:
                    on_frame(frames, state, facade)
                except Exception:  # noqa: BLE001
                    log.exception("on_frame")
            P.project_state_to_widgets(state, facade)
            t_projected = _now(PROFILE_ON)
            try:
                map_ui.render()
            except Exception:  # noqa: BLE001 - карта не роняет цикл
                log.exception("Ошибка отрисовки карты")
            t_mapped = _now(PROFILE_ON)
            dpg.render_dearpygui_frame()
            t_rendered = _now(PROFILE_ON)

            if PROFILE_ON:
                prof["drain"] += t_drained - t_drain
                prof["project"] += t_projected - t_drained
                prof["map"] += t_mapped - t_projected
                prof["render"] += t_rendered - t_mapped
                prof_frames += 1
                if prof_frames >= PROFILE_EVERY:
                    span = max(1e-9, t_rendered - prof_window_start)
                    ms = 1000.0 / prof_frames
                    total = sum(prof.values())
                    other = span - total
                    print(f"[FPS-02] {prof_frames} кадров за {span:.2f} с → "
                          f"{prof_frames / span:.1f} fps | мс/кадр: "
                          f"drain {prof['drain'] * ms:.2f}  "
                          f"project {prof['project'] * ms:.2f}  "
                          f"map {prof['map'] * ms:.2f}  "
                          f"render {prof['render'] * ms:.2f}  "
                          f"прочее {other * ms:.2f}  "
                          f"сумма {total * ms:.2f}", flush=True)
                    for key in prof:
                        prof[key] = 0.0
                    prof_frames = 0
                    prof_window_start = t_rendered

            frames += 1
            if headless_frames and frames >= headless_frames:
                if screenshot:
                    dpg.output_frame_buffer(screenshot)
                    dpg.render_dearpygui_frame()   # кадр, который снимается
                    log.info("Скриншот сохранён: %s", screenshot)
                break
    finally:
        facade.stop(timeout=4.0)
        try:
            dpg.destroy_context()
        except Exception:  # noqa: BLE001
            pass
    return 0
