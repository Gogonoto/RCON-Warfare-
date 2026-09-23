"""
Офлайн-имитация сервера Minecraft по протоколу Source RCON.

Зачем
-----
Отлаживать авиадиспетчерскую на боевом мире дорого и опасно (команды `fill`
реально разрушают постройки). Этот модуль поднимает локальный TCP-сервер,
который:

* говорит на настоящем протоколе RCON (auth, пакеты, склейка длинных ответов);
* держит синтетический мир с детерминированным рельефом — поэтому сканер
  можно проверить без сервера;
* двигает «игроков» по простым траекториям — трекер и ИИ получают живые цели;
* пишет в `command_log` все полученные команды — тесты проверяют, ЧТО именно
  ядро отправило на сервер (например, что модель не перерисовывается 60 раз/с).

Использование
-------------
    from rwf.mock_server import MockMCServer
    srv = MockMCServer(port=0)          # 0 — любой свободный порт
    srv.start()
    ...
    srv.stop()
    print(srv.port, len(srv.command_log))
"""
from __future__ import annotations

import math
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

SEA_LEVEL = 62


# ---------------------------------------------------------------------------
#  Синтетический мир
# ---------------------------------------------------------------------------
def ground_height(x: float, z: float) -> int:
    """Детерминированный «рельеф»: холмы, долины, одно озеро."""
    h = (
        64.0
        + 14.0 * math.sin(x / 41.0) * math.cos(z / 37.0)
        + 9.0 * math.sin((x + z) / 57.0)
        + 5.0 * math.cos(x / 13.0 - z / 17.0)
    )
    # «Озеро» вокруг точки (120, -80)
    d = math.hypot(x - 120.0, z + 80.0)
    if d < 45.0:
        h -= (45.0 - d) * 0.55
    return int(round(max(-40.0, min(190.0, h))))


#: шаг и полуширина синтетической дорожной сетки (блоки)
ROAD_SPACING = 128
ROAD_HALF = 3


def is_road(x: float, z: float) -> bool:
    """Дорога в мире-имитаторе: сетка мощёных полос вдоль осей X и Z.

    Нужна, чтобы наземная техника в демо вела себя как в бою: выходила на
    дорогу и шла по ней быстрее, чем по пересечёнке (`rwf/groundnav.py`).
    """
    def near(v: float) -> bool:
        return abs(v - round(v / ROAD_SPACING) * ROAD_SPACING) <= ROAD_HALF
    return near(x) or near(z)


def surface_kind(x: float, z: float) -> str:
    """Тип верхнего блока колонки."""
    h = ground_height(x, z)
    if h > SEA_LEVEL and is_road(x, z):
        return "gray_concrete"        # классифицируется сканером как road
    if h < SEA_LEVEL - 1:
        return "sand" if h > SEA_LEVEL - 6 else "stone"
    if h > 150:
        return "snow_block"
    if h > 112:
        return "stone"
    if h <= SEA_LEVEL + 2:
        return "sand"
    return "grass_block"


def top_y(x: float, z: float) -> int:
    """Верхняя граница непустого блока колонки: вода тоже считается."""
    gh = ground_height(x, z)
    return SEA_LEVEL if gh < SEA_LEVEL else gh


#: Типы сущностей, которые сервер «знает». Всё остальное — ошибка разбора,
#: как в настоящем vanilla: на ней и строятся зонды возможностей.
KNOWN_ENTITY_TYPES = {
    "marker", "armor_stand", "area_effect_cloud", "interaction",
    "block_display", "item_display", "text_display",
    "tnt", "falling_block", "item", "arrow", "spectral_arrow", "fireball",
    "small_fireball", "dragon_fireball", "wither_skull", "snowball", "egg",
    "ender_pearl", "trident", "shulker_bullet", "llama_spit", "firework_rocket",
    "player", "zombie", "skeleton", "creeper", "cow", "pig", "sheep",
    "chicken", "villager", "enderman", "spider", "slime", "phantom",
    "drowned", "husk", "stray", "vex", "warden", "allay", "frog", "tadpole",
    "goat", "axolotl", "glow_squid", "camel", "sniffer", "breeze", "armadillo",
    "lightning_bolt", "experience_orb", "boat", "minecart", "painting",
    "item_frame", "glow_item_frame", "leash_knot", "eye_of_ender",
}


TAG_ALIASES = {
    "#minecraft:leaves": {"oak_leaves", "spruce_leaves", "birch_leaves"},
    "#minecraft:logs": {"oak_log", "spruce_log", "birch_log"},
    "#minecraft:dirt": {"dirt", "grass_block", "podzol", "coarse_dirt"},
    "#minecraft:sand": {"sand", "red_sand"},
    "#minecraft:base_stone_overworld": {"stone", "granite", "diorite", "andesite",
                                        "deepslate"},
}


@dataclass
class FakeEntity:
    eid: int
    kind: str
    pos: List[float]
    tags: List[str] = field(default_factory=list)
    data: Dict[str, str] = field(default_factory=dict)


@dataclass
class FakePlayer:
    name: str
    pos: List[float]
    yaw: float = 0.0
    pitch: float = 0.0
    online: bool = True
    scores: Dict[str, int] = field(default_factory=dict)
    # параметры синтетического движения
    _phase: float = 0.0
    _radius: float = 25.0
    _speed: float = 0.35

    def drift(self, dt: float) -> None:
        """Медленно ходить по кругу и оставаться на поверхности.

        Высота пересчитывается от рельефа: иначе «игрок» зависал бы в воздухе
        над холмом или проваливался в озеро, и тесты привязки к земле теряли
        смысл.
        """
        if not self.online:
            return
        self._phase += dt * self._speed
        self.pos[0] += math.cos(self._phase) * self._radius * dt * 0.35
        self.pos[2] += math.sin(self._phase) * self._radius * dt * 0.35
        self.pos[1] = float(top_y(self.pos[0], self.pos[2]) + 1)
        self.yaw = (math.degrees(self._phase) + 90.0) % 360.0


class MockMCServer:
    """RCON-сервер, притворяющийся Minecraft."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 25575,
        password: str = "2203",
        version: str = "1.20.1",
        flavor: str = "vanilla",          # 'vanilla' | 'paper'
        players: Tuple[str, ...] = ("Arlik88", "Steve"),
        split_long_responses: bool = True,
        latency: float = 0.0,
        animate_players: bool = True,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.version = version
        self.flavor = flavor
        self.split_long_responses = split_long_responses
        self.latency = latency
        self.animate_players = animate_players

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._conns: List[threading.Thread] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.command_log: List[str] = []
        self.max_log = 20000
        self.blocks: Dict[Tuple[int, int, int], str] = {}
        self.entities: Dict[int, FakeEntity] = {}
        self._next_eid = 1
        self.tps = 20
        self._time = 1000

        self.players: Dict[str, FakePlayer] = {}
        for i, name in enumerate(players):
            ang = i * 2.1
            self.players[name] = FakePlayer(
                name=name,
                pos=[math.cos(ang) * 30.0, float(ground_height(0, 0) + 1),
                     math.sin(ang) * 30.0],
                _phase=ang,
            )
        self._last_anim = time.time()

    # ---------------------------------------------------------------- запуск
    def start(self) -> "MockMCServer":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(16)
        self._sock.settimeout(0.25)
        self.port = self._sock.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="mock-rcon", daemon=True)
        self._thread.start()
        if self.animate_players:
            t = threading.Thread(target=self._anim_loop, name="mock-anim", daemon=True)
            t.start()
            self._conns.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> "MockMCServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()   # type: ignore[union-attr]
            except (socket.timeout, OSError):
                continue
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            t.start()
            self._conns.append(t)

    def _anim_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            dt = now - self._last_anim
            self._last_anim = now
            with self._lock:
                for p in self.players.values():
                    p.drift(dt)
            self._stop.wait(0.1)

    # -------------------------------------------------------------- протокол
    def _serve(self, conn: socket.socket) -> None:
        # TCP_NODELAY обязателен: без него связка Nagle + delayed ACK даёт
        # ~25 мс простоя на каждую команду даже на localhost. Это артефакт
        # имитатора, а не свойство протокола — бенчмарк стал бы врать.
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        conn.settimeout(0.5)
        buf = bytearray()
        authed = False
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= 4:
                    (length,) = struct.unpack_from("<i", buf, 0)
                    if length < 10 or length > 8192:
                        return
                    if len(buf) < 4 + length:
                        break
                    payload = bytes(buf[4:4 + length])
                    del buf[:4 + length]
                    pid, ptype = struct.unpack_from("<ii", payload, 0)
                    body = payload[8:length - 2].decode("utf-8", "replace")
                    if ptype == SERVERDATA_AUTH:
                        authed = body == self.password
                        self._send(conn, pid if authed else -1,
                                   SERVERDATA_AUTH_RESPONSE, "")
                        continue
                    if not authed:
                        self._send(conn, -1, SERVERDATA_AUTH_RESPONSE, "")
                        continue
                    resp = self.handle(body)
                    self._send_multi(conn, pid, SERVERDATA_RESPONSE_VALUE, resp)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, conn: socket.socket, pid: int, ptype: int, body: str) -> None:
        data = body.encode("utf-8", "replace")
        if self.latency:
            time.sleep(self.latency)
        conn.sendall(struct.pack("<iii", len(data) + 10, pid, ptype) + data + b"\x00\x00")

    def _send_multi(self, conn: socket.socket, pid: int, ptype: int, body: str) -> None:
        """Ответ длиннее пакета: разрезать (как делают современные серверы)."""
        data = body.encode("utf-8", "replace")
        limit = 4080
        if not self.split_long_responses and len(data) > limit:
            data = data[:limit]
            self._send(conn, pid, ptype, data.decode("utf-8", "ignore"))
            return
        if len(data) <= limit:
            self._send(conn, pid, ptype, body)
            return
        for i in range(0, len(data), limit):
            self._send(conn, pid, ptype, data[i:i + limit].decode("utf-8", "ignore"))

    # --------------------------------------------------------------- команды
    def log_command(self, cmd: str) -> None:
        with self._lock:
            self.command_log.append(cmd)
            if len(self.command_log) > self.max_log:
                del self.command_log[: len(self.command_log) // 2]

    def commands_since(self, index: int) -> List[str]:
        with self._lock:
            return list(self.command_log[index:])

    def count(self, pattern: str) -> int:
        rx = re.compile(pattern)
        with self._lock:
            return sum(1 for c in self.command_log if rx.search(c))

    def handle(self, cmd: str) -> str:
        """Разбор одной команды. Возвращает текст ответа, как vanilla."""
        cmd = cmd.strip()
        if cmd:
            self.log_command(cmd)
        if not cmd:
            return self._unknown(cmd)

        head, _, rest = cmd.partition(" ")
        rest = rest.strip()
        h = head.lower()

        if h == "list":
            return self._cmd_list(rest)
        if h in ("data",):
            return self._cmd_data(rest)
        if h == "execute":
            return self._cmd_execute(rest)
        if h == "tp" or h == "teleport":
            return self._cmd_tp(rest)
        if h == "summon":
            return self._cmd_summon(rest)
        if h == "kill":
            return self._cmd_kill(rest)
        if h in ("setblock",):
            return self._cmd_setblock(rest)
        if h == "fill":
            return self._cmd_fill(rest)
        if h in ("say", "tellraw", "msg", "tell", "w"):
            return f"[Server] {rest[:120]}" if h == "say" else ""
        if h == "title":
            return ""
        if h in ("particle", "playsound", "stopsound"):
            return ""
        if h == "time":
            m = re.search(r"\bset\s+(\w+|\d+)", rest)
            self._time = 1000 if (m and m.group(1) == "day") else self._time
            return f"Set the time to {self._time}"
        if h == "weather":
            return "Set the weather to clear"
        if h == "gamerule":
            return "Game rule test is already set to: false"
        if h == "scoreboard":
            return self._cmd_scoreboard(rest)
        if h == "version" or h == "ver":
            if self.flavor == "paper":
                return (f"This server is running Paper version git-Paper-{self.version} "
                        f"(MC: {self.version})")
            return self._unknown(cmd)
        if h == "help":
            return self._long_help()
        if h == "ride":
            # /ride появился только в 1.20.5 — важно для определения версии
            if self._version_tuple() >= (1, 20, 5):
                return "No entity was found"
            return self._unknown(cmd)
        if h == "damage":
            # /damage появился в 1.19.4 — синтаксис разбирается, цели нет
            return "No entity was found"
        if h == "give":
            return f"Gave 1 [item] to {rest.split()[0] if rest else 'someone'}"
        if h == "gamemode":
            return f"Set {rest.split()[-1] if rest else 'player'}'s game mode"
        if h == "seed":
            return "Seed: [-1234567890]"
        return self._unknown(cmd)

    # ------------------------------------------------------------- помощники
    def _version_tuple(self) -> Tuple[int, ...]:
        try:
            return tuple(int(p) for p in re.findall(r"\d+", self.version)[:3])
        except ValueError:
            return (1, 20, 1)

    def _unknown(self, cmd: str) -> str:
        return ("Unknown or incomplete command, see below for error position:\n"
                f"{cmd}\n{' ' * max(0, len(cmd) // 2)}^")

    def _long_help(self) -> str:
        """Ответ заведомо длиннее 4 Кб — проверка склейки многопакетных ответов."""
        lines = ["--- Showing help page 1 of 12 (/help <page>) ---"]
        for i in range(140):
            lines.append(
                f"/command_{i:03d} <arg1> <arg2> [optional] - "
                f"Описание команды номер {i}, достаточно длинное, чтобы набрать объём."
            )
        return "\n".join(lines)

    def _cmd_list(self, rest: str) -> str:
        with self._lock:
            names = [p.name for p in self.players.values() if p.online]
        if "uuids" in rest:
            return (f"There are {len(names)} of a max of 20 players online: "
                    + ", ".join(f"{n} (uuid-{i})" for i, n in enumerate(names)))
        return f"There are {len(names)} of a max of 20 players online: {', '.join(names)}"

    def _resolve_targets(self, selector: str) -> List[FakePlayer]:
        """Очень упрощённый разбор селекторов: ник, @a, @p, @e[tag=...]."""
        sel = selector.strip()
        with self._lock:
            alive = [p for p in self.players.values() if p.online]
            if sel == "@a":
                return alive
            if sel in ("@p", "@s", "@r"):
                return alive[:1]
            if sel.startswith("@e"):
                return []
            p = self.players.get(sel)
            return [p] if p and p.online else []

    def _cmd_data(self, rest: str) -> str:
        m = re.match(r"get\s+entity\s+(\S+)\s*(\S*)", rest)
        if m:
            target, path = m.group(1), m.group(2)
            players = self._resolve_targets(target)
            if not players:
                if target.startswith("@e"):
                    return "No entity was found"
                return f"Can't get entity data; no such entity {target}"
            p = players[0]
            with self._lock:
                if path == "Pos":
                    return (f"{p.name} has the following entity data: "
                            f"[{p.pos[0]:.4f}d, {p.pos[1]:.4f}d, {p.pos[2]:.4f}d]")
                if path == "Rotation":
                    return (f"{p.name} has the following entity data: "
                            f"[{p.yaw:.2f}f, {p.pitch:.2f}f]")
                if path == "Health":
                    return f"{p.name} has the following entity data: 20.0f"
                return f"{p.name} has the following entity data: {{Pos: [...]}}"
        if rest.startswith("modify"):
            return "Modified data of ... successfully"
        return f"Invalid data command: {rest[:60]}"

    def _cmd_tp(self, rest: str) -> str:
        parts = rest.split()
        if len(parts) < 4:
            return self._unknown("tp " + rest)
        target, xs, ys, zs = parts[0], parts[1], parts[2], parts[3]
        players = self._resolve_targets(target)
        if not players:
            return "No entity was found"
        try:
            x, y, z = float(xs.rstrip("~")), float(ys.rstrip("~")), float(zs.rstrip("~"))
        except ValueError:
            return self._unknown("tp " + rest)
        p = players[0]
        with self._lock:
            p.pos = [x, y, z]
        return f"Teleported {p.name} to {x}, {y}, {z}"

    def _cmd_summon(self, rest: str) -> str:
        parts = rest.split(None, 3)
        kind = parts[0] if parts else "minecraft:armor_stand"
        pos = [0.0, 100.0, 0.0]
        for i, v in enumerate(parts[1:4]):
            try:
                pos[i] = float(v)
            except (ValueError, IndexError):
                break
        nbt = parts[3] if len(parts) > 3 else ""
        tags = re.findall(r'Tags:\s*\[([^\]]*)\]', nbt)
        tag_list: List[str] = []
        if tags:
            tag_list = [t.strip().strip('"\'') for t in tags[0].split(",") if t.strip()]
        with self._lock:
            eid = self._next_eid
            self._next_eid += 1
            self.entities[eid] = FakeEntity(eid, kind, pos, tag_list)
        label = kind.split(":")[-1].replace("_", " ").title()
        return f"Summoned new {label}"

    def _cmd_kill(self, rest: str) -> str:
        sel = rest.strip() or "@e"
        m = re.search(r"tag=([\w\-\.]+)", sel)
        with self._lock:
            victims = []
            for eid, e in list(self.entities.items()):
                if sel.startswith("@e"):
                    if m and m.group(1) not in e.tags:
                        continue
                    victims.append(eid)
            for eid in victims:
                self.entities.pop(eid, None)
        if not victims:
            return "No entity was found"
        return f"Killed {len(victims)} entities"

    def _cmd_setblock(self, rest: str) -> str:
        parts = rest.split()
        if len(parts) < 4:
            return self._unknown("setblock " + rest)
        try:
            x, y, z = int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
        except ValueError:
            return self._unknown("setblock " + rest)
        block = parts[3]
        with self._lock:
            if block in ("minecraft:air", "air"):
                self.blocks.pop((x, y, z), None)
            else:
                self.blocks[(x, y, z)] = block
        return f"Changed the block at {x}, {y}, {z}"

    def _cmd_fill(self, rest: str) -> str:
        parts = rest.split()
        if len(parts) < 7:
            return self._unknown("fill " + rest)
        try:
            x1, y1, z1 = (int(float(v)) for v in parts[0:3])
            x2, y2, z2 = (int(float(v)) for v in parts[3:6])
        except ValueError:
            return self._unknown("fill " + rest)
        block = parts[6]
        n = 0
        with self._lock:
            for x in range(min(x1, x2), max(x1, x2) + 1):
                for y in range(min(y1, y2), max(y1, y2) + 1):
                    for z in range(min(z1, z2), max(z1, z2) + 1):
                        n += 1
                        if block in ("minecraft:air", "air"):
                            self.blocks.pop((x, y, z), None)
                        else:
                            self.blocks[(x, y, z)] = block
                        if n >= 32768:
                            return "The volume is too big"
        return f"Successfully filled {n} blocks"

    def _cmd_scoreboard(self, rest: str) -> str:
        parts = rest.split()
        if len(parts) >= 3 and parts[0] == "objectives":
            return "Created new objective test" if "add" in parts else ""
        if len(parts) >= 4 and parts[0] == "players":
            op, name, obj = parts[1], parts[2], parts[3]
            with self._lock:
                p = self.players.get(name)
                if p is None:
                    return "No player was found"
                if op == "get":
                    return f"{name} has {p.scores.get(obj, 0)} in {obj}"
                if op == "set":
                    try:
                        p.scores[obj] = int(parts[4])
                    except (IndexError, ValueError):
                        p.scores[obj] = 0
                    return f"Set {obj} for {name} to {p.scores[obj]}"
                if op == "add":
                    try:
                        p.scores[obj] = p.scores.get(obj, 0) + int(parts[4])
                    except (IndexError, ValueError):
                        pass
                    return f"Added 1 to {obj} for {name}"
        return ""

    # --------------------------------------------------------------- execute
    def _cmd_execute(self, rest: str) -> str:
        """Поддержка: `execute if block X Y Z <block>` и `execute positioned ...`."""
        tokens = rest.split()
        i = 0
        cx = cy = cz = None
        while i < len(tokens):
            t = tokens[i]
            if t == "positioned" and i + 3 < len(tokens):
                try:
                    cx = float(tokens[i + 1]); cy = float(tokens[i + 2])
                    cz = float(tokens[i + 3])
                except ValueError:
                    pass
                i += 4
                continue
            if t == "if" and i + 2 < len(tokens) and tokens[i + 1] == "entity":
                return self._probe_entity(tokens[i + 2])
            if t == "if" and i + 4 < len(tokens) and tokens[i + 1] == "block":
                try:
                    bx, by, bz = int(float(tokens[i + 2])), int(float(tokens[i + 3])), int(float(tokens[i + 4]))
                except ValueError:
                    return "Test failed"
                block = tokens[i + 5] if i + 5 < len(tokens) else "minecraft:air"
                ok = self.block_at(bx, by, bz, block)
                # Если после if идёт `run ...` — при провале команда не выполняется
                if ok and "run" in tokens[i + 6:]:
                    sub = rest.split("run", 1)[1].strip()
                    return self.handle(sub)
                return "Test passed" if ok else "Test failed"
            if t == "run":
                return self.handle(rest.split("run", 1)[1].strip())
            i += 1
        return self._unknown("execute " + rest)

    def _probe_entity(self, selector: str) -> str:
        """`execute if entity <selector>` — используется зондами возможностей.

        Неизвестный тип сущности обязан давать ошибку разбора: именно по ней
        ядро отличает 1.19.4+ (display-сущности есть) от более старых версий.
        """
        m = re.search(r"type=([\w:#\-\.]+)", selector)
        if m:
            kind = m.group(1).split(":")[-1]
            if kind not in KNOWN_ENTITY_TYPES:
                return self._unknown(f"execute if entity {selector}")
        tg = re.search(r"tag=([\w\-\.]+)", selector)
        if selector.startswith("@a") or selector.startswith("@p"):
            with self._lock:
                n = sum(1 for p in self.players.values() if p.online)
            return "Test passed" if n else "Test failed"
        with self._lock:
            n = sum(1 for e in self.entities.values()
                    if (not m or e.kind.endswith(m.group(1).split(":")[-1]))
                    and (not tg or tg.group(1) in e.tags))
        return "Test passed" if n else "Test failed"

    def block_at(self, x: int, y: int, z: int, block: str) -> bool:
        """Проверка `if block`: учитывает и синтетический рельеф, и setblock."""
        block = block.strip()
        with self._lock:
            placed = self.blocks.get((x, y, z))
        if placed is not None:
            return self._match(placed, block)
        gh = ground_height(x, z)
        kind = surface_kind(x, z)
        water_top = SEA_LEVEL if gh < SEA_LEVEL else None

        if block in ("minecraft:air", "air"):
            return y > top_y(x, z)
        if block in ("minecraft:water", "water"):
            return water_top is not None and gh < y <= water_top
        if block in ("minecraft:lava", "lava"):
            return False
        if block in ("minecraft:bedrock", "bedrock"):
            return y <= -59
        if y > gh:
            return False
        if y == gh:
            return self._match(kind, block)
        # Глубже поверхности
        if y >= gh - 3:
            return self._match("dirt", block)
        return self._match("stone", block)

    @staticmethod
    def _match(actual: str, query: str) -> bool:
        actual = actual.split(":")[-1]
        query = query.strip()
        if query.startswith("#"):
            return actual in TAG_ALIASES.get(query, set())
        return actual == query.split(":")[-1]

    # ------------------------------------------------------------- служебное
    def set_player_online(self, name: str, online: bool = True) -> None:
        with self._lock:
            p = self.players.get(name)
            if p:
                p.online = online

    def move_player(self, name: str, x: float, y: float, z: float) -> None:
        with self._lock:
            p = self.players.get(name)
            if p:
                p.pos = [x, y, z]

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "players": {n: list(p.pos) for n, p in self.players.items()},
                "entities": len(self.entities),
                "blocks": len(self.blocks),
                "commands": len(self.command_log),
            }
