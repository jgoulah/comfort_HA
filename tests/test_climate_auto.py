"""Tests for Home Assistant Auto-mode command and state paths."""

from __future__ import annotations

import asyncio
from enum import Enum, IntFlag
import importlib.util
from pathlib import Path
import sys
import types
import unittest


INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / "kumo_cloud"


class HVACMode(Enum):
    """Minimal stand-in for Home Assistant's HVACMode."""

    OFF = "off"
    COOL = "cool"
    HEAT = "heat"
    DRY = "dry"
    FAN_ONLY = "fan_only"
    HEAT_COOL = "heat_cool"


class HVACAction(Enum):
    """Minimal stand-in for Home Assistant's HVACAction."""

    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    DRYING = "drying"
    FAN = "fan"
    IDLE = "idle"


class ClimateEntityFeature(IntFlag):
    """Minimal feature flags used while constructing the entity."""

    TARGET_TEMPERATURE = 1
    TARGET_TEMPERATURE_RANGE = 2
    TURN_OFF = 4
    TURN_ON = 8
    FAN_MODE = 16
    SWING_MODE = 32


def _install_module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_climate_module():
    package_name = "_test_kumo_climate"
    package = types.ModuleType(package_name)
    package.__path__ = [str(INTEGRATION_DIR)]

    class CoordinatorEntity:
        def __init__(self, coordinator: object) -> None:
            self.coordinator = coordinator

        def async_write_ha_state(self) -> None:
            return None

    package.KumoCloudDataUpdateCoordinator = object
    package.KumoCloudDevice = object
    sys.modules[package_name] = package

    homeassistant = _install_module("homeassistant")
    components = _install_module("homeassistant.components")
    climate_package = _install_module(
        "homeassistant.components.climate",
        ClimateEntity=object,
        ClimateEntityFeature=ClimateEntityFeature,
        HVACAction=HVACAction,
        HVACMode=HVACMode,
    )
    climate_const = _install_module(
        "homeassistant.components.climate.const",
        ATTR_TARGET_TEMP_HIGH="target_temp_high",
        ATTR_TARGET_TEMP_LOW="target_temp_low",
    )
    config_entries = _install_module(
        "homeassistant.config_entries", ConfigEntry=object
    )
    ha_const = _install_module(
        "homeassistant.const",
        ATTR_TEMPERATURE="temperature",
        UnitOfTemperature=types.SimpleNamespace(CELSIUS="°C", FAHRENHEIT="°F"),
    )
    core = _install_module("homeassistant.core", HomeAssistant=object)
    exceptions = _install_module(
        "homeassistant.exceptions", HomeAssistantError=type(
            "HomeAssistantError", (Exception,), {}
        )
    )
    helpers = _install_module("homeassistant.helpers")
    device_registry = _install_module(
        "homeassistant.helpers.device_registry", DeviceInfo=dict
    )
    entity_platform = _install_module(
        "homeassistant.helpers.entity_platform",
        AddConfigEntryEntitiesCallback=object,
    )
    update_coordinator = _install_module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity,
    )

    homeassistant.components = components
    homeassistant.config_entries = config_entries
    homeassistant.const = ha_const
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    components.climate = climate_package
    climate_package.const = climate_const
    helpers.device_registry = device_registry
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator

    _load_module(f"{package_name}.const", INTEGRATION_DIR / "const.py")
    _load_module(
        f"{package_name}.cloud_commands",
        INTEGRATION_DIR / "cloud_commands.py",
    )
    return _load_module(
        f"{package_name}.climate", INTEGRATION_DIR / "climate.py"
    )


climate = _load_climate_module()
climate_commands = sys.modules["_test_kumo_climate.cloud_commands"]


class FakeDevice:
    """Small device fixture that supplies the entity's live properties."""

    def __init__(self, operation_mode: str = "off", power: int = 0) -> None:
        self.coordinator = object()
        self.unique_id = "serial-zone"
        self.zone_id = "zone"
        self.device_serial = "serial"
        self.zone_data = {
            "adapter": {
                "operationMode": operation_mode,
                "power": power,
                "spHeat": 14.444445,
                "spCool": 26.5,
            }
        }
        self.device_data = {
            "operationMode": operation_mode,
            "power": power,
            "spHeat": 14.444445,
            "spCool": 26.5,
        }
        self.profile_data = [
            {
                "hasModeHeat": True,
                "minimumSetPoints": {"heat": 10, "cool": 19, "auto": 19},
                "maximumSetPoints": {"heat": 28, "cool": 30, "auto": 28},
            }
        ]
        self.sent_commands = []

    async def send_command(self, commands, on_accepted=None) -> None:
        """Model the cloud API boundary used by KumoCloudDevice."""
        self.sent_commands.append(
            climate_commands.normalize_cloud_commands(commands)
        )
        if on_accepted is not None:
            on_accepted()


class FailingAutoDevice(FakeDevice):
    """Device that rejects Auto but accepts the fail-safe off command."""

    async def send_command(self, commands, on_accepted=None) -> None:
        normalized = climate_commands.normalize_cloud_commands(commands)
        self.sent_commands.append(normalized)
        if normalized.get("operationMode") == "auto":
            raise RuntimeError("simulated Auto command failure")
        if on_accepted is not None:
            on_accepted()


class ClimateAutoTests(unittest.TestCase):
    """Exercise all entity paths that can encounter an Auto mode."""

    def _capture_commands(self, entity):
        sent = []

        async def capture(commands):
            sent.append(commands)

        entity._send_command_and_refresh = capture
        return sent

    def test_heat_cool_mode_change_sends_canonical_auto(self) -> None:
        entity = climate.KumoCloudClimate(FakeDevice())
        sent = self._capture_commands(entity)

        asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT_COOL))

        self.assertEqual(
            sent,
            [
                {
                    "operationMode": "auto",
                    "spHeat": 14.444445,
                    "spCool": 26.5,
                }
            ],
        )

    def test_heat_and_cool_mode_changes_remain_mode_only(self) -> None:
        for hvac_mode, cloud_mode in (
            (HVACMode.HEAT, "heat"),
            (HVACMode.COOL, "cool"),
        ):
            with self.subTest(hvac_mode=hvac_mode):
                entity = climate.KumoCloudClimate(FakeDevice())
                sent = self._capture_commands(entity)

                asyncio.run(entity.async_set_hvac_mode(hvac_mode))

                self.assertEqual(sent, [{"operationMode": cloud_mode}])

    def test_all_reported_auto_states_map_to_heat_cool(self) -> None:
        for operation_mode in ("auto", "autoCool", "autoHeat"):
            with self.subTest(operation_mode=operation_mode):
                entity = climate.KumoCloudClimate(
                    FakeDevice(operation_mode=operation_mode, power=1)
                )

                self.assertEqual(entity.hvac_mode, HVACMode.HEAT_COOL)
                self.assertEqual(entity.hvac_action, HVACAction.IDLE)

    def test_reported_targets_prefer_fresher_adapter_band(self) -> None:
        device = FakeDevice(operation_mode="autoCool", power=1)
        device.zone_data["adapter"].update({"spHeat": 15.5, "spCool": 24.0})
        device.device_data.update({"spHeat": 15.5, "spCool": 18.0})
        entity = climate.KumoCloudClimate(device)

        self.assertEqual(entity.target_temperature_low, 15.5)
        self.assertEqual(entity.target_temperature_high, 24.0)

    def test_auto_range_preserves_requested_setpoints_until_cloud_boundary(self) -> None:
        entity = climate.KumoCloudClimate(
            FakeDevice(operation_mode="autoCool", power=1)
        )
        sent = self._capture_commands(entity)

        asyncio.run(
            entity.async_set_temperature(
                target_temp_low=14.444445,
                target_temp_high=26.5,
            )
        )

        self.assertEqual(
            sent,
            [
                {
                    "operationMode": "auto",
                    "spHeat": 14.444445,
                    "spCool": 26.5,
                }
            ],
        )

    def test_auto_range_is_quantized_at_cloud_boundary(self) -> None:
        device = FakeDevice(operation_mode="autoCool", power=1)
        entity = climate.KumoCloudClimate(device)

        asyncio.run(
            entity.async_set_temperature(
                target_temp_low=15.555555555555555,
                target_temp_high=23.88888888888889,
            )
        )

        self.assertEqual(
            device.sent_commands,
            [{"operationMode": "auto", "spHeat": 15.5, "spCool": 24.0}],
        )

    def test_turn_on_normalizes_cached_auto_state_at_cloud_boundary(self) -> None:
        device = FakeDevice(operation_mode="autoCool", power=0)
        entity = climate.KumoCloudClimate(device)

        asyncio.run(entity.async_turn_on())

        self.assertEqual(
            device.sent_commands,
            [{"operationMode": "auto", "spHeat": 14.5, "spCool": 26.5}],
        )

    def test_failed_auto_command_immediately_turns_unit_off(self) -> None:
        device = FailingAutoDevice()
        entity = climate.KumoCloudClimate(device)

        with self.assertRaises(RuntimeError):
            asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT_COOL))

        self.assertEqual(
            device.sent_commands,
            [
                {"operationMode": "auto", "spHeat": 14.5, "spCool": 26.5},
                {"operationMode": "off"},
            ],
        )

    def test_invalid_cached_auto_band_is_refused_and_unit_stays_off(self) -> None:
        device = FakeDevice()
        device.zone_data["adapter"].update({"spHeat": 15.5, "spCool": 18.0})
        entity = climate.KumoCloudClimate(device)

        with self.assertRaises(climate.HomeAssistantError):
            asyncio.run(entity.async_set_hvac_mode(HVACMode.HEAT_COOL))

        self.assertEqual(device.sent_commands, [{"operationMode": "off"}])


def _fahrenheit_hass():
    """Minimal hass stand-in configured for US customary units."""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            units=types.SimpleNamespace(temperature_unit="°F")
        )
    )


class ClimateFahrenheitTests(unittest.TestCase):
    """Fahrenheit display and input go through Mitsubishi's table."""

    def _entity(self, sp_heat=21.0, room=21.0):
        device = FakeDevice(operation_mode="heat", power=1)
        device.zone_data["adapter"].update({"spHeat": sp_heat, "roomTemp": room})
        device.device_data.update({"spHeat": sp_heat})
        entity = climate.KumoCloudClimate(device)
        entity.hass = _fahrenheit_hass()
        return entity, device

    def test_reports_mitsubishi_fahrenheit(self) -> None:
        entity, _ = self._entity(sp_heat=21.0, room=22.0)

        self.assertEqual(entity.temperature_unit, "°F")
        self.assertEqual(entity.target_temperature, 69.0)
        self.assertEqual(entity.current_temperature, 71.0)
        self.assertEqual(entity.target_temperature_step, 1.0)
        self.assertEqual(entity.min_temp, 50.0)  # 10 C heat minimum
        self.assertEqual(entity.max_temp, 86.0)  # 30 C cool maximum

    def test_fahrenheit_setpoint_is_sent_as_table_celsius(self) -> None:
        for fahrenheit, celsius in ((69, 21.0), (71, 22.0), (68, 20.0)):
            with self.subTest(fahrenheit=fahrenheit):
                entity, device = self._entity()

                asyncio.run(entity.async_set_temperature(temperature=fahrenheit))

                self.assertEqual(device.sent_commands[-1]["spHeat"], celsius)

    def test_fahrenheit_range_is_sent_as_table_celsius(self) -> None:
        device = FakeDevice(operation_mode="autoCool", power=1)
        entity = climate.KumoCloudClimate(device)
        entity.hass = _fahrenheit_hass()

        asyncio.run(
            entity.async_set_temperature(target_temp_low=69, target_temp_high=75)
        )

        self.assertEqual(
            device.sent_commands,
            [{"operationMode": "auto", "spHeat": 21.0, "spCool": 24.0}],
        )

    def test_celsius_installs_are_unchanged(self) -> None:
        entity, device = self._entity(sp_heat=21.0, room=22.0)
        entity.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(
                units=types.SimpleNamespace(temperature_unit="°C")
            )
        )

        self.assertEqual(entity.temperature_unit, "°C")
        self.assertEqual(entity.target_temperature, 21.0)
        self.assertEqual(entity.target_temperature_step, 0.5)
        asyncio.run(entity.async_set_temperature(temperature=21.5))
        self.assertEqual(device.sent_commands[-1]["spHeat"], 21.5)

    def test_state_is_written_when_the_cloud_accepts(self) -> None:
        entity, device = self._entity()
        writes = []
        entity.async_write_ha_state = lambda: writes.append(
            list(device.sent_commands)
        )

        asyncio.run(entity.async_set_temperature(temperature=71))

        # One write from inside send_command (context still attached), one
        # after the refresh completes.
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0], [device.sent_commands[0]])


if __name__ == "__main__":
    unittest.main()
