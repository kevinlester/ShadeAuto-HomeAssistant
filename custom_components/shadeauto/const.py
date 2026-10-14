from homeassistant.const import Platform

DOMAIN = "shadeauto"
CONF_HOST = "host"

# Platforms
PLATFORMS: list[Platform] = [Platform.COVER, Platform.SENSOR, Platform.BINARY_SENSOR]

# Defaults
DEFAULT_POLL = 30               # idle poll seconds
DEFAULT_LOW_BATT = 20           # percent threshold for low-battery
SEND_SPACING_SEC = 0.75         # code default; can be overridden by option below
DEFAULT_SEND_SPACING = SEND_SPACING_SEC
DEFAULT_VERIFY_ENABLED = True
DEFAULT_VERIFY_DELAY = 60.0     # seconds to wait before verifying position / retry
DEFAULT_NOTIFICATION_TIMEOUT = 2.0   # seconds to hold each long-poll before returning

# Motion estimation defaults
DEFAULT_FULL_TRAVEL_TIME = 28.0      # seconds to move from 0 -> 100
DEFAULT_ESTIMATION_ENABLED = True    # enable opening/closing & estimation by default

# TDBU (top-down / bottom-up) dual-rail support
# ModuleType values that expose a second, independently-motorised rail. Single-rail
# shades (e.g. ModuleType 32) always report MiddleRailPosition as 0, so detection is
# by module type, not by the mere presence of the field.
TDBU_MODULE_TYPES = {33}
RAIL_BOTTOM = "bottom"
RAIL_MIDDLE = "middle"
# Hub status/control field name for each rail. Bottom is the original single-rail
# field; middle is the second (top-down) rail on TDBU shades.
RAIL_FIELDS = {RAIL_BOTTOM: "BottomRailPosition", RAIL_MIDDLE: "MiddleRailPosition"}
# Both hub fields measure rail height: 0=lowered, 100=raised. The top rail
# must use the same axis as the bottom rail and the collision guard.
RAIL_INVERT = {RAIL_BOTTOM: False, RAIL_MIDDLE: False}
# Collision guard: keep the two rails of a TDBU shade from crossing. Only ever
# applied to TDBU shades; single-rail shades are never clamped.
DEFAULT_COLLISION_GUARD = True
DEFAULT_RAIL_MIN_GAP = 0             # extra separation (0-100 units) to keep between rails

PORT = 10123
