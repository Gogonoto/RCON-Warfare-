"""
Настройки приложения: загрузка/сохранение JSON, переопределение из окружения.

Отдельный модуль нужен, чтобы ни один класс не держал «магические константы»
в теле (в наброске пароль и хост были зашиты прямо в `ui.py`).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, get_type_hints

DEFAULT_CONFIG_PATH = Path("rwf.json")

#: Канонический префикс переменных окружения (RCON Warfare)
ENV_PREFIX = "RWF"
#: Прежнее имя проекта: принимается как устаревший псевдоним
LEGACY_ENV_PREFIX = "BOMBER"


def env_value(name: str) -> Optional[str]:
    """Значение переменной окружения; `RWF_*` важнее устаревшего `BOMBER_*`."""
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        value = os.environ.get(f"{prefix}_{name}")
        if value:
            return value
    return None

TICK_RATES: Tuple[float, ...] = (0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0)
DEFAULT_TICK = 0.25


@dataclass
class RCONConfig:
    host: str = "127.0.0.1"
    port: int = 25575
    password: str = "2203"
    timeout: float = 8.0
    pool_size: int = 6
    queue_workers: int = 2
    queue_rate: float = 250.0          # команд в секунду максимум
    batch_size: int = 24
    auto_reconnect: bool = True
    # Офлайн-режим: вместо реального сервера поднимаем MockMCServer.
    mock: bool = False
    mock_version: str = "1.20.1"
    mock_flavor: str = "vanilla"       # 'vanilla' | 'paper'
    mock_players: Tuple[str, ...] = ("Arlik88", "Steve")


@dataclass
class SimConfig:
    """Параметры симуляции (физика юнитов, тик, модель)."""
    tick: float = DEFAULT_TICK
    # 'display' — техника из block_display-сущностей (1.19.4+), дёшево и плавно.
    # 'blocks'  — перестройка блоков, как в наброске (медленно, ломает мир).
    # Блочная модель (выбор заказчика). Бюджет команд считается так:
    #   2 × N_блоков × model_update_hz  на один движущийся юнит.
    # Для Су-25 (22 блока) при 5 Гц это ~220 команд/с — впритык к безопасному
    # пределу 250/с (см. SKILL.md). Выше поднимать нельзя: очередь начнёт
    # отбрасывать VISUAL-команды, и модель будет обновляться реже заданного.
    model_backend: str = "blocks"
    model_update_hz: float = 5.0
    model_yaw_step: float = 4.0        # квант курса для перерисовки, град
    gravity: float = 9.8
    terminal_speed: float = 78.0       # блоков/с
    collide_terrain: bool = True
    consume_ammo: bool = True
    consume_fuel: bool = True


@dataclass
class MapConfig:
    size: int = 1000                   # размер сцены в пикселях
    view_radius: float = 400.0         # метров по половине ширины
    min_radius: float = 30.0
    max_radius: float = 4000.0
    follow: bool = True
    show_grid: bool = True
    show_terrain: bool = True
    show_routes: bool = True
    show_markers: bool = True
    show_players: bool = True
    pan_step_fraction: float = 0.125   # доля экрана на шаг WASD
    redraw_hz: float = 30.0


@dataclass
class ScannerConfig:
    radius: int = 256
    step: int = 8
    top_y: int = 200                   # выше гор в обычном мире не бывает
    bottom_y: int = -64
    coarse: int = 16                   # шаг грубого спуска перед бинпоиском
    batch_size: int = 128              # команд в одном сетевом заходе
    max_columns: int = 200000          # предохранитель от «сканирую весь мир»
    # Потоки намеренно не нужны: сервер исполняет команды последовательно,
    # а задержку скрывает пакетная отправка (см. docstring scanner.py).


@dataclass
class CombatConfig:
    """Боевое применение и разрушаемость мира.

    По решению заказчика разрушаемость настраивается, а не зашита:
    пробивка туннеля в пещеру — исходная фича проекта, но `fill ... destroy`
    с дропом на десятки тысяч блоков заметно нагружает сервер.
    """
    # --- бомбы ------------------------------------------------------------
    gravity_effective: float = 15.0    # м/с²: у сущностей MC ~16 с учётом drag
    bomb_lead: bool = True             # упреждение по времени падения
    bomb_interval: float = 0.35        # с между бомбами в залпе (иначе сливаются)
    # --- воронка ----------------------------------------------------------
    crater_enabled: bool = True
    crater_radius: int = 3             # радиус по калибру берётся из WEAPONS
    # --- пробивка до цели в пещере ---------------------------------------
    tunnel_enabled: bool = True
    tunnel_radius: int = 2             # 0 = 1x1, 1 = 3x3, 2 = 5x5
    tunnel_max_depth: int = 80         # предел шахты вниз от точки удара
    tunnel_only_underground: bool = True   # не копать, если цель на поверхности
    # --- способ ломания ---------------------------------------------------
    drop_blocks: bool = True           # True: 'destroy' (с дропом), False: 'air'
    confirm_destructive: bool = True   # спрашивать перед первым применением
    destructive_confirmed: bool = False
    # --- снаряды ----------------------------------------------------------
    guided_missiles: bool = True       # УР реально наводятся, а не летят прямо
    use_block_missiles: bool = True    # УР/ПТУР — блочная ракета (видна как техника)
    player_gun_enabled: bool = True    # игроки могут бить технику триггером
    gun_poll_interval: float = 0.5     # с между опросами триггера rwf_fire
    manpads_enabled: bool = True       # ПЗРК: rwf_lock/rwf_missile из игры
    manpads_poll_interval: float = 0.35
    missile_turn_rate: float = 75.0    # град/с разворота ракеты
    missile_max_time: float = 12.0     # с полёта до самоуничтожения
    missile_hit_radius: float = 3.0
    max_tracked_missiles: int = 24
    muzzle_speed_scale: float = 1.0    # множитель скорости снарядов
    # --- ограничения ------------------------------------------------------
    max_commands_per_volley: int = 60  # предохранитель от залпа в 25 TNT подряд
    chat_feedback: bool = True         # tellraw/actionbar о применении
    silent_mode: bool = False          # ничего не писать в игровой чат


@dataclass
class UIConfig:
    language: str = "ru"
    theme: str = "dark"
    window_width: int = 1680
    window_height: int = 980
    splitter_sizes: Tuple[int, int, int] = (400, 880, 400)
    log_lines: int = 2000
    silent_chat: bool = False          # не писать tellraw в игровой чат
    hotkeys_wasd: bool = True
    confirm_destructive: bool = True   # спрашивать перед fill/kill
    inertia: bool = True               # UX-02: инерция камеры карты
    ui_scale: float = 1.0              # UX-03: масштаб UI (Ctrl+колесо)


@dataclass
class AppConfig:
    rcon: RCONConfig = field(default_factory=RCONConfig)
    sim: SimConfig = field(default_factory=SimConfig)
    combat: CombatConfig = field(default_factory=CombatConfig)
    map: MapConfig = field(default_factory=MapConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    pilot: str = ""
    target: str = ""

    # --------------------------------------------------------------- файл
    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> "AppConfig":
        p = Path(path or env_value("CONFIG") or DEFAULT_CONFIG_PATH)
        cfg = cls()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                cfg = _from_dict(cls, data)
                cfg._path = p                      # type: ignore[attr-defined]
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                # Битый конфиг не должен мешать запуску — работаем на дефолтах.
                cfg = cls()
                cfg._path = p                      # type: ignore[attr-defined]
                cfg._load_error = str(exc)         # type: ignore[attr-defined]
        else:
            cfg._path = p                          # type: ignore[attr-defined]
        cfg._apply_env()
        return cfg

    def save(self, path: Optional[Path | str] = None) -> Path:
        p = Path(path or getattr(self, "_path", DEFAULT_CONFIG_PATH))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False),
                     encoding="utf-8")
        self._path = p                             # type: ignore[attr-defined]
        return p

    @property
    def path(self) -> Path:
        return Path(getattr(self, "_path", DEFAULT_CONFIG_PATH))

    @property
    def load_error(self) -> Optional[str]:
        return getattr(self, "_load_error", None)

    # ---------------------------------------------------------- окружение
    def _apply_env(self) -> None:
        """Переменные окружения переопределяют файл настроек.

        Канонический префикс — ``RWF_`` (RCON Warfare): ``RWF_HOST``,
        ``RWF_PORT``, ``RWF_PASSWORD``, ``RWF_MOCK``, ``RWF_TICK``,
        ``RWF_CONFIG``. Устаревший ``BOMBER_`` (прежнее имя проекта)
        принимается как псевдоним, чтобы скрипты и юниты прошлых версий
        продолжали работать.
        """
        host = env_value("HOST")
        if host:
            self.rcon.host = host
        port = env_value("PORT")
        if port:
            try:
                self.rcon.port = int(port)
            except ValueError:
                pass
        password = env_value("PASSWORD")
        if password:
            self.rcon.password = password
        mock = env_value("MOCK")
        if mock:
            self.rcon.mock = mock.lower() in ("1", "true", "yes", "on")
        tick = env_value("TICK")
        if tick:
            try:
                self.sim.tick = float(tick)
            except ValueError:
                pass


def _from_dict(cls, data: Dict[str, Any]) -> Any:
    """Аккуратно собрать dataclass-дерево из словаря, игнорируя лишние ключи."""
    if not is_dataclass(cls):
        return data
    hints = get_type_hints(cls)
    kwargs: Dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints.get(f.name, str)
        origin = getattr(ftype, "__origin__", None)
        if origin is not None:
            # Tuple[str, ...] / List[...] из JSON приходят списками — приводим.
            if origin is tuple and isinstance(value, list):
                value = tuple(value)
            kwargs[f.name] = value
            continue
        if is_dataclass(ftype) and isinstance(value, dict):
            kwargs[f.name] = _from_dict(ftype, value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)
