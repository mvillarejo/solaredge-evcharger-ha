"""Data update coordinator for SolarEdge EV Charger."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
import urllib.parse
from datetime import timedelta
from typing import Any

import aiohttp
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    API_BASE_URL,
    API_DEVICES_ENDPOINT,
    API_CONTROL_ENDPOINT,
    CONF_COOKIE,
    CONF_COOKIE_OBTAINED_AT,
    CONF_MONITORING_REFRESH_COOKIE,
    COOKIE_NAME,
    COOKIE_REFRESH_DAYS,
    COGNITO_DOMAIN,
    COGNITO_CLIENT_ID,
    COGNITO_REDIRECT_URI,
    API_AUTH_TOKEN_ENDPOINT,
    API_AUTH_REFRESH_ENDPOINT,
    MONITORING_REFRESH_COOKIE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; HomeAssistant)"


class InvalidAuth(Exception):
    """Raised when credentials are invalid."""


class CannotConnect(Exception):
    """Raised when the API is unreachable."""


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256 method."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _extract_hidden(html: str, name: str) -> str:
    """Return the value of a hidden input field, or ''."""
    m = re.search(
        rf'<input[^>]+name="{re.escape(name)}"[^>]+value="([^"]*)"',
        html, re.IGNORECASE,
    ) or re.search(
        rf'<input[^>]+value="([^"]*)"[^>]+name="{re.escape(name)}"',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else ""


def _extract_cookie(jar: Any, name: str) -> str:
    """Return a named cookie's value from an aiohttp CookieJar, or ''."""
    for morsel in jar:
        if morsel.key == name:
            return morsel.value
    return ""


async def async_login(
    session: aiohttp.ClientSession, email: str, password: str, timeout: int
) -> tuple[str, str]:
    """Authenticate with SolarEdge ONE via Cognito PKCE and return (spring_cookie, refresh_cookie).

    Flow:
      1. Generate PKCE pair
      2. GET Cognito hosted-UI login page (with code_challenge)
      3. POST credentials → capture auth code from redirect
      4. Exchange code for Cognito tokens
      5. POST tokens to /services/auth/token → get monitoring cookies
    """
    code_verifier, code_challenge = _pkce_pair()
    headers = {"User-Agent": _UA}

    cognito_login_url = (
        f"{COGNITO_DOMAIN}/login"
        f"?response_type=code"
        f"&client_id={COGNITO_CLIENT_ID}"
        f"&scope=email+openid"
        f"&redirect_uri={urllib.parse.quote(COGNITO_REDIRECT_URI, safe='')}"
        f"&code_challenge_method=S256"
        f"&code_challenge={code_challenge}"
    )

    try:
        async with async_timeout.timeout(timeout):
            # Step 1: fetch login page
            async with session.get(
                cognito_login_url, headers=headers, allow_redirects=True
            ) as page_resp:
                html = await page_resp.text()
                login_page_url = str(page_resp.url)
                _LOGGER.debug("Cognito login page: status=%s url=%s", page_resp.status, login_page_url)

            csrf = _extract_hidden(html, "csrf")
            _LOGGER.debug("Cognito csrf token present: %s", bool(csrf))

            # Step 2: POST credentials
            # Form action is the same URL path without the code_challenge query params;
            # Cognito keeps PKCE state server-side via the session cookie.
            form_action = re.search(r'<form[^>]+action="([^"]+)"', html, re.IGNORECASE)
            if form_action:
                raw_action = form_action.group(1).replace("&amp;", "&")
                post_url = (
                    raw_action if raw_action.startswith("http")
                    else f"{COGNITO_DOMAIN}{raw_action}"
                )
            else:
                post_url = login_page_url

            form_data = {
                "username": email,
                "password": password,
                "csrf": csrf,
                "cognitoAsfData": "",
            }
            async with session.post(
                post_url,
                data=form_data,
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=False,
            ) as cred_resp:
                location = cred_resp.headers.get("Location", "")
                _LOGGER.debug(
                    "Cognito credentials POST: status=%s location=%r",
                    cred_resp.status, location,
                )

                if cred_resp.status not in (301, 302):
                    raise InvalidAuth(f"Unexpected Cognito response: {cred_resp.status}")

                # Failure: redirect back to the login page
                if COGNITO_DOMAIN in location and "error" in location.lower():
                    raise InvalidAuth("Cognito rejected the credentials")
                if COGNITO_DOMAIN in location and "/login" in location:
                    raise InvalidAuth("Cognito redirected back to login — wrong email or password")

                # Success: redirect to our callback with ?code=...
                parsed = urllib.parse.urlparse(location)
                qs = urllib.parse.parse_qs(parsed.query)
                auth_code = qs.get("code", [None])[0]

            if not auth_code:
                raise InvalidAuth(f"No auth code in Cognito redirect: {location!r}")

            _LOGGER.debug("Got Cognito auth code (first 8 chars): %s…", auth_code[:8])

            # Step 3: exchange auth code for tokens
            async with session.post(
                f"{COGNITO_DOMAIN}/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": COGNITO_CLIENT_ID,
                    "redirect_uri": COGNITO_REDIRECT_URI,
                    "code": auth_code,
                    "code_verifier": code_verifier,
                },
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            ) as token_resp:
                _LOGGER.debug("Cognito token exchange: status=%s", token_resp.status)
                if token_resp.status != 200:
                    body = await token_resp.text()
                    raise InvalidAuth(f"Token exchange failed {token_resp.status}: {body[:200]}")
                tokens = await token_resp.json(content_type=None)

            _LOGGER.debug(
                "Cognito tokens received: token_type=%s expires_in=%s",
                tokens.get("token_type"), tokens.get("expires_in"),
            )

            # Step 4: exchange Cognito tokens for monitoring session cookies
            async with session.post(
                f"{API_BASE_URL}{API_AUTH_TOKEN_ENDPOINT}",
                json={
                    "id_token": tokens["id_token"],
                    "access_token": tokens["access_token"],
                    "refresh_token": tokens["refresh_token"],
                    "expires_in": tokens["expires_in"],
                    "token_type": tokens.get("token_type", "Bearer"),
                },
                headers={**headers, "Content-Type": "application/json"},
            ) as auth_resp:
                _LOGGER.debug(
                    "Monitoring /services/auth/token: status=%s set-cookie=%s",
                    auth_resp.status,
                    auth_resp.headers.getall("Set-Cookie", []),
                )
                if auth_resp.status not in (200, 204):
                    body = await auth_resp.text()
                    raise InvalidAuth(
                        f"/services/auth/token failed {auth_resp.status}: {body[:200]}"
                    )

            # Collect the monitoring cookies from the session jar
            spring_cookie = _extract_cookie(session.cookie_jar, COOKIE_NAME)
            refresh_cookie = _extract_cookie(session.cookie_jar, MONITORING_REFRESH_COOKIE)

            _LOGGER.debug(
                "Monitoring cookies: %s present=%s, %s present=%s",
                COOKIE_NAME, bool(spring_cookie),
                MONITORING_REFRESH_COOKIE, bool(refresh_cookie),
            )

            if not spring_cookie and not refresh_cookie:
                all_keys = [m.key for m in session.cookie_jar]
                raise InvalidAuth(
                    f"No monitoring cookies received. Got cookies: {all_keys}"
                )

            return spring_cookie, refresh_cookie

    except (aiohttp.ClientError, TimeoutError) as err:
        raise CannotConnect(f"Connection error during login: {err}") from err


async def async_refresh_session(
    session: aiohttp.ClientSession, refresh_cookie: str, timeout: int
) -> tuple[str, str] | None:
    """Refresh the monitoring session using the se_monitoring_refresh cookie.

    Returns (spring_cookie, new_refresh_cookie) on success, None on failure.
    """
    headers = {
        "User-Agent": _UA,
        "Content-Type": "application/json",
        "Cookie": f"{MONITORING_REFRESH_COOKIE}={refresh_cookie}",
    }
    try:
        async with async_timeout.timeout(timeout):
            async with session.post(
                f"{API_BASE_URL}{API_AUTH_REFRESH_ENDPOINT}",
                headers=headers,
            ) as resp:
                _LOGGER.debug(
                    "Session refresh: status=%s set-cookie=%s",
                    resp.status,
                    resp.headers.getall("Set-Cookie", []),
                )
                if resp.status not in (200, 204):
                    return None

        new_spring = _extract_cookie(session.cookie_jar, COOKIE_NAME)
        new_refresh = _extract_cookie(session.cookie_jar, MONITORING_REFRESH_COOKIE) or refresh_cookie
        return new_spring, new_refresh

    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Session refresh failed: %s", err)
        return None


_SEARCH_SITES_BODY = {
    "pageRequest": {
        "sitesInPage": 100,
        "pageNum": 1,
        "sortRequest": {"sortColumnType": "name", "sortOrder": "ASC"},
    },
    "locationFilter": {"countries": [], "states": [], "city": "", "address": "", "zip": ""},
    "peakPowerFilter": {"min": 0, "max": 8},
    "maxImpactFilter": {"min": 0, "max": 9},
    "installationDateFilter": {},
    "statusFilter": [],
    "serialNumber": "",
    "siteNameFilter": "",
    "accountNameFilter": [],
    "groupFilter": "",
    "favoriteFilter": False,
    "devicesFilter": {},
    "demoSitesFilter": False,
    "siteMagnitudeFilter": None,
    "geoBoundingBox": None,
}


async def async_get_sites(
    session: aiohttp.ClientSession, timeout: int
) -> list[dict[str, Any]]:
    """Return a list of sites for the authenticated user.

    Uses POST /services/sitelist/searchSites — auth is via session cookies
    (same jar populated by async_login), no Bearer header needed.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _UA,
        "Referer": "https://monitoring.solaredge.com/one",
        "Origin": "https://monitoring.solaredge.com",
    }
    url = f"{API_BASE_URL}/services/sitelist/searchSites?v={int(time.time() * 1000)}"
    to = aiohttp.ClientTimeout(total=timeout)

    try:
        async with session.post(url, json=_SEARCH_SITES_BODY, headers=headers, timeout=to) as resp:
            _LOGGER.debug("searchSites → status=%s", resp.status)
            if resp.status != 200:
                body = await resp.text()
                _LOGGER.debug("searchSites error body: %s", body[:500])
                return []
            data = await resp.json(content_type=None)
            _LOGGER.debug("searchSites response: %s", str(data)[:500])
            return _parse_sites_response(data)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Site discovery failed: %s", err)
        return []


def _parse_sites_response(data: Any) -> list[dict[str, Any]]:
    """Normalise searchSites response into [{id, name}].

    searchSites returns {"page": [...], "totalSitesInSearch": N} where each
    site has "solarFieldId" as the numeric site ID.
    """
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        # searchSites: {"page": [...]}
        raw = (
            data.get("page")
            or data.get("sites")
            or data.get("SiteDetails")
            or data.get("siteList")
            or []
        )
        if isinstance(raw, dict):
            raw = [raw]
    else:
        return []

    result = []
    for site in raw:
        if not isinstance(site, dict):
            continue
        site_id = str(
            site.get("solarFieldId")
            or site.get("fieldId")
            or site.get("id")
            or site.get("siteId")
            or ""
        )
        name = site.get("name") or site.get("siteName") or f"Site {site_id}"
        if site_id:
            result.append({"id": site_id, "name": name})
    return result


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class SolarEdgeEVChargerCoordinator(DataUpdateCoordinator):
    """Manage fetching SolarEdge EV Charger data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        site_id: str,
        email: str,
        password: str,
        cookie: str,
        refresh_cookie: str,
        cookie_obtained_at: float,
        device_id: str | None = None,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.site_id = site_id
        self.email = email
        self.password = password
        self.cookie = cookie
        self.refresh_cookie = refresh_cookie
        self.cookie_obtained_at = cookie_obtained_at
        self.device_id = device_id
        self.timeout = timeout
        self._config_entry = config_entry
        self._session: aiohttp.ClientSession | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _api_headers(self) -> dict[str, str]:
        return {
            "Cookie": f"{COOKIE_NAME}={self.cookie}",
            "Accept": "application/json",
            "User-Agent": _UA,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with async_timeout.timeout(self.timeout):
                await self._maybe_refresh()
                return await self._fetch_data()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"API connection error: {err}") from err
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _maybe_refresh(self) -> None:
        age_days = (time.time() - self.cookie_obtained_at) / 86400
        if age_days < COOKIE_REFRESH_DAYS:
            return
        _LOGGER.debug("Session is %.1f days old — refreshing", age_days)
        await self._do_refresh()

    async def _do_refresh(self) -> None:
        """Try fast refresh first; fall back to full Cognito re-auth."""
        session = self._get_session()

        # Fast path: use the se_monitoring_refresh cookie
        if self.refresh_cookie:
            result = await async_refresh_session(session, self.refresh_cookie, self.timeout)
            if result:
                spring, refresh = result
                if spring:
                    self._update_cookies(spring, refresh)
                    return
                _LOGGER.debug("Fast refresh returned no spring cookie — falling back")

        # Slow path: full Cognito login
        _LOGGER.debug("Performing full Cognito re-authentication")
        try:
            spring, refresh = await async_login(session, self.email, self.password, self.timeout)
            self._update_cookies(spring, refresh)
        except (InvalidAuth, CannotConnect) as err:
            _LOGGER.error("Failed to refresh authentication: %s", err)

    def _update_cookies(self, spring: str, refresh: str) -> None:
        self.cookie = spring
        self.refresh_cookie = refresh
        self.cookie_obtained_at = time.time()
        new_data = dict(self._config_entry.data)
        new_data[CONF_COOKIE] = spring
        new_data[CONF_MONITORING_REFRESH_COOKIE] = refresh
        new_data[CONF_COOKIE_OBTAINED_AT] = self.cookie_obtained_at
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        _LOGGER.debug("Session cookies updated")

    async def _fetch_data(self) -> dict[str, Any]:
        url = f"{API_BASE_URL}{API_DEVICES_ENDPOINT.format(site_id=self.site_id)}"
        session = self._get_session()

        async with session.get(url, headers=self._api_headers()) as resp:
            if resp.status in (401, 403):
                _LOGGER.warning("Got %s — attempting emergency re-auth", resp.status)
                await self._do_refresh()
                async with session.get(url, headers=self._api_headers()) as retry:
                    if retry.status != 200:
                        raise UpdateFailed(f"API returned {retry.status} after re-auth")
                    data = await retry.json(content_type=None)
            elif resp.status != 200:
                raise UpdateFailed(f"API returned status {resp.status}")
            else:
                data = await resp.json(content_type=None)

        if "devicesByType" not in data:
            raise UpdateFailed("Invalid API response: missing devicesByType")
        if "EV_CHARGER" not in data["devicesByType"]:
            raise UpdateFailed("No EV Charger found in site")

        ev_chargers = data["devicesByType"]["EV_CHARGER"]
        if not ev_chargers:
            raise UpdateFailed("No EV Charger data available")

        if not self.device_id:
            self.device_id = str(ev_chargers[0].get("reporterId"))

        return ev_chargers[0]

    async def async_start_charging(self) -> bool:
        return await self._set_charging_state(100)

    async def async_stop_charging(self) -> bool:
        return await self._set_charging_state(0)

    async def _set_charging_state(self, level: int) -> bool:
        if not self.device_id:
            _LOGGER.error("Device ID not available")
            return False

        url = f"{API_BASE_URL}{API_CONTROL_ENDPOINT.format(site_id=self.site_id, device_id=self.device_id)}"
        headers = {
            **self._api_headers(),
            "Content-Type": "application/json",
        }
        payload = {"mode": "MANUAL", "level": level, "duration": None}

        try:
            session = self._get_session()
            async with async_timeout.timeout(self.timeout):
                async with session.put(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        _LOGGER.error("Failed to set charging state: %s", resp.status)
                        return False
                    result = await resp.json(content_type=None)
                    if result.get("status") == "PASSED":
                        await self.async_request_refresh()
                        return True
                    _LOGGER.error("API returned error: %s", result)
                    return False
        except Exception as err:
            _LOGGER.error("Error setting charging state: %s", err)
            return False

    async def async_shutdown(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
