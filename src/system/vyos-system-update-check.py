#!/usr/bin/env python3
#
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

import argparse
import json

from pathlib import Path
from sys import exit
from time import sleep

from vyos.utils.process import call
from vyos.utils.update_status import CHECK_INTERVAL
from vyos.utils.update_status import check_system_version
from vyos.utils.update_status import next_retry

import vyos.version

motd_file = Path('/run/motd.d/10-vyos-update')


if __name__ == '__main__':
    # Parse command arguments and get config
    parser = argparse.ArgumentParser()
    parser.add_argument('-c',
                        '--config',
                        action='store',
                        help='Path to system-update-check configuration',
                        required=False,
                        default='/run/vyos-system-update.conf',
                        type=Path)

    args = parser.parse_args()

    # The service runs by default, so the run-time configuration file is
    # optional: it only exists when the `system update-check` CLI node is
    # configured (to override the URL). When it is absent or has no URL, fall
    # back to the default update URL baked into the image at build time
    # (vyos-build writes it into /usr/share/vyos/version.json from the build
    # flavor's `update_url`). This lets update notifications work with zero
    # configuration.
    url_json = None
    config_path = Path(args.config)
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text())
            url_json = config.get('url')
        except Exception as err:
            print(f'Configuration file "{config_path}" is malformed: {err}')

    if not url_json:
        url_json = vyos.version.get_version_data().get('update_url')

    if not url_json:
        # No URL configured and none baked into the image: nothing to check.
        print('No update-check URL configured or baked into the image; exiting.')
        exit(0)

    retry = None
    while True:
        result = check_system_version(url_json)
        if result and result.get('update_available') and result.get('remote_version'):
            local_version = result['local_version']
            remote_version = result['remote_version']
            call(f'wall -n "Update available: {remote_version} \nUpdate URL: {result.get("url")}"')
            # MOTD used in /run/motd.d/10-vyos-update
            motd_file.parent.mkdir(exist_ok=True)
            motd_file.write_text(f'---\n'
                                 f'Current version: {local_version}\n'
                                 f'Update available: \033[1;34m{remote_version}\033[0m\n'
                                 f'---\n')
        if result and result.get('error'):
            # The uplink is often not up yet at boot: retry soon instead of
            # waiting a full interval
            retry = next_retry(retry)
            sleep(retry)
        else:
            retry = None
            sleep(CHECK_INTERVAL)
