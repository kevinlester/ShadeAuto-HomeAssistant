# ShadeAuto (local) — Home Assistant Custom Integration

Control Norman **ShadeAuto** shades locally through the hub's undocumented HTTP API: lift, **vane tilt** on PerfectSheer sheer shades, **both rails** of top-down/bottom-up and day/night shades, stop, and battery monitoring, with all the usual `cover` UI features and **instant updates** from the hub's change notifications.

---
> ⚠️ This uses **undocumented** local endpoints (`/NM/v1/*`). Firmware may change behavior. Isolate the hub and restrict access to TCP **10123**.
---

## Features
- **Auto-discover shades** from any ShadeAuto hub(s); works with **multiple hubs** (add each hub by IP/hostname).
- **Each shade is driven the way its type needs.** The hub reports a type for every shade; the integration picks a profile from it (see [Supported shade types](#supported-shade-types)):
  - **PerfectSheer**: lift plus **vane tilt**.
  - **Single-rail** shades (cellular, roller, roman): lift only.
  - **Two-rail** shades (top-down/bottom-up, day/night): one cover per rail, with a guard that keeps the rails from crossing.
- One **Device** per shade, named as in the ShadeAuto app, showing model and motor firmware.
  - A **Cover** entity: open / close / **set position** (0–100) / **stop**, plus **tilt** (open / close / set / stop) on PerfectSheer shades. Two-rail shades get a second **Top** cover.
  - One **Battery (%)** sensor and one **Battery low** flag per shade.
- **The hub is the source of truth.** Positions and vane state always converge to what the hub reports, including moves made from the ShadeAuto app or a remote.
- **Instant updates.** The integration listens to the hub's change notifications, so Home Assistant follows a shade within a second or two of the hub hearing about it: the shade shows as opening or closing as soon as the hub takes a command, whether from Home Assistant or the ShadeAuto (Norman) app, and its new position appears as soon as the shade reports where it stopped. A move made with a physical remote shows when the shade reports in to the hub, which it does not always do (see [Limitations](#limitations)).
- **Polling as the safety net.** The hub is still polled every 30 s, and every couple of seconds while a shade moves (both configurable), so nothing is lost when the notifications stop; see [How updates reach Home Assistant](#how-updates-reach-home-assistant).

## Entities per shade
- `cover.<shade>`: `current_position` 0 = fully lowered, 100 = fully raised (on a two-rail shade, the bottom rail). PerfectSheer only: `current_tilt_position` 0 = vanes closed, 100 = vanes open (light through).
- `cover.<shade>_top`: two-rail shades only, the top rail; 0 = lowered, 100 = raised.
- `sensor.<shade>_battery`: battery percent (diagnostic).
- `binary_sensor.<shade>_battery_low`: low-battery flag, threshold in Options (diagnostic).

## Supported shade types
The hub reports every shade's type as a `ModuleType`/`ModuleDetail` pair; the device page shows it as the model (for example "PerfectSheer", or "Shade (type 33/3)"). Only PerfectSheer has been tested with this integration on real hardware. The other rows rely on the sources named, so if you own one of those shades, please report how it behaves.

| Shade | Type | Handled as | How it was verified |
|---|---|---|---|
| PerfectSheer | 48/3 | lift + vane tilt | This project, five shades on a real hub (`docs/hub-characterization-2026-10-22.log`). |
| PerfectSheer (second generation) | 49/3 | lift + vane tilt | Not verified. The ShadeAuto app calls it PerfectSheer ([kedube/ha-norman](https://github.com/kedube/ha-norman)'s reading of the app). |
| Cellular | 32/2 | single-rail | Real shades in kedube/ha-norman. Not tested with this integration. |
| Cellular | 32/0 | single-rail | The ShadeAuto app only (kedube/ha-norman). |
| Roller | 48/1 | single-rail | Users' diagnostics in kedube/ha-norman issues #2 and #5. Not tested with this integration. |
| Roman; second-generation roller and roman | 48/2; 49/1, 49/2 | single-rail | The ShadeAuto app only (kedube/ha-norman). |
| Other products of those families | 32, 48 or 49 with any other detail | single-rail | Not seen anywhere; handled like the rest of the family, without a warning. |
| Top-down/bottom-up or day/night cellular | 33/3 | two-rail | Real shades in kedube/ha-norman. This port has not been tested on hardware. |
| Top-down/bottom-up cellular | 33/other, 32/3, 32/4, 32/5 | two-rail | The ShadeAuto app only (kedube/ha-norman). |
| SmartDrape | 80/any | lift only | One user's diagnostics in kedube/ha-norman issue #2. The drape's vanes are not supported. |
| Shutter | 1/any | lift only | The ShadeAuto app only. Shutters have louvres instead of rails, which are not supported; the lift commands probably do nothing. |
| Anything else | other types, or 32, 48 or 49 reported without a detail | lift only | A warning in the log asks you to report the type. Please open an issue with the pair and what the shade is. |

The two-rail handling follows [upstream PR #9](https://github.com/kevinlester/ShadeAuto-HomeAssistant/pull/9), which was tested on one real type-33 shade; the PR does not say which `ModuleDetail` it had.

`docs/HUB_PROTOCOL.md` (section 3.1) has the details and the evidence for each family.

## How tilt works on PerfectSheer shades
Sheer (PerfectSheer) shades can only tilt their vanes while the shade is **fully lowered** — the hub enforces this and closes the vanes whenever the shade is raised. The integration follows the hub:

- `cover.open_cover_tilt`, `cover.close_cover_tilt` and `cover.set_cover_tilt_position` **lower the shade if it is not already down**, then set the vanes.
- Raising the shade to any height closes the vanes; lowering it fully (`close_cover` or position 0) keeps whatever vane setting it had.
- `stop_cover` and `stop_cover_tilt` both stop the motor. Home Assistant stops showing the shade as opening or closing right away; the position it actually stopped at appears a couple of seconds after the hub reports it (about 35 s after the stop on the hubs seen so far). A shade stopped above the bottom ends with its vanes closed.

Only PerfectSheer shades get tilt controls. To hide them on those too, set **Vane tilt** to **Off** in the integration's options; the commands sent stay the same, so this never closes anyone's vanes.

## Two-rail shades (top-down/bottom-up, day/night)
These shades have two rails that move independently, and each gets its own cover on the shade's device:

- `cover.<shade>` drives the **bottom** rail and `cover.<shade>_top` the **top** rail.
- Both positions are rail **heights**: 0 = lowered, 100 = raised. So on both covers *open* means raise and *close* means lower, which is not the same as letting light in: bottom 0 / top 100 covers the window completely, both at 100 leaves it fully open, and bottom 0 / top 40 lets light in above the fabric.
- The hub needs both rails in every command, so moving one rail always restates where the other one is heading (the target last sent to it, else the hub's target, else its position). When that is not known, the move is refused with an error rather than sent with a guess: if the hub has not reported the other rail, and after a stop until the hub reports where the shade stopped, because restating the target the hub still holds would restart the move you just stopped.
- The **collision guard** (on by default) keeps the top rail at or above the bottom rail. If a move would cross them, the other rail moves just far enough out of the way: raising the bottom rail past the top rail raises the top rail with it, and lowering the top rail past the bottom rail lowers the bottom rail with it. **Minimum gap between the rails** adds a margin the guard keeps between them; with a gap of 10 %, for example, the bottom rail goes no higher than 90 % and the top rail no lower than 10 %. With the guard off, the other rail is held where it is and the move is sent as asked.
- Stop on either cover stops the whole shade (the hub has one stop command per shade).

This follows upstream PR #9, which was tested on one real shade. This integration has not been run against a two-rail shade yet; please report how yours behaves.

## How updates reach Home Assistant
The hub has a notification stream: a connection it keeps open and on which it sends a short message whenever a shade's target or position changes. The message says which shade changed but not what changed, so the integration reads the hub's status as soon as one arrives (messages that arrive together share one read).

- **While the stream is open**, a change shows within a second or two: a command the hub takes from Home Assistant or the app (the shade starts showing as opening or closing), the end of every move the hub hears about (the new position), and a shade reporting in.
- **The stream is replaced every 5 minutes**, even when it looks fine, because long-lived streams have been seen to go quiet without an error.
- **When the stream is down** (the hub restarted, its Wi-Fi dropped, the connection broke), the integration keeps trying to reopen it, waiting a little longer after each failed attempt, up to a minute. A stream counts as working again only once it has stayed open for a minute, so one the hub closes as soon as it is opened is treated as a failed attempt too. Meanwhile polling works exactly as in 0.5.0: every 30 s while idle, every couple of seconds while a shade moves. The hub does not resend what happened while the stream was down, so the integration reads the full status as soon as the stream is back. Nothing is lost; it just shows up at the next poll instead of at once.
- **With push updates turned off** (see [Options](#options)), the integration never opens the stream and learns about changes by polling alone, as 0.5.0 did. Verify still asks a shade that has not arrived to report in before re-sending its command (see the verify option); turn verify off to send no such request at all.

## Requirements
- ShadeAuto hub reachable on your LAN (port **10123**).
- Home Assistant **2025.8** or newer. Older releases stay on 0.3.0 through HACS; see CONTRIBUTING.md for how the minimum is chosen.

## Changing the hub address
If your hub gets a new IP address, open Settings → *Devices & Services* → **ShadeAuto**, click the ⋯ menu next to the hub entry and choose **Reconfigure**. Devices, areas, names, entity ids and automations are kept: the integration identifies the hub by its `ThingName`, not by its address.

## Options
Settings → *Devices & Services* → **ShadeAuto** → **Configure**. Saving reloads the integration so changes take effect at once.

- **Use the hub's change notifications (push updates)** (`push_enabled`): listen to the hub's notification stream and read the hub's status as soon as it announces a change (default off). Off: the stream is never opened and Home Assistant learns about changes by polling alone, as in 0.5.0. Polling runs either way. It does not turn off verify's report-in request (see **Verify the shade arrived and retry once**), which then always waits the full 10 s.
- **Poll interval while idle**: `/status` poll interval while nothing is moving (default 30 s). With push updates on, this is the safety net for anything the notifications miss.
- **Poll interval while a shade is moving**: faster polling during a move (default 2 s). The hub only reports a position once a move has finished. With push updates on, its notification usually brings that news first; this poll catches it when the stream is down.
- **Vane tilt** (`tilt_mode`): **Automatic** shows tilt controls and `current_tilt_position` on PerfectSheer shades, the only ones with vanes; **Off** hides them everywhere (default Automatic).
- **Two-rail shades: keep the top rail at or above the bottom rail** (`collision_guard`): the collision guard described above (default on). Turn it off only if it stops a position your shade can really reach.
- **Two-rail shades: minimum gap between the rails** (`rail_min_gap`): extra separation the guard keeps, 0–50 % (default 0 %).
- **Low battery threshold**: battery % at which the `*_battery_low` flag turns on (default 20 %).
- **Minimum time between shade commands**: gap between hub `/control` posts (default 1.5 s, up to 3 s). The hub has one radio and silently drops commands sent too close together while still reporting success. On a hub with thirteen shades, [kedube/ha-norman](https://github.com/kedube/ha-norman) lost commands at 1.0–1.2 s, and 1.5 s survived several whole-house runs but still lost the odd command over long use. If commands to many shades at once (scenes) get lost, raise it.
- **Verify the shade arrived and retry once**: after the delay below, check that the position the hub reports reached what was asked for (lift and vanes on PerfectSheer, both rails on two-rail shades, the lift alone otherwise). If it did not, the command is re-sent once, whether the hub never took it (its target never matched ours) or took it and the shade did not get there. The exception is a hub that had taken the command and whose target has changed since: the shade was re-commanded from the app or a remote in the meantime, and nothing is re-sent. Before re-sending, the integration asks that one shade to report its position to the hub (`StatusRequest`) and waits up to 10 s for the answer (the full 10 s with push updates off); a shade that had arrived but not yet told the hub is not sent the command again. If the report leaves the hub's target at the shade's position, which the hub is reported to do, that does not count as a re-command, and the command is still re-sent. Only that shade is asked, never every shade on the hub at once (see [Limitations](#limitations)). The request goes out whenever the shade is short of its target at the verify delay, so with a short delay and a long move it can reach a shade that is still travelling; nothing moved when it was sent to a shade at rest, but a moving shade has not been tried. Turning this option off sends no such request.
- **Seconds to wait before verifying** (default 60 s, minimum 30 s), counted from when the command is sent, which in a scene can be a few seconds after the call. The hub reports where a shade ended up 10–35 s after a command; checking earlier would re-send a command to a shade that is still moving.
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

### Upgrading from 0.5.x
Nothing to do. Push updates are on after the upgrade; entity ids, unique ids, devices and your options are kept. To go back to polling only, turn off **Use the hub's change notifications (push updates)** in the options.

### Upgrading from 0.4.x
Everything under 0.5.x applies, and also:
- Home Assistant **2025.8** or newer is enough now (0.4.0 needed 2026.10).
- Entity ids and unique ids of existing entities are unchanged.
- **Shades without vanes lose their tilt controls.** 0.4.x showed tilt on every shade unless *Expose vane tilt* was off; now only PerfectSheer shades have it. Remove tilt actions aimed at other shades from automations and scripts, or they will fail. Your old *Expose vane tilt* setting becomes **Vane tilt**: on is Automatic, off is Off.
- **Two-rail shades** get a new `cover.<shade>_top` entity, and the existing cover now drives only the bottom rail. In 0.4.x the existing cover treated the top rail as vane tilt and sent it to 0 with every raise; check automations and scenes that used tilt on these shades and move them to the Top cover.
- **Single-rail shades** are sent bottom-only commands again, as 0.3.0 did.
- **Minimum time between shade commands** now defaults to 1.5 s. If you ever saved the options form, it stored the old default of 0.75 s and that value is kept; raise it to 1.5 s unless you have a reason not to.
- A stored verify delay below 30 s is raised to 30 s.

### Upgrading from 0.3.x
Everything under 0.4.x applies, and also: shades that were still called "Shade <id>" take their name from the ShadeAuto app (renames you made in Home Assistant are kept). Devices that 0.3.x registered under the hub's IP address are moved to the hub's `ThingName` identity on the first start, so they keep their areas and names and survive a later address change. The *Notification timeout* option is gone; push updates replace 0.3.x's notification watcher, which never showed an event (see [How updates reach Home Assistant](#how-updates-reach-home-assistant)). The verify delay now has a minimum of 30 s.

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
- **Moves from the app show only after up to 30 s?** Then the integration is not getting the hub's notifications. Check that **Use the hub's change notifications (push updates)** is on in the options. A stream that drops after working for at least a minute logs one warning, "Lost the change notifications from the ShadeAuto hub at ...", and an info line, "Change notifications from the ShadeAuto hub at ... are back", once a reopened stream has stayed up for a minute; for every attempt in between, and for a stream that never opened at all, turn on debug logging (below) and look for messages about the notification stream. `python3 tools/hubwatch.py --host HUB_IP --names --notify` (above) shows whether the hub sends events at all. Everything keeps working meanwhile, at polling speed.
- **Logs** (add to `configuration.yaml`):
  ```yaml
  logger:
    logs:
      custom_components.shadeauto: debug
  ```

## Limitations
- The hub reports positions only when a move has **finished** (typically 10–35 s after the command), never while moving. The slider animation bridges that gap for lifts; vane changes appear when the hub reports them.
- Moves made with a **physical remote** still depend on the shade reporting to the hub, and the hub learns of them only sometimes; moves from the ShadeAuto app are always reported. When the hub does learn of a remote move, push updates show it at once; when it does not, Home Assistant can only show what the hub knows. Push updates change how quickly Home Assistant hears what the hub knows, not what the hub knows.
- **No hub-wide report-ins.** The hub can ask every shade at once to report its position, which might correct positions it lost track of. The integration deliberately never does that: in [kedube/ha-norman](https://github.com/kedube/ha-norman) issue #5 a hub-wide report-in closed a user's roller shades with no move command sent. It only ever asks one shade at a time, and only before re-sending a command to that shade.
- A shade renamed in the ShadeAuto app keeps its old name in Home Assistant until the integration is reloaded or Home Assistant restarts. The hub announces renames, but this release only re-reads positions when it does.
- Vanes can be tilted only while the shade is fully lowered (a hub rule, see above).
- Only PerfectSheer shades have been tested on real hardware. Two-rail and single-rail support follows other projects' findings (see [Supported shade types](#supported-shade-types)); SmartDrape vanes and shutter louvres are not supported.
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
