"""Optional bridge that lets TeslaMate consume this integration's telemetry.

TeslaMate is a self-hosted logger with dashboards of its own, and it normally
reaches Tesla through the same Fleet API this integration does — two systems
paying for the same data. It can instead take its stream from
``TESLA_WSS_HOST``, so pointing that at us lets one Tesla-facing system feed
both.

Everything TeslaMate-specific lives in this subpackage:

* ``protocol.py`` — encodes our decoded signals as the streaming-protocol
  frames TeslaMate parses (pure, no Home Assistant).
* ``bridge.py`` — the HTTP view, the per-VIN fan-out, and the heartbeats.
* this module — wiring, so the integration's setup path stays one call.

Nothing outside the subpackage needs to know the bridge exists, apart from the
``datapoint_topic`` the coordinator publishes every datum on.
"""
from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from ..const import CONF_VEHICLE_ID, CONF_VIN, DOMAIN
from ..coordinator import signal_dispatcher_topic
from .bridge import STREAM_PATH, TeslaMateStreamHub, TeslaTelemetryStreamView
from .protocol import SOURCE_SIGNALS

if TYPE_CHECKING:
    from ..coordinator import TeslaTelemetryCoordinator
    from ..tesla_api import TeslaApi

_LOGGER = logging.getLogger(__name__)

# Options key for the opt-in switch. Lives here rather than in const.py
# because nothing outside the subpackage has any use for it.
CONF_TESLAMATE_BRIDGE = "teslamate_bridge"

_HUB = "teslamate_hub"
_VEHICLE_IDS = "teslamate_vins_by_vehicle_id"
_ENABLED = "teslamate_enabled_vins"


def bridge_enabled(entry: ConfigEntry) -> bool:
    """Whether this entry serves TeslaMate. Off unless the user opts in."""
    return bool(entry.options.get(CONF_TESLAMATE_BRIDGE, False))


async def ensure_vehicle_id(
    hass: HomeAssistant, entry: ConfigEntry, api: TeslaApi
) -> None:
    """Backfill Tesla's numeric vehicle id for entries created before the
    bridge needed it.

    TeslaMate names the vehicle by that id unless ``TESLA_WSS_USE_VIN`` is
    set, so without it a subscription can only be matched against the VIN. One
    Fleet API call at setup settles this for good — and only for entries that
    lack the id, since the config flow records it for new ones.

    Best-effort: if the lookup fails the user's fallback is that environment
    variable, so this never fails setup.
    """
    # The key's presence, not its value, is what records that the lookup has
    # been done — so a vehicle that will never report an id does not cost a
    # list_vehicles call on every restart.
    if CONF_VEHICLE_ID in entry.data:
        return

    vin = entry.data[CONF_VIN]
    try:
        vehicles = await api.list_vehicles()
    except Exception as err:  # noqa: BLE001 — setup must not depend on this
        _LOGGER.debug("tesla_telemetry: vehicle id lookup failed for %s: %s", vin, err)
        return

    # Only ``vehicle_id``: it is the legacy numeric id TeslaMate streams under
    # (its ``vid``). ``id`` and ``id_s`` are the Fleet API's own id (its
    # ``eid``) — a different number — and taking one would map an id no
    # subscription ever uses.
    vehicle_id = next(
        (
            str(vehicle["vehicle_id"])
            for vehicle in vehicles
            if vehicle.get("vin") == vin and vehicle.get("vehicle_id")
        ),
        "",
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_VEHICLE_ID: vehicle_id}
    )
    if vehicle_id:
        _LOGGER.info(
            "tesla_telemetry: recorded vehicle id %s for %s", vehicle_id, vin
        )


def setup_bridge(
    hass: HomeAssistant,
    entry: ConfigEntry,
    vin: str,
    secrets_by_vin: dict[str, str],
    coordinators_by_vin: dict[str, TeslaTelemetryCoordinator],
) -> None:
    """Register the bridge for one entry.

    It authenticates with the same proxy secret as the vehicle endpoint — the
    reverse proxy injects that header on both hops — so it reads the maps the
    caller already maintains rather than keeping copies of them. The
    coordinators are what a new connection's row is seeded from.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    vehicle_ids: dict[str, str] = domain_data.setdefault(_VEHICLE_IDS, {})
    enabled: set[str] = domain_data.setdefault(_ENABLED, set())
    enabled.add(vin)
    # TeslaMate addresses a vehicle by Tesla's numeric id unless
    # TESLA_WSS_USE_VIN is set; accepting both spares the user that variable.
    # Entries created before the id was recorded are addressed by VIN.
    if vehicle_id := entry.data.get(CONF_VEHICLE_ID):
        vehicle_ids[str(vehicle_id)] = vin

    if _HUB not in domain_data:
        hub = TeslaMateStreamHub(hass)
        hass.http.register_view(
            TeslaTelemetryStreamView(
                hub, secrets_by_vin, vehicle_ids, enabled, coordinators_by_vin
            )
        )
        domain_data[_HUB] = hub
        _LOGGER.info("tesla_telemetry: TeslaMate bridge registered at %s", STREAM_PATH)

    hub: TeslaMateStreamHub = domain_data[_HUB]
    # Subscribe to the integration's existing per-signal topics rather than
    # adding a hook to the coordinator: nothing on its per-datum path changes
    # for users who never run TeslaMate. Only the signals that can fill a
    # column are of interest — the rest the bridge would ignore anyway.
    for name in SOURCE_SIGNALS:
        entry.async_on_unload(
            async_dispatcher_connect(
                hass,
                signal_dispatcher_topic(vin, name),
                partial(hub.publish, vin, name),
            )
        )


def unload_bridge(hass: HomeAssistant, vin: str) -> None:
    """Stop serving this vehicle, and drop any connection already open.

    HA cannot unregister a view without a restart, so the view stays — but it
    is no longer enabled for this VIN, so the next connection is refused, and
    a client that is already connected is closed here rather than left reading
    a stream its entry no longer authorises.
    """
    domain_data = hass.data.get(DOMAIN, {})
    domain_data.get(_ENABLED, set()).discard(vin)
    vehicle_ids = domain_data.get(_VEHICLE_IDS, {})
    for vehicle_id, mapped in list(vehicle_ids.items()):
        if mapped == vin:
            del vehicle_ids[vehicle_id]
    hub: TeslaMateStreamHub | None = domain_data.get(_HUB)
    if hub is not None:
        hub.close_vin(vin)
