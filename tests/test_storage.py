"""Тесты хранилища маршрутов: JSON, безопасность имён, импорт/экспорт."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from rwf.routes import Action, Route, Waypoint
from rwf.storage import (FORMAT_ID, RouteLibrary, build_demo_routes,
                            sanitize_name)


class TestSanitize(unittest.TestCase):
    def test_path_traversal_blocked(self):
        """Имя маршрута не должно позволять выйти за пределы каталога."""
        for evil in ("../../etc/passwd", "..\\..\\windows\\system32",
                     "/etc/shadow", "a/b/c", "....//....//x"):
            name = sanitize_name(evil)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertNotIn("..", name)
            self.assertTrue(name)

    def test_empty_and_dots_become_default(self):
        self.assertEqual(sanitize_name(""), "route")
        self.assertEqual(sanitize_name("   "), "route")
        self.assertEqual(sanitize_name("..."), "route")

    def test_cyrillic_and_spaces_preserved(self):
        self.assertEqual(sanitize_name("Удар по базе"), "Удар по базе")

    def test_length_limited(self):
        self.assertLessEqual(len(sanitize_name("x" * 500)), 64)

    def test_weird_symbols_replaced(self):
        self.assertEqual(sanitize_name('a<b>c:d"e|f?g*h'), "a_b_c_d_e_f_g_h")


class _LibraryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rwf_routes_"))
        self.lib = RouteLibrary(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_route(self, name="Тестовый", loop=False):
        return Route([
            Waypoint(10.0, 20.0, 150.0, Action.NAVIGATE, note="рубеж"),
            Waypoint(30.0, 40.0, 90.0, Action.BOMB, count=2, duration=4.0,
                     target_name="Victim", target_altitude=64.0),
            Waypoint(50.0, 60.0, 200.0, Action.RTB),
        ], loop=loop, name=name, owner_kind="player")


class TestSaveLoad(_LibraryCase):
    def test_directory_is_created(self):
        nested = self.tmp / "a" / "b" / "c"
        RouteLibrary(nested)
        self.assertTrue(nested.is_dir())

    def test_round_trip(self):
        route = self.make_route("Удар по базе", loop=True)
        path = self.lib.save(route)
        self.assertTrue(path.exists())
        self.assertEqual(path.suffix, ".json")

        loaded = self.lib.load("Удар по базе")
        self.assertEqual(loaded.name, "Удар по базе")
        self.assertTrue(loaded.loop)
        self.assertEqual(len(loaded), 3)
        wp = loaded.waypoints[1]
        self.assertEqual((wp.x, wp.z, wp.altitude, wp.action, wp.count,
                          wp.duration, wp.target_name, wp.target_altitude),
                         (30.0, 40.0, 90.0, Action.BOMB, 2, 4.0, "Victim", 64.0))

    def test_file_is_readable_json(self):
        self.lib.save(self.make_route("Читаемый"))
        data = json.loads(self.lib.path_for("Читаемый").read_text(encoding="utf-8"))
        self.assertEqual(data["format"], FORMAT_ID)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["name"], "Читаемый")
        self.assertEqual(len(data["waypoints"]), 3)

    def test_runtime_state_is_not_saved(self):
        route = self.make_route("Состояние")
        route.waypoints[0].reached = True
        route.waypoints[0].action_done = True
        route.waypoints[0].skipped = True
        route.current_idx = 2
        route.done = True
        self.lib.save(route)
        loaded = self.lib.load("Состояние")
        self.assertEqual(loaded.current_idx, 0)
        self.assertFalse(loaded.done)
        for wp in loaded.waypoints:
            self.assertFalse(wp.reached)
            self.assertFalse(wp.action_done)
            self.assertFalse(wp.skipped)

    def test_load_by_path_and_by_name(self):
        path = self.lib.save(self.make_route("По пути"))
        self.assertEqual(len(self.lib.load(str(path))), 3)
        self.assertEqual(len(self.lib.load("По пути")), 3)

    def test_load_missing_lists_available(self):
        self.lib.save(self.make_route("Есть"))
        with self.assertRaises(FileNotFoundError) as ctx:
            self.lib.load("Нет")
        self.assertIn("Есть", str(ctx.exception))

    def test_load_corrupted_file(self):
        self.lib.save(self.make_route("Битый"))
        self.lib.path_for("Битый").write_text("{ не json", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            self.lib.load("Битый")
        self.assertIn("повреждён", str(ctx.exception))

    def test_load_foreign_format(self):
        path = self.lib.dir / "чужой.json"
        path.write_text(json.dumps({"format": "other.tool", "waypoints": []}),
                        encoding="utf-8")
        with self.assertRaises(ValueError):
            self.lib.load("чужой")

    def test_overwrite(self):
        self.lib.save(self.make_route("Один"))
        short = Route([Waypoint(0, 0)], name="Один")
        self.lib.save(short)
        self.assertEqual(len(self.lib.load("Один")), 1)
        self.assertEqual(self.lib.names(), ["Один"])

    def test_explicit_name_overrides(self):
        route = self.make_route("Внутреннее")
        self.lib.save(route, name="Внешнее")
        self.assertIn("Внешнее", self.lib.names())
        self.assertEqual(self.lib.load("Внешнее").name, "Внешнее")


class TestListing(_LibraryCase):
    def test_names_sorted(self):
        for name in ("bravo", "alpha", "charlie"):
            self.lib.save(self.make_route(name))
        self.assertEqual(self.lib.names(), ["alpha", "bravo", "charlie"])
        self.assertEqual(self.lib.names(), sorted(self.lib.names()))

    def test_info_contents(self):
        self.lib.save(self.make_route("Инфо", loop=True))
        info = self.lib.info("Инфо")
        self.assertEqual(info.name, "Инфо")
        self.assertEqual(info.waypoints, 3)
        self.assertTrue(info.loop)
        self.assertEqual(info.owner_kind, "player")
        self.assertGreater(info.size, 0)
        self.assertEqual(info.actions[Action.BOMB], 1)
        self.assertEqual(info.actions[Action.NAVIGATE], 1)
        self.assertEqual(info.actions[Action.RTB], 1)

    def test_list_skips_broken_files(self):
        self.lib.save(self.make_route("Хороший"))
        (self.lib.dir / "плохой.json").write_text("{{{", encoding="utf-8")
        infos = self.lib.list()
        self.assertEqual([i.name for i in infos], ["Хороший"])

    def test_info_missing_returns_none(self):
        self.assertIsNone(self.lib.info("нету"))

    def test_delete(self):
        self.lib.save(self.make_route("Удалить"))
        self.assertTrue(self.lib.delete("Удалить"))
        self.assertFalse(self.lib.exists("Удалить"))
        self.assertFalse(self.lib.delete("Удалить"))


class TestExportImport(_LibraryCase):
    def test_export_import_round_trip(self):
        for name in ("Первый", "Второй"):
            self.lib.save(self.make_route(name))
        blob = self.lib.export_all()
        self.assertEqual(len(blob["routes"]), 2)
        self.assertEqual(blob["format"], "rwf.route-library")

        other_dir = Path(tempfile.mkdtemp(prefix="rwf_routes2_"))
        try:
            other = RouteLibrary(other_dir)
            n = other.import_all(blob)
            self.assertEqual(n, 2)
            self.assertEqual(set(other.names()), {"Первый", "Второй"})
            self.assertEqual(other.names(), sorted(other.names()),
                             "список маршрутов не отсортирован")
            self.assertEqual(len(other.load("Второй")), 3)
        finally:
            shutil.rmtree(other_dir, ignore_errors=True)

    def test_import_without_overwrite(self):
        self.lib.save(self.make_route("Существует"))
        blob = {"format": "rwf.route-library", "version": 1,
                "routes": [{"format": FORMAT_ID, "version": 1,
                            "name": "Существует", "waypoints": []}]}
        self.assertEqual(self.lib.import_all(blob), 0)
        self.assertEqual(len(self.lib.load("Существует")), 3)
        self.assertEqual(self.lib.import_all(blob, overwrite=True), 1)
        self.assertEqual(len(self.lib.load("Существует")), 0)

    def test_import_rejects_foreign_blob(self):
        with self.assertRaises(ValueError):
            self.lib.import_all({"format": "nope"})


class TestDemoRoutes(_LibraryCase):
    def test_demo_routes_are_valid_and_savable(self):
        demos = build_demo_routes()
        self.assertGreaterEqual(len(demos), 3)
        for name, route in demos.items():
            self.assertEqual(route.name, name)
            self.assertGreater(len(route), 1)
            for wp in route.waypoints:
                self.assertIn(wp.action, Action.ALL)
            path = self.lib.save(route)
            loaded = self.lib.load(name)
            self.assertEqual(len(loaded), len(route))
            self.assertTrue(path.exists())

    def test_demo_actions_cover_key_features(self):
        actions = {wp.action
                   for r in build_demo_routes().values() for wp in r.waypoints}
        for required in (Action.NAVIGATE, Action.BOMB, Action.STRAFE,
                         Action.MISSILE, Action.HOLD, Action.RECON):
            self.assertIn(required, actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
