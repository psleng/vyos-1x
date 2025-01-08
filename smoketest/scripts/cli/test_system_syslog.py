#!/usr/bin/env python3
#
# Copyright (C) 2019-2024 VyOS maintainers and contributors
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

import re
import unittest

from base_vyostest_shim import VyOSUnitTestSHIM

from vyos.utils.file import read_file
from vyos.utils.process import process_named_running
from vyos.xml_ref import default_value

PROCESS_NAME = 'rsyslogd'
RSYSLOG_CONF = '/etc/rsyslog.d/00-vyos.conf'

base_path = ['system', 'syslog']

def get_config_value(key):
    tmp = read_file(RSYSLOG_CONF)
    tmp = re.findall(r'\n?{}\s+(.*)'.format(key), tmp)
    return tmp[0]

class TestRSYSLOGService(VyOSUnitTestSHIM.TestCase):
    @classmethod
    def setUpClass(cls):
        super(TestRSYSLOGService, cls).setUpClass()

        # ensure we can also run this test on a live system - so lets clean
        # out the current configuration :)
        cls.cli_delete(cls, base_path)

    def tearDown(self):
        # Check for running process
        self.assertTrue(process_named_running(PROCESS_NAME))

        # delete testing SYSLOG config
        self.cli_delete(base_path)
        self.cli_commit()

        # Check for running process
        self.assertFalse(process_named_running(PROCESS_NAME))

    def test_syslog_console(self):
        self.cli_set(base_path + ['console', 'facility', 'all', 'level', 'warning'])
        self.cli_commit()
        self.assertIn('/dev/console', get_config_value('\*.warning'))

    def test_syslog_global(self):
        hostname = 'vyos123'
        domainname = 'example.local'
        self.cli_set(['system', 'host-name', hostname])
        self.cli_set(['system', 'domain-name', domainname])
        self.cli_set(base_path + ['global', 'marker', 'interval', '600'])
        self.cli_set(base_path + ['global', 'preserve-fqdn'])
        self.cli_set(base_path + ['global', 'facility', 'kern', 'level', 'err'])

        self.cli_commit()

        config = read_file(RSYSLOG_CONF)
        expected = [
            '$MarkMessagePeriod 600',
            '$PreserveFQDN on',
            'kern.err',
            f'$LocalHostName {hostname}.{domainname}',
        ]

        for e in expected:
            self.assertIn(e, config)

    def test_syslog_remote(self):
        rhosts = {
            '169.254.0.1': {
                'facility': {'name' : 'auth', 'level': 'info'},
                'protocol': 'udp',
            },
            '169.254.0.2': {
                'port': '1514',
                'protocol': 'udp',
            },
            '169.254.0.3': {
                'format': ['include-timezone', 'octet-counted'],
                'protocol': 'tcp',
            },
        }
        default_port = default_value(base_path + ['remote', next(iter(rhosts)), 'port'])

        for remote, remote_options in rhosts.items():
            remote_base = base_path + ['remote', remote]

            if 'port' in remote_options:
                self.cli_set(remote_base + ['port', remote_options['port']])

            if ('facility' in remote_options and
                'name' in remote_options['facility'] and
                'level' in remote_options['facility']
                ):
                facility = remote_options['facility']['name']
                level = remote_options['facility']['level']
                self.cli_set(remote_base + ['facility', facility, 'level', level])

            if 'format' in remote_options:
                for format in remote_options['format']:
                    self.cli_set(remote_base + ['format', format])

            if 'protocol' in remote_options:
                protocol = remote_options['protocol']
                self.cli_set(remote_base + ['protocol', protocol])

        self.cli_commit()

        config = read_file(RSYSLOG_CONF)
        for remote, remote_options in rhosts.items():
            tmp = ' '
            if ('facility' in remote_options and
                'name' in remote_options['facility'] and
                'level' in remote_options['facility']
                ):
                facility = remote_options['facility']['name']
                level = remote_options['facility']['level']
                tmp = f'{facility}.{level} '

            tmp += '@'
            if 'protocol' in remote_options and remote_options['protocol'] == 'tcp':
                tmp += '@'

            if 'format' in remote_options and 'octet-counted' in remote_options['format']:
                tmp += '(o)'

            port = default_port
            if 'port' in remote_options:
                port = remote_options['port']

            tmp += f'{remote}:{port}'

            if 'format' in remote_options and 'include-timezone' in remote_options['format']:
                tmp += ';RSYSLOG_SyslogProtocol23Format'

            self.assertIn(tmp, config)

if __name__ == '__main__':
    unittest.main(verbosity=2)
