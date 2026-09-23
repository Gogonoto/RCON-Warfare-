"""Тесты блочной модели: дифф, отсутствие следа, округление, повороты."""
from __future__ import annotations

import math
import unittest

from rwf.model import (BLUEPRINTS, BlockModel, Blueprint, Cell, get_blueprint,
                          project)


class TestBlueprints(unittest.TestCase):
    def test_all_blueprints_valid(self):
        self.assertGreaterEqual(len(BLUEPRINTS), 6)
        for name, bp in BLUEPRINTS.items():
            self.assertIsInstance(bp, Blueprint, name)
            self.assertGreater(len(bp), 5, f"{name}: слишком мало клеток")
            self.assertTrue(all(c.block.startswith("minecraft:") for c in bp.cells),
                            f"{name}: блок без пространства имён")

    def test_unknown_blueprint_reports_available(self):
        with self.assertRaises(KeyError) as ctx:
            get_blueprint("nope")
        self.assertIn("su25", str(ctx.exception))

    def test_empty_blueprint_rejected(self):
        with self.assertRaises(ValueError):
            Blueprint("empty", [])


class TestProjection(unittest.TestCase):
    def test_yaw_rotates_model(self):
        bp = get_blueprint("su25")
        a = project(bp, (0.0, 100.0, 0.0), yaw=0.0)
        b = project(bp, (0.0, 100.0, 0.0), yaw=90.0)
        xs_a = [k[0] for k in a]
        zs_b = [k[2] for k in b]
        # При yaw 0 длина вдоль Z, при yaw 90 — вдоль X
        self.assertGreater(max(xs_a) - min(xs_a), 1)
        self.assertGreater(max(zs_b) - min(zs_b), 1)

    def test_coordinates_are_integers(self):
        for name, bp in BLUEPRINTS.items():
            for yaw in (0, 37, 90, 180, 271):
                cells = project(bp, (12.34, 90.0, -56.78), yaw=yaw, pitch=-7.0)
                self.assertTrue(all(isinstance(v, int) for k in cells for v in k),
                                f"{name} yaw={yaw}: координаты не целые")

    def test_negative_coordinates_not_shifted(self):
        """Регресс UNIT-13: `int(-3.7)` = −3, модель «прыгала» на блок."""
        bp = get_blueprint("su25")
        pos_a = project(bp, (-100.0, 90.0, -100.0), yaw=0.0)
        pos_b = project(bp, (100.0, 90.0, 100.0), yaw=0.0)
        ca = (sum(k[0] for k in pos_a) / len(pos_a), sum(k[2] for k in pos_a) / len(pos_a))
        cb = (sum(k[0] for k in pos_b) / len(pos_b), sum(k[2] for k in pos_b) / len(pos_b))
        self.assertAlmostEqual(ca[0] - (-100.0), cb[0] - 100.0, delta=0.35)
        self.assertAlmostEqual(ca[1] - (-100.0), cb[1] - 100.0, delta=0.35)

    def test_pitch_moves_nose_up(self):
        bp = get_blueprint("su25")
        flat = project(bp, (0.0, 100.0, 0.0), yaw=0.0, pitch=0.0)
        up = project(bp, (0.0, 100.0, 0.0), yaw=0.0, pitch=-30.0)
        # pitch < 0 — нос вверх: передние клетки должны подняться
        nose_flat = max(k[1] for k in flat if k[2] > 0)
        nose_up = max(k[1] for k in up if k[2] > 0)
        self.assertGreaterEqual(nose_up, nose_flat)

    def test_roll_tilts_wings(self):
        bp = get_blueprint("su25")
        flat = project(bp, (0.0, 100.0, 0.0), yaw=0.0, roll=0.0)
        rolled = project(bp, (0.0, 100.0, 0.0), yaw=0.0, roll=35.0)
        ys_flat = sorted({k[1] for k in flat})
        ys_rolled = sorted({k[1] for k in rolled})
        self.assertGreaterEqual(len(ys_rolled), len(ys_flat))


class TestBlockModelDiff(unittest.TestCase):
    def setUp(self):
        self.model = BlockModel(get_blueprint("su25"))

    def test_first_sync_writes_everything(self):
        writes, clears = self.model.sync((0.0, 100.0, 0.0), yaw=0.0)
        self.assertEqual(len(clears), 0)
        self.assertEqual(len(writes), self.model.placed_count)
        self.assertGreater(len(writes), 10)

    def test_second_sync_is_free(self):
        """Идемпотентность: тот же кадр = ноль команд на сервер."""
        self.model.sync((0.0, 100.0, 0.0), yaw=0.0)
        writes, clears = self.model.sync((0.0, 100.0, 0.0), yaw=0.0)
        self.assertEqual(writes, [])
        self.assertEqual(clears, [])

    def test_moving_costs_less_than_full_rebuild(self):
        full = len(self.model.sync((0.0, 100.0, 0.0), yaw=0.0)[0])
        writes, clears = self.model.sync((1.0, 100.0, 0.0), yaw=0.0)
        self.assertLess(len(writes), full,
                        "перемещение на блок требует полной перерисовки")
        self.assertLess(len(writes) + len(clears), 2 * full)

    def test_no_trail_after_long_flight(self):
        """Регресс UNIT-11: после перелёта в мире не должно остаться блоков.

        Симулируем мир как словарь: применяем writes и clears в порядке выдачи.
        """
        world_blocks: dict = {}
        model = BlockModel(get_blueprint("tu95"))
        for i in range(60):
            writes, clears = model.sync((float(i), 120.0, 0.0), yaw=90.0)
            for (x, y, z) in clears:
                world_blocks.pop((x, y, z), None)
            for (x, y, z), block in writes:
                world_blocks[(x, y, z)] = block
        # В полёте в мире ровно модель и ничего лишнего
        self.assertEqual(len(world_blocks), model.placed_count)
        # Снимаем модель — мир должен стать абсолютно пустым
        for (x, y, z) in model.clear():
            world_blocks.pop((x, y, z), None)
        self.assertEqual(world_blocks, {},
                         f"остался след из {len(world_blocks)} блоков")

    def test_clear_returns_all_placed(self):
        self.model.sync((0.0, 100.0, 0.0), yaw=0.0)
        placed = self.model.placed_count
        blocks = self.model.clear()
        self.assertEqual(len(blocks), placed)
        self.assertEqual(self.model.placed_count, 0)
        self.assertEqual(self.model.clear(), [])

    def test_bounds_and_position(self):
        self.model.sync((10.0, 100.0, -20.0), yaw=0.0)
        self.assertEqual(self.model.position, (10.0, 100.0, -20.0))
        lo, hi = self.model.bounds
        self.assertTrue(all(a <= b for a, b in zip(lo, hi)))
        self.assertTrue(all(lo[i] <= v <= hi[i]
                            for i, v in enumerate((10, 100, -20))))

    def test_counters(self):
        self.model.sync((0.0, 100.0, 0.0), yaw=0.0)
        self.model.sync((5.0, 100.0, 0.0), yaw=0.0)
        self.assertGreater(self.model.total_writes, 0)
        self.assertGreater(self.model.total_clears, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
