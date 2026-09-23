"""Транспортный самолёт и наземная техника (TRANSPORT-01…, GROUND-01…)."""
from __future__ import annotations

import unittest

from rwf.bases import ACCEPTS, KIND_AIRPORT, KIND_CARRIER, KIND_GROUND
from rwf.icons import SILHOUETTES
from rwf.model import get_blueprint
from rwf.routes import ACTION_COLORS, ACTION_LABELS, Action
from rwf.units import VARIANTS, build_unit


class TestCatalog(unittest.TestCase):
    def test_variants_exist(self):
        for key, kind in (("transport", "transport"), ("truck", "truck"),
                          ("apc", "apc"), ("mbt", "tank")):
            self.assertIn(key, VARIANTS)
            self.assertEqual(VARIANTS[key].kind, kind)

    def test_blueprints_and_silhouettes(self):
        for key in ("transport", "truck", "apc"):
            u = build_unit(key, f"t_{key}")
            bp = get_blueprint(u.spec.blueprint)
            self.assertGreater(len(bp.cells), 10)
            self.assertIn(u.spec.kind, SILHOUETTES)

    def test_ground_specs(self):
        for key in ("truck", "apc", "mbt"):
            spec = VARIANTS[key].spec
            self.assertTrue(spec.ground_unit)
            self.assertGreater(spec.turn_rate, 0)
            self.assertGreater(spec.max_speed, 0)

    def test_cargo(self):
        self.assertGreater(VARIANTS["transport"].spec.cargo_max, 0)
        self.assertGreater(VARIANTS["truck"].spec.cargo_max, 0)
        u = build_unit("transport", "t")
        self.assertEqual(u.cargo, 0.0)
        self.assertIn("cargo", u.snapshot())
        self.assertIn("cargo_max", u.snapshot())

    def test_truck_unarmed(self):
        u = build_unit("truck", "t")
        self.assertEqual(len(u.mounts), 0)
        self.assertEqual(u.ammo_max, 0)

    def test_bases_accept(self):
        self.assertIn("transport", ACCEPTS[KIND_AIRPORT])
        self.assertIn("transport", ACCEPTS[KIND_CARRIER])
        self.assertIn("truck", ACCEPTS[KIND_GROUND])
        self.assertIn("apc", ACCEPTS[KIND_GROUND])

    def test_drop_action_registered(self):
        self.assertIn(Action.DROP, Action.ALL)
        self.assertIn(Action.DROP, ACTION_LABELS)
        self.assertIn(Action.DROP, ACTION_COLORS)


class TestGroundMovement(unittest.TestCase):
    def test_tank_step_without_terrain_keeps_y(self):
        from rwf.world import World
        w = World()
        u = build_unit("truck", "t")
        u.pos = (0.0, 70.0, 0.0)
        u.set_target(heading=0.0, throttle=1.0)
        for _ in range(20):
            u.step(0.1, w)
        self.assertGreater(u.speed, 1.0)
        self.assertAlmostEqual(u.pos[1], 70.0)     # рельефа нет — идём ровно
        self.assertGreater(u.pos[2], 0.0)          # курс 0 = +Z
        self.assertEqual(u.status, "moving")

    def test_apc_turns(self):
        from rwf.world import World
        w = World()
        u = build_unit("apc", "t")
        u.set_target(heading=90.0, throttle=1.0)
        for _ in range(40):
            u.step(0.1, w)
        self.assertAlmostEqual(u.yaw, 90.0, delta=5.0)


if __name__ == "__main__":
    unittest.main()
