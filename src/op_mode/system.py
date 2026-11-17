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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys

from vyos.configquery import ConfigTreeQuery
from vyos.utils.update_status import cached_system_version

import vyos.opmode
import vyos.version

config = ConfigTreeQuery()
base = ['system', 'update-check']


def _get_update_url():
    # An explicitly configured URL overrides the default baked into the image
    # at build time (/usr/share/vyos/version.json from the build flavor's
    # `update_url`).
    url = config.value(base + ['url'])
    if not url:
        url = vyos.version.get_version_data().get('update_url')
    return url


def _compare_version_raw():
    # Answer from the update-check daemon's cached result when fresh, so
    # `show system updates` and the GUI do not wait on the internet
    return cached_system_version(_get_update_url())


def _formatted_compare_version(data):
    local_version = data.get('local_version')
    remote_version = data.get('remote_version')
    url = data.get('url')
    if {'update_available','local_version', 'remote_version', 'url'} <= set(data):
        return f'Current version: {local_version}\n\nUpdate available: {remote_version}\nUpdate URL: {url}'
    elif local_version == remote_version and remote_version is not None:
        return f'No available updates for your system \n' \
               f'current version: {local_version}\nremote version: {remote_version}'
    else:
        return 'Update not found'


def _verify():
    # The subsystem is usable when a URL can be resolved: either configured via
    # CLI or baked into the image at build time.
    return bool(_get_update_url())


def show_update(raw: bool):
    if not _verify():
        raise vyos.opmode.UnconfiguredSubsystem("system update-check not configured")
    data = _compare_version_raw()
    if raw:
        return data
    else:
        return _formatted_compare_version(data)


if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
