"""Tests for region constants and token-based region detection (``const.py``).

The config flow preselects the account region from the ``ou_code`` claim of
the access-token JWT. ``const.py`` is pure (stdlib only), so it is loaded
directly by path under a synthetic package, with no Home Assistant installed.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

# --- Load const.py in isolation under a synthetic package -------------------
_PKG = "tesla_telemetry_isolated"
_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
)


def _load_isolated() -> ModuleType:
    if f"{_PKG}.const" in sys.modules:
        return sys.modules[f"{_PKG}.const"]
    pkg = ModuleType(_PKG)
    pkg.__path__ = [str(_DIR)]
    sys.modules[_PKG] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.const", _DIR / "const.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.const"] = module
    spec.loader.exec_module(module)
    return sys.modules[f"{_PKG}.const"]


const = _load_isolated()


def _make_token(claims: dict) -> str:
    """A syntactically valid, unsigned JWT with the given claims."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.signature"


# ---------------------------------------------------------------------------
# region tables
# ---------------------------------------------------------------------------
def test_partner_token_url_is_global_host() -> None:
    # The client_credentials endpoint is the same host for every region;
    # regional scoping happens via the `audience` in the request body.
    assert const.TESLA_PARTNER_TOKEN_URL == (
        "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    )


def test_selectable_regions_subset_of_known_regions() -> None:
    assert set(const.SELECTABLE_REGIONS) <= set(const.FLEET_API_BASE_URLS)
    assert const.REGION_CN not in const.SELECTABLE_REGIONS


def test_ou_codes_map_to_regions() -> None:
    assert set(const.TOKEN_OU_CODES.values()) == set(const.FLEET_API_BASE_URLS)


# ---------------------------------------------------------------------------
# region_from_access_token
# ---------------------------------------------------------------------------
def test_detects_na_eu_cn() -> None:
    assert const.region_from_access_token(_make_token({"ou_code": "NA"})) == "na"
    assert const.region_from_access_token(_make_token({"ou_code": "EU"})) == "eu"
    assert const.region_from_access_token(_make_token({"ou_code": "cn"})) == "cn"


def test_unknown_or_missing_ou_code_returns_none() -> None:
    assert const.region_from_access_token(_make_token({"ou_code": "XX"})) is None
    assert const.region_from_access_token(_make_token({"sub": "abc"})) is None


def test_malformed_tokens_return_none() -> None:
    assert const.region_from_access_token("") is None
    assert const.region_from_access_token("not-a-jwt") is None
    assert const.region_from_access_token("a.!!!.c") is None  # bad base64/json
