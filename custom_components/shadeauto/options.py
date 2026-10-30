"""Typed view of the config entry options."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from typing import Any

from .const import (
    CONF_ESTIMATION_ENABLED,
    CONF_FULL_TRAVEL_TIME_SEC,
    CONF_LOW_BATTERY_THRESHOLD,
    CONF_NOTIFICATION_TIMEOUT_SEC,
    CONF_POLL_SECONDS,
    CONF_SEND_SPACING_SEC,
    CONF_VERIFY_DELAY_SEC,
    CONF_VERIFY_ENABLED,
    DEFAULT_ESTIMATION_ENABLED,
    DEFAULT_FULL_TRAVEL_TIME,
    DEFAULT_LOW_BATT,
    DEFAULT_NOTIFICATION_TIMEOUT,
    DEFAULT_POLL,
    DEFAULT_SEND_SPACING,
    DEFAULT_VERIFY_DELAY,
    DEFAULT_VERIFY_ENABLED,
)

_LOGGER = logging.getLogger(__name__)


def _read[T](options: Mapping[str, Any], key: str, default: T, cast: Callable[[Any], T]) -> T:
    """Return ``cast(options[key])``, or ``default`` when the key is missing or the value is unusable.

    Options are written by our own options flow, so a bad value is unexpected, but a
    hand-edited or corrupted ``.storage`` entry must not stop the integration loading.
    """
    raw = options.get(key)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        _LOGGER.warning("Ignoring invalid value %r for option %s; using default %r", raw, key, default)
        return default


@dataclass(frozen=True, slots=True)
class ShadeAutoOptions:
    """Options from the integration's Configure dialog, with defaults applied.

    Parsed once per config entry load (the options flow reloads the entry when
    anything changes), so every consumer reads typed attributes instead of
    re-reading ``entry.options`` with its own fallbacks.
    """

    poll_seconds: int = DEFAULT_POLL
    low_battery_threshold: int = DEFAULT_LOW_BATT
    send_spacing_sec: float = DEFAULT_SEND_SPACING
    verify_enabled: bool = DEFAULT_VERIFY_ENABLED
    verify_delay_sec: float = DEFAULT_VERIFY_DELAY
    notification_timeout_sec: float = DEFAULT_NOTIFICATION_TIMEOUT
    estimation_enabled: bool = DEFAULT_ESTIMATION_ENABLED
    full_travel_time_sec: float = DEFAULT_FULL_TRAVEL_TIME

    @classmethod
    def from_mapping(cls, options: Mapping[str, Any]) -> ShadeAutoOptions:
        """Build from ``entry.options``; unknown keys are ignored and unusable values fall back to defaults."""
        return cls(
            poll_seconds=_read(options, CONF_POLL_SECONDS, DEFAULT_POLL, int),
            low_battery_threshold=_read(options, CONF_LOW_BATTERY_THRESHOLD, DEFAULT_LOW_BATT, int),
            send_spacing_sec=_read(options, CONF_SEND_SPACING_SEC, DEFAULT_SEND_SPACING, float),
            verify_enabled=_read(options, CONF_VERIFY_ENABLED, DEFAULT_VERIFY_ENABLED, bool),
            verify_delay_sec=_read(options, CONF_VERIFY_DELAY_SEC, DEFAULT_VERIFY_DELAY, float),
            notification_timeout_sec=_read(options, CONF_NOTIFICATION_TIMEOUT_SEC, DEFAULT_NOTIFICATION_TIMEOUT, float),
            estimation_enabled=_read(options, CONF_ESTIMATION_ENABLED, DEFAULT_ESTIMATION_ENABLED, bool),
            full_travel_time_sec=_read(options, CONF_FULL_TRAVEL_TIME_SEC, DEFAULT_FULL_TRAVEL_TIME, float),
        )
