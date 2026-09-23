"""Тесты физики и характеристик техники."""
from __future__ import annotations

import math
import unittest

from rwf.units import (Aircraft, Drone, Helicopter, Tank, Unit, UnitSpec,
                          VARIANTS, build_unit, variant_keys)
from rwf.world import World


def run(unit, seconds: float, dt: float, world: World | None = None) -> Unit:
    """Прокрутить физику на `seconds` секунд с шагом `dt`."""
    w = world or World()
    n = int(round(seconds / dt))
    for _ in range(n):
        unit.step(dt, w)
        if not unit.alive:
            break
    return unit


class TestConstruction(unittest.TestCase):
    def test_all_variants_build(self):
        self.assertEqual(len(VARIANTS), len(variant_keys()))
        for key in VARIANTS:
            unit = build_unit(key, f"t_{key}")
            self.assertIsNone(unit.id, "id присваивает World, а не конструктор")
            self.assertTrue(unit.alive)
            # снабженческие машины могут быть безоружными — но тогда груз
            if unit.mounts:
                self.assertGreater(unit.ammo_max, 0, key)
            else:
                self.assertGreater(unit.spec.cargo_max, 0, key)
            self.assertGreater(unit.fuel, 0, key)
            self.assertTrue(unit.model.blueprint.cells)

    def test_mounts_match_loadout(self):
        for key, variant in VARIANTS.items():
            unit = build_unit(key, f"t_{key}")
            self.assertEqual(len(unit.mounts), len(variant.loadout), key)
            for mount, (cat, slot, wkey) in zip(unit.mounts, variant.loadout):
                self.assertEqual(mount.category, cat)
                self.assertEqual(mount.slot, slot)
                self.assertEqual(mount.key, wkey)
                if wkey:
                    self.assertEqual(mount.ammo, mount.ammo_max)

    def test_available_weapons_fit_mount_categories(self):
        """Регресс UNIT-18: в AVAILABLE не должно быть категорий без подвесов."""
        for key, variant in VARIANTS.items():
            cats = {c for c, _s, _k in variant.loadout}
            for cat in variant.available:
                self.assertIn(cat, cats,
                              f"{key}: категория {cat} в AVAILABLE без подвеса")

    def test_zero_speed_is_respected(self):
        """Регресс UNIT-16: `if speed:` проглатывал нулевую скорость."""
        unit = build_unit("mbt", "tank", speed=0)
        self.assertEqual(unit.speed, 0.0)
        unit2 = build_unit("attack_heli", "heli", speed=0.0)
        self.assertEqual(unit2.speed, 0.0)

    def test_specs_are_independent_copies(self):
        """Регресс UNIT-15: общий изменяемый UnitSpec на все экземпляры.

        Spec заморожен (frozen dataclass) — это сильнее, чем копия: испортить
        характеристики одного юнита в принципе нельзя, а настройка делается
        через `copy()`, который возвращает новый объект.
        """
        a = build_unit("attacker", "a")
        b = build_unit("attacker", "b")
        self.assertIsNot(a.spec, b.spec)
        self.assertIsNot(a.spec, VARIANTS["attacker"].spec)
        with self.assertRaises(Exception):
            a.spec.max_speed = 1.0            # type: ignore[misc]
        tuned = a.spec.copy(max_speed=1.0)
        self.assertEqual(tuned.max_speed, 1.0)
        self.assertEqual(a.spec.max_speed, 42.0)
        self.assertEqual(b.spec.max_speed, 42.0)

    def test_common_interface_on_every_type(self):
        """Регресс UNIT-17: общий код не должен проверять поля через hasattr."""
        required = ("fuel", "g_load", "stalled", "speed", "status", "alive",
                    "pitch", "roll", "yaw", "pos", "mounts", "model")
        for key in VARIANTS:
            unit = build_unit(key, f"t_{key}")
            for attr in required:
                self.assertTrue(hasattr(unit, attr), f"{key}: нет поля {attr}")
            snap = unit.snapshot()
            for field in ("fuel_pct", "g_load", "stalled", "ammo", "status",
                          "controls", "mounts", "speed"):
                self.assertIn(field, snap, f"{key}: в snapshot нет {field}")


class TestControlSemantics(unittest.TestCase):
    """Регресс UNIT-01: управление — абсолютные цели, а не дельты."""

    def setUp(self):
        self.world = World()
        self.unit = build_unit("attacker", "jet", pos=(0.0, 150.0, 0.0), speed=30.0)

    def test_pitch_target_is_held_not_accumulated(self):
        self.unit.set_target(pitch=-10.0)
        run(self.unit, 3.0, 0.1, self.world)
        self.assertAlmostEqual(self.unit.pitch, -10.0, delta=0.6)
        run(self.unit, 3.0, 0.1, self.world)
        self.assertAlmostEqual(self.unit.pitch, -10.0, delta=0.6,
                               msg="тангаж накопился вместо удержания цели")

    def test_roll_does_not_decay(self):
        """Регресс UNIT-02: в наброске крен гас `roll *= 0.75` каждый тик."""
        self.unit.set_target(roll=40.0)
        run(self.unit, 2.0, 0.1, self.world)
        self.assertAlmostEqual(self.unit.roll, 40.0, delta=1.0)
        run(self.unit, 2.0, 0.1, self.world)
        self.assertGreater(self.unit.roll, 35.0, "крен самопроизвольно затухает")

    def test_trim_shifts_target(self):
        self.unit.set_target(pitch=0.0)
        run(self.unit, 1.0, 0.1, self.world)
        self.unit.trim(dpitch=-5.0)
        run(self.unit, 2.0, 0.1, self.world)
        self.assertAlmostEqual(self.unit.pitch, -5.0, delta=0.6)

    def test_trim_cancels_heading_autopilot(self):
        self.unit.set_target(heading=90.0)
        self.unit.trim(droll=10.0)
        self.assertIsNone(self.unit.controls.heading)

    def test_targets_are_clamped(self):
        self.unit.set_target(pitch=-500.0, roll=900.0, throttle=9.0)
        run(self.unit, 4.0, 0.1, self.world)
        self.assertGreaterEqual(self.unit.pitch, -self.unit.spec.max_pitch - 0.1)
        self.assertLessEqual(self.unit.controls.throttle, 1.0)

    def test_level_straightens(self):
        self.unit.set_target(roll=30.0, pitch=-20.0)
        run(self.unit, 1.5, 0.1, self.world)
        self.unit.level()
        run(self.unit, 3.0, 0.1, self.world)
        self.assertAlmostEqual(self.unit.roll, 0.0, delta=0.5)
        self.assertAlmostEqual(self.unit.pitch, 0.0, delta=0.5)


class TestAircraftPhysics(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def _jet(self, **kw):
        base = dict(pos=(0.0, 200.0, 0.0), speed=30.0, throttle=0.8)
        base.update(kw)
        return build_unit("attacker", "jet", **base)

    def test_moves_forward_along_heading(self):
        unit = self._jet()
        unit.set_target(heading=0.0, roll=0.0, pitch=0.0)
        run(unit, 2.0, 0.1, self.world)
        self.assertGreater(unit.pos[2], 20.0, "yaw=0 — это юг (+Z)")
        self.assertAlmostEqual(unit.pos[0], 0.0, delta=1.0)

    def test_bank_turns_and_loads_g(self):
        unit = self._jet()
        unit.set_target(roll=45.0, pitch=0.0)
        yaw0 = unit.yaw
        run(unit, 2.0, 0.1, self.world)
        self.assertNotAlmostEqual(unit.yaw, yaw0, delta=1.0)
        self.assertGreater(unit.g_load, 1.3, "крен 45° должен давать ~1.41g")
        self.assertAlmostEqual(unit.g_load, 1.0 / math.cos(math.radians(45)), delta=0.2)

    def test_g_limit_caps_bank(self):
        """Регресс UNIT-10: перегрузка ограничена паспортом планера."""
        unit = build_unit("bomber", "b", pos=(0.0, 250.0, 0.0), speed=25.0)
        unit.set_target(roll=89.0)
        run(unit, 4.0, 0.1, self.world)
        max_bank = math.degrees(math.acos(1.0 / unit.spec.max_g))
        self.assertLessEqual(unit.roll, max_bank + 0.5)
        self.assertLessEqual(unit.g_load, unit.spec.max_g + 0.01)

    def test_heading_autopilot_converges(self):
        unit = self._jet()
        unit.set_target(heading=270.0)
        run(unit, 12.0, 0.1, self.world)
        err = abs((unit.yaw - 270.0 + 540.0) % 360.0 - 180.0)
        self.assertLess(err, 12.0, f"автопилот не вышел на курс: {unit.yaw}")

    def test_heading_autopilot_takes_shorter_way(self):
        unit = self._jet()
        unit.yaw = 10.0
        unit.set_target(heading=350.0)          # короче через 0, а не через 180
        run(unit, 3.0, 0.1, self.world)
        self.assertTrue(unit.yaw > 300.0 or unit.yaw < 10.0,
                        f"разворот пошёл длинной дорогой: {unit.yaw}")

    def test_stall_loses_altitude_and_recovers(self):
        """Регресс UNIT-09: сваливание с потерей управления и выходом."""
        unit = build_unit("attacker", "jet", pos=(0.0, 250.0, 0.0),
                          speed=6.0, throttle=0.0)
        unit.spec = unit.spec.copy(stall_speed=12.0)
        run(unit, 2.0, 0.1, self.world)
        self.assertTrue(unit.stalled)
        self.assertEqual(unit.status, "stall")
        self.assertLess(unit.pos[1], 249.0, "в сваливании высота должна падать")
        # Вывод: полный газ
        unit.set_target(throttle=1.0, pitch=0.0)
        run(unit, 6.0, 0.1, self.world)
        self.assertFalse(unit.stalled, "самолёт не вышел из сваливания")

    def test_climb_and_descend(self):
        up = self._jet()
        up.set_target(pitch=-20.0, heading=None)
        run(up, 3.0, 0.1, self.world)
        down = self._jet()
        down.set_target(pitch=20.0)
        run(down, 3.0, 0.1, self.world)
        self.assertGreater(up.pos[1], 200.0)
        self.assertLess(down.pos[1], 200.0)

    def test_fuel_burns_and_cuts_throttle(self):
        """Регресс UNIT-08: расход зависит от тяги, на нуле тяга снимается."""
        unit = build_unit("attacker", "jet", pos=(0.0, 250.0, 0.0), speed=30.0)
        unit.fuel = 3.0
        unit.set_target(throttle=1.0)
        run(unit, 1.0, 0.1, self.world)
        self.assertLess(unit.fuel, 3.0)
        run(unit, 5.0, 0.1, self.world)
        self.assertEqual(unit.fuel, 0.0)
        self.assertEqual(unit.controls.throttle, 0.0)

    def test_idle_burns_less_than_full_throttle(self):
        a = build_unit("attacker", "a", pos=(0.0, 250.0, 0.0), speed=30.0)
        b = build_unit("attacker", "b", pos=(0.0, 250.0, 0.0), speed=30.0)
        a.set_target(throttle=0.1)
        b.set_target(throttle=1.0)
        run(a, 2.0, 0.1, self.world)
        run(b, 2.0, 0.1, self.world)
        self.assertGreater(a.fuel, b.fuel)

    def test_crashes_into_ground(self):
        unit = build_unit("attacker", "jet", pos=(0.0, 70.0, 0.0), speed=30.0)
        unit.set_target(pitch=35.0, throttle=1.0)
        run(unit, 8.0, 0.1, self.world)
        self.assertFalse(unit.alive)
        self.assertEqual(unit.status, "crashed")
        self.assertIn("земл", unit.crashed_reason)
        self.assertEqual(len(self.world.markers), 1)
        self.assertEqual(self.world.markers[0].kind, "crash")

    def test_min_altitude_levels_off_instead_of_crashing(self):
        """Ниже эксплуатационного минимума — выравнивание, не авария."""
        unit = build_unit("bomber", "b", pos=(0.0, 200.0, 0.0), speed=28.0)
        unit.set_target(pitch=40.0, throttle=0.3)     # пикирование
        run(unit, 30.0, 0.2, self.world)
        self.assertGreaterEqual(unit.pos[1], unit.spec.min_altitude - 0.01)
        self.assertTrue(unit.alive, "самолёт разбился о нижний предел высоты")
        self.assertIn(unit.status, ("low_altitude", "flying", "g_limit"))

    def test_ceiling_is_respected(self):
        unit = build_unit("fighter", "f", pos=(0.0, 300.0, 0.0), speed=40.0)
        unit.set_target(pitch=-40.0, throttle=1.0)
        run(unit, 20.0, 0.1, self.world)
        self.assertLessEqual(unit.pos[1], unit.spec.service_ceiling + 0.01)

    def test_dt_independent_turn_rate(self):
        """Регресс UNIT-03: результат не должен зависеть от частоты тика."""
        fine = build_unit("attacker", "a", pos=(0.0, 250.0, 0.0), speed=30.0)
        coarse = build_unit("attacker", "b", pos=(0.0, 250.0, 0.0), speed=30.0)
        for u in (fine, coarse):
            u.set_target(roll=30.0, pitch=0.0)
        run(fine, 2.0, 0.05, self.world)
        run(coarse, 2.0, 0.5, self.world)
        self.assertAlmostEqual(fine.yaw, coarse.yaw, delta=6.0,
                               msg=f"зависимость от dt: {fine.yaw} vs {coarse.yaw}")
        self.assertAlmostEqual(fine.speed, coarse.speed, delta=4.0)

    def test_velocity_matches_speed_and_heading(self):
        unit = self._jet()
        unit.yaw, unit.pitch = 90.0, 0.0
        unit.speed = 20.0
        vx, vy, vz = unit.velocity()
        self.assertAlmostEqual(math.hypot(vx, vy, vz), 20.0, delta=0.01)
        self.assertLess(vx, -19.0, "yaw=90 — это запад (−X)")


class TestHelicopterPhysics(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def _heli(self, **kw):
        base = dict(pos=(0.0, 120.0, 0.0), speed=0.0)
        base.update(kw)
        return build_unit("attack_heli", "heli", **base)

    def test_hover_holds_altitude(self):
        """Регресс UNIT-19: реальное зависание."""
        unit = self._heli()
        unit.set_target(throttle=unit.spec.hover_throttle, pitch=0.0, roll=0.0)
        run(unit, 5.0, 0.1, self.world)
        self.assertAlmostEqual(unit.pos[1], 120.0, delta=1.5)
        self.assertLess(unit.speed, 1.0)
        self.assertTrue(unit.alive)

    def test_collective_climbs_and_descends(self):
        up = self._heli()
        up.set_target(throttle=1.0)
        run(up, 3.0, 0.1, self.world)
        down = self._heli(pos=(0.0, 200.0, 0.0))
        down.set_target(throttle=0.0)
        run(down, 3.0, 0.1, self.world)
        self.assertGreater(up.pos[1], 125.0)
        self.assertLess(down.pos[1], 195.0)

    def test_cyclic_moves_forward(self):
        unit = self._heli()
        unit.yaw = 0.0
        unit.set_target(throttle=0.5, pitch=unit.spec.max_pitch)
        run(unit, 4.0, 0.1, self.world)
        self.assertGreater(unit.pos[2], 10.0, "нос вниз — движение вперёд (+Z)")

    def test_yaw_rate_turns_in_place(self):
        unit = self._heli()
        unit.set_target(throttle=0.5, yaw_rate=45.0)
        run(unit, 2.0, 0.1, self.world)
        self.assertAlmostEqual(unit.yaw, 90.0, delta=6.0)
        self.assertLess(abs(unit.pos[0]) + abs(unit.pos[2]), 2.0,
                        "вертолёт улетел вместо разворота на месте")

    def test_hover_burns_fuel(self):
        unit = self._heli()
        unit.set_target(throttle=0.5)
        fuel0 = unit.fuel
        run(unit, 10.0, 0.1, self.world)
        self.assertGreater(fuel0 - unit.fuel, 3.0,
                           "висение не должно быть бесплатным")
        # И существенно меньше, чем полный газ
        hard = self._heli()
        hard.set_target(throttle=1.0)
        run(hard, 10.0, 0.1, self.world)
        self.assertGreater(fuel0 - hard.fuel, fuel0 - unit.fuel)

    def test_landing_is_not_a_crash(self):
        unit = self._heli(pos=(0.0, 70.0, 0.0))
        unit.set_target(throttle=0.45)
        run(unit, 12.0, 0.1, self.world)
        self.assertTrue(unit.alive)
        self.assertEqual(unit.status, "landed")

    def test_hard_landing_crashes(self):
        unit = self._heli(pos=(0.0, 80.0, 0.0))
        unit.set_target(throttle=0.0)          # быстрое снижение
        run(unit, 12.0, 0.1, self.world)
        self.assertFalse(unit.alive)
        self.assertIn("посадк", unit.crashed_reason)

    def test_drone_is_a_unit_too(self):
        unit = build_unit("recon_drone", "d", pos=(0.0, 150.0, 0.0))
        self.assertIsInstance(unit, Drone)
        self.assertIsInstance(unit, Helicopter)
        unit.set_target(throttle=0.5)
        run(unit, 3.0, 0.1, self.world)
        self.assertAlmostEqual(unit.pos[1], 150.0, delta=2.0)


class TestTankPhysics(unittest.TestCase):
    def setUp(self):
        self.world = World()
        # Рельеф с известными высотами, чтобы проверить привязку к земле
        tiles = [(x, z, 64 + (4 if x > 40 else 0), "grass_block")
                 for x in range(-64, 128, 8) for z in range(-64, 64, 8)]
        self.world.terrain.step = 8
        self.world.terrain.set_tiles(tiles)

    def test_follows_terrain(self):
        """Регресс UNIT-06: танк не должен висеть на высоте спавна."""
        unit = build_unit("mbt", "tank", pos=(0.0, 200.0, 0.0), throttle=1.0)
        run(unit, 3.0, 0.1, self.world)
        self.assertAlmostEqual(unit.pos[1], 65.0, delta=1.0,
                               msg="танк не привязан к рельефу")

    def test_drives_forward(self):
        unit = build_unit("mbt", "tank", pos=(0.0, 65.0, 0.0), throttle=1.0)
        unit.yaw = 0.0
        run(unit, 4.0, 0.1, self.world)
        self.assertGreater(unit.pos[2], 10.0)

    def test_turn_rate_clamped(self):
        unit = build_unit("mbt", "tank", pos=(0.0, 65.0, 0.0), throttle=0.0)
        unit.set_target(yaw_rate=9999.0)
        run(unit, 1.0, 0.1, self.world)
        self.assertLessEqual(abs(unit.yaw), unit.spec.turn_rate + 1.0)

    def test_stops_on_steep_slope(self):
        tiles = [(x, z, 64 if x <= 0 else 64 + 12 * x, "stone")
                 for x in range(-32, 32, 8) for z in range(-32, 32, 8)]
        world = World()
        world.terrain.step = 8
        world.terrain.set_tiles(tiles)
        unit = build_unit("mbt", "tank", pos=(0.0, 65.0, 0.0), throttle=1.0)
        unit.yaw = 270.0                       # на восток (+X), в «гору»
        run(unit, 4.0, 0.1, world)
        self.assertEqual(unit.status, "stuck")

    def test_tank_has_no_stall(self):
        unit = build_unit("mbt", "tank", pos=(0.0, 65.0, 0.0), throttle=1.0)
        run(unit, 2.0, 0.1, self.world)
        self.assertFalse(unit.stalled)
        self.assertEqual(unit.g_load, 1.0)


class TestSnapshotAndModel(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_snapshot_is_a_copy(self):
        unit = build_unit("attacker", "jet", pos=(0.0, 150.0, 0.0))
        snap = unit.snapshot()
        snap["mounts"][0]["ammo"] = -999
        snap["pos"] = (1.0, 2.0, 3.0)
        self.assertEqual(unit.mounts[0].ammo, unit.mounts[0].ammo_max)
        self.assertEqual(unit.pos, (0.0, 150.0, 0.0))

    def test_model_commands_are_empty_when_static(self):
        """Стоящий на месте танк не должен генерировать команды перерисовки."""
        unit = build_unit("attacker", "jet", pos=(0.0, 150.0, 0.0))
        first = unit.model_commands()
        self.assertGreater(len(first), 10)
        self.assertEqual(unit.model_commands(), [],
                         "повторный вызов без изменения позы обязан быть пустым")

        tank = build_unit("mbt", "tank", pos=(0.0, 65.0, 0.0),
                          throttle=0.0, speed=0.0)
        tank.model_commands()
        for _ in range(20):
            tank.step(0.1, self.world)
        self.assertLess(tank.speed, 0.01)
        self.assertEqual(tank.model_commands(), [],
                         "неподвижный юнит перерисовывается впустую")

    def test_despawn_removes_exactly_placed_blocks(self):
        unit = build_unit("attacker", "jet", pos=(0.0, 150.0, 0.0))
        unit.model_commands()
        placed = set(unit.model.placed.keys())
        cmds = unit.despawn_commands()
        self.assertEqual(len(cmds), len(placed))
        self.assertTrue(all("minecraft:air" in c for c in cmds))
        self.assertEqual(unit.model.placed_count, 0)

    def test_telemetry_fields(self):
        unit = build_unit("attacker", "jet", pos=(0.0, 150.0, 0.0), speed=30.0)
        run(unit, 2.0, 0.1, self.world)
        t = unit.telemetry()
        for key in ("speed", "altitude", "heading", "g_load", "stalled",
                    "fuel_pct", "ammo", "status"):
            self.assertIn(key, t)
        self.assertGreater(t["flight_time"], 0.0)
        self.assertGreater(t["distance"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
