"""Tests for the TeslaMate bridge's subscribe path (``teslamate/bridge.py``).

Two behaviours here are not obvious from the code and are what a live
connection would otherwise have to prove:

* a new connection's row is seeded from the coordinator's cache, so a car that
  is parked when TeslaMate connects can still produce a frame from its next
  gear or charging change — a frame needs a location, and a stationary car
  does not stream one;
* the cache is only reused while the stream is live, because after an HA
  restart the last known location can predate a journey, and a stale position
  written into TeslaMate's history is worse than no frame at all.

The bridge imports Home Assistant and aiohttp, which the test environment does
not install, so ``teslamate_harness`` stubs them and loads the real modules.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from teslamate_harness import load, vehicle_data

pytest.importorskip("google.protobuf")

teslamate = load()
vdp = vehicle_data()
bridge = teslamate.bridge
protocol = teslamate.protocol

VIN = "7SAXCCE51RF445019"
VID = "1689354557125469"
SECRET = "s" * 64
TESLAMATE_COLUMNS = (
    "speed,odometer,soc,elevation,est_heading,est_lat,est_lng,"
    "power,shift_state,range,est_range,heading"
)


class _Coordinator:
    """Stands in for ``TeslaTelemetryCoordinator``: a signal cache."""

    def __init__(self, samples: dict[str, object], age: float = 1.0) -> None:
        self._samples = {
            name: SimpleNamespace(value=v, received_at=time.time() - age)
            for name, v in samples.items()
        }

    def get(self, name: str):
        return self._samples.get(name)


def _view(coordinator, *, enabled: bool = True):
    return bridge.TeslaTelemetryStreamView(
        SimpleNamespace(),
        {VIN: SECRET},
        {VID: VIN},
        {VIN} if enabled else set(),
        {VIN: coordinator} if coordinator is not None else {},
    )


def _subscribe(view, tag: str = VID, columns: str = TESLAMATE_COLUMNS):
    frame = json.dumps(
        {"msg_type": "data:subscribe_oauth", "token": "t", "value": columns, "tag": tag}
    )
    return view._subscribe(object(), frame, {VIN})


def _location(lat: float, lng: float):
    return vdp.Value(location_value=vdp.LocationValue(latitude=lat, longitude=lng))


def _gear(state: str):
    return vdp.Value(shift_state_value=vdp.ShiftState.Value(state))


def _double(value: float):
    return vdp.Value(double_value=value)


def test_a_live_cache_seeds_a_new_connection():
    view = _view(
        _Coordinator(
            {"Location": _location(37.5, -122.25), "Gear": _gear("ShiftStateP")}
        )
    )

    client = _subscribe(view)

    assert client is not None
    assert client.row.cells["est_lat"] == "37.5"
    assert client.row.cells["est_lng"] == "-122.25"
    assert client.row.cells["shift_state"] == "P"


def test_a_seeded_row_frames_on_the_next_gear_change():
    # The point of seeding: without it this datum alone could not make a frame.
    view = _view(_Coordinator({"Location": _location(37.5, -122.25)}))
    client = _subscribe(view)

    assert client.row.apply("Gear", _gear("ShiftStateD"))


def test_a_seeded_row_frames_on_the_next_charging_change():
    view = _view(
        _Coordinator(
            {"Location": _location(37.5, -122.25), "Gear": _gear("ShiftStateP")}
        )
    )
    client = _subscribe(view)

    assert client.row.apply("ACChargingPower", _double(11.4))


def test_a_quiet_vehicle_does_not_seed():
    # Asleep, or HA restarted and the cache predates a journey: the location
    # may no longer be where the car is, so it must not be reused.
    view = _view(
        _Coordinator(
            {"Location": _location(37.5, -122.25), "Gear": _gear("ShiftStateP")},
            age=bridge.SEED_MAX_AGE_SECONDS + 60,
        )
    )

    client = _subscribe(view)

    assert client.row.cells == {}


def test_a_vehicle_that_never_reported_does_not_seed():
    view = _view(_Coordinator({}))

    client = _subscribe(view)

    assert client.row.cells == {}


def test_no_coordinator_means_no_seed():
    view = _view(None)

    client = _subscribe(view)

    assert client is not None
    assert client.row.cells == {}


def test_seeding_respects_the_subscribed_columns():
    # A client that did not ask for a location gets no position, seeded or not.
    view = _view(
        _Coordinator(
            {"Location": _location(37.5, -122.25), "Gear": _gear("ShiftStateP")}
        )
    )

    client = _subscribe(view, columns="soc,shift_state")

    assert "est_lat" not in client.row.cells
    assert client.row.cells["shift_state"] == "P"


def test_an_unknown_tag_is_still_rejected():
    view = _view(_Coordinator({}))

    assert _subscribe(view, tag="not-a-vehicle") is None
