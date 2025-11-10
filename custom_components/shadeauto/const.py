from homeassistant.const import Platform

DOMAIN = "shadeauto"
CONF_HOST = "host"

# Platforms
PLATFORMS: list[Platform] = [Platform.COVER, Platform.SENSOR, Platform.BINARY_SENSOR]

# Defaults
DEFAULT_POLL = 30               # idle poll seconds
DEFAULT_BURST_INTERVAL = 2      # seconds between burst polls after commands
DEFAULT_BURST_CYCLES = 5        # number of burst polls
DEFAULT_LOW_BATT = 20           # percent threshold for low-battery
PORT = 10123
