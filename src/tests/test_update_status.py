# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
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

import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from vyos.utils import update_status as us


class FakeConfig:
    def __init__(self, paths):
        self.paths = {tuple(p) for p in paths}

    def exists(self, path):
        return any(p[: len(path)] == tuple(path) for p in self.paths)

    def list_nodes(self, path):
        n = len(path)
        return sorted({p[n] for p in self.paths if p[:n] == tuple(path) and len(p) > n})


class TestUpdateStatus(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'status.json'

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_success_and_url_match(self):
        us.write_status(self.path, 'https://a', result={'update_available': False})
        self.assertEqual(
            us.read_status(self.path, 'https://a')['result'],
            {'update_available': False},
        )
        self.assertIsNone(us.read_status(self.path, 'https://b'))

    def test_staleness_differs_for_success_and_error(self):
        now = time.time()
        us.write_status(self.path, 'u')
        with patch.object(us.time, 'time', return_value=now + us.SUCCESS_MAX_AGE - 60):
            self.assertIsNotNone(us.read_status(self.path, 'u'))
        with patch.object(us.time, 'time', return_value=now + us.SUCCESS_MAX_AGE + 60):
            self.assertIsNone(us.read_status(self.path, 'u'))

        us.write_status(self.path, 'u', error='unreachable')
        with patch.object(us.time, 'time', return_value=now + us.ERROR_MAX_AGE - 60):
            self.assertIsNotNone(us.read_status(self.path, 'u'))
        with patch.object(us.time, 'time', return_value=now + us.ERROR_MAX_AGE + 60):
            self.assertIsNone(us.read_status(self.path, 'u'))

    def test_missing_or_corrupt_file(self):
        self.assertIsNone(us.read_status(self.path, 'u'))
        self.path.write_text('{not json')
        self.assertIsNone(us.read_status(self.path, 'u'))
        self.assertIsNone(us.read_last_status(self.path))

    def test_backoff(self):
        delays = []
        delay = None
        for _ in range(8):
            delay = us.next_retry(delay)
            delays.append(delay)
        self.assertEqual(delays[0], us.RETRY_MIN)
        self.assertEqual(delays[1], 2 * us.RETRY_MIN)
        self.assertEqual(delays[-1], us.RETRY_MAX)

    def test_cellular_check_wanted_only_with_enabled_modem(self):
        wwan = ['interfaces', 'wwan', 'wwan0', 'apn', 'x']
        self.assertFalse(us.cellular_firmware_check_wanted(FakeConfig([])))
        self.assertTrue(us.cellular_firmware_check_wanted(FakeConfig([wwan])))
        self.assertFalse(
            us.cellular_firmware_check_wanted(
                FakeConfig([['interfaces', 'wwan', 'wwan0', 'disable']])
            )
        )
        self.assertFalse(
            us.cellular_firmware_check_wanted(
                FakeConfig([wwan, ['system', 'update-check', 'disable']])
            )
        )
        self.assertFalse(
            us.cellular_firmware_check_wanted(
                FakeConfig(
                    [wwan, ['system', 'update-check', 'cellular-firmware', 'disable']]
                )
            )
        )


class TestActiveConfigValue(TestCase):
    def test_legacy_backend_uses_single_lookup(self):
        with (
            patch('vyos.utils.backend.vyconf_backend', return_value=False),
            patch('vyos.utils.boot.boot_configuration_complete', return_value=True),
            patch('vyos.configquery.CliShellApiConfigQuery') as cli,
            patch('vyos.configquery.ConfigTreeQuery') as tree,
        ):
            cli.return_value.value.return_value = 'https://x'
            self.assertEqual(us.active_config_value(['a']), 'https://x')
            tree.assert_not_called()

    def test_missing_value_is_none(self):
        from vyos.configquery import ConfigQueryError

        with (
            patch('vyos.utils.backend.vyconf_backend', return_value=False),
            patch('vyos.utils.boot.boot_configuration_complete', return_value=True),
            patch('vyos.configquery.CliShellApiConfigQuery') as cli,
        ):
            cli.return_value.value.side_effect = ConfigQueryError('none')
            self.assertIsNone(us.active_config_value(['a']))

    def test_vyconf_backend_uses_config_tree(self):
        with (
            patch('vyos.utils.backend.vyconf_backend', return_value=True),
            patch('vyos.configquery.ConfigTreeQuery') as tree,
        ):
            tree.return_value.value.return_value = 'v'
            self.assertEqual(us.active_config_value(['a']), 'v')
