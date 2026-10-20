"""Tests for the proxy-secret checks in ``const.py``.

The secret is the only authentication on the WebSocket endpoint, which HA's
own port serves as well as nginx, so these guard both how strong a configured
secret must be and how a presented one is compared. ``const.py`` is
stdlib-only and loaded in isolation, as ``test_const_regions`` does.
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
    if f"{_PKG}.const" in sys.modules:
        return sys.modules[f"{_PKG}.const"]
    pkg = ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.const", _DIR / "const.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.const"] = module
    spec.loader.exec_module(module)
    return sys.modules[f"{_PKG}.const"]


const = _load_isolated()

GENERATED = "a" * 64  # the config flow's default is 64 hex characters


def test_generated_length_secret_is_strong() -> None:
    assert const.proxy_secret_is_strong(GENERATED)


def test_minimum_length_is_strong_and_one_less_is_not() -> None:
    n = const.MIN_PROXY_SECRET_LENGTH
    assert const.proxy_secret_is_strong("x" * n)
    assert not const.proxy_secret_is_strong("x" * (n - 1))


@pytest.mark.parametrize("secret", ["", "   ", "changeme", "secret123"])
def test_short_or_blank_secrets_are_weak(secret: str) -> None:
    assert not const.proxy_secret_is_strong(secret)


def test_surrounding_whitespace_does_not_count_toward_length() -> None:
    n = const.MIN_PROXY_SECRET_LENGTH
    assert not const.proxy_secret_is_strong(" " * 10 + "x" * (n - 1) + " " * 10)


def test_matching_secret_is_accepted() -> None:
    assert const.proxy_secret_matches(GENERATED, GENERATED)


def test_wrong_secret_is_refused() -> None:
    assert not const.proxy_secret_matches("b" * 64, GENERATED)


def test_prefix_of_secret_is_refused() -> None:
    assert not const.proxy_secret_matches(GENERATED[:-1], GENERATED)


@pytest.mark.parametrize("configured", [None, ""])
def test_unknown_vehicle_or_empty_secret_never_matches(configured) -> None:
    # An unknown VIN has no configured secret; an empty presented header
    # must not match an empty configured one either.
    assert not const.proxy_secret_matches("", configured)
    assert not const.proxy_secret_matches(GENERATED, configured)


def test_non_ascii_header_is_refused_not_raised() -> None:
    # hmac.compare_digest raises TypeError on non-ASCII str; comparing bytes
    # turns a junk header into a plain refusal instead of a 500.
    assert not const.proxy_secret_matches("sécret" * 8, GENERATED)
