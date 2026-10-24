"""Typed view of the config entry options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import (
    CONF_COLLISION_GUARD,
    CONF_ESTIMATION_ENABLED,
    CONF_FULL_TRAVEL_TIME_SEC,
    CONF_LOW_BATTERY_THRESHOLD,
    CONF_MOTION_POLL_SEC,
    CONF_POLL_SECONDS,
    CONF_PUSH_ENABLED,
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
    DEFAULT_PUSH_ENABLED,
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
    push_enabled: bool = DEFAULT_PUSH_ENABLED

    @classmethod
    def from_mapping(cls, options: Mapping[str, Any]) -> ShadeAutoOptions:
        """Build from ``entry.options``."""
        return cls(
            poll_seconds=int(options.get(CONF_POLL_SECONDS, DEFAULT_POLL)),
            motion_poll_sec=float(options.get(CONF_MOTION_POLL_SEC, DEFAULT_MOTION_POLL)),
            low_battery_threshold=int(options.get(CONF_LOW_BATTERY_THRESHOLD, DEFAULT_LOW_BATT)),
            send_spacing_sec=float(options.get(CONF_SEND_SPACING_SEC, DEFAULT_SEND_SPACING)),
            verify_enabled=bool(options.get(CONF_VERIFY_ENABLED, DEFAULT_VERIFY_ENABLED)),
            # 0.3.x's form accepted delays from 1 s; 0.4.0's starts at MIN_VERIFY_DELAY.
            verify_delay_sec=max(MIN_VERIFY_DELAY, float(options.get(CONF_VERIFY_DELAY_SEC, DEFAULT_VERIFY_DELAY))),
            estimation_enabled=bool(options.get(CONF_ESTIMATION_ENABLED, DEFAULT_ESTIMATION_ENABLED)),
            full_travel_time_sec=float(options.get(CONF_FULL_TRAVEL_TIME_SEC, DEFAULT_FULL_TRAVEL_TIME)),
            tilt_mode=_tilt_mode(options),
            collision_guard=bool(options.get(CONF_COLLISION_GUARD, DEFAULT_COLLISION_GUARD)),
            rail_min_gap=max(0, min(MAX_RAIL_MIN_GAP, int(options.get(CONF_RAIL_MIN_GAP, DEFAULT_RAIL_MIN_GAP)))),
            push_enabled=bool(options.get(CONF_PUSH_ENABLED, DEFAULT_PUSH_ENABLED)),
        )
