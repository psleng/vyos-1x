#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Unit tests for the bearer lease registry (vyos.utils.wwan.bearer_lease)."""

import sys
import types
from unittest import TestCase

# Optional on developer workstations, but imported by vyos package startup.
try:
    __import__('cracklib')
except ModuleNotFoundError:
    _cracklib = types.ModuleType('cracklib')
    _cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = _cracklib

from vyos.utils.wwan.bearer_lease import BearerLeaseRegistry
from vyos.utils.wwan.bearer_lease import DEFAULT_MAX_TTL


class _FakeClock:
    """A manually advanced monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestBearerLeaseRegistry(TestCase):
    def setUp(self):
        self.clock = _FakeClock()
        self.reg = BearerLeaseRegistry(clock=self.clock, max_ttl=None)

    # ---- basic demand ------------------------------------------------------

    def test_no_leases_means_no_demand(self):
        self.assertFalse(self.reg.any_live(0))
        self.assertEqual(self.reg.live_leases(0), [])
        self.assertEqual(self.reg.owners(0), [])
        self.assertEqual(self.reg.interfaces(), [])

    def test_acquire_creates_demand(self):
        self.reg.acquire(0, 'cli', ttl=None, reason='operator')
        self.assertTrue(self.reg.any_live(0))
        self.assertEqual(self.reg.owners(0), ['cli'])

    def test_demand_is_per_interface(self):
        self.reg.acquire(0, 'cli', ttl=None)
        self.assertTrue(self.reg.any_live(0))
        self.assertFalse(self.reg.any_live(1))
        self.assertEqual(self.reg.interfaces(), [0])

    def test_release_drops_demand(self):
        self.reg.acquire(0, 'cli', ttl=None)
        self.assertTrue(self.reg.release(0, 'cli'))
        self.assertFalse(self.reg.any_live(0))
        # releasing a non-existent lease is a harmless False
        self.assertFalse(self.reg.release(0, 'cli'))

    # ---- TTL expiry --------------------------------------------------------

    def test_finite_lease_expires(self):
        self.reg.acquire(0, 'dod-traffic', ttl=30)
        self.assertTrue(self.reg.any_live(0))
        self.clock.advance(29)
        self.assertTrue(self.reg.any_live(0))
        self.clock.advance(2)  # now 31s elapsed > 30s ttl
        self.assertFalse(self.reg.any_live(0))

    def test_renew_extends_expiry(self):
        self.reg.acquire(0, 'dod-traffic', ttl=30)
        self.clock.advance(20)
        self.reg.renew(0, 'dod-traffic', ttl=30)  # fresh 30s from now
        self.clock.advance(20)  # 40s since first acquire, 20s since renew
        self.assertTrue(self.reg.any_live(0))
        self.clock.advance(11)
        self.assertFalse(self.reg.any_live(0))

    def test_persistent_lease_never_expires(self):
        self.reg.acquire(0, 'cli', ttl=None)
        self.clock.advance(10_000)
        self.assertTrue(self.reg.any_live(0))

    def test_renew_preserves_acquired_at_age(self):
        self.reg.acquire(0, 'failover', ttl=15)
        self.clock.advance(10)
        self.reg.renew(0, 'failover', ttl=15)
        snap = self.reg.snapshot(0)[0]
        # age is measured from first acquisition, not the renew
        self.assertAlmostEqual(snap['age'], 10.0)

    # ---- multiple owners ---------------------------------------------------

    def test_multiple_owners_or_together(self):
        self.reg.acquire(0, 'failover', ttl=15)
        self.reg.acquire(0, 'dod-traffic', ttl=30)
        self.assertTrue(self.reg.any_live(0))
        self.assertEqual(self.reg.owners(0), ['dod-traffic', 'failover'])
        # the shorter failover lease lapses first; traffic still holds demand up
        self.clock.advance(16)
        self.assertTrue(self.reg.any_live(0))
        self.assertEqual(self.reg.owners(0), ['dod-traffic'])
        self.clock.advance(20)
        self.assertFalse(self.reg.any_live(0))

    def test_reacquire_same_owner_is_idempotent(self):
        self.reg.acquire(0, 'cli', ttl=None, reason='first')
        self.reg.acquire(0, 'cli', ttl=None, reason='second')
        leases = self.reg.live_leases(0)
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].reason, 'second')

    def test_release_owner_everywhere(self):
        self.reg.acquire(0, 'failover', ttl=15)
        self.reg.acquire(1, 'failover', ttl=15)
        self.reg.acquire(1, 'dod-traffic', ttl=30)
        removed = self.reg.release_owner('failover')
        self.assertEqual(sorted(removed), [0, 1])
        self.assertFalse(self.reg.any_live(0))
        self.assertEqual(self.reg.owners(1), ['dod-traffic'])

    # ---- sweep -------------------------------------------------------------

    def test_sweep_removes_only_expired(self):
        self.reg.acquire(0, 'dod-traffic', ttl=30)
        self.reg.acquire(0, 'cli', ttl=None)
        self.clock.advance(31)
        removed = self.reg.sweep()
        self.assertEqual([lease.owner for lease in removed], ['dod-traffic'])
        self.assertEqual(self.reg.owners(0), ['cli'])

    def test_any_live_independent_of_sweep(self):
        # Liveness must reflect the clock even if sweep has not run.
        self.reg.acquire(0, 'dod-traffic', ttl=30)
        self.clock.advance(31)
        self.assertFalse(self.reg.any_live(0))  # no sweep called

    # ---- max_ttl clamp -----------------------------------------------------

    def test_max_ttl_clamps_finite_ttl(self):
        reg = BearerLeaseRegistry(clock=self.clock, max_ttl=60)
        lease = reg.acquire(0, 'dod-traffic', ttl=600)
        self.assertEqual(lease.ttl, 60)
        self.clock.advance(61)
        self.assertFalse(reg.any_live(0))

    def test_max_ttl_does_not_affect_persistent(self):
        reg = BearerLeaseRegistry(clock=self.clock, max_ttl=60)
        reg.acquire(0, 'cli', ttl=None)
        self.clock.advance(10_000)
        self.assertTrue(reg.any_live(0))

    def test_default_max_ttl_value(self):
        self.assertEqual(DEFAULT_MAX_TTL, 3600.0)

    # ---- snapshot ----------------------------------------------------------

    def test_snapshot_shape(self):
        self.reg.acquire(0, 'failover', ttl=15, reason='active failover target')
        self.clock.advance(5)
        snap = self.reg.snapshot(0)
        self.assertEqual(len(snap), 1)
        entry = snap[0]
        self.assertEqual(entry['owner'], 'failover')
        self.assertEqual(entry['reason'], 'active failover target')
        self.assertEqual(entry['ttl'], 15)
        self.assertAlmostEqual(entry['expires_in'], 10.0)
        self.assertAlmostEqual(entry['age'], 5.0)

    def test_snapshot_persistent_expires_in_none(self):
        self.reg.acquire(0, 'cli', ttl=None)
        snap = self.reg.snapshot(0)
        self.assertIsNone(snap[0]['expires_in'])

    def test_get_returns_live_lease_only(self):
        self.reg.acquire(0, 'dod-traffic', ttl=30)
        self.assertIsNotNone(self.reg.get(0, 'dod-traffic'))
        self.clock.advance(31)
        self.assertIsNone(self.reg.get(0, 'dod-traffic'))
