"""
Тесты Части 3: геймификация (очки/звания/достижения), пресеты запуска и
расширенный арсенал.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rwf.presets import (Preset, PresetLibrary, default_presets,
                         ensure_seeded, sanitize_name)
from rwf.progress import ACHIEVEMENTS, RANKS, SCORE, Progress
from rwf.units import VARIANTS, build_unit
from rwf.weapons import WEAPONS


class TestProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "progress.json"
        self.p = Progress(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_score_accumulates_and_session(self):
        d, _ = self.p.event("kill")
        self.assertEqual(d, SCORE["kill"])
        self.p.event("landing")
        self.assertEqual(self.p.state.score,
                         SCORE["kill"] + SCORE["landing"])
        self.assertEqual(self.p.state.session, self.p.state.score)

    def test_loss_subtracts(self):
        self.p.event("kill")
        before = self.p.state.score
        d, _ = self.p.event("loss")
        self.assertLess(d, 0)
        self.assertEqual(self.p.state.score, before + d)

    def test_rank_progression(self):
        rank, lo, hi, frac = self.p.state.rank()
        self.assertEqual(rank, RANKS[0][1])
        self.p.state.score = RANKS[1][0]
        rank2, _, _, _ = self.p.state.rank()
        self.assertEqual(rank2, RANKS[1][1])
        self.assertGreaterEqual(frac, 0.0)
        self.assertLessEqual(frac, 1.0)

    def test_first_blood_unlocks_once(self):
        _, newly = self.p.event("kill")
        self.assertIn("first_blood", newly)
        _, newly2 = self.p.event("kill")
        self.assertNotIn("first_blood", newly2)
        self.assertIn("first_blood", self.p.state.unlocked)

    def test_ace_needs_five_kills(self):
        for _ in range(4):
            self.p.event("kill")
        self.assertNotIn("ace", self.p.state.unlocked)
        self.p.event("kill")
        self.assertIn("ace", self.p.state.unlocked)

    def test_delivery_counts_tons(self):
        self.p.event("delivery", tons=4.0)
        self.p.event("delivery", tons=4.0)
        self.p.event("delivery", tons=4.0)
        self.assertEqual(self.p.state.counters["delivered_tons"], 12)
        self.assertIn("carrier_friend", self.p.state.unlocked)

    def test_persistence_roundtrip(self):
        self.p.event("kill")
        self.p.event("kill")
        self.assertTrue(self.p.save())
        other = Progress(self.path)
        self.assertEqual(other.state.score, self.p.state.score)
        self.assertEqual(other.state.unlocked, self.p.state.unlocked)
        # сессия не переносится: новый запуск начинает с нуля
        self.assertEqual(other.state.session, 0)

    def test_corrupt_file_is_safe(self):
        self.path.write_text("{не json", encoding="utf-8")
        p = Progress(self.path)
        self.assertEqual(p.state.score, 0)

    def test_listener_called(self):
        seen = []
        self.p.on_event(lambda kind, delta, newly: seen.append((kind, delta)))
        self.p.event("landing")
        self.assertEqual(seen, [("landing", SCORE["landing"])])

    def test_snapshot_shape(self):
        self.p.event("kill")
        snap = self.p.snapshot()
        for key in ("score", "session", "rank", "rank_frac", "counters",
                    "unlocked", "achievements"):
            self.assertIn(key, snap)
        self.assertEqual(len(snap["achievements"]), len(ACHIEVEMENTS))
        done = [a for a in snap["achievements"] if a["done"]]
        self.assertTrue(all(a["key"] in snap["unlocked"] for a in done))


class TestPresets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = PresetLibrary(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_load_roundtrip(self):
        p = Preset("Мой пресет", "attacker", {0: "fab500", 2: "gsh23"},
                   altitude=210.0, is_bot=True, ai="strike")
        self.lib.save(p)
        back = self.lib.load("Мой пресет")
        self.assertEqual(back.variant, "attacker")
        self.assertEqual(back.loadout, {0: "fab500", 2: "gsh23"})
        self.assertAlmostEqual(back.altitude, 210.0)
        self.assertTrue(back.is_bot)
        self.assertEqual(back.ai, "strike")

    def test_names_and_delete(self):
        self.lib.save(Preset("a", "attacker"))
        self.lib.save(Preset("b", "fighter"))
        self.assertEqual(self.lib.names(), ["a", "b"])
        self.assertTrue(self.lib.delete("a"))
        self.assertEqual(self.lib.names(), ["b"])
        self.assertFalse(self.lib.delete("a"))

    def test_by_variant(self):
        self.lib.save(Preset("a", "attacker"))
        self.lib.save(Preset("b", "fighter"))
        self.assertEqual(self.lib.by_variant("fighter"), ["b"])

    def test_warm_catalog_does_not_touch_disk(self):
        """Регресс FPS-03: `by_variant` вызывается из проекции КАЖДЫЙ кадр.

        До кэша это было 9 чтений файлов на кадр. На медленной ФС (сетевой
        диск / антивирус на каждый доступ) замер дал 375 мс на кадр и 2.4 fps
        при цели 40+. Тёплый каталог обязан отдавать данные без файлового I/O.
        """
        self.lib.save(Preset("a", "attacker"))
        self.lib.save(Preset("b", "fighter"))
        self.assertEqual(self.lib.by_variant("attacker"), ["a"])   # прогрев
        with mock.patch.object(Path, "read_text",
                               side_effect=AssertionError("чтение диска")):
            self.assertEqual(self.lib.by_variant("attacker"), ["a"])
            self.assertEqual(self.lib.by_variant("fighter"), ["b"])
            self.assertEqual(self.lib.names(), ["a", "b"])
            self.assertEqual([p.name for p in self.lib.all()], ["a", "b"])

    def test_save_invalidates_cache(self):
        """Регресс FPS-03: кэш не должен прятать только что сохранённый пресет."""
        self.lib.save(Preset("a", "attacker"))
        self.assertEqual(self.lib.names(), ["a"])       # прогрев кэша
        self.lib.save(Preset("b", "fighter"))
        self.assertEqual(self.lib.names(), ["a", "b"])
        self.assertTrue(self.lib.delete("a"))
        self.assertEqual(self.lib.names(), ["b"])

    def test_load_returns_copy_not_cached_object(self):
        """Регресс FPS-03: редактор пресетов правит объект — кэш не портится."""
        self.lib.save(Preset("a", "attacker"))
        self.assertEqual(self.lib.by_variant("attacker"), ["a"])
        edited = self.lib.load("a")
        edited.variant = "fighter"
        edited.loadout[0] = "s8"
        self.assertEqual(self.lib.load("a").variant, "attacker")
        self.assertEqual(self.lib.load("a").loadout, {})
        self.assertEqual(self.lib.by_variant("attacker"), ["a"])

    def test_reload_picks_up_external_edits(self):
        """Контракт кэша: правки в обход класса видны только после reload()."""
        self.lib.save(Preset("a", "attacker"))
        self.assertEqual(self.lib.names(), ["a"])
        self.lib.path_for("external").write_text(
            '{"format": "rwf.preset", "version": 1, "name": "external",'
            ' "variant": "fighter"}', encoding="utf-8")
        self.assertEqual(self.lib.names(), ["a"], "кэш держит снимок каталога")
        self.lib.reload()
        self.assertEqual(self.lib.names(), ["a", "external"])
        self.assertEqual(self.lib.by_variant("fighter"), ["external"])

    def test_sanitize_blocks_traversal(self):
        self.assertNotIn("..", sanitize_name("../../etc/passwd"))
        self.assertNotIn("/", sanitize_name("a/b/c"))

    def test_unknown_weapon_dropped_on_load(self):
        path = self.lib.path_for("x")
        path.write_text('{"format": "rwf.preset", "version": 1, "name": "x",'
                        ' "variant": "attacker", "loadout": {"0": "нет_такого"}}',
                        encoding="utf-8")
        # ключ остаётся (валидация по узлам происходит при применении),
        # но clean_loadout его отбросит
        p = self.lib.load("x")
        allowed = {0: ["fab500"], 1: ["s8"]}
        self.assertEqual(p.clean_loadout(allowed), {})

    def test_clean_loadout_keeps_allowed(self):
        p = Preset("t", "attacker", {0: "fab500", 1: "fab500"})
        self.assertEqual(p.clean_loadout({0: ["fab500"], 1: ["s8"]}),
                         {0: "fab500"})

    def test_ensure_seeded(self):
        self.assertEqual(self.lib.names(), [])
        n = ensure_seeded(self.lib)
        self.assertGreaterEqual(n, 5)
        # повторный вызов не пересоздаёт
        self.assertEqual(ensure_seeded(self.lib), 0)
        # все стартовые пресеты ссылаются на существующие варианты и оружие
        for preset in self.lib.all():
            self.assertIn(preset.variant, VARIANTS)
            for key in preset.loadout.values():
                self.assertIn(key, WEAPONS)

    def test_default_presets_consistent(self):
        for preset in default_presets().values():
            self.assertIn(preset.variant, VARIANTS)
            u = build_unit(preset.variant, "proto")
            cats = [m.category for m in u.mounts]
            avail = dict(u.AVAILABLE or {})
            for slot, key in preset.loadout.items():
                self.assertLess(slot, len(cats))
                self.assertIn(key, avail.get(cats[slot], []),
                              f"{preset.name}: {key} не подходит к слоту {slot}")


class TestArsenal(unittest.TestCase):
    def test_new_weapons_exist(self):
        for key in ("kab500", "s25", "vikhr", "kornet", "ags17"):
            self.assertIn(key, WEAPONS)
            self.assertIn(WEAPONS[key].get("cat"),
                          {"bomb", "rocket", "missile", "cannon", "mg"})
            self.assertTrue(WEAPONS[key].get("label"))

    def test_new_variants_build(self):
        for key, kind in (("gunship", "helicopter"), ("ifv", "apc")):
            self.assertIn(key, VARIANTS)
            u = build_unit(key, "t")
            self.assertEqual(u.spec.kind, kind)
            self.assertGreater(len(u.mounts), 0)

    def test_ifv_is_ground_with_climb_limit(self):
        spec = VARIANTS["ifv"].spec
        self.assertTrue(spec.ground_unit)
        self.assertGreater(spec.max_climb_deg, 0)

    def test_gunship_can_carry(self):
        self.assertGreater(VARIANTS["gunship"].spec.cargo_max, 0)

    def test_loadouts_only_allowed_weapons(self):
        """Загрузка варианта не содержит оружия вне его допустимых наборов."""
        for key, variant in VARIANTS.items():
            u = build_unit(key, "proto")
            avail = dict(u.AVAILABLE or {})
            cats = [m.category for m in u.mounts]
            for i, mount in enumerate(u.mounts):
                if mount.key:
                    self.assertIn(mount.key, avail.get(cats[i], []),
                                  f"{key}: слот {i} ({mount.key})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
