# TDBU regression evidence

The deployed private fork uses version 0.4.1. This upstream PR leaves the manifest
version unchanged at 0.3.0 so the maintainer can manage the release.

Scope: rail command coordination and position reporting. This is a source-level,
transport-mocked regression suite, not a Home Assistant frontend or hardware test.
The initial run was source-only. Live installation and verification were subsequently
completed on Home Assistant Core 2026.10.1, as recorded below.

The reported fixture has both raw rail positions at 35, with the top physically
mostly lowered and the bottom immediately underneath it. The old top entity reports
65 and the bottom can retain a stale estimated 0 despite the raw reading of 35.
The intended full-open destination is both rails raised, represented by 100/100.

Run `python3 -m unittest discover -s tests -v`. The suite loads production constants,
coordinator methods and cover methods with minimal framework substitutes. The hub
transport is a fake that yields during every POST to expose concurrent scheduling.
No live hub requests occur. Motion registration substitutes omit HA background tasks;
notification polling, real retry timing and firmware behavior are not certified.

14 test cases pass on the candidate, including:

- Full-open from 35/35, in both rail orders, sequentially and concurrently.
- Concurrent independent moves preserve both destinations.
- Every tested guarded full-open payload obeys bottom <= middle.
- Companion movement is recorded for motion tracking and verification.
- Lowering the top moves an obstructing bottom to a compatible height.
- Positive minimum gap is maintained at travel endpoints.
- Unknown companion position prevents incomplete TDBU commands.
- Disabling the guard preserves the companion position.
- Single-rail payloads retain their original shape.
- Failed POSTs register no phantom targets.
- Retry payloads preserve the latest companion target.
- A retry superseded while waiting for the shade lock sends nothing.
- Completed estimates display reported height; new estimates start at measurement.

The same 14-case suite against cf6d5a6 (the command-race-only fix, before direction,
pair planning and display changes) reports 7 failures. This validates that the suite
catches the unresolved preset and position-display defects rather than merely passing
both implementations.

## Live verification (2026-10-13, America/Los_Angeles)

- Existing installation was version 0.4.0; its three changed Python files matched
  the repository's original source fingerprints.
- Backed up the installed component and scripts on the Home Assistant host and
  verified the backup bytes before copying the update.
- Installed const.py, coordinator.py, cover.py and manifest.json from pinned commit
  5cd1b49619a6a638cb8843b3d034115921693996. All four installed files passed SHA-256
  comparisons to the locally tested source.
- Preserved the existing daytime physical position by changing the inverted top
  target from 65 to 35. Changed the dining Close preset to explicitly lower bottoms
  and raise tops; the global nighttime preset already calls this dining script.
- `ha core check` completed successfully. Restarted Core with explicit user approval.
- After startup, the affected rail entities both reported current_position 35 and
  raw 35/35, establishing that the corrected direction/reporting behavior was active.
- Executed cover.open_cover on one dining bottom rail only, matching the existing
  Full Open script's action. Both raw rail positions ultimately reported 100/100,
  and both covers reported current_position 100.
- The user independently confirmed the shade was physically fully open.

The movement test covered one real TDBU shade. Other shades and the changed Daytime
and Close movement sequences were not physically exercised. The installed update is
manual; HACS still tracks the original upstream 0.3.0 package. An upstream redownload
would replace these custom files, so future upgrades should use the corrected fork.
