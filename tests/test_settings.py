"""Настройки: merge дефолтов, точечный доступ, стойкость к битому файлу."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rwf.settings import DEFAULTS, Settings


class TestSettings(unittest.TestCase):
    def test_defaults(self):
        s = Settings(data=None)
        self.assertTrue(s.sound_enabled)
        self.assertAlmostEqual(s.volume, 0.7)
        self.assertTrue(s.sound_event_enabled("fire"))

    def test_merge_and_dotted(self):
        s = Settings(data={"sound": {"volume": 0.3},
                           "custom": {"x": 1}})
        self.assertAlmostEqual(s.volume, 0.3)
        self.assertTrue(s.sound_enabled)          # дефолт дополнен
        self.assertEqual(s.get("custom.x"), 1)
        s.set("sound.events.fire", False)
        self.assertFalse(s.sound_event_enabled("fire"))
        self.assertIsNone(s.get("no.such.key"))
        self.assertEqual(s.get("no.such.key", 5), 5)

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "settings.json"
            s = Settings(path=p)
            s.set("connection.host", "mc.local")
            s.set("sound.volume", 0.42)
            self.assertTrue(s.save())
            s2 = Settings.load(p)
            self.assertEqual(s2.get("connection.host"), "mc.local")
            self.assertAlmostEqual(s2.volume, 0.42)

    def test_corrupt_file_degrades(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text("{не json", encoding="utf-8")
            s = Settings.load(p)
            self.assertTrue(s.load_error)
            self.assertTrue(s.sound_enabled)      # дефолты живы

    def test_volume_clamped(self):
        s = Settings(data={"sound": {"volume": 7.0}})
        self.assertAlmostEqual(s.volume, 1.0)
        s.set("sound.volume", -3)
        self.assertAlmostEqual(s.volume, 0.0)

    def test_defaults_immutable(self):
        s = Settings()
        s.set("sound.volume", 0.1)
        self.assertAlmostEqual(DEFAULTS["sound"]["volume"], 0.7)


if __name__ == "__main__":
    unittest.main()
