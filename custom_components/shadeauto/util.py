"""Small pure helpers shared by several modules."""

from __future__ import annotations

import codecs
import json
from typing import Any


class JsonStreamSplitter:
    """Cut a byte stream of back-to-back JSON values into the values.

    The hub's ``/notification`` reply is bare objects written one after the
    other, with no newline, length or other separator between them, and the
    network hands them over in arbitrary pieces: one chunk can end inside an
    object or hold several (docs/HUB_PROTOCOL.md 2.5). Bytes are decoded
    incrementally (a UTF-8 character can be split too), and ``raw_decode``
    takes one value at a time off the front of the buffer; an incomplete
    value stays there until the rest arrives. ``tools/hubwatch.py`` keeps its
    own copy (``StreamSplitter``), because the tool must not import the
    integration.
    """

    def __init__(self, max_buffer: int) -> None:
        self._text = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._decoder = json.JSONDecoder()
        self._buffer = ""
        self._max_buffer = max_buffer

    @property
    def pending(self) -> str:
        """Text received but not yet part of a complete value."""
        return self._buffer

    def feed(self, chunk: bytes) -> list[Any]:
        """Add ``chunk`` and return every value it completed, oldest first.

        Raises ``ValueError`` once more than ``max_buffer`` characters have
        piled up without forming a value: that is not what the hub writes,
        and waiting for more will not fix it.
        """
        self._buffer += self._text.decode(chunk)
        values: list[Any] = []
        while True:
            self._buffer = self._buffer.lstrip()
            if not self._buffer:
                break
            try:
                value, end = self._decoder.raw_decode(self._buffer)
            except json.JSONDecodeError:
                if len(self._buffer) > self._max_buffer:
                    raise ValueError(f"no JSON object in {len(self._buffer)} characters of the stream") from None
                break
            values.append(value)
            self._buffer = self._buffer[end:]
        return values


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
