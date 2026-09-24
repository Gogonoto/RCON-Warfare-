"""Тесты рендера карты: трансформация, растр, слои (без Qt)."""
from __future__ import annotations

import math
import unittest

from rwf.maprender import (BLOCK_PALETTE, GridLayer, HudLayer, MapRenderer,
                              MapTransform, MarkersLayer, PlayersLayer,
                              RouteLayer, TerrainRaster, UnitsLayer, ZonesLayer)
from rwf.ui import theme
from rwf.routes import Action, Route, Waypoint
from rwf.units import build_unit
from rwf.world import World


class TestTransform(unittest.TestCase):
    def setUp(self):
        self.tf = MapTransform(size=(800.0, 600.0), view_radius=400.0)

    def test_round_trip(self):
        for wx, wz in ((0.0, 0.0), (123.5, -77.25), (-500.0, 900.0)):
            sx, sy = self.tf.to_screen(wx, wz)
            bx, bz = self.tf.to_world(sx, sy)
            self.assertAlmostEqual(bx, wx, delta=1e-6)
            self.assertAlmostEqual(bz, wz, delta=1e-6)

    def test_zoom_at_keeps_point(self):
        self.tf.set_center(50.0, -30.0, keep_follow=False)
        sx, sy = 620.0, 180.0
        before = self.tf.to_world(sx, sy)
        self.tf.zoom_at(0.5, sx, sy)
        after = self.tf.to_world(sx, sy)
        self.assertAlmostEqual(before[0], after[0], delta=0.5)
        self.assertAlmostEqual(before[1], after[1], delta=0.5)

    def test_radius_clamped(self):
        self.tf.set_view_radius(1e9)
        self.assertLessEqual(self.tf.view_radius, self.tf.max_radius)
        self.tf.set_view_radius(0.0)
        self.assertGreaterEqual(self.tf.view_radius, self.tf.min_radius)

    def test_pan_disables_follow(self):
        self.tf.follow = True
        self.tf.pan(10.0, 10.0)
        self.assertFalse(self.tf.follow)
        # set_center(keep_follow=True) НЕ включает слежение обратно —
        # оно только не сбрасывает его. Включается явно.
        self.tf.set_center(5.0, 5.0, keep_follow=True)
        self.assertFalse(self.tf.follow)
        self.tf.follow = True
        self.tf.pan(1.0, 1.0)
        self.assertFalse(self.tf.follow)

    def test_resize_changes_scale(self):
        s0 = self.tf.scale                       # min(800,600)/800 = 0.75
        self.tf.resize(800.0, 1200.0)            # min теперь 800
        self.assertNotEqual(self.tf.scale, s0)
        self.assertAlmostEqual(self.tf.scale, 800.0 / (2 * self.tf.view_radius))

    def test_visible_rect(self):
        self.tf.set_center(0.0, 0.0, keep_follow=False)
        x0, z0, x1, z1 = self.tf.visible_world_rect()
        self.assertLess(x0, x1)
        self.assertLess(z0, z1)
        self.assertAlmostEqual(x1 - x0, 2 * self.tf.view_radius * (800 / 600), delta=1)


class _WorldCase(unittest.TestCase):
    def build_world(self):
        w = World()
        u = build_unit("attacker", "jet", pos=(10.0, 150.0, -20.0), speed=30.0)
        w.add_unit(u)
        b = build_unit("fighter", "bot", pos=(-40.0, 250.0, 60.0), speed=50.0,
                       is_bot=True)
        w.add_unit(b)
        w.set_route(u.id, Route([Waypoint(50, 50, 150, Action.BOMB),
                                 Waypoint(120, 90, 180)], unit_id=u.id))
        w.set_route(b.id, Route([Waypoint(0, 0, 250, Action.MISSILE)],
                                unit_id=b.id, owner_kind="bot"))
        w.set_player("Arlik88", (30.0, 64.0, 10.0))
        w.set_strike_zone(-50, -50, 60, 60)
        w.set_base(-200.0, -200.0)
        w.set_launch_point(-200.0, -200.0)
        w.add_marker(20, 20, "impact")
        w.add_marker(-10, 30, "crash")
        return w


class TestLayers(_WorldCase):
    def setUp(self):
        self.world = self.build_world()
        self.snap = self.world.snapshot()
        self.tf = MapTransform(size=(900.0, 900.0), view_radius=300.0)

    def test_route_layer_colors_by_action_and_bot(self):
        prims = RouteLayer().render(self.snap, self.tf)
        lines = [p for p in prims if p["type"] == "line"]
        self.assertTrue(lines)
        dashed = [p for p in lines if p.get("dash")]
        self.assertTrue(dashed, "маршрут бота должен быть пунктиром")
        bot = [p for p in dashed if not p.get("dash_offset")]
        self.assertTrue(bot, "у бота пунктир без анимации")
        for p in bot:
            self.assertEqual(p["color"], "#22e6ff", "маршрут бота рисуется cyan")
        # активный участок маршрута оператора — бегущий пунктир своего цвета
        for p in dashed:
            if p.get("dash_offset"):
                self.assertNotEqual(p["color"], "#22e6ff")

    def test_units_layer_draws_silhouettes(self):
        """Каждый юнит даёт силуэт из нескольких полигонов, а не стрелку."""
        prims = UnitsLayer().render(self.snap, self.tf)
        polys = [p for p in prims if p["type"] == "poly"]
        # 2 юнита: самолёт (3 полигона корпуса) + истребитель (3)
        self.assertGreaterEqual(len(polys), 6)
        for poly in polys:
            self.assertGreaterEqual(len(poly["points"]), 3)

    def test_silhouettes_are_mirror_symmetric(self):
        """Вид сверху симметричен относительно продольной оси (yaw=0)."""
        from rwf.icons import SILHOUETTES, lod_for, unit_icon
        lod = lod_for(0.5)
        for kind in ("aircraft", "helicopter", "drone", "tank"):
            prims = unit_icon(kind, 200.0, 200.0, 0.0, lod, "#fff", "#888")
            xs = [pt[0] for p in prims if p["type"] == "poly" for pt in p["points"]]
            if not xs:
                continue
            # множество X-координат симметрично относительно центра 200
            mirrored = {round(400.0 - x, 3) for x in xs}
            self.assertEqual({round(x, 3) for x in xs}, mirrored,
                             f"{kind}: силуэт несимметричен")

    def test_helicopter_has_rotor(self):
        """Вертолёт читается как вертолёт: есть ометаемый диск винта."""
        from rwf.icons import lod_for, unit_icon
        # Крупный план (mpp=0.3) — видны и диск, и лопасти
        prims = unit_icon("helicopter", 100.0, 100.0, 0.0, lod_for(0.3), "#fff", "#888")
        discs = [p for p in prims if p["type"] == "circle" and p.get("fill") is None]
        self.assertTrue(discs, "у вертолёта нет диска несущего винта")
        blades = [p for p in prims if p["type"] == "line"]
        self.assertGreaterEqual(len(blades), 4, "нет лопастей несущего винта")
        # Средний план: диск остаётся, лопасти заменяет линия-размах
        mid = unit_icon("helicopter", 100.0, 100.0, 0.0, lod_for(1.0), "#fff", "#888")
        self.assertTrue([p for p in mid if p["type"] == "circle"
                         and p.get("fill") is None])

    def test_lod_reduces_weight_when_far(self):
        from rwf.icons import lod_for
        near, far = lod_for(0.4), lod_for(8.0)
        self.assertGreater(near.alpha, far.alpha)
        self.assertTrue(near.show_label)
        self.assertFalse(far.show_label)
        self.assertFalse(far.show_blades)
        # иконка не исчезает в нечитаемую точку
        self.assertGreaterEqual(far.size_px, 6.0)

    def test_zones_layer(self):
        prims = ZonesLayer().render(self.snap, self.tf)
        kinds = {p["type"] for p in prims}
        self.assertIn("rect", kinds)
        texts = [p.get("text", "") for p in prims if p["type"] == "text"]
        self.assertTrue(any("ЗОНА" in t for t in texts))
        self.assertTrue(any("БАЗА" in t for t in texts))

    def test_grid_layer_skips_when_too_dense(self):
        tf = MapTransform(size=(900.0, 900.0), view_radius=30000.0)
        self.assertEqual(GridLayer().render(self.snap, tf), [])

    def test_markers_and_players(self):
        m = MarkersLayer().render(self.snap, self.tf)
        self.assertTrue(any(p["type"] == "line" for p in m))
        p = PlayersLayer().render(self.snap, self.tf)
        self.assertTrue(any(x["type"] == "circle" for x in p))
        self.assertTrue(any(x.get("text") == "Arlik88" for x in p))

    def test_hud_layer_has_scale_and_compass(self):
        prims = HudLayer().render(self.snap, self.tf)
        texts = [p.get("text", "") for p in prims if p["type"] == "text"]
        self.assertTrue(any("м" in t for t in texts))
        self.assertTrue(any(t in ("С", "В", "Ю", "З") for t in texts))


class TestRaster(_WorldCase):
    def make_terrain(self, x0=-64, x1=64, step=8):
        w = World()
        tiles = [(x, z, 64 + ((x + z) % 9), "grass_block" if (x + z) % 3 else "water")
                 for x in range(x0, x1 + 1, step) for z in range(x0, x1 + 1, step)]
        w.terrain.step = step
        w.terrain.set_tiles(tiles)
        return w

    def test_raster_matches_tile_grid(self):
        w = self.make_terrain()
        r = TerrainRaster()
        out = r.render(w.terrain)
        self.assertIsNotNone(out)
        buf, width, height, world = out
        self.assertEqual(len(buf), width * height * 3)
        self.assertEqual(width, height)
        self.assertAlmostEqual(world[0], -68.0)     # половина шага наружу
        self.assertAlmostEqual(world[2], 68.0)

    def test_raster_cached_by_descriptor_only(self):
        w = self.make_terrain()
        r = TerrainRaster()
        tf = MapTransform(size=(600.0, 600.0), view_radius=200.0)
        first = r.render(w.terrain)
        tf.pan(50.0, 50.0)                      # камера не влияет на кэш
        second = r.render(w.terrain)
        self.assertIs(first, second)
        w.terrain.set_tiles([(200, 200, 70, "stone")])   # рельеф изменился
        third = r.render(w.terrain)
        self.assertIsNot(third, second)

    def test_empty_terrain_is_none(self):
        self.assertIsNone(TerrainRaster().render(World().terrain))

    def test_palette_covers_kinds(self):
        for kind in ("water", "grass", "sand", "snow", "stone", "other"):
            self.assertIn(kind, BLOCK_PALETTE)
            self.assertEqual(len(BLOCK_PALETTE[kind]), 3)

    def test_renderer_emits_prims_and_image(self):
        w = self.make_terrain()
        renderer = MapRenderer(size=(600.0, 600.0), view_radius=200.0)
        snap = self.build_world().snapshot()
        prims = renderer.render(snap, follow_uid=None)
        self.assertTrue(prims)
        img = renderer.terrain_image(w.terrain)
        self.assertIsNotNone(img)
        renderer.set_layer_enabled("grid", False)
        self.assertFalse(renderer.layer("grid").enabled)


class TestTransformInertia(unittest.TestCase):
    """UX-02: экспоненциальная инерция камеры MapTransform."""

    def setUp(self):
        self.tf = MapTransform(size=(800.0, 600.0), view_radius=400.0)

    def test_set_center_deferred_until_tick(self):
        self.tf.set_center(1000.0, 500.0)
        self.assertEqual(self.tf.center, (0.0, 0.0))   # ещё не доехала
        self.assertTrue(self.tf.moving)

    def test_tick_converges_monotonically(self):
        self.tf.set_center(1000.0, 500.0)
        prev_dist = math.inf
        for _ in range(60):
            changed = self.tf.tick(1 / 60)
            d = abs(self.tf.center[0] - 1000.0)
            self.assertLessEqual(d, prev_dist + 1e-9)  # только ближе
            prev_dist = d
        self.assertFalse(self.tf.moving)
        self.assertAlmostEqual(self.tf.center[0], 1000.0, places=1)
        self.assertTrue(changed or d < 0.1)

    def test_frame_rate_independence(self):
        """30 кадров по 1/30 и 120 кадров по 1/120 — примерно один путь."""
        a = MapTransform(size=(800.0, 600.0), view_radius=400.0)
        b = MapTransform(size=(800.0, 600.0), view_radius=400.0)
        a.set_center(1000.0, 0.0)
        b.set_center(1000.0, 0.0)
        for _ in range(30):
            a.tick(1 / 30)
        for _ in range(60):
            b.tick(1 / 60)   # те же 1 секунда времени
        self.assertAlmostEqual(a.center[0], b.center[0], delta=5.0)

    def test_snap_jumps_to_target(self):
        self.tf.set_center(300.0, -200.0)
        self.tf.snap()
        self.assertEqual(self.tf.center, (300.0, -200.0))
        self.assertFalse(self.tf.moving)

    def test_zoom_smooth_and_clamped(self):
        self.tf.zoom_by(0.5)
        self.assertNotEqual(self.tf.view_radius, self.tf._target_radius)
        for _ in range(120):
            self.tf.tick(1 / 60)
        self.assertAlmostEqual(self.tf.view_radius,
                               self.tf._target_radius, places=1)
        self.tf.set_view_radius(1.0)          # ниже min_radius
        self.tf.snap()
        self.assertGreaterEqual(self.tf.view_radius, self.tf.min_radius)

    def test_inertia_off_is_instant(self):
        self.tf.inertia = False
        self.tf.set_center(700.0, 700.0)
        self.assertEqual(self.tf.center, (700.0, 700.0))
        self.assertFalse(self.tf.tick(1 / 60))    # дёргать нечего

    def test_pan_disables_follow_with_inertia(self):
        self.tf.pan_screen(50.0, 0.0)
        self.assertFalse(self.tf.follow)
        self.tf.tick(1 / 60)
        self.assertGreater(self.tf.center[0], 0.0)


class TestUiScaleTheme(unittest.TestCase):
    """UX-03: глобальный множитель масштаба UI (headless — без DPG-контекста)."""

    def setUp(self):
        self._saved = theme.ui_scale()

    def tearDown(self):
        theme.set_ui_scale(self._saved)

    def test_clamp_bounds(self):
        self.assertAlmostEqual(theme.set_ui_scale(99.0), theme.UI_SCALE_MAX)
        self.assertAlmostEqual(theme.set_ui_scale(0.01), theme.UI_SCALE_MIN)

    def test_scale_step_multiplicative(self):
        theme.set_ui_scale(1.0)
        up = theme.scale_step(+1)
        self.assertGreater(up, 1.0)
        back = theme.scale_step(-1)
        self.assertAlmostEqual(back, 1.0, places=3)

    def test_scale_step_persists_to_settings(self):
        from rwf.settings import Settings
        s = Settings(data={"ui": {"scale": 1.0}})
        val = theme.scale_step(+1, persist=s)
        self.assertAlmostEqual(s.get("ui.scale"), round(val, 3))

    def test_scale_step_survives_bad_settings_object(self):
        class Boom:
            def set(self, *a):
                raise RuntimeError("нет файла")
        theme.set_ui_scale(1.0)
        self.assertGreater(theme.scale_step(+1, persist=Boom()), 1.0)

    def test_noop_when_same_value(self):
        theme.set_ui_scale(1.2)
        self.assertAlmostEqual(theme.set_ui_scale(1.2), 1.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
