#!/usr/bin/env python3
# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
"""Opt-in CLI smoke tests for a configured lab modem.

Run with IGOS_SMS_TEST_INTERFACE=wwan0 on the target. The standard smoke-test
shim saves/restores the original configuration. No action command is executed.
"""

import os
import re
import unittest

from base_vyostest_shim import VyOSUnitTestSHIM
from vyos.configquery import ConfigTreeQuery
from vyos.configsession import ConfigSessionError
from vyos.utils.auth import verify_password

TEST_INTERFACE = os.environ.get('IGOS_SMS_TEST_INTERFACE', '')
PASSWORD = 'X4!rT9qW2zH7'


@unittest.skipUnless(
    re.fullmatch(r'wwan[0-9]+', TEST_INTERFACE),
    'Set IGOS_SMS_TEST_INTERFACE to a configured lab modem',
)
class SmsCommandCliTest(VyOSUnitTestSHIM.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = ['interfaces', 'wwan', TEST_INTERFACE, 'sms-command']
        if not ConfigTreeQuery().exists(cls.base[:-1]):
            raise unittest.SkipTest('The lab WWAN interface must already be configured')
        super().setUpClass()

    def setUp(self):
        self.cli_delete(self.base)
        self.cli_commit()

    def tearDown(self):
        self.cli_discard()
        self.cli_delete(self.base)
        self.cli_commit()
        super().tearDown()

    def configure_gateway(self):
        self.cli_set(self.base + ['allow-from', '+15555551234'])
        self.cli_set(self.base + ['password'], value=PASSWORD)

    def test_password_hash_and_rotation(self):
        self.configure_gateway()
        self.cli_commit()
        query = ConfigTreeQuery()
        self.assertFalse(query.exists(self.base + ['password']))
        hashed = query.value(self.base + ['password-hash'])
        self.assertTrue(verify_password(PASSWORD, hashed))
        replacement = 'Q7!wZ4tR8yN2'
        self.cli_set(self.base + ['password'], value=replacement)
        self.cli_commit()
        hashed = ConfigTreeQuery().value(self.base + ['password-hash'])
        self.assertTrue(verify_password(replacement, hashed))
        self.assertFalse(verify_password(PASSWORD, hashed))

    def test_action_verbs_are_opt_in(self):
        self.configure_gateway()
        self.cli_commit()
        self.assertFalse(ConfigTreeQuery().exists(self.base + ['privileged']))
        self.cli_set(self.base + ['privileged', 'verb', 'reset'])
        self.cli_set(self.base + ['privileged', 'allow-from', '+15555551234'])
        self.cli_commit()

    def test_invalid_configuration_rejected(self):
        self.configure_gateway()
        self.cli_set(self.base + ['privileged', 'verb', 'configure'])
        with self.assertRaises(ConfigSessionError):
            self.cli_commit()

    def test_whitespace_password_rejected(self):
        self.configure_gateway()
        self.cli_set(self.base + ['password'], value='With spaces123!')
        with self.assertRaises(ConfigSessionError):
            self.cli_commit()


if __name__ == '__main__':
    unittest.main(verbosity=2, failfast=VyOSUnitTestSHIM.TestCase.debug_on())
