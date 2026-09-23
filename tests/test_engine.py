"""Тесты движка юнитов: жизненный цикл, модель на сервере, огонь, пауза."""
from __future__ import annotations

import threading
import time
import unittest

from rwf import mc
from rwf.config import AppConfig
from rwf.engine import UnitEngine
from rwf.mock_server import MockMCServer
from rwf.rcon import CommandQueue, RCONPool
from rwf.units import build_unit
from rwf.weapons import Target, WeaponSystem
from rwf.world import World

HOST = "127.0.0.1"


class Stack:
    """Полный стек: имитатор сервера -> пул -> очередь -> мир -> оружие -> движок."""

    def __init__(self, tick: float = 0.1, model_hz: float = 0.0, **cfg_kw):
        self.server = MockMCServer(host=HOST, port=0, password="2203").start()
        self.pool = RCONPool(HOST, self.server.port, "2203", size=3, timeout=5.0)
        self.queue = CommandQueue(self.pool, workers=1, batch_size=32, rate=0)
        self.queue.start()
        self.world = World()
        self.cfg = AppConfig()
        self.cfg.sim.tick = tick
        self.cfg.sim.model_update_hz = model_hz
        self.weapons = WeaponSystem(self.cfg.combat, self.queue, self.world,
                                    bus=self.world.bus)
        self.engine = UnitEngine(self.world, self.queue, self.weapons, cfg=self.cfg)

    def close(self):
        self.engine.stop(join_timeout=2.0)
        self.queue.stop(timeout=2.0, flush=False)
        self.pool.close()
        self.server.stop()

    def flush(self, timeout: float = 8.0) -> bool:
        return self.queue.flush(timeout=timeout)

    def blocks(self) -> dict:
        return dict(self.server.blocks)


class TestSpawnAndLifecycle(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.engine = self.stack.engine

    def tearDown(self):
        self.stack.close()

    def test_id_assigned_before_return(self):
        """Регресс UI-02: id известен сразу, а не после запуска потока."""
        unit = self.engine.spawn("attacker", (0.0, 150.0, 0.0))
        self.assertIsNotNone(unit.id)
        self.assertIn(unit.id, self.engine.uids())
        self.assertIs(self.engine.get(unit.id), unit)
        self.assertIs(self.stack.world.get_unit(unit.id), unit)

    def test_spawn_puts_model_in_world(self):
        unit = self.engine.spawn("attacker", (0.0, 150.0, 0.0))
        self.assertTrue(self.stack.flush())
        blocks = self.stack.blocks()
        self.assertEqual(len(blocks), unit.model.placed_count)
        self.assertGreater(len(blocks), 10)
        self.assertTrue(all(b.startswith("minecraft:") for b in blocks.values()))

    def test_despawn_removes_every_model_block(self):
        """Регресс UNIT-11/UNIT-12: после снятия юнита в мире не остаётся блоков."""
        unit = self.engine.spawn("attacker", (0.0, 150.0, 0.0))
        self.stack.flush()
        self.assertGreater(len(self.stack.blocks()), 0)
        self.assertTrue(self.engine.despawn(unit.id))
        self.assertTrue(self.stack.flush())
        self.assertEqual(self.stack.blocks(), {},
                         "после деспауна в мире остались блоки модели")
        self.assertIsNone(self.stack.world.get_unit(unit.id))
        self.assertEqual(self.engine.uids(), [])

    def test_despawn_does_not_touch_terrain(self):
        """Модель убирает только то, что поставила сама."""
        self.stack.server.blocks[(500, 150, 500)] = "minecraft:diamond_block"
        unit = self.engine.spawn("attacker", (0.0, 150.0, 0.0))
        self.stack.flush()
        self.engine.despawn(unit.id)
        self.stack.flush()
        self.assertEqual(self.stack.blocks(), {(500, 150, 500): "minecraft:diamond_block"})

    def test_flight_leaves_no_trail(self):
        """Регресс UNIT-11: после перелёта в мире ровно одна модель."""
        unit = self.engine.spawn("attacker", (0.0, 200.0, 0.0), heading=0.0,
                                 throttle=0.9, speed=30.0)
        for _ in range(40):
            self.engine.tick_once(0.25)
        self.assertTrue(self.stack.flush())
        self.assertEqual(len(self.stack.blocks()), unit.model.placed_count,
                         "за самолётом тянется след из блоков")
        self.assertGreater(unit.distance_flown, 100.0)

    def test_second_despawn_is_noop(self):
        unit = self.engine.spawn("attacker", (0.0, 150.0, 0.0))
        self.assertTrue(self.engine.despawn(unit.id))
        self.assertFalse(self.engine.despawn(unit.id))

    def test_pause_freezes_unit(self):
        unit = self.engine.spawn("attacker", (0.0, 200.0, 0.0), speed=30.0)
        self.engine.pause(unit.id)
        pos0 = unit.pos
        for _ in range(20):
            self.engine.tick_once(0.1)
        self.assertEqual(unit.pos, pos0)
        self.assertTrue(self.engine.is_paused(unit.id))
        self.engine.pause(unit.id, False)
        self.engine.tick_once(0.1)
        self.assertNotEqual(unit.pos, pos0)

    def test_pause_all(self):
        unit = self.engine.spawn("attacker", (0.0, 200.0, 0.0), speed=30.0)
        self.engine.pause_all()
        pos0 = unit.pos
        for _ in range(10):
            self.engine.tick_once(0.1)
        self.assertEqual(unit.pos, pos0)
        self.engine.pause_all(False)

    def test_crash_removes_unit_and_explodes(self):
        unit = self.engine.spawn("attacker", (0.0, 70.0, 0.0), speed=30.0)
        unit.set_target(pitch=40.0, throttle=1.0)
        for _ in range(200):
            self.engine.tick_once(0.1)
            if not unit.alive:
                break
        self.assertFalse(unit.alive)
        self.engine.tick_once(0.1)               # движок должен заметить гибель
        self.assertTrue(self.stack.flush())
        self.assertEqual(self.engine.uids(), [])
        self.assertIsNone(self.stack.world.get_unit(unit.id))
        self.assertEqual(self.engine.stats.crashes, 1)
        # Контракт v15 (WRECK-01…): блоки МОДЕЛИ юнита убраны, а на месте
        # гибели остаётся след аварии — сгоревший остов и огонь. Проверяем
        # строго: в мире не осталось ничего, кроме клеток зарегистрированного
        # места аварии (иначе «чужие» блоки модели считаются неудалями).
        sites = self.engine.wreckage.sites()
        self.assertEqual(len(sites), 1, "место аварии не создано")
        site = sites[0]
        allowed = set(site.hull) | set(site.fire)
        leftover = set(self.stack.blocks())
        self.assertTrue(leftover <= allowed,
                        f"обломки модели не убраны: {leftover - allowed}")
        self.assertGreater(len(leftover), 0, "след аварии не поставлен в мир")
        self.assertTrue(self.stack.server.count(r"^summon minecraft:tnt"))
        self.assertTrue(self.stack.server.count(r"^summon minecraft:falling_block"),
                        "падающих обломков нет")

    def test_snapshots_are_copies(self):
        self.engine.spawn("attacker", (0.0, 150.0, 0.0))
        snaps = self.engine.snapshots()
        snaps[0]["label"] = "ИСПОРЧЕНО"
        snaps[0]["mounts"][0]["ammo"] = -5
        fresh = self.engine.snapshots()
        self.assertEqual(fresh[0]["label"], "Су-25")
        self.assertGreater(fresh[0]["mounts"][0]["ammo"], 0)

    def test_units_filter_dead(self):
        unit = self.engine.spawn("attacker", (0.0, 150.0, 0.0))
        unit.alive = False
        self.assertEqual(self.engine.units(), [])
        self.assertEqual(self.engine.units(include_dead=True), [unit])


class TestControlAndFire(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.engine = self.stack.engine
        self.unit = self.engine.spawn("attacker", (0.0, 200.0, 0.0), speed=30.0)

    def tearDown(self):
        self.stack.close()

    def test_set_target_and_trim(self):
        self.assertTrue(self.engine.set_target(self.unit.id, heading=90.0))
        self.assertEqual(self.unit.controls.heading, 90.0)
        self.assertTrue(self.engine.trim(self.unit.id, dpitch=-5.0))
        self.assertAlmostEqual(self.unit.controls.pitch, -5.0)
        self.assertTrue(self.engine.level(self.unit.id))
        self.assertEqual((self.unit.controls.roll, self.unit.controls.pitch), (0.0, 0.0))

    def test_commands_on_missing_uid(self):
        self.assertFalse(self.engine.set_target(9999, heading=0.0))
        self.assertFalse(self.engine.trim(9999, dpitch=1.0))
        self.assertFalse(self.engine.level(9999))
        self.assertFalse(self.engine.despawn(9999))
        self.assertEqual(self.engine.fire(9999), 0)
        self.assertFalse(self.engine.rearm(9999))

    def test_fire_consumes_ammo_and_reaches_server(self):
        before = self.unit.ammo_total
        n = self.engine.fire(self.unit.id, category="cannon",
                             target=Target(pos=(0.0, 64.0, 300.0)))
        self.assertEqual(n, 1)
        self.assertLess(self.unit.ammo_total, before)
        self.assertTrue(self.stack.flush())
        self.assertTrue(self.stack.server.count(r"^summon minecraft:"))

    def test_fire_by_mount_index(self):
        idx = next(i for i, m in enumerate(self.unit.mounts) if m.category == "bomb")
        n = self.engine.fire(self.unit.id, mount_index=idx)
        self.assertEqual(n, 1)
        self.assertTrue(self.stack.flush())
        self.assertTrue(self.stack.server.count(r"^summon minecraft:tnt"))
        self.assertEqual(self.engine.fire(self.unit.id, mount_index=999), 0)

    def test_load_weapon_validates_availability(self):
        idx = next(i for i, m in enumerate(self.unit.mounts) if m.category == "bomb")
        self.assertTrue(self.engine.load_weapon(self.unit.id, idx, "fab1500"))
        self.assertEqual(self.unit.mounts[idx].key, "fab1500")
        # Пушки нет в списке доступных для бомбового подвеса
        self.assertFalse(self.engine.load_weapon(self.unit.id, idx, "gsh23"))
        self.assertEqual(self.unit.mounts[idx].key, "fab1500")
        self.assertTrue(self.engine.load_weapon(self.unit.id, idx, None))
        self.assertEqual(self.unit.mounts[idx].ammo, 0)
        self.assertFalse(self.engine.load_weapon(self.unit.id, 999, "fab100"))

    def test_rearm_and_refuel(self):
        self.unit.mounts[0].consume(1)
        self.unit.fuel = 1.0
        self.assertTrue(self.engine.rearm(self.unit.id))
        self.assertTrue(self.engine.refuel(self.unit.id))
        self.assertEqual(self.unit.fuel, self.unit.spec.fuel_max)
        self.assertTrue(self.engine.refuel(self.unit.id, amount=5.0))
        self.engine.set_target(self.unit.id, throttle=0.5)

    def test_strike_zone_auto_bomb(self):
        """Регресс WPN-07: возвращён авто-сброс при влёте в зону удара."""
        self.stack.world.set_strike_zone(-50, -50, 50, 50)
        bomb = next(m for m in self.unit.mounts if m.category == "bomb")
        before = bomb.ammo
        for _ in range(3):
            self.engine.tick_once(0.1)
            bomb.last_fire = 0.0            # снимаем паузу между залпами
        self.assertTrue(self.stack.flush())
        self.assertLess(bomb.ammo, before, "зона удара не вызвала сброс")
        self.assertTrue(self.stack.server.count(r"^summon minecraft:tnt"))

    def test_strike_zone_ignores_bots(self):
        self.stack.world.set_strike_zone(-500, -500, 500, 500)
        bot = self.engine.spawn("attacker", (0.0, 200.0, 0.0), is_bot=True, speed=30.0)
        bomb = next(m for m in bot.mounts if m.category == "bomb")
        before = bomb.ammo
        self.engine.tick_once(0.1)
        self.assertEqual(bomb.ammo, before, "бот не должен управляться зоной удара")


class TestEngineLoop(unittest.TestCase):
    def test_background_thread_runs_and_stops(self):
        stack = Stack(tick=0.05)
        try:
            unit = stack.engine.spawn("attacker", (0.0, 200.0, 0.0), speed=30.0)
            stack.engine.start()
            self.assertTrue(stack.engine.running)
            time.sleep(0.4)
            self.assertGreater(stack.engine.stats.ticks, 2)
            self.assertGreater(unit.distance_flown, 0.0)
            started = time.monotonic()
            stack.engine.stop()                  # без join — не блокирует вызывающего
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(stack.engine.join(timeout=3.0))
            self.assertFalse(stack.engine.running)
        finally:
            stack.close()

    def test_model_rate_limit(self):
        """model_update_hz ограничивает частоту перерисовки модели."""
        stack = Stack(tick=0.05, model_hz=2.0)     # не чаще 2 раз в секунду
        try:
            unit = stack.engine.spawn("attacker", (0.0, 200.0, 0.0), speed=30.0)
            stack.flush()
            stack.server.command_log.clear()
            for _ in range(40):                    # 2 секунды симуляции
                stack.engine.tick_once(0.05)
                time.sleep(0.01)
            stack.flush()
            setblocks = stack.server.count(r"^setblock")
            self.assertGreater(setblocks, 0)
            self.assertLess(setblocks, 40 * 20,
                            "модель перерисовывается каждый тик без ограничения")
        finally:
            stack.close()

    def test_stats_snapshot(self):
        stack = Stack()
        try:
            unit = stack.engine.spawn("attacker", (0.0, 200.0, 0.0), speed=30.0)
            stack.engine.tick_once(0.1)
            stack.engine.fire(unit.id, category="cannon")
            st = stack.engine.stats_snapshot()
            for key in ("engine", "units", "weapons", "queue", "world_revision"):
                self.assertIn(key, st)
            self.assertEqual(st["units"], 1)
            self.assertGreaterEqual(st["engine"]["ticks"], 1)
            self.assertEqual(st["engine"]["spawn_calls"], 1)
        finally:
            stack.close()

    def test_tick_survives_unit_exception(self):
        """Ошибка физики одного юнита не должна ронять остальные."""
        import logging
        logging.getLogger("rwf.engine").setLevel(logging.CRITICAL)
        stack = Stack()
        try:
            good = stack.engine.spawn("attacker", (0.0, 200.0, 0.0), speed=30.0)
            bad = stack.engine.spawn("attacker", (50.0, 200.0, 0.0), speed=30.0)

            def boom(dt, world):
                raise RuntimeError("симулируем сбой физики")

            bad.step = boom                       # type: ignore[method-assign]
            pos0 = good.pos
            for _ in range(10):
                stack.engine.tick_once(0.1)
            self.assertNotEqual(good.pos, pos0, "здоровый юнит встал из-за ошибки соседа")
            self.assertTrue(good.alive)
        finally:
            stack.close()

    def test_multiple_units_are_independent(self):
        stack = Stack()
        try:
            a = stack.engine.spawn("attacker", (0.0, 200.0, 0.0), heading=0.0, speed=30.0)
            b = stack.engine.spawn("fighter", (200.0, 250.0, 0.0), heading=180.0, speed=50.0)
            c = stack.engine.spawn("mbt", (400.0, 65.0, 0.0), throttle=0.0,
                                   speed=0.0)
            self.assertEqual(len(stack.engine.uids()), 3)
            for _ in range(20):
                stack.engine.tick_once(0.1)
            self.assertGreater(a.pos[2], 0.0)          # летит на юг
            self.assertLess(b.pos[2], 0.0)             # летит на север
            self.assertEqual(c.speed, 0.0)
            stack.flush()
            self.assertEqual(len(stack.blocks()),
                             a.model.placed_count + b.model.placed_count
                             + c.model.placed_count)
        finally:
            stack.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
