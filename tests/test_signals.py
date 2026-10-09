"""Tests for signal-config resolution (``signals.py``).

The critical guarantee is that an entry with no overrides resolves to *exactly*
today's ``DEFAULT_INTERVALS_SECONDS`` — i.e. adding UI configurability changed
nothing about what gets pushed by default. The rest cover the override, disable,
preset-layering, and options-form parse paths.

``signals.py`` imports only ``const.py`` (pure) at module load — its proto and
Home Assistant dependencies are lazy — so both are loaded directly by path under
a synthetic package, with no Home Assistant installed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

# --- Load const.py + signals.py in isolation under a synthetic package -------
# signals.py uses ``from .const import ...``, so it needs a package context;
# we build one whose __path__ points at the integration dir without running its
# HA-importing __init__.py.
_PKG = "tesla_telemetry_isolated"
_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)


def _load_isolated() -> tuple[ModuleType, ModuleType]:
    if f"{_PKG}.signals" in sys.modules:
        return sys.modules[f"{_PKG}.const"], sys.modules[f"{_PKG}.signals"]
    pkg = ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    for name in ("const", "signals"):
        spec = importlib.util.spec_from_file_location(
            f"{_PKG}.{name}", _DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{_PKG}.const"], sys.modules[f"{_PKG}.signals"]


const, signals = _load_isolated()


def _entry(options: dict | None = None, data: dict | None = None) -> SimpleNamespace:
    """A minimal stand-in for a HA ConfigEntry (only .options/.data are read)."""
    return SimpleNamespace(options=options or {}, data=data or {})


# ---------------------------------------------------------------------------
# resolve_effective_intervals
# ---------------------------------------------------------------------------
def test_no_overrides_equals_current_default_config() -> None:
    """The headline guarantee: empty options ⇒ byte-for-byte today's config."""
    result = signals.resolve_effective_intervals(_entry())
    assert result == const.DEFAULT_INTERVALS_SECONDS
    assert result is not const.DEFAULT_INTERVALS_SECONDS  # must be a copy


def test_zero_disables_a_default_signal() -> None:
    entry = _entry({const.CONF_SIGNAL_OVERRIDES: {"Location": 0}})
    result = signals.resolve_effective_intervals(entry)
    assert "Location" not in result
    # Untouched signals are unaffected.
    assert result["VehicleSpeed"] == const.DEFAULT_INTERVALS_SECONDS["VehicleSpeed"]


def test_positive_override_changes_interval() -> None:
    entry = _entry({const.CONF_SIGNAL_OVERRIDES: {"VehicleSpeed": 2}})
    assert signals.resolve_effective_intervals(entry)["VehicleSpeed"] == 2


def test_added_catalog_signal_appears_enabled() -> None:
    entry = _entry({const.CONF_SIGNAL_OVERRIDES: {"Odometer": 60}})
    result = signals.resolve_effective_intervals(entry)
    assert result["Odometer"] == 60
    assert "Odometer" not in const.DEFAULT_INTERVALS_SECONDS  # truly an add


def test_high_rate_preset_layers_on_top() -> None:
    entry = _entry(data={const.CONF_INTERVAL_PRESET: const.INTERVAL_PRESET_HIGH_RATE})
    result = signals.resolve_effective_intervals(entry)
    assert result["Location"] == 1
    assert result["VehicleSpeed"] == 1


def test_bad_override_values_are_ignored() -> None:
    entry = _entry({const.CONF_SIGNAL_OVERRIDES: {"Location": "nope", "Soc": None}})
    result = signals.resolve_effective_intervals(entry)
    # Unparseable overrides fall through to the defaults.
    assert result["Location"] == const.DEFAULT_INTERVALS_SECONDS["Location"]
    assert result["Soc"] == const.DEFAULT_INTERVALS_SECONDS["Soc"]


# ---------------------------------------------------------------------------
# Category catalog invariant
# ---------------------------------------------------------------------------
def test_every_default_signal_is_in_exactly_one_category() -> None:
    counts: dict[str, int] = {}
    for sigs in const.SIGNAL_CATEGORIES.values():
        for sig in sigs:
            counts[sig] = counts.get(sig, 0) + 1
    # No signal appears in two categories.
    assert [s for s, c in counts.items() if c > 1] == []
    # Every default signal is placed in a category (so it's editable in the UI).
    assert set(const.DEFAULT_INTERVALS_SECONDS) - set(counts) == set()
    # And categories don't list phantom signals absent from the defaults.
    assert set(counts) - set(const.DEFAULT_INTERVALS_SECONDS) == set()


# ---------------------------------------------------------------------------
# parse_options_input — only deviations from the default are stored
# ---------------------------------------------------------------------------
def _form_from(effective: dict[str, int]) -> dict:
    """Build a submitted-form dict (nested sections) from a signal→interval map."""
    user_input: dict = {}
    for category, sigs in const.SIGNAL_CATEGORIES.items():
        user_input[category] = {
            s: effective.get(s, const.DEFAULT_INTERVALS_SECONDS.get(s, 0))
            for s in sigs
        }
    user_input["add_signals"] = {"signals": []}
    user_input["cost"] = {}
    return user_input


def test_parse_untouched_form_stores_no_overrides() -> None:
    entry = _entry()
    parsed = signals.parse_options_input(entry, _form_from({}))
    assert parsed[const.CONF_SIGNAL_OVERRIDES] == {}


def test_parse_stores_only_changed_signals() -> None:
    entry = _entry()
    form = _form_from({})
    form["driving"]["VehicleSpeed"] = 2  # change one
    form["driving"]["Location"] = 0  # disable one
    parsed = signals.parse_options_input(entry, form)
    assert parsed[const.CONF_SIGNAL_OVERRIDES] == {"VehicleSpeed": 2, "Location": 0}


def test_parse_add_signals_adds_at_default_interval() -> None:
    entry = _entry()
    form = _form_from({})
    form["add_signals"] = {"signals": ["Odometer"]}
    parsed = signals.parse_options_input(entry, form)
    assert parsed[const.CONF_SIGNAL_OVERRIDES]["Odometer"] == (
        const.DEFAULT_NEW_SIGNAL_INTERVAL
    )


def test_parse_round_trips_through_resolver() -> None:
    """Parse a changed form, feed it back as options, confirm the resolver
    yields the edited config."""
    entry = _entry()
    form = _form_from({})
    form["driving"]["VehicleSpeed"] = 3
    parsed = signals.parse_options_input(entry, form)
    result = signals.resolve_effective_intervals(_entry(parsed))
    assert result["VehicleSpeed"] == 3


# ---------------------------------------------------------------------------
# Full catalog enumeration (needs protobuf)
# ---------------------------------------------------------------------------
def test_all_catalog_signals_from_proto() -> None:
    pytest.importorskip("google.protobuf")
    catalog = signals.all_catalog_signals()
    assert "Unknown" not in catalog
    assert "VehicleSpeed" in catalog
    assert "Odometer" in catalog
    # Every curated default is a real catalog signal.
    assert set(const.DEFAULT_INTERVALS_SECONDS) <= set(catalog)
    assert len(catalog) > 200  # the full Field enum, not just the curated set


# ---------------------------------------------------------------------------
# Generic entities for catalog-added signals
# ---------------------------------------------------------------------------
def test_generic_entity_signals_only_enabled_additions() -> None:
    entry = _entry(
        {
            const.CONF_SIGNAL_OVERRIDES: {
                "Odometer": 60,  # added from the catalog
                "SentryMode": 0,  # stale zero — not an entity
                "Location": 2,  # curated retune — keeps its own entity
                "VehicleSpeed": 0,  # curated disable
            }
        }
    )
    assert signals.generic_entity_signals(entry) == ["Odometer"]


def test_no_overrides_means_no_generic_entities() -> None:
    assert signals.generic_entity_signals(_entry()) == []


def test_generic_unique_id_round_trip() -> None:
    uid = signals.generic_unique_id("5YJSA1E2XLF000001", "SentryMode")
    assert uid == "5YJSA1E2XLF000001_signal_SentryMode"
    assert (
        signals.signal_from_generic_unique_id("5YJSA1E2XLF000001", uid)
        == "SentryMode"
    )


@pytest.mark.parametrize(
    "unique_id",
    [
        "5YJSA1E2XLF000001_vehicle_speed_telemetry",  # curated entity
        "7SAYGDEE0PF000002_signal_SentryMode",  # another vehicle
        "5YJSA1E2XLF000001_signal_",  # empty signal
    ],
)
def test_signal_from_generic_unique_id_rejects_others(unique_id: str) -> None:
    assert signals.signal_from_generic_unique_id("5YJSA1E2XLF000001", unique_id) is None


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ("ACChargingPower", "AC charging power"),
        ("DiStatorTempF", "Di stator temp F"),
        ("SentryMode", "Sentry mode"),
        ("Odometer", "Odometer"),
        ("BMSState", "BMS state"),
    ],
)
def test_humanize_signal_name(signal: str, expected: str) -> None:
    assert signals.humanize_signal_name(signal) == expected
