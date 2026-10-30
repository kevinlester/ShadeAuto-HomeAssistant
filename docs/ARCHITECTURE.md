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

1. A service call lands on `ShadeAutoCover`, which turns it into one command
   for the shade's profile: `(bottom, middle)` on PerfectSheer and two-rail
   shades, `bottom` alone on single-rail and legacy shades (see
   [Shade profiles](#shade-profiles) and [Commands](#commands)).
2. The cover tells the coordinator about the command (`register_command`)
   **before** sending it, so the UI can show the move immediately, then posts
   it through `ShadeAutoApi`, tells the coordinator the post was answered
   (`command_sent`, which starts the command's grace period and verify
   timer), then refreshes the coordinator so the next poll sees the hub's new
   target. If the post fails or the service call is cancelled, the cover
   throws that optimism away again at once (see
   [Failed commands](#failed-commands)).
3. The coordinator polls `/status`, stores the hub's actual and target
   position for every axis the shade's profile tracks, and derives *moving*
   from them.
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
| `cover.py` | the cover entities: per-profile command mapping, tilt rules, the two-rail collision guard, slider animation | `coordinator` |
| `sensor.py`, `binary_sensor.py` | battery entities | `coordinator`, `util.battery_percent` |
| `entity.py` | base class: device info, availability, unique ids, naming | `coordinator` |
| `options.py` | `ShadeAutoOptions`, the typed view of `entry.options` | `const` |
| `config_flow.py` | add a hub, reconfigure its address, edit options | `api`, `options` |
| `__init__.py` | wires everything into `entry.runtime_data` | all of the above |
| `const.py` | every constant, with a comment on why it has that value, including the shade profile tables | nothing |
| `util.py` | small pure helpers (`to_int`, `battery_percent`) | nothing |
| `compat.py` | import-level shims for the oldest supported Home Assistant release (see CONTRIBUTING.md) | Home Assistant only |

Dependencies point downwards only: entities never call the hub directly for
state, and the API never knows about Home Assistant.

## Axes and the hub's truth

The hub knows two commanded axes per shade and reports an **actual** and a
**target** value for each. What the middle axis means depends on the shade's
family ([Shade profiles](#shade-profiles); HUB_PROTOCOL.md 3.1):

| Axis | Hub fields | Meaning |
|---|---|---|
| `bottom` | `BottomRailPosition` / `TargetBottomRailPosition` | bottom rail height: 0 = fully lowered, 100 = fully raised |
| `middle` | `MiddleRailPosition` / `TargetMiddleRailPosition` | PerfectSheer: vane tilt, 0 = closed, 100 = open. Two-rail: the upper rail's height, same axis as `bottom`. Single-rail and legacy: nothing (reads 0), not tracked |

The coordinator keeps one `AxisState` per tracked axis per shade:

```python
@dataclass
class AxisState:
    actual: int | None  # last position the hub reported
    target: int | None  # last target the hub reported
    pending: int | None  # target we sent that the hub has not reflected yet
    pending_ts: float | None  # when its post was answered (time.monotonic()); None while it is queued
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
`reflected`, the fact the verify step needs later), `PENDING_GRACE_SEC` after
the post was answered if the hub never reflects it (the command was lost or
refused), or at once when the `/control` post raises or the service call is
cancelled. The grace runs from `command_sent`, not from `register_command`:
the API sends one command at a time, `send_spacing_sec` apart, so in a scene a
command can wait for its slot longer than the grace (eight shades at the
default 1.5 s, five at the 3 s maximum), and the motion polls during that wait
must not drop its pending target. Once posted, an accepted command shows up
as the hub's target within a second, so the grace only matters for failures.

The poll interval asks a slightly different question than `is_moving`: "is
anything still unsettled?". After a stop `is_moving` is already false, but
the coordinator keeps polling at the motion interval until the hub reports
the stop point (target equals actual on every tracked axis) or the failsafe
elapses.
That is what makes the stopped position show up within a poll or two of the
hub reporting it instead of at the next idle poll.

### Why polling, and why two intervals

The hub reports positions only when a move has finished, never while moving.
0.5.0 learns about that by polling `/status`: every `poll_seconds` when idle,
every `motion_poll_sec` while any tracked axis of any shade is moving.
`_apply_poll_interval` switches `update_interval` after every refresh; the
refresh issued right after a command re-arms the timer at the fast interval
immediately.

Up to 0.4.x this section also said that the hub's `/notification` long-poll
never delivered an event, and 0.4.0 removed the integration's long-poll for
that reason. That was wrong. `/notification` is a stream that the hub never
ends; our clients read it to the end, timed out and threw away what had
arrived (HUB_PROTOCOL.md 2.5; `docs/hub-characterization-2026-10-22.log`, C5;
`docs/hub-characterization-2026-10-23.log`). Read incrementally, it announces
each accepted command and each finished move within a second or two, with the
shade ids only. Push updates on it are planned for 0.6.0: an event triggers a
`/status` read, and polling stays as the fallback, because the stream does not
replay what it missed while disconnected. A per-shade `StatusRequest` before a
retry is planned for the same release.

## Shade profiles

The hub serves very different shades through the same two rail fields, so
every shade gets a **profile** from its `(ModuleType, ModuleDetail)` pair when
the integration sets up. The profile decides which entities the shade has,
which axes the coordinator tracks, and what the cover sends. HUB_PROTOCOL.md
3.1 has the evidence for each family; only `sheer` was verified on hardware
by this project.

`shade_profile()` in `coordinator.py` does the lookup: an exact pair in
`PROFILE_BY_MODULE` first, then the family in `PROFILE_BY_MODULE_TYPE`, else
`legacy`. A missing `ModuleDetail` gives `legacy` for a type that has exact
pairs (32, 48, 49), because which product it is cannot be told; type 33 stays
`two_rail` without one. The tables, with a comment per entry on where it comes from, are in
`const.py`, and so is `PROFILE_AXES`, the axes each profile tracks. The pair
comes from `/GetAllPeripheral` and is completed from the first `/status`; the
profile is fixed after that, because the entities are built from it.

| Profile | `(ModuleType, ModuleDetail)` | Entities | Axes tracked | Payload |
|---|---|---|---|---|
| `sheer` | 48/3, 49/3 (PerfectSheer) | one cover with lift and tilt (tilt unless `tilt_mode` is `off`) | `bottom`, `middle` | always `(bottom, middle)`; [PerfectSheer](#perfectsheer-sheer) |
| `single_rail` | every other detail of 32, 48 and 49: 32/0, 32/2 (cellular), 48/1, 49/1 (roller), 48/2, 49/2 (roman) | one cover, lift only | `bottom` | `bottom` alone, as in 0.3.0; [Single-rail and legacy](#single-rail-and-legacy) |
| `two_rail` | 33/any, 32/3, 32/4, 32/5 (day/night, top-down/bottom-up) | the cover for the bottom rail, plus a "Top" cover for the upper rail | `bottom`, `middle` | always both rails, collision guard; [Two-rail](#two-rail-commands-and-the-collision-guard) |
| `legacy` | 80/any (SmartDrape), 1/any (shutter), 32/48/49 without a `ModuleDetail`, anything unrecognised | one cover, lift only | `bottom` | as `single_rail` |

Why each profile is the way it is:

- **`sheer`** is 0.4.0's behaviour, unchanged. Every rule in
  [Commands](#commands) was measured on these shades.
- **`single_rail`** ignores the middle rail completely. It always reads 0 on
  these shades, but app presets can leave its *target* at 100 (kedube's issue
  #2), and a tracked axis whose target never matches its actual would make the
  shade look like it is moving until the motion failsafe, and make verify
  re-send commands. Bottom-only payloads are what 0.3.0 sent and what upstream
  PR #9 keeps for single-rail shades; the hub's implied middle target of 0 does
  nothing on a shade without a middle rail.
- **`two_rail`** is ported from upstream PR #9, which was tested on one real
  type 33 shade. A bottom-only payload would set the upper rail's target to 0
  and pull it down, so both rails go in every command.
- **`legacy`** is for families with no verified behaviour in this codebase.
  It does the least that can go wrong: lift only, the 0.3.0 payload.
  SmartDrapes and shutters (`KNOWN_UNSUPPORTED_MODULE_TYPES`) get it quietly;
  a pair that is not in the tables at all also logs a warning asking the user
  to report its type, so the tables can grow from real reports.

`tilt_mode` (`auto` or `off`) only matters for `sheer`: `auto` exposes tilt on
those shades, `off` hides it. It replaced 0.4.0's `tilt_enabled`, which put
tilt on every shade whether it had vanes or not; stored values are migrated
(`true` becomes `auto`, `false` becomes `off`).

## Commands

The hub's rules, observed on real PerfectSheer hardware, drive every command
the cover sends:

- `BottomRailPosition` must be in every `/control`; a `MiddleRailPosition`-only
  payload is rejected with `Error 2`.
- A command **without** `MiddleRailPosition` makes the hub reset the middle
  target to 0: the vanes on a PerfectSheer, the upper rail on a two-rail
  shade. This is why the old integration closed the vanes on every lift.
- Vanes are only honoured while the shade is fully lowered: a vane target sent
  with `bottom > 0` is accepted and then silently reset to 0.
- `MotorStop: 170` stops the motor. The hub keeps the old target until it
  reports the stop point, about 35 s later, and only then rewrites the bottom
  target to where the shade stopped. A shade stopped above 0 ends with its
  vanes closed. There is one stop per shade, not one per rail.
- Out-of-range values are stored verbatim, so the client clamps to 0–100.

### PerfectSheer (`sheer`)

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

### Single-rail and legacy

| Service | Sent | Why |
|---|---|---|
| `open_cover` / `close_cover` / `set_cover_position(p)` | `bottom` = 100 / 0 / `p`, no `MiddleRailPosition` | 0.3.0's payload; the hub's implied middle 0 is harmless here |
| `stop_cover` | `MotorStop` | |

No tilt features, and only the bottom axis is registered, tracked and
verified. A `legacy` shade behaves the same; the profile exists so that the
families with no verified behaviour are easy to find and to promote once
someone reports how they behave.

### Two-rail: commands and the collision guard

A two-rail shade has two cover entities on one device. The original cover
(unique id unchanged) drives the **bottom** rail; the "Top" cover drives the
**upper** rail, the hub's `MiddleRailPosition`. Both show rail **height** on the
hub's axis, 0 = lowered, 100 = raised, so on both covers "open" means raise
and "close" means lower. Height alone does not say how much window is
covered: bottom 0 / top 100 is fully covered, 100 / 100 is fully open (both
rails stacked at the top), and bottom 0 / top 40 lets light in above the
fabric. Upstream PR #9 found the axis on real hardware; its first version had
inverted the upper rail and sent "open" to the wrong end.

Every command carries **both** rails, because the hub would set an omitted
`MiddleRailPosition` to 0 and drag the upper rail down. The rail the user did
not touch is restated where it is heading (`coordinator.get_held_position`:
our pending target, else the hub's target, else its actual position), so it
stays put unless the guard has to move it. As in PR #9, if that position is
not known the command is refused with a `HomeAssistantError` ("Could not move
shade Den: the hub has not reported where its top rail is, and every command
has to say where to hold it") rather than sent with a guess.

The held position also counts as unknown after our own stop, for a rail that
was still unsettled and on which the hub has reported nothing new since. The
hub keeps the abandoned target until it reports the stop point (about 35 s on
PerfectSheer, log C4b; 2.8 s on the two-rail shade kedube timed), and
restating that target would restart the move the stop ended, which
kedube/ha-norman saw happen on a two-rail shade. A rail that was not moving
at the stop is still held at its target, and the stall failsafe
(`MOTION_FAILSAFE_SEC`) ends the wait if the hub never reports.

The **collision guard** (`collision_guard`, on by default) keeps the upper rail
at least `rail_min_gap` percent (0–50, default 0) above the bottom rail.
`plan_rail_move()` in `cover.py` computes the pair:

- moving the **bottom** rail to `p`: `p` is capped at `100 - gap`, and the
  upper rail goes to `max(its held position, p + gap)`, so raising the bottom
  rail past the upper rail pushes the upper rail up with it;
- moving the **upper** rail to `p`: `p` is raised to at least `gap`, and the
  bottom rail goes to `min(its held position, p - gap)`, so lowering the upper
  rail past the bottom rail pushes the bottom rail down with it.

| Service | Main cover (bottom rail) | Top cover (upper rail) |
|---|---|---|
| `open_cover` | bottom 100 (`100 - gap` with a gap); the guard drags the top to 100 | top 100; bottom held (lowered to `100 - gap` if it is above) |
| `close_cover` | bottom 0; top held (raised to `gap` if it is below) | top 0 (`gap` with a gap); the guard pushes the bottom down as far as needed |
| `set_cover_position(p)` | bottom `p`, top held or dragged | top `p`, bottom held or pushed |
| `stop_cover` | `MotorStop` for the whole shade | the same `MotorStop` |

With the guard off, the other rail is always held where it is and nothing is
clamped. What the hub or the shade does with a request for crossed rails has
not been reported by anyone, so only turn the guard off if it blocks a
position your shade can really reach. As for PerfectSheer, the command's
targets for both rails are registered, so motion tracking and verify also
cover a rail the guard pushed. Each cover's opening/closing state and slider
animation follow its own rail. Only the commanded cover animates its slider:
a rail the guard drags or pushes shows opening/closing at once, but its
position changes only when the hub reports it. A stop on either cover sends
the shade's single `MotorStop`.

The guard is ported from PR #9; it has not been run against a real two-rail
shade by this project.

### Stop

A stop clears `pending` on both axes and cancels any verify for the shade,
so the entity stops showing opening/closing immediately and the slider
animation ends. If the `MotorStop` post fails, the service raises
`HomeAssistantError` ("Could not stop shade Kitchen: ..."); the stop is not
undone, because it may have reached the hub, so the motion flags stay off
until the hub reports its next change, as after a stop that went through. The coordinator does not stop polling, though: it stays at
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

A service call cancelled while its command waits for its send slot or is in
flight (a script was stopped, Home Assistant is shutting down) is undone the
same way, and the cancellation propagates. Whether the post reached the hub
is unknown; if it did, the hub's own target shows the move from the next
poll. Without the undo the pending target, whose grace starts only once the
post is answered, would keep the shade "moving" indefinitely.

## Slider animation ("estimation")

Because the hub reports end states only, a lift would make the position
slider sit still for up to half a minute and then jump. `ShadeAutoCover`
therefore interpolates `current_cover_position` from the last known position
to the target over `full_travel_time_sec` while the coordinator says the
cover's axis is moving (the bottom rail; the upper rail for a two-rail shade's
Top cover). The estimate is cosmetic: `_handle_coordinator_update` drops it
the moment `is_moving` turns false, and `_estimated_position` (a pure read)
then returns the hub's value. It is also dropped when the hub's target no
longer matches the target we are estimating toward, which means the app or a remote
re-commanded the shade mid-move; from then on the hub's actual is shown. A
failed command clears it too. Turn it off with the `estimation_enabled`
option.

The animation runs as a config-entry background task so it is cancelled on
unload; it also stops when the entity is removed.

## Verify and retry

`verify_delay_sec` after the command's post was answered (`command_sent`; a
command waiting for its send slot has not started its delay yet) the
coordinator refreshes once and checks that the shade arrived: the hub's actual position within `POSITION_TOLERANCE` of our
target on **every axis the command set** (both rails for PerfectSheer and
two-rail shades, the bottom rail alone otherwise). If it did not, the
`reflected` flag decides what the hub's target means:

- the hub had reflected our target earlier (`reflected` set) and now shows a
  different one: someone else re-commanded the shade from the app or a
  remote, or the hub rewrote a vane target itself (vanes above bottom 0).
  Fighting the user would be wrong, so there is no retry;
- otherwise (the hub never reflected our target, so the command was lost, or
  its target is still ours and the shade did not get there) the same payload
  (the `(bottom, middle)` pair, or `bottom` alone on single-rail and legacy
  shades) is re-sent, once.

A single-rail or legacy shade's middle axis is not tracked, so its stuck
middle target never triggers a retry.

The delay cannot go below 30 s: the hub reports end states 10–35 s after a
command, and a retry sent earlier would re-command a shade that is still
travelling. The options form enforces the minimum, and a stored value below
it (possible from 0.3.x, or from a hand-edited entry) is raised to 30 s when
the options are read. A newer command or a stop for the shade cancels the
pending verify, and so does unloading the entry. A retry whose `/control` post is
overtaken by a newer command does not overwrite that command's pending state.
The timer is an `async_call_later` callback.

## Options

`ShadeAutoOptions` is parsed once per entry load; the options flow derives
from `OptionsFlowWithReload`, so saving the form reloads the entry.

| Option | Used by | Effect |
|---|---|---|
| `poll_seconds` | coordinator | idle poll interval |
| `motion_poll_sec` | coordinator | poll interval while anything moves |
| `tilt_mode` | cover | `auto`: tilt features and `current_tilt_position` on `sheer` shades; `off`: no tilt anywhere. Replaces `tilt_enabled` (migrated) |
| `collision_guard`, `rail_min_gap` | cover | the two-rail collision guard and its minimum gap, 0–50 % (on, 0) |
| `low_battery_threshold` | binary_sensor | when "battery low" turns on |
| `send_spacing_sec` | api | minimum gap between `/control` posts; default 1.5 s, from kedube/ha-norman's 13-shade measurements (HUB_PROTOCOL.md 4.8) |
| `verify_enabled`, `verify_delay_sec` | coordinator | verify-and-retry (delay is at least 30 s) |
| `estimation_enabled`, `full_travel_time_sec` | cover | slider animation |

## Entities, devices and ids

| Entity | Exists for | Unique id | Name |
|---|---|---|---|
| `cover.<shade>` | every shade | `shadeauto_<identity>_<uid>` | none of its own; shows the device name |
| `cover.<shade>_top` | `two_rail` shades | `shadeauto_<identity>_<uid>_middle` | "Top" (translated), shown as "<shade> Top" |
| `sensor.<shade>_battery` | every shade | `shadeauto_<identity>_<uid>_battery` | "Battery" (translated) |
| `binary_sensor.<shade>_battery_low` | every shade | `shadeauto_<identity>_<uid>_battery_low` | "Battery low" (translated) |

`<identity>` is the hub's `ThingName`, or its host if the hub has none.

- Unique ids must never change; they are what keeps users' entity ids, areas
  and renames across upgrades. The bottom-rail cover of a two-rail shade keeps
  the plain id it had before 0.5.0, and the Top cover uses the `_middle`
  suffix that upstream PR #9 chose.
- Each shade is a device named after the hub's `PeripheralName`, with the
  model derived from `ModuleType`/`ModuleDetail` (`KNOWN_MODELS` in
  `entity.py`) and the motor firmware as `sw_version`, hanging off the hub
  device via `via_device_id`.
- Devices are identified by the hub's `ThingName` (`coordinator.identity`),
  never by the host, so Reconfigure (a host change) keeps devices, areas and
  user renames. Devices that 0.3.x created with host-based identifiers are
  migrated to the `ThingName` identity once, at setup.
- Entities use `has_entity_name`: the main cover has no name of its own (it
  shows the device name); the Top cover and the battery entities add a
  translated suffix from `translations/en.json`.
- Supported features depend on the profile: lift and stop on every cover,
  tilt only on `sheer` shades with `tilt_mode` `auto`.

## Adding things

**A new option.** Add the key and default to `const.py`, a field to
`ShadeAutoOptions` (with parsing in `from_mapping`), a selector in
`ShadeAutoOptionsFlow`, a label in `translations/en.json`, and read it from
`coordinator.options` wherever it applies. Add a round-trip assertion to
`tests/test_config_flow.py::test_options_flow_reloads_entry_with_new_values`.

**A new shade family.** Add its pair to `PROFILE_BY_MODULE` (one product) or
its `ModuleType` to `PROFILE_BY_MODULE_TYPE` (a whole family) in `const.py`,
with the profile that matches its rails and a comment naming the evidence, and
record that evidence (a `/status` record, what each rail does) in
HUB_PROTOCOL.md 3.1. Add the pair to `test_profile_table` in
`tests/test_profiles.py`, and to `hub_family` in `tests/conftest.py` if the
hub moves its rails differently from the families `FakeHub` already models.
If no existing profile fits, a new profile needs its own command mapping in
`cover.py`, an entry in `PROFILE_AXES`, `FakeHub` rules for that family
(tests add the shade with `FakeHub.add_shade`; the fixture files stay real
captures), and a real `/status` record once someone can capture one.

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
  `control_gate` holds `/control` posts in flight until a test releases them,
  for commands that overlap. Prefer extending it over mocking methods.
- The fixtures hold only PerfectSheer shades. `FakeHub.add_shade(uid, name,
  module_type, module_detail, bottom=, middle=, target_middle=)` adds a
  shade of another family to `/GetAllPeripheral` and `/status` before setup,
  and `FakeHub.family()` / `hub_family()` pick the per-family `/control`,
  settle and stop rules described in the `FakeHub` docstring. `hub_family`
  is kept apart from the integration's tables on purpose, so
  `test_profile_table` checks one against the other.
- Under the `freezer` fixture `time.monotonic()` is frozen **and**
  `asyncio.sleep()` never returns until time is advanced. Drive time with
  `freezer.tick()` + `async_fire_time_changed()`; schedule anything time-based
  in the integration with `async_call_later`, never `asyncio.sleep`, and do
  not `await hass.async_block_till_done(wait_background_tasks=True)` while an
  animation task is alive.
- Tests that move shades assert on `hub.commands` (what was sent) and on the
  entity state (what the user sees); both matter.
