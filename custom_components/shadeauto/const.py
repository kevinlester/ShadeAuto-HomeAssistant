from homeassistant.const import Platform

DOMAIN = "shadeauto"
CONF_HOST = "host"

# Platforms we expose
PLATFORMS: list[Platform] = [Platform.COVER, Platform.SENSOR]

# Default idle polling interval (seconds)
DEFAULT_POLL = 30
PORT = 10123
