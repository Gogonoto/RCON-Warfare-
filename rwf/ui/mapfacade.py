"""
Фасад тактической карты (секция 5 скилла tactical-ui-dearpygui).

Единственное место, где вызываются `dpg.draw_*` для карты. Владеет пером
`map_pen` (создано один раз — I2) и текстурой рельефа. Мир->экран — через
`MapRenderer.transform`, и тот же transform используется в хит-тестах,
поэтому клики и отрисовка не могут «разъехаться» (V2/чек-лист секции 9).

Порядок кадра: render() вызывается из главного цикла ПОСЛЕ drain и project:
1. слои/камера/планировщик синхронизируются из STATE;
2. `MapRenderer.render` выдаёт примитивы (вся тяжёлая геометрия там);
3. перо очищается `children_only=True` и перерисовывается интерпретатором
   из `painter.py` (emit-колбэк, никаких draw_* в других модулях).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import dearpygui.dearpygui as dpg

from . import painter
from . import state as st

log = logging.getLogger(__name__)

PEN = "map_pen"
TEX = "terrain_tex"
TEX_REGISTRY = "tex_registry"

#: порог пикселей: меньше — клик, больше — перетаскивание карты
DRAG_THRESHOLD = 5.0
#: радиус хит-теста юнита, px
UNIT_HIT_PX = 16.0
BASE_HIT_PX = 26.0


class DpgEmit:
    """Продукционный интерпретатор painter'а: примитив -> dpg.draw_*."""

    __slots__ = ("parent", "calls")

    def __init__(self, parent: str = PEN):
        self.parent = parent
        self.calls = 0

    def __call__(self, kind: str, **kw: Any) -> None:
        p = self.parent
        self.calls += 1
        if kind == "line":
            dpg.draw_line((kw["x1"], kw["y1"]), (kw["x2"], kw["y2"]),
                          color=kw["color"] or (255, 255, 255, 255),
                          thickness=kw["width"], parent=p)
        elif kind == "rect":
            fill = kw.get("fill")
            outline = kw.get("outline")
            x, y, w, h = kw["x"], kw["y"], kw["w"], kw["h"]
            dpg.draw_rectangle((x, y), (x + w, y + h),
                               color=outline or (0, 0, 0, 0),
                               thickness=kw["width"] if outline else 1,
                               fill=fill or (0, 0, 0, 0), parent=p)
        elif kind == "circle":
            fill = kw.get("fill")
            outline = kw.get("outline")
            dpg.draw_circle((kw["x"], kw["y"]), kw["r"],
                            color=outline or (0, 0, 0, 0),
                            thickness=kw["width"] if outline else 1,
                            fill=fill or (0, 0, 0, 0), parent=p)
        elif kind == "poly":
            fill = kw.get("fill")
            if kw.get("closed", True) and fill:
                dpg.draw_polygon(kw["points"],
                                 color=kw.get("outline") or (0, 0, 0, 0),
                                 thickness=kw["width"],
                                 fill=fill, parent=p)
            elif kw.get("closed", True):
                dpg.draw_polygon(kw["points"],
                                 color=kw.get("outline") or (0, 0, 0, 0),
                                 thickness=kw["width"],
                                 fill=(0, 0, 0, 0), parent=p)
            else:
                dpg.draw_polyline(kw["points"], closed=False,
                                  color=kw.get("outline") or (0, 0, 0, 0),
                                  thickness=kw["width"], parent=p)
        elif kind == "text":
            dpg.draw_text((kw["x"], kw["y"]), kw["text"],
                          size=kw["size"],
                          color=kw["color"] or (255, 255, 255, 255), parent=p)


class MapFacade:
    """Карта: перо, рельеф, камера, инструменты, хит-тесты."""

    def __init__(self, state: Dict[str, Any], facade, parent: str = "win_map"):
        self.state = state
        self.facade = facade
        self.renderer = facade.renderer
        self.emit = DpgEmit(PEN)
        self.parent_tag = parent
        self.w = 100.0
        self.h = 100.0
        self._tex_size: Optional[Tuple[int, int]] = None
        self._tex_np = None            # буфер float32 текстуры (для частичных обновлений)
        self._press: Optional[Tuple[float, float]] = None
        self._last_mouse: Optional[Tuple[float, float]] = None
        self._planner_drag: Optional[int] = None
        #: кэш примитивов слоёв-подложек (сетка/базы/зоны/маршруты/маркеры):
        #: пересчёт только при изменении ключа (FPS-01). Юниты/игроки/HUD —
        #: всегда свежие.
        self._bg_key: Optional[Tuple[Any, ...]] = None
        self._bg_prims: List[Dict[str, Any]] = []
        self.ctx = None          # ContextMenu: ставится в app.py (UX-14)

    # ------------------------------------------------------------ создание
    def create(self, parent: str, width: float, height: float) -> None:
        """Создать перо ОДИН раз (I2) и привязать обработчики мыши.

        Клики по перу — item-хендлеры (срабатывают только над картой);
        move/wheel/release — глобальные реестры DPG, поэтому каждый проверяет,
        что курсор внутри прямоугольника пера (иначе игнор).
        """
        dpg.add_drawlist(width=width, height=height, parent=parent, tag=PEN)
        with dpg.item_handler_registry(tag="map_hits"):
            dpg.add_item_clicked_handler(button=dpg.mvMouseButton_Left,
                                         callback=self._on_press_left)
            dpg.add_item_clicked_handler(button=dpg.mvMouseButton_Right,
                                         callback=self._on_press_right)
        dpg.bind_item_handler_registry(PEN, "map_hits")
        with dpg.handler_registry(tag="map_global_hits"):
            dpg.add_mouse_move_handler(callback=self._on_move)
            dpg.add_mouse_wheel_handler(callback=self._on_wheel)
            dpg.add_mouse_release_handler(button=dpg.mvMouseButton_Left,
                                          callback=self._on_release_left)
        self.resize(width, height)

    def resize(self, width: float, height: float) -> None:
        width = max(64.0, float(width))
        height = max(64.0, float(height))
        if (width, height) == (self.w, self.h):
            return
        self.w, self.h = width, height
        if dpg.does_item_exist(PEN):
            dpg.configure_item(PEN, width=width, height=height)
        self.renderer.transform.resize(width, height)

    # ------------------------------------------------------- координаты мыши
    def _pen_pos(self) -> Tuple[float, float]:
        """Позиция мыши в локальных координатах пера (одна точка калибровки V2)."""
        mx, my = dpg.get_mouse_pos(local=False)
        ox, oy = dpg.get_item_rect_min(PEN)
        return (mx - ox, my - oy)

    def _over_pen(self) -> bool:
        mx, my = dpg.get_mouse_pos(local=False)
        x0, y0 = dpg.get_item_rect_min(PEN)
        x1, y1 = dpg.get_item_rect_max(PEN)
        return x0 <= mx <= x1 and y0 <= my <= y1

    @staticmethod
    def _shift_down() -> bool:
        try:
            return (dpg.is_key_down(dpg.mvKey_Shift)
                    or dpg.is_key_down(dpg.mvKey_LShift)
                    or dpg.is_key_down(dpg.mvKey_RShift))
        except Exception:  # noqa: BLE001 - нет окна/ключа: без модификатора
            return False

    @staticmethod
    def _ctrl_down() -> bool:
        try:
            return (dpg.is_key_down(dpg.mvKey_Control)
                    or dpg.is_key_down(dpg.mvKey_LControl)
                    or dpg.is_key_down(dpg.mvKey_RControl))
        except Exception:  # noqa: BLE001
            return False

    def _world_under_mouse(self) -> Tuple[float, float]:
        lx, ly = self._pen_pos()
        return self.renderer.transform.to_world(lx, ly)

    # ------------------------------------------------------------- рендер
    def _auto_resize(self) -> None:
        """Перо следует за окном карты каждый кадр (DPG даёт размер окна
        только после первого layou-кадра — одноразового resize мало)."""
        if not dpg.does_item_exist(self.parent_tag):
            return
        w, h = dpg.get_item_rect_size(self.parent_tag)
        self.resize(w - 12.0, h - 34.0)

    def render(self) -> None:
        state = self.state
        renderer = self.renderer
        tf = renderer.transform
        self._auto_resize()

        # --- слои из STATE ---
        layers = state["layers"]
        renderer.units_layer.icon_scale = float(layers.get("icon_scale", 1.0))
        renderer.terrain_layer_enabled = bool(layers.get("terrain", True))
        for name in ("grid", "bases", "zones", "routes", "markers", "units",
                     "players", "planner", "tac", "hud"):
            renderer.set_layer_enabled(name, bool(layers.get(name, True)))

        # --- камера: слежение за выбранным ---
        tf.follow = False
        sel = state.get("selection")
        if state.get("follow") and sel is not None:
            u = (state["frame"].get("units") or {}).get(sel)
            if u:
                tf.set_center(u["pos"][0], u["pos"][2], keep_follow=False)

        # --- черновик планировщика -> слой карты ---
        renderer.planner.points = [
            (p["x"], p["z"], p.get("alt", 150.0), p.get("action", "navigate"))
            for p in state["planner"]["points"]
        ]

        # --- тактические черновики (TAC-01): слои TacLayer читает из STATE ---
        tac = renderer.tac
        tac.routes = state["unit_routes"]
        tac.targets = state["tac_targets"]
        tac.selected = list(state.get("selected_units") or [])
        drag = state.get("tac_drag")
        tac.drag = dict(drag) if drag else None

        snap = dict(state["frame"])
        tac.units = snap.get("units") or {}
        if drag:
            mouse = state["mouse"]
            tac.drag["cursor"] = (mouse.get("wx", 0.0), mouse.get("wz", 0.0))
        snap["bases"] = state["bases"]
        snap["selection"] = state.get("selection")
        snap["selected_base"] = state.get("selected_base")
        # первый угол зоны удара (инструмент «strike») — только в STATE,
        # рисуем его здесь же, не плодя draw_* по другим модулям
        # --- FPS-01: фон (сетка/базы/зоны/маршруты/маркеры) пересчитывается
        # только когда изменился камера или содержимое этих слоёв.
        tfk = (round(tf.center[0], 2), round(tf.center[1], 2),
               round(tf.scale, 4), self.w, self.h)
        bg_key = (tfk,
                  tuple(sorted((n, bool(layers.get(n, True)))
                               for n in ("grid", "bases", "zones", "routes",
                                         "markers"))),
                  snap.get("revision"),
                  # bases/маршруты живут в отдельных сообщениях без revision —
                  # берём дешёвые подписи, иначе кэш устарел бы намертво
                  tuple((b["id"], round(b["x"], 1), round(b["z"], 1),
                         round(b.get("heading", 0.0), 1))
                        for b in snap["bases"]),
                  {uid: (len(r.get("waypoints") or []),
                         r.get("active_index"), r.get("status"))
                   for uid, r in (snap.get("routes") or {}).items()},
                  len(snap.get("markers") or []))
        if bg_key != self._bg_key:
            self._bg_key = bg_key
            self._bg_prims = renderer.render_bg(snap)
        prims = self._bg_prims + renderer.render_fg(snap)
        corner = state.get("strike_corner")
        if corner is not None:
            sx, sy = tf.to_screen(corner[0], corner[1])
            mx, my = self._pen_pos()
            prims.append({"type": "rect",
                          "x": min(sx, mx), "y": min(sy, my),
                          "w": abs(mx - sx), "h": abs(my - sy),
                          "fill": "#ff334422", "outline": "#ff6666",
                          "width": 1, "dash": True})

        self._update_texture()
        dpg.delete_item(PEN, children_only=True)      # I2: только команды
        self._draw_terrain()
        painter.paint(self.emit, prims)

    # ------------------------------------------------------------- рельеф
    def _update_texture(self) -> None:
        t = self.state["terrain"]
        if not t.get("dirty"):
            return
        t["dirty"] = False
        data = t.get("data")
        if data is None:
            if self._tex_size is not None and dpg.does_item_exist(TEX):
                dpg.delete_item(TEX)
            self._tex_size = None
            self._tex_np = None
            return
        w, h = int(t["w"]), int(t["h"])
        try:
            import numpy as np
            arr = np.frombuffer(data, dtype=np.float32).reshape(-1)
        except ImportError:  # pragma: no cover
            arr = None
        if self._tex_size != (w, h):
            if dpg.does_item_exist(TEX):
                dpg.delete_item(TEX)
            payload = arr.copy() if arr is not None else data
            dpg.add_raw_texture(w, h, payload, format=dpg.mvFormat_Float_rgb,
                                parent=TEX_REGISTRY, tag=TEX)
            self._tex_size = (w, h)
            self._tex_np = arr
        elif arr is not None and self._tex_np is not None \
                and self._tex_np.size == arr.size:
            # Инкрементальное обновление: пишем изменённые блоки прямо в
            # буфер существующей float-текстуры (set_value на весь массив
            # при каждом скан-пакете стоил кадрового времени — FPS-01).
            changed = t.get("changed") or []
            dirty_any = False
            for rect in changed:
                cx0, cz0, cx1, cz1 = rect
                i0 = max(0, int(cx0)); i1 = min(w - 1, int(cx1))
                j0 = max(0, int(cz0)); j1 = min(h - 1, int(cz1))
                if i1 < i0 or j1 < j0:
                    continue
                flat = self._tex_np.reshape(h, w, 3)
                flat[j0:j1 + 1, i0:i1 + 1, :] = arr.reshape(h, w, 3)[j0:j1 + 1, i0:i1 + 1, :]
                dirty_any = True
            if not dirty_any:
                self._tex_np[:] = arr
            dpg.set_value(TEX, self._tex_np)
        else:
            dpg.set_value(TEX, arr if arr is not None else data)
            self._tex_np = arr

    def _draw_terrain(self) -> None:
        t = self.state["terrain"]
        rect = t.get("world")
        if t.get("data") is None or rect is None:
            return
        if not dpg.does_item_exist(TEX):
            return
        tf = self.renderer.transform
        x0, z0, x1, z1 = rect
        sx0, sy0 = tf.to_screen(x0, z0)
        sx1, sy1 = tf.to_screen(x1, z1)
        if sx1 - sx0 < 1.0 or sy1 - sy0 < 1.0:
            return
        dpg.draw_image(TEX, (sx0, sy0), (sx1, sy1), parent=PEN)

    # ------------------------------------------------------------ мышь: события
    def _on_move(self, sender, app_data, user_data) -> None:
        if not self._over_pen():
            self.state["mouse"]["inside"] = False
            self._last_mouse = None
            return
        lx, ly = self._pen_pos()
        wx, wz = self.renderer.transform.to_world(lx, ly)
        mouse = self.state["mouse"]
        mouse.update(wx=wx, wz=wz, sx=lx, sy=ly, inside=True)
        drag = self.state.get("tac_drag")
        if drag is not None and \
                dpg.is_mouse_button_down(dpg.mvMouseButton_Left):
            # активное тактическое перетаскивание (TAC-01, п.17 ТЗ)
            if drag["kind"] == "unit":
                st.tac_move_point(self.state, drag["uid"], 0, wx, wz)
            else:
                st.tac_move_point(self.state, drag["uid"], drag["index"],
                                  wx, wz)
            return
        if self._planner_drag is not None and \
                dpg.is_mouse_button_down(dpg.mvMouseButton_Left):
            st.planner_drag(self.state, self._planner_drag, wx, wz)
            return
        if self._press is not None and \
                dpg.is_mouse_button_down(dpg.mvMouseButton_Left):
            # перетаскивание карты
            if self._last_mouse is not None:
                dx = lx - self._last_mouse[0]
                dy = ly - self._last_mouse[1]
                self.renderer.transform.pan_screen(-dx, -dy)
        self._last_mouse = (lx, ly)

    def _on_press_left(self, sender, app_data, user_data) -> None:
        state = self.state
        pos = self._pen_pos()
        self._press = pos
        self._last_mouse = pos
        tool = state.get("tool", "select")
        if tool == "select" and state.get("selected_units"):
            # TAC-01: Shift+тяга от существующей точки маршрута -> новая точка
            hit_pt = self._tac_point_hit(*pos)
            if hit_pt is not None and self._shift_down():
                uid, idx = hit_pt
                pts = st.tac_unit_route(state, uid)
                state["tac_drag"] = {"kind": "new", "uid": uid,
                                     "index": len(pts), "from_index": idx,
                                     "cursor": (state["mouse"]["wx"],
                                                state["mouse"]["wz"])}
                return
            # TAC-01: тяга самой точки -> перестановка
            if hit_pt is not None:
                uid, idx = hit_pt
                state["tac_drag"] = {"kind": "point", "uid": uid, "index": idx,
                                     "moved": False,
                                     "cursor": (state["mouse"]["wx"],
                                                state["mouse"]["wz"])}
                return
            # TAC-01: тяга выделенного юнита -> весь маршрут едет за ним
            uid = self._unit_hit(*pos)
            if uid is not None and uid in (state.get("selected_units") or []):
                state["tac_drag"] = {"kind": "unit", "uid": uid, "moved": False,
                                     "cursor": (state["mouse"]["wx"],
                                                state["mouse"]["wz"])}
                return
        # захват точки планировщика под курсором — перетаскивание (UX-04)
        self._planner_drag = self._planner_hit(*pos)

    def _release_target_drop(self, drag: Dict[str, Any],
                             sx: float, sy: float) -> bool:
        """Окончание тактической тяги над объектом = засечь цель (п.17 ТЗ)."""
        state = self.state
        wx, wz = self.renderer.transform.to_world(sx, sy)
        uid_hit = self._unit_hit(sx, sy)
        if uid_hit is not None and uid_hit != drag.get("uid"):
            u = (state["frame"].get("units") or {}).get(uid_hit) or {}
            tgt = {"kind": "unit", "name": f"#{uid_hit} {u.get('label', '')}".strip(),
                   "x": wx, "z": wz}
        else:
            bid = self._base_hit(sx, sy)
            if bid is not None:
                b = next((b for b in state["bases"] if b["id"] == bid), None)
                tgt = ({"kind": "base", "name": b.get("name", "база"),
                        "x": b["x"], "z": b["z"]} if b else None)
            else:
                pname = self._player_hit(sx, sy)
                if pname is not None:
                    p = (state["frame"].get("players") or {}).get(pname) or {}
                    pos = p.get("pos") or (0, 0, 0)
                    tgt = {"kind": "player", "name": pname,
                           "x": pos[0], "z": pos[2]}
                else:
                    tgt = None
        if tgt is None:
            return False
        st.tac_set_target(state, int(drag["uid"]), tgt)
        self.facade.send("tac_apply", [int(drag["uid"])])
        self.facade.send("log_ui",
                         f"#{drag['uid']}: цель «{tgt['name']}» засечена, "
                         f"тип удара — в меню цели (ПКМ)", "cyan")
        return True

    def _on_release_left(self, sender, app_data, user_data) -> None:
        pos = self._pen_pos()
        press = self._press
        state = self.state
        self._press = None
        # --- завершение тактической тяги (TAC-01) --------------------------
        drag = state.get("tac_drag")
        if drag is not None:
            state["tac_drag"] = None
            moved = press is not None and \
                math.hypot(pos[0] - press[0], pos[1] - press[1]) \
                > DRAG_THRESHOLD
            if drag["kind"] == "new":
                wx, wz = self.renderer.transform.to_world(*pos)
                if moved and not self._release_target_drop(drag, *pos):
                    st.tac_add_point(state, drag["uid"], wx, wz)
                    self.facade.send("tac_apply", [int(drag["uid"])])
            elif drag["kind"] in ("point", "unit") and moved:
                if not self._release_target_drop(drag, *pos):
                    self.facade.send("tac_apply", [int(drag["uid"])])
            return
        # клик вне открытого контекстного меню закрывает его (UX-14)
        if self.ctx is not None and self.ctx.open:
            mx, my = dpg.get_mouse_pos(local=False)
            if not self.ctx.over(mx, my):
                self.ctx.close()
            return
        was_drag_idx = self._planner_drag
        self._planner_drag = None
        if press is None:
            return
        moved = math.hypot(pos[0] - press[0], pos[1] - press[1])
        if moved > DRAG_THRESHOLD:
            return                      # это было панорамирование/драг точки
        if was_drag_idx is not None:
            return                      # точку просто перетащили
        self._tool_click(*pos)

    def _on_press_right(self, sender, app_data, user_data) -> None:
        state = self.state
        if state.get("strike_corner") is not None:
            state["strike_corner"] = None
            return
        if self.ctx is None:
            return
        if self.ctx.open:                     # ПКМ при открытом меню = закрыть
            self.ctx.close()
            return
        if not self._over_pen():
            return
        sx, sy = self._pen_pos()
        wx, wz = self.renderer.transform.to_world(sx, sy)
        uid = self._unit_hit(sx, sy)
        bid = None if uid is not None else self._base_hit(sx, sy)
        player = None if (uid is not None or bid is not None) \
            else self._player_hit(sx, sy)
        items = self.ctx.items_for(uid, bid, player, wx, wz)
        mx, my = dpg.get_mouse_pos(local=False)
        self.ctx.open_at(mx, my, items)

    def _on_wheel(self, sender, app_data, user_data) -> None:
        if not self._over_pen():
            return
        try:
            delta = float(app_data)
        except (TypeError, ValueError):
            return
        if abs(delta) < 0.01:
            return
        factor = 0.85 if delta > 0 else 1.18
        lx, ly = self._pen_pos()
        self.renderer.transform.zoom_at(factor, lx, ly)

    # ------------------------------------------------------------ хит-тесты
    def _planner_hit(self, sx: float, sy: float) -> Optional[int]:
        pts = self.state["planner"]["points"]
        tf = self.renderer.transform
        best, best_d = None, 12.0
        for i, p in enumerate(pts):
            px, py = tf.to_screen(p["x"], p["z"])
            d = math.hypot(px - sx, py - sy)
            if d <= best_d:
                best, best_d = i, d
        return best

    def _tac_point_hit(self, sx: float, sy: float) -> Optional[Tuple[int, int]]:
        """Хит-тест черновых точек маршрута (TAC-01): (uid, index) или None."""
        tf = self.renderer.transform
        best: Optional[Tuple[int, int]] = None
        best_d = 12.0
        for uid, pts in (self.state["unit_routes"] or {}).items():
            for i, p in enumerate(pts):
                px, py = tf.to_screen(p["x"], p["z"])
                d = math.hypot(px - sx, py - sy)
                if d <= best_d:
                    best, best_d = (uid, i), d
        return best

    def _unit_hit(self, sx: float, sy: float) -> Optional[int]:
        units = (self.state["frame"].get("units") or {})
        tf = self.renderer.transform
        best, best_d = None, UNIT_HIT_PX
        for uid, u in units.items():
            px, py = tf.to_screen(u["pos"][0], u["pos"][2])
            d = math.hypot(px - sx, py - sy)
            if d <= best_d:
                best, best_d = uid, d
        return best

    def _player_hit(self, sx: float, sy: float) -> Optional[str]:
        players = (self.state["frame"].get("players") or {})
        tf = self.renderer.transform
        best, best_d = None, 14.0
        for name, rec in players.items():
            pos = rec.get("pos") or (0, 0, 0)
            px, py = tf.to_screen(pos[0], pos[2])
            d = math.hypot(px - sx, py - sy)
            if d <= best_d:
                best, best_d = name, d
        return best

    def _base_hit(self, sx: float, sy: float) -> Optional[int]:
        tf = self.renderer.transform
        best, best_d = None, BASE_HIT_PX
        for b in self.state["bases"]:
            px, py = tf.to_screen(b["x"], b["z"])
            d = math.hypot(px - sx, py - sy)
            if d <= max(BASE_HIT_PX, b.get("radius", 60.0) * tf.scale * 0.15):
                if best is None or d < best_d:
                    best, best_d = b["id"], d
        return best

    # ------------------------------------------------------------ инструменты
    def _tool_click(self, sx: float, sy: float) -> None:
        state = self.state
        wx, wz = self.renderer.transform.to_world(sx, sy)
        tool = state.get("tool", "select")

        if tool == "select":
            additive = self._shift_down() or self._ctrl_down()
            uid = self._unit_hit(sx, sy)
            if uid is not None:
                st.tac_select(state, uid, additive=additive)
                self.facade.send("select", state.get("selection"))
                return
            if additive:
                return          # Shift+клик по земле — не сбрасывает выделение
            bid = self._base_hit(sx, sy)
            state["selected_base"] = bid
            if bid is None:
                # TAC-01 (п.17 ТЗ): клик по карте у выделенной техники =
                # назначить/продлить маршрут; обычный клик по пустому месту
                # снимает выделение.
                sel_units = list(state.get("selected_units") or [])
                if sel_units and state.get("tac_click_routes", True):
                    wx2, wz2 = self.renderer.transform.to_world(sx, sy)
                    for u in sel_units:
                        st.tac_add_point(state, u, wx2, wz2)
                    self.facade.send("tac_apply", [int(u) for u in sel_units])
                    return
                st.tac_select(state, None)
                self.facade.send("select", None)
            return

        if tool == "waypoint":
            alt = 150.0
            if dpg.does_item_exist("in_wp_alt"):
                try:
                    alt = float(dpg.get_value("in_wp_alt") or 150.0)
                except (TypeError, ValueError):
                    alt = 150.0
            st.planner_add(state, wx, wz, alt=alt)
            return

        if tool == "wp_del":
            idx = self._planner_hit(sx, sy)
            if idx is not None:
                st.planner_remove(state, idx)
            return

        if tool == "strike":
            corner = state.get("strike_corner")
            if corner is None:
                state["strike_corner"] = (wx, wz)
            else:
                self.facade.send("set_strike_zone", corner[0], corner[1], wx, wz)
                state["strike_corner"] = None
            return

        if tool == "base":
            nb = state.get("new_base") or {}
            name = nb.get("name") or "База"
            kind = nb.get("kind") or "airport"
            self.facade.send("add_base", name, kind, wx, wz)
            return

        if tool == "carrier":
            bid = state.get("selected_base")
            base = next((b for b in state["bases"] if b["id"] == bid), None)
            if base is not None and base.get("movable"):
                self.facade.send("move_base", bid, wx, wz)
                return
            # иначе — выбрать авианосец кликом
            hit = self._base_hit(sx, sy)
            carriers = [b for b in state["bases"] if b.get("movable")]
            if hit is not None:
                state["selected_base"] = hit
            elif carriers:
                state["selected_base"] = carriers[0]["id"]
                self.facade.send("log_ui",
                                 f"Курс для «{carriers[0]['name']}»: "
                                 f"кликните точку карты", "cyan")
            return
