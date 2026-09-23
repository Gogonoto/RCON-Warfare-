"""
Шина событий — единственный канал связи ядра с UI.

Зачем
-----
В наброске фоновые потоки трогали виджеты Qt напрямую (`cmb_pilot.clear()`
из `_refresh_players_bg`) и перебирали `self.controllers` из потока юнита.
И то и другое — гонка, которая роняет приложение случайным образом.

Правило теперь простое: ядро НИЧЕГО не знает про Qt. Оно публикует события
в `EventBus`. UI подписывается и сам маршалит их в главный поток
(в Qt — через `Signal`, который потокобезопасен при `QueuedConnection`).

    bus = EventBus()
    bus.subscribe('log', handler)          # любой поток
    bus.publish('log', 'текст', level='ok')

Подписчики вызываются в потоке публикации, поэтому они обязаны быть
быстрыми и не блокирующими. Тяжёлая работа — только через очередь UI.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Tuple

log = logging.getLogger(__name__)

# Стандартные темы событий. Держим список, чтобы UI и ядро говорили на одном языке.
TOPIC_LOG = "log"                    # (message: str, level: str)
TOPIC_CONNECTED = "rcon.connected"   # (server_info: dict)
TOPIC_DISCONNECTED = "rcon.disconnected"  # (reason: str)
TOPIC_COMMAND_FAILED = "rcon.failed"      # (command: str, error: str)
TOPIC_PLAYER_ADDED = "player.added"       # (name: str, pos: tuple)
TOPIC_PLAYER_REMOVED = "player.removed"   # (name: str)
TOPIC_UNIT_ADDED = "unit.added"           # (uid: int)
TOPIC_UNIT_REMOVED = "unit.removed"       # (uid: int)
TOPIC_UNIT_UPDATED = "unit.updated"       # (uid: int)
TOPIC_ROUTE_SET = "route.set"             # (uid: int)
TOPIC_SCAN_PROGRESS = "scan.progress"     # (done: int, total: int)
TOPIC_SCAN_FINISHED = "scan.finished"     # (tiles: int)
TOPIC_WEAPON_FIRED = "weapon.fired"       # (uid: int, weapon: str)
TOPIC_WORLD_CHANGED = "world.changed"     # (revision: int)

Handler = Callable[..., Any]


class Subscription:
    """Жетон подписки. `sub.unsubscribe()` снимает подписку из любого потока."""

    __slots__ = ("_bus", "_topic", "_handler", "_alive")

    def __init__(self, bus: "EventBus", topic: str, handler: Handler):
        self._bus = bus
        self._topic = topic
        self._handler = handler
        self._alive = True

    def unsubscribe(self) -> None:
        if self._alive:
            self._alive = False
            self._bus._remove(self._topic, self._handler)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc) -> None:
        self.unsubscribe()


class EventBus:
    """Потокобезопасная pub/sub шина с поддержкой групповых подписок."""

    def __init__(self, swallow_errors: bool = True):
        self._lock = threading.RLock()
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)
        self._wildcard: List[Handler] = []
        # Глотать исключения подписчиков: одна кривая подписка не должна
        # убивать физический тик юнита.
        self._swallow = swallow_errors
        self.published = 0

    # ------------------------------------------------------------------ API
    def subscribe(self, topic: str, handler: Handler) -> Subscription:
        """Подписаться на тему. topic='*' — получать все события."""
        with self._lock:
            if topic == "*":
                if handler not in self._wildcard:
                    self._wildcard.append(handler)
            else:
                if handler not in self._handlers[topic]:
                    self._handlers[topic].append(handler)
        return Subscription(self, topic, handler)

    def subscribe_many(self, topics: Iterable[str], handler: Handler) -> List[Subscription]:
        return [self.subscribe(t, handler) for t in topics]

    def unsubscribe_all(self) -> None:
        with self._lock:
            self._handlers.clear()
            self._wildcard.clear()

    def publish(self, topic: str, *args: Any, **kwargs: Any) -> None:
        """Опубликовать событие. Вызывает подписчиков синхронно в этом потоке."""
        with self._lock:
            handlers = list(self._handlers.get(topic, ()))
            wildcard = list(self._wildcard)
            self.published += 1
        for h in handlers:
            self._call(h, topic, args, kwargs)
        for h in wildcard:
            self._call(h, topic, (topic,) + args, kwargs)

    # ------------------------------------------------------------- внутреннее
    def _call(self, handler: Handler, topic: str, args: Tuple, kwargs: Dict) -> None:
        try:
            handler(*args, **kwargs)
        except Exception:  # noqa: BLE001 - изолируем подписчиков друг от друга
            log.exception("Обработчик события %r упал", topic)
            if not self._swallow:
                raise

    def _remove(self, topic: str, handler: Handler) -> None:
        with self._lock:
            if topic == "*":
                if handler in self._wildcard:
                    self._wildcard.remove(handler)
            else:
                lst = self._handlers.get(topic)
                if lst and handler in lst:
                    lst.remove(handler)

    # -------------------------------------------------------------- отладка
    def topics(self) -> List[str]:
        with self._lock:
            return sorted(self._handlers.keys())

    def counts(self) -> Dict[str, int]:
        with self._lock:
            out = {t: len(h) for t, h in self._handlers.items() if h}
            out["*"] = len(self._wildcard)
            return out
