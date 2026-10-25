#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Tests for the WAN load-balancer health-ping helpers.

Covers the configurable per-check probe count that lets a single dropped packet
on a lossy link NOT fail the check (which, with failure-count 1, would flap the
interface).
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

from vyos import wanloadbalance as wlb


class TestHealthPing(TestCase):
    def test_ping_builds_count_and_passes_if_any(self):
        with patch.object(wlb, 'run', return_value=0) as r:
            self.assertTrue(
                wlb.health_ping_host('8.8.8.8', 'eth0', count=3, wait_time=5))
        cmd = r.call_args[0][0]
        self.assertIn('-c 3', cmd)
        self.assertIn('-I eth0', cmd)
        self.assertIn('8.8.8.8', cmd)

    def test_ping_fails_only_when_all_lost(self):
        with patch.object(wlb, 'run', return_value=1):
            self.assertFalse(
                wlb.health_ping_host('8.8.8.8', 'eth0', count=3, wait_time=5))

    def test_ping_default_count_is_one(self):
        with patch.object(wlb, 'run', return_value=0) as r:
            wlb.health_ping_host('8.8.8.8', 'eth0', wait_time=5)
        self.assertIn('-c 1', r.call_args[0][0])

    def test_ttl_builds_count(self):
        # health_ping_host_ttl returns True when a TTL-exceeded reply is seen
        # (ping rc != 0).
        with patch.object(wlb, 'run', return_value=1) as r:
            self.assertTrue(
                wlb.health_ping_host_ttl('8.8.8.8', 'eth0', count=2, ttl_limit=1))
        self.assertIn('-c 2', r.call_args[0][0])
