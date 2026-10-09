"""Tests for the generic ``Value`` decoder (``values.value_as_native``).

The decoder backs the generic sensors for catalog-added signals, which must
work on pre-2021 Model S/X and on 2021+ vehicles alike: the same field can
arrive typed or as a string, and on different ``Value`` arms by vehicle or
firmware. These cases pin that behaviour since live testing covers only one
generation.

``values.py`` needs only the generated protos, so it's loaded by path under a
synthetic package with no Home Assistant installed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("google.protobuf")

_PKG = "tesla_telemetry_values_isolated"
_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)


def _load(name: str, path: Path, *, package: bool = False) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name,
        path,
        submodule_search_locations=[str(path.parent)] if package else None,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_isolated() -> tuple[ModuleType, ModuleType]:
    if f"{_PKG}.values" in sys.modules:
        return sys.modules[f"{_PKG}.proto.vehicle_data_pb2"], sys.modules[f"{_PKG}.values"]
    pkg = ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    _load(f"{_PKG}.proto", _DIR / "proto" / "__init__.py", package=True)
    vdp = _load(f"{_PKG}.proto.vehicle_data_pb2", _DIR / "proto" / "vehicle_data_pb2.py")
    values = _load(f"{_PKG}.values", _DIR / "values.py")
    return vdp, values


vdp, values = _load_isolated()


def _value(**kwargs) -> object:
    return vdp.Value(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"double_value": 12345.6}, 12345.6),
        ({"float_value": 1.5}, 1.5),
        ({"int_value": 7}, 7.0),
        ({"long_value": 9_000_000_000}, 9_000_000_000.0),
    ],
)
def test_typed_numbers(kwargs: dict, expected: float) -> None:
    kind, state, attrs = values.value_as_native(_value(**kwargs))
    assert kind == values.VALUE_KIND_NUMBER
    assert state == pytest.approx(expected)
    assert attrs == {}


def test_numeric_string_is_a_number() -> None:
    """Untyped firmware sends numbers as strings — still a measurement."""
    kind, state, _ = values.value_as_native(_value(string_value="12.5"))
    assert kind == values.VALUE_KIND_NUMBER
    assert state == 12.5


@pytest.mark.parametrize("text", ["Home", "nan", "inf", "2024.44.25"])
def test_non_numeric_or_non_finite_string_stays_text(text: str) -> None:
    kind, state, _ = values.value_as_native(_value(string_value=text))
    assert kind == values.VALUE_KIND_STRING
    assert state == text


def test_empty_string_is_unknown_text() -> None:
    kind, state, _ = values.value_as_native(_value(string_value=""))
    assert kind == values.VALUE_KIND_STRING
    assert state is None


def test_bool() -> None:
    assert values.value_as_native(_value(boolean_value=True))[:2] == (
        values.VALUE_KIND_BOOL,
        "on",
    )
    assert values.value_as_native(_value(boolean_value=False))[:2] == (
        values.VALUE_KIND_BOOL,
        "off",
    )


def test_enum_drops_type_prefix() -> None:
    raw = "SentryModeStateArmed"
    v = _value(sentry_mode_state_value=vdp.SentryModeState.Value(raw))
    kind, state, attrs = values.value_as_native(v)
    assert kind == values.VALUE_KIND_ENUM
    assert state == "armed"
    assert attrs == {"raw": raw}


def test_enum_type_named_with_value_suffix() -> None:
    """``DetailedChargeStateValue`` values are prefixed without ``Value``."""
    raw = "DetailedChargeStateNoPower"
    v = _value(detailed_charge_state_value=vdp.DetailedChargeStateValue.Value(raw))
    kind, state, _ = values.value_as_native(v)
    assert kind == values.VALUE_KIND_ENUM
    assert state == "no_power"


def test_location_composite() -> None:
    v = _value(location_value=vdp.LocationValue(latitude=37.5, longitude=-122.25))
    kind, state, attrs = values.value_as_native(v)
    assert kind == values.VALUE_KIND_COMPOSITE
    assert attrs == {"latitude": 37.5, "longitude": -122.25}
    assert state == "latitude=37.5, longitude=-122.25"


def test_composite_lists_default_fields() -> None:
    """An all-closed ``Doors`` must still list every door as False."""
    kind, _, attrs = values.value_as_native(_value(door_value=vdp.Doors()))
    assert kind == values.VALUE_KIND_COMPOSITE
    assert attrs["DriverFront"] is False
    assert attrs["TrunkRear"] is False


def test_invalid_and_empty() -> None:
    assert values.value_as_native(_value(invalid=True)) == (None, None, {})
    assert values.value_as_native(_value()) == (None, None, {})


def test_same_signal_on_different_arms() -> None:
    """``FastChargerPresent`` is a bool on some cars and an enum on others;
    each sample is classified on its own."""
    as_bool = values.value_as_native(_value(boolean_value=True))
    as_enum = values.value_as_native(
        _value(fast_charger_value=vdp.FastCharger.Value("FastChargerSupercharger"))
    )
    assert as_bool[0] == values.VALUE_KIND_BOOL
    assert as_enum[:2] == (values.VALUE_KIND_ENUM, "supercharger")
