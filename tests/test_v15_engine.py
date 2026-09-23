"""
Интеграция переработки v15 в движке: режим службы «Стоянка / В бой»,
логистика авианосца, урон и уничтожение баз, след аварии на месте гибели.

Тестируется на полном стеке с имитатором сервера (как test_engine.py), потому
что здесь важны именно команды, уходящие в мир, и связка движок ↔ базы.
"""
from __future__ import annotations

import unittest

from rwf.bases import KIND_AIRPORT, KIND_CARRIER, KIND_GROUND, SUPPLY_FLOOR
from rwf.config import AppConfig
from rwf.engine import UnitEngine
from rwf.mock_server import MockMCServer
from rwf.rcon import CommandQueue, RCONPool
from rwf.units import build_unit
from rwf.weapons import WeaponSystem
from rwf.world import World

HOST = "127.0.0.1"


class Stack:
    def __init__(self, tick: float = 0.1):
        self.server = MockMCServer(host=HOST, port=0, password="2203").start()
        self.pool = RCONPool(HOST, self.server.port, "2203", size=3, timeout=5.0)
        self.queue = CommandQueue(self.pool, workers=1, batch_size=32, rate=0)
        self.queue.start()
        self.world = World()
        self.cfg = AppConfig()
        self.cfg.sim.tick = tick
        self.cfg.sim.model_update_hz = 0.0
        self.weapons = WeaponSystem(self.cfg.combat, self.queue, self.world,
                                    bus=self.world.bus)
        self.engine = UnitEngine(self.world, self.queue, self.weapons,
                                 cfg=self.cfg)
        self.logs = []
        self.world.bus.subscribe("log", lambda *a: self.logs.append(a))

    def close(self):
        self.engine.stop(join_timeout=2.0)
        self.queue.stop(timeout=2.0, flush=False)
        self.pool.close()
        self.server.stop()

    def flush(self, timeout: float = 8.0) -> bool:
        return self.queue.flush(timeout=timeout)


class TestDutyToggle(unittest.TestCase):
    """Тумблер «Стоянка / В бой» из пульта (UX-15)."""

    def setUp(self):
        self.st = Stack()
        self.eng = self.st.engine
        self.air = self.eng.bases.add("Аэродром", KIND_AIRPORT, 0.0, 0.0)
        # вертолётные площадки: аэродром винтокрылые НЕ принимает (ACCEPTS)
        self.ground = self.eng.bases.add("Гарнизон", KIND_GROUND, 600.0, 0.0)

    def tearDown(self):
        self.st.close()

    def test_default_duty_is_combat(self):
        u = self.eng.spawn("attacker", (0.0, 150.0, 400.0), speed=60.0)
        self.assertEqual(self.eng.duty(u.id), "combat")
        self.assertEqual(u.duty, "combat")
        self.assertIn("duty", u.snapshot())

    def test_park_requests_recovery(self):
        u = self.eng.spawn("attacker", (0.0, 120.0, 600.0), speed=60.0,
                           base_id=self.air.id)
        self.assertTrue(self.eng.set_duty(u.id, "park"))
        self.assertEqual(self.eng.duty(u.id), "park")
        self.assertEqual(u.duty, "park")
        self.assertIsNotNone(self.eng.recovery_info(u.id),
                             "standing duty не начал заход на посадку")

    def test_park_then_lands_and_is_serviced(self):
        """Стоянка = автовозврат + авто-ТО (ручные кнопки больше не нужны)."""
        u = self.eng.spawn("attacker", (0.0, 80.0, 400.0), speed=50.0,
                           base_id=self.air.id)
        u.fuel = u.spec.fuel_max * 0.2
        self.eng.set_duty(u.id, "park")
        for _ in range(1200):
            self.eng.tick_once(0.1)
            if u.id in getattr(self.eng, "_parked", {}):
                break
        parked = self.eng.parked_info(u.id)
        self.assertIsNotNone(parked, "машина не села на базу")
        self.assertEqual(u.status, "parked")
        self.assertEqual(u.fuel, u.spec.fuel_max, "авто-ТО не заправило")

    def test_combat_launches_parked_unit(self):
        node = self.ground.recovery_node("helicopter")
        u = self.eng.spawn("attack_heli", (node.x, node.y + 3.0, node.z),
                           heading=node.heading, speed=0.0,
                           base_id=self.ground.id)
        self.eng.set_duty(u.id, "park")
        for _ in range(20):
            self.eng.tick_once(0.25)
            if self.eng.parked_info(u.id):
                break
        self.assertIsNotNone(self.eng.parked_info(u.id),
                             "вертолёт не сел на площадку по «Стоянке»")
        self.assertTrue(self.eng.set_duty(u.id, "combat"))
        self.eng.tick_once(0.1)
        self.assertIsNone(self.eng.parked_info(u.id), "не взлетел по «В бой»")
        self.assertIsNotNone(self.eng.get_ai(u.id),
                             "«В бой» без маршрута должен дать патруль")

    def test_combat_restores_operator_route(self):
        """«В бой» возвращает маршрут оператора, а не подменяет патрулём."""
        from rwf.routes import Action, Route, Waypoint
        u = self.eng.spawn("attack_heli", (0.0, 90.0, 0.0), speed=40.0)
        route = Route([Waypoint(x=500.0, z=500.0, altitude=120.0,
                                action=Action.NAVIGATE)], u.id,
                      name="оператора")
        self.eng.assign_route(u.id, route)
        self.eng.set_duty(u.id, "park")        # маршрут снят и запомнен
        self.eng.set_duty(u.id, "combat")
        back = self.eng.world.get_route(u.id)
        self.assertIsNotNone(back, "маршрут оператора не восстановлен")
        self.assertEqual(back.name, "оператора")
        self.assertIsNone(self.eng.get_ai(u.id),
                          "патруль не должен затирать маршрут оператора")

    def test_combat_without_route_gets_patrol(self):
        u = self.eng.spawn("attacker", (0.0, 150.0, 0.0), speed=60.0)
        self.eng.set_duty(u.id, "combat")
        self.assertIsNotNone(self.eng.get_ai(u.id), "нет патруля вокруг взлёта")

    def test_duty_on_dead_unit_is_safe(self):
        u = self.eng.spawn("attacker", (0.0, 150.0, 0.0), speed=60.0)
        self.eng.despawn(u.id)
        self.assertFalse(self.eng.set_duty(u.id, "park"))


class TestCarrierLogistics(unittest.TestCase):
    """Авианосец без своей генерации: снабжение привозит транспортник."""

    def setUp(self):
        self.st = Stack()
        self.eng = self.st.engine
        self.air = self.eng.bases.add("Аэродром", KIND_AIRPORT, -800.0, 0.0)
        self.car = self.eng.bases.add("Авианосец", KIND_CARRIER, 0.0, 0.0)

    def tearDown(self):
        self.st.close()

    def test_carrier_supply_does_not_regen(self):
        self.car.supply = 40.0
        self.eng.bases.step_supply(300.0)
        self.assertEqual(self.car.supply, 40.0)

    def test_transport_delivery_refills_carrier(self):
        t = self.eng.spawn_from_base(self.air.id, "transport", 200.0)
        t.cargo = t.spec.cargo_max
        self.assertGreater(t.cargo, 0.0)
        self.car.supply = 10.0
        before = self.car.supply
        gained = self.eng.deliver_cargo(t.id, self.car.id)
        self.assertGreater(gained, 0.0)
        self.assertAlmostEqual(self.car.supply, before + gained)
        self.assertEqual(t.cargo, 0.0, "груз не списан с борта")

    def test_delivery_on_landing_is_automatic(self):
        """Сел на палубу с грузом — снабжение пополнилось БЕЗ ручной команды."""
        node = self.car.recovery_node("transport")
        t = self.eng.spawn("transport", (node.x, node.y + 3.0, node.z),
                           heading=node.heading, speed=0.0)
        t.cargo = t.spec.cargo_max
        self.car.supply = 10.0
        self.assertTrue(self.eng.request_recovery(t.id, self.car.id))
        for _ in range(20):
            self.eng.tick_once(0.25)
            if self.eng.parked_info(t.id):
                break
        self.assertIsNotNone(self.eng.parked_info(t.id),
                             "транспортник не сел на палубу")
        self.assertGreater(self.car.supply, 10.0,
                           "груз не ушёл в снабжение авианосца")
        self.assertEqual(t.cargo, 0.0, "груз остался на борту")

    def test_full_warehouse_does_not_eat_cargo(self):
        """Склад полон — тонны НЕ сгорают впустую, их можно увезти дальше."""
        t = self.eng.spawn_from_base(self.air.id, "transport", 200.0)
        t.cargo = 4.0
        self.car.supply = self.car.supply_max
        self.assertEqual(self.eng.deliver_cargo(t.id, self.car.id), 0.0)
        self.assertEqual(t.cargo, 4.0)

    def test_empty_hold_delivers_nothing(self):
        t = self.eng.spawn_from_base(self.air.id, "transport", 200.0)
        t.cargo = 0.0
        self.assertEqual(self.eng.deliver_cargo(t.id, self.car.id), 0.0)

    def test_carrier_deficit_blocks_service(self):
        self.car.supply = SUPPLY_FLOOR
        u = self.eng.spawn("attack_heli", (0.0, 66.0, 20.0),
                           base_id=self.car.id)
        u.fuel = 1.0
        done = self.eng.bases.service_at(self.car, u)
        self.assertTrue(any("заправка" in d and "%" in d for d in done),
                        f"ожидали частичную заправку, получили {done}")
        self.assertLess(u.fuel, u.spec.fuel_max)


class TestBaseDestruction(unittest.TestCase):
    def setUp(self):
        self.st = Stack()
        self.eng = self.st.engine
        self.base = self.eng.bases.add("Гарнизон", KIND_GROUND, 0.0, 0.0)

    def tearDown(self):
        self.st.close()

    def test_blast_damages_base(self):
        destroyed = self.eng.damage_bases((0.0, 64.0, 0.0), 60.0, 200.0,
                                          "бомба")
        self.assertEqual(destroyed, [])
        self.assertLess(self.base.health, self.base.health_max)

    def test_blast_destroys_base_and_clears_pads(self):
        u = self.eng.spawn("apc", (10.0, 65.0, 10.0), base_id=self.base.id)
        self.assertIsNotNone(self.eng.parked_info(u.id) or u.base_id)
        destroyed = self.eng.damage_bases((0.0, 64.0, 0.0), 200.0, 10 ** 6,
                                          "бомба")
        self.assertEqual(destroyed, [self.base.id])
        self.assertTrue(self.base.destroyed)
        self.assertTrue(all(p.occupied_by is None for p in self.base.pads))
        self.assertTrue(self.st.flush())
        self.assertGreater(len(self.eng.wreckage.sites()), 0,
                           "на месте базы не осталось обломков")

    def test_destroyed_base_not_chosen_for_recovery(self):
        self.base.take_damage(10 ** 6, "тест")
        u = self.eng.spawn("attack_heli", (0.0, 80.0, 300.0))
        self.assertFalse(self.eng.request_recovery(u.id))

    def test_far_blast_does_not_touch_base(self):
        self.eng.damage_bases((9000.0, 64.0, 9000.0), 30.0, 500.0)
        self.assertEqual(self.base.health, self.base.health_max)


class TestWreckIntegration(unittest.TestCase):
    def setUp(self):
        self.st = Stack()
        self.eng = self.st.engine

    def tearDown(self):
        self.st.close()

    def test_crash_leaves_burning_site(self):
        u = self.eng.spawn("attacker", (0.0, 70.0, 0.0), speed=30.0)
        u.set_target(pitch=45.0, throttle=1.0)
        for _ in range(200):
            self.eng.tick_once(0.1)
            if not u.alive:
                break
        self.eng.tick_once(0.1)
        self.st.flush()
        sites = self.eng.wreckage.sites()
        self.assertEqual(len(sites), 1)
        self.assertTrue(sites[0].burning)
        self.assertGreater(len(sites[0].hull), 0)
        self.assertGreater(len(sites[0].fire), 0)
        cmds = self.st.server.command_log
        self.assertTrue(any(c.startswith("summon minecraft:falling_block")
                            for c in cmds), "нет падающих обломков")
        self.assertTrue(any("minecraft:fire" in c for c in cmds),
                        "зона не горит")
        self.assertTrue(any("minecraft:coal_block" in c or
                            "minecraft:polished_blackstone" in c
                            for c in cmds), "нет сгоревшего остова")

    def test_supply_ticked_by_engine(self):
        base = self.eng.bases.add("Аэродром", KIND_AIRPORT, 0.0, 0.0)
        base.supply = 10.0
        for _ in range(20):
            self.eng.tick_once(0.5)
        self.assertGreater(base.supply, 10.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
