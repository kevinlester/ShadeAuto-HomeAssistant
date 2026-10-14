# ShadeAuto (local) — Home Assistant Custom Integration

Control Norman **ShadeAuto** shades locally via the hub’s undocumented HTTP API.  Support for all the usual `cover` UI features like current state, moving positions, and icon updates.

---
> ⚠️ This uses **undocumented** local endpoints (`/NM/v1/*`). Firmware may change behavior. Isolate the hub and restrict access to TCP **10123**.
---

## Features
- **Auto-discover shades** from any ShadeAuto hub(s).
- One **Device** per discovered Shade
  - One **Cover** entity per shade: open/close/**set position** (0–100).
  - **TDBU (top-down/bottom-up) shades** get a **second cover** for the top rail, controlled independently.
  - One **Battery (%)** sensor per shade.
  - One **Low Battery (%)** flag per shade.
- Works with **multiple hubs** (add each hub by IP/Hostname).

## Entities per shade (unique device)
- `cover.<shade>`: open/close/set position (0–100) — the bottom rail
- `cover.<shade>_top`: the top rail, **TDBU shades only**
- `sensor.<shade>_battery`: battery percent
- `binary_sensor.<shade>_battery_low`: low-battery flag (threshold in Options)

## TDBU (top-down/bottom-up) shades
TDBU shades (hub `ModuleType` 33) have two independently-motorised rails. This build
exposes each as its own cover: the original entity drives the bottom rail, and a second
`… Top` entity drives the top (middle) rail. Single-rail shades are unaffected and keep
their single cover and existing entity IDs.

Both rail positions use the hub's height axis: **0% = fully lowered** and **100% =
fully raised**. The top cover is no longer inverted in this update. Rail height does not
by itself describe how much of the window is covered; that depends on both rails.

The hub controls both rails together, so every TDBU move sends both rail positions,
holding the rail you didn't touch in place whenever the destination permits it. A
**collision guard** (on by default, Options) keeps the top rail at or above the bottom
rail. If a requested destination needs more room, the companion moves just enough
to permit it. For example, raising the bottom to 100% also raises a lowered top rail
to 100%. With a nonzero minimum gap, the bottom's highest position is 100 minus the
gap. Concurrent scene calls are serialized per shade, including retries.

### Upgrading from earlier TDBU builds

The old top cover mapped the hub's 35% height to 65% and sent a full-open command to
the bottom of its travel. With this update, full-open presets should set **both covers to 100%**
(or call `cover.open_cover` on both). Review saved top-rail positions: to preserve an
old physical destination, replace an old numeric top position `p` with `100 - p`.
Recapturing scenes at the desired physical position is another option. Entity IDs
are unchanged. Once estimated travel finishes, the displayed position returns to
the hub reading rather than remaining pinned to an old target.

## Notes
- Battery source is the hub’s `BatteryVoltage`. If it’s 0–100, we use it as-is; if it’s volts, we map 3.30–4.20 V → 0–100%.

## Requirements
- ShadeAuto hub reachable on your LAN (port **10123**).
- Home Assistant 2024.6+ (tested).

## Options
- **Poll seconds**: idle `/status` poll interval while shades are not moving (default 30s).
- **Low battery threshold**: battery % at which the `*_battery_low` flag turns on (default 20%).
- **Min time between shade commands**: enforces a small gap between hub `/control` posts (helps avoid overload).
- **Verify & retry once**: when enabled, checks a shade after a delay and sends one extra `/control` if it isn’t near the last requested position.
- **Verify delay (sec)**: how long to wait before verifying/retrying the last command.
- **Notification timeout (sec)**: hold time for the hub’s long-poll `/notification` request; `-1` lets the hub hold the connection open.
- **Estimation enabled**: when on, estimates shade motion so `current_position` and `opening/closing` states are updated smoothly between hub reports.
- **Estimated time to fully open (sec)**: time a shade typically takes to move from 0 → 100; used to scale the opening/closing duration and interpolate positions for partial moves.
- **TDBU: prevent the two rails from crossing**: collision guard for TDBU shades (default on).
- **TDBU: minimum gap to keep between rails (%)**: extra separation the guard enforces (default 0).

## Installation

### Via HACS (custom repository)
Click this badge to automatically add this repository to your list of custom repositories: [![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kevinlester&repository=ShadeAuto-HomeAssistant&category=integration)

If that doesn't work or is disagreeable to you, you can add it via the following:
HACS → *Integrations* → ⋯ → **Custom repositories** → add this GitHub repo URL, Category **Integration**.

Then
1. Find **ShadeAuto (local)** → **Download** → **Restart** Home Assistant.
2. Settings → *Devices & Services* → **Add Integration** → **ShadeAuto (local)** → enter your **hub IP**.
3. Repeat above step for additional hubs.

### Manual
1. Copy `custom_components/shadeauto/` into your HA `config/custom_components/`.
2. Restart HA, then add the integration.

## Example use

### Open a shade to 40%
Call the standard Cover service with your shade entity:

```yaml
service: cover.set_cover_position
target:
  entity_id: cover.deck_shade
data:
  position: 40
```
### Low battery notification based on `binary_sensor.<<shade>>_battery_low`

```alias: Shade battery low (notify)
mode: single
trigger:
  - platform: state
    entity_id:
      - binary_sensor.deck_shade_battery_low
      - binary_sensor.kevin_office_shade_battery_low
      # add more *_battery_low sensors here
    to: "on"
action:
  - service: persistent_notification.create
    data:
      title: "Shade battery low"
      message: >-
        {% set low = trigger.to_state %}
        {% set batt_sensor = 'sensor.' ~ low.entity_id.split('.',1)[1].replace('_battery_low','_battery') %}
        {{ low.name }} is low.
        {% if states(batt_sensor) not in ('unknown','unavailable','') %}
        Current level: {{ states(batt_sensor) }}%
        {% endif %}
```

### Low battery notification based on `binary_sensor.<<shade>>_battery`

```alias: Shade battery below 20%
trigger:
  - platform: numeric_state
    entity_id: sensor.deck_shade_battery
    below: 20
action:
  - service: persistent_notification.create
    data:
      title: "Shade battery low"
      message: "Deck Shade battery is at {{ states('sensor.deck_shade_battery') }}%."
```

## New Feature Requests
Create new feature requests via creating a new Github Issue in this repo.  Note that testing might require some assistance from your side in case the necessary hardware is unavailable to the devs.

## Troubleshooting
- **No entities?** Ensure the hub is reachable; try curl:
  ```bash
  curl -s -X POST http://HUB_IP:10123/NM/v1/registration -H 'Content-Type: application/json' -d '{"Timestamp": 1700000000}'
  ```
- **Logs** (add to `configuration.yaml`):
  ```yaml
  logger:
    logs:
      custom_components.shadeauto: debug
  ```

## Limitations
- TDBU support is detected by hub `ModuleType` (33) and rides the same undocumented
  endpoints; the control field/behaviour was reverse-engineered against real hardware
  and may vary by firmware. It degrades to single-rail behaviour when the second rail
  isn't present.
- Battery is exposed as **%**; mapping from volts is a heuristic if hub returns volts.
- API is not official; behavior may change with updates.

## Credits & License
- Community discoveries of the local endpoints inspired this integration.
- MIT License

