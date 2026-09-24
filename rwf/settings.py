"""
Пользовательские настройки RCON Warfare (SET-01…SET-04, BUGS.md раздел H).

Хранятся одним JSON-файлом (путь: `$RWF_SETTINGS` или
`~/.config/rwf/settings.json`), читаются ДО старта UI и tkinter-диалога и
сохраняются при выходе. Структура плоская по секциям:

    {
      "connection": {"host", "port", "password", "mock"},
      "sound":      {"enabled", "volume", "events": {имя: bool}},
      "ui":         {"right_w", "tele_h", "hotbar": bool, ...}
    }

Правила:
* неизвестные ключи из файла сохраняются как есть (forward-compat);
* отсутствующие ключи дополняются из DEFAULTS (merge recursивный);
* ошибки чтения/записи НЕ фатальны: настройки degrade до дефолтов,
  в журнал UI кладётся предупреждение (SET-03).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "connection": {
        "host": "127.0.0.1",
        "port": 25575,
        "password": "",
        "mock": True,
    },
    "sound": {
        "enabled": True,
        "volume": 0.7,                    # 0.0..1.0
        "events": {
            "ui": True,                   # клики интерфейса
            "fire": True,                 # выстрелы/пуски
            "explosion": True,            # попадания, уничтожение
            "spawn": True,                # запуск техники
            "landing": True,              # посадка/взлёт/обслуживание
            "lock": True,                 # захват ПЗРК
            "alarm": True,                # аварии: топливо, повреждения
        },
    },
    "ui": {
        "right_w": 444,
        "tele_h": 252,
        "hotbar": True,                   # левая панель инструментов видима
        "icon_scale": 1.0,
        "inertia": True,                  # UX-02: инерция камеры карты
        "scale": 1.0,                     # UX-03: масштаб UI (Ctrl+колесо)
    },
}


def default_path() -> Path:
    env = os.environ.get("RWF_SETTINGS")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    return home / ".config" / "rwf" / "settings.json"


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Дополнить `base` значениями из `extra` (рекурсивно по словарям)."""
    out = deepcopy(base)
    for key, val in (extra or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], val)
        else:
            out[key] = val
    return out


class Settings:
    """Потокбезопасное хранилище настроек с точечным доступом."""

    def __init__(self, path: Optional[Path] = None,
                 data: Optional[Dict[str, Any]] = None):
        self._lock = threading.RLock()
        self.path = Path(path) if path else default_path()
        self._data = deepcopy(DEFAULTS)
        self.load_error = ""
        if data:
            self._data = _merge(self._data, data)

    # ------------------------------------------------------------ чтение
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        s = cls(path=path)
        p = s.path
        try:
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    s._data = _merge(DEFAULTS, raw)
                else:
                    s.load_error = f"{p}: ожидался JSON-объект"
        except Exception as exc:  # noqa: BLE001 - деградация до дефолтов
            s.load_error = f"{p}: {exc}"
            log.warning("Настройки: %s", s.load_error)
        return s

    def dump(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return deepcopy(node)

    def set(self, dotted: str, value: Any) -> None:
        with self._lock:
            parts = dotted.split(".")
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    return
            node[parts[-1]] = value

    # ------------------------------------------------------------ запись
    def save(self, path: Optional[Path] = None) -> bool:
        p = Path(path) if path else self.path
        with self._lock:
            data = deepcopy(self._data)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось сохранить настройки %s: %s", p, exc)
            return False

    # ------------------------------------------------------------ вырезы
    @property
    def sound_enabled(self) -> bool:
        return bool(self.get("sound.enabled", True))

    def sound_event_enabled(self, event: str) -> bool:
        return bool(self.get(f"sound.events.{event}", True))

    @property
    def volume(self) -> float:
        try:
            return max(0.0, min(1.0, float(self.get("sound.volume", 0.7))))
        except (TypeError, ValueError):
            return 0.7
