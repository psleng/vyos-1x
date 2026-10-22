#!/usr/bin/env python3
#
# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Ethtool.get_auto_negotiation() must not trust the JSON field.

ethtool <= 6.10 prints the JSON "auto-negotiation" value inverted
(netlink/settings.c: print_bool(..., autoneg == AUTONEG_DISABLE)). The outputs
below were captured from an AM64x (am65-cpsw-nuss) port forced to 100/Full.
"""

import json
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

from vyos import ethtool as ethtool_mod
from vyos.ethtool import Ethtool

# JSON from ethtool 6.10 on a port with auto-negotiation OFF (bug: says true)
_JSON_FORCED = json.dumps([{
    'ifname': 'eth1',
    'supported-link-modes': ['10baseT/Half', '10baseT/Full', '100baseT/Half',
                             '100baseT/Full', '1000baseT/Full'],
    'supports-auto-negotiation': True,
    'advertised-auto-negotiation': False,
    'speed': 100,
    'duplex': 'Full',
    'auto-negotiation': True,
}])

_TEXT_FORCED = """Settings for eth1:
\tSupported link modes:   10baseT/Half 10baseT/Full
\t                        100baseT/Half 100baseT/Full
\t                        1000baseT/Full
\tSupports auto-negotiation: Yes
\tAdvertised auto-negotiation: No
\tSpeed: 100Mb/s
\tDuplex: Full
\tAuto-negotiation: off
\tLink detected: no
"""

_TEXT_AUTO = _TEXT_FORCED.replace('Auto-negotiation: off', 'Auto-negotiation: on')
_JSON_AUTO = _JSON_FORCED.replace('"auto-negotiation": true', '"auto-negotiation": false')


def _make(json_out, text_out):
    def fake_popen(cmd, *args, **kwargs):
        if cmd.startswith('ethtool --json --show-features'):
            return '[{}]', ''
        if cmd.startswith('ethtool --json --show-'):
            return '', 'not supported'
        if cmd.startswith('ethtool --json'):
            return json_out, ''
        if cmd.startswith('ethtool --driver'):
            return 'driver: am65-cpsw-nuss\n', ''
        if cmd.startswith('ethtool --show-channels'):
            return '', 'not supported'
        if cmd == 'ethtool eth1':
            return text_out, ''
        raise AssertionError(f'unexpected command: {cmd}')

    with patch.object(ethtool_mod, 'interface_exists', return_value=True), \
         patch.object(ethtool_mod, 'popen', side_effect=fake_popen):
        return Ethtool('eth1')


class TestEthtoolAutoNegotiation(TestCase):
    def test_forced_port_reads_off_despite_inverted_json(self):
        self.assertFalse(_make(_JSON_FORCED, _TEXT_FORCED).get_auto_negotiation())

    def test_autoneg_port_reads_on_despite_inverted_json(self):
        self.assertTrue(_make(_JSON_AUTO, _TEXT_AUTO).get_auto_negotiation())

    def test_falls_back_to_json_without_text_line(self):
        eth = _make(_JSON_FORCED, 'Settings for eth1:\n\tLink detected: no\n')
        self.assertTrue(eth.get_auto_negotiation())

    def test_unsupported_autoneg_is_false(self):
        no_an = _JSON_AUTO.replace('"supports-auto-negotiation": true',
                                   '"supports-auto-negotiation": false')
        self.assertFalse(_make(no_an, _TEXT_AUTO).get_auto_negotiation())

    def test_other_autoneg_lines_not_matched(self):
        # Only the exact "Auto-negotiation:" line counts, not "Supports ..." or
        # "Advertised auto-negotiation: No" / link-partner lines.
        text = _TEXT_FORCED + '\tLink partner advertised auto-negotiation: Yes\n'
        self.assertFalse(_make(_JSON_FORCED, text).get_auto_negotiation())
