"""Per-VIN telemetry signal cache.

Keeps the latest value for every signal name we've received and notifies
subscribed entities via the HA dispatcher. Entities own their own decoding
of the underlying ``Value`` proto — the coordinator stores the raw datum so
new signal types can be added without touching this file.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DEFAULT_INTERVALS_SECONDS, DOMAIN, STALE_INTERVAL_MULTIPLIER

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SignalSample:
    """One signal value plus when we and the vehicle stamped it."""

    value: Any
    received_at: float
    payload_created_at: float | None


def signal_dispatcher_topic(vin: str, name: str) -> str:
    """Dispatcher signal name an entity subscribes to."""
    return f"{DOMAIN}.{vin}.{name}"


class TeslaTelemetryCoordinator:
    """Holds the latest sample per signal name for a single vehicle."""

    def __init__(self, hass: HomeAssistant, vin: str) -> None:
        self.hass = hass
        self.vin = vin
        self._samples: dict[str, SignalSample] = {}

    @callback
    def async_publish(
        self,
        name: str,
        value: Any,
        payload_created_at: float | None = None,
    ) -> None:
        """Record a new value and notify subscribers."""
        sample = SignalSample(
            value=value,
            received_at=time.time(),
            payload_created_at=payload_created_at,
        )
        self._samples[name] = sample
        async_dispatcher_send(
            self.hass, signal_dispatcher_topic(self.vin, name), sample
        )

    def get(self, name: str) -> SignalSample | None:
        return self._samples.get(name)

    def is_stale(self, name: str, *, now: float | None = None) -> bool:
        sample = self._samples.get(name)
        if sample is None:
            return True
        interval = DEFAULT_INTERVALS_SECONDS.get(name, 60)
        cutoff = (now or time.time()) - interval * STALE_INTERVAL_MULTIPLIER
        return sample.received_at < cutoff
