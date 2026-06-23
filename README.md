# SolarEdge EV Charger — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant Compatible](https://img.shields.io/badge/Home%20Assistant-Compatible-blue.svg)](https://www.home-assistant.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub Release](https://img.shields.io/github/release/mvillarejo/solaredge-evcharger-ha.svg)](https://github.com/mvillarejo/solaredge-evcharger-ha/releases)

Home Assistant custom integration for monitoring and controlling **SolarEdge EV Chargers** via the SolarEdge ONE monitoring portal (`monitoring.solaredge.com`).

> **Based on** [briadelour/solaredge-evcharger-ha](https://github.com/briadelour/solaredge-evcharger-ha) — rewritten to support the **SolarEdge ONE** portal (European accounts, AWS Cognito authentication) and to remove the need for manual cookie extraction.

---

## What's New vs the Original

| Feature | Original | This fork |
|---|---|---|
| Authentication | Manual cookie copy-paste | Automatic email + password login |
| Portal | Old `monitoring.solaredge.com/solaredge-web` | New SolarEdge ONE (`/one`) |
| EU support | US-focused | Full EU (Cognito `eu-central-1`) |
| Site discovery | Manual Site ID entry | Auto-discover from your account |
| Session refresh | Manual | Automatic (no daily re-auth) |

---

## Features

- **Automatic login** — enter email and password, the integration handles the Cognito PKCE OAuth2 flow
- **Site discovery** — your sites are listed in a dropdown during setup
- **Sensors**: charger status, power (kW), session energy (kWh), total energy, solar usage, charge mode, scheduled time, excess PV mode
- **Binary sensors**: connected, charging
- **Buttons**: Start charging, Stop charging
- **Auto session refresh** — cookies are refreshed every 5 days without re-entering credentials

---

## Requirements

- Home Assistant 2023.8+
- SolarEdge monitoring account at [monitoring.solaredge.com](https://monitoring.solaredge.com)
- EV Charger registered to your account

---

## Installation

### HACS (recommended)

1. In HACS, go to **Integrations → ⋮ → Custom repositories**
2. Add `https://github.com/mvillarejo/solaredge-evcharger-ha` as an **Integration**
3. Search for **SolarEdge EV Charger** and install
4. Restart Home Assistant

### Manual

Copy the `custom_components/solaredge_ev_charger` folder into your HA `custom_components` directory and restart.

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **SolarEdge EV Charger**
3. Enter your **email** and **password** for `monitoring.solaredge.com`
4. Select your site from the dropdown
5. Done — entities will appear automatically

---

## Entities

| Entity | Type | Description |
|---|---|---|
| Charger Status | Sensor | Current charger state (CHARGING, PLUGGED_IN, NOT_CONNECTED, …) |
| Charging Power | Sensor | Current power draw (kW) |
| Session Energy | Sensor | Energy delivered in current session (kWh) |
| Total Energy | Sensor | Lifetime energy delivered (kWh) |
| Solar Usage | Sensor | Solar energy used in session |
| Charge Mode | Sensor | AUTO or MANUAL |
| Scheduled Charge Time | Sensor | Next scheduled charge time |
| Excess PV | Sensor | Excess PV charging mode status |
| Connected | Binary Sensor | Whether a vehicle is plugged in |
| Charging | Binary Sensor | Whether the charger is actively charging |
| Start Charging | Button | Manually start a charging session |
| Stop Charging | Button | Stop a charging session |

---

## Troubleshooting

Enable debug logging to trace the authentication flow:

```yaml
logger:
  default: warning
  logs:
    custom_components.solaredge_ev_charger: debug
```

---

## Attribution & License

This project is a fork and rewrite of [briadelour/solaredge-evcharger-ha](https://github.com/briadelour/solaredge-evcharger-ha), originally authored by [@briadelour](https://github.com/briadelour).

The EU portal authentication rewrite (SolarEdge ONE / AWS Cognito PKCE flow) was authored by [@mvillarejo](https://github.com/mvillarejo).

Both the original and this fork are released under the [MIT License](LICENSE).

---

> **Disclaimer:** This integration uses the private SolarEdge monitoring API. It is not officially supported or endorsed by SolarEdge. Use at your own risk.
