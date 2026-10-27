import pytest

from app.units import compare_values, convert, normalize_unit


def test_normalize_common_units():
    assert normalize_unit("мм").family == "length"
    assert normalize_unit("м²").family == "area"
    assert normalize_unit("м3/ч").family == "flow"
    assert normalize_unit("кПа").family == "pressure"
    assert normalize_unit("°C").family == "temperature"
    assert normalize_unit("") is None
    assert normalize_unit("штука") is None


def test_length_conversion_allows_equivalent_values():
    ok, note = compare_values(200, "мм", 0.2, "м", "=")
    assert ok is True
    assert "приведено" in note


def test_pressure_area_and_flow_conversion():
    assert compare_values(1, "кПа", 1000, "Па", ">=")[0] is True
    assert compare_values(1, "га", 10000, "м2", "=")[0] is True
    assert compare_values(100, "м3/ч", 100, "м3/ч", "=")[0] is True


def test_incomparable_units_are_not_forced():
    ok, reason = compare_values(200, "мм", 2, "кг", ">=")
    assert ok is None
    assert "величин" in reason


def test_temperature_conversion():
    value, _ = convert(32, "°F", "°C")
    assert value == pytest.approx(0.0, abs=1e-6)
    assert compare_values(20, "°C", 293.15, "K", "=")[0] is True


def test_operators_still_work_with_units():
    assert compare_values(200, "мм", 180, "мм", ">=")[0] is False
    assert compare_values(200, "мм", 200, "мм", ">=")[0] is True
    assert compare_values(10, "дБ", 9, "дБ", "<")[0] is True


def test_unitless_comparison():
    assert compare_values(10, "", 12, "", ">=")[0] is True
