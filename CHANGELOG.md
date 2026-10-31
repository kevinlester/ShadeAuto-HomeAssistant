# Changelog

All notable changes to this integration are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed
- A cover reads as idle at its estimated target once the estimated travel
  time has elapsed, as 0.3.0 did, instead of staying "opening" or "closing"
  until the hub hears from the shade (10-35 s after it stops, longer on some
  shades). The hub's report still corrects the position when it arrives.
  Only the opening/closing state changed; the slider already behaved this way.
- Push updates (`push_enabled`) are off by default. The stream has been
  observed for minutes on one hub so far and holds a connection to the hub
  open for as long as the integration runs, so it is opt-in until it has
  more hours on real hubs. Nothing else about it changed.

## [0.6.0] - 2026-10-24

### Added
- Push updates. The integration holds the hub's `/notification` stream open
  and reads `/status` as soon as the hub announces a change, with a 1 s
  debounce so a burst of events costs one or two reads. A shade shows as
  opening or closing as soon as the hub takes a command, including one from
  the ShadeAuto app (it used to take up to the 30 s idle poll), and its new
  position appears as soon as the shade reports where it stopped. The
  stream is reopened every 300 s, because long-lived streams have been seen
  to go quiet, and after any disconnect, waiting 5 s and twice as long after
  each further failure in a row, up to 60 s. A stream counts as working
  again only once it has stayed open for 60 s, so a hub that closes every
  stream at once costs a reconnect a minute and one warning, not a warning
  every 5 s; an answer other than the hub's `{"Error": "Success."}` counts
  as a refusal. Every reconnect is followed by a `/status` read, because the
  hub does not replay events missed while the stream was down. The listener
  never waits for the reads it asks for, so closing a stream cannot cancel
  one (on Home Assistant 2026.10 that would have shown the covers as
  unavailable), and a change announced while a read is in flight is read
  again within a second on every supported Home Assistant release.
  Polling continues unchanged as the fallback (README, "How updates reach
  Home Assistant"; `docs/ARCHITECTURE.md`, "Push updates";
  `docs/HUB_PROTOCOL.md` 2.5).
- Option "Use the hub's change notifications (push updates)"
  (`push_enabled`, default on). Off never opens the stream: updates come
  from polling alone, as in 0.5.0. Verify's per-shade report-in request
  (below) does not depend on it; turning verify off stops that.
- Tests: `FakeHub` serves `/notification` as a stream that tests write events
  to and can drop, to cover the listener's framing, reconnects and recycling,
  and answers `StatusRequest: 0` the way the hub does. The verify tests now
  expect the `StatusRequest` before the one retry, and the shared test
  options turn push updates off so the tests from before 0.6.0 keep their
  meaning.

### Changed
- Verify and retry: before re-sending a command, the integration asks that
  one shade to report in (`{"PeripheralUID": <uid>, "StatusRequest": 0}`)
  and waits up to 10 s for its report (the full 10 s when push updates are
  off or the stream is down), then reads `/status` and re-sends only if the
  same checks as before still call for it. A shade that got there but had
  not told the hub yet is no longer sent the command twice. A hub target that
  the report-in set to the shade's position, as kedube/ha-norman reports
  hubs do, does not count as someone else's command, so a command the hub
  stored but the shade never carried out is still re-sent, as in 0.5.0. The
  request can reach a shade that is still moving when the verify delay is
  shorter than its travel. The integration never sends a hub-wide
  report-in: kedube/ha-norman issue #5 saw one close a user's roller shades
  (`docs/HUB_PROTOCOL.md` 4.6).
- The manifest's `iot_class` is `local_push` (was `local_polling`).
- Documentation: `docs/HUB_PROTOCOL.md` 2.5 and 4.6 and the `StatusRequest`
  rows describe what the integration now does with the stream and with
  `StatusRequest`; `docs/ARCHITECTURE.md` describes the listener's
  lifecycle, the refresh debounce, the verify sequence and how tests drive
  the stream.

## [0.5.0] - 2026-10-23

### Added
- Shade profiles. Each shade is driven according to the `ModuleType` /
  `ModuleDetail` pair the hub reports for it (README, "Supported shade types";
  `docs/HUB_PROTOCOL.md` 3.1 has the evidence per family):
  - `sheer`, PerfectSheer 48/3 and 49/3: lift and vane tilt, unchanged from
    0.4.0. 48/3 is verified on real shades; 49/3 is known only from the
    vendor app.
  - `single_rail`, every other detail of types 32, 48 and 49 (cellular 32/0
    and 32/2, roller 48/1, roman 48/2, 49/1 and 49/2): lift only, bottom-only
    commands as in 0.3.0; the middle rail is not sent or tracked.
  - `two_rail`, 33/any and 32/3-5 (top-down/bottom-up, day/night): a second
    "<shade> Top" cover for the upper rail (unique id suffix `_middle`), both
    rails in every command (refused with an error while the other rail's
    position is unknown, including after a stop until the hub reports where
    the shade stopped), and a collision guard that keeps the top rail at
    or above the bottom rail plus a gap. New options `collision_guard`
    (default on) and `rail_min_gap` (0-50 %, default 0). Ported from upstream
    PR #9; not yet tested on hardware by this project.
  - `legacy`, SmartDrape 80/any, shutter 1/any, 32/48/49 without a
    `ModuleDetail` and anything unrecognised: like `single_rail`. An
    unrecognised pair logs one warning per setup asking for it to be
    reported.
- `docs/hub-characterization-2026-10-23.log`: the `/notification` stream and
  a per-shade `StatusRequest` on real hardware.

### Changed
- Supports Home Assistant 2025.8 and newer (0.4.0 required 2026.10). Three
  import-level shims in `compat.py` cover the API differences, CI runs the
  suite on 2025.8 as well as the latest release, and CONTRIBUTING.md
  documents when the minimum moves.
- The `tilt_enabled` option is replaced by `tilt_mode`: `auto` shows tilt on
  PerfectSheer shades only, `off` nowhere. Stored values are migrated
  (on becomes `auto`, off becomes `off`).
- The default minimum time between commands is 1.5 s (was 0.75 s), after
  kedube/ha-norman's measurements on a 13-shade hub, where 1.0-1.2 s lost
  commands. A value saved in the options form is kept. The form now allows
  up to 3 s (was 1 s).
- A stored verify delay below 30 s (possible from 0.3.x) is raised to 30 s.
- The grace period for the hub to reflect a command and the verify delay now
  start when the command's post is answered, not when it joins the send
  queue. With the longer spacing, a command in a scene can wait longer than
  the 10 s grace for its slot; it used to lose its "opening"/"closing" state
  before it was even sent. A service call cancelled while its command is
  queued or in flight is undone like a failed one.

### Fixed
- Shades without vanes no longer show tilt controls.
- Two-rail shades: commands no longer set the top rail's target to 0. 0.4.0
  sent `MiddleRailPosition: 0` with every raise, and 0.3.x sent bottom-only
  commands, which the hub treats as middle 0.
- Single-rail shades: a middle-rail target that an app preset left at 100
  (the rail itself reads 0) no longer counts as a move in progress or
  triggers a retry.
- `tools/hubwatch.py --notify` now shows the hub's notifications. It used to
  read each `/notification` reply to the end, which never comes, and gave up
  at a socket timeout with nothing; it now reads the stream as it arrives,
  prints each event, and reconnects when the stream ends or fails.
- Documentation: 0.4.0 said the hub never delivers notifications. That was
  wrong. `/notification` is a stream that stays open and carries an event for
  every accepted command and every finished move; our clients could not see
  it because they waited for the reply to end (`docs/HUB_PROTOCOL.md` 2.5,
  with `docs/hub-characterization-2026-10-22.log` and
  `docs/hub-characterization-2026-10-23.log`). The integration still polls;
  push updates are planned for 0.6.0.

## [0.4.0] - 2026-10-22

### Added
- Vane tilt on sheer (PerfectSheer) shades: `open_cover_tilt`,
  `close_cover_tilt`, `set_cover_tilt_position`, `stop_cover_tilt` and the
  `current_tilt_position` attribute (0 = closed, 100 = open). Tilt commands
  lower the shade if it is not already down, because the hub only honours
  vanes at the bottom.
- `stop_cover` for the lift, using the hub's `MotorStop` command. The shade
  stops showing as opening or closing at once, and the position it stopped
  at appears a couple of seconds after the hub reports it (the hub takes
  about 35 s to do so).
- Faster polling while a shade is moving (`motion_poll_sec` option).
- `tilt_enabled` option to hide tilt on shades without vanes.
- Reconfigure flow for changing the hub's address.
- Device page shows the shade model (PerfectSheer) and motor firmware.
- `tools/hubwatch.py`, a stdlib tool that watches the hub and can send
  single commands; `docs/HUB_PROTOCOL.md` and `docs/ARCHITECTURE.md`.
- Test suite with a fake hub, and CI (ruff, mypy, pytest, bandit, hassfest,
  HACS validation, CodeQL).

### Changed
- The hub is now the source of truth. Positions and vane state converge to
  what the hub reports, including moves made from the ShadeAuto app or a
  remote; the slider animation is cosmetic and yields to the hub.
- Every lift command carries an explicit vane target so the hub cannot
  silently close the vanes; lowering fully keeps the current vane setting.
- Shades are named after the hub's `PeripheralName` instead of
  "Shade <id>". Entity ids and unique ids are unchanged.
- Verification checks both the lift and the vane target, re-sends a command
  at most once, and only when the hub never took it. A target the hub took
  and then changed means the shade was re-commanded from the app or a remote
  (vane changes made in the app included), and nothing is re-sent. The
  verify delay has a minimum of 30 s, because the hub reports end states
  10–35 s after a command.
- Devices are identified by the hub's `ThingName` instead of its address, so
  changing the address with Reconfigure keeps devices, areas and names.
  Devices created by 0.3.x under the address are migrated at the first
  start.
- Options changes reload the entry; a hub that is briefly unreachable at
  startup is retried instead of failing setup, and so is a hub whose shade
  list (`/GetAllPeripheral`) comes back as an error or as something that is
  not JSON. The integration never loads with zero shades because of a bad
  reply.
- Requires Home Assistant 2026.10 or newer.

### Fixed
- Home Assistant showing a stale position after a command had settled, after
  a failed command, or after the shade was moved from the app or a remote.
- Hub rejections of a command (`Error` in the reply) and connection errors
  are reported as service errors naming the shade ("Could not move shade
  Kitchen: ...") instead of being ignored, and the shade goes back to the
  hub's last known position instead of staying "opening".
- The slider animation stops as soon as the hub's target changes under it
  (the shade was re-commanded from the app or a remote mid-move) instead of
  running on to the old target.

### Removed
- The `/notification` long-poll watcher and the `notification_timeout_sec`
  option. Stored values of the option are ignored. The reason given at the
  time, that the hub never delivers notifications, was wrong: the hub keeps
  the reply open as a stream, and both this watcher and `tools/hubwatch.py`
  waited for the reply to end and timed out, throwing away what had arrived
  (corrected in 0.5.0; `docs/HUB_PROTOCOL.md` 2.5).
- `Archive.zip`, an old snapshot of the source tree.

## [0.3.0] - 2025-11-17

Upstream release by kevinlester: lift control, battery sensors, position
estimation and verify-and-retry.
