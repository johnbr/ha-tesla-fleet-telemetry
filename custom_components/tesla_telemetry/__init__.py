"""The Tesla Fleet Telemetry custom integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import aiohttp_client, config_entry_oauth2_flow

from .const import (
    CONF_ALLOW_VEHICLE_COMMANDS,
    CONF_LAST_SYNC_AT,
    CONF_PRIVATE_KEY_PEM,
    CONF_PROXY_SECRET,
    CONF_REGION,
    CONF_VEHICLE_NAME,
    CONF_VIN,
    DEFAULT_ALLOW_VEHICLE_COMMANDS,
    DEFAULT_REGION,
    DOMAIN,
)
from .coordinator import TeslaTelemetryCoordinator
from .receiver import TeslaTelemetryView
from .services import (
    async_register_services,
    async_schedule_auto_resync,
    token_allows_commands,
)
from .signals import resolve_effective_intervals
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
    # Seed the staleness map from the entry's resolved config (defaults +
    # options overrides + preset) so disabled/retuned signals are judged
    # against their configured interval, not the hardcoded default.
    coordinator.effective_intervals = resolve_effective_intervals(entry)

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
        # Last config we pushed to the car, so the options-update listener can
        # skip redundant re-pushes. Assume the car already holds the current
        # resolved config (bootstrap/auto-resync keep it reconciled).
        "pushed_intervals": dict(coordinator.effective_intervals),
    }

    async_register_services(hass)
    _async_check_command_scope(hass, entry)

    # Daily check that re-pushes the telemetry config when it's >7 days old.
    # No-op until the user has run `bootstrap` at least once.
    entry.async_on_unload(async_schedule_auto_resync(hass, entry))

    # Re-push the telemetry config whenever the user edits signals/intervals
    # via the options flow, so changes take effect without waiting a day.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """React to an options change (per-signal enable/interval edits).

    Refresh the coordinator's staleness map and re-push the telemetry config
    so edits reach the vehicle immediately rather than waiting for the daily
    auto-resync. Entities are not reloaded — the entity set is static, so a
    disabled signal's entity simply stops receiving and goes unavailable.
    """
    record = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not record:
        return
    _async_check_command_scope(hass, entry)
    coordinator: TeslaTelemetryCoordinator = record["coordinator"]
    new_intervals = resolve_effective_intervals(entry)
    coordinator.effective_intervals = new_intervals

    # Skip a redundant push when the effective config is unchanged — e.g. only
    # the cost rate was edited, or this fired from our own last_sync stamp
    # below (which breaks what would otherwise be an update loop).
    if record.get("pushed_intervals") == new_intervals:
        return

    # Don't push for an entry that hasn't been bootstrapped/authorized yet; its
    # first bootstrap will push the current config. Record the marker so an
    # unrelated later update doesn't push either.
    if not entry.data.get(CONF_LAST_SYNC_AT):
        record["pushed_intervals"] = new_intervals
        return

    api = record.get("api")
    if api is None:
        return

    from .services import _build_telemetry_config, _stamp_last_sync
    from .tls_ca import DEFAULT_CA_BUNDLE_PEM

    cfg = _build_telemetry_config(entry, DEFAULT_CA_BUNDLE_PEM.strip() + "\n")
    try:
        result = await api.set_fleet_telemetry_config(entry.data[CONF_VIN], cfg)
    except Exception as err:  # noqa: BLE001 — never raise from an update listener
        _LOGGER.warning(
            "tesla_telemetry: options-change re-push failed for vin=%s: %s",
            entry.data[CONF_VIN],
            err,
        )
        return
    record["pushed_intervals"] = new_intervals
    _stamp_last_sync(hass, entry)
    _LOGGER.info(
        "tesla_telemetry: options change re-pushed telemetry config for "
        "vin=%s — %s",
        entry.data[CONF_VIN],
        result,
    )


@callback
def _async_check_command_scope(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start a re-auth when "Allow vehicle commands" is on but the entry's
    token doesn't carry ``vehicle_cmds``; the re-auth asks Tesla for it.

    Runs at setup and on every options update. Starting a re-auth that is
    already in progress is a no-op in HA, so repeats are harmless; an
    entry with the option off is never touched, and until the re-auth is
    completed the rest of the integration keeps working read-only.
    """
    if not entry.options.get(
        CONF_ALLOW_VEHICLE_COMMANDS, DEFAULT_ALLOW_VEHICLE_COMMANDS
    ):
        return
    if token_allows_commands(entry):
        return
    _LOGGER.info(
        "tesla_telemetry: %s has vehicle commands allowed but its token lacks "
        "vehicle_cmds — starting a re-auth to request it",
        entry.title,
    )
    entry.async_start_reauth(hass)


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
