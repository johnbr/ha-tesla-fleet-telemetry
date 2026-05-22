"""Sensor entities for Tesla Fleet Telemetry.

Each entity subscribes to one signal name on the per-VIN coordinator and
renders the latest value.  Decoding of the raw ``Value`` oneof lives in
``values.py``; this file is only concerned with HA entity wiring.

Entities use ``has_entity_name`` — the vehicle name lives on the HA device
(see ``TeslaTelemetryCoordinator.device_info``) and each entity carries
only its functional name (e.g. "Speed").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SIGNAL_AC_CHARGING_ENERGY_IN,
    SIGNAL_AC_CHARGING_POWER,
    SIGNAL_BATTERY_LEVEL,
    SIGNAL_CHARGE_AMPS,
    SIGNAL_CHARGE_LIMIT_SOC,
    SIGNAL_CHARGE_RATE_MILES_PER_HOUR,
    SIGNAL_CHARGER_VOLTAGE,
    SIGNAL_CHARGING_CABLE_TYPE,
    SIGNAL_DC_CHARGING_ENERGY_IN,
    SIGNAL_DC_CHARGING_POWER,
    SIGNAL_DETAILED_CHARGE_STATE,
    SIGNAL_EST_BATTERY_RANGE,
    SIGNAL_FAST_CHARGER_PRESENT,
    SIGNAL_GEAR,
    SIGNAL_HVAC_LEFT_TEMP_REQUEST,
    SIGNAL_HVAC_RIGHT_TEMP_REQUEST,
    SIGNAL_INSIDE_TEMP,
    SIGNAL_MILES_TO_ARRIVAL,
    SIGNAL_MINUTES_TO_ARRIVAL,
    SIGNAL_ODOMETER,
    SIGNAL_OUTSIDE_TEMP,
    SIGNAL_RATED_RANGE,
    SIGNAL_ROUTE_TRAFFIC_DELAY,
    SIGNAL_SOC,
    SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT,
    SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT,
    SIGNAL_SOFTWARE_UPDATE_VERSION,
    SIGNAL_TIME_TO_FULL_CHARGE,
    SIGNAL_TPMS_PRESSURE_FL,
    SIGNAL_TPMS_PRESSURE_FR,
    SIGNAL_TPMS_PRESSURE_RL,
    SIGNAL_TPMS_PRESSURE_RR,
    SIGNAL_VEHICLE_SPEED,
)
from .coordinator import (
    SignalSample,
    TeslaTelemetryCoordinator,
    signal_dispatcher_topic,
)
from .values import (
    value_as_bool,
    value_as_charge_state,
    value_as_enum_name,
    value_as_float,
    value_as_string,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: TeslaTelemetryCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    async_add_entities(
        [
            # Driving / nav
            SpeedSensor(coordinator),
            DistanceToArrivalSensor(coordinator),
            TimeToArrivalSensor(coordinator),
            TrafficDelaySensor(coordinator),
            OdometerSensor(coordinator),
            GearSensor(coordinator),
            # Battery / range
            BatteryLevelSensor(coordinator),
            SocSensor(coordinator),
            EstBatteryRangeSensor(coordinator),
            RatedRangeSensor(coordinator),
            # Charging
            ChargingStateSensor(coordinator),
            ChargeRateSensor(coordinator),
            AcChargingPowerSensor(coordinator),
            DcChargingPowerSensor(coordinator),
            AcChargingEnergyInSensor(coordinator),
            DcChargingEnergyInSensor(coordinator),
            ChargeAmpsSensor(coordinator),
            ChargerVoltageSensor(coordinator),
            FastChargerPresentSensor(coordinator),
            ChargingCableTypeSensor(coordinator),
            ChargeLimitSocSensor(coordinator),
            TimeToFullChargeSensor(coordinator),
            # Climate / cabin
            InsideTempSensor(coordinator),
            OutsideTempSensor(coordinator),
            HvacLeftTempRequestSensor(coordinator),
            HvacRightTempRequestSensor(coordinator),
            # TPMS
            TirePressureFlSensor(coordinator),
            TirePressureFrSensor(coordinator),
            TirePressureRlSensor(coordinator),
            TirePressureRrSensor(coordinator),
            # Software update
            SoftwareVersionSensor(coordinator),
            SoftwareUpdateDownloadSensor(coordinator),
            SoftwareUpdateInstallSensor(coordinator),
        ]
    )


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _BaseTelemetrySensor(SensorEntity):
    """Subscribe to one signal and call `_handle` on every sample."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _signal_name: str = ""

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        sample = self._coordinator.get(self._signal_name)
        if sample is not None:
            self._handle(sample)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_dispatcher_topic(
                    self._coordinator.vin, self._signal_name
                ),
                self._on_sample,
            )
        )

    @callback
    def _on_sample(self, sample: SignalSample) -> None:
        self._handle(sample)
        if self.hass is not None:
            self.async_write_ha_state()

    def _handle(self, sample: SignalSample) -> None:
        raise NotImplementedError


def _scalar_sensor(
    *,
    signal: str,
    suffix: str,
    name: str,
    device_class: SensorDeviceClass | None = None,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
    unit: str | None = None,
    precision: int | None = None,
    extractor: Callable[[Any], Any] = value_as_float,
) -> type[_BaseTelemetrySensor]:
    """Build a small SensorEntity subclass for a numeric signal.

    Most charging/range/temperature/TPMS sensors are identical except for the
    signal name, unit, and device class — this factory removes the boilerplate.
    """

    class _Sensor(_BaseTelemetrySensor):
        _signal_name = signal
        _attr_name = name
        _attr_device_class = device_class
        _attr_state_class = state_class
        _attr_native_unit_of_measurement = unit
        _attr_suggested_display_precision = precision

        def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
            super().__init__(coordinator)
            self._attr_unique_id = f"{coordinator.vin}_{suffix}"

        def _handle(self, sample: SignalSample) -> None:
            self._attr_native_value = extractor(sample.value)

    _Sensor.__name__ = f"_{suffix.title().replace('_', '')}"
    return _Sensor


# ---------------------------------------------------------------------------
# Driving / nav
# ---------------------------------------------------------------------------
class SpeedSensor(_BaseTelemetrySensor):
    _signal_name = SIGNAL_VEHICLE_SPEED
    _attr_name = "Speed"
    _attr_device_class = SensorDeviceClass.SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfSpeed.MILES_PER_HOUR
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_vehicle_speed_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_float(sample.value)


class DistanceToArrivalSensor(_BaseTelemetrySensor):
    _signal_name = SIGNAL_MILES_TO_ARRIVAL
    _attr_name = "Distance to arrival"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.MILES
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_miles_to_arrival_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_float(sample.value)


class TimeToArrivalSensor(_BaseTelemetrySensor):
    """Anchor the absolute ETA against the vehicle-side ``created_at`` so
    jittery delivery doesn't make the rendered "5 min from now" jump around."""

    _signal_name = SIGNAL_MINUTES_TO_ARRIVAL
    _attr_name = "Time to arrival"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_time_to_arrival_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        minutes = value_as_float(sample.value)
        if minutes is None or minutes < 0:
            self._attr_native_value = None
            return
        ref = sample.payload_created_at or sample.received_at
        self._attr_native_value = datetime.fromtimestamp(
            ref + minutes * 60, tz=timezone.utc
        )


class TrafficDelaySensor(_BaseTelemetrySensor):
    _signal_name = SIGNAL_ROUTE_TRAFFIC_DELAY
    _attr_name = "Traffic delay"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_traffic_delay_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_float(sample.value)


OdometerSensor = _scalar_sensor(
    signal=SIGNAL_ODOMETER,
    suffix="odometer_telemetry",
    name="Odometer",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.TOTAL_INCREASING,
    unit=UnitOfLength.MILES,
    precision=1,
)


class GearSensor(_BaseTelemetrySensor):
    """Friendly shift-state string (P/R/N/D) extracted from ShiftState enum."""

    _signal_name = SIGNAL_GEAR
    _attr_name = "Gear"
    _attr_state_class = None

    _GEAR_MAP = {
        "ShiftStateP": "P",
        "ShiftStateR": "R",
        "ShiftStateN": "N",
        "ShiftStateD": "D",
        "ShiftStateInvalid": None,
        "ShiftStateUnknown": None,
    }

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_gear_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        name = value_as_enum_name(sample.value)
        self._attr_native_value = self._GEAR_MAP.get(name, name) if name else None


# ---------------------------------------------------------------------------
# Battery / range
# ---------------------------------------------------------------------------
BatteryLevelSensor = _scalar_sensor(
    signal=SIGNAL_BATTERY_LEVEL,
    suffix="battery_level_telemetry",
    name="Battery level",
    device_class=SensorDeviceClass.BATTERY,
    unit=PERCENTAGE,
    precision=0,
)

SocSensor = _scalar_sensor(
    signal=SIGNAL_SOC,
    suffix="soc_telemetry",
    name="State of charge",
    device_class=SensorDeviceClass.BATTERY,
    unit=PERCENTAGE,
    precision=1,
)

EstBatteryRangeSensor = _scalar_sensor(
    signal=SIGNAL_EST_BATTERY_RANGE,
    suffix="battery_range_telemetry",
    name="Battery range",
    device_class=SensorDeviceClass.DISTANCE,
    unit=UnitOfLength.MILES,
    precision=0,
)

RatedRangeSensor = _scalar_sensor(
    signal=SIGNAL_RATED_RANGE,
    suffix="rated_range_telemetry",
    name="Rated range",
    device_class=SensorDeviceClass.DISTANCE,
    unit=UnitOfLength.MILES,
    precision=0,
)


# ---------------------------------------------------------------------------
# Charging
# ---------------------------------------------------------------------------
class ChargingStateSensor(_BaseTelemetrySensor):
    """Friendly charging state string (charging/disconnected/etc).

    Tracks Tesla's ``DetailedChargeState`` enum, mapped to lower-snake-case
    strings exposed as an ENUM sensor.
    """

    _signal_name = SIGNAL_DETAILED_CHARGE_STATE
    _attr_name = "Charging state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "disconnected",
        "no_power",
        "starting",
        "charging",
        "complete",
        "stopped",
    ]
    _attr_state_class = None

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_charging_state_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_charge_state(sample.value)


ChargeRateSensor = _scalar_sensor(
    signal=SIGNAL_CHARGE_RATE_MILES_PER_HOUR,
    suffix="charge_rate_telemetry",
    name="Charge rate",
    device_class=SensorDeviceClass.SPEED,
    unit=UnitOfSpeed.MILES_PER_HOUR,
    precision=1,
)

AcChargingPowerSensor = _scalar_sensor(
    signal=SIGNAL_AC_CHARGING_POWER,
    suffix="ac_charging_power_telemetry",
    name="AC charging power",
    device_class=SensorDeviceClass.POWER,
    unit=UnitOfPower.KILO_WATT,
    precision=2,
)

DcChargingPowerSensor = _scalar_sensor(
    signal=SIGNAL_DC_CHARGING_POWER,
    suffix="dc_charging_power_telemetry",
    name="DC charging power",
    device_class=SensorDeviceClass.POWER,
    unit=UnitOfPower.KILO_WATT,
    precision=1,
)

AcChargingEnergyInSensor = _scalar_sensor(
    signal=SIGNAL_AC_CHARGING_ENERGY_IN,
    suffix="ac_charging_energy_in_telemetry",
    name="AC charge energy added",
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    unit=UnitOfEnergy.KILO_WATT_HOUR,
    precision=2,
)

DcChargingEnergyInSensor = _scalar_sensor(
    signal=SIGNAL_DC_CHARGING_ENERGY_IN,
    suffix="dc_charging_energy_in_telemetry",
    name="DC charge energy added",
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    unit=UnitOfEnergy.KILO_WATT_HOUR,
    precision=2,
)

ChargeAmpsSensor = _scalar_sensor(
    signal=SIGNAL_CHARGE_AMPS,
    suffix="charge_amps_telemetry",
    name="Charger current",
    device_class=SensorDeviceClass.CURRENT,
    unit=UnitOfElectricCurrent.AMPERE,
    precision=1,
)

ChargerVoltageSensor = _scalar_sensor(
    signal=SIGNAL_CHARGER_VOLTAGE,
    suffix="charger_voltage_telemetry",
    name="Charger voltage",
    device_class=SensorDeviceClass.VOLTAGE,
    unit=UnitOfElectricPotential.VOLT,
    precision=0,
)


class FastChargerPresentSensor(_BaseTelemetrySensor):
    """Friendly fast-charger type (Supercharger/CCS/CHAdeMO/none).

    The signal is sometimes a bool (presence) and sometimes the FastCharger
    enum naming the charger type — handle both.
    """

    _signal_name = SIGNAL_FAST_CHARGER_PRESENT
    _attr_name = "Fast charger type"
    _attr_state_class = None

    _MAP = {
        "FastChargerUnknown": None,
        "FastChargerSupercharger": "Supercharger",
        "FastChargerCHAdeMO": "CHAdeMO",
        "FastChargerGB": "GB",
        "FastChargerACSingleWireCAN": "AC",
        "FastChargerCombo": "Combo",
        "FastChargerMCSingleWireCAN": "MC",
        "FastChargerOther": "Other",
    }

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_fast_charger_present_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        name = value_as_enum_name(sample.value)
        if name in self._MAP:
            self._attr_native_value = self._MAP[name]
            return
        bv = value_as_bool(sample.value)
        if bv is None:
            self._attr_native_value = None
        else:
            self._attr_native_value = "Fast" if bv else "None"


class ChargingCableTypeSensor(_BaseTelemetrySensor):
    _signal_name = SIGNAL_CHARGING_CABLE_TYPE
    _attr_name = "Charging cable"
    _attr_state_class = None

    _MAP = {
        "CableTypeUnknown": None,
        "CableTypeIEC": "IEC",
        "CableTypeSAE": "SAE",
        "CableTypeGB_AC": "GB_AC",
        "CableTypeGB_DC": "GB_DC",
        "CableTypeSNA": None,
    }

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_charging_cable_type_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        name = value_as_enum_name(sample.value)
        self._attr_native_value = self._MAP.get(name, value_as_string(sample.value))


ChargeLimitSocSensor = _scalar_sensor(
    signal=SIGNAL_CHARGE_LIMIT_SOC,
    suffix="charge_limit_soc_telemetry",
    name="Charge limit",
    state_class=None,
    unit=PERCENTAGE,
    precision=0,
)


class TimeToFullChargeSensor(_BaseTelemetrySensor):
    """Hours-until-full as a duration in minutes (Tesla emits hours as a float)."""

    _signal_name = SIGNAL_TIME_TO_FULL_CHARGE
    _attr_name = "Time to full charge"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_time_to_full_charge_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        hours = value_as_float(sample.value)
        if hours is None or hours < 0:
            self._attr_native_value = None
            return
        self._attr_native_value = round(hours * 60)


# ---------------------------------------------------------------------------
# Climate / cabin
# ---------------------------------------------------------------------------
InsideTempSensor = _scalar_sensor(
    signal=SIGNAL_INSIDE_TEMP,
    suffix="inside_temperature_telemetry",
    name="Inside temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)

OutsideTempSensor = _scalar_sensor(
    signal=SIGNAL_OUTSIDE_TEMP,
    suffix="outside_temperature_telemetry",
    name="Outside temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)

HvacLeftTempRequestSensor = _scalar_sensor(
    signal=SIGNAL_HVAC_LEFT_TEMP_REQUEST,
    suffix="hvac_left_temp_request_telemetry",
    name="Climate left setpoint",
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=None,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)

HvacRightTempRequestSensor = _scalar_sensor(
    signal=SIGNAL_HVAC_RIGHT_TEMP_REQUEST,
    suffix="hvac_right_temp_request_telemetry",
    name="Climate right setpoint",
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=None,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)


# ---------------------------------------------------------------------------
# TPMS
# ---------------------------------------------------------------------------
def _tpms(signal: str, position: str) -> type[_BaseTelemetrySensor]:
    return _scalar_sensor(
        signal=signal,
        suffix=f"tire_pressure_{position}_telemetry",
        name=f"Tire pressure {position.replace('_', ' ')}",
        device_class=SensorDeviceClass.PRESSURE,
        unit=UnitOfPressure.BAR,
        precision=2,
    )


TirePressureFlSensor = _tpms(SIGNAL_TPMS_PRESSURE_FL, "front_left")
TirePressureFrSensor = _tpms(SIGNAL_TPMS_PRESSURE_FR, "front_right")
TirePressureRlSensor = _tpms(SIGNAL_TPMS_PRESSURE_RL, "rear_left")
TirePressureRrSensor = _tpms(SIGNAL_TPMS_PRESSURE_RR, "rear_right")


# ---------------------------------------------------------------------------
# Software update
# ---------------------------------------------------------------------------
class SoftwareVersionSensor(_BaseTelemetrySensor):
    _signal_name = SIGNAL_SOFTWARE_UPDATE_VERSION
    _attr_name = "Software version"
    _attr_state_class = None

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_software_version_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_string(sample.value)


SoftwareUpdateDownloadSensor = _scalar_sensor(
    signal=SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT,
    suffix="software_update_download_telemetry",
    name="Software update download",
    state_class=None,
    unit=PERCENTAGE,
    precision=0,
)

SoftwareUpdateInstallSensor = _scalar_sensor(
    signal=SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT,
    suffix="software_update_install_telemetry",
    name="Software update install",
    state_class=None,
    unit=PERCENTAGE,
    precision=0,
)
