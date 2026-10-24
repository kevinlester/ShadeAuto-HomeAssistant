"""Constants shared across the ShadeAuto integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "shadeauto"
PLATFORMS: list[Platform] = [Platform.COVER, Platform.SENSOR, Platform.BINARY_SENSOR]
MANUFACTURER = "Norman (ShadeAuto)"

# --- Config entry data -------------------------------------------------------
CONF_HOST = "host"

# --- Hub transport -----------------------------------------------------------
DEFAULT_PORT = 10123  # the hub's local HTTP API; fixed by its firmware
REQUEST_TIMEOUT_SEC = 10.0  # total timeout for one request/response; bounds only connecting for /notification
# The /notification stream (docs/HUB_PROTOCOL.md 2.5). kedube/ha-norman saw
# long-lived streams go quiet, so it closes and reopens its stream every 300 s
# even when nothing is wrong; so do we.
NOTIFY_MAX_DURATION_SEC = 300.0
# Back-off before reopening a stream that ended or failed: starts here, doubles
# after every attempt until a stream has stayed up for NOTIFY_STABLE_SEC again,
# and never exceeds the cap.
NOTIFY_RETRY_MIN_SEC = 5.0
NOTIFY_RETRY_MAX_SEC = 60.0
# A stream counts as working again only once it has stayed open this long after
# the hub's acknowledgement. One that is acknowledged and then ends at once (a
# hub that allows one stream while another client holds it, say) is a failure
# like any other, so the back-off keeps growing and the outage gets one warning,
# not a warning and an info line every few seconds.
NOTIFY_STABLE_SEC = NOTIFY_RETRY_MAX_SEC
# The acknowledgement the hub writes first: {"Error": "Success."}, a string
# unlike every other endpoint's Error (docs/HUB_PROTOCOL.md 2.5). The integer 0
# of the other endpoints is taken as success too; any other value means the
# hub refused the stream.
NOTIFY_ACK = "Success."
# The hub's objects are a few dozen bytes; this much text without a complete
# JSON object means the stream is not what the hub writes, so it is reopened.
NOTIFY_MAX_BUFFER = 65536

# --- Option keys (entry.options) ---------------------------------------------
CONF_POLL_SECONDS = "poll_seconds"
CONF_MOTION_POLL_SEC = "motion_poll_sec"
CONF_LOW_BATTERY_THRESHOLD = "low_battery_threshold"
CONF_SEND_SPACING_SEC = "send_spacing_sec"
CONF_VERIFY_ENABLED = "verify_enabled"
CONF_VERIFY_DELAY_SEC = "verify_delay_sec"
CONF_ESTIMATION_ENABLED = "estimation_enabled"
CONF_FULL_TRAVEL_TIME_SEC = "full_travel_time_sec"
CONF_TILT_MODE = "tilt_mode"
CONF_COLLISION_GUARD = "collision_guard"
CONF_RAIL_MIN_GAP = "rail_min_gap"
CONF_PUSH_ENABLED = "push_enabled"
# 0.4.x stored a bool here; it is only read to migrate to CONF_TILT_MODE.
CONF_TILT_ENABLED = "tilt_enabled"

# --- Option defaults ---------------------------------------------------------
DEFAULT_POLL = 30  # seconds between /status polls while nothing is moving
DEFAULT_MOTION_POLL = 2.0  # seconds between polls while a shade is moving (the hub only reports end states)
DEFAULT_LOW_BATT = 20  # battery percentage at or below which "battery low" turns on
# Minimum seconds between two /control posts. The hub has one radio and drops
# commands sent faster than it can transmit them while still answering Error 0
# (kedube/ha-norman docs/NORMAN_API.md, "Control commands are paced": thirteen
# blinds sent the same command at 1.0-1.2 s intervals still missed some, 1.3 s
# reached all of them by hand but lost one now and then from an automation,
# 1.5 s survived several whole-house runs but longer use still lost the odd
# command, and that project ships 1.6 s). 1.5 s is the lowest spacing with no
# loss in those runs; verify-and-retry catches the odd command lost later, and
# the options allow up to 3 s for a hub that keeps losing them.
DEFAULT_SEND_SPACING = 1.5
MAX_SEND_SPACING = 3.0
DEFAULT_VERIFY_ENABLED = True
DEFAULT_VERIFY_DELAY = 60.0  # seconds after a command before checking the shade reached its target
# The hub reports end states 10-35 s after a command (docs/HUB_PROTOCOL.md 5.2);
# a verify sooner would re-send a command the shade is still carrying out.
# Stored values below this (from before the form enforced it) are raised to it.
MIN_VERIFY_DELAY = 30.0
DEFAULT_ESTIMATION_ENABLED = True
# Seconds assumed for a full 0 -> 100 travel; drives the slider animation only.
# Upstream's default for their shades. A PerfectSheer was observed to take
# roughly 60 s for a full travel (28-35 s for half travels in the log), so
# users tune it in the options.
DEFAULT_FULL_TRAVEL_TIME = 28.0
# Tilt modes: "auto" exposes vane tilt on the shades whose profile has vanes
# (PROFILE_SHEER), "off" exposes it nowhere. The commands a sheer shade gets
# are the same either way, so turning tilt off never closes anyone's vanes.
TILT_MODE_AUTO = "auto"
TILT_MODE_OFF = "off"
TILT_MODES = (TILT_MODE_AUTO, TILT_MODE_OFF)
DEFAULT_TILT_MODE = TILT_MODE_AUTO
# Two-rail collision guard (upstream PR #9): keep the middle rail at least
# rail_min_gap above the bottom rail by moving the other rail out of the way.
# On by default because the rails are not known to stop each other.
DEFAULT_COLLISION_GUARD = True
DEFAULT_RAIL_MIN_GAP = 0  # percent; PR #9's default
MAX_RAIL_MIN_GAP = 50  # PR #9's form range
# Read the hub's /notification stream and refresh as soon as it announces a
# change. Off means no stream at all: updates come from polling only, as in
# 0.5.0 (verify still asks the one shade to report in before a retry; see
# STATUS_REPORT_WAIT_SEC). Polling runs either way, because the stream does
# not replay what it missed while down.
DEFAULT_PUSH_ENABLED = True

# --- Hub field names ---------------------------------------------------------
ATTR_PERIPHERAL_UID = "PeripheralUID"
ATTR_BOTTOM_RAIL = "BottomRailPosition"
ATTR_MIDDLE_RAIL = "MiddleRailPosition"
ATTR_TARGET_BOTTOM_RAIL = "TargetBottomRailPosition"
ATTR_TARGET_MIDDLE_RAIL = "TargetMiddleRailPosition"
ATTR_BATTERY = "BatteryVoltage"  # despite the name, the hub reports a percentage
ATTR_MOTOR_STOP = "MotorStop"
ATTR_ERROR = "Error"
MOTOR_STOP = 170  # the hub's "execute" value for action fields such as MotorStop
# With a PeripheralUID, makes that one shade report in without moving
# (docs/HUB_PROTOCOL.md 4.6). 0 is the vendor app's value; 170 does nothing.
ATTR_STATUS_REQUEST = "StatusRequest"
STATUS_REQUEST = 0
# Never sent: a PeripheralUID does not confine ReportBatteryLevel to one shade,
# and hub-wide it closed shades on two users' hubs (kedube/ha-norman issue #5;
# docs/HUB_PROTOCOL.md 4.6).
NEVER_SENT_FIELDS = frozenset({"ReportBatteryLevel"})
# /notification objects: {"Status": <hub ms>, "PeripheralList": [<uid>, ...]}
# per change, and {"UpdateTime": {...}, "Timestamp": <ms>} after a rename or
# schedule change in the app (kedube).
ATTR_PERIPHERAL_LIST = "PeripheralList"
ATTR_UPDATE_TIME = "UpdateTime"

# --- Axes ---------------------------------------------------------------------
# The hub knows two commanded axes per shade: the bottom rail (lift) and the
# "middle rail", which is the vane angle on sheer shades and the second rail's
# height on two-rail shades. Single-rail shades only have the bottom rail.
AXIS_BOTTOM = "bottom"
AXIS_MIDDLE = "middle"
AXES = (AXIS_BOTTOM, AXIS_MIDDLE)
AXIS_FIELDS = {
    AXIS_BOTTOM: (ATTR_BOTTOM_RAIL, ATTR_TARGET_BOTTOM_RAIL),
    AXIS_MIDDLE: (ATTR_MIDDLE_RAIL, ATTR_TARGET_MIDDLE_RAIL),
}

# --- Shade profiles ------------------------------------------------------------
# A profile decides which entities a shade gets, which commands they send and
# which axes the coordinator tracks. It is chosen once at setup from the
# (ModuleType, ModuleDetail) pair the hub reports for the shade.
PROFILE_SHEER = "sheer"  # bottom rail = lift, middle rail = vane tilt honoured only at bottom 0
PROFILE_SINGLE_RAIL = "single_rail"  # bottom rail only; the middle rail always reads 0
PROFILE_TWO_RAIL = "two_rail"  # two independent rail heights (top-down/bottom-up, day/night)
PROFILE_LEGACY = "legacy"  # no verified behaviour; handled like single_rail, as 0.3.0 did

# Exact pairs, checked before PROFILE_BY_MODULE_TYPE.
PROFILE_BY_MODULE: dict[tuple[int, int], str] = {
    # Verified on five shades: vane tilt on the middle rail, honoured only while
    # the bottom target is 0 (docs/hub-characterization-2026-10-22.log, HUB_PROTOCOL.md 4.4).
    (48, 3): PROFILE_SHEER,
    # The second-generation roller module; the vendor app names detail 3
    # PerfectSheer as it does 48/3 (kedube/ha-norman docs/NORMAN_API.md, "Cover types").
    (49, 3): PROFILE_SHEER,
    # Cellular details the app gives two sliders, like type 33 (kedube, "Cover types").
    (32, 3): PROFILE_TWO_RAIL,
    (32, 4): PROFILE_TWO_RAIL,
    (32, 5): PROFILE_TWO_RAIL,
}
# Every other detail of these module types. A shade whose ModuleDetail is
# missing matches here only if its type has no entry above (so 33 does, and
# 32, 48 and 49 do not).
PROFILE_BY_MODULE_TYPE: dict[int, str] = {
    # Cellular (32/0, 32/2), roller (48/1), roman (48/2) and their 49/x
    # successors: the middle rail always reads 0, but app presets can leave
    # TargetMiddleRailPosition at 100 (kedube, "Cover types"), so that axis is
    # not tracked. 0.3.0 sent them bottom-only commands, and so does upstream PR #9.
    32: PROFILE_SINGLE_RAIL,
    48: PROFILE_SINGLE_RAIL,
    49: PROFILE_SINGLE_RAIL,
    # Top-down/bottom-up and day/night cellular: the middle rail is a second
    # height on the same 0-100 axis as the bottom rail (upstream PR #9, on one
    # real type-33 shade of unreported detail; kedube on 33/3).
    33: PROFILE_TWO_RAIL,
}
# Families the vendor app knows that have no verified behaviour here
# (SmartDrape: vanes in seven stops; Shutter: louvers as Position 0-7, no rails;
# kedube, "Cover types"). They get the legacy profile without a warning.
KNOWN_UNSUPPORTED_MODULE_TYPES: dict[int, str] = {1: "Shutter", 80: "SmartDrape"}
# The Top cover of a two-rail shade is the main cover's unique id plus this:
# upstream PR #9 (alexfgoldberg's fork) used it, so its users keep the entity.
TOP_COVER_UNIQUE_ID_SUFFIX = "_middle"
# Axes the coordinator tracks (pending targets, motion, fast polling, verify).
# Single-rail and legacy shades track the bottom rail only, so a middle target
# the hub never reaches (the stuck 100 above) cannot keep them "moving".
PROFILE_AXES: dict[str, tuple[str, ...]] = {
    PROFILE_SHEER: AXES,
    PROFILE_TWO_RAIL: AXES,
    PROFILE_SINGLE_RAIL: (AXIS_BOTTOM,),
    PROFILE_LEGACY: (AXIS_BOTTOM,),
}

# --- Motion tracking ---------------------------------------------------------
POSITION_TOLERANCE = 2  # a hub position this close to the target counts as "arrived"
PENDING_GRACE_SEC = 10.0  # how long a just-sent command counts as moving before the hub must reflect its target
MOTION_FAILSAFE_SEC = 120.0  # a shade whose target != actual for this long with no change is treated as stalled
# Cooldown of the coordinator's refresh requests (the stream's events). Home
# Assistant's default is 10 s, which would hold back a "move finished" event
# that arrives soon after another one (a shade's two events, or two shades in
# a scene) by up to 10 s; 1 s still folds a burst into one /status read.
REFRESH_COOLDOWN_SEC = 1.0
# How long verify waits for a shade to report in after its StatusRequest
# before deciding on what /status says anyway. Our hub announced the report
# 2.3 s after the request (docs/hub-characterization-2026-10-23.log), kedube's
# within 5 s on every try; without the stream the wait always runs out.
STATUS_REPORT_WAIT_SEC = 10.0
