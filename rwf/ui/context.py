"""
Контекстное меню по ПКМ (UX-14, жалоба заказчика «вынести интеракции на ПКМ»).

Одно плавающее окно `win_ctx` с пулом кнопок (I2): при открытии наполняется
пунктами под объект под курсором (юнит / база / игрок / пустая карта) и
ставится в координаты мыши; закрывается кликом вне меню, пунктом меню или
Esc-аналогом (ПКМ по карте при открытом меню).

Колбэки пунктов НЕ содержат логики — только `facade.send` / мутации STATE
в главном потоке (I1/I4).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

import dearpygui.dearpygui as dpg

from . import state as st
from . import theme as T

CTX_W = 208
CTX_ITEMS = 14

Item = Tuple[str, Optional[Callable[[], None]]]


def _wp_alt() -> float:
    try:
        return float(dpg.get_value("in_wp_alt") or 150.0)
    except Exception:  # noqa: BLE001
        return 150.0


def _open_section(key: str) -> None:
    from . import build as B
    B.set_section_open(key, True)


class ContextMenu:
    def __init__(self, state: Dict[str, Any], facade, map_ui):
        self.state = state
        self.facade = facade
        self.map_ui = map_ui
        self._items: List[Item] = []
        self._build()

    # ------------------------------------------------------------- создание
    def _build(self) -> None:
        with dpg.window(tag="win_ctx", no_title_bar=True, no_move=True,
                        no_resize=True, no_scrollbar=True, show=False,
                        width=CTX_W):
            with dpg.group(tag="ctx_col"):
                for i in range(CTX_ITEMS):
                    dpg.add_button(label="", width=CTX_W - 16, height=22,
                                   tag=f"ctx_btn_{i}", user_data=i,
                                   callback=self._cb_item, show=False)
                    dpg.add_separator(tag=f"ctx_sep_{i}", show=False)

    # -------------------------------------------------------------- служба
    @property
    def open(self) -> bool:
        return bool(dpg.does_item_exist("win_ctx") and
                    dpg.is_item_shown("win_ctx"))

    def close(self) -> None:
        if dpg.does_item_exist("win_ctx"):
            dpg.configure_item("win_ctx", show=False)

    def over(self, mx: float, my: float) -> bool:
        if not self.open:
            return False
        x0, y0 = dpg.get_item_rect_min("win_ctx")
        x1, y1 = dpg.get_item_rect_max("win_ctx")
        return x0 <= mx <= x1 and y0 <= my <= y1

    # -------------------------------------------------------------- открытие
    def open_at(self, sx: float, sy: float, items: List[Item]) -> None:
        self._items = items
        slot = 0
        for label, cb in items:
            if slot >= CTX_ITEMS:
                break
            if label == "-":
                dpg.configure_item(f"ctx_btn_{slot}", show=False)
                dpg.configure_item(f"ctx_sep_{slot}", show=True)
            else:
                dpg.configure_item(f"ctx_sep_{slot}", show=False)
                dpg.configure_item(f"ctx_btn_{slot}", show=True, label=label)
            slot += 1
        for i in range(slot, CTX_ITEMS):
            dpg.configure_item(f"ctx_btn_{i}", show=False)
            dpg.configure_item(f"ctx_sep_{i}", show=False)
        vw = dpg.get_viewport_client_width() or 1200
        vh = dpg.get_viewport_client_height() or 800
        h = slot * 26 + 12
        x = min(max(4.0, sx), vw - CTX_W - 8)
        y = min(max(4.0, sy), vh - h - 8)
        dpg.configure_item("win_ctx", pos=(int(x), int(y)), show=True)

    def _cb_item(self, sender, app_data, user_data) -> None:
        idx = int(user_data)
        self.close()
        if 0 <= idx < len(self._items):
            cb = self._items[idx][1]
            self.facade.sound_play("ui")
            if cb:
                cb()

    # ------------------------------------------------------------- наполнение
    def items_for(self, uid: Optional[int], base_id: Optional[int],
                  player: Optional[str], wx: float, wz: float) -> List[Item]:
        state, facade = self.state, self.facade
        items: List[Item] = []
        if uid is not None:
            items += [
                ("Выбрать", lambda: self._select_unit(uid)),
                ("В пульт", lambda: self._select_unit(uid)),
                ("Следить за объектом", self._toggle_follow),
                ("-", None),
                # Ручное ТО убрано (UX-15): заправка/снаряжение/ремонт
                # происходят АВТОМАТИЧЕСКИ по касанию базы. Оператор
                # управляет РЕЖИМОМ службы, а не услугами.
                ("Стоянка: возврат и авто-ТО",
                 lambda: facade.send("set_duty", uid, "park")),
                ("В бой: взлёт и патруль/маршрут",
                 lambda: facade.send("set_duty", uid, "combat")),
                ("Сдать груз на базу (логистика)",
                 lambda: (self._select_unit(uid),
                          facade.send("deliver_cargo_selected"))),
                ("-", None),
                ("Огонь: готовое оружие",
                 lambda: (self._select_unit(uid),
                          facade.send("fire_selected"))),
                ("Снять с карты",
                 lambda: facade.send("despawn", uid, False)),
            ]
        elif base_id is not None:
            base = next((b for b in state.get("bases") or []
                         if b["id"] == base_id), None)
            items += [
                ("Выбрать базу", lambda: self._select_base(base_id)),
                ("Центрировать камеру",
                 lambda: self._center(wx, wz)),
            ]
            if base is not None and base.get("movable"):
                items.append((
                    "Курс авианосца сюда",
                    lambda: (self._select_base(base_id),
                             facade.send("move_base", base_id, wx, wz))))
            items += [
                ("-", None),
                ("Открыть раздел «Запуск»",
                 lambda: _open_section("launch")),
            ]
        elif player is not None:
            items += [
                ("Игрок в пульт",
                 lambda: state.update(console_obj=("player", player))),
                ("Центрировать камеру", lambda: self._center(wx, wz)),
            ]
        else:
            items += [
                ("Точка маршрута сюда",
                 lambda: st.planner_add(state, wx, wz, alt=_wp_alt())),
                ("Зона удара отсюда",
                 lambda: state.update(strike_corner=(wx, wz))),
                ("Поставить базу сюда",
                 lambda: facade.send("add_base",
                                     (state.get("new_base") or {}).get("name")
                                     or "База",
                                     (state.get("new_base") or {}).get("kind")
                                     or "airport", wx, wz)),
                ("-", None),
                ("Центрировать камеру", lambda: self._center(wx, wz)),
                ("Скан рельефа здесь",
                 lambda: facade.send("scan", wx, wz, 300, 8)),
            ]
        return items

    # -------------------------------------------------------------- действия
    def _select_unit(self, uid: int) -> None:
        self.state["selection"] = uid
        self.state["console_obj"] = ("unit", uid)
        self.facade.send("select", uid)
        if self.state.get("follow"):
            self.facade.follow_uid = uid

    def _select_base(self, base_id: int) -> None:
        self.state["selected_base"] = base_id
        self.state["console_obj"] = ("base", base_id)

    def _toggle_follow(self) -> None:
        val = not bool(self.state.get("follow"))
        self.state["follow"] = val
        if dpg.does_item_exist("chk_follow"):
            dpg.set_value("chk_follow", val)
        self.facade.follow_uid = self.state.get("selection") if val else None

    def _center(self, wx: float, wz: float) -> None:
        self.facade.renderer.transform.set_center(wx, wz)
