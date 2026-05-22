"""The Tesla Fleet Telemetry custom integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

from .const import (
    CONF_PRIVATE_KEY_PEM,
    CONF_PROXY_SECRET,
    CONF_REGION,
    CONF_VEHICLE_NAME,
    CONF_VIN,
    DEFAULT_REGION,
    DOMAIN,
)
from .coordinator import TeslaTelemetryCoordinator
from .receiver import TeslaTelemetryView
from .services import async_register_services, async_schedule_auto_resync
from .tesla_api import TeslaApi

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up tesla_telemetry from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})

    vin: str = entry.data[CONF_VIN]
    proxy_secret: str = entry.data.get(CONF_PROXY_SECRET, "")
    # Entries created before CONF_VEHICLE_NAME existed fall back to the
    # entry title (minus the " (VIN)" suffix the config flow appends).
    vehicle_name: str = (
        entry.data.get(CONF_VEHICLE_NAME)
        or entry.title.removesuffix(f" ({vin})")
        or vin
    )

    # Resolve the application_credentials-backed OAuth implementation and
    # build the long-lived session HA's framework will refresh through.
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    oauth_session = config_entry_oauth2_flow.OAuth2Session(
        hass, entry, implementation
    )
    # Ensure we hold a fresh access token before the first API call —
    # also surfaces auth errors at setup time rather than mid-bootstrap.
    await oauth_session.async_ensure_token_valid()

    # ``LocalOAuth2Implementation`` (the standard application_credentials
    # backing) exposes client_id / client_secret directly. We need them
    # for the partner client_credentials grant used by partner_accounts.
    client_id = getattr(implementation, "client_id", "")
    client_secret = getattr(implementation, "client_secret", "")

    coordinator = TeslaTelemetryCoordinator(hass, vin, vehicle_name)

    api = TeslaApi(
        aiohttp_client.async_get_clientsession(hass),
        oauth_session,
        client_id=client_id,
        client_secret=client_secret,
        region=entry.data.get(CONF_REGION, DEFAULT_REGION),
        partner_private_key_pem=entry.data.get(CONF_PRIVATE_KEY_PEM),
    )

    # The HTTP view is registered once per HA instance and routes incoming
    # WS connections to the right coordinator by VIN. Multiple entries
    # (one per vehicle) share the same view.
    coordinators_by_vin: dict[str, TeslaTelemetryCoordinator] = (
        domain_data.setdefault("coordinators_by_vin", {})
    )
    coordinators_by_vin[vin] = coordinator

    if "view" not in domain_data:
        view = TeslaTelemetryView(coordinators_by_vin, proxy_secret)
        hass.http.register_view(view)
        domain_data["view"] = view
        _LOGGER.info(
            "tesla_telemetry: WebSocket view registered at /api/tesla_telemetry/ws"
        )
    elif proxy_secret and proxy_secret != domain_data["view"]._proxy_secret:
        _LOGGER.warning(
            "tesla_telemetry: entry %s has a different proxy secret than the "
            "first entry; the first secret remains in effect",
            entry.entry_id,
        )

    domain_data[entry.entry_id] = {
        "coordinator": coordinator,
        "vin": vin,
        "api": api,
    }

    async_register_services(hass)

    # Daily check that re-pushes the telemetry config when it's >7 days old.
    # No-op until the user has run `bootstrap` at least once.
    entry.async_on_unload(async_schedule_auto_resync(hass, entry))

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(
            entry, PLATFORMS
        )
    else:
        unload_ok = True

    if unload_ok:
        domain_data = hass.data.get(DOMAIN, {})
        record = domain_data.pop(entry.entry_id, None)
        if record is not None:
            domain_data.get("coordinators_by_vin", {}).pop(record["vin"], None)
        # The view + services stay registered: HA does not support
        # unregistering them without a restart. With no entries left, the
        # view's empty routing table will reject any subsequent
        # connections with 403, and the services raise ServiceValidationError.
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Block v1 entries — they used a hand-rolled OAuth flow whose data
    shape is incompatible with HA's OAuth2 framework. Returning False
    leaves the entry in a setup-failed state and prompts the user to
    re-create it (which will go through the new application_credentials
    flow and obtain an independent grant from Tesla)."""
    if entry.version < 2:
        _LOGGER.error(
            "tesla_telemetry: config entry %s was created against the old "
            "hand-rolled OAuth path (v%d). Delete this entry and re-add the "
            "integration — it now uses HA's application_credentials so it "
            "no longer fights tesla_fleet over the refresh token.",
            entry.entry_id,
            entry.version,
        )
        return False
    return True
