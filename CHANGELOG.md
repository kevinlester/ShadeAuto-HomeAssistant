# Changelog

All notable changes to this integration are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed
- Supports Home Assistant 2025.8 and newer (0.4.0 required 2026.10). Three
  import-level shims in `compat.py` cover the API differences, CI runs the
  suite on 2025.8 as well as the latest release, and CONTRIBUTING.md
  documents when the minimum moves.

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
  option; the hub never delivers notifications. Stored values of the option
  are ignored.
- `Archive.zip`, an old snapshot of the source tree.

## [0.3.0] - 2025-11-17

Upstream release by kevinlester: lift control, battery sensors, position
estimation and verify-and-retry.
