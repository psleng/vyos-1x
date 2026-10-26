#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Tests for protocols-failover connect-on-demand bearer-lease selection.

A connect-on-demand WWAN failover next-hop parks the bearer idle, so when it
becomes the active (lowest-metric healthy) next-hop the daemon must raise it via
a bearer lease, and release it (after a hold-down) when a better path recovers.
dial-on-demand WWANs are NOT leased (their traffic trigger dials).
"""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

try:
    __import__('cracklib')
except ModuleNotFoundError:
    _cracklib = types.ModuleType('cracklib')
    _cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = _cracklib


def _load_failover():
    path = os.path.join(os.path.dirname(__file__), '..', 'helpers',
                        'vyos-failover.py')
    spec = importlib.util.spec_from_file_location('vyos_failover', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nh(iface, metric):
    return SimpleNamespace(conf_iface=iface, conf_metric=metric)


def _route(*nexthops):
    return SimpleNamespace(nexthops=tuple(nexthops))


class TestActiveSelection(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vf = _load_failover()

    def _active(self, nexthops, alive, cod=('wwan0',)):
        with patch.object(self.vf, 'connect_on_demand_check',
                          side_effect=lambda i: i in cod):
            return self.vf.active_connect_on_demand_wwans(nexthops, alive)

    def test_wwan_managed_but_not_active_when_primary_up(self):
        nh = [_nh('eth0', 1), _nh('wwan0', 3)]
        managed, active = self._active(nh, {'eth0': True, 'wwan0': True})
        self.assertEqual(managed, {'wwan0'})
        self.assertEqual(active, set())

    def test_wwan_active_when_all_higher_priority_down(self):
        nh = [_nh('eth0', 1), _nh('eth1', 2), _nh('wwan0', 3)]
        managed, active = self._active(
            nh, {'eth0': False, 'eth1': False, 'wwan0': True})
        self.assertEqual(active, {'wwan0'})

    def test_wwan_not_active_when_a_higher_priority_alive(self):
        nh = [_nh('eth0', 1), _nh('eth1', 2), _nh('wwan0', 3)]
        managed, active = self._active(
            nh, {'eth0': False, 'eth1': True, 'wwan0': True})
        self.assertEqual(active, set())

    def test_non_connect_on_demand_wwan_not_managed(self):
        # e.g. a dial-on-demand wwan -> not lease-managed here.
        nh = [_nh('eth0', 1), _nh('wwan0', 3)]
        managed, active = self._active(
            nh, {'eth0': False, 'wwan0': True}, cod=())
        self.assertEqual(managed, set())
        self.assertEqual(active, set())


class TestReconcileLeases(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vf = _load_failover()

    def _routes(self):
        # eth0 m1 > eth1 m2 > wwan0 m3 (connect-on-demand)
        return [_route(_nh('eth0', 1), _nh('eth1', 2), _nh('wwan0', 3))]

    def _reconcile(self, alive, hold, now):
        with patch.object(self.vf, 'connect_on_demand_check',
                          side_effect=lambda i: i == 'wwan0'), \
             patch.object(self.vf, '_bearer_lease') as lease:
            self.vf.reconcile_failover_leases(self._routes(), alive, hold, now=now)
        return lease

    def test_acquires_when_active(self):
        hold = {}
        lease = self._reconcile({'eth0': False, 'eth1': False, 'wwan0': True},
                                hold, now=1000)
        lease.assert_called_once()
        self.assertEqual(lease.call_args.args[0], 'wwan0')
        self.assertTrue(lease.call_args.kwargs.get('acquire'))
        self.assertEqual(hold.get('wwan0'), 1000)

    def test_holddown_then_release(self):
        hd = self.vf.failover_release_holddown
        hold = {}
        # active -> acquire + record
        self._reconcile({'eth0': False, 'eth1': False, 'wwan0': True}, hold, 1000)
        # primary recovers within hold-down -> keep acquiring (hold), no release
        lease = self._reconcile({'eth0': True, 'wwan0': True}, hold, 1000 + hd - 1)
        self.assertTrue(lease.call_args.kwargs.get('acquire'))
        # past hold-down -> release
        lease = self._reconcile({'eth0': True, 'wwan0': True}, hold, 1000 + hd + 1)
        self.assertFalse(lease.call_args.kwargs.get('acquire'))
        self.assertNotIn('wwan0', hold)

    def test_no_managed_wwan_is_noop(self):
        with patch.object(self.vf, 'connect_on_demand_check', return_value=False), \
             patch.object(self.vf, '_bearer_lease') as lease:
            self.vf.reconcile_failover_leases(self._routes(),
                                              {'eth0': True}, {}, now=1000)
        lease.assert_not_called()
