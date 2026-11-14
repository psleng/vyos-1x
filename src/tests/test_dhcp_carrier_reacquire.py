#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Tests for vyos-dhcp-carrier-reacquire decision + marker logic.

The netlink monitor loop needs root and a live socket, so it is not exercised
here; the re-acquire decision is factored into the pure should_reacquire()
helper, and the opt-in is a marker file -- both unit-testable.
"""

import importlib.util
import os
import tempfile
from unittest import TestCase
from unittest.mock import patch


def _load():
    path = os.path.join(os.path.dirname(__file__), '..', 'helpers',
                        'vyos-dhcp-carrier-reacquire.py')
    spec = importlib.util.spec_from_file_location('vyos_dhcp_reacquire', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestShouldReacquire(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def _call(self, prev, lower_up, admin_up, opted, now=1000.0, last=0.0):
        return self.m.should_reacquire(prev, lower_up, admin_up, opted,
                                       now, last, min_interval=15)

    def test_carrier_up_transition_fires(self):
        # NO-CARRIER -> carrier, admin up, opted in, not debounced.
        self.assertTrue(self._call(prev=False, lower_up=True, admin_up=True,
                                   opted=True))

    def test_no_transition_when_already_up(self):
        self.assertFalse(self._call(prev=True, lower_up=True, admin_up=True,
                                    opted=True))

    def test_carrier_down_event_ignored(self):
        self.assertFalse(self._call(prev=True, lower_up=False, admin_up=True,
                                    opted=True))

    def test_admin_down_ignored(self):
        self.assertFalse(self._call(prev=False, lower_up=True, admin_up=False,
                                    opted=True))

    def test_not_opted_in_ignored(self):
        self.assertFalse(self._call(prev=False, lower_up=True, admin_up=True,
                                    opted=False))

    def test_debounced_within_interval(self):
        # transition is valid but last re-acquire was 5s ago (< 15s).
        self.assertFalse(self._call(prev=False, lower_up=True, admin_up=True,
                                    opted=True, now=1000.0, last=990.0))

    def test_fires_again_after_interval(self):
        self.assertTrue(self._call(prev=False, lower_up=True, admin_up=True,
                                   opted=True, now=1000.0, last=980.0))


class TestMarker(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def test_opted_in_follows_marker_file(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.m, '_dhclient_dir', d):
                self.assertFalse(self.m.opted_in('eth1'))
                open(self.m.marker_path('eth1'), 'w').close()
                self.assertTrue(self.m.opted_in('eth1'))
                self.assertEqual(os.path.basename(self.m.marker_path('eth1')),
                                 'dhclient_eth1.reacquire')
