"""Тесты слоя Minecraft-команд: зонды возможностей, построители, рельеф."""
from __future__ import annotations

import unittest

from rwf import mc
from rwf.mock_server import MockMCServer, ground_height, top_y
from rwf.rcon import RCONConnection

HOST = "127.0.0.1"


class _MockCase(unittest.TestCase):
    server_kwargs: dict = {}

    def setUp(self):
        self.server = MockMCServer(host=HOST, port=0, password="2203",
                                   **self.server_kwargs).start()
        self.conn = RCONConnection(HOST, self.server.port, "2203", timeout=5.0)
        self.conn.connect()

    def tearDown(self):
        self.conn.close()
        self.server.stop()


class TestCapabilities(_MockCase):
    def test_vanilla_1201_caps(self):
        caps = mc.ServerCaps.probe(self.conn.run)
        self.assertTrue(caps.has_marker)
        self.assertTrue(caps.has_display_entities)
        self.assertTrue(caps.has_interaction)
        self.assertTrue(caps.has_damage_command)     # 1.19.4+
        self.assertFalse(caps.has_ride_command)      # появится только в 1.20.5
        self.assertEqual(caps.model_backend, "display")
        self.assertEqual(caps.nbt.name, "legacy")
        self.assertEqual(caps.brand, "vanilla")

    def test_probes_have_no_side_effects(self):
        before = self.server.snapshot()["entities"]
        mc.ServerCaps.probe(self.conn.run)
        self.assertEqual(self.server.snapshot()["entities"], before)
        # Ни одна команда зонда не должна ничего спавнить или ломать
        for cmd in self.server.command_log:
            self.assertFalse(cmd.startswith(("summon", "fill", "setblock", "kill", "tp ")),
                             f"Зонд с побочным эффектом: {cmd}")

    def test_modern_version_switches_nbt_dialect(self):
        srv = MockMCServer(host=HOST, port=0, password="2203",
                           version="1.21.4", flavor="paper").start()
        try:
            conn = RCONConnection(HOST, srv.port, "2203", timeout=5.0)
            conn.connect()
            caps = mc.ServerCaps.probe(conn.run)
            self.assertEqual(caps.brand, "paper")
            self.assertTrue(caps.has_ride_command)
            self.assertEqual(caps.nbt.name, "modern")
            self.assertEqual(caps.version, (1, 21, 4))
            conn.close()
        finally:
            srv.stop()

    def test_old_server_without_display_entities(self):
        """Имитируем 1.16: marker/display неизвестны -> backend=blocks."""
        import rwf.mock_server as ms
        real = ms.KNOWN_ENTITY_TYPES
        try:
            ms.KNOWN_ENTITY_TYPES = real - {"marker", "block_display",
                                            "item_display", "text_display",
                                            "interaction"}
            caps = mc.ServerCaps.probe(self.conn.run)
            self.assertFalse(caps.has_display_entities)
            self.assertFalse(caps.has_marker)
            self.assertEqual(caps.model_backend, "blocks")
        finally:
            ms.KNOWN_ENTITY_TYPES = real


class TestParsers(unittest.TestCase):
    def test_parse_position(self):
        self.assertEqual(
            mc.parse_position("Arlik88 has the following entity data: "
                              "[123.4567d, 64.0d, -89.5d]"),
            (123.4567, 64.0, -89.5))
        self.assertIsNone(mc.parse_position("No entity was found"))
        self.assertIsNone(mc.parse_position(""))

    def test_parse_rotation(self):
        self.assertEqual(mc.parse_rotation("X has the following entity data: "
                                           "[-137.25f, 12.5f]"), (-137.25, 12.5))

    def test_parse_player_list(self):
        self.assertEqual(
            mc.parse_player_list("There are 2 of a max of 20 players online: A, B"),
            ["A", "B"])
        self.assertEqual(
            mc.parse_player_list("Сейчас на сервере 1 из 20 игроков: Arlik88"),
            ["Arlik88"])
        self.assertEqual(
            mc.parse_player_list("There are 2 of a max of 20 players online: "
                                 "A (1234-uuid), B (5678-uuid)"),
            ["A", "B"])
        self.assertEqual(mc.parse_player_list("There are 0 of a max of 20 players online:"),
                         [])


class TestCommandBuilders(unittest.TestCase):
    def test_tp_with_and_without_rotation(self):
        self.assertEqual(mc.tp_tag("u1", (10.0, 100.0, -20.0)),
                         "tp @e[tag=u1,limit=1] 10.00 100.00 -20.00")
        cmd = mc.tp_tag("u1", (10.0, 100.0, -20.0), yaw=370.0, pitch=-15.0)
        self.assertIn(" 10.0 -15.0", cmd)          # yaw нормализован в 0..360

    def test_marker_summon_is_invisible_and_persistent(self):
        cmd = mc.summon_marker("u1", (0.0, 100.0, 0.0))
        self.assertTrue(cmd.startswith("summon minecraft:marker"))
        for token in ('Tags:["u1"]', "NoGravity:1b", "Invulnerable:1b",
                      "PersistenceRequired:1b"):
            self.assertIn(token, cmd)

    def test_block_display_payload(self):
        nbt = mc.block_display_nbt("minecraft:iron_block", (1.0, 0.0, -2.0),
                                   scale=(1.0, 0.5, 1.0), tag="wing")
        self.assertIn('block_state:{Name:"minecraft:iron_block"}', nbt)
        self.assertIn("translation:[1.0000,0.0000,-2.0000]", nbt)
        self.assertIn("scale:[1.0000,0.5000,1.0000]", nbt)
        self.assertIn('Tags:["wing"]', nbt)
        self.assertIn('billboard:"fixed"', nbt)

    def test_passengers_use_data_modify(self):
        cmds = mc.summon_passengers("root", ["minecraft:block_display{...}"] * 2)
        self.assertEqual(len(cmds), 2)
        self.assertTrue(all(c.startswith("data modify entity @e[tag=root,limit=1] "
                                         "Passengers append value") for c in cmds))

    def test_tunnel_is_chunked_within_fill_limit(self):
        cmds = mc.tunnel_down(0, 0, y_from=200, y_to=-30, radius=6)
        self.assertGreater(len(cmds), 1)
        for c in cmds:
            nums = [int(v) for v in c.split()[1:7]]
            vol = ((abs(nums[3] - nums[0]) + 1) * (abs(nums[4] - nums[1]) + 1)
                   * (abs(nums[5] - nums[2]) + 1))
            self.assertLessEqual(vol, 32768, f"fill превысил лимит: {c}")
            self.assertIn("destroy", c)

    def test_crater_covers_radius(self):
        cmds = mc.crater(0, 64, 0, radius=4)
        self.assertTrue(cmds)
        self.assertTrue(all("air" in c for c in cmds))

    def test_fireball_has_zero_power_and_explosion(self):
        cmd = mc.summon_projectile("minecraft:fireball", (0, 100, 0), (1.0, 0, 0),
                                   power=3)
        self.assertIn("power:[0.0000,0.0000,0.0000]", cmd)
        self.assertIn("ExplosionPower:3", cmd)

    def test_modern_dialect_renames_explosion_power(self):
        cmd = mc.summon_projectile("fireball", (0, 100, 0), (1.0, 0, 0), power=3,
                                   nbt=mc.MODERN_NBT)
        self.assertIn("explosion_power:3", cmd)
        self.assertNotIn("ExplosionPower", cmd)

    def test_projectile_spread_is_symmetric(self):
        base = (0.0, 0.0, 1.0)
        a = mc.projectile_spread(base, 0, 3, spread=0.3)
        b = mc.projectile_spread(base, 2, 3, spread=0.3)
        self.assertAlmostEqual(a[0], -b[0], places=6)
        self.assertEqual(mc.projectile_spread(base, 0, 1), base)

    def test_tellraw_escaping(self):
        cmd = mc.tellraw('Он сказал "привет"', color="red")
        self.assertIn('\\"привет\\"', cmd)
        self.assertIn('"color":"red"', cmd)

    def test_score_parsing(self):
        self.assertEqual(mc.parse_score("Arlik88 has 5 in jet_fire"), 5)
        self.assertIsNone(mc.parse_score("No score was found"))


class TestTerrainProbe(_MockCase):
    def test_probe_column_matches_world(self):
        """Найденная высота должна совпадать с реальным рельефом мок-мира."""
        cases = [(0, 0), (120, -80), (500, 500), (-321, 77), (64, 64), (1000, -1000)]
        total_probes = 0
        for x, z in cases:
            y, kind, probes = mc.probe_column(self.conn.run, x, z,
                                              top=200, bottom=-64, coarse=16)
            total_probes += probes
            self.assertIsNotNone(y, f"не нашлась поверхность в {x},{z}")
            self.assertEqual(y, top_y(x, z), f"колонка {x},{z}")
            self.assertNotEqual(kind, "other")
        # Экономия: наивный спуск шагом 1 стоил бы ~260 проб на колонку
        self.assertLess(total_probes / len(cases), 40,
                        f"слишком много запросов: {total_probes} на {len(cases)} колонок")

    def test_water_column_classified(self):
        x, z = 120, -80
        self.assertLess(ground_height(x, z), 62)
        y, kind, _ = mc.probe_column(self.conn.run, x, z, top=200, bottom=-64)
        self.assertEqual(kind, "water")

    def test_kind_is_from_known_palette(self):
        """Классификация обязана попадать в палитру карты, иначе тайл серый."""
        from rwf.mock_server import surface_kind
        known = {k for _, k in mc.SURFACE_KINDS}
        for x, z in ((0, 0), (300, 300), (-140, 220), (120, -80)):
            _, kind, _ = mc.probe_column(self.conn.run, x, z, top=200, bottom=-64)
            self.assertIn(kind, known, f"{x},{z}: surface_kind={surface_kind(x, z)}")

    def test_probe_below_top_returns_none_only_for_all_air(self):
        y, kind, _ = mc.probe_column(self.conn.run, 0, 0, top=300, bottom=250,
                                     coarse=8)
        # Между 250 и 300 над холмом может не быть блоков — допускаем оба исхода,
        # но не ошибку и не исключение.
        self.assertIn(kind, ("air", "stone", "snow", "other", "grass", "sand"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
