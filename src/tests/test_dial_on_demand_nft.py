#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Tests for the dial-on-demand nftables trigger generation.

The key behaviour under test: when a dial-on-demand WWAN is governed by a WAN
load-balancing failover rule, the generated rules include a trigger keyed on the
WLB fwmark (fired AFTER WLB marking), so traffic WLB steers to the idle link can
bootstrap a dial. Without it the RAW-hook FIB rule cannot match WLB-steered
traffic and the bearer would have to be pinned up with a continuous lease.
"""

import sys
import types
from unittest import IsolatedAsyncioTestCase
from unittest import TestCase
from unittest.mock import AsyncMock
from unittest.mock import patch

try:
    __import__('cracklib')
except ModuleNotFoundError:
    _cracklib = types.ModuleType('cracklib')
    _cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = _cracklib

from vyos.utils.wwan.dial_on_demand import install_wwan_nft_rules as nft


class TestWlbInterfaceMark(TestCase):
    """wlb_interface_mark must mirror vyos-load-balancer.py: each
    interface-health member, in config order, gets mark 0xc8 + 1-based index."""

    def _cfg(self):
        return {'interface_health': {'eth0': {}, 'wwan0': {}}}

    def test_first_member_mark(self):
        self.assertEqual(nft.wlb_interface_mark('eth0', self._cfg()), hex(0xc9))

    def test_second_member_mark(self):
        self.assertEqual(nft.wlb_interface_mark('wwan0', self._cfg()), hex(0xca))

    def test_non_member_returns_none(self):
        self.assertIsNone(nft.wlb_interface_mark('wwan9', self._cfg()))

    def test_no_interface_health_returns_none(self):
        self.assertIsNone(nft.wlb_interface_mark('wwan0', {}))


class TestGenerateNftRules(IsolatedAsyncioTestCase):
    def _failover_cfg(self):
        return {
            'interface_health': {'eth0': {}, 'wwan0': {}},
            'rule': {'10': {
                'interface': {'eth0': {'weight': '10'},
                              'wwan0': {'weight': '1'}},
                'inbound_interface': 'any',
                'failover': {},
            }},
        }

    def _loadbalance_cfg(self):
        cfg = self._failover_cfg()
        del cfg['rule']['10']['failover']
        return cfg

    async def _run(self, config):
        captured = {}

        async def fake_nft(cmd=''):
            captured['cmd'] = cmd

        with patch.object(nft, 'get_config', return_value=config), \
             patch.object(nft, 'get_inbound_interfaces', return_value=['eth0']), \
             patch.object(nft, 'async_run_nft_cmd', AsyncMock(side_effect=fake_nft)):
            await nft.generate_nft_rules(interface='wwan0')
        return captured.get('cmd', '')

    async def test_failover_adds_mark_trigger(self):
        cmd = await self._run(self._failover_cfg())
        # The mark trigger fires after WLB marking (priority -140 > mangle -150)
        # on exactly the mark WLB steers to wwan0 (second health member -> 0xca).
        self.assertIn('wlb_demand', cmd)
        self.assertIn('priority -140', cmd)
        self.assertIn('meta mark 0xca queue num 1', cmd)
        # The original RAW-hook FIB rule is still emitted for the standalone path.
        self.assertIn('fib daddr oifname "wwan0"', cmd)

    async def test_loadbalance_rule_has_no_mark_trigger(self):
        # A non-failover (weighted load-balance) rule must not add the failover
        # dial trigger.
        cmd = await self._run(self._loadbalance_cfg())
        self.assertNotIn('wlb_demand', cmd)
        self.assertNotIn('meta mark', cmd)

    async def test_standalone_has_no_mark_trigger(self):
        # No load-balancing rules at all -> standalone intercept, no WLB mark.
        cmd = await self._run({'interface_health': {'wwan0': {}}})
        self.assertNotIn('wlb_demand', cmd)
