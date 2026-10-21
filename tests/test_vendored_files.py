"""Guards on the shipped tree that review caught by hand.

* The ``proto/`` directory redistributes Apache-2.0 files from
  teslamotors/fleet-telemetry, so the upstream license must travel with them.
* The ``set_interval_preset`` description must name exactly the signals the
  ``high_rate`` preset overrides, so the UI can't promise more than it does.

``const.py`` is stdlib only and loaded by path, as in the other tests; PyYAML
isn't a test dependency, so ``services.yaml`` is read as text.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

_PKG = "tesla_telemetry_isolated"
_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)


def _load_const() -> ModuleType:
    if f"{_PKG}.const" in sys.modules:
        return sys.modules[f"{_PKG}.const"]
    pkg = sys.modules.get(_PKG) or ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.const", _DIR / "const.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.const"] = module
    spec.loader.exec_module(module)
    return module


const = _load_const()


def test_proto_dir_ships_upstream_apache_license() -> None:
    license_text = (_DIR / "proto" / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in license_text


def test_proto_readme_attributes_upstream() -> None:
    readme = (_DIR / "proto" / "README.md").read_text(encoding="utf-8")
    assert "teslamotors/fleet-telemetry" in readme
    assert "Apache License 2.0" in readme
    assert "[`LICENSE`](LICENSE)" in readme


def _high_rate_paragraph() -> str:
    text = (_DIR / "services.yaml").read_text(encoding="utf-8")
    match = re.search(r"`high_rate`(.*?)\n\s*\n", text, re.DOTALL)
    assert match, "set_interval_preset description lost its high_rate paragraph"
    return match.group(1)


def test_high_rate_description_names_every_overridden_signal() -> None:
    paragraph = _high_rate_paragraph()
    overridden = const.INTERVAL_PRESET_OVERRIDES[const.INTERVAL_PRESET_HIGH_RATE]
    for signal in overridden:
        assert signal in paragraph, f"{signal} missing from the high_rate text"


def test_high_rate_description_names_no_other_signal() -> None:
    paragraph = _high_rate_paragraph()
    overridden = set(
        const.INTERVAL_PRESET_OVERRIDES[const.INTERVAL_PRESET_HIGH_RATE]
    )
    for signal in const.DEFAULT_INTERVALS_SECONDS:
        if signal in overridden:
            continue
        assert not re.search(rf"\b{re.escape(signal)}\b", paragraph), (
            f"high_rate text mentions {signal}, which the preset leaves alone"
        )
