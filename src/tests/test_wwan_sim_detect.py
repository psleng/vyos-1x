# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

import os
import sys
import threading
import types
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

# Optional on developer workstations, but imported by vyos package startup.
try:
    __import__('cracklib')
except ModuleNotFoundError:
    cracklib = types.ModuleType('cracklib')
    cracklib.VeryFascistCheck = lambda value: value
    sys.modules['cracklib'] = cracklib

from vyos.hardware.base import Board, Pin
from vyos.hardware import api as hardware_api
from vyos.utils.wwan import interfaces_wwan_state_machine as state_machine
from vyos.utils.wwan.interfaces_wwan_state_machine import ModemState
from vyos.utils.wwan.interfaces_wwan_state_machine import ModemStateMachine
from vyos.utils.wwan.sim_controller import GpioMuxSimController


class _FakeEdge:
    def __init__(self, event_type, line_offset=52):
        self.event_type = event_type
        self.line_offset = line_offset
        self.timestamp_ns = 1


class _FakeRequest:
    def __init__(self, initial_value, edge_namespace):
        self.fd, self._write_fd = os.pipe()
        self._value = initial_value
        self._edge_namespace = edge_namespace
        self._events = []
        self._get_count = 0
        self.final_sampled = threading.Event()
        self._released = False

    def get_value(self, _line):
        self._get_count += 1
        if self._get_count > 1:
            self.final_sampled.set()
        return self._value

    def read_edge_events(self):
        os.read(self.fd, 4096)
        events, self._events = self._events, []
        return events

    def emit(self, event_type, final_value):
        self._value = final_value
        self._events.append(_FakeEdge(event_type))
        os.write(self._write_fd, b'x')

    def release(self):
        if self._released:
            return
        self._released = True
        os.close(self.fd)
        os.close(self._write_fd)


class _FakeGpiod:
    class _Value:
        ACTIVE = 1
        INACTIVE = 0

    class _Direction:
        INPUT = 'input'

    class _Edge:
        BOTH = 'both'

    class _Bias:
        PULL_UP = 'pull-up'
        PULL_DOWN = 'pull-down'
        AS_IS = 'as-is'

    class _EdgeType:
        RISING_EDGE = 'rising'
        FALLING_EDGE = 'falling'

    def __init__(self, initial_value):
        self.line = SimpleNamespace(
            Value=self._Value,
            Direction=self._Direction,
            Edge=self._Edge,
            Bias=self._Bias,
        )
        self.EdgeEvent = SimpleNamespace(Type=self._EdgeType)
        self.initial_value = initial_value
        self.request = None
        self.created = threading.Event()

    @staticmethod
    def LineSettings(**kwargs):
        return kwargs

    def request_lines(self, _path, *, consumer, config):
        del consumer, config
        self.request = _FakeRequest(self.initial_value, self.EdgeEvent)
        self.created.set()
        return self.request


class _WatchBoard(Board):
    NAME = 'test-watch-board'
    PINS = {
        'SIM1_DETECT': Pin(
            bank=0, line=52, dir='in', bias='pull-up', group='cell'
        ),
    }
    fake_gpiod = None

    @classmethod
    def _gpiod(cls):
        return cls.fake_gpiod

    def _resolve_bank(self, _bank):
        return '/dev/fake-gpiochip'


class TestGPIOWatchFinalSampling(TestCase):
    def _run_watch(
        self,
        *,
        request_initial,
        caller_initial,
        edge_type=None,
        final_value=None,
    ):
        fake = _FakeGpiod(request_initial)
        _WatchBoard.fake_gpiod = fake
        board = _WatchBoard()
        stop_read, stop_write = os.pipe()
        observed = []
        observed_event = threading.Event()
        finished = threading.Event()

        def _collect():
            try:
                for event in board.watch_pins(
                    ['SIM1_DETECT'],
                    stop_fd=stop_read,
                    settle_ms=0,
                    initial_levels={'SIM1_DETECT': caller_initial},
                ):
                    observed.append(event)
                    observed_event.set()
            finally:
                finished.set()

        thread = threading.Thread(target=_collect, daemon=True)
        thread.start()
        self.assertTrue(fake.created.wait(1), 'GPIO request was not created')

        if edge_type is not None:
            fake.request.emit(edge_type, final_value)

        self.assertTrue(
            fake.request.final_sampled.wait(1),
            'watcher did not perform its final GPIO sample',
        )
        os.write(stop_write, b'x')
        self.assertTrue(finished.wait(1), 'GPIO watcher did not stop')
        thread.join(timeout=1)
        os.close(stop_read)
        os.close(stop_write)
        return observed, observed_event.is_set()

    def test_stale_falling_edge_is_suppressed_by_final_sample(self):
        observed, emitted = self._run_watch(
            request_initial=1,
            caller_initial=1,
            edge_type=_FakeGpiod._EdgeType.FALLING_EDGE,
            # The physical line recovered, but its rising edge was lost.
            final_value=1,
        )
        self.assertFalse(emitted)
        self.assertEqual(observed, [])

    def test_sustained_falling_edge_emits_absent_level(self):
        observed, emitted = self._run_watch(
            request_initial=1,
            caller_initial=1,
            edge_type=_FakeGpiod._EdgeType.FALLING_EDGE,
            final_value=0,
        )
        self.assertTrue(emitted)
        self.assertEqual([(name, level) for name, level, _ in observed], [
            ('SIM1_DETECT', 0),
        ])

    def test_initial_sample_watch_gap_is_reconciled(self):
        observed, emitted = self._run_watch(
            # Caller sampled present, but the line changed before the watcher
            # acquired its long-lived request.  No edge injection is needed.
            request_initial=0,
            caller_initial=1,
        )
        self.assertTrue(emitted)
        self.assertEqual([(name, level) for name, level, _ in observed], [
            ('SIM1_DETECT', 0),
        ])

    def test_periodic_sample_recovers_a_missed_edge(self):
        fake = _FakeGpiod(0)
        _WatchBoard.fake_gpiod = fake
        board = _WatchBoard()
        stop_read, stop_write = os.pipe()
        observed = []
        observed_event = threading.Event()

        def _collect():
            for event in board.watch_pins(
                ['SIM1_DETECT'],
                stop_fd=stop_read,
                settle_ms=0,
                initial_levels={'SIM1_DETECT': 0},
                verify_interval_ms=10,
            ):
                observed.append(event)
                observed_event.set()

        thread = threading.Thread(target=_collect, daemon=True)
        thread.start()
        self.assertTrue(fake.created.wait(1))
        # Physical line changed high, but no rising edge was delivered.
        fake.request._value = 1
        self.assertTrue(observed_event.wait(1))
        os.write(stop_write, b'x')
        thread.join(timeout=1)
        os.close(stop_read)
        os.close(stop_write)

        self.assertEqual([(name, level) for name, level, _ in observed], [
            ('SIM1_DETECT', 1),
        ])


class TestGpioMuxControllerWatch(TestCase):
    def test_watch_handoff_and_duplicate_state_suppression(self):
        class _Hardware:
            initial_levels = None

            @staticmethod
            def sim_detect_pins(_modem):
                return ['MODEM0_SIM_DETECT_0']

            @classmethod
            def watch_sim_detect(cls, _modem, **kwargs):
                cls.initial_levels = kwargs['initial_levels']
                yield 'MODEM0_SIM_DETECT_0', 'INSERTED', 1
                yield 'MODEM0_SIM_DETECT_0', 'INSERTED', 2

        fsm = SimpleNamespace(interface_number=0, _on_sim_detect_event=Mock())
        controller = GpioMuxSimController(fsm, 'MODEM0', _Hardware)
        controller._present[1] = False
        controller._known_slots.add(1)
        controller._loop = SimpleNamespace(
            call_soon_threadsafe=lambda callback, *args: callback(*args)
        )

        controller._watch_run()

        self.assertEqual(_Hardware.initial_levels, {
            'MODEM0_SIM_DETECT_0': 0,
        })
        self.assertTrue(controller._present[1])
        fsm._on_sim_detect_event.assert_called_once_with(1, True)

    def test_watch_sim_detect_uses_logical_active_low_level(self):
        board = SimpleNamespace(
            PINS={
                'SIM1_DETECT_N': Pin(
                    bank=0, line=1, dir='in', active_low=True
                ),
            },
            sim_detect_pins=Mock(return_value=['SIM1_DETECT_N']),
            watch_pins=Mock(return_value=iter([
                # Board.watch_pins already applies active_low through
                # libgpiod, so logical 1 means asserted/present.
                ('SIM1_DETECT_N', 1, 123),
            ])),
        )

        with patch.object(hardware_api, '_b', board):
            events = list(hardware_api.watch_sim_detect('MODEM0'))

        self.assertEqual(events, [
            ('SIM1_DETECT_N', 'INSERTED', 123),
        ])


class TestActiveSimRemovalPolicy(IsolatedAsyncioTestCase):
    async def test_one_removal_edge_only_arms_confirmation(self):
        fsm = SimpleNamespace(
            sim_controller=SimpleNamespace(is_gpio_mux=True),
            _admin_disabled=False,
            current_active_sim=1,
            config={'primary_sim_slot': 1},
            interface_number=0,
            _active_sim_removal_grace_seconds=2,
            _arm_active_sim_removal_watchdog=Mock(),
            _safe_create_task=Mock(),
        )

        ModemStateMachine._on_sim_detect_event(fsm, 1, False)

        fsm._arm_active_sim_removal_watchdog.assert_called_once_with(1)
        fsm._safe_create_task.assert_not_called()

    async def test_removal_is_ignored_while_admin_disabled(self):
        fsm = SimpleNamespace(
            sim_controller=SimpleNamespace(is_gpio_mux=True),
            _admin_disabled=True,
            current_active_sim=1,
            config={'primary_sim_slot': 1},
            interface_number=0,
            _arm_active_sim_removal_watchdog=Mock(),
            _safe_create_task=Mock(),
        )

        ModemStateMachine._on_sim_detect_event(fsm, 1, False)

        fsm._arm_active_sim_removal_watchdog.assert_not_called()
        fsm._safe_create_task.assert_not_called()

    async def test_active_slot_return_cancels_confirmation(self):
        async def _handle_insertion(_slot):
            return None

        scheduled = []

        def _schedule(coro, *, name):
            scheduled.append(name)
            coro.close()

        fsm = SimpleNamespace(
            sim_controller=SimpleNamespace(is_gpio_mux=True),
            _admin_disabled=False,
            current_active_sim=1,
            config={'primary_sim_slot': 1},
            interface_number=0,
            _active_sim_removal_watchdog_slot=1,
            _cancel_active_sim_removal_watchdog=Mock(),
            _safe_create_task=_schedule,
            _handle_sim_detect_insertion=_handle_insertion,
        )

        ModemStateMachine._on_sim_detect_event(fsm, 1, True)

        fsm._cancel_active_sim_removal_watchdog.assert_called_once_with()
        self.assertEqual(scheduled, ['sim_detect_insertion'])

    async def test_alternate_slot_insertion_keeps_confirmation_armed(self):
        async def _handle_insertion(_slot):
            return None

        def _schedule(coro, *, name):
            del name
            coro.close()

        fsm = SimpleNamespace(
            sim_controller=SimpleNamespace(is_gpio_mux=True),
            _admin_disabled=False,
            current_active_sim=1,
            config={'primary_sim_slot': 1},
            interface_number=0,
            _active_sim_removal_watchdog_slot=1,
            _cancel_active_sim_removal_watchdog=Mock(),
            _safe_create_task=_schedule,
            _handle_sim_detect_insertion=_handle_insertion,
        )

        ModemStateMachine._on_sim_detect_event(fsm, 2, True)

        fsm._cancel_active_sim_removal_watchdog.assert_not_called()

    async def test_transient_absence_does_not_fail_over(self):
        fsm = SimpleNamespace(
            interface_number=0,
            _active_sim_removal_grace_seconds=2,
            _active_sim_removal_watchdog_task=None,
            _active_sim_removal_watchdog_slot=1,
            _sim_switch_in_progress=False,
            _sim_failover_in_progress=False,
            current_active_sim=1,
            config={'primary_sim_slot': 1},
            sim_controller=SimpleNamespace(is_present=AsyncMock(return_value=True)),
            machine=SimpleNamespace(current_state=ModemState.CONNECTED.value),
            _cancel_failed_retry=Mock(),
            _handle_sim_missing_failover=AsyncMock(),
        )

        with patch.object(state_machine.asyncio, 'sleep', new=AsyncMock()):
            await ModemStateMachine._active_sim_removal_failover_watchdog(
                fsm, 1
            )

        fsm._cancel_failed_retry.assert_not_called()
        fsm._handle_sim_missing_failover.assert_not_awaited()

    async def test_sustained_absence_fails_over_even_if_modem_is_connected(self):
        fsm = SimpleNamespace(
            interface_number=0,
            _active_sim_removal_grace_seconds=2,
            _active_sim_removal_watchdog_task=None,
            _active_sim_removal_watchdog_slot=1,
            _sim_switch_in_progress=False,
            _sim_failover_in_progress=False,
            current_active_sim=1,
            config={'primary_sim_slot': 1},
            sim_controller=SimpleNamespace(is_present=AsyncMock(return_value=False)),
            machine=SimpleNamespace(current_state=ModemState.CONNECTED.value),
            _cancel_failed_retry=Mock(),
            _handle_sim_missing_failover=AsyncMock(),
        )

        with patch.object(state_machine.asyncio, 'sleep', new=AsyncMock()):
            await ModemStateMachine._active_sim_removal_failover_watchdog(
                fsm, 1
            )

        fsm._cancel_failed_retry.assert_called_once_with()
        fsm._handle_sim_missing_failover.assert_awaited_once_with()

    async def test_old_confirmation_does_not_clear_replacement_task(self):
        replacement = SimpleNamespace(done=lambda: False)
        fsm = SimpleNamespace(
            interface_number=0,
            _active_sim_removal_grace_seconds=2,
            _active_sim_removal_watchdog_task=replacement,
            _active_sim_removal_watchdog_slot=2,
            _sim_switch_in_progress=True,
            _sim_failover_in_progress=False,
            current_active_sim=1,
            config={'primary_sim_slot': 1},
        )

        with patch.object(state_machine.asyncio, 'sleep', new=AsyncMock()):
            await ModemStateMachine._active_sim_removal_failover_watchdog(
                fsm, 1
            )

        self.assertIs(fsm._active_sim_removal_watchdog_task, replacement)
        self.assertEqual(fsm._active_sim_removal_watchdog_slot, 2)
