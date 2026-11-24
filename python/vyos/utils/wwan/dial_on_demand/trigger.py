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

"""NFQUEUE dial-on-demand trigger for one WWAN interface.

Owns the per-interface nft table ``inet wwanN_raw_<q>`` and the
NetfilterQueue binding that consumes it.  All state lives on the instance (no
module globals), so the long-running ``DemandDaemon`` can arm and disarm the
trigger any number of times in one process.

See docs/igos-wwan-dial-on-demand-design.md (§4, §8, §10).
"""

import asyncio
import logging
import re
from collections import deque

from vyos.utils.wwan.dial_on_demand.install_wwan_nft_rules import (
    async_run_nft_cmd,
    basic_nft_commands,
    get_inbound_interfaces,
    wwan_queue_num,
)

logger = logging.getLogger(__name__)


def _default_nfqueue_factory():
    # Imported lazily so unit tests can inject a fake without the C extension.
    from netfilterqueue import NetfilterQueue  # pylint: disable=import-error

    return NetfilterQueue()


class NfqueueTrigger:
    """Install/remove the dial trigger and hold the packets it queues.

    Lifecycle used by the daemon:

    - ``ARMED``:   ``await install()`` then ``await wait_for_packet(t)``.
    - ``DIALING``: keep holding; ``drop_all()`` on flush-timeout (trigger
      stays installed so the next packet re-triggers a dial).
    - ``ACTIVE``:  ``await remove()`` (accepts anything still held).
    - stop:        ``await remove()``.
    """

    def __init__(self, interface, max_queued=1024,
                 nft_runner=async_run_nft_cmd,
                 inbound_interfaces=get_inbound_interfaces,
                 nfqueue_factory=_default_nfqueue_factory):
        self.interface = interface
        self.queue_num = wwan_queue_num(interface)
        self.table = f'{interface}_raw_{self.queue_num}'
        self._max_queued = max_queued
        self._run_nft = nft_runner
        self._inbound_interfaces = inbound_interfaces
        self._nfqueue_factory = nfqueue_factory

        self._loop = None
        self._nfqueue = None
        self._fd = None
        self._packets = deque()
        self._packet_event = asyncio.Event()
        # When True every newly queued packet is accepted immediately (bearer
        # is up but the table has not been removed yet).
        self._passthrough = False
        self._installed = False

    # ── public API ───────────────────────────────────────────────────────

    @property
    def installed(self):
        return self._installed

    @property
    def queued(self):
        return len(self._packets)

    async def remove_stale(self):
        """Delete every ``<interface>_raw_<n>`` table in any family.

        Covers tables left behind by a crashed instance or by an older
        queue-number scheme.  Called once at daemon start-up.
        """
        proc = await asyncio.create_subprocess_exec(
            'nft', 'list', 'tables',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f'nft list tables failed: {stderr.decode().strip()}')
        pattern = re.compile(
            rf'^table\s+(\S+)\s+({re.escape(self.interface)}_raw_\d+)$')
        script = ''
        for line in stdout.decode().splitlines():
            match = pattern.match(line.strip())
            if match:
                script += f'delete table {match.group(1)} {match.group(2)}\n'
        if script:
            logger.info('Removing stale dial trigger tables on %s',
                        self.interface)
            await self._run_nft(script)

    async def install(self):
        """Idempotently (re)install the trigger table and bind the queue.

        The queue is bound *before* the table is added so no packet can hit
        an NFQUEUE with no consumer.  The table is replaced atomically
        (``add`` + ``delete`` + re-add in one nft transaction), so repeated
        calls or a crash between install and remove never stack tables.
        """
        self._passthrough = False
        self._bind()
        try:
            await self._run_nft(self._install_script())
        except BaseException:
            # A failed batch leaves the previous table (if any) in place;
            # try to remove it so an unbound queue cannot black-hole egress.
            try:
                await self._run_nft(self._delete_script())
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning('Failed to remove %s after install error: %s',
                               self.table, exc)
            self._installed = False
            self._release_all(accept=True)
            self._unbind()
            raise
        self._installed = True
        logger.info('Dial trigger armed on %s (table inet %s, queue %d)',
                    self.interface, self.table, self.queue_num)

    async def remove(self, drop=False):
        """Remove the trigger table, release held packets, unbind the queue.

        Held packets are accepted unless ``drop`` is True.  Safe to call when
        nothing is installed.
        """
        try:
            await self._run_nft(self._delete_script())
        finally:
            was_installed = self._installed
            self._installed = False
            # Pull in anything the kernel queued before the table went away so
            # it gets an explicit verdict instead of being dropped on unbind.
            self._drain_socket()
            self._release_all(accept=not drop)
            self._unbind()
            if was_installed:
                logger.info('Dial trigger removed from %s', self.interface)

    async def wait_for_packet(self, timeout=None):
        """Return True once at least one packet is held, False on timeout."""
        if self._packets:
            return True
        self._packet_event.clear()
        try:
            await asyncio.wait_for(self._packet_event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return bool(self._packets)

    def accept_all(self):
        """Accept every held packet and pass through any further ones."""
        self._passthrough = True
        return self._release_all(accept=True)

    def drop_all(self):
        """Drop every held packet; keep trapping new ones."""
        count = self._release_all(accept=False)
        if count:
            logger.info('Dropped %d queued packet(s) on %s',
                        count, self.interface)
        return count

    # ── internals ────────────────────────────────────────────────────────

    def _install_script(self):
        ifaces = ', '.join(self._inbound_interfaces())
        return (
            self._delete_script()
            + basic_nft_commands(self.interface, self.queue_num)
            + f'add rule inet {self.table} prerouting '
              f'iifname {{ {ifaces} }} fib daddr oifname "{self.interface}" '
              f'queue num {self.queue_num}\n'
        )

    def _delete_script(self):
        # 'add' first makes the 'delete' a no-op-safe operation when the
        # table does not exist.
        return (f'add table inet {self.table}\n'
                f'delete table inet {self.table}\n')

    def _bind(self):
        if self._nfqueue is not None:
            return
        self._loop = asyncio.get_running_loop()
        nfqueue = self._nfqueue_factory()
        nfqueue.bind(self.queue_num, self._on_packet)
        self._nfqueue = nfqueue
        self._fd = nfqueue.get_fd()
        self._loop.add_reader(self._fd, self._on_readable)

    def _unbind(self):
        if self._nfqueue is None:
            return
        if self._fd is not None and self._loop is not None:
            self._loop.remove_reader(self._fd)
        try:
            self._nfqueue.unbind()
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug('NFQUEUE unbind failed: %s', exc)
        self._nfqueue = None
        self._fd = None

    def _on_readable(self):
        self._drain_socket()

    def _drain_socket(self):
        if self._nfqueue is None:
            return
        try:
            self._nfqueue.run(block=False)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning('NFQUEUE read failed on %s: %s',
                           self.interface, exc)

    def _on_packet(self, packet):
        # Runs on the event-loop thread (called from run(block=False)).
        if self._passthrough:
            packet.accept()
            return
        if len(self._packets) >= self._max_queued:
            packet.drop()
            return
        packet.retain()
        if not self._packets:
            logger.info('Dial trigger fired on %s', self.interface)
        self._packets.append(packet)
        self._packet_event.set()

    def _release_all(self, accept):
        count = 0
        while self._packets:
            packet = self._packets.popleft()
            try:
                if accept:
                    packet.accept()
                else:
                    packet.drop()
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug('NFQUEUE verdict failed: %s', exc)
            count += 1
        return count
