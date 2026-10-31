"""Tests for the TeslaMate streaming-protocol encoder (``protocol.py``).

TeslaMate decodes a ``data:update`` frame by zipping its CSV ``value`` against
the columns it subscribed with, positionally — so a short or reordered row
silently writes values into the wrong fields, and a frame that arrives without
a usable shift state costs it a Fleet API call. These cases pin both.

``protocol.py`` needs only the generated protos, so it is loaded by path
under a synthetic package with no Home Assistant installed, as
``test_values.py`` does.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("google.protobuf")

_PKG = "tesla_telemetry_stream_isolated"
_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)


def _load(name: str, path: Path, *, package: bool = False) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        name,
        path,
        submodule_search_locations=[str(path.parent)] if package else None,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_isolated() -> tuple[ModuleType, ModuleType]:
    if f"{_PKG}.teslamate.protocol" in sys.modules:
        return (
            sys.modules[f"{_PKG}.proto.vehicle_data_pb2"],
            sys.modules[f"{_PKG}.teslamate.protocol"],
        )
    pkg = ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    _load(f"{_PKG}.const", _DIR / "const.py")
    _load(f"{_PKG}.proto", _DIR / "proto" / "__init__.py", package=True)
    _load(f"{_PKG}.proto.vehicle_data_pb2", _DIR / "proto" / "vehicle_data_pb2.py")
    _load(f"{_PKG}.values", _DIR / "values.py")
    # The subpackage's __init__ wires the Home Assistant view, so only the
    # pure encoder is loaded; it needs nothing beyond const/values.
    bridge_pkg = ModuleType(f"{_PKG}.teslamate")
    bridge_pkg.__path__ = [str(_DIR / "teslamate")]
    sys.modules[f"{_PKG}.teslamate"] = bridge_pkg
    protocol = _load(
        f"{_PKG}.teslamate.protocol", _DIR / "teslamate" / "protocol.py"
    )
    return sys.modules[f"{_PKG}.proto.vehicle_data_pb2"], protocol


vdp, protocol = _load_isolated()


def _location(lat: float, lng: float):
    return vdp.Value(location_value=vdp.LocationValue(latitude=lat, longitude=lng))


def _double(value: float):
    return vdp.Value(double_value=value)


def _shift(state: str):
    return vdp.Value(shift_state_value=vdp.ShiftState.Value(state))


def _row(columns=None) -> protocol.StreamRow:
    return protocol.StreamRow(
        columns=columns or protocol.DEFAULT_COLUMNS, tag="VIN123"
    )


def _cells(frame: str) -> list[str]:
    return json.loads(frame)["value"].split(",")


def test_frame_has_one_cell_per_column_plus_timestamp():
    row = _row()
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Gear", _shift("ShiftStateD"))

    cells = _cells(row.frame(1_700_000_000.0))

    # TeslaMate zips [timestamp | its columns], so the arity is the contract.
    assert len(cells) == len(protocol.DEFAULT_COLUMNS) + 1
    assert cells[0] == "1700000000000"


def test_frame_carries_the_subscribed_column_order():
    columns = ("est_lng", "soc", "est_lat")
    row = _row(columns)
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Soc", _double(61.4))
    row.apply("Gear", _shift("ShiftStateD"))

    assert _cells(row.frame(0.0))[1:] == ["-122.25", "61", "37.5"]


def test_tag_is_echoed_verbatim():
    # TeslaMate matches the tag against whatever id it subscribed with, which
    # is the numeric vehicle id unless TESLA_WSS_USE_VIN is set.
    row = protocol.StreamRow(columns=("soc",), tag="1492932012345678")
    assert json.loads(row.frame(0.0))["tag"] == "1492932012345678"


def test_a_position_needs_location_and_a_recognised_shift_state():
    row = _row()
    assert not row.apply("Location", _location(37.5, -122.25)), "no gear yet"
    assert not row.apply("Gear", _shift("ShiftStateUnknown")), "unusable gear"
    assert row.apply("Gear", _shift("ShiftStateD")), "location + gear is a position"


def test_unknown_shift_state_is_cached_but_never_framed():
    # Emitting it would make TeslaMate answer every frame with an immediate
    # vehicle_data fetch.
    row = _row()
    row.apply("Location", _location(37.5, -122.25))
    for state in ("ShiftStateUnknown", "ShiftStateSNA"):
        assert not row.apply("Gear", _shift(state))


def test_an_unreadable_datum_updates_nothing_and_frames_nothing():
    # Tesla sends ``invalid`` for a signal it cannot read. The cached value
    # stands, but re-emitting it would tell TeslaMate the car just reported a
    # state it did not.
    row = _row()
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Gear", _shift("ShiftStateD"))

    assert not row.apply("Gear", vdp.Value(invalid=True))
    assert row.cells["shift_state"] == "D"


def test_untracked_signals_do_not_frame():
    row = _row()
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Gear", _shift("ShiftStateD"))
    # Already complete, but nothing that makes a frame due changed.
    assert not row.apply("Soc", _double(61.4))
    assert not row.apply("VehicleSpeed", _double(42.0))


def test_row_accumulates_across_frames():
    row = _row()
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Soc", _double(61.4))
    row.apply("Gear", _shift("ShiftStateD"))

    first = _cells(row.frame(0.0))
    row.apply("Location", _location(37.6, -122.35))
    second = _cells(row.frame(0.0))

    # soc was not re-sent, but TeslaMate must still see it in the row.
    assert first[3] == second[3] == "61"
    assert first[6] != second[6]


def test_power_is_negative_while_charging():
    row = _row()
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Gear", _shift("ShiftStateP"))
    assert _cells(row.frame(0.0))[8] == "0"

    row.apply("ACChargingPower", _double(11.4))
    assert _cells(row.frame(0.0))[8] == "-11"

    row.apply("DCChargingPower", _double(120.2))
    assert _cells(row.frame(0.0))[8] == "-120"


def test_a_trickle_charge_keeps_its_sign():
    # Rounded to 0 it would read as "not charging" to TeslaMate.
    row = _row()
    row.apply("ACChargingPower", _double(0.3))
    assert _cells(row.frame(0.0))[8] == "-1"


def test_charging_power_frames_once_the_row_is_complete():
    row = _row()
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Gear", _shift("ShiftStateP"))
    # This is what wakes TeslaMate out of a suspended state when a charge
    # session starts, so it has to produce a frame on its own.
    assert row.apply("ACChargingPower", _double(11.4))


def test_elevation_is_always_empty():
    # Fleet telemetry carries none, and TeslaMate's SRTM backfill skips
    # streamed drives.
    row = _row()
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Gear", _shift("ShiftStateD"))
    assert _cells(row.frame(0.0))[4] == ""


def test_frame_time_falls_back_to_now_when_the_payload_has_none():
    row = _row()
    row.apply("Gear", _shift("ShiftStateD"))
    stamp = int(_cells(row.frame(None))[0])
    assert stamp > 1_600_000_000_000


def test_columns_the_client_did_not_ask_for_stay_blank():
    row = _row(("soc",))
    row.apply("Location", _location(37.5, -122.25))
    row.apply("Soc", _double(61.4))
    row.apply("Gear", _shift("ShiftStateD"))
    # No est_lat/est_lng column -> no position -> no frame at all.
    assert _cells(row.frame(0.0)) == ["0", "61"]
