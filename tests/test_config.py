"""Тесты `rwf.config`: файл настроек, окружение, устойчивость к мусору.

Проект RCON Warfare. Канонический префикс переменных окружения — `RWF_`
(прежний `BOMBER_` принимается как псевдоним, регресс переименования).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rwf.config import (DEFAULT_CONFIG_PATH, ENV_PREFIX, LEGACY_ENV_PREFIX,
                        AppConfig, env_value)


def _clean_env() -> dict:
    """Окружение без наших переменных — иначе тесты влияют друг на друга."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(f"{ENV_PREFIX}_")
            and not k.startswith(f"{LEGACY_ENV_PREFIX}_")}


class TestEnvValue(unittest.TestCase):
    def test_empty_environment(self):
        with mock.patch.dict(os.environ, _clean_env(), clear=True):
            self.assertIsNone(env_value("HOST"))

    def test_canonical_prefix(self):
        with mock.patch.dict(os.environ, {"RWF_HOST": "10.0.0.1"}, clear=True):
            self.assertEqual(env_value("HOST"), "10.0.0.1")

    def test_legacy_prefix_still_accepted(self):
        """Регресс переименования: скрипты прошлых версий не должны сломаться."""
        with mock.patch.dict(os.environ, {"BOMBER_HOST": "10.0.0.9"},
                             clear=True):
            self.assertEqual(env_value("HOST"), "10.0.0.9")

    def test_canonical_wins_over_legacy(self):
        with mock.patch.dict(os.environ,
                             {"RWF_HOST": "new", "BOMBER_HOST": "old"},
                             clear=True):
            self.assertEqual(env_value("HOST"), "new")

    def test_empty_value_is_ignored(self):
        with mock.patch.dict(os.environ,
                             {"RWF_HOST": "", "BOMBER_HOST": "old"},
                             clear=True):
            self.assertEqual(env_value("HOST"), "old")


class TestApplyEnv(unittest.TestCase):
    def test_env_overrides_rcon_settings(self):
        env = {"RWF_HOST": "mc.example", "RWF_PORT": "25565",
               "RWF_PASSWORD": "secret", "RWF_MOCK": "YES",
               "RWF_TICK": "0.5"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = AppConfig.load(Path("нет-такого-файла.json"))
        self.assertEqual(cfg.rcon.host, "mc.example")
        self.assertEqual(cfg.rcon.port, 25565)
        self.assertEqual(cfg.rcon.password, "secret")
        self.assertTrue(cfg.rcon.mock)
        self.assertAlmostEqual(cfg.sim.tick, 0.5)

    def test_broken_numbers_do_not_crash(self):
        env = {"RWF_PORT": "не-число", "RWF_TICK": "мусор"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = AppConfig.load(Path("нет-такого-файла.json"))
        self.assertEqual(cfg.rcon.port, AppConfig().rcon.port)
        self.assertAlmostEqual(cfg.sim.tick, AppConfig().sim.tick)

    def test_mock_flag_forms(self):
        for raw, expected in (("1", True), ("true", True), ("on", True),
                              ("no", False)):
            with mock.patch.dict(os.environ, {"RWF_MOCK": raw}, clear=True):
                cfg = AppConfig.load(Path("нет-такого-файла.json"))
            self.assertIs(cfg.rcon.mock, expected, raw)


class TestFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rwf.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_file_name_is_rwf_json(self):
        self.assertEqual(DEFAULT_CONFIG_PATH.name, "rwf.json")

    def test_roundtrip(self):
        cfg = AppConfig()
        cfg.rcon.host = "127.0.0.1"
        cfg.rcon.port = 25575
        cfg.save(self.path)
        self.assertTrue(self.path.is_file())
        back = AppConfig.load(self.path)
        self.assertEqual(back.rcon.host, "127.0.0.1")
        self.assertEqual(back.rcon.port, 25575)
        self.assertEqual(back.path, self.path)

    def test_rwf_config_env_selects_file(self):
        custom = Path(self.tmp.name) / "custom.json"
        custom.write_text(json.dumps({"rcon": {"host": "из-файла"}}),
                          encoding="utf-8")
        with mock.patch.dict(os.environ, {"RWF_CONFIG": str(custom)},
                             clear=True):
            cfg = AppConfig.load()
        self.assertEqual(cfg.rcon.host, "из-файла")

    def test_missing_file_gives_defaults(self):
        cfg = AppConfig.load(Path(self.tmp.name) / "нет.json")
        self.assertEqual(cfg.rcon.host, AppConfig().rcon.host)
        self.assertIsNone(cfg.load_error)

    def test_broken_json_gives_defaults_and_error(self):
        self.path.write_text("{ это не json", encoding="utf-8")
        cfg = AppConfig.load(self.path)
        self.assertEqual(cfg.rcon.host, AppConfig().rcon.host)
        self.assertIsNotNone(cfg.load_error, "ошибка разбора должна быть видна")

    def test_unknown_keys_are_ignored(self):
        self.path.write_text(json.dumps({"rcon": {"host": "ok"},
                                         "мусор": 42}),
                             encoding="utf-8")
        cfg = AppConfig.load(self.path)
        self.assertEqual(cfg.rcon.host, "ok")


if __name__ == "__main__":
    unittest.main()
