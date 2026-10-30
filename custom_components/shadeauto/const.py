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
CONF_LOW_BATTERY_THRESHOLD = "low_battery_threshold"
CONF_SEND_SPACING_SEC = "send_spacing_sec"
CONF_VERIFY_ENABLED = "verify_enabled"
CONF_VERIFY_DELAY_SEC = "verify_delay_sec"
CONF_NOTIFICATION_TIMEOUT_SEC = "notification_timeout_sec"
CONF_ESTIMATION_ENABLED = "estimation_enabled"
CONF_FULL_TRAVEL_TIME_SEC = "full_travel_time_sec"

# --- Option defaults ---------------------------------------------------------
DEFAULT_POLL = 30  # seconds between /status polls
DEFAULT_LOW_BATT = 20  # battery percentage at or below which "battery low" turns on
# Minimum seconds between two /control posts. Upstream found back-to-back
# commands unreliable without pacing, so commands are serialised and spaced.
DEFAULT_SEND_SPACING = 0.75
DEFAULT_VERIFY_ENABLED = True
DEFAULT_VERIFY_DELAY = 60.0  # seconds after a command before checking the shade reached its target
DEFAULT_NOTIFICATION_TIMEOUT = 2.0  # seconds the hub may hold each long-poll before answering
DEFAULT_ESTIMATION_ENABLED = True
DEFAULT_FULL_TRAVEL_TIME = 28.0  # seconds a shade needs for a full 0 -> 100 travel; drives the slider animation

# --- Hub field names ---------------------------------------------------------
ATTR_PERIPHERAL_UID = "PeripheralUID"
ATTR_BOTTOM_RAIL = "BottomRailPosition"
ATTR_MIDDLE_RAIL = "MiddleRailPosition"
ATTR_BATTERY = "BatteryVoltage"  # despite the name, the hub reports a percentage

# --- Motion tracking ---------------------------------------------------------
POSITION_TOLERANCE = 2  # a hub position this close to the target counts as "arrived"
MOTION_FAILSAFE_SEC = 120.0  # stop waiting for a shade this long after the last command
