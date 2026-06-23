"""The SolarEdge EV Charger integration."""
from __future__ import annotations

import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_SITE_ID,
    CONF_COOKIE,
    CONF_COOKIE_OBTAINED_AT,
    CONF_MONITORING_REFRESH_COOKIE,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    CONF_DEVICE_ID,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
)
from .coordinator import SolarEdgeEVChargerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SolarEdge EV Charger from a config entry."""
    coordinator = SolarEdgeEVChargerCoordinator(
        hass,
        config_entry=entry,
        site_id=entry.data[CONF_SITE_ID],
        email=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        cookie=entry.data.get(CONF_COOKIE, ""),
        refresh_cookie=entry.data.get(CONF_MONITORING_REFRESH_COOKIE, ""),
        cookie_obtained_at=entry.data.get(CONF_COOKIE_OBTAINED_AT, time.time()),
        device_id=entry.data.get(CONF_DEVICE_ID),
        scan_interval=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        timeout=entry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: SolarEdgeEVChargerCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
