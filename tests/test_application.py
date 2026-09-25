"""P3.4: покрытие Application.connect() — успех, ошибки, деградация probe.

Регресс-метка: §7 п.4 handoff-документа (connect с ошибками не был покрыт).
Инварианты, которые закрепляют тесты:
  * при сбое подключения стек НЕ остаётся в полуинициализированном
    состоянии: pool is None, _mock остановлен, брошен ConnectionError_;
  * падение зондов возможностей НЕ роняет старт (probe безопасен);
  * disconnect идемпотентен и останавливает всё в обратном порядке (§8.9).
"""
import threading
import unittest

from rwf.app import Application, ConnectionError_
from rwf.config import AppConfig
from rwf.mc import ServerCaps
from rwf.mock_server import MockMCServer


def _app_mock(port: int, password: str = "2203") -> Application:
    cfg = AppConfig()
    cfg.rcon.host = "127.0.0.1"
    cfg.rcon.port = port
    cfg.rcon.password = password
    cfg.rcon.mock = False          # поднимаем свой MockMCServer явно
    cfg.rcon.pool_size = 1
    cfg.rcon.queue_workers = 1
    cfg.rcon.queue_rate = 0
    return Application(cfg)


class TestApplicationConnect(unittest.TestCase):
    def test_connect_and_disconnect_ok(self):
        server = MockMCServer(port=0, password="2203").start()
        try:
            app = _app_mock(server.port)
            caps = app.connect()
            self.assertIsInstance(caps, ServerCaps)
            self.assertIsNotNone(app.pool)
            self.assertIsNotNone(app.queue)
            self.assertIsNotNone(app.engine)
            self.assertTrue(app._started)
            app.disconnect()
            self.assertFalse(app._started)
            # disconnect закрывает пул, но не обнуляет ссылку (владелец — connect);
            # проверяем фактическое состояние: все соединения закрыты.
            self.assertEqual(app.pool.stats()["created"], 0)
            self.assertIsNone(app._mock)   # внешний mock не наш — не должен течь
        finally:
            server.stop()

    def test_wrong_password_raises_and_leaves_no_stack(self):
        """Неверный пароль -> ConnectionError_, pool/_mock обнулены."""
        server = MockMCServer(port=0, password="2203").start()
        try:
            app = _app_mock(server.port, password="WRONG")
            with self.assertRaises(ConnectionError_):
                app.connect()
            self.assertIsNone(app.pool)
            self.assertIsNone(app._mock)
            self.assertFalse(app._started)
        finally:
            server.stop()

    def test_unreachable_server_raises(self):
        """Недоступный сервер (порт закрыт) -> ConnectionError_, pool=None.

        Порт выбирается так, чтобы на нём заведомо никто не слушал:
        bind(:0) -> номер -> сразу close; в контейнере это устойчиво.
        """
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
        s.close()
        app = _app_mock(dead_port)
        with self.assertRaises(ConnectionError_):
            app.connect()
        self.assertIsNone(app.pool)
        self.assertFalse(app._started)

    def test_internal_mock_stopped_on_auth_failure(self):
        """Если connect поднимал внутренний mock и аутентификация не прошла,
        mock должен быть остановлен (не течь процессом-зомби)."""
        cfg = AppConfig()
        cfg.rcon.mock = True
        cfg.rcon.password = "2203"
        cfg.rcon.pool_size = 1
        cfg.rcon.queue_rate = 0
        app = Application(cfg)
        real_connect_all = None
        import rwf.rcon as rcon_mod
        orig = rcon_mod.RCONPool.connect_all

        def broken(self):
            raise rcon_mod.RCONAuthError("bad auth")
        rcon_mod.RCONPool.connect_all = broken
        try:
            with self.assertRaises(ConnectionError_):
                app.connect(use_mock=True)
        finally:
            rcon_mod.RCONPool.connect_all = orig
        self.assertIsNone(app._mock, "внутренний имитатор обязан быть остановлен")
        self.assertIsNone(app.pool)

    def test_probe_failure_does_not_crash_start(self):
        """§7 п.4: упавший ServerCaps.probe не роняет Application.

        Зонды внутри probe уже обёрнуты в try/except; дополнительно эмулируем
        «сервер режет connection» между connect_all и probe.
        """
        server = MockMCServer(port=0, password="2203").start()
        try:
            app = _app_mock(server.port)
            orig_run = app.__class__.__mro__  # noqa - просто sanity
            import rwf.mc as mc_mod
            orig_probe = mc_mod.ServerCaps.probe

            def angry_probe(run, assume_version=None):
                # run бросает на ПЕРВОМ ЖЕ обращении — как разорванный сокет
                def bad(cmd):
                    raise OSError("connection reset")
                return orig_probe(bad, assume_version=assume_version)
            mc_mod.ServerCaps.probe = classmethod(
                lambda cls, run, assume_version=None: angry_probe(run, assume_version))
            try:
                caps = app.connect()          # не должно упасть
                self.assertIsNotNone(caps)
                app.disconnect()
            finally:
                mc_mod.ServerCaps.probe = orig_probe
        finally:
            server.stop()

    def test_double_connect_is_noop(self):
        server = MockMCServer(port=0, password="2203").start()
        try:
            app = _app_mock(server.port)
            c1 = app.connect()
            c2 = app.connect()
            self.assertIs(c1, c2)
            app.disconnect()
        finally:
            server.stop()

    def test_disconnect_idempotent(self):
        server = MockMCServer(port=0, password="2203").start()
        try:
            app = _app_mock(server.port)
            app.connect()
            app.disconnect()
            app.disconnect()   # второй вызов — no-op, не падает
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
