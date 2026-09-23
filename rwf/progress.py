"""
Прогрессия оператора: очки, звания, достижения (GAME-01…).

ЧАСТЬ 3 заказчика: «геймификация (очки/достижения/прогрессия)». Диспетчерская
перестаёт быть безликим пультом: за полезную работу оператор получает очки,
очки складываются в звание, а里程碑-события открывают достижения. Всё
считается по СОБЫТИЯМ шины, поэтому модуль не лезет в физику и не влияет на
тик (инвариант I5): ему скармливают `event(kind, ...)`, он возвращает список
только что открытых достижений — а UI уже решает, показывать тост или нет.

Очки начисляются за результат, а не за активность: сбитая машина, посаженная
машина, доставленный груз, снесённая база, завершённый маршрут. Потеря своей
техники очки снимает — иначе «прогрессия» поощряла бы спам спавном.

Состояние живёт в JSON рядом с настройками (`progress.json`): звание и
достижения переживают перезапуск, счётчик сессии виден отдельно.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Очки за события
# ---------------------------------------------------------------------------
SCORE: Dict[str, int] = {
    "kill": 120,              # сбита вражеская машина
    "kill_ground": 90,        # наземная цель — дешевле воздушной
    "base_destroyed": 600,    # снесена вражеская база
    "landing": 25,            # своя машина села (любая база)
    "delivery": 40,           # груз дошёл до базы (логистика)
    "route_done": 30,         # маршрут оператора отработан до конца
    "strike": 60,             # попадание в зону удара
    "launch": 5,              # запуск машины (немного, чтобы не фармить)
    "loss": -80,              # потеря своей машины
    "crash": -60,             # своя машина разбилась сама
}

#: звания: порог суммарных очков -> название
RANKS: Tuple[Tuple[int, str], ...] = (
    (0, "Рядовой"),
    (400, "Ефрейтор"),
    (1000, "Мл. сержант"),
    (2000, "Сержант"),
    (3500, "Старшина"),
    (5500, "Лейтенант"),
    (8000, "Капитан"),
    (12000, "Майор"),
    (18000, "Полковник"),
    (26000, "Генерал"),
)


@dataclass(frozen=True)
class Achievement:
    key: str
    name: str
    hint: str
    #: проверка по счётчикам прогресса
    check: Callable[[Dict[str, int]], bool]


ACHIEVEMENTS: Tuple[Achievement, ...] = (
    Achievement("first_blood", "Первая кровь", "сбить первую машину",
                lambda c: c.get("kills", 0) >= 1),
    Achievement("ace", "Ас", "сбить 5 машин",
                lambda c: c.get("kills", 0) >= 5),
    Achievement("squadron", "Эскадрилья", "запустить 10 машин",
                lambda c: c.get("launches", 0) >= 10),
    Achievement("airbridge", "Воздушный мост", "3 доставки снабжения",
                lambda c: c.get("deliveries", 0) >= 3),
    Achievement("carrier_friend", "Друг авианосца",
                "привезти на базу 10 т снабжения",
                lambda c: c.get("delivered_tons", 0) >= 10),
    Achievement("fortress", "Крепостной", "уничтожить базу противника",
                lambda c: c.get("bases_destroyed", 0) >= 1),
    Achievement("steady_hands", "Твёрдая рука", "10 посадок без потерь",
                lambda c: c.get("landings", 0) >= 10),
    Achievement("route_master", "Штурман", "5 выполненных маршрутов",
                lambda c: c.get("routes_done", 0) >= 5),
    Achievement("marksman", "Меткий", "3 попадания в зону удара",
                lambda c: c.get("strikes", 0) >= 3),
    Achievement("veteran", "Ветеран", "час налёта суммарно",
                lambda c: c.get("flight_minutes", 0) >= 60),
)

#: маппинг события -> счётчики, которые оно приращивает
_COUNTER_BUMP: Dict[str, Tuple[Tuple[str, int], ...]] = {
    "kill": (("kills", 1),),
    "kill_ground": (("kills", 1),),
    "base_destroyed": (("bases_destroyed", 1),),
    "landing": (("landings", 1),),
    "delivery": (("deliveries", 1),),
    "route_done": (("routes_done", 1),),
    "strike": (("strikes", 1),),
    "launch": (("launches", 1),),
}


@dataclass
class ProgressState:
    score: int = 0                      # суммарные очки (за всё время)
    session: int = 0                    # очки текущей сессии
    counters: Dict[str, int] = field(default_factory=dict)
    unlocked: List[str] = field(default_factory=list)
    updated: float = field(default_factory=time.time)

    def rank(self) -> Tuple[str, int, int, float]:
        """(звание, порог звания, порог следующего, доля до следующего)."""
        name, lo, hi = RANKS[0][1], 0, RANKS[-1][0] * 2
        for i, (threshold, title) in enumerate(RANKS):
            if self.score >= threshold:
                name, lo = title, threshold
                hi = RANKS[i + 1][0] if i + 1 < len(RANKS) else threshold + 1
        span = max(1, hi - lo)
        frac = max(0.0, min(1.0, (self.score - lo) / span))
        return name, lo, hi, frac

    def to_dict(self) -> Dict[str, Any]:
        return {"score": self.score, "session": self.session,
                "counters": dict(self.counters),
                "unlocked": list(self.unlocked), "updated": self.updated}


class Progress:
    """Накопление очков и достижений. Потокобезопасен (события из рабочих
    потоков шины)."""

    def __init__(self, path: Optional[Path] = None,
                 persist: bool = True):
        self.path = Path(path) if path else None
        self.persist = persist and self.path is not None
        self.state = ProgressState()
        self._lock = threading.Lock()
        self._listeners: List[Callable[[str, int, List[str]], None]] = []
        if self.persist:
            self.load()

    # ------------------------------------------------------------ служба
    def on_event(self, cb: Callable[[str, int, List[str]], None]) -> None:
        """Подписка: cb(kind, delta_score, newly_unlocked)."""
        self._listeners.append(cb)

    def load(self) -> bool:
        if not self.path or not self.path.is_file():
            return False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.exception("progress.json не читается — начинаем с нуля")
            return False
        with self._lock:
            self.state.score = int(data.get("score", 0))
            self.state.counters = {str(k): int(v) for k, v in
                                   (data.get("counters") or {}).items()}
            known = {a.key for a in ACHIEVEMENTS}
            self.state.unlocked = [k for k in (data.get("unlocked") or [])
                                   if k in known]
        return True

    def save(self) -> bool:
        if not self.persist:
            return False
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state.to_dict(), ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError:
            log.exception("Не удалось сохранить прогресс")
            return False

    # ------------------------------------------------------------ события
    def event(self, kind: str, **kw: Any) -> Tuple[int, List[str]]:
        """Учесть событие. Возвращает (дельта очков, новые достижения)."""
        delta = SCORE.get(kind, 0)
        extra: List[Tuple[str, int]] = []
        if kind == "delivery":
            tons = float(kw.get("tons", 0.0))
            extra.append(("delivered_tons", int(round(tons))))
        if kind == "flight":
            extra.append(("flight_minutes",
                          int(round(float(kw.get("minutes", 0.0))))))
        with self._lock:
            self.state.score += delta
            self.state.session += delta
            self.state.updated = time.time()
            for key, n in list(_COUNTER_BUMP.get(kind, ())) + extra:
                self.state.counters[key] = \
                    self.state.counters.get(key, 0) + n
            newly = [a.key for a in ACHIEVEMENTS
                     if a.key not in self.state.unlocked
                     and a.check(self.state.counters)]
            self.state.unlocked.extend(newly)
        if newly or delta:
            for cb in list(self._listeners):
                try:
                    cb(kind, delta, newly)
                except Exception:  # noqa: BLE001
                    log.exception("Слушатель прогресса упал")
        if newly and self.persist:
            self.save()
        return delta, newly

    # ------------------------------------------------------------ снимок
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            rank, lo, hi, frac = self.state.rank()
            return {"score": self.state.score, "session": self.state.session,
                    "rank": rank, "rank_lo": lo, "rank_hi": hi,
                    "rank_frac": round(frac, 3),
                    "counters": dict(self.state.counters),
                    "unlocked": list(self.state.unlocked),
                    "achievements": [
                        {"key": a.key, "name": a.name, "hint": a.hint,
                         "done": a.key in self.state.unlocked}
                        for a in ACHIEVEMENTS]}
