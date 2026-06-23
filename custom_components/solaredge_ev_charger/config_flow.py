"""Config flow for SolarEdge EV Charger integration."""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
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
    DEFAULT_NAME,
    API_BASE_URL,
    API_DEVICES_ENDPOINT,
    COOKIE_NAME,
    ERROR_AUTH_FAILED,
    ERROR_CANNOT_CONNECT,
    ERROR_INVALID_SITE,
)
from .coordinator import (
    InvalidAuth,
    CannotConnect,
    async_login,
    async_get_sites,
)

_LOGGER = logging.getLogger(__name__)


async def _validate_site(
    cookie: str, site_id: str, timeout: int
) -> dict[str, Any]:
    """Verify that the site has an EV charger and return its device_id and name."""
    url = f"{API_BASE_URL}{API_DEVICES_ENDPOINT.format(site_id=site_id)}"
    headers = {
        "Cookie": f"{COOKIE_NAME}={cookie}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    raise Exception(ERROR_AUTH_FAILED)
                data = await resp.json(content_type=None)

                if "devicesByType" not in data or "EV_CHARGER" not in data["devicesByType"]:
                    raise Exception(ERROR_INVALID_SITE)

                ev_chargers = data["devicesByType"]["EV_CHARGER"]
                if not ev_chargers:
                    raise Exception(ERROR_INVALID_SITE)

                device_id = str(ev_chargers[0].get("reporterId", ""))
                if not device_id:
                    raise Exception(ERROR_INVALID_SITE)

                charger_name = ev_chargers[0].get("name", DEFAULT_NAME)
                return {"title": charger_name, CONF_DEVICE_ID: device_id}

    except aiohttp.ClientError as err:
        raise Exception(ERROR_CANNOT_CONNECT) from err


class SolarEdgeEVChargerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SolarEdge EV Charger."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._cookie: str = ""
        self._refresh_cookie: str = ""
        self._cookie_obtained_at: float = 0.0
        self._credentials: dict[str, Any] = {}
        self._sites: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: collect email, password, and optional settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            timeout = user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

            try:
                # Use one session for both login and site discovery so all
                # monitoring cookies (se_monitoring_auth, SolarEdge_SSO, etc.)
                # are in the jar and sent automatically to the sites endpoint.
                async with aiohttp.ClientSession() as session:
                    spring_cookie, refresh_cookie = await async_login(
                        session, email, password, timeout
                    )

                    self._cookie = spring_cookie
                    self._refresh_cookie = refresh_cookie
                    self._cookie_obtained_at = time.time()
                    self._credentials = user_input

                    # Discover sites using the same session (full cookie jar)
                    sites = await async_get_sites(session, timeout)

                if len(sites) == 1:
                    # Single site — proceed directly
                    return await self._async_finish(sites[0]["id"], sites[0]["name"])

                if len(sites) > 1:
                    self._sites = sites
                    return await self.async_step_site()

                # No sites from API — show manual site ID entry
                return await self.async_step_site()

            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                    vol.Optional(
                        CONF_TIMEOUT, default=DEFAULT_TIMEOUT
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=120)),
                }
            ),
            errors=errors,
        )

    async def async_step_site(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: select or enter a site ID."""
        errors: dict[str, str] = {}

        if user_input is not None:
            site_id = str(user_input[CONF_SITE_ID]).strip()
            site_name = next(
                (s["name"] for s in self._sites if s["id"] == site_id),
                f"Site {site_id}",
            )
            return await self._async_finish(site_id, site_name)

        if self._sites:
            # Build a selector from discovered sites
            site_options = {s["id"]: f"{s['name']} ({s['id']})" for s in self._sites}
            schema = vol.Schema(
                {vol.Required(CONF_SITE_ID): vol.In(site_options)}
            )
        else:
            # Fallback: free-text site ID entry
            schema = vol.Schema({vol.Required(CONF_SITE_ID): str})

        return self.async_show_form(
            step_id="site",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "site_id_help": "Find your Site ID in the SolarEdge monitoring URL: …/site/XXXXXXX/…"
            },
        )

    async def _async_finish(self, site_id: str, site_name: str) -> FlowResult:
        """Validate the site and create the config entry."""
        errors: dict[str, str] = {}
        timeout = self._credentials.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

        try:
            info = await _validate_site(self._cookie, site_id, timeout)
        except Exception as err:
            error_msg = str(err)
            if ERROR_AUTH_FAILED in error_msg:
                errors["base"] = "invalid_auth"
            elif ERROR_CANNOT_CONNECT in error_msg:
                errors["base"] = "cannot_connect"
            elif ERROR_INVALID_SITE in error_msg:
                errors["base"] = "invalid_site"
            else:
                errors["base"] = "unknown"
            _LOGGER.error("Error validating site %s: %s", site_id, error_msg)

            # Re-show site step with the error
            if self._sites:
                site_options = {s["id"]: f"{s['name']} ({s['id']})" for s in self._sites}
                schema = vol.Schema({vol.Required(CONF_SITE_ID): vol.In(site_options)})
            else:
                schema = vol.Schema({vol.Required(CONF_SITE_ID): str})
            return self.async_show_form(
                step_id="site", data_schema=schema, errors=errors
            )

        unique_id = f"{site_id}_{info[CONF_DEVICE_ID]}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        entry_data = {
            CONF_USERNAME: self._credentials[CONF_USERNAME],
            CONF_PASSWORD: self._credentials[CONF_PASSWORD],
            CONF_SITE_ID: site_id,
            CONF_DEVICE_ID: info[CONF_DEVICE_ID],
            CONF_COOKIE: self._cookie,
            CONF_MONITORING_REFRESH_COOKIE: self._refresh_cookie,
            CONF_COOKIE_OBTAINED_AT: self._cookie_obtained_at,
            CONF_SCAN_INTERVAL: self._credentials.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            CONF_TIMEOUT: timeout,
        }

        return self.async_create_entry(
            title=info.get("title", site_name),
            data=entry_data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SolarEdgeEVChargerOptionsFlow:
        """Get the options flow."""
        return SolarEdgeEVChargerOptionsFlow(config_entry)


class SolarEdgeEVChargerOptionsFlow(config_entries.OptionsFlow):
    """Handle options for SolarEdge EV Charger."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialise."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow updating credentials and polling settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            new_data = dict(self.config_entry.data)
            new_password = user_input.get(CONF_PASSWORD, "").strip()
            email = user_input.get(CONF_USERNAME, new_data[CONF_USERNAME]).strip()
            timeout = user_input.get(CONF_TIMEOUT, new_data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT))

            # If credentials changed, re-authenticate to get a fresh cookie
            credentials_changed = (
                email != new_data[CONF_USERNAME] or bool(new_password)
            )
            if credentials_changed:
                password = new_password or new_data[CONF_PASSWORD]
                try:
                    async with aiohttp.ClientSession() as session:
                        spring, refresh = await async_login(session, email, password, timeout)
                    new_data[CONF_USERNAME] = email
                    new_data[CONF_PASSWORD] = password
                    new_data[CONF_COOKIE] = spring
                    new_data[CONF_MONITORING_REFRESH_COOKIE] = refresh
                    new_data[CONF_COOKIE_OBTAINED_AT] = time.time()
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error re-authenticating")
                    errors["base"] = "unknown"

            if not errors:
                new_data[CONF_SCAN_INTERVAL] = user_input.get(
                    CONF_SCAN_INTERVAL, new_data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                )
                new_data[CONF_TIMEOUT] = timeout
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )
                return self.async_create_entry(title="", data={})

        current = self.config_entry.data
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_USERNAME,
                        description={"suggested_value": current.get(CONF_USERNAME, "")},
                    ): str,
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=300)),
                    vol.Optional(
                        CONF_TIMEOUT,
                        default=current.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
                    ): vol.All(vol.Coerce(int), vol.Range(min=10, max=120)),
                }
            ),
            errors=errors,
            description_placeholders={
                "password_help": "Leave blank to keep existing password"
            },
        )
