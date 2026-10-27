#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Tests for the dial-on-demand nftables trigger generation.

The key behaviour under test: the generated rules intercept router-originated
traffic leaving the WWAN (RAW output hook) and LAN->WAN forwarded traffic whose
FIB egress resolves to the WWAN (RAW prerouting hook), queueing both so an idle
dial-on-demand bearer can be bootstrapped. IPv4 and IPv6 unicast must both reach
the trigger; only IPv6 multicast chatter is dropped first.
"""

import sys
import types
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from unittest.mock import patch

try:
    __import__('cracklib')
except ModuleNotFoundError:
    _cracklib = types.ModuleType('cracklib')
    _cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = _cracklib

from vyos.utils.wwan.dial_on_demand import install_wwan_nft_rules as nft


class TestIpv6TrapParity(IsolatedAsyncioTestCase):
    """IPv6 unicast must reach the dial trigger (parity with IPv4); only IPv6
    multicast chatter is dropped."""

    def test_basic_commands_drop_only_multicast(self):
        cmd = nft.basic_nft_commands('wwan0', 1)
        # Only IPv6 multicast is dropped before the queue...
        self.assertIn('ip6 daddr ff00::/8 drop', cmd)
        # ...NOT all IPv6 (which would also block genuine unicast).
        self.assertNotIn('meta nfproto ipv6 drop', cmd)
        # The protocol-agnostic queue rule still follows the drop, so IPv4 and
        # IPv6 unicast both reach the NFQUEUE trigger.
        self.assertIn('oifname wwan0 queue num 1', cmd)

    async def _run(self):
        captured = {}

        async def fake_nft(cmd=''):
            captured['cmd'] = cmd

        with patch.object(nft, 'get_inbound_interfaces', return_value=['eth0']), \
             patch.object(nft, 'async_run_nft_cmd', AsyncMock(side_effect=fake_nft)):
            await nft.generate_nft_rules(interface='wwan0')
        return captured.get('cmd', '')

    async def test_full_ruleset_does_not_blanket_drop_ipv6(self):
        cmd = await self._run()
        self.assertNotIn('meta nfproto ipv6 drop', cmd)
        self.assertIn('ip6 daddr ff00::/8 drop', cmd)
        # The forwarded prerouting queue (family-agnostic inet+fib) is present,
        # so forwarded IPv6 unicast resolving to wwan0 is queued too.
        self.assertIn('fib daddr oifname "wwan0"', cmd)
        # LAN-side forwarded traffic (eth0) is the trigger source.
        self.assertIn('iifname { eth0 }', cmd)
