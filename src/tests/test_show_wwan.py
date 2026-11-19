#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

import importlib.util
import pathlib
import sys
import types
from unittest import TestCase

try:
    __import__('cracklib')
except ModuleNotFoundError:
    _cracklib = types.ModuleType('cracklib')
    _cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = _cracklib

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'op_mode' / 'show_wwan.py'
_spec = importlib.util.spec_from_file_location('show_wwan', _SCRIPT)
show_wwan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(show_wwan)


def _connection_lines(status):
    text = show_wwan._format_status(status, 'wwan0')
    return [line.strip() for line in text.split('\n')]


class TestStatusRegistration(TestCase):
    BASE = {
        'fsm_state': 'CONNECTED',
        'operator_name': 'Telstra',
        'sim_operator': 'Telstra',
        'sim_mcc_mnc': '50501',
    }

    def test_roaming_shows_visited_network_and_home_network(self):
        lines = _connection_lines({**self.BASE, 'operator_code': '302220',
                                   'registration_state': 5,
                                   'serving_network_name': 'TELUS'})
        self.assertIn('Operator:                TELUS', lines)
        self.assertIn('Registration:            roaming', lines)
        self.assertIn('Home network:            Telstra (50501)', lines)

    def test_home_registration_has_no_home_network_line(self):
        lines = _connection_lines({**self.BASE, 'operator_code': '50501',
                                   'registration_state': 1})
        self.assertIn('Operator:                Telstra', lines)
        self.assertIn('Registration:            home', lines)
        self.assertFalse(any(line.startswith('Home network:') for line in lines))
