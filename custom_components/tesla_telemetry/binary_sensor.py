"""Binary sensor entities for Tesla Fleet Telemetry.

Covers door/window/lock/charge-port/charging-cable/charging-active/HVAC-on/
sentry-armed/user-presence — i.e. signals whose useful value is on/off.

Entities use ``has_entity_name`` — the vehicle name lives on the HA device
and each entity carries only its functional name.
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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    SIGNAL_BATTERY_HEATER_ON,
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
            DoorBinarySensor(
                coordinator, "front_driver", "DriverFront", "Front driver door"
            ),
            DoorBinarySensor(
                coordinator,
                "front_passenger",
                "PassengerFront",
                "Front passenger door",
            ),
            DoorBinarySensor(
                coordinator, "rear_driver", "DriverRear", "Rear driver door"
            ),
            DoorBinarySensor(
                coordinator,
                "rear_passenger",
                "PassengerRear",
                "Rear passenger door",
            ),
            DoorBinarySensor(
                coordinator,
                "frunk",
                "TrunkFront",
                "Frunk",
                device_class=BinarySensorDeviceClass.OPENING,
            ),
            DoorBinarySensor(
                coordinator,
                "trunk",
                "TrunkRear",
                "Trunk",
                device_class=BinarySensorDeviceClass.OPENING,
            ),
            # Windows
            WindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_FRONT_DRIVER,
                "front_driver",
                "Front driver window",
            ),
            WindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_FRONT_PASSENGER,
                "front_passenger",
                "Front passenger window",
            ),
            WindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_REAR_DRIVER,
                "rear_driver",
                "Rear driver window",
            ),
            WindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_REAR_PASSENGER,
                "rear_passenger",
                "Rear passenger window",
            ),
            # Other body / charging
            LockBinarySensor(coordinator),
            ChargePortBinarySensor(coordinator),
            ChargeCableBinarySensor(coordinator),
            ChargingActiveBinarySensor(coordinator),
            HvacPowerBinarySensor(coordinator),
            SentryArmedBinarySensor(coordinator),
            UserPresentBinarySensor(coordinator),
            BatteryHeaterBinarySensor(coordinator),
        ]
    )


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _BaseTelemetryBinarySensor(BinarySensorEntity, RestoreEntity):
    """Subscribe to one signal and pass each sample to ``_handle``.

    Inherits ``RestoreEntity`` so the last on/off state survives a restart —
    the telemetry stream is push-on-change, so a signal would otherwise read
    ``unknown`` until it next toggles.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _signal_name: str = ""

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        sample = self._coordinator.get(self._signal_name)
        if sample is not None:
            self._handle(sample)
        else:
            last = await self.async_get_last_state()
            if last is not None and last.state in ("on", "off"):
                self._attr_is_on = last.state == "on"
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
class DoorBinarySensor(_BaseTelemetryBinarySensor):
    """One door bit out of the ``DoorState`` composite struct."""

    _signal_name = SIGNAL_DOOR_STATE
    _attr_device_class = BinarySensorDeviceClass.DOOR

    def __init__(
        self,
        coordinator: TeslaTelemetryCoordinator,
        slug: str,
        bit_name: str,
        name: str,
        *,
        device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.DOOR,
    ) -> None:
        super().__init__(coordinator)
        self._bit_name = bit_name
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.vin}_door_{slug}_telemetry"
        self._attr_device_class = device_class

    def _handle(self, sample: SignalSample) -> None:
        doors = value_as_door_state(sample.value)
        if doors is None:
            self._attr_is_on = None
        else:
            self._attr_is_on = doors.get(self._bit_name)


class WindowBinarySensor(_BaseTelemetryBinarySensor):
    _attr_device_class = BinarySensorDeviceClass.WINDOW

    def __init__(
        self,
        coordinator: TeslaTelemetryCoordinator,
        signal: str,
        slug: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._signal_name = signal
        self._attr_name = name
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
) -> type[_BaseTelemetryBinarySensor]:
    class _BS(_BaseTelemetryBinarySensor):
        _signal_name = signal
        _attr_name = name
        _attr_device_class = device_class

        def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
            super().__init__(coordinator)
            self._attr_unique_id = f"{coordinator.vin}_{slug}"

        def _handle(self, sample: SignalSample) -> None:
            self._attr_is_on = extractor(sample.value)

    _BS.__name__ = f"_{slug.title().replace('_', '')}"
    return _BS


class LockBinarySensor(_BaseTelemetryBinarySensor):
    """``Locked`` is reported as a boolean.

    HA's LOCK device class is on=unlocked / off=locked, so we invert.
    """

    _signal_name = SIGNAL_LOCKED
    _attr_name = "Lock"
    _attr_device_class = BinarySensorDeviceClass.LOCK

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_lock_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        locked = value_as_bool(sample.value)
        self._attr_is_on = None if locked is None else not locked


ChargePortBinarySensor = _bool_binary_sensor(
    signal=SIGNAL_CHARGE_PORT_DOOR_OPEN,
    slug="charge_port_door_open_telemetry",
    name="Charge port door",
    device_class=BinarySensorDeviceClass.OPENING,
)


class ChargeCableBinarySensor(_BaseTelemetryBinarySensor):
    """A cable is present whenever ``ChargingCableType`` reports anything
    other than ``Unknown`` / ``SNA`` (Signal Not Available)."""

    _signal_name = SIGNAL_CHARGING_CABLE_TYPE
    _attr_name = "Charge cable"
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


class ChargingActiveBinarySensor(_BaseTelemetryBinarySensor):
    """True while the car is actively pulling charge (Charging or Starting)."""

    _signal_name = SIGNAL_DETAILED_CHARGE_STATE
    _attr_name = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_charging_active_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_is_on = value_charging_active(sample.value)


class HvacPowerBinarySensor(_BaseTelemetryBinarySensor):
    """HVAC is "on" for any state other than Off/Unknown."""

    _signal_name = SIGNAL_HVAC_POWER
    _attr_name = "Climate"
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


class SentryArmedBinarySensor(_BaseTelemetryBinarySensor):
    """Sentry mode is "armed" when the enum reports Armed/Aware/Panic.

    Idle/Off/Unknown are reported as off so the dashboard chip lights up only
    when the car is actively watching its surroundings.
    """

    _signal_name = SIGNAL_SENTRY_MODE
    _attr_name = "Sentry armed"
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


UserPresentBinarySensor = _bool_binary_sensor(
    signal=SIGNAL_DRIVER_SEAT_OCCUPIED,
    slug="user_present_telemetry",
    name="User present",
    device_class=BinarySensorDeviceClass.OCCUPANCY,
)

BatteryHeaterBinarySensor = _bool_binary_sensor(
    signal=SIGNAL_BATTERY_HEATER_ON,
    slug="battery_heater_telemetry",
    name="Battery heater",
    device_class=BinarySensorDeviceClass.HEAT,
)
