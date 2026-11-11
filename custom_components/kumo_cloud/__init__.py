"""The Kumo Cloud integration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import KumoCloudAPI, KumoCloudAuthError, KumoCloudConnectionError
from .cloud_commands import normalize_cloud_commands
from .const import CONF_SITE_ID, DEFAULT_SCAN_INTERVAL, DOMAIN, OPERATION_MODE_OFF

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE]

# How long an accepted command's values win over cloud reads that don't show
# them yet. The cloud can lag well past the 10s post-command refresh.
PENDING_COMMAND_TIMEOUT = 90.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Kumo Cloud from a config entry."""

    # Create API client
    api = KumoCloudAPI(hass)

    # Initialize with stored tokens if available
    if "access_token" in entry.data:
        api.username = entry.data[CONF_USERNAME]
        api.access_token = entry.data["access_token"]
        api.refresh_token = entry.data["refresh_token"]

    try:
        # Try to login or refresh tokens
        if not api.access_token:
            await api.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
        else:
            # Verify the token works by making a test request
            try:
                await api.get_account_info()
            except KumoCloudAuthError:
                # Token expired, try to login again
                await api.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])

    except KumoCloudAuthError as err:
        raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
    except KumoCloudConnectionError as err:
        raise ConfigEntryNotReady(f"Unable to connect: {err}") from err

    # Create the coordinator
    coordinator = KumoCloudDataUpdateCoordinator(hass, api, entry.data[CONF_SITE_ID])

    # Fetch initial data so we have data when entities are added
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator in hass data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            coordinator = hass.data[DOMAIN][entry.entry_id]
            # Close the API session
            await coordinator.api.close()
            hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class KumoCloudDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Kumo Cloud data."""

    def __init__(self, hass: HomeAssistant, api: KumoCloudAPI, site_id: str) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api
        self.site_id = site_id
        self.zones: list[dict[str, Any]] = []
        self.devices: dict[str, dict[str, Any]] = {}
        self.device_profiles: dict[str, list[dict[str, Any]]] = {}
        # Track consecutive failures to avoid marking unavailable on transient errors
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5  # Only mark unavailable after 5 consecutive failures
        # serial -> {key: (value, expires_at)} for accepted commands the cloud
        # may not report yet.
        self._pending: dict[str, dict[str, tuple[Any, float]]] = {}

    def remember_accepted(self, device_serial: str, updates: dict[str, Any]) -> None:
        """Hold accepted command values until the cloud reports them."""
        expires = time.monotonic() + PENDING_COMMAND_TIMEOUT
        pending = self._pending.setdefault(device_serial, {})
        for key, value in updates.items():
            pending[key] = (value, expires)

    def _log_unrequested_mode_change(
        self,
        device_serial: str,
        detail: dict[str, Any],
        pending: dict[str, tuple[Any, float]],
    ) -> None:
        """Warn when the cloud reports a mode or power change HA did not send."""
        old = self.devices.get(device_serial)
        if not old or "operationMode" in pending or "power" in pending:
            return
        if (old.get("operationMode"), old.get("power")) == (
            detail.get("operationMode"),
            detail.get("power"),
        ):
            return
        _LOGGER.warning(
            "Kumo cloud reports %s changed outside HA: mode %s -> %s, power %s -> %s "
            "(updatedAt %s -> %s)",
            device_serial,
            old.get("operationMode"),
            detail.get("operationMode"),
            old.get("power"),
            detail.get("power"),
            old.get("updatedAt"),
            detail.get("updatedAt"),
        )

    def _overlay_pending(self, device_serial: str, detail: dict[str, Any]) -> dict[str, Any]:
        """Keep pending command values over a cloud read that lags behind.

        A pending value is dropped once the cloud reports it or it expires.
        """
        pending = self._pending.get(device_serial)
        self._log_unrequested_mode_change(device_serial, detail, pending or {})
        if not pending:
            return detail
        now = time.monotonic()
        for key in list(pending):
            value, expires = pending[key]
            if now >= expires or _same_value(detail.get(key), value):
                del pending[key]
            else:
                detail[key] = value
        if not pending:
            del self._pending[device_serial]
        return detail

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Kumo Cloud."""
        try:
            # Get zones for the site
            zones = await self.api.get_zones(self.site_id)

            # Get device details for each zone
            devices = {}
            device_profiles = {}

            for zone in zones:
                if "adapter" in zone and zone["adapter"]:
                    device_serial = zone["adapter"]["deviceSerial"]

                    # Get device details and profile in parallel
                    device_detail_task = self.api.get_device_details(device_serial)
                    device_profile_task = self.api.get_device_profile(device_serial)

                    device_detail, device_profile = await asyncio.gather(
                        device_detail_task, device_profile_task
                    )

                    devices[device_serial] = self._overlay_pending(
                        device_serial, device_detail
                    )
                    device_profiles[device_serial] = device_profile

            # Store the data for access by entities
            self.zones = zones
            self.devices = devices
            self.device_profiles = device_profiles

            # Reset failure counter on successful update
            self._consecutive_failures = 0

            return {
                "zones": zones,
                "devices": devices,
                "device_profiles": device_profiles,
            }

        except KumoCloudAuthError as err:
            # Try to refresh token once
            try:
                await self.api.refresh_access_token()
                # Retry the request
                return await self._async_update_data()
            except KumoCloudAuthError as refresh_err:
                self._consecutive_failures += 1
                # Authentication errors are critical - always raise
                if self._consecutive_failures >= self._max_consecutive_failures:
                    raise UpdateFailed(
                        f"Authentication failed: {refresh_err}"
                    ) from refresh_err
                # Return last known data for transient auth issues
                _LOGGER.warning(
                    "Authentication error (attempt %d/%d), using last known state",
                    self._consecutive_failures,
                    self._max_consecutive_failures,
                )
                return {
                    "zones": self.zones,
                    "devices": self.devices,
                    "device_profiles": self.device_profiles,
                }
        except KumoCloudConnectionError as err:
            self._consecutive_failures += 1
            error_msg = str(err).lower()
            # Check if this is a transient error (429, timeout) vs critical error
            is_transient = "429" in error_msg or "timeout" in error_msg or "rate limit" in error_msg

            if is_transient and self._consecutive_failures < self._max_consecutive_failures:
                # For transient errors, return last known data to keep entities available
                _LOGGER.warning(
                    "Transient API error (attempt %d/%d): %s. Using last known state.",
                    self._consecutive_failures,
                    self._max_consecutive_failures,
                    err,
                )
                return {
                    "zones": self.zones,
                    "devices": self.devices,
                    "device_profiles": self.device_profiles,
                }
            else:
                # Only raise UpdateFailed after multiple consecutive failures
                raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._max_consecutive_failures:
                raise UpdateFailed(f"Unexpected error: {err}") from err
            # Return last known data for transient errors
            _LOGGER.warning(
                "Unexpected error (attempt %d/%d): %s. Using last known state.",
                self._consecutive_failures,
                self._max_consecutive_failures,
                err,
            )
            return {
                "zones": self.zones,
                "devices": self.devices,
                "device_profiles": self.device_profiles,
            }

    async def async_refresh_device(self, device_serial: str) -> None:
        """Refresh a specific device's data immediately."""
        try:
            old_detail = dict(self.devices.get(device_serial, {}))

            device_detail = await self.api.get_device_details(device_serial)
            fresh = dict(device_detail)
            device_detail = self._overlay_pending(device_serial, device_detail)

            _LOGGER.info(
                "[TEMPTRACE] refresh_device serial=%s "
                "OLD(spHeat=%s spCool=%s roomTemp=%s power=%s mode=%s updatedAt=%s) "
                "NEW(spHeat=%s spCool=%s roomTemp=%s power=%s mode=%s updatedAt=%s) "
                "pending=%s",
                device_serial,
                old_detail.get("spHeat"),
                old_detail.get("spCool"),
                old_detail.get("roomTemp"),
                old_detail.get("power"),
                old_detail.get("operationMode"),
                old_detail.get("updatedAt"),
                fresh.get("spHeat"),
                fresh.get("spCool"),
                fresh.get("roomTemp"),
                fresh.get("power"),
                fresh.get("operationMode"),
                fresh.get("updatedAt"),
                self._pending.get(device_serial),
            )

            # Update the cached device data
            self.devices[device_serial] = device_detail

            # Also update the zone data if it contains the same info
            for zone in self.zones:
                if "adapter" in zone and zone["adapter"]:
                    if zone["adapter"]["deviceSerial"] == device_serial:
                        # Update adapter data with fresh device data
                        zone["adapter"].update(
                            {
                                "roomTemp": device_detail.get("roomTemp"),
                                "operationMode": device_detail.get("operationMode"),
                                "power": device_detail.get("power"),
                                "fanSpeed": device_detail.get("fanSpeed"),
                                "airDirection": device_detail.get("airDirection"),
                                "spCool": device_detail.get("spCool"),
                                "spHeat": device_detail.get("spHeat"),
                                "humidity": device_detail.get("humidity"),
                            }
                        )
                        break

            # Update the coordinator's data dict
            self.data = {
                "zones": self.zones,
                "devices": self.devices,
                "device_profiles": self.device_profiles,
            }

            # Notify all listeners that data has been updated
            self.async_update_listeners()

            _LOGGER.debug("Refreshed device %s data", device_serial)

        except Exception as err:
            _LOGGER.warning("Failed to refresh device %s: %s", device_serial, err)


class KumoCloudDevice:
    """Representation of a Kumo Cloud device."""

    def __init__(
        self,
        coordinator: KumoCloudDataUpdateCoordinator,
        zone_id: str,
        device_serial: str,
    ) -> None:
        """Initialize the device."""
        self.coordinator = coordinator
        self.zone_id = zone_id
        self.device_serial = device_serial
        self._zone_data: dict[str, Any] | None = None
        self._device_data: dict[str, Any] | None = None
        self._profile_data: list[dict[str, Any]] | None = None

    @property
    def zone_data(self) -> dict[str, Any]:
        """Get the zone data."""
        # Always get fresh data from coordinator
        for zone in self.coordinator.zones:
            if zone["id"] == self.zone_id:
                return zone
        return {}

    @property
    def device_data(self) -> dict[str, Any]:
        """Get the device data."""
        # Always get fresh data from coordinator
        return self.coordinator.devices.get(self.device_serial, {})

    @property
    def profile_data(self) -> list[dict[str, Any]]:
        """Get the device profile data."""
        # Always get fresh data from coordinator
        return self.coordinator.device_profiles.get(self.device_serial, [])

    @property
    def available(self) -> bool:
        """Return True if device is available."""
        adapter = self.zone_data.get("adapter", {})
        device_data = self.device_data

        # Check both adapter and device data for connection status
        adapter_connected = adapter.get("connected", False)
        device_connected = device_data.get("connected", adapter_connected)

        return device_connected

    @property
    def name(self) -> str:
        """Return the name of the device."""
        return self.zone_data.get("name", f"Zone {self.zone_id}")

    @property
    def unique_id(self) -> str:
        """Return a unique ID for the device."""
        return f"{self.device_serial}_{self.zone_id}"

    def _apply_accepted_command(self, commands: dict[str, Any]) -> None:
        """Cache the values of a command the cloud accepted.

        The cloud reports the new state only on the next refresh. Caching the
        accepted values lets the entity show them straight away; the refresh
        that follows overwrites them with whatever the device reports.
        """
        accepted = normalize_cloud_commands(commands)
        updates = {
            key: accepted[key]
            for key in ("operationMode", "spHeat", "spCool", "fanSpeed", "airDirection")
            if key in accepted
        }
        if "operationMode" in updates:
            updates["power"] = 0 if updates["operationMode"] == OPERATION_MODE_OFF else 1
        if not updates:
            return

        self.coordinator.remember_accepted(self.device_serial, updates)
        device_data = self.coordinator.devices.get(self.device_serial)
        if device_data is not None:
            device_data.update(updates)
        adapter = self.zone_data.get("adapter")
        if adapter:
            adapter.update(updates)

    async def send_command(
        self,
        commands: dict[str, Any],
        on_accepted: Callable[[], None] | None = None,
    ) -> None:
        """Send a command to the device and refresh status.

        on_accepted runs right after the cloud accepts the command, once the
        accepted values are cached, so the caller can write state while its
        service call context is still current.
        """
        try:
            # Send the command
            await self.coordinator.api.send_command(self.device_serial, commands)
            _LOGGER.info(
                "[TEMPTRACE] api.send_command serial=%s commands=%s",
                self.device_serial,
                commands,
            )

            self._apply_accepted_command(commands)
            if on_accepted is not None:
                on_accepted()

            # Wait a moment for the command to be processed
            await asyncio.sleep(10)

            # Refresh this specific device's data immediately
            await self.coordinator.async_refresh_device(self.device_serial)

        except Exception as err:
            _LOGGER.error(
                "Failed to send command to device %s: %s", self.device_serial, err
            )
            raise


def _same_value(reported: Any, expected: Any) -> bool:
    """Compare a cloud value with a commanded one (setpoints as floats)."""
    if isinstance(expected, (int, float)) and isinstance(reported, (int, float)):
        return abs(float(reported) - float(expected)) < 0.01
    return reported == expected
