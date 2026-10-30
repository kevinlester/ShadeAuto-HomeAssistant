"""Small pure helpers shared by several modules."""

from __future__ import annotations

from typing import Any


def to_int(value: Any) -> int | None:
    """Return ``value`` as an int, or ``None`` when it is missing or not numeric.

    The hub serialises most numbers as ints but some as strings (the peripheral
    list says ``"ModuleType": "48"``), so every numeric hub field goes through
    here instead of a scattered ``try: int(...)``.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def battery_percent(raw: Any) -> int | None:
    """Return the hub's ``BatteryVoltage`` field as a 0-100 percentage.

    Despite its name the field carries a percentage (observed hubs report
    ``100``). Values outside 0-100 are treated as unknown rather than clamped,
    so a firmware change cannot silently show a wrong level.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if 0 <= value <= 100:
        return round(value)
    return None
