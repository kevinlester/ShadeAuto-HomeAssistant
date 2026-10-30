# ShadeAuto hub local HTTP protocol

This document describes what the ShadeAuto (NienMade) shade hub actually does on
its local HTTP API, as observed on one real hub with five PerfectSheer shades,
plus what two other projects report for shade families that hub does not have.
It is written for someone who maintains the Home Assistant integration in
`custom_components/shadeauto/` and has never seen the hardware.

Nothing here comes from vendor documentation; there is none. Every statement is
backed by one of these sources, and the source is named next to the claim:

| Source | What it is |
|---|---|
| `tests/fixtures/registration.json` | A real `/registration` reply. ThingName and Wi-Fi SSID scrubbed. |
| `tests/fixtures/get_all_peripheral.json` | A real `/GetAllPeripheral` reply. ThingName scrubbed and GeoLoc zeroed. It holds one room, five groups and five shade names; whether any of those names was altered before commit is not recorded. |
| `tests/fixtures/status.json` | A real `/status` reply, captured in the same second as the two above. |
| `tests/fixtures/error_invalid.json` | The hub's error envelope for a `/GetAllPeripheral` call with `ThingName: null`. |
| "the log" | [`hub-characterization-2026-10-22.log`](hub-characterization-2026-10-22.log), a timestamped record produced by `tools/hubwatch.py` during the characterization session on 2026-10-22. Each `#####` header is a separate hubwatch run (each starts with a fresh initial snapshot, so changes between runs were not recorded), labelled C1, T2, C3 (also called T1), T3, C2a, C2b, C4a, C4b, C6a, C6b, C6c, C5, C8, C8a, C8b and "restore". The first four were transcribed into the file afterwards from the session's hubwatch output; the rest were appended live. All of them were run against shade 12177 ("Kitchen"). |
| "the 2026-10-23 log" | [`hub-characterization-2026-10-23.log`](hub-characterization-2026-10-23.log), a 200 s probe of the same hub and shade 12177: the `/notification` stream read incrementally, `/status` polled once a second, and one `/control` `{"StatusRequest": 0}` to 12177. Its header says how it was produced. |
| "session notes" | What the shade's owner reported doing with the physical remote and the vendor app during C5 and C8, and the ~11 s vane-tilt timing seen with an earlier ad-hoc watcher before the log existed. On 2026-10-23: that the vane move to 55 in that log was made in the vendor app. These are not machine-logged and cannot be re-read from a file. |
| "kedube" | `docs/NORMAN_API.md` in [kedube/ha-norman](https://github.com/kedube/ha-norman), another Home Assistant integration for the same hub. Its author tested a `NienMadeHub` on firmware 6.1.25 with 13 shades of types 32 and 33, captured the vendor app's traffic and read the app's network library; types 48/1 and 80/1 come from users' diagnostics in its issues #2 and #5. Section names are quoted where cited ("Waking a blind", "Cover types", ...). Nothing from it has been reproduced here unless the text says so. |
| "PR #9" | [kevinlester/ShadeAuto-HomeAssistant pull request #9](https://github.com/kevinlester/ShadeAuto-HomeAssistant/pull/9), two-rail (top-down/bottom-up) support for type 33. Its `docs/TDBU-REGRESSION.md` records a live test on one real type 33 shade on 2026-10-13. |
| "PR #7" | [kevinlester/ShadeAuto-HomeAssistant pull request #7](https://github.com/kevinlester/ShadeAuto-HomeAssistant/pull/7), vane tilt for a shade whose `ModuleType` it does not name (`TILT_SUPPORT.md`). |
| `custom_components/shadeauto/*.py` (`api.py`, `const.py`, `coordinator.py`, `cover.py`, `entity.py`, `util.py`), `tools/hubwatch.py`, `README.md` | How the integration and the watch tool build requests and consume replies today. |

Anything that is a guess is marked **unverified**. If you find out more, fix the
text and name your evidence.

---

## 1. Overview

**The hub.** A small Wi-Fi bridge that talks RF to Norman motorised shades.
`registration.json` identifies it as `Model: "NienMadeHub"`, hub firmware
`FirmwareVersion: "6.1.25"`, `FirmwareCode: 90`, `FirmwareId: 48`,
`DeviceType: 48`. Its `ThingName` has the form `us-east-1:<uuid>_<digits>`;
the field name and the `us-east-1:` prefix suggest an AWS identity (Cognito or
IoT) and a vendor-cloud link, but that is an inference from the string alone
(**unverified**: nothing in the fixtures, the log or the code shows cloud
traffic, and that path is not covered here). In the fixtures the ThingName is
scrubbed to `us-east-1:00000000-0000-0000-0000-000000000000_0000000000000000`.

**The shades.** Five shades were attached during characterization. All report
`ModuleType: 48`, `ModuleDetail: 3` and motor `FirmwareVersion: "2.4.1"`
(`status.json`). They are Norman PerfectSheer shades (the vendor name comes
from the owner and from `README.md`/`const.py`; the product line is visible in
`get_all_peripheral.json`, where shade 14500 still carries its factory default
name `PerfectSheer_38A4`): a lifting bottom rail plus tiltable vanes. The
protocol calls the vane tilt `MiddleRailPosition`. On other shade families the
same field means something else (a second rail, or nothing at all); section 3.1
lists what is known per family and where it comes from. Unless a passage says
otherwise, every observation in this document is of a PerfectSheer.

**Transport.**

- Every call is `POST http://<host>:10123/NM/v1/<endpoint>` with a JSON body
  and `Content-Type: application/json` (`api.py`, `hubwatch.py`).
- Plain HTTP was used throughout. Both clients' comments say there is no HTTPS
  variant, but HTTPS was never tried (**unverified**).
- No authentication, no session, no cookies. Anyone on the LAN who can reach
  TCP 10123 can move the shades.
- Both clients default to port 10123 (`DEFAULT_PORT` in `const.py` and
  `hubwatch.py`); `const.py` describes it as fixed by the firmware. No other
  port was tried (**unverified**).
- GET was not tried (**unverified** whether it does anything).
- Endpoint names are used with the exact casing shown below; whether the hub
  is case-sensitive is **unverified**.

**ThingName.** `/registration` returns it. `/GetAllPeripheral` requires it:
sending `ThingName: null` produces the error envelope in `error_invalid.json`.
Both clients also send it on `/control`, and the watch tool on
`/notification`. Whether `/control` requires it is **unverified** (it was never
omitted); kedube found that `/notification` ignores its request body
altogether (2.5). `/status` and `/registration` were only ever called with
`{"Timestamp": <seconds>}` and work with that.

**Three reply styles.** `/registration`, `/status` and `/control` answer with a
flat object that carries `Error` (0 = ok) and the hub's timestamps.
`/GetAllPeripheral` answers with an envelope
`{"status": {"code", "error"}, "results": {...}}`. `/notification` answers with
a stream of JSON objects that does not end. See each endpoint.

**Timestamps.** Requests carry `Timestamp` in whole seconds. Replies echo it as
`RequestTimestamp` (seconds) and add the hub's own clock as `Timestamp` in
milliseconds. In `status.json`, `RequestTimestamp` is `1792658302` and
`Timestamp` is `1792658302353`. The hub's clock agreed with the client's to
well under a second in every exchange in the log: `RequestTimestamp` always
equals the whole-second part of the hub's `Timestamp`, each hub stamp lands
within a second of the hubwatch snapshot line printed right after it (C2a:
`1792660353684` is 05:12:33.684 local, snapshot 05:12:33; C8a: 05:34:28.971,
snapshot 05:34:29), and the echoed
`TaskID` (client milliseconds masked to 31 bits) differs from the hub stamp
masked the same way by between -8 and +36 ms (C2a 21 ms, C2b 19 ms). The
2026-10-23 log agrees: the `/control` reply stamp 1792744667098 is 04:37:47.098
and was printed at 04:37:47, the first event's 1792744669337 is 04:37:49.337
and was printed at 04:37:49. Log times are the client's wall clock in
America/Detroit (EDT, UTC-4). The latencies in 2.5, 4.10 and 5.2 rely on this
agreement.

---

## 2. Endpoints

### 2.1 `/registration`

Identify the hub.

**Request**

```json
{"Timestamp": 1792658302}
```

**Response** (trimmed to one of the five `Peripherals` entries; the other four
are byte-for-byte identical apart from `GroupID`/`PeripheralUID`; `RoomID` is
11819 on all five)

```json
{
  "FirmwareId": 48,
  "DeviceType": 48,
  "Model": "NienMadeHub",
  "ThingName": "us-east-1:00000000-0000-0000-0000-000000000000_0000000000000000",
  "PairingMode": 4,
  "CleanPairing": 0,
  "StatusRequest": 0,
  "ReportBatteryLevel": 0,
  "OTA": 1,
  "TotalSegment": 1,
  "Segment": 1,
  "Peripherals": [
    {
      "ModuleType": 48,
      "RoomID": 11819,
      "GroupID": 2,
      "PeripheralUID": 14500,
      "RequestModuleInfo": 0,
      "Switch": 1,
      "MotorStop": 170,
      "Favorite": 0,
      "Calibration": 0,
      "ConfigToScene": 287,
      "SetToScene": 287,
      "CleanAllScene": 0,
      "BatteryVoltage": 100,
      "ModuleDetail": 3,
      "TargetBottomRailPosition": 100,
      "TargetMiddleRailPosition": 100,
      "BottomRailPosition": 100,
      "MiddleRailPosition": 100,
      "SpeedInformation": 15,
      "CalibrationState": 1,
      "SetMotorToTopLimit": 170,
      "SetMotorToBottomLimit": 170,
      "MotorFineTuneToUp": 170,
      "MotorFineTuneToDown": 170,
      "SetTopLimit": 0,
      "CleanTopLimit": 0,
      "SetBottomLimit": 0,
      "CleanBottomLimit": 0,
      "SetMiddleLimit": 0,
      "CleanMiddleLimit": 0,
      "CleanRfPairing": 0,
      "CleanAllPosition": 0,
      "MotorSpeedAdjust": 259,
      "ReverseMotorDirection": 0,
      "SmartDialSwitch": 1,
      "FirmwareVersion": ""
    }
  ],
  "Error": 0,
  "FirmwareVersion": "6.1.25",
  "FirmwareCode": 90,
  "WiFiSSID": "ExampleSSID",
  "RequestTimestamp": 1792658302,
  "Timestamp": 1792658302262
}
```

Top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `FirmwareId`, `DeviceType` | int | Both 48, the same number as the shades' `ModuleType`. Meaning **unverified**. |
| `Model` | string | `"NienMadeHub"`. |
| `ThingName` | string | Hub identity. Needed for `/GetAllPeripheral`. |
| `PairingMode` | int | 4 here, 0 in `/status`. Meaning **unverified**. |
| `CleanPairing`, `StatusRequest`, `ReportBatteryLevel` | int | All 0. Look like hub-level command fields (compare `StatusRequest` in section 4). **Unverified.** |
| `OTA` | int | 1 here, 0 in `/status`. Meaning **unverified**. |
| `TotalSegment`, `Segment` | int | Both 1. Possibly paging for hubs with many shades; with one segment every shade is in one reply. **Unverified.** |
| `Peripherals` | array | One entry per shade. **Not live state**: see section 6. |
| `Error` | int | 0. |
| `FirmwareVersion`, `FirmwareCode` | string, int | Hub firmware `"6.1.25"`, code 90. |
| `WiFiSSID` | string | The Wi-Fi network the hub is on (scrubbed in the fixture). |
| `RequestTimestamp`, `Timestamp` | int | Echo of the request's `Timestamp` (s); hub clock (ms). |

**What the integration uses it for.** `ShadeAutoApi.registration()` stores
`ThingName` and nothing else. The coordinator exposes it as
`coordinator.identity` and uses it (falling back to the host) as the stable
identity in entity unique IDs and in device identifiers. Devices are tied to
the ThingName, not the host, so changing the address through Reconfigure
keeps devices, areas and names; devices that 0.3.x created with host-based
identifiers are migrated to the ThingName once, at setup. The call runs in
the coordinator's `_async_setup` hook, so a failure raises
`ConfigEntryNotReady` and setup is retried.

**Errors.** None observed. Behaviour with a missing or malformed body is
**unverified**.

### 2.2 `/GetAllPeripheral`

Names and grouping of every shade. This is the only endpoint that returns the
user-visible shade names.

**Request** (as the integration sends it)

```json
{"ThingName": "us-east-1:00000000-0000-0000-0000-000000000000_0000000000000000", "TaskID": 1, "Timestamp": 1792658302}
```

Both clients send the constant `TaskID: 1` here (`api.py`, `hubwatch.py`); the
per-command TaskID convention in 4.7 applies to `/control` only, and nothing
was observed to depend on the value.

**Response** (trimmed to two of the five groups)

```json
{
  "status": {"code": 0, "error": ""},
  "results": {
    "ThingName": "us-east-1:00000000-0000-0000-0000-000000000000_0000000000000000",
    "CustomDeviceName": "ShadeHub",
    "TimeZone": "America/Detroit",
    "GeoLoc": {"Latitude": "0.000000", "Longitude": "0.000000"},
    "NetworkID": "0",
    "RoomList": [
      {
        "RoomID": "11819",
        "RoomName": "Room_2E2B",
        "Icon": "1",
        "Color": "1",
        "Sorting": "0",
        "SceneList": [],
        "GroupList": [
          {
            "GroupID": "8",
            "GroupName": "Group_4",
            "Sorting": "4",
            "PeripheralList": [
              {"PeripheralUID": "23500", "PeripheralName": "Office", "Sorting": "0", "ModuleType": "48", "ModuleDetail": "3"}
            ]
          },
          {
            "GroupID": "1",
            "GroupName": "Group_1",
            "Sorting": "1",
            "PeripheralList": [
              {"PeripheralUID": "12177", "PeripheralName": "Kitchen", "Sorting": "0", "ModuleType": "48", "ModuleDetail": "3"}
            ]
          }
        ]
      }
    ],
    "SceneGroupList": [],
    "TaskID": 1,
    "RequestTimestamp": 1792658302,
    "Timestamp": 1792658302321
  }
}
```

Things to notice:

- **Every scalar inside `RoomList` is a string** (`"PeripheralUID": "12177"`,
  `"ModuleType": "48"`, and likewise the ids, names, `Sorting`, `Icon` and
  `Color`), as are `NetworkID` and the `GeoLoc` coordinates, whereas `/status`
  and `/registration` give integers. Only the trailing `TaskID`,
  `RequestTimestamp` and `Timestamp` in `results` are integers. `util.to_int()`
  exists for this.
- The structure is `RoomList -> GroupList -> PeripheralList`. On this hub each
  shade sits alone in its own group and all groups are in one room. The
  integration does not hard-code this path; it searches the reply for any dict
  containing `PeripheralUID` (`find_dicts_with_key` in `api.py`).
- `RoomID` and `GroupID` match the integer `RoomID`/`GroupID` on each shade in
  `/status`.
- `SceneList` and `SceneGroupList` were empty; scene objects are **unverified**.
- `CustomDeviceName` is the hub's name in the vendor app. `GeoLoc` is the hub's
  configured location (zeroed in the fixture); `TimeZone` is as reported
  (`America/Detroit`).
- The shades on this hub, as the fixture names them: 14500
  `PerfectSheer_38A4`, 23500 `Office`, 23388 `Guest Room`, 12177 `Kitchen`,
  3190 `Master Bedroom`.
- `TaskID` is echoed back inside `results`.

**What the integration uses it for.** `ShadeAutoApi.get_all_peripheral()` is
called once at setup, from the coordinator's `_async_setup` hook, to build
the list of shades (`Peripheral.from_hub` in `coordinator.py`), which decides
which entities exist. The error envelope (`status.code` non-zero, see
"Errors" below) and a body that is not JSON are both treated as connection
errors: setup raises `ConfigEntryNotReady` and Home Assistant retries it
later. The integration never loads with zero shades because of a bad reply.
`Peripheral.from_hub`
reads `PeripheralName` (falling back to `Name`, then `Shade <uid>`), so the
names in this reply become the device names;
`hubwatch.py --names` reads the same key. The `ModuleType`/`ModuleDetail` pair
(as strings here, as integers in `/status`) picks the shade's profile (3.1) and
the device model string (`(48, 3)` is shown as "PerfectSheer").

**Errors.** With `ThingName: null` (which is what the integration sends if it
calls this before `/registration`), the hub answers (`error_invalid.json`):

```json
{
  "status": {"code": 1, "error": "Invalid JSON format or content."},
  "results": {"TaskID": 1, "RequestTimestamp": 1792658302, "Timestamp": 1792658302381}
}
```

The HTTP status code of that reply was not recorded (**unverified**; it was
captured as a normal body, so it was probably 200). `api.py` raises
`ShadeAutoResponseError` (a `ShadeAutoError` carrying `.code` and the
`status.error` text) when `status.code` is non-zero, which setup turns into
`ConfigEntryNotReady`; it never returns `[]` for this envelope. Other `code`
values are **unverified**.

### 2.3 `/status`

Live state of every shade. This is the endpoint to poll. Positions may be
stale after physical-remote use (5.4).

**Request**

```json
{"Timestamp": 1792658302}
```

**Response** (trimmed to two of the five shades)

```json
{
  "DeviceType": 48,
  "PairingMode": 0,
  "OTA": 0,
  "TotalSegment": 1,
  "Segment": 1,
  "Peripherals": [
    {
      "ModuleType": 48,
      "RoomID": 11819,
      "GroupID": 8,
      "PeripheralUID": 23500,
      "ModuleDetail": 3,
      "BatteryVoltage": 100,
      "TargetBottomRailPosition": 35,
      "TargetMiddleRailPosition": 0,
      "BottomRailPosition": 35,
      "MiddleRailPosition": 0,
      "SpeedInformation": 0,
      "CalibrationState": 0,
      "FirmwareVersion": "2.4.1",
      "Timestamp": 1792654338
    },
    {
      "ModuleType": 48,
      "RoomID": 11819,
      "GroupID": 1,
      "PeripheralUID": 12177,
      "ModuleDetail": 3,
      "BatteryVoltage": 100,
      "TargetBottomRailPosition": 0,
      "TargetMiddleRailPosition": 50,
      "BottomRailPosition": 0,
      "MiddleRailPosition": 50,
      "SpeedInformation": 0,
      "CalibrationState": 0,
      "FirmwareVersion": "2.4.1",
      "Timestamp": 1792656340
    }
  ],
  "WiFiRSSI": -38,
  "Error": 0,
  "RequestTimestamp": 1792658302,
  "Timestamp": 1792658302353
}
```

The per-shade fields are described in section 3.

**What the integration uses it for.** The coordinator polls it every
`poll_seconds` (default 30 s) while nothing is moving and every
`motion_poll_sec` (default 2 s) while any shade is moving. It copies the actual
and target position of both rails plus `BatteryVoltage`, `ModuleType`,
`ModuleDetail` and `FirmwareVersion` per shade (`STATUS_FIELDS` in
`coordinator.py`), and a shade counts as moving while actual != target on an
axis its profile tracks (`AXIS_FIELDS`, `is_moving`): both rails on
PerfectSheer and two-rail shades, the bottom rail only on single-rail and
legacy shades, whose middle target can sit at 100 forever while the rail reads
0 (3.1). After a `MotorStop` the entity stops showing motion at once, but the coordinator keeps polling at the motion interval
until the hub reports the stop point (target == actual) or the motion
failsafe elapses, because the hub takes about 35 s to rewrite the target
(4.6). The cover position and tilt, the opening/closing direction, the
battery sensor, the low-battery binary sensor and the device `sw_version` all
come from here. It is also re-read right after a command, and again at the
verify delay, when the coordinator compares the hub's `Target*` fields on
both axes with what it sent: a target the hub never reflected means the
command was lost and it is re-sent once; a target the hub reflected and then
changed means the app, a remote or the hub itself (4.4) re-commanded the
shade, and nothing is re-sent. The cover's slider estimate is dropped as soon
as the hub's target stops matching the target it is estimating toward.

**Errors.** None observed. Top-level `Error` was 0 on every poll in the log
(`hub Error=0` lines). Non-zero values are **unverified**.

### 2.4 `/control`

Send a command to one shade. Full semantics are in section 4; this section is
only the wire format. Six rules you must know before sending anything:

1. Always send `BottomRailPosition`. A payload with `MiddleRailPosition` alone
   is rejected with `Error: 2` (4.2).
2. Omitting `MiddleRailPosition` sets the middle target to 0. On a PerfectSheer
   that closes the vanes (4.3); on a two-rail shade it sends the top rail to
   the bottom (3.1).
3. PerfectSheer vanes are only honoured while the bottom target is 0. You may
   send both fields in one command; the shade lowers, then tilts (4.4).
4. The hub does not validate 0-100; clamp yourself (4.5).
5. `MotorStop: 170` stops the motor, and the hub then re-targets the shade to
   wherever it stopped (4.6).
6. `StatusRequest: 0` with a `PeripheralUID` makes that one shade report in
   without moving. Without a `PeripheralUID` it becomes a hub-wide report-in,
   and a hub-wide report-in closed one user's roller shades, so never send it
   unscoped. Never send `ReportBatteryLevel` at all: a `PeripheralUID` does not
   confine it to one shade (4.6).

**Request** (as `hubwatch.build_control_payload` and
`ShadeAutoApi._send_control` build it: `PeripheralUID`, `TaskID` and
`Timestamp` always, `ThingName` when known, then whatever command fields apply)

```json
{
  "PeripheralUID": 12177,
  "TaskID": 1659037383,
  "Timestamp": 1792660399,
  "ThingName": "us-east-1:00000000-0000-0000-0000-000000000000_0000000000000000",
  "BottomRailPosition": 0,
  "MiddleRailPosition": 100
}
```

| Field | Required? | Notes |
|---|---|---|
| `PeripheralUID` | yes | Integer. The shade from `/status` / `/GetAllPeripheral`. |
| `TaskID` | always sent | Client-chosen integer; the hub echoes it in the reply. The integration uses `int(time.time()*1000) & 0x7FFFFFFF` (section 4.7). Whether the hub rejects a missing TaskID is **unverified**. |
| `Timestamp` | always sent | Whole seconds; echoed as `RequestTimestamp`. Whether it is validated is **unverified**. |
| `ThingName` | always sent when known | Whether `/control` requires it is **unverified**. |
| command fields | one or more | `BottomRailPosition`, `MiddleRailPosition`, `MotorStop`, `StatusRequest`. `MiddleRailPosition` is rejected unless `BottomRailPosition` is also present (4.2); omitting `MiddleRailPosition` sets the middle target to 0 (4.3). `StatusRequest` goes alone, with value 0 (4.6). Section 6 lists the other field names the hub knows; most are limit, calibration or pairing commands and must not be sent. |

**Success response** (log C2b). The hub echoes the position fields it stored as
targets, which includes a `MiddleRailPosition: 0` the request did not contain
(C4a, C6a; see 4.3) and an out-of-range value unchanged (C6a), plus `Error: 0`.
Check `Error`, not field equality.

```json
{"PeripheralUID": 12177, "BottomRailPosition": 0, "MiddleRailPosition": 100, "Error": 0, "TaskID": 1659037383, "RequestTimestamp": 1792660399, "Timestamp": 1792660399834}
```

**Rejection response** (log C1; T3 is identical in shape: a payload containing
`MiddleRailPosition` but no `BottomRailPosition`)

```json
{"PeripheralUID": 12177, "Error": 2, "TaskID": 1658827752, "RequestTimestamp": 1792660190, "Timestamp": 1792660190194}
```

On a rejection no command field is echoed and `/status` does not change (the
snapshot right after C1 still shows `TargetMiddleRailPosition=50`). `Error`
values other than 0 and 2 are **unverified**.

**What the integration uses it for.** `ShadeAutoApi.control(uid, bottom=...,
middle=...)` always sends `BottomRailPosition` (`bottom` is keyword-only and
required) and adds `MiddleRailPosition` whenever `middle` is not `None`; both
are clamped to 0-100 first (`clamp_position()`). `ShadeAutoApi.stop(uid)`
sends `MotorStop: 170`. `_send_control()` raises `ShadeAutoCommandError` when
the reply's `Error` is non-zero, so an `Error: 2` rejection reaches callers as
an exception, not as a returned dict. The cover entity turns that exception,
like a connection error, into a `HomeAssistantError` naming the shade
("Could not move shade Kitchen: ...") and drops its optimistic state at once,
so the entity shows the hub's last known position instead of "opening". What
the cover entity (`cover.py`) sends depends on the shade's profile (3.1):

- **PerfectSheer:** both fields on every open, close, set-position and tilt
  call. Lowering to 0 and close keep the current vane target, any other height
  sends `MiddleRailPosition: 0` explicitly (4.4 would reset it anyway), and
  tilt commands send `BottomRailPosition: 0`.
- **Single-rail and legacy:** `BottomRailPosition` only, as 0.3.0 did. The
  implied `MiddleRailPosition: 0` (4.3) is harmless on a shade whose middle
  rail always reads 0.
- **Two-rail:** both rails on every command. The rail the user did not touch
  is held where it is heading (our pending target, else the hub's target, else
  its position), unless the collision guard has to move it to keep the top
  rail at or above the bottom rail. When that position is not known (the hub
  has not reported the rail, or we stopped the shade and the hub has not yet
  reported where that rail stopped) the command is refused instead of sent
  (see `docs/ARCHITECTURE.md`).

The verify-and-retry logic resends the same payload, once, and only when the
hub never reflected the targets it was sent; a retry overtaken by a newer
command does not overwrite that command's pending state.

### 2.5 `/notification`

A push stream. The hub keeps the response open and writes one small JSON object
each time a shade's target or position changes.

> **Correction, 2026-10-23.** Up to 0.4.x this section said that the hub never
> delivers notifications, and 0.4.0 removed the integration's `/notification`
> code because of it. That was wrong. The fault was in how our clients read the
> reply, not in the hub; "What we concluded on 2026-10-22, and why it was
> wrong" below has the details. The integration does not use the stream yet
> (0.5.0); push updates are planned for 0.6.0.

**Request** (as `tools/hubwatch.py --notify` and the 2026-10-23 probe send it;
the values are illustrative)

```json
{"ThingName": "us-east-1:00000000-0000-0000-0000-000000000000_0000000000000000", "Timestamp": 1792744658, "Timeout": 300}
```

The body seems to make no difference. kedube found that the hub ignores all
three fields: an empty body works, a stream opened with `Timeout: 5` was still
open 15 s later, and a `Timestamp` from before an event replayed nothing
("POST /NM/v1/notification", 2026-10-15). Only the body above has been tried
here.

**Response** (2026-10-23 log). `HTTP 200`, `Content-Type:
application/octet-stream`, `Transfer-Encoding: chunked`. The hub writes the
first object at once:

```json
{"Error": "Success."}
```

`Error` is a **string** here, unlike the integer `Error` of the other
endpoints, so a client that checks `Error == 0` would treat the stream as
failed. Then the hub keeps the response open and writes one more object per
change:

```json
{"Status": 1792744669337, "PeripheralList": [12177]}
```

- `Status` is the hub's clock in milliseconds, not a status code.
- `PeripheralList` holds shade **ids**, not records: the event says *which*
  shades changed, not *what* changed. Read `/status` to find out.
- There is no framing. No newline, length or separator comes between objects,
  so the reader has to split the bytes itself (`StreamSplitter` in
  `hubwatch.py` uses `json.JSONDecoder().raw_decode`). The 2026-10-23 log does
  not record how the bytes were split across reads, so assume nothing: one
  read can end inside an object or hold several.
- The hub does not end the response. In the 2026-10-23 log the one stream
  stayed open, with no EOF and no error, for the whole 200 s probe. **A client
  that reads the body to the end waits forever.** Read whatever has arrived
  (`read1` in `http.client`) and process it as it comes.

What produced an event in the 2026-10-23 log (shade 12177; hub times shown on
the client's clock, which agreed with the hub to well under a second):

| Event (`Status`) | Cause | What `/status` showed |
|---|---|---|
| 04:37:49.337 | `{"PeripheralUID": 12177, "StatusRequest": 0}`, accepted 2.2 s earlier (reply stamp 04:37:47.098) | Per-shade `Timestamp` 1792731364 (00:56:04) -> 1792744669 (04:37:49). No position or target changed and nothing moved. |
| 04:39:04.841 | A vane move to 55 made in the vendor app (session notes); the hub took the new target | `TargetMiddleRailPosition 0 -> 55` at the next poll. Per-shade `Timestamp` unchanged. |
| 04:39:26.350 | The move finished, 21.5 s later | `MiddleRailPosition 0 -> 55` and per-shade `Timestamp` -> 1792744766 (04:39:26) at the next poll. |

So a command the hub accepts produces one event at once (the target changed)
and another when the shade reports the end of the move (the position changed),
and a shade reporting in with nothing changed produces one too. kedube reports
the same pattern for its own commands and for app moves ("Notification stream,
as observed"), and also says remote moves produce events. That last part is
**unverified** here: the 2026-10-23 probe had no remote move, and 5.4 shows the
hub does not always learn of remote moves at all.

Reported by kedube and **not yet seen on this hub**:

- Events that happen while no stream is open are not replayed when one is
  opened. After connecting or reconnecting, read `/status` to catch up.
- Long-lived streams have been seen to go quiet, so kedube closes and reopens
  its stream every 300 s and treats an unexpected EOF as a disconnect.
  `hubwatch.py` reopens a stream that has been silent for 300 s, or that ended
  or failed, after a 2 s back-off.
- Changes made in the app other than moves arrive as `UpdateTime` objects, for
  example `{"UpdateTime": {"peripheral": ...}, "Timestamp": <ms>}` after a shade
  is renamed (also `"room"`, `"device"` for the hub's name, and `"schedule"`).
  A client re-reads `/GetAllPeripheral` on one to pick up new names.

**What we concluded on 2026-10-22, and why it was wrong.** *This conclusion is
superseded; the evidence is kept because it explains the mistake.* During the
C5 run `hubwatch.py --notify` long-polled with `Timeout: 50` the whole time.
Every request ended empty, logged as `NOTIFY (hub returned without an event)`
at 05:19:04, 05:20:04, 05:21:04, 05:22:04 and 05:23:19, and the session notes
said no event came for the app's changes or for our own commands either. From
that, this section concluded that the hub never delivers notifications. The
README, the 0.4.0 changelog and `ARCHITECTURE.md` repeated it, and 0.4.0
removed the integration's long-poll and its `notification_timeout_sec` option.

The hub was answering all along. The 0.4.0 `hubwatch.py` read each reply with
`urllib`'s `read()`, which returns only at the end of the body, and this body
never ends. So every request waited for the 60 s socket timeout
(`Timeout + 10`) and then returned `""`, throwing away the
`{"Error": "Success."}` and any events that had arrived. That is why four of
the five requests ended exactly 60 s after they started (the notify thread
started at 05:18:04). The old text did suspect that the client was giving up
while the hub still held the connection, but not that the hub had been writing
to it all along.
The fifth took 75 s. The socket timeout restarts whenever bytes arrive, so
something was probably written to the stream about 15 s into that request,
during the remote phase of C5; what it was cannot be known, because it was
discarded (**unverified**). The integration before 0.4.0 had the same flaw:
0.3.0's `ShadeAutoApi.notification()` read the reply with aiohttp's
`resp.text()`, also to the end, behind a socket read timeout of
`notification_timeout_sec` (default 2 s), and returned `""` when that expired.
Neither client could ever have shown an event.

**What the integration uses it for.** Nothing yet; 0.5.0 learns everything by
polling `/status` (2.3). Push updates are planned for 0.6.0: an event would
trigger an immediate `/status` read instead of waiting for the next poll, with
a per-shade `StatusRequest` before a retry (4.6). `tools/hubwatch.py --notify`
is the only client in this repository that calls the endpoint.

**Errors.** None observed. The first object's `Error` was `"Success."`; other
values are **unverified**.

---

## 3. Shade status record

Every element of `Peripherals` in a `/status` reply from this hub's
PerfectSheer shades has exactly these fields (`status.json`). Other families
add or drop some; kedube lists `RssiMean` and `StallCurrent` on types 32 and
33, and `Position` for shutters ("Observed fields").

| Field | Type | Units / range | Meaning | Integration uses it? |
|---|---|---|---|---|
| `PeripheralUID` | int | | The shade's id. Key for `/control` and for matching `/GetAllPeripheral`. | Yes (entity identity). |
| `ModuleType` | int | 48 observed | Shade family (product line). 48 here; kedube lists 1, 32, 33, 48, 49 and 80 from the vendor app (3.1). The hub's own `DeviceType` is also 48; whether that means anything is **unverified**. | Yes: with `ModuleDetail` it picks the shade's profile (3.1) and the device model string. |
| `ModuleDetail` | int | 3 observed | Product within the family; all five PerfectSheer shades report 3 (3.1). | See `ModuleType`. |
| `RoomID` | int | | Matches `RoomList[].RoomID` in `/GetAllPeripheral`. | No. |
| `GroupID` | int | | Matches `GroupList[].GroupID`. | No. |
| `BatteryVoltage` | int | percent, 0-100 | **A percentage despite the name.** 100 observed on every shade. `battery_percent()` treats values outside 0-100 as unknown. | Yes (battery sensor, low-battery flag). |
| `TargetBottomRailPosition` | int | 0-100 (not enforced, see 4.5) | The lift target the hub currently holds. Changes the moment a command is accepted via `/control` or the vendor app. Remote-driven moves are different: the target was either never updated (C5) or updated together with the actual once the shade stopped (C8); see 5.4. | Yes (motion tracking: moving while actual != target; opening/closing direction). |
| `TargetMiddleRailPosition` | int | 0-100 (not enforced) | The vane target the hub currently holds (the upper rail's target on two-rail shades, 3.1). Set by `/control` and app commands, also rewritten by the hub itself (sections 4.3, 4.4); for remote moves see 5.4. | Yes (motion tracking). |
| `BottomRailPosition` | int | 0-100 | Actual lift. **0 = fully lowered, 100 = fully raised.** Only updated when motion ends. | Yes (cover position). |
| `MiddleRailPosition` | int | 0-100 | Actual vane tilt on PerfectSheer. **0 = vanes closed, 100 = vanes open** (light through). Intermediate values are real: `status.json` shows shade 12177 at 50. Only updated when motion ends. Other families: 3.1. | Yes, by profile: tilt position on PerfectSheer (unless `tilt_mode` is off), the top rail's position on two-rail shades, ignored on single-rail and legacy shades. |
| `SpeedInformation` | int | 0 observed | Never changed during any move in the log (it is one of the fields `hubwatch.py` diffs on every poll, `DEFAULT_FIELDS`). 15 in the registration template. Meaning **unverified**. | No. |
| `CalibrationState` | int | 0 observed | Never changed in the log (also a diffed field). 1 in the registration template. Meaning **unverified**. | No. |
| `FirmwareVersion` | string | `"2.4.1"` | Shade motor firmware. | Yes (device `sw_version`). |
| `Timestamp` | int | seconds | **When the hub last heard from the shade**, not when the record last changed. In the 2026-10-23 log a `StatusRequest` moved it from 1792731364 to 1792744669, the second of the stream event, with nothing else changing; a new target from the app left it alone; the end of that move set it to 1792744766, again the second of the event. kedube reports the same ("Waking a blind"). | No. |

Top-level fields of the `/status` reply:

| Field | Type | Meaning |
|---|---|---|
| `DeviceType` | int | 48, as in `/registration`. |
| `PairingMode` | int | 0 (the registration reply says 4). Stayed 0 throughout the log. Meaning **unverified**. |
| `OTA` | int | 0 (registration says 1). Stayed 0. Meaning **unverified**. |
| `TotalSegment`, `Segment` | int | 1 and 1. See section 2.1. |
| `Peripherals` | array | One record per shade, as above. |
| `WiFiRSSI` | int | dBm, hub's Wi-Fi signal (-38). |
| `Error` | int | 0 on every poll seen. |
| `RequestTimestamp` | int | Echo of the request `Timestamp` (seconds). |
| `Timestamp` | int | Hub clock, milliseconds. |

### 3.1 Shade families: what `MiddleRailPosition` means

`ModuleType` names the shade family and `ModuleDetail` the product within it.
The same two rail fields mean different things per family, so the integration
(0.5.0) maps each pair to a *profile*, and the profile decides which entities
exist and what is sent (`docs/ARCHITECTURE.md`, "Shade profiles").

Only the PerfectSheer row was verified by this project. Every other row rests
on the source named in it, and none has been tried with this integration. If
you own one of those shades, the notes below say what to check.

| `ModuleType`/`ModuleDetail` | Product (vendor app's name) | What `MiddleRailPosition` is | Source | Profile |
|---|---|---|---|---|
| 48/3 | PerfectSheer | Vane tilt, 0 closed .. 100 open, honoured only while the bottom target is 0 (4.4) | This hub, five shades (fixtures, 2026-10-22 log) | `sheer` |
| 49/3 | PerfectSheer, second-generation module | Assumed as 48/3 | kedube: vendor app only | `sheer` |
| 32/0, 32/2 | Cellular shade | Nothing; reads 0 | kedube: 32/2 on real hardware, 32/0 vendor app only | `single_rail` |
| 48/1, 48/2 | Roller shade, Roman shade | Nothing; reads 0, but app presets can leave the *target* at 100 | kedube: 48/1 from users' diagnostics (its issues #2, #5), 48/2 vendor app only | `single_rail` |
| 49/1, 49/2 | As 48/1, 48/2 (second-generation module) | Assumed as 48/1, 48/2 | kedube: vendor app only | `single_rail` |
| 32, 48, 49 with any other detail | Not known | Assumed as the rest of the family | None | `single_rail`, no warning |
| 33/x | Cellular shade with two rails: day/night or top-down/bottom-up | The upper rail's height, on the same axis as the bottom rail | kedube: 33/3 on real hardware, 33/4-5 vendor app only; PR #9: one real type 33 shade | `two_rail` |
| 32/3, 32/4, 32/5 | Cellular shade, top-down/bottom-up | As 33 | kedube: vendor app only (the app draws two sliders) | `two_rail` |
| 80/x | SmartDrape | Vanes in seven stops, open at 50, closed at 0 and 100 | kedube: 80/1 from one user's diagnostics (issue #2); PR #7 matches | `legacy` |
| 1/x | Shutter | No rails; the louvres are a separate `Position` field, 0-7 | kedube: vendor app only | `legacy` |
| 32, 48, 49 without a `ModuleDetail` | Cannot be told | Unknown | | `legacy`, with a warning in the log |
| anything else | | Unknown | | `legacy`, with a warning in the log |

**PerfectSheer (48/3, 49/3).** Sections 2 and 4 describe this family. The
middle rail is the vane angle; the vanes open only while the shade is fully
lowered, the hub rewrites the vane target to 0 otherwise (4.4), and a command
without `MiddleRailPosition` closes them (4.3). kedube's app decoding says the
vendor app also calls 49/3 PerfectSheer and handles family 49 like 48 ("Cover
types"); no 49/3 is known to have been seen on a hub, so its behaviour is
assumed, not verified.

**Single-rail families (32/0, 32/2, 48/1, 48/2, 49/1, 49/2).** Only the bottom
rail moves, and `MiddleRailPosition` always reads 0 (kedube: every 32/2 on its
13-shade hub, target 0 as well; 48/1 in its issues #2 and #5). The middle
*target* is less tidy: in issue #2 the app's presets left
`TargetMiddleRailPosition` at 100 on every shade while the rail stayed at 0. A
client that tracks the middle rail then sees the shade as moving forever, and
one that verifies it keeps retrying; kedube says that is how it found type 48.
0.3.0 sent `BottomRailPosition` alone to every shade, and PR #9 keeps that for
single-rail shades (its test `test_single_rail_payload_is_unchanged`); the
implied `MiddleRailPosition: 0` (4.3) is harmless on a shade without a middle
rail. kedube instead sends both rails to every type and reports the hub
accepts that equally ("POST /NM/v1/control"); not tried here.

**Two-rail families (33/x, 32/3-5).** `BottomRailPosition` is the bottom rail's
height and `MiddleRailPosition` the upper rail's height, on the same axis:
0 = lowered, 100 = raised. Fully covered is bottom 0 / middle 100 (the fabric
spans the window), fully open is 100 / 100 (both rails stacked at the top), and
bottom 0 / middle 40 is a top-down position that lets light in above the
fabric. The upper rail must stay at or above the bottom rail. Sources:

- PR #9 measured the axis on one real type 33 shade: its first version
  inverted the upper rail, the fix states "Both hub fields measure rail height:
  0=lowered, 100=raised", and its live test (`docs/TDBU-REGRESSION.md`,
  2026-10-13) sent `open_cover` to the bottom rail of a shade at 35 / 35, which
  ended at 100 / 100 and was confirmed physically fully open.
- kedube: the vendor app's "Best privacy" preset sets bottom 0 / middle 100
  and "Best view" 100 / 100 ("Room-wide and hub-wide control"); on 2026-10-10
  a room-wide command with bottom 0 / middle 100 put two type 33 shades back in
  their starting position ("POST /NM/v1/control"); and 33/3 shades are
  day/night shades whose middle rail "tracks 0-100" ("Cover types").
  `/status` reports `Target*` fields for them as for PerfectSheer.
- The hub rule that a command without `MiddleRailPosition` sets the middle
  target to 0 (4.3, verified here on PerfectSheer) means a bottom-only command
  sends the upper rail to the bottom. PR #9 says the same of type 33 ("a
  bottom-only payload implies MiddleRailPosition=0"). So 0.3.0 (bottom-only)
  and 0.4.0 (which sent `MiddleRailPosition: 0` with every raise) would both
  have pulled the upper rail down on these shades. That is an inference; we
  have no report of it happening.

There is one `MotorStop` per shade and no known per-rail stop. kedube saw a
type 33 shade report where it stopped 2.8 s after a stop (2026-10-15), much
faster than a PerfectSheer's ~35 s (4.6). 32/3-5 are two-rail only because the
vendor app draws two sliders for them (kedube, "Cover types"); no one is known
to have seen one on a hub.

**SmartDrape (80/x).** kedube, from one user's diagnostics (issue #2): the
bottom rail is how far the drape is drawn, and the middle rail is the vane
openness in seven stops (0, 17, 33, 50, 66, 83, 100), open at 50 and closed at
both ends. PR #7 adds tilt for a shade it does not identify, with the same
shape: 0 and 100 are closed in opposite directions, 50 is open, and the motor
resets the tilt to about 17 when the drape moves. That matches kedube's
SmartDrape, but PR #7 gives no `ModuleType`, so the match is an inference.
0.5.0 gives SmartDrapes the `legacy` profile (lift only, bottom-only commands,
no tilt), because neither the tilt scale nor what a bottom-only command does to
the vanes (by 4.3 probably sends them to 0, closed) has been checked with this
integration.

**Shutter (1/x).** kedube, from the vendor app's library only: no rails; the
louvres are one `Position` field, 0-7 (7 shut, 3 horizontal), sent alone. The
`legacy` profile sends `BottomRailPosition`, which a shutter presumably
ignores (**unverified**). Louvre control is not supported.

**Anything else.** `legacy`: lift only, bottom-only commands, and a warning in
the log asking the user to report the type (SmartDrapes and shutters get the
same profile without the warning). Other details of types 32, 48 and 49 are
not "anything else": they get `single_rail` without a warning, like the rest
of their family; only a 32, 48 or 49 reported without a `ModuleDetail` falls
back to `legacy`, because which product it is cannot be told. The pair comes
from `/GetAllPeripheral`, with `/status` filling in a missing value, and the
warning names the pair and every shade that reports it, once per setup.
If you see it, open an issue with
the `ModuleType`/`ModuleDetail` pair (the device page shows it as
"Shade (type X/Y)"), what the shade is, and a `/status` record from
`tools/hubwatch.py --raw`.

---

## 4. Control semantics

Unless a passage names another source, everything in this section was learnt
by sending commands to shade 12177, a PerfectSheer, and watching `/status` at
1 s intervals. The table at the end of the section lists the raw evidence.

### 4.1 The command fields that matter

| Field | Value | Effect |
|---|---|---|
| `BottomRailPosition` | 0-100 | Move the bottom rail. 0 lowers fully, 100 raises fully. |
| `MiddleRailPosition` | 0-100 | PerfectSheer: set the vane tilt, 0 closed, 100 open, only meaningful when the shade is fully lowered (4.4). Two-rail shades: move the upper rail (3.1). |
| `MotorStop` | 170 | Stop whatever the motor is doing right now. |
| `StatusRequest` | 0, with `PeripheralUID` | Ask that one shade to report in. Nothing moves (4.6). |

The hub accepts both position fields in one request. In C2b the shade was at
bottom 50 / vanes 0; `{"BottomRailPosition": 0, "MiddleRailPosition": 100}`
made it lower and then tilt, and the next `/status` change reported both
actuals in the same update (`BottomRailPosition 50 -> 0` and
`MiddleRailPosition 0 -> 100` at 05:13:47.896, after a command sent at
05:13:19). The same happened in "restore" (45/0 to 0/100 in one update).

### 4.2 The mandatory-bottom rule

**A request that carries `MiddleRailPosition` without `BottomRailPosition` is
rejected with `Error: 2` and nothing changes** (log C1 and T3; the reply shape
is in 2.4). To tilt the vanes you must always also send the bottom
position, even if it is the position the shade is already at.

kedube reports the opposite: that the vendor app's group screen sends
`MiddleRailPosition` alone and the hub accepts it ("POST /NM/v1/control"). Here
it was rejected both times it was tried, on a PerfectSheer addressed by
`PeripheralUID`. Whether the rule differs by family or by how the command is
addressed is **unverified**; send `BottomRailPosition` every time.

### 4.3 The default-middle-to-0 rule

**If you send `BottomRailPosition` without `MiddleRailPosition`, the hub treats
it as `MiddleRailPosition: 0`.** Evidence:

- The reply to `{"BottomRailPosition": 100}` (C4a) and to
  `{"BottomRailPosition": 250}` (C6a) echoes `"MiddleRailPosition": 0` even
  though the request had no such field.
- In C4a the shade was at vanes 100 (open) before the command; the first
  `/status` after it shows `TargetMiddleRailPosition=0`.
- Log T2: with the vane target at 50 (from the vendor app), the bottom-only
  command `{"BottomRailPosition": 0}` moved `TargetMiddleRailPosition` from 50
  to 0 in the very next snapshot, while `MiddleRailPosition` was still 50.

The implicit `MiddleRailPosition: 0` is tied to `BottomRailPosition`: the
replies to `MotorStop`-only (C4b), `RequestModuleInfo`-only (C8a) and
`StatusRequest`-only (C8b, and the 2026-10-23 log) payloads contain no
`MiddleRailPosition` at all.
Whether a stop leaves a non-zero vane target alone is **unverified** (the vane
target was already 0 in C4b, C8a and C8b, so `/status` cannot tell).

Consequence: **a client that sends `BottomRailPosition` alone closes the
vanes** of a PerfectSheer, and, by the same rule, sends the upper rail of a
two-rail shade to the bottom (3.1; not verified on such a shade here). Callers
of `ShadeAutoApi.control()` must pass the middle value they want to keep as
`middle`. The PerfectSheer cover therefore always sends both fields: the
current vane target when lowering to 0 or closing, and an explicit
`MiddleRailPosition: 0` for any other height (4.4 would reset it anyway).

### 4.4 Vanes are only honoured at bottom 0 (PerfectSheer)

A `MiddleRailPosition` target is accepted whatever the bottom position is, but
the hub only keeps it when the bottom target is 0 (C2b, C5, "restore"); the
vanes themselves move once the rail is down. With a bottom target above 0 the
hub quietly rewrites the vane target back to 0:

- **At the end of a lift** (C2a): `{"BottomRailPosition": 50,
  "MiddleRailPosition": 100}` from bottom 0 was accepted with
  `TargetMiddleRailPosition=100`. When the lift completed 30.4 s later (reply
  stamp 05:12:33.684, change logged 05:13:04.105) the same update showed
  `BottomRailPosition 0 -> 50` and `TargetMiddleRailPosition 100 -> 0`.
  `MiddleRailPosition` never left 0.
- **While stationary** (C6c): with the shade stopped at bottom 25,
  `{"BottomRailPosition": 25, "MiddleRailPosition": 100}` set
  `TargetMiddleRailPosition=100`, and 9.4 s later the hub changed it back to 0
  on its own (`TargetMiddleRailPosition 100 -> 0` at 05:17:20.612 after a
  05:17:11 command). The vanes did not move.
- C4b is consistent with this but does not prove it on its own: the raise from
  bottom 0 / vanes 100 was a bottom-only command (C4a), so the vane target was
  already 0 by 4.3 before the motor moved; stopping at 25 then reported
  `MiddleRailPosition 100 -> 0` together with the new bottom position. The
  clean evidence is C2a.
- A tilt sent as a separate command while the shade was still lowering to 0
  was also honoured (C5, via the vendor app: `TargetBottom 25 -> 0` at
  05:23:36.5, `TargetMiddle 0 -> 100` 7.3 s later while the rail was still
  travelling, both actuals together at 05:24:05.7). The app's exact payload is
  unknown, so from `/control` send both fields in one request anyway.

So the physical model is: vanes can be tilted only when the shade is all the
way down; raising the shade closes them. `status.json` fits the same model:
every shade above bottom 0 (14500 and 3190 at 100, 23500 at 35, 23388 at 30)
has vanes 0 and vane target 0; the only non-zero vane value (12177 at 50) is on
the shade at bottom 0.

### 4.5 No range validation

`BottomRailPosition: 250` (C6a) and `MiddleRailPosition: 250` (C6b) were both
accepted with `Error: 0`, echoed back unchanged, and stored as
`TargetBottomRailPosition=250` / `TargetMiddleRailPosition=250`. Each 250
target survived only until the next command about 4 s later overwrote it (C6b
set `TargetBottom 250 -> 25`, C6c set `TargetMiddle 250 -> 100`); the hub was
never seen to clamp, reject or revert one by itself, and what the motor did
with 250 is **unverified** (no motion could have been reported in 4 s).
**Clients must clamp to 0-100 themselves.** `clamp_position()` in `api.py`
does this, and `ShadeAutoApi.control()` applies it to both fields before
sending, so nothing above the API layer can send an out-of-range value; the
cover entity clamps its inputs with the same helper.

### 4.6 MotorStop, StatusRequest and the other action fields

`{"MotorStop": 170}` on its own is accepted (`{"PeripheralUID": 12177,
"MotorStop": 170, "Error": 0, ...}`, C4b) and stops the motor. Sent 8.5 s into a
full raise from 0 (C4a), the shade stopped at 25. About 35 s after the stop the
hub reported, in one update, `BottomRailPosition 0 -> 25`,
`TargetBottomRailPosition 100 -> 25` and `MiddleRailPosition 100 -> 0`. So
**after a stop the hub rewrites the target to the actual position**; a client
should not expect the old target to survive.

**`StatusRequest: 0` makes one shade report in.** In the 2026-10-23 log,
`{"PeripheralUID": 12177, "StatusRequest": 0}` (plus `TaskID`, `Timestamp`,
`ThingName`) was answered `{"PeripheralUID": 12177, "StatusRequest": 0,
"Error": 0, ...}` with hub stamp 04:37:47.098. 2.2 s later the stream carried
`{"Status": 1792744669337, "PeripheralList": [12177]}` (2.5), and the next
`/status` showed the shade's `Timestamp` moved from 1792731364 (00:56:04, when
the hub had last heard from it) to 1792744669 (04:37:49). No position or target
changed and the shade did not move. kedube describes the same request as the
vendor app's per-shade refresh, captured from the app on 2026-10-14, with the
shade's `Timestamp` moving within 5-10 s in its tests ("Waking a blind").

Why it matters: a report-in is how the hub corrects a stale position. kedube
saw the hub keep a wrong `MiddleRailPosition` for an hour until the shades
reported in, and a shade that accepted a move but did not go keep "old position,
new target" until it was asked; a report-in also overwrites the target with the
actual position ("Waking a blind"). A per-shade `StatusRequest` before a retry
is planned for 0.6.0; 0.5.0 does not send it.

**Only ever send it with a `PeripheralUID`.** kedube found that without one,
`StatusRequest` becomes a hub-wide report-in (a `RoomID` alone does not confine
it), and that a hub-wide report-in can move shades: on one user's hub
(its issue #5, three type 48/1 roller shades, hub 6.1.25, motors 2.4.1) a
hub-wide `ReportBatteryLevel` followed by a `StatusRequest` to each shade
closed the shades twice, with no move command sent; lone per-shade
`StatusRequest`s closed nothing there, so the hub-wide request was the likely
trigger. On another hub a `ReportBatteryLevel` closed nine type 33 shades.
`ReportBatteryLevel` is not confined by a `PeripheralUID` either (kedube's
isolated tests of 2026-10-14 moved shades in other rooms), so this project never
sends it.

**The 2026-10-22 test with 170 did not show this.** `{"RequestModuleInfo": 170}`
(C8a) and `{"StatusRequest": 170}` (C8b) were both accepted with `Error: 0` and
echoed, and no status change was logged before the next command was sent (40 s
after C8a, about three minutes after C8b); the log does not record how long
each watch kept polling. (The log does print a `TargetMiddleRailPosition 100 ->
0` line under the C8a header; its timestamp, 05:34:16.528, is the C8 remote-move
update printed late, not a C8a effect.) That run could not have seen a report-in
anyway: hubwatch did not track the per-shade `Timestamp` then, and its
`/notification` reader could not show events (2.5). Whether the value 170 also
works is therefore **unverified**; send 0, as the vendor app does. kedube found
that `RequestModuleInfo: 0` with a `PeripheralUID` also made the addressed
shade (types 32 and 33) report in within 10 s, with no extra fields in the
reply ("Control verbs"); not tried here.

170 is 0xAA. It is the value the registration template gives every
"do something" field (`MotorStop`, `SetMotorToTopLimit`, `MotorFineTuneToUp`,
...), so it looks like a generic "execute" magic number. That is an inference
from the template, not a tested fact. kedube's reading of the vendor app agrees
for the motor actions and adds that report and settings fields (`StatusRequest`,
`ReportBatteryLevel`, the limit fields) take 0 instead.

### 4.7 TaskID and Timestamp conventions

- `TaskID`: the integration uses the current time in milliseconds masked to
  31 bits (`int(time.time()*1000) & 0x7FFFFFFF`), so it is unique per command
  and fits a signed 32-bit int. The hub echoes it unchanged (C2a:
  `1658991231` out and back). Nothing was observed that depends on the value.
- `Timestamp`: whole seconds. `ShadeAutoApi.control()` keeps it **strictly
  increasing** across commands (`_last_ts`), bumping by one if two commands
  land in the same second. The code comment says the hub expects this; it was
  not tested today, so treat "the hub rejects non-increasing timestamps" as
  **unverified** and keep the convention anyway.
- `RequestTimestamp` / `Timestamp` in the reply: see "Timestamps" in section 1.

### 4.8 Pacing

`/control` posts are serialised behind a lock and spaced at least
`send_spacing_sec` apart (`DEFAULT_SEND_SPACING`). Up to 0.4.x the default was
0.75 s, because the previous maintainers found back-to-back commands
unreliable. 0.5.0 raises it to 1.5 s on kedube's measurements ("Control
commands are paced"): the hub has one radio and loses commands sent faster than
it can transmit them, while still answering `Error: 0` and storing every
target. Thirteen commands sent 1.0-1.2 s apart still lost a few shades; 1.3 s
lost none when sent by hand but now and then one from an automation; 1.5 s
survived several whole-house runs, although longer use still lost the odd
command, so kedube ships 1.6 s. A unique `Timestamp` and `TaskID` per command
did not help. None of this was re-tested here, with five shades
(**unverified**). On a hub with many shades, raise the option if commands get
lost; verify-and-retry catches a lost command, but only after the verify delay.

### 4.9 Worked example: open the vanes from Home Assistant

Goal: shade 12177 ("Kitchen") should end fully lowered with its vanes open.

1. Know the ThingName (from `/registration`) and the UID (from
   `/GetAllPeripheral` or `/status`).
2. Send one `/control` with **both** position fields. Do not send
   `MiddleRailPosition` alone (4.2), and do not send `BottomRailPosition` alone
   (that would close the vanes, 4.3):

   ```http
   POST http://<host>:10123/NM/v1/control
   Content-Type: application/json

   {"PeripheralUID": 12177, "TaskID": 1659037383, "Timestamp": 1792660399,
    "ThingName": "us-east-1:00000000-0000-0000-0000-000000000000_0000000000000000",
    "BottomRailPosition": 0, "MiddleRailPosition": 100}
   ```

3. Expect the echo with `Error: 0` (section 2.4). If `Error` is 2, the
   payload was malformed; nothing moved.
4. Expect `/status` to show `TargetBottomRailPosition=0` and
   `TargetMiddleRailPosition=100` on the very next poll, while
   `BottomRailPosition` / `MiddleRailPosition` keep their old values.
5. Poll `/status` until `MiddleRailPosition == 100`. If the shade had to lower
   first, that took 28-34 s in today's runs (C2b, "restore"); a tilt from an
   already-lowered shade took about 11 s via the vendor app (session notes).
   No intermediate positions will appear (section 5).
6. To close the vanes again send `{"BottomRailPosition": 0,
   "MiddleRailPosition": 0}`; to half-open them send `{"BottomRailPosition": 0,
   "MiddleRailPosition": 50}` (50 is a real resting value, `status.json`).

This is what the integration does in `cover.py`: `async_set_cover_tilt_position`,
`async_open_cover_tilt` and `async_close_cover_tilt` all send `(bottom 0,
middle tilt)` through `ShadeAutoApi.control(uid, bottom=0, middle=tilt)`, which
lowers the shade first if it is not already at 0. `bottom` is keyword-only and
required, so the mandatory-bottom rule (4.2) cannot be violated from the API.

### 4.10 Evidence table

Times are the client's wall clock from the log (America/Detroit, EDT, UTC-4).
"Sent" is the hub's millisecond `Timestamp` from the `/control` reply, shown on
that clock with the raw value in brackets (`1792660353684` is 05:12:33.684);
each latency is "reported" minus "sent", where "reported" is when `hubwatch`
saw the `/status` change. All commands were to UID 12177.

| Entry | State before (bottom/vanes) | Payload (command fields) | Sent (hub ms stamp) | Reply | What `/status` did |
|---|---|---|---|---|---|
| C2a | 0 / 0 | `BottomRailPosition 50, MiddleRailPosition 100` | 05:12:33.684 (1792660353684) | `Error 0`, both echoed | Targets 50/100 at once. 05:13:04.1 (30.4 s): `Bottom 0 -> 50`, `TargetMiddle 100 -> 0`. Vanes stayed 0. |
| C2b | 50 / 0 | `BottomRailPosition 0, MiddleRailPosition 100` | 05:13:19.834 (1792660399834) | `Error 0` | 05:13:47.9 (28.1 s): `Bottom 50 -> 0` and `Middle 0 -> 100` in one update. |
| C4a | 0 / 100 | `BottomRailPosition 100` | 05:15:32.285 (1792660532285) | `Error 0`, reply adds `MiddleRailPosition 0` | Targets 100/0 at once. |
| C4b | moving, 8.5 s after C4a | `MotorStop 170` | 05:15:40.774 (1792660540774) | `MotorStop 170, Error 0` | 05:16:16.3 (35.5 s after the stop): `Bottom 0 -> 25`, `TargetBottom 100 -> 25`, `Middle 100 -> 0`. |
| C6a | 25 / 0 | `BottomRailPosition 250` | 05:17:02.605 (1792660622605) | `Error 0`, `250` echoed, `MiddleRailPosition 0` added | `TargetBottom=250`. Motion not observed before the next command (4.3 s). |
| C6b | 25 / 0 (bottom target still 250 from C6a) | `BottomRailPosition 25, MiddleRailPosition 250` | 05:17:06.914 (1792660626914) | `Error 0`, `250` echoed | `TargetBottom 250 -> 25`, `TargetMiddle=250`; overwritten by C6c 4.3 s later. |
| C6c | 25 / 0 | `BottomRailPosition 25, MiddleRailPosition 100` | 05:17:11.194 (1792660631194) | `Error 0` | `TargetMiddle=100` (was 250 after C6b); 05:17:20.6 (9.4 s): `TargetMiddle 100 -> 0`. No motion. |
| C5 | 25 / 0 | none (remote, then vendor app) | | | Remote phase (per session notes, before 05:23:36): nothing. App phase: 05:23:36.5 `TargetBottom 25 -> 0`; 05:23:43.8 `TargetMiddle 0 -> 100`; 05:24:05.7 `Bottom 25 -> 0` and `Middle 0 -> 100`. |
| C8 | 0 / 100 | none (remote; "raise and stop" per session notes, the log header says only "remote move") | | | 05:34:16.5: `Bottom 0 -> 45`, `TargetBottom 0 -> 45`, `Middle 100 -> 0`, `TargetMiddle 100 -> 0`, all in one update (the last line is printed under the C8a header). |
| C8a | 45 / 0 | `RequestModuleInfo 170` | 05:34:28.971 (1792661668971) | `Error 0`, echoed | Nothing logged before C8b. |
| C8b | 45 / 0 | `StatusRequest 170` | 05:35:09.686 (1792661709686) | `Error 0`, echoed | Nothing logged before "restore". The watcher tracked neither the per-shade `Timestamp` nor the stream, so a report-in would not have shown (4.6). |
| restore | 45 / 0 | `BottomRailPosition 0, MiddleRailPosition 100` | 05:38:03.906 (1792661883906) | `Error 0` | 05:38:38.2 (34.3 s): `Bottom 45 -> 0` and `Middle 0 -> 100`. |
| C1 | 0 / 50 (target 50) | `MiddleRailPosition 0` | 05:09:50.194 (1792660190194) | `Error 2`, no fields echoed | Nothing; targets unchanged. |
| T2 | 0 / 50 (target 50) | `BottomRailPosition 0` | 05:11:04.575 (1792660264575) | `Error 0`, reply adds `MiddleRailPosition 0` | `TargetMiddle 50 -> 0` in the next snapshot; vanes still 50. |
| C3 | 0 / 50 (target 0) | `BottomRailPosition 0, MiddleRailPosition 0` | 05:11:11.974 (1792660270974) | `Error 0`, both echoed | 05:11:32.9: `Middle 50 -> 0` (21.9 s after C3, 28.3 s after T2; which command the motor acted on cannot be told apart). |
| T3 | 0 / 0 | `MiddleRailPosition 100` | 05:11:41.274 (1792660301274) | `Error 2`, no fields echoed | Nothing. |

`BatteryVoltage`, `CalibrationState` and `SpeedInformation` were tracked
throughout and never changed.

The 2026-10-23 log adds one command, `StatusRequest 0` to 12177 at 04:37:47.098
(1792744667098), with the shade at 0 / 0: `Error 0`, echoed; a stream event
2.2 s later; the per-shade `Timestamp` moved; nothing moved (4.6).

---

## 5. Timing and observability

### 5.1 End-state-only reporting

The hub never reports an intermediate position. `Target*` fields change the
moment a command is accepted from `/control` or the vendor app (remote moves:
see 5.4); the actual `BottomRailPosition`
and `MiddleRailPosition` jump straight from the old value to the new one when
the motor stops, and when both rails move they are reported in the same
update (C2b, "restore", C4b). With a 1 s poll interval there was not a single
diff line showing a position between the start and end values. The
`/notification` stream showed the same pattern in the 2026-10-23 log: one
event when the target changed, one when the move had ended, nothing in between
(2.5).

This is why the cover entity estimates the position during a move
(`full_travel_time_sec`, default 28 s) instead of reading it from the hub.

### 5.2 Observed settle latencies

Time from the command (hub millisecond timestamp in the reply) to the `/status`
poll that showed the new actual position:

| Move | Latency | Evidence |
|---|---|---|
| Lift 0 -> 50 | ~30 s (30.4 s) | C2a |
| Lower 50 -> 0 plus tilt 0 -> 100 | ~28 s | C2b |
| Lower 45 -> 0 plus tilt 0 -> 100 | ~34 s | restore |
| Stop during a raise (ended at 25) | ~35 s after the stop command | C4b |
| Vane tilt only, shade already lowered, via vendor app | ~11 s | session notes |
| Vane tilt 0 -> 55 only, shade already lowered, via vendor app | 21.5 s from the target-change event to the end-of-move event | 2026-10-23 log |
| App: lower 25 -> 0, then tilt 7 s later | 29 s after the lower, 22 s after the tilt | C5 |
| Hub reverting an impossible vane target while stationary | ~9 s | C6c |

Part of each latency is motor travel and part is the hub waiting for the
shade's RF report; the split is **unverified**. The two tilt-only figures
disagree (11 s and 21.5 s) for reasons that are **unverified**; only the second
was machine-logged. The practical rule: after a command, expect the `Target*`
fields to reflect it on the very next poll, but
do not expect the actual positions to change for about 30 s, and do not treat
"no change after 10 s" as failure. The integration's verify delay (default
60 s) and motion failsafe (120 s) are consistent with this.

### 5.3 Notifications

*Until 2026-10-23 this section was titled "Notifications never fire". That was
wrong; 2.5 explains the mistake.*

The `/notification` stream announces, within a second or two, that a shade's
target or position changed, or that a shade reported in (2.5, 2026-10-23 log).
It carries only the shade's id, so a client still reads `/status` to learn what
changed, and it does not replay what happened while it was closed, so a client
also reads `/status` after every reconnect. A push-driven client therefore
still polls, just much less urgently. 0.5.0 does not use the stream yet and
polls `/status` as described in 2.3; push updates are planned for 0.6.0.

### 5.4 What the hub sees of app and remote

- **Vendor app.** Commands from the app go through the hub, so they show up
  immediately as `Target*` changes and later as actuals, exactly like our own
  commands (C5: `TargetBottom 25 -> 0` at 05:23:36.5, `TargetMiddle 0 -> 100`
  at 05:23:43.8, actuals at 05:24:05.7).
- **Physical RF remote.** Inconsistent. In C5 the remote was used first (the
  session notes say "close vanes, raise fully") and **nothing changed in
  `/status`** during that phase; the hub still believed the shade was at 25
  and the subsequent app command started from that belief. In C8 a remote move
  (session notes: "raise and stop"; the log header says only "remote move")
  **did** appear, once the shade stopped, as a single update `Bottom 0 -> 45`,
  `TargetBottom 0 -> 45`, `Middle 100 -> 0`, `TargetMiddle 100 -> 0`. So the
  hub sometimes learns remote-driven positions after the fact and sometimes
  does not. Why is **unverified**. (One guess that fits both runs: a stop
  mid-travel makes the shade report its position, while running to an end
  limit does not; C5 was a full raise, C8 a stop at 45. It is only a guess;
  note that the hub's own target was 0 in C8, so "reports only when it reaches
  a position the hub asked for" does not fit.) Treat hub positions as possibly
  stale after any manual use, and keep polling even when the integration
  believes nothing is moving.
- **Stale positions after a remote move** might be fixed by a per-shade
  `StatusRequest` (4.6), which makes the shade report where it is. That has
  not been tried on a shade the hub had lost track of (**unverified**). The
  stream may also say more than `/status` here: in C5 the long-poll request
  that covered the remote phase lasted 75 s instead of 60 s, which suggests the
  hub wrote something to the stream then (2.5), but what it was was discarded.

---

## 6. Registration template

Each entry of `Peripherals` in `/registration` carries, besides identity, a
block of command-looking fields. **This block is a schema, not state**: all
five shades have byte-identical values (`TargetBottomRailPosition 100`,
`MiddleRailPosition 100`, `SpeedInformation 15`, `CalibrationState 1`,
`FirmwareVersion ""`), while `/status` captured in the same second shows real,
differing positions (12177 at 0/50, 23500 at 35/0), `SpeedInformation 0`,
`CalibrationState 0` and `FirmwareVersion "2.4.1"`. Read it as "fields the hub
knows how to send to a shade, with their default or trigger values".

| Field | Template value | What is known |
|---|---|---|
| `BottomRailPosition`, `TargetBottomRailPosition` | 100 | Lift position / target. Fully exercised (section 4). |
| `MiddleRailPosition`, `TargetMiddleRailPosition` | 100 | Vane tilt / target on PerfectSheer. Fully exercised. Other families: 3.1. |
| `MotorStop` | 170 | Stops the motor. Exercised (C4b). |
| `RequestModuleInfo` | 0 | Sent as 170: accepted, no visible effect seen (C8a), but the watcher could not have seen a report-in (4.6). kedube: as 0 with a `PeripheralUID`, the shade reports in. Purpose otherwise **unverified**. |
| `StatusRequest` | (hub level, 0) | **Sent as 0 with a `PeripheralUID` on `/control`: that shade reports in** (`Error 0`, a stream event 2.2 s later, its `Timestamp` in `/status` moves, nothing moves; 2026-10-23 log, 4.6). Sent as 170 in C8b: accepted, effect unknown. **Never send it without a `PeripheralUID`**: kedube found it then sweeps the whole hub, and a hub-wide report-in closed one user's roller shades (4.6). Also appears at the top level of `/registration`. |
| `CleanPairing` | (hub level, 0) | **Unverified.** Not sent. Top level of `/registration` only. Name suggests it erases the hub's pairings. **Never send.** |
| `ReportBatteryLevel` | (hub level, 0) | Not sent. Top level of `/registration` only. kedube: the vendor app's battery report for a room (`RoomID`) or the whole hub; a `PeripheralUID` does not confine it; a hub-wide one closed type 48/1 roller shades on one hub and nine type 33 shades on another (4.6). **Never send.** |
| `Switch` | 1 | Not sent. kedube: the vendor app's presets, `0` "Best privacy" (bottom 0, middle 100) and `1` "Best view" (100, 100), addressed by `RoomID` + `GroupID`, or the whole hub when neither is given. **It moves shades.** |
| `Favorite` | 0 | Not sent. kedube: sends shades to their stored favourite position, addressed like `Switch`. **It moves shades.** |
| `Calibration`, `CalibrationState` | 0, 1 | **Unverified.** Not sent. Calibration could re-learn travel limits. **Do not send.** |
| `ConfigToScene`, `SetToScene` | 287 | **Unverified.** Not sent. Scene store/recall; 287 may be a scene id or a magic value. |
| `CleanAllScene` | 0 | **Unverified.** Not sent. Name suggests it erases all scenes. **Do not send.** |
| `SetMotorToTopLimit`, `SetMotorToBottomLimit` | 170 | **Unverified.** Not sent. Likely "drive to the stored limit". |
| `MotorFineTuneToUp`, `MotorFineTuneToDown` | 170 | **Unverified.** Not sent. Likely jog the motor a small step. |
| `SetTopLimit`, `CleanTopLimit`, `SetBottomLimit`, `CleanBottomLimit`, `SetMiddleLimit`, `CleanMiddleLimit` | 0 | **Unverified.** Not sent. These program or erase the motor's travel limits. Sending one by accident could make the shade lose its end stops. **Never send casually.** |
| `CleanRfPairing` | 0 | **Unverified.** Not sent. Name suggests it unpairs the shade from the hub. **Never send casually.** |
| `CleanAllPosition` | 0 | **Unverified.** Not sent. **Never send casually.** |
| `MotorSpeedAdjust` | 259 | **Unverified.** Not sent. |
| `ReverseMotorDirection` | 0 | **Unverified.** Not sent. **Never send casually.** |
| `SmartDialSwitch` | 1 | **Unverified.** Not sent. |
| `BatteryVoltage` | 100 | Same field as in `/status`. |
| `SpeedInformation` | 15 | Meaning **unverified**; 0 in live status. |
| `FirmwareVersion` | `""` | Empty in the template; `"2.4.1"` in live status. |

Only `BottomRailPosition`, `MiddleRailPosition`, `MotorStop`,
`RequestModuleInfo` and `StatusRequest` have ever been sent from this project.
Anything with Limit, Calibration, Pairing, Clean or Reverse in its name should
be treated as potentially destructive until someone proves otherwise on a
shade they are willing to re-calibrate.

---

## 7. Open questions / future probes

Things we do not know and how one might find out. Everything here is safe to
try except where noted.

1. **Which request fields are actually required?** Try `/control` without
   `ThingName`, without `TaskID`, and with a non-increasing `Timestamp`, and
   `/status` with an empty body `{}`. Expect either normal replies or
   `Error`/`status.code` values we have not seen yet; record both.
2. **The `Error` code vocabulary.** Only 0 and 2 have been seen on `/control`
   and only `status.code` 0 and 1 on `/GetAllPeripheral`. Send an unknown
   `PeripheralUID` and a non-numeric position to collect more.
3. **What does the motor do with an out-of-range target** (`250`)? Send it and
   wait a full minute without a follow-up command. Clamp in the client
   regardless.
4. **Tilt-only latency via `/control`.** The vendor app gave ~11 s (session
   notes) and 21.5 s for 0 -> 55 (2026-10-23 log); confirm with
   `{"BottomRailPosition": 0, "MiddleRailPosition": N}` on a shade already at
   bottom 0, for a few values of N.
5. **Does `/notification` ever work?** *Answered 2026-10-23: yes.* It is a
   stream that never ends, and our clients read it wrongly (2.5). Follow-up
   questions are 13 and 14.
6. **Why are remote moves sometimes invisible** (C5) and sometimes reported
   (C8)? Repeat remote moves of different kinds (full travel vs stop mid-way,
   tilt vs lift) with `hubwatch.py --notify` running and `Timestamp` added to
   `--fields`, and note which ones show up. A stream event or a new per-shade
   `Timestamp` without a position change would mean the hub heard the shade
   but did not take its position. Then send `StatusRequest: 0` to the shade
   (with its `PeripheralUID`) and see whether the position catches up.
7. **`PairingMode` 4 vs 0 and `OTA` 1 vs 0** between `/registration` and
   `/status`: does `/registration` report capabilities and `/status` current
   state, or something else?
8. **`SpeedInformation` and `CalibrationState`.** Watch them across a full
   travel and during battery change; they never moved today.
9. **Per-shade `Timestamp` in `/status`.** *Answered 2026-10-23:* it is when
   the hub last heard from the shade, not when the record last changed
   (section 3; 2026-10-23 log; kedube agrees).
10. **`Segment`/`TotalSegment`.** Only testable on a hub with more shades than
    fit in one reply, if such a limit exists.
11. **`SetToScene`, `SmartDialSwitch`.** Unknown. kedube has since identified
    `Switch` and `Favorite` as the vendor app's presets, which move shades
    (section 6); test anything on this list only on a shade whose position you
    do not care about, with the watcher running, and never test the
    Limit/Clean/Reverse fields on a shade you cannot afford to re-calibrate.
12. **Stop during a vane-only move.** `stop_cover_tilt` sends the same
    `MotorStop: 170` as `stop_cover` (one motor), but a stop while the vanes
    are tilting at bottom 0 was never tried. Send `{"BottomRailPosition": 0,
    "MiddleRailPosition": 100}` to a lowered shade with closed vanes, then
    `MotorStop` about 3 s later, and record what `MiddleRailPosition` and
    `TargetMiddleRailPosition` end up as and how long the hub takes to report
    them. A lift stopped above 0 ends with the vanes closed and is reported
    about 35 s later (4.6); whether a stopped tilt keeps a partial vane
    position is unknown.
13. **How long does a stream stay healthy on this hub?** kedube found that
    long-lived streams can go quiet and reopens every 300 s; the 2026-10-23
    probe lasted only 200 s. Leave `hubwatch.py --notify` running for a few
    hours, move shades now and then, and check that every `/status` change
    still has its event. Note when events stop, and whether reopening brings
    them back.
14. **`UpdateTime` objects.** kedube reports them after renames in the app
    (2.5). Rename a shade in the app with `hubwatch.py --notify` running, and
    record the object.
15. **`StatusRequest` on PerfectSheer.** One try, at 0 / 0, moved nothing
    (4.6). Repeat with the shade raised and with its vanes open, and with a
    position the hub has lost track of (question 6), before 0.6.0 starts
    sending it before retries. Whether `StatusRequest: 170` (C8b) does the same
    as 0 is open; there is no reason to send 170.
16. **Other shade families.** Everything in 3.1 outside PerfectSheer comes from
    other projects. If you own a single-rail, two-rail, SmartDrape or shutter
    shade, save a `/status` document with `hubwatch.py --raw`, then send
    commands that move one rail at a time with `hubwatch.py --control`
    (always including `BottomRailPosition`, 4.2), and check the claims in
    3.1: that the middle rail reads 0 on single-rail shades, and on two-rail
    shades the axis and what a bottom-only command does to the upper rail.

To keep collecting evidence: the characterization logs live next to this file
(`docs/hub-characterization-2026-10-22.log`,
`docs/hub-characterization-2026-10-23.log`). When you learn something new, run
`tools/hubwatch.py` with its output appended to a new dated log in `docs/`, give
the log a header that says how it was produced, and cite the entry here.
