#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

"""Tests for lease-driven bearer reconciliation.

Covers the FSM reconciler (ModemStateMachine._reconcile_bearer_demand), the
lease-aware D-Bus disconnect_bearer(), and WLB's failover-lease selection.
The core invariant under test: with no leases, existing behaviour is unchanged.
"""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace
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

from vyos.utils.wwan import interfaces_wwan_state_machine as state_machine
from vyos.utils.wwan.bearer_lease import BearerLeaseRegistry
from vyos.utils.wwan.interfaces_wwan_config import InterfaceConfig
from vyos.utils.wwan.interfaces_wwan_state_machine import ModemEvent
from vyos.utils.wwan.interfaces_wwan_state_machine import ModemState
from vyos.utils.wwan.interfaces_wwan_state_machine import ModemStateMachine

IDLE = ModemState.REGISTERED_IDLE.value
CONNECTED = ModemState.CONNECTED.value


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _make_fsm(clock, mode='dial-on-demand', state=CONNECTED):
    """A ModemStateMachine with only the attributes the lease path touches."""
    fsm = ModemStateMachine.__new__(ModemStateMachine)
    fsm.interface_number = 0
    fsm.connection_mode = mode
    fsm.machine = SimpleNamespace(current_state=state)
    fsm.bearer_leases = BearerLeaseRegistry(clock=clock, max_ttl=None)
    fsm.bearer_requested = False
    fsm.user_disconnected = False
    fsm.connect_requested = False
    fsm._shutting_down = False
    fsm._admin_disabled = False
    fsm._airplane_mode_active = False
    fsm._airplane_mode_requested = False
    fsm._data_limit_hold = False
    fsm._lease_owns_bearer = False
    fsm._lease_idle_deadline = None
    fsm._lease_idle_grace = 5.0
    fsm._lease_sweep_interval = 1.0
    fsm._lease_sweep_task = None
    fsm._safe_create_task = Mock()
    fsm.transition = Mock()
    return fsm


class TestReconcileBearerDemand(TestCase):
    def setUp(self):
        self.clock = _Clock()
        patcher = patch.object(state_machine.time, 'monotonic', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tick(self, fsm, seconds=1.0):
        self.clock.advance(seconds)
        fsm._reconcile_bearer_demand()

    # ---- invariant: no leases => no behaviour change -----------------------

    def test_no_leases_never_idles_dial_on_demand_boot_bearer(self):
        # dial-on-demand connects at boot with bearer_requested False; the
        # reconciler must leave that bearer alone.
        fsm = _make_fsm(self.clock, state=CONNECTED)
        for _ in range(30):
            self._tick(fsm)
        fsm.transition.assert_not_called()
        self.assertFalse(fsm._lease_owns_bearer)

    def test_no_leases_never_connects_idle_bearer(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm.bearer_requested = True  # operator intent is the existing path's job
        for _ in range(5):
            self._tick(fsm)
        fsm.transition.assert_not_called()
        self.assertFalse(fsm.connect_requested)

    def test_always_on_ignores_leases(self):
        fsm = _make_fsm(self.clock, mode='always-on', state=IDLE)
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.transition.assert_not_called()

    # ---- lease brings the bearer up ----------------------------------------

    def test_lease_connects_from_registered_idle(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm.user_disconnected = True
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.transition.assert_called_once_with(ModemEvent.CONNECT)
        self.assertTrue(fsm._lease_owns_bearer)
        self.assertFalse(fsm.user_disconnected)

    def test_lease_queues_connect_from_failed(self):
        fsm = _make_fsm(self.clock, state=ModemState.FAILED.value)
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.transition.assert_not_called()
        self.assertTrue(fsm.connect_requested)

    def test_transitional_state_defers(self):
        fsm = _make_fsm(self.clock, state=ModemState.CONNECTING.value)
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.transition.assert_not_called()
        self.assertFalse(fsm.connect_requested)

    # ---- lease lapse idles only a bearer the lease owns --------------------

    def test_release_idles_owned_bearer_after_grace(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.machine.current_state = CONNECTED
        fsm.transition.reset_mock()

        fsm.bearer_leases.release(0, 'wlb-failover')
        self._tick(fsm, 0)         # arms the grace deadline
        self._tick(fsm, 4)         # still inside the 5s grace
        fsm.transition.assert_not_called()
        self._tick(fsm, 2)         # grace elapsed
        fsm.transition.assert_called_once_with(ModemEvent.ENTER_IDLE)
        self.assertFalse(fsm._lease_owns_bearer)

    def test_expired_lease_idles_owned_bearer(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.machine.current_state = CONNECTED
        fsm.transition.reset_mock()
        self._tick(fsm, 16)        # lease lapsed (crashed client); arm grace
        self._tick(fsm, 6)
        fsm.transition.assert_called_once_with(ModemEvent.ENTER_IDLE)

    def test_lease_adopts_already_connected_bearer(self):
        fsm = _make_fsm(self.clock, state=CONNECTED)
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.transition.assert_not_called()
        self.assertTrue(fsm._lease_owns_bearer)
        fsm.bearer_leases.release(0, 'wlb-failover')
        self._tick(fsm, 0)
        self._tick(fsm, 6)
        fsm.transition.assert_called_once_with(ModemEvent.ENTER_IDLE)

    def test_new_lease_during_grace_cancels_teardown(self):
        fsm = _make_fsm(self.clock, state=CONNECTED)
        fsm._lease_owns_bearer = True
        self._tick(fsm, 0)         # arm grace
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        self._tick(fsm, 10)
        fsm.transition.assert_not_called()
        self.assertIsNone(fsm._lease_idle_deadline)

    def test_operator_request_takes_over_on_release(self):
        fsm = _make_fsm(self.clock, state=CONNECTED)
        fsm._lease_owns_bearer = True
        fsm.bearer_requested = True
        self._tick(fsm, 0)
        self._tick(fsm, 10)
        fsm.transition.assert_not_called()
        self.assertFalse(fsm._lease_owns_bearer)

    def test_ownership_ends_when_bearer_already_down(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm._lease_owns_bearer = True
        self._tick(fsm)
        fsm.transition.assert_not_called()
        self.assertFalse(fsm._lease_owns_bearer)

    # ---- vetoes ------------------------------------------------------------

    def test_data_limit_hold_vetoes_lease(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm._data_limit_hold = True
        fsm.user_disconnected = True
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.transition.assert_not_called()
        self.assertFalse(fsm.connect_requested)
        self.assertTrue(fsm.user_disconnected)

    def test_airplane_vetoes_lease(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm._airplane_mode_active = True
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm.transition.assert_not_called()

    def test_hold_lift_with_live_lease_connects(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm._data_limit_hold = True
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        fsm._reconcile_bearer_demand()
        fsm._data_limit_hold = False
        self._tick(fsm)
        fsm.transition.assert_called_once_with(ModemEvent.CONNECT)

    # ---- sweeper lifecycle -------------------------------------------------

    def test_acquire_starts_sweeper(self):
        fsm = _make_fsm(self.clock, state=IDLE)
        fsm.bearer_lease_acquire('wlb-failover', ttl=15, reason='t')
        fsm._safe_create_task.assert_called_once()
        fsm._safe_create_task.call_args.args[0].close()  # silence never-awaited


class TestLeaseSweepLoop(IsolatedAsyncioTestCase):
    async def test_loop_exits_with_no_lease_and_no_ownership(self):
        clock = _Clock()
        fsm = _make_fsm(clock, state=CONNECTED)
        await fsm._lease_sweep_loop()  # must return, not spin forever
        fsm.transition.assert_not_called()


class TestLeaseAwareDisconnectBearer(IsolatedAsyncioTestCase):
    def _iface(self, fsm):
        return SimpleNamespace(fsm=fsm, interface_number=0,
                               _save_runtime_state=Mock())

    async def test_no_lease_behaves_as_before(self):
        fsm = _make_fsm(_Clock(), state=CONNECTED)
        fsm.bearer_requested = True
        result = await InterfaceConfig.disconnect_bearer.__wrapped__(self._iface(fsm))
        self.assertEqual(result, 'accepted')
        self.assertTrue(fsm.user_disconnected)
        self.assertFalse(fsm.bearer_requested)
        fsm.transition.assert_called_once_with(ModemEvent.ENTER_IDLE)

    async def test_live_lease_suppresses_teardown(self):
        fsm = _make_fsm(_Clock(), state=CONNECTED)
        fsm.bearer_requested = True
        fsm.bearer_leases.acquire(0, 'wlb-failover', ttl=15)
        await InterfaceConfig.disconnect_bearer.__wrapped__(self._iface(fsm))
        self.assertFalse(fsm.user_disconnected)
        self.assertFalse(fsm.bearer_requested)
        fsm.transition.assert_not_called()


class TestOndemandIdleTeardown(IsolatedAsyncioTestCase):
    """The on-demand idle teardown returns the Linux interface to its
    mode-correct baseline: connect-on-demand goes link DOWN with no address,
    dial-on-demand keeps the link UP (interface untouched)."""

    def _fsm(self, mode):
        fsm = _make_fsm(_Clock(), mode=mode, state=CONNECTED)
        fsm._disconnect_bearer = AsyncMock(return_value=True)
        fsm._clear_interface_addresses = AsyncMock()
        fsm._set_interface_down = AsyncMock()
        return fsm

    async def test_connect_on_demand_flushes_and_downs_link(self):
        fsm = self._fsm('connect-on-demand')
        result = await ModemStateMachine._ondemand_idle_teardown(fsm)
        self.assertTrue(result)
        fsm._disconnect_bearer.assert_awaited_once()
        fsm._clear_interface_addresses.assert_awaited_once_with('wwan0')
        fsm._set_interface_down.assert_awaited_once()

    async def test_dial_on_demand_leaves_link_untouched(self):
        fsm = self._fsm('dial-on-demand')
        result = await ModemStateMachine._ondemand_idle_teardown(fsm)
        self.assertTrue(result)
        fsm._disconnect_bearer.assert_awaited_once()
        fsm._clear_interface_addresses.assert_not_awaited()
        fsm._set_interface_down.assert_not_awaited()

    async def test_propagates_failed_disconnect_result(self):
        fsm = self._fsm('connect-on-demand')
        fsm._disconnect_bearer = AsyncMock(return_value=False)
        result = await ModemStateMachine._ondemand_idle_teardown(fsm)
        self.assertFalse(result)


def _load_wlb():
    path = os.path.join(os.path.dirname(__file__), '..', 'helpers',
                        'vyos-load-balancer.py')
    spec = importlib.util.spec_from_file_location('vyos_load_balancer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestWlbFailoverLeases(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wlb = _load_wlb()

    def _lb(self, eth_up, wwan_up=True, failover=True):
        rule = {'interface': {'eth0': {'weight': '10'}, 'wwan0': {'weight': '1'}}}
        if failover:
            rule['failover'] = {}
        return {
            'rule': {'10': rule},
            'health_state': {'eth0': {'state': eth_up},
                             'wwan0': {'state': wwan_up}},
        }

    def _active(self, lb, dod=True):
        with patch.object(self.wlb, 'on_demand_check', return_value=dod):
            return self.wlb._active_failover_wwans(lb)

    def test_primary_up_wwan_managed_not_active(self):
        managed, active = self._active(self._lb(eth_up=True))
        self.assertEqual(managed, {'wwan0'})
        self.assertEqual(active, set())

    def test_primary_down_wwan_active(self):
        managed, active = self._active(self._lb(eth_up=False))
        self.assertEqual(active, {'wwan0'})

    def test_non_failover_rule_not_managed(self):
        managed, active = self._active(self._lb(eth_up=False, failover=False))
        self.assertEqual(managed, set())
        self.assertEqual(active, set())

    def test_non_on_demand_wwan_not_managed(self):
        managed, active = self._active(self._lb(eth_up=False), dod=False)
        self.assertEqual(managed, set())

    def test_reconcile_acquires_and_releases(self):
        client = Mock()
        clients = {'wwan0': client}
        with patch.object(self.wlb, 'on_demand_check', return_value=True):
            self.wlb.reconcile_failover_leases(self._lb(eth_up=False), clients)
            client.bearer_lease_acquire.assert_called_once_with(
                0, 'wlb-failover', ttl=self.wlb.failover_lease_ttl,
                reason='active failover target')
            self.wlb.reconcile_failover_leases(self._lb(eth_up=True), clients)
            client.bearer_lease_release.assert_called_once_with(0, 'wlb-failover')

    def test_reconcile_survives_client_error(self):
        client = Mock()
        client.bearer_lease_acquire.side_effect = RuntimeError('dbus down')
        with patch.object(self.wlb, 'on_demand_check', return_value=True):
            self.wlb.reconcile_failover_leases(self._lb(eth_up=False),
                                               {'wwan0': client})

    def test_release_holddown_holds_then_releases(self):
        # A persistent hold_state + injected clock exercises the release
        # hold-down: a recovered primary must be stably up for the whole window
        # before the metered cellular lease is released.
        client = Mock()
        clients = {'wwan0': client}
        hold = {}
        hd = self.wlb.failover_release_holddown
        with patch.object(self.wlb, 'on_demand_check', return_value=True):
            # t0: wwan0 is the active egress -> acquire + record last-active
            self.wlb.reconcile_failover_leases(self._lb(eth_up=False),
                                               clients, hold, now=1000)
            # within the hold-down, primary recovers -> keep renewing, no release
            self.wlb.reconcile_failover_leases(self._lb(eth_up=True),
                                               clients, hold, now=1000 + hd - 1)
            client.bearer_lease_release.assert_not_called()
            self.assertEqual(client.bearer_lease_acquire.call_count, 2)
            # past the hold-down with primary still up -> release
            self.wlb.reconcile_failover_leases(self._lb(eth_up=True),
                                               clients, hold, now=1000 + hd + 1)
            client.bearer_lease_release.assert_called_once_with(0, 'wlb-failover')

    def test_holddown_resets_when_active_again(self):
        # A flap back to active during the hold-down restarts the timer, so the
        # bearer is never dropped while the better path keeps flapping.
        client = Mock()
        clients = {'wwan0': client}
        hold = {}
        hd = self.wlb.failover_release_holddown
        with patch.object(self.wlb, 'on_demand_check', return_value=True):
            self.wlb.reconcile_failover_leases(self._lb(eth_up=False),
                                               clients, hold, now=1000)
            self.wlb.reconcile_failover_leases(self._lb(eth_up=True),
                                               clients, hold, now=1010)
            # active again: last-active moves to 1020
            self.wlb.reconcile_failover_leases(self._lb(eth_up=False),
                                               clients, hold, now=1020)
            # would have passed the ORIGINAL window (1000+hd) but not the reset
            self.wlb.reconcile_failover_leases(self._lb(eth_up=True),
                                               clients, hold, now=1020 + hd - 1)
            client.bearer_lease_release.assert_not_called()


class TestBearerPathValidation(TestCase):
    def test_valid_bearer_path(self):
        self.assertTrue(state_machine.is_valid_bearer_path(
            '/org/freedesktop/ModemManager1/Bearer/3'))

    def test_invalid_bearer_paths(self):
        for bad in (None, '', 'None', '/', 3, 3.0, [],
                    '/org/freedesktop/ModemManager1/Modem/0'):
            self.assertFalse(state_machine.is_valid_bearer_path(bad),
                             msg=f'{bad!r} should be invalid')


class TestWlbOnDemandPredicates(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wlb = _load_wlb()

    @staticmethod
    def _conf(mode):
        conf = Mock()
        conf.return_effective_value.return_value = mode
        return conf

    def test_on_demand_matches_both_on_demand_modes(self):
        for mode in ('connect-on-demand', 'dial-on-demand'):
            self.assertTrue(
                self.wlb.on_demand_check('wwan0', conf=self._conf(mode)), mode)

    def test_on_demand_excludes_always_on_and_non_wwan(self):
        self.assertFalse(
            self.wlb.on_demand_check('wwan0', conf=self._conf('always-on')))
        # eth* is never a WWAN regardless of the (irrelevant) mode value
        self.assertFalse(
            self.wlb.on_demand_check('eth0', conf=self._conf('dial-on-demand')))

    def test_both_on_demand_modes_are_lease_managed(self):
        # Both on-demand modes get the lease: a dial-on-demand traffic trigger
        # cannot bootstrap an idle bearer under WLB's policy routing, so the
        # route-independent lease raises it for both.
        for mode in ('connect-on-demand', 'dial-on-demand'):
            self.assertTrue(
                self.wlb.on_demand_check('wwan0', conf=self._conf(mode)), mode)
        self.assertFalse(
            self.wlb.on_demand_check('wwan0', conf=self._conf('always-on')))
