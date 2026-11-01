"""Guards on the shipped tree that review caught by hand.

* The ``proto/`` directory redistributes Apache-2.0 files from
  teslamotors/fleet-telemetry, so the upstream license must travel with them.
* The ``set_interval_preset`` description must name exactly the signals the
  ``high_rate`` preset overrides, so the UI can't promise more than it does.

``const.py`` is stdlib only and loaded by path, as in the other tests. Service
descriptions live in ``strings.json`` and its translations, so the high-rate
guard reads every shipped language as JSON.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

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


def _description_files() -> list[Path]:
    """``strings.json`` plus every shipped translation.

    The high_rate paragraph is translated too, so a guard that only reads the
    English source would let the translations drift away from what the preset
    actually does.
    """
    return [_DIR / "strings.json", *sorted((_DIR / "translations").glob("*.json"))]


def _high_rate_paragraph(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    description = data["services"]["set_interval_preset"]["description"]
    match = re.search(r"`high_rate`(.*)", description, re.DOTALL)
    assert match, f"{path.name}: lost its high_rate paragraph"
    return match.group(1)


@pytest.mark.parametrize("path", _description_files(), ids=lambda p: p.name)
def test_high_rate_description_names_every_overridden_signal(path: Path) -> None:
    paragraph = _high_rate_paragraph(path)
    overridden = const.INTERVAL_PRESET_OVERRIDES[const.INTERVAL_PRESET_HIGH_RATE]
    for signal in overridden:
        assert signal in paragraph, (
            f"{path.name}: {signal} missing from the high_rate text"
        )


@pytest.mark.parametrize("path", _description_files(), ids=lambda p: p.name)
def test_high_rate_description_names_no_other_signal(path: Path) -> None:
    paragraph = _high_rate_paragraph(path)
    overridden = set(
        const.INTERVAL_PRESET_OVERRIDES[const.INTERVAL_PRESET_HIGH_RATE]
    )
    for signal in const.DEFAULT_INTERVALS_SECONDS:
        if signal in overridden:
            continue
        assert not re.search(rf"\b{re.escape(signal)}\b", paragraph), (
            f"{path.name}: high_rate text mentions {signal}, which the preset "
            "leaves alone"
        )
