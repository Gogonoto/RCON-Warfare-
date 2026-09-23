"""REAL-01/02: скорость и манёвренность не зависят от периода тика."""
from __future__ import annotations

import unittest

from rwf.units import build_unit
from rwf.world import World


def _fly(variant: str, total: float, dt: float) -> tuple:
    w = World()
    u = build_unit(variant, "t")
    u.pos = (0.0, 150.0, 0.0)
    u.set_target(heading=0.0, throttle=1.0)
    t = 0.0
    while t < total - 1e-9:
        step = min(dt, total - t)
        u.step(step, w)
        t += step
    return u.pos, u.speed, u.yaw


class TestTickIndependence(unittest.TestCase):
    def test_distance_independent_of_step(self):
        # REAL-02: путь за одно и то же время не зависит от дробления dt
        # (допуск — квант ускорения на изломе рампы тяги).
        fine = _fly("attacker", 10.0, 0.05)
        coarse = _fly("attacker", 10.0, 0.25)
        self.assertAlmostEqual(fine[0][2], coarse[0][2], delta=2.0)
        self.assertAlmostEqual(fine[1], coarse[1], delta=0.5)

    def test_turn_independent_of_step(self):
        fine = _fly("attack_heli", 4.0, 0.02)
        coarse = _fly("attack_heli", 4.0, 0.4)
        self.assertAlmostEqual(fine[2], coarse[2], delta=2.0)

    def test_set_tick_clamped(self):
        from rwf.engine import UnitEngine
        from rwf.config import AppConfig
        e = UnitEngine(World(), None, None, cfg=AppConfig())
        e.set_tick(0.001)
        self.assertGreaterEqual(e.tick_dt, 0.02)
        e.set_tick(99.0)
        self.assertLessEqual(e.tick_dt, 2.0)


if __name__ == "__main__":
    unittest.main()
