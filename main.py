#!/usr/bin/env python3
"""
RCON Warfare — точка входа.

Запуск
------
    python main.py --mock            офлайн-демо на встроенном сервере
    python main.py --selftest        прогнать все тесты
    python main.py --probe           опросить реальный сервер (возможности/версия)
    python main.py --watch 10        10 секунд следить за игроками
    python main.py --scan 128 8      сканировать рельеф и напечатать ASCII-карту
    python main.py --bench           сравнить пакетную и поштучную отправку
    python main.py --gui             запустить диспетчерскую (Dear PyGui)

Без аргументов — то же, что `--mock`: поднимает имитатор сервера и показывает
весь тракт целиком, чтобы проект можно было проверить без Minecraft.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from rwf import __version__, mc
from rwf.config import AppConfig
from rwf.events import TOPIC_SCAN_PROGRESS
from rwf.rcon import CommandQueue, Priority, RCONError, RCONPool
from rwf.scanner import TerrainScanner
from rwf.tracker import PlayerTracker
from rwf.world import World

# ---------------------------------------------------------------------------
#  ANSI
# ---------------------------------------------------------------------------
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"


def head(text: str) -> str:
    return f"\n{C.BOLD}{C.CYAN}=== {text} ==={C.RESET}"


def ok(text: str) -> str:
    return f"{C.GREEN}[+]{C.RESET} {text}"


def warn(text: str) -> str:
    return f"{C.YELLOW}[!]{C.RESET} {text}"


def bad(text: str) -> str:
    return f"{C.RED}[-]{C.RESET} {text}"


# ---------------------------------------------------------------------------
#  ASCII-карта рельефа
# ---------------------------------------------------------------------------
KIND_COLORS = {
    "water": C.BLUE, "lava": C.RED, "grass": C.GREEN, "sand": C.YELLOW,
    "snow": C.WHITE, "stone": C.DIM, "leaves": C.GREEN, "log": C.YELLOW,
    "dirt": C.YELLOW, "gravel": C.DIM, "other": C.MAGENTA,
}
KIND_CHARS = {
    "water": "~", "lava": "^", "grass": ".", "sand": ":", "snow": "#",
    "stone": "A", "leaves": "&", "log": "T", "dirt": ",", "gravel": ";",
    "other": "?",
}


def ascii_map(world: World, width: int = 78, height: int = 26) -> str:
    """Нарисовать рельеф символами — headless-отладка карты без Qt.

    Пустых ячеек не оставляем: для каждой клетки берём ближайший тайл сетки
    сканирования, иначе карта выглядит «в дырочку» при шаге 8 и крупном зуме.
    """
    snap = world.terrain_snapshot()
    tiles = snap["tiles"]
    if not tiles:
        return f"{C.DIM}(рельеф пуст — сначала выполните сканирование){C.RESET}"
    x0, z0, x1, z1 = snap["bounds"]
    y0, y1 = snap["y_range"]
    step = max(1, snap["step"])
    span_x = max(1, x1 - x0)
    span_z = max(1, z1 - z0)
    span_y = max(1, y1 - y0)

    def sample(cx: int, cz: int):
        wx = x0 + (cx + 0.5) * span_x / width
        wz = z0 + (cz + 0.5) * span_z / height
        bx = int(round(wx / step)) * step
        bz = int(round(wz / step)) * step
        return tiles.get((bx, bz))

    lines = []
    for cz in range(height):
        out = []
        for cx in range(width):
            cell = sample(cx, cz)
            if cell is None:
                out.append(f"{C.DIM} {C.RESET}")
                continue
            y, kind = cell
            shade = (y - y0) / span_y
            bright = ("\033[1m" if shade > 0.66
                      else ("\033[0m" if shade > 0.33 else "\033[2m"))
            color = KIND_COLORS.get(kind, C.MAGENTA)
            out.append(f"{bright}{color}{KIND_CHARS.get(kind, '?')}{C.RESET}")
        lines.append("".join(out))
    legend = "  ".join(f"{KIND_COLORS.get(k, '')}{KIND_CHARS.get(k, '?')}{C.RESET}={k}"
                       for k in ("water", "grass", "sand", "stone", "snow", "other"))
    lines.append(f"{C.DIM}X: {x0}..{x1}   Z: {z0}..{z1}   Y: {y0}..{y1}   "
                 f"шаг: {step}{C.RESET}")
    lines.append(legend)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Подключение
# ---------------------------------------------------------------------------
def build_pool(cfg: AppConfig) -> Tuple[RCONPool, Optional[object]]:
    """Создать пул. В mock-режиме сначала поднимает имитатор сервера."""
    mock = None
    if cfg.rcon.mock:
        from rwf.mock_server import MockMCServer
        mock = MockMCServer(
            host=cfg.rcon.host, port=0 if cfg.rcon.port == 0 else cfg.rcon.port,
            password=cfg.rcon.password, version=cfg.rcon.mock_version,
            flavor=cfg.rcon.mock_flavor, players=cfg.rcon.mock_players,
        ).start()
        cfg.rcon.port = mock.port
        print(ok(f"Имитатор сервера поднят на {cfg.rcon.host}:{mock.port} "
                 f"(версия {cfg.rcon.mock_version}, {cfg.rcon.mock_flavor})"))
    pool = RCONPool(cfg.rcon.host, cfg.rcon.port, cfg.rcon.password,
                    size=cfg.rcon.pool_size, timeout=cfg.rcon.timeout,
                    on_event=lambda cmd, err: print(bad(f"RCON: {err} | {cmd[:60]}")))
    return pool, mock


# ---------------------------------------------------------------------------
#  Команды CLI
# ---------------------------------------------------------------------------
def cmd_selftest(argv: List[str]) -> int:
    import unittest
    loader = unittest.TestLoader()
    suite = loader.discover("tests", top_level_dir=".")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def cmd_probe(cfg: AppConfig) -> int:
    pool, mock = build_pool(cfg)
    try:
        pool.connect_all()
        print(ok("Соединение установлено, пул прогрет."))
        caps = mc.ServerCaps.probe(pool.run, assume_version=cfg.rcon.mock_version)
        print(head("Возможности сервера"))
        print(f"  бренд:          {caps.brand}")
        print(f"  версия:         {caps.version_text} -> {caps.version}")
        print(f"  display-сущности (1.19.4+): {caps.has_display_entities}")
        print(f"  marker (1.17+):             {caps.has_marker}")
        print(f"  interaction:                {caps.has_interaction}")
        print(f"  /ride (1.20.5+):            {caps.has_ride_command}")
        print(f"  /damage (1.19.4+):          {caps.has_damage_command}")
        print(f"  NBT-диалект:                {caps.nbt.name}")
        print(f"  {C.BOLD}backend модели:  {caps.model_backend}{C.RESET}")
        names = mc.parse_player_list(pool.run("list"))
        print(head("Игроки"))
        print("  " + (", ".join(names) if names else f"{C.DIM}(никого){C.RESET}"))
        return 0
    except RCONError as exc:
        print(bad(f"Не удалось подключиться: {exc}"))
        print(warn("Проверьте server.properties: enable-rcon=true, rcon.port, "
                   "rcon.password; либо запустите демо: python main.py --mock"))
        return 2
    finally:
        pool.close()
        if mock:
            mock.stop()


def cmd_watch(cfg: AppConfig, seconds: float) -> int:
    pool, mock = build_pool(cfg)
    world = World()
    try:
        caps = mc.ServerCaps.probe(pool.run, assume_version=cfg.rcon.mock_version)
        print(ok(caps.describe()))
        tracker = PlayerTracker(pool, world, interval=0.5)
        tracker.start()
        print(head(f"Наблюдение {seconds:.0f} с (Ctrl+C — остановить)"))
        end = time.time() + seconds
        last_rev = -1
        while time.time() < end:
            snap = world.snapshot()
            if snap["revision"] != last_rev:
                last_rev = snap["revision"]
                line = " | ".join(
                    f"{C.GREEN}{n}{C.RESET} ({p['pos'][0]:.0f}, {p['pos'][1]:.0f}, "
                    f"{p['pos'][2]:.0f})"
                    for n, p in snap["players"].items())
                print(f"  {line or C.DIM + '(никого)' + C.RESET}")
            time.sleep(0.5)
        tracker.stop()
        print(head("Статистика трекера"))
        for k, v in tracker.stats().items():
            print(f"  {k}: {v}")
        print(head("Статистика RCON"))
        for k, v in pool.stats().items():
            print(f"  {k}: {v}")
        return 0
    finally:
        pool.close()
        if mock:
            mock.stop()


def cmd_scan(cfg: AppConfig, radius: int, step: int,
             center: Optional[Tuple[float, float]] = None) -> int:
    pool, mock = build_pool(cfg)
    world = World()
    try:
        caps = mc.ServerCaps.probe(pool.run, assume_version=cfg.rcon.mock_version)
        print(ok(caps.describe()))
        if center is None:
            tracker = PlayerTracker(pool, world)
            found = tracker.poll_once()
            if found:
                name, pos = next(iter(found.items()))
                center = (pos[0], pos[2])
                print(ok(f"Центр — позиция игрока {name}: {center[0]:.0f}, {center[1]:.0f}"))
            else:
                center = (0.0, 0.0)
                print(warn("Игроков нет, сканирую вокруг (0, 0)"))
        scanner = TerrainScanner(
            pool, world, top=cfg.scanner.top_y, bottom=cfg.scanner.bottom_y,
            coarse=16, batch=cfg.scanner.batch_size,
            max_columns=cfg.scanner.max_columns)
        world.bus.subscribe(TOPIC_SCAN_PROGRESS,
                            lambda d, t: print(f"\r  прогресс: {d}/{t} "
                                               f"({100.0 * d / max(1, t):.0f}%)",
                                               end="", flush=True))
        print(head(f"Сканирование r={radius}, шаг={step}"))
        res = scanner.scan_sync(center, radius, step)
        print("\n" + ok(res.describe()))
        print(head("Карта рельефа"))
        print(ascii_map(world))
        return 0
    finally:
        pool.close()
        if mock:
            mock.stop()


def cmd_bench(cfg: AppConfig) -> int:
    """Сравнить поштучную отправку команд с пакетной (pipelining)."""
    pool, mock = build_pool(cfg)
    try:
        n = 200
        cmds = [f"execute if block {i} 200 0 minecraft:air" for i in range(n)]
        with pool.lease() as conn:
            t0 = time.perf_counter()
            for c in cmds[: n // 4]:
                conn.run(c)
            seq = time.perf_counter() - t0
            t0 = time.perf_counter()
            conn.run_many(cmds)
            piped = time.perf_counter() - t0
        seq_full = seq * 4
        print(head("Пропускная способность RCON"))
        print(f"  поштучно ({n} команд):   {seq_full * 1000:7.1f} мс")
        print(f"  пакетом  ({n} команд):   {piped * 1000:7.1f} мс")
        print(f"  {C.BOLD}ускорение: {seq_full / max(piped, 1e-9):.1f}x{C.RESET}")
        print(C.DIM + "  На удалённом сервере разница растёт пропорционально RTT."
              + C.RESET)
        return 0
    finally:
        pool.close()
        if mock:
            mock.stop()


def cmd_demo(cfg: AppConfig) -> int:
    """Полный офлайн-прогон: подключение -> зонд -> игроки -> рельеф -> очередь."""
    cfg.rcon.mock = True
    pool, mock = build_pool(cfg)
    world = World()
    if not mock:
        print(bad("mock-сервер не поднялся"))
        return 1
    try:
        pool.connect_all()
        print(ok(f"RCON подключён к {cfg.rcon.host}:{cfg.rcon.port} "
                 f"(пул {cfg.rcon.pool_size} соединений)"))

        caps = mc.ServerCaps.probe(pool.run, assume_version=cfg.rcon.mock_version)
        print(ok(caps.describe()))

        print(head("Игроки"))
        tracker = PlayerTracker(pool, world, interval=0.4)
        found = tracker.poll_once()
        for name, pos in found.items():
            print(f"  {C.GREEN}{name:<12}{C.RESET} X={pos[0]:8.1f} "
                  f"Y={pos[1]:6.1f} Z={pos[2]:8.1f}")
        print(f"  {C.DIM}запросов на сервер: 1 list + {len(found)} Pos + "
              f"{len(found)} Rotation = одним пакетом{C.RESET}")

        print(head("Рельеф"))
        scanner = TerrainScanner(pool, world, top=cfg.scanner.top_y,
                                 bottom=cfg.scanner.bottom_y, coarse=16, batch=64)
        center = (found[next(iter(found))][0], found[next(iter(found))][2]) if found else (0.0, 0.0)
        res = scanner.scan_sync(center, radius=64, step=8)
        print("  " + ok(res.describe()))
        print(ascii_map(world))

        print(head("Очередь команд (визуальные кадры сливаются)"))
        q = CommandQueue(pool, workers=1, batch_size=16)
        q.start()
        for i in range(40):
            q.submit(mc.tp_tag("demo_unit", (float(i), 120.0, float(i)), yaw=i * 9.0),
                     Priority.VISUAL, key="model:demo")
        q.flush(timeout=5.0)
        q.stop()
        st = q.stats()
        print(f"  отправлено 40 кадров модели -> на сервер ушло {st['executed']} "
              f"(слито {st['merged']})")
        print(f"  {C.DIM}без слияния это было бы 40 команд tp вместо "
              f"{st['executed']}{C.RESET}")

        print(head("Итог"))
        ps = pool.stats()
        print(f"  команд отправлено: {ps['sent']}, получено ответов: {ps['received']}, "
              f"ошибок: {ps['errors']}")
        print(ok("Тракт ядра работает без Minecraft-сервера."))
        print(warn("Это демо нижнего слоя (сеть, мир, скан, очередь). "
                   "Полный стек с картой и пультом: `python main.py gui`."))
        return 0
    finally:
        pool.close()
        mock.stop()


def cmd_flight(cfg: AppConfig, seconds: float = 20.0) -> int:
    """Сквозное демо: спавн самолёта, полёт по курсам, огонь, деспаун.

    Заодно проверяет ключевое свойство блочной модели — после всего полёта
    в мире не остаётся ни одного лишнего блока.
    """
    from rwf.app import Application
    from rwf.weapons import Target

    cfg.rcon.mock = True
    app = Application(cfg)
    try:
        caps = app.connect()
        print(ok(f"Подключено: {caps.describe()}"))
        print(f"  {C.DIM}модель техники: {cfg.sim.model_backend} (блоки), "
              f"перерисовка {cfg.sim.model_update_hz:.0f} Гц, "
              f"тик {cfg.sim.tick} с{C.RESET}")
        app.tracker.poll_once()
        players = list(app.world.get_players())
        center = app.focus_center()
        print(ok(f"Игроки: {', '.join(players) or '(нет)'}; центр {center[0]:.0f}, {center[1]:.0f}"))

        print(head("Спавн штурмовика"))
        unit = app.spawn("attacker", (center[0], 180.0, center[1]), heading=0.0,
                         speed=30.0, throttle=0.85)
        app.queue.flush(timeout=5.0)
        blocks_after_spawn = len(app.mock.blocks)
        print(f"  #{unit.id} {unit.spec.label}: блоков модели в мире = {blocks_after_spawn}")

        print(head("Полёт по курсам (автопилот)"))
        legs = [(90.0, 4.0), (180.0, 4.0), (270.0, 4.0), (0.0, 4.0)]
        for heading, dur in legs:
            unit.set_target(heading=heading)
            t0 = time.time()
            while time.time() - t0 < dur:
                app.engine.tick_once(cfg.sim.tick)
                time.sleep(cfg.sim.tick)
            t = unit.telemetry()
            print(f"  курс {heading:5.1f}° -> факт {t['heading']:6.1f}°  "
                  f"V={t['speed']:5.1f} м/с  H={t['altitude']:6.1f} м  "
                  f"n={t['g_load']:.2f}  топливо {t['fuel_pct']:.0f}%")
        app.queue.flush(timeout=5.0)
        blocks_in_flight = len(app.mock.blocks)
        # Запоминаем размер модели ДО деспауна: despawn() очищает список
        # поставленных блоков, и сравнивать после него уже не с чем.
        model_size = unit.model.placed_count
        print(f"  блоков в мире после {sum(d for _h, d in legs):.0f} с полёта: "
              f"{blocks_in_flight} (модель = {model_size})")
        if blocks_in_flight != model_size:
            print(bad("СЛЕД ИЗ БЛОКОВ: в мире лишние блоки!"))
        else:
            print(ok("Следа нет: в мире ровно блоки текущей модели"))

        print(head("Огонь"))
        target = None
        if players:
            p = app.world.get_player(players[0])
            if p:
                target = Target(pos=p.pos, name=p.name)
        n_gun = app.engine.fire(unit.id, category="cannon", target=target)
        n_rock = app.engine.fire(unit.id, category="rocket", target=target)
        n_bomb = app.engine.fire(unit.id, category="bomb", target=target)
        app.queue.flush(timeout=5.0)
        # даём бомбе «упасть»: эффекты поражения отложены на время падения
        for _ in range(60):
            app.engine.tick_once(cfg.sim.tick)
        app.queue.flush(timeout=5.0)
        print(f"  подвесов сработало: пушка={n_gun} НАР={n_rock} бомбы={n_bomb}")
        print(f"  статистика оружия: {app.weapons.stats()}")
        print(f"  боезапас: {unit.ammo_total}/{unit.ammo_max}")

        print(head("Деспаун"))
        app.engine.despawn(unit.id)
        app.queue.flush(timeout=5.0)
        left = len(app.mock.blocks)
        print(f"  блоков в мире после снятия юнита: {left}")
        if left == 0:
            print(ok("Мир чист: модель убрана полностью, ландшафт не тронут"))
        else:
            print(bad(f"Осталось {left} блоков — модель убирается не полностью"))

        print(head("Команды, ушедшие на сервер"))
        total = len(app.mock.command_log)
        kinds = {}
        for c in app.mock.command_log:
            kinds[c.split()[0]] = kinds.get(c.split()[0], 0) + 1
        top = sorted(kinds.items(), key=lambda kv: -kv[1])[:8]
        print(f"  всего {total}: " + ", ".join(f"{k}={v}" for k, v in top))
        print(ok("Сквозной прогон завершён"))
        return 0 if (left == 0 and blocks_in_flight == model_size) else 4
    finally:
        app.shutdown()


def cmd_mission(cfg: AppConfig, seconds: float = 45.0) -> int:
    """Демо итерации 3: маршруты, ИИ по фазам, библиотека маршрутов.

    В воздухе одновременно:
      * самолёт игрока по сохранённому маршруту из JSON;
      * бот-штурмовик со своим ИИ (заходы по фазам);
      * бот-перехватчик;
      * дрон-камикадзе.
    """
    from rwf.ai import make_ai
    from rwf.app import Application
    from rwf.routes import Route
    from rwf.storage import RouteLibrary, build_demo_routes

    cfg.rcon.mock = True
    app = Application(cfg)
    try:
        app.connect()
        app.tracker.poll_once()
        players = list(app.world.get_players())
        victim = players[0] if players else ""
        center = app.focus_center()
        print(ok(f"Сервер: {app.caps.describe()}"))
        print(ok(f"Цель: {victim or '(нет)'}  центр: {center[0]:.0f}, {center[1]:.0f}"))

        # --- библиотека маршрутов ---------------------------------------
        print(head("Библиотека маршрутов (JSON)"))
        lib = RouteLibrary(Path("routes"))
        for name, route in build_demo_routes().items():
            path = lib.save(route)
            print(f"  сохранён: {name:<28s} -> {path.name}")
        loaded = lib.load("Удар и отход")
        print(f"  загружен: {loaded.name!r}, точек {len(loaded)}, "
              f"цикл {loaded.loop}")
        for info in lib.list():
            acts = ", ".join(f"{k}×{v}" for k, v in sorted(info.actions.items()))
            print(f"    · {info.name:<26s} {info.waypoints} точек  [{acts}]")

        # --- база для возврата ------------------------------------------
        base = (center[0] - 700.0, center[1])
        app.world.set_base(*base)
        print(ok(f"База задана: ({base[0]:.0f}, {base[1]:.0f})"))

        # --- юниты -------------------------------------------------------
        print(head("Поднимаем группу"))
        player_unit = app.spawn("attacker", (base[0], 170.0, base[1]), heading=90.0,
                                speed=35.0, throttle=0.9)
        engine = app.engine
        engine.assign_route(player_unit.id, loaded)
        print(f"  #{player_unit.id} {player_unit.spec.label} — маршрут "
              f"{loaded.name!r} ({len(loaded)} точек)")

        bots = []
        if victim:
            b1 = app.spawn("attacker", (base[0], 180.0, base[1] + 60.0),
                           heading=90.0, speed=35.0, throttle=0.9, is_bot=True)
            engine.set_ai(b1.id, make_ai("strike", cfg=cfg, target_name=victim))
            bots.append(("штурмовик", b1))
            b2 = app.spawn("fighter", (base[0], 260.0, base[1] - 60.0),
                           heading=90.0, speed=55.0, throttle=1.0, is_bot=True)
            engine.set_ai(b2.id, make_ai("fighter", cfg=cfg, target_name=victim))
            bots.append(("перехватчик", b2))
            b3 = app.spawn("kamikaze_drone", (base[0], 140.0, base[1] + 120.0),
                           heading=90.0, speed=30.0, throttle=1.0, is_bot=True)
            engine.set_ai(b3.id, make_ai("kamikaze", cfg=cfg, target_name=victim))
            bots.append(("камикадзе", b3))
        for label, bot in bots:
            print(f"  #{bot.id} {bot.spec.label:<10s} — ИИ «{label}»")

        # --- полёт -------------------------------------------------------
        print(head(f"Полёт {seconds:.0f} с"))
        engine.start()
        t0, next_report = time.time(), 0.0
        while time.time() - t0 < seconds:
            elapsed = time.time() - t0
            if elapsed >= next_report:
                next_report += 5.0
                parts = []
                for label, bot in bots:
                    st = engine.ai_status(bot.id)
                    if not bot.alive:
                        parts.append(f"{label}:{bot.crashed_reason or 'потерян'}")
                    elif st:
                        parts.append(f"{label}:{st['phase_label']}")
                rs = engine.route_status(player_unit.id)
                cur = rs.get('current')
                cur_disp = "-" if cur is None else min(cur + 1, rs.get('points', 1))
                line = (f"  t={elapsed:5.1f}с  игрок: точка "
                        f"{cur_disp} из {rs.get('points', '-')} "
                        f"[{rs.get('state', '-')}]  |  " + " ".join(parts))
                print(line)
            time.sleep(0.2)
        engine.stop(join_timeout=2.0)
        app.queue.flush(timeout=8.0)

        # --- итоги -------------------------------------------------------
        print(head("Итоги"))
        st = engine.stats_snapshot()
        print(f"  юнитов в воздухе: {st['units']}   тиков: {st['engine']['ticks']}")
        print(f"  выстрелов: {st['weapons']['shots']}   "
              f"боезапаса израсходовано: {st['weapons']['ammo_spent']}")
        print(f"  УР пущено: {st['weapons']['missiles_launched']}   "
              f"попаданий: {st['weapons']['missiles_hit']}")
        print(f"  аварии/потери: {st['engine']['crashes']}")
        rs = engine.route_status(player_unit.id)
        print(f"  маршрут игрока: {rs.get('points', '-') - rs.get('current', 0)}"
              f" осталось, точка {min(rs.get('current', 0) + 1, rs.get('points', 1))}"
              f"/{rs.get('points', '-')}"
              f"  завершён: {rs.get('done', '-')}")
        print(f"  команд на сервер: {len(app.mock.command_log)}, "
              f"блоков в мире: {len(app.mock.blocks)}")
        marks = {}
        for m in app.world.markers:
            marks[m.kind] = marks.get(m.kind, 0) + 1
        if marks:
            print(f"  метки на карте: {marks}")
        print(ok("Миссия завершена"))
        return 0
    finally:
        app.shutdown()


def cmd_gui(cfg: AppConfig) -> int:
    try:
        from rwf.ui import run as run_ui      # type: ignore
    except ImportError:
        print(bad("Не удалось импортировать rwf.ui (нужен dearpygui)."))
        print(warn("Готово ядро: rcon, mc, world, tracker, scanner, events, config."))
        print("Проверить его можно так:  python main.py --mock")
        print("План и статус — в README.md, список дефектов наброска — в BUGS.md.")
        return 3
    # UI-23: стартовое окно настройки (Tkinter) УДАЛЕНО. Диспетчерская — это и
    # есть стартовый экран: подключение, пресеты и настройки живут внутри неё,
    # поэтому отдельный диалог только добавлял шаг перед работой.
    from rwf.settings import Settings
    settings = Settings.load()
    if settings.load_error:
        print(warn(f"Настройки: {settings.load_error}"))
    return run_ui(cfg, settings=settings)


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rwf",
        description="RCON Warfare — диспетчерская боевой техники для Minecraft через RCON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    p.add_argument("--version", action="version", version=f"RCON Warfare {__version__}")
    p.add_argument("--host", help="адрес сервера (по умолчанию из rwf.json)")
    p.add_argument("--port", type=int, help="порт RCON")
    p.add_argument("--password", help="пароль RCON")
    p.add_argument("--config", help="путь к файлу конфигурации")
    p.add_argument("--mock", action="store_true",
                   help="использовать встроенный имитатор сервера")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("demo", help="офлайн-демо всего тракта (по умолчанию)")
    sub.add_parser("selftest", help="прогнать тесты")
    sub.add_parser("probe", help="опросить возможности сервера")
    sp = sub.add_parser("watch", help="следить за игроками")
    sp.add_argument("seconds", nargs="?", type=float, default=10.0)
    sp = sub.add_parser("scan", help="сканировать рельеф")
    sp.add_argument("radius", nargs="?", type=int, default=128)
    sp.add_argument("step", nargs="?", type=int, default=8)
    sp.add_argument("--x", type=float, default=None)
    sp.add_argument("--z", type=float, default=None)
    sp = sub.add_parser("flight", help="демо: полёт, огонь, деспаун")
    sp.add_argument("seconds", nargs="?", type=float, default=20.0)
    sp = sub.add_parser("mission", help="демо: маршруты, ИИ, библиотека")
    sp.add_argument("seconds", nargs="?", type=float, default=45.0)
    sub.add_parser("bench", help="сравнить пакетную и поштучную отправку")
    sp = sub.add_parser("gui", help="запустить диспетчерскую (Dear PyGui)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Удобство: `--selftest`/`--scan` работают как команды-подкоманды.
    alias = {"--selftest": "selftest", "--probe": "probe", "--watch": "watch",
             "--scan": "scan", "--bench": "bench", "--gui": "gui", "--demo": "demo",
             "--flight": "flight", "--mission": "mission"}
    argv = [alias[a] if a in alias else a for a in argv]

    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    if not args.verbose:
        # Демо печатает собственный отчёт — логи ядра прячем, чтобы не мешали.
        logging.getLogger().setLevel(logging.WARNING)

    cfg = AppConfig.load(args.config)
    if args.host:
        cfg.rcon.host = args.host
    if args.port:
        cfg.rcon.port = args.port
    if args.password:
        cfg.rcon.password = args.password
    if args.mock:
        cfg.rcon.mock = True

    print(f"{C.BOLD}{C.CYAN}RCON Warfare v{__version__}{C.RESET} — "
          f"диспетчерская авиаударов для Minecraft")

    cmd = args.command or "gui"
    if cmd == "selftest":
        return cmd_selftest(argv)
    if cmd == "probe":
        return cmd_probe(cfg)
    if cmd == "watch":
        return cmd_watch(cfg, args.seconds)
    if cmd == "scan":
        center = (args.x, args.z) if args.x is not None and args.z is not None else None
        return cmd_scan(cfg, args.radius, args.step, center)
    if cmd == "bench":
        return cmd_bench(cfg)
    if cmd == "flight":
        return cmd_flight(cfg, args.seconds)
    if cmd == "mission":
        return cmd_mission(cfg, args.seconds)
    if cmd == "gui":
        return cmd_gui(cfg)
    return cmd_demo(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{warn('Прервано пользователем.')}")
        sys.exit(130)
