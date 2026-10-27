#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""End-to-end seam test: dial-on-demand WWAN over ``protocols failover``.

A dial-on-demand WWAN failover next-hop parks its bearer idle. For the data
trigger to ever dial it, the failover daemon must keep the (idle) link selected
as a backup next-hop and install its route, WITHOUT taking a bearer lease -- the
nft/NFQUEUE traffic trigger is what dials. This covers that contract:

  1. health  -- an idle dial-on-demand WWAN stays eligible via the FSM modem
                spoof (same method as connect-on-demand), not an IP probe;
  2. no lease -- dial-on-demand is never lease-managed (it relies on the trigger);
  3. promote -- the route-install command is family-correct for IPv4 AND IPv6;
  4. caveats -- a non-live FSM state withdraws the route (black-hole), and the
                spoof is only consulted when a check target is configured.

The live half of the seam (promoted route -> forwarded packet -> NFQUEUE ->
connect_bearer) runs on the unit; see test_dial_on_demand_nft.py for the
family-agnostic trigger ruleset that fires once the route is installed.
"""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock
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


def _target(addr='192.0.2.1'):
    return SimpleNamespace(target=addr, vrf='', interface='')


def _nh_for_cmd(route, iface='wwan0', metric=10):
    # Only the fields get_ip_command_args() reads; a via-less (point-to-point)
    # WWAN next-hop has next_hop=None.
    return SimpleNamespace(route=route, next_hop=None, conf_iface=iface,
                           onlink='', conf_metric=metric, vrf_opt='')


class TestDialOnDemandFailoverSeam(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vf = _load_failover()

    def _modem_client(self, fsm_state):
        """A fake WWANClientSync reporting a fixed FSM state."""
        client = MagicMock()
        client.get_status.return_value = {'fsm_state': fsm_state}
        factory = MagicMock(return_value=client)
        return factory

    def _is_alive(self, fsm_state, targets=(_target(),), on_demand=True):
        # rc_cmd is stubbed to FAIL: if the health path ever fell through to an
        # ICMP/ARP/TCP probe the result would be False, so a True result proves
        # the modem spoof short-circuited the probe.
        with patch.object(self.vf, 'on_demand_check',
                          side_effect=lambda i: on_demand and i == 'wwan0'), \
             patch.object(self.vf, 'WWANClientSync',
                          self._modem_client(fsm_state)), \
             patch.object(self.vf, 'rc_cmd', return_value=(1, '')):
            return self.vf.is_target_alive(list(targets), 'wwan0', proto='icmp')

    # 1. health -----------------------------------------------------------
    def test_idle_states_keep_wwan_eligible(self):
        # The three "on the network" FSM states must all read as healthy so the
        # idle dial-on-demand link stays selected as a backup next-hop.
        for state in ('CONNECTED', 'REGISTERED_IDLE', 'CONNECTING'):
            with self.subTest(fsm_state=state):
                self.assertTrue(self._is_alive(state))

    def test_health_via_spoof_not_probe(self):
        # Healthy FSM + failing rc_cmd => True can only come from the spoof.
        self.assertTrue(self._is_alive('REGISTERED_IDLE'))

    # 4. caveat: black-hole ----------------------------------------------
    def test_non_live_fsm_state_withdraws_route(self):
        # If the idle bearer ever leaves the live set, the spoof reports down and
        # failover withdraws the route (documents the black-hole risk).
        for state in ('DISCONNECTED', 'REGISTERING', 'UNKNOWN'):
            with self.subTest(fsm_state=state):
                self.assertFalse(self._is_alive(state))

    # 4. caveat: a check target must exist -------------------------------
    def test_spoof_requires_a_check_target(self):
        # With no check targets the loop body (and thus the spoof) never runs,
        # so the next-hop reads as down and is never promoted.
        self.assertFalse(self._is_alive('REGISTERED_IDLE', targets=()))

    # 2. no lease ---------------------------------------------------------
    def test_dial_on_demand_not_lease_managed(self):
        # dial-on-demand is NOT connect-on-demand, so it must not be selected for
        # a bearer lease: it relies on the traffic trigger to dial instead.
        nh = [SimpleNamespace(conf_iface='eth0', conf_metric=1),
              SimpleNamespace(conf_iface='wwan0', conf_metric=3)]
        with patch.object(self.vf, 'connect_on_demand_check', return_value=False):
            managed, active = self.vf.active_connect_on_demand_wwans(
                nh, {'eth0': False, 'wwan0': True})
        self.assertEqual(managed, set())
        self.assertEqual(active, set())

    # 3. promote: family-correct install command --------------------------
    def test_route_promotion_command_ipv4(self):
        cmd = self.vf.get_ip_command_args(_nh_for_cmd('0.0.0.0/0'))
        self.assertIn('0.0.0.0/0', cmd)
        self.assertIn('dev wwan0', cmd)
        self.assertIn('metric 10', cmd)
        self.assertIn('proto failover', cmd)
        self.assertNotIn('via', cmd)  # via-less point-to-point WWAN

    def test_route_promotion_command_ipv6(self):
        cmd = self.vf.get_ip_command_args(_nh_for_cmd('::/0'))
        self.assertIn('::/0', cmd)
        self.assertIn('dev wwan0', cmd)
        self.assertIn('metric 10', cmd)
        self.assertIn('proto failover', cmd)
        self.assertNotIn('via', cmd)
