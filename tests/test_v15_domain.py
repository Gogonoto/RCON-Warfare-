"""
Тесты переработки v15: статусы, экономика и прочность баз, разрушения,
навигация наземной техники по рельефу.

Закрывают требования заказчика из ЧАСТИ 1 (пункт «реальные состояния юнитов»)
и ЧАСТИ 2 (экономика баз + логистика авианосца, разрушения, наземная техника
с коллизией и приоритетом дорог).
"""
from __future__ import annotations

import math
import time
import unittest

from rwf import mc
from rwf.bases import (SUPPLY_FLOOR, SUPPLY_PER_TON, BaseManager,
                       KIND_AIRPORT, KIND_CARRIER, KIND_GROUND)
from rwf.groundnav import (GroundNav, passable, surface_cost, tile_cost)
from rwf.units import VARIANTS, build_unit
from rwf.unitstate import (STATES, describe, state_alarm, state_color,
                           state_label, unit_state)
from rwf.wreckage import (BURNT_MATERIALS, WreckageManager, burnt_hull,
                          debris_commands, fire_cells)
from rwf.world import World


# ---------------------------------------------------------------------------
#  Человекочитаемые состояния техники
# ---------------------------------------------------------------------------
def _snap(**kw):
    """Минимальный снимок юнита: всё, что нужно `unitstate`."""
    base = {"status": "flying", "alive": True, "health_pct": 100.0,
            "speed": 60.0, "vs": 0.0, "roll": 0.0, "pitch": 0.0,
            "g_load": 1.0, "kind": "aircraft", "mounts": []}
    base.update(kw)
    return base


class TestUnitState(unittest.TestCase):
    def test_spawned_is_never_shown(self):
        """Заказчик: статуса «spawned» в интерфейсе быть не должно."""
        key = unit_state(_snap(status="spawned", speed=0.0))
        self.assertEqual(key, "standby")
        self.assertEqual(state_label(key), "Стоит")
        for status in ("spawned", "idle", "landed", "serviced"):
            self.assertNotIn(status, state_label(unit_state(_snap(status=status,
                                                                speed=0.0))))

    def test_required_states_exist(self):
        wanted = {"Стоит", "Маневрирует", "Отстреливается", "Падает", "Горит"}
        labels = {v[0] for v in STATES.values()}
        self.assertTrue(wanted <= labels, f"нет состояний: {wanted - labels}")

    def test_standing(self):
        self.assertEqual(state_label(unit_state(_snap(status="idle",
                                                      speed=0.0))), "Стоит")

    def test_maneuver_by_bank_and_g(self):
        self.assertEqual(unit_state(_snap(roll=35.0)), "maneuver")
        self.assertEqual(unit_state(_snap(g_load=2.4)), "maneuver")
        self.assertEqual(state_label("maneuver"), "Маневрирует")

    def test_firing_from_mount_age(self):
        mounts = [{"since_fire": 0.4, "fired_total": 12}]
        key = unit_state(_snap(mounts=mounts))
        self.assertEqual(key, "firing")
        self.assertEqual(state_label(key), "Отстреливается")
        # давний выстрел состоянием «стреляет» уже не считается
        self.assertNotEqual(unit_state(_snap(mounts=[{"since_fire": 30.0,
                                                      "fired_total": 12}])),
                            "firing")

    def test_falling_wins_over_maneuver(self):
        key = unit_state(_snap(vs=-18.0, roll=40.0, status="stall"))
        self.assertEqual(key, "falling")
        self.assertEqual(state_label(key), "Падает")
        self.assertTrue(state_alarm(key))

    def test_burning_on_low_hp(self):
        key = unit_state(_snap(health_pct=22.0, status="damaged"))
        self.assertEqual(key, "burning")
        self.assertEqual(state_label(key), "Горит")

    def test_priority_order(self):
        """Гибель > падение > огонь > выстрел > манёвр > полёт."""
        dead = _snap(alive=False, vs=-30.0, health_pct=5.0,
                     mounts=[{"since_fire": 0.1, "fired_total": 3}])
        self.assertEqual(unit_state(dead), "destroyed")
        falling = _snap(vs=-30.0, health_pct=5.0,
                        mounts=[{"since_fire": 0.1, "fired_total": 3}])
        self.assertEqual(unit_state(falling), "falling")
        burning = _snap(health_pct=10.0,
                        mounts=[{"since_fire": 0.1, "fired_total": 3}])
        self.assertEqual(unit_state(burning), "burning")

    def test_ground_units_roll_and_stand(self):
        self.assertEqual(unit_state(_snap(kind="tank", status="moving",
                                          speed=8.0)), "rolling")
        self.assertEqual(unit_state(_snap(kind="truck", status="idle",
                                          speed=0.0)), "standby")
        self.assertEqual(state_label("rolling"), "Едет")

    def test_landing_and_parked(self):
        self.assertEqual(unit_state(_snap(), {"recovery": True}), "landing")
        self.assertEqual(unit_state(_snap(status="parked", speed=0.0),
                                    {"parked": True}), "parked")
        self.assertEqual(state_label("parked"), "Стоит на базе")

    def test_patrol_and_route(self):
        self.assertEqual(unit_state(_snap(), {"ai": "patrol"}), "patrol")
        self.assertEqual(unit_state(_snap(), {"route": True}), "route")

    def test_describe_tuple_and_colors(self):
        key, label, color, alarm = describe(_snap(health_pct=12.0))
        self.assertEqual((key, label, alarm), ("burning", "Горит", True))
        self.assertEqual(len(color), 4)
        self.assertEqual(color, state_color("burning"))

    def test_unit_snapshot_has_state_inputs(self):
        """Снимок юнита обязан нести поля, по которым считается состояние."""
        u = build_unit("attacker", "t")
        snap = u.snapshot()
        for field in ("vs", "ground", "burning", "duty", "status",
                      "health_pct", "roll", "g_load", "mounts"):
            self.assertIn(field, snap)
        self.assertIn("since_fire", u.mounts[0].snapshot())


# ---------------------------------------------------------------------------
#  Экономика и прочность баз
# ---------------------------------------------------------------------------
class TestBaseEconomy(unittest.TestCase):
    def setUp(self):
        self.mgr = BaseManager()
        self.air = self.mgr.add("Аэродром", KIND_AIRPORT, 0.0, 0.0)
        self.car = self.mgr.add("Авианосец", KIND_CARRIER, 400.0, 400.0)
        self.ground = self.mgr.add("Гарнизон", KIND_GROUND, -400.0, 0.0)

    def test_health_field_exists(self):
        """Заказчик: у базы есть прочность — её можно уничтожить."""
        for b in (self.air, self.car, self.ground):
            self.assertGreater(b.health, 0)
            self.assertEqual(b.health, b.health_max)
            self.assertIn("health", b.to_dict())
            self.assertIn("health_pct", b.to_dict())

    def test_base_can_be_destroyed(self):
        self.assertFalse(self.air.take_damage(100.0))
        self.assertLess(self.air.health, self.air.health_max)
        self.assertTrue(self.air.take_damage(10 ** 6, "бомба"))
        self.assertTrue(self.air.destroyed)
        self.assertFalse(self.air.alive)
        self.assertEqual(self.air.health, 0.0)
        # уничтоженная база не обслуживает
        u = build_unit("attacker", "t")
        u.fuel = 1.0
        self.assertEqual(self.mgr.service_at(self.air, u), [])

    def test_supply_regen_accumulates(self):
        self.air.supply = 100.0
        gained = self.mgr.step_supply(10.0)
        self.assertIn(self.air.id, gained)
        self.assertAlmostEqual(self.air.supply, 100.0 + self.air.supply_regen * 10.0)

    def test_supply_capped_at_max(self):
        self.air.supply = self.air.supply_max - 1.0
        self.mgr.step_supply(1000.0)
        self.assertLessEqual(self.air.supply, self.air.supply_max)

    def test_carrier_has_no_regen(self):
        """Авианосец без своей генерации — только доставка."""
        self.assertEqual(self.car.supply_regen, 0.0)
        self.car.supply = 10.0
        self.mgr.step_supply(600.0)
        self.assertEqual(self.car.supply, 10.0)

    def test_carrier_resupplied_by_cargo(self):
        self.car.supply = 20.0
        gained = self.car.deliver_cargo(5.0)
        self.assertAlmostEqual(gained, 5.0 * SUPPLY_PER_TON)
        self.assertAlmostEqual(self.car.supply, 20.0 + 5.0 * SUPPLY_PER_TON)

    def test_supply_never_spent_below_floor(self):
        """Очки не кончаются полностью: неснижаемый остаток."""
        self.air.supply = 50.0
        paid = self.air.spend(10 ** 6)
        self.assertLessEqual(paid, 50.0)
        self.assertGreaterEqual(self.air.supply, SUPPLY_FLOOR - 1e-6)
        self.assertFalse(self.air.can_afford(10 ** 6))

    def test_service_costs_supply(self):
        u = build_unit("attacker", "t")
        u.fuel = 10.0
        u.health = 30.0
        before = self.air.supply
        done = self.mgr.service_at(self.air, u)
        self.assertTrue(done)
        self.assertLess(self.air.supply, before)
        self.assertGreater(self.air.supply_spent, 0.0)
        self.assertEqual(u.fuel, u.spec.fuel_max)

    def test_service_is_partial_on_deficit(self):
        """Дефицит: услуга оказывается частично, а не «бесплатно и полностью»."""
        u = build_unit("attacker", "t")
        u.fuel = 0.0
        self.air.supply = SUPPLY_FLOOR + 4.0
        done = self.mgr.service_at(self.air, u)
        self.assertTrue(any("заправка" in d for d in done))
        self.assertLess(u.fuel, u.spec.fuel_max, "при дефиците полная заправка")
        self.assertGreater(u.fuel, 0.0)

    def test_free_service_when_nothing_needed(self):
        u = build_unit("attacker", "t")     # полный бак и корпус
        before = self.air.supply
        self.assertEqual(self.mgr.service_at(self.air, u), [])
        self.assertEqual(self.air.supply, before)

    def test_snapshot_exposes_economy(self):
        snap = self.car.to_dict()
        for key in ("supply", "supply_max", "supply_pct", "supply_regen",
                    "health", "health_max", "health_pct", "destroyed"):
            self.assertIn(key, snap)


# ---------------------------------------------------------------------------
#  Разрушения
# ---------------------------------------------------------------------------
class TestWreckage(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.mgr = WreckageManager(queue=None, world=self.world)

    def test_burnt_hull_is_deterministic_and_burnt(self):
        from rwf.model import get_blueprint
        bp = get_blueprint(VARIANTS["attacker"].spec.blueprint)
        a = burnt_hull(bp, (10.0, 70.0, 20.0), 90.0, seed=7)
        b = burnt_hull(bp, (10.0, 70.0, 20.0), 90.0, seed=7)
        self.assertEqual(a, b, "остов должен быть воспроизводимым")
        self.assertTrue(a, "остов пуст")
        self.assertTrue(set(a.values()) <= set(BURNT_MATERIALS))
        # другой seed — другой набор «отвалившихся» клеток
        self.assertNotEqual(a, burnt_hull(bp, (10.0, 70.0, 20.0), 90.0, seed=8))

    def test_fire_cells_sit_above_hull(self):
        hull = {(0, 64, 0): "minecraft:coal_block",
                (1, 64, 0): "minecraft:iron_block"}
        cells = fire_cells(hull, seed=1)
        self.assertTrue(cells)
        for (x, y, z) in cells:
            self.assertNotIn((x, y, z), hull, "огонь внутри блока остова")
            self.assertIn((x, y - 1, z), hull, "огонь не над остовом")

    def test_debris_are_falling_blocks(self):
        cmds = debris_commands((0.0, 70.0, 0.0), "aircraft", seed=3)
        self.assertTrue(cmds)
        for c in cmds:
            self.assertTrue(c.startswith("summon minecraft:falling_block"), c)
            self.assertIn("DropItem:0b", c)
            self.assertIn("Motion:", c)

    def test_debris_modern_dialect(self):
        cmds = debris_commands((0.0, 70.0, 0.0), "tank", seed=3,
                               nbt=mc.MODERN_NBT)
        self.assertTrue(all("block_state:" in c for c in cmds))

    def test_create_site_marks_world_and_burns(self):
        site = self.mgr.create((100.0, 70.0, 50.0), kind="aircraft",
                               label="Су-25", reason="сбит",
                               blueprint=VARIANTS["attacker"].spec.blueprint,
                               yaw=90.0, unit_id=42)
        self.assertTrue(site.burning)
        self.assertGreater(len(site.hull), 0)
        self.assertGreater(len(site.fire), 0)
        self.assertEqual(site.unit_id, 42)
        markers = [m for m in self.world.snapshot().get("markers", [])
                   if m["kind"] == "wreck"]
        self.assertEqual(len(markers), 1)
        self.assertIn("обломки", markers[0]["text"])

    def test_site_without_blueprint_gets_rubble(self):
        site = self.mgr.create((0.0, 64.0, 0.0), kind="base", label="База")
        self.assertGreater(len(site.hull), 0)

    def test_step_extinguishes_after_burn_time(self):
        site = self.mgr.create((0.0, 64.0, 0.0), kind="tank")
        site.burn_until = time.time() - 1.0
        self.mgr.step(0.1)
        self.assertTrue(site.extinguished)
        self.assertFalse(site.burning)
        self.assertEqual(self.mgr.stats["extinguished"], 1)

    def test_step_smokes_while_burning(self):
        site = self.mgr.create((0.0, 64.0, 0.0), kind="tank")
        site.last_smoke = 0.0                      # «давно» не дымили
        sent = self.mgr.step(0.1, now=time.time())
        self.assertGreater(sent, 0)

    def test_remove_clears_site(self):
        site = self.mgr.create((0.0, 64.0, 0.0), kind="tank")
        self.mgr.remove(site.id)
        self.assertIsNone(self.mgr.get(site.id))
        self.assertEqual(self.mgr.snapshot(), [])

    def test_limit_evicts_oldest(self):
        mgr = WreckageManager(queue=None, world=World(), limit=3)
        for i in range(6):
            mgr.create((float(i), 64.0, 0.0), kind="drone")
        self.assertLessEqual(len(mgr.sites()), 3)

    def test_snapshot_serialisable(self):
        self.mgr.create((0.0, 64.0, 0.0), kind="helicopter", label="Ми-24")
        snap = self.mgr.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertIn("burning", snap[0])
        self.assertEqual(snap[0]["label"], "Ми-24")


# ---------------------------------------------------------------------------
#  Наземная техника: коллизия, уклон, дороги
# ---------------------------------------------------------------------------
class _FakeTerrain:
    """Плоская сетка рельефа для тестов навигации."""

    def __init__(self, step=8, height=64, kind="grass"):
        self.step = step
        self.height = height
        self.kind = kind
        self._tiles = {}

    def get(self, x, z):
        return self._tiles.get((x, z))

    def height_at(self, x, z):
        t = self.get(int(round(x / self.step)) * self.step,
                     int(round(z / self.step)) * self.step)
        return t[0] if t else None


class _RampTerrain(_FakeTerrain):
    """Стена/крутой подъём по оси +Z начиная с тайла z >= WALL_Z.

    Стена начинается НЕ в нуле, чтобы точка старта машины (z = 0) гарантированно
    лежала в «ровном» тайле: иначе из-за округления к шагу сетки машина сразу
    оказывалась внутри стены и тест проверял не коллизию, а арифметику snap().
    """

    WALL_Z = 8

    def __init__(self, step=8, climb=40):
        super().__init__(step=step)
        self.climb = climb
        for x in range(-160, 161, step):
            for z in range(-160, 161, step):
                h = 64 if z < self.WALL_Z else 64 + self.climb
                self._tiles[(x, z)] = (h, "stone")


class TestGroundNav(unittest.TestCase):
    def test_road_is_cheaper_than_rough(self):
        """Приоритет дорог: асфальт дешевле травы и тем более песка."""
        self.assertLess(surface_cost("road"), surface_cost("grass"))
        self.assertLess(surface_cost("grass"), surface_cost("sand"))
        self.assertTrue(math.isinf(surface_cost("water")))
        self.assertTrue(math.isinf(surface_cost("lava")))

    def test_steep_slope_impassable(self):
        self.assertTrue(passable("grass", 10.0))
        self.assertFalse(passable("grass", 55.0, max_climb_deg=30.0))
        self.assertTrue(math.isinf(tile_cost("grass", 60.0, 30.0)))

    def test_water_blocks(self):
        nav = GroundNav(_FakeTerrain(kind="water"), step=8)
        nav.terrain._tiles[(0, 0)] = (64, "water")
        nav.terrain._tiles[(0, 8)] = (64, "water")
        self.assertTrue(nav.blocked_ahead(0.0, 0.0, 0.0))

    def test_wall_blocks_and_no_movement(self):
        """Коллизия: впереди непреодолимый подъём — машина НЕ едет сквозь."""
        nav = GroundNav(_RampTerrain(climb=40), step=8, max_climb_deg=25.0)
        # старт в «ровном» тайле (z=0), шаг через границу в стену (тайл z=8)
        x, y, z = 0.0, 65.0, 0.0
        nx, ny, nz, _course, _pitch, moved = nav.advance(x, y, z, 0.0, 12.0,
                                                         auto_detour=False)
        self.assertFalse(moved)
        self.assertEqual((nx, nz), (x, z), "машина проехала сквозь препятствие")
        self.assertTrue(nav.blocked_ahead(x, z, 0.0))

    def test_gentle_slope_passes_with_pitch(self):
        terr = _FakeTerrain(step=8)
        for x in range(-80, 81, 8):
            for i, z in enumerate(range(-80, 81, 8)):
                terr._tiles[(x, z)] = (64 + i, "grass")     # ~7° на шаг 8 м
        nav = GroundNav(terr, step=8, max_climb_deg=30.0)
        nx, ny, nz, _c, pitch, moved = nav.advance(0.0, 65.0, -8.0, 0.0, 4.0,
                                                   auto_detour=False)
        self.assertTrue(moved)
        self.assertLess(pitch, 0.0, "на подъёме тангаж должен задирать нос")

    def test_choose_heading_prefers_passable(self):
        """Автопилот уводит курс от стены, если есть объезд."""
        terr = _FakeTerrain(step=8)
        for x in range(-160, 161, 8):
            for z in range(-160, 161, 8):
                kind = "grass"
                h = 64
                if x == 0 and z >= 0:                  # стена в один тайл
                    h, kind = 120, "stone"
                terr._tiles[(x, z)] = (h, kind)
        nav = GroundNav(terr, step=8, max_climb_deg=25.0)
        self.assertTrue(nav.blocked_ahead(0.0, -8.0, 0.0), "стена не видна")
        yaw, cost, blocked = nav.choose_heading(0.0, -8.0, 0.0)
        self.assertFalse(blocked)
        self.assertNotAlmostEqual(abs(yaw), 0.0, delta=1.0)
        self.assertTrue(math.isfinite(cost))

    def test_detour_impossible_when_surrounded(self):
        terr = _FakeTerrain(step=8)
        for x in range(-160, 161, 8):
            for z in range(-160, 161, 8):
                terr._tiles[(x, z)] = (200, "stone")
        terr._tiles[(0, 0)] = (64, "grass")
        nav = GroundNav(terr, step=8, max_climb_deg=20.0)
        _yaw, _cost, blocked = nav.choose_heading(0.0, 0.0, 0.0)
        self.assertTrue(blocked)
        self.assertIsNone(nav.detour_heading(0.0, 0.0, 0.0))

    def test_report_shape(self):
        nav = GroundNav(_FakeTerrain(), step=8)
        rep = nav.report(0.0, 0.0, 0.0)
        for key in ("slope", "cost", "here", "ahead", "blocked"):
            self.assertIn(key, rep)


class TestTankCollision(unittest.TestCase):
    def _tank_on(self, terrain, variant="mbt"):
        u = build_unit(variant, "t")
        u.pos = (0.0, 65.0, -20.0)
        u.set_target(heading=0.0, throttle=1.0)
        return u

    def test_tank_stops_at_wall(self):
        """Главная жалоба: «едет сквозь блоки» — больше не едет."""
        w = World()
        w.terrain = _RampTerrain(climb=60)
        u = self._tank_on(w.terrain)
        u.controls.heading = 0.0
        z0 = u.pos[2]
        for _ in range(60):
            u.step(0.1, w)
        self.assertLess(u.pos[2], _RampTerrain.WALL_Z,
                        "танк прошёл сквозь стену")
        self.assertTrue(u.blocked)
        self.assertEqual(u.status, "stuck")
        self.assertEqual(u.speed, 0.0)
        self.assertLess(u.pos[2], _RampTerrain.WALL_Z,
                        "танк въехал в стену")

    def test_tank_climb_limit_from_spec(self):
        for key in ("mbt", "apc", "truck"):
            self.assertTrue(hasattr(VARIANTS[key].spec, "max_climb_deg"))
            self.assertGreater(VARIANTS[key].spec.max_climb_deg, 0)
        # у тяжёлой техники предел не выше, чем у грузовика вне дорог
        self.assertLessEqual(VARIANTS["truck"].spec.max_climb_deg,
                             VARIANTS["mbt"].spec.max_climb_deg + 1e-6)

    def test_tank_rides_flat_terrain(self):
        w = World()
        w.terrain = _FakeTerrain(step=8)
        for x in range(-160, 161, 8):
            for z in range(-160, 161, 8):
                w.terrain._tiles[(x, z)] = (64, "grass")
        u = self._tank_on(w.terrain)
        for _ in range(40):
            u.step(0.1, w)
        self.assertTrue(u.blocked is False)
        self.assertGreater(u.pos[2], -20.0)
        self.assertAlmostEqual(u.pos[1], 65.0)     # 64 + 1 блок над поверхностью
        self.assertEqual(u.status, "moving")
        self.assertEqual(u.ground_kind, "grass")

    def test_road_is_faster_than_sand(self):
        """Приоритет дорог: на асфальте машина разгоняется сильнее."""
        def run(kind):
            w = World()
            w.terrain = _FakeTerrain(step=8)
            for x in range(-200, 201, 8):
                for z in range(-200, 201, 8):
                    w.terrain._tiles[(x, z)] = (64, kind)
            u = build_unit("truck", "t")
            u.pos = (0.0, 65.0, -100.0)
            u.set_target(heading=0.0, throttle=1.0)
            for _ in range(120):
                u.step(0.1, w)
            return u.speed, u.pos[2]
        road_speed, road_z = run("road")
        sand_speed, sand_z = run("sand")
        self.assertGreater(road_speed, sand_speed)
        self.assertGreater(road_z, sand_z)

    def test_tank_does_not_enter_water(self):
        w = World()
        w.terrain = _FakeTerrain(step=8)
        for x in range(-160, 161, 8):
            for z in range(-160, 161, 8):
                kind = "water" if z >= 0 else "grass"
                w.terrain._tiles[(x, z)] = (64, kind)
        u = self._tank_on(w.terrain)
        for _ in range(80):
            u.step(0.1, w)
        self.assertLess(u.pos[2], 0.0, "танк заехал в воду")
        self.assertEqual(u.status, "stuck")


class TestSurfaceKinds(unittest.TestCase):
    def test_road_in_palette(self):
        self.assertIn("road", {k for _b, k in mc.SURFACE_KINDS})
        from rwf.maprender import BLOCK_PALETTE
        self.assertIn("road", BLOCK_PALETTE)

    def test_mock_world_has_roads(self):
        from rwf.mock_server import is_road, surface_kind
        self.assertTrue(is_road(0.0, 37.0))
        self.assertFalse(is_road(64.0, 64.0))
        self.assertEqual(surface_kind(0.0, 37.0), "gray_concrete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
