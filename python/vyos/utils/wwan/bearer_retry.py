#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Bearer connect-retry profiles
=============================

While a client holds a live bearer lease (demand) but the modem cannot
establish the data bearer, the FSM keeps retrying the connect on its own --
the client never loops on connect, it just keeps renewing its lease.  How
*aggressively* the FSM retries is a single named profile, applied **uniformly**
to every failure reason (no special-casing of "fatal" vs "transient"; a fatal
condition simply parks retries at the ceiling and is surfaced in status).

Each profile maps to the FSM's existing exponential-backoff fields:

* ``intervals``     -- the per-attempt delay schedule (seconds).  Attempt *n*
                       waits ``intervals[n]``; once past the end of the list,
                       every further attempt waits ``max_interval``.
* ``max_interval``  -- the ceiling (seconds) applied indefinitely thereafter.

The ``normal`` profile is intentionally identical to the FSM's historical
hard-coded schedule, so the default behaviour is unchanged when no
``connect-retry`` is configured.
"""

from __future__ import annotations

from typing import Dict
from typing import List
from typing import Tuple

DEFAULT_RETRY_PROFILE = 'normal'

# profile name -> (intervals, max_interval)
RETRY_PROFILES: Dict[str, Dict[str, object]] = {
    # Gentle on the modem/carrier and on power: longer early waits, 2 h cap.
    'conservative': {
        'intervals': [30, 120, 300, 900, 1800],
        'max_interval': 7200,
    },
    # Historical default -- MUST match the FSM's prior hard-coded schedule so
    # the out-of-the-box behaviour is unchanged.
    'normal': {
        'intervals': [30, 60, 120, 300, 600, 1800, 3600],
        'max_interval': 7200,
    },
    # Fast recovery for failover-critical links: tight early retries, 5 min cap.
    'aggressive': {
        'intervals': [5, 10, 20, 30, 60, 120],
        'max_interval': 300,
    },
}


def retry_schedule(profile: str) -> Tuple[List[int], int]:
    """Return ``(intervals, max_interval)`` for *profile*.

    Falls back to :data:`DEFAULT_RETRY_PROFILE` for an unknown name so a stale
    or mistyped value can never disable retries entirely.
    """
    spec = RETRY_PROFILES.get(profile) or RETRY_PROFILES[DEFAULT_RETRY_PROFILE]
    return list(spec['intervals']), int(spec['max_interval'])  # type: ignore[arg-type]


def backoff_delay(profile: str, attempt: int) -> int:
    """Delay (seconds) for a zero-based *attempt* number under *profile*.

    Mirrors the FSM's own selection logic: ``intervals[attempt]`` until the
    schedule is exhausted, then ``max_interval`` forever.
    """
    intervals, max_interval = retry_schedule(profile)
    if 0 <= attempt < len(intervals):
        return intervals[attempt]
    return max_interval
