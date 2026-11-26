"""Conf/op-mode alert contracts, with appliance-only imports isolated."""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import vyos
from vyos import ConfigError
from vyos.alerts.catalog import load_catalog
from vyos.alerts.record import History, read_history

REPO = Path(__file__).resolve().parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, REPO / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        modules = {
            'vyos.config': stub('vyos.config', Config=Mock()),
            'vyos.airbag': stub('vyos.airbag', enable=lambda: None),
            'vyos.utils.dict': load('python/vyos/utils/dict.py', 'alert_dict'),
            'vyos.utils.process': stub('vyos.utils.process', call=Mock()),
            'vyos.utils.email': load('python/vyos/utils/email.py', 'alert_email'),
        }
        with (
            patch.dict(sys.modules, modules),
            patch.object(vyos, 'airbag', modules['vyos.airbag'], create=True),
        ):
            self.module = load('src/conf_mode/service_event-handler.py', 'alert_conf')
        self.module.service_conf = Path(self.temp.name) / 'runtime.json'
        self.module.load_catalog = lambda: load_catalog(
            REPO / 'data/alerts/catalog.json'
        )
        self.config = {
            'alert': {
                'x': {
                    'type': 'interface-down',
                    'interface': 'eth0',
                    'notify': {'email': ['ops']},
                }
            },
            '_email': {'profile': 'active', 'recipients': {'ops': {'enabled': True}}},
            '_wwan': ['wwan0'],
        }

    def test_get_config_uses_mangled_keys_and_preserves_tag_names(self):
        conf = Mock()
        conf.get_config_dict.return_value = {}
        conf.list_nodes.return_value = []
        conf.return_value.return_value = None
        self.module.get_config(conf)
        self.assertEqual(
            conf.get_config_dict.call_args.kwargs['key_mangling'], ('-', '_')
        )
        self.assertTrue(
            conf.get_config_dict.call_args.kwargs['no_tag_node_value_mangle']
        )

    def test_recipient_deletion_or_missing_active_profile_is_rejected(self):
        self.module.verify(self.config)
        self.config['_email']['recipients'].clear()
        with self.assertRaises(ConfigError):
            self.module.verify(self.config)
        self.config['_email']['recipients']['ops'] = {}
        self.config['_email']['profile'] = None
        with self.assertRaises(ConfigError):
            self.module.verify(self.config)

    def test_sms_modem_deletion_and_reserved_environment_are_rejected(self):
        self.config['sms_recipient'] = {
            'oncall': {'number': '+15555551234', 'interface': 'wwan1'}
        }
        with self.assertRaises(ConfigError):
            self.module.verify(self.config)
        self.config['sms_recipient']['oncall']['interface'] = 'wwan0'
        self.module.verify(self.config)
        self.config['event'] = {
            'custom': {
                'filter': {'pattern': '.*'},
                'script': {
                    'path': '/config/scripts/x.sh',
                    'environment': {'message': {'value': 'x'}},
                },
            }
        }
        with self.assertRaises(ConfigError):
            self.module.verify(self.config)

    def test_runtime_excludes_email_configuration_and_is_private(self):
        self.module.generate(self.config)
        runtime = json.loads(self.module.service_conf.read_text())
        self.assertNotIn('_email', runtime)
        self.assertEqual(self.module.service_conf.stat().st_mode & 0o777, 0o600)
        self.module.apply(self.config)
        self.module.call.assert_called_with(
            'systemctl restart vyos-event-handler.service'
        )
        self.module.generate({})
        self.assertFalse(self.module.service_conf.exists())
        self.module.apply({})
        self.module.call.assert_called_with('systemctl stop vyos-event-handler.service')

    def test_disabled_rule_parameters_are_still_verified(self):
        self.config['alert']['x']['disable'] = ''
        del self.config['alert']['x']['interface']
        with self.assertRaises(ConfigError):
            self.module.verify(self.config)


class OpModeTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(sys.modules, {'vyos.opmode': stub('vyos.opmode')}):
            self.module = load('src/op_mode/event_handler.py', 'alert_op')
        self.module.load_catalog = lambda: load_catalog(
            REPO / 'data/alerts/catalog.json'
        )

    def test_catalog_ids_are_values_to_survive_api_key_normalization(self):
        types = self.module.show(True, types=True)['types']
        self.assertIn('interface-down', [t['id'] for t in types])

    def test_paging_includes_new_boots_but_never_previous_boots(self):
        records = [
            {'boot_id': boot, 'sequence': seq, 'severity': 'info'}
            for boot in ['old', 'current', 'next']
            for seq in [1, 2]
        ]
        with patch.object(self.module, 'read_history', return_value=records):
            result = self.module.show(True, after=1, boot_id='current')
            self.assertEqual(
                [(r['boot_id'], r['sequence']) for r in result],
                [('next', 2), ('next', 1), ('current', 2)],
            )
            with self.assertRaises(ValueError):
                self.module.show(True, after=1)

    def test_clear_preserves_sequence_across_daemon_restarts(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(root)
            history.sequence = 7
            from vyos.alerts.record import atomic_json

            atomic_json(
                Path(root) / 'sequence.json',
                {'boot_id': history.boot_id, 'sequence': 7},
            )
            self.module.ROOT = Path(root)
            self.module.clear()
            self.assertEqual(read_history(root), [])
            self.assertEqual(History(root).sequence, 7)


if __name__ == '__main__':
    unittest.main()
