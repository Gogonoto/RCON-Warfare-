"""
Хранилище маршрутов: сохранение и загрузка в JSON.

Закрывает ROUTE-13. Маршруты лежат отдельными файлами в каталоге (по умолчанию
`routes/`), поэтому их можно править руками, класть в git и передавать другим.

Формат файла
------------
    {
      "format": "rwf.route",
      "version": 1,
      "name": "Удар по базе",
      "loop": false,
      "owner_kind": "player",
      "waypoints": [ {x, z, altitude, action, count, duration, ...}, ... ]
    }

Состояние исполнения (`reached`, `action_done`, `skipped`) не сохраняется —
маршрут загружается «чистым».

Имена файлов нормализуются, выход за пределы каталога (`../../etc/passwd`)
невозможен: `sanitize_name()` оставляет только безопасные символы.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .routes import Action, Route, Waypoint

log = logging.getLogger(__name__)

FORMAT_ID = "rwf.route"
FORMAT_VERSION = 1

#: допустимые символы имени маршрута (кириллица, латиница, цифры, пробел, _ - .)
_NAME_OK = re.compile(r"[^0-9A-Za-zА-Яа-яЁё _\-.]+")


def sanitize_name(name: str, max_len: int = 64) -> str:
    """Безопасное имя файла из произвольной строки.

    Убирает разделители путей (выход за пределы каталога невозможен),
    схлопывает повторяющиеся точки и служебные символы, ограничивает длину.
    """
    cleaned = (name or "").strip().replace("/", "_").replace("\\", "_")
    cleaned = _NAME_OK.sub("_", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)      # «....» -> «.»
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return (cleaned or "route")[:max_len]


@dataclass
class RouteInfo:
    """Краткое описание сохранённого маршрута для списка в UI."""
    name: str
    filename: str
    waypoints: int
    loop: bool
    owner_kind: str
    modified: float
    size: int
    actions: Dict[str, int]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "filename": self.filename,
            "waypoints": self.waypoints, "loop": self.loop,
            "owner_kind": self.owner_kind, "modified": self.modified,
            "size": self.size, "actions": self.actions,
        }


class RouteLibrary:
    """Каталог сохранённых маршрутов."""

    def __init__(self, directory: Path | str = "routes"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ путь
    def path_for(self, name: str) -> Path:
        return self.dir / f"{sanitize_name(name)}.json"

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    # ------------------------------------------------------- сохранение
    def save(self, route: Route, name: Optional[str] = None) -> Path:
        """Сохранить маршрут. `name` переопределяет имя из маршрута."""
        target = name or route.name or f"route_{int(time.time())}"
        if name and not route.name:
            route.name = name
        data = route.to_dict()
        data["name"] = target
        path = self.path_for(target)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        log.info("Маршрут %r сохранён в %s (%d точек)", target, path, len(route))
        return path

    def load(self, name: str) -> Route:
        """Загрузить маршрут по имени (с расширением или без)."""
        path = Path(name) if Path(name).suffix == ".json" else self.path_for(name)
        if not path.exists():
            raise FileNotFoundError(
                f"Маршрут {name!r} не найден. Доступны: "
                f"{', '.join(self.names()) or '—'}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Файл {path.name} повреждён: {exc}") from exc
        route = Route.from_dict(data)
        if not route.name:
            route.name = path.stem
        route.restart()
        return route

    def delete(self, name: str) -> bool:
        path = Path(name) if Path(name).suffix == ".json" else self.path_for(name)
        if path.exists():
            path.unlink()
            return True
        return False

    # ---------------------------------------------------------- перечисление
    def names(self) -> List[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def list(self) -> List[RouteInfo]:
        out: List[RouteInfo] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                info = self.info(path.stem)
            except (ValueError, OSError, KeyError) as exc:
                log.warning("Не удалось прочитать %s: %s", path.name, exc)
                continue
            if info is not None:
                out.append(info)
        return out

    def info(self, name: str) -> Optional[RouteInfo]:
        path = Path(name) if Path(name).suffix == ".json" else self.path_for(name)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        wps = data.get("waypoints", [])
        actions: Dict[str, int] = {}
        for wp in wps:
            act = wp.get("action", Action.NAVIGATE)
            actions[act] = actions.get(act, 0) + 1
        stat = path.stat()
        return RouteInfo(
            name=data.get("name") or path.stem,
            filename=path.name, waypoints=len(wps),
            loop=bool(data.get("loop", False)),
            owner_kind=data.get("owner_kind", "player"),
            modified=stat.st_mtime, size=stat.st_size, actions=actions)

    # ------------------------------------------------------------ пакетно
    def export_all(self) -> Dict[str, Any]:
        """Все маршруты одним словарём — для резервной копии."""
        return {
            "format": "rwf.route-library",
            "version": FORMAT_VERSION,
            "exported": time.time(),
            "routes": [json.loads(self.path_for(n).read_text(encoding="utf-8"))
                       for n in self.names()],
        }

    def import_all(self, data: Dict[str, Any], overwrite: bool = False) -> int:
        """Импорт резервной копии. Возвращает число загруженных маршрутов."""
        if data.get("format") != "rwf.route-library":
            raise ValueError("Это не резервная копия библиотеки маршрутов")
        count = 0
        for item in data.get("routes", []):
            name = sanitize_name(item.get("name") or "route")
            if not overwrite and self.exists(name):
                continue
            self.path_for(name).write_text(
                json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
            count += 1
        return count


def build_demo_routes() -> Dict[str, Route]:
    """Готовые маршруты-примеры: их удобно сохранить и посмотреть в деле."""
    strike = Route([
        Waypoint(0.0, -200.0, 160.0, Action.NAVIGATE, note="рубеж захода"),
        Waypoint(0.0, 0.0, 90.0, Action.BOMB, count=2, target_name="",
                 note="сброс по цели"),
        Waypoint(0.0, 260.0, 210.0, Action.NAVIGATE, note="отход"),
        Waypoint(-300.0, 260.0, 180.0, Action.HOLD, duration=10.0,
                 note="сбор после удара"),
    ], loop=False, owner_kind="player", name="Удар и отход")

    sweep = Route([
        Waypoint(-200.0, -200.0, 120.0, Action.RECON, duration=8.0,
                 note="разведка NW"),
        Waypoint(200.0, -200.0, 120.0, Action.RECON, duration=8.0,
                 note="разведка NE"),
        Waypoint(200.0, 200.0, 120.0, Action.STRAFE, count=2, duration=4.0,
                 note="обстрел SE"),
        Waypoint(-200.0, 200.0, 120.0, Action.NAVIGATE, note="замыкание круга"),
    ], loop=True, owner_kind="player", name="Разведка по квадрату")

    heli = Route([
        Waypoint(0.0, -80.0, 60.0, Action.NAVIGATE, pass_mode="precise",
                 radius=8.0, note="подход"),
        Waypoint(0.0, 0.0, 40.0, Action.HOLD, duration=6.0, pass_mode="precise",
                 note="висение над целью"),
        Waypoint(0.0, 0.0, 40.0, Action.MISSILE, count=2, note="пуск ПТУР"),
        Waypoint(0.0, -120.0, 80.0, Action.NAVIGATE, pass_mode="precise",
                 note="отход"),
    ], loop=False, owner_kind="player", name="Вертолёт: висение и ПТУР")

    return {r.name: r for r in (strike, sweep, heli)}
