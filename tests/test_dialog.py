"""Стартовое Tkinter-окно: headless-ветка и пропись результатов (UX-15/16)."""
from __future__ import annotations

import unittest

from rwf.config import AppConfig
from rwf.settings import Settings
from rwf.ui import dialog


class TestDialogHeadless(unittest.TestCase):
    def test_force_false_returns_settings(self):
        s = Settings(data={"connection": {"host": "h1", "port": 1234,
                                          "password": "pw", "mock": False}})
        res = dialog.ask_connection(s, force=False)
        self.assertEqual(res["host"], "h1")
        self.assertEqual(res["port"], 1234)
        self.assertEqual(res["password"], "pw")
        self.assertFalse(res["mock"])

    def test_apply_to(self):
        s = Settings()
        dialog.apply_to(s, {"host": "mc", "port": 25576, "password": "x",
                            "mock": True, "sound": False, "volume": 0.25})
        self.assertEqual(s.get("connection.host"), "mc")
        self.assertEqual(s.get("connection.port"), 25576)
        self.assertFalse(s.sound_enabled)
        self.assertAlmostEqual(s.volume, 0.25)
        cfg = AppConfig()
        cfg.rcon.host = s.get("connection.host")
        self.assertEqual(cfg.rcon.host, "mc")


if __name__ == "__main__":
    unittest.main()
