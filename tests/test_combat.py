"""Тесты боевых механик: урон, подбитие, базы, блочные ракеты, огонь из игры."""
from __future__ import annotations

import math
import unittest

from rwf.bases import (KIND_AIRPORT, KIND_CARRIER, KIND_GROUND, BaseManager,
                          ACCEPTS)
from rwf.combat import (PLAYER_GUN_CONE, PLAYER_GUN_RANGE, CombatSystem,
                           PlayerGunPoller)
from rwf.config import CombatConfig
from rwf.missiles import MissileManager, MissileSpec, MissileUnit
from rwf.routes import Action, Route, Waypoint
from rwf.units import build_unit
from rwf.weapons import Target, WeaponSystem
from rwf.world import World


class FakeQueue:
    def __init__(self):
        self.commands = []

    def submit(self, command, priority=10, key=None):
        self.commands.append(command)

    def submit_many(self, commands, priority=10, key_prefix=None):
        self.commands.extend(commands)

    def flush(self, timeout=1.0):
        return True

    def stats(self):
        return {"submitted": len(self.commands)}


class TestDamage(unittest.TestCase):
    def test_armor_reduces_damage(self):
        tank = build_unit("mbt", "t")          # броня 55
        tank.take_damage(100, "bomb")
        self.assertAlmostEqual(tank.health, 260 - 45, delta=0.1)
        self.assertEqual(tank.status, "damaged")

    def test_destroy_at_zero(self):
        u = build_unit("fighter", "f")         # 90 hp, броня 5
        destroyed = u.take_damage(500, "bomb")
        self.assertTrue(destroyed)
        self.assertFalse(u.alive)
        self.assertEqual(u.status, "destroyed")
        self.assertEqual(u.health, 0.0)

    def test_no_damage_when_dead(self):
        u = build_unit("fighter", "f")
        u.destroy("test")
        self.assertFalse(u.take_damage(100, "x"))

    def test_repair(self):
        u = build_unit("attacker", "a")
        u.take_damage(60, "gun")
        u.repair()
        self.assertEqual(u.health, u.max_health)

    def test_health_in_snapshot(self):
        u = build_unit("attacker", "a")
        u.take_damage(30, "gun")
        snap = u.snapshot()
        self.assertIn("health", snap)
        self.assertIn("health_pct", snap)
        self.assertLess(snap["health_pct"], 100)


class TestCombatSystem(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.combat = CombatSystem(self.world)
        self.u1 = build_unit("attacker", "a", pos=(0.0, 100.0, 0.0))
        self.u2 = build_unit("attacker", "b", pos=(500.0, 100.0, 0.0))
        self.world.add_unit(self.u1)
        self.world.add_unit(self.u2)

    def test_blast_hits_only_in_radius(self):
        evs = self.combat.resolve_blast(self.world.iter_units(), (0.0, 100.0, 0.0),
                                        radius=60.0, damage=50.0,
                                        source_kind="bomb", source_id=2)
        ids = {e.target_id for e in evs}
        self.assertIn(self.u1.id, ids)
        self.assertNotIn(self.u2.id, ids, "дальний юнит не должен получать урон")

    def test_blast_falloff(self):
        near = build_unit("attacker", "n", pos=(10.0, 100.0, 0.0))
        far = build_unit("attacker", "f", pos=(50.0, 100.0, 0.0))
        self.world.add_unit(near); self.world.add_unit(far)
        hp_n, hp_f = near.health, far.health
        self.combat.resolve_blast(self.world.iter_units(), (0.0, 100.0, 0.0),
                                  radius=60.0, damage=60.0)
        lost_n = hp_n - near.health
        lost_f = hp_f - far.health
        self.assertGreater(lost_n, lost_f, "урон не убывает с дистанцией")

    def test_blast_skips_owner(self):
        before = self.u1.health
        self.combat.resolve_blast(self.world.iter_units(), (0.0, 100.0, 0.0),
                                  60.0, 50.0, skip=self.u1.id)
        self.assertEqual(self.u1.health, before, "инициатор не должен бить сам себя")

    def test_kill_feed(self):
        self.combat.resolve_blast(self.world.iter_units(), (0.0, 100.0, 0.0),
                                  60.0, 999.0, source_kind="bomb")
        self.assertTrue(self.combat.feed)
        self.assertEqual(self.combat.kills, 1)
        self.assertIn("УНИЧТОЖЕН", self.combat.feed[-1].describe())

    def test_stats(self):
        self.combat.hit(self.u1, 10, "gun")
        st = self.combat.stats()
        self.assertEqual(st["kills"], 0)
        self.assertGreater(st["total_damage"], 0)


class TestPlayerGun(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.combat = CombatSystem(self.world)
        self.bot = build_unit("attacker", "bot", pos=(100.0, 100.0, 0.0), is_bot=True)
        self.world.add_unit(self.bot)

    def _rec(self, yaw):
        return {"pos": (0.0, 100.0, 0.0), "yaw": yaw, "pitch": 0.0}

    def test_hit_in_cone(self):
        poller = PlayerGunPoller(self.world, self.combat, pool=None)
        # бот на востоке (+X); yaw смотрящий на восток = 270
        ev = poller.fire_from_player("Arlik88", self._rec(270.0))
        self.assertIsNotNone(ev, "выстрел на восток должен попасть в бота на востоке")
        self.assertLess(self.bot.health, self.bot.max_health)

    def test_miss_out_of_cone(self):
        poller = PlayerGunPoller(self.world, self.combat, pool=None)
        ev = poller.fire_from_player("Arlik88", self._rec(90.0))   # смотрит на запад
        self.assertIsNone(ev, "выстрел в сторону не должен попадать")

    def test_miss_out_of_range(self):
        poller = PlayerGunPoller(self.world, self.combat, pool=None)
        far = {"pos": (-1000.0, 100.0, 0.0), "yaw": 270.0, "pitch": 0.0}
        self.assertIsNone(poller.fire_from_player("X", far))

    def test_only_enemies_targeted(self):
        friendly = build_unit("attacker", "fr", pos=(100.0, 100.0, 0.0), is_bot=False)
        self.world.add_unit(friendly)
        poller = PlayerGunPoller(self.world, self.combat, pool=None)
        ev = poller.fire_from_player("X", self._rec(270.0))
        # по умолчанию враги = боты; Friendly тоже на востоке, но ближе? оба на 100
        # is_enemy по умолчанию is_bot -> попадёт в бота
        self.assertIsNotNone(ev)
        self.assertEqual(ev.target_id, self.bot.id)


class TestBases(unittest.TestCase):
    def setUp(self):
        self.mgr = BaseManager()

    def test_add_and_types(self):
        a = self.mgr.add("Северный", KIND_AIRPORT, 0.0, 0.0)
        c = self.mgr.add("Шторм", KIND_CARRIER, 500.0, 500.0)
        g = self.mgr.add("Гарнизон", KIND_GROUND, -500.0, 0.0)
        self.assertEqual(a.kind, KIND_AIRPORT)
        self.assertTrue(c.movable, "авианосец должен быть перемещаемым")
        self.assertFalse(a.movable)
        self.assertTrue(g.pads)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.mgr.add("X", "spaceport", 0, 0)

    def test_accepts(self):
        a = self.mgr.add("A", KIND_AIRPORT, 0, 0)
        g = self.mgr.add("G", KIND_GROUND, 0, 0)
        self.assertTrue(a.accepts("aircraft"))
        self.assertFalse(a.accepts("tank"))
        self.assertTrue(g.accepts("tank"))
        self.assertFalse(g.accepts("aircraft"))

    def test_pads_take_release(self):
        a = self.mgr.add("A", KIND_AIRPORT, 0, 0)
        pad = a.take_pad(1)
        self.assertIsNotNone(pad)
        self.assertEqual(pad.occupied_by, 1)
        a.release_pad(1)
        self.assertEqual(pad.occupied_by, None)
        # занять все стоянки — свободных не остаётся
        for i in range(len(a.pads)):
            self.assertIsNotNone(a.take_pad(100 + i))
        self.assertIsNone(a.free_pad())

    def test_nearest_filters(self):
        self.mgr.add("close_air", KIND_AIRPORT, 100.0, 0.0)
        self.mgr.add("far_air", KIND_AIRPORT, 900.0, 0.0)
        self.mgr.add("close_ground", KIND_GROUND, 150.0, 0.0)
        b = self.mgr.nearest(0.0, 0.0, unit_kind="aircraft")
        self.assertEqual(b.name, "close_air")
        b = self.mgr.nearest(0.0, 0.0, kind=KIND_GROUND)
        self.assertEqual(b.name, "close_ground")

    def test_spawn_point_uses_pad_heading(self):
        a = self.mgr.add("A", KIND_AIRPORT, 0.0, 0.0, heading=90.0)
        x, z, heading = self.mgr.spawn_point(a, 7, 150.0)
        self.assertEqual(heading, 90.0)
        self.assertIsNotNone(a.free_pad() or a.pads[0].occupied_by)

    def test_service(self):
        a = self.mgr.add("A", KIND_AIRPORT, 0, 0)
        u = build_unit("attacker", "u")
        u.fuel = 1.0
        u.take_damage(50, "gun")
        u.mounts[0].consume(2)
        done = self.mgr.service_at(a, u)
        self.assertIn("заправка", done)
        self.assertIn("боезапас", done)
        self.assertIn("ремонт", done)
        self.assertEqual(u.fuel, u.spec.fuel_max)
        self.assertEqual(u.health, u.max_health)

    def test_carrier_moves(self):
        c = self.mgr.add("C", KIND_CARRIER, 0.0, 0.0)
        c.move_to((100.0, 0.0))
        for _ in range(50):
            c.step(1.0)
        self.assertGreater(c.x, 0.0)
        self.assertLess(c.x, 100.0 + 1.0)


class TestBlockMissiles(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.combat = CombatSystem(self.world)
        self.queue = FakeQueue()
        self.manager = MissileManager(self.queue, self.combat,
                                      get_units=self.world.iter_units)

    def test_missile_is_block_model(self):
        m = MissileUnit(1, (0.0, 100.0, 0.0), (0.0, 0.0, 60.0),
                        Target(pos=(0.0, 100.0, 300.0)))
        n = m.model_commands(self.queue)
        self.assertGreater(n, 0, "ракета должна ставить блоки в мире")
        self.assertGreaterEqual(m.model.placed_count, 4,
                                "ракета = несколько блоков")

    def test_missile_steers_to_target(self):
        m = MissileUnit(1, (0.0, 100.0, 0.0), (60.0, 0.0, 0.0),
                        Target(pos=(0.0, 100.0, 300.0)),
                        MissileSpec(speed=80.0, turn_rate=90.0))
        for _ in range(120):
            if m.step(0.1) != "fly":
                break
        # после наведения направление должно смотреть на цель (преимущественно +Z)
        self.assertGreater(m.dir[2], 0.5, "ракета не довернула к цели")

    def test_missile_hits_and_damages(self):
        victim = build_unit("attacker", "v", pos=(0.0, 100.0, 300.0))
        self.world.add_unit(victim)
        before = victim.health
        self.manager.launch((0.0, 100.0, 0.0), (0.0, 0.0, 80.0),
                            Target(pos=(0.0, 100.0, 300.0)),
                            MissileSpec(speed=100.0, turn_rate=120.0, damage=80.0),
                            owner_id=99)
        events = []
        for _ in range(200):
            events += self.manager.step(0.05)
            if not self.manager.missiles:
                break
        hits = [e for e in events if e["kind"] == "hit"]
        self.assertTrue(hits, "ракета не попала")
        self.assertLess(victim.health, before, "урон не нанесён")
        # блоки ракеты убраны из мира при детонации
        self.assertEqual(self.manager.missiles, [], "ракета не удалена после попадания")
        air_cmds = [c for c in self.queue.commands if "air" in c]
        self.assertTrue(air_cmds, "ракета не убрала свои блоки")

    def test_missile_expires(self):
        self.manager.launch((0.0, 100.0, 0.0), (0.0, 0.0, 60.0),
                            Target(pos=(5000.0, 100.0, 5000.0)),
                            MissileSpec(ttl=0.3))
        events = []
        for _ in range(40):
            events += self.manager.step(0.05)
        self.assertTrue(any(e["kind"] == "expired" for e in events))
        self.assertEqual(self.manager.missiles, [])

    def test_snapshot_entries_for_map(self):
        self.manager.launch((0.0, 100.0, 0.0), (0.0, 0.0, 60.0),
                            Target(pos=(0.0, 100.0, 200.0)))
        entries = self.manager.snapshot_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "missile")
        self.assertIn("pos", entries[0])

    def test_weapon_fire_creates_block_missile(self):
        cfg = CombatConfig(use_block_missiles=True)
        ws = WeaponSystem(cfg, self.queue, self.world, combat=self.combat,
                          missiles=self.manager)
        unit = build_unit("fighter", "f", pos=(0.0, 150.0, 0.0))
        self.world.add_unit(unit)
        mount = next(m for m in unit.mounts if m.category == "missile")
        tgt = Target(pos=(0.0, 100.0, 300.0), name="Victim")
        self.world.set_player("Victim", (0.0, 100.0, 300.0))
        before = self.manager.launched
        self.assertTrue(ws.fire(unit, mount, tgt))
        self.assertEqual(self.manager.launched, before + 1,
                         "пуск УР должен создавать блочную ракету")

    def test_weapon_fire_fallback_without_manager(self):
        """P2.3: fallback-путь (без MissileManager) создаёт GuidedMissile и
        списывает ровно 1 ракету с подвески. Инвариант WPN-08: consume(1) —
        единственный источник списания; ни один путь пуска не имеет права
        трогать mount.ammo повторно (двойной расход)."""
        cfg = CombatConfig(use_block_missiles=True)
        ws = WeaponSystem(cfg, self.queue, self.world)   # missiles=None
        unit = build_unit("fighter", "f", pos=(0.0, 150.0, 0.0))
        mount = next(m for m in unit.mounts if m.category == "missile")
        self.world.set_player("V", (0.0, 100.0, 300.0))
        before = mount.ammo
        self.assertTrue(ws.fire(unit, mount, Target(pos=(0.0, 100.0, 300.0), name="V")))
        self.assertEqual(len(ws.missiles), 1, "без менеджера — старая УР")
        self.assertEqual(mount.ammo, before - 1,
                         "fallback-пуск должен списать ровно 1 ракету (регресс двойного consume)")
        self.assertEqual(ws.ammo_spent, 1)
        self.assertEqual(ws.missiles_launched, 1)

    def test_weapon_fire_block_missile_consumes_exactly_one(self):
        """P2.3: основной путь (через MissileManager) тоже списывает ровно 1."""
        cfg = CombatConfig(use_block_missiles=True)
        ws = WeaponSystem(cfg, self.queue, self.world, combat=self.combat,
                          missiles=self.manager)
        unit = build_unit("fighter", "f", pos=(0.0, 150.0, 0.0))
        mount = next(m for m in unit.mounts if m.category == "missile")
        tgt = Target(pos=(0.0, 100.0, 300.0), name="Victim")
        self.world.set_player("Victim", (0.0, 100.0, 300.0))
        before = mount.ammo
        self.assertTrue(ws.fire(unit, mount, tgt))
        self.assertEqual(mount.ammo, before - 1,
                         "блочный пуск должен списать ровно 1 ракету")
        self.assertEqual(ws.ammo_spent, 1)


class TestEngineIntegration(unittest.TestCase):
    def setUp(self):
        from rwf.engine import UnitEngine
        from rwf.rcon import CommandQueue, RCONPool
        from rwf.mock_server import MockMCServer
        self.server = MockMCServer(port=0, password="2203").start()
        self.pool = RCONPool("127.0.0.1", self.server.port, "2203", size=2, timeout=5.0)
        self.queue = CommandQueue(self.pool, workers=1, rate=0)
        self.queue.start()
        self.world = World()
        from rwf.config import AppConfig
        self.cfg = AppConfig()
        self.weapons = WeaponSystem(self.cfg.combat, self.queue, self.world)
        self.engine = UnitEngine(self.world, self.queue, self.weapons, cfg=self.cfg)

    def tearDown(self):
        self.engine.stop(join_timeout=1.0)
        self.queue.stop(timeout=1.0, flush=False)
        self.pool.close()
        self.server.stop()

    def test_spawn_from_base_and_service(self):
        base = self.engine.bases.add("A", KIND_AIRPORT, 0.0, 0.0, heading=0.0)
        unit = self.engine.spawn_from_base(base.id, "attacker", 150.0)
        self.assertIsNotNone(unit)
        self.assertEqual(unit.base_id, base.id)
        self.assertTrue(any(p.occupied_by == unit.id for p in base.pads))
        unit.fuel = 5.0
        done = self.engine.service_at_nearest_base(unit.id)
        self.assertTrue(done)
        self.assertEqual(unit.fuel, unit.spec.fuel_max)
        # деспаун освобождает стоянку
        self.engine.despawn(unit.id)
        self.assertTrue(all(p.occupied_by != unit.id for p in base.pads))

    def test_destroyed_unit_removed_and_pad_freed(self):
        base = self.engine.bases.add("A", KIND_AIRPORT, 0.0, 0.0)
        unit = self.engine.spawn_from_base(base.id, "attacker", 150.0)
        uid = unit.id
        self.engine.combat.hit(unit, 9999.0, "test")
        self.engine.tick_once(0.1)     # движок подбирает уничтоженный юнит
        self.assertIsNone(self.engine.get(uid))
        self.assertTrue(all(p.occupied_by != uid for p in base.pads))

    def test_bases_in_snapshot_for_map(self):
        self.engine.bases.add("A", KIND_AIRPORT, 0.0, 0.0)
        snap = self.engine.bases.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertIn("pads", snap[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
