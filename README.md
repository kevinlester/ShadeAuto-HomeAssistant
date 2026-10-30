# ShadeAuto (local) — Home Assistant Custom Integration

Control Norman **ShadeAuto** shades locally through the hub's undocumented HTTP API: lift, **vane tilt** on PerfectSheer-style sheer shades, stop, and battery monitoring, with all the usual `cover` UI features.

---
> ⚠️ This uses **undocumented** local endpoints (`/NM/v1/*`). Firmware may change behavior. Isolate the hub and restrict access to TCP **10123**.
---

## Features
- **Auto-discover shades** from any ShadeAuto hub(s); works with **multiple hubs** (add each hub by IP/hostname).
- One **Device** per shade, named as in the ShadeAuto app, showing model and motor firmware.
  - One **Cover** entity per shade: open / close / **set position** (0–100) / **stop**, plus **tilt** (open / close / set / stop) for vane shades.
  - One **Battery (%)** sensor and one **Battery low** flag per shade.
- **The hub is the source of truth.** Positions and vane state always converge to what the hub reports, including moves made from the ShadeAuto app or a remote. While a shade moves, the hub is polled every couple of seconds; otherwise every 30 s (both configurable).

## Entities per shade
- `cover.<shade>`: `current_position` 0 = fully lowered, 100 = fully raised; `current_tilt_position` 0 = vanes closed, 100 = vanes open (light through).
- `sensor.<shade>_battery`: battery percent (diagnostic).
- `binary_sensor.<shade>_battery_low`: low-battery flag, threshold in Options (diagnostic).

## How tilt works on these shades
Sheer (PerfectSheer) shades can only tilt their vanes while the shade is **fully lowered** — the hub enforces this and closes the vanes whenever the shade is raised. The integration follows the hub:

- `cover.open_cover_tilt`, `cover.close_cover_tilt` and `cover.set_cover_tilt_position` **lower the shade if it is not already down**, then set the vanes.
- Raising the shade to any height closes the vanes; lowering it fully (`close_cover` or position 0) keeps whatever vane setting it had.
- `stop_cover` and `stop_cover_tilt` both stop the motor. Home Assistant stops showing the shade as opening or closing right away; the position it actually stopped at appears a couple of seconds after the hub reports it (about 35 s after the stop on the hubs seen so far). A shade stopped above the bottom ends with its vanes closed.

If your shades have no vanes, turn **Expose vane tilt** off in the integration's options and the tilt controls disappear.

## Requirements
- ShadeAuto hub reachable on your LAN (port **10123**).
- Home Assistant **2026.10** or newer.

## Changing the hub address
If your hub gets a new IP address, open Settings → *Devices & Services* → **ShadeAuto**, click the ⋯ menu next to the hub entry and choose **Reconfigure**. Devices, areas, names, entity ids and automations are kept: the integration identifies the hub by its `ThingName`, not by its address.

## Options
Settings → *Devices & Services* → **ShadeAuto** → **Configure**. Saving reloads the integration so changes take effect at once.

- **Poll interval while idle**: `/status` poll interval while nothing is moving (default 30 s).
- **Poll interval while a shade is moving**: faster polling during a move (default 2 s). The hub only reports a position once a move has finished, so this is how quickly Home Assistant notices.
- **Expose vane tilt**: show tilt controls and `current_tilt_position` (default on).
- **Low battery threshold**: battery % at which the `*_battery_low` flag turns on (default 20 %).
- **Minimum time between shade commands**: gap between hub `/control` posts (default 0.75 s).
- **Verify the shade arrived and retry once**: after the delay below, check both the lift and the vane target the hub holds against what was asked for. If the hub never took the command (its target never matched ours), the command is re-sent once. If the hub had taken it and the target changed afterwards, the shade was re-commanded from the app or a remote in the meantime and nothing is re-sent.
- **Seconds to wait before verifying** (default 60 s, minimum 30 s). The hub reports where a shade ended up 10–35 s after a command; checking earlier would re-send a command to a shade that is still moving.
- **Animate the position slider while a shade is moving**: interpolates `current_position` during a lift so the slider moves instead of jumping; the hub's position always wins once the move has settled, and the animation gives up early if the hub's target changes mid-move (someone re-commanded the shade from the app or a remote).
- **Time for a full open or close**: travel time used by the animation. The default of 28 s is upstream's value for their shades; PerfectSheer shades were observed to need roughly 60 s for a full travel (28–35 s for half travels), so set it to about 60 for those.

## Installation

### Via HACS (custom repository)
Click this badge to add the upstream repository to your custom repositories: [![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kevinlester&repository=ShadeAuto-HomeAssistant&category=integration)

Or: HACS → *Integrations* → ⋯ → **Custom repositories** → add this GitHub repo URL, Category **Integration**.

Then
1. Find **ShadeAuto (local)** → **Download** → **Restart** Home Assistant.
2. Settings → *Devices & Services* → **Add Integration** → **ShadeAuto (local)** → enter your **hub IP**.
3. Repeat for additional hubs.

### Manual
1. Copy `custom_components/shadeauto/` into your HA `config/custom_components/`.
2. Restart HA, then add the integration.

### Upgrading from 0.3.x
Entity ids and unique ids are unchanged. Shades that were still called "Shade <id>" take their name from the ShadeAuto app (renames you made in Home Assistant are kept). Devices that 0.3.x registered under the hub's IP address are moved to the hub's `ThingName` identity on the first start, so they keep their areas and names and survive a later address change. The *Notification timeout* option is gone; the hub never delivered notifications, so the integration polls instead. The verify delay now has a minimum of 30 s.

## Example use

### Open a shade to 40 %
```yaml
action: cover.set_cover_position
target:
  entity_id: cover.kitchen
data:
  position: 40
```

### Let light through: lower the shade and open the vanes half way
```yaml
action: cover.set_cover_tilt_position
target:
  entity_id: cover.kitchen
data:
  tilt_position: 50
```

### Low battery notification based on `binary_sensor.<shade>_battery_low`
```yaml
alias: Shade battery low (notify)
mode: single
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.kitchen_battery_low
      - binary_sensor.office_battery_low
      # add more *_battery_low sensors here
    to: "on"
actions:
  - action: persistent_notification.create
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

## Troubleshooting
- **No entities?** Ensure the hub is reachable; try curl:
  ```bash
  curl -s -X POST http://HUB_IP:10123/NM/v1/registration -H 'Content-Type: application/json' -d '{"Timestamp": 1700000000}'
  ```
  If the hub answers but its shade list comes back as an error or as something that is not JSON, the integration does not load with zero shades; it keeps retrying setup and logs why.
- **"Could not move shade …" when calling a service?** The hub refused the command or did not answer. The shade is shown at the position the hub last reported, nothing is retried, and the message names the shade and the reason.
- **Watch what the hub is doing** while you use the app, a remote or Home Assistant:
  ```bash
  python3 tools/hubwatch.py --host HUB_IP --names
  ```
  It prints a line for every position or target change. `tools/hubwatch.py --help` lists the other modes (sending a single command, dumping raw status).
- **Logs** (add to `configuration.yaml`):
  ```yaml
  logger:
    logs:
      custom_components.shadeauto: debug
  ```

## Limitations
- The hub reports positions only when a move has **finished** (typically 10–35 s after the command), never while moving. The slider animation bridges that gap for lifts; vane changes appear when the hub reports them.
- Moves made with a **physical remote** are reported by the hub only sometimes; moves from the ShadeAuto app are always reported. Home Assistant can only show what the hub knows.
- Vanes can be tilted only while the shade is fully lowered (a hub rule, see above). True top-down/bottom-up shades are not supported as a second lift position.
- `stop_cover_tilt` is exposed, but stopping a shade in the middle of a vane-only move has not been tested separately. It is the same motor and the same stop command as `stop_cover`; what the hub reports for the vanes afterwards is unknown.
- Battery is whatever the hub reports in `BatteryVoltage`, which is a percentage on the hubs seen so far.
- The API is not official; behavior may change with hub firmware updates.

## Developing
See `docs/HUB_PROTOCOL.md` for the hub's protocol as observed on real hardware, and `CONTRIBUTING.md` for the test and lint setup (`uv venv --python 3.14 && uv pip install -r requirements_test.txt && .venv/bin/python -m pytest`).

## New feature requests
Open a GitHub issue. Testing may need your help if the developers do not have the hardware.

## Credits & License
- Community discoveries of the local endpoints inspired this integration.
- MIT License
