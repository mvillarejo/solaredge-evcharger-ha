# SolarEdge EV Charger

Home Assistant integration for monitoring and controlling SolarEdge EV Chargers via the **SolarEdge ONE** monitoring portal (`monitoring.solaredge.com`).

## Features

- Automatic login via email and password — no manual cookie extraction
- Site auto-discovery with dropdown selection
- Real-time charger status, power, energy, and session data
- Start / Stop charging controls
- Session cookie auto-refresh (no daily re-authentication)
- European SolarEdge ONE portal (`eu-central-1`)

## Requirements

- SolarEdge monitoring account at [monitoring.solaredge.com](https://monitoring.solaredge.com)
- An EV Charger registered to your account

## Installation via HACS

1. Add this repository as a custom repository in HACS
2. Search for **SolarEdge EV Charger** and install
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration** and search for *SolarEdge EV Charger*
