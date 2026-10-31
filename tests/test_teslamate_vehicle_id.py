"""Tests for the vehicle-id backfill in ``teslamate/__init__.py``.

TeslaMate names the vehicle by Tesla's numeric id unless ``TESLA_WSS_USE_VIN``
is set, and an entry created before the bridge existed only recorded the VIN.
The backfill closes that gap with one Fleet API call — but it runs inside
``async_setup_entry``, so the property that matters most is that it can never
fail setup.

``teslamate_harness`` stubs Home Assistant and aiohttp and loads the real
modules by path, as ``test_auto_resync.py`` does.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from teslamate_harness import load

pytest.importorskip("google.protobuf")

VIN = "7SAXCCE51RF445019"
VID = "1689354557125469"

teslamate = load()


class _Entry:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class _ConfigEntries:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def async_update_entry(self, entry: _Entry, **kwargs: Any) -> None:
        self.updates.append(kwargs)
        entry.data = kwargs.get("data", entry.data)


def _hass() -> SimpleNamespace:
    return SimpleNamespace(config_entries=_ConfigEntries())


class _Api:
    """Stands in for ``TeslaApi``: records calls, returns or raises."""

    def __init__(self, vehicles: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self._vehicles = vehicles

    async def list_vehicles(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self._vehicles is None:
            raise RuntimeError("boom")
        return self._vehicles


def test_an_entry_that_already_has_the_id_never_calls_the_api():
    hass, api = _hass(), _Api([{"vin": VIN, "id_s": VID}])
    entry = _Entry({"vin": VIN, "vehicle_id": VID})

    asyncio.run(teslamate.ensure_vehicle_id(hass, entry, api))

    assert api.calls == 0
    assert hass.config_entries.updates == []


def test_the_id_is_recorded_from_the_matching_vehicle():
    hass = _hass()
    api = _Api(
        [
            {"vin": "5YJ3E1EA7KF000001", "vehicle_id": "1"},
            {"vin": VIN, "vehicle_id": VID},
        ]
    )
    entry = _Entry({"vin": VIN})

    asyncio.run(teslamate.ensure_vehicle_id(hass, entry, api))

    assert api.calls == 1
    assert entry.data["vehicle_id"] == VID
    # The rest of the entry is carried through untouched.
    assert entry.data["vin"] == VIN


def test_only_the_legacy_id_is_taken():
    # ``id`` and ``id_s`` are the Fleet API's own id (TeslaMate's ``eid``), a
    # different number from the ``vid`` it streams under. Storing one would
    # map an id no subscription ever uses, so neither is a fallback.
    for key in ("id", "id_s"):
        hass = _hass()
        entry = _Entry({"vin": VIN})
        asyncio.run(
            teslamate.ensure_vehicle_id(hass, entry, _Api([{"vin": VIN, key: VID}]))
        )
        assert entry.data.get("vehicle_id") == "", key


def test_the_legacy_id_wins_when_both_are_listed():
    hass = _hass()
    entry = _Entry({"vin": VIN})
    api = _Api([{"vin": VIN, "vehicle_id": VID, "id_s": "929799867918428"}])

    asyncio.run(teslamate.ensure_vehicle_id(hass, entry, api))

    assert entry.data["vehicle_id"] == VID


def test_a_miss_is_remembered_so_it_is_looked_up_once():
    # Writing the empty key is what records that the lookup has been done.
    hass = _hass()
    entry = _Entry({"vin": VIN})
    api = _Api([{"vin": "5YJ3E1EA7KF000001", "vehicle_id": "1"}])

    asyncio.run(teslamate.ensure_vehicle_id(hass, entry, api))
    assert api.calls == 1
    assert entry.data["vehicle_id"] == ""

    asyncio.run(teslamate.ensure_vehicle_id(hass, entry, api))
    assert api.calls == 1, "a remembered miss must not call the API again"


def test_a_failing_lookup_does_not_break_setup():
    hass = _hass()
    entry = _Entry({"vin": VIN})

    asyncio.run(teslamate.ensure_vehicle_id(hass, entry, _Api(None)))

    assert hass.config_entries.updates == []
    assert "vehicle_id" not in entry.data


def test_a_vehicle_missing_from_the_account_records_no_id():
    hass = _hass()
    entry = _Entry({"vin": VIN})

    asyncio.run(
        teslamate.ensure_vehicle_id(hass, entry, _Api([{"vin": "5YJ3E1EA7KF000001"}]))
    )

    assert entry.data["vehicle_id"] == ""


def test_a_vehicle_without_an_id_still_records_the_attempt():
    hass = _hass()
    entry = _Entry({"vin": VIN})

    asyncio.run(teslamate.ensure_vehicle_id(hass, entry, _Api([{"vin": VIN}])))

    assert entry.data["vehicle_id"] == ""
