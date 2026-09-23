"""
Композиционный корень приложения.

Собирает весь стек и управляет его жизненным циклом:

    AppConfig -> RCONPool -> CommandQueue -> World -> WeaponSystem
                        \\-> ServerCaps      \\-> UnitEngine
                         -> PlayerTracker    -> TerrainScanner

Отдельный модуль нужен, чтобы ни CLI, ни будущий UI не собирали связку вручную
(именно ручная сборка в `ui.py` наброска породила дефекты UI-02, UI-03, UI-05:
порядок инициализации и остановки нигде не был зафиксирован).

Порядок остановки — строго обратный порядку запуска: сначала перестаём
генерировать команды (движок, трекер, сканер), потом сливаем очередь, затем
закрываем соединения.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .config import AppConfig
from .engine import UnitEngine
from .events import EventBus
from .mc import ServerCaps
from .mock_server import MockMCServer
from .rcon import CommandQueue, RCONError, RCONPool
from .scanner import TerrainScanner
from .tracker import PlayerTracker
from .weapons import WeaponSystem
from .world import World

log = logging.getLogger(__name__)


class ConnectionError_(RCONError):
    """Не удалось подключиться к серверу."""


class Application:
    """Владеет всем стеком. Один экземпляр на процесс."""

    def __init__(self, cfg: Optional[AppConfig] = None,
                 bus: Optional[EventBus] = None):
        self.cfg = cfg or AppConfig()
        self.bus = bus or EventBus()
        self.world = World(bus=self.bus)

        self.pool: Optional[RCONPool] = None
        self.queue: Optional[CommandQueue] = None
        self.weapons: Optional[WeaponSystem] = None
        self.engine: Optional[UnitEngine] = None
        self.tracker: Optional[PlayerTracker] = None
        self.scanner: Optional[TerrainScanner] = None
        self.gun_poller = None        # PlayerGunPoller (создаётся в connect)
        self.manpads = None           # ManpadsSystem (создаётся в connect)
        self.caps: Optional[ServerCaps] = None
        self._mock: Optional[MockMCServer] = None
        self._started = False

    # ------------------------------------------------------------ подключение
    def connect(self, use_mock: Optional[bool] = None) -> ServerCaps:
        """Подключиться к серверу, опросить возможности и собрать стек.

        `use_mock=True` поднимает локальный имитатор вместо настоящего сервера.
        """
        if self._started:
            return self.caps or ServerCaps()
        mock = self.cfg.rcon.mock if use_mock is None else use_mock
        if mock:
            self._mock = MockMCServer(
                host=self.cfg.rcon.host, port=0,
                password=self.cfg.rcon.password,
                version=self.cfg.rcon.mock_version,
                flavor=self.cfg.rcon.mock_flavor,
                players=tuple(self.cfg.rcon.mock_players),
            ).start()
            self.cfg.rcon.port = self._mock.port
            log.info("Имитатор сервера поднят на %s:%d", self.cfg.rcon.host,
                     self._mock.port)

        c = self.cfg.rcon
        self.pool = RCONPool(c.host, c.port, c.password, size=c.pool_size,
                             timeout=c.timeout, on_event=self._on_command_failed)
        try:
            self.pool.connect_all()
        except RCONError as exc:
            self.pool.close()
            self.pool = None
            if self._mock:
                self._mock.stop()
                self._mock = None
            raise ConnectionError_(
                f"Не удалось подключиться к {c.host}:{c.port} — {exc}. "
                f"Проверьте enable-rcon, rcon.port и rcon.password "
                f"в server.properties.") from exc

        self.caps = ServerCaps.probe(self.pool.run,
                                     assume_version=c.mock_version)
        if self.caps.model_backend != "blocks":
            log.info("Сервер поддерживает display-сущности, но по выбору "
                     "заказчика используется блочная модель.")

        self.queue = CommandQueue(
            self.pool, workers=c.queue_workers, rate=c.queue_rate,
            batch_size=c.batch_size, on_event=self._on_command_failed)
        self.queue.start()

        self.weapons = WeaponSystem(self.cfg.combat, self.queue, self.world,
                                    bus=self.bus, caps=self.caps)
        self.engine = UnitEngine(self.world, self.queue, self.weapons,
                                 cfg=self.cfg, bus=self.bus)
        self.engine.set_tick(self.cfg.sim.tick)
        # Огонь из игры и ПЗРК — ванильные scoreboard-триггеры (rwf_fire,
        # rwf_lock, rwf_missile). Создаются здесь, а не в UI: composition root
        # обязан собирать весь боевой стек независимо от интерфейса.
        from .combat import PlayerGunPoller
        from .manpads import ManpadsSystem
        self.gun_poller = PlayerGunPoller(self.world, self.engine.combat,
                                          self.pool,
                                          interval=self.cfg.combat.gun_poll_interval,
                                          enabled=self.cfg.combat.player_gun_enabled)
        self.manpads = ManpadsSystem(self.world, self.engine.missiles,
                                     pool=self.pool, queue=self.queue,
                                     interval=self.cfg.combat.manpads_poll_interval,
                                     enabled=self.cfg.combat.manpads_enabled,
                                     bus=self.bus)
        self.tracker = PlayerTracker(self.pool, self.world, bus=self.bus,
                                     on_error=self._on_error)
        self.scanner = TerrainScanner(
            self.pool, self.world, bus=self.bus,
            top=self.cfg.scanner.top_y, bottom=self.cfg.scanner.bottom_y,
            coarse=self.cfg.scanner.coarse, batch=self.cfg.scanner.batch_size,
            max_columns=self.cfg.scanner.max_columns,
            on_error=self._on_error)
        self._started = True
        log.info("Стек собран: %s", self.caps.describe())
        return self.caps

    def disconnect(self, flush_timeout: float = 3.0) -> None:
        """Остановить всё в обратном порядке."""
        if not self._started:
            return
        if self.engine:
            self.engine.stop()
            self.engine.join(timeout=2.0)
        if getattr(self, "gun_poller", None):
            self.gun_poller.stop(timeout=2.0)
        if getattr(self, "manpads", None):
            self.manpads.stop(timeout=2.0)
        if self.tracker:
            self.tracker.stop(timeout=2.0)
        if self.scanner:
            self.scanner.stop()
        if self.queue:
            self.queue.flush(timeout=flush_timeout)
            self.queue.stop(timeout=2.0, flush=False)
        if self.pool:
            self.pool.close()
        if self._mock:
            self._mock.stop()
            self._mock = None
        self._started = False
        log.info("Стек остановлен")

    def shutdown(self) -> None:
        self.disconnect()
        self.bus.unsubscribe_all()

    # ------------------------------------------------------------- shortcuts
    def start(self) -> "Application":
        """Подключиться и запустить фоновые потоки (движок + трекер)."""
        self.connect()
        if self.engine:
            self.engine.start()
        if self.tracker:
            self.tracker.start()
        if self.gun_poller:
            self.gun_poller.start()
        if self.manpads:
            self.manpads.start()
        return self

    @property
    def started(self) -> bool:
        return self._started

    @property
    def mock(self) -> Optional[MockMCServer]:
        return self._mock

    def spawn(self, variant: str, pos: Tuple[float, float, float],
              heading: float = 0.0, is_bot: bool = False, **kw: Any):
        assert self.engine is not None, "сначала connect()"
        return self.engine.spawn(variant, pos, heading=heading, is_bot=is_bot, **kw)

    def spawn_at_player(self, variant: str, player: str, altitude: float = 150.0,
                        is_bot: bool = False, **kw: Any):
        """Запуск над игроком — основной сценарий из исходного ТЗ."""
        assert self.engine is not None and self.pool is not None
        from . import mc
        pos = mc.parse_position(self.pool.run(mc.get_pos_cmd(player)))
        if pos is None:
            raise ValueError(f"Игрок {player!r} не найден или офлайн")
        return self.engine.spawn(variant, (pos[0], altitude, pos[2]),
                                 heading=0.0, is_bot=is_bot, **kw)

    def scan(self, center: Optional[Tuple[float, float]] = None,
             radius: Optional[int] = None, step: Optional[int] = None) -> bool:
        assert self.scanner is not None
        if center is None:
            center = self.focus_center()
        return self.scanner.start(center, radius or self.cfg.scanner.radius,
                                  step or self.cfg.scanner.step)

    def focus_center(self) -> Tuple[float, float]:
        """Точка интереса: первый юнит, иначе первый игрок, иначе (0, 0)."""
        units = self.engine.units() if self.engine else []
        if units:
            return (units[0].pos[0], units[0].pos[2])
        players = self.world.get_players()
        if players:
            p = next(iter(players.values()))["pos"]
            return (p[0], p[2])
        return (0.0, 0.0)

    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "started": self._started,
            "server": self.caps.describe() if self.caps else "не подключено",
            "players": len(self.world.get_players()),
            "units": len(self.engine.units()) if self.engine else 0,
            "terrain_tiles": len(self.world.terrain),
        }
        if self.pool:
            out["rcon"] = self.pool.stats()
        if self.queue:
            out["queue"] = self.queue.stats()
        if self.engine:
            out["engine"] = self.engine.stats.as_dict()
            out["weapons"] = self.weapons.stats() if self.weapons else {}
        if self.tracker:
            out["tracker"] = self.tracker.stats()
        if self.manpads:
            out["manpads"] = self.manpads.stats()
        return out

    # --------------------------------------------------------------- события
    def _on_command_failed(self, command: str, error: str) -> None:
        log.warning("Команда не выполнена: %s | %s", error[:120], command[:80])
        self.bus.publish("rcon.failed", command, error)

    def _on_error(self, message: str) -> None:
        log.warning("%s", message)
        self.bus.publish("rcon.failed", "", message)

    # ------------------------------------------------------------- контекст
    def __enter__(self) -> "Application":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.shutdown()
