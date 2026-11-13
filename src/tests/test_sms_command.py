# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
"""Gateway tests run on a workstation without a modem or VyOS config backend.

FSM/D-Bus methods are compiled from their source AST to exercise the real methods
without importing hardware-specific dependencies. Password strength uses a mock
when cracklib is unavailable; password hashing/verification use real passlib.
"""

import ast
import asyncio
import copy
import datetime
import json
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    __import__('cracklib')
except ModuleNotFoundError:
    stub = types.ModuleType('cracklib')
    stub.ASCII_UPPERCASE = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    stub.ASCII_LOWERCASE = 'abcdefghijklmnopqrstuvwxyz'

    def unavailable(_password):
        raise RuntimeError('cracklib unavailable on this workstation')

    stub.FascistCheck = unavailable
    sys.modules['cracklib'] = stub

from passlib.hash import sha512_crypt
from vyos.utils import auth
from vyos.utils.wwan import sms_command as sms

ROOT = Path(__file__).resolve().parents[2]
NUMBER = '+15555551234'
PASSWORD = 'X4!rT9qW2zH7'
HASH = sha512_crypt.using(rounds=1000).hash(PASSWORD)


def settings(**overrides):
    return {
        'allow_from': [NUMBER],
        'password_hash': HASH,
        'rate_limit': 5,
        'timeout': 20,
        'max_reply_segments': 4,
        **overrides,
    }


def extracted_class(path, classname, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    source = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == classname
    )
    methods = [
        copy.deepcopy(n)
        for n in source.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    for method in methods:
        method.decorator_list = []
    cls = ast.ClassDef(
        name=classname, bases=[], keywords=[], body=methods, decorator_list=[]
    )
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(ROOT / path), 'exec'), namespace)  # noqa: S102 - trusted repository source
    return namespace[classname]


class CatalogAndReplyTests(unittest.TestCase):
    def test_catalog_default_and_action_confirmation(self):
        catalog = sms.load_catalog()
        self.assertEqual(catalog['show']['tier'], 'read-only')
        self.assertTrue(
            all(e['tier'] == 'privileged' for v, e in catalog.items() if v != 'show')
        )

    def test_invalid_catalog_fails_closed(self):
        for entry in (
            {'wrapper_verb': 'reset', 'tier': 'read-only'},
            {'wrapper_verb': 'reset', 'tier': 'privileged', 'confirm': False},
        ):
            sms.load_catalog.cache_clear()
            body = json.dumps(
                {
                    'verbs': {
                        'show': {'wrapper_verb': 'show', 'tier': 'read-only'},
                        'reset': entry,
                    }
                }
            )
            with (
                patch.object(Path, 'read_text', return_value=body),
                self.assertRaises(ValueError),
            ):
                sms.load_catalog()
        sms.load_catalog.cache_clear()

    def test_number_matching_rejects_unlisted_numbers_and_text(self):
        for number in ('433460', 'Rogers', '+123', '1555;1234'):
            self.assertFalse(sms.number_allowed(number, [NUMBER]))
        self.assertFalse(sms.number_allowed('5555551234', [NUMBER]))
        self.assertTrue(sms.number_allowed(NUMBER[1:], [NUMBER]))
        self.assertFalse(sms.number_allowed('+4414165551234', ['+14165551234']))
        self.assertFalse(sms.number_allowed('+25555551234', [NUMBER]))

    def test_international_number_lengths_and_short_number_isolation(self):
        for number in (
            '+6831234',
            '+442079460018',
            '+919876543210',
            '+123456789012345',
        ):
            with self.subTest(number=number):
                self.assertEqual(sms.normalize_number(number), number[1:])
                self.assertTrue(sms.number_allowed(number[1:], [number]))
                sms.runtime_config(settings(allow_from=[number]))
        self.assertFalse(sms.number_allowed('+441234567', ['+1234567']))
        self.assertFalse(sms.number_allowed('+1234567', ['+441234567']))
        for invalid in ('', '+', '+1', '+0123456', '+1234567890123456', 'country-name'):
            self.assertIsNone(sms.normalize_number(invalid))

    def test_allowlist_requires_e164_but_modem_may_omit_plus(self):
        for number in (
            '442079460018',
            '00442079460018',
            '+44 2079460018',
            '+44-2079460018',
            '+0123456789',
            '+1',
        ):
            with self.subTest(number=number), self.assertRaises(ValueError):
                sms.runtime_config(settings(allow_from=[number]))
        self.assertTrue(sms.number_allowed('442079460018', ['+442079460018']))

    def test_gsm_output_bounded_including_extensions(self):
        for text in ('x' * 10000, '^' * 10000, '\nline' * 10000):
            payload, parts = sms.bound_reply(text, 4)
            self.assertEqual(parts, 4)
            self.assertTrue(payload.endswith('...'))
            self.assertLessEqual(
                sum(2 if c in '^{}\\[~]|€' else 1 for c in payload), 600
            )

    def test_unicode_output_bounds_use_utf16_units(self):
        for text in ('界' * 10000, '🙂' * 10000):
            payload, parts = sms.bound_reply(text, 4)
            self.assertEqual(parts, 4)
            self.assertTrue(payload.endswith('...'))
            self.assertLessEqual(len(payload.encode('utf-16-be')) // 2, 268)

    def test_empty_and_short_output(self):
        self.assertEqual(sms.bound_reply('', 4), ('no output', 1))
        self.assertEqual(sms.bound_reply('hello', 4), ('hello', 1))
        self.assertEqual(sms.bound_reply('hello', 0), ('', 0))

    def test_payload_preserved_at_budget_boundary(self):
        for text, maximum in [('x' * 600, 4), ('^' * 300, 4), ('界' * 268, 4)]:
            self.assertEqual(sms.bound_reply(text, maximum), (text, maximum))


class PasswordAndConfigTests(unittest.TestCase):
    def test_real_hash_verification(self):
        self.assertTrue(auth.verify_password(PASSWORD, HASH))
        self.assertFalse(auth.verify_password('incorrect', HASH))
        for invalid in ('!', '*', '', '$6$broken', HASH[:-1], '$1$bad$bad'):
            self.assertFalse(auth.verify_password(PASSWORD, invalid))

    def test_sms_hash_uses_same_linux_context_as_login(self):
        from passlib.hosts import linux_context

        with patch.object(linux_context, 'hash', return_value=HASH) as hasher:
            self.assertEqual(auth.hash_password(PASSWORD), HASH)
            hasher.assert_called_once_with(PASSWORD)
        hashed = auth.hash_password(PASSWORD)
        self.assertTrue(linux_context.verify(PASSWORD, hashed))
        self.assertTrue(auth.verify_password(PASSWORD, hashed))
        with self.assertRaises(ValueError):
            auth.validate_password_hash('$2b$12$' + 'a' * 53)

    def test_supported_hash_work_factor_is_not_capped_by_sms(self):
        auth.validate_password_hash(HASH.replace('rounds=1000', 'rounds=999999999'))

    def test_old_suffix_setting_cannot_enable_partial_matching(self):
        cfg = sms.runtime_config(settings(phone_match_digits=1))
        self.assertNotIn('phone_match_digits', cfg)
        self.assertFalse(sms.number_allowed('+25555551234', cfg['allow_from']))

    def test_no_extra_allowlist_or_configured_limit_caps(self):
        numbers = [f'+1555{i:07d}' for i in range(64)]
        result = sms.runtime_config(
            settings(
                allow_from=numbers,
                rate_limit=1000,
                timeout=3600,
                max_reply_segments=100,
            )
        )
        self.assertEqual(result['allow_from'], numbers)
        self.assertEqual(result['rate_limit'], 1000)
        self.assertEqual(result['timeout'], 3600)
        self.assertEqual(result['max_reply_segments'], 100)
        with patch.object(
            auth,
            'evaluate_strength',
            return_value={'strength': auth.EPasswdStrength.STRONG},
        ):
            sms.verify_config(settings(password=PASSWORD * 12))

    def test_configuration_defaults_and_disabled_verbs(self):
        cfg = sms.runtime_config({'allow_from': [NUMBER], 'password_hash': HASH})
        self.assertEqual(cfg['timeout'], 20)
        self.assertEqual(cfg['rate_limit'], 5)
        self.assertNotIn('privileged', cfg)

    def test_bad_config_fails_closed(self):
        for cfg in (
            settings(allow_from=[]),
            settings(timeout=0),
            settings(rate_limit=0),
            settings(privileged={'verb': ['configure']}),
            settings(privileged={'verb': ['show']}),
            settings(allow_from=['not-a-number']),
            settings(password_hash='!'),
            settings(password=PASSWORD),
        ):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                sms.runtime_config(cfg)

    def test_disabled_login_policy_skips_sms_strength_checks(self):
        with patch.object(auth, 'evaluate_strength') as strength:
            for password in ('igos', 'a', 'longpasswordonly'):
                sms.verify_config(settings(password=password))
            strength.assert_not_called()
        for password in ('', 'With spaces123!'):
            with self.assertRaises(ValueError):
                sms.verify_config(settings(password=password))

    def test_enabled_login_policy_enforces_defaults_and_custom_thresholds(self):
        with patch.object(
            auth,
            'evaluate_strength',
            return_value={'strength': auth.EPasswdStrength.STRONG},
        ):
            with self.assertRaises(ValueError):
                sms.verify_config(settings(password='igos'), {})
            sms.verify_config(settings(password=PASSWORD), {})
            with self.assertRaises(ValueError):
                sms.verify_config(settings(password=PASSWORD), {'minimum_length': 40})
            for policy in (
                {'minimum_digit': 5},
                {'minimum_uppercase': 10},
                {'minimum_special': 5},
            ):
                with self.subTest(policy=policy), self.assertRaises(ValueError):
                    sms.verify_config(settings(password=PASSWORD), policy)

    def test_cracklib_rejection_when_login_policy_enabled(self):
        for result in (auth.EPasswdStrength.WEAK, auth.EPasswdStrength.ERROR):
            with (
                patch.object(
                    auth, 'evaluate_strength', return_value={'strength': result}
                ),
                self.assertRaises(ValueError),
            ):
                sms.verify_config(settings(password=PASSWORD), {})


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fsm = SimpleNamespace(
            config={'sms_command': settings()},
            interface_number=0,
            _sms_command_armed=True,
            sms_send=AsyncMock(),
            _safe_create_task=lambda coro, name=None: asyncio.create_task(coro),
        )
        self.gateway = sms.Gateway(self.fsm, self.fsm.config['sms_command'])
        self.runner = AsyncMock(return_value='version output')
        self.patch = patch.object(sms, 'run_command', self.runner)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def asyncTearDown(self):
        self.gateway.close()
        if self.gateway.tasks:
            await asyncio.gather(*self.gateway.tasks, return_exceptions=True)

    async def request(self, body, number=NUMBER):
        self.gateway.submit(number, body)
        tasks = list(self.gateway.tasks)
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(0)

    def enable_action(self):
        self.gateway.config['privileged'] = {'verb': ['reset']}
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)

    def pending_for(self, number=NUMBER):
        sender = sms.normalize_number(number)
        return next(
            item
            for (owner, _nonce), item in self.gateway.pending.items()
            if owner == sender
        )

    async def test_show_dispatches_exact_argv(self):
        await self.request(PASSWORD + ' show version')
        self.runner.assert_awaited_once_with(
            [sms.WRAPPER, 'show', 'version'], 20, max_reply_segments=4
        )

    async def test_bad_password_and_sender_silent(self):
        await self.request('incorrect show version')
        await self.request(PASSWORD + ' show version', '+15555556789')
        self.runner.assert_not_awaited()
        self.fsm.sms_send.assert_not_awaited()

    async def test_explicit_verb_and_disabled_actions(self):
        await self.request(PASSWORD + ' interfaces wwan wwan0')
        await self.request(PASSWORD + ' reboot now')
        self.runner.assert_not_awaited()

    async def test_all_shell_control_characters_rejected(self):
        for char in (';', '|', '`', '$', '\\', '>', '<', '&', '*', '?', '\n'):
            # Direct handle bypasses admission limits to test each parser input.
            await self.gateway.handle(
                NUMBER, sms.normalize_number(NUMBER), PASSWORD + f' show "a{char}b"'
            )
        self.runner.assert_not_awaited()

    async def test_quoted_path_preserved(self):
        await self.request(PASSWORD + ' show "two words"')
        self.runner.assert_awaited_once_with(
            [sms.WRAPPER, 'show', 'two words'], 20, max_reply_segments=4
        )

    async def test_action_requires_single_use_confirmation(self):
        self.enable_action()
        await self.request(PASSWORD + ' reset vpn ipsec')
        self.runner.assert_not_awaited()
        nonce = self.pending_for().nonce
        await self.request(PASSWORD + ' CONFIRM ' + nonce)
        self.runner.assert_awaited_once_with(
            [sms.WRAPPER, 'reset', 'vpn', 'ipsec'], 20, max_reply_segments=4
        )
        await self.request(PASSWORD + ' CONFIRM ' + nonce)
        self.assertEqual(self.runner.await_count, 1)

    async def test_expired_confirmation(self):
        self.enable_action()
        await self.request(PASSWORD + ' reset vpn')
        key = next(iter(self.gateway.pending))
        _, nonce, command = self.gateway.pending[key]
        self.gateway.pending[key] = sms.PendingAction(0, nonce, command)
        await self.request(PASSWORD + ' CONFIRM ' + nonce)
        self.runner.assert_not_awaited()
        self.assertNotIn(key, self.gateway.pending)

    async def test_confirmation_is_bound_to_sender(self):
        self.enable_action()
        other = '+15555556789'
        self.gateway.config['allow_from'].append(other)
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)
        await self.request(PASSWORD + ' reset vpn')
        nonce = self.pending_for().nonce
        await self.request(PASSWORD + ' CONFIRM ' + nonce, other)
        self.runner.assert_not_awaited()
        self.assertIn((sms.normalize_number(NUMBER), nonce), self.gateway.pending)

    async def test_confirmation_ack_and_output_share_reply_budget(self):
        self.enable_action()
        self.gateway.config['max_reply_segments'] = 2
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)
        await self.request(PASSWORD + ' reset vpn')
        nonce = self.pending_for().nonce
        self.fsm.sms_send.reset_mock()
        self.runner.return_value = 'a' * 10000
        await self.request(PASSWORD + ' CONFIRM ' + nonce)
        self.assertEqual(self.fsm.sms_send.await_count, 2)
        self.assertTrue(self.fsm.sms_send.call_args.args[1].endswith('...'))

    async def test_configured_rate_is_the_only_admission_quota(self):
        release, all_started = asyncio.Event(), asyncio.Event()
        active = 0
        self.gateway.config['rate_limit'] = 60
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)

        async def delayed(*_args, **_kwargs):
            nonlocal active
            active += 1
            if active == 40:
                all_started.set()
            await release.wait()
            return False

        with patch.object(asyncio, 'to_thread', delayed):
            for _ in range(40):
                self.gateway.submit(NUMBER, PASSWORD + ' show version')
            try:
                await asyncio.wait_for(all_started.wait(), 5)
                self.assertEqual(len(self.gateway.tasks), 40)
            finally:
                release.set()
                await asyncio.gather(*self.gateway.tasks)

    async def test_two_commands_execute_simultaneously(self):
        both_started, release = asyncio.Event(), asyncio.Event()
        active = 0

        async def command(*_args, **_kwargs):
            nonlocal active
            active += 1
            if active == 2:
                both_started.set()
            await release.wait()
            return 'done'

        self.runner.side_effect = command
        for path in ('version', 'interfaces'):
            self.gateway.submit(NUMBER, PASSWORD + ' show ' + path)
        try:
            await asyncio.wait_for(both_started.wait(), 5)
            self.assertEqual(self.runner.await_count, 2)
        finally:
            release.set()
            await asyncio.gather(*self.gateway.tasks)
        self.assertEqual(self.fsm.sms_send.await_count, 2)

    async def test_slow_reply_does_not_block_next_command(self):
        sending, release, second_executed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def send(*_args):
            sending.set()
            await release.wait()

        self.fsm.sms_send.side_effect = send
        self.gateway.submit(NUMBER, PASSWORD + ' show version')
        await asyncio.wait_for(sending.wait(), 5)

        async def command(*_args, **_kwargs):
            second_executed.set()
            return 'second result'

        self.runner.side_effect = command
        self.gateway.submit(NUMBER, PASSWORD + ' show interfaces')
        try:
            await asyncio.wait_for(second_executed.wait(), 5)
            self.assertEqual(self.fsm.sms_send.await_count, 1)
        finally:
            release.set()
            await asyncio.gather(*self.gateway.tasks)
        self.assertEqual(self.fsm.sms_send.await_count, 2)

    async def test_parallel_execution_has_no_extra_fixed_ceiling(self):
        all_started, release = asyncio.Event(), asyncio.Event()
        active = 0
        self.gateway.config['rate_limit'] = 60
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)

        async def command(*_args, **_kwargs):
            nonlocal active
            active += 1
            if active == 40:
                all_started.set()
            await release.wait()
            return 'done'

        self.runner.side_effect = command
        for _ in range(40):
            self.gateway.submit(NUMBER, PASSWORD + ' show version')
        try:
            await asyncio.wait_for(all_started.wait(), 5)
            self.assertEqual(active, 40)
        finally:
            release.set()
            await asyncio.gather(*self.gateway.tasks)
        self.assertEqual(self.runner.await_count, 40)

    async def test_request_length_has_no_extra_sms_gateway_cap(self):
        path = 'x' * 2000
        await self.request(PASSWORD + ' show ' + path)
        self.assertEqual(self.runner.await_args.args[0][-1], path)

    async def test_parallel_actions_keep_independent_nonces(self):
        self.enable_action()
        for path in ('ipsec', 'openvpn'):
            self.gateway.submit(NUMBER, PASSWORD + ' reset vpn ' + path)
        await asyncio.gather(*self.gateway.tasks)
        self.assertEqual(len(self.gateway.pending), 2)
        commands = [
            PASSWORD + ' CONFIRM ' + pending.nonce
            for pending in self.gateway.pending.values()
        ]
        for command in commands:
            self.gateway.submit(NUMBER, command)
        await asyncio.gather(*self.gateway.tasks)
        self.assertEqual(self.runner.await_count, 2)
        self.assertFalse(self.gateway.pending)

    async def test_acceptance_wait_uses_transport_without_extra_deadline(self):
        self.enable_action()
        self.gateway.config['max_reply_segments'] = 1
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)
        await self.request(PASSWORD + ' reset vpn')
        nonce = self.pending_for().nonce
        sending, release = asyncio.Event(), asyncio.Event()

        async def send(*_args):
            sending.set()
            await release.wait()

        self.fsm.sms_send.side_effect = send
        self.gateway.submit(NUMBER, PASSWORD + ' CONFIRM ' + nonce)
        await asyncio.wait_for(sending.wait(), 5)
        self.runner.assert_not_awaited()
        release.set()
        await asyncio.gather(*self.gateway.tasks)
        self.runner.assert_awaited_once()

    async def test_reply_preserves_words_matching_password(self):
        self.gateway.config['password_hash'] = sha512_crypt.using(rounds=1000).hash(
            'igos'
        )
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)
        self.runner.return_value = 'Hostname: igos\nUser: igos\nVersion: iGOS'
        await self.request('igos show version')
        self.assertEqual(self.fsm.sms_send.call_args.args[1], self.runner.return_value)

    async def test_reply_preserves_command_output_without_secret_substitution(self):
        self.runner.return_value = 'echo ' + PASSWORD + ' hash ' + HASH
        await self.request(PASSWORD + ' show version')
        self.assertEqual(self.fsm.sms_send.call_args.args[1], self.runner.return_value)

    async def test_parallel_replies_use_one_submission_each(self):
        async def command(argv, _timeout, **_kwargs):
            return ('A' if argv[-1] == 'version' else 'B') * 500

        async def send(*_args):
            await asyncio.sleep(0)

        self.runner.side_effect = command
        self.fsm.sms_send.side_effect = send
        for path in ('version', 'interfaces'):
            self.gateway.submit(NUMBER, PASSWORD + ' show ' + path)
        await asyncio.gather(*self.gateway.tasks)
        messages = [call.args[1] for call in self.fsm.sms_send.await_args_list]
        self.assertEqual(len(messages), 2)
        groups = ['A' if 'AAAA' in text else 'B' for text in messages]
        self.assertIn(groups, [['A', 'B'], ['B', 'A']])
        self.assertCountEqual(messages, ['A' * 500, 'B' * 500])

    async def test_parallel_confirmation_replay_executes_once(self):
        self.enable_action()
        await self.request(PASSWORD + ' reset vpn')
        nonce = self.pending_for().nonce
        for _ in range(2):
            self.gateway.submit(NUMBER, PASSWORD + ' CONFIRM ' + nonce)
        await asyncio.gather(*self.gateway.tasks)
        self.runner.assert_awaited_once()

    async def test_failed_command_does_not_stop_parallel_request(self):
        async def command(argv, _timeout, **_kwargs):
            if argv[-1] == 'version':
                raise RuntimeError('execution failed')
            return 'successful parallel output'

        self.runner.side_effect = command
        for path in ('version', 'interfaces'):
            self.gateway.submit(NUMBER, PASSWORD + ' show ' + path)
        await asyncio.gather(*self.gateway.tasks)
        messages = [call.args[1] for call in self.fsm.sms_send.await_args_list]
        self.assertIn('successful parallel output', messages)
        self.assertIn('command execution failed', messages)

    async def test_privileged_allowlist_further_restricts(self):
        self.enable_action()
        self.gateway.config['privileged']['allow_from'] = ['+15555556789']
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)
        await self.request(PASSWORD + ' reset vpn')
        self.assertFalse(self.gateway.pending)
        self.fsm.sms_send.assert_not_awaited()

    async def test_plus_variants_share_rate_limit_identity(self):
        self.gateway.config['rate_limit'] = 1
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)
        await self.request(PASSWORD + ' show uptime', NUMBER)
        await self.request(PASSWORD + ' show uptime', NUMBER[1:])
        self.runner.assert_awaited_once()
        self.assertEqual(list(self.gateway.rates), [NUMBER[1:]])

    async def test_country_prefix_collision_cannot_authorize(self):
        await self.request(PASSWORD + ' show uptime', '+25555551234')
        self.runner.assert_not_awaited()
        self.fsm.sms_send.assert_not_awaited()

    async def test_confirmation_ownership_keeps_full_international_number(self):
        self.enable_action()
        other = '+25555551234'
        self.gateway.config['allow_from'].append(other)
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)
        await self.request(PASSWORD + ' reset vpn')
        nonce = self.pending_for().nonce
        await self.request(PASSWORD + ' CONFIRM ' + nonce, other)
        self.runner.assert_not_awaited()
        self.assertIn((sms.normalize_number(NUMBER), nonce), self.gateway.pending)
        await self.request(PASSWORD + ' CONFIRM ' + nonce, NUMBER[1:])
        self.runner.assert_awaited_once()

    async def test_rate_limit_counts_confirmation(self):
        self.enable_action()
        self.gateway.config['rate_limit'] = 2
        self.fsm.config['sms_command'] = copy.deepcopy(self.gateway.config)
        await self.request(PASSWORD + ' reset vpn')
        nonce = self.pending_for().nonce
        await self.request(PASSWORD + ' CONFIRM ' + nonce)
        await self.request(PASSWORD + ' show version')
        self.assertEqual(self.runner.await_count, 1)

    async def test_config_removal_and_disarm_prevent_execution(self):
        self.fsm.config = {}
        await self.request(PASSWORD + ' show version')
        self.fsm.config = {'sms_command': settings()}
        self.fsm._sms_command_armed = False
        await self.request(PASSWORD + ' show version')
        self.runner.assert_not_awaited()

    async def test_password_rotation_during_authentication(self):
        async def rotate(*_args, **_kwargs):
            self.fsm.config = {'sms_command': settings(password_hash='changed')}
            return True

        with patch.object(asyncio, 'to_thread', rotate):
            await self.request(PASSWORD + ' show version')
        self.runner.assert_not_awaited()

    async def test_started_action_survives_disconnect_and_allows_parallel_requests(
        self,
    ):
        self.enable_action()
        await self.request(PASSWORD + ' reset vpn')
        nonce = self.pending_for().nonce
        started, release = asyncio.Event(), asyncio.Event()

        async def action(argv, _timeout, **_kwargs):
            if argv[1] == 'show':
                return 'parallel result'
            started.set()
            await release.wait()
            return 'done'

        self.runner.side_effect = action
        self.gateway.submit(NUMBER, PASSWORD + ' CONFIRM ' + nonce)
        await started.wait()
        task = next(iter(self.gateway.executing_tasks))
        self.gateway.close()
        replacement = sms.Gateway(self.fsm, self.fsm.config['sms_command'])
        replacement.submit(NUMBER, PASSWORD + ' show version')
        await asyncio.gather(*replacement.tasks)
        self.assertEqual(self.runner.await_count, 2)
        release.set()
        await task
        self.assertFalse(task.cancelled())
        self.assertFalse(self.gateway.executing_tasks)

    async def test_reply_timeout_is_logged_without_retry(self):
        self.fsm.sms_send.side_effect = TimeoutError()
        with self.assertLogs(sms.logger, level='WARNING') as logs:
            await self.request(PASSWORD + ' show version')
        self.assertIn('error=TimeoutError', logs.output[0])
        self.assertEqual(self.fsm.sms_send.await_count, 1)

    async def test_reply_log_contains_destination_and_exact_bounded_response(self):
        self.runner.return_value = 'Hostname: igos\nQuoted "value"\n' + '界' * 1000
        with self.assertLogs(sms.logger, level='INFO') as logs:
            await self.request(PASSWORD + ' show version')
        entries = [line for line in logs.output if 'SMS reply sending' in line]
        self.assertEqual(len(entries), 1)
        self.assertIn('number=' + NUMBER, entries[0])
        payload = self.fsm.sms_send.call_args.args[1]
        self.assertTrue(payload.endswith('...'))
        encoded = entries[0].split('response=', 1)[1]
        self.assertEqual(json.loads(encoded), payload)
        self.assertNotIn('\n', encoded)
        self.assertNotIn(PASSWORD, '\n'.join(logs.output))
        self.assertTrue(
            any(
                'SMS reply sent' in line and 'number=' + NUMBER in line
                for line in logs.output
            )
        )

    async def test_failed_reply_log_records_attempt_and_destination(self):
        self.runner.return_value = 'reply that could not be sent'
        self.fsm.sms_send.side_effect = RuntimeError('transport exception ' + PASSWORD)
        with self.assertLogs(sms.logger, level='INFO') as logs:
            await self.request(PASSWORD + ' show uptime')
        self.assertTrue(
            any(
                'response=' in line and self.runner.return_value in line
                for line in logs.output
            )
        )
        self.assertTrue(
            any(
                'reply delivery failed' in line and 'number=' + NUMBER in line
                for line in logs.output
            )
        )
        self.assertFalse(any('SMS reply sent' in line for line in logs.output))
        self.assertNotIn(PASSWORD, '\n'.join(logs.output))

    async def test_send_failure_no_retry(self):
        self.fsm.sms_send.side_effect = RuntimeError('send failed')
        await self.request(PASSWORD + ' show version')
        self.assertEqual(self.fsm.sms_send.await_count, 1)

    async def test_own_number_dropped(self):
        self.fsm._sms_command_own_numbers = [NUMBER]
        await self.request(PASSWORD + ' show version')
        self.runner.assert_not_awaited()

    async def test_unconfigured_submit_does_not_load_catalog(self):
        self.fsm.config = {}
        with patch.object(sms, 'load_catalog') as loader:
            sms.submit(self.fsm, NUMBER, PASSWORD + ' show version')
            loader.assert_not_called()
        self.assertFalse(hasattr(self.fsm, '_sms_command_gateway'))


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stderr_and_nonzero_exit(self):
        out = await sms.run_command(
            [
                sys.executable,
                '-c',
                'import sys; print("bad command", file=sys.stderr); sys.exit(2)',
            ],
            2,
        )
        self.assertIn('bad command', out)

    async def test_large_output_is_bounded(self):
        out = await sms.run_command([sys.executable, '-c', 'print("a" * 1000000)'], 2)
        self.assertLess(len(out), 4 * sms.GSM_SEGMENT_SIZE * 4 + 20)
        self.assertIn('...', out)

    async def test_output_retention_follows_configured_reply_budget(self):
        output = await sms.run_command(
            [sys.executable, '-c', 'print("x" * 20000)'],
            2,
            max_reply_segments=100,
        )
        self.assertEqual(len(output), 20001)
        self.assertNotIn('...', output)

    async def test_timeout_kills_descendants_and_loop_stays_responsive(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'child-ran'
            code = (
                'import os,time,pathlib; pid=os.fork(); '
                f'time.sleep(1 if pid == 0 else 10); pathlib.Path({str(marker)!r}).write_text("bad")'
            )
            ticks = 0

            async def ticker():
                nonlocal ticks
                for _ in range(10):
                    ticks += 1
                    await asyncio.sleep(0.02)

            out, _ = await asyncio.gather(
                sms.run_command([sys.executable, '-c', code], 0.15), ticker()
            )
            self.assertIn('timed out', out)
            self.assertGreaterEqual(ticks, 10)
            await asyncio.sleep(1)
            self.assertFalse(marker.exists())

    async def test_cancellation_kills_process(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ran'
            code = f'import time,pathlib; time.sleep(.7); pathlib.Path({str(marker)!r}).touch()'
            task = asyncio.create_task(
                sms.run_command([sys.executable, '-c', code], 10)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.8)
            self.assertFalse(marker.exists())


class SmsHostIntegrationTests(unittest.TestCase):
    def setUp(self):
        namespace = {
            'json': json,
            'os': __import__('os'),
            'SMS_MAX_MESSAGES': 100,
            'logger': logging.getLogger('sms-tests'),
        }
        cls = extracted_class(
            'python/vyos/utils/wwan/interfaces_wwan_state_machine.py',
            'ModemStateMachine',
            {
                '_sms_load',
                '_sms_save',
                '_sms_append',
                '_cleanup_sms_listener',
                'sms_send',
                'sms_list',
                'sms_read',
                'sms_delete',
                'sms_delete_all',
            },
            namespace,
        )
        self.host = sms.with_sms_commands(cls)()
        self.host.config = {}
        self.host.interface_number = 0
        self.host.modem_path = '/modem/0'
        self.host._sms_messaging_iface = None
        self.host._sms_added_handler = None
        self.host._sms_listener_bound_path = None
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'sms.json'
        self.host._sms_storage_path = lambda: str(self.path)

    def test_command_class_inherits_original_send_and_public_history_methods(self):
        base = type(self.host).__mro__[2]
        for name in (
            'sms_send',
            'sms_list',
            'sms_read',
            'sms_delete',
            'sms_delete_all',
        ):
            self.assertIs(getattr(type(self.host), name), getattr(base, name))
        self.assertNotIn('sms_send', sms.SmsCommandRuntime.__dict__)
        self.assertNotIn('_sms_load', sms.SmsCommandRuntime.__dict__)
        self.assertNotIn('_sms_save', sms.SmsCommandRuntime.__dict__)

    def test_unconfigured_factory_preserves_original_transition_and_config_calls(self):
        class LegacyHost:
            def __init__(self):
                self.config = {}

            def transition(self, event):
                return ('original transition', event)

            def apply_config(self, config):
                self.config = config
                return 'original configuration'

        host = sms.with_sms_commands(LegacyHost)()
        self.assertEqual(host.transition('event'), ('original transition', 'event'))
        self.assertEqual(
            host.apply_config({'ordinary': True}), 'original configuration'
        )
        self.assertEqual(host.config, {'ordinary': True})
        self.assertFalse(hasattr(host, '_sms_command_runtime'))

    def test_unconfigured_sms_uses_original_storage_without_creating_runtime(self):
        records = [{'id': 1, 'direction': 'incoming', 'text': 'TEST USER'}]
        with patch.object(sms, 'runtime_for') as runtime:
            self.host._sms_save(copy.deepcopy(records))
            self.assertEqual(self.host._sms_load(), records)
            runtime.assert_not_called()
        self.assertFalse(hasattr(self.host, '_sms_command_runtime'))

    def test_configured_storage_delegates_and_masks_without_replacing_host_methods(
        self,
    ):
        records = [
            {'id': 1, 'direction': 'incoming', 'text': PASSWORD + ' show uptime'}
        ]
        self.path.write_text(json.dumps(records))
        self.host.config = {'sms_command': settings()}
        original = self.host._sms_load.__func__
        self.assertEqual(self.host._sms_load()[0]['text'], '******** show uptime')
        runtime = self.host._sms_command_runtime
        self.assertIsInstance(runtime, sms.SmsCommandRuntime)
        self.assertIs(runtime.host, self.host)
        self.assertIs(self.host._sms_load.__func__, original)
        self.assertNotIn(PASSWORD, self.path.read_text())

    def test_disabling_gateway_closes_command_state_and_restores_original_storage(self):
        self.host.config = {'sms_command': settings()}
        self.host._sms_save([])
        gateway = self.host._sms_command_gateway = Mock()
        sms.configuration_changed(self.host, {})
        gateway.close.assert_called_once()
        self.assertFalse(hasattr(self.host, '_sms_command_runtime'))
        self.host.config = {}
        records = [{'id': 1, 'direction': 'incoming', 'text': 'TEST USER'}]
        self.host._sms_save(records)
        self.assertEqual(self.host._sms_load(), records)


class SmsLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        namespace = {
            'asyncio': asyncio,
            'logging': logging,
            'time': time,
            'Variant': lambda _signature, value: SimpleNamespace(value=value),
            'datetime': datetime,
            'os': __import__('os'),
            'json': json,
            'SMS_MAX_MESSAGES': 100,
            'MODEM_MANAGER_SERVICE': 'mm',
            'SMS_INTERFACE': 'sms',
            'MESSAGING_INTERFACE': 'messaging',
            'MODEM_INTERFACE': 'modem',
            'logger': logging.getLogger('sms-tests'),
            'submit': lambda *args: sms.submit(*args),
            'mask_password': sms.mask_password,
            'PASSWORD_MASK': sms.PASSWORD_MASK,
            'SMS_RECEIVE_ATTEMPTS': 3,
            'SMS_RETRY_BACKOFF': 0.5,
            'SMS_STATE_RECEIVING': 2,
            'SMS_STATE_RECEIVED': 3,
        }
        from vyos.utils.wwan.wwan_configuration import ConfigurationLoader

        states = SimpleNamespace(
            CONNECTED=SimpleNamespace(value='CONNECTED'),
            REGISTERED_IDLE=SimpleNamespace(value='REGISTERED_IDLE'),
        )
        namespace.update(
            ModemState=states, reconfigure_logging=Mock(return_value='journal')
        )
        self.config_loader = ConfigurationLoader(0)
        base = extracted_class(
            'python/vyos/utils/wwan/interfaces_wwan_state_machine.py',
            'ModemStateMachine',
            {
                '_apply_parsed_configuration',
                '_sms_load',
                '_sms_save',
                '_sms_append',
                'sms_send',
                'sms_list',
                'sms_read',
                'sms_delete',
                'sms_delete_all',
                '_handle_incoming_sms',
                '_setup_sms_listener',
                '_cleanup_sms_listener',
                '_drain_existing_sms',
            },
            namespace,
        )
        cls = sms.with_sms_commands(base)
        protocol = SimpleNamespace(
            MODEM_MANAGER_SERVICE='mm',
            SMS_INTERFACE='sms',
            MESSAGING_INTERFACE='messaging',
            MODEM_INTERFACE='modem',
        )
        patcher = patch.multiple(
            sms,
            logger=namespace['logger'],
            _modem_constants=lambda: protocol,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fsm = cls()
        self.fsm._sms_storage_path = lambda: str(Path(self.directory.name) / 'sms.json')
        self.fsm.config = {'sms_command': settings()}
        self.fsm.interface_number = 0
        self.fsm.modem_path = '/modem/0'
        self.fsm._sms_command_armed = True
        self.fsm._sms_command_generation = 'generation'
        self.fsm._sms_command_armed_at = datetime.datetime.now(datetime.UTC).replace(
            microsecond=0
        )
        self.fsm._sms_listener_bound_path = None
        self.fsm._sms_messaging_iface = None
        self.fsm._sms_added_handler = None
        self.values = {
            'Number': NUMBER,
            'Text': PASSWORD + ' show version',
            'Timestamp': datetime.datetime.now(datetime.UTC).isoformat(),
            'State': 3,
            'Messages': [],
            'OwnNumbers': [],
        }
        self.props = Mock()
        self.props.call_get = AsyncMock(
            side_effect=lambda iface, key: SimpleNamespace(value=self.values[key])
        )
        self.messaging = Mock(
            call_delete=AsyncMock(), call_create=AsyncMock(return_value='/sms/outgoing')
        )
        self.sms_iface = Mock(call_send=AsyncMock())
        self.proxy = Mock()
        self.proxy.get_interface.side_effect = lambda name: (
            self.messaging
            if name == 'messaging'
            else self.sms_iface
            if name == 'sms'
            else self.props
        )
        self.fsm.bus = SimpleNamespace(
            introspect=AsyncMock(), get_proxy_object=Mock(return_value=self.proxy)
        )
        self.tasks = []

        def create(coro, name=None):
            task = asyncio.create_task(coro, name=name)
            self.tasks.append(task)
            return task

        self.fsm._safe_create_task = create
        self.submit = Mock()
        patcher = patch.object(sms, 'submit', self.submit)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.fsm._cleanup_sms_listener()

    async def receive(self, path='/sms/1', **kwargs):
        return await self.fsm._handle_incoming_sms(
            path, generation='generation', **kwargs
        )

    async def test_sms_configuration_hook_preserves_unrelated_host_configuration(self):
        gateway = self.fsm._sms_command_gateway = Mock()
        current = copy.deepcopy(self.fsm.config)
        sms.configuration_changed(self.fsm, {})
        gateway.close.assert_called_once()
        self.assertFalse(self.fsm._sms_command_armed)
        self.assertNotEqual(self.fsm._sms_command_generation, 'generation')
        self.assertEqual(self.fsm.config, current)
        self.assertFalse(hasattr(self.fsm, '_sms_command_gateway'))

    async def test_unchanged_gateway_configuration_keeps_listener_and_pending_work(
        self,
    ):
        gateway = self.fsm._sms_command_gateway = Mock()
        self.fsm._sms_messaging_iface = self.messaging
        sms.configuration_changed(self.fsm, copy.deepcopy(self.fsm.config))
        gateway.close.assert_not_called()
        self.assertTrue(self.fsm._sms_command_armed)
        self.assertIs(self.fsm._sms_messaging_iface, self.messaging)

    async def test_command_replies_reuse_original_sender_and_cleanup_order(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_delete(_path):
            started.set()
            await release.wait()

        self.messaging.call_delete.side_effect = slow_delete
        task = asyncio.create_task(self.fsm.sms_send(NUMBER, 'test reply'))
        await asyncio.wait_for(started.wait(), 5)
        self.assertFalse(task.done())
        self.sms_iface.call_send.assert_awaited_once()
        release.set()
        result = await task
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(self.fsm.bus.introspect.await_count, 2)
        self.assertEqual(self.fsm._sms_load()[-1]['text'], 'test reply')

    async def test_live_complete_claim_saved_before_dispatch_and_password_masked(self):
        def check(*args):
            records = self.fsm._sms_load()
            self.assertTrue(records[0]['command_consumed'])
            self.assertEqual(records[0]['text'], '******** show version')

        self.submit.side_effect = check
        await self.receive(command_eligible=True)
        self.submit.assert_called_once()
        self.messaging.call_delete.assert_awaited_once()
        await self.receive(command_eligible=True)
        self.submit.assert_called_once()

    async def test_drain_never_executes_and_masks_password(self):
        await self.receive(drained=True)
        self.submit.assert_not_called()
        self.assertTrue(self.fsm._sms_load()[0]['drained'])
        self.assertEqual(self.fsm._sms_load()[0]['text'], '******** show version')

    async def test_configured_history_masks_ordinary_and_unauthorized_sms(self):
        for index, (number, text) in enumerate(
            (
                (NUMBER, 'Hello from my phone'),
                ('+447911123456', 'Carrier or unauthorized message'),
                (NUMBER, 'WrongPassword show version'),
            )
        ):
            self.values.update(Number=number, Text=text)
            await self.receive(path=f'/sms/plain-{index}', command_eligible=True)
            record = self.fsm._sms_load()[-1]
            self.assertEqual(record['text'], '******** ' + text.split(None, 1)[1])
            self.assertEqual(record['number'], number)

    async def test_prearm_signal_cannot_become_live(self):
        await self.receive(command_eligible=False)
        self.submit.assert_not_called()
        self.assertTrue(self.fsm._sms_load()[0]['drained'])

    async def test_partial_completion_updates_same_record(self):
        self.values['State'] = 2
        self.values['Text'] = PASSWORD + ' show'
        await self.receive(command_eligible=True)
        self.submit.assert_not_called()
        self.messaging.call_delete.assert_not_awaited()
        handler = self.props.on_properties_changed.call_args.args[0]
        self.values.update(State=3, Text=PASSWORD + ' show version')
        handler('sms', {'State': SimpleNamespace(value=3)}, [])
        await asyncio.gather(*self.tasks)
        self.submit.assert_called_once_with(
            self.fsm, NUMBER, PASSWORD + ' show version'
        )
        self.assertEqual(len(self.fsm._sms_load()), 1)
        self.assertEqual(self.fsm._sms_load()[0]['status'], 'received')

    async def test_completion_between_property_reads_never_executes_partial_body(self):
        self.values['State'] = 2
        original = self.props.call_get.side_effect

        async def assemble(interface, key):
            if key == 'Text' and self.values['State'] == 2:
                self.values['State'] = 3
                self.values['Text'] = PASSWORD + ' show version'
                return SimpleNamespace(value=PASSWORD + ' show')
            return original(interface, key)

        self.props.call_get.side_effect = assemble
        await self.receive(command_eligible=True)
        await asyncio.gather(*self.tasks)
        self.submit.assert_called_once_with(
            self.fsm, NUMBER, PASSWORD + ' show version'
        )

    async def test_drained_partial_stays_stale_on_completion(self):
        self.values['State'] = 2
        await self.receive(drained=True)
        handler = self.props.on_properties_changed.call_args.args[0]
        self.values['State'] = 3
        handler('sms', {'State': SimpleNamespace(value=3)}, [])
        await asyncio.gather(*self.tasks)
        self.submit.assert_not_called()

    async def test_old_or_invalid_timestamp_cannot_execute(self):
        for index, stamp in enumerate(('2000-01-01T00:00:00+00:00', '', 'invalid')):
            self.values['Timestamp'] = stamp
            await self.receive(path=f'/sms/{index}', command_eligible=True)
        self.submit.assert_not_called()

    async def test_reused_modem_path_new_generation_is_new_record(self):
        await self.receive(drained=True)
        self.fsm._sms_command_generation = 'next'
        await self.fsm._handle_incoming_sms(
            '/sms/1', generation='next', command_eligible=True
        )
        self.assertEqual(len(self.fsm._sms_load()), 2)
        self.submit.assert_called_once()

    async def test_unconfigured_keeps_normal_sms_body_no_gateway(self):
        self.fsm.config = {}
        await self.fsm._handle_incoming_sms('/sms/1')
        self.assertEqual(self.fsm._sms_load()[0]['text'], self.values['Text'])
        self.submit.assert_not_called()

    async def test_failed_persistence_prevents_execution(self):
        self.fsm._sms_save = Mock(side_effect=OSError('disk full'))
        await self.receive(command_eligible=True)
        self.submit.assert_not_called()
        self.messaging.call_delete.assert_not_awaited()

    async def test_listener_arms_after_drain_and_reconnect_drains_again(self):
        self.values['Messages'] = ['/sms/1']
        await self.fsm._setup_sms_listener()
        self.assertTrue(self.fsm._sms_command_armed)
        self.assertTrue(self.fsm._sms_load()[0]['drained'])
        self.submit.assert_not_called()
        generation = self.fsm._sms_command_generation
        self.fsm._cleanup_sms_listener()
        await self.fsm._setup_sms_listener()
        self.assertNotEqual(self.fsm._sms_command_generation, generation)
        self.assertEqual(len(self.fsm._sms_load()), 2)
        self.submit.assert_not_called()

    async def test_password_absent_from_storage_and_receive_logs(self):
        with self.assertLogs('sms-tests', level='DEBUG') as captured:
            await self.receive(command_eligible=True)
        self.assertNotIn(PASSWORD, Path(self.fsm._sms_storage_path()).read_text())
        self.assertNotIn(PASSWORD, '\n'.join(captured.output))
        self.submit.assert_called_once_with(self.fsm, NUMBER, self.values['Text'])

    async def test_incoming_log_shows_sender_and_masked_body(self):
        self.values['Text'] = PASSWORD + ' show "uptime"\nsecond line'
        with self.assertLogs('sms-tests', level='INFO') as logs:
            await self.receive(command_eligible=True)
        entry = next(line for line in logs.output if 'Incoming SMS interface=' in line)
        self.assertIn('number=' + NUMBER, entry)
        self.assertEqual(
            json.loads(entry.split('message=', 1)[1]),
            '******** show "uptime"\nsecond line',
        )
        self.assertNotIn(PASSWORD, '\n'.join(logs.output))

    async def test_unconfigured_receive_preserves_original_log_without_message_body(
        self,
    ):
        self.fsm.config = {}
        with self.assertLogs('sms-tests', level='INFO') as logs:
            await self.receive()
        self.assertTrue(
            any('Incoming SMS from ' + NUMBER in line for line in logs.output)
        )
        self.assertNotIn(PASSWORD, '\n'.join(logs.output))

    async def test_outgoing_history_preserves_command_output(self):
        text = 'Hostname: igos hash ' + HASH
        self.fsm._sms_save([{'id': 1, 'direction': 'outgoing', 'text': text}])
        self.assertEqual(self.fsm._sms_load()[0]['text'], text)

    async def test_legacy_stored_password_is_redacted_on_read(self):
        Path(self.fsm._sms_storage_path()).write_text(
            json.dumps(
                [{'id': 1, 'direction': 'incoming', 'text': PASSWORD + ' show version'}]
            )
        )
        self.assertEqual(self.fsm._sms_load()[0]['text'], '******** show version')
        self.assertNotIn(PASSWORD, Path(self.fsm._sms_storage_path()).read_text())

    async def test_redaction_writeback_failure_keeps_history_and_later_append(self):
        path = Path(self.fsm._sms_storage_path())
        original = [
            {'id': 1, 'direction': 'incoming', 'text': 'Hello'},
            {'id': 2, 'direction': 'incoming', 'text': PASSWORD + ' show uptime'},
            {'id': 3, 'direction': 'incoming', 'text': 'Test message'},
        ]
        path.write_text(json.dumps(original))
        save = self.fsm._sms_save
        self.fsm._sms_save = Mock(side_effect=OSError('disk full ' + PASSWORD))
        with self.assertLogs('sms-tests', level='WARNING') as logs:
            loaded = self.fsm._sms_load()
        self.assertEqual([m['id'] for m in loaded], [1, 2, 3])
        self.assertEqual(loaded[1]['text'], '******** show uptime')
        self.assertNotIn(PASSWORD, json.dumps(loaded))
        self.assertNotIn(PASSWORD, '\n'.join(logs.output))
        self.assertEqual(json.loads(path.read_text()), original)

        # Even if migration still fails, a subsequent successful append must
        # retain all older records instead of building from an empty history.
        attempts = 0

        def recover(records):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError('migration write failed')
            save(records)

        self.fsm._sms_save = recover
        with self.assertLogs('sms-tests', level='WARNING'):
            self.fsm._sms_append(
                {'id': 4, 'direction': 'incoming', 'text': 'New message'}
            )
        persisted = json.loads(path.read_text())
        self.assertEqual([m['id'] for m in persisted], [1, 2, 3, 4])
        self.assertNotIn(PASSWORD, path.read_text())

    async def test_transient_setup_failure_reattaches_without_polling(self):
        failed = False

        async def introspect(*_args):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError('temporary')

        self.fsm.bus.introspect.side_effect = introspect
        await self.fsm._setup_sms_listener()
        self.assertEqual(self.fsm._sms_listener_bound_path, '/modem/0')
        self.assertTrue(self.fsm._sms_command_armed)
        self.assertEqual(self.messaging.on_added.call_count, 2)

    async def test_transient_save_failure_retains_then_imports_message(self):
        save = self.fsm._sms_save
        failed = False

        def persist(records):
            nonlocal failed
            if not failed:
                failed = True
                self.messaging.call_delete.assert_not_awaited()
                raise OSError('temporary storage error')
            save(records)

        self.fsm._sms_save = persist
        self.assertTrue(await self.receive(command_eligible=True))
        self.assertEqual(len(self.fsm._sms_load()), 1)
        self.submit.assert_called_once()
        self.messaging.call_delete.assert_awaited_once()

    async def test_transient_read_failure_retries_same_notification(self):
        original = self.props.call_get.side_effect
        failed = False

        def read(interface, key):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError('temporary D-Bus failure')
            return original(interface, key)

        self.props.call_get.side_effect = read
        self.assertTrue(await self.receive(command_eligible=True))
        self.assertEqual(len(self.fsm._sms_load()), 1)
        self.submit.assert_called_once()

    async def test_overlapping_notifications_store_and_execute_once(self):
        await asyncio.gather(*(self.receive(command_eligible=True) for _ in range(3)))
        self.assertEqual(len(self.fsm._sms_load()), 1)
        self.submit.assert_called_once()

    async def test_old_callback_after_reattach_is_ignored(self):
        await self.fsm._setup_sms_listener()
        old_callback = self.fsm._sms_added_handler
        self.fsm._cleanup_sms_listener()
        await self.fsm._setup_sms_listener()
        old_callback('/sms/stale', True)
        self.assertFalse(self.tasks)
        self.submit.assert_not_called()

    async def test_rearming_existing_subscription_drops_old_generation_guard(self):
        await self.fsm._setup_sms_listener()
        old_generation = self.fsm._sms_command_generation
        self.fsm._sms_command_armed = False
        await self.fsm._setup_sms_listener()
        self.assertNotEqual(self.fsm._sms_command_generation, old_generation)
        self.fsm._sms_added_handler('/sms/rearmed', True)
        await asyncio.sleep(0)
        self.submit.assert_called_once()

    async def test_generation_change_during_read_cannot_save_or_delete(self):
        original = self.props.call_get.side_effect

        def read(interface, key):
            if key == 'Text':
                self.fsm._sms_command_generation = 'replacement'
            return original(interface, key)

        self.props.call_get.side_effect = read
        self.assertFalse(await self.receive(command_eligible=True))
        self.assertFalse(self.fsm._sms_load())
        self.submit.assert_not_called()
        self.messaging.call_delete.assert_not_awaited()

    async def test_plain_sms_listener_has_attachment_identity(self):
        self.fsm.config = {}
        await self.fsm._setup_sms_listener()
        self.assertTrue(self.fsm._sms_command_generation)
        self.fsm._sms_added_handler('/sms/plain', True)
        await asyncio.sleep(0)
        await asyncio.gather(*self.tasks)
        self.assertEqual(len(self.fsm._sms_load()), 1)
        self.submit.assert_not_called()

    async def test_configuration_reload_preserves_listener_handles(self):
        await self.fsm._setup_sms_listener()
        proxy = self.fsm._sms_messaging_iface
        callback = self.fsm._sms_added_handler
        self.fsm._passthrough = Mock()
        self.fsm._bridging_radvd = Mock()
        self.fsm.parsed_config = self.config_loader.load_configuration({})
        self.fsm._apply_parsed_configuration()
        self.assertIs(self.fsm._sms_messaging_iface, proxy)
        self.assertIs(self.fsm._sms_added_handler, callback)
        self.assertEqual(self.fsm._sms_listener_bound_path, '/modem/0')
        self.fsm._cleanup_sms_listener()
        proxy.off_added.assert_called_with(callback)

    async def test_commit_transition_rearms_with_new_privileged_settings(self):
        await self.fsm._setup_sms_listener()
        previous = self.fsm._sms_command_generation
        self.fsm.config['sms_command']['privileged'] = {'verb': ['reboot']}
        sms.runtime_for(self.fsm).state_changed('CONFIGURING')
        self.assertFalse(self.fsm._sms_command_armed)
        sms.runtime_for(self.fsm).state_changed('CONNECTING')
        sms.runtime_for(self.fsm).state_changed('CONNECTED')
        await asyncio.gather(*self.tasks)
        self.assertTrue(self.fsm._sms_command_armed)
        self.assertNotEqual(self.fsm._sms_command_generation, previous)
        self.assertEqual(self.messaging.on_added.call_count, 4)
        self.assertEqual(self.messaging.off_added.call_count, 3)
        await self.fsm._setup_sms_listener()
        self.assertEqual(self.messaging.on_added.call_count, 4)

    async def test_registered_without_bearer_arms_gateway(self):
        self.fsm._sms_command_armed = False
        sms.runtime_for(self.fsm).state_changed('REGISTERED_IDLE')
        await asyncio.gather(*self.tasks)
        self.assertTrue(self.fsm._sms_command_armed)

    async def test_leaving_ready_state_cancels_inflight_setup(self):
        started = asyncio.Event()

        async def wait_for_modem():
            started.set()
            await asyncio.Event().wait()

        self.fsm._setup_sms_listener = wait_for_modem
        sms.runtime_for(self.fsm).state_changed('CONNECTED')
        await started.wait()
        task = sms.runtime_for(self.fsm).setup_task
        sms.runtime_for(self.fsm).state_changed('DISCONNECTED')
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertFalse(self.fsm._sms_command_armed)

    async def test_failed_drain_never_arms(self):
        self.fsm._drain_existing_sms = AsyncMock(return_value=False)
        await self.fsm._setup_sms_listener()
        self.assertFalse(self.fsm._sms_command_armed)

    async def test_cleanup_disarms_and_invalidates_pending(self):
        gateway = self.fsm._sms_command_gateway = Mock()
        self.fsm._cleanup_sms_listener()
        gateway.close.assert_called_once()
        self.assertFalse(self.fsm._sms_command_armed)
        self.assertNotEqual(self.fsm._sms_command_generation, 'generation')


class ConfModeTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'src/conf_mode/interfaces_wwan.py'
        tree = ast.parse(path.read_text())
        names = {
            'get_config',
            'generate',
            'build_fsm_config',
            '_leaf',
            '_leaf_int',
            '_leaf_exists',
            '_csv_to_list',
            '_build_ipv6_bridging',
            '_build_ipv6_management_address',
            '_build_ip_passthrough',
        }
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        self.namespace = {
            '_dial_on_demand_active': Mock(return_value=False),
            '_detect_passthrough_conflicts': Mock(return_value=[]),
            '_detect_ipv6_bridging_conflicts': Mock(return_value=[]),
        }
        module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
        exec(compile(module, str(path), 'exec'), self.namespace)  # noqa: S102 - trusted repository source

    def read_config(self, explicit, sms_config, policy=None):
        conf = Mock()
        policy_path = ['system', 'login', 'enhanced-policy', 'password']
        conf.exists.side_effect = lambda path: (
            explicit
            if path[-1] == 'sms-command'
            else policy is not None and path == policy_path
        )
        conf.get_config_dict.return_value = policy
        self.namespace['get_interface_dict'] = Mock(
            return_value=('wwan0', {'ifname': 'wwan0', 'sms_command': sms_config})
        )
        return self.namespace['get_config'](conf)

    def test_recursive_defaults_do_not_activate_gateway(self):
        wwan = self.read_config(False, {'timeout': 20, 'rate_limit': 5})
        self.assertNotIn('sms_command', wwan)
        with patch.object(sms, 'load_catalog') as loader:
            output = self.namespace['build_fsm_config'](wwan)
        self.assertNotIn('sms_command', output)
        loader.assert_not_called()

    def test_sms_reads_actual_login_policy_path(self):
        policy = {'minimum_length': '14', 'minimum_special': '2'}
        self.assertEqual(
            self.read_config(True, settings(), policy)['_sms_password_policy'], policy
        )
        self.assertEqual(
            self.read_config(True, settings(), {})['_sms_password_policy'], {}
        )
        self.assertIsNone(self.read_config(True, settings())['_sms_password_policy'])

    def test_explicit_gateway_forwarded(self):
        wwan = self.read_config(True, settings())
        output = self.namespace['build_fsm_config'](wwan)
        self.assertEqual(output['sms_command'], settings())

    def test_generate_writes_hash_and_removes_password(self):
        wwan = {'ifname': 'wwan0', 'sms_command': settings(password=PASSWORD)}
        with (
            patch('vyos.utils.configfs.add_cli_node') as add,
            patch('vyos.utils.configfs.delete_cli_node') as delete,
        ):
            self.namespace['generate'](wwan)
        base = ['interfaces', 'wwan', 'wwan0', 'sms-command']
        self.assertEqual(add.call_args.args[0], base + ['password-hash'])
        hashed = add.call_args.kwargs['value']
        self.assertTrue(auth.verify_password(PASSWORD, hashed))
        delete.assert_called_once_with(base + ['password'])
        self.assertNotIn('password', wwan['sms_command'])
        self.assertEqual(wwan['sms_command']['password_hash'], hashed)

    def test_deleted_interface_does_not_generate_password(self):
        wwan = {'deleted': {}, 'sms_command': settings(password=PASSWORD)}
        with patch.object(auth, 'hash_password') as hasher:
            self.namespace['generate'](wwan)
            hasher.assert_not_called()


class DbusConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_restore_calls_coroutine_not_dbus_decorator(self):
        cls = extracted_class(
            'python/vyos/utils/wwan/interfaces_wwan_config.py',
            'InterfaceConfig',
            {'_apply_restored_config'},
            {'asyncio': asyncio, 'logger': logging.getLogger('sms-tests')},
        )
        obj = cls()
        obj.interface_number = 0
        obj._restored_runtime_state = {}
        obj._set_configuration = AsyncMock()
        obj.set_configuration = Mock(return_value=None)
        obj._quarantine_cache = Mock()
        configuration = {'sms_command': settings(privileged={'verb': ['reboot']})}
        with patch.object(asyncio, 'sleep', AsyncMock()):
            await obj._apply_restored_config(configuration)
        obj._set_configuration.assert_awaited_once_with(configuration)
        obj.set_configuration.assert_not_called()
        obj._quarantine_cache.assert_not_called()

    async def test_sms_key_is_forwarded_and_omission_removes_it(self):
        cls = extracted_class(
            'python/vyos/utils/wwan/interfaces_wwan_config.py',
            'InterfaceConfig',
            {'set_configuration', '_set_configuration'},
            {'logger': logging.getLogger('sms-tests'), 'DBusError': RuntimeError},
        )
        obj = cls()
        obj.DEFAULT_CONFIG = {}
        obj.interface_number = 0
        obj.fsm = SimpleNamespace(config={}, apply_config=Mock())
        obj._extract_variant_value = lambda value: value
        obj._validate_configuration = Mock()
        obj._save_configuration = Mock()
        await obj.set_configuration({'sms_command': settings()})
        first = obj.fsm.apply_config.call_args.args[0]
        self.assertIn('sms_command', first)
        obj.fsm.config = first
        await obj.set_configuration({})
        self.assertNotIn('sms_command', obj.fsm.apply_config.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
