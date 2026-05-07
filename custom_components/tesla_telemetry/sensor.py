"""Sensor entities for Tesla Fleet Telemetry.

Each entity subscribes to one signal name on the per-VIN coordinator and
renders the latest value.  Decoding of the raw ``Value`` oneof lives in
``values.py``; this file is only concerned with HA entity wiring.
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
from homeassistant.helpers.device_registry import DeviceInfo
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
            RoadrunnerSpeedSensor(coordinator),
            RoadrunnerDistanceToArrivalSensor(coordinator),
            RoadrunnerTimeToArrivalSensor(coordinator),
            RoadrunnerTrafficDelaySensor(coordinator),
            RoadrunnerOdometerSensor(coordinator),
            RoadrunnerGearSensor(coordinator),
            # Battery / range
            RoadrunnerBatteryLevelSensor(coordinator),
            RoadrunnerSocSensor(coordinator),
            RoadrunnerEstBatteryRangeSensor(coordinator),
            RoadrunnerRatedRangeSensor(coordinator),
            # Charging
            RoadrunnerChargingStateSensor(coordinator),
            RoadrunnerChargeRateSensor(coordinator),
            RoadrunnerAcChargingPowerSensor(coordinator),
            RoadrunnerDcChargingPowerSensor(coordinator),
            RoadrunnerAcChargingEnergyInSensor(coordinator),
            RoadrunnerDcChargingEnergyInSensor(coordinator),
            RoadrunnerChargeAmpsSensor(coordinator),
            RoadrunnerChargerVoltageSensor(coordinator),
            RoadrunnerFastChargerPresentSensor(coordinator),
            RoadrunnerChargingCableTypeSensor(coordinator),
            RoadrunnerChargeLimitSocSensor(coordinator),
            RoadrunnerTimeToFullChargeSensor(coordinator),
            # Climate / cabin
            RoadrunnerInsideTempSensor(coordinator),
            RoadrunnerOutsideTempSensor(coordinator),
            RoadrunnerHvacLeftTempRequestSensor(coordinator),
            RoadrunnerHvacRightTempRequestSensor(coordinator),
            # TPMS
            RoadrunnerTirePressureFlSensor(coordinator),
            RoadrunnerTirePressureFrSensor(coordinator),
            RoadrunnerTirePressureRlSensor(coordinator),
            RoadrunnerTirePressureRrSensor(coordinator),
            # Software update
            RoadrunnerSoftwareVersionSensor(coordinator),
            RoadrunnerSoftwareUpdateDownloadSensor(coordinator),
            RoadrunnerSoftwareUpdateInstallSensor(coordinator),
        ]
    )


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _BaseRoadrunnerSensor(SensorEntity):
    """Subscribe to one signal and call `_handle` on every sample."""

    _attr_should_poll = False
    _signal_name: str = ""

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.vin)},
            manufacturer="Tesla",
            name="Roadrunner",
        )

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
) -> type[_BaseRoadrunnerSensor]:
    """Build a small SensorEntity subclass for a numeric signal.

    Most charging/range/temperature/TPMS sensors are identical except for the
    signal name, unit, and device class — this factory removes the boilerplate.
    """

    class _Sensor(_BaseRoadrunnerSensor):
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

    _Sensor.__name__ = f"_Roadrunner{suffix.title().replace('_', '')}"
    return _Sensor


# ---------------------------------------------------------------------------
# Driving / nav
# ---------------------------------------------------------------------------
class RoadrunnerSpeedSensor(_BaseRoadrunnerSensor):
    _signal_name = SIGNAL_VEHICLE_SPEED
    _attr_name = "Roadrunner Speed Telemetry"
    _attr_device_class = SensorDeviceClass.SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfSpeed.MILES_PER_HOUR
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_vehicle_speed_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_float(sample.value)


class RoadrunnerDistanceToArrivalSensor(_BaseRoadrunnerSensor):
    _signal_name = SIGNAL_MILES_TO_ARRIVAL
    _attr_name = "Roadrunner Distance to Arrival Telemetry"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfLength.MILES
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_miles_to_arrival_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_float(sample.value)


class RoadrunnerTimeToArrivalSensor(_BaseRoadrunnerSensor):
    """Anchor the absolute ETA against the vehicle-side ``created_at`` so
    jittery delivery doesn't make the rendered "5 min from now" jump around."""

    _signal_name = SIGNAL_MINUTES_TO_ARRIVAL
    _attr_name = "Roadrunner Time to Arrival Telemetry"
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


class RoadrunnerTrafficDelaySensor(_BaseRoadrunnerSensor):
    _signal_name = SIGNAL_ROUTE_TRAFFIC_DELAY
    _attr_name = "Roadrunner Traffic Delay Telemetry"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_traffic_delay_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_float(sample.value)


RoadrunnerOdometerSensor = _scalar_sensor(
    signal=SIGNAL_ODOMETER,
    suffix="odometer_telemetry",
    name="Roadrunner Odometer Telemetry",
    device_class=SensorDeviceClass.DISTANCE,
    state_class=SensorStateClass.TOTAL_INCREASING,
    unit=UnitOfLength.MILES,
    precision=1,
)


class RoadrunnerGearSensor(_BaseRoadrunnerSensor):
    """Friendly shift-state string (P/R/N/D) extracted from ShiftState enum."""

    _signal_name = SIGNAL_GEAR
    _attr_name = "Roadrunner Gear Telemetry"
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
RoadrunnerBatteryLevelSensor = _scalar_sensor(
    signal=SIGNAL_BATTERY_LEVEL,
    suffix="battery_level_telemetry",
    name="Roadrunner Battery Level Telemetry",
    device_class=SensorDeviceClass.BATTERY,
    unit=PERCENTAGE,
    precision=0,
)

RoadrunnerSocSensor = _scalar_sensor(
    signal=SIGNAL_SOC,
    suffix="soc_telemetry",
    name="Roadrunner State of Charge Telemetry",
    device_class=SensorDeviceClass.BATTERY,
    unit=PERCENTAGE,
    precision=1,
)

RoadrunnerEstBatteryRangeSensor = _scalar_sensor(
    signal=SIGNAL_EST_BATTERY_RANGE,
    suffix="battery_range_telemetry",
    name="Roadrunner Battery Range Telemetry",
    device_class=SensorDeviceClass.DISTANCE,
    unit=UnitOfLength.MILES,
    precision=0,
)

RoadrunnerRatedRangeSensor = _scalar_sensor(
    signal=SIGNAL_RATED_RANGE,
    suffix="rated_range_telemetry",
    name="Roadrunner Rated Range Telemetry",
    device_class=SensorDeviceClass.DISTANCE,
    unit=UnitOfLength.MILES,
    precision=0,
)


# ---------------------------------------------------------------------------
# Charging
# ---------------------------------------------------------------------------
class RoadrunnerChargingStateSensor(_BaseRoadrunnerSensor):
    """Friendly charging state string (charging/disconnected/etc).

    Tracks Tesla's ``DetailedChargeState`` enum, mapped to lower-snake-case
    so the legacy ``sensor.roadrunner_charging`` UI shows the same labels.
    """

    _signal_name = SIGNAL_DETAILED_CHARGE_STATE
    _attr_name = "Roadrunner Charging State Telemetry"
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


RoadrunnerChargeRateSensor = _scalar_sensor(
    signal=SIGNAL_CHARGE_RATE_MILES_PER_HOUR,
    suffix="charge_rate_telemetry",
    name="Roadrunner Charge Rate Telemetry",
    device_class=SensorDeviceClass.SPEED,
    unit=UnitOfSpeed.MILES_PER_HOUR,
    precision=1,
)

RoadrunnerAcChargingPowerSensor = _scalar_sensor(
    signal=SIGNAL_AC_CHARGING_POWER,
    suffix="ac_charging_power_telemetry",
    name="Roadrunner AC Charging Power Telemetry",
    device_class=SensorDeviceClass.POWER,
    unit=UnitOfPower.KILO_WATT,
    precision=2,
)

RoadrunnerDcChargingPowerSensor = _scalar_sensor(
    signal=SIGNAL_DC_CHARGING_POWER,
    suffix="dc_charging_power_telemetry",
    name="Roadrunner DC Charging Power Telemetry",
    device_class=SensorDeviceClass.POWER,
    unit=UnitOfPower.KILO_WATT,
    precision=1,
)

RoadrunnerAcChargingEnergyInSensor = _scalar_sensor(
    signal=SIGNAL_AC_CHARGING_ENERGY_IN,
    suffix="ac_charging_energy_in_telemetry",
    name="Roadrunner AC Charge Energy Added Telemetry",
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    unit=UnitOfEnergy.KILO_WATT_HOUR,
    precision=2,
)

RoadrunnerDcChargingEnergyInSensor = _scalar_sensor(
    signal=SIGNAL_DC_CHARGING_ENERGY_IN,
    suffix="dc_charging_energy_in_telemetry",
    name="Roadrunner DC Charge Energy Added Telemetry",
    device_class=SensorDeviceClass.ENERGY,
    state_class=SensorStateClass.TOTAL_INCREASING,
    unit=UnitOfEnergy.KILO_WATT_HOUR,
    precision=2,
)

RoadrunnerChargeAmpsSensor = _scalar_sensor(
    signal=SIGNAL_CHARGE_AMPS,
    suffix="charge_amps_telemetry",
    name="Roadrunner Charger Current Telemetry",
    device_class=SensorDeviceClass.CURRENT,
    unit=UnitOfElectricCurrent.AMPERE,
    precision=1,
)

RoadrunnerChargerVoltageSensor = _scalar_sensor(
    signal=SIGNAL_CHARGER_VOLTAGE,
    suffix="charger_voltage_telemetry",
    name="Roadrunner Charger Voltage Telemetry",
    device_class=SensorDeviceClass.VOLTAGE,
    unit=UnitOfElectricPotential.VOLT,
    precision=0,
)


class RoadrunnerFastChargerPresentSensor(_BaseRoadrunnerSensor):
    """Friendly fast-charger type (Supercharger/CCS/CHAdeMO/none).

    The signal is sometimes a bool (presence) and sometimes the FastCharger
    enum naming the charger type — handle both.
    """

    _signal_name = SIGNAL_FAST_CHARGER_PRESENT
    _attr_name = "Roadrunner Fast Charger Type Telemetry"
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


class RoadrunnerChargingCableTypeSensor(_BaseRoadrunnerSensor):
    _signal_name = SIGNAL_CHARGING_CABLE_TYPE
    _attr_name = "Roadrunner Charging Cable Telemetry"
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


RoadrunnerChargeLimitSocSensor = _scalar_sensor(
    signal=SIGNAL_CHARGE_LIMIT_SOC,
    suffix="charge_limit_soc_telemetry",
    name="Roadrunner Charge Limit Telemetry",
    state_class=None,
    unit=PERCENTAGE,
    precision=0,
)


class RoadrunnerTimeToFullChargeSensor(_BaseRoadrunnerSensor):
    """Hours-until-full as a duration in minutes (Tesla emits hours as a float)."""

    _signal_name = SIGNAL_TIME_TO_FULL_CHARGE
    _attr_name = "Roadrunner Time to Full Charge Telemetry"
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
RoadrunnerInsideTempSensor = _scalar_sensor(
    signal=SIGNAL_INSIDE_TEMP,
    suffix="inside_temperature_telemetry",
    name="Roadrunner Inside Temperature Telemetry",
    device_class=SensorDeviceClass.TEMPERATURE,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)

RoadrunnerOutsideTempSensor = _scalar_sensor(
    signal=SIGNAL_OUTSIDE_TEMP,
    suffix="outside_temperature_telemetry",
    name="Roadrunner Outside Temperature Telemetry",
    device_class=SensorDeviceClass.TEMPERATURE,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)

RoadrunnerHvacLeftTempRequestSensor = _scalar_sensor(
    signal=SIGNAL_HVAC_LEFT_TEMP_REQUEST,
    suffix="hvac_left_temp_request_telemetry",
    name="Roadrunner Climate Left Setpoint Telemetry",
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=None,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)

RoadrunnerHvacRightTempRequestSensor = _scalar_sensor(
    signal=SIGNAL_HVAC_RIGHT_TEMP_REQUEST,
    suffix="hvac_right_temp_request_telemetry",
    name="Roadrunner Climate Right Setpoint Telemetry",
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=None,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)


# ---------------------------------------------------------------------------
# TPMS
# ---------------------------------------------------------------------------
def _tpms(signal: str, position: str) -> type[_BaseRoadrunnerSensor]:
    return _scalar_sensor(
        signal=signal,
        suffix=f"tire_pressure_{position}_telemetry",
        name=(
            "Roadrunner Tire Pressure "
            f"{position.replace('_', ' ').title()} Telemetry"
        ),
        device_class=SensorDeviceClass.PRESSURE,
        unit=UnitOfPressure.BAR,
        precision=2,
    )


RoadrunnerTirePressureFlSensor = _tpms(SIGNAL_TPMS_PRESSURE_FL, "front_left")
RoadrunnerTirePressureFrSensor = _tpms(SIGNAL_TPMS_PRESSURE_FR, "front_right")
RoadrunnerTirePressureRlSensor = _tpms(SIGNAL_TPMS_PRESSURE_RL, "rear_left")
RoadrunnerTirePressureRrSensor = _tpms(SIGNAL_TPMS_PRESSURE_RR, "rear_right")


# ---------------------------------------------------------------------------
# Software update
# ---------------------------------------------------------------------------
class RoadrunnerSoftwareVersionSensor(_BaseRoadrunnerSensor):
    _signal_name = SIGNAL_SOFTWARE_UPDATE_VERSION
    _attr_name = "Roadrunner Software Version Telemetry"
    _attr_state_class = None

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_software_version_telemetry"

    def _handle(self, sample: SignalSample) -> None:
        self._attr_native_value = value_as_string(sample.value)


RoadrunnerSoftwareUpdateDownloadSensor = _scalar_sensor(
    signal=SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT,
    suffix="software_update_download_telemetry",
    name="Roadrunner Software Update Download Telemetry",
    state_class=None,
    unit=PERCENTAGE,
    precision=0,
)

RoadrunnerSoftwareUpdateInstallSensor = _scalar_sensor(
    signal=SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT,
    suffix="software_update_install_telemetry",
    name="Roadrunner Software Update Install Telemetry",
    state_class=None,
    unit=PERCENTAGE,
    precision=0,
)
