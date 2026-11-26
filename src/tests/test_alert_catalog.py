"""Alert engine and delivery tests run without systemd, a modem or libvyosconfig."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vyos.alerts.catalog import compile_fields, expand, load_catalog, match_fields
from vyos.alerts.delivery import Delivery
from vyos.alerts.engine import Engine
from vyos.alerts.migration import GUI_SCRIPT, migrate, preset
from vyos.alerts.record import History, read_history

_spec = importlib.util.spec_from_file_location(
    'alert_email', Path(__file__).resolve().parents[2] / 'python/vyos/utils/email.py'
)
alert_email = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alert_email)
send_mail = alert_email.send_mail

CATALOG = Path(__file__).resolve().parents[2] / 'data/alerts/catalog.json'


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.catalog = load_catalog(CATALOG)
        self.now = 1000
        self.delivered = []
        self.config = {
            'alert': {
                'down': {
                    'type': 'interface-down',
                    'interface': 'eth0',
                    'notify': {'email': ['ops'], 'sms': ['oncall']},
                }
            }
        }
        self.history = History(self.root)
        self.delivery = Delivery(
            self.config,
            self.root,
            clock=lambda: self.now,
            handlers={
                c: lambda r, t: self.delivered.append((r, t))
                for c in ('email', 'sms', 'script')
            },
        )

    def engine(self, config=None, **kwargs):
        return Engine(
            config or self.config,
            self.history,
            self.delivery,
            self.catalog,
            clock=lambda: self.now,
            **kwargs,
        )

    def link(self, engine, state='DOWN', interface='eth0'):
        engine.process_message(
            {
                'SYSLOG_IDENTIFIER': 'vyos-netlinkd',
                'MESSAGE': f'RTM_NEWLINK -> {interface}, state={state}, mac=00:11:22:33:44:55',
            }
        )

    def test_every_catalog_type_matches_sample_and_rejects_wrong_source(self):
        samples = {
            'login-success': (
                'sshd',
                'Accepted publickey for admin from 192.0.2.1 port 22 ssh2',
            ),
            'login-success-console': (
                'login',
                'pam_unix(login:session): session opened for user admin(uid=1000)',
            ),
            'login-failed': (
                'sshd',
                'Failed password for invalid user admin from 192.0.2.1 port 22 ssh2',
            ),
            'vrrp-state': (
                'keepalived-fifo.py',
                'INSTANCE router changed state to MASTER',
            ),
            'dhcp-server-lease': (
                'kea-dhcp4',
                'INFO DHCP4_LEASE_ALLOC [hw] lease 192.0.2.2 has been allocated for 600 seconds',
            ),
            'dhcp-server-release': (
                'kea-dhcp4',
                'INFO DHCP4_RELEASE [hw] address 192.0.2.2 was released properly.',
            ),
            'dhcp-client-bound': (
                'dhclient',
                'bound to 192.0.2.2 -- renewal in 300 seconds.',
            ),
            'dhcp-client-release': (
                'dhclient',
                'DHCPRELEASE of 192.0.2.2 on eth0 to 192.0.2.1 port 67',
            ),
        }
        for typ, definition in self.catalog['types'].items():
            with self.subTest(typ=typ):
                cfg = {'type': typ}
                for key in definition['parameters']:
                    cfg[key] = {
                        'interface': 'wwan0' if typ.startswith('wwan') else 'eth0',
                        'service': 'ssh.service',
                        'group': 'router',
                    }[key]
                rule = expand('x', cfg, self.catalog)
                entry = {k: v for k, v in rule['match'].items() if k != 'MESSAGE'}
                if typ in samples:
                    entry['SYSLOG_IDENTIFIER'], entry['MESSAGE'] = samples[typ]
                elif typ.startswith('interface-'):
                    entry['MESSAGE'] = (
                        f'RTM_NEWLINK -> eth0, state={typ.split("-")[1].upper()}, mac=aa'
                    )
                else:
                    entry['MESSAGE'] = 'source event'
                fields = compile_fields(rule['match'])
                self.assertIsNotNone(match_fields(fields, entry))
                for key in rule['match']:
                    wrong = dict(entry, **{key: 'unrelated'})
                    self.assertIsNone(match_fields(fields, wrong))

    def test_edge_observes_opposite_state_and_exact_interface(self):
        engine = self.engine()
        self.link(engine, interface='veth0')
        self.link(engine)
        self.link(engine)
        self.assertEqual(len(read_history(self.root)), 1)
        self.now += 61
        self.link(engine, 'UP')
        self.link(engine)
        self.assertEqual(len(read_history(self.root)), 2)

    def test_hold_down_history_and_queue_count(self):
        config = {
            'event': {
                'x': {'filter': {'pattern': 'failure'}, 'notify': {'email': ['ops']}}
            }
        }
        engine = self.engine(config)
        for _ in range(100):
            engine.process_message({'MESSAGE': 'failure'})
        records = read_history(self.root)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['suppressed'], 99)
        self.assertEqual(self.delivery.queues['email'][0]['record']['suppressed'], 99)
        self.assertEqual(self.delivered, [])
        self.delivery.step('email')
        self.assertEqual(self.delivered[0][0]['suppressed'], 99)

    def test_failure_queues_retry_without_blocking_other_channels(self):
        self.delivery.handlers['email'] = lambda r, t: (_ for _ in ()).throw(OSError())
        self.link(self.engine())
        self.delivery.step('email')
        self.delivery.step('sms')
        self.assertEqual(len(self.delivery.queues['email']), 1)
        self.assertEqual(len(self.delivery.queues['sms']), 0)
        restored = Delivery(
            self.config,
            self.root,
            clock=lambda: self.now,
            handlers=self.delivery.handlers,
        )
        self.assertEqual(restored.queues['email'][0]['attempts'], 1)
        self.now += 3
        restored.handlers['email'] = lambda r, t: None
        restored.step('email')
        self.assertEqual(restored.queues['email'], [])

    def test_no_sudo_login_or_alert_feedback(self):
        engine = self.engine({'alert': {'login': {'type': 'login-success'}}})
        engine.process_message(
            {
                'SYSLOG_IDENTIFIER': 'sudo',
                'MESSAGE': 'pam_unix(sudo:session): session opened for user root',
            }
        )
        self.assertEqual(read_history(self.root), [])
        engine = self.engine({'event': {'all': {'filter': {'pattern': '.*'}}}})
        engine.process_message({'SYSLOG_IDENTIFIER': 'igos-alert', 'MESSAGE': 'hello'})
        self.assertEqual(read_history(self.root), [])

    def test_commit_suppresses_start_but_not_failure(self):
        config = {
            'alert': {
                t: {'type': t, 'service': 'ssh.service'}
                for t in ('service-start', 'service-failed')
            }
        }
        engine = self.engine(config, committing=lambda: True)
        for typ in config['alert']:
            entry = dict(
                self.catalog['types'][typ]['match'],
                UNIT='ssh.service',
                MESSAGE='systemd message',
            )
            engine.process_message(entry)
        self.assertEqual(
            [r['type'] for r in read_history(self.root)], ['service-failed']
        )

    def test_wwan_selector_and_source_severity(self):
        engine = self.engine(
            {'alert': {'w': {'type': 'wwan-bearer-down', 'interface': 'wwan0'}}}
        )
        entry = {
            'SYSLOG_IDENTIFIER': 'igos-wwan-alertbus',
            'MESSAGE': 'lost bearer',
            'WWAN_ALERT_CODE': 'WWAN_BEARER_DOWN',
            'WWAN_ALERT_INTERFACE': '1',
            'WWAN_ALERT_SEVERITY': 'warning',
        }
        engine.process_message(entry)
        entry['WWAN_ALERT_INTERFACE'] = '0'
        engine.process_message(entry)
        self.assertEqual(read_history(self.root)[0]['object'], 'wwan0')
        self.assertEqual(read_history(self.root)[0]['severity'], 'warning')

    def test_sequence_survives_daemon_restart_and_history_is_bounded(self):
        self.link(self.engine())
        history = History(self.root, size=1)
        self.assertEqual(history.sequence, 1)
        engine = Engine(self.config, history, self.delivery, self.catalog)
        self.link(engine)
        self.assertEqual(len(read_history(self.root)), 1)
        self.assertEqual(read_history(self.root)[0]['sequence'], 2)
        self.assertEqual((self.root / 'history.jsonl').stat().st_mode & 0o777, 0o600)

    def test_parameters_cannot_be_regex(self):
        with self.assertRaises(ValueError):
            expand('x', {'type': 'interface-down', 'interface': 'eth.*'}, self.catalog)
        rule = expand(
            'x', {'type': 'interface-down', 'interface': 'eth0.1'}, self.catalog
        )
        self.assertIn('eth0\\.1', rule['match']['MESSAGE'])
        with self.assertRaises(ValueError):
            expand('x', {'type': 'login-success', 'interface': 'eth0'}, self.catalog)

    def test_migration_matches_only_known_patterns(self):
        self.assertEqual(
            preset('kea-dhcp4', '.*LEASE_ALLOCATED.*'), ('dhcp-server-lease', {})
        )
        self.assertEqual(
            preset('vyos-netlinkd', 'RTM_NEWLINK.*eth0,.*state=DOWN.*'),
            ('interface-down', {'interface': 'eth0'}),
        )
        self.assertIsNone(preset('vyos-netlinkd', 'RTM_NEWLINK.*eth.*,.*state=DOWN.*'))

    def test_custom_script_only_preserves_every_match(self):
        config = {
            'event': {
                'old': {
                    'filter': {'pattern': 'failure'},
                    'script': {'path': '/config/scripts/old.sh'},
                }
            }
        }
        engine = self.engine(config)
        for _ in range(3):
            engine.process_message({'MESSAGE': 'failure'})
        self.assertEqual(len(self.delivery.queues['script']), 3)
        self.assertEqual(len(read_history(self.root)), 3)

    def test_sms_cap_and_summary_survive_restart(self):
        self.config['rate_limit'] = {'sms': '1'}
        engine = self.engine()
        for _ in range(3):
            self.link(engine, 'UP')
            self.link(engine)
            self.now += 61
        for _ in range(3):
            self.delivery.step('sms')
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.delivery.status['sms']['summary']['count'], 2)
        restored = Delivery(
            self.config,
            self.root,
            clock=lambda: self.now,
            handlers=self.delivery.handlers,
        )
        self.now += 3600
        restored.step('sms')
        self.assertEqual(
            self.delivered[-1][0]['message'], '2 alerts suppressed, see history'
        )
        self.assertEqual(restored.status['sms']['sent'], 1)

    def test_expiry_and_overflow_keep_critical_longest(self):
        self.link(self.engine())
        item = self.delivery.queues['email'][0]
        # Fill the bounded queue without doing 1000 disk commits.
        self.delivery.queues['email'] = [dict(item, id=str(i)) for i in range(1000)]
        self.delivery.queues['email'][0]['record'] = dict(
            item['record'], severity='info'
        )
        self.delivery.enqueue(item['record'], {'notify': {'email': ['ops']}})
        self.assertEqual(len(self.delivery.queues['email']), 1000)
        self.assertTrue(
            all(
                q['record']['severity'] == 'critical'
                for q in self.delivery.queues['email']
            )
        )
        self.assertEqual(self.delivery.status['email']['dropped'], 1)
        self.now += 86401
        self.delivery.step('email')
        self.assertEqual(self.delivery.queues['email'], [])
        self.assertEqual(self.delivery.status['email']['expired'], 1000)

    def test_script_gets_fresh_environment_and_original_message(self):
        record = {
            'id': 'id',
            'type': 'custom',
            'severity': 'warning',
            'object': 'x',
            'state': '',
            'message': 'summary',
            'log': {'MESSAGE': 'source'},
        }
        target = {
            'path': '/config/scripts/test.sh',
            'environment': {'TEST': {'value': 'value'}},
        }
        with patch('vyos.alerts.delivery.subprocess.Popen') as popen:
            popen.return_value.wait.return_value = 0
            self.delivery.script(record, target)
            first = popen.call_args.kwargs['env']
            self.delivery.script(dict(record, log={'MESSAGE': 'another'}), target)
            second = popen.call_args.kwargs['env']
            self.assertEqual(first['message'], 'source')
            self.assertEqual(second['message'], 'another')
            self.assertEqual(first['ALERT_MESSAGE'], 'summary')
            self.assertIsNot(first, second)
            self.assertNotIn('message', target['environment'])

    def test_migration_converts_presets_preserves_custom_and_leaves_sms_off(self):
        import copy

        class Tree:
            def __init__(self, data):
                self.data = copy.deepcopy(data)

            def get(self, path):
                node = self.data
                for key in path:
                    node = node[key]
                return node

            def exists(self, path):
                try:
                    self.get(path)
                    return True
                except KeyError:
                    return False

            def return_value(self, path):
                return self.get(path)

            def list_nodes(self, path):
                return list(self.get(path))

            def set(self, path, value=None, replace=True):
                node = self.data
                for key in path[:-1]:
                    node = node.setdefault(key, {})
                if not replace and path[-1] in node:
                    old = node[path[-1]]
                    node[path[-1]] = (old if isinstance(old, list) else [old]) + [value]
                else:
                    node[path[-1]] = value if value is not None else {}

            def delete(self, path):
                node = self.get(path[:-1])
                del node[path[-1]]

        script = {
            'path': GUI_SCRIPT,
            'environment': {
                'sms_number': {'value': '+15555551234'},
                'email_enabled': {'value': 'true'},
                'gui_level': {'value': 'critical'},
            },
        }
        tree = Tree(
            {
                'service': {
                    'event-handler': {
                        'event': {
                            'dhcp': {
                                'filter': {
                                    'syslog-identifier': 'kea-dhcp4',
                                    'pattern': '.*LEASE_ALLOCATED.*',
                                },
                                'script': script,
                            },
                            'custom': {
                                'filter': {'pattern': '.*unusual.*'},
                                'script': script,
                            },
                            'legacy': {
                                'filter': {'pattern': '.*'},
                                'script': {'path': '/config/scripts/custom.sh'},
                            },
                        }
                    }
                },
                'system': {
                    'email': {
                        'recipient': {
                            'ops': {'enable': {}},
                            'backup': {'enable': {}},
                            'muted': {},
                        }
                    }
                },
            }
        )
        migrate(tree)
        config = tree.get(['service', 'event-handler'])
        self.assertEqual(config['alert']['dhcp']['type'], 'dhcp-server-lease')
        self.assertEqual(config['alert']['dhcp']['notify']['email'], ['ops', 'backup'])
        self.assertNotIn('sms', config['alert']['dhcp']['notify'])
        self.assertIn('monitor-only', config['event']['custom'])
        self.assertNotIn('script', config['event']['custom'])
        self.assertEqual(
            config['event']['legacy']['script']['path'], '/config/scripts/custom.sh'
        )
        self.assertEqual(len(config['sms-recipient']), 2)
        before = copy.deepcopy(tree.data)
        migrate(tree)
        self.assertEqual(tree.data, before)

    def test_mail_headers_and_timeout(self):
        with patch.object(alert_email.subprocess, 'run') as run:
            run.return_value.returncode = 0
            send_mail(['ops@example.com'], 'Subject\r\nBcc: attacker', 'body', 'active')
            self.assertNotIn('\nBcc:', run.call_args.kwargs['input'])
            self.assertEqual(run.call_args.kwargs['timeout'], 30)
            self.assertEqual(run.call_args.args[0][-2:], ['--', 'ops@example.com'][-2:])


if __name__ == '__main__':
    unittest.main()
