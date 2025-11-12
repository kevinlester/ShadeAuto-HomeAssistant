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

PORT = 10123
