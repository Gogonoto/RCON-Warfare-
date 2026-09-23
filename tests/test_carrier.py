"""Авианосец: блочная палуба, плавание, унифицированная нода приёма техники.

Проверяет задел v13:
* чертёж палубы и его дифф-синхронизация через движок;
* движение базы к цели с доворотом носа (heading = yaw Minecraft);
* стоянки в локальных осях — поворачиваются вместе с кораблём;
* RecoveryNode: геометрический контракт захода для всех типов баз;
* приём на посадку (вертолёт/самолёт), стоянка, обслуживание;
* припаркованная техника едет вместе с палубой;
* взлёт со стоянки: вертолёт — отрыв, самолёт — катапульта;
* гибель на стоянке освобождает место.
"""
from __future__ import annotations

import math
import time
import unittest

from rwf.bases import (CARRIER_DECK_Y, KIND_AIRPORT, KIND_CARRIER, KIND_GROUND,
                       Base, BaseManager, BasePad, RecoveryNode, angle_diff,
                       forward_vec, right_vec, yaw_to)
from rwf.config import AppConfig
from rwf.engine import UnitEngine
from rwf.mock_server import MockMCServer
from rwf.model import BLUEPRINTS, get_blueprint, project
from rwf.rcon import CommandQueue, RCONPool
from rwf.routes import Action, Route, Waypoint
from rwf.units import build_unit
from rwf.weapons import WeaponSystem
from rwf.world import World

HOST = "127.0.0.1"


class Stack:
    """Полный стек с имитатором сервера (как в test_engine)."""

    def __init__(self, tick: float = 0.1, model_hz: float = 0.0):
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
        self.engine = UnitEngine(self.world, self.queue, self.weapons,
                                 cfg=self.cfg)
        self.engine.carrier_sync_interval = 0.0

    def close(self):
        self.engine.stop(join_timeout=2.0)
        self.queue.stop(timeout=2.0, flush=False)
        self.pool.close()
        self.server.stop()

    def flush(self, timeout: float = 8.0) -> bool:
        return self.queue.flush(timeout=timeout)

    def blocks(self) -> dict:
        return dict(self.server.blocks)


# ---------------------------------------------------------------------------
#  Геометрия и соглашение курса
# ---------------------------------------------------------------------------
class TestHeadingConvention(unittest.TestCase):
    def test_forward_is_minecraft_yaw(self):
        """yaw 0 = юг (+Z), 90 = запад (−X) — как у юнитов."""
        fx, fz = forward_vec(0.0)
        self.assertAlmostEqual(fx, 0.0, places=6)
        self.assertAlmostEqual(fz, 1.0, places=6)
        fx, fz = forward_vec(90.0)
        self.assertAlmostEqual(fx, -1.0, places=6)
        self.assertAlmostEqual(fz, 0.0, places=6)

    def test_right_is_perpendicular(self):
        for h in (0.0, 37.0, 180.0, 271.0):
            fx, fz = forward_vec(h)
            rx, rz = right_vec(h)
            self.assertAlmostEqual(fx * rx + fz * rz, 0.0, places=6)

    def test_yaw_to_roundtrip(self):
        for h in (0.0, 45.0, 137.0, 270.0):
            fx, fz = forward_vec(h)
            self.assertAlmostEqual(angle_diff(yaw_to(fx, fz), h), 0.0, places=4)

    def test_angle_diff_shortest_way(self):
        self.assertAlmostEqual(angle_diff(350.0, 10.0), -20.0, places=6)
        self.assertAlmostEqual(angle_diff(10.0, 350.0), 20.0, places=6)


class TestPadsLocal(unittest.TestCase):
    def test_pads_rotate_with_base(self):
        """Смещения стоянок локальные: при повороте базы едут с ней."""
        b = Base(id=1, name="C", kind=KIND_CARRIER, x=0.0, z=0.0, heading=0.0)
        pad = b.pads[0]
        x0, z0, h0 = pad.world(b)
        b.heading = 90.0
        x1, z1, h1 = pad.world(b)
        self.assertAlmostEqual(h1, 90.0, places=6)
        # локальные (fwd=-9, right=3): fwd=(-sin,cos), right=(cos,sin).
        # heading 0: fwd=(0,1), right=(1,0) -> (3, -9).
        # heading 90: fwd=(-1,0), right=(0,1) -> (9, 3).
        self.assertAlmostEqual(x0, 3.0, places=6)
        self.assertAlmostEqual(z0, -9.0, places=6)
        self.assertAlmostEqual(x1, 9.0, places=6)
        self.assertAlmostEqual(z1, 3.0, places=6)
        # мировые оффсеты в снимке пересчитаны
        d = b.to_dict()
        self.assertAlmostEqual(d["pads"][0]["heading"], 90.0, places=6)
        self.assertNotAlmostEqual(d["pads"][0]["offset"][0], x0 - b.x, places=3)

    def test_carrier_pads_along_deck_starboard(self):
        b = Base(id=1, name="C", kind=KIND_CARRIER, x=0.0, z=0.0)
        self.assertEqual(len(b.pads), 4)
        self.assertTrue(all(p.right == 3.0 for p in b.pads))
        self.assertTrue(all(abs(p.fwd) <= 12.0 for p in b.pads))

    def test_airport_pads_along_runway(self):
        b = Base(id=1, name="A", kind=KIND_AIRPORT, x=0.0, z=0.0)
        self.assertEqual(len(b.pads), 6)
        self.assertTrue(all(p.right == 12.0 for p in b.pads))

    def test_ground_pads_at_corners(self):
        b = Base(id=1, name="G", kind=KIND_GROUND, x=0.0, z=0.0)
        self.assertEqual(len(b.pads), 4)
        for p in b.pads:
            self.assertAlmostEqual(abs(p.fwd), 14.0, places=6)
            self.assertAlmostEqual(abs(p.right), 14.0, places=6)


class TestCarrierSailing(unittest.TestCase):
    def setUp(self):
        self.mgr = BaseManager()

    def test_moves_toward_target_and_turns_nose(self):
        c = self.mgr.add("Nimitz", KIND_CARRIER, 0.0, 0.0, heading=0.0)
        c.move_to((100.0, 0.0))            # восток = yaw 270
        for _ in range(400):
            c.step(0.25)
        self.assertIsNone(c.move_target)   # прибыл в радиусе станции — цель снята
        # финиш внутри радиуса прибытия, нос смотрит на точку (кривая погони)
        d = c.distance_to(100.0, 0.0)
        self.assertLessEqual(d, c.arrive_radius + 0.5)
        from rwf.bases import yaw_to
        bearing = yaw_to(100.0 - c.x, 0.0 - c.z)
        self.assertLess(abs(angle_diff(bearing, c.heading)), 20.0)

    def test_turn_rate_limits_rotation(self):
        c = self.mgr.add("C", KIND_CARRIER, 0.0, 0.0, heading=0.0)
        c.move_to((100.0, 0.0))
        c.step(1.0)                        # turn_rate=4 град/с
        self.assertLessEqual(abs(angle_diff(270.0, c.heading)), 90.0 - 4.0)

    def test_airport_is_not_movable(self):
        a = self.mgr.add("A", KIND_AIRPORT, 0.0, 0.0)
        self.assertFalse(a.movable)
        a.move_to((50.0, 50.0))
        self.assertIsNone(a.move_target)
        self.assertEqual(a.step(1.0), 0.0)

    def test_manager_step_reports_moved(self):
        c = self.mgr.add("C", KIND_CARRIER, 0.0, 0.0)
        c.move_to((100.0, 0.0))
        moved = self.mgr.step(1.0)
        self.assertIn(c.id, moved)
        self.assertGreater(moved[c.id], 0.0)


# ---------------------------------------------------------------------------
#  RecoveryNode — унифицированная нода приёма
# ---------------------------------------------------------------------------
class TestRecoveryNode(unittest.TestCase):
    def setUp(self):
        self.mgr = BaseManager()

    def test_carrier_deck_node_geometry(self):
        c = self.mgr.add("C", KIND_CARRIER, 0.0, 0.0, heading=0.0)
        node = c.recovery_node("helicopter")
        self.assertIsNotNone(node)
        self.assertEqual(node.kind, "deck")
        self.assertAlmostEqual(node.y, CARRIER_DECK_Y, places=6)
        # точка касания позади центра вдоль курса (heading 0 => +Z вперёд)
        self.assertAlmostEqual(node.x, 0.0, places=6)
        self.assertLess(node.z, 0.0)
        ax, az, aalt = node.approach_point()
        self.assertLess(az, node.z)                 # прямая начинается южнее
        self.assertGreater(aalt, node.y)            # и выше точки касания
        self.assertAlmostEqual(aalt, node.y + node.approach_alt, places=6)

    def test_aircraft_gets_long_approach(self):
        c = self.mgr.add("C", KIND_CARRIER, 0.0, 0.0)
        heli = c.recovery_node("helicopter")
        ac = c.recovery_node("aircraft")
        self.assertGreater(ac.approach_dist, heli.approach_dist)
        self.assertGreater(ac.max_touchdown_speed, 50.0)

    def test_airport_runway_node(self):
        a = self.mgr.add("A", KIND_AIRPORT, 0.0, 0.0, heading=90.0)
        node = a.recovery_node("aircraft")
        self.assertEqual(node.kind, "runway")
        self.assertAlmostEqual(node.heading, 90.0, places=6)
        self.assertIsNone(a.recovery_node("helicopter"))   # аэродром не вертолётный

    def test_ground_base_helipad_only_for_rotary(self):
        g = self.mgr.add("G", KIND_GROUND, 0.0, 0.0)
        node = g.recovery_node("helicopter")
        self.assertEqual(node.kind, "helipad")
        self.assertIsNone(g.recovery_node("aircraft"))
        self.assertIsNone(g.recovery_node("tank"))         # танк не летает

    def test_node_to_dict_has_contract_fields(self):
        c = self.mgr.add("C", KIND_CARRIER, 0.0, 0.0)
        d = c.recovery_node("drone").to_dict()
        for key in ("base_id", "kind", "x", "z", "y", "heading",
                    "approach_dist", "approach_alt", "max_touchdown_speed"):
            self.assertIn(key, d)


# ---------------------------------------------------------------------------
#  Чертёж палубы
# ---------------------------------------------------------------------------
class TestCarrierBlueprint(unittest.TestCase):
    def test_blueprint_registered(self):
        bp = get_blueprint("carrier")
        self.assertIn("carrier", BLUEPRINTS)
        self.assertEqual(bp.length, 24)
        self.assertEqual(bp.width, 10)
        self.assertGreater(len(bp), 250)      # палуба + остров + разметка

    def test_projection_puts_deck_at_given_y(self):
        bp = get_blueprint("carrier")
        cells = project(bp, (100.0, 64.0, 200.0), 0.0)
        ys = {k[1] for k in cells}
        self.assertIn(64, ys)                  # палуба
        self.assertGreater(max(ys), 68)        # остров выше палубы
        xs = [k[0] for k in cells]
        self.assertTrue(min(xs) >= 100 - 6 and max(xs) <= 100 + 6)  # ширина 10


# ---------------------------------------------------------------------------
#  Движок: палуба в мире, посадка, стоянка, взлёт
# ---------------------------------------------------------------------------
class TestEngineCarrier(unittest.TestCase):
    def setUp(self):
        self.stack = Stack()
        self.engine = self.stack.engine

    def tearDown(self):
        self.stack.close()

    def _carrier(self, x=0.0, z=0.0, heading=0.0):
        return self.engine.bases.add("Nimitz", KIND_CARRIER, x, z,
                                     heading=heading, radius=90.0)

    def test_deck_blocks_appear_in_world(self):
        self._carrier()
        self.engine.tick_once(0.25)
        self.assertTrue(self.stack.flush())
        blocks = self.stack.blocks()
        self.assertGreater(len(blocks), 250)
        self.assertIn("minecraft:gray_concrete", blocks.values())
        ys = {pos[1] for pos in blocks}
        self.assertIn(int(CARRIER_DECK_Y - 1), ys)     # палуба под юнитами

    def test_deck_travels_with_ship(self):
        c = self._carrier()
        self.engine.tick_once(0.25)
        self.stack.flush()
        before = {pos[0] for pos in self.stack.blocks()}
        c.move_to((60.0, 0.0))
        for _ in range(60):
            self.engine.tick_once(0.25)
        self.stack.flush()
        after = {pos[0] for pos in self.stack.blocks()}
        self.assertGreater(c.x, 20.0)
        self.assertGreater(max(after), max(before) + 10)   # палуба уехала
        # хвоста из блоков не осталось: все блоки в районе корабля
        self.assertGreater(min(after), c.x - 30.0)

    def test_no_resync_when_standing_still(self):
        c = self._carrier()
        self.engine.tick_once(0.25)
        model = self.engine._carrier_models[c.id]
        w1 = model.total_writes
        for _ in range(10):
            self.engine.tick_once(0.25)
        self.assertEqual(model.total_writes, w1)   # квант позиции не изменился

    def test_clear_base_models_removes_deck(self):
        self._carrier()
        self.engine.tick_once(0.25)
        self.stack.flush()
        self.assertGreater(len(self.stack.blocks()), 0)
        self.engine.clear_base_models()
        self.stack.flush()
        self.assertEqual(self.stack.blocks(), {})

    def test_helicopter_lands_on_deck(self):
        c = self._carrier()
        node = c.recovery_node("helicopter")
        u = self.engine.spawn("attack_heli", (node.x + 5.0, node.y + 3.0, node.z),
                              heading=node.heading)
        u.speed = 0.0
        self.assertTrue(self.engine.request_recovery(u.id, c.id))
        self.engine.tick_once(0.25)
        info = self.engine.parked_info(u.id)
        self.assertIsNotNone(info)
        self.assertEqual(info["base_id"], c.id)
        self.assertEqual(u.status, "parked")
        self.assertAlmostEqual(u.pos[1], CARRIER_DECK_Y, places=6)
        self.assertEqual(u.fuel, u.spec.fuel_max)        # обслужен на палубе
        self.assertIsNone(self.stack.world.get_route(u.id))
        self.assertTrue(any(p.occupied_by == u.id for p in c.pads))

    def test_aircraft_lands_on_deck(self):
        c = self._carrier()
        node = c.recovery_node("aircraft")
        u = self.engine.spawn("attacker", (node.x, node.y + 4.0, node.z - 40.0),
                              heading=node.heading, speed=70.0)
        self.assertTrue(self.engine.request_recovery(u.id, c.id))
        # самолёт на короткой финале: долетает до окна захвата за пару тиков
        for _ in range(4):
            self.engine.tick_once(0.25)
            if self.engine.parked_info(u.id):
                break
        self.assertIsNotNone(self.engine.parked_info(u.id))
        self.assertEqual(u.status, "parked")
        self.assertLessEqual(u.speed, node.max_touchdown_speed)

    def test_touchdown_requires_capture_window(self):
        c = self._carrier()
        node = c.recovery_node("helicopter")
        # высоко над палубой — захвата нет
        u = self.engine.spawn("attack_heli", (node.x, node.y + 60.0, node.z))
        u.speed = 0.0
        self.engine.request_recovery(u.id, c.id)
        self.engine.tick_once(0.25)
        self.assertIsNone(self.engine.parked_info(u.id))
        self.assertIsNotNone(self.engine.recovery_info(u.id))

    def test_parked_rides_with_carrier(self):
        c = self._carrier()
        node = c.recovery_node("helicopter")
        u = self.engine.spawn("attack_heli", (node.x, node.y + 2.0, node.z))
        u.speed = 0.0
        self.engine.request_recovery(u.id, c.id)
        self.engine.tick_once(0.25)
        self.assertIsNotNone(self.engine.parked_info(u.id))
        x0 = u.pos[0]
        c.heading = 270.0                  # нос уже на восток — без доворота
        c.move_to((x0 + 40.0, 0.0))
        for _ in range(40):
            self.engine.tick_once(0.25)
        self.assertGreater(u.pos[0], x0 + 5.0)     # уехал вместе с палубой
        self.assertEqual(u.status, "parked")
        self.stack.flush()
        xs = {pos[0] for pos in self.stack.blocks() if pos[1] == int(CARRIER_DECK_Y)}
        self.assertTrue(any(abs(bx - u.pos[0]) < 8 for bx in xs))

    def test_launch_parked_helicopter(self):
        c = self._carrier()
        node = c.recovery_node("helicopter")
        u = self.engine.spawn("attack_heli", (node.x, node.y + 2.0, node.z))
        u.speed = 0.0
        self.engine.request_recovery(u.id, c.id)
        self.engine.tick_once(0.25)
        self.assertIsNotNone(self.engine.parked_info(u.id))
        self.assertTrue(self.engine.launch_parked(u.id))
        self.assertEqual(u.status, "flying")
        self.assertIsNone(self.engine.parked_info(u.id))
        self.assertTrue(all(p.occupied_by != u.id for p in c.pads))

    def test_launch_parked_aircraft_catapult(self):
        c = self._carrier()
        node = c.recovery_node("aircraft")
        u = self.engine.spawn("attacker", (node.x, node.y + 3.0, node.z),
                              heading=node.heading, speed=60.0)
        self.engine.request_recovery(u.id, c.id)
        self.engine.tick_once(0.25)
        self.assertIsNotNone(self.engine.parked_info(u.id))
        self.assertTrue(self.engine.launch_parked(u.id))
        self.assertGreaterEqual(u.speed, u.spec.stall_speed * 1.2)

    def test_spawn_from_carrier_catapults_aircraft(self):
        c = self._carrier()
        u = self.engine.spawn_from_base(c.id, "attacker", 65.0)
        self.assertIsNotNone(u)
        self.assertGreaterEqual(u.speed, u.spec.stall_speed * 1.2)
        self.assertGreaterEqual(u.pos[1], CARRIER_DECK_Y)

    def test_death_on_deck_frees_pad(self):
        c = self._carrier()
        node = c.recovery_node("helicopter")
        u = self.engine.spawn("attack_heli", (node.x, node.y + 2.0, node.z))
        u.speed = 0.0
        self.engine.request_recovery(u.id, c.id)
        self.engine.tick_once(0.25)
        self.assertIsNotNone(self.engine.parked_info(u.id))
        u.take_damage(99999.0, "test")
        self.engine.tick_once(0.25)
        self.assertTrue(all(p.occupied_by is None for p in c.pads))
        self.assertIsNone(self.engine.parked_info(u.id))

    def test_move_base_api(self):
        c = self._carrier()
        a = self.engine.bases.add("A", KIND_AIRPORT, 500.0, 500.0)
        self.assertTrue(self.engine.move_base(c.id, 120.0, 40.0))
        self.assertEqual(c.move_target, (120.0, 40.0))
        self.assertFalse(self.engine.move_base(a.id, 0.0, 0.0))
        self.assertFalse(self.engine.move_base(9999, 0.0, 0.0))

    def test_no_pad_no_landing(self):
        """Все стоянки заняты — заход отменяется, юнит не разбивается."""
        c = self._carrier()
        for i in range(len(c.pads)):
            c.pads[i].occupied_by = 1000 + i
        node = c.recovery_node("helicopter")
        u = self.engine.spawn("attack_heli", (node.x, node.y + 2.0, node.z))
        u.speed = 0.0
        self.assertFalse(self.engine.request_recovery(u.id, c.id))
        self.engine.tick_once(0.25)
        self.assertTrue(u.alive)


class TestAIAutoRecovery(unittest.TestCase):
    """Бот с пустым баком после завершения маршрута сам запрашивает посадку."""

    def setUp(self):
        self.stack = Stack()
        self.engine = self.stack.engine

    def tearDown(self):
        self.stack.close()

    def test_route_finished_triggers_recovery(self):
        from rwf.ai import BaseAI

        class DoneAI(BaseAI):
            """Не перестраивает маршрут и всегда «хочет на базу»."""
            name = "done"

            def build(self, unit, world):
                return None

            def needs_service(self, unit):
                self.reason = "тест"
                return True

        self.engine.bases.add("G", KIND_GROUND, 0.0, 0.0, radius=60.0)
        u = self.engine.spawn("attack_heli", (10.0, 70.0, 10.0), heading=0.0)
        u.speed = 0.0
        self.engine.set_ai(u.id, DoneAI())
        wp = Waypoint(x=u.pos[0], z=u.pos[2], altitude=70.0,
                      action=Action.NAVIGATE, pass_mode="precise", radius=30.0)
        route = Route([wp], u.id, name="test")
        self.stack.world.set_route(u.id, route)
        route.advance()                       # маршрут завершён
        self.assertTrue(route.done)
        self.engine.tick_once(0.25)           # _tick_ai видит done -> recovery
        self.assertTrue(self.engine.recovery_info(u.id) is not None
                        or self.engine.parked_info(u.id) is not None)


if __name__ == "__main__":
    unittest.main()
