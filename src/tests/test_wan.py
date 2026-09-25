# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Portable WAN tests; no real WAN traffic or VyOS package initialization."""

import errno
import importlib.util
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

repository_dir = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'wan_testing_under_test',
    repository_dir / 'python/vyos/utils/wan/wan_testing.py',
)
wan = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wan
spec.loader.exec_module(wan)


def dns_reply(*, query_id=7, flags=0x8180, rcode=0, query_type=1):
    question = (b'\x07example\x03com\x00'
                + wan.struct.pack('!HH', query_type, 1))
    return wan.struct.pack(
        '!HHHHHH', query_id, flags | rcode, 1, 0, 0, 0) + question


class TestInterfaceStatus(unittest.TestCase):
    def test_local_readiness(self):
        link = {'ifname': 'eth0', 'flags': ['UP', 'LOWER_UP'], 'operstate': 'UP',
                'addr_info': [{'family': 'inet', 'scope': 'global', 'local': '192.0.2.2'}]}
        for route, passed in (({'dev': 'eth0'}, True), ({'dev': 'eth1'}, False),
                              ({'dev': 'eth0', 'flags': ['linkdown']}, False)):
            with self.subTest(route=route), patch.object(
                    wan, '_read_ip', side_effect=[[link], [route]]):
                self.assertEqual(wan.check_interface_status('eth0').passed, passed)


class TestProbes(unittest.TestCase):
    @patch.object(wan.subprocess, 'run')
    def test_ping(self, run):
        for code, passed in ((0, True), (1, False), (2, False)):
            run.return_value = subprocess.CompletedProcess([], code)
            self.assertEqual(wan.check_ping('eth0', '1.1.1.1').passed, passed)
        self.assertIn('-I', run.call_args.args[0])
        self.assertIn('eth0', run.call_args.args[0])
        run.side_effect = subprocess.TimeoutExpired('ping', 1)
        self.assertEqual(wan.check_ping('eth0', '::1').reason, 'timeout')

    @patch.object(wan, '_bound_socket')
    def test_tcp(self, bound):
        sock = bound.return_value.__enter__.return_value
        for code, passed in ((0, True), (errno.ECONNREFUSED, True),
                              (errno.ETIMEDOUT, False), (errno.ENETUNREACH, False)):
            sock.connect_ex.return_value = code
            self.assertEqual(wan.check_tcp('eth0', '192.0.2.1', 443).passed, passed)
        sock.connect_ex.assert_called_with(('192.0.2.1', 443))
        bound.side_effect = PermissionError
        self.assertFalse(wan.check_tcp('eth0', '192.0.2.1', 443).passed)

    @patch.object(wan.socket, 'socket')
    def test_binding_and_failure_cleanup(self, constructor):
        with patch.object(wan.socket, 'SO_BINDTODEVICE', 25, create=True):
            wan._bound_socket('eth0', '2001:db8::1', socket.SOCK_STREAM, 2)
            constructor.assert_called_with(socket.AF_INET6, socket.SOCK_STREAM)
            constructor.return_value.setsockopt.assert_called_with(
                socket.SOL_SOCKET, 25, b'eth0\0')
            constructor.return_value.setsockopt.side_effect = PermissionError
            with self.assertRaises(PermissionError):
                wan._bound_socket('eth0', '1.1.1.1', socket.SOCK_STREAM, 2)
            constructor.return_value.close.assert_called_once()

    @patch.object(wan.secrets, 'randbelow', return_value=7)
    @patch.object(wan, '_bound_socket')
    def test_dns_any_valid_response_passes(self, bound, random):
        sock = bound.return_value.__enter__.return_value
        for rcode, reason in ((0, 'response'), (2, 'servfail'),
                              (3, 'nxdomain'), (5, 'refused')):
            sock.recv.return_value = dns_reply(rcode=rcode)
            result = wan.check_dns('eth0', '1.1.1.1', 'example.com')
            self.assertEqual((result.passed, result.reason), (True, reason))
        sock.connect.assert_called_with(('1.1.1.1', 53))
        self.assertTrue(sock.sendall.call_args.args[0].endswith(b'\x00\x01\x00\x01'))

    @patch.object(wan.secrets, 'randbelow', return_value=7)
    @patch.object(wan, '_bound_socket')
    def test_dns_truncation_uses_tcp(self, bound, random):
        udp_context, tcp_context = MagicMock(), MagicMock()
        udp_sock = udp_context.__enter__.return_value
        tcp_sock = tcp_context.__enter__.return_value
        bound.side_effect = [udp_context, tcp_context]
        udp_sock.recv.return_value = dns_reply(flags=0x8380)
        response = dns_reply()
        tcp_sock.recv.side_effect = [wan.struct.pack('!H', len(response)), response]
        self.assertTrue(wan.check_dns('eth0', '1.1.1.1', 'example.com').passed)
        tcp_sock.connect.assert_called_with(('1.1.1.1', 53))
        self.assertEqual(tcp_sock.sendall.call_args.args[0][2:4], b'\x00\x07')

    @patch.object(wan.secrets, 'randbelow', return_value=7)
    @patch.object(wan, '_bound_socket')
    def test_dns_bad_response_and_timeout(self, bound, random):
        sock = bound.return_value.__enter__.return_value
        for response in (b'bad', dns_reply(query_id=8), dns_reply(query_type=28)):
            sock.recv.return_value = response
            result = wan.check_dns('eth0', '1.1.1.1', 'example.com')
            self.assertEqual((result.passed, result.reason), (False, 'check_error'))
        sock.recv.side_effect = socket.timeout
        result = wan.check_dns('eth0', '1.1.1.1', 'example.com')
        self.assertEqual((result.passed, result.reason), (False, 'timeout'))

    @patch.object(wan.subprocess, 'run')
    def test_http(self, run):
        for code in ('200', '301', '404', '503'):
            run.return_value = subprocess.CompletedProcess([], 0, code)
            self.assertTrue(wan.check_http('eth0', 'https://example.com').passed)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['curl', '--disable'])
        self.assertIn('if!eth0', command)
        self.assertIn('--noproxy', command)
        self.assertNotIn('--insecure', command)
        for exit_code, code in ((60, '000'), (7, '000'), (28, '200'), (0, '000')):
            run.return_value = subprocess.CompletedProcess([], exit_code, code)
            self.assertFalse(wan.check_http('eth0', 'https://example.com').passed)

    def test_invalid_config(self):
        for call in (
            lambda: wan.check_ping('eth0', '-bad'),
            lambda: wan.check_ping('eth0', '1.1.1.1', timeout=float('nan')),
            lambda: wan.check_tcp('eth0', '1.1.1.1', 0),
            lambda: wan.check_http('eth0', 'file:///etc/passwd'),
            lambda: wan.check_http('eth0', 'https://user:pass@example.com'),
            lambda: wan.PathTest('https', 'http://example.com').run('eth0'),
            lambda: wan.run_path_tests('eth0', []),
        ):
            with self.assertRaises(ValueError):
                call()


class TestDirectCLI(unittest.TestCase):
    @patch.object(wan, '_monitor_cli', return_value=0)
    def test_two_targets_and_monitor_settings(self, monitor):
        self.assertEqual(wan.main([
            'wwan0', '--method', 'ping', '--primary-target', '1.1.1.1',
            '--secondary-target', '8.8.8.8', '--timeout', '5', '--watch',
            '--interval', '20', '--policy', 'all', '--failure-threshold', '4',
            '--recovery-threshold', '3',
        ]), 0)
        args, tests, state = monitor.call_args.args
        self.assertEqual(args.interfaces, ['wwan0'])
        self.assertTrue(args.watch)
        self.assertEqual(args.interval, 20)
        self.assertEqual(tests, [wan.PathTest('ping', target, timeout=5)
                                 for target in ('1.1.1.1', '8.8.8.8')])
        self.assertEqual((state.policy, state.failure_threshold, state.recovery_threshold),
                         ('all', 4, 3))

    @patch.object(wan, '_monitor_cli', return_value=0)
    def test_other_methods(self, monitor):
        cases = [
            ('tcp', '1.1.1.1', ['--port', '443'], wan.PathTest('tcp', '1.1.1.1', port=443)),
            ('dns', '1.1.1.1', ['--name', 'example.com', '--record-type', 'AAAA'],
             wan.PathTest('dns', '1.1.1.1', name='example.com', record_type='AAAA')),
            ('http', 'http://example.com/', [], wan.PathTest('http', 'http://example.com/')),
            ('https', 'https://example.com/', [], wan.PathTest('https', 'https://example.com/')),
        ]
        for method, target, options, expected in cases:
            with self.subTest(method=method):
                wan.main(['wwan0', '--method', method, '--primary-target', target, *options])
                self.assertEqual(monitor.call_args.args[1], [expected])

    @patch.object(wan, '_monitor_cli')
    @patch('sys.stderr')
    def test_invalid_combinations_fail_before_monitoring(self, stderr, monitor):
        base = ['wwan0', '--method', 'ping', '--primary-target', '1.1.1.1']
        cases = [
            ['wwan0', '--method', 'ping'],
            ['wwan0', '--primary-target', '1.1.1.1'],
            ['wwan0', '--watch'],
            ['wwan0', '--method', 'tcp', '--primary-target', '1.1.1.1'],
            ['wwan0', '--method', 'dns', '--primary-target', '1.1.1.1'],
            base + ['--port', '80'], base + ['--name', 'example.com'],
            base + ['--record-type', 'AAAA'], base + ['--timeout', '0'],
            base + ['--secondary-target', 'invalid'], base + ['eth0'],
        ]
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as error:
                wan.main(argv)
            self.assertEqual(error.exception.code, 2)
        monitor.assert_not_called()

    @patch.object(wan, 'check_interface_status')
    @patch('builtins.print')
    def test_status_without_probe_options(self, output, status):
        status.return_value = wan.InterfaceStatus('wwan0', True, 'up')
        with self.assertRaises(SystemExit) as error:
            wan.main(['wwan0'])
        self.assertEqual(error.exception.code, 2)
        status.assert_not_called()

    @patch.object(wan, 'check_interface_status')
    @patch('builtins.print')
    def test_explicit_interface_status_method(self, output, status):
        status.return_value = wan.InterfaceStatus('wwan0', True, 'up')
        self.assertEqual(wan.main(['wwan0', '--method', 'interface-status']), 0)
        status.assert_called_once_with('wwan0', family=4, table='main')


class TestMonitor(unittest.TestCase):
    def results(self, *values):
        return [wan.ProbeResult('eth0', 'ping', str(i), value, '')
                for i, value in enumerate(values)]

    def test_any_target_example(self):
        monitor = wan.PathMonitor(up=True)
        for values in ((False, False), (False, True), (False, True)):
            self.assertTrue(monitor.update(self.results(*values)))
        self.assertEqual(monitor.failures, 0)

    def test_thresholds_and_streak_reset(self):
        monitor = wan.PathMonitor(up=True)
        fail, success = self.results(False), self.results(True)
        self.assertTrue(monitor.update(fail))
        self.assertTrue(monitor.update(fail))
        self.assertFalse(monitor.update(fail))
        self.assertFalse(monitor.update(success))
        self.assertFalse(monitor.update(fail))
        self.assertFalse(monitor.update(success))
        self.assertTrue(monitor.update(success))

    def test_all_policy_and_interface_gate(self):
        monitor = wan.PathMonitor(policy='all', failure_threshold=1)
        self.assertFalse(monitor.update(self.results(True, False)))
        self.assertFalse(monitor.update(self.results(True, True)))
        self.assertTrue(monitor.update(self.results(True, True)))
        self.assertFalse(monitor.update(self.results(True, True), interface_ready=False))

    def test_unknown_initial_state(self):
        monitor = wan.PathMonitor()
        self.assertIsNone(monitor.update(self.results(True)))
        self.assertTrue(monitor.update(self.results(True)))
        with self.assertRaises(ValueError):
            monitor.update([])
        for kwargs in ({'policy': 'none'}, {'failure_threshold': 0}, {'recovery_threshold': 1.5}):
            with self.assertRaises(ValueError):
                wan.PathMonitor(**kwargs)

    def test_mixed_tests(self):
        tests = [MagicMock(), MagicMock()]
        results = wan.run_path_tests('eth0', tests)
        self.assertEqual(results, [test.run.return_value for test in tests])
        for test in tests:
            test.run.assert_called_once_with('eth0')

    def test_route_failover_changes_metrics_on_transitions(self):
        runner = MagicMock()
        failover = wan.RouteFailover('eth0', 'wwan0', primary_gateway='10.10.0.1',
                                     cellular_gateway='10.113.11.9', runner=runner)
        self.assertTrue(failover.apply(True))
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(failover.apply(False))
        self.assertEqual(runner.call_count, 4)
        self.assertFalse(failover.apply(False))
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertIn('10.10.0.1', commands[0])
        self.assertIn('10.113.11.9', commands[1])
        self.assertEqual(commands[2][-2:], ['metric', '220'])
        self.assertEqual(commands[3][-2:], ['metric', '10'])

    @patch.object(wan, '_default_gateway', side_effect=['10.10.0.1', '10.113.11.9'])
    def test_route_failover_discovers_gateways(self, gateways):
        runner = MagicMock()
        failover = wan.RouteFailover('eth0', 'wwan0', runner=runner)
        failover.apply(True)
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertIn('10.10.0.1', commands[0])
        self.assertIn('10.113.11.9', commands[1])

    def test_table_output_for_mixed_results(self):
        results = [
            wan.ProbeResult('wwan0', 'ping', '1.1.1.1', True, 'echo_reply'),
            wan.ProbeResult('wwan0', 'https', 'https://example.com/', False,
                            'request_failed'),
        ]
        output = wan._format_path_test_table(
            'wwan0', 'UP', 'any', 0, 2, results,
            timestamp='2026-09-24 12:34:56')
        lines = output.splitlines()
        self.assertEqual(len(lines), 4)
        for heading in ('TIME', 'INTERFACE', 'STATE', 'POLICY', 'FAILURES',
                        'SUCCESSES', 'METHOD', 'TARGET', 'RESULT', 'REASON'):
            self.assertIn(heading, lines[0])
        self.assertIn('PING', lines[2])
        self.assertIn('1.1.1.1', lines[2])
        self.assertIn('PASS', lines[2])
        self.assertIn('echo_reply', lines[2])
        self.assertIn('HTTPS', lines[3])
        self.assertIn('FAIL', lines[3])
        self.assertIn('request_failed', lines[3])

    @patch.object(wan, 'run_path_tests')
    @patch.object(wan, 'check_interface_status')
    @patch('builtins.print')
    def test_cli_interface_gate_and_one_shot_exit(self, output, status, probes):
        args = SimpleNamespace(interfaces=['eth0'], family=4, table='main',
                               policy='any', watch=False)
        tests = [wan.PathTest('ping', '1.1.1.1')]
        status.return_value = wan.InterfaceStatus('eth0', False, 'link_down')
        self.assertEqual(wan._monitor_cli(args, tests, wan.PathMonitor()), 1)
        probes.assert_not_called()
        table = output.call_args.args[0]
        self.assertIn('INTERFACE', table)
        self.assertIn('eth0', table)
        self.assertIn('FAIL', table)
        self.assertIn('link_down', table)
        status.return_value = wan.InterfaceStatus('eth0', True, 'up')
        probes.return_value = self.results(True)
        self.assertEqual(wan._monitor_cli(args, tests, wan.PathMonitor()), 0)


if __name__ == '__main__':
    unittest.main()
