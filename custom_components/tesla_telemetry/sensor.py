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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_COST_PER_MILLION_SIGNALS,
    DEFAULT_COST_PER_MILLION_SIGNALS,
    DOMAIN,
    SIGNAL_AC_CHARGING_ENERGY_IN,
    SIGNAL_AC_CHARGING_POWER,
    SIGNAL_BATTERY_LEVEL,
    SIGNAL_CHARGE_LIMIT_SOC,
    SIGNAL_CHARGING_CABLE_TYPE,
    SIGNAL_COUNT_FLUSH_INTERVAL_SECONDS,
    SIGNAL_DC_CHARGING_ENERGY_IN,
    SIGNAL_DC_CHARGING_POWER,
    SIGNAL_DETAILED_CHARGE_STATE,
    SIGNAL_EST_BATTERY_RANGE,
    SIGNAL_FAST_CHARGER_PRESENT,
    SIGNAL_GEAR,
    SIGNAL_INSIDE_TEMP,
    SIGNAL_MILES_TO_ARRIVAL,
    SIGNAL_MINUTES_TO_ARRIVAL,
    SIGNAL_MODULE_TEMP_MAX,
    SIGNAL_MODULE_TEMP_MIN,
    SIGNAL_MOTOR_STATOR_TEMP_FRONT,
    SIGNAL_MOTOR_STATOR_TEMP_REAR,
    SIGNAL_OUTSIDE_TEMP,
    SIGNAL_RATED_RANGE,
    SIGNAL_SOC,
    SIGNAL_SOFTWARE_UPDATE_DOWNLOAD_PCT,
    SIGNAL_SOFTWARE_UPDATE_INSTALL_PCT,
    SIGNAL_SOFTWARE_UPDATE_VERSION,
    SIGNAL_TIME_TO_FULL_CHARGE,
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
            GearSensor(coordinator),
            # Battery / range
            BatteryLevelSensor(coordinator),
            SocSensor(coordinator),
            EstBatteryRangeSensor(coordinator),
            RatedRangeSensor(coordinator),
            # Charging
            ChargingStateSensor(coordinator),
            AcChargingPowerSensor(coordinator),
            DcChargingPowerSensor(coordinator),
            AcChargingEnergyInSensor(coordinator),
            DcChargingEnergyInSensor(coordinator),
            FastChargerPresentSensor(coordinator),
            ChargingCableTypeSensor(coordinator),
            ChargeLimitSocSensor(coordinator),
            TimeToFullChargeSensor(coordinator),
            # Climate / cabin
            InsideTempSensor(coordinator),
            OutsideTempSensor(coordinator),
            # Software update
            SoftwareVersionSensor(coordinator),
            SoftwareUpdateDownloadSensor(coordinator),
            SoftwareUpdateInstallSensor(coordinator),
            # Powertrain / performance
            MotorStatorTempFrontSensor(coordinator),
            MotorStatorTempRearSensor(coordinator),
            ModuleTempMaxSensor(coordinator),
            ModuleTempMinSensor(coordinator),
            AvgBatteryTempSensor(coordinator),
            # Signal accounting / estimated cost
            SignalsReceivedSensor(coordinator),
            EstimatedSignalCostSensor(coordinator, entry),
        ]
    )


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class _BaseTelemetrySensor(RestoreSensor):
    """Subscribe to one signal and call `_handle` on every sample.

    Inherits ``RestoreSensor`` so the last value survives a restart — the
    telemetry stream is push-on-change, so slow signals (tire pressure,
    odometer, etc.) would otherwise read ``unknown`` until they next change.
    """

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
        else:
            await self._async_restore_last()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_dispatcher_topic(
                    self._coordinator.vin, self._signal_name
                ),
                self._on_sample,
            )
        )

    async def _async_restore_last(self) -> None:
        """Restore the last native value across restarts.

        Holds the stale-but-useful reading until fresh telemetry arrives;
        a no-op when there's nothing stored (first run).
        """
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value

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
            ref + minutes * 60, tz=UTC
        )


class GearSensor(_BaseTelemetrySensor):
    """Friendly shift-state string (P/R/N/D) extracted from ShiftState enum."""

    _signal_name = SIGNAL_GEAR
    _attr_name = "Gear"
    _attr_state_class = None

    _GEAR_MAP: ClassVar[dict[str, str | None]] = {
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
    # HA declares _attr_options as a per-instance attribute, so it can't
    # be a ClassVar; this list is never mutated.
    _attr_options = [  # noqa: RUF012
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

class FastChargerPresentSensor(_BaseTelemetrySensor):
    """Friendly fast-charger type (Supercharger/CCS/CHAdeMO/none).

    The signal is sometimes a bool (presence) and sometimes the FastCharger
    enum naming the charger type — handle both.
    """

    _signal_name = SIGNAL_FAST_CHARGER_PRESENT
    _attr_name = "Fast charger type"
    _attr_state_class = None

    _MAP: ClassVar[dict[str, str | None]] = {
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

    _MAP: ClassVar[dict[str, str | None]] = {
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


# ---------------------------------------------------------------------------
# Powertrain / performance
# ---------------------------------------------------------------------------
MotorStatorTempFrontSensor = _scalar_sensor(
    signal=SIGNAL_MOTOR_STATOR_TEMP_FRONT,
    suffix="motor_stator_temp_front_telemetry",
    name="Front motor stator temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    unit=UnitOfTemperature.CELSIUS,
    precision=0,
)

MotorStatorTempRearSensor = _scalar_sensor(
    signal=SIGNAL_MOTOR_STATOR_TEMP_REAR,
    suffix="motor_stator_temp_rear_telemetry",
    name="Rear motor stator temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    unit=UnitOfTemperature.CELSIUS,
    precision=0,
)

ModuleTempMaxSensor = _scalar_sensor(
    signal=SIGNAL_MODULE_TEMP_MAX,
    suffix="battery_module_temp_max_telemetry",
    name="Battery temperature (max)",
    device_class=SensorDeviceClass.TEMPERATURE,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)

ModuleTempMinSensor = _scalar_sensor(
    signal=SIGNAL_MODULE_TEMP_MIN,
    suffix="battery_module_temp_min_telemetry",
    name="Battery temperature (min)",
    device_class=SensorDeviceClass.TEMPERATURE,
    unit=UnitOfTemperature.CELSIUS,
    precision=1,
)


class AvgBatteryTempSensor(_BaseTelemetrySensor):
    """Representative HV battery temperature: mean of the hottest and coldest
    module.

    Tesla doesn't stream a single pack temperature — only ``ModuleTempMax`` and
    ``ModuleTempMin`` (the extremes across the pack).  Their mean is the most
    meaningful "actual" pack temp.  Subscribes to both and recomputes whenever
    either updates, rendering ``unavailable`` until both have arrived.
    """

    _attr_name = "Battery temperature (avg)"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_battery_temp_avg_telemetry"

    async def async_added_to_hass(self) -> None:
        self._recompute()
        if self._attr_native_value is None:
            await self._async_restore_last()
        for signal in (SIGNAL_MODULE_TEMP_MAX, SIGNAL_MODULE_TEMP_MIN):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    signal_dispatcher_topic(self._coordinator.vin, signal),
                    self._on_either_sample,
                )
            )

    @callback
    def _on_either_sample(self, sample: SignalSample) -> None:
        self._recompute()
        if self.hass is not None:
            self.async_write_ha_state()

    def _recompute(self) -> None:
        hi_sample = self._coordinator.get(SIGNAL_MODULE_TEMP_MAX)
        lo_sample = self._coordinator.get(SIGNAL_MODULE_TEMP_MIN)
        if hi_sample is None or lo_sample is None:
            self._attr_native_value = None
            return
        hi = value_as_float(hi_sample.value)
        lo = value_as_float(lo_sample.value)
        if hi is None or lo is None:
            self._attr_native_value = None
            return
        self._attr_native_value = (hi + lo) / 2.0


# ---------------------------------------------------------------------------
# Signal accounting / estimated cost
# ---------------------------------------------------------------------------
class _SignalStatSensor(RestoreSensor):
    """Diagnostic sensor reporting a coordinator-maintained running total.

    Flushes to HA state on a fixed timer instead of on every signal: the
    whole point is to *measure* signal volume, so writing a state row per
    signal would recreate the recorder load we're trying to observe. One
    write per ``SIGNAL_COUNT_FLUSH_INTERVAL_SECONDS`` regardless of rate.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_sensor_data()
        restored = last.native_value if last is not None else None
        self._apply_restore(restored)
        self.async_write_ha_state()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._flush,
                timedelta(seconds=SIGNAL_COUNT_FLUSH_INTERVAL_SECONDS),
            )
        )

    def _apply_restore(self, restored: Any) -> None:
        """Seed persisted lifetime state from the restored native value.

        Default no-op — purely derived sensors (e.g. cost) have nothing of
        their own to restore.
        """

    @callback
    def _flush(self, _now: datetime) -> None:
        self.async_write_ha_state()


class SignalsReceivedSensor(_SignalStatSensor):
    """Lifetime count of Tesla-billed signals received for this vehicle.

    ``TOTAL_INCREASING`` so it feeds long-term statistics and a
    ``utility_meter`` (daily/monthly signals). The ``by_signal`` breakdown in
    the attributes covers only the current process (``signals_since_restart``);
    the state itself is the restored lifetime total plus that.
    """

    _attr_name = "Signals received"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: TeslaTelemetryCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.vin}_signals_received_telemetry"

    def _apply_restore(self, restored: Any) -> None:
        if restored is None:
            return
        try:
            self._coordinator.restored_signal_base = int(float(restored))
        except (TypeError, ValueError):
            pass

    @property
    def native_value(self) -> int:
        return self._coordinator.lifetime_signals

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "signals_since_restart": self._coordinator.signals_since_start,
            "by_signal": dict(self._coordinator.signal_counts.most_common()),
        }


class EstimatedSignalCostSensor(_SignalStatSensor):
    """Running estimate of what Tesla bills for this vehicle's stream.

    Purely derived: lifetime signal count × the configured rate (read live
    from ``entry.options``, so editing it in the options flow takes effect on
    the next flush). Rendered in the HA-configured currency.
    """

    _attr_name = "Estimated signal cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:cash"

    def __init__(
        self, coordinator: TeslaTelemetryCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = (
            f"{coordinator.vin}_estimated_signal_cost_telemetry"
        )

    @property
    def _rate_per_million(self) -> float:
        return self._entry.options.get(
            CONF_COST_PER_MILLION_SIGNALS, DEFAULT_COST_PER_MILLION_SIGNALS
        )

    @property
    def native_unit_of_measurement(self) -> str:
        return (self.hass.config.currency if self.hass else None) or "USD"

    @property
    def native_value(self) -> float:
        signals = self._coordinator.lifetime_signals
        return round(signals * self._rate_per_million / 1_000_000, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "cost_per_million_signals": round(self._rate_per_million, 6),
            "signals_counted": self._coordinator.lifetime_signals,
        }
