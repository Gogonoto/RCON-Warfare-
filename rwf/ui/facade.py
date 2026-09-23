"""
Фасад ядра для интерфейса: CoreFacade.

Единственная связь UI с `Application`/`UnitEngine` (секция 7 скилла):

    [движок/RCON/сканер] --MSG_Q--> [главный цикл DPG: drain -> STATE]
    [колбэки DPG]        --OUT_Q--> [поток управления фасада] --> ядро

* Рабочие потоки НЕ трогают STATE и dpg — только кладут плоские кортежи в MSG_Q.
* Колбэки UI НЕ вызывают ядро напрямую — только `send("метод", *args)` в OUT_Q;
  блокирующий сетевой I/O (подключение, скан) живёт в потоке управления.
* `MapRenderer` принадлежит ГЛАВНОМУ потоку (камера, слои, планировщик);
  рабочий поток снимков использует только `TerrainRaster.render` (кэш растра
  живёт в нём и из главного потока не вызывается — владение разделено).

Модуль намеренно не импортирует dearpygui: логику фасада можно тестировать
без GL-контекста.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..ai import ai_labels, make_ai
from ..app import Application
from ..config import AppConfig
from ..presets import Preset, PresetLibrary, ensure_seeded
from ..progress import ACHIEVEMENTS, Progress
from ..settings import Settings
from ..sound import SoundManager
from ..gamefx import GameFX
from ..maprender import MapRenderer
from ..routes import ACTION_COLORS, ACTION_LABELS, Action, Route, Waypoint
from ..storage import RouteLibrary
from ..units import VARIANTS
from ..weapons import Target

log = logging.getLogger(__name__)

#: частота снимков мира для UI, Гц
SNAPSHOT_HZ = 15.0
#: частота медленных данных (статус, ПЗРК), Гц
SLOW_HZ = 2.0

#: темы шины, которые попадают в журнал UI
_BUS_LOG = {
    "combat": ("red", lambda args: str(args[0]) if args else ""),
    "log": (None, lambda args: (str(args[0]), (args[1] if len(args) > 1 else ""))),
    "unit.landed": ("green", lambda args: f"Юнит #{args[0]} совершил посадку"),
    "unit.launched": ("green", lambda args: f"Юнит #{args[0]} взлетел с базы"),
    "unit.recovery": ("cyan", lambda args: f"Юнит #{args[0]} заходит на посадку"),
    "manpads.lock": ("green", lambda args: f"ПЗРК: {args[0]} захватил цель #{args[1]}"),
    "manpads.launch": ("aqua", lambda args: f"ПЗРК: {args[0]} — ПУСК по #{args[1]}"),
    "missile.hit": ("yellow", lambda args: f"Ракета #{args[0]} поразила цель"),
    "rcon.failed": ("yellow", lambda args: f"RCON: {str(args[1])[:100]}"
                    if len(args) > 1 else "RCON: ошибка команды"),
}


class CoreFacade:
    """Управление ядром из UI + публикация снимков в MSG_Q."""

    def __init__(self, cfg: Optional[AppConfig] = None,
                 msg_q: Optional[queue.Queue] = None,
                 out_q: Optional[queue.Queue] = None,
                 settings: Optional[Settings] = None):
        self.cfg = cfg or AppConfig()
        self.settings = settings or Settings.load()
        self.sound = SoundManager(self.settings)
        self.msg_q: queue.Queue = msg_q if msg_q is not None else queue.Queue()
        self.out_q: queue.Queue = out_q if out_q is not None else queue.Queue()
        self.app: Optional[Application] = None
        self.fx: Optional[GameFX] = None
        self.renderer = MapRenderer(
            size=(self.cfg.map.size, self.cfg.map.size),
            view_radius=self.cfg.map.view_radius)
        self.renderer.transform.min_radius = self.cfg.map.min_radius
        self.renderer.transform.max_radius = self.cfg.map.max_radius
        self.renderer.transform.follow = False      # следование ведёт mapfacade
        self.renderer.set_action_colors(ACTION_COLORS)
        self.library = RouteLibrary(self.cfg.path.parent / "routes")
        #: пресеты запуска (LAUNCH-01): именованные рецепты машин
        self.presets = PresetLibrary(self.cfg.path.parent / "presets")
        ensure_seeded(self.presets)
        #: очки/звания/достижения оператора (GAME-01)
        self.progress = Progress(self.cfg.path.parent / "progress.json")

        self.selected_uid: Optional[int] = None
        self.selected_base_id: Optional[int] = None
        self.follow_uid: Optional[int] = None

        self._running = threading.Event()
        self._snap_thread: Optional[threading.Thread] = None
        self._ctl_thread: Optional[threading.Thread] = None
        self._bus_subs: List[Any] = []
        self.connected = False
        # кэши «изменилось ли» — чтобы не спамить одинаковые снимки в очередь
        self._last_frame_rev = -1
        self._last_terrain_key: Optional[Any] = None
        self._last_bases_sig: Optional[Any] = None
        self._last_scan: Optional[Tuple[int, int, bool]] = None

    # ------------------------------------------------------------ жизненный цикл
    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self.sound.start()
        self._snap_thread = threading.Thread(target=self._snapshot_loop,
                                             name="ui-snapshots", daemon=True)
        self._ctl_thread = threading.Thread(target=self._control_loop,
                                            name="ui-control", daemon=True)
        self._snap_thread.start()
        self._ctl_thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._running.clear()
        self.sound.stop(timeout=1.0)
        self.settings.save()
        for t in (self._ctl_thread, self._snap_thread):
            if t:
                t.join(timeout=timeout)
        self._ctl_thread = self._snap_thread = None
        self._detach_bus()
        if self.app:
            try:
                self.app.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка остановки приложения")
            self.app = None
        self.connected = False

    def send(self, method: str, *args: Any) -> None:
        """Из колбэка UI: команда в поток управления (никогда не напрямую!)."""
        self.out_q.put((method, args))

    # ------------------------------------------------------------ шина событий
    def _attach_bus(self, bus) -> None:
        self._detach_bus()
        self._bus_subs.append(bus.subscribe("*", self._relay_bus))
        self._bus_subs.append(bus.subscribe("*", self.sound.bus_hook))

    def _on_score(self, args: Tuple[Any, ...]) -> None:
        """Событие прогресса из ядра: учесть очки, открыть достижения."""
        try:
            kind = str(args[0]) if args else ""
            kw = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
            delta, newly = self.progress.event(kind, **kw)
            self.msg_q.put(("progress", self.progress.snapshot()))
            if delta:
                self.msg_q.put(("log", f"Очки: {delta:+d} ({kind})",
                                "green" if delta > 0 else "yellow"))
            for key in newly:
                ach = next((a for a in ACHIEVEMENTS if a.key == key), None)
                if ach is not None:
                    self.msg_q.put(("log", f"ДОСТИЖЕНИЕ: {ach.name} — "
                                            f"{ach.hint}", "aqua"))
                    self.sound_play("lock")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки события прогресса")

    def _detach_bus(self) -> None:
        for sub in self._bus_subs:
            try:
                sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._bus_subs.clear()

    def _relay_bus(self, topic: str, *args: Any, **kwargs: Any) -> None:
        """Вызывается в потоке ядра: только put в MSG_Q (I1)."""
        if topic == "score":
            self._on_score(args)
            return
        entry = _BUS_LOG.get(topic)
        if entry is None:
            return
        try:
            level, fmt = entry
            out = fmt(args)
            if isinstance(out, tuple):
                text, lvl = out
            else:
                text, lvl = out, (level or "")
            self.msg_q.put(("log", text, lvl))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ подключение
    def connect(self, host: Optional[str] = None, port: Optional[int] = None,
                password: Optional[str] = None, mock: Optional[bool] = None
                ) -> bool:
        """Выполняется в потоке управления: сеть не блокирует рендер."""
        cfg = self.cfg
        if host:
            cfg.rcon.host = host
        if port:
            cfg.rcon.port = int(port)
        if password is not None:
            cfg.rcon.password = password
        if mock is not None:
            cfg.rcon.mock = bool(mock)
        self.msg_q.put(("log", "Подключение к серверу…", "yellow"))
        try:
            app = Application(cfg)
            caps = app.connect()
            app.engine.start()
            app.tracker.start()
            if app.gun_poller:
                app.gun_poller.start()
            if app.manpads:
                app.manpads.start()
        except Exception as exc:  # noqa: BLE001
            self.msg_q.put(("connected", False, str(exc)))
            self.msg_q.put(("log", f"Не удалось подключиться: {exc}", "red"))
            self.connected = False
            return False
        self.app = app
        self.connected = True
        self.fx = GameFX(app.queue, cfg.combat, silent=cfg.combat.silent_mode)
        fx = self.fx

        def _announce_damage(ev) -> None:
            if getattr(ev, "destroyed", False):
                fx.say(f"{ev.target_label} #{ev.target_id} уничтожен", "red")

        app.engine.fx_announce = _announce_damage
        self._attach_bus(app.bus)
        desc = caps.describe()
        self.msg_q.put(("connected", True, desc))
        self.msg_q.put(("log", f"Подключено: {desc}", "green"))
        return True

    def disconnect(self) -> None:
        if self.app:
            try:
                self.app.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка отключения")
            self.app = None
        self._detach_bus()
        self.connected = False
        self._last_frame_rev = -1
        self._last_terrain_key = None
        self.msg_q.put(("connected", False, "не подключено"))
        self.msg_q.put(("log", "Отключено от сервера", "yellow"))

    # ------------------------------------------------------------ поток снимков
    def _snapshot_loop(self) -> None:
        period = 1.0 / SNAPSHOT_HZ
        slow_period = 1.0 / SLOW_HZ
        last_slow = 0.0
        while self._running.is_set():
            started = time.perf_counter()
            try:
                self._push_snapshots(started, slow_period, last_slow)
                if started - last_slow >= slow_period:
                    last_slow = started
            except Exception:  # noqa: BLE001
                log.exception("Ошибка потока снимков")
            self._running.wait(max(0.0, period - (time.perf_counter() - started)))

    def _push_snapshots(self, now: float, slow_period: float,
                        last_slow: float) -> None:
        app = self.app
        if app is None or not app.started:
            return
        q = self.msg_q
        world = app.world
        engine = app.engine

        snap = world.snapshot()
        rev = snap.get("revision", -1)
        if rev != self._last_frame_rev:
            self._last_frame_rev = rev
            q.put(("frame", snap))

        # базы: ревизия мира за ними не следит (авианосец движется) —
        # публикуем при любом изменении подписи
        bases = engine.bases.snapshot()
        sig = tuple((b["id"], round(b["x"], 1), round(b["z"], 1),
                     round(b["heading"], 1), b["free_pads"],
                     tuple(p["occupied_by"] for p in b["pads"]),
                     b.get("moving")) for b in bases)
        if sig != self._last_bases_sig:
            self._last_bases_sig = sig
            q.put(("bases", bases))

        # рельеф: растр пересчитывается только при изменении сетки (I5),
        # конвертация RGB->RGBA тоже здесь, в рабочем потоке
        grid = world.terrain
        key = grid.descriptor()
        if key != self._last_terrain_key:
            self._last_terrain_key = key
            data = self.renderer.terrain_image(grid)
            if data is None:
                q.put(("terrain_clear",))
            else:
                buf, w, h, world_rect = data
                # DPG 2.x принимает только float-текстуры: конвертируем
                # RGB-байты в float32 (0..1) ЗДЕСЬ, в рабочем потоке (I5),
                # чтобы главный цикл не тратил кадр на пересчёт.
                try:
                    from array import array
                    floats = array("f", (b * (1.0 / 255.0) for b in buf))
                except Exception:  # noqa: BLE001
                    floats = None
                if floats is not None:
                    q.put(("terrain", floats, w, h, world_rect, key))

        scan = (world.scan_progress[0], world.scan_progress[1], world.scanning)
        if scan != self._last_scan:
            self._last_scan = scan
            q.put(("scan", scan[0], scan[1], scan[2]))

        # медленные данные: телеметрия/пульт выбранного, статус, ПЗРК
        if now - last_slow >= slow_period:
            uid = self.selected_uid
            if uid is not None and engine:
                unit = engine.get(uid)
                if unit is not None:
                    q.put(("telemetry", uid, unit.telemetry()))
                    q.put(("console", uid, self.console_snapshot(uid)))
                    q.put(("route_status", uid, engine.route_status(uid)))
                    st = engine.ai_status(uid)
                    if st:
                        q.put(("ai_status", uid, st))
            if app.manpads:
                q.put(("manpads", app.manpads.snapshot()))
            try:
                q.put(("status", app.status()))
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ поток управления
    def _control_loop(self) -> None:
        while self._running.is_set():
            try:
                item = self.out_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:                 # сентинель остановки
                break
            method, args = item
            fn: Optional[Callable] = getattr(self, method, None)
            if fn is None or not callable(fn):
                self.msg_q.put(("log", f"OUT_Q: неизвестная команда {method!r}",
                                "red"))
                continue
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                log.exception("OUT_Q %s упал", method)
                self.msg_q.put(("log", f"{method}: {exc}", "red"))

    # ------------------------------------------------------------ выбор/журнал
    def log_ui(self, message: str, level: str = "") -> None:
        self.msg_q.put(("log", message, level))

    def sound_play(self, event: str) -> None:
        """Звук интерфейса/события (неблокирующе, настройки учитываются)."""
        self.sound.play(event)

    def set_setting(self, dotted: str, value) -> None:
        self.settings.set(dotted, value)

    def service_uid(self, uid: int) -> None:
        if not self.app:
            return
        done = self.app.engine.service_at_nearest_base(uid)
        if done:
            self.msg_q.put(("log", f"#{uid}: обслужен "
                                   f"({', '.join(done)})", "green"))
            self.sound.play("landing")
        else:
            self.msg_q.put(("log", "Нет подходящей базы для обслуживания",
                            "red"))

    def select(self, uid: Optional[int]) -> None:
        self.selected_uid = uid
        self.msg_q.put(("selected", uid))

    def select_base(self, base_id: Optional[int]) -> None:
        self.selected_base_id = base_id

    def set_follow(self, enabled: bool) -> None:
        self.follow_uid = self.selected_uid if enabled else None

    def center_on_selection(self) -> None:
        """Центрировать карту на выбранном объекте (юнит, база или игрок)."""
        app = self.app
        if app is None:
            return
        uid = self.selected_uid
        if uid is not None:
            unit = app.engine.get(uid)
            if unit is not None:
                self.renderer.transform.set_center(unit.pos[0], unit.pos[2],
                                                   keep_follow=False)
                return
        base = app.engine.bases.get(self.selected_base_id) \
            if getattr(self, "selected_base_id", None) else None
        if base is not None:
            self.renderer.transform.set_center(base.x, base.z,
                                               keep_follow=False)

    # ------------------------------------------------- режим службы (UX-15)
    def set_duty(self, uid: Optional[int], mode: str) -> None:
        """«Стоянка» / «В бой»: автовозврат с ТО либо автовзлёт и патруль."""
        if not self._require_connection():
            return
        assert self.app is not None
        target = uid if uid is not None else self.selected_uid
        if target is None:
            self.log_ui("Не выбран объект для смены режима", "yellow")
            return
        ok = self.app.engine.set_duty(int(target), mode)
        self.msg_q.put(("duty", int(target), mode, bool(ok)))

    def deliver_cargo_selected(self) -> None:
        """Сдать груз на ближайшую базу (логистика авианосца)."""
        if not self._require_connection() or self.selected_uid is None:
            return
        assert self.app is not None
        gained = self.app.engine.deliver_cargo(self.selected_uid)
        if gained <= 0:
            self.log_ui("Груз не принят: пустой борт или склад базы полон",
                        "yellow")

    def damage_base(self, base_id: int, amount: float) -> None:
        """Служебное: снять прочность с базы (тесты/отладка сценариев)."""
        if self.app is None:
            return
        base = self.app.engine.bases.get(base_id)
        if base is not None and base.take_damage(amount, "оператор"):
            self.app.engine._on_base_destroyed(base, "оператор")

    def _require_connection(self) -> bool:
        if not self.connected or self.app is None:
            self.msg_q.put(("log", "Нет подключения к серверу", "red"))
            return False
        return True

    # ------------------------------------------------------------ юниты
    @staticmethod
    def variants() -> List[Tuple[str, str, str]]:
        return [(k, v.label, v.description) for k, v in VARIANTS.items()]

    @staticmethod
    def action_labels() -> List[Tuple[str, str]]:
        return [(k, ACTION_LABELS[k]) for k in Action.ALL]

    @staticmethod
    def ai_templates() -> List[Tuple[str, str]]:
        return ai_labels()

    def spawn(self, variant: str, x: float, z: float, altitude: float,
              is_bot: bool = False, speed: Optional[float] = None,
              throttle: Optional[float] = None) -> Optional[int]:
        if not self._require_connection():
            return None
        assert self.app is not None
        try:
            unit = self.app.spawn(variant, (x, altitude, z), heading=0.0,
                                  is_bot=is_bot, speed=speed, throttle=throttle)
        except (KeyError, ValueError) as exc:
            self.msg_q.put(("log", f"Не удалось запустить: {exc}", "red"))
            return None
        self.select(unit.id)
        self.follow_uid = unit.id
        if self.fx:
            self.fx.announce_spawn(unit.spec.label, pos=unit.pos)
        self.msg_q.put(("log", f"Запущен {unit.spec.label} #{unit.id} "
                               f"({x:.0f}, {altitude:.0f}, {z:.0f})", "green"))
        return unit.id

    def despawn(self, uid: int, explode: bool = False) -> None:
        if not self._require_connection():
            return
        assert self.app is not None
        if self.app.engine.despawn(uid, explode=explode):
            self.msg_q.put(("log", f"#{uid}: снят с карты", "yellow"))
            if self.selected_uid == uid:
                self.select(None)

    def trim(self, droll: float = 0.0, dpitch: float = 0.0,
             dyaw: float = 0.0, dthrottle: float = 0.0) -> None:
        if self.app and self.selected_uid is not None:
            self.app.engine.trim(self.selected_uid, droll=droll, dpitch=dpitch,
                                 dyaw=dyaw, dthrottle=dthrottle)

    def level(self) -> None:
        if self.app and self.selected_uid is not None:
            self.app.engine.level(self.selected_uid)
            self.msg_q.put(("log", f"#{self.selected_uid}: выровнен", "aqua"))

    def pause_all_units(self, value: bool = True) -> None:
        if self.app:
            self.app.engine.pause_all(value)

    def toggle_pause(self) -> None:
        if not self.app or self.selected_uid is None:
            return
        engine = self.app.engine
        now = not engine.is_paused(self.selected_uid)
        engine.pause(self.selected_uid, now)
        self.msg_q.put(("log", f"#{self.selected_uid}: "
                               f"{'пауза' if now else 'продолжить'}", "yellow"))

    def fire_selected(self, category: Optional[str] = None,
                      mount_index: Optional[int] = None) -> None:
        if not self.app or self.selected_uid is None:
            return
        unit = self.app.engine.get(self.selected_uid)
        if unit is None:
            return
        target = self._auto_target(unit)
        n = self.app.engine.fire(unit.id, category=category,
                                 mount_index=mount_index, target=target)
        if n:
            label = category or (f"подвес {mount_index + 1}"
                                 if mount_index is not None else "готовое")
            self.msg_q.put(("log", f"#{unit.id}: огонь — {label} ({n})", "red"))
        else:
            self.msg_q.put(("log",
                            "Огонь невозможен: нет готовых подвесов или цели",
                            "yellow"))

    def _auto_target(self, unit) -> Optional[Target]:
        """Ближайший игрок — цель по умолчанию для ручного огня."""
        if self.app is None:
            return None
        players = self.app.world.get_players()
        best, best_d = None, float("inf")
        for name, rec in players.items():
            d = ((rec["pos"][0] - unit.pos[0]) ** 2
                 + (rec["pos"][2] - unit.pos[2]) ** 2) ** 0.5
            if d < best_d:
                best, best_d = name, d
        if best is None:
            return None
        rec = self.app.world.get_player(best)
        return Target(pos=rec.pos, vel=rec.vel, name=best) if rec else None

    @staticmethod
    def available_for_mount(unit, index: int) -> List[str]:
        if unit is None or not (0 <= index < len(unit.mounts)):
            return []
        return list(unit.AVAILABLE.get(unit.mounts[index].category, []))

    def available_for_selected(self, index: int) -> List[str]:
        if not self.app or self.selected_uid is None:
            return []
        return self.available_for_mount(
            self.app.engine.get(self.selected_uid), index)

    def load_weapon(self, uid: int, index: int, key: Optional[str]) -> bool:
        if not self.app:
            return False
        ok = self.app.engine.load_weapon(uid, index, key)
        if ok:
            from ..weapons import WEAPONS
            label = WEAPONS[key]["label"] if key else "— пусто —"
            self.msg_q.put(("log", f"#{uid}: подвес {index + 1} → {label}",
                            "aqua"))
        else:
            self.msg_q.put(("log",
                            f"#{uid}: оружие недоступно для этого подвеса",
                            "red"))
        return ok

    def rearm(self, uid: int) -> None:
        if self.app and self.app.engine.rearm(uid):
            self.msg_q.put(("log", f"#{uid}: боезапас пополнен", "green"))

    def refuel(self, uid: int) -> None:
        if self.app and self.app.engine.refuel(uid):
            self.msg_q.put(("log", f"#{uid}: заправлен", "green"))

    # ------------------------------------------------------------ маршруты
    def assign_route_points(self, uid: Optional[int],
                            points: List[Dict[str, Any]],
                            name: str = "") -> bool:
        """Назначить маршрут выбранным/указанным юнитам из точек планировщика."""
        if not self._require_connection():
            return False
        assert self.app is not None
        uid = self.selected_uid if uid is None else uid
        if uid is None:
            self.msg_q.put(("log", "Сначала выберите юнит", "red"))
            return False
        if not points:
            self.msg_q.put(("log", "Маршрут пуст", "red"))
            return False
        wps = [Waypoint(x=float(p["x"]), z=float(p["z"]),
                        altitude=float(p.get("alt", 150.0)),
                        action=str(p.get("action", Action.NAVIGATE)),
                        pass_mode=str(p.get("pass_mode", "auto")),
                        target_name=str(p.get("target_name", "")))
               for p in points]
        route = Route(wps, uid, name=name or "маршрут оператора")
        self.app.engine.assign_route(uid, route)
        self.msg_q.put(("log", f"Маршрут назначен #{uid}: {len(wps)} точек",
                        "green"))
        return True

    def clear_route(self, uid: Optional[int] = None) -> None:
        if not self.app:
            return
        uid = self.selected_uid if uid is None else uid
        if uid is not None:
            self.app.engine.assign_route(uid, None)
            self.app.engine.cancel_recovery(uid)
            self.msg_q.put(("log", f"#{uid}: маршрут снят", "yellow"))

    # ------------------------------------------------------------------- ИИ
    def set_ai(self, uid: Optional[int], template: str, target: str = "") -> None:
        if not self._require_connection():
            return
        assert self.app is not None
        uid = self.selected_uid if uid is None else uid
        if uid is None:
            self.msg_q.put(("log", "Выберите юнит для ИИ", "red"))
            return
        try:
            ai = make_ai(template, cfg=self.cfg, target_name=target)
        except KeyError as exc:
            self.msg_q.put(("log", str(exc), "red"))
            return
        self.app.engine.set_ai(uid, ai)
        self.msg_q.put(("log", f"#{uid}: включён ИИ «{template}»"
                        + (f", цель {target}" if target else ""), "aqua"))

    def clear_ai(self, uid: Optional[int] = None) -> None:
        if not self.app:
            return
        uid = self.selected_uid if uid is None else uid
        if uid is not None:
            self.app.engine.set_ai(uid, None)
            self.msg_q.put(("log", f"#{uid}: ИИ выключен", "yellow"))

    # --------------------------------------------------------------- сканер
    def scan(self, x: Optional[float] = None, z: Optional[float] = None,
             radius: Optional[int] = None, step: Optional[int] = None) -> None:
        if not self._require_connection():
            return
        assert self.app is not None
        center = None
        if x is not None and z is not None:
            center = (x, z)
        elif self.selected_uid is not None:
            unit = self.app.engine.get(self.selected_uid)
            if unit is not None:
                center = (unit.pos[0], unit.pos[2])
        if self.app.scan(center=center, radius=radius, step=step):
            self.msg_q.put(("log", "Скан рельефа начат", "cyan"))
        else:
            self.msg_q.put(("log", "Скан уже идёт", "yellow"))

    def cancel_scan(self) -> None:
        if self.app and self.app.scanner:
            self.app.scanner.cancel()
            self.msg_q.put(("log", "Скан отменён", "yellow"))

    def clear_terrain(self) -> None:
        if self.app:
            self.app.world.terrain.clear()
            self._last_terrain_key = None
            self.msg_q.put(("terrain_clear",))
            self.msg_q.put(("log", "Рельеф очищен", "yellow"))

    # --------------------------------------------------------------- метки
    def set_waypoint(self, x: float, z: float) -> None:
        if self.app:
            self.app.world.set_waypoint(x, z)

    def set_strike_zone(self, x1: float, z1: float, x2: float, z2: float) -> None:
        if self.app:
            self.app.world.set_strike_zone(x1, z1, x2, z2)
            self.msg_q.put(("log",
                            f"Зона удара: ({x1:.0f},{z1:.0f}) — ({x2:.0f},{z2:.0f})",
                            "red"))

    def set_launch_point(self, x: float, z: float) -> None:
        if self.app:
            self.app.world.set_launch_point(x, z)

    def set_base_marker(self, x: float, z: float) -> None:
        if self.app:
            self.app.world.set_base(x, z)

    def clear_zones(self) -> None:
        if self.app:
            self.app.world.clear_strike_zone()
            self.app.world.clear_waypoint()
            self.app.world.clear_launch_point()
            self.app.world.clear_base()
            self.msg_q.put(("log", "Метки очищены", "yellow"))

    # ---------------------------------------------------------------- базы
    def add_base(self, name: str, kind: str, x: float, z: float,
                 heading: float = 0.0) -> bool:
        if not self._require_connection():
            return False
        assert self.app is not None
        try:
            base = self.app.engine.bases.add(name, kind, x, z, heading=heading)
        except ValueError as exc:
            self.msg_q.put(("log", str(exc), "red"))
            return False
        self.msg_q.put(("log", f"База «{base.name}» ({base.kind}) создана "
                               f"в ({x:.0f}, {z:.0f})", "green"))
        return True

    def move_base(self, base_id: int, x: float, z: float) -> None:
        if not self.app:
            return
        if not self.app.engine.move_base(base_id, x, z):
            self.msg_q.put(("log", "База не может перемещаться "
                                   "(или не найдена)", "red"))

    # ------------------------------------------------------------ пресеты
    def preset_names(self, variant: Optional[str] = None) -> List[str]:
        """Имена пресетов, при желании — только для варианта техники."""
        if variant is None:
            return self.presets.names()
        return self.presets.by_variant(variant)

    def get_preset(self, name: str) -> Optional[Preset]:
        try:
            return self.presets.load(name)
        except (OSError, ValueError, KeyError):
            self.msg_q.put(("log", f"Пресет «{name}» не читается", "red"))
            return None

    def save_preset(self, preset: Preset) -> bool:
        try:
            self.presets.save(preset)
            self.msg_q.put(("log", f"Пресет «{preset.name}» сохранён", "green"))
            self.msg_q.put(("preset_saved", preset.name))
            return True
        except OSError as exc:
            self.msg_q.put(("log", f"Не удалось сохранить пресет: {exc}",
                            "red"))
            return False

    def delete_preset(self, name: str) -> bool:
        ok = self.presets.delete(name)
        self.msg_q.put(("log", f"Пресет «{name}» удалён" if ok
                        else f"Пресет «{name}» не найден",
                        "green" if ok else "yellow"))
        return ok

    def launch_preset(self, name: str, base_id: Optional[int]) -> Optional[int]:
        """Запуск машины по пресету (основной путь оператора, LAUNCH-01)."""
        preset = self.get_preset(name)
        if preset is None:
            return None
        uid = self.launch_from_base(base_id, preset.variant, preset.altitude,
                                    preset.is_bot, dict(preset.loadout))
        if uid is not None and preset.ai:
            self.set_ai(uid, preset.ai, "")
        return uid

    def create_base_dialog(self, name: str, kind: str, x: float, z: float,
                           heading: float = 0.0,
                           health: Optional[float] = None) -> bool:
        """Создание базы из диалога (BASE-ADD): тип, локация, прочность."""
        if not self._require_connection():
            return False
        assert self.app is not None
        try:
            base = self.app.engine.bases.add(name, kind, x, z,
                                             heading=heading, health=health)
        except ValueError as exc:
            self.msg_q.put(("log", str(exc), "red"))
            return False
        self.msg_q.put(("log",
                        f"База «{base.name}» создана в ({x:.0f}, {z:.0f}), "
                        f"прочность {base.health_max:.0f}", "green"))
        self.msg_q.put(("base_created", base.id))
        return True

    def launch_from_base(self, base_id: int, variant: str, altitude: float,
                         is_bot: bool = False,
                         loadout: Optional[Dict[int, str]] = None
                         ) -> Optional[int]:
        if not self._require_connection():
            return None
        assert self.app is not None
        unit = self.app.engine.spawn_from_base(base_id, variant, altitude,
                                               is_bot=is_bot)
        if unit is None:
            self.msg_q.put(("log", "Нет свободной стоянки на базе", "red"))
            return None
        for idx, key in (loadout or {}).items():
            if key:
                self.app.engine.load_weapon(unit.id, int(idx), key)
        self.select(unit.id)
        self.follow_uid = unit.id
        if self.fx:
            self.fx.announce_spawn(unit.spec.label, pos=unit.pos)
        self.msg_q.put(("log",
                        f"Запуск #{unit.id} {unit.spec.label} с базы", "green"))
        return unit.id

    def request_recovery(self, uid: Optional[int],
                         base_id: Optional[int] = None) -> None:
        if not self.app:
            return
        uid = self.selected_uid if uid is None else uid
        if uid is None:
            return
        if not self.app.engine.request_recovery(uid, base_id):
            self.msg_q.put(("log",
                            "Посадка невозможна: нет принимающей базы "
                            "или свободных стоянок", "red"))

    def launch_parked(self, uid: Optional[int]) -> None:
        if not self.app:
            return
        uid = self.selected_uid if uid is None else uid
        if uid is not None and not self.app.engine.launch_parked(uid):
            self.msg_q.put(("log", "Юнит не на стоянке", "yellow"))

    def service_selected(self) -> None:
        if not self.app or self.selected_uid is None:
            return
        done = self.app.engine.service_at_nearest_base(self.selected_uid)
        if done:
            self.msg_q.put(("log",
                            f"#{self.selected_uid}: обслужен "
                            f"({', '.join(done)})", "green"))
        else:
            self.msg_q.put(("log", "Нет подходящей базы для обслуживания",
                            "red"))

    # ------------------------------------------------------- библиотека маршрутов
    def library_names(self) -> List[str]:
        names = self.library.names()
        self.msg_q.put(("route_library", names))
        return names

    def save_route(self, name: str, points: List[Dict[str, Any]]) -> None:
        if not name or not points:
            self.msg_q.put(("log", "Не указано имя или маршрут пуст", "red"))
            return
        wps = [Waypoint(x=float(p["x"]), z=float(p["z"]),
                        altitude=float(p.get("alt", 150.0)),
                        action=str(p.get("action", Action.NAVIGATE)))
               for p in points]
        self.library.save(Route(wps, 0, name=name), name)
        self.msg_q.put(("log", f"Маршрут «{name}» сохранён", "green"))
        self.library_names()

    def load_route(self, name: str) -> List[Dict[str, Any]]:
        try:
            route = self.library.load(name)
        except Exception as exc:  # noqa: BLE001
            self.msg_q.put(("log", f"Не загрузить «{name}»: {exc}", "red"))
            return []
        points = [{"x": wp.x, "z": wp.z, "alt": wp.altitude,
                   "action": wp.action} for wp in route.waypoints]
        self.msg_q.put(("planner_loaded", points))
        self.msg_q.put(("log", f"Маршрут «{name}» загружен: {len(points)} точек",
                        "green"))
        return points

    def delete_route(self, name: str) -> None:
        if self.library.delete(name):
            self.msg_q.put(("log", f"Маршрут «{name}» удалён", "yellow"))
            self.library_names()

    # ------------------------------------------------------------ снимки для UI
    def console_snapshot(self, uid: int) -> Dict[str, Any]:
        """Обогащённый снимок для пульта юнита."""
        import math as _m
        if self.app is None:
            return {}
        unit = self.app.engine.get(uid)
        if unit is None:
            return {}
        snap = unit.snapshot()
        snap["vs"] = -_m.sin(_m.radians(unit.pitch)) * unit.speed
        snap["faction"] = unit.faction
        base = (self.app.engine.bases.get(unit.base_id)
                if unit.base_id is not None else None)
        snap["base_name"] = base.name if base else ""
        nb = self.app.engine.bases.nearest(unit.pos[0], unit.pos[2],
                                           unit_kind=unit.spec.kind)
        snap["nearest_base"] = ({"name": nb.name, "kind": nb.kind,
                                 "distance": nb.distance_to(unit.pos[0],
                                                            unit.pos[2])}
                                if nb else None)
        snap["parked"] = self.app.engine.parked_info(uid)
        snap["recovery"] = self.app.engine.recovery_info(uid)
        snap["mount_available"] = [self.available_for_mount(unit, i)
                                   for i in range(len(unit.mounts))]
        return snap
