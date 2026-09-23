#!/usr/bin/env python3
"""
Кадр модальных окон v15: редактор пресетов (пол-экрана) и диалог новой базы.

    DISPLAY=:98 python3 tools/screenshot_modals.py artifacts/name.png

Ставит демонстрацию как tools/screenshot.py, затем открывает редактор
пресетов (с загруженным пресетом и палитрой оружия) и оставляет его в кадре.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rwf.config import AppConfig          # noqa: E402
from rwf.ui import build as B             # noqa: E402
from rwf.ui.app import run                # noqa: E402


class Demo:
    def __init__(self):
        self.phase = 0

    def __call__(self, frames: int, state: Dict[str, Any], facade) -> None:
        if not state["connection"]["connected"]:
            return
        if self.phase == 0:
            self.phase = 1
            facade.send("add_base", "Аэродром Северный", "airport",
                        -260.0, -180.0, 90.0)
            facade.send("add_base", "Авианосец «Адмирал»", "carrier",
                        40.0, 120.0, 270.0)
            return
        if self.phase == 1 and frames > 60:
            self.phase = 2
            names = facade.preset_names("attacker")
            B.open_preset_editor(facade, preset_name=(names[0] if names
                                                      else None),
                                 variant="attacker")
            return
        if self.phase == 2 and frames > 140:
            self.phase = 3
            B.set_section_open("conn", False)
            return


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", nargs="?", default="artifacts/ui_modals.png")
    ap.add_argument("--frames", type=int, default=260)
    args = ap.parse_args(argv)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig()
    cfg.rcon.mock = True
    rc = run(cfg, headless_frames=args.frames, screenshot=args.out,
             autoconnect=True, on_frame=Demo())
    print(f"кадр сохранён: {args.out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
