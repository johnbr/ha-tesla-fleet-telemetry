"""Helpers for unpacking Tesla `Value` oneofs.

The vehicle's protobuf payload uses one ``Value`` message with a ``oneof``
field whose populated arm depends on the signal.  These helpers narrow the
oneof down to a Python primitive (or ``None`` when the signal carried the
``invalid`` flag or an unset variant).

Keep this module dependency-free except for the protobuf import — sensor and
binary_sensor platforms both consume it.
"""
from __future__ import annotations

from typing import Any

from .proto import vehicle_data_pb2 as vdp


def value_as_float(value: Any) -> float | None:
    """Return a numeric value out of any of the numeric oneof arms."""
    if value.HasField("invalid"):
        return None
    if value.HasField("double_value"):
        return value.double_value
    if value.HasField("float_value"):
        return value.float_value
    if value.HasField("int_value"):
        return float(value.int_value)
    if value.HasField("long_value"):
        return float(value.long_value)
    return None


def value_as_bool(value: Any) -> bool | None:
    """Return a bool from ``boolean_value`` or numeric truthy/falsy fallbacks."""
    if value.HasField("invalid"):
        return None
    if value.HasField("boolean_value"):
        return value.boolean_value
    f = value_as_float(value)
    if f is None:
        return None
    return bool(f)


def value_as_string(value: Any) -> str | None:
    if value.HasField("invalid"):
        return None
    if value.HasField("string_value") and value.string_value:
        return value.string_value
    return None


def value_as_enum_name(value: Any) -> str | None:
    """Return the enum name from whichever enum-typed oneof arm is set.

    Tesla uses many one-off enums (``ShiftState``, ``HvacPowerState``,
    ``DetailedChargeStateValue``, ``SentryModeState``, ``DefrostModeState``,
    ``HvacAutoModeState``, ``ChargingState``, ``FastCharger``, ``CableType``,
    ``DisplayState``).  We don't enumerate them here — instead we use the
    proto's ``WhichOneof`` reflection to find the populated arm and resolve
    its enum descriptor on demand.  Returns the enum value's *name* (e.g.
    ``"DetailedChargeStateCharging"``), the caller is responsible for
    mapping to a friendly string.
    """
    if value.HasField("invalid"):
        return None
    arm = value.WhichOneof("value")
    if arm is None:
        return None
    field = value.DESCRIPTOR.fields_by_name.get(arm)
    if field is None or field.enum_type is None:
        return None
    raw = getattr(value, arm)
    name = field.enum_type.values_by_number.get(int(raw))
    return name.name if name is not None else None


def value_as_door_state(value: Any) -> dict[str, bool] | None:
    """Decode a ``Doors`` composite into a dict of named bools."""
    if value.HasField("invalid"):
        return None
    if not value.HasField("door_value"):
        return None
    d = value.door_value
    return {
        "DriverFront": d.DriverFront,
        "DriverRear": d.DriverRear,
        "PassengerFront": d.PassengerFront,
        "PassengerRear": d.PassengerRear,
        "TrunkFront": d.TrunkFront,
        "TrunkRear": d.TrunkRear,
    }


# Friendly-name mapping for window state.  ``WindowStateClosed`` → ``"closed"``.
_WINDOW_STATE_FRIENDLY = {
    "WindowStateUnknown": None,
    "WindowStateClosed": "closed",
    "WindowStatePartiallyOpen": "partial",
    "WindowStateOpened": "open",
}


def value_as_window_state(value: Any) -> str | None:
    name = value_as_enum_name(value)
    if name is None:
        return None
    return _WINDOW_STATE_FRIENDLY.get(name, name)


def value_is_window_open(value: Any) -> bool | None:
    """A window is "open" if it isn't fully closed (treats ``partial`` as open)."""
    name = value_as_enum_name(value)
    if name is None:
        return None
    if name == "WindowStateClosed":
        return False
    if name in ("WindowStatePartiallyOpen", "WindowStateOpened"):
        return True
    return None


# Friendly mapping for charging state.  Returned as the SENSOR state string
# so the sensor entity can use it directly.
_DETAILED_CHARGE_FRIENDLY = {
    "DetailedChargeStateUnknown": None,
    "DetailedChargeStateDisconnected": "disconnected",
    "DetailedChargeStateNoPower": "no_power",
    "DetailedChargeStateStarting": "starting",
    "DetailedChargeStateCharging": "charging",
    "DetailedChargeStateComplete": "complete",
    "DetailedChargeStateStopped": "stopped",
}


def value_as_charge_state(value: Any) -> str | None:
    name = value_as_enum_name(value)
    if name is None:
        return None
    return _DETAILED_CHARGE_FRIENDLY.get(name, name)


def value_charging_active(value: Any) -> bool | None:
    """True if the car is actively pulling power (Charging or Starting)."""
    name = value_as_enum_name(value)
    if name is None:
        return None
    return name in ("DetailedChargeStateCharging", "DetailedChargeStateStarting")


# Friendly mapping for the battery-management-system state enum (``BMSState``).
# Exposed as the ENUM sensor state string.
_BMS_STATE_FRIENDLY = {
    "BMSStateUnknown": None,
    "BMSStateStandby": "standby",
    "BMSStateDrive": "drive",
    "BMSStateSupport": "support",
    "BMSStateCharge": "charge",
    "BMSStateFEIM": "feim",
    "BMSStateClearFault": "clear_fault",
    "BMSStateFault": "fault",
    "BMSStateWeld": "weld",
    "BMSStateTest": "test",
    "BMSStateSNA": None,
}

# Stable option list for the BMS-state ENUM sensor (excludes the None-mapped
# Unknown/SNA sentinels).
BMS_STATE_OPTIONS = [
    "standby",
    "drive",
    "support",
    "charge",
    "feim",
    "clear_fault",
    "fault",
    "weld",
    "test",
]


def value_as_bms_state(value: Any) -> str | None:
    name = value_as_enum_name(value)
    if name is None:
        return None
    return _BMS_STATE_FRIENDLY.get(name, name)


__all__ = [
    "value_as_float",
    "value_as_bool",
    "value_as_string",
    "value_as_enum_name",
    "value_as_door_state",
    "value_as_window_state",
    "value_is_window_open",
    "value_as_charge_state",
    "value_charging_active",
    "value_as_bms_state",
    "BMS_STATE_OPTIONS",
]


# Suppress the unused-import warning for vdp — kept for typing/symbol export
# reasons, the proto package being importable here also serves as a quick
# fail-fast if the build is missing the compiled protos.
_ = vdp
