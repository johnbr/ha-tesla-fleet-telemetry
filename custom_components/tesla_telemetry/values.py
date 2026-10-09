"""Helpers for unpacking Tesla `Value` oneofs.

The vehicle's protobuf payload uses one ``Value`` message with a ``oneof``
field whose populated arm depends on the signal.  These helpers narrow the
oneof down to a Python primitive (or ``None`` when the signal carried the
``invalid`` flag or an unset variant).

Keep this module dependency-free except for the protobuf import — sensor and
binary_sensor platforms both consume it.
"""
from __future__ import annotations

import math
import re
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


# --------------------------------------------------------------------------
# Generic decoding for catalog signals that have no purpose-built entity.
#
# The same field can arrive on different ``Value`` arms depending on the
# vehicle generation and firmware — pre-2021 S/X and untyped firmware send
# numbers as ``string_value``; ``FastChargerPresent`` is a bool on some cars
# and an enum on others — so nothing here assumes a type: every sample is
# classified from the arm that is actually populated.
# --------------------------------------------------------------------------
VALUE_KIND_NUMBER = "number"
VALUE_KIND_BOOL = "bool"
VALUE_KIND_STRING = "string"
VALUE_KIND_ENUM = "enum"
VALUE_KIND_COMPOSITE = "composite"

_NUMERIC_ARMS = frozenset({"int_value", "long_value", "float_value", "double_value"})
_MAX_STATE_LEN = 255
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _enum_friendly(enum_type_name: str, value_name: str) -> str:
    """``SentryModeStateArmed`` → ``armed``: drop the enum type's own name
    prefix (Tesla's convention), then snake_case what's left."""
    stem = value_name
    for prefix in (enum_type_name, enum_type_name.removesuffix("Value")):
        if prefix and stem.startswith(prefix) and len(stem) > len(prefix):
            stem = stem[len(prefix):]
            break
    return _CAMEL_BOUNDARY.sub("_", stem).lower()


def _message_fields(message: Any) -> dict[str, Any]:
    """Every field of a composite value (``LocationValue``, ``Doors``,
    ``Time``, …), defaults included so an all-closed ``Doors`` still lists
    each door. Walked by descriptor rather than ``MessageToDict`` so the
    output doesn't depend on the installed protobuf version's options."""
    fields: dict[str, Any] = {}
    for desc in message.DESCRIPTOR.fields:
        raw = getattr(message, desc.name)
        if desc.enum_type is not None:
            enum_value = desc.enum_type.values_by_number.get(int(raw))
            fields[desc.name] = enum_value.name if enum_value else int(raw)
        elif desc.message_type is not None:
            fields[desc.name] = _message_fields(raw)
        else:
            fields[desc.name] = raw
    return fields


def value_as_native(value: Any) -> tuple[str | None, Any, dict[str, Any]]:
    """Classify any ``Value`` into ``(kind, state, attributes)``.

    ``kind`` is one of the ``VALUE_KIND_*`` constants, or ``None`` with a
    ``None`` state when the datum is invalid or empty. Numeric strings are
    reported as numbers so untyped firmware still yields a measurement.
    """
    if value.HasField("invalid"):
        return None, None, {}
    arm = value.WhichOneof("value")
    if arm is None:
        return None, None, {}
    if arm in _NUMERIC_ARMS:
        return VALUE_KIND_NUMBER, value_as_float(value), {}
    if arm == "boolean_value":
        return VALUE_KIND_BOOL, "on" if value.boolean_value else "off", {}
    if arm == "string_value":
        text = value.string_value
        try:
            number = float(text)
        except ValueError:
            return VALUE_KIND_STRING, text[:_MAX_STATE_LEN] or None, {}
        if math.isfinite(number):
            return VALUE_KIND_NUMBER, number, {}
        return VALUE_KIND_STRING, text, {}

    field = value.DESCRIPTOR.fields_by_name[arm]
    if field.enum_type is not None:
        name = value_as_enum_name(value)
        if name is None:
            return VALUE_KIND_ENUM, None, {}
        friendly = _enum_friendly(field.enum_type.name, name)
        return VALUE_KIND_ENUM, friendly, {"raw": name}
    if field.message_type is not None:
        fields = _message_fields(getattr(value, arm))
        summary = ", ".join(f"{k}={v}" for k, v in fields.items())
        return VALUE_KIND_COMPOSITE, summary[:_MAX_STATE_LEN] or None, fields
    return None, None, {}


__all__ = [
    "value_as_bool",
    "value_as_charge_state",
    "value_as_door_state",
    "value_as_enum_name",
    "value_as_float",
    "value_as_native",
    "value_as_string",
    "value_as_window_state",
    "value_charging_active",
    "value_is_window_open",
]


# Suppress the unused-import warning for vdp — kept for typing/symbol export
# reasons, the proto package being importable here also serves as a quick
# fail-fast if the build is missing the compiled protos.
_ = vdp
