"""
Трекер игроков: кто онлайн и где они.

Что исправлено относительно наброска
------------------------------------
* **Один заход вместо N.** Было: `list`, затем по одному `data get entity <ник> Pos`
  на каждого игрока, последовательно. При 5 игроках и RTT 30 мс это 180 мс на
  опрос — трекер сам становился узким местом. Теперь позиции запрашиваются
  пачкой через `run_many` (pipelining): 2 сетевых захода на любой онлайн.
* **Вышедшие удаляются.** `world.sync_players()` убирает тех, кого нет в `list`,
  иначе на карте навсегда оставались «призраки».
* **Парсинг вынесен в `mc.parse_*`** — устойчив к локали сервера и к формату
  `Nick (uuid)` у `list uuids`.
* **Ошибки не убивают поток.** Обрыв связи логируется, трекер продолжает
  работать и сам переподключается через пул.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from . import mc
from .events import EventBus
from .rcon import RCONError, RCONPool
from .world import World

log = logging.getLogger(__name__)


class PlayerTracker:
    """Фоновый опрос списка игроков и их координат."""

    def __init__(
        self,
        pool: RCONPool,
        world: World,
        bus: Optional[EventBus] = None,
        interval: float = 0.8,
        fetch_rotation: bool = True,
        fetch_health: bool = False,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.pool = pool
        self.world = world
        self.bus = bus if bus is not None else world.bus
        self.interval = max(0.1, interval)
        self.fetch_rotation = fetch_rotation
        self.fetch_health = fetch_health
        self._on_error = on_error

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.polls = 0
        self.failures = 0
        self.last_poll_ms = 0.0
        self.last_error: str = ""

    # ---------------------------------------------------------------- запуск
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="player-tracker",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ цикл
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except RCONError as exc:
                self.failures += 1
                self.last_error = str(exc)
                log.warning("Трекер игроков: %s", exc)
                if self._on_error:
                    self._on_error(str(exc))
                # Не долбим сервер: пауза длиннее обычного интервала.
                self._stop.wait(min(5.0, self.interval * 4))
                continue
            except Exception as exc:  # noqa: BLE001 - поток не должен умирать
                self.failures += 1
                self.last_error = repr(exc)
                log.exception("Трекер игроков: неожиданная ошибка")
                if self._on_error:
                    self._on_error(repr(exc))
            self._stop.wait(self.interval)

    # ------------------------------------------------------------- один опрос
    def poll_once(self) -> Dict[str, tuple]:
        """Синхронный опрос. Возвращает {ник: (x, y, z)} — удобно для тестов."""
        started = time.monotonic()
        names = mc.parse_player_list(self.pool.run("list"))
        self.world.sync_players(names)
        if not names:
            self.polls += 1
            self.last_poll_ms = (time.monotonic() - started) * 1000.0
            return {}

        # Собираем все запросы в одну пачку: 1 сетевой заход на позиции,
        # ещё один — на повороты (если нужны).
        pos_cmds = [mc.get_pos_cmd(n) for n in names]
        extra_cmds: List[str] = []
        if self.fetch_rotation:
            extra_cmds += [mc.get_rot_cmd(n) for n in names]
        if self.fetch_health:
            extra_cmds += [f"data get entity {n} Health" for n in names]

        responses = self.pool.run_many(pos_cmds + extra_cmds)
        pos_resp = responses[:len(names)]
        rot_resp = responses[len(names):len(names) * 2] if self.fetch_rotation else []
        hp_resp = responses[-len(names):] if self.fetch_health else []

        found: Dict[str, tuple] = {}
        for i, name in enumerate(names):
            pos = mc.parse_position(pos_resp[i])
            if pos is None:
                continue                      # игрок вышел между запросами
            yaw = pitch = 0.0
            if rot_resp:
                rot = mc.parse_rotation(rot_resp[i])
                if rot:
                    yaw, pitch = rot
            health = 20.0
            if hp_resp:
                m = hp_resp[i]
                try:
                    health = float(m.rsplit(":", 1)[-1].strip().rstrip("f"))
                except (ValueError, AttributeError):
                    health = 20.0
            self.world.set_player(name, pos, yaw, pitch, health)
            found[name] = pos

        self.polls += 1
        self.last_poll_ms = (time.monotonic() - started) * 1000.0
        return found

    # ---------------------------------------------------------------- статус
    def stats(self) -> Dict[str, float]:
        return {
            "polls": self.polls, "failures": self.failures,
            "last_poll_ms": round(self.last_poll_ms, 1),
            "interval": self.interval,
        }
