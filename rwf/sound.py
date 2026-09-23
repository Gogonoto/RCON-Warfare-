"""
Звук RCON Warfare (SND-01…SND-06, BUGS.md раздел H).

Без внешних зависимостей: сигналы синтезируются в PCM 16 бит моно 22050 Гц
(`wave` + `math` из stdlib) и отдаются бэкенду:

    1. pygame.mixer            — если установлен и есть устройство;
    2. CLI-плеер (paplay/aplay/afplay/ffplay) — subprocess, fire-and-forget;
    3. NullBackend             — устройства нет: игра молчит, но API живой
       ( headless-скриншоты и тесты не должны падать — SND-05 ).

Громкость применяется на этапе синтеза (кэш сэмплов пересоздаётся при смене
громкости бакетами по 0.1 — SND-04). Воспроизведение НЕ блокирует ни главный
поток UI, ни поток движка: события уходят в очередь одного рабочего потока.

События: ui, fire, explosion, spawn, landing, lock, alarm. Каждое можно
отключить в настройках (`sound.events.<имя>`), мастер выключается
`sound.enabled`.
"""
from __future__ import annotations

import io
import logging
import math
import queue
import shutil
import subprocess
import threading
import wave
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

RATE = 22050
EVENTS = ("ui", "fire", "explosion", "spawn", "landing", "lock", "alarm")

# тема шины ядра -> звуковое событие
BUS_EVENTS = {
    "combat": "fire",
    "missile.hit": "explosion",
    "unit.landed": "landing",
    "unit.launched": "landing",
    "unit.recovery": "landing",
    "manpads.lock": "lock",
    "manpads.launch": "fire",
    "unit.destroyed": "explosion",
}


# ---------------------------------------------------------------------------
#  Синтез
# ---------------------------------------------------------------------------
def _fade(n: int, i: int, attack: int, release: int) -> float:
    """Трапеция 0..1:Attack..Release..n — убирает щелчки краёв."""
    if i < attack:
        return i / max(1, attack)
    if i > n - release:
        return max(0.0, (n - i) / max(1, release))
    return 1.0


def _render(duration: float, gen) -> List[float]:
    n = max(1, int(RATE * duration))
    attack = min(n // 8, int(RATE * 0.005))
    release = min(n // 3, int(RATE * 0.05))
    return [gen(i / RATE, i) * _fade(n, i, attack, release)
            for i in range(n)]


def _noise(seed: int) -> float:
    """Детерминированный псевдослучайный шум (воспроизводимость тестов)."""
    x = math.sin(seed * 12.9898) * 43758.5453
    return (x - math.floor(x)) * 2.0 - 1.0


def _gen_ui(t: float, i: int) -> float:
    # короткий сухой «тик» панели: 1.6 кГц + обертон
    return 0.35 * math.sin(2 * math.pi * 1600 * t) + \
        0.12 * math.sin(2 * math.pi * 3200 * t)


def _gen_fire(t: float, i: int) -> float:
    # выстрел: шумовой импульс с быстрым спадом + низкая составляющая
    env = math.exp(-t * 26.0)
    return env * (0.55 * _noise(i) + 0.45 * math.sin(2 * math.pi * 110 * t))


def _gen_explosion(t: float, i: int) -> float:
    # взрыв: долгий шумовой спад + гул 45 Гц
    env = math.exp(-t * 6.5)
    return env * (0.6 * _noise(i // 2) + 0.5 * math.sin(2 * math.pi * 45 * t))


def _gen_spawn(t: float, i: int) -> float:
    # запуск: восходящий свип 300->900 Гц
    f = 300.0 + 600.0 * min(1.0, t / 0.35)
    return 0.4 * math.sin(2 * math.pi * f * t)


def _gen_landing(t: float, i: int) -> float:
    # посадка/обслуживание: двухтоновый положительный сигнал
    f = 660.0 if t < 0.12 else 880.0
    return 0.38 * math.sin(2 * math.pi * f * t)


def _gen_lock(t: float, i: int) -> float:
    # захват ПЗРК: двойной писк 1.2 кГц
    on = (t % 0.16) < 0.09
    return 0.4 * math.sin(2 * math.pi * 1200 * t) if on else 0.0


def _gen_alarm(t: float, i: int) -> float:
    # авария: сирена 700/950 Гц
    f = 700.0 if (t % 0.4) < 0.2 else 950.0
    return 0.34 * math.sin(2 * math.pi * f * t)


_GENS = {
    "ui": (0.07, _gen_ui),
    "fire": (0.28, _gen_fire),
    "explosion": (0.85, _gen_explosion),
    "spawn": (0.42, _gen_spawn),
    "landing": (0.3, _gen_landing),
    "lock": (0.34, _gen_lock),
    "alarm": (0.6, _gen_alarm),
}


def synth_wav(event: str, volume: float = 0.7) -> bytes:
    """Синтезировать событие в байты WAV (16 бит моно 22050 Гц)."""
    duration, gen = _GENS[event]
    samples = _render(duration, gen)
    vol = max(0.0, min(1.0, volume))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        frames = bytearray()
        for s in samples:
            v = int(max(-1.0, min(1.0, s * vol)) * 32767)
            frames += v.to_bytes(2, "little", signed=True)
        w.writeframes(bytes(frames))
    return buf.getvalue()


# ---------------------------------------------------------------------------
#  Бэкенды
# ---------------------------------------------------------------------------
class NullBackend:
    name = "null"

    def play(self, wav: bytes) -> None:  # noqa: D401
        return

    def close(self) -> None:
        return


class CliBackend:
    """Fire-and-forget через системный плеер (SND-02)."""

    def __init__(self, cmd: List[str]):
        self.cmd = cmd
        self.name = f"cli:{cmd[0]}"

    def play(self, wav: bytes) -> None:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            path = f.name
        try:
            subprocess.Popen([*self.cmd, path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            log.debug("звук: плеер %s недоступен", self.cmd[0])

    def close(self) -> None:
        return


class PygameBackend:
    def __init__(self):
        import pygame  # noqa: PLC0415 - опциональная зависимость
        pygame.mixer.init(frequency=RATE, channels=1)
        self._pygame = pygame
        self.name = "pygame"

    def play(self, wav: bytes) -> None:
        import io as _io
        snd = self._pygame.mixer.Sound(file=_io.BytesIO(wav))
        snd.play()

    def close(self) -> None:
        try:
            self._pygame.mixer.quit()
        except Exception:  # noqa: BLE001
            pass


def pick_backend(preferred: Optional[str] = None):
    """Выбор бэкенда: pygame -> CLI-плеер -> null (SND-05)."""
    if preferred == "null":
        return NullBackend()
    if preferred != "cli":
        try:
            return PygameBackend()
        except Exception:  # noqa: BLE001
            pass
    for cmd in (["paplay"], ["aplay", "-q"], ["afplay"],
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(cmd[0]):
            return CliBackend(cmd)
    return NullBackend()


# ---------------------------------------------------------------------------
#  Менеджер
# ---------------------------------------------------------------------------
class SoundManager:
    """Очередь событий + один рабочий поток (не блокирует UI, SND-03)."""

    def __init__(self, settings=None, backend=None):
        self.settings = settings
        self._backend = backend
        self._q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=256)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cache: Dict[str, bytes] = {}
        self._cache_vol = -1.0
        self.played: List[str] = []      # для тестов/отладки
        self.backend_name = "null"

    # ------------------------------------------------------------- цикл
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self._backend is None:
            self._backend = pick_backend()
        self.backend_name = getattr(self._backend, "name", "null")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="rwf-sound",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None
        if self._backend:
            try:
                self._backend.close()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                wav = self._wav(event)
                self._backend.play(wav)
                self.played.append(event)
            except Exception:  # noqa: BLE001 - звук не роняет приложение
                log.exception("звук: ошибка воспроизведения %s", event)

    def _wav(self, event: str) -> bytes:
        vol = self.settings.volume if self.settings else 0.7
        bucket = round(vol * 10) / 10.0
        if bucket != self._cache_vol:
            self._cache.clear()
            self._cache_vol = bucket
        if event not in self._cache:
            self._cache[event] = synth_wav(event, bucket)
        return self._cache[event]

    # ------------------------------------------------------------- API
    def play(self, event: str) -> None:
        """Неблокирующий запрос звука; фильтруется настройками."""
        if event not in _GENS:
            return
        if self.settings is not None:
            if not self.settings.sound_enabled:
                return
            if not self.settings.sound_event_enabled(event):
                return
        try:
            self._q.put_nowait(event)
        except queue.Full:  # переполнение — молча пропускаем (SND-06)
            pass

    def bus_hook(self, topic: str, *args, **kwargs) -> None:
        """Подписка на шину ядра: тема -> событие."""
        event = BUS_EVENTS.get(topic)
        if event:
            self.play(event)
