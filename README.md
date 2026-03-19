# carconnectivity-plugin-campermode

A CarConnectivity plugin providing a mobile-friendly web UI for overnight car
climatisation cycling - periodic heating/cooling with battery safety and timer
scheduling (camper mode).

## Features

- Periodic climatisation cycles (30 min on, configurable pause between cycles)
- Battery safety: auto-stop below configurable threshold
- Timer scheduling for automatic activation
- Mobile-first web UI at a dedicated port
- VW heating zone control (seat, steering wheel, mirror, rear window, windscreen)

## Installation

```bash
pip install carconnectivity-plugin-campermode
```

## Configuration

Add to your `carconnectivity.json` (comments shown for documentation — remove
them from the actual file as JSON does not support comments):

```jsonc
{
    "type": "campermode",
    "config": {
        // ── Web UI ───────────────────────────────────────────────────────────
        // Network interface the web server listens on.
        // "0.0.0.0" = all interfaces; "127.0.0.1" = localhost only.
        "host": "0.0.0.0",

        // TCP port for the web UI. Default: 4001.
        "port": 4001,

        // ── Authentication ───────────────────────────────────────────────────
        // Single-user login. Both keys must be present to enable auth.
        // When omitted the UI is accessible without a password.
        "username": "admin",
        "password": "secret",

        // Multi-user alternative to username/password above.
        // "users": [
        //     { "username": "alice", "password": "hunter2" },
        //     { "username": "bob",   "password": "s3cr3t"  }
        // ],

        // ── HTTPS ────────────────────────────────────────────────────────────
        // Set to true to enable TLS. Without certificate paths below, a
        // temporary self-signed certificate is generated automatically.
        // "https": true,

        // Provide your own certificate/key pair instead of the auto-generated one.
        // "ssl_certificate_file":     "/etc/ssl/certs/campermode.crt",
        // "ssl_certificate_key_file": "/etc/ssl/private/campermode.key",

        // ── Session security ─────────────────────────────────────────────────
        // Flask session secret key. Set this to a long random string in
        // production; if omitted a new key is generated on every restart,
        // which invalidates all active browser sessions.
        // Generate with: openssl rand -hex 32
        // "secret_key": "change-me-to-a-long-random-string",

        // ── Vehicle targeting ────────────────────────────────────────────────
        // VIN of the vehicle to control. When omitted the first vehicle
        // found in the garage is used automatically.
        // "vin": "WVWZZZ1JZXXXXXXXX",

        // ── Persistence ──────────────────────────────────────────────────────
        // Path to the JSON file where climatisation settings and timers are
        // stored. Default: "campermode.json" (relative to the working directory).
        // "data_file": "/var/lib/carconnectivity/campermode.json"
    }
}
```

### Climatisation settings

The values below are stored in `campermode.json` and can be adjusted at any
time through the web UI. They are listed here for reference.

| Setting | Default | Range / notes |
|---|---|---|
| `min_battery_level` | `20` | 10–90 % — session stops when battery drops to this level |
| `cycle_duration_minutes` | `30` | ≥ 1 — how long each climatisation cycle runs |
| `minutes_between_cycles` | `0` | ≥ 0 — pause between consecutive cycles (0 = back-to-back) |
| `total_duration_minutes` | `60` | ≥ 1 — overall session length (ignored when `endless` is true) |
| `endless` | `false` | Run indefinitely until manually stopped or battery threshold hit |
| `target_temperature` | `22.0` | 15.5–30.0 °C (VW system limits) |
| `window_heating` | `false` | Enable window heating during each cycle |
| `front_zone_left` | `false` | Front-left heating zone |
| `front_zone_right` | `false` | Front-right heating zone |
| `rear_zone_left` | `false` | Rear-left heating zone |
| `rear_zone_right` | `false` | Rear-right heating zone |
