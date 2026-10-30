"""Typed view of the config entry options."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
from typing import Any

from .const import (
    CONF_COLLISION_GUARD,
    CONF_ESTIMATION_ENABLED,
    CONF_FULL_TRAVEL_TIME_SEC,
    CONF_LOW_BATTERY_THRESHOLD,
    CONF_MOTION_POLL_SEC,
    CONF_POLL_SECONDS,
    CONF_RAIL_MIN_GAP,
    CONF_SEND_SPACING_SEC,
    CONF_TILT_ENABLED,
    CONF_TILT_MODE,
    CONF_VERIFY_DELAY_SEC,
    CONF_VERIFY_ENABLED,
    DEFAULT_COLLISION_GUARD,
    DEFAULT_ESTIMATION_ENABLED,
    DEFAULT_FULL_TRAVEL_TIME,
    DEFAULT_LOW_BATT,
    DEFAULT_MOTION_POLL,
    DEFAULT_POLL,
    DEFAULT_RAIL_MIN_GAP,
    DEFAULT_SEND_SPACING,
    DEFAULT_TILT_MODE,
    DEFAULT_VERIFY_DELAY,
    DEFAULT_VERIFY_ENABLED,
    MAX_RAIL_MIN_GAP,
    MIN_VERIFY_DELAY,
    TILT_MODE_AUTO,
    TILT_MODE_OFF,
    TILT_MODES,
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


def _tilt_mode(options: Mapping[str, Any]) -> str:
    """The stored tilt mode, migrating 0.4.x's ``tilt_enabled`` bool when there is none.

    ``tilt_enabled: False`` meant "no tilt on any shade", which is ``off``;
    ``True`` (or nothing stored) meant tilt on the vane shades, which is
    ``auto``. A valid ``tilt_mode`` wins over a stale ``tilt_enabled`` left
    behind in the same mapping.
    """
    mode = options.get(CONF_TILT_MODE)
    if mode in TILT_MODES:
        return str(mode)
    if CONF_TILT_ENABLED in options:
        return TILT_MODE_AUTO if bool(options[CONF_TILT_ENABLED]) else TILT_MODE_OFF
    return DEFAULT_TILT_MODE


@dataclass(frozen=True, slots=True)
class ShadeAutoOptions:
    """Options from the integration's Configure dialog, with defaults applied.

    Parsed once per config entry load (the options flow reloads the entry when
    anything changes), so every consumer reads typed attributes instead of
    re-reading ``entry.options`` with its own fallbacks. Unknown keys, such as
    options from older versions, are ignored. Values that an older version
    could store outside today's form ranges are moved into them, because the
    options form uses the parsed values as its defaults and would refuse to
    save an out-of-range one.
    """

    poll_seconds: int = DEFAULT_POLL
    motion_poll_sec: float = DEFAULT_MOTION_POLL
    low_battery_threshold: int = DEFAULT_LOW_BATT
    send_spacing_sec: float = DEFAULT_SEND_SPACING
    verify_enabled: bool = DEFAULT_VERIFY_ENABLED
    verify_delay_sec: float = DEFAULT_VERIFY_DELAY
    estimation_enabled: bool = DEFAULT_ESTIMATION_ENABLED
    full_travel_time_sec: float = DEFAULT_FULL_TRAVEL_TIME
    tilt_mode: str = DEFAULT_TILT_MODE
    collision_guard: bool = DEFAULT_COLLISION_GUARD
    rail_min_gap: int = DEFAULT_RAIL_MIN_GAP

    @classmethod
    def from_mapping(cls, options: Mapping[str, Any]) -> ShadeAutoOptions:
        """Build from ``entry.options``; unknown keys are ignored and unusable values fall back to defaults."""
        return cls(
            poll_seconds=_read(options, CONF_POLL_SECONDS, DEFAULT_POLL, int),
            motion_poll_sec=_read(options, CONF_MOTION_POLL_SEC, DEFAULT_MOTION_POLL, float),
            low_battery_threshold=_read(options, CONF_LOW_BATTERY_THRESHOLD, DEFAULT_LOW_BATT, int),
            send_spacing_sec=_read(options, CONF_SEND_SPACING_SEC, DEFAULT_SEND_SPACING, float),
            verify_enabled=_read(options, CONF_VERIFY_ENABLED, DEFAULT_VERIFY_ENABLED, bool),
            # 0.3.x's form accepted delays from 1 s; 0.4.0's starts at MIN_VERIFY_DELAY.
            verify_delay_sec=max(MIN_VERIFY_DELAY, _read(options, CONF_VERIFY_DELAY_SEC, DEFAULT_VERIFY_DELAY, float)),
            estimation_enabled=_read(options, CONF_ESTIMATION_ENABLED, DEFAULT_ESTIMATION_ENABLED, bool),
            full_travel_time_sec=_read(options, CONF_FULL_TRAVEL_TIME_SEC, DEFAULT_FULL_TRAVEL_TIME, float),
            tilt_mode=_tilt_mode(options),
            collision_guard=_read(options, CONF_COLLISION_GUARD, DEFAULT_COLLISION_GUARD, bool),
            rail_min_gap=max(0, min(MAX_RAIL_MIN_GAP, _read(options, CONF_RAIL_MIN_GAP, DEFAULT_RAIL_MIN_GAP, int))),
        )
