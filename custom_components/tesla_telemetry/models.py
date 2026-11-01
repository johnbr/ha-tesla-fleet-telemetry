"""Runtime data for the Tesla Fleet Telemetry integration.

Kept in its own module so the platforms and the service handlers can import it
without a circular import on the package ``__init__``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

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


def runtime_data_if_available(
    entry: ConfigEntry,
) -> TeslaTelemetryRuntimeData | None:
    """The entry's runtime data, or ``None`` before setup has assigned it.

    Keyed off the attribute rather than ``entry.state`` on purpose.
    ``runtime_data`` is assigned part-way through ``async_setup_entry``, but
    core only flips the state to ``LOADED`` once that function returns — and a
    callback registered with ``async_at_started`` while Home Assistant is
    already running fires as an *eager* task, so it can run synchronously
    inside ``async_setup_entry`` and see ``SETUP_IN_PROGRESS`` with the runtime
    data already in place. Testing the state there would make the first
    auto-resync check skip and wait a full day for its interval.

    ``runtime_data`` is removed again on unload, so the attribute is also the
    honest answer for anything that can run outside a loaded entry: a service
    action, a diagnostics download, an options-update listener.
    """
    return getattr(entry, "runtime_data", None)
