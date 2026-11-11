"""Tests for KumoCloudDevice command handling."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


INTEGRATION_DIR = Path(__file__).parents[1] / "custom_components" / "kumo_cloud"


def _install_module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_package():
    """Load the integration package with stand-ins for Home Assistant."""
    package_name = "_test_kumo_device"

    class DataUpdateCoordinator:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __class_getitem__(cls, item):
            return cls

    _install_module("homeassistant")
    _install_module("homeassistant.config_entries", ConfigEntry=object)
    _install_module(
        "homeassistant.const",
        CONF_PASSWORD="password",
        CONF_USERNAME="username",
        Platform=types.SimpleNamespace(CLIMATE="climate"),
    )
    _install_module("homeassistant.core", HomeAssistant=object)
    _install_module(
        "homeassistant.exceptions",
        ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}),
        ConfigEntryNotReady=type("ConfigEntryNotReady", (Exception,), {}),
    )
    _install_module("homeassistant.helpers")
    _install_module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=type("UpdateFailed", (Exception,), {}),
    )

    # The API client is replaced by a fake below; only its names are imported.
    api = types.ModuleType(f"{package_name}.api")
    api.KumoCloudAPI = object
    api.KumoCloudAuthError = type("KumoCloudAuthError", (Exception,), {})
    api.KumoCloudConnectionError = type("KumoCloudConnectionError", (Exception,), {})
    sys.modules[f"{package_name}.api"] = api

    for module_name in ("const", "cloud_commands"):
        qualified = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(
            qualified, INTEGRATION_DIR / f"{module_name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)

    spec = importlib.util.spec_from_file_location(
        package_name,
        INTEGRATION_DIR / "__init__.py",
        submodule_search_locations=[str(INTEGRATION_DIR)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    assert spec.loader is not None
    spec.loader.exec_module(package)
    return package


integration = _load_package()


class FakeApi:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent = []
        # What the cloud reports on the next read; starts as the device state.
        self.cloud = {
            "operationMode": "off",
            "power": 0,
            "spHeat": 20.0,
            "spCool": 25.0,
        }

    async def send_command(self, device_serial, commands):
        if self.fail:
            raise RuntimeError("HTTP error: 400")
        self.sent.append((device_serial, commands))
        return {}

    async def get_device_details(self, device_serial):
        return dict(self.cloud)


class FakeCoordinator(integration.KumoCloudDataUpdateCoordinator):
    def __init__(self, api: FakeApi) -> None:
        super().__init__(None, api, "site")
        self.zones = [
            {
                "id": "zone",
                "adapter": {
                    "deviceSerial": "serial",
                    "operationMode": "off",
                    "power": 0,
                    "spHeat": 20.0,
                    "spCool": 25.0,
                },
            }
        ]
        self.devices = {
            "serial": {
                "operationMode": "off",
                "power": 0,
                "spHeat": 20.0,
                "spCool": 25.0,
            }
        }
        self.device_profiles = {}
        self.refreshes = []

    def async_update_listeners(self):
        pass

    async def async_refresh_device(self, device_serial):
        self.refreshes.append(device_serial)
        await super().async_refresh_device(device_serial)


class DeviceCommandTests(unittest.TestCase):
    def _device(self, fail: bool = False):
        coordinator = FakeCoordinator(FakeApi(fail=fail))
        return integration.KumoCloudDevice(coordinator, "zone", "serial"), coordinator

    def _send(self, device, commands, on_accepted=None):
        with mock.patch.object(integration.asyncio, "sleep", new=mock.AsyncMock()):
            asyncio.run(device.send_command(commands, on_accepted=on_accepted))

    def test_accepted_values_are_cached_before_the_callback(self) -> None:
        device, coordinator = self._device()
        seen = []

        def on_accepted():
            seen.append(
                (
                    dict(coordinator.devices["serial"]),
                    list(coordinator.refreshes),
                )
            )

        self._send(
            device,
            {"operationMode": "heat", "spHeat": 22.0, "spCool": 25.0},
            on_accepted,
        )

        self.assertEqual(len(seen), 1)
        cached, refreshes_at_callback = seen[0]
        self.assertEqual(cached["operationMode"], "heat")
        self.assertEqual(cached["power"], 1)
        self.assertEqual(cached["spHeat"], 22.0)
        # The callback runs before the delayed refresh.
        self.assertEqual(refreshes_at_callback, [])
        self.assertEqual(coordinator.refreshes, ["serial"])
        self.assertEqual(coordinator.zones[0]["adapter"]["spHeat"], 22.0)

    def test_cached_setpoint_is_the_normalized_value(self) -> None:
        device, coordinator = self._device()

        self._send(device, {"spHeat": (71 - 32) * 5 / 9})

        self.assertEqual(coordinator.devices["serial"]["spHeat"], 22.0)

    def test_off_clears_power(self) -> None:
        device, coordinator = self._device()
        coordinator.devices["serial"].update({"operationMode": "heat", "power": 1})

        self._send(device, {"operationMode": "off"})

        self.assertEqual(coordinator.devices["serial"]["power"], 0)
        self.assertEqual(coordinator.zones[0]["adapter"]["power"], 0)

    def test_lagging_cloud_read_keeps_the_accepted_value(self) -> None:
        device, coordinator = self._device()
        coordinator.devices["serial"].update({"operationMode": "heat", "power": 1})
        coordinator.api.cloud.update({"operationMode": "heat", "power": 1})

        # The cloud still reports 20.0 at the post-command refresh.
        self._send(device, {"spHeat": 22.0})

        self.assertEqual(coordinator.devices["serial"]["spHeat"], 22.0)
        self.assertEqual(coordinator.zones[0]["adapter"]["spHeat"], 22.0)

    def test_pending_value_clears_once_the_cloud_reports_it(self) -> None:
        device, coordinator = self._device()
        self._send(device, {"spHeat": 22.0})
        self.assertIn("serial", coordinator._pending)

        coordinator.api.cloud["spHeat"] = 22.0
        asyncio.run(coordinator.async_refresh_device("serial"))
        self.assertNotIn("serial", coordinator._pending)

        # Later changes made outside HA show up straight away.
        coordinator.api.cloud["spHeat"] = 19.0
        asyncio.run(coordinator.async_refresh_device("serial"))
        self.assertEqual(coordinator.devices["serial"]["spHeat"], 19.0)

    def test_pending_value_expires(self) -> None:
        device, coordinator = self._device()
        self._send(device, {"spHeat": 22.0})

        later = integration.time.monotonic() + integration.PENDING_COMMAND_TIMEOUT + 1
        with mock.patch.object(integration.time, "monotonic", return_value=later):
            asyncio.run(coordinator.async_refresh_device("serial"))

        self.assertEqual(coordinator.devices["serial"]["spHeat"], 20.0)
        self.assertNotIn("serial", coordinator._pending)

    def test_rejected_command_changes_nothing(self) -> None:
        device, coordinator = self._device(fail=True)
        before = dict(coordinator.devices["serial"])
        called = []

        with self.assertRaises(RuntimeError):
            self._send(device, {"spHeat": 22.0}, lambda: called.append(True))

        self.assertEqual(called, [])
        self.assertEqual(coordinator.devices["serial"], before)
        self.assertEqual(coordinator.refreshes, [])


if __name__ == "__main__":
    unittest.main()
