#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Tests for the dial-on-demand per-dial-attempt flush timer.

A failed/too-slow dial must not wedge the nfqueue consumer: packet_consumer
waits for the bearer only until flush_timeout, then DROPS the held packets, tears
the trap down and signals shutdown so the pipeline exits and systemd re-arms it.
"""

import asyncio
import sys
import types
from unittest import IsolatedAsyncioTestCase
from unittest import TestCase
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

try:
    __import__('cracklib')
except ModuleNotFoundError:
    _cracklib = types.ModuleType('cracklib')
    _cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = _cracklib

try:
    __import__('netfilterqueue')
except ModuleNotFoundError:
    _nfq = types.ModuleType('netfilterqueue')
    _nfq.NetfilterQueue = object
    sys.modules['netfilterqueue'] = _nfq

from vyos.utils.wwan.dial_on_demand import wwan_nft_reconnect as nftr


class TestFlushTimeoutClamp(TestCase):
    def test_flush_clamped_to_connect_timeout(self):
        # A flush shorter than the connect wait must not abort a dial early.
        h = nftr.PacketHandler("wwan0", connect_timeout=30, flush_timeout=10, loop=Mock())
        self.assertEqual(h.flush_timeout, 30)

    def test_flush_respected_when_larger(self):
        h = nftr.PacketHandler("wwan0", connect_timeout=30, flush_timeout=90, loop=Mock())
        self.assertEqual(h.flush_timeout, 90)


class TestPacketConsumerFlush(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        nftr.modem_ready_event.clear()
        nftr.shutdown_event.clear()
        while not nftr.packet_queue.empty():
            nftr.packet_queue.get_nowait()

    def _handler(self, flush_timeout):
        return nftr.PacketHandler('wwan0', connect_timeout=0,
                                  flush_timeout=flush_timeout,
                                  loop=asyncio.get_event_loop())

    async def test_timeout_drops_held_packets(self):
        handler = self._handler(flush_timeout=0)  # clamp(0, connect 0) -> fires now
        pkt1, pkt2 = Mock(), Mock()
        nftr.packet_queue.put_nowait(pkt1)
        nftr.packet_queue.put_nowait(pkt2)
        with patch.object(nftr, 'teardown_nftables', AsyncMock(return_value=True)):
            await handler.packet_consumer(queue_num=1)
        await asyncio.sleep(0)  # let the scheduled shutdown_event.set run
        pkt1.drop.assert_called_once()
        pkt1.accept.assert_not_called()
        pkt2.drop.assert_called_once()
        # Pipeline is signalled to exit so systemd re-arms it.
        self.assertTrue(nftr.shutdown_event.is_set())

    async def test_ready_accepts_held_packets(self):
        handler = self._handler(flush_timeout=30)
        pkt = Mock()
        nftr.packet_queue.put_nowait(pkt)
        nftr.modem_ready_event.set()  # bearer came up before the flush deadline
        with patch.object(nftr, 'teardown_nftables', AsyncMock(return_value=True)):
            await handler.packet_consumer(queue_num=1)
        await asyncio.sleep(0)  # let the scheduled shutdown_event.set run
        pkt.accept.assert_called_once()
        pkt.drop.assert_not_called()
        self.assertTrue(nftr.shutdown_event.is_set())
