"""
Golden-тесты силуэтов карты (P3.1).

Идея: `unit_icon` + `painter.paint` — чистые функции (список примитивов ->
список вызовов emit), поэтому «картинку» можно зафиксировать БЕЗ GL-контекста:
канонизируем отсортированный лог emit-вызовов каждого силуэта и сравниваем
хеш с golden-файлом в tests/golden/. Это детерминированная версия скриншотного
теста: ловит случайное изменение формы/топологии силуэта (добавили полигон,
сдвинули точку, поменяли ширину линии) так же надёжно, как PNG-дифф, но без
зависимости от рендерера и платформы.

Обновление эталонов — ЯВНОЙ командой (защита от «случайно позеленело»:
если тест упал, сначала разберись, правильное ли поведение новое):

    RWF_UPDATE_GOLDEN=1 python -m pytest tests/test_golden.py -q

Хеш считается от repr канонизированных вызовов; координаты округляются до
0.01 px — этого достаточно, чтобы поймать любую осмысленную правку силуэта,
но не зависеть от шума последних битов float.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import unittest

from rwf.icons import DEFAULT_SILHOUETTE, Lod, SILHOUETTES, unit_icon
from rwf.ui.painter import paint

GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"
UPDATE = bool(os.environ.get("RWF_UPDATE_GOLDEN"))

# фиксированные параметры съёмки (детерминизм важен не меньше, чем эталон)
SX, SY = 120.0, 80.0
YAW = 37.0            # не круглый: видны ошибки знаков синуса/косинуса
ROTOR_PHASE = 1.234   # фаза несущего винта (для вертолёта/дрона)
FILL, OUTLINE = "#8fb7ff", "#dfe9ff"

#: три уровня детализации соответствуют трём LOD-режимам карты (§9 документа:
#: «при 3 LOD-режимах × N типах это лотерея» — снимаем лотерею эталонами)
LODS = {
    "far": Lod(size_px=6.0, detail=0, alpha=90, show_label=False,
               show_rotors=False, show_blades=False),
    "mid": Lod(size_px=14.0, detail=1, alpha=170, show_label=True,
               show_rotors=True, show_blades=False),
    "near": Lod(size_px=26.0, detail=2, alpha=255, show_label=True,
                show_rotors=True, show_blades=True),
}


def _canon(value):
    """Округление float в структурах вызова для стабильного хеша."""
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, tuple):
        return tuple(_canon(v) for v in value)
    if isinstance(value, list):
        return [_canon(v) for v in value]
    return value


def _capture(kind: str, lod_name: str) -> str:
    """Канонический текст emit-лога силуэта при данном LOD."""
    calls = []

    def emit(op, **kw):
        calls.append((op, kw))

    prims = unit_icon(kind, SX, SY, YAW, LODS[lod_name], FILL, OUTLINE,
                      rotor_phase=ROTOR_PHASE)
    n = paint(emit, prims)
    assert n == len(calls), "paint() должен возвращать число emit-вызовов"
    lines = []
    for op, kw in sorted(calls, key=lambda c: repr(c)):
        args = ",".join(f"{k}={_canon(v)!r}" for k, v in sorted(kw.items()))
        lines.append(f"{op}({args})")
    return "\n".join(lines) + "\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestSilhouetteGolden(unittest.TestCase):
    """Эталонные логи примитивов: по файлу на пару (kind, lod)."""

    @classmethod
    def setUpClass(cls):
        if UPDATE:
            GOLDEN_DIR.mkdir(exist_ok=True)

    def test_all_kinds_have_silhouettes(self):
        # защита от молчаливого вылета kind из словаря: тогда все золотые
        # файлы совпали бы с DEFAULT_SILHOUETTE и тест потерял смысл
        self.assertGreaterEqual(len(SILHOUETTES), 7)
        self.assertIn(DEFAULT_SILHOUETTE, SILHOUETTES)

    def test_golden_files_match(self):
        problems = []
        for kind in sorted(SILHOUETTES):
            for lod_name in LODS:
                text = _capture(kind, lod_name)
                digest = _digest(text)
                path = GOLDEN_DIR / f"{kind}__{lod_name}.txt"
                if UPDATE:
                    path.write_text(text, encoding="utf-8")
                    continue
                if not path.exists():
                    problems.append(f"{path.name}: нет эталона")
                    continue
                stored = path.read_text(encoding="utf-8")
                if _digest(stored) != digest:
                    problems.append(
                        f"{path.name}: хеш {_digest(stored)[:12]} != "
                        f"текущий {digest[:12]}")
        if problems:
            self.fail(
                "Golden-эталоны рассинхронизированы:\n  " + "\n  ".join(problems)
                + "\nЕсли изменения ожидаемы: RWF_UPDATE_GOLDEN=1 pytest "
                  "tests/test_golden.py")

    def test_lod_changes_topology(self):
        # sanity: дальний LOD — точка, средний — без мелочи; если это
        # сломается, золотые файлы перестанут различаться между собой
        far = _capture("helicopter", "far")
        mid = _capture("helicopter", "mid")
        near = _capture("helicopter", "near")
        self.assertNotEqual(far, mid)
        self.assertNotEqual(mid, near)


if __name__ == "__main__":
    unittest.main()
