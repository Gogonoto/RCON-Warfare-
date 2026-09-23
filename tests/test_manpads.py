"""ПЗРК игрока: захват цели триггером, пуск «Иглы», наведение на живой юнит.

Сценарий из игры: `/trigger rwf_lock` -> `/trigger rwf_missile`.
"""
from __future__ import annotations

import math
import time
import unittest

from rwf import mc
from rwf.combat import CombatSystem
from rwf.manpads import (MANPADS_CONE, MANPADS_RANGE, MANPADS_SPEC,
                         TRIGGER_LOCK, TRIGGER_MISSILE, ManpadsSystem)
from rwf.missiles import MissileManager, MissileUnit
from rwf.units import build_unit
from rwf.weapons import Target
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


class FakePool:
    """Отвечает на `scoreboard players get` из таблицы триггеров."""

    def __init__(self):
        self.commands = []
        self.scores = {}          # (player, objective) -> int

    def run(self, command: str) -> str:
        self.commands.append(command)
        parts = command.split()
        if len(parts) >= 4 and parts[:2] == ["scoreboard", "players"]:
            if parts[2] == "get":
                name, obj = parts[3], parts[4]
                return f"{name} has {self.scores.get((name, obj), 0)} in {obj}"
            if parts[2] == "set":
                name, obj = parts[3], parts[4]
                try:
                    self.scores[(name, obj)] = int(parts[5])
                except (IndexError, ValueError):
                    self.scores[(name, obj)] = 0
        return ""

    def set_trigger(self, player: str, trigger: str, value: int = 1) -> None:
        self.scores[(player, trigger)] = value


class TestManpadsLock(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.combat = CombatSystem(self.world)
        self.queue = FakeQueue()
        self.pool = FakePool()
        self.missiles = MissileManager(self.queue, self.combat,
                                       get_units=self.world.iter_units)
        self.sys = ManpadsSystem(self.world, self.missiles, pool=self.pool,
                                 queue=self.queue)
        # бот-вертолёт на востоке в 300 м
        self.bot = build_unit("attack_heli", "bot", pos=(300.0, 120.0, 0.0),
                              is_bot=True)
        self.world.add_unit(self.bot)
        # игрок в начале координат смотрит на восток (yaw 270)
        self.world.set_player("Arlik88", (0.0, 100.0, 0.0), yaw=270.0,
                              pitch=-4.0)
        self.rec = {"pos": (0.0, 100.0, 0.0), "yaw": 270.0, "pitch": -4.0}

    def test_lock_success(self):
        ev = self.sys.try_lock("Arlik88", self.rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["kind"], "lock")
        self.assertEqual(ev["unit_id"], self.bot.id)
        lock = self.sys.get_lock("Arlik88")
        self.assertIsNotNone(lock)
        self.assertEqual(lock.unit_id, self.bot.id)
        self.assertLess(lock.distance, 310.0)
        # actionbar-подтверждение ушло в очередь
        self.assertTrue(any("ЗАХВАТ" in c for c in self.queue.commands))

    def test_lock_out_of_cone(self):
        rec = dict(self.rec, yaw=90.0)        # смотрит на запад
        self.assertIsNone(self.sys.try_lock("Arlik88", rec))
        self.assertIsNone(self.sys.get_lock("Arlik88"))

    def test_lock_out_of_range(self):
        far = build_unit("attack_heli", "far",
                         pos=(MANPADS_RANGE + 100.0, 120.0, 0.0), is_bot=True)
        self.world.add_unit(far)
        rec = dict(self.rec)
        self.world.remove_unit(self.bot.id)   # остаётся только дальний бот
        self.assertIsNone(self.sys.try_lock("Arlik88", rec))

    def test_lock_ignores_ground_and_players(self):
        self.world.remove_unit(self.bot.id)
        tank = build_unit("mbt", "tank", pos=(100.0, 64.0, 0.0), is_bot=True)
        self.world.add_unit(tank)
        self.assertIsNone(self.sys.try_lock("Arlik88", self.rec))
        # не-бот (техника игрока) по умолчанию не цель ПЗРК
        self.world.remove_unit(tank.id)
        friendly = build_unit("attack_heli", "f", pos=(200.0, 120.0, 0.0),
                              is_bot=False)
        self.world.add_unit(friendly)
        self.assertIsNone(self.sys.try_lock("Arlik88", self.rec))

    def test_lock_prefers_axis_of_sight(self):
        """Два бота в конусе — захват по минимальному углу к взгляду."""
        self.world.remove_unit(self.bot.id)   # убираем цель, лежащую на оси
        on_axis = build_unit("recon_drone", "on", pos=(250.0, 110.0, 0.0),
                             is_bot=True)
        off_axis = build_unit("attack_heli", "off", pos=(250.0, 110.0, 80.0),
                              is_bot=True)
        self.world.add_unit(on_axis)
        self.world.add_unit(off_axis)
        ev = self.sys.try_lock("Arlik88", self.rec)
        self.assertEqual(ev["unit_id"], on_axis.id)

    def test_lock_expires(self):
        self.sys.try_lock("Arlik88", self.rec)
        lock = self.sys.locks["Arlik88"]
        lock.at = time.monotonic() - 3600.0    # «старый» захват
        self.assertIsNone(self.sys.get_lock("Arlik88"))

    def test_lock_dropped_when_target_dies(self):
        self.sys.try_lock("Arlik88", self.rec)
        self.bot.take_damage(99999.0, "test")
        self.assertIsNone(self.sys.get_lock("Arlik88"))


class TestManpadsLaunch(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.combat = CombatSystem(self.world)
        self.queue = FakeQueue()
        self.pool = FakePool()
        self.missiles = MissileManager(self.queue, self.combat,
                                       get_units=self.world.iter_units)
        self.sys = ManpadsSystem(self.world, self.missiles, pool=self.pool,
                                 queue=self.queue)
        self.bot = build_unit("attack_heli", "bot", pos=(300.0, 120.0, 0.0),
                              is_bot=True)
        self.world.add_unit(self.bot)
        self.world.set_player("Arlik88", (0.0, 100.0, 0.0), yaw=270.0,
                              pitch=-4.0)
        self.rec = {"pos": (0.0, 100.0, 0.0), "yaw": 270.0, "pitch": -4.0}

    def test_launch_creates_homing_missile(self):
        self.sys.try_lock("Arlik88", self.rec)
        ev = self.sys.try_launch("Arlik88", self.rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["kind"], "launch")
        self.assertEqual(len(self.missiles.missiles), 1)
        m = self.missiles.missiles[0]
        self.assertIs(m.homing_unit, self.bot)      # наведение на ЖИВОЙ юнит
        self.assertEqual(m.spec, MANPADS_SPEC)
        self.assertEqual(m.owner_label, "игрок Arlik88")
        self.assertEqual(self.sys.launches, 1)
        self.assertIsNone(self.sys.get_lock("Arlik88"))   # захват израсходован

    def test_launch_without_lock(self):
        self.assertIsNone(self.sys.try_launch("Arlik88", self.rec))
        self.assertEqual(self.sys.misses_no_lock, 1)
        self.assertEqual(self.missiles.missiles, [])

    def test_cooldown_blocks_second_launch(self):
        self.sys.try_lock("Arlik88", self.rec)
        self.assertIsNotNone(self.sys.try_launch("Arlik88", self.rec))
        self.sys.try_lock("Arlik88", self.rec)
        self.assertIsNone(self.sys.try_launch("Arlik88", self.rec))
        self.assertEqual(self.sys.misses_cooldown, 1)
        self.assertEqual(len(self.missiles.missiles), 1)

    def test_missile_hits_moving_target(self):
        """Ракета доводится по живой цели: бот уворачивается — ракета ловит."""
        self.sys.try_lock("Arlik88", self.rec)
        self.sys.try_launch("Arlik88", self.rec)
        self.assertEqual(len(self.missiles.missiles), 1)
        # бот уходит и плавно маневрирует; тикаем через менеджер — он же детонирует
        hit = False
        t = 0.0
        for _ in range(200):
            t += 0.05
            self.bot.pos = (300.0 + 10.0 * t,
                            120.0 + 8.0 * math.sin(0.9 * t),
                            22.0 * math.sin(0.5 * t))
            events = self.missiles.step(0.05)
            if any(e["kind"] == "hit" for e in events):
                hit = True
                break
        self.assertTrue(hit, "ракета ПЗРК должна догнать маневрирующую цель")
        self.assertEqual(self.missiles.hits, 1)
        self.assertLess(self.bot.health, self.bot.max_health)

    def test_poll_once_reads_triggers(self):
        """Полный цикл из игры: trigger -> lock -> trigger -> launch."""
        self.pool.set_trigger("Arlik88", TRIGGER_LOCK, 1)
        events = self.sys.poll_once()
        self.assertEqual([e["kind"] for e in events], ["lock"])
        # триггер сброшен
        self.assertEqual(self.pool.scores[("Arlik88", TRIGGER_LOCK)], 0)
        self.pool.set_trigger("Arlik88", TRIGGER_MISSILE, 1)
        events = self.sys.poll_once()
        self.assertEqual([e["kind"] for e in events], ["launch"])
        self.assertTrue(any("scoreboard objectives add rwf_lock" in c
                            for c in self.pool.commands))
        self.assertTrue(any("scoreboard objectives add rwf_missile" in c
                            for c in self.pool.commands))

    def test_snapshot_for_ui(self):
        self.sys.try_lock("Arlik88", self.rec)
        snap = self.sys.snapshot()
        self.assertIn("Arlik88", snap)
        self.assertEqual(snap["Arlik88"]["lock"]["unit_id"], self.bot.id)
        self.sys.try_launch("Arlik88", self.rec)
        snap = self.sys.snapshot()
        self.assertGreater(snap["Arlik88"]["cooldown"], 0.0)
        self.assertIsNone(snap["Arlik88"]["lock"])


class TestMissileHomingUnit(unittest.TestCase):
    """Ракета с homing_unit обновляет Target по живому юниту каждый тик."""

    def test_target_follows_unit(self):
        world = World()
        combat = CombatSystem(world)
        queue = FakeQueue()
        manager = MissileManager(queue, combat, get_units=world.iter_units)
        bot = build_unit("attack_heli", "bot", pos=(200.0, 100.0, 0.0),
                         is_bot=True)
        world.add_unit(bot)
        static = Target(pos=(200.0, 100.0, 0.0), name="bot", unit_id=bot.id)
        m = manager.launch((0.0, 100.0, 0.0), (90.0, 0.0, 0.0), static,
                           homing_unit=bot)
        bot.pos = (200.0, 160.0, 0.0)         # цель резко поднялась
        m.step(0.1)
        self.assertAlmostEqual(m.target.pos[1], 160.0, places=3)

    def test_dead_homing_unit_keeps_last_target(self):
        world = World()
        combat = CombatSystem(world)
        queue = FakeQueue()
        manager = MissileManager(queue, combat, get_units=world.iter_units)
        bot = build_unit("attack_heli", "bot", pos=(200.0, 100.0, 0.0),
                         is_bot=True)
        world.add_unit(bot)
        static = Target(pos=(200.0, 100.0, 0.0), name="bot", unit_id=bot.id)
        m = manager.launch((0.0, 100.0, 0.0), (90.0, 0.0, 0.0), static,
                           homing_unit=bot)
        bot.pos = (200.0, 130.0, 0.0)
        m.step(0.1)
        bot.alive = False
        bot.pos = (200.0, 0.0, 0.0)
        m.step(0.1)
        self.assertAlmostEqual(m.target.pos[1], 130.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
