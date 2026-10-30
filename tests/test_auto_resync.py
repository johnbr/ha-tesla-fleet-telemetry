"""Tests for the auto-resync of ``fleet_telemetry_config``.

The daily check is what keeps the car's config ahead of Tesla's ~30-day
``exp``. Two of its decisions are load-bearing and used to be wrong:

* an entry with no ``last_sync_at`` returned on the first line, so a re-added
  entry (the v1 → v2 migration asks for exactly that) never renewed the config
  the previous install had pushed — it expired and the stream stopped, with
  nothing in the log;
* ``async_track_time_interval`` restarts its countdown on every HA start, so
  the check is also run once at startup.

``services.py`` imports Home Assistant at module load, which the test
environment does not install (see ``tests/test_signals.py`` for the same
constraint), so the handful of names it touches are stubbed and the module is
loaded by path under a synthetic package.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_PKG = "tesla_telemetry_isolated"
_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)

DAY = 24 * 3600
VIN = "7SAXCCE51RF445019"
HOSTNAME = "telemetry.example.com"


def _module(name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # so ``from homeassistant.helpers import x`` resolves
    module.__dict__.update(attrs)
    return module


def _identity(fn: Any) -> Any:
    return fn


def _validator(*_args: Any, **_kwargs: Any) -> Any:
    return None


def _cv_module() -> ModuleType:
    """``config_validation``: the module-level schemas only need names here.

    Nothing validates in these tests, so anything ``services.py`` reaches for
    can be a permissive callable.
    """
    module = _module("homeassistant.helpers.config_validation")
    module.string = str
    module.latitude = float
    module.longitude = float
    module.__getattr__ = lambda _name: _validator  # type: ignore[attr-defined]
    return module


def _stubs() -> dict[str, ModuleType]:
    """The Home Assistant and aiohttp names ``services.py`` imports."""
    error = type("HomeAssistantError", (Exception,), {})
    return {
        "aiohttp": _module("aiohttp"),
        "homeassistant": _module("homeassistant"),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {})
        ),
        "homeassistant.const": _module(
            "homeassistant.const", ATTR_LATITUDE="latitude", ATTR_LONGITUDE="longitude"
        ),
        "homeassistant.core": _module(
            "homeassistant.core",
            HomeAssistant=type("HomeAssistant", (), {}),
            ServiceCall=type("ServiceCall", (), {}),
            ServiceResponse=type("ServiceResponse", (), {}),
            SupportsResponse=SimpleNamespace(OPTIONAL="optional", ONLY="only"),
            callback=_identity,
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions",
            HomeAssistantError=error,
            ServiceValidationError=type("ServiceValidationError", (Exception,), {}),
            Unauthorized=type("Unauthorized", (Exception,), {}),
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.config_validation": _cv_module(),
        "homeassistant.helpers.event": _module(
            "homeassistant.helpers.event",
            async_track_time_interval=lambda *a, **k: (lambda: None),
        ),
        "homeassistant.helpers.start": _module(
            "homeassistant.helpers.start",
            async_at_started=lambda *a, **k: (lambda: None),
        ),
        "homeassistant.helpers.config_entry_oauth2_flow": _module(
            "homeassistant.helpers.config_entry_oauth2_flow",
            OAuth2Session=type("OAuth2Session", (), {}),
        ),
    }


@pytest.fixture(scope="module")
def services() -> Any:
    """``services.py``, loaded with Home Assistant stubbed out."""
    stubs = _stubs()
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        pkg = sys.modules.get(_PKG) or ModuleType(_PKG)
        pkg.__path__ = [str(_DIR)]
        sys.modules[_PKG] = pkg
        name = f"{_PKG}.services"
        spec = importlib.util.spec_from_file_location(name, _DIR / "services.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        sys.modules.pop(f"{_PKG}.services", None)


class FakeApi:
    """Just the two calls the resync makes."""

    def __init__(self, reported: dict | None = None, error: Exception | None = None):
        self.reported = reported
        self.error = error
        self.get_calls = 0
        self.pushed: list[tuple[str, Any]] = []

    async def get_fleet_telemetry_config(self, vin: str) -> dict:
        self.get_calls += 1
        if self.error is not None:
            raise self.error
        return self.reported or {}

    async def set_fleet_telemetry_config(self, vin: str, config: Any) -> dict:
        self.pushed.append((vin, config))
        return {"ok": True}


class Entry:
    """A config entry: the resync only reads and replaces ``data``."""

    def __init__(self, entry_id: str = "entry-1", **data: Any):
        self.entry_id = entry_id
        self.data = {"vin": VIN, "hostname": HOSTNAME, **data}


class Hass:
    """The slice of ``HomeAssistant`` the resync touches."""

    def __init__(self, entry: Entry):
        self.record: dict[str, Any] = {}
        self.data = {"tesla_telemetry": {entry.entry_id: self.record}}
        self.stamps: list[dict[str, Any]] = []
        self.config_entries = SimpleNamespace(async_update_entry=self._update)
        self._entry = entry

    def _update(self, entry: Entry, data: dict[str, Any] | None = None) -> None:
        entry.data = data or entry.data
        self.stamps.append(entry.data)


def run(services: Any, monkeypatch: pytest.MonkeyPatch, entry: Entry, api: FakeApi) -> Hass:
    """Run one check against ``entry`` with ``api`` standing in for Tesla."""
    hass = Hass(entry)
    monkeypatch.setattr(services, "_entry_api", lambda _hass, _entry: api)
    monkeypatch.setattr(services, "_build_telemetry_config", lambda _entry, _ca: {"sent": True})
    asyncio.run(services._auto_resync_if_due(hass, entry))
    return hass


def config_for(hostname: str | None = HOSTNAME) -> dict:
    """A ``get_fleet_telemetry_config`` body as Tesla returns it."""
    return {"synced": True, "key_paired": True, "config": {"hostname": hostname}}


class TestAdoptingAnInheritedConfig:
    """The car holds a config this entry never pushed (no ``last_sync_at``)."""

    def test_our_hostname_is_adopted_and_stamped(
        self, services: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry, api = Entry(), FakeApi(config_for())
        hass = run(services, monkeypatch, entry, api)

        assert len(api.pushed) == 1, "the inherited config should be re-pushed"
        assert api.pushed[0][0] == VIN
        assert entry.data["last_sync_at"] > 0, "adopting arms the weekly renewal"
        assert hass.stamps, "the stamp goes through the config entry"

    def test_another_endpoint_is_left_alone(
        self, services: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry, api = Entry(), FakeApi(config_for("someone-else.example.com"))
        hass = run(services, monkeypatch, entry, api)

        assert api.pushed == []
        assert "last_sync_at" not in entry.data
        assert hass.stamps == []

    @pytest.mark.parametrize("reported", [{}, {"config": None}, {"config": {}}])
    def test_a_car_without_a_config_is_left_alone(
        self, services: Any, monkeypatch: pytest.MonkeyPatch, reported: dict
    ) -> None:
        entry, api = Entry(), FakeApi(reported)
        run(services, monkeypatch, entry, api)

        assert api.pushed == []
        assert "last_sync_at" not in entry.data

    def test_a_failed_lookup_warns_and_does_not_push(
        self,
        services: Any,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        entry, api = Entry(), FakeApi(error=RuntimeError("Tesla said no"))
        with caplog.at_level(logging.WARNING, logger=services.__name__):
            run(services, monkeypatch, entry, api)

        assert api.pushed == []
        assert "cannot read the car's telemetry config" in caplog.text
        assert "Tesla said no" in caplog.text

    def test_the_car_is_asked_once_per_run(
        self, services: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A "not ours" answer cannot change on its own, so stop asking."""
        entry, api = Entry(), FakeApi(config_for("someone-else.example.com"))
        hass = Hass(entry)
        monkeypatch.setattr(services, "_entry_api", lambda _hass, _entry: api)
        monkeypatch.setattr(
            services, "_build_telemetry_config", lambda _entry, _ca: {"sent": True}
        )
        for _ in range(3):
            asyncio.run(services._auto_resync_if_due(hass, entry))

        assert api.get_calls == 1


class TestRenewingOurOwnConfig:
    def test_a_recent_sync_does_no_work(
        self, services: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = Entry(last_sync_at=int(time.time()) - DAY)
        api = FakeApi()
        run(services, monkeypatch, entry, api)

        assert api.get_calls == 0, "nothing to check while the stamp is fresh"
        assert api.pushed == []

    def test_an_old_stamp_is_renewed(
        self, services: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry = Entry(last_sync_at=int(time.time()) - 8 * DAY)
        api = FakeApi()
        hass = run(services, monkeypatch, entry, api)

        assert len(api.pushed) == 1
        assert entry.data["last_sync_at"] > int(time.time()) - DAY
        assert hass.stamps
