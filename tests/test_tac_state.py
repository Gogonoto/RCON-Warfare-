"""TAC-01 (п.17 ТЗ): черновики тактических маршрутов в STATE.

Тестируется чистый state-слой (без DPG и без движка): выбор юнитов,
добавление/перемещение/удаление точек, засечённые цели и очистка после
назначения маршрута движку.
"""
from __future__ import annotations

import unittest

from rwf.ui import state as st


class TestTacSelect(unittest.TestCase):
    def test_single_select(self):
        s = st.make_state()
        st.tac_select(s, 5)
        self.assertEqual(s["selected_units"], [5])
        self.assertEqual(s["selection"], 5)

    def test_single_select_replaces(self):
        s = st.make_state()
        st.tac_select(s, 5)
        st.tac_select(s, 7)
        self.assertEqual(s["selected_units"], [7])

    def test_additive_toggle(self):
        s = st.make_state()
        st.tac_select(s, 5)
        st.tac_select(s, 7, additive=True)
        st.tac_select(s, 9, additive=True)
        self.assertEqual(s["selected_units"], [5, 7, 9])
        # повторный аддитивный клик снимает выделение
        st.tac_select(s, 7, additive=True)
        self.assertEqual(s["selected_units"], [5, 9])

    def test_none_clears(self):
        s = st.make_state()
        st.tac_select(s, 5)
        st.tac_select(s, None)
        self.assertEqual(s["selected_units"], [])
        self.assertIsNone(s["selection"])


class TestTacRoutes(unittest.TestCase):
    def test_add_and_move_points(self):
        s = st.make_state()
        st.tac_add_point(s, 3, 100.0, 200.0)
        st.tac_add_point(s, 3, 150.0, 250.0)
        pts = s["unit_routes"][3]
        self.assertEqual(len(pts), 2)
        st.tac_move_point(s, 3, 0, 111.0, 222.0)
        self.assertEqual(pts[0], {"x": 111.0, "z": 222.0})

    def test_insert_after(self):
        s = st.make_state()
        for x in (0.0, 10.0, 20.0):
            st.tac_add_point(s, 1, x, 0.0)
        st.tac_add_point(s, 1, 5.0, 0.0, after=0)
        self.assertEqual([p["x"] for p in s["unit_routes"][1]],
                         [0.0, 5.0, 10.0, 20.0])

    def test_remove_last_pops_entry(self):
        s = st.make_state()
        st.tac_add_point(s, 2, 1.0, 2.0)
        st.tac_remove_point(s, 2, 0)
        self.assertNotIn(2, s["unit_routes"])

    def test_clear_route_points_removes_draft(self):
        """tac_apply полагается на эту функцию — пустых списков быть не должно."""
        s = st.make_state()
        st.tac_add_point(s, 4, 9.0, 9.0)
        st.tac_clear_route_points(s, 4)
        self.assertNotIn(4, s["unit_routes"])
        # повторный вызов безопасен
        st.tac_clear_route_points(s, 4)

    def test_clear_all(self):
        s = st.make_state()
        st.tac_add_point(s, 1, 0.0, 0.0)
        st.tac_add_point(s, 2, 0.0, 0.0)
        st.tac_clear_routes(s)
        self.assertEqual(s["unit_routes"], {})


class TestTacTargets(unittest.TestCase):
    def test_set_and_clear(self):
        s = st.make_state()
        st.tac_set_target(s, 6, {"kind": "unit", "name": "Танк", "x": 1.0,
                                 "z": 2.0})
        self.assertEqual(s["tac_targets"][6]["name"], "Танк")
        st.tac_set_target(s, 6, None)
        self.assertNotIn(6, s["tac_targets"])

    def test_target_stores_copy(self):
        s = st.make_state()
        src = {"kind": "base", "x": 0.0, "z": 0.0}
        st.tac_set_target(s, 8, src)
        src["x"] = 99.0
        self.assertEqual(s["tac_targets"][8]["x"], 0.0)


if __name__ == "__main__":
    unittest.main()
