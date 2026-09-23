"""Тесты ИИ: гибридный перепланировщик, фазы захода, шаблоны, сквозной прогон."""
from __future__ import annotations

import math
import time
import unittest

from rwf import mc
from rwf.ai import (AI_LABELS, AI_TEMPLATES, DEFAULT_AI_FOR_KIND, PROFILES,
                       BaseAI, FighterAI, KamikazeAI, PatrolAI, Phase, ReconAI,
                       StrikeAI, ai_labels, make_ai, make_ai_for_unit)
from rwf.config import AppConfig
from rwf.engine import UnitEngine
from rwf.mock_server import MockMCServer
from rwf.rcon import CommandQueue, RCONPool
from rwf.routes import Action, Route, RouteExecutor, Waypoint
from rwf.units import build_unit
from rwf.weapons import WeaponSystem
from rwf.world import World

HOST = "127.0.0.1"


class _AICase(unittest.TestCase):
    variant = "attacker"
    ai_kwargs: dict = {}
    template = "strike"

    def setUp(self):
        self.world = World()
        self.cfg = AppConfig()
        self.unit = build_unit(self.variant, "bot", pos=(0.0, 180.0, -600.0),
                               speed=35.0, throttle=0.9, is_bot=True)
        self.world.add_unit(self.unit)
        self.world.set_player("Victim", (0.0, 64.0, 0.0))
        self.world.set_base(0.0, -800.0)
        self.ai = make_ai(self.template, cfg=self.cfg, target_name="Victim",
                          **self.ai_kwargs)

    def move_target(self, x, y, z):
        self.world.set_player("Victim", (x, y, z))


class TestHybridReplanning(_AICase):
    """AI-01: перепланирование каждый тик обнуляло прогресс бота."""

    def test_first_update_returns_route(self):
        route = self.ai.update(self.unit, self.world, 0.25)
        self.assertIsNotNone(route)
        self.assertEqual(route.owner_kind, "bot")
        self.assertEqual(route.unit_id, self.unit.id)
        self.assertGreaterEqual(len(route), 3)

    def test_second_update_returns_none(self):
        """Главный регресс: без изменений новый маршрут НЕ создаётся."""
        first = self.ai.update(self.unit, self.world, 0.25)
        self.world.set_route(self.unit.id, first)
        second = self.ai.update(self.unit, self.world, 0.25)
        self.assertIsNone(second, "маршрут пересоздан на следующем же тике")
        self.assertIs(self.world.get_route(self.unit.id), first)

    def test_no_replan_while_target_still(self):
        first = self.ai.update(self.unit, self.world, 0.25)
        self.world.set_route(self.unit.id, first)
        for _ in range(10):
            self.assertIsNone(self.ai.update(self.unit, self.world, 0.25))
        self.assertEqual(self.ai.replans, 1)

    def test_replan_on_target_moved_beyond_threshold(self):
        self.ai.replan_threshold = 30.0
        self.ai.replan_interval = 1e9            # выключаем временной триггер
        first = self.ai.update(self.unit, self.world, 0.25)
        self.world.set_route(self.unit.id, first)
        self.move_target(0.0, 64.0, 10.0)        # сдвиг 10 < 30
        self.assertIsNone(self.ai.update(self.unit, self.world, 0.25))
        self.move_target(0.0, 64.0, 100.0)       # сдвиг 100 > 30
        self.ai.update(self.unit, self.world, 0.25)
        self.assertEqual(self.ai.replans, 2)

    def test_replan_on_interval(self):
        self.ai.replan_interval = 0.05
        self.ai.replan_threshold = 1e9           # выключаем триггер смещения
        first = self.ai.update(self.unit, self.world, 0.25)
        self.world.set_route(self.unit.id, first)
        self.assertIsNone(self.ai.update(self.unit, self.world, 0.25))
        time.sleep(0.08)
        self.ai.update(self.unit, self.world, 0.25)
        self.assertEqual(self.ai.replans, 2)

    def test_replan_preserves_progress(self):
        """Прогресс захода не сбрасывается при пересчёте (AI-01)."""
        first = self.ai.update(self.unit, self.world, 0.25)
        self.world.set_route(self.unit.id, first)
        first.current_idx = 1
        first.waypoints[0].reached = True
        self.ai.replan_interval = 0.0
        self.move_target(50.0, 64.0, 50.0)
        self.ai.update(self.unit, self.world, 0.25)
        route = self.world.get_route(self.unit.id)
        self.assertIs(route, first, "маршрут заменён вместо обновления точек")
        self.assertEqual(route.current_idx, 1)
        self.assertTrue(route.waypoints[0].reached)

    def test_new_route_when_previous_done(self):
        first = self.ai.update(self.unit, self.world, 0.25)
        self.world.set_route(self.unit.id, first)
        first.done = True
        self.ai.on_route_finished(self.unit, self.world)
        second = self.ai.update(self.unit, self.world, 0.25)
        self.assertIsNotNone(second)


class TestStrikeProfile(_AICase):
    def test_three_phases_with_own_altitudes(self):
        route = self.ai.update(self.unit, self.world, 0.25)
        wps = route.waypoints
        self.assertEqual(len(wps), 3)
        p = self.ai.profile
        self.assertEqual(wps[0].action, Action.NAVIGATE)
        self.assertAlmostEqual(wps[0].altitude, p.approach_alt)
        self.assertEqual(wps[1].action, p.action)
        self.assertAlmostEqual(wps[1].altitude, p.attack_alt)
        self.assertEqual(wps[2].action, Action.NAVIGATE)
        self.assertAlmostEqual(wps[2].altitude, p.escape_alt)
        self.assertEqual(self.ai.phase, Phase.APPROACH)

    def test_attack_point_is_the_target(self):
        self.move_target(120.0, 64.0, -40.0)
        route = self.ai.update(self.unit, self.world, 0.25)
        attack = route.waypoints[1]
        self.assertAlmostEqual(attack.x, 120.0)
        self.assertAlmostEqual(attack.z, -40.0)
        self.assertEqual(attack.target_name, "Victim",
                         "ударная точка не привязана к живой цели")

    def test_attack_point_follows_moving_target(self):
        route = self.ai.update(self.unit, self.world, 0.25)
        self.world.set_route(self.unit.id, route)
        self.move_target(300.0, 64.0, 300.0)
        self.ai.replan_interval = 0.0
        self.ai.update(self.unit, self.world, 0.25)
        attack = self.world.get_route(self.unit.id).waypoints[1]
        self.assertAlmostEqual(attack.x, 300.0)
        self.assertAlmostEqual(attack.z, 300.0)

    def test_approach_comes_from_current_side(self):
        """AI-10: заход со стороны юнита, а не разворот «спиной вперёд»."""
        self.unit.pos = (0.0, 180.0, -600.0)
        route = self.ai.update(self.unit, self.world, 0.25)
        entry = route.waypoints[0]
        # Юнит севернее цели (z = -600), значит и рубеж захода севернее
        self.assertLess(entry.z, 0.0, f"рубеж захода не со стороны юнита: {entry.z}")
        stand_off = math.hypot(entry.x, entry.z)
        self.assertAlmostEqual(stand_off, self.ai.profile.stand_off, delta=1.0)

    def test_approach_uses_heading_when_over_target(self):
        self.unit.pos = (0.0, 180.0, 5.0)      # почти над целью
        self.unit.yaw = 180.0                   # летим на север (−Z)
        route = self.ai.update(self.unit, self.world, 0.25)
        entry = route.waypoints[0]
        self.assertLess(entry.z, 0.0, "рубеж захода оказался позади по курсу")

    def test_escape_is_opposite_to_entry(self):
        """Зашли с одной стороны — уходим с другой, а не разворачиваемся."""
        route = self.ai.update(self.unit, self.world, 0.25)
        entry, escape = route.waypoints[0], route.waypoints[2]
        self.assertLess(entry.z * escape.z, 0,
                        "отход идёт в ту же сторону, что и заход")
        self.assertGreater(abs(escape.z), abs(entry.z) * 0.5)

    def test_passes_then_rtb(self):
        self.ai.pass_index = self.ai.profile.passes
        route = self.ai.update(self.unit, self.world, 0.25)
        self.assertEqual(len(route), 1)
        self.assertEqual(route.waypoints[0].action, Action.RTB)
        self.assertEqual(self.ai.phase, Phase.RTB)

    def test_low_fuel_goes_rtb(self):
        """AI-07: с пустым баком нельзя продолжать атаку."""
        self.unit.fuel = self.unit.spec.fuel_max * 0.05
        route = self.ai.update(self.unit, self.world, 0.25)
        self.assertEqual(route.waypoints[0].action, Action.RTB)
        self.assertIn("топливо", self.ai.reason)

    def test_no_ammo_goes_rtb(self):
        for m in self.unit.mounts:
            if m.category == "bomb":
                m.ammo = 0
        route = self.ai.update(self.unit, self.world, 0.25)
        self.assertEqual(route.waypoints[0].action, Action.RTB)
        self.assertIn("боезапас", self.ai.reason)

    def test_rtb_without_base_loiters(self):
        self.world.base = None
        self.world.launch_point = None
        self.unit.fuel = 1.0
        route = self.ai.update(self.unit, self.world, 0.25)
        self.assertEqual(self.ai.phase, Phase.LOITER)
        self.assertTrue(all(wp.action == Action.HOLD for wp in route.waypoints))
        self.assertTrue(route.loop)

    def test_target_lost_picks_nearest(self):
        """AI-11: назначенная цель вышла — берём ближайшего игрока."""
        self.world.remove_player("Victim")
        self.ai.target_name = "Gone"
        self.world.set_player("Near", (0.0, 64.0, -500.0))
        self.world.set_player("Far", (2000.0, 64.0, 2000.0))
        route = self.ai.update(self.unit, self.world, 0.25)
        self.assertEqual(self.ai.target_name, "Near")
        self.assertAlmostEqual(route.waypoints[1].x, 0.0)
        self.assertAlmostEqual(route.waypoints[1].z, -500.0)

    def test_no_players_loiters(self):
        self.world.players.clear()
        route = self.ai.update(self.unit, self.world, 0.25)
        self.assertEqual(self.ai.phase, Phase.LOITER)
        self.assertTrue(route.loop)

    def test_dead_unit_is_not_planned(self):
        self.unit.alive = False
        self.assertIsNone(self.ai.update(self.unit, self.world, 0.25))


class TestOtherTemplates(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.cfg = AppConfig()
        self.world.set_player("Victim", (0.0, 64.0, 0.0))
        self.world.set_base(0.0, -500.0)

    def _unit(self, variant, pos=(0.0, 200.0, -700.0)):
        u = build_unit(variant, "b", pos=pos, speed=40.0, is_bot=True)
        self.world.add_unit(u)
        return u

    def test_fighter_intercepts_with_lead(self):
        unit = self._unit("fighter")
        ai = make_ai("fighter", cfg=self.cfg, target_name="Victim")
        route = ai.update(unit, self.world, 0.25)
        self.assertEqual(route.waypoints[0].action, Action.MISSILE)
        self.assertEqual(route.waypoints[0].target_name, "Victim")
        # Точка пуска между юнитом и целью, а не за целью
        launch = route.waypoints[0]
        self.assertLess(launch.z, 0.0)
        self.assertGreater(launch.z, unit.pos[2])
        self.assertEqual(ai.phase, Phase.INTERCEPT)

    def test_fighter_lead_accounts_for_target_motion(self):
        unit = self._unit("fighter")
        ai = make_ai("fighter", cfg=self.cfg, target_name="Victim")
        self.world.set_player("Victim", (0.0, 64.0, 0.0), vel=(0.0, 0.0, 0.0))
        still = ai.update(unit, self.world, 0.25).waypoints[1]
        ai2 = make_ai("fighter", cfg=self.cfg, target_name="Victim")
        self.world.set_player("Victim", (0.0, 64.0, 0.0), vel=(40.0, 0.0, 0.0))
        moving = ai2.update(unit, self.world, 0.25).waypoints[1]
        self.assertNotAlmostEqual(still.x, moving.x, delta=1.0,
                                  msg="упреждение не учтено")

    def test_kamikaze_single_waypoint(self):
        unit = self._unit("kamikaze_drone")
        ai = make_ai("kamikaze", cfg=self.cfg, target_name="Victim")
        route = ai.update(unit, self.world, 0.25)
        self.assertEqual(len(route), 1)
        self.assertEqual(route.waypoints[0].action, Action.KAMIKAZE)
        self.assertFalse(route.loop)
        self.assertEqual(route.waypoints[0].target_name, "Victim")

    def test_kamikaze_ignores_fuel_and_ammo(self):
        unit = self._unit("kamikaze_drone")
        unit.fuel = 0.0
        for m in unit.mounts:
            m.ammo = 0
        ai = make_ai("kamikaze", cfg=self.cfg, target_name="Victim")
        route = ai.update(unit, self.world, 0.25)
        self.assertEqual(route.waypoints[0].action, Action.KAMIKAZE)

    def test_patrol_is_looped_and_stable(self):
        unit = self._unit("attack_heli")
        pts = [(100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
        ai = make_ai("patrol", cfg=self.cfg, waypoints=pts, altitude=90.0)
        route = ai.update(unit, self.world, 0.25)
        self.assertTrue(route.loop)
        self.assertEqual(len(route), 3)
        self.assertAlmostEqual(route.waypoints[0].altitude, 90.0)
        self.assertFalse(ai.should_replan(unit, self.world, time.monotonic()))

    def test_patrol_without_points_builds_a_box(self):
        unit = self._unit("attack_heli")
        ai = make_ai("patrol", cfg=self.cfg)
        route = ai.update(unit, self.world, 0.25)
        self.assertEqual(len(route), 4)
        self.assertTrue(route.loop)

    def test_recon_uses_recon_action(self):
        unit = self._unit("recon_drone")
        ai = make_ai("recon", cfg=self.cfg, waypoints=[(0.0, 0.0), (200.0, 0.0)])
        route = ai.update(unit, self.world, 0.25)
        self.assertTrue(all(wp.action == Action.RECON for wp in route.waypoints))
        self.assertGreater(route.waypoints[0].duration, 3.0)

    def test_rwf_profile_is_high(self):
        unit = self._unit("bomber")
        ai = make_ai("bomber", cfg=self.cfg, target_name="Victim")
        route = ai.update(unit, self.world, 0.25)
        self.assertGreater(route.waypoints[0].altitude, 200.0)
        self.assertGreater(route.waypoints[1].altitude, 150.0)

    def test_heli_profile_uses_precise_mode(self):
        unit = self._unit("attack_heli")
        ai = make_ai("heli", cfg=self.cfg, target_name="Victim")
        route = ai.update(unit, self.world, 0.25)
        self.assertEqual(route.waypoints[0].pass_mode, "precise")
        self.assertLess(route.waypoints[1].altitude, 80.0)


class TestRegistry(unittest.TestCase):
    def test_make_ai_all_templates(self):
        for key in AI_TEMPLATES:
            ai = make_ai(key)
            self.assertIsInstance(ai, BaseAI)
            self.assertTrue(ai.name)

    def test_make_ai_unknown_template(self):
        with self.assertRaises(KeyError) as ctx:
            make_ai("terminator")
        self.assertIn("strike", str(ctx.exception))

    def test_profiles_are_wired_to_templates(self):
        for key in ("strike", "gun_run", "rocket_run", "bomber", "heli", "drone"):
            ai = make_ai(key)
            self.assertIsInstance(ai, StrikeAI)
            self.assertIs(ai.profile, PROFILES[key])

    def test_labels_cover_all_templates(self):
        keys = {k for k, _v in ai_labels()}
        self.assertEqual(keys, set(AI_TEMPLATES))
        self.assertEqual(set(AI_LABELS), set(AI_TEMPLATES))
        for _k, label in ai_labels():
            self.assertTrue(any(ch.isalpha() for ch in label))

    def test_default_ai_for_kind(self):
        variants = {"attacker": "aircraft", "attack_heli": "helicopter",
                    "recon_drone": "drone", "mbt": "tank"}
        for variant, kind in variants.items():
            unit = build_unit(variant, "u")
            self.assertEqual(unit.spec.kind, kind)
            ai = make_ai_for_unit(unit)
            self.assertIsInstance(ai, BaseAI)
            self.assertIn(DEFAULT_AI_FOR_KIND[kind], AI_TEMPLATES)
        # Танк по умолчанию получает штурмовой шаблон (пушечный заход)
        self.assertIsInstance(make_ai_for_unit(build_unit("mbt", "t")), StrikeAI)

    def test_explicit_template_overrides_default(self):
        unit = build_unit("attacker", "u")
        ai = make_ai_for_unit(unit, template="kamikaze")
        self.assertIsInstance(ai, KamikazeAI)

    def test_ai_status_shape(self):
        ai = make_ai("strike", target_name="X")
        st = ai.status()
        for key in ("ai", "phase", "phase_label", "target", "pass", "replans"):
            self.assertIn(key, st)

    def test_phase_labels_cover_all_phases(self):
        for phase in (Phase.IDLE, Phase.APPROACH, Phase.ATTACK, Phase.ESCAPE,
                      Phase.INTERCEPT, Phase.RTB, Phase.LOITER, Phase.PATROL,
                      Phase.RECON, Phase.KAMIKAZE):
            self.assertIn(phase, Phase.LABELS)


class TestBotEndToEnd(unittest.TestCase):
    """Сквозной прогон: бот сам находит цель, заходит и бомбит."""

    def setUp(self):
        self.server = MockMCServer(host=HOST, port=0, password="2203",
                                   players=("Victim",), animate_players=False).start()
        self.pool = RCONPool(HOST, self.server.port, "2203", size=2, timeout=5.0)
        self.queue = CommandQueue(self.pool, workers=1, batch_size=32, rate=0)
        self.queue.start()
        self.world = World()
        self.cfg = AppConfig()
        self.cfg.sim.tick = 0.25
        self.weapons = WeaponSystem(self.cfg.combat, self.queue, self.world)
        self.engine = UnitEngine(self.world, self.queue, self.weapons, cfg=self.cfg)

    def tearDown(self):
        self.engine.stop(join_timeout=1.0)
        self.queue.stop(timeout=1.0, flush=False)
        self.pool.close()
        self.server.stop()

    def test_strike_bot_bombs_target(self):
        self.world.set_player("Victim", (0.0, 64.0, 400.0))
        self.world.set_base(0.0, -900.0)
        unit = self.engine.spawn("attacker", (0.0, 170.0, -600.0), heading=0.0,
                                 speed=35.0, throttle=0.9, is_bot=True)
        self.engine.set_ai(unit.id, make_ai("strike", cfg=self.cfg,
                                            target_name="Victim"))
        bombs_before = self.unit_ammo(unit)
        for _ in range(400):
            self.engine.tick_once(0.25)
            if self.unit_ammo(unit) < bombs_before:
                break
        self.queue.flush(timeout=8.0)
        self.assertLess(self.unit_ammo(unit), bombs_before,
                        f"бот не применил оружие: {self.engine.route_status(unit.id)}")
        self.assertTrue(self.server.count(r"^summon minecraft:tnt"),
                        "бомбы не дошли до сервера")
        st = self.engine.ai_status(unit.id)
        self.assertEqual(st["ai"].split(":")[0], "strike")
        self.assertGreater(st["replans"], 0)

    def test_kamikaze_bot_reaches_target(self):
        self.world.set_player("Victim", (0.0, 64.0, 300.0))
        unit = self.engine.spawn("kamikaze_drone", (0.0, 140.0, -200.0),
                                 heading=0.0, speed=30.0, is_bot=True)
        self.engine.set_ai(unit.id, make_ai("kamikaze", cfg=self.cfg,
                                           target_name="Victim"))
        for _ in range(400):
            self.engine.tick_once(0.25)
            if not unit.alive:
                break
        self.queue.flush(timeout=8.0)
        self.assertFalse(unit.alive, "камикадзе не дошёл до цели")
        self.assertEqual(unit.status, "kamikaze")
        self.assertTrue(self.server.count(r"^summon minecraft:tnt"))
        # Юнит убран из мира, блоки модели не остались
        self.assertEqual(self.engine.uids(), [])
        # v15: после гибели остаётся след аварии, но только он — блоки модели
        # камикадзе обязаны быть убраны (WRECK-01…).
        sites = self.engine.wreckage.sites()
        self.assertEqual(len(sites), 1)
        allowed = set(sites[0].hull) | set(sites[0].fire)
        self.assertTrue(set(self.server.blocks) <= allowed,
                        "в мире остались блоки модели юнита")

    def test_patrol_bot_keeps_flying(self):
        unit = self.engine.spawn("recon_drone", (0.0, 150.0, 0.0),
                                 speed=15.0, is_bot=True)
        self.engine.set_ai(unit.id, make_ai("patrol", cfg=self.cfg,
                                           waypoints=[(200.0, 0.0), (200.0, 200.0),
                                                      (0.0, 200.0)],
                                           altitude=150.0))
        for _ in range(160):
            self.engine.tick_once(0.25)
        self.assertTrue(unit.alive)
        self.assertGreater(unit.distance_flown, 100.0)
        self.assertEqual(self.engine.uids(), [unit.id])

    @staticmethod
    def unit_ammo(unit) -> int:
        return sum(m.ammo for m in unit.mounts if m.category == "bomb")


if __name__ == "__main__":
    unittest.main(verbosity=2)
