"""Config flow for tesla_telemetry.

Uses HA's standard OAuth2 framework via ``application_credentials``:

  1. ``pick_implementation`` (provided by AbstractOAuth2FlowHandler) —
     pick or paste the Tesla developer app's client_id/secret stored in
     HA's application_credentials.
  2. ``auth`` (provided by AbstractOAuth2FlowHandler) — bounce the user
     through Tesla's auth.tesla.com authorize URL and back. HA handles
     the code-for-token swap.
  3. ``region`` — confirm the account's Fleet API region (NA/EU),
     preselected from the ``ou_code`` claim in the access-token JWT.
  4. ``vehicle`` — pick the VIN this entry will track. Validated against
     Tesla's `/api/1/vehicles` response and against ``async_set_unique_id``
     to prevent duplicate entries.
  5. ``endpoint`` — public hostname, port, partner domain, proxy shared
     secret, and the partner EC P-256 private key (PEM). These are
     deployment-wide; when adding a second vehicle the step is pre-filled
     from an existing entry.

Each integration goes through OAuth independently and gets its own
refresh-token chain from Tesla — no more rotation race with tesla_fleet.
One config entry is created per vehicle (VIN), each its own HA device.

Re-auth (``reauth`` → ``reauth_confirm`` → the same OAuth steps) replaces
only the token. It exists for the "Allow vehicle commands" option: setup
asks Tesla for read-only scopes, and a re-auth of an entry with that
option on asks for ``vehicle_cmds`` as well.
"""
from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow, selector

from .const import (
    CONF_ALLOW_VEHICLE_COMMANDS,
    CONF_HOSTNAME,
    CONF_PARTNER_DOMAIN,
    CONF_PORT,
    CONF_PRIVATE_KEY_PEM,
    CONF_PROXY_SECRET,
    CONF_REGION,
    CONF_VEHICLE_NAME,
    CONF_VIN,
    DEFAULT_ALLOW_VEHICLE_COMMANDS,
    DEFAULT_REGION,
    DOMAIN,
    SELECTABLE_REGIONS,
    oauth_scopes,
    region_from_access_token,
)
from .signals import build_options_schema, parse_options_input
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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TeslaTelemetryOptionsFlow:
        return TeslaTelemetryOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Flow entry point. Region is asked after OAuth: the authorize
        request is region-independent, and the access token's ``ou_code``
        claim then preselects the region for confirmation."""
        return await self.async_step_pick_implementation()

    async def async_step_region(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the account region. Preselected from the ``ou_code``
        claim of the freshly minted access token (the same detection HA
        core's tesla_fleet uses); the region selects the regional Fleet
        API base URL used for every API call from here on (vehicle list,
        telemetry config, partner registration)."""
        if user_input is not None:
            self._region = user_input[CONF_REGION]
            return await self.async_step_vehicle()
        schema = vol.Schema(
            {
                vol.Required(CONF_REGION, default=self._region): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SELECTABLE_REGIONS),
                        translation_key="region",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="region", data_schema=schema)

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        # No `audience` on the authorize call. Per Tesla's docs it is
        # required only on the partner client_credentials grant (where
        # TeslaApi sends it, scoped to the entry's regional Fleet API
        # base URL). User tokens minted without an audience work against
        # every regional Fleet API — HA core's tesla_fleet and TeslaMate
        # both authorize without one.
        #
        # Read-only scopes unless this is a re-auth of an entry whose "Allow
        # vehicle commands" option is on — the one way `vehicle_cmds` is
        # ever requested.
        allow = False
        if self.source == SOURCE_REAUTH:
            allow = bool(
                self._get_reauth_entry().options.get(
                    CONF_ALLOW_VEHICLE_COMMANDS, DEFAULT_ALLOW_VEHICLE_COMMANDS
                )
            )
        return {"scope": " ".join(oauth_scopes(allow))}

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Started by the integration when an entry's token lacks a scope it
        needs (see ``__init__._async_check_command_scope``), or by HA."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={"name": self._get_reauth_entry().title},
            )
        return await self.async_step_user()

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Hook called by AbstractOAuth2FlowHandler once OAuth completes.
        Stash the token, preselect the region from the ``ou_code`` claim,
        and continue to integration-specific steps before actually
        creating the entry. A re-auth instead swaps the token into the
        existing entry, once the new grant is shown to reach its vehicle."""
        if self.source == SOURCE_REAUTH:
            return await self._async_finish_reauth(data)
        self._oauth_data = data
        detected = region_from_access_token(
            data["token"].get("access_token") or ""
        )
        if detected is not None:
            self._region = detected
        return await self.async_step_region()

    async def _async_finish_reauth(
        self, data: dict[str, Any]
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        session = aiohttp_client.async_get_clientsession(self.hass)
        try:
            vehicles = await list_vehicles_with_token(
                session,
                data["token"].get("access_token") or "",
                entry.data.get(CONF_REGION, DEFAULT_REGION),
            )
        except TeslaAuthError:
            return self.async_abort(reason="oauth_unauthorized")
        except (TimeoutError, TeslaApiError, aiohttp.ClientError) as err:
            _LOGGER.warning("tesla_telemetry: reauth list_vehicles failed: %s", err)
            return self.async_abort(reason="cannot_connect")
        # A different Tesla account would silently orphan this vehicle.
        if not any(v.get("vin") == entry.data[CONF_VIN] for v in vehicles):
            return self.async_abort(reason="reauth_wrong_account")
        return self.async_update_reload_and_abort(
            entry,
            data_updates={
                "auth_implementation": data["auth_implementation"],
                "token": data["token"],
            },
        )

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
            except (TimeoutError, TeslaApiError, aiohttp.ClientError) as err:
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


class TeslaTelemetryOptionsFlow(OptionsFlow):
    """Per-signal telemetry configuration, estimated-cost rate, and the
    "Allow vehicle commands" switch.

    A single form, built by :func:`signals.build_options_schema`, with one
    collapsible section per signal category. Each signal is a minimum-refresh
    interval in seconds; 0 disables it. Any signal from the full Tesla catalog
    can be added via the picker. Saving writes ``entry.options`` and the
    entry's update listener re-pushes the config to the vehicle.

    Also carries the estimated-cost rate (Tesla bills per streaming signal; the
    default mirrors the published US rate of ~$1 / 150,000 signals), read live
    by ``EstimatedSignalCostSensor``.

    "Allow vehicle commands" (default off) gates the ``navigate`` service.
    Turning it on starts a re-auth when the entry's token lacks
    ``vehicle_cmds`` — see ``__init__._async_check_command_scope``.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = parse_options_input(self.config_entry, user_input)
            commands = user_input.get("vehicle_commands") or {}
            options[CONF_ALLOW_VEHICLE_COMMANDS] = bool(
                commands.get(
                    CONF_ALLOW_VEHICLE_COMMANDS,
                    self.config_entry.options.get(
                        CONF_ALLOW_VEHICLE_COMMANDS, DEFAULT_ALLOW_VEHICLE_COMMANDS
                    ),
                )
            )
            return self.async_create_entry(title="", data=options)
        allowed = self.config_entry.options.get(
            CONF_ALLOW_VEHICLE_COMMANDS, DEFAULT_ALLOW_VEHICLE_COMMANDS
        )
        schema = build_options_schema(self.config_entry).extend(
            {
                vol.Required("vehicle_commands"): data_entry_flow.section(
                    vol.Schema(
                        {
                            vol.Optional(
                                CONF_ALLOW_VEHICLE_COMMANDS, default=allowed
                            ): bool,
                        }
                    ),
                    {"collapsed": not allowed},
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


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
