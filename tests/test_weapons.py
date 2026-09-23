"""Тесты оружия: боезапас, темп, баллистика, эффекты, наведение УР."""
from __future__ import annotations

import math
import time
import unittest

from rwf import mc
from rwf.config import CombatConfig
from rwf.units import build_unit
from rwf.weapons import (WEAPONS, GuidedMissile, Target, WeaponMount,
                            WeaponSystem, available_for, build_mounts, distance,
                            segment_distance, solve_lead, speed_mps, unit_vector,
                            weapon)
from rwf.world import World


class FakeQueue:
    """Очередь-регистратор: позволяет проверять сами отправленные команды."""

    def __init__(self):
        self.commands = []
        self.keys = []

    def submit(self, command, priority=10, key=None):
        self.commands.append(command)
        self.keys.append(key)

    def submit_many(self, commands, priority=10, key_prefix=None):
        for c in commands:
            self.submit(c, priority, key_prefix)

    def flush(self, timeout=1.0):
        return True

    def stats(self):
        return {"submitted": len(self.commands)}

    def find(self, needle):
        return [c for c in self.commands if needle in c]


class TestMounts(unittest.TestCase):
    def test_category_is_enforced(self):
        mount = WeaponMount("cannon", "Нос")
        with self.assertRaises(ValueError):
            mount.load("fab500")            # бомбу в пушечный подвес нельзя
        mount.load("gsh23")
        self.assertEqual(mount.ammo, WEAPONS["gsh23"]["count"])

    def test_unknown_weapon_reports_available(self):
        with self.assertRaises(KeyError) as ctx:
            weapon("kpvt")
        self.assertIn("gsh23", str(ctx.exception))

    def test_unload_and_reload(self):
        mount = WeaponMount("bomb", "Отсек", "fab500")
        mount.consume(2)
        self.assertEqual(mount.ammo, WEAPONS["fab500"]["count"] - 2)
        mount.load(None)
        self.assertEqual((mount.ammo, mount.key), (0, None))
        self.assertFalse(mount.ready())
        mount.load("fab500")
        self.assertEqual(mount.ammo, mount.ammo_max)
        mount.consume(99)
        self.assertEqual(mount.ammo, 0)
        mount.reload()
        self.assertEqual(mount.ammo, mount.ammo_max)

    def test_consume_never_goes_negative(self):
        mount = WeaponMount("mg", "ПКМ", "pkm")
        before = mount.ammo
        used = mount.consume(before + 50)
        self.assertEqual(used, before)
        self.assertEqual(mount.ammo, 0)

    def test_snapshot_is_a_copy(self):
        mount = WeaponMount("cannon", "Нос", "gsh23")
        snap = mount.snapshot()
        snap["ammo"] = -1
        self.assertEqual(mount.ammo, WEAPONS["gsh23"]["count"])
        for key in ("category", "slot", "label", "ammo", "ammo_max", "ready"):
            self.assertIn(key, snap)

    def test_build_mounts_rejects_bad_category(self):
        with self.assertRaises(ValueError):
            build_mounts([("teleport", "Слот", None)])

    def test_catalog_helpers(self):
        bombs = available_for("bomb")
        self.assertIn("fab500", bombs)
        self.assertTrue(all(WEAPONS[k]["cat"] == "bomb" for k in bombs))
        self.assertEqual(set(available_for("nope")), set())
        self.assertAlmostEqual(speed_mps("gsh23"),
                               WEAPONS["gsh23"]["speed"] * 20.0)
        for key, spec in WEAPONS.items():
            self.assertIn(spec["cat"], ("bomb", "rocket", "missile", "cannon", "mg"),
                          f"{key}: неизвестная категория")
            self.assertGreater(spec.get("count", 0), 0, f"{key}: нет боезапаса")


class TestBallistics(unittest.TestCase):
    def test_solve_lead_static_target(self):
        t = solve_lead((0, 100, 0), (100, 64, 0), (0, 0, 0), 50.0)
        self.assertAlmostEqual(t, math.hypot(100, 36) / 50.0, delta=0.05)

    def test_solve_lead_receding_target_takes_longer(self):
        still = solve_lead((0, 100, 0), (100, 64, 0), (0, 0, 0), 50.0)
        away = solve_lead((0, 100, 0), (100, 64, 0), (20, 0, 0), 50.0)
        self.assertGreater(away, still)

    def test_solve_lead_closing_target_is_quicker(self):
        still = solve_lead((0, 100, 0), (100, 64, 0), (0, 0, 0), 50.0)
        toward = solve_lead((0, 100, 0), (100, 64, 0), (-20, 0, 0), 50.0)
        self.assertLess(toward, still)

    def test_segment_distance_catches_fast_pass(self):
        """Быстрая ракета проходит сквозь цель между кадрами — отрезок ловит."""
        target = (0.0, 64.0, 0.0)
        before, after = (0.0, 64.0, -30.0), (0.0, 64.0, 30.0)
        self.assertAlmostEqual(segment_distance(target, before, after), 0.0)
        self.assertAlmostEqual(distance(target, before), 30.0)
        self.assertAlmostEqual(distance(target, after), 30.0)
        # Промах в стороне
        miss = (25.0, 64.0, 0.0)
        self.assertAlmostEqual(segment_distance(miss, before, after), 25.0)

    def test_unit_vector(self):
        self.assertIsNone(unit_vector((0, 0, 0), (0, 0, 0)))
        v = unit_vector((0, 0, 0), (0, 0, 10))
        self.assertAlmostEqual(v[2], 1.0)


class _WeaponCase(unittest.TestCase):
    variant = "attacker"
    cfg_kwargs: dict = {}

    def setUp(self):
        self.world = World()
        self.queue = FakeQueue()
        self.cfg = CombatConfig(**self.cfg_kwargs)
        self.weapons = WeaponSystem(self.cfg, self.queue, self.world,
                                    bus=self.world.bus)
        self.unit = build_unit(self.variant, "jet", pos=(0.0, 150.0, 0.0),
                               speed=30.0, throttle=0.8)
        self.world.add_unit(self.unit)

    def mount(self, category):
        for m in self.unit.mounts:
            if m.category == category:
                return m
        raise AssertionError(f"у {self.variant} нет подвеса {category}")

    def target(self, pos=(0.0, 64.0, 300.0), vel=(0.0, 0.0, 0.0), name=""):
        return Target(pos=pos, vel=vel, name=name)


class TestFiring(_WeaponCase):
    def test_gun_consumes_ammo(self):
        """Регресс UNIT-07: боезапас обязан расходоваться."""
        m = self.mount("cannon")
        before = m.ammo
        self.assertTrue(self.weapons.fire(self.unit, m, self.target()))
        self.assertLess(m.ammo, before)
        self.assertEqual(m.ammo, before - WEAPONS[m.key]["burst"])
        self.assertGreater(m.fired_total, 0)

    def test_cooldown_blocks_immediate_second_shot(self):
        """Регресс WPN-08: пауза берётся из каталога, а не хардкодом."""
        m = self.mount("cannon")
        now = time.monotonic()
        self.assertTrue(self.weapons.fire(self.unit, m, self.target(), now=now))
        self.assertFalse(self.weapons.fire(self.unit, m, self.target(), now=now))
        later = now + m.cooldown + 0.01
        self.assertTrue(self.weapons.fire(self.unit, m, self.target(), now=later))

    def test_empty_mount_does_not_fire(self):
        m = self.mount("cannon")
        m.ammo = 0
        self.assertFalse(self.weapons.fire(self.unit, m, self.target()))
        self.assertEqual(self.queue.commands, [])

    def test_gun_commands_use_catalog_entity(self):
        m = self.mount("cannon")
        spec = weapon(m.key)
        self.weapons.fire(self.unit, m, self.target())
        shots = self.queue.find(str(spec["entity"]))
        self.assertEqual(len(shots), spec["burst"])

    def test_gun_aims_with_lead(self):
        """Стрельба по движущейся цели идёт с упреждением, а не «в грудь»."""
        m = self.mount("cannon")
        moving = self.target(pos=(0.0, 64.0, 300.0), vel=(30.0, 0.0, 0.0))
        self.weapons.fire(self.unit, m, moving)
        shot = self.queue.find("Motion")[0]
        mx = float(shot.split("Motion:[")[1].split(",")[0])
        self.assertGreater(mx, 0.01, "упреждение по X не учтено")

    def test_rocket_fireball_has_zero_power_and_explosion(self):
        """Регресс WPN-02/WPN-03: нет несуществующего `direction`, power обнулён."""
        m = self.mount("rocket")
        self.assertTrue(self.weapons.fire(self.unit, m, self.target()))
        shots = self.queue.find("minecraft:fireball")
        self.assertTrue(shots)
        for s in shots:
            self.assertNotIn("direction:", s)
            self.assertIn("power:[0.0000,0.0000,0.0000]", s)
            self.assertIn("ExplosionPower:", s)

    def test_rocket_salvo_is_spread(self):
        """Регресс WPN-09: снаряды залпа не должны сливаться в одной точке."""
        m = self.mount("rocket")
        self.weapons.fire(self.unit, m, self.target())
        shots = self.queue.find("summon minecraft:fireball")
        self.assertGreaterEqual(len(shots), 2)
        origins = [s.split("summon minecraft:fireball ")[1].split(" {")[0]
                   for s in shots]
        self.assertEqual(len(set(origins)), len(origins),
                         "все снаряды залпа спавнятся в одной точке")


class TestBombing(_WeaponCase):
    def test_bomb_summons_tnt_with_fuse_and_motion(self):
        m = self.mount("bomb")
        self.assertTrue(self.weapons.fire(self.unit, m))
        tnts = self.queue.find("summon minecraft:tnt")
        self.assertGreater(len(tnts), 0)
        for cmd in tnts:
            self.assertIn("fuse:", cmd)
            self.assertIn("Motion:", cmd)
            fuse = int(cmd.split("fuse:")[1].split(",")[0].rstrip("}"))
            self.assertGreater(fuse, 4)

    def test_fuse_matches_fall_time(self):
        """Взрыватель должен срабатывать к моменту падения, а не сразу."""
        m = self.mount("bomb")
        self.weapons.fire(self.unit, m)
        cmd = self.queue.find("summon minecraft:tnt")[0]
        fuse = int(cmd.split("fuse:")[1].split(",")[0].rstrip("}"))
        height = self.unit.pos[1] - 63.0        # рельеф не отсканирован -> уровень моря
        expected = math.sqrt(2 * height / self.cfg.gravity_effective) * 20
        self.assertAlmostEqual(fuse, expected, delta=2.0)

    def test_bomb_train_is_spaced(self):
        """Бомбы в залпе расходятся вдоль курса, а не валятся в одну точку."""
        m = self.mount("bomb")
        spec = weapon(m.key)
        self.weapons.fire(self.unit, m)
        tnts = self.queue.find("summon minecraft:tnt")
        self.assertEqual(len(tnts), min(spec["tnt"], m.ammo_max))
        # Веер расходится вдоль вектора скорости, поэтому сравниваем позицию целиком
        spots = [" ".join(c.split("summon minecraft:tnt ")[1].split()[:3]) for c in tnts]
        self.assertEqual(len(set(spots)), len(spots), "бомбы легли в одну точку")
        gaps = {round(abs(float(a.split()[2]) - float(b.split()[2])), 3)
                for a, b in zip(spots, spots[1:])}
        self.assertEqual(len(gaps), 1, f"неравномерный веер: {gaps}")

    def test_volley_is_capped(self):
        m = self.mount("bomb")
        m.load("nuke")
        m.ammo = 500
        self.cfg.max_commands_per_volley = 3
        self.weapons.fire(self.unit, m)
        self.assertLessEqual(len(self.queue.find("summon minecraft:tnt")), 3)

    def test_impact_effects_are_scheduled_not_immediate(self):
        m = self.mount("bomb")
        self.weapons.fire(self.unit, m)
        self.assertEqual(self.queue.find("fill"), [],
                         "воронка применена до падения бомбы")
        self.assertEqual(len(self.weapons.effects), 1)
        # Прокручиваем время вперёд
        self.weapons.effects[0].delay = -1.0
        self.weapons.update(0.1)
        self.assertTrue(self.queue.find("air"), "эффекты поражения не применились")
        self.assertEqual(self.weapons.effects, [])

    def test_crater_applied_at_impact(self):
        m = self.mount("bomb")
        m.load("fab500")
        self.weapons.fire(self.unit, m)
        self.weapons.effects[0].delay = -1.0
        self.weapons.update(0.1)
        fills = self.queue.find("fill")
        self.assertTrue(fills)
        self.assertTrue(all("minecraft:air" in f for f in fills))
        self.assertIn("destroy" if self.cfg.drop_blocks else "fill",
                      " ".join(fills))

    def test_tunnel_to_cave_target(self):
        """Регресс WPN-01: шахта до цели в пещере возвращается в проект."""
        self.cfg.destructive_confirmed = True
        m = self.mount("bomb")
        deep = self.target(pos=(0.0, 10.0, 0.0))     # цель глубоко под землёй
        self.unit.pos = (0.0, 150.0, 0.0)
        self.weapons.fire(self.unit, m, deep)
        self.weapons.effects[0].delay = -1.0
        self.weapons.update(0.1)
        fills = self.queue.find("fill")
        ys = [int(f.split()[2]) for f in fills]
        self.assertTrue(any(y <= 12 for y in ys),
                        f"шахта не дошла до уровня цели: {sorted(ys)[:5]}")

    def test_tunnel_skipped_for_surface_target(self):
        self.cfg.destructive_confirmed = True
        m = self.mount("bomb")
        surface = self.target(pos=(0.0, 149.0, 10.0))
        self.weapons.fire(self.unit, m, surface)
        self.weapons.effects[0].delay = -1.0
        self.weapons.update(0.1)
        deep = [f for f in self.queue.find("fill")
                if int(f.split()[2]) < 60]
        self.assertEqual(deep, [], "копает шахту при цели на поверхности")

    def test_tunnel_blocked_without_confirmation(self):
        """Подтверждение разрушений: копать нельзя, пока пользователь не разрешил."""
        self.cfg.confirm_destructive = True
        self.cfg.destructive_confirmed = False
        m = self.mount("bomb")
        self.weapons.fire(self.unit, m, self.target(pos=(0.0, 5.0, 0.0)))
        self.weapons.effects[0].delay = -1.0
        self.weapons.update(0.1)
        self.assertEqual(self.weapons.blocked_destructive, 1)
        self.assertEqual([f for f in self.queue.find("fill") if int(f.split()[2]) < 60],
                         [])

    def test_no_bombs_no_fire(self):
        m = self.mount("bomb")
        m.ammo = 0
        self.assertFalse(self.weapons.fire(self.unit, m))


class TestGuidedMissiles(_WeaponCase):
    variant = "fighter"

    def test_missile_needs_a_target(self):
        """УР без цели не пускаем: боезапас не расходуется впустую."""
        m = self.mount("missile")
        before = m.ammo
        self.assertFalse(self.weapons.fire(self.unit, m, None))
        self.assertEqual(m.ammo, before)
        self.assertEqual(self.queue.commands, [])

    def test_missile_out_of_range(self):
        m = self.mount("missile")
        far = self.target(pos=(0.0, 64.0, 99999.0))
        self.assertFalse(self.weapons.fire(self.unit, m, far))
        self.assertEqual(self.weapons.missiles, [])

    def test_missile_is_spawned_as_guided_entity(self):
        m = self.mount("missile")
        self.assertTrue(self.weapons.fire(self.unit, m, self.target()))
        self.assertEqual(len(self.weapons.missiles), 1)
        msl = self.weapons.missiles[0]
        spawn = self.queue.find("summon")[0]
        self.assertIn("NoGravity:1b", spawn)
        self.assertIn(f'"{msl.tag}"', spawn)
        self.assertNotIn("direction:", spawn)

    def test_missile_steers_and_hits(self):
        """Регресс WPN-05: УР реально наводится, а не летит прямо."""
        m = self.mount("missile")
        m.load("r73")
        tgt = self.target(pos=(0.0, 100.0, 350.0))     # в пределах дальности Р-73
        self.assertTrue(self.weapons.fire(self.unit, m, tgt))
        for _ in range(400):
            self.weapons.update(0.1)
            if not self.weapons.missiles:
                break
        self.assertEqual(self.weapons.missiles, [], "ракета не долетела")
        self.assertEqual(self.weapons.missiles_hit, 1)
        self.assertTrue(self.queue.find("summon minecraft:tnt"),
                        "попадание не вызвало взрыв")

    def test_missile_hits_moving_target(self):
        m = self.mount("missile")
        m.load("agm")
        tgt = self.target(pos=(0.0, 100.0, 250.0), vel=(18.0, 0.0, 0.0),
                          name="Arlik88")
        self.world.set_player("Arlik88", (0.0, 100.0, 250.0))
        self.assertTrue(self.weapons.fire(self.unit, m, tgt))
        # Цель уходит вбок — ракета должна довернуться
        for i in range(400):
            px = 18.0 * (i * 0.1)
            self.world.set_player("Arlik88", (px, 100.0, 250.0))
            self.weapons.update(0.1)
            if not self.weapons.missiles:
                break
        self.assertEqual(self.weapons.missiles_hit, 1)

    def test_missile_expires(self):
        m = self.mount("missile")
        m.load("r73")
        # Ракета летит вверх, мимо цели — сработать должен таймер жизни
        self.cfg.missile_max_time = 0.5
        tgt = self.target(pos=(0.0, 100.0, 50.0))
        self.weapons.fire(self.unit, m, tgt)
        self.weapons.missiles[0].vel = (0.0, 10.0, 0.0)
        for _ in range(30):
            self.weapons.update(0.1)
        self.assertEqual(self.weapons.missiles, [], "ракета не самоуничтожилась по TTL")
        self.assertTrue(self.queue.find("kill @e[tag=rwf_msl_"),
                        "просроченная ракета не убрана из мира")

    def test_guided_disabled_falls_back_to_unguided(self):
        self.cfg.guided_missiles = False
        m = self.mount("missile")
        self.assertTrue(self.weapons.fire(self.unit, m, self.target()))
        self.assertEqual(self.weapons.missiles, [])
        self.assertTrue(self.queue.find("fireball") or self.queue.find("arrow"))

    def test_missile_cap_respected(self):
        self.cfg.max_tracked_missiles = 1
        m = self.mount("missile")
        self.assertTrue(self.weapons.fire(self.unit, m, self.target()))
        m.ammo = 10
        m.last_fire = 0.0
        self.weapons.fire(self.unit, m, self.target())
        self.assertLessEqual(len(self.weapons.missiles), 1)

    def test_steer_limits_turn_rate(self):
        msl = GuidedMissile(1, "r73", (0.0, 100.0, 0.0), (0.0, 0.0, 2.6),
                            Target(pos=(100.0, 100.0, 0.0)), self.cfg)
        before = msl.vel
        msl.steer(0.1, (100.0, 100.0, 0.0))
        # Скорость по модулю сохраняется, направление довернуто ограниченно
        self.assertAlmostEqual(math.sqrt(sum(c * c for c in msl.vel)),
                               math.sqrt(sum(c * c for c in before)), delta=1e-6)
        self.assertGreater(msl.vel[0], 0.0)
        self.assertLess(msl.vel[0], 2.6, "ракета развернулась быстрее turn_rate")


class TestVolleyHelpers(_WeaponCase):
    def test_fire_category(self):
        n = self.weapons.fire_category(self.unit, "bomb", self.target())
        self.assertGreaterEqual(n, 1)
        self.assertTrue(self.queue.find("tnt"))

    def test_fire_ready_with_filter(self):
        n = self.weapons.fire_ready(self.unit, self.target(),
                                    categories=("cannon",))
        self.assertEqual(n, 1)
        self.assertFalse(self.queue.find("summon minecraft:tnt"))

    def test_default_target_is_nearest_player(self):
        self.world.set_player("far", (0.0, 64.0, 900.0))
        self.world.set_player("near", (0.0, 64.0, 120.0))
        m = self.mount("cannon")
        self.assertTrue(self.weapons.fire(self.unit, m))     # цель не передана
        shot = self.queue.find("summon")[0]
        self.assertIn("Motion", shot)

    def test_stats(self):
        m = self.mount("cannon")
        self.weapons.fire(self.unit, m, self.target())
        st = self.weapons.stats()
        self.assertEqual(st["shots"], 1)
        self.assertGreater(st["ammo_spent"], 0)
        for key in ("missiles_active", "missiles_launched", "pending_effects"):
            self.assertIn(key, st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
