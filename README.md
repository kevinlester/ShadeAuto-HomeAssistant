# ShadeAuto (local) — Home Assistant Custom Integration

Control Norman **ShadeAuto** shades locally via the hub’s undocumented HTTP API.  
✅ Multiple hubs • ✅ One device per shade • ✅ Covers (open/close/set position) • ✅ Battery **%** sensor per shade • ✅ Low-battery binary sensor • ✅ Burst polling after commands

> ⚠️ This uses **undocumented** local endpoints (`/NM/v1/*`). Firmware may change behavior. Isolate the hub and restrict access to TCP **10123**.

## Features
- **Auto-discover shades** from the hub.
- One **Device** per discovered Shade
- One **Cover** entity per shade: open/close/**set position** (0–100).
- One **Battery (%)** sensor per shade.
- Works with **multiple hubs** (add each hub by IP).

## Options
- **Poll seconds**: default 30s idle polling.
- **Burst interval / cycles**: fast polls after a move (e.g., 2s × 5).
- **Low battery threshold**: default 20% for the `*_battery_low` binary sensor.

## Entities per shade (unique device)
- `cover.<shade>`: open/close/set position (0–100)
- `sensor.<shade>_battery`: battery percent
- `binary_sensor.<shade>_battery_low`: low-battery flag (threshold in Options)

## Notes
- Battery source is the hub’s `BatteryVoltage`. If it’s 0–100, we use it as-is; if it’s volts, we map 3.30–4.20 V → 0–100%.

## Requirements
- ShadeAuto hub reachable on your LAN (port **10123**).
- Home Assistant 2024.6+ (tested).

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

## Configuration
- **Host**: hub IP (e.g., `192.168.1.50`).
- **Options** → *Poll seconds*: default **30s**. (Faster polling increases traffic.)

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
- No top/middle rail (TDBU) control in this build.
- Battery is exposed as **%**; mapping from volts is a heuristic if hub returns volts.
- API is not official; behavior may change with updates.

## Credits & License
- Community discoveries of the local endpoints inspired this integration.
- MIT License. Add a `LICENSE` if you publish publicly.

