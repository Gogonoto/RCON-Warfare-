"""
Minecraft-специфика: построители команд, определение возможностей сервера,
зондирование рельефа.

Все функции возвращают СТРОКУ-команду. Ничего не отправляется на сервер —
отправкой занимается `rcon.CommandQueue`. Это позволяет:

* тестировать команды без сервера (просто сравнение строк);
* собирать пачки команд и уходить в один сетевой заход (pipelining);
* не блокировать физический тик на RTT.

Версии
------
Базовая версия проекта — **1.20.1**. С 1.21.2 Minecraft перевёл NBT сущностей
на snake_case (`ExplosionPower` → `explosion_power`), поэтому команды строятся
через `NbtDialect`, а не хардкодом. Возможности сервера определяются зондами,
которые не имеют побочных эффектов: неизвестная команда/тип сущности даёт
`Unknown or incomplete command`, известная — `Test failed` / `No entity was found`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

Runner = Callable[[str], str]      # любая функция «команда -> ответ»

Vec3 = Tuple[float, float, float]

UNKNOWN_CMD = "Unknown or incomplete command"


# ---------------------------------------------------------------------------
#  Версия
# ---------------------------------------------------------------------------
def parse_version(text: str) -> Tuple[int, ...]:
    """'1.20.1' / 'git-Paper-123 (MC: 1.20.4)' -> (1, 20, 4)."""
    m = re.search(r"MC:\s*(\d+(?:\.\d+)*)", text)
    if not m:
        m = re.search(r"(\d+\.\d+(?:\.\d+)*)", text)
    if not m:
        return (0,)
    return tuple(int(p) for p in m.group(1).split("."))


@dataclass(frozen=True)
class NbtDialect:
    """Синтаксис NBT сущностей. legacy — до 1.21.2, modern — с 1.21.2."""
    name: str = "legacy"

    # --- имена полей -----------------------------------------------------
    @property
    def explosion_power(self) -> str:
        return "ExplosionPower" if self.name == "legacy" else "explosion_power"

    @property
    def motion(self) -> str:
        return "Motion"          # Motion не переименовывали

    @property
    def tags(self) -> str:
        return "Tags"

    @property
    def passengers(self) -> str:
        return "Passengers"

    @property
    def block_state(self) -> str:
        """Ключ NBT блока у falling_block: переименован в 1.21.5+."""
        return "BlockState" if self.name == "legacy" else "block_state"

    def bool(self, value: bool) -> str:
        return "1b" if value else "0b"

    def vec(self, v: Sequence[float], suffix: str = "") -> str:
        return "[" + ",".join(f"{c:.4f}{suffix}" for c in v) + "]"

    def veci(self, v: Sequence[float]) -> str:
        return "[" + ",".join(f"{c:.4f}d" for c in v) + "]"


LEGACY_NBT = NbtDialect("legacy")
MODERN_NBT = NbtDialect("modern")


def dialect_for(version: Tuple[int, ...]) -> NbtDialect:
    return MODERN_NBT if version >= (1, 21, 2) else LEGACY_NBT


# ---------------------------------------------------------------------------
#  Возможности сервера
# ---------------------------------------------------------------------------
PROBE_TAG_A = "rwf_probe_a"
PROBE_TAG_B = "rwf_probe_b"


@dataclass
class ServerCaps:
    """Что умеет сервер. Определяется зондами, а не строкой версии."""
    version: Tuple[int, ...] = (1, 20, 1)
    version_text: str = "1.20.1"
    brand: str = "vanilla"                 # vanilla | paper | spigot | other
    has_display_entities: bool = True      # 1.19.4+
    has_interaction: bool = True           # 1.19.4+
    has_marker: bool = True                # 1.17+
    has_ride_command: bool = False         # 1.20.5+
    has_damage_command: bool = False       # 1.19.4+
    has_execute_store: bool = True         # 1.13+
    probes_done: bool = False
    raw: Dict[str, str] = field(default_factory=dict)

    @property
    def nbt(self) -> NbtDialect:
        return dialect_for(self.version)

    @property
    def model_backend(self) -> str:
        """Чем рисовать технику: сущностями (дёшево) или блоками (legacy)."""
        return "display" if self.has_display_entities and self.has_marker else "blocks"

    def describe(self) -> str:
        feats = [n for n, v in (
            ("display-сущности", self.has_display_entities),
            ("marker", self.has_marker),
            ("interaction", self.has_interaction),
            ("/ride", self.has_ride_command),
            ("/damage", self.has_damage_command),
        ) if v]
        return (f"{self.brand} {self.version_text} | backend={self.model_backend} | "
                + ", ".join(feats))

    # ---------------------------------------------------------------- зонды
    @classmethod
    def probe(cls, run: Runner, assume_version: Optional[str] = None) -> "ServerCaps":
        """Опросить сервер. Все зонды безопасны: ничего не создаётся и не ломается.

        `run` — любая функция, выполняющая команду (соединение, пул или очередь).
        """
        caps = cls()
        raw: Dict[str, str] = {}

        def ask(cmd: str) -> str:
            try:
                out = (run(cmd) or "").strip()
            except Exception as exc:  # noqa: BLE001 - зонд не должен ронять старт
                out = f"<error {exc}>"
            raw[cmd] = out
            return out

        def known(resp: str) -> bool:
            """Команда разобрана сервером (пусть и не нашла цель)."""
            return UNKNOWN_CMD not in resp and "<error" not in resp

        # 1. Бренд и версия. `version` есть только у Bukkit-семейства.
        ver_resp = ask("version")
        if known(ver_resp):
            caps.brand = "paper" if "paper" in ver_resp.lower() else (
                "spigot" if "spigot" in ver_resp.lower() else "bukkit")
            caps.version_text = ver_resp.splitlines()[0][:80]
            caps.version = parse_version(ver_resp) or caps.version
        elif assume_version:
            caps.version_text = assume_version
            caps.version = parse_version(assume_version) or (1, 20, 1)
        else:
            # Vanilla не отдаёт версию. Берём консервативный базовый уровень,
            # а реальные возможности всё равно определяются зондами ниже.
            caps.version_text = "vanilla (версия не определяется)"
            caps.version = (1, 20, 1)

        # 2. Наличие типов сущностей: зонд с пустой выборкой ничего не создаёт.
        caps.has_marker = known(ask(
            f"execute if entity @e[type=minecraft:marker,tag={PROBE_TAG_A},limit=1]"))
        caps.has_display_entities = known(ask(
            f"execute if entity @e[type=minecraft:block_display,tag={PROBE_TAG_A},limit=1]"))
        caps.has_interaction = known(ask(
            f"execute if entity @e[type=minecraft:interaction,tag={PROBE_TAG_A},limit=1]"))

        # 3. Команды, появившиеся в разных версиях.
        caps.has_ride_command = known(ask(
            f"ride @e[type=minecraft:marker,tag={PROBE_TAG_A},limit=1] mount "
            f"@e[type=minecraft:marker,tag={PROBE_TAG_B},limit=1]"))
        caps.has_damage_command = known(ask(
            f"damage @e[type=minecraft:marker,tag={PROBE_TAG_A},limit=1] 1"))
        caps.has_execute_store = known(ask(
            "execute store result score @s rwf_probe run list"))

        caps.raw = raw
        caps.probes_done = True
        log.info("Возможности сервера: %s", caps.describe())
        return caps


# ---------------------------------------------------------------------------
#  Селекторы и служебные команды
# ---------------------------------------------------------------------------
def by_tag(tag: str, limit: int = 1, extra: str = "") -> str:
    """`@e[tag=...,limit=1]` — стандартный способ выбрать «нашу» сущность."""
    parts = [f"tag={tag}", f"limit={limit}"]
    if extra:
        parts.append(extra)
    return "@e[" + ",".join(parts) + "]"


def kill_tag(tag: str) -> str:
    return f"kill {by_tag(tag, limit=512)}"


def count_tag(tag: str) -> str:
    return f"execute if entity {by_tag(tag, limit=1)}"


# ---------------------------------------------------------------------------
#  Перемещение и модель
# ---------------------------------------------------------------------------
def fmt_pos(x: float, y: float, z: float, prec: int = 2) -> str:
    return f"{x:.{prec}f} {y:.{prec}f} {z:.{prec}f}"


def tp_tag(tag: str, pos: Vec3, yaw: Optional[float] = None,
           pitch: Optional[float] = None) -> str:
    """Переместить якорную сущность. Одна команда = весь кадр модели."""
    sel = by_tag(tag)
    if yaw is None:
        return f"tp {sel} {fmt_pos(*pos)}"
    p = 0.0 if pitch is None else pitch
    return f"tp {sel} {fmt_pos(*pos)} {yaw % 360:.1f} {p:.1f}"


def summon_marker(tag: str, pos: Vec3, nbt: NbtDialect = LEGACY_NBT,
                  invulnerable: bool = True) -> str:
    """Невидимый якорь. `marker` дешевле armor_stand: не рендерится, без ИИ."""
    body = (f"{{{nbt.tags}:[\"{tag}\"],NoGravity:{nbt.bool(True)},"
            f"Silent:{nbt.bool(True)}")
    if invulnerable:
        body += f",Invulnerable:{nbt.bool(True)},PersistenceRequired:{nbt.bool(True)}"
    body += "}"
    return f"summon minecraft:marker {fmt_pos(*pos)} {body}"


def summon_armor_stand(tag: str, pos: Vec3, nbt: NbtDialect = LEGACY_NBT) -> str:
    body = (f"{{{nbt.tags}:[\"{tag}\"],Invisible:{nbt.bool(True)},Marker:{nbt.bool(True)},"
            f"NoGravity:{nbt.bool(True)},Silent:{nbt.bool(True)},"
            f"Invulnerable:{nbt.bool(True)},PersistenceRequired:{nbt.bool(True)}}}")
    return f"summon minecraft:armor_stand {fmt_pos(*pos)} {body}"


def block_display_nbt(block: str, offset: Vec3, scale: Vec3 = (1.0, 1.0, 1.0),
                      nbt: NbtDialect = LEGACY_NBT, tag: str = "",
                      brightness: Optional[int] = None) -> str:
    """NBT block_display: блок, смещение относительно носителя, масштаб.

    Смещение (`transformation.translation`) задаётся в локальных координатах
    носителя и ПОВОРАЧИВАЕТСЯ вместе с его yaw — поэтому достаточно одного `tp`
    якоря, чтобы развернулась вся модель.
    """
    parts = [
        f"block_state:{{Name:\"{block}\"}}",
        f"transformation:{{translation:{nbt.vec(offset)},scale:{nbt.vec(scale)}}}",
        "billboard:\"fixed\"",
        f"NoGravity:{nbt.bool(True)}",
        f"Invulnerable:{nbt.bool(True)}",
        f"Persistent:{nbt.bool(True)}",
    ]
    if tag:
        parts.append(f"{nbt.tags}:[\"{tag}\"]")
    if brightness is not None:
        parts.append(f"brightness:{{sky:{brightness},block:{brightness}}}")
    return "minecraft:block_display{" + ",".join(parts) + "}"


def summon_passengers(root_tag: str, children: Iterable[str],
                      nbt: NbtDialect = LEGACY_NBT) -> List[str]:
    """Посадить части модели на якорь.

    До 1.20.5 команды `/ride` нет — пассажиры добавляются через
    `data modify ... Passengers append value <nbt>`. В 1.20.5+ то же самое
    делает `/ride ... mount`, но `data modify` работает везде, поэтому
    используем его как единственный путь.
    """
    root = by_tag(root_tag)
    return [f"data modify entity {root} {nbt.passengers} append value {child}"
            for child in children]


def clear_passengers(root_tag: str, nbt: NbtDialect = LEGACY_NBT) -> str:
    return f"data remove entity {by_tag(root_tag)} {nbt.passengers}"


def set_display_offset(display_tag: str, offset: Vec3,
                       nbt: NbtDialect = LEGACY_NBT) -> str:
    """Поменять смещение конкретной части модели (например, поворот руля)."""
    return (f"data modify entity {by_tag(display_tag)} "
            f"transformation.translation set value {nbt.vec(offset)}")


# ---------------------------------------------------------------------------
#  Блоки: модель «в лоб» (legacy-бэкенд)
# ---------------------------------------------------------------------------
def setblock(x: int, y: int, z: int, block: str, replace: bool = True) -> str:
    mode = " replace" if replace else ""
    return f"setblock {x} {y} {z} {block}{mode}"


def fill(x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, block: str,
         mode: str = "") -> str:
    """mode: '' | 'replace' | 'destroy' | 'hollow' | 'keep' | 'outline'."""
    tail = f" {mode.strip()}" if mode.strip() else ""
    return f"fill {x1} {y1} {z1} {x2} {y2} {z2} {block}{tail}"


def tunnel_down(x: int, z: int, y_from: int, y_to: int, radius: int = 1,
                destroy: bool = True) -> List[str]:
    """Пробить shaft от поверхности до y_to — «достать игрока в пещере».

    Ключевая команда — `fill ... air destroy`: она не просто ставит воздух,
    а ломает блоки (с дропом), и, главное, гарантированно открывает полость.
    Одна команда на каждые 64 блока по Y, чтобы не упираться в лимит 32768.
    """
    cmds: List[str] = []
    hi, lo = max(y_from, y_to), min(y_from, y_to)
    mode = "destroy" if destroy else ""
    step = 64
    y = hi
    while y >= lo:
        bottom = max(lo, y - step + 1)
        cmds.append(fill(x - radius, bottom, z - radius,
                         x + radius, y, z + radius, "minecraft:air", mode))
        y = bottom - 1
    return cmds


def crater(x: int, y: int, z: int, radius: int, destroy: bool = True) -> List[str]:
    """Приблизительная сферическая воронка: несколько горизонтальных слоёв."""
    cmds: List[str] = []
    mode = "destroy" if destroy else ""
    for dy in range(-radius, radius + 1):
        r = int(round((radius ** 2 - dy ** 2) ** 0.5)) if abs(dy) <= radius else 0
        if r <= 0:
            continue
        cmds.append(fill(x - r, y + dy, z - r, x + r, y + dy, z + r,
                         "minecraft:air", mode))
    return cmds


# ---------------------------------------------------------------------------
#  Взрывы и снаряды
# ---------------------------------------------------------------------------
def summon_tnt(pos: Vec3, fuse: int = 60, motion: Optional[Vec3] = None,
               nbt: NbtDialect = LEGACY_NBT) -> str:
    parts = [f"fuse:{int(fuse)}"]
    if motion:
        parts.append(f"{nbt.motion}:{nbt.vec(motion)}")
    return f"summon minecraft:tnt {fmt_pos(*pos)} {{{','.join(parts)}}}"


def falling_block(pos: Vec3, block: str, motion: Optional[Vec3] = None,
                  nbt: NbtDialect = LEGACY_NBT) -> str:
    """Падающий обломок: реальный блок, который летит и разбивается.

    `DropItem:0b` — не дропает предмет (иначе место падения заваливается
    мусором), `HurtEntities:0b` — не калечит игроков: это декорация аварии.
    """
    parts = [f"{nbt.block_state}:{{Name:\"{block}\"}}", "Time:1",
             f"DropItem:{nbt.bool(False)}", f"HurtEntities:{nbt.bool(False)}"]
    if motion:
        parts.append(f"{nbt.motion}:{nbt.vec(motion)}")
    return f"summon minecraft:falling_block {fmt_pos(*pos)} {{{','.join(parts)}}}"


def fire_layer(x: int, y: int, z: int, block: str = "minecraft:fire") -> str:
    """Один блок огня (зоны горения вокруг обломков)."""
    return setblock(x, y, z, block)


def instant_explosion(pos: Vec3, nbt: NbtDialect = LEGACY_NBT) -> str:
    """Взрыв прямо сейчас (fuse=0). Используется для камикадзе и детонации."""
    return summon_tnt(pos, fuse=0, nbt=nbt)


def summon_projectile(kind: str, pos: Vec3, motion: Vec3,
                      power: Optional[float] = None,
                      nbt: NbtDialect = LEGACY_NBT,
                      owner_tag: Optional[str] = None) -> str:
    """Снаряд: fireball / small_fireball / arrow / snowball / trident...

    Важные тонкости vanilla:

    * `Motion` — начальная скорость, `power` (у fireball) — постоянное
      ускорение. Для «прямого» выстрела у fireball нужно занулить `power`,
      иначе снаряд будет плавно догонять ускорение и улетит не туда.
    * `ExplosionPower` (до 1.21.2) / `explosion_power` (с 1.21.2) — радиус взрыва.
    * Стрела без `Owner` всё равно наносит урон игрокам — для «дружественного
      огня» это то, что нужно.
    """
    parts: List[str] = [f"{nbt.motion}:{nbt.vec(motion)}"]
    kind_id = kind.split(":")[-1]
    if kind_id in ("fireball", "dragon_fireball", "wither_skull"):
        # Нулевое ускорение — иначе fireball летит по своей внутренней логике.
        parts.append(f"power:{nbt.vec([0.0, 0.0, 0.0])}")
        if power is not None:
            parts.append(f"{nbt.explosion_power}:{int(power)}")
    elif kind_id == "small_fireball":
        parts.append(f"power:{nbt.vec([0.0, 0.0, 0.0])}")
    if owner_tag:
        # Owner хранит UUID; тег передать нельзя, поэтому владелец опционален.
        pass
    return f"summon {kind if ':' in kind else 'minecraft:' + kind} " \
           f"{fmt_pos(*pos)} {{{','.join(parts)}}}"


def projectile_spread(motion: Vec3, index: int, total: int,
                      spread: float = 0.25) -> Vec3:
    """Развести залп веером: index от 0 до total-1."""
    if total <= 1:
        return motion
    t = (index - (total - 1) / 2.0) * spread
    mx, my, mz = motion
    return (mx + t * (1 if abs(mx) < abs(mz) else 0),
            my + t * 0.3,
            mz + t * (1 if abs(mz) <= abs(mx) else 0))


# ---------------------------------------------------------------------------
#  Эффекты и сообщения
# ---------------------------------------------------------------------------
def particle(name: str, pos: Vec3, delta: Vec3 = (0.3, 0.3, 0.3),
             speed: float = 0.0, count: int = 8, viewers: str = "@a") -> str:
    return (f"particle {name} {fmt_pos(*pos)} {delta[0]} {delta[1]} {delta[2]} "
            f"{speed} {count} {viewers}")


def playsound(sound: str, pos: Vec3, target: str = "@a", volume: float = 1.0,
              pitch: float = 1.0, category: str = "master") -> str:
    return (f"playsound {sound} {category} {target} {fmt_pos(*pos)} "
            f"{volume} {pitch}")


def _json_text(text: str, color: str = "white") -> str:
    safe = (text.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", "\\n"))
    return f'{{"text":"{safe}","color":"{color}"}}'


def tellraw(text: str, color: str = "gold", target: str = "@a",
            prefix: str = "[Bomber] ") -> str:
    return f"tellraw {target} {_json_text(prefix + text, color)}"


def actionbar(player: str, text: str, color: str = "yellow") -> str:
    return f"title {player} actionbar {_json_text(text, color)}"


def title(player: str, text: str, color: str = "red",
          subtitle: Optional[str] = None) -> List[str]:
    cmds = [f"title {player} title {_json_text(text, color)}"]
    if subtitle:
        cmds.append(f"title {player} subtitle {_json_text(subtitle, 'yellow')}")
    return cmds


# ---------------------------------------------------------------------------
#  Чтение мира
# ---------------------------------------------------------------------------
_POS_RE = re.compile(r"\[\s*(-?[\d.]+)d?\s*,\s*(-?[\d.]+)d?\s*,\s*(-?[\d.]+)d?\s*\]")
_ROT_RE = re.compile(r"\[\s*(-?[\d.]+)f?\s*,\s*(-?[\d.]+)f?\s*\]")
_LIST_RE = re.compile(r":\s*(.*)$", re.MULTILINE)


def parse_position(response: str) -> Optional[Vec3]:
    """`data get entity X Pos` -> (x, y, z). Устойчиво к локали и формату."""
    m = _POS_RE.search(response or "")
    if not m:
        return None
    try:
        return (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    except ValueError:
        return None


def parse_rotation(response: str) -> Optional[Tuple[float, float]]:
    m = _ROT_RE.search(response or "")
    if not m:
        return None
    try:
        return (float(m.group(1)), float(m.group(2)))
    except ValueError:
        return None


def parse_player_list(response: str) -> List[str]:
    """`list` -> ['Arlik88', 'Steve']. Работает и с русской локалью сервера."""
    m = _LIST_RE.search(response or "")
    if not m:
        return []
    tail = m.group(1).strip()
    if not tail:
        return []
    names = []
    for part in tail.split(","):
        name = part.strip()
        if not name:
            continue
        name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()   # «Nick (uuid)»
        if name:
            names.append(name)
    return names


def get_pos_cmd(target: str) -> str:
    return f"data get entity {target} Pos"


def get_rot_cmd(target: str) -> str:
    return f"data get entity {target} Rotation"


# ---------------------------------------------------------------------------
#  Scoreboard-триггеры (управление из игры)
# ---------------------------------------------------------------------------
def trigger_objective(name: str) -> str:
    return f"scoreboard objectives add {name} trigger"


def trigger_get(player: str, name: str) -> str:
    return f"scoreboard players get {player} {name}"


def trigger_reset(player: str, name: str) -> str:
    return f"scoreboard players set {player} {name} 0"


def parse_score(response: str) -> Optional[int]:
    """`scoreboard players get X obj` -> int или None."""
    m = re.search(r"has\s+(-?\d+)", response or "")
    if m:
        return int(m.group(1))
    m = re.search(r"(-?\d+)\s*$", (response or "").strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
#  Зондирование рельефа
# ---------------------------------------------------------------------------
#: Порядок проб при классификации: сначала самое вероятное.
SURFACE_KINDS: Tuple[Tuple[str, str], ...] = (
    ("minecraft:water", "water"),
    ("minecraft:lava", "lava"),
    ("minecraft:grass_block", "grass"),
    ("minecraft:sand", "sand"),
    ("minecraft:snow_block", "snow"),
    ("minecraft:gravel", "gravel"),
    ("minecraft:dirt", "dirt"),
    ("minecraft:oak_leaves", "leaves"),
    ("minecraft:oak_log", "log"),
    ("minecraft:stone", "stone"),
    # Мощёные поверхности — дороги и площадки. Стоят В КОНЦЕ списка: к этому
    # моменту почти все колонки уже опознаны как трава/вода/камень, поэтому
    # лишние пробы уходят лишь на немногие нераспознанные тайлы (GROUND-04).
    ("minecraft:gray_concrete", "road"),
    ("minecraft:light_gray_concrete", "road"),
    ("minecraft:polished_andesite", "road"),
    ("minecraft:stone_bricks", "road"),
    ("minecraft:cobblestone", "road"),
)

AIR_BLOCKS = ("minecraft:air", "minecraft:cave_air", "minecraft:void_air")


def if_block(x: int, y: int, z: int, block: str) -> str:
    return f"execute if block {x} {y} {z} {block}"


def _test_passed(resp: str) -> bool:
    """Vanilla отвечает 'Test passed' / 'Test failed' (локаль сервера — en)."""
    r = (resp or "").lower()
    if UNKNOWN_CMD.lower() in r:
        raise ValueError(f"Сервер не понял команду: {resp[:80]}")
    return "pass" in r


def probe_column(run: Runner, x: int, z: int, top: int = 200, bottom: int = -64,
                 coarse: int = 16, check_cave_air: bool = True
                 ) -> Tuple[Optional[int], str, int]:
    """Найти поверхность колонки. Возвращает (y, kind, probes).

    Алгоритм (вместо 56 запросов в наброске — обычно 10-18):

    1. Грубый спуск от `top` с шагом `coarse` — ищем первый непустой блок.
       Это корректно обрабатывает нависающие скалы (в отличие от бинпоиска
       по всей колонке, который может «провалиться» в полость).
    2. Уточнение бинарным поиском внутри найденного окна (3-4 запроса).
    3. Классификация — пробы по списку `SURFACE_KINDS` с ранним выходом:
       для травы и воды это 1-2 запроса.

    `cave_air` считается воздухом (иначе пещерная полость у поверхности
    принималась бы за грунт) — на это уходит не больше одного лишнего запроса.
    """
    probes = 0

    def is_air(y: int) -> bool:
        nonlocal probes
        probes += 1
        try:
            if _test_passed(run(if_block(x, y, z, "minecraft:air"))):
                return True
        except ValueError:
            return False
        if check_cave_air:
            for alt in AIR_BLOCKS[1:]:
                probes += 1
                try:
                    if _test_passed(run(if_block(x, y, z, alt))):
                        return True
                except ValueError:
                    return False
        return False

    y = top
    found: Optional[int] = None
    while y > bottom:
        if not is_air(y):
            found = y
            break
        y -= coarse
    if found is None:
        # Вся колонка пустая до самого низа: проверим нижнюю границу.
        if not is_air(bottom):
            return bottom, classify_block(run, x, bottom, z), probes + 1
        return None, "air", probes

    # found — непустой блок, found + coarse — пустой (это предыдущая проба).
    # Поверхность где-то между ними: бинарный поиск границы.
    lo, hi = found, min(top + coarse, found + coarse)
    if found == top:
        # Самая первая проба уже дала блок — выше смотреть некуда.
        return top, classify_block(run, x, top, z), probes + 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if is_air(mid):
            hi = mid
        else:
            lo = mid
    surface = lo                      # самый верхний непустой блок
    kind = classify_block(run, x, surface, z)
    return surface, kind, probes


def classify_block(run: Runner, x: int, y: int, z: int,
                   kinds: Sequence[Tuple[str, str]] = SURFACE_KINDS) -> str:
    """Определить тип блока пробами. Ранний выход — обычно 1-3 запроса."""
    for block, kind in kinds:
        try:
            if _test_passed(run(if_block(x, y, z, block))):
                return kind
        except ValueError:
            continue
    return "other"
