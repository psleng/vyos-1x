#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Tests for protocols-failover resilience against the DHCP lease method.

Covers the three failover/DHCP fixes:

* atomic dhclient lease write -- the reader contract get_dhcp_router() relies
  on: a complete lease yields the gateway, while a lease missing new_routers
  (the partial-write hazard the atomic write removes) yields None;
* process_dhcp_interface() debounce -- a transient "no gateway" read must not
  immediately withdraw a working dhcp-interface route;
* the health-probe gateway route -- add_probe_routes() lets the check reach an
  off-link target via the next-hop gateway, prefix-agnostically, so a withdrawn
  route can recover on its own.
"""

import importlib.util
import os
import sys
import tempfile
import types
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


def _nexthop(vf, **over):
    """Build a NextHopNamedTuple with sensible defaults for a dhcp-interface
    next-hop, overriding any field by keyword."""
    defaults = dict(
        route='0.0.0.0/0',
        dhcp_interface='eth1',
        next_hop=None,
        vrf=None,
        vrf_opt='',
        conf_iface='eth1',
        conf_metric=1,
        port=None,
        port_opt='',
        policy='any-available',
        proto='icmp',
        targets=(vf.TargetNamedTuple(target='1.1.1.1', vrf=None,
                                     interface=None),),
        pretty_targets='1.1.1.1',
        timeout='1',
        onlink='onlink',
        failure_count=3,
        success_count=2,
    )
    defaults.update(over)
    return vf.NextHopNamedTuple(**defaults)


class TestLeaseReaderContract(TestCase):
    """get_dhcp_router() is what vyos-failover polls; the atomic lease write
    exists so this reader never samples a partially-written file."""

    def _router_for(self, contents):
        from vyos.template import get_dhcp_router
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'dhclient_eth1.lease'), 'w') as f:
                f.write(contents)
            with patch.dict('vyos.template.directories',
                            {'isc_dhclient_dir': d}):
                return get_dhcp_router('eth1')

    def test_complete_lease_yields_gateway(self):
        contents = (
            "reason='RENEW'\n"
            "interface='eth1'\n"
            "new_ip_address='10.12.91.249'\n"
            "new_routers='192.168.200.1'\n"
        )
        self.assertEqual(self._router_for(contents), '192.168.200.1')

    def test_partial_lease_without_routers_yields_none(self):
        # The exact window a truncate+append write exposes and the atomic
        # rename() removes: file present, new_routers not yet written.
        contents = "reason='RENEW'\ninterface='eth1'\n"
        self.assertIsNone(self._router_for(contents))

    def test_missing_lease_yields_none(self):
        from vyos.template import get_dhcp_router
        with tempfile.TemporaryDirectory() as d:
            with patch.dict('vyos.template.directories',
                            {'isc_dhclient_dir': d}):
                self.assertIsNone(get_dhcp_router('eth1'))


class TestProcessDhcpInterfaceDebounce(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vf = _load_failover()

    def _run(self, gateways):
        """Feed a sequence of get_dhcp_router() return values and record, per
        call, the resolved next_hop and whether the route was withdrawn."""
        vf = self.vf
        nhc = _nexthop(vf)
        store = {}
        miss = {}
        results = []
        for gw in gateways:
            with patch.object(vf, 'get_dhcp_router', return_value=gw), \
                 patch.object(vf, 'is_route_exists', return_value=True), \
                 patch.object(vf, 'delete_route') as del_route:
                out = vf.process_dhcp_interface(nhc, store, miss)
            results.append((out.next_hop if out else None,
                            del_route.called))
        return results

    def test_single_transient_miss_keeps_route(self):
        # up, then one miss (limit is 3) -> route kept, no withdrawal.
        res = self._run(['192.168.200.1', None])
        self.assertEqual(res[0], ('192.168.200.1', False))
        self.assertEqual(res[1], ('192.168.200.1', False))

    def test_withdraws_only_after_limit_consecutive_misses(self):
        gws = ['192.168.200.1'] + [None] * self.vf.dhcp_gw_absent_limit
        res = self._run(gws)
        # misses 1..limit-1 keep the route...
        for i in range(1, self.vf.dhcp_gw_absent_limit):
            self.assertEqual(res[i], ('192.168.200.1', False),
                             f'miss {i} should keep the route')
        # ...the limit-th miss withdraws it.
        self.assertEqual(res[self.vf.dhcp_gw_absent_limit], (None, True))

    def test_recovery_before_limit_resets_counter(self):
        # up, miss, miss, up, miss, miss -> never reaches 3 in a row, no delete.
        res = self._run(['192.168.200.1', None, None,
                         '192.168.200.1', None, None])
        self.assertFalse(any(withdrawn for _, withdrawn in res))
        self.assertTrue(all(nh == '192.168.200.1' for nh, _ in res))


class TestProbeRoutes(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vf = _load_failover()

    def test_probe_args_dhcp_onlink_ipv4(self):
        vf = self.vf
        nhc = _nexthop(vf, next_hop='192.168.200.1', dhcp_interface=None,
                       onlink='onlink')
        args = vf.get_probe_route_args('1.1.1.1', nhc)
        self.assertEqual(
            args.split(),
            ['1.1.1.1', 'via', '192.168.200.1', 'dev', 'eth1', 'onlink',
             'proto', 'failover'])

    def test_probe_args_static_ipv6_no_onlink(self):
        # On-subnet / link-local gateway: no onlink inherited from config, so
        # the probe route carries none either (prefix-agnostic).
        vf = self.vf
        nhc = _nexthop(vf, route='::/0', next_hop='fe80::280:baff:fe00:218',
                       dhcp_interface=None, onlink='', proto='icmp')
        args = vf.get_probe_route_args('2606:4700:4700::1111', nhc)
        self.assertEqual(
            args.split(),
            ['2606:4700:4700::1111', 'via', 'fe80::280:baff:fe00:218', 'dev',
             'eth1', 'proto', 'failover'])

    def test_add_probe_routes_installs_per_target(self):
        vf = self.vf
        nhc = _nexthop(vf, next_hop='192.168.200.1', dhcp_interface=None)
        with patch.object(vf, 'rc_cmd', return_value=(0, '')) as rc:
            added = vf.add_probe_routes(nhc)
        self.assertEqual(len(added), 1)
        self.assertTrue(rc.call_args.args[0].startswith(
            'ip route add 1.1.1.1 via 192.168.200.1 dev eth1'))

    def test_via_less_nexthop_is_noop(self):
        # A point-to-point next-hop (no gateway) needs no probe route.
        vf = self.vf
        nhc = _nexthop(vf, next_hop=None, dhcp_interface=None,
                       conf_iface='wwan0', onlink='',
                       targets=(vf.TargetNamedTuple(target='8.8.8.8', vrf=None,
                                                    interface=None),))
        with patch.object(vf, 'rc_cmd') as rc:
            self.assertEqual(vf.add_probe_routes(nhc), [])
        rc.assert_not_called()

    def test_arp_check_is_noop(self):
        vf = self.vf
        nhc = _nexthop(vf, next_hop='192.168.200.1', dhcp_interface=None,
                       proto='arp')
        with patch.object(vf, 'rc_cmd') as rc:
            self.assertEqual(vf.add_probe_routes(nhc), [])
        rc.assert_not_called()

    def test_target_with_interface_override_skipped(self):
        vf = self.vf
        nhc = _nexthop(
            vf, next_hop='192.168.200.1', dhcp_interface=None,
            targets=(vf.TargetNamedTuple(target='1.1.1.1', vrf=None,
                                         interface='eth2'),))
        with patch.object(vf, 'rc_cmd', return_value=(0, '')) as rc:
            self.assertEqual(vf.add_probe_routes(nhc), [])
        rc.assert_not_called()

    def test_del_probe_routes_removes_each(self):
        vf = self.vf
        with patch.object(vf, 'rc_cmd', return_value=(0, '')) as rc:
            vf.del_probe_routes(['1.1.1.1 via 192.168.200.1 dev eth1 proto failover'])
        rc.assert_called_once_with(
            'ip route del 1.1.1.1 via 192.168.200.1 dev eth1 proto failover')
