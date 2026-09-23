#!/usr/bin/env python3
"""
Headless-скриншот интерфейса RCON Warfare (Dear PyGui под Xvfb/офскрин).

Ставит демонстрацию на имитаторе сервера: три базы (аэродром, плывущий
авианосец, гарнизон), скан рельефа, запуски с баз, зона удара, черновик
маршрута с действиями, бот-истребитель, захват ПЗРК — и снимает кадр.

    python3 tools/screenshot.py [путь.png] [--frames N]

Нужен запущенный X-сервер (в песочнице: `Xvfb :99 &` + DISPLAY=:99).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rwf.config import AppConfig          # noqa: E402
from rwf.ui import state as S              # noqa: E402
from rwf.ui.app import run                 # noqa: E402


class Demo:
    """Пошаговая постановка: по одному шагу на готовность предыдущего."""

    def __init__(self):
        self.phase = 0
        self.uids: list = []

    def __call__(self, frames: int, state: Dict[str, Any], facade) -> None:
        if not state["connection"]["connected"]:
            return
        bases = {b["name"]: b for b in state["bases"]}
        units = (state["frame"].get("units") or {})

        if self.phase == 0:
            self.phase = 1
            facade.send("add_base", "Аэродром Северный", "airport",
                        -260.0, -180.0, 90.0)
            facade.send("add_base", "Авианосец «Адмирал»", "carrier",
                        40.0, 120.0, 270.0)
            facade.send("add_base", "Гарнизон", "ground", 320.0, -240.0, 0.0)
            facade.send("scan", -60.0, 0.0, 300, 8)
            return

        if self.phase == 1 and len(bases) >= 3:
            self.phase = 2
            air = bases["Аэродром Северный"]["id"]
            car = bases["Авианосец «Адмирал»"]["id"]
            facade.send("launch_from_base", air, "bomber", 190.0, False, {})
            facade.send("launch_from_base", air, "attacker", 170.0, False, {})
            facade.send("launch_from_base", car, "fighter", 180.0, True, {})
            facade.send("launch_from_base", car, "attack_heli", 66.0, True, {})
            gar = bases["Гарнизон"]["id"]
            facade.send("launch_from_base", air, "transport", 210.0,
                        False, {})
            facade.send("launch_from_base", gar, "truck", 0.0, False, {})
            facade.send("launch_from_base", gar, "apc", 0.0, True, {})
            facade.send("move_base", car, -160.0, 260.0)
            facade.send("set_strike_zone", -60.0, -40.0, 130.0, 70.0)
            S.planner_add(state, -190.0, -110.0, 160.0, "navigate")
            S.planner_add(state, -50.0, -10.0, 120.0, "bomb")
            S.planner_add(state, 80.0, 30.0, 140.0, "strafe")
            S.planner_add(state, 210.0, 90.0, 150.0, "navigate")
            return

        if self.phase == 2 and len(units) >= 7:
            # след боя: сбиваем машину ДО паузы, чтобы тик движка успел
            # создать место аварии (обломки, кратер, огонь) — на паузе
            # tick_once не крутится и след не появился бы (WRECK-01…)
            if not getattr(self, "killed", False):
                self.killed = True
                self.hold = 14
                bots0 = sorted(u for u in units
                               if units[u].get("is_bot")
                               and units[u].get("kind") in
                               ("tank", "truck", "apc"))
                app0 = facade.app
                victim = (bots0 or sorted(units))[-1]
                if app0 is not None:
                    unit = app0.engine.get(victim)
                    if unit is not None:
                        unit.take_damage(unit.health * 3.0, "демо-показ",
                                         None, app0.world)
                self.phase = 25        # ждём, пока тик создаст след аварии
                return
        if self.phase == 25:
            self.hold = getattr(self, "hold", 14) - 1
            if self.hold > 0:
                return
            self.phase = 3
            self.uids = sorted(units)
            # статичная композиция: пауза техники, камера между базами
            facade.send("pause_all_units", True)
            facade.renderer.transform.follow = False
            facade.renderer.transform.set_center(0.0, 40.0)
            facade.renderer.transform.set_view_radius(460.0)
            uid = self.uids[0]
            state["selection"] = uid
            facade.send("select", uid)
            pts = [{"x": p["x"], "z": p["z"], "alt": p["alt"],
                    "action": p["action"]}
                   for p in state["planner"]["points"]]
            facade.send("assign_route_points", uid, pts)
            bots = [u for u in self.uids if units[u].get("is_bot")]
            if bots:
                facade.send("set_ai", bots[0], "fighter", "")
            # наземная колонна: короткий маршрут от гарнизона
            ground = [u for u in self.uids
                      if units[u].get("kind") in ("truck", "apc", "tank")]
            if ground:
                facade.send("assign_route_points", ground[0], [
                    {"x": 240.0, "z": -180.0, "alt": 0.0,
                     "action": "navigate"},
                    {"x": 140.0, "z": -60.0, "alt": 0.0,
                     "action": "navigate"}])
            facade.send("library_names")
            facade.renderer.transform.set_center(0.0, 40.0)
            facade.renderer.transform.set_view_radius(460.0)
            from rwf.ui import build as B
            B.set_section_open("conn", False)      # свернуть «Связь» для кадра
            return

        if self.phase == 3 and frames > 240:
            # захват ПЗРК игроком (демонстрация дока «Игроки и ПЗРК»)
            self.phase = 4
            app = facade.app
            if app is not None and app.manpads is not None:
                players = app.world.get_players()
                bots = [u for u in app.world.iter_units()
                        if getattr(u, "is_bot", False)
                        and u.spec.kind in ("aircraft", "helicopter", "drone")]
                if players and bots:
                    name = next(iter(players))
                    bot = bots[0]
                    rec = dict(players[name])
                    # наводим взгляд игрока на бота
                    import math
                    dx = bot.pos[0] - rec["pos"][0]
                    dy = bot.pos[1] - rec["pos"][1]
                    dz = bot.pos[2] - rec["pos"][2]
                    horiz = math.hypot(dx, dz) or 1e-6
                    rec["yaw"] = math.degrees(math.atan2(-dx, dz)) % 360.0
                    rec["pitch"] = math.degrees(math.atan2(-dy, horiz))
                    app.manpads.try_lock(name, rec)
            state["follow"] = False
            facade.renderer.transform.set_center(0.0, 40.0)
            facade.renderer.transform.set_view_radius(460.0)
            # контекстное меню ПКМ в кадре: демонстрация UX-14
            ctx = state.get("ctx")
            if ctx is not None and not ctx.open and self.uids:
                items = ctx.items_for(self.uids[0], None, None, 0.0, 0.0)
                ctx.open_at(600, 250, items)
            return


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", nargs="?", default="artifacts/ui_dpg.png")
    ap.add_argument("--frames", type=int, default=620)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    cfg = AppConfig()
    cfg.rcon.mock = True
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    demo = Demo()
    rc = run(cfg, headless_frames=args.frames, screenshot=args.out,
             autoconnect=True, on_frame=demo)
    print(f"фаза демо: {demo.phase}, кадр сохранён: {args.out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
