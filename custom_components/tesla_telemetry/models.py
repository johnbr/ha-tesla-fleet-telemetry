"""Runtime data for the Tesla Fleet Telemetry integration.

Kept in its own module so the platforms and the service handlers can import it
without a circular import on the package ``__init__``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry, ConfigEntryState

if TYPE_CHECKING:
    from .coordinator import TeslaTelemetryCoordinator
    from .tesla_api import TeslaApi


@dataclass
class TeslaTelemetryRuntimeData:
    """Everything one config entry (one vehicle) needs while it is loaded."""

    coordinator: TeslaTelemetryCoordinator
    vin: str
    api: TeslaApi
    #: Whether the TeslaMate bridge is serving this vehicle's stream.
    teslamate: bool
    #: The signal → interval map last pushed to the car, so the options-update
    #: listener can skip a re-push that would change nothing.
    pushed_intervals: dict[str, int] = field(default_factory=dict)


type TeslaTelemetryConfigEntry = ConfigEntry[TeslaTelemetryRuntimeData]


def runtime_data_if_loaded(
    entry: ConfigEntry,
) -> TeslaTelemetryRuntimeData | None:
    """The entry's runtime data, or ``None`` when it is not loaded.

    ``ConfigEntry.runtime_data`` is assigned by ``async_setup_entry`` and
    removed again on unload, so anything that can run outside a loaded entry —
    a service action, a diagnostics download, an options-update listener — has
    to tolerate its absence rather than assume setup has run.
    """
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data
