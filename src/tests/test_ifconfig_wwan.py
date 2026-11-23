#!/usr/bin/env python3
#
# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Conf-mode must not overwrite the MTU the WWAN state machine applied.

The FSM sets the carrier/per-SIM/fallback MTU on connect and on every config
change; a commit re-applying the configured fallback (default 1420) clobbered a
carrier 1500 and, with qmi_wwan raw_ip, caused rx_over_errors.
"""

import sys
import types
from unittest import TestCase
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
    _coalesce = types.ModuleType('vyos.netlink.coalesce')
    _coalesce.CoalesceError = type('CoalesceError', (Exception,), {})
    _coalesce.GeneralNetlinkError = type('GeneralNetlinkError', (Exception,), {})
    _coalesce.get_coalesce = lambda ifname: {}
    sys.modules['vyos.netlink.coalesce'] = _coalesce

try:
    __import__('hurry.filesize')
except ModuleNotFoundError:
    _hurry = types.ModuleType('hurry')
    _filesize = types.ModuleType('hurry.filesize')
    _filesize.size = lambda value, **kwargs: str(value)
    _filesize.alternative = []
    _hurry.filesize = _filesize
    sys.modules['hurry'] = _hurry
    sys.modules['hurry.filesize'] = _filesize

from vyos.ifconfig.interface import Interface
from vyos.ifconfig.wwan import WWANIf


def _make_if():
    wwan = WWANIf.__new__(WWANIf)
    wwan.ifname = 'wwan0'
    return wwan


def _generic_update(self, config):
    # Stand-in for Interface.update(): it applies the configured mtu.
    self.set_mtu(config['mtu'])


class TestWWANMtuOwnership(TestCase):
    def setUp(self):
        p = patch.object(Interface, 'set_mtu')
        self.base_set_mtu = p.start()
        self.addCleanup(p.stop)
        p = patch.object(Interface, 'update', _generic_update)
        p.start()
        self.addCleanup(p.stop)

    def test_commit_does_not_apply_configured_mtu(self):
        for defer_admin_up in (True, False):
            _make_if().update({'mtu': '1420'}, defer_admin_up=defer_admin_up)
        self.base_set_mtu.assert_not_called()

    def test_set_mtu_outside_update_still_applies(self):
        wwan = _make_if()
        wwan.update({'mtu': '1420'})
        wwan.set_mtu('1500')
        self.base_set_mtu.assert_called_once_with('1500')
