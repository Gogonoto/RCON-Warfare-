"""
Пресеты запуска (LAUNCH-01…).

Заказчик: «основной экран запуска = только пресеты (Тип транспорта →
Пресет → База → Старт); редактор пресетов — отдельное окно в пол-экрана
поверх всего (drag-and-drop вооружения, настройка техники, сохранение
пресета)».

Раньше секция «Запуск» каждый раз заставляла оператора собирать машину
вручную: тип, высота, бот, три комбобокса подвесов. Пресет — это именованный
рецепт машины: вариант техники, загрузка подвесов по слотам, стартовая
высота, флаг бота. Пресеты лежат JSON-файлами в каталоге (как маршруты в
`storage.py`): их можно править руками, класть в git и передавать смене.

Формат файла
------------
    {
      "format": "rwf.preset",
      "version": 1,
      "name": "Штурмовик: НАР + пушка",
      "variant": "attacker",
      "altitude": 150.0,
      "is_bot": false,
      "loadout": {"0": "fab500", "1": "s8"},
      "ai": "strike"
    }

`loadout` — словарь «индекс слота -> ключ оружия»; пустые слоты не пишутся.
Неизвестное оружие при загрузке молча отбрасывается (пресет не должен
ломать запуск после изменения арсенала).
"""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

FORMAT_ID = "rwf.preset"
FORMAT_VERSION = 1

_NAME_OK = re.compile(r"[^0-9A-Za-zА-Яа-яЁё _\-.]+")
MAX_SLOTS = 4


def sanitize_name(name: str, max_len: int = 64) -> str:
    """Безопасное имя файла пресета (выход за пределы каталога невозможен)."""
    cleaned = (name or "").strip().replace("/", "_").replace("\\", "_")
    cleaned = _NAME_OK.sub("_", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return (cleaned or "preset")[:max_len]


@dataclass
class Preset:
    """Рецепт машины для быстрого запуска."""

    name: str
    variant: str = "attacker"
    loadout: Dict[int, str] = field(default_factory=dict)
    altitude: float = 150.0
    is_bot: bool = False
    ai: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"format": FORMAT_ID, "version": FORMAT_VERSION,
                "name": self.name, "variant": self.variant,
                "altitude": float(self.altitude), "is_bot": bool(self.is_bot),
                "ai": self.ai,
                "loadout": {str(k): v for k, v in sorted(self.loadout.items())}}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Preset":
        loadout: Dict[int, str] = {}
        for k, v in (data.get("loadout") or {}).items():
            try:
                slot = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= slot < MAX_SLOTS and isinstance(v, str) and v:
                loadout[slot] = v
        return cls(name=str(data.get("name") or "preset"),
                   variant=str(data.get("variant") or "attacker"),
                   loadout=loadout,
                   altitude=float(data.get("altitude") or 150.0),
                   is_bot=bool(data.get("is_bot")),
                   ai=str(data.get("ai") or ""))

    def clean_loadout(self, available: Dict[int, List[str]]) -> Dict[int, str]:
        """Убрать оружие из слотов, куда оно не подходит по категории.

        `available` — {слот: [ключи допустимого оружия]}. Пресет, собранный
        под другую машину, не должен пытаться повесить ФАБ на пулемётный
        узел: такие записи молча отбрасываются.
        """
        out: Dict[int, str] = {}
        for slot, key in self.loadout.items():
            allowed = available.get(slot)
            if allowed is None or key in allowed:
                out[slot] = key
        return out


class PresetLibrary:
    """Каталог пресетов: файлы JSON, по одному на пресет.

    Каталог кэшируется в памяти (FPS-03). Проекция UI спрашивает имена
    пресетов КАЖДЫЙ кадр (`project._project_presets` -> `by_variant`), а
    каталог лежит на диске: без кэша это 9 чтений файлов на кадр. На
    медленной ФС (сетевой диск, антивирус на каждый доступ) это 375 мс на
    кадр и 2.4 fps вместо 40+.

    Владелец данных по-прежнему здесь, второй источник правды не заводится:
    кэш инвалидируется собственными правками (`save`/`delete`) и явным
    `reload()`. Правки каталога «снаружи», мимо этого класса, подхватываются
    только после `reload()` — на старте сессии его делает `CoreFacade`.
    """

    def __init__(self, directory: Path | str = "presets"):
        self.dir = Path(directory)
        #: stem файла -> Preset (у кэшированных имя = stem); None — кэш пуст
        self._cache: Optional[Dict[str, Preset]] = None
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # noqa: BLE001 - каталог может быть только для чтения
            log.warning("Каталог пресетов %s недоступен для записи", self.dir)

    def path_for(self, name: str) -> Path:
        return self.dir / f"{sanitize_name(name)}.json"

    def exists(self, name: str) -> bool:
        return self.path_for(name).is_file()

    # ------------------------------------------------------------------ кэш
    def reload(self) -> None:
        """Сбросить кэш: следующий запрос перечитает каталог с диска."""
        self._cache = None

    def _entries(self) -> Dict[str, Preset]:
        """Снимок каталога (единственное место файлового чтения каталога)."""
        if self._cache is None:
            self._cache = self._scan()
        return self._cache

    def _scan(self) -> Dict[str, Preset]:
        out: Dict[str, Preset] = {}
        for name in sorted(path.stem for path in self.dir.glob("*.json")):
            try:
                out[name] = self._read(name)
            except (ValueError, OSError, KeyError):  # noqa: BLE001
                log.exception("Пресет %s не читается — пропущен", name)
        return out

    def _read(self, name: str) -> Preset:
        """Пресет прямо из файла, без кэша (имя = stem файла)."""
        data = json.loads(self.path_for(name).read_text(encoding="utf-8"))
        preset = Preset.from_dict(data)
        preset.name = name
        return preset

    def save(self, preset: Preset) -> Path:
        path = self.path_for(preset.name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(preset.to_dict(), ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        tmp.replace(path)
        self._cache = None          # FPS-03: состав каталога изменился
        log.info("Пресет «%s» сохранён в %s", preset.name, path)
        return path

    def load(self, name: str) -> Preset:
        """Пресет по имени. Возвращается КОПИЯ — кэш портить нельзя.

        Редактор пресетов правит загруженный объект перед сохранением,
        поэтому объект кэша наружу не отдаётся.
        """
        key = sanitize_name(name)
        preset = self._entries().get(key)
        if preset is None:
            # имени нет в снимке каталога: читаем напрямую — так же, как
            # раньше, вплоть до FileNotFoundError на несуществующем файле
            preset = self._read(key)
        out = copy.deepcopy(preset)
        out.name = name
        return out

    def delete(self, name: str) -> bool:
        path = self.path_for(name)
        if path.is_file():
            path.unlink()
            self._cache = None      # FPS-03: состав каталога изменился
            return True
        return False

    def names(self) -> List[str]:
        return sorted(self._entries())

    def all(self) -> List[Preset]:
        return [copy.deepcopy(preset)
                for _name, preset in sorted(self._entries().items())]

    def by_variant(self, variant: str) -> List[str]:
        """Имена пресетов для варианта техники (без чтения файлов).

        Эквивалентно `[p.name for p in self.all() if p.variant == variant]`:
        у кэшированных пресетов имя = stem файла, как и в `all()`.
        """
        return [name for name, preset in sorted(self._entries().items())
                if preset.variant == variant]


def default_presets() -> Dict[str, Preset]:
    """Стартовый набор, чтобы экран запуска не был пустым при первом запуске.

    Слоты пресета = индексы подвесов варианта, поэтому загрузка сверена с
    фактическими узлами каждой машины (тест test_default_presets_consistent).
    """
    return {
        "Штурмовик: бомбы + НАР": Preset(
            "Штурмовик: бомбы + НАР", "attacker",
            {0: "fab500", 1: "s8", 2: "s8", 3: "agm"}, altitude=180.0,
            ai="strike"),
        "Истребитель: перехват": Preset(
            "Истребитель: перехват", "fighter",
            {0: "r73", 1: "r73", 2: "r27", 3: "r27"}, altitude=260.0,
            ai="fighter"),
        "Бомбардировщик: FAB-1500": Preset(
            "Бомбардировщик: FAB-1500", "bomber",
            {0: "fab1500", 1: "fab1500", 2: "fab500", 3: "fab500"},
            altitude=300.0, ai="bomber"),
        "Вертолёт: НАР + Вихрь": Preset(
            "Вертолёт: НАР + Вихрь", "attack_heli",
            {0: "s8", 1: "s8", 2: "vikhr", 3: "vikhr"}, altitude=70.0,
            ai="heli"),
        "Ми-24: сопровождение": Preset(
            "Ми-24: сопровождение", "gunship",
            {0: "s8", 1: "s8", 2: "vikhr", 3: "vikhr"}, altitude=90.0,
            ai="heli"),
        "БПЛА: разведка + ПТУР": Preset(
            "БПЛА: разведка + ПТУР", "recon_drone", {0: "fab100", 2: "vikhr"},
            altitude=140.0, ai="recon"),
        "Транспортник: 5 т снабжения": Preset(
            "Транспортник: 5 т снабжения", "transport", {}, altitude=160.0),
        "Танк: прорыв": Preset(
            "Танк: прорыв", "mbt", {}, altitude=0.0),
        "БМП-3: сопровождение колонн": Preset(
            "БМП-3: сопровождение колонн", "ifv", {1: "kornet"},
            altitude=0.0),
    }


def ensure_seeded(library: PresetLibrary) -> int:
    """Создать стартовые пресеты, если каталог пуст. Возвращает число новых."""
    if library.names():
        return 0
    n = 0
    for preset in default_presets().values():
        try:
            library.save(preset)
            n += 1
        except OSError:  # noqa: BLE001
            log.exception("Не удалось создать стартовый пресет %s", preset.name)
    return n
