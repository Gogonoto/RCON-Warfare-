"""Тесты RCON-слоя: протокол, пул, очередь команд. Запускаются на мок-сервере."""
from __future__ import annotations

import socket
import threading
import time
import unittest

from rwf.mock_server import MockMCServer
from rwf.rcon import (
    CommandQueue, Priority, RCONAuthError, RCONConnection, RCONDisconnected,
    RCONPool,
)

HOST = "127.0.0.1"


class _ServerCase(unittest.TestCase):
    """Общая обвязка: поднять мок-сервер на свободном порту, убрать после."""

    server_kwargs: dict = {}

    def setUp(self) -> None:
        self.server = MockMCServer(host=HOST, port=0, password="2203",
                                   **self.server_kwargs).start()
        self.port = self.server.port
        self._clean: list = []

    def tearDown(self) -> None:
        for obj in self._clean:
            try:
                obj.stop() if hasattr(obj, "stop") else obj.close()
            except Exception:  # noqa: BLE001
                pass
        self.server.stop()

    def make_conn(self, password: str = "2203", **kw) -> RCONConnection:
        c = RCONConnection(HOST, self.port, password, timeout=4.0, **kw)
        self._clean.append(c)
        return c

    def make_pool(self, size: int = 4, password: str = "2203") -> RCONPool:
        p = RCONPool(HOST, self.port, password, size=size, timeout=4.0)
        self._clean.append(p)
        return p


class TestProtocol(_ServerCase):
    def test_auth_ok(self):
        c = self.make_conn()
        c.connect()
        self.assertTrue(c.connected)
        self.assertIn("Arlik88", c.run("list"))

    def test_auth_bad_password(self):
        with self.assertRaises(RCONAuthError):
            self.make_conn(password="wrong").connect()

    def test_parse_positions(self):
        c = self.make_conn()
        c.connect()
        resp = c.run("data get entity Arlik88 Pos")
        self.assertIn("[", resp)
        nums = resp.split("[")[1].split("]")[0].replace("d", "").split(",")
        self.assertEqual(len(nums), 3)
        for n in nums:
            float(n)

    def test_long_response_is_reassembled(self):
        """Ответ >4 Кб приходит несколькими пакетами — клиент обязан их склеить."""
        c = self.make_conn()
        c.connect()
        resp = c.run("help")
        self.assertGreater(len(resp), 4096)
        self.assertIn("/command_139", resp)      # самая последняя строка
        self.assertIn("/command_000", resp)      # и самая первая

    def test_unknown_command_text(self):
        c = self.make_conn()
        c.connect()
        resp = c.run("totally-not-a-command")
        self.assertIn("Unknown or incomplete command", resp)

    def test_reconnect_after_broken_socket(self):
        c = self.make_conn()
        c.connect()
        self.assertEqual(c.run("say before"), "[Server] before")
        c.close()                                # имитируем обрыв
        self.assertFalse(c.connected)
        self.assertEqual(c.run("say after"), "[Server] after")
        self.assertGreaterEqual(c.stats.reconnects, 1)

    def test_pipelining_single_roundtrip(self):
        c = self.make_conn()
        c.connect()
        base = c.stats.sent                       # минус пакет авторизации
        cmds = [f"say p{i}" for i in range(20)]
        out = c.run_many(cmds)
        self.assertEqual(len(out), 20)
        self.assertEqual(out[7], "[Server] p7")
        self.assertEqual(out[19], "[Server] p19")
        # 20 команд + 1 маркер конца — одним заходом, без 20 отдельных RTT
        self.assertEqual(c.stats.sent - base, 21)

    def test_server_down_raises_disconnected(self):
        dead = socket.socket()
        dead.bind((HOST, 0))
        port = dead.getsockname()[1]
        dead.close()                             # порт гарантированно свободен
        c = RCONConnection(HOST, port, "x", timeout=0.5, max_retries=1)
        with self.assertRaises(RCONDisconnected):
            c.run("list")
        c.close()


class TestPool(_ServerCase):
    def test_concurrent_access_does_not_desync(self):
        """Главная проверка: 8 потоков на пуле из 3 соединений, ответы не путаются."""
        pool = self.make_pool(size=3)
        errors: list = []
        barrier = threading.Barrier(8)

        def worker(idx: int):
            try:
                barrier.wait(timeout=5)
                for j in range(25):
                    msg = f"t{idx}-{j}"
                    resp = pool.run(f"say {msg}")
                    if resp != f"[Server] {msg}":
                        errors.append((msg, resp))
            except Exception as exc:  # noqa: BLE001
                errors.append((idx, repr(exc)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [], f"Рассинхронизация/ошибки: {errors[:5]}")
        self.assertLessEqual(pool.stats()["created"], 3)

    def test_pool_exhaustion_reports_clearly(self):
        pool = self.make_pool(size=2)
        a = pool.acquire()
        b = pool.acquire()
        with self.assertRaises(RCONDisconnected):
            pool.acquire(timeout=0.3)
        pool.release(a)
        pool.release(b)

    def test_broken_connection_is_discarded(self):
        pool = self.make_pool(size=2)
        c = pool.acquire()
        c.broken = True
        pool.release(c)                          # должен закрыть, а не вернуть
        self.assertEqual(pool.stats()["free"], 0)
        c2 = pool.acquire()
        self.assertTrue(c2.connected)
        pool.release(c2)


class TestCommandQueue(_ServerCase):
    def test_merging_stale_visual_commands(self):
        """Неотправленные кадры модели с тем же ключом сливаются в один."""
        pool = self.make_pool(size=2)
        q = CommandQueue(pool, workers=1)        # НЕ стартуем — детерминированно
        for i in range(50):
            q.submit(f"tp @e[tag=u1] {i} 100 {i}", Priority.VISUAL, key="model:1")
        self.assertEqual(q.pending(), 1)
        self.assertEqual(q.merged, 49)
        q.start()
        self.assertTrue(q.flush(timeout=5))
        self.assertEqual(q.executed, 1)
        q.stop(flush=False)
        # На сервер ушёл только последний кадр
        self.assertEqual(self.server.count(r"^tp @e\[tag=u1\] 49 "), 1)

    def test_overflow_drops_only_visual(self):
        pool = self.make_pool(size=2)
        q = CommandQueue(pool, workers=1, maxsize=5)
        q.submit("say important", Priority.CRITICAL)
        for i in range(30):
            q.submit(f"say visual{i}", Priority.VISUAL, key=f"k{i}")
        self.assertLessEqual(q.pending(), 5)
        self.assertGreater(q.dropped, 0)
        q.start()
        self.assertTrue(q.flush(timeout=5))
        q.stop(flush=False)
        self.assertIn("say important", self.server.command_log)

    def test_all_commands_reach_server_in_order(self):
        pool = self.make_pool(size=2)
        q = CommandQueue(pool, workers=1, batch_size=8)
        q.start()
        for i in range(60):
            q.submit(f"say m{i}", Priority.NORMAL)
        self.assertTrue(q.flush(timeout=10))
        q.stop()
        got = [c for c in self.server.command_log if c.startswith("say m")]
        self.assertEqual(got, [f"say m{i}" for i in range(60)])

    def test_rate_limit_is_shared_across_workers(self):
        """Два воркера не должны давать двойную скорость: бюджет общий."""
        pool = self.make_pool(size=3)
        rate = 200.0
        q = CommandQueue(pool, workers=2, rate=rate, batch_size=16)
        q.start()
        n = 400
        started = time.monotonic()
        for i in range(n):
            q.submit(f"say r{i}", Priority.NORMAL)
        self.assertTrue(q.flush(timeout=30))
        elapsed = time.monotonic() - started
        q.stop()
        expected = n / rate
        self.assertGreaterEqual(elapsed, expected * 0.75,
                                f"rate-limit не сработал: {n} команд за "
                                f"{elapsed:.2f} с при лимите {rate}/с")
        # И не должен быть намного строже (иначе очередь просто простаивает)
        self.assertLess(elapsed, expected * 3.0 + 1.0,
                        f"очередь слишком медленная: {elapsed:.2f} с")

    def test_sync_run_through_queue(self):
        pool = self.make_pool(size=2)
        q = CommandQueue(pool, workers=1)
        q.start()
        self.assertEqual(q.run("say sync", timeout=5), "[Server] sync")
        q.stop()


class TestMockServerTerrain(_ServerCase):
    """Сканер рельефа будет опираться на `execute if block` — проверяем мок."""

    def test_air_probe_matches_heightmap(self):
        from rwf.mock_server import ground_height, top_y
        c = self.make_conn()
        c.connect()
        for x, z in ((0, 0), (120, -80), (500, 500), (-321, 77)):
            ty = top_y(x, z)          # над озером это уровень моря, не грунт
            above = c.run(f"execute if block {x} {ty + 1} {z} minecraft:air")
            below = c.run(f"execute if block {x} {ty} {z} minecraft:air")
            self.assertIn("Test passed", above, f"x={x} z={z} top={ty}")
            self.assertIn("Test failed", below, f"x={x} z={z} top={ty}")
            self.assertLessEqual(ground_height(x, z), ty)

    def test_water_over_lake(self):
        from rwf.mock_server import ground_height
        c = self.make_conn()
        c.connect()
        x, z = 120, -80
        self.assertLess(ground_height(x, z), 62)
        self.assertIn("Test passed",
                      c.run(f"execute if block {x} 60 {z} minecraft:water"))

    def test_version_capability_probe(self):
        """`ride` есть только в 1.20.5+ — так ядро определяет возможности."""
        c = self.make_conn()
        c.connect()
        self.assertIn("Unknown or incomplete command", c.run("ride @a mount @e"))

    def test_players_list_parsing(self):
        c = self.make_conn()
        c.connect()
        self.assertEqual(c.run("list"),
                         "There are 2 of a max of 20 players online: Arlik88, Steve")
        self.server.set_player_online("Steve", False)
        self.assertNotIn("Steve", c.run("list"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
