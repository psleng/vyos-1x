#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
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

"""
Bearer lease registry
======================

A small, dependency-free data structure that records *who* currently wants a
WWAN data bearer to be up, so the modem state machine (FSM) can make a single
authoritative up/down decision instead of several components racing to connect
and disconnect the bearer directly.

Model
-----
Every client that needs the bearer up holds a **lease**, identified by a
``(interface_number, owner)`` pair.  A lease is *positive demand only*: its
presence means "I want this bearer up"; its absence means "I no longer need
it".  There are no negative/veto leases -- a hard *down* is expressed by the
FSM's admin-disable, and the resting *idle* state is simply the absence of any
lease (for the on-demand connection modes).

Leases are **time-bounded**.  An owner promises to renew its lease within
``ttl`` seconds; if it stops renewing (or crashes), the lease expires and its
demand disappears on its own.  This makes a forgotten or crashed client safe on
a metered cellular link: a stuck lease cannot pin the bearer up forever.  An
``owner`` may hold a *persistent* lease (``ttl=None``) that never expires and
must be released explicitly -- reserved for operator intent.

The bearer-up decision lives in the FSM and is, in pseudocode::

    desired = DOWN  if admin_disabled
              UP    if connection_mode == 'always-on'        # policy floor
              UP    if registry.any_live(iface)              # demand
              IDLE  otherwise

This module intentionally knows nothing about D-Bus, ModemManager, timers or
connection modes.  It is a pure in-memory map with an injectable clock so it
can be unit-tested deterministically.

Concurrency
-----------
The registry is **not** internally locked.  It is owned by the WWAN manager
process and is only ever mutated from that process's single asyncio event loop
(the D-Bus method handlers and the FSM reconcile/sweep task).  Cross-process
clients (WLB, the dial-on-demand service) do not touch it directly -- they call
the D-Bus lease API, which runs on that same loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

# Safety ceiling applied to every *finite* lease TTL.  A client asking for a
# longer TTL is clamped to this value, so even a well-behaved-but-misconfigured
# renewer cannot pin a metered bearer up for an unreasonable span between
# renewals.  Persistent leases (ttl=None) bypass this on purpose and are gated
# by the D-Bus owner allow-list instead.
DEFAULT_MAX_TTL = 3600.0


@dataclass
class Lease:
    """A single demand token for one interface, held by one named owner.

    Attributes
    ----------
    interface_number:
        WWAN interface index (the ``N`` in ``wwanN``).
    owner:
        Allow-listed client identity, e.g. ``"failover"``, ``"dod-traffic"``
        or ``"cli"``.  Unique per interface: re-acquiring with the same owner
        refreshes the existing lease rather than stacking a second one.
    reason:
        Free-text annotation surfaced by operational ``show`` output and logs
        (e.g. ``"active failover target"`` or ``"egress traffic"``).
    ttl:
        Renewal budget in seconds, or ``None`` for a persistent lease that
        never expires and must be released explicitly.
    acquired_at:
        Monotonic timestamp of first acquisition (kept across renewals; used
        for the displayed age).
    renewed_at:
        Monotonic timestamp of the most recent acquire/renew.
    expires_at:
        Monotonic deadline, or ``None`` for a persistent lease.
    """

    interface_number: int
    owner: str
    reason: str = ''
    ttl: Optional[float] = None
    acquired_at: float = 0.0
    renewed_at: float = 0.0
    expires_at: Optional[float] = None

    def is_live(self, now: float) -> bool:
        """Return ``True`` if this lease has not expired as of *now*."""
        if self.expires_at is None:
            return True
        return self.expires_at > now

    def expires_in(self, now: float) -> Optional[float]:
        """Seconds until expiry (never negative), or ``None`` if persistent."""
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - now)


class BearerLeaseRegistry:
    """In-memory store of bearer-demand leases, keyed by ``(interface, owner)``.

    Parameters
    ----------
    clock:
        Callable returning a monotonic timestamp.  Defaults to
        :func:`time.monotonic`; tests inject a fake clock.
    max_ttl:
        Safety ceiling applied to finite TTLs.  Set to ``None`` to disable
        clamping (not recommended outside tests).
    """

    def __init__(self,
                 clock: Callable[[], float] = time.monotonic,
                 max_ttl: Optional[float] = DEFAULT_MAX_TTL) -> None:
        self._clock = clock
        self._max_ttl = max_ttl
        # (interface_number, owner) -> Lease
        self._leases: Dict[Tuple[int, str], Lease] = {}

    # ---- mutation -----------------------------------------------------------

    def acquire(self,
                interface_number: int,
                owner: str,
                ttl: Optional[float] = None,
                reason: str = '',
                now: Optional[float] = None) -> Lease:
        """Create or refresh a lease (acquire and renew are the same op).

        Idempotent per ``(interface_number, owner)``: a repeat call refreshes
        the deadline/reason in place and returns the same logical lease.  A
        finite ``ttl`` is clamped to ``max_ttl``; ``ttl=None`` yields a
        persistent lease.
        """
        if now is None:
            now = self._clock()

        clamped = ttl
        if ttl is not None:
            ttl = float(ttl)
            if self._max_ttl is not None:
                clamped = min(ttl, self._max_ttl)
            else:
                clamped = ttl

        key = (interface_number, owner)
        existing = self._leases.get(key)
        acquired_at = existing.acquired_at if existing is not None else now
        expires_at = None if clamped is None else now + clamped

        lease = Lease(
            interface_number=interface_number,
            owner=owner,
            reason=reason,
            ttl=clamped,
            acquired_at=acquired_at,
            renewed_at=now,
            expires_at=expires_at,
        )
        self._leases[key] = lease
        return lease

    # ``renew`` is a readability alias; acquire already refreshes in place.
    renew = acquire

    def release(self, interface_number: int, owner: str) -> bool:
        """Drop a lease.  Returns ``True`` if one existed, ``False`` otherwise."""
        return self._leases.pop((interface_number, owner), None) is not None

    def release_owner(self, owner: str) -> List[int]:
        """Drop ``owner``'s lease on every interface.

        Returns the list of interface numbers from which a lease was removed.
        Used when a client disowns all its demand at once.
        """
        removed = [iface for (iface, own) in list(self._leases)
                   if own == owner]
        for iface in removed:
            self._leases.pop((iface, owner), None)
        return removed

    def sweep(self, now: Optional[float] = None) -> List[Lease]:
        """Remove every expired lease.  Returns the leases that were removed.

        The FSM calls this on its periodic tick; callers then reconcile any
        interface whose demand just dropped to zero.  Liveness queries do not
        depend on sweep having run -- :meth:`any_live` and friends evaluate the
        clock directly -- so sweep is purely housekeeping to bound memory.
        """
        if now is None:
            now = self._clock()
        expired = [lease for lease in self._leases.values()
                   if not lease.is_live(now)]
        for lease in expired:
            self._leases.pop((lease.interface_number, lease.owner), None)
        return expired

    def clear(self, interface_number: Optional[int] = None) -> None:
        """Drop all leases, or all leases for one interface."""
        if interface_number is None:
            self._leases.clear()
            return
        for key in [k for k in self._leases if k[0] == interface_number]:
            self._leases.pop(key, None)

    # ---- queries ------------------------------------------------------------

    def any_live(self, interface_number: int,
                 now: Optional[float] = None) -> bool:
        """Is there at least one unexpired lease for this interface?

        This is the demand signal the FSM ORs with its policy floor.
        """
        if now is None:
            now = self._clock()
        for (iface, _owner), lease in self._leases.items():
            if iface == interface_number and lease.is_live(now):
                return True
        return False

    def live_leases(self, interface_number: int,
                    now: Optional[float] = None) -> List[Lease]:
        """All unexpired leases for this interface, sorted by owner."""
        if now is None:
            now = self._clock()
        leases = [lease for (iface, _owner), lease in self._leases.items()
                  if iface == interface_number and lease.is_live(now)]
        return sorted(leases, key=lambda lease: lease.owner)

    def owners(self, interface_number: int,
               now: Optional[float] = None) -> List[str]:
        """Owner identities currently holding a live lease on this interface."""
        return [lease.owner for lease in self.live_leases(interface_number, now)]

    def get(self, interface_number: int, owner: str,
            now: Optional[float] = None) -> Optional[Lease]:
        """Return the live lease for ``(interface, owner)``, or ``None``."""
        if now is None:
            now = self._clock()
        lease = self._leases.get((interface_number, owner))
        if lease is not None and lease.is_live(now):
            return lease
        return None

    def interfaces(self, now: Optional[float] = None) -> List[int]:
        """Interface numbers that currently have at least one live lease."""
        if now is None:
            now = self._clock()
        found = {iface for (iface, _owner), lease in self._leases.items()
                 if lease.is_live(now)}
        return sorted(found)

    def snapshot(self, interface_number: int,
                 now: Optional[float] = None) -> List[dict]:
        """A JSON-friendly view of the live leases, for operational ``show``.

        Each entry carries the owner, reason, ttl, seconds-until-expiry
        (``None`` when persistent) and the lease age in seconds.
        """
        if now is None:
            now = self._clock()
        result = []
        for lease in self.live_leases(interface_number, now):
            result.append({
                'owner': lease.owner,
                'reason': lease.reason,
                'ttl': lease.ttl,
                'expires_in': lease.expires_in(now),
                'age': max(0.0, now - lease.acquired_at),
            })
        return result
