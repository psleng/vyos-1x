#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Unit tests for bearer connect-retry profiles (vyos.utils.wwan.bearer_retry)."""

import sys
import types
from unittest import TestCase

try:
    __import__('cracklib')
except ModuleNotFoundError:
    _cracklib = types.ModuleType('cracklib')
    _cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = _cracklib

from vyos.utils.wwan.bearer_retry import DEFAULT_RETRY_PROFILE
from vyos.utils.wwan.bearer_retry import RETRY_PROFILES
from vyos.utils.wwan.bearer_retry import backoff_delay
from vyos.utils.wwan.bearer_retry import retry_schedule


class TestBearerRetry(TestCase):
    def test_default_profile_matches_historical_fsm_schedule(self):
        # This MUST stay in lockstep with the FSM's prior hard-coded values so
        # the default behaviour is preserved when connect-retry is unset.
        intervals, max_interval = retry_schedule(DEFAULT_RETRY_PROFILE)
        self.assertEqual(intervals, [30, 60, 120, 300, 600, 1800, 3600])
        self.assertEqual(max_interval, 7200)

    def test_all_profiles_present(self):
        self.assertEqual(set(RETRY_PROFILES),
                         {'conservative', 'normal', 'aggressive'})

    def test_unknown_profile_falls_back_to_default(self):
        self.assertEqual(retry_schedule('bogus'),
                         retry_schedule(DEFAULT_RETRY_PROFILE))

    def test_schedule_returns_copies(self):
        a, _ = retry_schedule('normal')
        a.append(999)
        b, _ = retry_schedule('normal')
        self.assertNotIn(999, b)

    def test_backoff_delay_walks_then_caps(self):
        intervals, max_interval = retry_schedule('aggressive')
        for i, expected in enumerate(intervals):
            self.assertEqual(backoff_delay('aggressive', i), expected)
        # past the end of the schedule -> ceiling forever
        self.assertEqual(backoff_delay('aggressive', len(intervals)), max_interval)
        self.assertEqual(backoff_delay('aggressive', len(intervals) + 50), max_interval)

    def test_aggressive_is_faster_than_conservative(self):
        self.assertLess(backoff_delay('aggressive', 0),
                        backoff_delay('conservative', 0))
        self.assertLess(retry_schedule('aggressive')[1],
                        retry_schedule('conservative')[1])
