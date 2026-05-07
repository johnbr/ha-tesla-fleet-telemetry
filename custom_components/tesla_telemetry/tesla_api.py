"""Async client for the bits of the Tesla Fleet API this integration needs.

Scope is intentionally tight — only the calls required to provision and
maintain a `fleet_telemetry_config` for one VIN, plus the partner-account
registration endpoint (used once during onboarding).

  * User OAuth handled by HA core via ``OAuth2Session`` — refresh and
    persistence live in HA's framework, not here. We just read the
    current access token and use it as a bearer.
  * Partner OAuth (client_credentials grant) → access token for
    ``/api/1/partner_accounts`` (separate flow from user OAuth)
  * GET /api/1/vehicles                          — verify VIN is on account
  * GET /api/1/vehicles/{vin}/fleet_telemetry_config
  * POST /api/1/vehicles/fleet_telemetry_config_jws — push our config
  * DELETE /api/1/vehicles/{vin}/fleet_telemetry_config
  * POST /api/1/partner_accounts                 — register partner domain
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .const import (
    DEFAULT_REGION,
    FLEET_API_BASE_URLS,
    TESLA_PARTNER_TOKEN_URL,
    TOKEN_REFRESH_LEEWAY,
)
from .crypto import sign_telemetry_config_jwt

_LOGGER = logging.getLogger(__name__)


class TeslaApiError(Exception):
    """Any non-success response from Tesla we couldn't recover from."""

    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"tesla api {status}: {message}")
        self.status = status
        self.body = body


class TeslaAuthError(TeslaApiError):
    """OAuth refresh failed (likely revoked credentials)."""


@dataclass(slots=True)
class TelemetryFieldConfig:
    """One field's streaming policy. `interval_seconds` is the floor —
    Tesla pushes on change but never faster than this."""

    interval_seconds: int

    def to_dict(self) -> dict[str, int]:
        return {"interval_seconds": self.interval_seconds}


@dataclass(slots=True)
class TelemetryConfig:
    """Body of `fleet_telemetry_config_create`. `ca` is the PEM bundle the
    vehicle will validate the receiver's TLS cert against (typically the
    LE root + intermediate). `exp` is a unix timestamp; Tesla rejects
    config requests older than ~24 h, so we set it ~30 days out by
    default and refresh when we re-sync."""

    hostname: str
    port: int
    ca: str
    fields: dict[str, TelemetryFieldConfig]
    alert_types: list[str] = field(default_factory=list)
    exp: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "port": self.port,
            "ca": self.ca,
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "alert_types": list(self.alert_types),
            "exp": self.exp
            if self.exp is not None
            else int(time.time()) + 30 * 24 * 3600,
        }


async def list_vehicles_with_token(
    session: aiohttp.ClientSession,
    access_token: str,
    region: str = DEFAULT_REGION,
) -> list[dict[str, Any]]:
    """One-shot vehicle list using a bearer token directly.

    Used during config flow before we have an OAuth2Session bound to the
    entry. Refresh isn't needed in that brief window — the freshly minted
    access_token has hours of validity.
    """
    if region not in FLEET_API_BASE_URLS:
        raise ValueError(f"unknown region {region!r}")
    url = f"{FLEET_API_BASE_URLS[region]}/api/1/vehicles"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    async with session.get(url, headers=headers) as resp:
        text = await resp.text()
        if resp.status == 401:
            raise TeslaAuthError(401, text[:500] or "unauthorized", text)
        if resp.status >= 400:
            raise TeslaApiError(
                resp.status, text[:500] or resp.reason or "", text
            )
        try:
            data = _json.loads(text) if text else {}
        except ValueError as err:
            raise TeslaApiError(resp.status, f"non-json response: {err}", text)
        return list(data.get("response") or [])


class TeslaApi:
    """Stateful async API client. Safe to share across coroutines on a
    single event loop — the partner-token refresh is guarded by an
    asyncio.Lock; user-token refresh is delegated to HA's OAuth2Session
    which has its own coordination."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        oauth_session: OAuth2Session,
        *,
        client_id: str,
        client_secret: str,
        region: str = DEFAULT_REGION,
        partner_private_key_pem: str | None = None,
    ) -> None:
        if region not in FLEET_API_BASE_URLS:
            raise ValueError(f"unknown region {region!r}")
        self._session = session
        self._oauth_session = oauth_session
        self._client_id = client_id
        self._client_secret = client_secret
        self._region = region
        self._base_url = FLEET_API_BASE_URLS[region]
        self._partner_private_key_pem = partner_private_key_pem

        self._partner_token: str | None = None
        self._partner_expires_at: float = 0.0
        self._partner_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def list_vehicles(self) -> list[dict[str, Any]]:
        data = await self._user_request("GET", "/api/1/vehicles")
        return list(data.get("response") or [])

    async def get_fleet_telemetry_config(self, vin: str) -> dict[str, Any]:
        data = await self._user_request(
            "GET", f"/api/1/vehicles/{vin}/fleet_telemetry_config"
        )
        return dict(data.get("response") or {})

    async def set_fleet_telemetry_config(
        self, vin: str, config: TelemetryConfig
    ) -> dict[str, Any]:
        """Push the config to Tesla's signed-JWT endpoint.

        Tesla rejects the plain ``fleet_telemetry_config`` endpoint with
        ``"This endpoint must be called through the Vehicle Command HTTP
        Proxy"`` and expects the body, signed with the partner private
        key as a Tesla.SS256 JWT, posted to ``..._jws`` instead. We
        implement the signing locally — see ``crypto.py``.
        """
        if not self._partner_private_key_pem:
            raise TeslaApiError(
                0,
                "no partner private key configured — cannot sign telemetry "
                "config (re-create the integration with the PEM populated)",
            )
        token = sign_telemetry_config_jwt(
            self._partner_private_key_pem, config.to_dict()
        )
        body = {"vins": [vin], "token": token}
        data = await self._user_request(
            "POST", "/api/1/vehicles/fleet_telemetry_config_jws", json=body
        )
        return dict(data.get("response") or {})

    async def delete_fleet_telemetry_config(
        self, vin: str
    ) -> dict[str, Any]:
        data = await self._user_request(
            "DELETE", f"/api/1/vehicles/{vin}/fleet_telemetry_config"
        )
        return dict(data.get("response") or {})

    async def register_partner_domain(self, domain: str) -> dict[str, Any]:
        """One-shot during onboarding — call once per partner domain.
        Tesla validates the .well-known public-key URL on `domain` matches
        the keypair the developer app was created with."""
        data = await self._partner_request(
            "POST", "/api/1/partner_accounts", json={"domain": domain}
        )
        return dict(data.get("response") or {})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _user_request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
    ) -> dict[str, Any]:
        token = await self._user_access_token()
        try:
            return await self._raw_request(method, path, token, json)
        except TeslaApiError as err:
            if err.status != 401:
                raise
            _LOGGER.debug(
                "tesla_telemetry: 401 from Tesla, forcing refresh and retrying"
            )
            await self._force_user_refresh()
            token = await self._user_access_token()
            return await self._raw_request(method, path, token, json)

    async def _partner_request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
    ) -> dict[str, Any]:
        token = await self._partner_access_token()
        try:
            return await self._raw_request(method, path, token, json)
        except TeslaApiError as err:
            if err.status != 401:
                raise
            self._partner_token = None
            token = await self._partner_access_token()
            return await self._raw_request(method, path, token, json)

    async def _raw_request(
        self,
        method: str,
        path: str,
        token: str,
        json: Any,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        async with self._session.request(
            method, url, headers=headers, json=json
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise TeslaApiError(resp.status, text[:500] or resp.reason or "", text)
            if not text:
                return {}
            try:
                return _json.loads(text)
            except ValueError as err:
                raise TeslaApiError(resp.status, f"non-json response: {err}", text)

    # ---- user tokens (delegated to HA's OAuth2Session) ----

    async def _user_access_token(self) -> str:
        await self._oauth_session.async_ensure_token_valid()
        return self._oauth_session.token["access_token"]

    async def _force_user_refresh(self) -> None:
        """Pre-expire the cached token so async_ensure_token_valid will
        force a refresh on the next call. HA's OAuth2Session checks
        ``expires_at`` against ``time.time() + 10`` to decide whether to
        refresh, so writing a past timestamp guarantees a refresh."""
        entry = self._oauth_session.config_entry
        token = dict(entry.data.get("token") or {})
        token["expires_at"] = 0
        self._oauth_session.hass.config_entries.async_update_entry(
            entry, data={**entry.data, "token": token}
        )

    # ---- partner tokens (separate client_credentials grant) ----

    async def _partner_access_token(self) -> str:
        if self._partner_token and time.time() < self._partner_expires_at:
            return self._partner_token
        async with self._partner_lock:
            if self._partner_token and time.time() < self._partner_expires_at:
                return self._partner_token
            await self._refresh_partner_token()
            assert self._partner_token is not None
            return self._partner_token

    async def _refresh_partner_token(self) -> None:
        body = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": "openid vehicle_device_data",
            "audience": self._base_url,
        }
        data = await self._post_form(
            TESLA_PARTNER_TOKEN_URL, body, error_cls=TeslaAuthError
        )
        self._partner_token = data["access_token"]
        self._partner_expires_at = (
            time.time() + int(data.get("expires_in", 28800)) - TOKEN_REFRESH_LEEWAY
        )

    async def _post_form(
        self,
        url: str,
        body: dict[str, str],
        *,
        error_cls: type[TeslaApiError] = TeslaApiError,
    ) -> dict[str, Any]:
        async with self._session.post(url, data=body) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise error_cls(resp.status, text[:500] or resp.reason or "", text)
            try:
                return _json.loads(text)
            except ValueError as err:
                raise error_cls(resp.status, f"non-json oauth response: {err}", text)
