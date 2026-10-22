#!/usr/bin/env python3
#
# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Speed/duplex must be applied with the interface UP.

phylink drivers (TI am65-cpsw on AM64x) attach the PHY only in ndo_open, so an
ethtool speed/duplex change made while the interface is down is dropped.
"""

import sys
import types
from unittest import TestCase
from unittest.mock import Mock
from unittest.mock import patch

try:
    __import__('cracklib')
except ModuleNotFoundError:
    _cracklib = types.ModuleType('cracklib')
    _cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = _cracklib

try:
    __import__('pyroute2')
except ModuleNotFoundError:
    # Only the netlink coalesce helper needs pyroute2; it is not involved here.
    _coalesce = types.ModuleType('vyos.netlink.coalesce')
    _coalesce.CoalesceError = type('CoalesceError', (Exception,), {})
    _coalesce.GeneralNetlinkError = type('GeneralNetlinkError', (Exception,), {})
    _coalesce.get_coalesce = lambda ifname: {}
    sys.modules['vyos.netlink.coalesce'] = _coalesce

try:
    __import__('hurry.filesize')
except ModuleNotFoundError:
    # Imported by vyos.ifconfig (WireGuard) at package load; unused here.
    _hurry = types.ModuleType('hurry')
    _filesize = types.ModuleType('hurry.filesize')
    _filesize.size = lambda value, **kwargs: str(value)
    _filesize.alternative = []
    _hurry.filesize = _filesize
    sys.modules['hurry'] = _hurry
    sys.modules['hurry.filesize'] = _filesize

from vyos.ifconfig import ethernet as ethernet_mod
from vyos.ifconfig.ethernet import EthernetIf
from vyos.ifconfig.interface import Interface

# Everything EthernetIf.update() touches besides speed/duplex.
_OTHER_SETTERS = ('set_flow_control', 'set_gro', 'set_gso', 'set_hw_tc_offload',
                  'set_lro', 'set_rps', 'set_rfs', 'set_sg', 'set_tso',
                  'set_ring_buffer', 'set_interrupt_coalescing',
                  'set_switchdev', 'set_eapol')


def _make_if():
    eth = EthernetIf.__new__(EthernetIf)
    eth.ifname = 'eth0'
    eth.config = {'ifname': 'eth0'}
    eth.ethtool = Mock(name='stale-ethtool')
    return eth


def _config(**extra):
    cfg = {'ifname': 'eth0', 'speed': '100', 'duplex': 'full'}
    cfg.update(extra)
    return cfg


class TestUpdateSpeedDuplex(TestCase):
    def setUp(self):
        self.eth = _make_if()
        self.eth.set_speed_duplex = Mock()
        p = patch.object(ethernet_mod, 'Ethtool')
        self.Ethtool = p.start()
        self.addCleanup(p.stop)

    def test_reapplied_after_coming_up_from_down(self):
        # boot / re-enable: nodes unchanged, but the PHY was just attached
        self.eth._update_speed_duplex(_config(), was_down=True)
        self.Ethtool.assert_called_once_with('eth0')
        self.assertIs(self.eth.ethtool, self.Ethtool.return_value)
        self.eth.set_speed_duplex.assert_called_once_with('100', 'full', quiet=True)

    def test_explicit_change_on_up_interface(self):
        self.eth._update_speed_duplex(_config(speed_duplex_changed={}),
                                      was_down=False)
        self.eth.set_speed_duplex.assert_called_once_with('100', 'full', quiet=False)

    def test_unchanged_and_already_up_does_nothing(self):
        self.eth._update_speed_duplex(_config(), was_down=False)
        self.eth.set_speed_duplex.assert_not_called()
        self.Ethtool.assert_not_called()

    def test_disabled_interface_deferred(self):
        self.eth._update_speed_duplex(
            _config(speed_duplex_changed={}, disable={}), was_down=True)
        self.eth.set_speed_duplex.assert_not_called()

    def test_missing_keys_ignored(self):
        self.eth._update_speed_duplex({'ifname': 'eth0', 'speed_duplex_changed': {}},
                                      was_down=True)
        self.eth.set_speed_duplex.assert_not_called()


class TestUpdateOrdering(TestCase):
    """speed/duplex is applied only after the base class brought the link up."""

    def _run_update(self, admin_state, config):
        eth = _make_if()
        calls = []
        for name in _OTHER_SETTERS:
            setattr(eth, name, Mock())
        eth.get_admin_state = Mock(return_value=admin_state)
        eth.set_speed_duplex = Mock(
            side_effect=lambda *a, **k: calls.append(('speed', a, k)))
        with patch.object(Interface, 'update',
                          lambda self, cfg: calls.append(('base_update',))), \
             patch.object(ethernet_mod, 'Ethtool'):
            eth.update(config)
        return calls

    def test_speed_after_base_update_on_boot(self):
        calls = self._run_update('down', _config())
        self.assertEqual(calls[0], ('base_update',))
        self.assertEqual(calls[1], ('speed', ('100', 'full'), {'quiet': True}))

    def test_speed_after_base_update_on_change(self):
        calls = self._run_update('up', _config(speed_duplex_changed={}))
        self.assertEqual([c[0] for c in calls], ['base_update', 'speed'])
        self.assertEqual(calls[1][2], {'quiet': False})

    def test_no_speed_call_when_up_and_unchanged(self):
        calls = self._run_update('up', _config())
        self.assertEqual(calls, [('base_update',)])


class TestSetSpeedDuplexQuiet(TestCase):
    def setUp(self):
        self.eth = _make_if()
        self.eth._cmd = Mock()

    def test_quiet_suppresses_unsupported_warning(self):
        self.eth.ethtool.check_speed_duplex.return_value = True
        self.eth.ethtool.check_auto_negotiation_supported.return_value = False
        with patch.object(ethernet_mod, 'Warning') as warn:
            self.eth.set_speed_duplex('auto', 'auto', quiet=True)
            warn.assert_not_called()
            self.eth.set_speed_duplex('auto', 'auto')
            warn.assert_called_once()
        self.eth._cmd.assert_not_called()

    def test_forced_applied_when_autoneg_on(self):
        self.eth.ethtool.check_speed_duplex.return_value = True
        self.eth.ethtool.check_auto_negotiation_supported.return_value = True
        self.eth.ethtool.get_auto_negotiation.return_value = True
        self.eth.set_speed_duplex('100', 'full')
        self.eth._cmd.assert_called_once_with(
            'ethtool --change eth0 speed 100 duplex full autoneg off')

    def test_auto_restarts_negotiation_from_forced(self):
        self.eth.ethtool.check_speed_duplex.return_value = True
        self.eth.ethtool.check_auto_negotiation_supported.return_value = True
        self.eth.ethtool.get_auto_negotiation.return_value = False
        self.eth.set_speed_duplex('auto', 'auto')
        self.assertEqual(
            [c.args[0] for c in self.eth._cmd.call_args_list],
            ['ethtool --change eth0 autoneg on', 'ethtool --negotiate eth0'])
