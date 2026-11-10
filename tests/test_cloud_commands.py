"""Focused tests for Kumo Cloud Auto command handling."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


def _load_cloud_commands_module():
    """Load the dependency-free command helpers without Home Assistant."""
    integration_dir = (
        Path(__file__).parents[1] / "custom_components" / "kumo_cloud"
    )
    package_name = "_test_kumo_cloud"
    package = types.ModuleType(package_name)
    package.__path__ = [str(integration_dir)]
    sys.modules[package_name] = package

    for module_name in ("const", "cloud_commands"):
        qualified_name = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(
            qualified_name, integration_dir / f"{module_name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)

    return sys.modules[f"{package_name}.cloud_commands"]


cloud_commands = _load_cloud_commands_module()


def _load_api_module():
    """Load the API client with small stand-ins for optional runtime deps."""
    integration_dir = (
        Path(__file__).parents[1] / "custom_components" / "kumo_cloud"
    )
    package_name = "_test_kumo_api"
    package = types.ModuleType(package_name)
    package.__path__ = [str(integration_dir)]
    sys.modules[package_name] = package

    class ClientResponseError(Exception):
        status = 500

    class ClientTimeout:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = Exception
    aiohttp.ClientResponse = object
    aiohttp.ClientResponseError = ClientResponseError
    aiohttp.ClientSession = object
    aiohttp.ClientTimeout = ClientTimeout
    sys.modules["aiohttp"] = aiohttp

    homeassistant = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: None
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    exceptions.HomeAssistantError = HomeAssistantError
    homeassistant.helpers = helpers
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    helpers.aiohttp_client = aiohttp_client
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.exceptions"] = exceptions

    for module_name in ("const", "cloud_commands", "api"):
        qualified_name = f"{package_name}.{module_name}"
        spec = importlib.util.spec_from_file_location(
            qualified_name, integration_dir / f"{module_name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified_name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)

    return sys.modules[f"{package_name}.api"]


api_module = _load_api_module()


class CloudCommandTests(unittest.TestCase):
    """Verify cloud-only normalization and incoming Auto variants."""

    def test_reported_auto_modes_remain_recognized(self) -> None:
        self.assertEqual(
            cloud_commands.REPORTED_AUTO_MODES,
            frozenset({"auto", "autoCool", "autoHeat"}),
        )

    def test_reported_auto_variants_are_normalized_for_cloud_writes(self) -> None:
        for mode in ("autoCool", "autoHeat"):
            with self.subTest(mode=mode):
                source = {"operationMode": mode, "spHeat": 14.444445}
                normalized = cloud_commands.normalize_cloud_commands(source)

                self.assertEqual(normalized["operationMode"], "auto")
                self.assertEqual(normalized["spHeat"], 14.5)
                self.assertEqual(source["operationMode"], mode)
                self.assertEqual(source["spHeat"], 14.444445)

    def test_other_commands_are_not_changed(self) -> None:
        source = {"operationMode": "heat", "spHeat": 16, "spCool": 26.5}
        self.assertEqual(cloud_commands.normalize_cloud_commands(source), source)

    def test_fahrenheit_conversions_use_mitsubishi_setpoint_table(self) -> None:
        source = {
            "spHeat": 15.555555555555555,
            "spCool": 23.88888888888889,
        }

        self.assertEqual(
            cloud_commands.normalize_cloud_commands(source),
            {"spHeat": 15.5, "spCool": 24.0},
        )
        self.assertEqual(source["spHeat"], 15.555555555555555)
        self.assertEqual(source["spCool"], 23.88888888888889)

    def test_nonstandard_fahrenheit_mappings_match_mitsubishi(self) -> None:
        commands = {
            "spHeat": (64 - 32) * 5 / 9,
            "spCool": (70 - 32) * 5 / 9,
        }

        self.assertEqual(
            cloud_commands.normalize_cloud_commands(commands),
            {"spHeat": 17.5, "spCool": 21.5},
        )

    def test_table_celsius_displays_as_mitsubishi_fahrenheit(self) -> None:
        for celsius, fahrenheit in (
            (20.0, 68.0),
            (21.0, 69.0),
            (21.5, 70.0),
            (22.0, 71.0),
            (22.5, 72.0),
            (16.0, 61.0),
            (26.5, 80.0),
        ):
            with self.subTest(celsius=celsius):
                self.assertEqual(
                    cloud_commands.celsius_to_display_fahrenheit(celsius),
                    fahrenheit,
                )

    def test_off_table_celsius_uses_plain_conversion(self) -> None:
        # 20.5 C and 19.0 C are not on the table; 10 C is below it.
        self.assertEqual(cloud_commands.celsius_to_display_fahrenheit(20.5), 68.9)
        self.assertEqual(cloud_commands.celsius_to_display_fahrenheit(19.0), 66.2)
        self.assertEqual(cloud_commands.celsius_to_display_fahrenheit(10), 50.0)
        self.assertEqual(
            cloud_commands.celsius_to_display_fahrenheit(21.04), 69.9
        )
        self.assertIsNone(cloud_commands.celsius_to_display_fahrenheit(None))

    def test_whole_fahrenheit_converts_through_table(self) -> None:
        for fahrenheit, celsius in ((68, 20.0), (69, 21.0), (71, 22.0), (69.0, 21.0)):
            with self.subTest(fahrenheit=fahrenheit):
                self.assertEqual(
                    cloud_commands.fahrenheit_to_cloud_celsius(fahrenheit), celsius
                )

    def test_fractional_or_off_table_fahrenheit_is_quantized_at_boundary(self) -> None:
        # 70.5 F -> 21.39 C -> 21.5 C at the boundary (shows as 70 F).
        sent = cloud_commands.normalize_cloud_commands(
            {"spHeat": cloud_commands.fahrenheit_to_cloud_celsius(70.5)}
        )
        self.assertEqual(sent, {"spHeat": 21.5})
        sent = cloud_commands.normalize_cloud_commands(
            {"spHeat": cloud_commands.fahrenheit_to_cloud_celsius(50)}
        )
        self.assertEqual(sent, {"spHeat": 10.0})

    def test_table_round_trips_unchanged_through_normalization(self) -> None:
        for fahrenheit in range(61, 81):
            with self.subTest(fahrenheit=fahrenheit):
                celsius = cloud_commands.fahrenheit_to_cloud_celsius(fahrenheit)
                normalized = cloud_commands.normalize_cloud_commands(
                    {"spHeat": celsius}
                )["spHeat"]
                self.assertEqual(normalized, celsius)
                self.assertEqual(
                    cloud_commands.celsius_to_display_fahrenheit(normalized),
                    float(fahrenheit),
                )

    def test_send_command_normalizes_at_cloud_boundary(self) -> None:
        api = object.__new__(api_module.KumoCloudAPI)
        captured = {}

        async def capture(method, endpoint, data):
            captured.update(method=method, endpoint=endpoint, data=data)
            return {"devices": ["serial"]}

        api._request = capture
        result = asyncio.run(
            api.send_command(
                "serial",
                {
                    "operationMode": "autoHeat",
                    "spHeat": 14.444445,
                    "spCool": 26.5,
                },
            )
        )

        self.assertEqual(result, {"devices": ["serial"]})
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["endpoint"], "/devices/send-command")
        self.assertEqual(
            captured["data"],
            {
                "deviceSerial": "serial",
                "commands": {
                    "operationMode": "auto",
                    "spHeat": 14.5,
                    "spCool": 26.5,
                },
            },
        )

    def test_http_error_includes_validation_body(self) -> None:
        api = object.__new__(api_module.KumoCloudAPI)

        class Response:
            status = 400

            async def text(self):
                return '{"commands":["invalidSpHeatRange"]}'

        with self.assertRaises(api_module.KumoCloudConnectionError) as raised:
            asyncio.run(
                api._raise_request_error(
                    Response(), "POST", "/devices/send-command"
                )
            )

        self.assertIn("HTTP error: 400", str(raised.exception))
        self.assertIn("invalidSpHeatRange", str(raised.exception))

    def test_validation_error_is_preserved(self) -> None:
        body = '{"commands":["invalidSpHeatRange"]}'
        self.assertEqual(
            cloud_commands.sanitize_error_body(body),
            '{"commands":["invalidSpHeatRange"]}',
        )

    def test_credentials_and_tokens_are_redacted(self) -> None:
        body = (
            '{"commands":["invalidOperationMode"],'
            '"access_token":"abc","password":"secret",'
            '"account":{"email":"person@example.com"}}'
        )
        detail = cloud_commands.sanitize_error_body(body)

        self.assertIn("invalidOperationMode", detail)
        self.assertNotIn("abc", detail)
        self.assertNotIn("secret", detail)
        self.assertNotIn("person@example.com", detail)
        self.assertEqual(detail.count("<redacted>"), 3)

    def test_token_in_non_sensitive_json_field_is_redacted(self) -> None:
        body = '{"message":"Bearer header.payload.signature"}'
        detail = cloud_commands.sanitize_error_body(body)

        self.assertNotIn("header.payload.signature", detail)
        self.assertIn("Bearer <redacted>", detail)


if __name__ == "__main__":
    unittest.main()
