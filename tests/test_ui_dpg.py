"""Тесты UI-слоя RCON Warfare (Dear PyGui).

Проверяется всё, что не требует GL-контекста: интерпретатор примитивов
карты (`painter`), редуктор STATE и операции планировщика (`state`),
логика фасада ядра (`facade`) — включая библиотеку маршрутов.
Интеграционный тест полного цикла рендера выполняется только при наличии
X-дисплея (Xvfb), иначе пропускается.
"""
from __future__ import annotations

import os
import queue
import tempfile
import unittest
from pathlib import Path

from rwf.config import AppConfig
from rwf.ui import painter, state as st
from rwf.ui.facade import CoreFacade


class Rec:
    """Записывающий интерпретатор для painter.paint."""

    def __init__(self):
        self.calls = []

    def __call__(self, kind, **kw):
        self.calls.append((kind, kw))


class TestPainter(unittest.TestCase):
    def test_parse_color_forms(self):
        self.assertEqual(painter.parse_color("#fff"), (255, 255, 255, 255))
        self.assertEqual(painter.parse_color("#ff8800"), (255, 136, 0, 255))
        self.assertEqual(painter.parse_color("#ff880080"), (255, 136, 0, 128))
        self.assertEqual(painter.parse_color("#ff8800", 64), (255, 136, 0, 64))
        self.assertIsNone(painter.parse_color(None))
        self.assertEqual(painter.parse_color((10, 20, 30)), (10, 20, 30, 255))
        self.assertEqual(painter.parse_color("мусор"), (255, 255, 255, 255))

    def test_dash_segments(self):
        segs = painter.dash_segments(0, 0, 100, 0, dash=7, gap=5)
        self.assertGreater(len(segs), 5)
        self.assertAlmostEqual(segs[0][0], 0.0)
        self.assertAlmostEqual(segs[0][2], 7.0)
        self.assertAlmostEqual(segs[-1][2], 100.0)
        one = painter.dash_segments(0, 0, 0, 0)
        self.assertEqual(one, [(0, 0, 0, 0)])

    def test_anchor_center(self):
        x, y = painter.anchor_point("abcd", 100.0, 50.0, 10.0, "center")
        w, h = painter.text_size("abcd", 10.0)
        self.assertAlmostEqual(x, 100.0 - w / 2)
        self.assertAlmostEqual(y, 50.0 - h / 2)
        nx, ny = painter.anchor_point("abcd", 100.0, 50.0, 10.0, "nw")
        self.assertEqual((nx, ny), (100.0, 50.0))

    def test_paint_all_kinds(self):
        prims = [
            {"type": "line", "x1": 0, "y1": 0, "x2": 10, "y2": 0,
             "color": "#ffffff", "width": 2},
            {"type": "line", "x1": 0, "y1": 5, "x2": 40, "y2": 5,
             "color": "#ffffff", "width": 1, "dash": True},
            {"type": "rect", "x": 1, "y": 2, "w": 30, "h": 20,
             "fill": "#11223344", "outline": "#445566", "width": 1},
            {"type": "circle", "x": 5, "y": 5, "r": 4,
             "fill": None, "outline": "#00ff00", "width": 1},
            {"type": "poly", "points": [(0, 0), (10, 0), (10, 10)],
             "fill": "#0000ff88", "outline": "#ffffff", "width": 2},
            {"type": "text", "x": 3, "y": 3, "text": "ПРИВЕТ",
             "color": "#00ff00", "size": 9, "anchor": "center"},
            {"type": "text", "x": 3, "y": 3, "text": "", "color": "#fff"},
            {"type": "poly", "points": [(1, 1)], "outline": "#fff"},
        ]
        rec = Rec()
        n = painter.paint(rec, prims)
        kinds = [c[0] for c in rec.calls]
        self.assertIn("line", kinds)
        self.assertIn("rect", kinds)
        self.assertIn("circle", kinds)
        self.assertIn("poly", kinds)
        self.assertIn("text", kinds)
        self.assertEqual(kinds.count("text"), 1, "пустой текст не рисуем")
        self.assertEqual(kinds.count("poly"), 1, "полигон из 1 точки пропускаем")
        self.assertGreaterEqual(kinds.count("line"), 2, "пунктир = сегменты")
        self.assertEqual(n, len(rec.calls))
        # цвета дошли как RGBA-кортежи
        for _kind, kw in rec.calls:
            for key in ("color", "fill", "outline"):
                if key in kw and kw[key] is not None:
                    self.assertEqual(len(kw[key]), 4)

    def test_paint_empty(self):
        self.assertEqual(painter.paint(Rec(), []), 0)


class TestState(unittest.TestCase):
    def setUp(self):
        self.state = st.make_state()

    def test_apply_log_and_unknown(self):
        st.apply_message(self.state, ("log", "привет", "green"))
        self.assertEqual(self.state["log"][-1], ("привет", "green"))
        self.assertEqual(self.state["log_rev"], 1)
        st.apply_message(self.state, ("неизвестная_тема",))
        self.assertIn("неизвестная_тема", self.state["log"][-1][0])

    def test_apply_frame_and_terrain(self):
        st.apply_message(self.state, ("frame", {"revision": 7, "units": {}}))
        self.assertEqual(self.state["frame_rev"], 7)
        st.apply_message(self.state, ("terrain", b"rgba", 2, 2, (0, 0, 1, 1),
                                      ("k",)))
        t = self.state["terrain"]
        self.assertTrue(t["dirty"])
        self.assertEqual(t["w"], 2)
        st.apply_message(self.state, ("terrain_clear",))
        self.assertIsNone(self.state["terrain"]["data"])

    def test_apply_connection_and_misc(self):
        st.apply_message(self.state, ("connected", True, "vanilla 1.20.1"))
        self.assertTrue(self.state["connection"]["connected"])
        st.apply_message(self.state, ("bases", [{"id": 1}]))
        self.assertEqual(self.state["bases"], [{"id": 1}])
        st.apply_message(self.state, ("console", 3, {"id": 3}))
        self.assertEqual(self.state["console"], {"id": 3})
        st.apply_message(self.state, ("scan", 5, 10, True))
        self.assertEqual(self.state["scan"]["done"], 5)
        st.apply_message(self.state, ("manpads", {"Arlik88": {"lock": None}}))
        self.assertIn("Arlik88", self.state["manpads"])

    def test_planner_ops(self):
        st.planner_add(self.state, 1.0, 2.0, 100.0, "bomb")
        st.planner_add(self.state, 3.0, 4.0, 120.0, "navigate")
        st.planner_add(self.state, 5.0, 6.0, 80.0, "strafe")
        self.assertEqual(len(self.state["planner"]["points"]), 3)
        st.planner_move(self.state, 2, 0)
        self.assertEqual(self.state["planner"]["points"][0]["x"], 5.0)
        st.planner_set_action(self.state, 0, "recon")
        self.assertEqual(self.state["planner"]["points"][0]["action"], "recon")
        st.planner_drag(self.state, 1, 9.0, 9.0)
        self.assertEqual(self.state["planner"]["points"][1]["x"], 9.0)
        st.planner_remove(self.state, 0)
        self.assertEqual(len(self.state["planner"]["points"]), 2)
        st.planner_remove(self.state, 99)      # вне диапазона — тихо
        st.planner_clear(self.state)
        self.assertEqual(self.state["planner"]["points"], [])

    def test_planner_loaded_topic(self):
        st.apply_message(self.state, ("planner_loaded",
                                      [{"x": 1, "z": 2, "alt": 3,
                                        "action": "navigate"}]))
        self.assertEqual(len(self.state["planner"]["points"]), 1)


class TestFacade(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = AppConfig()
        self.cfg._path = Path(self.tmp.name) / "rwf.json"  # type: ignore[attr-defined]
        self.facade = CoreFacade(self.cfg)

    def tearDown(self):
        self.facade.stop(timeout=1.0)
        self.tmp.cleanup()

    def test_send_queues_command(self):
        self.facade.send("select", 5)
        self.assertEqual(self.facade.out_q.get_nowait(), ("select", (5,)))

    def test_static_catalogs(self):
        self.assertTrue(self.facade.variants())
        labels = dict(self.facade.action_labels())
        self.assertIn("bomb", labels)
        self.assertTrue(self.facade.ai_templates())

    def test_select_publishes(self):
        self.facade.select(11)
        self.assertEqual(self.facade.selected_uid, 11)
        self.assertIn(("selected", 11), list(_drain(self.facade.msg_q)))

    def test_route_library_roundtrip(self):
        pts = [{"x": 10.0, "z": 20.0, "alt": 150.0, "action": "navigate"},
               {"x": 30.0, "z": 40.0, "alt": 120.0, "action": "bomb"}]
        self.facade.save_route("тест", pts)
        self.assertIn("тест", self.facade.library.names())
        loaded = self.facade.load_route("тест")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[1]["action"], "bomb")
        topics = [m[0] for m in _drain(self.facade.msg_q)]
        self.assertIn("planner_loaded", topics)
        self.facade.delete_route("тест")
        self.assertNotIn("тест", self.facade.library.names())

    def test_commands_without_connection_are_safe(self):
        """Без подключения команды не падают, а пишут предупреждение."""
        self.assertIsNone(self.facade.spawn("attacker", 0, 0, 100))
        self.facade.toggle_pause()
        self.facade.fire_selected()
        self.assertFalse(self.facade.assign_route_points(None, [{"x": 1}]))
        msgs = list(_drain(self.facade.msg_q))
        self.assertTrue(any(m[0] == "log" for m in msgs))


def _drain(q: queue.Queue):
    while True:
        try:
            yield q.get_nowait()
        except queue.Empty:
            return


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"),
                     "нужен X-дисплей (Xvfb) для интеграционного рендера")
class TestHeadlessRender(unittest.TestCase):
    def test_run_frames(self):
        from rwf.ui.app import run
        cfg = AppConfig()
        cfg.rcon.mock = True
        seen = {}

        def on_frame(n, state, facade):
            if n == 30:
                seen["frame_rev"] = state.get("frame_rev")
                seen["connected"] = state["connection"]["connected"]
                self._probe_v15(state, seen)

        rc = run(cfg, headless_frames=40, autoconnect=True, on_frame=on_frame)
        self.assertEqual(rc, 0)
        self.assertTrue(seen.get("connected"))
        self._assert_v15(seen)

    def _probe_v15(self, state, seen):
        """Раскладка v15: без win_top, панель объекта слева, тумблер режима.

        Вызывается ВНУТРИ единственного run() за процесс: DPG-контекст
        нельзя создавать дважды в одном процессе (ABI-ограничение).
        """
        import dearpygui.dearpygui as dpg
        seen["tags"] = {t: dpg.does_item_exist(t) for t in (
            "win_tools", "win_tools_tab", "win_obj", "win_obj_tab", "win_ctx",
            "units_table", "bases_table", "tel_params", "tel_mounts",
            "cmb_console_obj", "tel_pic", "chk_sound", "sld_volume",
            "btn_fire", "btn_duty_park", "btn_duty_combat", "cmb_ai_target",
            "txt_bar_fuel", "txt_bar_hull", "chk_follow")}
        # win_top удалён по требованию заказчика («пустая полоса сверху»)
        seen["no_topbar"] = not dpg.does_item_exist("win_top")
        seen["geom"] = {t: dpg.get_item_rect_size(t) for t in
                        ("win_obj", "win_map", "win_right")}
        seen["pos"] = {t: dpg.get_item_pos(t) for t in
                       ("win_obj", "win_map", "win_right")}
        ctx = state.get("ctx")
        seen["ctx_none"] = ctx.open
        ctx.open_at(200, 200, [("Пункт 1", None), ("-", None),
                               ("Пункт 2", None)])
        seen["ctx_open"] = ctx.open
        seen["empty_items"] = [i[0] for i in
                               ctx.items_for(None, None, None, 10.0, 10.0)]
        seen["unit_items"] = [i[0] for i in
                              ctx.items_for(7, None, None, 1.0, 2.0)]
        ctx.close()
        seen["ctx_closed"] = not ctx.open

    def _assert_v15(self, seen):
        self.assertTrue(all(seen["tags"].values()), seen["tags"])
        self.assertTrue(seen["no_topbar"], "верхняя полоса win_top не удалена")
        obj, mp, right = (seen["geom"]["win_obj"], seen["geom"]["win_map"],
                          seen["geom"]["win_right"])
        self.assertLess(obj[0], 340, "панель объекта не компактнее 340 px")
        self.assertGreater(mp[0], 400, "карта потеряла место")
        self.assertAlmostEqual(obj[1], mp[1], delta=2,
                               msg="панель объекта и карта разной высоты")
        self.assertAlmostEqual(obj[1], right[1], delta=2)
        self.assertLess(seen["pos"]["win_obj"][0], seen["pos"]["win_map"][0],
                        "панель объекта не слева от карты")
        # ручного ТО в меню юнита больше нет (авто-сервис на базе)
        self.assertNotIn("Заправить", seen["unit_items"])
        self.assertNotIn("Снарядить", seen["unit_items"])
        self.assertTrue(any("Стоянка" in i for i in seen["unit_items"]))
        self.assertFalse(seen["ctx_none"])
        self.assertTrue(seen["ctx_open"])
        self.assertTrue(seen["ctx_closed"])
        self.assertIn("Точка маршрута сюда", seen["empty_items"])
        self.assertIn("Зона удара отсюда", seen["empty_items"])
        self.assertIn("Снять с карты", seen["unit_items"])

if __name__ == "__main__":
    unittest.main()
