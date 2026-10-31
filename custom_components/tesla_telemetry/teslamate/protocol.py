"""Encoding of telemetry signals as TeslaMate's streaming-protocol frames.

TeslaMate's ``TESLA_WSS_HOST`` points at a server speaking Tesla's legacy
streaming protocol: the client subscribes with a list of column names and
reads back ``data:update`` frames whose ``value`` is a CSV row in that order.

This module is the pure half of that bridge — no Home Assistant, no sockets,
so it can be exercised directly. ``bridge.py`` owns the connections.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..const import (
    SIGNAL_AC_CHARGING_POWER,
    SIGNAL_DC_CHARGING_POWER,
    SIGNAL_EST_BATTERY_RANGE,
    SIGNAL_GEAR,
    SIGNAL_GPS_HEADING,
    SIGNAL_LOCATION,
    SIGNAL_ODOMETER,
    SIGNAL_RATED_RANGE,
    SIGNAL_SOC,
    SIGNAL_VEHICLE_SPEED,
)
from ..values import value_as_enum_name, value_as_float

# The columns TeslaMate subscribes with (``TeslaApi.Stream.@columns``). We
# honour whatever list a client actually sends; this is the fallback for a
# subscribe frame that omits ``value``.
DEFAULT_COLUMNS: tuple[str, ...] = (
    "speed",
    "odometer",
    "soc",
    "elevation",
    "est_heading",
    "est_lat",
    "est_lng",
    "power",
    "shift_state",
    "range",
    "est_range",
    "heading",
)

# Column -> the signal that fills it. ``power`` and ``elevation`` are derived,
# see ``_cell`` and ``StreamRow._power``.
_COLUMN_SIGNAL: dict[str, str] = {
    "speed": SIGNAL_VEHICLE_SPEED,
    "odometer": SIGNAL_ODOMETER,
    "soc": SIGNAL_SOC,
    "est_heading": SIGNAL_GPS_HEADING,
    "heading": SIGNAL_GPS_HEADING,
    "est_lat": SIGNAL_LOCATION,
    "est_lng": SIGNAL_LOCATION,
    "shift_state": SIGNAL_GEAR,
    "range": SIGNAL_RATED_RANGE,
    "est_range": SIGNAL_EST_BATTERY_RANGE,
}

# TeslaMate parses these with Integer.parse, so they must not carry decimals.
_INT_COLUMNS = frozenset(
    {"speed", "soc", "range", "est_range", "heading", "est_heading"}
)

# Everything that can fill a column, which is also everything worth priming a
# new connection's row with.
SOURCE_SIGNALS: frozenset[str] = frozenset(_COLUMN_SIGNAL.values()) | {
    SIGNAL_AC_CHARGING_POWER,
    SIGNAL_DC_CHARGING_POWER,
}

# A frame is only useful to TeslaMate when it can become a position or a state
# change, and that needs a location plus a shift state it recognises. Anything
# else (ShiftStateUnknown, ShiftStateSNA) makes TeslaMate fetch vehicle_data
# immediately, so emitting those frames would cost an API call each.
_VALID_SHIFT_STATES = frozenset({"P", "R", "N", "D"})

# Signals whose arrival is worth a frame. Location and Gear are what a position
# is made of; charging power is what wakes TeslaMate out of a suspended state
# when a charge session starts, which it would otherwise only notice at the
# next poll. Everything else only refreshes the row for a later frame.
_TRIGGER_SIGNALS = frozenset(
    {SIGNAL_LOCATION, SIGNAL_GEAR, SIGNAL_AC_CHARGING_POWER, SIGNAL_DC_CHARGING_POWER}
)


def _real(value: float) -> str:
    """A float as TeslaMate's ``Float.parse`` expects it."""
    return repr(float(value))


def _cell(column: str, value: Any) -> str | None:
    """The CSV cell ``column`` gets from this datum, or None if the datum
    carries nothing for it."""
    if column in ("est_lat", "est_lng"):
        if not value.HasField("location_value"):
            return None
        location = value.location_value
        return _real(
            location.latitude if column == "est_lat" else location.longitude
        )
    if column == "shift_state":
        name = value_as_enum_name(value)
        # ShiftStateD -> D
        return name.removeprefix("ShiftState") if name else None
    if column == "odometer":
        number = value_as_float(value)
        return None if number is None else _real(number)
    if column in _INT_COLUMNS:
        number = value_as_float(value)
        return None if number is None else str(round(number))
    return None


@dataclass
class StreamRow:
    """The row a single TeslaMate connection is subscribed to.

    TeslaMate zips the CSV against the columns it subscribed with, so a frame
    must carry every column: values are cached as they arrive and the whole row
    is re-emitted whenever one of ``_TRIGGER_SIGNALS`` makes a frame due.
    """

    columns: tuple[str, ...]
    tag: str
    cells: dict[str, str] = field(default_factory=dict)
    ac_kw: float = 0.0
    dc_kw: float = 0.0

    def apply(self, name: str, value: Any) -> bool:
        """Fold one datum in. True when a frame is due.

        A datum that carries nothing usable — Tesla sends ``invalid`` for a
        signal it cannot read — updates nothing, and so triggers nothing: the
        cached value stands, and re-emitting it would tell TeslaMate the car
        just reported a state it did not.
        """
        updated = False
        if name == SIGNAL_AC_CHARGING_POWER:
            charging = value_as_float(value)
            if charging is not None:
                self.ac_kw = charging
                updated = True
        elif name == SIGNAL_DC_CHARGING_POWER:
            charging = value_as_float(value)
            if charging is not None:
                self.dc_kw = charging
                updated = True

        for column, signal in _COLUMN_SIGNAL.items():
            if signal != name or column not in self.columns:
                continue
            cell = _cell(column, value)
            if cell is not None:
                self.cells[column] = cell
                updated = True

        return updated and name in _TRIGGER_SIGNALS and self._complete()

    def _complete(self) -> bool:
        """Whether the cached row can become a position."""
        return (
            bool(self.cells.get("est_lat"))
            and bool(self.cells.get("est_lng"))
            and self.cells.get("shift_state") in _VALID_SHIFT_STATES
        )

    def _power(self) -> str:
        """TeslaMate's convention: negative while charging, in whole kW."""
        kw = max(self.ac_kw, self.dc_kw)
        if kw <= 0:
            return "0"
        # Keep the sign even for a sub-kW trickle, which would round to 0 and
        # read as "not charging".
        return str(-max(1, round(kw)))

    def frame(self, created_at: float | None) -> str:
        """A ``data:update`` frame carrying the whole row."""
        stamp = created_at if created_at is not None else time.time()
        cells = [str(int(stamp * 1000))]
        for column in self.columns:
            if column == "power":
                cells.append(self._power())
            else:
                # elevation is always empty: fleet telemetry carries none, and
                # TeslaMate's SRTM backfill skips streamed drives.
                cells.append(self.cells.get(column, ""))
        return json.dumps(
            {"msg_type": "data:update", "tag": self.tag, "value": ",".join(cells)}
        )
