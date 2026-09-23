"""Тесты состояния мира: блокировки, снимки, маршруты, скан, маркеры."""
from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass
from typing import Any, Dict, List

from rwf.events import (
    TOPIC_PLAYER_ADDED, TOPIC_PLAYER_REMOVED, TOPIC_ROUTE_SET, TOPIC_UNIT_ADDED,
    EventBus,
)
from rwf.world import Marker, TerrainGrid, World


# Минимальные «юнит» и «маршрут» — повторяют контракт snapshot(),
# чтобы тесты не зависели от модулей, которые ещё пишутся.
#
# КОНТРАКТ: `snapshot()` обязан быть безопасным при одновременной мутации
# объекта из другого потока (своя блокировка) и обязан возвращать КОПИИ
# изменяемых структур. World.snapshot() вызывает его под своим локом, но
# RouteExecutor правит waypoints из потока юнита.
@dataclass
class FakeUnit:
    id: int = 0
    kind: str = "aircraft"
    is_bot: bool = False
    label: str = "Су-25"
    pos: tuple = (0.0, 100.0, 0.0)

    def snapshot(self) -> Dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "is_bot": self.is_bot,
                "label": self.label, "pos": self.pos}


@dataclass
class FakeWaypoint:
    x: float
    z: float
    action: str = "navigate"
    reached: bool = False


class FakeRoute:
    def __init__(self, waypoints: List[FakeWaypoint] | None = None):
        self.waypoints: List[FakeWaypoint] = waypoints or []
        self.unit_id: Any = None
        self.current_idx = 0
        self.loop = False
        self.owner_kind = "player"
        self.done = False
        self._lock = threading.RLock()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"unit_id": self.unit_id, "current_idx": self.current_idx,
                    "loop": self.loop, "owner_kind": self.owner_kind,
                    "done": self.done,
                    "waypoints": [dict(w.__dict__) for w in self.waypoints]}

    def add(self, wp: FakeWaypoint) -> None:
        with self._lock:
            self.waypoints.append(wp)

    def trim(self, n: int) -> None:
        with self._lock:
            del self.waypoints[:n]
            self.current_idx = max(0, self.current_idx - n)


class TestWorldBasics(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.world = World(bus=self.bus)

    def test_add_remove_unit_ids_unique(self):
        u1, u2 = FakeUnit(), FakeUnit()
        id1 = self.world.add_unit(u1)
        id2 = self.world.add_unit(u2)
        self.assertEqual((u1.id, u2.id), (id1, id2))
        self.assertNotEqual(id1, id2)
        self.world.remove_unit(id1)
        self.assertIsNone(self.world.get_unit(id1))
        self.assertEqual(self.world.unit_ids(), [id2])

    def test_route_cleared_with_none(self):
        """Регресс: `set_route(uid, None)` падал на `route.owner_kind`."""
        u = FakeUnit(is_bot=True)
        uid = self.world.add_unit(u)
        r = FakeRoute()
        self.world.set_route(uid, r)
        self.assertEqual(r.owner_kind, "bot")
        self.assertEqual(r.unit_id, uid)
        self.world.set_route(uid, None)          # не должно бросать исключение
        self.assertIsNone(self.world.get_route(uid))

    def test_removing_unit_drops_its_route(self):
        u = FakeUnit()
        uid = self.world.add_unit(u)
        self.world.set_route(uid, FakeRoute())
        self.world.remove_unit(uid)
        self.assertIsNone(self.world.get_route(uid))

    def test_snapshot_is_isolated(self):
        """Правка снимка не должна менять мир (и наоборот — гонка на чтении)."""
        u = FakeUnit()
        uid = self.world.add_unit(u)
        r = FakeRoute()
        r.add(FakeWaypoint(10, 20))
        self.world.set_route(uid, r)

        snap = self.world.snapshot()
        snap["units"][uid]["label"] = "ИСПОРЧЕНО"
        snap["routes"][uid]["waypoints"].append({"x": 0, "z": 0})
        snap["players"]["ghost"] = {"pos": (0, 0, 0)}

        self.assertEqual(self.world.get_unit(uid).label, "Су-25")
        self.assertEqual(len(self.world.get_route(uid).waypoints), 1)
        self.assertEqual(self.world.get_players(), {})

    def test_snapshot_survives_concurrent_route_mutation(self):
        """Регресс: 'list changed size during iteration' при живом маршруте."""
        u = FakeUnit()
        uid = self.world.add_unit(u)
        route = FakeRoute()
        for i in range(200):
            route.add(FakeWaypoint(i, i))
        self.world.set_route(uid, route)

        stop = threading.Event()
        errors: List[str] = []

        def mutator():
            while not stop.is_set():
                route.add(FakeWaypoint(1, 1))
                if len(route.waypoints) > 400:
                    route.trim(200)
                route.current_idx = len(route.waypoints) // 2

        def reader():
            for _ in range(300):
                try:
                    snap = self.world.snapshot()
                    len(snap["routes"][uid]["waypoints"])
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))
                    return

        t1 = threading.Thread(target=mutator)
        t2 = threading.Thread(target=reader)
        t1.start(); t2.start()
        t2.join(timeout=15)
        stop.set(); t1.join(timeout=5)
        self.assertEqual(errors, [])

    def test_players_sync_removes_offline(self):
        self.world.set_player("A", (1, 64, 1))
        self.world.set_player("B", (2, 64, 2))
        seen: List[str] = []
        self.bus.subscribe(TOPIC_PLAYER_REMOVED, lambda name: seen.append(name))
        self.world.sync_players(["A"])
        self.assertEqual(list(self.world.get_players()), ["A"])
        self.assertEqual(seen, ["B"])

    def test_player_added_event_once(self):
        got: List[str] = []
        self.bus.subscribe(TOPIC_PLAYER_ADDED, lambda name, pos: got.append(name))
        self.world.set_player("A", (1, 64, 1))
        self.world.set_player("A", (2, 64, 2))
        self.assertEqual(got, ["A"])
        self.assertEqual(self.world.get_player("A").pos, (2, 64, 2))

    def test_route_event_published(self):
        u = FakeUnit()
        uid = self.world.add_unit(u)
        got: List[int] = []
        self.bus.subscribe(TOPIC_ROUTE_SET, lambda u_: got.append(u_))
        self.world.set_route(uid, FakeRoute())
        self.world.set_route(uid, None)
        self.assertEqual(got, [uid, uid])

    def test_strike_zone_normalized_and_query(self):
        self.world.set_strike_zone(100, 100, -50, -20)
        self.assertEqual(self.world.strike_zone, (-50, -20, 100, 100))
        self.assertTrue(self.world.in_strike_zone(0, 0))
        self.assertFalse(self.world.in_strike_zone(500, 500))
        self.assertFalse(self.world.in_strike_zone(501, 0, margin=2))
        self.assertTrue(self.world.in_strike_zone(101, 0, margin=2))   # допуск у кромки
        self.assertFalse(self.world.in_strike_zone(103, 0, margin=2))
        self.world.clear_strike_zone()
        self.assertFalse(self.world.in_strike_zone(0, 0))

    def test_markers_ttl_and_limit(self):
        w = World(marker_limit=5)
        for i in range(20):
            w.add_marker(i, i, "bomb", ttl=60)
        self.assertEqual(len(w.markers), 5)
        w.add_marker(1, 1, "nuke", ttl=0.05)
        time.sleep(0.08)
        self.assertEqual(w.prune_markers(), 1)
        w.clear_markers("bomb")
        self.assertEqual(len(w.markers), 0)

    def test_marker_alive(self):
        m = Marker(0, 0, ttl=0.01)
        self.assertTrue(m.alive())
        time.sleep(0.02)
        self.assertFalse(m.alive())
        self.assertTrue(Marker(0, 0, ttl=0).alive())   # ttl=0 — вечный

    def test_revision_grows(self):
        r0 = self.world.revision
        self.world.set_waypoint(1, 2)
        self.assertGreater(self.world.revision, r0)
        r1 = self.world.revision
        self.world.clear_waypoint()
        self.assertGreater(self.world.revision, r1)
        r2 = self.world.revision
        self.world.clear_waypoint()                    # повторная очистка — no-op
        self.assertEqual(self.world.revision, r2)

    def test_scan_progress_is_atomic(self):
        """Регресс: read-modify-write из 6 потоков терял инкременты."""
        self.world.begin_scan(total=6000, step=8)
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait(timeout=5)
            for _ in range(1000):
                self.world.scan_add(1)

        ts = [threading.Thread(target=worker) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        self.assertEqual(self.world.scan_progress, (6000, 6000))
        self.assertEqual(self.world.end_scan(), 0)
        self.assertFalse(self.world.scanning)


class TestTerrainGrid(unittest.TestCase):
    def test_bounds_and_bulk_write(self):
        g = TerrainGrid(step=8)
        g.set_tiles([(x, z, 70, "grass_block")
                     for x in range(0, 64, 8) for z in range(0, 64, 8)])
        self.assertEqual(len(g), 64)
        self.assertEqual((g.min_x, g.min_z, g.max_x, g.max_z), (0, 0, 56, 56))
        snap = g.snapshot()
        self.assertEqual(snap["step"], 8)
        self.assertEqual(len(snap["tiles"]), 64)

    def test_height_at_nearest_tile(self):
        g = TerrainGrid(step=8)
        g.set_tiles([(0, 0, 64, "grass_block"), (8, 0, 90, "stone")])
        self.assertEqual(g.height_at(1, 1), 64)
        self.assertEqual(g.height_at(7.6, 0.4), 90)
        self.assertIsNone(TerrainGrid().height_at(0, 0))

    def test_clear(self):
        g = TerrainGrid()
        g.set_tile(0, 0, 64, "stone")
        g.clear()
        self.assertEqual(len(g), 0)
        self.assertIsNone(g.get(0, 0))


class TestThreadSafetyStress(unittest.TestCase):
    def test_mixed_concurrent_access(self):
        """Юниты/маршруты/маркеры/снимки из 8 потоков — без исключений."""
        world = World()
        errors: List[str] = []
        stop = threading.Event()

        def writer(n: int):
            try:
                for i in range(120):
                    u = FakeUnit(is_bot=(n % 2 == 0))
                    uid = world.add_unit(u)
                    world.set_route(uid, FakeRoute())
                    world.add_marker(i, i, "bomb", ttl=5)
                    world.set_player(f"p{n}", (i, 64, i))
                    world.snapshot()
                    world.set_route(uid, None)
                    world.remove_unit(uid)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"writer{n}: {exc!r}")
            finally:
                if n == 0:
                    stop.set()

        def reader():
            try:
                while not stop.is_set():
                    world.snapshot(include_terrain=True)
                    world.get_players()
                    world.prune_markers()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"reader: {exc!r}")

        ts = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        ts += [threading.Thread(target=reader) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(world.units, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
