"""Platform for Kumo Cloud climate integration."""

from __future__ import annotations

import logging
from math import isfinite
import traceback
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.components.climate.const import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import KumoCloudDataUpdateCoordinator, KumoCloudDevice
from .cloud_commands import (
    REPORTED_AUTO_MODES,
    celsius_to_display_fahrenheit,
    fahrenheit_to_cloud_celsius,
    normalize_cloud_commands,
)
from .const import (
    DOMAIN,
    OPERATION_MODE_OFF,
    OPERATION_MODE_COOL,
    OPERATION_MODE_HEAT,
    OPERATION_MODE_DRY,
    OPERATION_MODE_VENT,
    OPERATION_MODE_AUTO,
    FAN_SPEED_AUTO,
    FAN_SPEED_LOW,
    FAN_SPEED_MEDIUM,
    FAN_SPEED_HIGH,
    AIR_DIRECTION_HORIZONTAL,
    AIR_DIRECTION_VERTICAL,
    AIR_DIRECTION_SWING,
)

_LOGGER = logging.getLogger(__name__)


def _log_temp_context(
    device: KumoCloudDevice, where: str, extra: dict[str, Any] | None = None
) -> None:
    """Debug trace: log adapter vs device setpoints (TEMPTRACE)."""
    adapter = device.zone_data.get("adapter", {})
    device_data = device.device_data
    _LOGGER.info(
        "[TEMPTRACE] %s zone=%s serial=%s mode=%s "
        "adapter(spHeat=%s spCool=%s roomTemp=%s power=%s) "
        "device(spHeat=%s spCool=%s roomTemp=%s power=%s) extra=%s",
        where,
        device.zone_id,
        device.device_serial,
        device_data.get("operationMode", adapter.get("operationMode")),
        adapter.get("spHeat"),
        adapter.get("spCool"),
        adapter.get("roomTemp"),
        adapter.get("power"),
        device_data.get("spHeat"),
        device_data.get("spCool"),
        device_data.get("roomTemp"),
        device_data.get("power"),
        extra or {},
    )


# Mapping from Kumo Cloud operation modes to Home Assistant HVAC modes
KUMO_TO_HVAC_MODE = {
    OPERATION_MODE_OFF: HVACMode.OFF,
    OPERATION_MODE_COOL: HVACMode.COOL,
    OPERATION_MODE_HEAT: HVACMode.HEAT,
    OPERATION_MODE_DRY: HVACMode.DRY,
    OPERATION_MODE_VENT: HVACMode.FAN_ONLY,
    **{mode: HVACMode.HEAT_COOL for mode in REPORTED_AUTO_MODES},
}

HVAC_TO_KUMO_MODE = {
    HVACMode.OFF: OPERATION_MODE_OFF,
    HVACMode.COOL: OPERATION_MODE_COOL,
    HVACMode.HEAT: OPERATION_MODE_HEAT,
    HVACMode.DRY: OPERATION_MODE_DRY,
    HVACMode.FAN_ONLY: OPERATION_MODE_VENT,
    # The cloud command API accepts "auto". autoCool/autoHeat are reported
    # device states and remain supported by KUMO_TO_HVAC_MODE above.
    HVACMode.HEAT_COOL: OPERATION_MODE_AUTO,
}

# Reverse mapping
# HVAC_TO_KUMO_MODE = {v: k for k, v in KUMO_TO_HVAC_MODE.items()}

# Fan speed mappings
KUMO_FAN_SPEEDS = [FAN_SPEED_AUTO, FAN_SPEED_LOW, FAN_SPEED_MEDIUM, FAN_SPEED_HIGH]

# Air direction mappings
KUMO_AIR_DIRECTIONS = [
    AIR_DIRECTION_HORIZONTAL,
    AIR_DIRECTION_VERTICAL,
    AIR_DIRECTION_SWING,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Kumo Cloud climate devices."""
    coordinator: KumoCloudDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for zone in coordinator.zones:
        if "adapter" in zone and zone["adapter"]:
            device_serial = zone["adapter"]["deviceSerial"]
            zone_id = zone["id"]

            device = KumoCloudDevice(coordinator, zone_id, device_serial)
            entities.append(KumoCloudClimate(device))

    async_add_entities(entities)


class KumoCloudClimate(CoordinatorEntity, ClimateEntity):
    """Representation of a Kumo Cloud climate device."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, device: KumoCloudDevice) -> None:
        """Initialize the climate device."""
        super().__init__(device.coordinator)
        self.device = device
        self._attr_unique_id = device.unique_id

        # Set up supported features based on device profile
        self._setup_supported_features()

    def _setup_supported_features(self) -> None:
        """Set up supported features based on device capabilities."""
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

        profile = self.device.profile_data
        if profile:
            profile_data = profile[0] if isinstance(profile, list) else profile

            # Check for fan speed support
            if profile_data.get("numberOfFanSpeeds", 0) > 0:
                features |= ClimateEntityFeature.FAN_MODE

            # Check for vane/swing support
            if profile_data.get("hasVaneSwing", False):
                features |= ClimateEntityFeature.SWING_MODE

            if profile_data.get("hasVaneDir", False):
                features |= ClimateEntityFeature.SWING_MODE

        self._attr_supported_features = features

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        zone_data = self.device.zone_data
        device_data = self.device.device_data

        model = device_data.get("model", {}).get("materialDescription", "Unknown Model")

        return DeviceInfo(
            identifiers={(DOMAIN, self.device.device_serial)},
            name=zone_data.get("name", "Kumo Cloud Device"),
            manufacturer="Mitsubishi Electric",
            model=model,
            sw_version=device_data.get("model", {}).get("serialProfile"),
            serial_number=device_data.get("serialNumber"),
        )

    @property
    def _use_fahrenheit(self) -> bool:
        """Report in Fahrenheit when Home Assistant uses Fahrenheit.

        Kumo Cloud works in Celsius, but Mitsubishi maps Fahrenheit through a
        proprietary table. Reporting Celsius lets Home Assistant convert with
        plain math, which shows 69-72 F setpoints one degree high.
        """
        hass = getattr(self, "hass", None)
        if hass is None:
            return False
        return hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT

    @property
    def temperature_unit(self) -> str:
        """Return the unit the entity reports temperatures in."""
        if self._use_fahrenheit:
            return UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.CELSIUS

    def _to_ha(self, celsius: Any) -> Any:
        """Convert a cloud Celsius value to the entity's unit."""
        if celsius is None or not self._use_fahrenheit:
            return celsius
        return celsius_to_display_fahrenheit(celsius)

    def _from_ha(self, value: Any) -> Any:
        """Convert a value in the entity's unit to cloud Celsius."""
        if value is None or not self._use_fahrenheit:
            return value
        return fahrenheit_to_cloud_celsius(value)

    def _raw_current_temperature(self) -> float | None:
        """Return the room temperature in cloud Celsius."""
        adapter = self.device.zone_data.get("adapter", {})
        return adapter.get("roomTemp")

    def _raw_target_temperature(self) -> float | None:
        """Return the active single setpoint in cloud Celsius."""
        adapter = self.device.zone_data.get("adapter", {})
        hvac_mode = self.hvac_mode

        if hvac_mode == HVACMode.COOL:
            return adapter.get("spCool")
        elif hvac_mode == HVACMode.HEAT:
            return adapter.get("spHeat")
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._to_ha(self._raw_current_temperature())

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        return self._to_ha(self._raw_target_temperature())

    @property
    def target_temperature_low(self) -> float | None:
        """Return low target temperature (heat setpoint)."""
        heat, _ = self._current_auto_setpoints()
        return self._to_ha(heat)

    @property
    def target_temperature_high(self) -> float | None:
        """Return high target temperature (cool setpoint)."""
        _, cool = self._current_auto_setpoints()
        return self._to_ha(cool)

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current HVAC mode."""
        # Check both adapter (zone) and device data for most current status
        adapter = self.device.zone_data.get("adapter", {})
        device_data = self.device.device_data

        # Use device data if available (more current), otherwise use adapter data
        operation_mode = device_data.get(
            "operationMode", adapter.get("operationMode", OPERATION_MODE_OFF)
        )
        power = device_data.get("power", adapter.get("power", 0))

        # If power is 0, device is off regardless of operation mode
        if power == 0:
            return HVACMode.OFF

        return KUMO_TO_HVAC_MODE.get(operation_mode, HVACMode.OFF)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available HVAC modes."""
        modes = [HVACMode.OFF]

        profile = self.device.profile_data
        if profile:
            profile_data = profile[0] if isinstance(profile, list) else profile

            # Add modes based on device capabilities
            if profile_data.get("hasModeHeat", False):
                modes.append(HVACMode.HEAT)

            modes.append(HVACMode.COOL)  # All units should support cool

            if profile_data.get("hasModeDry", False):
                modes.append(HVACMode.DRY)

            if profile_data.get("hasModeVent", False):
                modes.append(HVACMode.FAN_ONLY)

            # Auto mode if device supports both heat and cool
            if profile_data.get("hasModeHeat", False):
                modes.append(HVACMode.HEAT_COOL)

        return modes

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return current HVAC action based on actual device status."""
        hvac_mode = self.hvac_mode
        if hvac_mode == HVACMode.OFF:
            return HVACAction.OFF

        # Check both adapter (zone) and device data for most current status
        adapter = self.device.zone_data.get("adapter", {})
        device_data = self.device.device_data

        # Use device data if available (more current), otherwise use adapter data
        power = device_data.get("power", adapter.get("power", 0))
        operation_mode = device_data.get(
            "operationMode", adapter.get("operationMode", OPERATION_MODE_OFF)
        )

        if power == 0:
            return HVACAction.OFF

        # If device is on and has a valid operation mode, show it as active
        if operation_mode == OPERATION_MODE_HEAT:
            # For heating mode, show as heating if power is on
            return HVACAction.HEATING
        elif operation_mode == OPERATION_MODE_COOL:
            # For cooling mode, show as cooling if power is on
            return HVACAction.COOLING
        elif operation_mode == OPERATION_MODE_DRY:
            return HVACAction.DRYING
        elif operation_mode == OPERATION_MODE_VENT:
            return HVACAction.FAN
        elif operation_mode in REPORTED_AUTO_MODES:
            # For auto mode, determine action based on current vs target temperature
            # Compare in cloud Celsius so the 1 degree band means the same
            # thing regardless of the display unit.
            current_temp = self._raw_current_temperature()
            target_temp = self._raw_target_temperature()

            if current_temp is not None and target_temp is not None:
                temp_diff = current_temp - target_temp
                if temp_diff > 1.0:  # More than 1 degree above target
                    return HVACAction.COOLING
                elif temp_diff < -1.0:  # More than 1 degree below target
                    return HVACAction.HEATING

            # Default to idle for auto mode if we can't determine
            return HVACAction.IDLE

        # If power is on but we can't determine the action, show as idle
        return HVACAction.IDLE

    @property
    def fan_mode(self) -> str | None:
        """Return current fan mode."""
        # Check device data first, then adapter data
        device_data = self.device.device_data
        adapter = self.device.zone_data.get("adapter", {})
        return device_data.get("fanSpeed", adapter.get("fanSpeed"))

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the list of available fan modes."""
        profile = self.device.profile_data
        if not profile:
            return None

        profile_data = profile[0] if isinstance(profile, list) else profile
        num_fan_speeds = profile_data.get("numberOfFanSpeeds", 0)

        if num_fan_speeds == 0:
            return None

        # Return fan modes based on number of speeds supported
        modes = [FAN_SPEED_AUTO]
        if num_fan_speeds >= 1:
            modes.append(FAN_SPEED_LOW)
        if num_fan_speeds >= 2:
            modes.append(FAN_SPEED_MEDIUM)
        if num_fan_speeds >= 3:
            modes.append(FAN_SPEED_HIGH)

        return modes

    @property
    def swing_mode(self) -> str | None:
        """Return current swing mode."""
        # Check device data first, then adapter data
        device_data = self.device.device_data
        adapter = self.device.zone_data.get("adapter", {})
        return device_data.get("airDirection", adapter.get("airDirection"))

    @property
    def swing_modes(self) -> list[str] | None:
        """Return the list of available swing modes."""
        profile = self.device.profile_data
        if not profile:
            return None

        profile_data = profile[0] if isinstance(profile, list) else profile

        modes = []
        if profile_data.get("hasVaneDir", False) or profile_data.get(
            "hasVaneSwing", False
        ):
            modes.extend(KUMO_AIR_DIRECTIONS)

        return modes if modes else None

    @property
    def min_temp(self) -> float:
        """Return minimum temperature."""
        profile = self.device.profile_data
        if profile:
            profile_data = profile[0] if isinstance(profile, list) else profile
            min_setpoints = profile_data.get("minimumSetPoints", {})
            # Return the minimum of heat and cool setpoints
            return self._to_ha(
                min(min_setpoints.get("heat", 16), min_setpoints.get("cool", 16))
            )
        return self._to_ha(16.0)

    @property
    def max_temp(self) -> float:
        """Return maximum temperature."""
        profile = self.device.profile_data
        if profile:
            profile_data = profile[0] if isinstance(profile, list) else profile
            max_setpoints = profile_data.get("maximumSetPoints", {})
            # Return the maximum of heat and cool setpoints
            return self._to_ha(
                max(max_setpoints.get("heat", 30), max_setpoints.get("cool", 30))
            )
        return self._to_ha(30.0)

    @property
    def target_temperature_step(self) -> float:
        """Return the supported step of target temperature."""
        if self._use_fahrenheit:
            # Mitsubishi's Fahrenheit table is defined in whole degrees.
            return 1.0
        return 0.5  # Kumo Cloud typically supports 0.5 degree steps

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        # Keep entity available if we have data, even if last update failed
        # This prevents automations from triggering when entity comes back online
        has_data = (
            self.device.zone_data
            and self.device.device_data
            and self.coordinator.data is not None
        )
        # Only mark unavailable if we have no data at all
        # return has_data and self.device.available
        return bool(has_data)

    async def _send_command_and_refresh(self, commands: dict[str, Any]) -> None:
        """Send command and ensure fresh status update."""
        _log_temp_context(
            self.device, "_send_command_and_refresh BEFORE", {"commands": commands}
        )
        stack = "".join(traceback.format_stack(limit=8))
        _LOGGER.info(
            "[TEMPTRACE] command caller stack for %s:\n%s",
            self.device.device_serial,
            stack,
        )

        # The device writes the commanded values as soon as the cloud accepts
        # them, while this service call's context is still attached. The
        # follow-up refresh comes ~10s later, after Home Assistant has
        # dropped the context (5s), so without this the change shows up as
        # external in the logbook.
        await self.device.send_command(
            commands, on_accepted=self.async_write_ha_state
        )

        _log_temp_context(
            self.device, "_send_command_and_refresh AFTER", {"commands": commands}
        )
        self.async_write_ha_state()

    def _current_auto_setpoints(self) -> tuple[Any, Any]:
        """Return one coherent heat/cool pair, preferring zone adapter data."""
        adapter = self.device.zone_data.get("adapter", {})
        device_data = self.device.device_data

        for source in (adapter, device_data):
            heat = source.get("spHeat")
            cool = source.get("spCool")
            if heat is not None and cool is not None:
                return heat, cool

        heat = adapter.get("spHeat")
        cool = adapter.get("spCool")
        return (
            device_data.get("spHeat") if heat is None else heat,
            device_data.get("spCool") if cool is None else cool,
        )

    def _validate_auto_setpoints(self, heat: Any, cool: Any) -> None:
        """Validate a normalized Auto band without changing user targets."""
        normalized = normalize_cloud_commands({"spHeat": heat, "spCool": cool})
        normalized_heat = normalized.get("spHeat")
        normalized_cool = normalized.get("spCool")

        if (
            isinstance(normalized_heat, bool)
            or isinstance(normalized_cool, bool)
            or not isinstance(normalized_heat, (int, float))
            or not isinstance(normalized_cool, (int, float))
            or not isfinite(normalized_heat)
            or not isfinite(normalized_cool)
        ):
            raise HomeAssistantError("Auto mode requires both heat and cool setpoints")
        if normalized_heat >= normalized_cool:
            raise HomeAssistantError(
                "Auto heat setpoint must be lower than the cool setpoint"
            )

        profile = self.device.profile_data
        if not profile:
            return

        profile_data = profile[0] if isinstance(profile, list) else profile
        minimums = profile_data.get("minimumSetPoints", {})
        maximums = profile_data.get("maximumSetPoints", {})
        violations = []

        heat_min = minimums.get("heat")
        heat_max = maximums.get("heat")
        cool_min = minimums.get("cool")
        cool_max = maximums.get("cool")

        if heat_min is not None and normalized_heat < heat_min:
            violations.append(f"heat {normalized_heat}°C is below {heat_min}°C")
        if heat_max is not None and normalized_heat > heat_max:
            violations.append(f"heat {normalized_heat}°C is above {heat_max}°C")
        if cool_min is not None and normalized_cool < cool_min:
            violations.append(f"cool {normalized_cool}°C is below {cool_min}°C")
        if cool_max is not None and normalized_cool > cool_max:
            violations.append(f"cool {normalized_cool}°C is above {cool_max}°C")

        if violations:
            raise HomeAssistantError(
                f"Auto setpoint is outside the device profile: {', '.join(violations)}"
            )

    async def _send_auto_command_or_turn_off(self, heat: Any, cool: Any) -> None:
        """Send Auto mode and its complete band, turning off on any failure."""
        commands = {
            "operationMode": OPERATION_MODE_AUTO,
            "spHeat": heat,
            "spCool": cool,
        }

        try:
            self._validate_auto_setpoints(heat, cool)
            await self._send_command_and_refresh(commands)
        except Exception:
            _LOGGER.exception(
                "Auto command failed for device %s; turning it off as a fail-safe",
                self.device.device_serial,
            )
            try:
                await self._send_command_and_refresh(
                    {"operationMode": OPERATION_MODE_OFF}
                )
            except Exception:
                _LOGGER.exception(
                    "Fail-safe turn-off also failed for device %s",
                    self.device.device_serial,
                )
            raise

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target HVAC mode."""
        _log_temp_context(
            self.device,
            "async_set_hvac_mode ENTRY",
            {"requested_hvac_mode": str(hvac_mode)},
        )
        if hvac_mode == HVACMode.OFF:
            await self._send_command_and_refresh({"operationMode": OPERATION_MODE_OFF})
        elif hvac_mode == HVACMode.HEAT_COOL:
            heat, cool = self._current_auto_setpoints()
            await self._send_auto_command_or_turn_off(heat, cool)
        else:
            kumo_mode = HVAC_TO_KUMO_MODE.get(hvac_mode)
            if kumo_mode:
                commands = {"operationMode": kumo_mode}
                _LOGGER.info(
                    "[TEMPTRACE] async_set_hvac_mode built commands zone=%s serial=%s commands=%s",
                    self.device.zone_id,
                    self.device.device_serial,
                    commands,
                )
                await self._send_command_and_refresh(commands)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        adapter = self.device.zone_data.get("adapter", {})
        device_data = self.device.device_data
        commands: dict[str, Any] = {}

        _log_temp_context(self.device, "async_set_temperature ENTRY", {"kwargs": kwargs})

        # Requested values arrive in the entity's unit; work in cloud Celsius.
        # Range set (preferred for HEAT_COOL / auto)
        low = self._from_ha(kwargs.get(ATTR_TARGET_TEMP_LOW))
        high = self._from_ha(kwargs.get(ATTR_TARGET_TEMP_HIGH))

        if low is not None or high is not None:
            current_low, current_high = self._current_auto_setpoints()

            if low is None:
                low = current_low
            if high is None:
                high = current_high

            _LOGGER.info(
                "[TEMPTRACE] async_set_temperature RANGE zone=%s serial=%s "
                "requested_low=%s requested_high=%s current_low=%s current_high=%s commands=%s",
                self.device.zone_id,
                self.device.device_serial,
                kwargs.get(ATTR_TARGET_TEMP_LOW),
                kwargs.get(ATTR_TARGET_TEMP_HIGH),
                current_low,
                current_high,
                commands,
            )

            await self._send_auto_command_or_turn_off(low, high)
            return

        target_temp = self._from_ha(kwargs.get(ATTR_TEMPERATURE))
        if target_temp is None:
            _LOGGER.info(
                "[TEMPTRACE] async_set_temperature EXIT no target temp kwargs=%s", kwargs
            )
            return

        hvac_mode = self.hvac_mode

        if hvac_mode == HVACMode.COOL:
            commands["spCool"] = target_temp
            sp_heat = device_data.get("spHeat", adapter.get("spHeat"))
            if sp_heat is not None:
                commands["spHeat"] = sp_heat

        elif hvac_mode == HVACMode.HEAT:
            commands["spHeat"] = target_temp
            sp_cool = device_data.get("spCool", adapter.get("spCool"))
            if sp_cool is not None:
                commands["spCool"] = sp_cool

        elif hvac_mode == HVACMode.HEAT_COOL:
            await self._send_auto_command_or_turn_off(target_temp - 2, target_temp)
            return

        _LOGGER.info(
            "[TEMPTRACE] async_set_temperature SINGLE zone=%s serial=%s "
            "hvac_mode=%s target_temp=%s commands=%s",
            self.device.zone_id,
            self.device.device_serial,
            hvac_mode,
            target_temp,
            commands,
        )

        if commands:
            await self._send_command_and_refresh(commands)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode."""
        await self._send_command_and_refresh({"fanSpeed": fan_mode})

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set new target swing mode."""
        await self._send_command_and_refresh({"airDirection": swing_mode})

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        # Turn on with the last used mode, or cool mode if no previous mode
        adapter = self.device.zone_data.get("adapter", {})
        device_data = self.device.device_data

        # Use device data if available, otherwise adapter data
        operation_mode = device_data.get(
            "operationMode", adapter.get("operationMode", OPERATION_MODE_COOL)
        )

        # If the operation mode is "off", default to cool
        if operation_mode == OPERATION_MODE_OFF:
            operation_mode = OPERATION_MODE_COOL

        if operation_mode in REPORTED_AUTO_MODES:
            heat, cool = self._current_auto_setpoints()
            await self._send_auto_command_or_turn_off(heat, cool)
        else:
            await self._send_command_and_refresh({"operationMode": operation_mode})

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self._send_command_and_refresh({"operationMode": OPERATION_MODE_OFF})
