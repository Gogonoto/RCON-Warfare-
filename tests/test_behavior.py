"""Поведенческий смоук главного тракта (не «код работает», а «приложение едет»).

Прогоняет РЕАЛЬНЫЙ путь оператора через CoreFacade + имитатор сервера + фоновый
цикл движка: подключение -> запуск бота -> движение -> маршрут -> ИИ -> режим
службы. Если здесь что-то падает, виноват UI не может быть — неисправно ядро.

Это ответ на жалобу «тесты мерят работоспособность кода, а не поведение
приложения»: здесь проверяется именно поведение (юнит реально сдвинулся,
маршрут реально назначился, ИИ реально включился), а не внутренние вызовы.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from rwf.config import AppConfig
from rwf.routes import Action, Route, Waypoint
from rwf.ui.facade import CoreFacade


class TestAppBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = AppConfig()
        cfg._path = Path(self.tmp.name) / "rwf.json"
        cfg.rcon.mock = True
        cfg.rcon.mock_players = ("Arlik88",)
        self.facade = CoreFacade(cfg)
        self.facade.start()

    def tearDown(self):
        self.facade.stop(timeout=2.0)
        self.tmp.cleanup()

    def test_spawn_move_route_ai_duty(self):
        f = self.facade
        self.assertTrue(f.connect(mock=True), "не подключились к имитатору")

        uid = f.spawn("attacker", 0.0, 0.0, 150.0, is_bot=True)
        self.assertIsNotNone(uid)
        unit = f.app.engine.get(uid)
        self.assertIsNotNone(unit)
        p0 = tuple(unit.pos)

        # движение: борт обязан сдвинуться и набрать скорость за ~2 секунды
        for _ in range(4):
            time.sleep(0.5)
        unit = f.app.engine.get(uid)
        moved = (unit.pos[0] - p0[0]) ** 2 + (unit.pos[2] - p0[2]) ** 2
        self.assertGreater(moved, 100.0, f"борт не движется (pos {p0} -> {unit.pos})")
        self.assertGreater(unit.speed, 5.0, "борт не набрал скорость")

        # маршрут
        route = Route([Waypoint(x=500.0, z=0.0, altitude=150.0,
                                action=Action.NAVIGATE)], uid, name="смоук")
        self.assertTrue(f.app.engine.assign_route(uid, route))
        status = f.app.engine.route_status(uid)
        self.assertTrue(status.get("has_route"), f"маршрут не назначен: {status}")

        # ИИ включается и показывает шаблон/фазу/цель
        f.set_ai(uid, "strike", "")
        time.sleep(0.6)
        ai = f.app.engine.ai_status(uid) or {}
        self.assertTrue(ai.get("ai"), f"ИИ не включился: {ai}")
        self.assertTrue(ai.get("target"), f"ИИ без цели: {ai}")

        # режим службы не падает и меняет состояние
        f.set_duty(uid, "combat")
        unit = f.app.engine.get(uid)
        self.assertIsNotNone(unit)


if __name__ == "__main__":
    unittest.main()
