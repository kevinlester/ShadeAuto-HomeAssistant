# ShadeAuto hub local HTTP protocol

This document describes what the ShadeAuto (NienMade) shade hub actually does on
its local HTTP API, as observed on one real hub. It is written for someone who
maintains the Home Assistant integration in `custom_components/shadeauto/` and
has never seen the hardware.

Nothing here comes from vendor documentation; there is none. Every statement is
backed by one of these sources, and the source is named next to the claim:

| Source | What it is |
|---|---|
| `tests/fixtures/registration.json` | A real `/registration` reply. ThingName and Wi-Fi SSID scrubbed. |
| `tests/fixtures/get_all_peripheral.json` | A real `/GetAllPeripheral` reply. ThingName scrubbed and GeoLoc zeroed. It holds one room, five groups and five shade names; whether any of those names was altered before commit is not recorded. |
| `tests/fixtures/status.json` | A real `/status` reply, captured in the same second as the two above. |
| `tests/fixtures/error_invalid.json` | The hub's error envelope for a `/GetAllPeripheral` call with `ThingName: null`. |
| "the log" | [`hub-characterization-2026-10-22.log`](hub-characterization-2026-10-22.log), a timestamped record produced by `tools/hubwatch.py` during the characterization session on 2026-10-22. Each `#####` header is a separate hubwatch run (each starts with a fresh initial snapshot, so changes between runs were not recorded), labelled C1, T2, C3 (also called T1), T3, C2a, C2b, C4a, C4b, C6a, C6b, C6c, C5, C8, C8a, C8b and "restore". The first four were transcribed into the file afterwards from the session's hubwatch output; the rest were appended live. All of them were run against shade 12177 ("Kitchen"). |
| "session notes" | What the shade's owner reported doing with the physical remote and the vendor app during C5 and C8, and the ~11 s vane-tilt timing seen with an earlier ad-hoc watcher before the log existed. These are not machine-logged and cannot be re-read from a file. |
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
protocol calls the vane tilt `MiddleRailPosition`.

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
`/notification`; whether those two actually require it is **unverified**
(it was never omitted). `/status` and `/registration` were only ever called
with `{"Timestamp": <seconds>}` and work with that.

**Two reply styles.** `/registration`, `/status` and `/control` answer with a
flat object that carries `Error` (0 = ok) and the hub's timestamps.
`/GetAllPeripheral` answers with an envelope
`{"status": {"code", "error"}, "results": {...}}`. See each endpoint.

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
masked the same way by between -8 and +36 ms (C2a 21 ms, C2b 19 ms). Log times
are the client's wall clock in America/Detroit (EDT, UTC-4). The latencies in
4.10 and 5.2 rely on this agreement.

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
from this reply picks the device model string (`entity.py`: `(48, 3)` is shown
as "PerfectSheer").

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
`coordinator.py`), and a shade counts as moving while actual != target on either axis
(`AXIS_FIELDS`, `is_moving`). After a `MotorStop` the entity stops showing
motion at once, but the coordinator keeps polling at the motion interval
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
only the wire format. Five rules you must know before sending anything:

1. Always send `BottomRailPosition`. A payload with `MiddleRailPosition` alone
   is rejected with `Error: 2` (4.2).
2. Omitting `MiddleRailPosition` sets the vane target to 0, i.e. closes the
   vanes (4.3).
3. Vanes are only honoured while the bottom target is 0. You may send both
   fields in one command; the shade lowers, then tilts (4.4).
4. The hub does not validate 0-100; clamp yourself (4.5).
5. `MotorStop: 170` stops the motor, and the hub then re-targets the shade to
   wherever it stopped (4.6).

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
| command fields | one or more | `BottomRailPosition`, `MiddleRailPosition`, `MotorStop`. `MiddleRailPosition` is rejected unless `BottomRailPosition` is also present (4.2); omitting `MiddleRailPosition` sets the vane target to 0 (4.3). Section 6 lists the other field names the hub knows; most are limit, calibration or pairing commands and must not be sent. |

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
so the entity shows the hub's last known position instead of "opening". The
cover entity (`cover.py`) passes both
fields on every open, close, set-position and tilt call: lowering to 0 and
close keep the current vane target, any other height sends
`MiddleRailPosition: 0` explicitly (4.4 would reset it anyway), and tilt
commands send `BottomRailPosition: 0`. The verify-and-retry logic resends the
same pair, once, and only when the hub never reflected the targets it was
sent; a retry overtaken by a newer command does not overwrite that command's
pending state.

### 2.5 `/notification`

A long-poll that is supposed to deliver change events.

**Request** (as `hubwatch.py --notify` sends it; the integration no longer
calls this endpoint)

```json
{"ThingName": "us-east-1:00000000-0000-0000-0000-000000000000_0000000000000000", "Timestamp": 1792660684, "Timeout": 50.0}
```

`Timestamp` is presumably a watermark ("events since") and `Timeout` the number
of seconds the hub may hold the connection. Both meanings are inherited from
the previous integration and are **unverified**: no event was ever returned,
hubwatch simply sends the current time as `Timestamp` on every poll, and the
hub did not visibly honour the hold (see below). The example values are
illustrative; hubwatch does not log the payload. Whether any other `Timeout`
value (a longer hold, or `-1` for "indefinitely") behaves differently is
**unverified**; only 50 was tried.

**Response.** **No event was ever received.** During the C5 run the watch tool
polled with `Timeout: 50` continuously; every poll ended without a body, logged
as `NOTIFY (hub returned without an event)` at 05:19:04, 05:20:04, 05:21:04,
05:22:04 and 05:23:19. Those polls covered the physical-remote phase of C5.
The vendor-app phase (05:23:36 to 05:24:05) fell inside the poll started at
05:23:19, whose end is not in the log; the session notes say that no event came
for the app's changes or for our own `/control` commands either.

Look at the spacing: the notify thread started with the watch at 05:18:04, and
four of the five polls ended exactly 60 s after the previous one, which is
hubwatch's client-side socket timeout (`Timeout + 10`, `hubwatch.py`), not the
50 s hold that was requested; the fifth took 75 s and is unexplained.
`hubwatch.Hub.notification()` returns `""` both for an empty body and for a
client-side timeout, so the log cannot prove which ended each poll, but a hub
that answered at the requested hold would have produced about 50 s spacing. The
most likely reading is that the client gave up while the hub still held the
connection. The log therefore cannot show the hub ever answering a
`/notification` request at all, with or without a body, and whether it honours
`Timeout` is **unverified**. Any client must impose its own timeout on this
call. What is certain is that no event was delivered during a window that
contained a remote move.

The event format the previous integration expected (one or more concatenated
JSON objects with a `PeripheralList` array and a `Status` or `Timestamp` key)
is **unverified**; nothing matching it was ever observed.

**What the integration uses it for.** Nothing. The previous integration
long-polled here while a command was pending; that code is gone, `api.py` has
no notification method and the options no longer include a notification
timeout. The current coordinator relies on `/status` polling only.
`tools/hubwatch.py --notify` is the only client that still calls this endpoint.
`README.md` ("Upgrading from 0.3.x") records that the "Notification timeout"
option was removed for the same reason.

**Errors.** None observed.

---

## 3. Shade status record

Every element of `Peripherals` in a `/status` reply has exactly these fields
(`status.json`).

| Field | Type | Units / range | Meaning | Integration uses it? |
|---|---|---|---|---|
| `PeripheralUID` | int | | The shade's id. Key for `/control` and for matching `/GetAllPeripheral`. | Yes (entity identity). |
| `ModuleType` | int | 48 observed | Device class code. Same number as the hub's `DeviceType`. Meaning **unverified**. | Copied into the coordinator data. The device model string is derived from the same pair as reported by `/GetAllPeripheral` (`entity.py`: (48, 3) is "PerfectSheer"). |
| `ModuleDetail` | int | 3 observed | Sub-type; all five PerfectSheer shades report 3. Meaning **unverified**. | See `ModuleType`. |
| `RoomID` | int | | Matches `RoomList[].RoomID` in `/GetAllPeripheral`. | No. |
| `GroupID` | int | | Matches `GroupList[].GroupID`. | No. |
| `BatteryVoltage` | int | percent, 0-100 | **A percentage despite the name.** 100 observed on every shade. `battery_percent()` treats values outside 0-100 as unknown. | Yes (battery sensor, low-battery flag). |
| `TargetBottomRailPosition` | int | 0-100 (not enforced, see 4.5) | The lift target the hub currently holds. Changes the moment a command is accepted via `/control` or the vendor app. Remote-driven moves are different: the target was either never updated (C5) or updated together with the actual once the shade stopped (C8); see 5.4. | Yes (motion tracking: moving while actual != target; opening/closing direction). |
| `TargetMiddleRailPosition` | int | 0-100 (not enforced) | The vane target the hub currently holds. Set by `/control` and app commands, also rewritten by the hub itself (sections 4.3, 4.4); for remote moves see 5.4. | Yes (motion tracking). |
| `BottomRailPosition` | int | 0-100 | Actual lift. **0 = fully lowered, 100 = fully raised.** Only updated when motion ends. | Yes (cover position). |
| `MiddleRailPosition` | int | 0-100 | Actual vane tilt on PerfectSheer. **0 = vanes closed, 100 = vanes open** (light through). Intermediate values are real: `status.json` shows shade 12177 at 50. Only updated when motion ends. | Yes (cover tilt position while `tilt_enabled`, the default; motion tracking). |
| `SpeedInformation` | int | 0 observed | Never changed during any move in the log (it is one of the fields `hubwatch.py` diffs on every poll, `DEFAULT_FIELDS`). 15 in the registration template. Meaning **unverified**. | No. |
| `CalibrationState` | int | 0 observed | Never changed in the log (also a diffed field). 1 in the registration template. Meaning **unverified**. | No. |
| `FirmwareVersion` | string | `"2.4.1"` | Shade motor firmware. | Yes (device `sw_version`). |
| `Timestamp` | int | seconds | Differs per shade and is older than the reply's own timestamps, so it is probably the time this record last changed. **Unverified.** | No. |

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

---

## 4. Control semantics

Everything in this section was learnt by sending commands to shade 12177 and
watching `/status` at 1 s intervals. The table at the end of the section lists
the raw evidence.

### 4.1 The three command fields that matter

| Field | Value | Effect |
|---|---|---|
| `BottomRailPosition` | 0-100 | Move the bottom rail. 0 lowers fully, 100 raises fully. |
| `MiddleRailPosition` | 0-100 | Set the vane tilt. 0 closed, 100 open. Only meaningful when the shade is fully lowered (4.4). |
| `MotorStop` | 170 | Stop whatever the motor is doing right now. |

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
`StatusRequest`-only (C8b) payloads contain no `MiddleRailPosition` at all.
Whether a stop leaves a non-zero vane target alone is **unverified** (the vane
target was already 0 in C4b, C8a and C8b, so `/status` cannot tell).

Consequence: **a client that sends `BottomRailPosition` alone closes the
vanes.** Callers of `ShadeAutoApi.control()` must pass the vane value they want
to keep as `middle`. The cover entity therefore always sends both fields: the
current vane target when lowering to 0 or closing, and an explicit
`MiddleRailPosition: 0` for any other height (4.4 would reset it anyway).

### 4.4 Vanes are only honoured at bottom 0

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

### 4.6 MotorStop and the other "170" fields

`{"MotorStop": 170}` on its own is accepted (`{"PeripheralUID": 12177,
"MotorStop": 170, "Error": 0, ...}`, C4b) and stops the motor. Sent 8.5 s into a
full raise from 0 (C4a), the shade stopped at 25. About 35 s after the stop the
hub reported, in one update, `BottomRailPosition 0 -> 25`,
`TargetBottomRailPosition 100 -> 25` and `MiddleRailPosition 100 -> 0`. So
**after a stop the hub rewrites the target to the actual position**; a client
should not expect the old target to survive.

`{"RequestModuleInfo": 170}` (C8a) and `{"StatusRequest": 170}` (C8b) were both
accepted with `Error: 0` and echoed, and **no effect was observed**: no status
change was logged before the next command was sent (40 s after C8a, about
three minutes after C8b); the log does not record how long each watch kept
polling. (The log does print a `TargetMiddleRailPosition 100 -> 0` line under
the C8a header; its timestamp, 05:34:16.528, is the C8 remote-move update
printed late, not a C8a effect.) They did no harm either. What they are for is
**unverified**.

170 is 0xAA. It is the value the registration template gives every
"do something" field (`MotorStop`, `SetMotorToTopLimit`, `MotorFineTuneToUp`,
...), so it looks like a generic "execute" magic number. That is an inference
from the template, not a tested fact.

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
`send_spacing_sec` apart (default 0.75 s, `DEFAULT_SEND_SPACING`). The reason
given in `const.py` is that the previous maintainers found back-to-back
commands unreliable. That was not re-tested today (**unverified**), but keep
the spacing; it is cheap.

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
| C8b | 45 / 0 | `StatusRequest 170` | 05:35:09.686 (1792661709686) | `Error 0`, echoed | Nothing logged before "restore". |
| restore | 45 / 0 | `BottomRailPosition 0, MiddleRailPosition 100` | 05:38:03.906 (1792661883906) | `Error 0` | 05:38:38.2 (34.3 s): `Bottom 45 -> 0` and `Middle 0 -> 100`. |
| C1 | 0 / 50 (target 50) | `MiddleRailPosition 0` | 05:09:50.194 (1792660190194) | `Error 2`, no fields echoed | Nothing; targets unchanged. |
| T2 | 0 / 50 (target 50) | `BottomRailPosition 0` | 05:11:04.575 (1792660264575) | `Error 0`, reply adds `MiddleRailPosition 0` | `TargetMiddle 50 -> 0` in the next snapshot; vanes still 50. |
| C3 | 0 / 50 (target 0) | `BottomRailPosition 0, MiddleRailPosition 0` | 05:11:11.974 (1792660270974) | `Error 0`, both echoed | 05:11:32.9: `Middle 50 -> 0` (21.9 s after C3, 28.3 s after T2; which command the motor acted on cannot be told apart). |
| T3 | 0 / 0 | `MiddleRailPosition 100` | 05:11:41.274 (1792660301274) | `Error 2`, no fields echoed | Nothing. |

`BatteryVoltage`, `CalibrationState` and `SpeedInformation` were tracked
throughout and never changed.

---

## 5. Timing and observability

### 5.1 End-state-only reporting

The hub never reports an intermediate position. `Target*` fields change the
moment a command is accepted from `/control` or the vendor app (remote moves:
see 5.4); the actual `BottomRailPosition`
and `MiddleRailPosition` jump straight from the old value to the new one when
the motor stops, and when both rails move they are reported in the same
update (C2b, "restore", C4b). With a 1 s poll interval there was not a single
diff line showing a position between the start and end values.

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
| App: lower 25 -> 0, then tilt 7 s later | 29 s after the lower, 22 s after the tilt | C5 |
| Hub reverting an impossible vane target while stationary | ~9 s | C6c |

Part of each latency is motor travel and part is the hub waiting for the
shade's RF report; the split is **unverified**. The practical rule: after a
command, expect the `Target*` fields to reflect it on the very next poll, but
do not expect the actual positions to change for about 30 s, and do not treat
"no change after 10 s" as failure. The integration's verify delay (default
60 s) and motion failsafe (120 s) are consistent with this.

### 5.3 Notifications never fire

See section 2.5 for the evidence. Nothing in the integration depends on
`/notification` any more; polling `/status` is the only reliable way to learn
that a move finished.

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
| `MiddleRailPosition`, `TargetMiddleRailPosition` | 100 | Vane tilt / target. Fully exercised. |
| `MotorStop` | 170 | Stops the motor. Exercised (C4b). |
| `RequestModuleInfo` | 0 | Sent as 170: accepted, no visible effect (C8a). Purpose **unverified**. |
| `StatusRequest` | (hub level, 0) | Sent as 170 on `/control`: accepted, no visible effect (C8b). Also appears at the top level of `/registration`. **Unverified.** |
| `CleanPairing` | (hub level, 0) | **Unverified.** Not sent. Top level of `/registration` only. Name suggests it erases the hub's pairings. **Never send.** |
| `ReportBatteryLevel` | (hub level, 0) | **Unverified.** Not sent. Top level of `/registration` only. Possibly a hub-level battery refresh, a counterpart to `StatusRequest`. |
| `Switch` | 1 | **Unverified.** Not sent. Possibly enable/disable. |
| `Favorite` | 0 | **Unverified.** Not sent. Possibly "go to favourite position". |
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
4. **Tilt-only latency via `/control`.** The ~11 s figure is from the vendor
   app; confirm it with `{"BottomRailPosition": 0, "MiddleRailPosition": N}`
   on a shade already at bottom 0.
5. **Does `/notification` ever work?** Try a long hold (`Timeout: 300` and
   `-1`), a `Timestamp` of 0, and omitting `ThingName`, with a client timeout
   well above the hold so that a reply at the requested hold can be told apart
   from the client giving up (2.5). The long-poll and its option have already
   been removed from the integration and the README; this probe is only for
   curiosity about the hub, not for the integration.
6. **Why are remote moves sometimes invisible** (C5) and sometimes reported
   (C8)? Repeat remote moves of different kinds (full travel vs stop mid-way,
   tilt vs lift) with the watcher running and note which ones show up.
7. **`PairingMode` 4 vs 0 and `OTA` 1 vs 0** between `/registration` and
   `/status`: does `/registration` report capabilities and `/status` current
   state, or something else?
8. **`SpeedInformation` and `CalibrationState`.** Watch them across a full
   travel and during battery change; they never moved today.
9. **Per-shade `Timestamp` in `/status`.** Confirm it is "last change time" by
   comparing it with the wall clock of a known move.
10. **`Segment`/`TotalSegment`.** Only testable on a hub with more shades than
    fit in one reply, if such a limit exists.
11. **`Favorite`, `Switch`, `SetToScene`, `SmartDialSwitch`.** Probably
    harmless, but unknown. Test on a shade whose position you do not care
    about, with the watcher running, and never test the Limit/Clean/Reverse
    fields on a shade you cannot afford to re-calibrate.
12. **Stop during a vane-only move.** `stop_cover_tilt` sends the same
    `MotorStop: 170` as `stop_cover` (one motor), but a stop while the vanes
    are tilting at bottom 0 was never tried. Send `{"BottomRailPosition": 0,
    "MiddleRailPosition": 100}` to a lowered shade with closed vanes, then
    `MotorStop` about 3 s later, and record what `MiddleRailPosition` and
    `TargetMiddleRailPosition` end up as and how long the hub takes to report
    them. A lift stopped above 0 ends with the vanes closed and is reported
    about 35 s later (4.6); whether a stopped tilt keeps a partial vane
    position is unknown.
13. **Keep collecting evidence.** The characterization log lives next to this
    file (`docs/hub-characterization-2026-10-22.log`). When you learn
    something new, run `tools/hubwatch.py` with its output appended to a new
    dated log in `docs/`, and cite the entry here.
