"""Exercise recipient selection and partial delivery without a real modem."""
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).parents[1] / 'op_mode' / 'generate_test_sms.py'


class TestSmsNotifications(unittest.TestCase):
    def setUp(self):
        configquery = ModuleType('vyos.configquery')
        configquery.ConfigTreeQuery = Mock()
        modem = ModuleType('vyos.utils.wwan.wwan_client')
        modem.WWANClientSync = Mock()
        with patch.dict(sys.modules, {'vyos.configquery': configquery,
                                     'vyos.utils.wwan.wwan_client': modem}):
            spec = importlib.util.spec_from_file_location('generate_test_sms', SCRIPT)
            self.sender = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.sender)
        self.config = {'active-profile': 'cellular',
                       'profile': {'cellular': {'interface': 'wwan2'}},
                       'recipient': {
                           'first': {'number': '+14165550101', 'enable': {}},
                           'second': {'number': '+14165550102', 'enable': {}},
                           'disabled': {'number': '+14165550103'}}}

    def test_enabled_recipients_use_active_modem(self):
        client = Mock()
        client.send_sms.return_value = {'message_id': 1}
        results = self.sender.send_sms_notifications(self.config, 'alert', client)
        self.assertEqual(client.send_sms.call_count, 2)
        self.assertEqual(client.send_sms.call_args_list[0].args, (2, '+14165550101', 'alert'))
        self.assertTrue(all(item['success'] for item in results))

    def test_first_failure_does_not_skip_second_recipient(self):
        client = Mock()
        client.send_sms.side_effect = [RuntimeError('modem error'), {'message_id': 2}]
        results = self.sender.send_sms_notifications(self.config, 'alert', client)
        self.assertEqual([item['success'] for item in results], [False, True])

    def test_missing_active_profile_rejected(self):
        self.config['active-profile'] = 'missing'
        with self.assertRaises(ValueError):
            self.sender.send_sms_notifications(self.config, 'alert', Mock())

    def test_no_enabled_recipients_rejected(self):
        self.config['recipient'] = {}
        with self.assertRaises(ValueError):
            self.sender.send_sms_notifications(self.config, 'alert', Mock())


if __name__ == '__main__':
    unittest.main()
