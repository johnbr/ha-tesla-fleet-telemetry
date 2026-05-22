"""Config flow for tesla_telemetry.

Uses HA's standard OAuth2 framework via ``application_credentials``:

  1. ``pick_implementation`` (provided by AbstractOAuth2FlowHandler) —
     pick or paste the Tesla developer app's client_id/secret stored in
     HA's application_credentials.
  2. ``auth`` (provided by AbstractOAuth2FlowHandler) — bounce the user
     through Tesla's auth.tesla.com authorize URL and back. HA handles
     the code-for-token swap.
  3. ``vehicle`` — pick the VIN this entry will track. Validated against
     Tesla's `/api/1/vehicles` response and against ``async_set_unique_id``
     to prevent duplicate entries.
  4. ``endpoint`` — public hostname, port, partner domain, proxy shared
     secret, and the partner EC P-256 private key (PEM). These are
     deployment-wide; when adding a second vehicle the step is pre-filled
     from an existing entry.

Each integration goes through OAuth independently and gets its own
refresh-token chain from Tesla — no more rotation race with tesla_fleet.
One config entry is created per vehicle (VIN), each its own HA device.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow, selector

from .const import (
    CONF_HOSTNAME,
    CONF_PARTNER_DOMAIN,
    CONF_PORT,
    CONF_PRIVATE_KEY_PEM,
    CONF_PROXY_SECRET,
    CONF_REGION,
    CONF_VEHICLE_NAME,
    CONF_VIN,
    DEFAULT_REGION,
    DOMAIN,
    FLEET_API_BASE_URLS,
    OAUTH_SCOPES,
)
from .tesla_api import TeslaApiError, TeslaAuthError, list_vehicles_with_token

_LOGGER = logging.getLogger(__name__)


class TeslaTelemetryOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle the Tesla OAuth dance, then continue to vehicle + endpoint steps."""

    DOMAIN = DOMAIN
    # Bumped from 1: v1 entries used a hand-rolled refresh_token grant
    # whose data shape is incompatible with HA's OAuth2 framework.
    # async_migrate_entry returns False for v1 so the user re-creates them.
    VERSION = 2

    def __init__(self) -> None:
        super().__init__()
        self._oauth_data: dict[str, Any] | None = None
        self._region: str = DEFAULT_REGION
        self._vehicles: list[dict[str, Any]] = []
        self._chosen_vin: str | None = None
        # Stable across re-renders so refreshing the form doesn't churn.
        self._default_proxy_secret = secrets.token_hex(32)

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        # Tesla wants `audience` pointing at the regional Fleet API base
        # URL on the authorize call. NA is the only region this
        # integration currently supports — extend if needed.
        return {
            "scope": " ".join(OAUTH_SCOPES),
            "audience": FLEET_API_BASE_URLS[DEFAULT_REGION],
        }

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Hook called by AbstractOAuth2FlowHandler once OAuth completes.
        Stash the token and continue to integration-specific steps before
        actually creating the entry."""
        self._oauth_data = data
        return await self.async_step_vehicle()

    # -------------------- Step: vehicle --------------------
    async def async_step_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._oauth_data is not None

        if not self._vehicles:
            access_token = self._oauth_data["token"]["access_token"]
            session = aiohttp_client.async_get_clientsession(self.hass)
            try:
                self._vehicles = await list_vehicles_with_token(
                    session, access_token, self._region
                )
            except TeslaAuthError as err:
                _LOGGER.warning(
                    "tesla_telemetry: list_vehicles auth_failed status=%s body=%s",
                    err.status,
                    err.body,
                )
                return self.async_abort(reason="oauth_unauthorized")
            except (TeslaApiError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("tesla_telemetry: list_vehicles failed: %s", err)
                return self.async_abort(reason="cannot_connect")
            if not self._vehicles:
                return self.async_abort(reason="no_vehicles")

        if user_input is not None:
            vin = user_input[CONF_VIN]
            if not any(v.get("vin") == vin for v in self._vehicles):
                errors[CONF_VIN] = "vin_not_found"
            else:
                self._chosen_vin = vin
                await self.async_set_unique_id(vin)
                self._abort_if_unique_id_configured()
                return await self.async_step_endpoint()

        vin_options = {
            v["vin"]: f"{v['vin']} — {v.get('display_name') or '(no name)'}"
            for v in self._vehicles
            if v.get("vin")
        }
        schema = vol.Schema({vol.Required(CONF_VIN): vol.In(vin_options)})
        return self.async_show_form(
            step_id="vehicle", data_schema=schema, errors=errors
        )

    # -------------------- Step: endpoint --------------------
    async def async_step_endpoint(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            pem_error = _validate_partner_key_pem(
                user_input.get(CONF_PRIVATE_KEY_PEM, "")
            )
            if pem_error:
                errors[CONF_PRIVATE_KEY_PEM] = pem_error

            if not errors:
                assert self._oauth_data is not None
                vehicle = next(
                    (v for v in self._vehicles if v.get("vin") == self._chosen_vin),
                    {},
                )
                # Falls back to the VIN when the vehicle has no display
                # name; this becomes the HA device name for the vehicle.
                vehicle_name = vehicle.get("display_name") or self._chosen_vin
                title = f"{vehicle_name} ({self._chosen_vin})"
                # ``self._oauth_data`` already holds ``auth_implementation``
                # (which application_credential to use) and ``token`` (the
                # OAuth tokens). HA's OAuth2Session reads both at runtime.
                data = {
                    **self._oauth_data,
                    CONF_VIN: self._chosen_vin,
                    CONF_VEHICLE_NAME: vehicle_name,
                    CONF_REGION: self._region,
                    CONF_HOSTNAME: user_input[CONF_HOSTNAME].strip(),
                    CONF_PORT: int(user_input[CONF_PORT]),
                    CONF_PARTNER_DOMAIN: user_input[CONF_PARTNER_DOMAIN].strip(),
                    CONF_PROXY_SECRET: user_input[CONF_PROXY_SECRET],
                    CONF_PRIVATE_KEY_PEM: user_input[CONF_PRIVATE_KEY_PEM],
                }
                return self.async_create_entry(title=title, data=data)

        # The endpoint is deployment-wide — same nginx, same partner key —
        # so when the user adds a second (or later) vehicle, pre-fill the
        # form from an existing entry to make it a click-through. After a
        # validation error, keep what the user just typed instead.
        if user_input is not None:
            suggested: dict[str, Any] = user_input
        else:
            existing = self._async_current_entries()
            suggested = dict(existing[0].data) if existing else {}

        schema = vol.Schema(
            {
                vol.Required(CONF_HOSTNAME): str,
                vol.Required(CONF_PORT, default=443): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_PARTNER_DOMAIN): str,
                vol.Required(
                    CONF_PROXY_SECRET, default=self._default_proxy_secret
                ): str,
                vol.Required(CONF_PRIVATE_KEY_PEM): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
            }
        )
        return self.async_show_form(
            step_id="endpoint",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )


def _validate_partner_key_pem(pem: str) -> str | None:
    """Return an error key if the PEM is not a valid EC P-256 private key."""
    if not pem.strip():
        return "bad_key"
    try:
        # Imported lazily so the module loads even if cryptography is
        # missing during tooling that doesn't run the form.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = serialization.load_pem_private_key(
            pem.encode("utf-8"), password=None
        )
    except Exception:  # noqa: BLE001 — any parse failure means bad input
        return "bad_key"
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        return "wrong_curve"
    if key.curve.name != "secp256r1":
        return "wrong_curve"
    return None
