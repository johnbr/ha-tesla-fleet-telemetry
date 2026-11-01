"""Tests for the diagnostics redaction.

A diagnostics download is meant to be attached to a public issue, so anything
that identifies the car has to be masked before it leaves the instance. Review
caught the first version exposing the VIN three ways at once — as a key in
``entry.data``, inside ``entry.title`` (the config flow appends `` (VIN)``) and
as ``runtime.vin`` — plus the numeric vehicle id the TeslaMate bridge stores.

``diagnostics.py`` imports Home Assistant at module load, which the test
environment does not install, so the handful of names it touches are stubbed
and the module is loaded by path under a synthetic package, as in
``tests/test_auto_resync.py``.
"""
from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

_PKG = "tesla_telemetry_isolated"
_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)

VIN = "7SAXCCE51RF445019"
VEHICLE_ID = "1234567890"
SECRET_PEM = "-----BEGIN EC PRIVATE KEY-----\nsecret\n-----END EC PRIVATE KEY-----"
PROXY_SECRET = "a" * 64


def _module(name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []
    module.__dict__.update(attrs)
    return module


def _redact(value: Any, to_redact: set[str]) -> Any:
    """Stand-in for ``async_redact_data``: mask values of the given keys."""
    if isinstance(value, dict):
        return {
            key: ("**REDACTED**" if key in to_redact else _redact(val, to_redact))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, to_redact) for item in value]
    return value


def _stubs() -> dict[str, ModuleType]:
    components = _module("homeassistant.components")
    diagnostics = _module(
        "homeassistant.components.diagnostics", async_redact_data=_redact
    )
    components.diagnostics = diagnostics
    return {
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": components,
        "homeassistant.components.diagnostics": diagnostics,
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries",
            ConfigEntry=type("ConfigEntry", (), {}),
            ConfigEntryState=SimpleNamespace(LOADED="loaded"),
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=type("HomeAssistant", (), {})
        ),
    }


def _load() -> ModuleType:
    """Load const/models/diagnostics under a synthetic package."""
    if f"{_PKG}.diagnostics" in sys.modules:
        return sys.modules[f"{_PKG}.diagnostics"]

    stubs = _stubs()
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)

    pkg = ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    for name in ("const", "models", "diagnostics"):
        spec = importlib.util.spec_from_file_location(
            f"{_PKG}.{name}", _DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{name}"] = module
        spec.loader.exec_module(module)

    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    return sys.modules[f"{_PKG}.diagnostics"]


def _entry(loaded: bool = True) -> SimpleNamespace:
    entry = SimpleNamespace(
        title=f"Model X ({VIN})",
        version=2,
        data={
            "vin": VIN,
            "vehicle_id": VEHICLE_ID,
            "private_key_pem": SECRET_PEM,
            "proxy_secret": PROXY_SECRET,
            "hostname": "telemetry.example.com",
            "token": {"access_token": "secret-token"},
        },
        options={"allow_vehicle_commands": False},
        state="loaded" if loaded else "not_loaded",
    )
    if loaded:
        entry.runtime_data = SimpleNamespace(
            vin=VIN,
            teslamate=False,
            pushed_intervals={"Location": 5},
            coordinator=SimpleNamespace(
                signals_since_start=10,
                lifetime_signals=100,
                effective_intervals={"Location": 5},
                signal_counts=Counter({"Location": 3}),
                is_stale=lambda _name: False,
            ),
        )
    return entry


def _run(entry: SimpleNamespace) -> dict[str, Any]:
    import asyncio

    module = _load()
    return asyncio.run(module.async_get_config_entry_diagnostics(None, entry))


def test_vin_is_redacted_everywhere_it_appears() -> None:
    result = _run(_entry())
    flat = repr(result)
    assert VIN not in flat, f"VIN leaked in {flat}"
    assert result["entry"]["title"] == "Model X (**REDACTED**)"
    assert result["runtime"]["vin"] == "**REDACTED**"


def test_vehicle_id_is_redacted() -> None:
    result = _run(_entry())
    assert VEHICLE_ID not in repr(result)


def test_secrets_are_redacted() -> None:
    result = _run(_entry())
    flat = repr(result)
    assert SECRET_PEM not in flat
    assert PROXY_SECRET not in flat
    assert "secret-token" not in flat


def test_non_sensitive_fields_survive() -> None:
    """Redaction must not gut the file — it is meant to be useful."""
    result = _run(_entry())
    assert result["entry"]["data"]["hostname"] == "telemetry.example.com"
    assert result["runtime"]["lifetime_signals"] == 100
    assert result["runtime"]["pushed_intervals"] == {"Location": 5}


def test_unloaded_entry_is_reported_not_crashed() -> None:
    result = _run(_entry(loaded=False))
    assert result["runtime"] == "entry is not loaded"
    assert VIN not in repr(result)
