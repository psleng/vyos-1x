#!/usr/bin/env python3
# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Long-running dial-on-demand daemon for one WWAN interface.

The daemon is a pure *demand sensor*: it never calls ``connect_bearer()`` or
``disconnect_bearer()``.  It expresses demand to igos-wwan-manager by
acquiring/renewing the ``dod-traffic`` bearer lease; the manager's lease
reconciler alone connects and idles the bearer.

States (docs/igos-wwan-dial-on-demand-design.md §4):

- ``SYNC``    bearer unknown, trigger removed.  (Re)connect D-Bus, read the
              bearer status, pick ACTIVE or ARMED.
- ``ACTIVE``  bearer up, trigger removed.  Sample ``tx_bytes``; renew the
              lease only when traffic is seen.  Watch for the bearer going
              down.
- ``ARMED``   bearer down, trigger installed.  Wait for a queued packet or
              for the bearer to come up by other means.
- ``DIALING`` bearer coming up, trigger installed (queueing).  Lease held up
              to connect-timeout; release packets on connect, drop them at
              flush-timeout.

Any D-Bus failure or a NameOwnerChanged for the manager returns to ``SYNC``.
On stop the trigger is removed but the lease is deliberately **not** released
(§8): it lapses on its own if nothing renews it.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum

from automaton import machines  # pylint: disable=import-error
from dbus_next.aio import MessageBus  # pylint: disable=import-error
from dbus_next.constants import BusType  # pylint: disable=import-error
from dbus_next.errors import DBusError  # pylint: disable=import-error

from vyos.utils.wwan.dial_on_demand._client import open_client_with_retry
from vyos.utils.wwan.dial_on_demand.trigger import NfqueueTrigger
from vyos.utils.wwan.wwan_client import BUS_NAME
from vyos.utils.wwan.wwan_client import WWANError

logger = logging.getLogger(__name__)

LEASE_OWNER = 'dod-traffic'

_DBUS_DAEMON_NAME = 'org.freedesktop.DBus'
_DBUS_DAEMON_PATH = '/org/freedesktop/DBus'

# Errors that mean "the manager link is unusable" -> resync.
_DBUS_ERRORS = (WWANError, DBusError, OSError, EOFError, asyncio.TimeoutError)


class DemandState(str, Enum):
    SYNC = 'SYNC'
    ACTIVE = 'ACTIVE'
    ARMED = 'ARMED'
    DIALING = 'DIALING'


class DemandEvent(str, Enum):
    DBUS_ERROR = 'dbus_error'
    FLUSH_TIMEOUT = 'flush_timeout'
    PACKET_QUEUED = 'packet_queued'
    TX_CHANGED = 'tx_changed'
    DISCONNECTED = 'disconnected'
    CONNECTED = 'connected'


@dataclass(frozen=True)
class DemandTransition:
    from_state: str
    to_state: str
    event: str


TRANSITIONS = [
    DemandTransition(DemandState.SYNC.value, DemandState.ACTIVE.value, DemandEvent.CONNECTED.value),
    DemandTransition(DemandState.SYNC.value, DemandState.ARMED.value, DemandEvent.DISCONNECTED.value),
    DemandTransition(DemandState.ACTIVE.value, DemandState.ACTIVE.value, DemandEvent.TX_CHANGED.value),
    DemandTransition(DemandState.ACTIVE.value, DemandState.ARMED.value, DemandEvent.DISCONNECTED.value),
    DemandTransition(DemandState.ARMED.value, DemandState.DIALING.value, DemandEvent.PACKET_QUEUED.value),
    DemandTransition(DemandState.ARMED.value, DemandState.ACTIVE.value, DemandEvent.CONNECTED.value),
    DemandTransition(DemandState.DIALING.value, DemandState.ACTIVE.value, DemandEvent.CONNECTED.value),
    DemandTransition(DemandState.DIALING.value, DemandState.ARMED.value, DemandEvent.FLUSH_TIMEOUT.value),
] + [
    # any -> SYNC on D-Bus error / manager restart
    DemandTransition(state.value, DemandState.SYNC.value, DemandEvent.DBUS_ERROR.value)
    for state in DemandState
]


class ManagerUnavailable(RuntimeError):
    """The manager stayed unreachable past the SYNC deadline."""


class _ResyncNeeded(Exception):
    """Internal: a D-Bus call failed; go back to SYNC."""


def _read_tx_bytes(interface):
    try:
        with open(f'/sys/class/net/{interface}/statistics/tx_bytes',
                  encoding='utf-8') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class DemandDaemon:
    """Per-interface dial-on-demand state machine.  All state is on the
    instance, so any number of cycles can run in one process."""

    def __init__(self, interface='wwan0', idle_timeout=30, connect_timeout=30,
                 flush_timeout=60, manager_timeout=90.0, call_timeout=10.0,
                 status_poll=2.0, dial_poll=1.0,
                 client_factory=open_client_with_retry, trigger=None,
                 tx_reader=_read_tx_bytes, watch_manager=True):
        self.interface = interface
        self.ifnum = int(interface[len('wwan'):])
        self.idle_timeout = max(1, int(idle_timeout))
        self.connect_timeout = max(1, int(connect_timeout))
        # Never abort a dial before connect-timeout has had a chance to run.
        self.flush_timeout = max(int(flush_timeout), self.connect_timeout)
        # Longer than the manager's own RestartSec=30 (§7).
        self.manager_timeout = float(manager_timeout)
        self.call_timeout = float(call_timeout)
        self.status_poll = float(status_poll)
        self.dial_poll = float(dial_poll)
        # tx_bytes sample cadence and lease renewal cadence (D2).
        self.tx_poll = max(1.0, min(5.0, self.idle_timeout / 3.0))
        # Lease TTL = idle-timeout (D5).  The manager clamps it to 3600 s.
        self.lease_ttl = float(self.idle_timeout)

        self._client_factory = client_factory
        self._trigger = trigger if trigger is not None else NfqueueTrigger(interface)
        self._read_tx = tx_reader
        self._watch_manager = watch_manager

        self.machine = machines.FiniteMachine()
        for state in DemandState:
            self.machine.add_state(state.value)
        for t in TRANSITIONS:
            self.machine.add_transition(t.from_state, t.to_state, t.event)
        self.machine.default_start_state = DemandState.SYNC.value
        self.machine.initialize()

        self._handlers = {
            DemandState.SYNC.value: self._run_sync,
            DemandState.ACTIVE.value: self._run_active,
            DemandState.ARMED.value: self._run_armed,
            DemandState.DIALING.value: self._run_dialing,
        }

        self._client = None
        self._stop = asyncio.Event()
        self._resync = asyncio.Event()
        self._owner_bus = None
        self._owner_iface = None
        self._manager_owner = ''

    @property
    def state(self):
        return self.machine.current_state

    def request_stop(self):
        """Ask ``run()`` to exit (SIGTERM handler)."""
        self._stop.set()

    # ── main loop ────────────────────────────────────────────────────────

    async def run(self):
        """Run until stopped.  Returns the process exit code."""
        logger.info('Dial-on-demand daemon starting on %s (idle %ss, '
                    'connect %ss, flush %ss)', self.interface,
                    self.idle_timeout, self.connect_timeout, self.flush_timeout)
        try:
            await self._trigger.remove_stale()
            if self._watch_manager:
                await self._start_owner_watch()
            while not self._stop.is_set():
                if self.state == DemandState.SYNC.value:
                    self._resync.clear()
                await self._run_state()
            return 0
        except ManagerUnavailable as exc:
            logger.error('%s; exiting', exc)
            return 1
        except Exception:  # pylint: disable=broad-except
            logger.exception('Dial-on-demand daemon failed on %s',
                             self.interface)
            return 1
        finally:
            await self._shutdown()

    async def _run_state(self):
        """Run the current state's handler until it transitions, the manager
        changes owner, or a stop is requested."""
        handler = asyncio.create_task(self._handlers[self.state]())
        stop_wait = asyncio.create_task(self._stop.wait())
        resync_wait = asyncio.create_task(self._resync.wait())
        tasks = (handler, stop_wait, resync_wait)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if handler.done() and not handler.cancelled():
            exc = handler.exception()
            if isinstance(exc, _ResyncNeeded):
                logger.warning('D-Bus error on %s in %s: %s; resyncing',
                               self.interface, self.state, exc)
                await self._fire(DemandEvent.DBUS_ERROR)
            elif exc is not None:
                raise exc
            return

        if self._stop.is_set():
            return
        logger.warning('WWAN manager owner changed; resyncing %s',
                       self.interface)
        await self._fire(DemandEvent.DBUS_ERROR)

    async def _fire(self, event):
        """Process *event* and run the entry action of the new state."""
        old = self.state
        self.machine.process_event(event.value)
        new = self.state
        if new != old:
            logger.info('%s: %s -> %s (%s)', self.interface, old, new,
                        event.value)
        if new == DemandState.ARMED.value and not self._trigger.installed:
            await self._trigger.install()
        elif new == DemandState.ACTIVE.value and old != new:
            # Bearer is up: release anything held and stop trapping.
            await self._trigger.remove()

    # ── state handlers ───────────────────────────────────────────────────

    async def _run_sync(self):
        await self._trigger.remove()
        await self._drop_client()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.manager_timeout
        while True:
            try:
                self._client = await self._client_factory(
                    self.interface,
                    connect_timeout=max(0.0, deadline - loop.time()))
                status = await self._call(
                    self._client.get_bearer_status(self.ifnum))
                break
            except (_ResyncNeeded, *_DBUS_ERRORS) as exc:
                await self._drop_client()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise ManagerUnavailable(
                        f'WWAN manager unreachable for '
                        f'{self.manager_timeout:.0f}s: {exc}') from exc
                logger.info('WWAN manager not reachable yet (%s); retrying',
                            exc)
                await asyncio.sleep(min(1.0, remaining))

        if status == 'connected':
            await self._fire(DemandEvent.CONNECTED)
            # Adopt: hold the lease once so the reconciler owns the bearer
            # and can idle it when there is no traffic (§6).
            await self._acquire_lease('adopt')
        else:
            logger.info("what is status: (%s)", status)
            await self._fire(DemandEvent.DISCONNECTED)

    async def _run_active(self):
        last_tx = self._read_tx(self.interface)
        next_tx_sample = time.monotonic() + self.tx_poll
        tick = min(self.status_poll, self.tx_poll)
        while True:
            await asyncio.sleep(tick)
            status = await self._call(
                self._client.get_bearer_status(self.ifnum))
            if status == 'disconnected':
                # Lease lapsed and the FSM idled the bearer, or it dropped.
                await self._fire(DemandEvent.DISCONNECTED)
                return

            now = time.monotonic()
            if now < next_tx_sample:
                continue
            next_tx_sample = now + self.tx_poll
            tx = self._read_tx(self.interface)
            if tx != last_tx:
                last_tx = tx
                await self._fire(DemandEvent.TX_CHANGED)
                await self._acquire_lease('egress traffic')

    async def _run_armed(self):
        while True:
            if await self._trigger.wait_for_packet(self.status_poll):
                await self._fire(DemandEvent.PACKET_QUEUED)
                await self._acquire_lease('egress traffic')
                return
            status = await self._call(
                self._client.get_bearer_status(self.ifnum))
            if status == 'connected':
                # Brought up by failover/operator/FSM: stop trapping, then
                # adopt so it idles once nobody needs it.
                await self._fire(DemandEvent.CONNECTED)
                await self._acquire_lease('adopt')
                return

    async def _run_dialing(self):
        start = time.monotonic()
        connect_deadline = start + self.connect_timeout
        flush_deadline = start + self.flush_timeout
        last_renew = start
        while True:
            status = await self._call(
                self._client.get_bearer_status(self.ifnum))
            if status == 'connected':
                await self._fire(DemandEvent.CONNECTED)
                return

            now = time.monotonic()
            if now >= flush_deadline:
                self._trigger.drop_all()
                await self._fire(DemandEvent.FLUSH_TIMEOUT)
                return

            # Keep the lease alive only while the dial may still succeed;
            # after connect-timeout it is left to lapse.
            if now < connect_deadline and now - last_renew >= self.tx_poll:
                await self._acquire_lease('dialing')
                last_renew = now

            await asyncio.sleep(min(self.dial_poll, flush_deadline - now))

    # ── D-Bus helpers ────────────────────────────────────────────────────

    async def _call(self, awaitable):
        try:
            return await asyncio.wait_for(awaitable, self.call_timeout)
        except _DBUS_ERRORS as exc:
            raise _ResyncNeeded(str(exc) or type(exc).__name__) from exc

    async def _acquire_lease(self, reason):
        await self._call(self._client.bearer_lease_acquire(
            self.ifnum, LEASE_OWNER, self.lease_ttl, reason))
        logger.info('Lease %s on %s renewed (ttl %ss, %s)', LEASE_OWNER,
                     self.interface, self.lease_ttl, reason)

    async def _drop_client(self):
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.close()
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug('Closing WWAN client failed: %s', exc)

    async def _start_owner_watch(self):
        """Subscribe to NameOwnerChanged for the manager (§7).  Best effort:
        D-Bus call failures still drive a resync if this is unavailable."""
        bus = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            intro = await bus.introspect(_DBUS_DAEMON_NAME, _DBUS_DAEMON_PATH)
            iface = bus.get_proxy_object(
                _DBUS_DAEMON_NAME, _DBUS_DAEMON_PATH, intro
            ).get_interface(_DBUS_DAEMON_NAME)
            try:
                self._manager_owner = await iface.call_get_name_owner(BUS_NAME)
            except DBusError:
                self._manager_owner = ''
            iface.on_name_owner_changed(self._on_name_owner_changed)
            self._owner_bus, self._owner_iface = bus, iface
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning('Manager NameOwnerChanged watch unavailable: %s',
                           exc)
            if bus is not None:
                bus.disconnect()

    def _stop_owner_watch(self):
        if self._owner_iface is not None:
            try:
                self._owner_iface.off_name_owner_changed(
                    self._on_name_owner_changed)
            except Exception:  # pylint: disable=broad-except
                pass
        if self._owner_bus is not None:
            self._owner_bus.disconnect()
        self._owner_iface = None
        self._owner_bus = None

    def _on_name_owner_changed(self, name, old_owner, new_owner):
        if name != BUS_NAME or new_owner == self._manager_owner:
            return
        logger.info('WWAN manager owner %s -> %s', old_owner or '(none)',
                    new_owner or '(none)')
        self._manager_owner = new_owner
        self._resync.set()

    # ── shutdown ─────────────────────────────────────────────────────────

    async def _shutdown(self):
        # Remove the trigger so wwanN egress is never queued with no
        # consumer.  Do NOT release the lease (§8).
        try:
            await self._trigger.remove()
        except Exception as exc:  # pylint: disable=broad-except
            logger.error('Failed to remove dial trigger on %s: %s',
                         self.interface, exc)
        await self._drop_client()
        self._stop_owner_watch()
        logger.info('Dial-on-demand daemon stopped on %s', self.interface)
