#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

import importlib.util
import os
import pathlib
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


class TestWrappedBands(TestCase):
    BANDS = ', '.join([f'umts-{n}' for n in (1, 3, 4, 6, 5, 8, 9, 2)]
                      + [f'eutran-{n}' for n in range(1, 72)])

    def _wrap(self, tty_columns):
        def fake_size(fd):
            # fd 1 is the pager pipe; only stderr/stdin reach the terminal
            if tty_columns is None or fd == 1:
                raise OSError('not a tty')
            return os.terminal_size((tty_columns, 24))

        env = {k: v for k, v in os.environ.items() if k != 'COLUMNS'}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(show_wwan.os, 'get_terminal_size', side_effect=fake_size), \
             patch.object(show_wwan.sys, 'stdout', types.SimpleNamespace(fileno=lambda: 1)), \
             patch.object(show_wwan.sys, 'stderr', types.SimpleNamespace(fileno=lambda: 2)), \
             patch.object(show_wwan.sys, 'stdin', types.SimpleNamespace(fileno=lambda: 0)):
            return show_wwan._kv_wrapped('Enabled bands:', self.BANDS)

    def test_no_tty_falls_back_to_80_columns(self):
        lines = self._wrap(None)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= 80 for line in lines), lines)

    def test_piped_stdout_uses_terminal_width_from_stderr(self):
        lines = self._wrap(100)
        self.assertTrue(all(len(line) <= 100 for line in lines), lines)
        self.assertTrue(any(len(line) > 80 for line in lines), lines)
