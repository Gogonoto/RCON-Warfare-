"""
Стартовое окно настройки подключения на Tkinter (UX-15, v14).

Запускается ДО основного интерфейса из `main.py gui`: оператор задаёт
адрес/порт/пароль RCON (или имитатор), звук и громкость — и только потом
стартует Dear PyGui-диспетчерская с этими параметрами. Значения
предзаполняются из `settings.json` и сохраняются обратно (SET-02).

Без DISPLAY окно не открывается: возвращаются значения из настроек,
чтобы headless-сценарии (скриншоты, CI) не блокировались (UX-16).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

BG = "#141815"
FG = "#d3e0d3"
DIM = "#7e8e7e"
ACCENT = "#70d686"
ENTRY_BG = "#1b211b"

ICON_PATH = os.path.join(os.path.dirname(__file__), "assets", "icon.png")


def ask_connection(settings, force: bool = True) -> Optional[Dict[str, Any]]:
    """Показать диалог; вернуть словарь настроек запуска или None (отмена).

    `force=False` — не показывать окно вовсе (headless/скриншоты).
    """
    if not force:
        return _from_settings(settings)
    if not os.environ.get("DISPLAY") and os.name != "nt":
        log.warning("Нет DISPLAY: диалог настройки пропущен")
        return _from_settings(settings)
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # noqa: BLE001
        log.warning("Tkinter недоступен (%s): значения из настроек", exc)
        return _from_settings(settings)

    result: Dict[str, Any] = {}

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось открыть Tkinter-окно (%s)", exc)
        return _from_settings(settings)

    root.title("RCON Warfare — подключение и запуск")
    root.geometry("470x432")
    root.resizable(False, False)
    root.configure(bg=BG)
    try:
        icon = tk.PhotoImage(file=ICON_PATH)
        root.iconphoto(True, icon)
    except Exception:  # noqa: BLE001 - иконка не обязательна
        icon = None

    style = ttk.Style()
    try:
        style.theme_use("clam")
        style.configure("TCheckbutton", background=BG, foreground=FG,
                        font=("DejaVu Sans Mono", 10))
        style.configure("TButton", font=("DejaVu Sans Mono", 10))
        style.configure("TScale", background=BG)
        style.configure("TLabel", background=BG, foreground=FG,
                        font=("DejaVu Sans Mono", 10))
    except Exception:  # noqa: BLE001
        pass

    conn = settings.get("connection", {}) or {}

    tk.Label(root, text="RCON WARFARE", bg=BG, fg=ACCENT,
             font=("DejaVu Sans Mono", 15, "bold")).pack(pady=(16, 2))
    tk.Label(root, text="тактический слой диспетчерской боевой техники",
             bg=BG, fg=DIM, font=("DejaVu Sans Mono", 9)).pack(pady=(0, 10))

    form = tk.Frame(root, bg=BG)
    form.pack(fill="x", padx=28)

    def row(label: str, widget) -> None:
        tk.Label(form, text=label, bg=BG, fg=DIM, width=9, anchor="w",
                 font=("DejaVu Sans Mono", 10)).grid(
            column=0, row=form.grid_size()[1], sticky="w", pady=3)
        widget.grid(column=1, row=form.grid_size()[1] - 1, sticky="we",
                    pady=3, padx=(4, 0))
        form.columnconfigure(1, weight=1)

    var_host = tk.StringVar(value=str(conn.get("host", "127.0.0.1")))
    var_port = tk.StringVar(value=str(conn.get("port", 25575)))
    var_pass = tk.StringVar(value=str(conn.get("password", "")))
    e_host = tk.Entry(form, textvariable=var_host, bg=ENTRY_BG, fg=FG,
                      insertbackground=FG, relief="flat",
                      font=("DejaVu Sans Mono", 10))
    e_port = tk.Entry(form, textvariable=var_port, bg=ENTRY_BG, fg=FG,
                      insertbackground=FG, relief="flat", width=8,
                      font=("DejaVu Sans Mono", 10))
    e_pass = tk.Entry(form, textvariable=var_pass, bg=ENTRY_BG, fg=FG,
                      insertbackground=FG, relief="flat", show="*",
                      font=("DejaVu Sans Mono", 10))
    row("Хост", e_host)
    row("Порт", e_port)
    row("Пароль", e_pass)

    var_mock = tk.BooleanVar(value=bool(conn.get("mock", True)))
    chk = ttk.Checkbutton(form, text="Имитатор сервера (без Minecraft)",
                          variable=var_mock)
    chk.grid(column=1, row=form.grid_size()[1], sticky="w", pady=3)

    sep = tk.Frame(root, bg="#2c3a2e", height=1)
    sep.pack(fill="x", padx=28, pady=12)

    snd = tk.Frame(root, bg=BG)
    snd.pack(fill="x", padx=28)
    var_sound = tk.BooleanVar(value=bool(settings.get("sound.enabled", True)))
    ttk.Checkbutton(snd, text="Звук событий", variable=var_sound).pack(
        anchor="w")
    vol_frame = tk.Frame(snd, bg=BG)
    vol_frame.pack(fill="x", pady=(2, 0))
    tk.Label(vol_frame, text="Громкость", bg=BG, fg=DIM,
             font=("DejaVu Sans Mono", 10)).pack(side="left")
    var_vol = tk.DoubleVar(value=float(settings.get("sound.volume", 0.7)) * 100)
    ttk.Scale(vol_frame, from_=0, to=100, variable=var_vol,
              length=220).pack(side="left", padx=8)

    btns = tk.Frame(root, bg=BG)
    btns.pack(pady=18)

    def ok() -> None:
        try:
            port = int(var_port.get() or 25575)
        except ValueError:
            port = 25575
        result.update(host=var_host.get().strip() or "127.0.0.1",
                      port=port,
                      password=var_pass.get(),
                      mock=bool(var_mock.get()),
                      sound=bool(var_sound.get()),
                      volume=max(0.0, min(1.0, var_vol.get() / 100.0)))
        root.destroy()

    def cancel() -> None:
        result.clear()
        root.destroy()

    b_ok = tk.Button(btns, text="  ЗАПУСТИТЬ ДИСПЕТЧЕРСКУЮ  ", command=ok,
                     bg="#1e2a20", fg=ACCENT, activebackground="#2a3a2c",
                     activeforeground=ACCENT, relief="flat", bd=1,
                     font=("DejaVu Sans Mono", 10, "bold"))
    b_ok.pack(side="left", padx=(0, 10), ipady=4)
    tk.Button(btns, text="Отмена", command=cancel, bg="#241d1d",
              fg="#e2685e", activebackground="#332424", relief="flat",
              bd=1, font=("DejaVu Sans Mono", 10)).pack(side="left", ipady=4)
    root.bind("<Return>", lambda e: ok())
    root.bind("<Escape>", lambda e: cancel())
    root.protocol("WM_DELETE_WINDOW", cancel)

    shot = os.environ.get("RWF_DIALOG_SHOT")
    if shot:                      # служебный режим: снимок окна для ревью
        try:
            root.update_idletasks()
            root.update()
            from PIL import ImageGrab
            img = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY"))
            x, y = root.winfo_rootx(), root.winfo_rooty()
            w, h = root.winfo_width(), root.winfo_height()
            img.crop((x, y, x + w, y + h)).save(shot)
        except Exception as exc:  # noqa: BLE001
            log.warning("Снимок диалога не сделан: %s", exc)
        ok()
        return result

    root.mainloop()

    if not result:
        return None
    return result


def _from_settings(settings) -> Dict[str, Any]:
    conn = settings.get("connection", {}) or {}
    return {
        "host": str(conn.get("host", "127.0.0.1")),
        "port": int(conn.get("port", 25575)),
        "password": str(conn.get("password", "")),
        "mock": bool(conn.get("mock", True)),
        "sound": bool(settings.get("sound.enabled", True)),
        "volume": float(settings.get("sound.volume", 0.7)),
    }


def apply_to(settings, res: Dict[str, Any]) -> None:
    """Прописать результат диалога в настройки и AppConfig-вырез."""
    settings.set("connection.host", res["host"])
    settings.set("connection.port", res["port"])
    settings.set("connection.password", res["password"])
    settings.set("connection.mock", res["mock"])
    settings.set("sound.enabled", res["sound"])
    settings.set("sound.volume", res["volume"])
