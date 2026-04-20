# Tilt Support for Norman ShadeAuto Blinds

## Background

The Norman ShadeAuto Hub exposes a local HTTP API on port 10123 that allows control of connected blinds. The existing Home Assistant integration (via HACS) supported opening and closing blinds using `BottomRailPosition` (0-100), but had no tilt control — even though the hub already reported tilt position in its status responses.

Some Norman blinds have tiltable slats (like shutters or sheer shades). These can tilt left-closed, right-closed, or flat/open. The Norman app supports this, but the HA integration did not.

## What we discovered

By probing the hub's API, we found:

1. **The hub already reports tilt** — the `/NM/v1/status` endpoint returns `MiddleRailPosition` (0-100) alongside `BottomRailPosition` for shades that support tilt.

2. **The control endpoint requires both values** — sending only `MiddleRailPosition` in a `/NM/v1/control` request returns `Error: 2`. Both `BottomRailPosition` and `MiddleRailPosition` must be included together.

3. **Tilt mapping:**
   - `0` = tilted fully one direction (closed)
   - `50` = flat / perpendicular to window (open)
   - `100` = tilted fully the other direction (closed)

4. **The motor adjusts tilt during position changes** — when the shade moves up or down, the motor resets the tilt to around 17 to facilitate the movement. This means tilt must be re-read from the hub after any position command completes.

## What was changed

### api.py
Added a `middle` parameter to the `control()` method. When provided, it includes `MiddleRailPosition` in the JSON payload sent to the hub.

### coordinator.py
- Added `MiddleRailPosition` to the list of fields stored from hub status responses (previously only `BottomRailPosition`, `BatteryVoltage`, and `Name` were kept).
- During the first status read after setup, checks whether each shade reports `MiddleRailPosition`. If it does, the shade is flagged with `supports_tilt: True` in the peripheral metadata.

### cover.py
- Tilt features (`OPEN_TILT`, `CLOSE_TILT`, `SET_TILT_POSITION`) are conditionally enabled per-shade based on the `supports_tilt` flag detected during setup.
- Added `current_cover_tilt_position` property that reads `MiddleRailPosition` from the coordinator's cached status.
- Added tilt commands:
  - `set_tilt_position` — sends both the current bottom position and the requested tilt to the hub
  - `open_tilt` — sets tilt to 50 (flat)
  - `close_tilt` — sets tilt to 0
- Existing position commands (`open`, `close`, `set_position`) now pass the current tilt value through to the hub, so it receives both fields as required.
- After any tilt command, a status refresh is triggered to pick up whatever position the motor actually settled at.

## Design decisions

- **Auto-detection over configuration** — tilt support is detected from the hub's responses rather than requiring the user to manually enable it. Shades that don't support tilt (like rollers) simply won't report `MiddleRailPosition` and won't get tilt controls in HA.
- **Always send both values** — since the hub rejects control requests that only contain one position field, every command includes both bottom and middle, even if only one is changing.
- **Trust the hub's reported position** — because the motor adjusts tilt during shade movement, we always re-read the actual tilt from the hub rather than assuming it stayed where we set it.
