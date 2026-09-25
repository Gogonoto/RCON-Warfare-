"""Быстрый запуск UI с имитатором — для локальной проверки."""
from rwf.config import AppConfig
from rwf.ui.app import run

cfg = AppConfig()
cfg.rcon.mock = True              # поднять MockMCServer
cfg.rcon.mock_players = ("Arlik88", "Steve")

raise SystemExit(run(cfg, autoconnect=True))