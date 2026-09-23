"""Тесты маршрутов: достижение точек на скорости, действия, камикадзе, RTB."""
from __future__ import annotations

import math
import time
import unittest

from rwf import mc
from rwf.config import AppConfig
from rwf.engine import UnitEngine
from rwf.model import BlockModel
from rwf.mock_server import MockMCServer
from rwf.rcon import CommandQueue, RCONPool
from rwf.routes import (ACTION_COLORS, ACTION_LABELS, Action, Route,
                           RouteExecutor, Waypoint)
from rwf.units import build_unit
from rwf.weapons import PendingEffect, Target, WeaponSystem
from rwf.world import World

HOST = "127.0.0.1"


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

    def find(self, needle):
        return [c for c in self.commands if needle in c]


class _Harness:
    """Юнит + мир + оружие + исполнитель, без сервера (очередь-регистратор)."""

    def __init__(self, variant="attacker", pos=(0.0, 150.0, -400.0),
                 speed=30.0, **cfg_kw):
        self.world = World()
        self.queue = FakeQueue()
        self.cfg = AppConfig()
        for k, v in cfg_kw.items():
            setattr(self.cfg.combat, k, v)
        self.weapons = WeaponSystem(self.cfg.combat, self.queue, self.world,
                                    bus=self.world.bus)
        self.unit = build_unit(variant, "u", pos=pos, speed=speed, throttle=0.85)
        self.world.add_unit(self.unit)
        self.ex = RouteExecutor(self.unit, self.world, self.weapons, cfg=self.cfg)
        self.dt = 0.25

    def run(self, route, seconds, dt=None):
        """Крутить исполнитель и физику вместе."""
        dt = dt or self.dt
        self.world.set_route(self.unit.id, route)
        n = int(round(seconds / dt))
        for _ in range(n):
            self.ex.tick(route, dt)
            self.unit.step(dt, self.world)
            if not self.unit.alive or route.done:
                break
        return route


class TestWaypointAndRoute(unittest.TestCase):
    def test_waypoint_validates_action(self):
        with self.assertRaises(ValueError):
            Waypoint(0, 0, action="teleport")

    def test_waypoint_validates_pass_mode(self):
        with self.assertRaises(ValueError):
            Waypoint(0, 0, pass_mode="teleport")

    def test_pass_mode_auto_resolves_by_kind(self):
        jet = build_unit("attacker", "j")
        heli = build_unit("attack_heli", "h")
        tank = build_unit("mbt", "t")
        wp = Waypoint(0, 0, pass_mode="auto")
        self.assertEqual(wp.resolved_mode(jet), "fly")
        self.assertEqual(wp.resolved_mode(heli), "precise")
        self.assertEqual(wp.resolved_mode(tank), "precise")
        self.assertEqual(Waypoint(0, 0, pass_mode="precise").resolved_mode(jet),
                         "precise")

    def test_aircraft_radius_covers_one_tick(self):
        """Радиус точки не может быть меньше пути за тик."""
        jet = build_unit("fighter", "j")       # 68 м/с
        r = Waypoint(0, 0).resolved_radius(jet)
        self.assertGreaterEqual(r, jet.spec.max_speed * 0.5)

    def test_route_advance_and_finish(self):
        r = Route([Waypoint(i * 10, 0) for i in range(3)])
        self.assertEqual(r.current().x, 0)
        self.assertTrue(r.advance())
        self.assertEqual(r.current().x, 10)
        self.assertTrue(r.advance())
        self.assertFalse(r.done)
        self.assertFalse(r.advance())          # последняя точка
        self.assertTrue(r.done)
        self.assertIsNone(r.current())

    def test_route_loop_resets_flags(self):
        r = Route([Waypoint(0, 0), Waypoint(10, 0)], loop=True)
        r.waypoints[0].reached = True
        r.waypoints[0].action_done = True
        r.advance()
        r.advance()                            # замыкаем круг
        self.assertEqual(r.current_idx, 0)
        self.assertEqual(r.cycles, 1)
        self.assertFalse(r.done)
        self.assertFalse(r.waypoints[0].reached)

    def test_replace_waypoints_keeps_progress(self):
        """Основа фикса AI-01: перепланирование не сбрасывает прогресс."""
        r = Route([Waypoint(i, 0) for i in range(3)])
        r.current_idx = 2
        r.waypoints[0].reached = True
        r.waypoints[1].action_done = True
        r.replace_waypoints([Waypoint(100 + i, 0) for i in range(3)])
        self.assertEqual(r.current_idx, 2)
        self.assertTrue(r.waypoints[0].reached)
        self.assertTrue(r.waypoints[1].action_done)
        self.assertEqual(r.current().x, 102)

    def test_replace_waypoints_without_progress(self):
        r = Route([Waypoint(0, 0), Waypoint(10, 0)])
        r.current_idx = 1
        r.replace_waypoints([Waypoint(5, 5)], keep_progress=False)
        self.assertEqual(r.current_idx, 0)

    def test_insert_remove_move_keep_index_sane(self):
        r = Route([Waypoint(i, 0) for i in range(3)])
        r.current_idx = 1
        r.insert(0, Waypoint(-10, 0))
        self.assertEqual(r.current_idx, 2)
        removed = r.remove(0)
        self.assertEqual(removed.x, -10)
        self.assertEqual(r.current_idx, 1)
        self.assertTrue(r.move(0, 2))
        self.assertFalse(r.move(0, 99))
        self.assertIsNone(r.remove(99))

    def test_snapshot_is_a_copy(self):
        r = Route([Waypoint(1, 2, action=Action.BOMB)])
        snap = r.snapshot()
        snap["waypoints"][0]["x"] = 999
        snap["waypoints"].append({"x": 0})
        self.assertEqual(r.waypoints[0].x, 1)
        self.assertEqual(len(r), 1)

    def test_dict_round_trip(self):
        r = Route([Waypoint(10, 20, 150, Action.BOMB, count=3, duration=5.0,
                            pass_mode="fly", note="главная"),
                   Waypoint(30, 40, 90, Action.STRAFE)],
                  loop=True, name="Тестовый")
        data = r.to_dict()
        r2 = Route.from_dict(data)
        self.assertEqual(len(r2), 2)
        self.assertEqual(r2.name, "Тестовый")
        self.assertTrue(r2.loop)
        wp = r2.waypoints[0]
        self.assertEqual((wp.x, wp.z, wp.altitude, wp.action, wp.count,
                          wp.duration, wp.note),
                         (10, 20, 150, Action.BOMB, 3, 5.0, "главная"))

    def test_dict_drops_runtime_state(self):
        r = Route([Waypoint(0, 0)])
        r.waypoints[0].reached = True
        r.waypoints[0].action_done = True
        data = r.to_dict()
        self.assertNotIn("reached", data["waypoints"][0])
        self.assertFalse(Route.from_dict(data).waypoints[0].reached)

    def test_from_dict_rejects_foreign_format(self):
        with self.assertRaises(ValueError):
            Route.from_dict({"format": "something.else", "waypoints": []})
        with self.assertRaises(ValueError):
            Route.from_dict({"format": "rwf.route", "version": 99,
                             "waypoints": []})

    def test_actions_are_documented(self):
        for act in Action.ALL:
            self.assertIn(act, ACTION_LABELS)
            self.assertIn(act, ACTION_COLORS)
            self.assertTrue(ACTION_COLORS[act].startswith("#"))


class TestArrivalDetection(unittest.TestCase):
    """Главная регрессия наброска: ROUTE-01/ROUTE-02."""

    def setUp(self):
        self.h = _Harness(pos=(0.0, 150.0, -600.0), speed=60.0)

    def test_fast_aircraft_reaches_waypoint(self):
        """60 м/с при тике 0.5 с = 30 блоков за тик. Окно в 3 блока не работало."""
        h = _Harness(pos=(0.0, 150.0, -600.0), speed=60.0)
        h.dt = 0.5
        route = Route([Waypoint(0.0, 0.0, 150.0, Action.NAVIGATE,
                                pass_mode="fly"),
                       Waypoint(0.0, 600.0, 150.0, Action.NAVIGATE)])
        for _ in range(60):
            h.ex.tick(route, 0.5)
            h.unit.step(0.5, h.world)
            if route.current_idx > 0 or route.done:
                break
        self.assertGreater(route.current_idx, 0,
                           "самолёт на 60 м/с не отметил пролёт точки")
        self.assertTrue(route.waypoints[0].reached)
        self.assertFalse(route.waypoints[0].skipped)

    def test_extremely_fast_unit_still_registers(self):
        h = _Harness(variant="fighter", pos=(0.0, 250.0, -900.0), speed=68.0)
        h.dt = 1.0                     # 68 блоков за тик — хуже некуда
        route = Route([Waypoint(0.0, 0.0, 250.0), Waypoint(0.0, 900.0, 250.0)])
        for _ in range(40):
            h.ex.tick(route, 1.0)
            h.unit.step(1.0, h.world)
            if route.current_idx > 0:
                break
        self.assertGreater(route.current_idx, 0)

    def test_radius_arrival_for_precise_mode(self):
        h = _Harness(variant="attack_heli", pos=(0.0, 80.0, -40.0), speed=10.0)
        h.unit.set_target(throttle=0.5, pitch=0.0)
        route = Route([Waypoint(0.0, 0.0, 80.0, pass_mode="precise", radius=6.0),
                       Waypoint(0.0, 100.0, 80.0)])
        for _ in range(120):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
            if route.current_idx > 0:
                break
        self.assertGreater(route.current_idx, 0)
        self.assertTrue(route.waypoints[0].reached)

    def test_timeout_advances_instead_of_hanging(self):
        """ROUTE-03: зависнуть на точке навсегда нельзя."""
        h = _Harness(pos=(0.0, 150.0, 0.0), speed=0.0)
        h.ex.waypoint_timeout = 1.0
        route = Route([Waypoint(5000.0, 5000.0, 150.0), Waypoint(0.0, 0.0)])
        for _ in range(40):
            h.ex.tick(route, 0.25)
        self.assertTrue(route.waypoints[0].skipped)
        self.assertGreaterEqual(route.current_idx, 1)

    def test_off_course_unit_does_not_loop_forever(self):
        """Юнит летит мимо точки — маршрут всё равно должен продвигаться."""
        h = _Harness(pos=(0.0, 150.0, -300.0), speed=40.0)
        h.unit.yaw = 90.0                     # летит на запад, точка на юге
        route = Route([Waypoint(0.0, 0.0, 150.0), Waypoint(10.0, 10.0)])
        h.ex.waypoint_timeout = 3.0
        for _ in range(60):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
            if route.current_idx > 0:
                break
        self.assertGreater(route.current_idx, 0)


class TestActions(unittest.TestCase):
    def test_bomb_releases_before_target(self):
        """ROUTE-04: сброс ДО точки, на расчётной дистанции падения."""
        h = _Harness(pos=(0.0, 150.0, -500.0), speed=30.0)
        route = Route([Waypoint(0.0, 0.0, 150.0, Action.BOMB, count=2)])
        release_z = None
        for _ in range(120):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
            if h.queue.find("minecraft:tnt") and release_z is None:
                release_z = h.unit.pos[2]
            if route.done:
                break
        self.assertIsNotNone(release_z, "бомбы не сброшены")
        self.assertLess(release_z, -20.0,
                        f"сброс произошёл слишком поздно: z={release_z:.0f}")
        self.assertTrue(route.waypoints[0].action_done)

    def test_bomb_count_limits_volley(self):
        h = _Harness(pos=(0.0, 120.0, -200.0), speed=25.0)
        route = Route([Waypoint(0.0, 0.0, 120.0, Action.BOMB, count=1)])
        for _ in range(60):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
            if route.done:
                break
        mounts = [m for m in h.unit.mounts if m.category == "bomb"]
        spent = sum(m.ammo_max - m.ammo for m in mounts)
        self.assertLessEqual(spent, 4, f"израсходовано больше, чем count: {spent}")

    def test_strafe_is_time_limited_and_spends_ammo(self):
        """ROUTE-05: обстрел ограничен длительностью и боезапасом."""
        h = _Harness(pos=(0.0, 100.0, -150.0), speed=25.0)
        route = Route([Waypoint(0.0, 0.0, 100.0, Action.STRAFE, duration=2.0)])
        gun = next(m for m in h.unit.mounts if m.category == "cannon")
        before = gun.ammo
        for _ in range(80):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
            if route.done:
                break
        self.assertLess(gun.ammo, before, "обстрел не расходовал боезапас")
        self.assertTrue(route.waypoints[0].action_done)
        self.assertTrue(route.done or route.current_idx > 0)

    def test_missile_without_mount_skips_and_advances(self):
        """ROUTE-03: действие без подходящего подвеса не должно зависать."""
        h = _Harness(variant="recon_drone", pos=(0.0, 150.0, -200.0), speed=20.0)
        for m in h.unit.mounts:
            if m.category == "missile":
                m.load(None)                   # снимаем УР
        route = Route([Waypoint(0.0, 0.0, 150.0, Action.MISSILE),
                       Waypoint(0.0, 300.0, 150.0)])
        for _ in range(80):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
            if route.current_idx > 0:
                break
        self.assertGreater(route.current_idx, 0, "маршрут завис на точке без УР")
        self.assertTrue(route.waypoints[0].skipped)

    def test_hold_keeps_aircraft_flying(self):
        """ROUTE-08: удержание для самолёта — вираж, а не остановка в воздухе."""
        h = _Harness(pos=(0.0, 180.0, 0.0), speed=30.0)
        route = Route([Waypoint(0.0, 0.0, 180.0, Action.HOLD, duration=6.0,
                                pass_mode="precise", radius=400.0)])
        start_yaw = h.unit.yaw
        for _ in range(24):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
        self.assertGreater(h.unit.speed, 5.0, "самолёт остановился в воздухе")
        self.assertTrue(h.unit.alive)
        self.assertNotAlmostEqual(h.unit.yaw, start_yaw, delta=5.0,
                                  msg="виряж не выполняется")

    def test_hold_for_helicopter_hovers(self):
        h = _Harness(variant="attack_heli", pos=(0.0, 100.0, 0.0), speed=0.0)
        route = Route([Waypoint(0.0, 0.0, 100.0, Action.HOLD, duration=3.0,
                                pass_mode="precise", radius=20.0)])
        for _ in range(20):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
        self.assertTrue(h.unit.alive)
        self.assertAlmostEqual(h.unit.pos[1], 100.0, delta=6.0)

    def test_recon_leaves_markers(self):
        h = _Harness(pos=(0.0, 180.0, 0.0), speed=30.0)
        route = Route([Waypoint(0.0, 0.0, 180.0, Action.RECON, duration=4.0,
                                pass_mode="precise", radius=400.0)])
        for _ in range(40):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
        self.assertTrue(any(m.kind == "recon" for m in h.world.markers))

    def test_rtb_refuels_and_rearms(self):
        """ROUTE-15: возврат на базу реально обслуживает юнит."""
        h = _Harness(pos=(0.0, 150.0, -300.0), speed=30.0)
        h.unit.fuel = 10.0
        h.unit.mounts[0].consume(1)
        h.world.set_base(0.0, 0.0)
        route = Route([Waypoint(0.0, 0.0, 150.0, Action.RTB)])
        for _ in range(120):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
            if route.done:
                break
        # Обслуживание произошло; за тот же тик топливо уже чуть-чуть сожжено
        self.assertGreater(h.unit.fuel, h.unit.spec.fuel_max * 0.98)
        self.assertEqual(h.unit.mounts[0].ammo, h.unit.mounts[0].ammo_max)
        self.assertTrue(route.done)
        self.assertTrue(any(m.kind == "service" for m in h.world.markers))

    def test_rtb_without_base_reports(self):
        h = _Harness(pos=(0.0, 150.0, 0.0), speed=30.0)
        route = Route([Waypoint(0.0, 0.0, 150.0, Action.RTB)])
        h.ex.tick(route, 0.25)
        self.assertFalse(route.waypoints[0].action_done)
        self.assertIn("база не задана", " ".join(route.progress_log))

    def test_navigate_just_advances(self):
        h = _Harness(pos=(0.0, 150.0, -100.0), speed=30.0)
        route = Route([Waypoint(0.0, 0.0, 150.0), Waypoint(0.0, 400.0, 150.0)])
        for _ in range(60):
            h.ex.tick(route, 0.25)
            h.unit.step(0.25, h.world)
            if route.current_idx > 0:
                break
        self.assertEqual(route.current_idx, 1)


class TestKamikaze(unittest.TestCase):
    def test_kamikaze_destroys_unit_and_target_area(self):
        """ROUTE-06: реальный таран — взрыв, шахта, гибель юнита."""
        world = World()
        queue = FakeQueue()
        cfg = AppConfig()
        cfg.combat.destructive_confirmed = True
        cfg.combat.tunnel_enabled = True
        weapons = WeaponSystem(cfg.combat, queue, world)
        unit = build_unit("kamikaze_drone", "kam", pos=(0.0, 120.0, -200.0),
                          speed=30.0)
        world.add_unit(unit)
        world.set_player("Victim", (0.0, 20.0, 0.0))     # цель глубоко под землёй
        ex = RouteExecutor(unit, world, weapons, cfg=cfg)
        route = Route([Waypoint(0.0, 0.0, 22.0, Action.KAMIKAZE,
                                pass_mode="precise", radius=4.0,
                                target_name="Victim")])
        for _ in range(400):
            ex.tick(route, 0.2)
            unit.step(0.2, world)
            weapons.update(0.2)
            if not unit.alive:
                break
        self.assertFalse(unit.alive, "камикадзе не сдетонировал")
        self.assertEqual(unit.status, "kamikaze")
        self.assertTrue(queue.find("summon minecraft:tnt"), "взрыва не было")
        self.assertTrue(any(m.kind == "kamikaze" for m in world.markers))
        # Шахта до цели в пещере (WPN-01)
        fills = queue.find("fill")
        self.assertTrue(fills, "шахта до подземной цели не пробита")
        self.assertTrue(any(int(f.split()[2]) <= 22 for f in fills),
                        "шахта не дошла до глубины цели")


class TestRouteIntegration(unittest.TestCase):
    """Сквозная проверка: маршрут исполняется на настоящем стеке с сервером."""

    def setUp(self):
        self.server = MockMCServer(host=HOST, port=0, password="2203").start()
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

    def test_full_route_flies_and_bombs(self):
        self.world.set_player("Victim", (0.0, 64.0, 300.0))
        unit = self.engine.spawn("attacker", (0.0, 160.0, -500.0), heading=0.0,
                                 speed=35.0, throttle=0.9)
        route = Route([
            Waypoint(0.0, 0.0, 160.0, Action.NAVIGATE, note="рубеж"),
            Waypoint(0.0, 300.0, 100.0, Action.BOMB, count=2,
                     target_name="Victim", note="сброс"),
            Waypoint(0.0, 700.0, 200.0, Action.NAVIGATE, note="отход"),
        ], name="Интеграционный")
        self.engine.assign_route(unit.id, route)

        for _ in range(240):
            self.engine.tick_once(0.25)
            if route.done:
                break
        self.queue.flush(timeout=8.0)

        self.assertTrue(route.waypoints[0].reached, "первая точка не достигнута")
        self.assertTrue(route.waypoints[1].action_done,
                        f"бомбы не сброшены: {route.progress_log[-4:]}")
        self.assertTrue(route.done, f"маршрут не завершён: idx={route.current_idx}")
        self.assertTrue(self.server.count(r"^summon minecraft:tnt"),
                        "на сервер не ушло ни одной бомбы")
        self.assertGreater(unit.distance_flown, 900.0)
        # Модель убрана/перерисована без следа
        self.assertLessEqual(len(self.server.blocks), unit.model.placed_count + 2)

    def test_assign_route_clears_ai(self):
        """UI-08: назначенный вручную маршрут не должен затираться ботом."""
        from rwf.ai import make_ai
        unit = self.engine.spawn("attacker", (0.0, 160.0, 0.0), speed=30.0,
                                 is_bot=True)
        self.engine.set_ai(unit.id, make_ai("strike"))
        self.assertIsNotNone(self.engine.get_ai(unit.id))
        route = Route([Waypoint(100.0, 100.0, 150.0)], name="Ручной")
        self.engine.assign_route(unit.id, route)
        self.assertIsNone(self.engine.get_ai(unit.id))
        self.assertIs(self.world.get_route(unit.id), route)

    def test_route_status_report(self):
        unit = self.engine.spawn("attacker", (0.0, 160.0, 0.0), speed=30.0)
        route = Route([Waypoint(0.0, 300.0, 150.0, Action.BOMB),
                       Waypoint(0.0, 600.0, 150.0)], name="Статус")
        self.engine.assign_route(unit.id, route)
        st = self.engine.route_status(unit.id)
        self.assertTrue(st["has_route"])
        self.assertEqual(st["points"], 2)
        self.assertEqual(st["current"], 0)
        self.assertEqual(st["name"], "Статус")
        self.assertIn("state", st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
