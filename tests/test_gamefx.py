"""Тесты игровой обратной связи: чат, actionbar, звуки, тихий режим."""
from __future__ import annotations

import unittest

from rwf.config import CombatConfig
from rwf.gamefx import GameFX
from rwf.rcon import CommandQueue, RCONPool
from rwf.mock_server import MockMCServer

HOST = "127.0.0.1"


class FakeQueue:
    def __init__(self):
        self.commands = []

    def submit(self, command, priority=10, key=None):
        self.commands.append(command)

    def submit_many(self, commands, priority=10, key_prefix=None):
        self.commands.extend(commands)

    def flush(self, timeout=1.0):
        return True

    def stats(self):
        return {"submitted": len(self.commands)}


class TestGameFX(unittest.TestCase):
    def setUp(self):
        self.queue = FakeQueue()
        self.cfg = CombatConfig()
        self.fx = GameFX(self.queue, self.cfg)

    def test_say_builds_tellraw(self):
        self.fx.say("Тест сообщения", "green")
        cmd = self.queue.commands[-1]
        self.assertIn("tellraw", cmd)
        self.assertIn("Тест сообщения", cmd)
        self.assertIn('"color":"green"', cmd)
        self.assertEqual(self.fx.sent_chat, 1)

    def test_say_escapes_quotes(self):
        self.fx.say('Он сказал "привет"')
        self.assertIn('\\"привет\\"', self.queue.commands[-1])

    def test_silent_mode_suppresses_chat(self):
        self.fx.silent = True
        self.fx.say("не должно уйти")
        self.assertEqual(self.queue.commands, [])
        # actionbar адресату остаётся и в тихом режиме
        self.fx.actionbar("Arlik88", "видимо")
        self.assertTrue(self.queue.commands)

    def test_chat_feedback_off(self):
        cfg = CombatConfig(chat_feedback=False)
        fx = GameFX(self.queue, cfg)
        fx.say("молчим")
        fx.actionbar("X", "молчим")
        fx.title("X", "молчим")
        self.assertEqual(self.queue.commands, [])

    def test_actionbar_and_title(self):
        self.fx.actionbar("Arlik88", "над хотбаром", "yellow")
        self.assertIn("actionbar", self.queue.commands[-1])
        self.fx.title("Arlik88", "ВЗЛЁТ", subtitle="Су-25")
        # actionbar сам собирается через `title ... actionbar`, поэтому
        # считаем именно пары title/subtitle
        titles = [c for c in self.queue.commands
                  if c.startswith("title") and " actionbar" not in c]
        self.assertEqual(len(titles), 2)
        self.assertIn("title Arlik88 title", titles[0])
        self.assertIn("title Arlik88 subtitle", titles[1])

    def test_sound_and_particles(self):
        self.fx.sound("minecraft:entity.generic.explode", (0.0, 64.0, 0.0))
        self.assertIn("playsound", self.queue.commands[-1])
        self.fx.particles("minecraft:large_smoke", (0.0, 64.0, 0.0))
        self.assertIn("particle", self.queue.commands[-1])
        self.assertEqual(self.fx.sent_fx, 2)

    def test_announce_spawn_and_loss(self):
        self.fx.announce_spawn("Су-25", "Arlik88", (0.0, 150.0, 0.0))
        joined = " ".join(self.queue.commands)
        self.assertIn("tellraw", joined)
        self.assertIn("actionbar", joined)
        self.assertIn("playsound", joined)
        self.queue.commands.clear()
        self.fx.announce_loss("Су-25", "столкновение", (0.0, 60.0, 0.0))
        joined = " ".join(self.queue.commands)
        self.assertIn("потерян", joined)
        self.assertIn("particle", joined)

    def test_strike_announce_color_by_kind(self):
        self.fx.announce_strike("Су-25", (10.0, 64.0, 20.0), "nuke")
        self.assertIn("dark_red", self.queue.commands[0])

    def test_countdown_runs_in_background(self):
        done = []
        self.fx.countdown("Arlik88", seconds=1, on_done=lambda: done.append(1))
        import time
        deadline = time.time() + 5
        while not done and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(done, [1], "колбэк отсчёта не вызвался")
        self.assertTrue(any("actionbar" in c for c in self.queue.commands))

    def test_stats(self):
        self.fx.say("x")
        st = self.fx.stats()
        self.assertEqual(st["chat"], 1)
        self.assertIn("fx", st)


class TestGameFXRealQueue(unittest.TestCase):
    """Команды обратной связи реально доходят до сервера через очередь."""

    def setUp(self):
        self.server = MockMCServer(host=HOST, port=0, password="2203").start()
        self.pool = RCONPool(HOST, self.server.port, "2203", size=2, timeout=5.0)
        self.queue = CommandQueue(self.pool, workers=1, rate=0)
        self.queue.start()

    def tearDown(self):
        self.queue.stop(timeout=2.0, flush=False)
        self.pool.close()
        self.server.stop()

    def test_messages_reach_server(self):
        fx = GameFX(self.queue, CombatConfig())
        fx.say("Проверка связи", "green")
        fx.actionbar("Arlik88", "над хотбаром")
        self.assertTrue(self.queue.flush(timeout=5))
        joined = " ".join(self.server.command_log)
        self.assertIn("tellraw", joined)
        self.assertIn("title", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
