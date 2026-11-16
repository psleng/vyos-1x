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
        # Probe route lives in the dedicated probe table (scoped), reusing the
        # next-hop's onlink from config.
        vf = self.vf
        nhc = _nexthop(vf, next_hop='192.168.200.1', dhcp_interface=None,
                       onlink='onlink')
        args = vf.get_probe_route_args('1.1.1.1', nhc)
        self.assertEqual(
            args.split(),
            ['1.1.1.1', 'via', '192.168.200.1', 'dev', 'eth1', 'onlink',
             'table', str(vf.probe_table), 'proto', 'failover'])

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
             'eth1', 'table', str(vf.probe_table), 'proto', 'failover'])

    def test_probe_rule_scopes_to_local_origin_and_target(self):
        # The policy rule must match only locally-generated packets (iif lo)
        # to the specific target, so forwarded/downstream traffic never uses
        # the probe route.
        vf = self.vf
        rule = vf.get_probe_rule_args('1.1.1.1')
        self.assertEqual(
            rule.split(),
            ['iif', 'lo', 'to', '1.1.1.1', 'lookup', str(vf.probe_table),
             'priority', str(vf.probe_rule_prio)])

    def test_add_probe_routes_installs_scoped_route_and_rule(self):
        vf = self.vf
        nhc = _nexthop(vf, next_hop='192.168.200.1', dhcp_interface=None)
        with patch.object(vf, 'rc_cmd', return_value=(0, '')) as rc:
            added = vf.add_probe_routes(nhc)
        self.assertEqual(len(added), 1)
        cmds = [c.args[0] for c in rc.call_args_list]
        # a route into the probe table, then the scoping rule
        self.assertTrue(any(
            c.startswith('ip route replace 1.1.1.1 via 192.168.200.1 dev eth1')
            and f'table {vf.probe_table}' in c for c in cmds))
        self.assertTrue(any(
            c.startswith('ip rule add iif lo to 1.1.1.1') for c in cmds))

    def test_ipv6_probe_uses_v6_ip(self):
        vf = self.vf
        nhc = _nexthop(
            vf, route='::/0', next_hop='fe80::280:baff:fe00:218',
            dhcp_interface=None, onlink='',
            targets=(vf.TargetNamedTuple(target='2606:4700:4700::1111',
                                         vrf=None, interface=None),))
        with patch.object(vf, 'rc_cmd', return_value=(0, '')) as rc:
            vf.add_probe_routes(nhc)
        cmds = [c.args[0] for c in rc.call_args_list]
        self.assertTrue(all('ip -6 ' in c for c in cmds),
                        f'expected v6 ip commands, got {cmds}')

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

    def test_del_probe_routes_removes_rule_then_route(self):
        vf = self.vf
        token = [('1.1.1.1 via 192.168.200.1 dev eth1 table 250 proto failover',
                  'iif lo to 1.1.1.1 lookup 250 priority 12500', '')]
        with patch.object(vf, 'rc_cmd', return_value=(0, '')) as rc:
            vf.del_probe_routes(token)
        cmds = [c.args[0] for c in rc.call_args_list]
        self.assertEqual(cmds, [
            'ip rule del iif lo to 1.1.1.1 lookup 250 priority 12500',
            'ip route del 1.1.1.1 via 192.168.200.1 dev eth1 table 250 proto failover',
        ])


class TestConntrackFlush(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vf = _load_failover()

    def test_flush_uses_reply_dst_and_family(self):
        vf = self.vf
        with patch.object(vf, 'rc_cmd', return_value=(0, '')) as rc, \
             patch.object(vf, 'journal'):
            vf.flush_conntrack_for_addrs(['10.12.91.249',
                                          '2605:b100::1'], vacated_iface='eth1')
        cmds = [c.args[0] for c in rc.call_args_list]
        self.assertIn('/usr/sbin/conntrack -D -f ipv4 -q 10.12.91.249', cmds)
        self.assertIn('/usr/sbin/conntrack -D -f ipv6 -q 2605:b100::1', cmds)

    def test_get_iface_addresses_parses_global_only(self):
        vf = self.vf
        sample = (
            '[{"addr_info":['
            '{"local":"10.12.91.249","scope":"global"},'
            '{"local":"fe80::1","scope":"link"},'
            '{"local":"2605:b100::1","scope":"global"}]}]'
        )
        with patch.object(vf, 'rc_cmd', return_value=(0, sample)):
            addrs = vf.get_iface_addresses('eth1')
        self.assertEqual(addrs, ['10.12.91.249', '2605:b100::1'])

    def test_flush_is_per_family_selectable(self):
        # The main loop filters the vacated interface's addresses to the failed
        # route's family before flushing; assert the flush helper honours each
        # family independently so a v4 failover never flushes v6 (and vice
        # versa) when both IPv4 and IPv6 NAT are in use.
        vf = self.vf
        with patch.object(vf, 'rc_cmd', return_value=(0, '')) as rc, \
             patch.object(vf, 'journal'):
            vf.flush_conntrack_for_addrs(['10.12.91.249'], vacated_iface='eth1')
        cmds = [c.args[0] for c in rc.call_args_list]
        self.assertEqual(cmds, ['/usr/sbin/conntrack -D -f ipv4 -q 10.12.91.249'])
        self.assertFalse(any('ipv6' in c for c in cmds))

    def test_route_family_filter_selects_matching_addrs(self):
        # Mirror the main-loop filter: given a route destination, only the
        # same-family addresses of the vacated interface are flushed.
        addrs = {'10.12.91.249', '2605:b100::1'}
        v4_only = {a for a in addrs if (':' in a) == (':' in '0.0.0.0/0')}
        v6_only = {a for a in addrs if (':' in a) == (':' in '::/0')}
        self.assertEqual(v4_only, {'10.12.91.249'})
        self.assertEqual(v6_only, {'2605:b100::1'})
