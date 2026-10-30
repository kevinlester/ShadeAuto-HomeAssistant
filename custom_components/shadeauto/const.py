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
REQUEST_TIMEOUT_SEC = 10.0  # total timeout for one request/response

# --- Option keys (entry.options) ---------------------------------------------
CONF_POLL_SECONDS = "poll_seconds"
CONF_MOTION_POLL_SEC = "motion_poll_sec"
CONF_LOW_BATTERY_THRESHOLD = "low_battery_threshold"
CONF_SEND_SPACING_SEC = "send_spacing_sec"
CONF_VERIFY_ENABLED = "verify_enabled"
CONF_VERIFY_DELAY_SEC = "verify_delay_sec"
CONF_ESTIMATION_ENABLED = "estimation_enabled"
CONF_FULL_TRAVEL_TIME_SEC = "full_travel_time_sec"
CONF_TILT_ENABLED = "tilt_enabled"

# --- Option defaults ---------------------------------------------------------
DEFAULT_POLL = 30  # seconds between /status polls while nothing is moving
DEFAULT_MOTION_POLL = 2.0  # seconds between polls while a shade is moving (the hub only reports end states)
DEFAULT_LOW_BATT = 20  # battery percentage at or below which "battery low" turns on
# Minimum seconds between two /control posts. Upstream found back-to-back
# commands unreliable without pacing, so commands are serialised and spaced.
DEFAULT_SEND_SPACING = 0.75
DEFAULT_VERIFY_ENABLED = True
DEFAULT_VERIFY_DELAY = 60.0  # seconds after a command before checking the shade reached its target
DEFAULT_ESTIMATION_ENABLED = True
# Seconds assumed for a full 0 -> 100 travel; drives the slider animation only.
# Upstream's default for their shades. A PerfectSheer was observed to take
# roughly 60 s for a full travel (28-35 s for half travels in the log), so
# users tune it in the options.
DEFAULT_FULL_TRAVEL_TIME = 28.0
DEFAULT_TILT_ENABLED = True  # expose MiddleRailPosition as vane tilt (PerfectSheer); turn off for plain shades

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

# --- Axes ---------------------------------------------------------------------
# A shade has two commanded axes: the bottom rail (lift) and, on vane-tilt
# shades, the "middle rail" which the hub uses for the vane angle.
AXIS_BOTTOM = "bottom"
AXIS_MIDDLE = "middle"
AXES = (AXIS_BOTTOM, AXIS_MIDDLE)
AXIS_FIELDS = {
    AXIS_BOTTOM: (ATTR_BOTTOM_RAIL, ATTR_TARGET_BOTTOM_RAIL),
    AXIS_MIDDLE: (ATTR_MIDDLE_RAIL, ATTR_TARGET_MIDDLE_RAIL),
}

# --- Motion tracking ---------------------------------------------------------
POSITION_TOLERANCE = 2  # a hub position this close to the target counts as "arrived"
PENDING_GRACE_SEC = 10.0  # how long a just-sent command counts as moving before the hub must reflect its target
MOTION_FAILSAFE_SEC = 120.0  # a shade whose target != actual for this long with no change is treated as stalled
