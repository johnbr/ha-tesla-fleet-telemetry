"""Device tracker entities for Tesla Fleet Telemetry.

Two trackers are exposed per vehicle:

  * Location — the car's live GPS location, state derived from zone (HA
    computes home / not_home / <zone> from lat/lon).
  * Route    — the active in-car nav destination. State is the destination
    name string; lat/lon attrs point at the destination, not the car.

Both subscribe to dispatcher signals from `TeslaTelemetryCoordinator` and
use ``has_entity_name`` so the vehicle name comes from the HA device.
"""
from __future__ import annotations

import logging

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_HOME, STATE_NOT_HOME
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    SIGNAL_DESTINATION_LOCATION,
    SIGNAL_DESTINATION_NAME,
    SIGNAL_LOCATION,
)
from .coordinator import (
    SignalSample,
    TeslaTelemetryCoordinator,
    signal_dispatcher_topic,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TeslaTelemetryCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities(
        [
            LocationTracker(coordinator),
            RouteTracker(coordinator),
        ]
    )


class _BaseTelemetryTracker(TrackerEntity, RestoreEntity):
    """Shared plumbing — dispatcher subscription, lat/lon storage.

    Inherits ``RestoreEntity`` so the last position survives a restart — the
    location signal is push-on-change, so a parked car would otherwise report
    an unknown position until it next moves.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_source_type = SourceType.GPS

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        self._coordinator = coordinator
        self._latitude: float | None = None
        self._longitude: float | None = None
        self._attr_device_info = coordinator.device_info

    @property
    def latitude(self) -> float | None:
        return self._latitude

    @property
    def longitude(self) -> float | None:
        return self._longitude

    def _subscribe(self, signal_name: str, handler) -> None:
        """Connect `handler` to the coordinator's dispatch topic and seed
        it with any sample already on file."""
        sample = self._coordinator.get(signal_name)
        if sample is not None:
            handler(sample)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_dispatcher_topic(self._coordinator.vin, signal_name),
                handler,
            )
        )

    async def _async_restore_location(self) -> State | None:
        """Restore the last lat/lon from saved attributes; returns the last
        state so subclasses can also recover their state string."""
        last = await self.async_get_last_state()
        if last is None:
            return None
        lat = last.attributes.get("latitude")
        lon = last.attributes.get("longitude")
        if lat is not None and lon is not None:
            self._latitude = lat
            self._longitude = lon
        return last


class LocationTracker(_BaseTelemetryTracker):
    _attr_name = "Location"

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_location_telemetry"

    async def async_added_to_hass(self) -> None:
        if self._coordinator.get(SIGNAL_LOCATION) is None:
            await self._async_restore_location()
        self._subscribe(SIGNAL_LOCATION, self._on_location)

    @callback
    def _on_location(self, sample: SignalSample) -> None:
        if sample.value.HasField("location_value"):
            lv = sample.value.location_value
            self._latitude = lv.latitude
            self._longitude = lv.longitude
        else:
            self._latitude = None
            self._longitude = None
        if self.hass is not None:
            self.async_write_ha_state()


class RouteTracker(_BaseTelemetryTracker):
    _attr_name = "Route"

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_route_telemetry"
        self._destination_name: str | None = None

    @property
    def location_name(self) -> str | None:
        """Returning a non-None value here makes HA use it as the state
        directly, bypassing zone-from-lat/lon resolution: the state is the
        in-car navigation destination name."""
        return self._destination_name

    async def async_added_to_hass(self) -> None:
        if (
            self._coordinator.get(SIGNAL_DESTINATION_NAME) is None
            and self._coordinator.get(SIGNAL_DESTINATION_LOCATION) is None
        ):
            last = await self._async_restore_location()
            # Restore the destination name only when the saved state was a real
            # place name, not a computed zone (home/not_home) or empty marker.
            if last is not None and last.state not in (
                STATE_HOME,
                STATE_NOT_HOME,
                "unknown",
                "unavailable",
                "",
            ):
                self._destination_name = last.state
        self._subscribe(SIGNAL_DESTINATION_NAME, self._on_name)
        self._subscribe(SIGNAL_DESTINATION_LOCATION, self._on_location)

    @callback
    def _on_name(self, sample: SignalSample) -> None:
        if sample.value.HasField("string_value") and sample.value.string_value:
            self._destination_name = sample.value.string_value
        else:
            self._destination_name = None
        if self.hass is not None:
            self.async_write_ha_state()

    @callback
    def _on_location(self, sample: SignalSample) -> None:
        if sample.value.HasField("location_value"):
            lv = sample.value.location_value
            self._latitude = lv.latitude
            self._longitude = lv.longitude
        else:
            self._latitude = None
            self._longitude = None
        if self.hass is not None:
            self.async_write_ha_state()
