"""Нормализация и конвертация единиц измерения.

Модель часто возвращает «200» + «мм» в СТО и «0,2» + «м» в ТЗ. Без
конвертации такие пары раньше уходили в «экспертную оценку». Здесь
описан небольшой, но расширяемый словарь величин, которые встречаются в
строительных ТЗ и СТО.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
    family: str
    # коэффициент перевода в базовую единицу семейства
    factor: float = 1.0
    # смещение для температур (база — °C)
    offset: float = 0.0


def _u(family: str, factor: float = 1.0, offset: float = 0.0) -> Unit:
    return Unit(family, factor, offset)


ALIASES: dict[str, Unit] = {
    # длина (база — метр)
    "мм": _u("length", 0.001),
    "миллиметр": _u("length", 0.001),
    "миллиметров": _u("length", 0.001),
    "см": _u("length", 0.01),
    "сантиметр": _u("length", 0.01),
    "м": _u("length", 1.0),
    "метр": _u("length", 1.0),
    "метров": _u("length", 1.0),
    "пм": _u("length", 1.0),
    "км": _u("length", 1000.0),
    "километр": _u("length", 1000.0),
    "дюйм": _u("length", 0.0254),
    "мм2": _u("area", 1e-6),
    # площадь (база — м²)
    "см2": _u("area", 1e-4),
    "м2": _u("area", 1.0),
    "квм": _u("area", 1.0),
    "квадратныйметр": _u("area", 1.0),
    "га": _u("area", 10000.0),
    "гектар": _u("area", 10000.0),
    "км2": _u("area", 1e6),
    # объём (база — м³)
    "мл": _u("volume", 1e-6),
    "л": _u("volume", 0.001),
    "литр": _u("volume", 0.001),
    "м3": _u("volume", 1.0),
    "кубм": _u("volume", 1.0),
    "кубическийметр": _u("volume", 1.0),
    "см3": _u("volume", 1e-6),
    # масса (база — кг)
    "г": _u("mass", 0.001),
    "грамм": _u("mass", 0.001),
    "кг": _u("mass", 1.0),
    "килограмм": _u("mass", 1.0),
    "т": _u("mass", 1000.0),
    "тонна": _u("mass", 1000.0),
    # давление (база — Па)
    "па": _u("pressure", 1.0),
    "кпа": _u("pressure", 1000.0),
    "мпа": _u("pressure", 1e6),
    "бар": _u("pressure", 100000.0),
    "ммводст": _u("pressure", 9.80665),
    "ммртст": _u("pressure", 133.322),
    # мощность (база — Вт)
    "вт": _u("power", 1.0),
    "квт": _u("power", 1000.0),
    "мвт": _u("power", 1e6),
    # расход воздуха/воды (база — м³/с)
    "м3ч": _u("flow", 1 / 3600),
    "м3с": _u("flow", 1.0),
    "лс": _u("flow", 0.001),
    "лмин": _u("flow", 1 / 60000),
    "лч": _u("flow", 1 / 3_600_000),
    # время (база — секунда)
    "с": _u("time", 1.0),
    "сек": _u("time", 1.0),
    "мин": _u("time", 60.0),
    "ч": _u("time", 3600.0),
    "час": _u("time", 3600.0),
    "сут": _u("time", 86400.0),
    # скорость (база — м/с)
    "мс": _u("speed", 1.0),
    "кмч": _u("speed", 1 / 3.6),
    # температура (база — °C)
    "c": _u("temperature", 1.0),
    "°c": _u("temperature", 1.0),
    "градус": _u("temperature", 1.0),
    "f": _u("temperature", 5 / 9, -32 * 5 / 9),
    "°f": _u("temperature", 5 / 9, -32 * 5 / 9),
    "k": _u("temperature", 1.0, -273.15),
    # прочее
    "%": _u("percent", 1.0),
    "процент": _u("percent", 1.0),
    "дб": _u("db", 1.0),
    "дба": _u("db", 1.0),
    "шт": _u("count", 1.0),
    "раз": _u("count", 1.0),
}

# Приведение записи единицы к ключу словаря.
_SUBSTITUTIONS = [
    (" ", ""),
    (".", ""),
    ("·", ""),
    ("²", "2"),
    ("³", "3"),
    ("^2", "2"),
    ("^3", "3"),
    ("кубм", "м3"),
    ("кубм", "м3"),
    ("квм", "м2"),
    ("кубическихметров", "м3"),
    ("квадратныхметров", "м2"),
    ("градусовцельсия", "c"),
    ("градc", "c"),
    ("°с", "c"),
    ("℃", "c"),
    ("дб(а)", "дба"),
    ("дбa", "дба"),
    ("ммводст", "ммводст"),
    ("ммртст", "ммртст"),
    ("м3/час", "м3ч"),
    ("м3/ч", "м3ч"),
    ("м3/с", "м3с"),
    ("л/с", "лс"),
    ("л/мин", "лмин"),
    ("км/ч", "кмч"),
    ("м/с", "мс"),
    ("кг/м3", "кгм3"),
]


def normalize_unit(raw: object) -> Unit | None:
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("ё", "е")
    if not text:
        return None
    for source, target in _SUBSTITUTIONS:
        text = text.replace(source, target)
    text = re.sub(r"[^a-zа-я0-9%°]", "", text)
    if not text:
        return None
    return ALIASES.get(text)


def unit_label(raw: object) -> str:
    return str(raw or "").strip()


def convert(value: float, from_unit: object, to_unit: object) -> tuple[float | None, str]:
    """Перевести значение в целевую единицу.

    Возвращает (значение, пояснение). Если единицы несопоставимы —
    (None, причина).
    """
    source = normalize_unit(from_unit)
    target = normalize_unit(to_unit)
    if source is None or target is None:
        return None, "единица измерения не распознана"
    if source.family != target.family:
        return None, "разные физические величины"
    if source.family == "temperature":
        celsius = value * source.factor + source.offset
        result = (celsius - target.offset) / target.factor
    else:
        result = value * source.factor / target.factor
    return result, ""


def _close(left: float, right: float, tolerance: float = 1e-6) -> bool:
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) <= tolerance * scale


def compare_values(
    expected: float,
    expected_unit: object,
    actual: float,
    actual_unit: object,
    operator: str,
) -> tuple[bool | None, str]:
    """Сравнить фактическое значение (ТЗ) с эталонным (СТО) с учётом единиц."""
    op = (operator or "").strip()
    if op not in {">=", ">", "<=", "<", "=", "==", "!="}:
        return None, "неизвестный оператор сравнения"

    if normalize_unit(expected_unit) is None and normalize_unit(actual_unit) is None:
        converted = actual
        note = ""
    else:
        converted, reason = convert(actual, actual_unit, expected_unit)
        if converted is None:
            return None, reason
        note = ""
        if str(expected_unit or "").strip().casefold() != str(actual_unit or "").strip().casefold():
            note = (
                f"Значение ТЗ приведено к единицам СТО: {actual:g} {unit_label(actual_unit)}"
                f" → {converted:.6g} {unit_label(expected_unit)}."
            )

    if op == ">=":
        result = converted >= expected or _close(converted, expected)
    elif op == ">":
        result = converted > expected and not _close(converted, expected)
    elif op == "<=":
        result = converted <= expected or _close(converted, expected)
    elif op == "<":
        result = converted < expected and not _close(converted, expected)
    elif op in {"=", "=="}:
        result = _close(converted, expected)
    else:  # "!="
        result = not _close(converted, expected)
    return result, note
