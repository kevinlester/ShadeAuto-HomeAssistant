# ShadeAuto (local) — Home Assistant Custom Integration

Control Norman **ShadeAuto** shades locally through the hub's undocumented HTTP API. Shades appear as standard Home Assistant covers with open, close, stop, set position, and tilt where the shade supports it, plus battery sensors and state that follows the shade however it was moved.

---
> ⚠️ This uses **undocumented** local endpoints (`/NM/v1/*`). Firmware may change behavior.
---

## Features
- **Auto-discover shades** from any ShadeAuto hub(s); works with **multiple hubs** (add each hub by IP/hostname).
- One **Device** per discovered shade, named as in the ShadeAuto app, showing model and motor firmware.
  - One **Cover** entity per shade that supports the following:
    - open / close / stop
    - set position (0–100)
    - tilt (open / close / set / stop) on PerfectSheer shades and SmartDrapes
  - One **Top** cover for two-rail shades
  - **Battery (%)** sensor per shade
  - **Battery low** flag per shade.
- **State syncing**. The hub acts as the source of truth, which allows this integration to keep the state of the shades in sync regardless of how they were moved, even if they were moved outside of HA. See [How shade updates reach Home Assistant](#how-shade-updates-reach-home-assistant).

## Requirements
- Home Assistant must be able to reach the ShadeAuto hub on TCP port **10123**.
- Home Assistant **2025.8** or newer. See [CONTRIBUTING.md](CONTRIBUTING.md) for how the minimum is chosen.

## Entities per shade
Every shade gets one device with these entities (`<shade>` is the shade's name from the ShadeAuto app, for example `cover.kitchen`):

| Entity | What it is | Values |
|---|---|---|
| `cover.<shade>` | The shade. Supports open, close, stop and set position. | State: `open`, `closed`, `opening`, `closing`. Attribute `current_position`: 0 = fully lowered, 100 = fully raised. |
| `cover.<shade>_top` | Two-rail shades only: the top rail. The main cover then drives the bottom rail. | Same as above, for the top rail. |
| `sensor.<shade>_battery` | Battery level (diagnostic). | Percent. |
| `binary_sensor.<shade>_battery_low` | Low-battery flag (diagnostic). | `on` below the threshold set in Options. |

PerfectSheer shades add tilt to `cover.<shade>`: open, close, stop and set tilt, with the attribute `current_tilt_position`, 0 = vanes closed, 100 = vanes open. SmartDrapes add the same tilt controls on the hub's own scale: 0 and 100 = vanes closed in opposite directions, 50 = vanes open (see [SmartDrapes](#smartdrapes)).

## Supported shade types
The hub reports every shade's type as a `ModuleType`/`ModuleDetail` pair; the device page shows it as the model (for example "PerfectSheer", or "Shade (type 33/3)"). Not all combinations of hardware have been tested. If you have success or failure with unverified hardware below, please let us know.

| Shade | Type | Handled as | How it was verified |
|---|---|---|---|
| PerfectSheer | 48/3 | lift + vane tilt | Five shades on a real hub (`docs/hub-characterization-2026-10-22.log`). Thanks to Modusmundi. |
| PerfectSheer (second generation) | 49/3 | lift + vane tilt | Not verified. The ShadeAuto app calls it PerfectSheer ([kedube/ha-norman](https://github.com/kedube/ha-norman)'s reading of the app). |
| Cellular | 32/2 | single-rail | Real shades in kedube/ha-norman. Not tested with this integration. |
| Cellular | 32/0 | single-rail | The ShadeAuto app only (kedube/ha-norman). |
| Roller | 48/1 | single-rail | Users' diagnostics in kedube/ha-norman issues #2 and #5. Not tested with this integration. |
| Roman | 48/1 | single-rail | The maintainer's own shades, on a real hub with this integration. The hub reports the same type for them as for rollers. |
| Roman; second-generation roller and roman | 48/2; 49/1, 49/2 | single-rail | The ShadeAuto app only (kedube/ha-norman). Unconfirmed: the roman shades above report 48/1, not 48/2, so the app's names may not match the hub's types. |
| Other products of those families | 32, 48 or 49 with any other detail | single-rail | Not seen anywhere; handled like the rest of the family, without a warning. |
| Top-down/bottom-up or day/night cellular | 33/3 | two-rail | Real shades in kedube/ha-norman. This port has not been tested on hardware. |
| Top-down/bottom-up cellular | 33/other, 32/3, 32/4, 32/5 | two-rail | The ShadeAuto app only (kedube/ha-norman). |
| SmartDrape | 80/any | draw + vane tilt | Three 80/1 drapes (firmware 0.2.3) on a real hub, tested by rtm: tilt, draw, stop and the vanes kept through a draw. The tilt follows [PR #7](https://github.com/kevinlester/ShadeAuto-HomeAssistant/pull/7). |
| Shutter | 1/any | lift only | The ShadeAuto app only. Shutters have louvres instead of rails, which are not supported; the lift commands probably do nothing. |
| Anything else | other types, or 32, 48 or 49 reported without a detail | lift only | A warning in the log asks you to report the type. Please open an issue with the pair and what the shade is. |

[docs/HUB_PROTOCOL.md](docs/HUB_PROTOCOL.md) (section 3.1) has the details and the evidence for each family.

## How tilt works on PerfectSheer shades
Sheer (PerfectSheer) shades can only tilt their vanes while the shade is **fully lowered** — the hub enforces this and closes the vanes whenever the shade is raised. The integration follows the hub:

- `cover.open_cover_tilt`, `cover.close_cover_tilt` and `cover.set_cover_tilt_position` **lower the shade if it is not already down**, then set the vanes.
- Raising the shade to any height closes the vanes; lowering it fully (`close_cover` or position 0) keeps whatever vane setting it had.
- `stop_cover` and `stop_cover_tilt` both stop the motor. Home Assistant stops showing the shade as opening or closing right away; the position it actually stopped at appears a couple of seconds after the hub reports it (about 35 s after the stop on the hubs seen so far). A shade stopped above the bottom ends with its vanes closed.

Only PerfectSheer shades and SmartDrapes get tilt controls. To hide them on those too, set **Vane tilt** to **Off** in the integration's options; the commands sent stay the same, so this never closes anyone's vanes.

## SmartDrapes
A SmartDrape's cover is the draw: `current_position` 0 = drawn closed, 100 = fully open. Its vanes are the tilt, on the hub's own scale rather than Home Assistant's usual one: **0 and 100 are closed** (tilted fully in opposite directions) and **50 is open**. Open tilt sends 50 and close tilt sends 0; set the tilt position directly for anything in between. The hub reports the vanes in stops about 17 apart, so a value you set reads back as the nearest stop.

**How far the vanes close depends on the draw.** With the drape fully closed (position 0) the vanes reach 0 and 100. With the drape open at all they stop short: on the three drapes tested, close tilt (0) lands at **17** and a tilt of 100 lands at **83**, the hub rewrites its own vane target to match, and the vendor app shows the same limit. So on an open drape `current_tilt_position` reads 17 after a close tilt. If you would rather see the open drape's range as 0 to 100, set **SmartDrapes: Min vane value when closed** to 17 in the [options](#options). On an open drape closed then reads 0 and 100, open stays 50, and the stops in between fall at 24 and 74; on a fully closed drape the hub's scale is used unchanged, so 0 is really 0. The number only changes how the tilt is shown and sent; the hub still moves the vanes to the same places.

The vanes never close on their own when the drape returns to closed: they stay where the open drape left them until a tilt command moves them. With the option set, closing the drape takes vanes that were "closed" (at 17 or 83) fully shut in the same command, so one close shuts everything; with the option at 0, send a close tilt after the close, as rtm's evening automation does.

Unlike PerfectSheer shades, a drape is never moved before a tilt: tilt commands leave the draw where it is. Every draw restates the vane position the hub last reported, so moving the drape does not close its vanes. Only the draw is tracked for "opening"/"closing" and for verify; after a tilt the integration keeps polling quickly until the hub reports the new vane position (about 12 s on the drapes tested), so `current_tilt_position` follows within seconds.

Tested by rtm on three SmartDrapes (80/1, firmware 0.2.3) with Home Assistant 2026.9; please report how yours behaves, especially where its vanes stop.

## Two-rail shades (top-down/bottom-up, day/night)
These shades have two rails that move independently, and each gets its own cover on the shade's device:

- `cover.<shade>` drives the **bottom** rail and `cover.<shade>_top` the **top** rail.
- Both positions are rail **heights**: 0 = lowered, 100 = raised. So on both covers *open* means raise and *close* means lower, which is not the same as letting light in: bottom 0 / top 100 covers the window completely, both at 100 leaves it fully open, and bottom 0 / top 40 lets light in above the fabric.
- The hub needs both rails in every command, so moving one rail always restates where the other one is heading (the target last sent to it, else the hub's target, else its position). When that is not known, the move is refused with an error rather than sent with a guess: if the hub has not reported the other rail, and after a stop until the hub reports where the shade stopped, because restating the target the hub still holds would restart the move you just stopped.
- The **collision guard** (on by default) keeps the top rail at or above the bottom rail. If a move would cross them, the other rail moves just far enough out of the way: raising the bottom rail past the top rail raises the top rail with it, and lowering the top rail past the bottom rail lowers the bottom rail with it. **Minimum gap between the rails** adds a margin the guard keeps between them; with a gap of 10 %, for example, the bottom rail goes no higher than 90 % and the top rail no lower than 10 %. With the guard off, the other rail is held where it is and the move is sent as asked.
- Stop on either cover stops the whole shade (the hub has one stop command per shade).

This follows [PR #9](https://github.com/kevinlester/ShadeAuto-HomeAssistant/pull/9), which was tested on one real shade. This integration has not been run against a two-rail shade yet; please report how yours behaves.

## How shade updates reach Home Assistant
This integration has 3 mechanisms for keeping the state of the shades in sync with HA: 
- A state poller that regularly polls the hub for the states of all shades. It runs at one of two speeds:
  - While nothing is moving, the hub is polled every 30 s ([Poll interval while idle](#options)). A move made from the app or a remote shows up at the next poll.
  - As soon as a poll, a command from HA, or a push notification shows a shade moving, polling drops to every 2 s ([Poll interval while a shade is moving](#options)) until the hub reports where the shade stopped, which it does 10 to 35 s after the move ends.
- An estimator that bridges the gap while a shade moves. HA shows the shade as opening or closing and animates the slider based on the travel time ([Time for a full open or close](#options)). When that time is up, the shade reads as open or closed at its estimated position. The hub's report, 10 to 35 s later, corrects it if the shade stopped somewhere else.
- A push notifier (disabled by default) that listens for changes in real time and updates a shade's state as soon as it can. This approach leads to more timely syncing, but is still being vetted so is off by default. If you enable this approach, please let us know how it works for you! See the next section for how it works.

## How the push notifier works
The hub has a notification stream: a connection it keeps open and on which it sends a short message whenever a shade's target or position changes. The message says which shade changed but not what changed, so the integration reads the hub's status as soon as one arrives (messages that arrive together share one read).

- **While the stream is open**, a change shows within a second or two: a command the hub takes from Home Assistant or the app (the shade starts showing as opening or closing), the end of every move the hub hears about (the new position), and a shade reporting in.
- **The stream is replaced every 5 minutes**, even when it looks fine, because long-lived streams have been seen to go quiet without an error.
- **When the stream is down** (the hub restarted, its Wi-Fi dropped, the connection broke), the integration keeps trying to reopen it, waiting a little longer after each failed attempt, up to a minute. A stream counts as working again only once it has stayed open for a minute, so one the hub closes as soon as it is opened is treated as a failed attempt too. Meanwhile polling is still active: every 30 s while idle, every couple of seconds while a shade moves. The hub does not resend what happened while the stream was down, so the integration reads the full status as soon as the stream is back. Nothing is lost; it just shows up at the next poll instead of at once.

## Changing the hub address
If your hub gets a new IP address, open Settings → *Devices & Services* → **ShadeAuto**, click the ⋯ menu next to the hub entry and choose **Reconfigure**. Devices, areas, names, entity ids and automations are kept: the integration identifies the hub by its `ThingName`, not by its address.

## Options
Settings → *Devices & Services* → **ShadeAuto** → **Configure**. Saving reloads the integration so changes take effect at once.

| Option | Default | What it does |
|---|---|---|
| **Poll interval while idle** | 30 s | How often `/status` is read while nothing is moving. With push updates on, this is the safety net for anything the notifications miss. |
| **Poll interval while a shade is moving** | 2 s | Faster polling during a move. The hub only reports a position once a move has finished. With push updates on, its notification usually brings that news first; this poll catches it when the stream is down. |
| **Use the hub's change notifications (push updates)** | off | Listen to the hub's notification stream and read the hub's status as soon as it announces a change. Off: the stream is never opened and Home Assistant learns about changes by polling alone. Polling runs either way. It does not turn off verify's report-in request (see **Verify the shade arrived and retry once if not**), which then always waits the full 10 s. **Note**: off by default as a precaution. The stream holds a connection to the hub open for as long as the integration runs, and it has only been tested on a few hubs so far. If you turn it on, please report how it behaves, good or bad. |
| **Vane tilt** | Automatic | **Automatic** shows tilt controls and `current_tilt_position` on the shades with vanes, PerfectSheer and SmartDrape; **Off** hides them everywhere. |
| **SmartDrapes: Min vane value when closed** | 0 | 0 shows a drape's vanes on the hub's own scale. While a drape is open its vanes stop short of 0 and 100; if closing them reads back as 17, enter 17: on an open drape the hub's 17 to 83 is then shown and set as 0 to 100, so closed reads 0 and 100 and open stays 50. A fully closed drape keeps the hub's scale, and closing the drape shuts vanes that were closed as far as it allowed. Up to 49. See [SmartDrapes](#smartdrapes). |
| **Two-rail shades: keep the top rail at or above the bottom rail** | on | The collision guard described above. Turn it off only if it stops a position your shade can really reach. |
| **Two-rail shades: minimum gap between the rails** | 0 % | Extra separation the guard keeps, 0–50 %. |
| **Low battery threshold** | 20 % | Battery % at which the `*_battery_low` flag turns on. |
| **Minimum time between shade commands** | 1.5 s | Gap between hub `/control` posts, up to 3 s. The hub has one radio and silently drops commands sent too close together while still reporting success. On a hub with thirteen shades, [kedube/ha-norman](https://github.com/kedube/ha-norman) lost commands at 1.0–1.2 s, and 1.5 s survived several whole-house runs but still lost the odd command over long use. If commands to many shades at once (scenes) get lost, raise it. |
| **Verify the shade arrived and retry once if not** | on | After the delay below, check that the position the hub reports reached what was asked for (lift and vanes on PerfectSheer, both rails on two-rail shades, the lift alone otherwise). If it did not, the command is re-sent once. Before re-sending, it asks that one shade to report its position to the hub and waits up to 10 s, so a shade that arrived but had not yet reported is not sent the command again. Only that one shade is ever asked to report in (see [Limitations](#limitations)). |
| **Seconds to wait before verifying** | 60 s | Minimum 30 s, counted from when the command is sent, which in a scene can be a few seconds after the call. The hub reports where a shade ended up 10–35 s after a command; checking earlier would re-send a command to a shade that is still moving. |
| **Animate the position slider while a shade is moving** | on | Interpolates `current_position` during a lift so the slider moves instead of jumping; the hub's position always wins once the move has settled, and the animation gives up early if the hub's target changes mid-move (someone re-commanded the shade from the app or a remote). |
| **Time for a full open or close** | 28 s | Travel time used by the animation. The default suits the shades this value was measured on; PerfectSheer shades were observed to need roughly 60 s for a full travel (28–35 s for half travels), so set it to about 60 for those. |

## Installation

### Via HACS (custom repository)
Click this badge to add this repository to your HACS custom repositories: [![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kevinlester&repository=ShadeAuto-HomeAssistant&category=integration)

Or: HACS → *Integrations* → ⋯ → **Custom repositories** → add this GitHub repo URL, Category **Integration**.

Then
1. Find **ShadeAuto (local)** → **Download** → **Restart** Home Assistant.
2. Settings → *Devices & Services* → **Add Integration** → **ShadeAuto (local)** → enter your **hub IP**.
3. Repeat for additional hubs.

### Manual
1. Copy `custom_components/shadeauto/` into your HA `config/custom_components/`.
2. Restart HA, then add the integration.

### Upgrading from 0.3.x
Entities, devices and automations are kept. The following changes might be noticed:
- Shades that were called `Shade <id>` now take their name from the ShadeAuto app (renames you made in Home Assistant are kept).
- Devices that were registered under the hub's IP address are moved to the hub's `ThingName` identity on the first start, so they keep their areas and names and survive a later address change. 
- The *Notification timeout* option is gone since it never worked. Polling replaces it, and a new option for proper push updates was added, though it is off by default for continued testing. Enable it and let us know how it works if you are brave!  See [How shade updates reach Home Assistant](#how-shade-updates-reach-home-assistant).
- The verify delay now has a minimum of 30 s.
- **Minimum time between shade commands** now defaults to 1.5 s. If you ever saved the options form on 0.3.x, it stored the old 0.75 s and that value is kept; raise it to 1.5 s.

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

### Two-rail shade: cover the window completely
Bottom rail down, top rail up, as a script (here for a shade called "Den"):
```yaml
alias: Den fully covered
sequence:
  - action: cover.close_cover
    target:
      entity_id: cover.den
  - action: cover.open_cover
    target:
      entity_id: cover.den_top
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
- **"Could not move shade …" when calling a service?** The hub refused the command or did not answer. The shade is shown at the position the hub last reported, nothing is retried, and the message names the shade and the reason. On a two-rail shade, "the hub has not reported where its top rail is" (or bottom rail) means the command was not sent because the other rail's position is not known yet, for example just after a stop; try again once the hub has reported it.
- **Watch what the hub is doing** while you use the app, a remote or Home Assistant:
  ```bash
  python3 tools/hubwatch.py --host HUB_IP --names
  ```
  It prints a line for every position or target change. Add `--notify` to also print the events the hub pushes on its notification stream. `tools/hubwatch.py --help` lists the other modes (sending a single command, dumping raw status).
- **Moves from the app show only after up to 30 s?** That is polling speed, which is the default. Turn on **Use the hub's change notifications (push updates)** in the options to see them within a second or two. If push updates are on and this still happens, the stream is down. A stream that drops after working for at least a minute logs one warning, "Lost the change notifications from the ShadeAuto hub at ...", and an info line, "Change notifications from the ShadeAuto hub at ... are back", once a reopened stream has stayed up for a minute; for every attempt in between, and for a stream that never opened at all, turn on debug logging (below) and look for messages about the notification stream. `python3 tools/hubwatch.py --host HUB_IP --names --notify` (above) shows whether the hub sends events at all. Everything keeps working meanwhile, at polling speed.
- **Logs** (add to `configuration.yaml`):
  ```yaml
  logger:
    logs:
      custom_components.shadeauto: debug
  ```

## Limitations
- The hub reports positions only when a move has **finished** (typically 10–35 s after the command), never while moving. The slider animation and the opening/closing state bridge that gap for lifts; vane changes appear when the hub reports them.
- Moves made with a **physical remote** still depend on the shade reporting to the hub, and the hub learns of them only sometimes; moves from the ShadeAuto app are always reported. When the hub does learn of a remote move, push updates show it at once; when it does not, Home Assistant can only show what the hub knows. Push updates change how quickly Home Assistant hears what the hub knows, not what the hub knows.
- **No hub-wide report-ins.** The hub can ask every shade at once to report its position, which might correct positions it lost track of. The integration deliberately never does that: in [kedube/ha-norman](https://github.com/kedube/ha-norman) issue #5 a hub-wide report-in closed a user's roller shades with no move command sent. It only ever asks one shade at a time, and only before re-sending a command to that shade.
- A shade renamed in the ShadeAuto app keeps its old name in Home Assistant until the integration is reloaded or Home Assistant restarts. The hub announces renames, but this release only re-reads positions when it does.
- Vanes can be tilted only while the shade is fully lowered (a hub rule, see above).
- Only PerfectSheer, roman 48/1 and SmartDrape shades have been tested on real hardware. Two-rail and single-rail support follows other projects' findings (see [Supported shade types](#supported-shade-types)); shutter louvres are not supported.
- `stop_cover_tilt` is exposed, but stopping a shade in the middle of a vane-only move has not been tested separately. It is the same motor and the same stop command as `stop_cover`; what the hub reports for the vanes afterwards is unknown.
- Battery is whatever the hub reports in `BatteryVoltage`, which is a percentage on the hubs seen so far.
- The API is not official; behavior may change with hub firmware updates.

## Developing
See [docs/HUB_PROTOCOL.md](docs/HUB_PROTOCOL.md) for the hub's protocol as observed on real hardware, and [CONTRIBUTING.md](CONTRIBUTING.md) for the test and lint setup (`uv venv --python 3.14 && uv pip install -r requirements_test.txt && .venv/bin/python -m pytest`).

## New feature requests
Open a GitHub issue. Testing may need your help if the developers do not have the hardware.

## Credits & License
- Community discoveries of the local endpoints inspired this integration.
- MIT License
