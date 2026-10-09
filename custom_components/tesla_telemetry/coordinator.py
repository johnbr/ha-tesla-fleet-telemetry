"""Per-VIN telemetry signal cache.

Keeps the latest value for every signal name we've received and notifies
subscribed entities via the HA dispatcher. Entities own their own decoding
of the underlying ``Value`` proto — the coordinator stores the raw datum so
new signal types can be added without touching this file.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DEFAULT_INTERVALS_SECONDS, DOMAIN, STALE_INTERVAL_MULTIPLIER

_LOGGER = logging.getLogger(__name__)

# Tesla encodes the model in the 4th VIN character. Unknown codes fall back
# to ``None`` so the device card simply omits a model rather than guessing.
_VIN_MODEL_CODES: dict[str, str] = {
    "S": "Model S",
    "3": "Model 3",
    "X": "Model X",
    "Y": "Model Y",
    "R": "Roadster",
}


def model_from_vin(vin: str) -> str | None:
    """Best-effort Tesla model name from a VIN."""
    if len(vin) >= 4:
        return _VIN_MODEL_CODES.get(vin[3].upper())
    return None


@dataclass(slots=True)
class SignalSample:
    """One signal value plus when we and the vehicle stamped it."""

    value: Any
    received_at: float
    payload_created_at: float | None


def signal_dispatcher_topic(vin: str, name: str) -> str:
    """Dispatcher signal name an entity subscribes to."""
    return f"{DOMAIN}.{vin}.{name}"


def signals_changed_topic(entry_id: str) -> str:
    """Dispatcher signal sent when an entry's signal selection may have
    changed, so the sensor platform can add/remove generic entities."""
    return f"{DOMAIN}.{entry_id}.signals_changed"


class TeslaTelemetryCoordinator:
    """Holds the latest sample per signal name for a single vehicle."""

    def __init__(
        self, hass: HomeAssistant, vin: str, vehicle_name: str
    ) -> None:
        self.hass = hass
        self.vin = vin
        self.vehicle_name = vehicle_name
        self._samples: dict[str, SignalSample] = {}
        # Signal accounting. Each published datum is one Tesla-billed signal.
        # ``signals_since_start`` counts everything received this process;
        # ``restored_signal_base`` is the lifetime total the counter sensor
        # restores across restarts, so ``lifetime_signals`` stays monotonic.
        self.signals_since_start = 0
        self.restored_signal_base = 0
        self.signal_counts: Counter[str] = Counter()
        # Effective per-signal intervals used for staleness. Seeded to the
        # built-in defaults and refreshed by __init__.async_setup_entry (and
        # its options-update listener) from the entry's resolved config, so
        # staleness tracks whatever interval the signal is actually configured
        # at rather than the hardcoded default.
        self.effective_intervals: dict[str, int] = dict(DEFAULT_INTERVALS_SECONDS)
        # Every entity for this vehicle attaches to one HA device, named
        # after the vehicle so multiple Teslas stay cleanly separated.
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, vin)},
            manufacturer="Tesla",
            model=model_from_vin(vin),
            name=vehicle_name,
            serial_number=vin,
        )

    @callback
    def async_publish(
        self,
        name: str,
        value: Any,
        payload_created_at: float | None = None,
    ) -> None:
        """Record a new value and notify subscribers."""
        self.signals_since_start += 1
        self.signal_counts[name] += 1
        sample = SignalSample(
            value=value,
            received_at=time.time(),
            payload_created_at=payload_created_at,
        )
        self._samples[name] = sample
        async_dispatcher_send(
            self.hass, signal_dispatcher_topic(self.vin, name), sample
        )

    @property
    def lifetime_signals(self) -> int:
        """Total signals ever received for this vehicle — the restored
        base plus everything counted since this process started."""
        return self.restored_signal_base + self.signals_since_start

    def get(self, name: str) -> SignalSample | None:
        return self._samples.get(name)

    def is_stale(self, name: str, *, now: float | None = None) -> bool:
        sample = self._samples.get(name)
        if sample is None:
            return True
        interval = self.effective_intervals.get(name, 60)
        cutoff = (now or time.time()) - interval * STALE_INTERVAL_MULTIPLIER
        return sample.received_at < cutoff
