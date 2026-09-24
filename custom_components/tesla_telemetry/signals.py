"""Signal catalog + effective-config resolution for tesla_telemetry.

The set of signals streamed from the vehicle and their per-signal minimum
refresh intervals is resolved here from three layers, lowest to highest
precedence:

  1. ``DEFAULT_INTERVALS_SECONDS`` — the built-in default set (const.py).
  2. ``entry.options[CONF_SIGNAL_OVERRIDES]`` — per-signal user overrides from
     the options flow (``{signal: interval}``; 0 disables a default-on signal,
     a positive value overrides the interval).
  3. The active interval preset (``INTERVAL_PRESET_OVERRIDES``).

The full catalog of selectable signals is the Tesla ``Field`` proto enum,
enumerated at runtime so it tracks proto updates with no hardcoded list.

The pure resolver (``resolve_effective_intervals``) and the catalog
(``all_catalog_signals``) deliberately avoid Home Assistant imports so they can
be unit-tested without the HA test harness; the options-flow schema helpers
import voluptuous / HA selectors lazily inside the functions that need them.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from .const import (
    CONF_INTERVAL_PRESET,
    CONF_SIGNAL_OVERRIDES,
    DEFAULT_INTERVALS_SECONDS,
    DEFAULT_NEW_SIGNAL_INTERVAL,
    INTERVAL_PRESET_DEFAULT,
    INTERVAL_PRESET_OVERRIDES,
    SIGNAL_CATEGORIES,
    SIGNAL_INTERVAL_MAX,
)


@lru_cache(maxsize=1)
def all_catalog_signals() -> tuple[str, ...]:
    """Every selectable Tesla signal name, sorted.

    Sourced from the generated ``Field`` proto enum minus the ``Unknown``
    sentinel (value 0). Cached — the enum never changes at runtime.
    """
    # Lazy import keeps the resolver above free of the protobuf dependency.
    from .proto import vehicle_data_pb2

    field_names = vehicle_data_pb2.Field.keys()
    names = [n for n in field_names if n != "Unknown"]
    return tuple(sorted(names))


@lru_cache(maxsize=1)
def _curated_signals() -> frozenset[str]:
    """Signals that belong to a named category — i.e. the curated set."""
    return frozenset(s for sigs in SIGNAL_CATEGORIES.values() for s in sigs)


def _coerce_interval(value: Any) -> int | None:
    """Parse a form/stored value into a non-negative int, else ``None``."""
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return None
    return interval if interval >= 0 else None


def signal_overrides(entry: Any) -> dict[str, int]:
    """The sanitised per-signal overrides stored on ``entry`` (``{sig: int}``)."""
    options = getattr(entry, "options", None) or {}
    raw = options.get(CONF_SIGNAL_OVERRIDES) or {}
    result: dict[str, int] = {}
    for name, value in raw.items():
        interval = _coerce_interval(value)
        if interval is not None:
            result[name] = interval
    return result


def resolve_effective_intervals(entry: Any) -> dict[str, int]:
    """The ``{signal: interval}`` map to push to Tesla for ``entry``.

    Defaults, overlaid by the user's per-signal overrides (0 removes a
    signal), then the active interval preset on top.
    """
    intervals = dict(DEFAULT_INTERVALS_SECONDS)
    for name, interval in signal_overrides(entry).items():
        if interval > 0:
            intervals[name] = interval
        else:
            intervals.pop(name, None)
    preset = (getattr(entry, "data", None) or {}).get(
        CONF_INTERVAL_PRESET, INTERVAL_PRESET_DEFAULT
    )
    for name, interval in INTERVAL_PRESET_OVERRIDES.get(preset, {}).items():
        intervals[name] = interval
    return intervals


def additional_signals(entry: Any) -> list[str]:
    """Overridden signals not part of any curated category — the ones the user
    added from the full catalog. Sorted for stable display."""
    curated = _curated_signals()
    return sorted(s for s in signal_overrides(entry) if s not in curated)


# --------------------------------------------------------------------------
# Options-flow schema helpers.
#
# HA / voluptuous imports are lazy so importing this module (for the resolver
# above) never requires Home Assistant to be installed.
# --------------------------------------------------------------------------
def build_options_schema(entry: Any) -> Any:
    """Voluptuous schema for the options form.

    One collapsible section per curated category (a number field per signal,
    0 = disabled), an "Additional signals" section for catalog signals the
    user has added, an "Add signals" picker over the rest of the catalog, and
    the estimated-cost rate.
    """
    import voluptuous as vol

    from homeassistant import data_entry_flow
    from homeassistant.helpers import selector

    from .const import (
        CONF_COST_PER_MILLION_SIGNALS,
        DEFAULT_COST_PER_MILLION_SIGNALS,
    )

    overrides = signal_overrides(entry)

    def _section(signals: list[str], default_for: Any) -> Any:
        fields: dict[Any, Any] = {}
        for signal in signals:
            current = overrides.get(signal, default_for(signal))
            fields[vol.Optional(signal, default=current)] = vol.All(
                vol.Coerce(int), vol.Range(min=0, max=SIGNAL_INTERVAL_MAX)
            )
        return data_entry_flow.section(vol.Schema(fields), {"collapsed": True})

    schema: dict[Any, Any] = {}
    for category, signals in SIGNAL_CATEGORIES.items():
        schema[vol.Required(category)] = _section(
            signals, lambda s: DEFAULT_INTERVALS_SECONDS.get(s, 0)
        )

    extras = additional_signals(entry)
    if extras:
        schema[vol.Required("additional")] = _section(
            extras, lambda s: DEFAULT_NEW_SIGNAL_INTERVAL
        )

    addable = [
        s
        for s in all_catalog_signals()
        if s not in _curated_signals() and s not in overrides
    ]
    schema[vol.Required("add_signals")] = data_entry_flow.section(
        vol.Schema(
            {
                vol.Optional("signals", default=list): selector.SelectSelector(
                    # ``addable`` is already sorted by all_catalog_signals().
                    selector.SelectSelectorConfig(
                        options=addable,
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                        custom_value=False,
                    )
                ),
            }
        ),
        {"collapsed": True},
    )

    current_cost = (getattr(entry, "options", None) or {}).get(
        CONF_COST_PER_MILLION_SIGNALS, DEFAULT_COST_PER_MILLION_SIGNALS
    )
    schema[vol.Required("cost")] = data_entry_flow.section(
        vol.Schema(
            {
                vol.Optional(
                    CONF_COST_PER_MILLION_SIGNALS, default=current_cost
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            }
        ),
        {"collapsed": True},
    )

    return vol.Schema(schema)


def parse_options_input(entry: Any, user_input: dict[str, Any]) -> dict[str, Any]:
    """Turn submitted options-form values into the stored options dict.

    Only per-signal values that deviate from the default are stored, so an
    untouched form leaves ``signal_overrides`` empty (== default config). A
    value of 0 is a real override that disables a default-on signal. Signals
    chosen in the "Add signals" picker are added at
    ``DEFAULT_NEW_SIGNAL_INTERVAL`` and drop back out if later set to 0.
    """
    from .const import (
        CONF_COST_PER_MILLION_SIGNALS,
        DEFAULT_COST_PER_MILLION_SIGNALS,
    )

    new_overrides: dict[str, int] = {}

    for key in (*SIGNAL_CATEGORIES.keys(), "additional"):
        section = user_input.get(key) or {}
        for signal, value in section.items():
            interval = _coerce_interval(value)
            if interval is None:
                continue
            interval = min(interval, SIGNAL_INTERVAL_MAX)
            default = DEFAULT_INTERVALS_SECONDS.get(signal, 0)
            if interval != default:
                new_overrides[signal] = interval

    add_section = user_input.get("add_signals") or {}
    for signal in add_section.get("signals") or []:
        if signal in DEFAULT_INTERVALS_SECONDS or signal in new_overrides:
            continue
        new_overrides[signal] = DEFAULT_NEW_SIGNAL_INTERVAL

    cost_section = user_input.get("cost") or {}
    cost = cost_section.get(CONF_COST_PER_MILLION_SIGNALS)
    if cost is None:
        cost = (getattr(entry, "options", None) or {}).get(
            CONF_COST_PER_MILLION_SIGNALS, DEFAULT_COST_PER_MILLION_SIGNALS
        )

    return {
        CONF_SIGNAL_OVERRIDES: new_overrides,
        CONF_COST_PER_MILLION_SIGNALS: float(cost),
    }
