"""Diagnostics support for Tesla Fleet Telemetry.

Two kinds of thing are redacted before a download leaves the instance:

* **Secrets** — the OAuth token bundle, the partner private key and the proxy
  shared secret.
* **Identifiers** — the VIN and Tesla's numeric vehicle id. A diagnostics file
  is meant to be attached to a public issue, and either one identifies a real
  car, so they are masked wherever they appear: in the entry data, in the
  entry title (the config flow appends `` (VIN)``) and in the runtime block.

What is left — region, hostname, which signals are configured and how often,
and the live counters — is what actually helps debug a stuck stream.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_PRIVATE_KEY_PEM,
    CONF_PROXY_SECRET,
    CONF_VEHICLE_ID,
    CONF_VIN,
)
from .models import TeslaTelemetryConfigEntry, runtime_data_if_loaded

REDACTED = "**REDACTED**"

TO_REDACT = {
    CONF_PRIVATE_KEY_PEM,
    CONF_PROXY_SECRET,
    CONF_VEHICLE_ID,
    CONF_VIN,
    "token",
    "access_token",
    "refresh_token",
    "id_token",
}


def _without_vin(text: str, vin: str) -> str:
    """``text`` with any occurrence of ``vin`` masked.

    The VIN is not a dict key in the entry title, so ``async_redact_data``
    cannot reach it.
    """
    return text.replace(vin, REDACTED) if vin else text


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TeslaTelemetryConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    vin = entry.data.get(CONF_VIN, "")

    diagnostics: dict[str, Any] = {
        "entry": {
            "title": _without_vin(entry.title, vin),
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
    }

    runtime = runtime_data_if_loaded(entry)
    if runtime is None:
        diagnostics["runtime"] = "entry is not loaded"
        return diagnostics

    coordinator = runtime.coordinator
    diagnostics["runtime"] = {
        "vin": _without_vin(runtime.vin, vin),
        "teslamate_bridge": runtime.teslamate,
        "signals_since_start": coordinator.signals_since_start,
        "lifetime_signals": coordinator.lifetime_signals,
        "pushed_intervals": dict(runtime.pushed_intervals),
        "effective_intervals": dict(coordinator.effective_intervals),
        "signal_counts": dict(coordinator.signal_counts.most_common()),
        "stale_signals": sorted(
            name
            for name in coordinator.effective_intervals
            if coordinator.is_stale(name)
        ),
    }

    return diagnostics
