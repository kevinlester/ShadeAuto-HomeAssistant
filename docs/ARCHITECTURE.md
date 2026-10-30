# Architecture

How the integration is put together, what each piece is responsible for, and
the rules that make shade motion behave. Read this before changing anything in
`custom_components/shadeauto/`. The hub's wire protocol is documented
separately in [HUB_PROTOCOL.md](HUB_PROTOCOL.md).

## Data flow

```
 Home Assistant service call            poll timer (30 s idle / 2 s moving)
   cover.set_cover_tilt_position 50               |
            |                                     v
            v                              ShadeAutoCoordinator
       ShadeAutoCover ── register_command ──>  per-shade AxisState
            |                                (actual, target, pending)
            |  api.control(bottom=0, middle=50)     ^
            v                                      | /status every poll
       ShadeAutoApi ───── POST /NM/v1/control ────> hub ─── RF ──> shade motor
            |                                      |
            └── await coordinator.async_refresh() ─┘
                                                   |
                   entities read get_position / get_target / is_moving
```

1. A service call lands on `ShadeAutoCover`, which turns it into one
   *composite* command `(bottom, middle)` (see [Commands](#commands)).
2. The cover tells the coordinator about the command (`register_command`)
   **before** sending it, so the UI can show the move immediately, then posts
   it through `ShadeAutoApi`, then refreshes the coordinator so the next poll
   sees the hub's new target. If the post fails, the cover throws that
   optimism away again at once (see [Failed commands](#failed-commands)).
3. The coordinator polls `/status`, stores the hub's actual and target
   position for both axes of every shade, and derives *moving* from them.
4. Entities render whatever the coordinator holds. Nothing else keeps state.

Setup runs once before the first poll, in Home Assistant's `_async_setup`
hook of the coordinator: `/registration` for the hub's `ThingName`, then
`/GetAllPeripheral` for the list of shades. Anything that goes wrong there is
a connection error and raises `ConfigEntryNotReady`, so Home Assistant retries
setup later. That includes `/GetAllPeripheral` answering with its error
envelope or with a body that is not JSON; the integration never loads with
zero shades because of a bad reply.

## Modules

| Module | Responsibility | Knows about |
|---|---|---|
| `api.py` | HTTP client: endpoints, payload shape, pacing, reply checking, exceptions | the hub protocol only |
| `coordinator.py` | polling, per-axis motion state, adaptive interval, verify-and-retry | `api`, `options` |
| `cover.py` | the cover entity: command mapping, tilt rules, slider animation | `coordinator` |
| `sensor.py`, `binary_sensor.py` | battery entities | `coordinator`, `util.battery_percent` |
| `entity.py` | base class: device info, availability, unique ids, naming | `coordinator` |
| `options.py` | `ShadeAutoOptions`, the typed view of `entry.options` | `const` |
| `config_flow.py` | add a hub, reconfigure its address, edit options | `api`, `options` |
| `__init__.py` | wires everything into `entry.runtime_data` | all of the above |
| `const.py` | every constant, with a comment on why it has that value | nothing |
| `util.py` | small pure helpers (`to_int`, `battery_percent`) | nothing |
| `compat.py` | import-level shims for the oldest supported Home Assistant release (see CONTRIBUTING.md) | Home Assistant only |

Dependencies point downwards only: entities never call the hub directly for
state, and the API never knows about Home Assistant.

## Axes and the hub's truth

A shade has two commanded axes. The hub reports an **actual** and a
**target** value for each:

| Axis | Hub fields | Meaning |
|---|---|---|
| `bottom` | `BottomRailPosition` / `TargetBottomRailPosition` | lift: 0 = fully lowered, 100 = fully raised |
| `middle` | `MiddleRailPosition` / `TargetMiddleRailPosition` | vane tilt on sheer shades: 0 = closed, 100 = open |

The coordinator keeps one `AxisState` per axis per shade:

```python
@dataclass
class AxisState:
    actual: int | None  # last position the hub reported
    target: int | None  # last target the hub reported
    pending: int | None  # target we sent that the hub has not reflected yet
    pending_ts: float | None  # when we sent it (time.monotonic())
    changed_ts: float | None  # when actual/target last changed
    reflected: bool  # the hub's target matched our last command at least once
```

**The hub is the source of truth.** There is deliberately no code path that
returns a position we merely hoped for once the hub disagrees. The only
optimism is `pending`; it is bounded in time and dropped the moment a command
fails.

### What "moving" means

`is_moving(uid, axis)` is true when:

- we sent a command and the hub has not yet echoed it as its target
  (`pending` set), or
- the hub's target differs from its actual by more than
  `POSITION_TOLERANCE`,

and false again when:

- the hub's target and actual agree, or
- we sent a stop (the shade is no longer going anywhere, even though the hub
  keeps the old target until it reports the stop point, ~35 s later), or
- nothing has changed for `MOTION_FAILSAFE_SEC` (a stalled or unreachable
  shade is not a move), or
- the command failed (see [Failed commands](#failed-commands)).

`pending` is cleared when the hub's reported target equals it (which also sets
`reflected`, the fact the verify step needs later), after `PENDING_GRACE_SEC`
if the hub never reflects it (the command was lost or refused), or at once
when the `/control` post raises. The hub reflects accepted commands within a
second, so the grace period only matters for failures.

The poll interval asks a slightly different question than `is_moving`: "is
anything still unsettled?". After a stop `is_moving` is already false, but
the coordinator keeps polling at the motion interval until the hub reports
the stop point (target equals actual on both axes) or the failsafe elapses.
That is what makes the stopped position show up within a poll or two of the
hub reporting it instead of at the next idle poll.

### Why polling, and why two intervals

The hub reports positions only when a move has finished — never while
moving — and its `/notification` long-poll never delivered an event in
testing (not for our commands, the vendor app, or a remote). So the
coordinator polls `/status`: every `poll_seconds` when idle, every
`motion_poll_sec` while any axis of any shade is moving. `_apply_poll_interval`
switches `update_interval` after every refresh; the refresh issued right after
a command re-arms the timer at the fast interval immediately.

## Commands

The hub's rules, observed on real hardware, drive every command the cover
sends:

- `BottomRailPosition` must be in every `/control`; a `MiddleRailPosition`-only
  payload is rejected with `Error 2`.
- A command **without** `MiddleRailPosition` makes the hub reset the vane
  target to 0. This is why the old integration closed the vanes on every
  lift.
- Vanes are only honoured while the shade is fully lowered: a vane target sent
  with `bottom > 0` is accepted and then silently reset to 0.
- `MotorStop: 170` stops the motor. The hub keeps the old target until it
  reports the stop point, about 35 s later, and only then rewrites the bottom
  target to where the shade stopped. A shade stopped above 0 ends with its
  vanes closed.
- Out-of-range values are stored verbatim, so the client clamps to 0–100.

Hence the mapping in `cover.py`:

| Service | Sent as `(bottom, middle)` | Why |
|---|---|---|
| `open_cover` | `(100, 0)` | raised shades cannot have open vanes |
| `close_cover` | `(0, current vane target)` | lowering fully keeps the vanes as they are |
| `set_cover_position(p)` | `(p, current vane target)` if `p == 0`, else `(p, 0)` | same two rules |
| `open_cover_tilt` / `close_cover_tilt` / `set_cover_tilt_position(t)` | `(0, t)` | vanes only work at the bottom, so tilting lowers the shade (as HA's PowerView integration does for Silhouette shades) |
| `stop_cover` / `stop_cover_tilt` | `MotorStop` | one motor; either stop stops it (a stop during a vane-only move was not separately tested) |

`register_command` marks **both** axes pending, because every command sets
both targets on the hub. `is_closed` is `position == 0` regardless of tilt,
matching Home Assistant's cover model. `is_opening`/`is_closing` derive from
the **bottom** axis only: a vane change does not make a closed shade "opening".

### Stop

A stop clears `pending` on both axes and cancels any verify for the shade,
so the entity stops showing opening/closing immediately and the slider
animation ends. The coordinator does not stop polling, though: it stays at
the motion interval until the hub reports the stop point (target equals
actual) or `MOTION_FAILSAFE_SEC` elapses, so the position the shade actually
stopped at appears within a couple of seconds of the hub reporting it (about
35 s after the stop on the real hub).

### Failed commands

When `/control` raises, either a connection error or a reply with
`Error != 0`, the cover discards the optimistic state immediately: `pending`
is cleared on both axes, the verify is cancelled, and the estimate and its
animation task are dropped. The entity goes back to showing the hub's last
known position, and the service call raises `HomeAssistantError` naming the
shade, for example "Could not move shade Kitchen: ...". A failed command never
leaves the entity in "opening".

## Slider animation ("estimation")

Because the hub reports end states only, a lift would make the position
slider sit still for up to half a minute and then jump. `ShadeAutoCover`
therefore interpolates `current_cover_position` from the last known position
to the target over `full_travel_time_sec` while the coordinator says the
bottom axis is moving. The estimate is cosmetic: `_handle_coordinator_update`
drops it the moment `is_moving` turns false, and `_estimated_position` (a
pure read) then returns the hub's value. It is also dropped when the hub's target no longer matches the
target we are estimating toward, which means the app or a remote
re-commanded the shade mid-move; from then on the hub's actual is shown. A
failed command clears it too. Turn it off with the `estimation_enabled`
option.

The animation runs as a config-entry background task so it is cancelled on
unload; it also stops when the entity is removed.

## Verify and retry

After `verify_delay_sec` the coordinator refreshes once and compares the
hub's target with ours on **both** axes. For an axis whose hub target differs
from ours, the `reflected` flag decides what that difference means:

- the hub had reflected our target earlier (`reflected` set) and now shows a
  different one: someone else re-commanded the shade from the app or a
  remote, or the hub rewrote a vane target itself (vanes above bottom 0).
  Fighting the user would be wrong, so there is no retry;
- the hub never reflected our target: the command was lost, so the same
  `(bottom, middle)` pair is re-sent, once.

The delay cannot go below 30 s: the hub reports end states 10–35 s after a
command, and a retry sent earlier would re-command a shade that is still
travelling. A newer command or a stop for the shade cancels the pending
verify, and so does unloading the entry. A retry whose `/control` post is
overtaken by a newer command does not overwrite that command's pending state.
The timer is an `async_call_later` callback.

## Options

`ShadeAutoOptions` is parsed once per entry load; the options flow derives
from `OptionsFlowWithReload`, so saving the form reloads the entry.

| Option | Used by | Effect |
|---|---|---|
| `poll_seconds` | coordinator | idle poll interval |
| `motion_poll_sec` | coordinator | poll interval while anything moves |
| `tilt_enabled` | cover | expose tilt features and `current_tilt_position` |
| `low_battery_threshold` | binary_sensor | when "battery low" turns on |
| `send_spacing_sec` | api | minimum gap between `/control` posts |
| `verify_enabled`, `verify_delay_sec` | coordinator | verify-and-retry (delay is at least 30 s) |
| `estimation_enabled`, `full_travel_time_sec` | cover | slider animation |

## Entities, devices and ids

- Unique ids are `shadeauto_<ThingName or host>_<uid>[_battery|_battery_low]`
  and must never change; they are what keeps users' entity ids, areas and
  renames across upgrades.
- Each shade is a device named after the hub's `PeripheralName`, with the
  model derived from `ModuleType`/`ModuleDetail` (`KNOWN_MODELS` in
  `entity.py`) and the motor firmware as `sw_version`, hanging off the hub
  device via `via_device_id`.
- Devices are identified by the hub's `ThingName` (`coordinator.identity`),
  never by the host, so Reconfigure (a host change) keeps devices, areas and
  user renames. Devices that 0.3.x created with host-based identifiers are
  migrated to the `ThingName` identity once, at setup.
- Entities use `has_entity_name`: the cover has no name of its own (it shows
  the device name), the battery entities add a translated suffix from
  `translations/en.json`.

## Adding things

**A new option.** Add the key and default to `const.py`, a field to
`ShadeAutoOptions` (with parsing in `from_mapping`), a selector in
`ShadeAutoOptionsFlow`, a label in `translations/en.json`, and read it from
`coordinator.options` wherever it applies. Add a round-trip assertion to
`tests/test_config_flow.py::test_options_flow_reloads_entry_with_new_values`.

**A new hub field.** Add its name to `const.py`, include it in
`coordinator.STATUS_FIELDS` if entities need it, and extend the `FakeHub` in
`tests/conftest.py` so tests see it. Capture a real `/status` with
`tools/hubwatch.py --raw` and update `tests/fixtures/status.json` (scrub
nothing but identity; the fixtures must stay real).

**A new command.** Add the field name to `const.py`, a method to
`ShadeAutoApi` that goes through `_send_control`, teach `FakeHub._control`
how the hub reacts, then wire the entity. Check the hub's reaction with
`tools/hubwatch.py --control` first and record it in `HUB_PROTOCOL.md`.

## Testing notes

- `tests/conftest.py` serves real hub responses through Home Assistant's
  aiohttp mocker and keeps live per-shade state; `FakeHub` applies the same
  rules as the hub (see its docstring). Its knobs (`settle_after_polls`,
  `stuck`, `ignore_commands`, `reject`, `offline`, `external_move()`,
  `external_command()`) cover slow, lost, refused and overtaken commands.
  Prefer extending it over mocking methods.
- Under the `freezer` fixture `time.monotonic()` is frozen **and**
  `asyncio.sleep()` never returns until time is advanced. Drive time with
  `freezer.tick()` + `async_fire_time_changed()`; schedule anything time-based
  in the integration with `async_call_later`, never `asyncio.sleep`, and do
  not `await hass.async_block_till_done(wait_background_tasks=True)` while an
  animation task is alive.
- Tests that move shades assert on `hub.commands` (what was sent) and on the
  entity state (what the user sees); both matter.
