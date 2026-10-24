"""Push updates: read the hub's change stream and refresh as soon as it announces a change.

The hub's ``/notification`` reply is a stream that does not end
(docs/HUB_PROTOCOL.md 2.5): an acknowledgement, ``{"Error": "Success."}``,
then one ``{"Status": <hub ms>, "PeripheralList": [<uid>, ...]}`` whenever a
shade's target or position changes or a shade reports in: once when a
command is accepted and once when the move ends, whoever sent it. The ids say
only *which* shades changed, so each event requests a coordinator refresh,
which reads ``/status``; the coordinator's debouncer folds bursts together.
The listener only requests the refresh and never waits for it, so closing a
stream (the recycle, a reconnect) cannot cancel a ``/status`` read in flight.

:class:`NotificationListener` keeps one stream open for as long as the entry
is loaded. It runs as a config-entry background task, so unloading the entry
or stopping Home Assistant cancels it.

* Once the hub acknowledges a stream (``{"Error": "Success."}``), one
  refresh catches up on whatever happened while no stream was open: the hub
  does not replay it. Any other ``Error`` value is a refusal and ends the
  stream like a failure.
* Every event with shade ids, and every ``UpdateTime`` object (a rename or
  schedule change in the app), requests a refresh and wakes a verify that
  waits for one of those shades to report in. Anything else is ignored.
* The stream is closed and reopened every ``NOTIFY_MAX_DURATION_SEC`` even
  when healthy, because long-lived streams have been seen to go quiet
  (kedube/ha-norman).
* A stream that ends or fails is reopened after a back-off that starts at
  ``NOTIFY_RETRY_MIN_SEC`` and doubles up to ``NOTIFY_RETRY_MAX_SEC``. It
  starts over only once a stream has stayed up for ``NOTIFY_STABLE_SEC``
  after its acknowledgement: a stream that is acknowledged and then ends at
  once is a failure like any other. A warning is logged when a stream that
  had been up that long is lost, and an info line once a new one has been up
  that long; the attempts in between log at debug level.

Polling is unchanged and keeps running, so nothing depends on the stream:
it only makes changes show up sooner. Every wait is an ``async_call_later``
timer (:func:`async_wait`), never ``asyncio.sleep``.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .api import ShadeAutoError
from .const import (
    ATTR_ERROR,
    ATTR_PERIPHERAL_LIST,
    ATTR_UPDATE_TIME,
    DOMAIN,
    NOTIFY_ACK,
    NOTIFY_MAX_DURATION_SEC,
    NOTIFY_RETRY_MAX_SEC,
    NOTIFY_RETRY_MIN_SEC,
    NOTIFY_STABLE_SEC,
)
from .coordinator import ShadeAutoCoordinator, async_wait

_LOGGER = logging.getLogger(__name__)


class StreamRefusedError(ShadeAutoError):
    """The hub answered ``/notification`` with an ``Error`` other than its acknowledgement."""


def is_acknowledgement(answer: Any) -> bool:
    """Whether a stream's first ``Error`` value says the hub took the stream.

    The hub writes ``"Success."`` (docs/HUB_PROTOCOL.md 2.5); the integer 0 of
    the other endpoints counts too. Anything else is unverified and is taken
    as a refusal.
    """
    if isinstance(answer, str):
        return answer == NOTIFY_ACK
    return isinstance(answer, int) and not isinstance(answer, bool) and answer == 0


@dataclass(slots=True)
class _Connection:
    """How one stream went."""

    acknowledged: bool = False  # the hub wrote its {"Error": "Success."} acknowledgement
    stable: bool = False  # it then stayed open for NOTIFY_STABLE_SEC
    recycled: bool = False  # we closed it after NOTIFY_MAX_DURATION_SEC
    error: BaseException | None = None  # why it failed; None if the hub ended it (EOF) or we recycled it
    cancel_stable: CALLBACK_TYPE | None = None  # the NOTIFY_STABLE_SEC timer, while it runs


class NotificationListener:
    """Keeps the hub's ``/notification`` stream open and refreshes the coordinator on every change."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: ShadeAutoCoordinator) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._host = coordinator.api.host
        self._healthy = False  # the last stream stayed up NOTIFY_STABLE_SEC and none has failed since
        self._lost_logged = False  # the warning for the current outage is out; the info line is still due

    @callback
    def async_start(self) -> None:
        """Start listening in the background, for as long as the entry stays loaded."""
        self._entry.async_create_background_task(
            self._hass, self._async_run(), name=f"{DOMAIN} notifications {self._host}"
        )

    async def _async_run(self) -> None:
        """Keep a stream open: reopen at once after a recycle, after a back-off after anything else."""
        delay = NOTIFY_RETRY_MIN_SEC
        try:
            while True:
                conn = await self._async_connection()
                self._coordinator.push_connected = False
                if conn.stable:
                    delay = NOTIFY_RETRY_MIN_SEC
                if conn.recycled and conn.acknowledged:
                    _LOGGER.debug(
                        "Reopening the notification stream from %s, as every %.0f s",
                        self._host,
                        NOTIFY_MAX_DURATION_SEC,
                    )
                    continue
                self._log_failure(conn, delay)
                await async_wait(self._hass, delay)
                delay = min(delay * 2, NOTIFY_RETRY_MAX_SEC)
        finally:
            self._coordinator.push_connected = False

    async def _async_connection(self) -> _Connection:
        """Hold one stream until the hub ends it, it fails, or it is due to be recycled.

        The reading runs in a task of its own so that the recycle timer can
        end it without cancelling this loop.
        """
        conn = _Connection()
        reader = self._entry.async_create_background_task(
            self._hass, self._async_read(conn), name=f"{DOMAIN} notification stream {self._host}"
        )
        ended: asyncio.Future[None] = self._hass.loop.create_future()

        def _reader_done(_task: asyncio.Task[None]) -> None:
            if not ended.done():
                ended.set_result(None)

        reader.add_done_callback(_reader_done)
        try:
            await async_wait(self._hass, NOTIFY_MAX_DURATION_SEC, until=ended)
        finally:
            if not reader.done():
                conn.recycled = True
                reader.cancel()
                await asyncio.wait([reader])
            if conn.cancel_stable is not None:
                conn.cancel_stable()
                conn.cancel_stable = None
        if not reader.cancelled() and (err := reader.exception()) is not None:
            _LOGGER.error("Unexpected error while reading the hub's change notifications", exc_info=err)
            conn.error = err
        return conn

    async def _async_read(self, conn: _Connection) -> None:
        """Read one stream to its end, handling each object as it arrives."""
        try:
            async with aclosing(self._coordinator.api.notification_stream()) as stream:
                async for item in stream:
                    self._handle(item, conn)
        except ShadeAutoError as err:
            conn.error = err

    @callback
    def _handle(self, item: Any, conn: _Connection) -> None:
        """Request a refresh for a change; note the acknowledgement; ignore anything else.

        The refresh is requested, not awaited (``async_hub_changed``), so the
        recycle, which cancels this reading, never cancels a ``/status`` read.
        """
        if not isinstance(item, dict):
            _LOGGER.debug("Ignoring notification %r", item)
            return
        uids = item.get(ATTR_PERIPHERAL_LIST)
        if isinstance(uids, list) and uids:
            _LOGGER.debug("Hub reports a change on %s", uids)
            self._coordinator.async_shades_reported(str(uid) for uid in uids)
            self._coordinator.async_hub_changed()
        elif ATTR_UPDATE_TIME in item:
            _LOGGER.debug("Hub reports a change of its settings (%s)", item[ATTR_UPDATE_TIME])
            self._coordinator.async_hub_changed()
        elif ATTR_ERROR in item and not conn.acknowledged:
            self._acknowledged(conn, item[ATTR_ERROR])
        else:
            _LOGGER.debug("Ignoring notification %s", item)

    @callback
    def _acknowledged(self, conn: _Connection, answer: Any) -> None:
        """The hub answered the stream: catch up on what was missed, or give up on a refusal.

        Raises :class:`StreamRefusedError` for anything but the acknowledgement,
        which ends the stream as a failure. The stream counts as working (the
        back-off starts over, the info line after an outage) only once it has
        stayed open for ``NOTIFY_STABLE_SEC``.
        """
        if not is_acknowledgement(answer):
            raise StreamRefusedError(f"the hub answered {ATTR_ERROR} {answer!r}")
        conn.acknowledged = True
        self._coordinator.push_connected = True
        _LOGGER.debug("Notification stream from %s is open (hub answered %r)", self._host, answer)

        @callback
        def _stable(_now: datetime) -> None:
            conn.cancel_stable = None
            conn.stable = True
            if self._lost_logged:
                _LOGGER.info("Change notifications from the ShadeAuto hub at %s are back", self._host)
                self._lost_logged = False
            self._healthy = True

        conn.cancel_stable = async_call_later(self._hass, NOTIFY_STABLE_SEC, _stable)
        # The hub does not replay events from while no stream was open.
        self._coordinator.async_hub_changed()

    def _log_failure(self, conn: _Connection, delay: float) -> None:
        """A warning when a stream that was up ``NOTIFY_STABLE_SEC`` fails; debug lines for the attempts after it."""
        if conn.error is not None:
            reason = str(conn.error) or type(conn.error).__name__
        elif conn.recycled:
            reason = f"no acknowledgement in {NOTIFY_MAX_DURATION_SEC:.0f} s"
        else:
            reason = "the hub ended it"
        if self._healthy:
            self._healthy = False
            self._lost_logged = True
            _LOGGER.warning(
                "Lost the change notifications from the ShadeAuto hub at %s (%s); polling continues, and the "
                "stream is reopened in the background",
                self._host,
                reason,
            )
        _LOGGER.debug("Notification stream from %s ended (%s); reopening in %.0f s", self._host, reason, delay)
