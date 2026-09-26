"""Vehicle-command request bodies and result checking.

Stdlib only, so the tests can load it without Home Assistant installed (the
same isolation ``const.py`` gets). The HTTP side lives in ``tesla_api.py``.

These are plain Fleet API REST commands. Vehicles that require Tesla's signed
Vehicle Command Protocol (most built from 2021 on) reject them; pre-2021
Model S/X accept them as-is. The body shapes match the ``tesla_fleet_api``
library HA core's ``tesla_fleet`` uses for the same endpoints.
"""
from __future__ import annotations

import time
from typing import Any

DEFAULT_LOCALE = "en-US"

# Tesla's remote-nav trip order for ``navigation_gps_request``: 0 leaves it to
# the car (the default), 1 replaces the trip, 2 prepends a stop, 3 appends one.
NAV_ORDERS = (0, 1, 2, 3)


class CommandRefused(Exception):
    """Tesla delivered the command but the car answered ``result: false``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason or "no reason given")
        self.reason = reason


def navigation_request_body(
    destination: str,
    *,
    locale: str = DEFAULT_LOCALE,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """Body for ``command/navigation_request`` — the Fleet API form of the
    phone app's "share to car". The car geocodes the text itself, so a street
    address, a place name, or "name, city" behave as they do when shared from
    a phone."""
    text = destination.strip()
    if not text:
        raise ValueError("destination is empty")
    return {
        "type": "share_ext_content_raw",
        "locale": locale,
        "timestamp_ms": timestamp_ms
        if timestamp_ms is not None
        else int(time.time() * 1000),
        "value": {"android.intent.extra.TEXT": text},
    }


def navigation_gps_body(
    latitude: float, longitude: float, order: int = 0
) -> dict[str, Any]:
    """Body for ``command/navigation_gps_request``."""
    lat, lon = float(latitude), float(longitude)
    if not -90.0 <= lat <= 90.0:
        raise ValueError(f"latitude out of range: {lat}")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"longitude out of range: {lon}")
    if order not in NAV_ORDERS:
        raise ValueError(f"order must be one of {NAV_ORDERS}, got {order!r}")
    return {"lat": lat, "lon": lon, "order": order}


def check_command_result(data: Any) -> dict[str, Any]:
    """Return a command call's ``response`` dict, raising ``CommandRefused``
    when the car reports ``result: false``.

    A command's HTTP 200 only means Tesla relayed it; whether the car acted is
    the ``result`` flag inside, with any explanation in ``reason``.
    """
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, dict):
        raise CommandRefused(f"unexpected response: {data!r}"[:300])
    if not response.get("result"):
        raise CommandRefused(str(response.get("reason") or ""))
    return response
