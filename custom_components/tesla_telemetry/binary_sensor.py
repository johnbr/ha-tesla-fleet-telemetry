"""Binary sensor entities for Tesla Fleet Telemetry.

Covers door/window/lock/charge-port/charging-cable/charging-active/HVAC-on/
sentry-armed/user-presence — i.e. signals whose useful value is on/off.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SIGNAL_CHARGE_PORT_DOOR_OPEN,
    SIGNAL_CHARGING_CABLE_TYPE,
    SIGNAL_DETAILED_CHARGE_STATE,
    SIGNAL_DOOR_STATE,
    SIGNAL_DRIVER_SEAT_OCCUPIED,
    SIGNAL_HVAC_POWER,
    SIGNAL_LOCKED,
    SIGNAL_SENTRY_MODE,
    SIGNAL_WINDOW_FRONT_DRIVER,
    SIGNAL_WINDOW_FRONT_PASSENGER,
    SIGNAL_WINDOW_REAR_DRIVER,
    SIGNAL_WINDOW_REAR_PASSENGER,
)
from .coordinator import (
    SignalSample,
    TeslaTelemetryCoordinator,
    signal_dispatcher_topic,
)
from .values import (
    value_as_bool,
    value_as_door_state,
    value_as_enum_name,
    value_charging_active,
    value_is_window_open,
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
            # Doors (each derives one bool from the Doors composite struct)
            RoadrunnerDoorBinarySensor(
                coordinator, "front_driver", "DriverFront", "Front Driver"
            ),
            RoadrunnerDoorBinarySensor(
                coordinator, "front_passenger", "PassengerFront", "Front Passenger"
            ),
            RoadrunnerDoorBinarySensor(
                coordinator, "rear_driver", "DriverRear", "Rear Driver"
            ),
            RoadrunnerDoorBinarySensor(
                coordinator, "rear_passenger", "PassengerRear", "Rear Passenger"
            ),
            RoadrunnerDoorBinarySensor(
                coordinator,
                "frunk",
                "TrunkFront",
                "Frunk",
                device_class=BinarySensorDeviceClass.OPENING,
            ),
            RoadrunnerDoorBinarySensor(
                coordinator,
                "trunk",
                "TrunkRear",
                "Trunk",
                device_class=BinarySensorDeviceClass.OPENING,
            ),
            # Windows
            RoadrunnerWindowBinarySensor(
                coordinator, SIGNAL_WINDOW_FRONT_DRIVER, "front_driver", "Front Driver"
            ),
            RoadrunnerWindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_FRONT_PASSENGER,
                "front_passenger",
                "Front Passenger",
            ),
            RoadrunnerWindowBinarySensor(
                coordinator, SIGNAL_WINDOW_REAR_DRIVER, "rear_driver", "Rear Driver"
            ),
            RoadrunnerWindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_REAR_PASSENGER,
                "rear_passenger",
                "Rear Passenger",
            ),
            # Other body / charging
            RoadrunnerLockBinarySensor(coordinator),
            RoadrunnerChargePortBinarySensor(coordinator),
            RoadrunnerChargeCableBinarySensor(coordinator),
            RoadrunnerChargingActiveBinarySensor(coordinator),
            RoadrunnerHvacPowerBinarySensor(coordinator),
            RoadrunnerSentryArmedBinarySensor(coordinator),
            RoadrunnerUserPresentBinarySensor(coordinator),
        ]
    )


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _BaseRoadrunnerBinarySensor(BinarySensorEntity):
    """Subscribe to one signal and pass each sample to ``_handle``."""

    _attr_should_poll = False
    _signal_name: str = ""

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.vin)},
            manufacturer="Tesla",
            name="Roadrunner",
        )

    async def async_added_to_hass(self) -> None:
        sample = self._coordinator.get(self._signal_name)
        if sample is not None:
            self._handle(sample)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_dispatcher_topic(
                    self._coordinator.vin, self._signal_name
                ),
                self._on_sample,
            )
        )

    @callback
    def _on_sample(self, sample: SignalSample) -> None:
        self._handle(sample)
        if self.hass is not None:
            self.async_write_ha_state()

    def _handle(self, sample: SignalSample) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Doors / windows
# ---------------------------------------------------------------------------
class RoadrunnerDoorBinarySensor(_BaseRoadrunnerBinarySensor):
    """One door bit out of the ``DoorState`` composite struct."""

    _signal_name = SIGNAL_DOOR_STATE
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(
        self,
        coordinator: TeslaTelemetryCoordinator,
        slug: str,
        bit_name: str,
        label: str,
        *,
        device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.DOOR,
    ) -> None:
        super().__init__(coordinator)
        self._bit_name = bit_name
        self._attr_name = f"Roadrunner {label} Door Telemetry"
        self._attr_unique_id = f"{coordinator.vin}_door_{slug}_telemetry"
        self._attr_device_class = device_class

    def _handle(self, sample: SignalSample) -> None:
        doors = value_as_door_state(sample.value)
        if doors is None:
            self._attr_is_on = None
        else:
            self._attr_is_on = doors.get(self._bit_name)


class RoadrunnerWindowBinarySensor(_BaseRoadrunnerBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.WINDOW

    def __init__(
        self,
        coordinator: TeslaTelemetryCoordinator,
        signal: str,
        slug: str,
        label: str,
    ) -> None:
        super().__init__(coordinator)
        self._signal_name = signal
        self._attr_name = f"Roadrunner {label} Window Telemetry"
        self._attr_unique_id = f"{coordinator.vin}_window_{slug}_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_is_on = value_is_window_open(sample.value)


# ---------------------------------------------------------------------------
# Misc body / security / charging
# ---------------------------------------------------------------------------
def _bool_binary_sensor(
    *,
    signal: str,
    slug: str,
    name: str,
    device_class: BinarySensorDeviceClass | None = None,
    extractor: Callable[[Any], bool | None] = value_as_bool,
) -> type[_BaseRoadrunnerBinarySensor]:
    class _BS(_BaseRoadrunnerBinarySensor):
        _signal_name = signal
        _attr_name = name
        _attr_device_class = device_class

        def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
            super().__init__(coordinator)
            self._attr_unique_id = f"{coordinator.vin}_{slug}"

        def _handle(self, sample: SignalSample) -> None:
            self._attr_is_on = extractor(sample.value)

    _BS.__name__ = f"_Roadrunner{slug.title().replace('_', '')}"
    return _BS


class RoadrunnerLockBinarySensor(_BaseRoadrunnerBinarySensor):
    """``Locked`` is reported as a boolean.

    HA's LOCK device class is on=unlocked / off=locked, so we invert.
    """

    _signal_name = SIGNAL_LOCKED
    _attr_name = "Roadrunner Lock Telemetry"
    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_lock_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        locked = value_as_bool(sample.value)
        self._attr_is_on = None if locked is None else not locked


RoadrunnerChargePortBinarySensor = _bool_binary_sensor(
    signal=SIGNAL_CHARGE_PORT_DOOR_OPEN,
    slug="charge_port_door_open_telemetry",
    name="Roadrunner Charge Port Door Telemetry",
    device_class=BinarySensorDeviceClass.OPENING,
)


class RoadrunnerChargeCableBinarySensor(_BaseRoadrunnerBinarySensor):
    """A cable is present whenever ``ChargingCableType`` reports anything
    other than ``Unknown`` / ``SNA`` (Signal Not Available)."""

    _signal_name = SIGNAL_CHARGING_CABLE_TYPE
    _attr_name = "Roadrunner Charge Cable Telemetry"
    _attr_device_class = BinarySensorDeviceClass.PLUG

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_charge_cable_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        name = value_as_enum_name(sample.value)
        if name is None:
            self._attr_is_on = None
            return
        self._attr_is_on = name not in ("CableTypeUnknown", "CableTypeSNA")


class RoadrunnerChargingActiveBinarySensor(_BaseRoadrunnerBinarySensor):
    """True while the car is actively pulling charge (Charging or Starting)."""

    _signal_name = SIGNAL_DETAILED_CHARGE_STATE
    _attr_name = "Roadrunner Charging Active Telemetry"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_charging_active_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_is_on = value_charging_active(sample.value)


class RoadrunnerHvacPowerBinarySensor(_BaseRoadrunnerBinarySensor):
    """HVAC is "on" for any state other than Off/Unknown."""

    _signal_name = SIGNAL_HVAC_POWER
    _attr_name = "Roadrunner Climate Active Telemetry"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_hvac_power_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        name = value_as_enum_name(sample.value)
        if name is None:
            self._attr_is_on = None
            return
        self._attr_is_on = name not in ("HvacPowerStateOff", "HvacPowerStateUnknown")


class RoadrunnerSentryArmedBinarySensor(_BaseRoadrunnerBinarySensor):
    """Sentry mode is "armed" when the enum reports Armed/Aware/Panic.

    Idle/Off/Unknown are reported as off so the dashboard chip lights up only
    when the car is actively watching its surroundings.
    """

    _signal_name = SIGNAL_SENTRY_MODE
    _attr_name = "Roadrunner Sentry Armed Telemetry"
    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_sentry_armed_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        name = value_as_enum_name(sample.value)
        if name is None:
            # Sentry status sometimes arrives as a plain bool on older firmware
            self._attr_is_on = value_as_bool(sample.value)
            return
        self._attr_is_on = name in (
            "SentryModeStateArmed",
            "SentryModeStateAware",
            "SentryModeStatePanic",
        )


RoadrunnerUserPresentBinarySensor = _bool_binary_sensor(
    signal=SIGNAL_DRIVER_SEAT_OCCUPIED,
    slug="user_present_telemetry",
    name="Roadrunner User Present Telemetry",
    device_class=BinarySensorDeviceClass.OCCUPANCY,
)
