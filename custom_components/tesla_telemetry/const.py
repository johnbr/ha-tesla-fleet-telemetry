"""Constants for the tesla_telemetry integration."""
from __future__ import annotations

import base64
import binascii
import json

DOMAIN = "tesla_telemetry"

# Telemetry signal names — must match enum names in
# teslamotors/fleet-telemetry protos/vehicle_data.proto.
# ----- Driving / nav -----
SIGNAL_LOCATION = "Location"
SIGNAL_GPS_STATE = "GpsState"
SIGNAL_VEHICLE_SPEED = "VehicleSpeed"
SIGNAL_GEAR = "Gear"
SIGNAL_DESTINATION_NAME = "DestinationName"
SIGNAL_DESTINATION_LOCATION = "DestinationLocation"
SIGNAL_MILES_TO_ARRIVAL = "MilesToArrival"
SIGNAL_MINUTES_TO_ARRIVAL = "MinutesToArrival"
# ----- Battery / range -----
SIGNAL_BATTERY_LEVEL = "BatteryLevel"
SIGNAL_SOC = "Soc"
SIGNAL_EST_BATTERY_RANGE = "EstBatteryRange"
SIGNAL_RATED_RANGE = "RatedRange"
# ----- Charging -----
SIGNAL_DETAILED_CHARGE_STATE = "DetailedChargeState"
SIGNAL_AC_CHARGING_POWER = "ACChargingPower"
SIGNAL_DC_CHARGING_POWER = "DCChargingPower"
SIGNAL_AC_CHARGING_ENERGY_IN = "ACChargingEnergyIn"
SIGNAL_DC_CHARGING_ENERGY_IN = "DCChargingEnergyIn"
SIGNAL_FAST_CHARGER_PRESENT = "FastChargerPresent"
SIGNAL_CHARGING_CABLE_TYPE = "ChargingCableType"
SIGNAL_CHARGE_LIMIT_SOC = "ChargeLimitSoc"
SIGNAL_TIME_TO_FULL_CHARGE = "TimeToFullCharge"
SIGNAL_CHARGE_PORT_DOOR_OPEN = "ChargePortDoorOpen"
# ----- Climate / cabin -----
SIGNAL_INSIDE_TEMP = "InsideTemp"
SIGNAL_OUTSIDE_TEMP = "OutsideTemp"
SIGNAL_HVAC_AC_ENABLED = "HvacACEnabled"
SIGNAL_HVAC_AUTO_MODE = "HvacAutoMode"
# ----- Body / security -----
SIGNAL_DOOR_STATE = "DoorState"
SIGNAL_WINDOW_FRONT_DRIVER = "FdWindow"
SIGNAL_WINDOW_FRONT_PASSENGER = "FpWindow"
SIGNAL_WINDOW_REAR_DRIVER = "RdWindow"
SIGNAL_WINDOW_REAR_PASSENGER = "RpWindow"
SIGNAL_LOCKED = "Locked"
SIGNAL_DRIVER_SEAT_OCCUPIED = "DriverSeatOccupied"
# ----- Software update -----
SIGNAL_SOFTWARE_UPDATE_VERSION = "SoftwareUpdateVersion"
SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT = "SoftwareUpdateDownloadPercentComplete"
SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT = "SoftwareUpdateInstallationPercentComplete"
# ----- Powertrain / performance -----
# Drive-inverter signals are suffixed F (front) / R (rear). RWD cars only
# report the rear drive unit, so the front entities stay unavailable on them.
SIGNAL_MOTOR_STATOR_TEMP_FRONT = "DiStatorTempF"
SIGNAL_MOTOR_STATOR_TEMP_REAR = "DiStatorTempR"
# HV battery pack
SIGNAL_MODULE_TEMP_MAX = "ModuleTempMax"
SIGNAL_MODULE_TEMP_MIN = "ModuleTempMin"

# Per-field intervals sent in fleet_telemetry_config (seconds). Tesla emits a
# signal on change AND no more than once per the configured interval; it does
# NOT poll, so a high value is just a ceiling for high-churn signals — values
# that rarely change still arrive immediately when they do.
DEFAULT_INTERVALS_SECONDS: dict[str, int] = {
    # high-churn / driving — needs throttling
    SIGNAL_LOCATION: 5,
    SIGNAL_VEHICLE_SPEED: 5,
    SIGNAL_GEAR: 5,
    # navigation — only meaningful while a route is active
    SIGNAL_DESTINATION_NAME: 30,
    SIGNAL_DESTINATION_LOCATION: 30,
    SIGNAL_MILES_TO_ARRIVAL: 15,
    SIGNAL_MINUTES_TO_ARRIVAL: 15,
    # charging — fluctuates while charging, idle otherwise.
    # DetailedChargeState is the exception: a discrete enum (Disconnected /
    # NoPower / Starting / Charging / Complete) that moves a handful of times
    # per session, so it belongs with the push-on-change group below at 1 s
    # rather than the throttled power/energy signals. Measured against the
    # by_signal counter, its nearest analogues cost 3-4 signals per session
    # (FastChargerPresent, ChargingCableType) — ~0.1% of stream volume.
    SIGNAL_DETAILED_CHARGE_STATE: 1,
    SIGNAL_AC_CHARGING_POWER: 10,
    SIGNAL_DC_CHARGING_POWER: 10,
    SIGNAL_AC_CHARGING_ENERGY_IN: 30,
    SIGNAL_DC_CHARGING_ENERGY_IN: 30,
    SIGNAL_FAST_CHARGER_PRESENT: 60,
    SIGNAL_CHARGING_CABLE_TYPE: 60,
    SIGNAL_TIME_TO_FULL_CHARGE: 30,
    SIGNAL_CHARGE_PORT_DOOR_OPEN: 5,
    # battery / range — drifts slowly, EXCEPT the displayed pack percentage
    # under fast DC charge, where it can step more than once per 30 s and the
    # old ceiling was the thing making the dashboard SOC look stale. Push-on-
    # change means the lower ceiling costs nothing while parked or driving: it
    # only binds when the value is genuinely moving that fast. Kept in step
    # with DCChargingPower above (also 10 s) so the power reading and the SOC
    # it is moving arrive on the same cadence.
    SIGNAL_BATTERY_LEVEL: 10,
    SIGNAL_SOC: 30,
    SIGNAL_EST_BATTERY_RANGE: 30,
    SIGNAL_RATED_RANGE: 60,
    # climate / cabin
    SIGNAL_INSIDE_TEMP: 30,
    SIGNAL_OUTSIDE_TEMP: 60,
    SIGNAL_HVAC_AC_ENABLED: 30,
    SIGNAL_HVAC_AUTO_MODE: 30,
    # body / security — discrete, push-on-change is what we want
    SIGNAL_DOOR_STATE: 1,
    SIGNAL_WINDOW_FRONT_DRIVER: 1,
    SIGNAL_WINDOW_FRONT_PASSENGER: 1,
    SIGNAL_WINDOW_REAR_DRIVER: 1,
    SIGNAL_WINDOW_REAR_PASSENGER: 1,
    SIGNAL_LOCKED: 1,
    SIGNAL_DRIVER_SEAT_OCCUPIED: 5,
    # user-set / discrete — push-on-change delivers instantly, so the ceiling
    # only bounds back-to-back changes; keep it low, there's no flood risk
    SIGNAL_CHARGE_LIMIT_SOC: 10,
    SIGNAL_SOFTWARE_UPDATE_VERSION: 3600,
    SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT: 60,
    SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT: 60,
    # powertrain / performance — high-churn while driving, so use low ceilings
    # to keep them live without flooding (push-on-change still applies)
    SIGNAL_MOTOR_STATOR_TEMP_FRONT: 10,
    SIGNAL_MOTOR_STATOR_TEMP_REAR: 10,
    SIGNAL_MODULE_TEMP_MAX: 30,
    SIGNAL_MODULE_TEMP_MIN: 30,
}

# Curated grouping of the default signals into collapsible sections for the
# options flow. Every key of DEFAULT_INTERVALS_SECONDS must appear in exactly
# one category (guarded by tests/test_signals.py). Signals the user adds from
# the full Tesla catalog that aren't listed here surface under a synthetic
# "Additional signals" section instead.
SIGNAL_CATEGORIES: dict[str, list[str]] = {
    "driving": [
        SIGNAL_LOCATION,
        SIGNAL_VEHICLE_SPEED,
        SIGNAL_GEAR,
        SIGNAL_DESTINATION_NAME,
        SIGNAL_DESTINATION_LOCATION,
        SIGNAL_MILES_TO_ARRIVAL,
        SIGNAL_MINUTES_TO_ARRIVAL,
    ],
    "battery": [
        SIGNAL_BATTERY_LEVEL,
        SIGNAL_SOC,
        SIGNAL_EST_BATTERY_RANGE,
        SIGNAL_RATED_RANGE,
    ],
    "charging": [
        SIGNAL_DETAILED_CHARGE_STATE,
        SIGNAL_AC_CHARGING_POWER,
        SIGNAL_DC_CHARGING_POWER,
        SIGNAL_AC_CHARGING_ENERGY_IN,
        SIGNAL_DC_CHARGING_ENERGY_IN,
        SIGNAL_FAST_CHARGER_PRESENT,
        SIGNAL_CHARGING_CABLE_TYPE,
        SIGNAL_CHARGE_LIMIT_SOC,
        SIGNAL_TIME_TO_FULL_CHARGE,
        SIGNAL_CHARGE_PORT_DOOR_OPEN,
    ],
    "climate": [
        SIGNAL_INSIDE_TEMP,
        SIGNAL_OUTSIDE_TEMP,
        SIGNAL_HVAC_AC_ENABLED,
        SIGNAL_HVAC_AUTO_MODE,
    ],
    "body": [
        SIGNAL_DOOR_STATE,
        SIGNAL_WINDOW_FRONT_DRIVER,
        SIGNAL_WINDOW_FRONT_PASSENGER,
        SIGNAL_WINDOW_REAR_DRIVER,
        SIGNAL_WINDOW_REAR_PASSENGER,
        SIGNAL_LOCKED,
        SIGNAL_DRIVER_SEAT_OCCUPIED,
    ],
    "software": [
        SIGNAL_SOFTWARE_UPDATE_VERSION,
        SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT,
        SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT,
    ],
    "powertrain": [
        SIGNAL_MOTOR_STATOR_TEMP_FRONT,
        SIGNAL_MOTOR_STATOR_TEMP_REAR,
        SIGNAL_MODULE_TEMP_MAX,
        SIGNAL_MODULE_TEMP_MIN,
    ],
}

# Per-signal overrides configured through the options flow, stored on
# ``entry.options`` as ``{signal_name: interval_seconds}``. A stored 0 disables
# a signal that's on by default; a positive value overrides its interval. Only
# deviations from DEFAULT_INTERVALS_SECONDS are stored, so an entry with no
# overrides pushes exactly the default config above.
CONF_SIGNAL_OVERRIDES = "signal_overrides"

# Interval assigned to a catalog signal the moment it's added from the full
# Tesla catalog (an added signal has no default of its own).
DEFAULT_NEW_SIGNAL_INTERVAL = 60

# Bounds for a per-signal minimum-refresh interval (seconds). 0 is accepted in
# the form as the "disable this signal" sentinel; enabled signals are >= MIN.
SIGNAL_INTERVAL_MIN = 1
SIGNAL_INTERVAL_MAX = 86_400

# Named interval presets.  ``high_rate`` rewrites Location/VehicleSpeed down to
# 1s for live trace / driving log style use; all other signals (Gear, charging,
# etc.) keep their default ceilings.  Apply via the
# ``set_interval_preset`` service; revert with the ``default`` preset (or just
# wait for the next auto-resync, which always re-pushes defaults).
INTERVAL_PRESET_DEFAULT = "default"
INTERVAL_PRESET_HIGH_RATE = "high_rate"

INTERVAL_PRESET_OVERRIDES: dict[str, dict[str, int]] = {
    INTERVAL_PRESET_DEFAULT: {},
    INTERVAL_PRESET_HIGH_RATE: {
        SIGNAL_LOCATION: 1,
        SIGNAL_VEHICLE_SPEED: 1,
    },
}

# Currently-applied preset name. Persisted in entry.data so a HA restart
# preserves any user-selected high-rate session. The auto-resync timer always
# uses the preset stored here.
CONF_INTERVAL_PRESET = "interval_preset"

# After this many missed intervals an entity reports `unavailable`.
STALE_INTERVAL_MULTIPLIER = 4

# --- Signal accounting -------------------------------------------------
# Every datum the vehicle streams is one Tesla-billed "signal". The
# coordinator counts them at the single choke point (``async_publish``);
# the `Signals received` sensor exposes the running total plus a per-signal
# breakdown, and `Estimated signal cost` multiplies it by the rate below.
#
# The counter sensors flush to HA state on a timer rather than per signal —
# writing a state row on every signal would flood the recorder with exactly
# the volume we're trying to measure. One write per interval, whatever the
# signal rate.
SIGNAL_COUNT_FLUSH_INTERVAL_SECONDS = 60

# Estimated streaming cost. Tesla bills ~$1 per 150,000 streaming signals
# (per data point); the default mirrors that published US rate, expressed
# per million signals so it stays a readable number to override. Configured
# per entry via the options flow (``entry.options``) so the estimate can
# track Tesla's pricing or a non-US region.
CONF_COST_PER_MILLION_SIGNALS = "cost_per_million_signals"
DEFAULT_COST_PER_MILLION_SIGNALS = 1_000_000 / 150_000  # ≈ 6.667

# Storage (homeassistant.helpers.storage.Store)
STORAGE_VERSION = 1
STORAGE_KEY_PRIVATE_KEY = "tesla_telemetry_private_key"
STORAGE_KEY_OAUTH = "tesla_telemetry_oauth"

# Config entry data keys
CONF_VIN = "vin"
CONF_VEHICLE_NAME = "vehicle_name"
CONF_PARTNER_DOMAIN = "partner_domain"
CONF_PROXY_SECRET = "proxy_secret"
CONF_REGION = "region"
CONF_HOSTNAME = "hostname"
CONF_PORT = "port"
CONF_PRIVATE_KEY_PEM = "private_key_pem"

# WebSocket endpoint registered on HA's HTTP server. nginx proxies the
# vehicle's mTLS WSS connection here after validating the client cert.
WS_PATH = "/api/tesla_telemetry/ws"

# Headers nginx injects after mTLS termination
HEADER_PROXY_SECRET = "X-Tesla-Proxy-Secret"
HEADER_VERIFIED_VIN = "X-Tesla-Verified-Vin"

# --- Tesla Fleet API endpoints ---------------------------------------
# User OAuth refresh (refresh_token grant). Region-agnostic.
TESLA_USER_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"

# OAuth2 endpoints exposed via application_credentials. The token endpoint
# is the same URL we used for refresh_token grants — Tesla's auth server
# handles both `authorization_code` (fresh login) and `refresh_token`
# grants on the one URL.
OAUTH_AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
OAUTH_TOKEN_URL = TESLA_USER_TOKEN_URL
OAUTH_SCOPES = ["openid", "offline_access", "vehicle_device_data"]

# Region → Fleet API base URL. Default is North America.
REGION_NA = "na"
REGION_EU = "eu"
REGION_CN = "cn"
FLEET_API_BASE_URLS: dict[str, str] = {
    REGION_NA: "https://fleet-api.prd.na.vn.cloud.tesla.com",
    REGION_EU: "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    REGION_CN: "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}
DEFAULT_REGION = REGION_NA

# Partner OAuth (client_credentials grant). The endpoint is global — the
# same host for every region (mirrors the tesla_fleet_api library HA core
# uses); regional scoping comes from the `audience` in the request body,
# which TeslaApi sets to the entry's Fleet API base URL. Used for
# partner_accounts/register and other partner-scoped calls.
TESLA_PARTNER_TOKEN_URL = (
    "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
)

# Regions offered in the config flow. China is deliberately excluded (as
# in HA core's tesla_fleet): it uses separate auth infrastructure and the
# paths below are untested for it. The REGION_CN entry stays in
# FLEET_API_BASE_URLS so the API client keeps validating it.
SELECTABLE_REGIONS: tuple[str, ...] = (REGION_NA, REGION_EU)

# Tesla access-token JWTs carry the account's operational region in the
# `ou_code` claim (e.g. "NA", "EU"). The config flow compares it against
# the user's region choice and logs a mismatch. Same claim HA core's
# `tesla_fleet` integration uses for region detection.
TOKEN_OU_CODES: dict[str, str] = {
    "na": REGION_NA,
    "eu": REGION_EU,
    "cn": REGION_CN,
}


def region_from_access_token(token: str) -> str | None:
    """Best-effort region detection from an access-token JWT payload.

    Decodes the unverified JWT claims and maps Tesla's ``ou_code`` claim
    to a region constant. Returns ``None`` when the claim is absent or
    unknown — callers should fall back to ``DEFAULT_REGION``. Deliberately
    no signature check: this is only ever a UX default, never a trust
    decision.
    """
    try:
        payload_b64 = token.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
        ou_code = str(payload.get("ou_code", "")).lower()
    except (IndexError, ValueError, TypeError, binascii.Error):
        return None
    return TOKEN_OU_CODES.get(ou_code)

# Buffer applied to OAuth `expires_in` so we refresh before Tesla considers
# the token stale. Seconds.
TOKEN_REFRESH_LEEWAY = 60

# Auto-resync of fleet_telemetry_config — Tesla's `exp` field is ~30 days,
# so we check daily and re-push if the last successful sync is more than
# 7 days old. Survives HA restarts because `last_sync_at` lives in
# entry.data.
CONF_LAST_SYNC_AT = "last_sync_at"
AUTO_RESYNC_CHECK_INTERVAL_SECONDS = 24 * 3600
AUTO_RESYNC_MAX_AGE_SECONDS = 7 * 24 * 3600
