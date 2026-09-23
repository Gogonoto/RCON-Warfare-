"""Тесты трекера игроков и сканера рельефа (на мок-сервере)."""
from __future__ import annotations

import time
import unittest

from rwf import mc
from rwf.events import TOPIC_PLAYER_REMOVED, TOPIC_SCAN_PROGRESS
from rwf.mock_server import MockMCServer, ground_height, top_y
from rwf.rcon import RCONPool
from rwf.scanner import TerrainScanner
from rwf.tracker import PlayerTracker
from rwf.world import World

HOST = "127.0.0.1"


class _Base(unittest.TestCase):
    def setUp(self):
        self.server = MockMCServer(host=HOST, port=0, password="2203",
                                   players=("Arlik88", "Steve", "Notch")).start()
        self.pool = RCONPool(HOST, self.server.port, "2203", size=4, timeout=5.0)
        self.world = World()

    def tearDown(self):
        self.pool.close()
        self.server.stop()


class TestPlayerTracker(_Base):
    def test_poll_once_finds_all_players(self):
        tr = PlayerTracker(self.pool, self.world, interval=0.2)
        found = tr.poll_once()
        self.assertEqual(set(found), {"Arlik88", "Steve", "Notch"})
        self.assertEqual(set(self.world.get_players()), {"Arlik88", "Steve", "Notch"})
        for name, pos in found.items():
            self.assertEqual(len(pos), 3)

    def test_single_roundtrip_batch(self):
        """Регресс: было 1 + N запросов подряд, стало 2 пакетных захода."""
        tr = PlayerTracker(self.pool, self.world, fetch_rotation=True)
        self.server.command_log.clear()
        tr.poll_once()
        cmds = list(self.server.command_log)
        # 1 x list + 3 x Pos + 3 x Rotation
        self.assertEqual(len(cmds), 7)
        self.assertEqual(cmds[0], "list")
        self.assertEqual(sum(1 for c in cmds if c.endswith("Pos")), 3)
        self.assertEqual(sum(1 for c in cmds if c.endswith("Rotation")), 3)

    def test_rotation_and_position_parsed(self):
        tr = PlayerTracker(self.pool, self.world, fetch_rotation=True)
        tr.poll_once()
        rec = self.world.get_player("Arlik88")
        self.assertIsNotNone(rec)
        # Игрок мок-сервера стоит на поверхности (над озером — на уровне моря)
        self.assertAlmostEqual(rec.pos[1], top_y(rec.pos[0], rec.pos[2]) + 1,
                               delta=1.5)
        self.assertIsInstance(rec.yaw, float)
        self.assertTrue(-90.0 <= rec.pitch <= 90.0)

    def test_offline_players_removed(self):
        tr = PlayerTracker(self.pool, self.world)
        tr.poll_once()
        self.assertEqual(len(self.world.get_players()), 3)
        removed = []
        self.world.bus.subscribe(TOPIC_PLAYER_REMOVED, lambda n: removed.append(n))
        self.server.set_player_online("Steve", False)
        tr.poll_once()
        self.assertEqual(removed, ["Steve"])
        self.assertNotIn("Steve", self.world.get_players())

    def test_background_loop_updates_positions(self):
        tr = PlayerTracker(self.pool, self.world, interval=0.1)
        tr.start()
        try:
            time.sleep(0.6)
            self.assertGreaterEqual(tr.polls, 2)
            p1 = dict(self.world.get_players())
            time.sleep(0.4)
            p2 = dict(self.world.get_players())
            # Игроки мок-сервера двигаются — координаты должны измениться
            moved = any(p1[n]["pos"] != p2[n]["pos"] for n in p1 if n in p2)
            self.assertTrue(moved, "позиции не обновляются")
        finally:
            tr.stop()
        self.assertFalse(tr.running)

    def test_survives_server_restart(self):
        tr = PlayerTracker(self.pool, self.world, interval=0.1)
        tr.poll_once()
        self.server.stop()                     # рвём связь
        time.sleep(0.05)
        try:
            tr.poll_once()
        except Exception as exc:  # noqa: BLE001
            self.fail(f"Трекер не пережил обрыв связи: {exc!r}")
        self.server.start()                    # поднимаем обратно (порт тот же)
        time.sleep(0.1)
        found = tr.poll_once()
        self.assertEqual(set(found), {"Arlik88", "Steve", "Notch"})

    def test_parse_list_localized(self):
        names = mc.parse_player_list("Сейчас на сервере 1 из 20 игроков: Arlik88")
        self.assertEqual(names, ["Arlik88"])


class TestTerrainScanner(_Base):
    def setUp(self):
        super().setUp()
        self.scanner = TerrainScanner(self.pool, self.world, top=200, bottom=-64,
                                      coarse=16, batch=64)

    def test_scan_matches_mock_world(self):
        res = self.scanner.scan_sync((0, 0), radius=32, step=8)
        self.assertFalse(res.cancelled)
        self.assertGreater(res.tiles, 0)
        grid = self.world.terrain
        for (x, z), (y, kind) in grid.items():
            self.assertEqual(y, top_y(x, z), f"высота не совпала в {x},{z}")
            self.assertIn(kind, {k for _, k in mc.SURFACE_KINDS} | {"other"})

    def test_batching_cuts_roundtrips(self):
        """Главная оптимизация: заходов в разы меньше, чем колонок."""
        res = self.scanner.scan_sync((0, 0), radius=32, step=8)
        columns = res.columns
        self.assertGreater(columns, 50)
        self.assertGreater(res.commands, columns)          # несколько проб на колонку
        self.assertLess(res.roundtrips, columns,
                        f"пакетирование не сработало: {res.roundtrips} заходов "
                        f"на {columns} колонок")
        self.assertLess(res.commands / max(1, res.roundtrips), 64.0 + 1)

    def test_progress_events_and_world_state(self):
        seen = []
        self.world.bus.subscribe(TOPIC_SCAN_PROGRESS, lambda d, t: seen.append((d, t)))
        res = self.scanner.scan_sync((0, 0), radius=24, step=8)
        self.assertGreater(len(seen), 0)
        done, total = self.world.scan_progress
        self.assertEqual((done, total), (res.columns, res.columns))
        self.assertFalse(self.world.scanning)

    def test_restart_after_finish(self):
        """Регресс наброска: флаг `running` не сбрасывался — второй запуск молчал."""
        self.scanner.scan_sync((0, 0), radius=16, step=16)
        self.assertFalse(self.scanner.scanning)
        self.assertTrue(self.scanner.start((0, 0), radius=16, step=16))
        self.assertTrue(self.scanner.wait(timeout=30))
        self.assertFalse(self.scanner.scanning)

    def test_cancel_keeps_partial_results(self):
        """Отмена по событию прогресса — детерминированно, без привязки к скорости."""
        import threading as _th
        first_progress = _th.Event()
        self.world.bus.subscribe(TOPIC_SCAN_PROGRESS,
                                 lambda d, t: first_progress.set())
        # Область заведомо больше, чем успевает обработаться до первого события
        self.assertTrue(self.scanner.start((0, 0), radius=128, step=4))
        self.assertTrue(first_progress.wait(timeout=20),
                        "сканер не опубликовал ни одного события прогресса")
        self.scanner.cancel()
        self.assertTrue(self.scanner.wait(timeout=30))

        res = self.scanner.last
        self.assertIsNotNone(res)
        self.assertTrue(res.cancelled)
        self.assertFalse(self.scanner.scanning)
        self.assertFalse(self.world.scanning)

        # Повторный запуск после отмены обязан работать
        self.assertTrue(self.scanner.start((0, 0), radius=16, step=16))
        self.assertTrue(self.scanner.wait(timeout=30))
        self.assertFalse(self.scanner.scanning)
        self.scanner.stop()

    def test_double_start_refused(self):
        self.assertTrue(self.scanner.start((0, 0), radius=48, step=8))
        self.assertFalse(self.scanner.start((0, 0), radius=48, step=8))
        self.scanner.stop()

    def test_max_columns_guard(self):
        sc = TerrainScanner(self.pool, self.world, max_columns=10)
        self.assertFalse(sc.start((0, 0), radius=256, step=8))
        self.assertIn("Слишком большая область", sc.error)
        self.assertFalse(sc.scanning)

    def test_water_area_detected(self):
        res = self.scanner.scan_sync((120, -80), radius=24, step=8)
        kinds = {k for _xy, (_y, k) in self.world.terrain.items()}
        self.assertIn("water", kinds, f"озеро не найдено, тайлы: {res.tiles}")

    def test_new_scan_clears_previous(self):
        self.scanner.scan_sync((0, 0), radius=16, step=16)
        first = len(self.world.terrain)
        self.assertGreater(first, 0)
        self.scanner.scan_sync((900, 900), radius=16, step=16)
        keys = [k for k, _ in self.world.terrain.items()]
        self.assertTrue(all(abs(x - 900) <= 24 and abs(z - 900) <= 24
                            for x, z in keys),
                        "старый рельеф не очищен")


if __name__ == "__main__":
    unittest.main(verbosity=2)
