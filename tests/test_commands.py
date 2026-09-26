"""Tests for the vehicle-command bodies and result checking (``commands.py``).

``commands.py`` is stdlib-only, so it is loaded directly by path under a
synthetic package, with no Home Assistant installed (as ``test_const_regions``
does for ``const.py``).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PKG = "tesla_telemetry_isolated"
_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)


def _load_isolated() -> ModuleType:
    if f"{_PKG}.commands" in sys.modules:
        return sys.modules[f"{_PKG}.commands"]
    if _PKG not in sys.modules:
        pkg = ModuleType(_PKG)
        pkg.__path__ = [str(_DIR)]
        sys.modules[_PKG] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.commands", _DIR / "commands.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.commands"] = module
    spec.loader.exec_module(module)
    return module


commands = _load_isolated()


def test_navigation_request_body_matches_share_to_car() -> None:
    body = commands.navigation_request_body(
        "  1 Tesla Road, Austin, TX  ", timestamp_ms=1234
    )
    assert body == {
        "type": "share_ext_content_raw",
        "locale": "en-US",
        "timestamp_ms": 1234,
        "value": {"android.intent.extra.TEXT": "1 Tesla Road, Austin, TX"},
    }


def test_navigation_request_body_stamps_current_time() -> None:
    body = commands.navigation_request_body("Somewhere")
    assert isinstance(body["timestamp_ms"], int)
    assert body["timestamp_ms"] > 1_700_000_000_000


def test_navigation_request_body_rejects_blank() -> None:
    with pytest.raises(ValueError):
        commands.navigation_request_body("   ")


def test_navigation_gps_body() -> None:
    assert commands.navigation_gps_body("30.2222", -97.6167, 1) == {
        "lat": 30.2222,
        "lon": -97.6167,
        "order": 1,
    }


@pytest.mark.parametrize(
    ("lat", "lon", "order"),
    [(91, 0, 0), (0, -181, 0), (0, 0, 4), (0, 0, -1)],
)
def test_navigation_gps_body_rejects_out_of_range(lat, lon, order) -> None:
    with pytest.raises(ValueError):
        commands.navigation_gps_body(lat, lon, order)


def test_check_command_result_ok() -> None:
    assert commands.check_command_result(
        {"response": {"result": True, "reason": ""}}
    ) == {"result": True, "reason": ""}


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"response": {"result": False, "reason": "not_supported"}}, "not_supported"),
        ({"response": {"result": False}}, ""),
        ({"response": None}, None),
        ({}, None),
        ("garbage", None),
    ],
)
def test_check_command_result_refused(data, reason) -> None:
    with pytest.raises(commands.CommandRefused) as info:
        commands.check_command_result(data)
    if reason is not None:
        assert info.value.reason == reason


def test_navigation_request_body_locale() -> None:
    body = commands.navigation_request_body("Brandenburger Tor", locale="de-DE")
    assert body["locale"] == "de-DE"
    assert commands.navigation_request_body("x")["locale"] == commands.DEFAULT_LOCALE
