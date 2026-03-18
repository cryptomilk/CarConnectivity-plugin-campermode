# carconnectivity-plugin-campermode

A CarConnectivity plugin providing a mobile-friendly web UI for overnight car
climatisation cycling — periodic heating/cooling with battery safety and timer
scheduling (camper mode).

## Features

- Periodic climatisation cycles (30 min on, configurable pause between cycles)
- Battery safety: auto-stop below configurable threshold
- Timer scheduling for automatic activation
- Mobile-first web UI at a dedicated port
- VW climate zone control (seat heating zones)

## Installation

```bash
pip install carconnectivity-plugin-campermode
```

## Configuration

Add to your `carconnectivity.json`:

```json
{
    "type": "campermode",
    "config": {
        "host": "0.0.0.0",
        "port": 4001,
        "username": "admin",
        "password": "secret"
    }
}
```

See the spec (`CARCONNECT_CAMPERMODE.md`) for full configuration options.
