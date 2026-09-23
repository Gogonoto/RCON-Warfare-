"""
Сканер рельефа: снимает высоты и типы блоков вокруг точки и кладёт в `World`.

Почему не так, как в наброске
-----------------------------
Набросок на каждую колонку делал ~56 запросов `execute if block` по одному
(спуск шагом 8 + уточнение) и ещё до 10 на классификацию. Для области
радиусом 256 с шагом 8 это 4096 колонок × ~66 ≈ **270 000 сетевых заходов**.
На локальном сервере — минуты, на удалённом — десятки минут. Плюс 6 потоков,
которые брали соединения из round-robin пула и смешивали пакеты.

Здесь применяется **пакетное зондирование по уровням**: вместо того чтобы вести
каждую колонку сверху вниз отдельно, мы спрашиваем одну и ту же высоту сразу у
всех незакрытых колонок одним `run_many` (pipelining). Тогда число сетевых
заходов зависит от ГЛУБИНЫ поиска, а не от числа колонок:

    фаза A: грубый спуск, шаг 16, y=200..-64               ~ 17 заходов
    фаза B: бинарное уточнение в окне 16 блоков            ~  4 захода
    фаза C: классификация по ~10 типам с ранним выходом    ~  4-10 заходов
    ---------------------------------------------------------------
    итого ~30 заходов на всю область (при ~25 командах на колонку)

При тех же 4096 колонках это ~600 сетевых заходов вместо 270 000 — три порядка.

Многопоточность намеренно НЕ используется: сервер Minecraft исполняет команды
в одном потоке, поэтому параллельные RCON-соединения не ускоряют работу, а лишь
создают очередь и гонки. Один поток + большие пачки — оптимум.

Результат пишется в мир постепенно: после фазы B карта уже показывает высоты
(тайлы с kind='other'), фаза C лишь уточняет типы. Отмена сохраняет то, что
успело найтись.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import mc
from .events import TOPIC_SCAN_PROGRESS, EventBus
from .rcon import RCONError, RCONPool
from .world import World

log = logging.getLogger(__name__)

Coord = Tuple[int, int]


@dataclass
class ScanResult:
    tiles: int = 0
    columns: int = 0
    commands: int = 0
    roundtrips: int = 0
    seconds: float = 0.0
    cancelled: bool = False

    def describe(self) -> str:
        return (f"{self.tiles} тайлов / {self.columns} колонок за {self.seconds:.1f} с "
                f"({self.commands} команд, {self.roundtrips} сетевых заходов)")


class TerrainScanner:
    """Сканирование рельефа: фоновое (`start`) или блокирующее (`scan_sync`)."""

    def __init__(
        self,
        pool: RCONPool,
        world: World,
        bus: Optional[EventBus] = None,
        top: int = 200,
        bottom: int = -64,
        coarse: int = 16,
        batch: int = 128,
        kinds: Sequence[Tuple[str, str]] = mc.SURFACE_KINDS,
        max_columns: int = 200000,
        progress_every: int = 64,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.pool = pool
        self.world = world
        self.bus = bus if bus is not None else world.bus
        self.top = top
        self.bottom = bottom
        self.coarse = max(1, coarse)
        self.batch = max(1, batch)
        self.kinds = list(kinds)
        self.max_columns = max_columns
        self.progress_every = max(1, progress_every)
        self._on_error = on_error

        self._stop = threading.Event()
        self._last_reported = -1
        self._thread: Optional[threading.Thread] = None
        self.scanning = False
        self.last: Optional[ScanResult] = None
        self.error: str = ""

    # ---------------------------------------------------------------- запуск
    def start(self, center: Tuple[float, float], radius: int, step: int = 8) -> bool:
        """Запустить фоновое сканирование.

        False — если сканирование уже идёт или область больше `max_columns`.
        """
        if self.scanning:
            self.error = "Сканирование уже идёт."
            return False
        columns = self.column_grid(center, radius, step)
        if not columns:
            self.error = "Пустая область сканирования."
            return False
        if len(columns) > self.max_columns:
            self.error = (f"Слишком большая область: {len(columns)} колонок "
                          f"(лимит {self.max_columns}). Увеличьте шаг.")
            log.warning("%s", self.error)
            if self._on_error:
                self._on_error(self.error)
            return False

        self._stop.clear()
        self.error = ""
        self.scanning = True
        self._last_reported = -1
        self.world.begin_scan(total=len(columns), step=step)
        self._thread = threading.Thread(target=self._run, args=(columns,),
                                        name="terrain-scanner", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._stop.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """True — поток завершился, False — ещё работает (таймаут)."""
        if not self._thread:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def stop(self) -> None:
        """Отменить и дождаться остановки."""
        self.cancel()
        self.wait(timeout=5.0)

    # ------------------------------------------------------------------ сетка
    def column_grid(self, center: Tuple[float, float], radius: int,
                    step: int) -> List[Coord]:
        step = max(1, int(step))
        cx, cz = int(center[0]), int(center[1])
        r = max(step, int(radius))
        return [(x, z) for x in range(cx - r, cx + r + 1, step)
                for z in range(cz - r, cz + r + 1, step)]

    # ------------------------------------------------------------------- ядро
    def scan_sync(self, center: Tuple[float, float], radius: int,
                  step: int = 8) -> ScanResult:
        """Блокирующее сканирование — для CLI и тестов."""
        columns = self.column_grid(center, radius, step)
        self._stop.clear()
        self._last_reported = -1
        self.scanning = True
        self.world.begin_scan(total=len(columns), step=step)
        try:
            return self._scan_columns(columns)
        finally:
            self.scanning = False

    def _run(self, columns: List[Coord]) -> None:
        try:
            self._scan_columns(columns)
        except Exception as exc:  # noqa: BLE001 - поток не должен умирать молча
            self.error = repr(exc)
            log.exception("Сканер упал")
            if self._on_error:
                self._on_error(repr(exc))
            self.world.end_scan()
        finally:
            # Регресс наброска: флаг `running` не сбрасывался, и повторный
            # запуск молча отказывался работать.
            self.scanning = False

    def _scan_columns(self, columns: List[Coord]) -> ScanResult:
        started = time.monotonic()
        res = ScanResult(columns=len(columns))

        # --- фаза A: грубый спуск по уровням -------------------------------
        found: Dict[Coord, int] = {}
        active: List[Coord] = list(columns)
        y = self.top
        while active and y >= self.bottom:
            if self._stop.is_set():
                break
            air = self._probe_list([(c, y) for c in active], "minecraft:air", res)
            if self._stop.is_set():
                # Пачка оборвана на середине: её результат неполный, а
                # незаполненные позиции помечены «не воздух». Не учитываем —
                # иначе в частичный результат попадут выдуманные высоты.
                break
            still_active: List[Coord] = []
            for col, is_air in zip(active, air):
                if is_air:
                    still_active.append(col)
                else:
                    found[col] = y
            active = still_active
            self._progress(len(found), res.columns)
            y -= self.coarse

        # --- фаза B: бинарное уточнение внутри окна ------------------------
        # found[col] — непустой блок, found[col]+coarse — пустой (прошлый уровень).
        windows: Dict[Coord, Tuple[int, int]] = {}
        surface: Dict[Coord, int] = {}
        for col, fy in found.items():
            if fy >= self.top:
                surface[col] = fy            # выше искать некуда
            else:
                windows[col] = (fy, min(self.top, fy + self.coarse))

        while windows:
            if self._stop.is_set():
                break
            todo = [c for c, (lo, hi) in windows.items() if hi - lo > 1]
            if not todo:
                break
            mids = {c: (windows[c][0] + windows[c][1]) // 2 for c in todo}
            air = self._probe_list([(c, mids[c]) for c in todo], "minecraft:air", res)
            for col, is_air in zip(todo, air):
                lo, hi = windows[col]
                windows[col] = (lo, mids[col]) if is_air else (mids[col], hi)
        for col, (lo, _hi) in windows.items():
            surface[col] = lo

        # Сразу публикуем высоты — карта начинает отображаться до классификации.
        self._write_tiles(surface, {c: "other" for c in surface})
        res.tiles = len(surface)

        if self._stop.is_set():
            return self._finish(res, started, cancelled=True)

        # --- фаза C: классификация с ранним выходом ------------------------
        kinds: Dict[Coord, str] = {c: "other" for c in surface}
        unresolved = set(surface)
        for block, kind in self.kinds:
            if not unresolved or self._stop.is_set():
                break
            cols = list(unresolved)
            hits = self._probe_list([(c, surface[c]) for c in cols], block, res)
            changed: List[Coord] = []
            for col, hit in zip(cols, hits):
                if hit:
                    kinds[col] = kind
                    unresolved.discard(col)
                    changed.append(col)
            if changed:
                self._write_tiles({c: surface[c] for c in changed}, kinds)
            self._progress(len(found), res.columns)

        return self._finish(res, started, cancelled=self._stop.is_set())

    # --------------------------------------------------------------- зонды
    def _probe_list(self, pairs: Sequence[Tuple[Coord, int]], block: str,
                    res: ScanResult) -> List[bool]:
        """Пакетная проверка `execute if block` для списка (колонка, y).

        Результат всегда той же длины и в том же порядке, что и `pairs`.
        """
        results: List[bool] = []
        for i in range(0, len(pairs), self.batch):
            if self._stop.is_set():
                results.extend([False] * (len(pairs) - i))
                break
            chunk = pairs[i:i + self.batch]
            cmds = [mc.if_block(x, y, z, block) for (x, z), y in chunk]
            res.roundtrips += 1
            res.commands += len(cmds)
            try:
                resp = self.pool.run_many(cmds)
            except RCONError as exc:
                self.error = str(exc)
                log.warning("Сканер: %s", exc)
                if self._on_error:
                    self._on_error(str(exc))
                resp = [""] * len(cmds)
            if len(resp) != len(cmds):
                resp = list(resp) + [""] * (len(cmds) - len(resp))
            for r in resp:
                try:
                    results.append(mc._test_passed(r))
                except ValueError:
                    results.append(False)
        return results

    # ------------------------------------------------------------- служебное
    def _write_tiles(self, surface: Dict[Coord, int], kinds: Dict[Coord, str]) -> None:
        self.world.terrain.set_tiles(
            [(x, z, surface[(x, z)], kinds.get((x, z), "other"))
             for (x, z) in surface])

    def _progress(self, done: int, total: int) -> None:
        total = max(1, total)
        done = max(0, min(total, done))
        self.world.set_scan_progress(done, total)
        # Публикуем только при реальном изменении, кратном шагу, иначе UI
        # получает десятки одинаковых событий (и мигающий прогресс-бар).
        first = self._last_reported < 0
        if not first and done == self._last_reported:
            return
        if first or done >= total or (done - self._last_reported) >= self.progress_every:
            self._last_reported = done
            self.bus.publish(TOPIC_SCAN_PROGRESS, done, total)

    def _finish(self, res: ScanResult, started: float,
                cancelled: bool = False) -> ScanResult:
        res.seconds = time.monotonic() - started
        res.cancelled = cancelled
        res.tiles = len(self.world.terrain)
        self.world.set_scan_progress(res.columns, res.columns)
        self.world.end_scan()
        self.last = res
        log.info("Скан: %s%s", res.describe(), " [отменён]" if cancelled else "")
        return res
