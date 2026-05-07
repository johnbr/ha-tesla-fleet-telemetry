"""HA services exposed by tesla_telemetry.

Three services, all keyed by an optional `entry_id` (the integration
auto-resolves when there's exactly one entry configured):

  * ``bootstrap``                — one-time onboarding. Calls
    ``register_partner_domain`` (skippable via flag for accounts already
    registered through another integration) and pushes an initial
    ``set_fleet_telemetry_config`` so the vehicle starts streaming.
  * ``resync_telemetry_config``  — re-pushes the same config. Tesla's
    ``exp`` field is ~30 days. The integration also auto-checks daily
    and re-pushes when the last sync is more than 7 days old, so manual
    invocation is rarely needed.
  * ``dump_public_key``          — emits the EC P-256 public key derived
    from the configured private key, ready to host at the partner
    domain's ``.well-known`` path. Returns the PEM as a service response.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    AUTO_RESYNC_CHECK_INTERVAL_SECONDS,
    AUTO_RESYNC_MAX_AGE_SECONDS,
    CONF_HOSTNAME,
    CONF_INTERVAL_PRESET,
    CONF_LAST_SYNC_AT,
    CONF_PARTNER_DOMAIN,
    CONF_PORT,
    CONF_PRIVATE_KEY_PEM,
    CONF_VIN,
    DEFAULT_INTERVALS_SECONDS,
    DOMAIN,
    INTERVAL_PRESET_DEFAULT,
    INTERVAL_PRESET_OVERRIDES,
)
from .tesla_api import TelemetryConfig, TelemetryFieldConfig, TeslaApi
from .tls_ca import DEFAULT_CA_BUNDLE_PEM

_LOGGER = logging.getLogger(__name__)

SERVICE_BOOTSTRAP = "bootstrap"
SERVICE_RESYNC = "resync_telemetry_config"
SERVICE_DUMP_PUBLIC_KEY = "dump_public_key"
SERVICE_GET_CONFIG = "get_telemetry_config"
SERVICE_SET_INTERVAL_PRESET = "set_interval_preset"

ATTR_ENTRY_ID = "entry_id"
ATTR_CA_PEM = "ca_pem"
ATTR_REGISTER_PARTNER = "register_partner_domain"
ATTR_PRESET = "preset"

_BOOTSTRAP_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        vol.Optional(ATTR_CA_PEM): str,
        vol.Optional(ATTR_REGISTER_PARTNER, default=True): bool,
    }
)

_RESYNC_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        vol.Optional(ATTR_CA_PEM): str,
    }
)

_DUMP_KEY_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): str})

_GET_CONFIG_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): str})

_SET_PRESET_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): str,
        vol.Required(ATTR_PRESET): vol.In(list(INTERVAL_PRESET_OVERRIDES)),
        vol.Optional(ATTR_CA_PEM): str,
    }
)


def _resolve_entry(hass: HomeAssistant, entry_id: str | None) -> ConfigEntry:
    """Return the entry the service call is targeting."""
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                f"unknown tesla_telemetry config entry: {entry_id}"
            )
        return entry
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("no tesla_telemetry config entries")
    if len(entries) > 1:
        raise ServiceValidationError(
            "multiple tesla_telemetry entries — pass `entry_id`"
        )
    return entries[0]


def _entry_api(hass: HomeAssistant, entry: ConfigEntry) -> TeslaApi:
    record = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not record or record.get("api") is None:
        raise HomeAssistantError(
            f"entry {entry.entry_id} has no API client — was setup_entry called?"
        )
    return record["api"]


def _resolve_intervals(entry: ConfigEntry) -> dict[str, int]:
    """Apply the entry's saved preset overrides on top of the defaults.

    The resulting dict is what we push to Tesla — defaults for everything,
    overridden per-signal by the active preset.
    """
    preset = entry.data.get(CONF_INTERVAL_PRESET, INTERVAL_PRESET_DEFAULT)
    overrides = INTERVAL_PRESET_OVERRIDES.get(preset, {})
    intervals = dict(DEFAULT_INTERVALS_SECONDS)
    intervals.update(overrides)
    return intervals


def _build_telemetry_config(entry: ConfigEntry, ca_pem: str) -> TelemetryConfig:
    return TelemetryConfig(
        hostname=entry.data[CONF_HOSTNAME],
        port=int(entry.data[CONF_PORT]),
        ca=ca_pem,
        fields={
            name: TelemetryFieldConfig(interval_seconds=interval)
            for name, interval in _resolve_intervals(entry).items()
        },
    )


def _stamp_last_sync(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Record the unix timestamp of a successful telemetry config push.
    Survives restarts so auto-resync can decide whether to fire."""
    new_data = {**entry.data, CONF_LAST_SYNC_AT: int(time.time())}
    hass.config_entries.async_update_entry(entry, data=new_data)


async def _bootstrap_handler(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry = _resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
    api = _entry_api(hass, entry)

    response: dict[str, Any] = {"vin": entry.data[CONF_VIN]}

    if call.data.get(ATTR_REGISTER_PARTNER, True):
        domain = entry.data[CONF_PARTNER_DOMAIN]
        try:
            response["partner_register"] = await api.register_partner_domain(
                domain
            )
        except Exception as err:  # noqa: BLE001 — partner reg is best-effort
            _LOGGER.warning(
                "tesla_telemetry: partner_accounts/register failed for %s: %s "
                "(continuing — re-run with register_partner_domain: false if "
                "the partner is already registered through another integration)",
                domain,
                err,
            )
            response["partner_register_error"] = str(err)

    ca_pem = (call.data.get(ATTR_CA_PEM) or DEFAULT_CA_BUNDLE_PEM).strip() + "\n"
    cfg = _build_telemetry_config(entry, ca_pem)
    response["telemetry_config"] = await api.set_fleet_telemetry_config(
        entry.data[CONF_VIN], cfg
    )
    _stamp_last_sync(hass, entry)
    _LOGGER.info(
        "tesla_telemetry: bootstrap completed for vin=%s — %s",
        entry.data[CONF_VIN],
        response["telemetry_config"],
    )
    return response


async def _resync_handler(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry = _resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
    api = _entry_api(hass, entry)
    ca_pem = (call.data.get(ATTR_CA_PEM) or DEFAULT_CA_BUNDLE_PEM).strip() + "\n"
    cfg = _build_telemetry_config(entry, ca_pem)
    response = await api.set_fleet_telemetry_config(entry.data[CONF_VIN], cfg)
    _stamp_last_sync(hass, entry)
    _LOGGER.info(
        "tesla_telemetry: resync completed for vin=%s — %s",
        entry.data[CONF_VIN],
        response,
    )
    return {"vin": entry.data[CONF_VIN], "telemetry_config": response}


async def _get_config_handler(call: ServiceCall) -> ServiceResponse:
    """Query Tesla's GET /api/1/vehicles/{vin}/fleet_telemetry_config so the
    user can verify whether the car has synced our pushed config without
    having to fish a bearer token out of `.storage`."""
    hass = call.hass
    entry = _resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
    api = _entry_api(hass, entry)
    vin = entry.data[CONF_VIN]
    response = await api.get_fleet_telemetry_config(vin)
    _LOGGER.info(
        "tesla_telemetry: get_fleet_telemetry_config vin=%s synced=%s",
        vin,
        response.get("synced"),
    )
    return {"vin": vin, "telemetry_config": response}


async def _set_interval_preset_handler(call: ServiceCall) -> ServiceResponse:
    """Switch the telemetry interval preset and re-push the config.

    The preset name is persisted in entry.data so a HA restart preserves the
    user's choice. Auto-resync also honours it.
    """
    hass = call.hass
    entry = _resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
    api = _entry_api(hass, entry)
    preset: str = call.data[ATTR_PRESET]

    new_data = {**entry.data, CONF_INTERVAL_PRESET: preset}
    hass.config_entries.async_update_entry(entry, data=new_data)
    # _build_telemetry_config now reads the just-saved preset.
    entry = hass.config_entries.async_get_entry(entry.entry_id)  # type: ignore[assignment]
    assert entry is not None

    ca_pem = (call.data.get(ATTR_CA_PEM) or DEFAULT_CA_BUNDLE_PEM).strip() + "\n"
    cfg = _build_telemetry_config(entry, ca_pem)
    response = await api.set_fleet_telemetry_config(entry.data[CONF_VIN], cfg)
    _stamp_last_sync(hass, entry)
    intervals = _resolve_intervals(entry)
    _LOGGER.info(
        "tesla_telemetry: interval preset=%s applied for vin=%s — %s",
        preset,
        entry.data[CONF_VIN],
        response,
    )
    return {
        "vin": entry.data[CONF_VIN],
        "preset": preset,
        "intervals": intervals,
        "telemetry_config": response,
    }


async def _dump_public_key_handler(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry = _resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
    pem = entry.data.get(CONF_PRIVATE_KEY_PEM, "")
    if not pem.strip():
        raise HomeAssistantError("entry has no private key configured")

    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except Exception as err:  # noqa: BLE001
        raise HomeAssistantError(f"could not parse private key: {err}") from err

    pub_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    domain = entry.data.get(CONF_PARTNER_DOMAIN, "<partner-domain>")
    _LOGGER.info(
        "tesla_telemetry: host the following public key at "
        "https://%s/.well-known/appspecific/com.tesla.3p.public-key.pem\n%s",
        domain,
        pub_pem,
    )
    return {"partner_domain": domain, "public_key_pem": pub_pem}


def async_register_services(hass: HomeAssistant) -> None:
    """Register all tesla_telemetry services. Idempotent — safe to call
    on every entry setup."""
    if not hass.services.has_service(DOMAIN, SERVICE_BOOTSTRAP):
        hass.services.async_register(
            DOMAIN,
            SERVICE_BOOTSTRAP,
            _bootstrap_handler,
            schema=_BOOTSTRAP_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RESYNC):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RESYNC,
            _resync_handler,
            schema=_RESYNC_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_DUMP_PUBLIC_KEY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_DUMP_PUBLIC_KEY,
            _dump_public_key_handler,
            schema=_DUMP_KEY_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_GET_CONFIG):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_CONFIG,
            _get_config_handler,
            schema=_GET_CONFIG_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_INTERVAL_PRESET):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_INTERVAL_PRESET,
            _set_interval_preset_handler,
            schema=_SET_PRESET_SCHEMA,
            supports_response=SupportsResponse.OPTIONAL,
        )


# ---------------------------------------------------------------------
# Auto-resync
# ---------------------------------------------------------------------
async def _auto_resync_if_due(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-push fleet_telemetry_config when the last successful sync is
    older than ``AUTO_RESYNC_MAX_AGE_SECONDS``. Skips silently if the
    user hasn't bootstrapped yet (no ``last_sync_at`` recorded) — we
    don't push a config they haven't authorized."""
    last = entry.data.get(CONF_LAST_SYNC_AT)
    if not last:
        return
    age = time.time() - last
    if age < AUTO_RESYNC_MAX_AGE_SECONDS:
        return
    try:
        api = _entry_api(hass, entry)
    except HomeAssistantError as err:
        _LOGGER.debug("tesla_telemetry: auto-resync skipped — %s", err)
        return
    cfg = _build_telemetry_config(entry, DEFAULT_CA_BUNDLE_PEM.strip() + "\n")
    try:
        result = await api.set_fleet_telemetry_config(entry.data[CONF_VIN], cfg)
    except Exception as err:  # noqa: BLE001 — never surface from a timer tick
        _LOGGER.warning(
            "tesla_telemetry: auto-resync failed for vin=%s after %ds: %s",
            entry.data[CONF_VIN],
            int(age),
            err,
        )
        return
    _stamp_last_sync(hass, entry)
    _LOGGER.info(
        "tesla_telemetry: auto-resync ok for vin=%s after %ds: %s",
        entry.data[CONF_VIN],
        int(age),
        result,
    )


@callback
def async_schedule_auto_resync(
    hass: HomeAssistant, entry: ConfigEntry
) -> Any:
    """Register the daily resync timer for an entry. Returns the cancel
    callable HA's entry-unload machinery should invoke when the entry
    goes away."""

    async def _tick(now: datetime) -> None:
        await _auto_resync_if_due(hass, entry)

    return async_track_time_interval(
        hass, _tick, timedelta(seconds=AUTO_RESYNC_CHECK_INTERVAL_SECONDS)
    )
