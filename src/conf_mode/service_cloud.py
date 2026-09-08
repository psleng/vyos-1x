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

import json
import os

from vyos.config import Config
from vyos.utils.process import call
from vyos import ConfigError
from vyos import airbag

airbag.enable()

config_file = r'/etc/igos-cloud-proxy/igos-cloud-proxy.conf'
persistent_config_file = r'/config/igos-cloud-proxy/igos-cloud-proxy.conf'
service_name = 'igos-cloud-proxy'


def _read_registration_pin(path):
    try:
        with open(path) as config_fd:
            current_config = json.load(config_fd)
        registration_code = current_config.get('tmpDeviceRegistrationPIN')
        if isinstance(registration_code, str) and registration_code:
            return registration_code
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


def _write_persistent_registration_pin(registration_code):
    if not isinstance(registration_code, str) or not registration_code:
        return

    persistent_config_dir = os.path.dirname(persistent_config_file)
    if not os.path.exists(persistent_config_dir):
        os.makedirs(persistent_config_dir)

    config = {'tmpDeviceRegistrationPIN': registration_code}
    with open(persistent_config_file, 'w') as config_fd:
        json.dump(config, config_fd, indent=4)
        config_fd.write('\n')
    os.chmod(persistent_config_file, 0o600)


def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()
    base = ['service', 'cloud']
    if not conf.exists(base):
        return None

    cloud = conf.get_config_dict(base, key_mangling=('-', '_'), get_first_key=True)
    return cloud


def verify(cloud):
    return None


def generate(cloud):
    call(f'systemctl stop {service_name}')

    if cloud is None or 'enabled' not in cloud:
        if os.path.exists(config_file):
            os.unlink(config_file)
        return None

    config_dir = os.path.dirname(config_file)
    if not os.path.exists(config_dir):
        os.makedirs(config_dir)

    config = {}
    registration_code = _read_registration_pin(persistent_config_file)
    if registration_code is None:
        registration_code = _read_registration_pin(config_file)
        if registration_code is not None:
            _write_persistent_registration_pin(registration_code)

    if registration_code is not None:
        config['tmpDeviceRegistrationPIN'] = registration_code

    if 'override_device_url' in cloud:
        config['perleCloudURL_OVERRIDE'] = cloud['override_device_url']

    with open(config_file, 'w') as config_fd:
        json.dump(config, config_fd, indent=4)
        config_fd.write('\n')
    os.chmod(config_file, 0o600)
    return None


def apply(cloud):
    if cloud is None or 'enabled' not in cloud:
        call(f'systemctl stop {service_name}')
        return None

    call(f'systemctl start {service_name}')


if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
