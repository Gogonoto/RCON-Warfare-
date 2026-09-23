"""
Обратная связь в игре: чат, actionbar, заголовки, звуки, частицы.

Возвращает то, что было потеряно при переходе с консольного скрипта на GUI
(GAME-03…GAME-06): игрок должен видеть и слышать, что происходит, а не
смотреть только в окно диспетчерской.

Все команды уходят в очередь (приоритет NORMAL/VISUAL), поэтому обратная
связь не блокирует ни физический тик, ни GUI. «Тихий режим» гасит сообщения
в общий чат, но оставляет actionbar адресату.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import mc
from .config import CombatConfig
from .rcon import CommandQueue, Priority

log = logging.getLogger(__name__)

Vec3 = Tuple[float, float, float]


class GameFX:
    """Отправка игровых сообщений и эффектов."""

    def __init__(self, queue: CommandQueue, cfg: Optional[CombatConfig] = None,
                 silent: bool = False, prefix: str = "[RWF] "):
        self.queue = queue
        self.cfg = cfg or CombatConfig()
        self.silent = silent
        self.prefix = prefix
        self.sent_chat = 0
        self.sent_actionbar = 0
        self.sent_fx = 0
        self._countdowns: Dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ чат
    def say(self, text: str, color: str = "gold", target: str = "@a") -> None:
        """Сообщение в чат. В тихом режиме или при выключенной обратной связи — молчит."""
        if self.silent or not self.cfg.chat_feedback:
            log.debug("chat (подавлен): %s", text)
            return
        self.queue.submit(mc.tellraw(text, color=color, target=target,
                                    prefix=self.prefix), Priority.NORMAL)
        self.sent_chat += 1

    def actionbar(self, player: str, text: str, color: str = "yellow") -> None:
        """Строка над хотбаром — видна только адресату, чат не засоряет."""
        if not self.cfg.chat_feedback:
            return
        self.queue.submit(mc.actionbar(player, text, color), Priority.VISUAL,
                          key=f"actionbar:{player}")
        self.sent_actionbar += 1

    def title(self, player: str, text: str, subtitle: Optional[str] = None,
              color: str = "red") -> None:
        if not self.cfg.chat_feedback:
            return
        for cmd in mc.title(player, text, color=color, subtitle=subtitle):
            self.queue.submit(cmd, Priority.NORMAL)

    # -------------------------------------------------------------- эффекты
    def sound(self, name: str, pos: Vec3, volume: float = 1.0,
              pitch: float = 1.0, target: str = "@a") -> None:
        self.queue.submit(mc.playsound(name, pos, target=target, volume=volume,
                                       pitch=pitch), Priority.VISUAL,
                          key=f"snd:{int(pos[0])}:{int(pos[2])}")
        self.sent_fx += 1

    def particles(self, name: str, pos: Vec3, count: int = 20,
                  spread: Vec3 = (1.0, 1.0, 1.0), speed: float = 0.05) -> None:
        self.queue.submit(mc.particle(name, pos, spread, speed, count),
                          Priority.VISUAL, key=f"fx:{int(pos[0])}:{int(pos[2])}")
        self.sent_fx += 1

    # ------------------------------------------------------------- сценарии
    def countdown(self, player: str, seconds: int = 3,
                  on_done: Optional[Callable[[], None]] = None,
                  label: str = "Запуск") -> None:
        """Обратный отсчёт в actionbar/title (GAME-05), в отдельном потоке."""
        if player in self._countdowns and self._countdowns[player].is_alive():
            return

        def run() -> None:
            for i in range(max(0, seconds), 0, -1):
                self.actionbar(player, f"{label} через {i}...", "yellow")
                if i <= 3:
                    self.title(player, str(i), color="red")
                    self.sound("minecraft:block.note_block.pling",
                               (0.0, 0.0, 0.0), 0.6, 1.0 + i * 0.1)
                time.sleep(1.0)
            self.title(player, "ВЗЛЁТ", subtitle=label, color="green")
            self.sound("minecraft:entity.firework_rocket.launch", (0.0, 0.0, 0.0),
                       1.0, 0.8)
            if on_done is not None:
                try:
                    on_done()
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка в колбэке отсчёта")

        t = threading.Thread(target=run, name=f"countdown-{player}", daemon=True)
        self._countdowns[player] = t
        t.start()

    def announce_spawn(self, label: str, pilot: Optional[str] = None,
                       pos: Optional[Vec3] = None) -> None:
        text = f"{label} в воздухе" + (f", пилот {pilot}" if pilot else "")
        self.say(text, "green")
        if pilot:
            self.actionbar(pilot, f"{label} запущен", "green")
        if pos:
            self.sound("minecraft:entity.ender_dragon.flap", pos, 0.8, 1.3)
            self.particles("minecraft:campfire_cosy_smoke", pos, 30,
                           (2.0, 1.0, 2.0))

    def announce_strike(self, label: str, pos: Vec3, kind: str = "bomb") -> None:
        colors = {"bomb": "gold", "nuke": "dark_red", "missile": "light_purple",
                  "strafe": "red", "kamikaze": "dark_purple"}
        self.say(f"{label}: {pos[0]:.0f} {pos[2]:.0f}",
                 colors.get(kind, "gold"))
        self.sound("minecraft:entity.generic.explode", pos, 1.0, 0.7)

    def announce_loss(self, label: str, reason: str, pos: Vec3) -> None:
        self.say(f"{label} потерян: {reason}", "red")
        self.particles("minecraft:large_smoke", pos, 60, (2.5, 2.5, 2.5), 0.05)
        self.sound("minecraft:entity.generic.explode", pos, 1.2, 0.6)

    def warn(self, text: str) -> None:
        self.say(text, "red")

    def stats(self) -> Dict[str, int]:
        return {"chat": self.sent_chat, "actionbar": self.sent_actionbar,
                "fx": self.sent_fx, "suppressed": int(self.silent)}
