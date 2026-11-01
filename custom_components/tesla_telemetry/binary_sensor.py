"""Binary sensor entities for Tesla Fleet Telemetry.

Covers door/window/lock/charge-port/charging-cable/charging-active/
user-presence — i.e. signals whose useful value is on/off.

Entities use ``has_entity_name`` — the vehicle name lives on the HA device
and each entity carries only its functional name.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    SIGNAL_CHARGE_PORT_DOOR_OPEN,
    SIGNAL_CHARGING_CABLE_TYPE,
    SIGNAL_DETAILED_CHARGE_STATE,
    SIGNAL_DOOR_STATE,
    SIGNAL_DRIVER_SEAT_OCCUPIED,
    SIGNAL_LOCKED,
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
from .models import TeslaTelemetryConfigEntry
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
    entry: TeslaTelemetryConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TeslaTelemetryCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            # Doors (each derives one bool from the Doors composite struct)
            DoorBinarySensor(
                coordinator, "front_driver", "DriverFront", "door_front_driver"
            ),
            DoorBinarySensor(
                coordinator,
                "front_passenger",
                "PassengerFront",
                "door_front_passenger",
            ),
            DoorBinarySensor(
                coordinator, "rear_driver", "DriverRear", "door_rear_driver"
            ),
            DoorBinarySensor(
                coordinator,
                "rear_passenger",
                "PassengerRear",
                "door_rear_passenger",
            ),
            DoorBinarySensor(
                coordinator,
                "frunk",
                "TrunkFront",
                "frunk",
                device_class=BinarySensorDeviceClass.OPENING,
            ),
            DoorBinarySensor(
                coordinator,
                "trunk",
                "TrunkRear",
                "trunk",
                device_class=BinarySensorDeviceClass.OPENING,
            ),
            # Windows
            WindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_FRONT_DRIVER,
                "front_driver",
                "window_front_driver",
            ),
            WindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_FRONT_PASSENGER,
                "front_passenger",
                "window_front_passenger",
            ),
            WindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_REAR_DRIVER,
                "rear_driver",
                "window_rear_driver",
            ),
            WindowBinarySensor(
                coordinator,
                SIGNAL_WINDOW_REAR_PASSENGER,
                "rear_passenger",
                "window_rear_passenger",
            ),
            # Other body / charging
            LockBinarySensor(coordinator),
            ChargePortBinarySensor(coordinator),
            ChargeCableBinarySensor(coordinator),
            ChargingActiveBinarySensor(coordinator),
            UserPresentBinarySensor(coordinator),
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
        key: str,
        *,
        device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.DOOR,
    ) -> None:
        super().__init__(coordinator)
        self._bit_name = bit_name
        self._attr_translation_key = key
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
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._signal_name = signal
        self._attr_translation_key = key
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
    key: str,
    device_class: BinarySensorDeviceClass | None = None,
    extractor: Callable[[Any], bool | None] = value_as_bool,
) -> type[_BaseTelemetryBinarySensor]:
    class _BS(_BaseTelemetryBinarySensor):
        _signal_name = signal
        _attr_translation_key = key
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
    _attr_translation_key = "lock"
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
    key="charge_port_door",
    device_class=BinarySensorDeviceClass.OPENING,
)


class ChargeCableBinarySensor(_BaseTelemetryBinarySensor):
    """A cable is present whenever ``ChargingCableType`` reports anything
    other than ``Unknown`` / ``SNA`` (Signal Not Available).

    THE CAR DOES NOT USE ``CableTypeSNA`` TO SAY "UNPLUGGED" — it sends the
    signal with the protobuf ``invalid`` flag set, so ``value_as_enum_name``
    returns None.  Reporting that as ``None`` (HA ``unknown``) meant this
    entity NEVER produced ``off``: measured over 30 days on a real car,
    11 × ``on``, 46 × ``unknown``, zero ``off``.  Downstream that pushed every
    unplug onto the polled Fleet sensor, which lagged 6-27 min behind the
    physical unplug and hid the dashboard's driving cards for the first
    minutes of each post-charge drive.

    ``_handle`` only ever runs on a real sample (``_on_sample``, or a cached
    one at ``async_added_to_hass``), so an undecodable sample is the car
    actively declining to name a cable = no cable.  With no sample at all the
    entity still restores/stays unknown, which is the correct startup state.
    """

    _signal_name = SIGNAL_CHARGING_CABLE_TYPE
    _attr_translation_key = "charge_cable"
    _attr_device_class = BinarySensorDeviceClass.PLUG

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_charge_cable_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        name = value_as_enum_name(sample.value)
        if name is None:
            # invalid / undecodable sample ⇒ no cable (see class docstring)
            self._attr_is_on = False
            return
        self._attr_is_on = name not in ("CableTypeUnknown", "CableTypeSNA")


UserPresentBinarySensor = _bool_binary_sensor(
    signal=SIGNAL_DRIVER_SEAT_OCCUPIED,
    slug="user_present_telemetry",
    key="user_present",
    device_class=BinarySensorDeviceClass.OCCUPANCY,
)


class ChargingActiveBinarySensor(_BaseTelemetryBinarySensor):
    """True while the car is actively pulling charge (Charging or Starting)."""

    _signal_name = SIGNAL_DETAILED_CHARGE_STATE
    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_charging_active_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_is_on = value_charging_active(sample.value)
