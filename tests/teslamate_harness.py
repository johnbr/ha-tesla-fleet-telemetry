"""Loads the ``teslamate`` subpackage for tests, with Home Assistant stubbed.

The test environment does not install Home Assistant or aiohttp (see
``test_signals.py`` for the same constraint), but the bridge imports both at
module load. The handful of names it touches are stubbed here and the modules
are loaded by path under a synthetic package, so the tests can exercise the
real code rather than a copy of it.

Not a test module itself — ``test_teslamate_vehicle_id.py`` and
``test_teslamate_bridge.py`` both import it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

_PKG = "tesla_telemetry_teslamate_isolated"
_DIR = (
    Path(__file__).resolve().parents[1] / "custom_components" / "tesla_telemetry"
)


def _module(name: str, **attrs: Any) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []
    module.__dict__.update(attrs)
    return module


def _stub(name: str, **attrs: Any) -> None:
    sys.modules[name] = _module(name, **attrs)


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


def _install_stubs() -> None:
    def _callback(fn: Any) -> Any:
        return fn

    async def _connect(*_args: Any, **_kwargs: Any) -> Any:
        return lambda: None

    class _View:
        pass

    _stub("homeassistant")
    _stub("homeassistant.config_entries", ConfigEntry=object)
    _stub("homeassistant.core", HomeAssistant=object, callback=_callback)
    _stub("homeassistant.helpers")
    _stub(
        "homeassistant.helpers.dispatcher",
        async_dispatcher_connect=_connect,
        async_dispatcher_send=lambda *_args, **_kwargs: None,
    )
    _stub("homeassistant.helpers.device_registry", DeviceInfo=dict)
    _stub("homeassistant.components")
    _stub("homeassistant.components.http", HomeAssistantView=_View)
    _stub(
        "aiohttp",
        WSMsgType=SimpleNamespace(TEXT="text"),
        web=SimpleNamespace(),
    )


def load() -> ModuleType:
    """The ``teslamate`` package, with ``.protocol`` and ``.bridge`` loaded."""
    if f"{_PKG}.teslamate" in sys.modules:
        return sys.modules[f"{_PKG}.teslamate"]

    _install_stubs()
    pkg = ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    _load(f"{_PKG}.const", _DIR / "const.py")
    _load(f"{_PKG}.proto", _DIR / "proto" / "__init__.py", package=True)
    _load(f"{_PKG}.proto.vehicle_data_pb2", _DIR / "proto" / "vehicle_data_pb2.py")
    _load(f"{_PKG}.values", _DIR / "values.py")
    _load(f"{_PKG}.coordinator", _DIR / "coordinator.py")
    _load(f"{_PKG}.teslamate", _DIR / "teslamate" / "__init__.py", package=True)
    _load(f"{_PKG}.teslamate.protocol", _DIR / "teslamate" / "protocol.py")
    _load(f"{_PKG}.teslamate.bridge", _DIR / "teslamate" / "bridge.py")
    return sys.modules[f"{_PKG}.teslamate"]


def vehicle_data():
    """The generated protobuf module, for building ``Value`` messages."""
    load()
    return sys.modules[f"{_PKG}.proto.vehicle_data_pb2"]
