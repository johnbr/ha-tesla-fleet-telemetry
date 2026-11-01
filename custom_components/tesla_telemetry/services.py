"""HA services exposed by tesla_telemetry.

Three services, all keyed by an optional `entry_id` (the integration
auto-resolves when there's exactly one entry configured):

  * ``bootstrap``                — one-time onboarding. Calls
    ``register_partner_domain`` (skippable via flag for accounts already
    registered through another integration) and pushes an initial
    ``set_fleet_telemetry_config`` so the vehicle starts streaming.
  * ``resync_telemetry_config``  — re-pushes the same config. Tesla's
    ``exp`` field is ~30 days. The integration also auto-checks daily
    and re-pushes once the config the car reports expires within 7 days,
    so manual invocation is rarely needed.
  * ``dump_public_key``          — emits the EC P-256 public key derived
    from the configured private key, ready to host at the partner
    domain's ``.well-known`` path. Returns the PEM as a service response.
  * ``navigate``                 — sends a destination (free text, or
    coordinates) to the car's navigation. The integration's only write to
    the vehicle, and OFF unless the entry's "Allow vehicle commands" option
    is on; also needs the ``vehicle_cmds`` scope (turning the option on
    starts a re-auth for it when missing) and a vehicle that accepts
    unsigned Fleet API commands.
"""
from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import aiohttp
import probatio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .commands import DEFAULT_LOCALE, NAV_ORDERS, CommandRefused
from .const import (
    AUTO_RESYNC_CHECK_INTERVAL_SECONDS,
    AUTO_RESYNC_MAX_AGE_SECONDS,
    AUTO_RESYNC_RENEW_BEFORE_SECONDS,
    CONF_ALLOW_VEHICLE_COMMANDS,
    CONF_HOSTNAME,
    CONF_INTERVAL_PRESET,
    CONF_LAST_SYNC_AT,
    CONF_PARTNER_DOMAIN,
    CONF_PORT,
    CONF_PRIVATE_KEY_PEM,
    CONF_VIN,
    DEFAULT_ALLOW_VEHICLE_COMMANDS,
    DOMAIN,
    FIELD_MINIMUM_DELTAS,
    INTERVAL_PRESET_OVERRIDES,
    VEHICLE_COMMANDS_SCOPE,
    scopes_from_access_token,
)
from .models import (
    TeslaTelemetryConfigEntry,
    TeslaTelemetryRuntimeData,
    runtime_data_if_loaded,
)
from .signals import resolve_effective_intervals
from .tesla_api import TelemetryConfig, TelemetryFieldConfig, TeslaApi, TeslaApiError
from .tls_ca import DEFAULT_CA_BUNDLE_PEM

_LOGGER = logging.getLogger(__name__)

SERVICE_BOOTSTRAP = "bootstrap"
SERVICE_RESYNC = "resync_telemetry_config"
SERVICE_DUMP_PUBLIC_KEY = "dump_public_key"
SERVICE_GET_CONFIG = "get_telemetry_config"
SERVICE_SET_INTERVAL_PRESET = "set_interval_preset"
SERVICE_NAVIGATE = "navigate"

ATTR_ENTRY_ID = "entry_id"
ATTR_CA_PEM = "ca_pem"
ATTR_REGISTER_PARTNER = "register_partner_domain"
ATTR_PRESET = "preset"
ATTR_DESTINATION = "destination"
ATTR_ORDER = "order"
ATTR_LOCALE = "locale"

_BOOTSTRAP_SCHEMA = probatio.Schema(
    {
        probatio.Optional(ATTR_ENTRY_ID): str,
        probatio.Optional(ATTR_CA_PEM): str,
        probatio.Optional(ATTR_REGISTER_PARTNER, default=True): bool,
    }
)

_RESYNC_SCHEMA = probatio.Schema(
    {
        probatio.Optional(ATTR_ENTRY_ID): str,
        probatio.Optional(ATTR_CA_PEM): str,
    }
)

_DUMP_KEY_SCHEMA = probatio.Schema({probatio.Optional(ATTR_ENTRY_ID): str})

_GET_CONFIG_SCHEMA = probatio.Schema({probatio.Optional(ATTR_ENTRY_ID): str})

_SET_PRESET_SCHEMA = probatio.Schema(
    {
        probatio.Optional(ATTR_ENTRY_ID): str,
        probatio.Required(ATTR_PRESET): probatio.In(list(INTERVAL_PRESET_OVERRIDES)),
        probatio.Optional(ATTR_CA_PEM): str,
    }
)

# Exactly one of `destination` (free text) or latitude+longitude.
_NAVIGATE_SCHEMA = probatio.All(
    probatio.Schema(
        {
            probatio.Optional(ATTR_ENTRY_ID): str,
            probatio.Optional(ATTR_DESTINATION): cv.string,
            probatio.Inclusive(ATTR_LATITUDE, "coordinates"): cv.latitude,
            probatio.Inclusive(ATTR_LONGITUDE, "coordinates"): cv.longitude,
            probatio.Optional(ATTR_ORDER, default=0): probatio.All(
                probatio.Coerce(int), probatio.In(NAV_ORDERS)
            ),
            # Text destinations only: the locale the car reads the text in.
            # A BCP 47 tag such as en-US or de-DE.
            probatio.Optional(ATTR_LOCALE, default=DEFAULT_LOCALE): probatio.All(
                cv.string, probatio.Match(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
            ),
        }
    ),
    cv.has_at_least_one_key(ATTR_DESTINATION, ATTR_LATITUDE),
    cv.has_at_most_one_key(ATTR_DESTINATION, ATTR_LATITUDE),
)


def _resolve_entry(
    hass: HomeAssistant, entry_id: str | None
) -> TeslaTelemetryConfigEntry:
    """Return the entry the service call is targeting."""
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
                translation_placeholders={"entry_id": entry_id},
            )
        return entry
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entries"
        )
    if len(entries) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="multiple_entries"
        )
    return entries[0]


def _entry_runtime(entry: TeslaTelemetryConfigEntry) -> TeslaTelemetryRuntimeData:
    """The entry's runtime data, or a translated error when it is not loaded."""
    runtime = runtime_data_if_loaded(entry)
    if runtime is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"entry_id": entry.entry_id},
        )
    return runtime


def _entry_api(entry: TeslaTelemetryConfigEntry) -> TeslaApi:
    return _entry_runtime(entry).api


_Handler = Callable[[ServiceCall], Awaitable[ServiceResponse]]


def _admin_only(handler: _Handler) -> _Handler:
    """Refuse the call unless it comes from an admin (or from HA itself).

    For the services that change what the car streams, which Tesla bills per
    signal, or what CA it trusts. HA's ``async_register_admin_service`` does
    the same check; it is done here so it holds on every HA version the
    integration supports.
    """

    @functools.wraps(handler)
    async def _wrapper(call: ServiceCall) -> ServiceResponse:
        if call.context.user_id:
            user = await call.hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise Unauthorized(context=call.context)
            if not user.is_admin:
                raise Unauthorized(
                    context=call.context, permission="admin"
                )
        return await handler(call)

    return _wrapper


async def _tesla_call[T](awaitable: Awaitable[T]) -> T:
    """Await a Fleet API call, turning its failures into a readable
    ``HomeAssistantError`` instead of a raw traceback in the UI."""
    try:
        return await awaitable
    except TeslaApiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"status": str(err.status), "message": str(err)},
        ) from err
    except (TimeoutError, aiohttp.ClientError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err


def _resolve_intervals(entry: ConfigEntry) -> dict[str, int]:
    """The ``{signal: interval}`` map to push to Tesla for this entry.

    Delegates to :func:`signals.resolve_effective_intervals`, which layers the
    built-in defaults, the user's per-signal options-flow overrides, and the
    active interval preset. Kept as a thin wrapper so the service handlers and
    ``_build_telemetry_config`` have a stable local name.
    """
    return resolve_effective_intervals(entry)


def _with_data(entry: ConfigEntry, data: dict[str, Any]) -> SimpleNamespace:
    """``entry`` as it will read once ``data`` is saved, for the resolver
    (which only reads ``.data`` and ``.options``)."""
    return SimpleNamespace(data=data, options=entry.options)


def _build_telemetry_config(entry: ConfigEntry, ca_pem: str) -> TelemetryConfig:
    return TelemetryConfig(
        hostname=entry.data[CONF_HOSTNAME],
        port=int(entry.data[CONF_PORT]),
        ca=ca_pem,
        fields={
            name: TelemetryFieldConfig(
                interval_seconds=interval,
                minimum_delta=FIELD_MINIMUM_DELTAS.get(name),
            )
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
    api = _entry_api(entry)

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
    response["telemetry_config"] = await _tesla_call(
        api.set_fleet_telemetry_config(entry.data[CONF_VIN], cfg)
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
    api = _entry_api(entry)
    ca_pem = (call.data.get(ATTR_CA_PEM) or DEFAULT_CA_BUNDLE_PEM).strip() + "\n"
    cfg = _build_telemetry_config(entry, ca_pem)
    response = await _tesla_call(
        api.set_fleet_telemetry_config(entry.data[CONF_VIN], cfg)
    )
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
    api = _entry_api(entry)
    vin = entry.data[CONF_VIN]
    response = await _tesla_call(api.get_fleet_telemetry_config(vin))
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
    api = _entry_api(entry)
    preset: str = call.data[ATTR_PRESET]

    runtime = _entry_runtime(entry)
    previous_pushed = runtime.pushed_intervals
    new_data = {**entry.data, CONF_INTERVAL_PRESET: preset}
    # Saving the preset fires the entry's update listener, which re-pushes
    # whenever the resolved intervals differ from the last push. This handler
    # pushes itself (it may carry a ca_pem override, and returns Tesla's
    # answer), so mark the new intervals as pushed first or Tesla would get
    # the same config twice; restored below if the push fails.
    runtime.pushed_intervals = resolve_effective_intervals(
        _with_data(entry, new_data)
    )
    hass.config_entries.async_update_entry(entry, data=new_data)
    # _build_telemetry_config now reads the just-saved preset.
    entry = hass.config_entries.async_get_entry(entry.entry_id)  # type: ignore[assignment]
    assert entry is not None

    ca_pem = (call.data.get(ATTR_CA_PEM) or DEFAULT_CA_BUNDLE_PEM).strip() + "\n"
    cfg = _build_telemetry_config(entry, ca_pem)
    try:
        response = await _tesla_call(
            api.set_fleet_telemetry_config(entry.data[CONF_VIN], cfg)
        )
    except HomeAssistantError:
        runtime.pushed_intervals = previous_pushed
        raise
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


def token_allows_commands(entry: ConfigEntry) -> bool:
    """Whether the entry's current access token carries ``vehicle_cmds``.

    Unreadable scopes count as allowed — Tesla is then the one to say no
    (403), rather than this guess blocking a working token.
    """
    token = (entry.data.get("token") or {}).get("access_token") or ""
    scopes = scopes_from_access_token(token)
    return scopes is None or VEHICLE_COMMANDS_SCOPE in scopes


def _command_error(err: TeslaApiError) -> HomeAssistantError:
    """Turn a failed command call into something actionable."""
    body = str(err.body or err)[:300]
    if err.status == 408:
        return HomeAssistantError(
            translation_domain=DOMAIN, translation_key="vehicle_asleep"
        )
    if "command protocol" in body.lower():
        return HomeAssistantError(
            translation_domain=DOMAIN, translation_key="signed_commands_required"
        )
    if err.status == 403:
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_forbidden",
            translation_placeholders={"message": body},
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="api_error",
        translation_placeholders={"status": str(err.status), "message": body},
    )


async def _navigate_handler(call: ServiceCall) -> ServiceResponse:
    """Send a destination to the car's navigation.

    The destination is deliberately not logged above debug level: it is
    often someone else's address.
    """
    hass = call.hass
    entry = _resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
    if not entry.options.get(
        CONF_ALLOW_VEHICLE_COMMANDS, DEFAULT_ALLOW_VEHICLE_COMMANDS
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="commands_disabled",
            translation_placeholders={"name": entry.title},
        )
    if not token_allows_commands(entry):
        entry.async_start_reauth(hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="missing_command_scope",
            translation_placeholders={
                "name": entry.title,
                "scope": VEHICLE_COMMANDS_SCOPE,
            },
        )
    api = _entry_api(entry)
    vin = entry.data[CONF_VIN]
    try:
        if ATTR_DESTINATION in call.data:
            method = "navigation_request"
            result = await api.navigation_request(
                vin, call.data[ATTR_DESTINATION], locale=call.data[ATTR_LOCALE]
            )
        else:
            method = "navigation_gps_request"
            result = await api.navigation_gps_request(
                vin,
                call.data[ATTR_LATITUDE],
                call.data[ATTR_LONGITUDE],
                call.data[ATTR_ORDER],
            )
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_destination",
            translation_placeholders={"error": str(err)},
        ) from err
    except CommandRefused as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_refused",
            translation_placeholders={"reason": str(err)},
        ) from err
    except TeslaApiError as err:
        raise _command_error(err) from err
    _LOGGER.info("tesla_telemetry: %s delivered to vin=%s", method, vin)
    _LOGGER.debug("tesla_telemetry: %s payload %s", method, dict(call.data))
    return {"vin": vin, "method": method, "result": result}


async def _dump_public_key_handler(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry = _resolve_entry(hass, call.data.get(ATTR_ENTRY_ID))
    pem = entry.data.get(CONF_PRIVATE_KEY_PEM, "")
    if not pem.strip():
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="no_private_key"
        )

    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except Exception as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="bad_private_key",
            translation_placeholders={"error": str(err)},
        ) from err

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
    """Register the integration's service actions.

    Called once from ``async_setup``, which Home Assistant runs once per
    process for an integration that has config entries, so the registrations
    are not guarded against repeats: a duplicate ``async_register`` would be a
    bug here, not something to swallow.
    """
    hass.services.async_register(
        DOMAIN,
        SERVICE_BOOTSTRAP,
        _admin_only(_bootstrap_handler),
        schema=_BOOTSTRAP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESYNC,
        _admin_only(_resync_handler),
        schema=_RESYNC_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DUMP_PUBLIC_KEY,
        _admin_only(_dump_public_key_handler),
        schema=_DUMP_KEY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CONFIG,
        _admin_only(_get_config_handler),
        schema=_GET_CONFIG_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_INTERVAL_PRESET,
        _admin_only(_set_interval_preset_handler),
        schema=_SET_PRESET_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_NAVIGATE,
        _navigate_handler,
        schema=_NAVIGATE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


# ---------------------------------------------------------------------
# Auto-resync
# ---------------------------------------------------------------------
def _exp_seconds(value: Any) -> int | None:
    """A config's ``exp`` as a unix timestamp in seconds, or ``None`` when
    Tesla sent no usable value. Some responses carry milliseconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Anything this large is milliseconds; in seconds it would be year 5138.
    return int(value / 1000) if value > 100_000_000_000 else int(value)


async def _auto_resync_if_due(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-push fleet_telemetry_config when the config the car holds is due to
    expire.

    ``config.exp`` is what ends the stream, so it decides when to renew — not
    our own ``last_sync_at``, which a restored backup or a push from another
    instance leaves saying "recent" while the car's copy runs out. The stamp is
    the fallback when there is no expiry to go by: Tesla sent none, the car
    reports no config at all, or the read of the car's config failed.

    An entry with no ``last_sync_at`` never pushed a config itself — which is
    also what a re-added entry looks like, and the car then still holds the one
    the previous install pushed. That one is re-pushed rather than left to
    expire, and pushing stamps the entry, which the options listener and the
    fallback above rely on.
    """
    last = entry.data.get(CONF_LAST_SYNC_AT)
    try:
        api = _entry_api(entry)
    except HomeAssistantError as err:
        # Not debug: with no API client the config is never renewed, and the
        # only symptom is the stream stopping a month later.
        _LOGGER.warning(
            "tesla_telemetry: auto-resync skipped for vin=%s — %s",
            entry.data[CONF_VIN],
            err,
        )
        return

    remaining = None
    try:
        reported = await api.get_fleet_telemetry_config(entry.data[CONF_VIN])
    except Exception as err:  # noqa: BLE001 — never surface from a timer tick
        # The push may still work, so a stale stamp is enough to renew on;
        # without one there is nothing to adopt either, since adoption needs
        # the hostname this read would have told us.
        _LOGGER.warning(
            "tesla_telemetry: cannot read the car's telemetry config for "
            "vin=%s — %s",
            entry.data[CONF_VIN],
            err,
        )
        if not last:
            return
    else:
        config = (reported or {}).get("config") or {}
        hostname = config.get("hostname")
        if hostname and hostname != entry.data.get(CONF_HOSTNAME):
            # Another endpoint's config: not ours to renew, however stale our
            # stamp. Asked again on the next check rather than remembered —
            # this entry bootstrapping, or another instance pushing, both
            # change the answer.
            _LOGGER.debug(
                "tesla_telemetry: vin=%s holds a config for %s, not %s — "
                "nothing to renew",
                entry.data[CONF_VIN],
                hostname,
                entry.data.get(CONF_HOSTNAME),
            )
            return
        if not hostname:
            # No config at all. Without a stamp there is nothing to adopt.
            # With one, this entry did push a config, and it may have lapsed
            # and been dropped rather than reported with a past `exp` —
            # Tesla's behaviour there isn't documented — so the stamp decides,
            # as when the read fails.
            _LOGGER.debug(
                "tesla_telemetry: vin=%s reports no telemetry config%s",
                entry.data[CONF_VIN],
                " — falling back to the last-sync stamp" if last else "",
            )
            if not last:
                return
        else:
            exp = _exp_seconds(config.get("exp"))
            if exp is not None:
                remaining = int(exp - time.time())

    if last:
        if remaining is not None:
            if remaining > AUTO_RESYNC_RENEW_BEFORE_SECONDS:
                _LOGGER.debug(
                    "tesla_telemetry: vin=%s config expires in %ss — not due yet",
                    entry.data[CONF_VIN],
                    remaining,
                )
                return
        elif time.time() - last < AUTO_RESYNC_MAX_AGE_SECONDS:
            # No expiry to go by, so the stamp is all there is.
            return
    else:
        _LOGGER.warning(
            "tesla_telemetry: vin=%s holds a telemetry config this entry did "
            "not push — re-pushing it so the renewal covers it",
            entry.data[CONF_VIN],
        )

    if remaining is None:
        why = f" after {int(time.time() - last)}s" if last else ""
    elif remaining < 0:
        why = f", config expired {-remaining}s ago"
    else:
        why = f", config expires in {remaining}s"
    cfg = _build_telemetry_config(entry, DEFAULT_CA_BUNDLE_PEM.strip() + "\n")
    try:
        result = await api.set_fleet_telemetry_config(entry.data[CONF_VIN], cfg)
    except Exception as err:  # noqa: BLE001 — never surface from a timer tick
        _LOGGER.warning(
            "tesla_telemetry: auto-resync failed for vin=%s%s: %s",
            entry.data[CONF_VIN],
            why,
            err,
        )
        return
    _stamp_last_sync(hass, entry)
    _LOGGER.info(
        "tesla_telemetry: auto-resync ok for vin=%s%s: %s",
        entry.data[CONF_VIN],
        why,
        result,
    )


@callback
def async_schedule_auto_resync(
    hass: HomeAssistant, entry: ConfigEntry
) -> Any:
    """Register the daily resync check for an entry. Returns the cancel
    callable HA's entry-unload machinery should invoke when the entry
    goes away.

    The interval restarts its countdown on every HA start, so on its own it
    only fires once the instance has been up for 24 h straight — an instance
    that restarts more often than that would never run the check at all. The
    extra check turns a restart from "postpone" into "check now". It runs when
    HA has finished starting (or at once, when the entry is set up later), so
    the Tesla round-trip never lands in the middle of a cold boot.
    """

    async def _tick(_now: Any = None) -> None:
        await _auto_resync_if_due(hass, entry)

    unsub_interval = async_track_time_interval(
        hass, _tick, timedelta(seconds=AUTO_RESYNC_CHECK_INTERVAL_SECONDS)
    )
    unsub_startup = async_at_started(hass, _tick)
    return lambda: (unsub_interval(), unsub_startup())
