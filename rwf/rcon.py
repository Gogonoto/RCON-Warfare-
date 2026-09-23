"""
Source RCON (протокол Minecraft) — соединение, пул, очередь команд.

Что исправлено относительно наброска
------------------------------------
1. **Многопакетные ответы.** Minecraft отдаёт ответ кусками, если он больше
   ~4 Кб. Набросок читал ровно один пакет и терял остаток, после чего поток
   рассинхронизировался навсегда (дальше все команды возвращали мусор).
   Здесь после каждой команды отправляется пустой пакет-маркер, и чтение идёт
   до него — так собирается полный ответ любой длины.

2. **Пул раздаёт соединения эксклюзивно.** `RCONPool.get()` в наброске был
   round-robin: при 6 соединениях и 8 потоках два потока получали один сокет
   и смешивали пакеты. Теперь соединение арендуется (`lease`) и возвращается
   обратно; одновременно его держит ровно один поток.

3. **Пакетная отправка (pipelining).** Перерисовка модели — это 15-20 команд.
   Отправлять их по одной и ждать ответ — значит упереться в RTT.
   `run_many()` шлёт все пакеты сразу и потом читает все ответы.

4. **Переподключение.** Любой обрыв сокета автоматически восстанавливает
   соединение и повторяет команду (если она ещё не была доставлена).

5. **Очередь с приоритетами и «слиянием».** Визуальные команды (перерисовка
   модели) помечаются ключом: если предыдущая ещё не отправлена, она
   выбрасывается — рисовать промежуточные кадры бессмысленно. Это снимает
   главную нагрузку с сервера.

Все публичные методы потокобезопасны.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import queue
import socket
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Протокол
# ---------------------------------------------------------------------------
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

MAX_BODY = 4096          # Minecraft не принимает тело длиннее (и сам режет ответы)
_HEADER = struct.Struct("<iii")   # size | id | type


class RCONError(Exception):
    """Базовая ошибка RCON."""


class RCONAuthError(RCONError):
    """Неверный пароль / сервер отверг аутентификацию."""


class RCONTimeout(RCONError):
    """Сервер не ответил вовремя. Соединение считается испорченным."""


class RCONDisconnected(RCONError):
    """Соединение закрыто, переподключиться не удалось."""


@dataclass
class RCONStats:
    """Счётчики для диагностики и HUD."""
    sent: int = 0
    received: int = 0
    bytes_in: int = 0
    errors: int = 0
    reconnects: int = 0
    dropped: int = 0            # выброшено визуальных команд из очереди
    merged: int = 0             # слито команд по ключу
    last_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0
    _lat_sum: float = 0.0
    _lat_n: int = 0

    def note_latency(self, ms: float) -> None:
        self.last_latency_ms = ms
        self._lat_sum += ms
        self._lat_n += 1
        self.avg_latency_ms = self._lat_sum / self._lat_n

    def as_dict(self) -> Dict[str, float]:
        return {
            "sent": self.sent, "received": self.received, "bytes_in": self.bytes_in,
            "errors": self.errors, "reconnects": self.reconnects,
            "dropped": self.dropped, "merged": self.merged,
            "latency_ms": round(self.last_latency_ms, 1),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
        }


# ---------------------------------------------------------------------------
#  Соединение
# ---------------------------------------------------------------------------
class RCONConnection:
    """Одно TCP-соединение по протоколу Source RCON.

    Не потокобезопасно само по себе: одновременный доступ сериализует
    `_cmd_lock`, но штатный способ — арендовать соединение через
    `RCONPool.lease()`.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        timeout: float = 8.0,
        encoding: str = "utf-8",
        auto_reconnect: bool = True,
        max_retries: int = 2,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.encoding = encoding
        self.auto_reconnect = auto_reconnect
        self.max_retries = max_retries

        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._cmd_lock = threading.Lock()
        self._ids = itertools.count(1)
        self.stats = RCONStats()
        self.broken = False
        self.connected_at: Optional[float] = None

    # ------------------------------------------------------------ жизненный цикл
    def connect(self) -> None:
        """Установить соединение и авторизоваться. Бросает RCONAuthError/RCONError."""
        self.close(quiet=True)
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._buf.clear()
        self.broken = False

        rid = self._next_id()
        self._send_packet(rid, SERVERDATA_AUTH, self.password)
        pid, ptype, body = self._recv_packet()
        # Часть серверов сначала присылает пустой RESPONSE_VALUE, потом AUTH_RESPONSE.
        if ptype != SERVERDATA_AUTH_RESPONSE:
            pid, ptype, body = self._recv_packet()
        if pid == -1 or ptype != SERVERDATA_AUTH_RESPONSE:
            self.close(quiet=True)
            raise RCONAuthError(
                f"Сервер {self.host}:{self.port} отверг пароль RCON."
            )
        self.connected_at = time.time()
        log.debug("RCON подключён к %s:%s", self.host, self.port)

    def close(self, quiet: bool = False) -> None:
        self.broken = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                if not quiet:
                    raise

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self.broken

    def ping(self) -> bool:
        try:
            self.run("list")
            return True
        except RCONError:
            return False

    # ------------------------------------------------------------ пакеты
    def _next_id(self) -> int:
        return next(self._ids)

    def _send_packet(self, pid: int, ptype: int, body: str) -> None:
        sock = self._sock
        if sock is None:
            raise RCONDisconnected("Соединение не установлено.")
        data = body.encode(self.encoding, errors="replace")
        if len(data) > MAX_BODY - 12:
            raise RCONError(
                f"Команда длиннее {MAX_BODY - 12} байт, Minecraft её отвергнет: "
                f"{body[:60]}..."
            )
        payload = _HEADER.pack(len(data) + 10, pid, ptype) + data + b"\x00\x00"
        sock.sendall(payload)
        self.stats.sent += 1

    def _recv_bytes(self, n: int) -> bytes:
        sock = self._sock
        if sock is None:
            raise RCONDisconnected("Соединение не установлено.")
        buf = self._buf
        while len(buf) < n:
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                self.broken = True
                raise RCONTimeout(f"Таймаут ответа от {self.host}:{self.port}") from exc
            except OSError as exc:
                self.broken = True
                raise RCONDisconnected(f"Ошибка сокета: {exc}") from exc
            if not chunk:
                self.broken = True
                raise RCONDisconnected("Сервер закрыл соединение.")
            buf.extend(chunk)
        out = bytes(buf[:n])
        del buf[:n]
        return out

    def _recv_packet(self) -> Tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._recv_bytes(4))
        if length < 10 or length > 4096 + 64:
            self.broken = True
            raise RCONError(f"Некорректная длина пакета RCON: {length}")
        data = self._recv_bytes(length)
        pid, ptype = struct.unpack("<ii", data[:8])
        body = data[8:length - 2]
        self.stats.received += 1
        self.stats.bytes_in += length
        return pid, ptype, body.decode(self.encoding, errors="replace")

    # ------------------------------------------------------------ команды
    def run(self, command: str) -> str:
        """Выполнить одну команду и вернуть полный (склеенный) ответ."""
        results = self._execute([command], retries=self.max_retries)
        return results[0] if results else ""

    def run_many(self, commands: Sequence[str]) -> List[str]:
        """Выполнить пачку команд одним заходом (pipelining)."""
        if not commands:
            return []
        return self._execute(list(commands), retries=self.max_retries)

    def _execute(self, commands: List[str], retries: int) -> List[str]:
        attempt = 0
        while True:
            try:
                if self._sock is None or self.broken:
                    self._ensure_connected()
                return self._exchange(commands)
            except RCONAuthError:
                raise
            except (RCONError, OSError) as exc:
                self.stats.errors += 1
                self.broken = True
                if getattr(exc, "_delivered", False):
                    # Команда уже ушла на сервер — повтор даст двойной
                    # взрыв/спавн. Не повторяем, сообщаем пустым ответом.
                    log.warning("RCON: ответ потерян после отправки (%s)", exc)
                    return [""] * len(commands)
                attempt += 1
                if not self.auto_reconnect or attempt > max(0, retries):
                    raise RCONDisconnected(str(exc)) from exc
                log.info("RCON: переподключение (%s), попытка %d", exc, attempt)
                time.sleep(min(1.5, 0.15 * attempt))

    def _ensure_connected(self) -> None:
        reconnect = self.connected_at is not None
        self.connect()
        if reconnect:
            self.stats.reconnects += 1

    def _exchange(self, commands: List[str]) -> List[str]:
        """Отправить команды + маркер конца, собрать ответы по id."""
        started = time.monotonic()
        ids: List[int] = []
        with self._cmd_lock:
            # --- фаза отправки -------------------------------------------
            try:
                for cmd in commands:
                    pid = self._next_id()
                    ids.append(pid)
                    self._send_packet(pid, SERVERDATA_EXECCOMMAND, cmd)
                end_id = self._next_id()
                self._send_packet(end_id, SERVERDATA_EXECCOMMAND, "")
            except (OSError, RCONError) as exc:
                exc._delivered = True          # type: ignore[attr-defined]
                raise
            # --- фаза приёма ---------------------------------------------
            bodies: Dict[int, List[str]] = {pid: [] for pid in ids}
            try:
                guard = 0
                limit = len(ids) * 8 + 32
                while True:
                    pid, ptype, body = self._recv_packet()
                    guard += 1
                    if pid == -1 and ptype == SERVERDATA_AUTH_RESPONSE:
                        raise RCONAuthError(
                            "Сессия RCON истекла (сервер ответил id=-1).")
                    if pid == end_id:
                        break
                    if pid in bodies:
                        bodies[pid].append(body)
                    if guard > limit:       # защита от рассинхрона потока
                        self.broken = True
                        raise RCONError(
                            "Поток RCON рассинхронизирован (лишние пакеты).")
            except (OSError, RCONError) as exc:
                exc._delivered = True          # type: ignore[attr-defined]
                raise
        self.stats.note_latency((time.monotonic() - started) * 1000.0)
        return ["".join(bodies[pid]) for pid in ids]

    # ------------------------------------------------------------ отладка
    def __repr__(self) -> str:  # pragma: no cover
        state = "connected" if self.connected else "closed"
        return f"<RCONConnection {self.host}:{self.port} {state}>"


# ---------------------------------------------------------------------------
#  Пул соединений
# ---------------------------------------------------------------------------
class RCONPool:
    """Пул RCON-соединений с эксклюзивной арендой.

    Соединения создаются лениво: пул можно собрать до того, как сервер
    поднялся, и он не заблокирует старт приложения.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        size: int = 6,
        timeout: float = 8.0,
        on_event=None,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.size = max(1, size)
        self.timeout = timeout
        self._on_event = on_event
        self._free: "queue.LifoQueue[RCONConnection]" = queue.LifoQueue()
        self._all: List[RCONConnection] = []
        self._lock = threading.Lock()
        self._closed = False
        self._created = 0
        self.leases = 0

    # --------------------------------------------------------------- аренда
    def acquire(self, timeout: float = 15.0) -> RCONConnection:
        """Взять свободное соединение (создав новое, если пул не заполнен)."""
        if self._closed:
            raise RCONDisconnected("Пул RCON закрыт.")
        try:
            conn = self._free.get_nowait()
        except queue.Empty:
            with self._lock:
                can_create = self._created < self.size
                if can_create:
                    self._created += 1
            if can_create:
                conn = RCONConnection(
                    self.host, self.port, self.password, timeout=self.timeout
                )
                with self._lock:
                    self._all.append(conn)
            else:
                try:
                    conn = self._free.get(timeout=timeout)
                except queue.Empty as exc:
                    raise RCONDisconnected(
                        f"Все {self.size} соединений RCON заняты (timeout={timeout}s)"
                    ) from exc
        if not conn.connected:
            conn.connect()
        self.leases += 1
        return conn

    def release(self, conn: RCONConnection) -> None:
        """Вернуть соединение в пул. Испорченное — закрыть и забыть."""
        if conn.broken or not conn.connected:
            try:
                conn.close(quiet=True)
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                if conn in self._all:
                    self._all.remove(conn)
                self._created = max(0, self._created - 1)
            return
        self._free.put(conn)

    @contextmanager
    def lease(self, timeout: float = 15.0) -> Iterator[RCONConnection]:
        conn = self.acquire(timeout=timeout)
        try:
            yield conn
        finally:
            self.release(conn)

    # --------------------------------------------------------------- команды
    def run(self, command: str) -> str:
        """Синхронно выполнить команду на свободном соединении."""
        try:
            with self.lease() as conn:
                return conn.run(command)
        except RCONError as exc:
            if self._on_event:
                self._on_event(command, str(exc))
            raise

    def run_many(self, commands: Sequence[str]) -> List[str]:
        if not commands:
            return []
        with self.lease() as conn:
            return conn.run_many(commands)

    # ---------------------------------------------------------------- прочее
    def connect_all(self) -> None:
        """Прогреть пул: открыть все соединения сразу (для проверки доступности)."""
        conns = [self.acquire() for _ in range(self.size)]
        for c in conns:
            self.release(c)

    def stats(self) -> Dict[str, float]:
        with self._lock:
            conns = list(self._all)
        out: Dict[str, float] = {"created": len(conns), "free": self._free.qsize(),
                                 "leases": self.leases}
        agg = {"sent": 0, "received": 0, "errors": 0, "reconnects": 0}
        for c in conns:
            agg["sent"] += c.stats.sent
            agg["received"] += c.stats.received
            agg["errors"] += c.stats.errors
            agg["reconnects"] += c.stats.reconnects
        out.update(agg)
        return out

    def close(self) -> None:
        self._closed = True
        with self._lock:
            conns, self._all = list(self._all), []
            self._created = 0
        for c in conns:
            try:
                c.close(quiet=True)
            except Exception:  # noqa: BLE001
                pass
        # Разблокировать тех, кто ждёт в acquire()
        while True:
            try:
                self._free.get_nowait()
            except queue.Empty:
                break

    def __enter__(self) -> "RCONPool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
#  Очередь команд с приоритетами
# ---------------------------------------------------------------------------
class Priority:
    """Чем меньше число, тем важнее команда."""
    CRITICAL = 0    # взрывы, спавн, удаление — не выбрасываются никогда
    NORMAL = 10     # выстрелы, tp, служебные
    VISUAL = 20     # перерисовка модели — можно сливать и выбрасывать


@dataclass(order=True)
class _Item:
    order: Tuple[int, int]          # (priority, seq) — для heapq
    command: str = field(compare=False)
    key: Optional[str] = field(default=None, compare=False)
    future: Optional[queue.SimpleQueue] = field(default=None, compare=False)
    cancelled: bool = field(default=False, compare=False)


class CommandQueue:
    """Фоновая очередь команд на сервер.

    Зачем: физический тик юнита не должен блокироваться на сетевом RTT, а
    сервер не должен получать 60 `setblock` в секунду на каждый юнит.

    * `submit(cmd, Priority.VISUAL, key='model:3')` — если предыдущая команда
      с тем же ключом ещё не отправлена, она помечается отменённой: рисовать
      промежуточный кадр всё равно никто не увидит.
    * Пачки команд уходят через `run_many` (pipelining).
    * `rate` ограничивает поток команд в секунду, чтобы не положить сервер.
    """

    def __init__(
        self,
        pool: RCONPool,
        workers: int = 2,
        rate: float = 250.0,
        batch_size: int = 24,
        maxsize: int = 5000,
        on_event=None,
    ):
        self.pool = pool
        self.batch_size = max(1, batch_size)
        self.rate = float(rate) if rate and rate > 0 else 0.0
        self.maxsize = maxsize
        self._on_event = on_event

        self._heap: List[_Item] = []
        self._by_key: Dict[str, _Item] = {}
        self._cond = threading.Condition()
        self._seq = itertools.count()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._workers = max(1, workers)
        self._pending_results: Dict[int, queue.SimpleQueue] = {}

        self._inflight = 0
        self.submitted = 0
        self.executed = 0
        self.dropped = 0
        self.merged = 0
        self.errors = 0
        # Общий ограничитель скорости: один на всех воркеров.
        # Без этого N воркеров давали N × rate и клали сервер.
        self._rate_lock = threading.Lock()
        self._next_allowed = 0.0

    # ---------------------------------------------------------------- запуск
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for i in range(self._workers):
            t = threading.Thread(target=self._worker, name=f"cmdq-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 3.0, flush: bool = True) -> None:
        """Остановить очередь. flush=False — сбросить неотправленные команды."""
        self._stop.set()
        with self._cond:
            if not flush:
                self.dropped += len(self._heap)
                self._heap.clear()
                self._by_key.clear()
            self._cond.notify_all()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()

    @property
    def running(self) -> bool:
        return bool(self._threads) and not self._stop.is_set()

    # --------------------------------------------------------------- подача
    def submit(self, command: str, priority: int = Priority.NORMAL,
               key: Optional[str] = None) -> None:
        """Поставить команду в очередь (неблокирующе)."""
        if not command:
            return
        with self._cond:
            if key is not None:
                prev = self._by_key.get(key)
                if prev is not None and not prev.cancelled:
                    # Слияние: старая команда с тем же ключом уже не нужна.
                    prev.cancelled = True
                    self.merged += 1
            if len(self._heap) >= self.maxsize:
                self._drop_lowest_locked()
            item = _Item(order=(priority, next(self._seq)), command=command, key=key)
            if key is not None:
                self._by_key[key] = item
            heapq.heappush(self._heap, item)
            self.submitted += 1
            self._cond.notify()

    def submit_many(self, commands: Iterable[str], priority: int = Priority.NORMAL,
                    key_prefix: Optional[str] = None) -> None:
        for i, cmd in enumerate(commands):
            self.submit(cmd, priority,
                        key=f"{key_prefix}:{i}" if key_prefix else None)

    def run(self, command: str, timeout: float = 10.0) -> str:
        """Поставить команду в очередь и дождаться ответа (приоритет CRITICAL)."""
        box: "queue.SimpleQueue" = queue.SimpleQueue()
        with self._cond:
            item = _Item(order=(Priority.CRITICAL, next(self._seq)),
                         command=command, future=box)
            heapq.heappush(self._heap, item)
            self.submitted += 1
            self._cond.notify()
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            return ""

    def _drop_lowest_locked(self) -> None:
        """Выбросить самую неважную команду (только VISUAL)."""
        for i in range(len(self._heap) - 1, -1, -1):
            it = self._heap[i]
            if it.order[0] >= Priority.VISUAL and not it.cancelled:
                it.cancelled = True
                self.dropped += 1
                if it.key is not None and self._by_key.get(it.key) is it:
                    del self._by_key[it.key]
                return
        # VISUAL не осталось — очередь переполнена важными командами
        self.dropped += 1
        heapq.heappop(self._heap)

    # --------------------------------------------------------------- воркер
    def _next_batch_locked(self) -> List[_Item]:
        while self._heap:
            it = heapq.heappop(self._heap)
            if it.cancelled:
                if it.key is not None and self._by_key.get(it.key) is it:
                    del self._by_key[it.key]
                continue
            return [it]
        return []

    def _throttle(self, n_commands: int) -> None:
        """Выдержать общий бюджет команд/с. Долг ограничен секундой, чтобы
        внезапный всплеск не останавливал очередь надолго."""
        if self.rate <= 0 or n_commands <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            self._next_allowed = min(max(self._next_allowed, now), now + 1.0)
            wait = self._next_allowed - now
            self._next_allowed += n_commands / self.rate
        if wait > 0:
            time.sleep(wait)

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while not self._heap and not self._stop.is_set():
                    self._cond.wait(0.2)
                if self._stop.is_set() and not self._heap:
                    return
                batch = self._next_batch_locked()
                # Добиваем пачку командами того же приоритета,
                # не трогая более важные.
                while len(batch) < self.batch_size and self._heap:
                    nxt = self._heap[0]
                    if nxt.cancelled:
                        heapq.heappop(self._heap)
                        continue
                    if nxt.order[0] != batch[0].order[0]:
                        break
                    heapq.heappop(self._heap)
                    batch.append(nxt)
            if not batch:
                continue
            self._inflight += len(batch)

            commands = [it.command for it in batch]
            self._throttle(len(commands))
            try:
                results = self.pool.run_many(commands)
            except RCONError as exc:
                self.errors += 1
                log.warning("Очередь команд: %s", exc)
                if self._on_event:
                    self._on_event(commands[0] if commands else "", str(exc))
                results = [""] * len(commands)
            self.executed += len(batch)
            self._inflight -= len(batch)
            for it, res in zip(batch, results):
                if it.future is not None:
                    it.future.put(res)
                elif res and res.startswith("Unknown") and self._on_event:
                    self._on_event(it.command, res)

    # --------------------------------------------------------------- статус
    def pending(self) -> int:
        with self._cond:
            return sum(1 for it in self._heap if not it.cancelled)

    def flush(self, timeout: float = 5.0) -> bool:
        """Дождаться, пока очередь пуста И все взятые воркером пачки отправлены."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pending() == 0 and self._inflight <= 0:
                return True
            time.sleep(0.005)
        return False

    def stats(self) -> Dict[str, float]:
        return {
            "submitted": self.submitted, "executed": self.executed,
            "pending": self.pending(), "dropped": self.dropped,
            "merged": self.merged, "errors": self.errors,
        }
