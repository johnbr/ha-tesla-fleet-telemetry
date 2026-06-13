"""Constants for the tesla_telemetry integration."""
from __future__ import annotations

DOMAIN = "tesla_telemetry"

# Telemetry signal names — must match enum names in
# teslamotors/fleet-telemetry protos/vehicle_data.proto.
# ----- Driving / nav -----
SIGNAL_LOCATION = "Location"
SIGNAL_GPS_STATE = "GpsState"
SIGNAL_VEHICLE_SPEED = "VehicleSpeed"
SIGNAL_ODOMETER = "Odometer"
SIGNAL_GEAR = "Gear"
SIGNAL_DESTINATION_NAME = "DestinationName"
SIGNAL_DESTINATION_LOCATION = "DestinationLocation"
SIGNAL_MILES_TO_ARRIVAL = "MilesToArrival"
SIGNAL_MINUTES_TO_ARRIVAL = "MinutesToArrival"
SIGNAL_ROUTE_TRAFFIC_DELAY = "RouteTrafficMinutesDelay"
# ----- Battery / range -----
SIGNAL_BATTERY_LEVEL = "BatteryLevel"
SIGNAL_SOC = "Soc"
SIGNAL_EST_BATTERY_RANGE = "EstBatteryRange"
SIGNAL_RATED_RANGE = "RatedRange"
SIGNAL_IDEAL_BATTERY_RANGE = "IdealBatteryRange"
# ----- Charging -----
SIGNAL_DETAILED_CHARGE_STATE = "DetailedChargeState"
SIGNAL_CHARGE_RATE_MILES_PER_HOUR = "ChargeRateMilePerHour"
SIGNAL_AC_CHARGING_POWER = "ACChargingPower"
SIGNAL_DC_CHARGING_POWER = "DCChargingPower"
SIGNAL_AC_CHARGING_ENERGY_IN = "ACChargingEnergyIn"
SIGNAL_DC_CHARGING_ENERGY_IN = "DCChargingEnergyIn"
SIGNAL_CHARGE_AMPS = "ChargeAmps"
SIGNAL_CHARGER_VOLTAGE = "ChargerVoltage"
SIGNAL_FAST_CHARGER_PRESENT = "FastChargerPresent"
SIGNAL_CHARGING_CABLE_TYPE = "ChargingCableType"
SIGNAL_CHARGE_LIMIT_SOC = "ChargeLimitSoc"
SIGNAL_TIME_TO_FULL_CHARGE = "TimeToFullCharge"
SIGNAL_CHARGE_PORT_DOOR_OPEN = "ChargePortDoorOpen"
# ----- Climate / cabin -----
SIGNAL_INSIDE_TEMP = "InsideTemp"
SIGNAL_OUTSIDE_TEMP = "OutsideTemp"
SIGNAL_HVAC_POWER = "HvacPower"
SIGNAL_HVAC_AC_ENABLED = "HvacACEnabled"
SIGNAL_HVAC_AUTO_MODE = "HvacAutoMode"
SIGNAL_HVAC_LEFT_TEMP_REQUEST = "HvacLeftTemperatureRequest"
SIGNAL_HVAC_RIGHT_TEMP_REQUEST = "HvacRightTemperatureRequest"
SIGNAL_DEFROST_MODE = "DefrostMode"
# ----- Body / security -----
SIGNAL_DOOR_STATE = "DoorState"
SIGNAL_WINDOW_FRONT_DRIVER = "FdWindow"
SIGNAL_WINDOW_FRONT_PASSENGER = "FpWindow"
SIGNAL_WINDOW_REAR_DRIVER = "RdWindow"
SIGNAL_WINDOW_REAR_PASSENGER = "RpWindow"
SIGNAL_LOCKED = "Locked"
SIGNAL_SENTRY_MODE = "SentryMode"
SIGNAL_DRIVER_SEAT_OCCUPIED = "DriverSeatOccupied"
# ----- TPMS -----
SIGNAL_TPMS_PRESSURE_FL = "TpmsPressureFl"
SIGNAL_TPMS_PRESSURE_FR = "TpmsPressureFr"
SIGNAL_TPMS_PRESSURE_RL = "TpmsPressureRl"
SIGNAL_TPMS_PRESSURE_RR = "TpmsPressureRr"
# ----- Software update -----
SIGNAL_SOFTWARE_UPDATE_VERSION = "SoftwareUpdateVersion"
SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT = "SoftwareUpdateDownloadPercentComplete"
SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT = "SoftwareUpdateInstallationPercentComplete"
# ----- Powertrain / performance -----
# Drive-inverter signals are suffixed F (front) / R (rear). RWD cars only
# report the rear drive unit, so the front entities stay unavailable on them.
SIGNAL_MOTOR_STATOR_TEMP_FRONT = "DiStatorTempF"
SIGNAL_MOTOR_STATOR_TEMP_REAR = "DiStatorTempR"
SIGNAL_MOTOR_TORQUE_FRONT = "DiTorqueActualF"
SIGNAL_MOTOR_TORQUE_REAR = "DiTorqueActualR"
SIGNAL_LATERAL_ACCELERATION = "LateralAcceleration"
SIGNAL_LONGITUDINAL_ACCELERATION = "LongitudinalAcceleration"
# HV battery pack
SIGNAL_PACK_VOLTAGE = "PackVoltage"
SIGNAL_PACK_CURRENT = "PackCurrent"
SIGNAL_MODULE_TEMP_MAX = "ModuleTempMax"
SIGNAL_MODULE_TEMP_MIN = "ModuleTempMin"
SIGNAL_BATTERY_HEATER_ON = "BatteryHeaterOn"
SIGNAL_BMS_STATE = "BMSState"

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
    SIGNAL_ROUTE_TRAFFIC_DELAY: 30,
    # charging — fluctuates while charging, idle otherwise
    SIGNAL_DETAILED_CHARGE_STATE: 5,
    SIGNAL_CHARGE_RATE_MILES_PER_HOUR: 10,
    SIGNAL_AC_CHARGING_POWER: 10,
    SIGNAL_DC_CHARGING_POWER: 10,
    SIGNAL_AC_CHARGING_ENERGY_IN: 30,
    SIGNAL_DC_CHARGING_ENERGY_IN: 30,
    SIGNAL_CHARGE_AMPS: 10,
    SIGNAL_CHARGER_VOLTAGE: 10,
    SIGNAL_FAST_CHARGER_PRESENT: 60,
    SIGNAL_CHARGING_CABLE_TYPE: 60,
    SIGNAL_TIME_TO_FULL_CHARGE: 30,
    SIGNAL_CHARGE_PORT_DOOR_OPEN: 5,
    # battery / range — drifts slowly
    SIGNAL_BATTERY_LEVEL: 30,
    SIGNAL_SOC: 30,
    SIGNAL_EST_BATTERY_RANGE: 30,
    SIGNAL_RATED_RANGE: 60,
    SIGNAL_IDEAL_BATTERY_RANGE: 60,
    # climate / cabin
    SIGNAL_INSIDE_TEMP: 30,
    SIGNAL_OUTSIDE_TEMP: 60,
    SIGNAL_HVAC_POWER: 5,
    SIGNAL_HVAC_AC_ENABLED: 30,
    SIGNAL_HVAC_AUTO_MODE: 30,
    SIGNAL_HVAC_LEFT_TEMP_REQUEST: 60,
    SIGNAL_HVAC_RIGHT_TEMP_REQUEST: 60,
    SIGNAL_DEFROST_MODE: 30,
    # body / security — discrete, push-on-change is what we want
    SIGNAL_DOOR_STATE: 1,
    SIGNAL_WINDOW_FRONT_DRIVER: 1,
    SIGNAL_WINDOW_FRONT_PASSENGER: 1,
    SIGNAL_WINDOW_REAR_DRIVER: 1,
    SIGNAL_WINDOW_REAR_PASSENGER: 1,
    SIGNAL_LOCKED: 1,
    SIGNAL_SENTRY_MODE: 5,
    SIGNAL_DRIVER_SEAT_OCCUPIED: 5,
    # user-set / rare changes — high ceiling, no real cost
    SIGNAL_CHARGE_LIMIT_SOC: 3600,
    SIGNAL_ODOMETER: 300,
    SIGNAL_TPMS_PRESSURE_FL: 300,
    SIGNAL_TPMS_PRESSURE_FR: 300,
    SIGNAL_TPMS_PRESSURE_RL: 300,
    SIGNAL_TPMS_PRESSURE_RR: 300,
    SIGNAL_SOFTWARE_UPDATE_VERSION: 3600,
    SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT: 60,
    SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT: 60,
    # powertrain / performance — high-churn while driving, so use low ceilings
    # to keep them live without flooding (push-on-change still applies)
    SIGNAL_MOTOR_STATOR_TEMP_FRONT: 10,
    SIGNAL_MOTOR_STATOR_TEMP_REAR: 10,
    SIGNAL_MOTOR_TORQUE_FRONT: 5,
    SIGNAL_MOTOR_TORQUE_REAR: 5,
    SIGNAL_LATERAL_ACCELERATION: 5,
    SIGNAL_LONGITUDINAL_ACCELERATION: 5,
    SIGNAL_PACK_VOLTAGE: 5,
    SIGNAL_PACK_CURRENT: 5,
    SIGNAL_MODULE_TEMP_MAX: 30,
    SIGNAL_MODULE_TEMP_MIN: 30,
    SIGNAL_BATTERY_HEATER_ON: 30,
    SIGNAL_BMS_STATE: 10,
}

# Named interval presets.  ``high_rate`` rewrites Location/VehicleSpeed/Gear/
# charging-power signals down to ~1s for live trace / driving log style use;
# all other signals keep their default ceilings.  Apply via the
# ``set_interval_preset`` service; revert with the ``default`` preset (or just
# wait for the next auto-resync, which always re-pushes defaults).
INTERVAL_PRESET_DEFAULT = "default"
INTERVAL_PRESET_HIGH_RATE = "high_rate"

INTERVAL_PRESET_OVERRIDES: dict[str, dict[str, int]] = {
    INTERVAL_PRESET_DEFAULT: {},
    INTERVAL_PRESET_HIGH_RATE: {
        SIGNAL_LOCATION: 1,
        SIGNAL_VEHICLE_SPEED: 1,
        SIGNAL_GEAR: 1,
        SIGNAL_AC_CHARGING_POWER: 2,
        SIGNAL_DC_CHARGING_POWER: 2,
        SIGNAL_CHARGER_VOLTAGE: 2,
        SIGNAL_CHARGE_AMPS: 2,
        SIGNAL_CHARGE_RATE_MILES_PER_HOUR: 2,
    },
}

# Currently-applied preset name. Persisted in entry.data so a HA restart
# preserves any user-selected high-rate session. The auto-resync timer always
# uses the preset stored here.
CONF_INTERVAL_PRESET = "interval_preset"

# After this many missed intervals an entity reports `unavailable`.
STALE_INTERVAL_MULTIPLIER = 4

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

# Partner OAuth (client_credentials grant) — needs an audience header
# pointing at the regional Fleet API. Used for partner_accounts/register
# and other partner-scoped calls.
TESLA_PARTNER_TOKEN_URL = (
    "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
)

# Region → Fleet API base URL. Default is North America. Add EU/CN if
# the integration ever needs to support those accounts.
REGION_NA = "na"
REGION_EU = "eu"
REGION_CN = "cn"
FLEET_API_BASE_URLS: dict[str, str] = {
    REGION_NA: "https://fleet-api.prd.na.vn.cloud.tesla.com",
    REGION_EU: "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    REGION_CN: "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}
DEFAULT_REGION = REGION_NA

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
