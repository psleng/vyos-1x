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
import sys
import warnings

import requests
from tabulate import tabulate
import urllib3

from vyos.configquery import ConfigTreeQuery
import vyos.opmode
from vyos.utils.process import call

default_https_port = 443
cloud_status_path = '/perlecloud/status'

config_file = r'/etc/igos-cloud-proxy/igos-cloud-proxy.conf'
service_name = 'igos-cloud-proxy'


def set_registration(code: str, raw: bool):
    config = ConfigTreeQuery()
    if not config.exists(['service', 'cloud', 'enabled']):
        raise vyos.opmode.UnconfiguredSubsystem('Cloud service is not configured')
    call(f'systemctl stop {service_name}')

    try:
        with open(config_file, 'r+') as cloud_config:
            data_json = json.load(cloud_config)
            if not isinstance(data_json, dict):
                raise vyos.opmode.UnconfiguredSubsystem('Invalid cloud configuration')
            data_json["tmpDeviceRegistrationPIN"] = code

            cloud_config.seek(0)
            json.dump(data_json, cloud_config, indent=4)
            cloud_config.write('\n')
            cloud_config.truncate()
    except OSError as error:
        raise vyos.opmode.UnconfiguredSubsystem(
            'Could not read cloud configuration'
        ) from error
    except json.JSONDecodeError as error:
        raise vyos.opmode.UnconfiguredSubsystem(
            'Could not parse cloud configuration'
        ) from error

    call(f'systemctl start {service_name}')


def show(raw: bool):
    config = ConfigTreeQuery()
    if not config.exists(['service', 'cloud']):
        raise vyos.opmode.UnconfiguredSubsystem('Cloud service is not configured')

    status = {
        'cloud_connection_status': 'disconnected',
        'cloud_device_id': '',
        'cloud_connection_if_name': '',
        'cloud_connection_ip_address': '',
        'cloud_connection_if_role': '',
    }
    port = default_https_port
    https_port_path = ['service', 'https', 'port']
    if config.exists(https_port_path):
        configured_port = config.value(https_port_path)
        if configured_port is not None:
            port = int(str(configured_port))

    url = f'https://127.0.0.1:{port}{cloud_status_path}'
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', urllib3.exceptions.InsecureRequestWarning)
            response = requests.get(url, timeout=5, verify=False)
        response.raise_for_status()
        response_data = response.json()
    except (requests.RequestException, ValueError):
        pass
    else:
        for field in status:
            if isinstance(response_data.get(field), str):
                status[field] = response_data[field]

    if raw:
        return status
    data = [
        ['Connection status', status['cloud_connection_status']],
        ['Device ID', status['cloud_device_id']],
        ['Connection interface', status['cloud_connection_if_name']],
        ['Connection IP address', status['cloud_connection_ip_address']],
        ['Connection interface role', status['cloud_connection_if_role']],
    ]
    return tabulate(data)


if __name__ == '__main__':
    try:
        result = vyos.opmode.run(sys.modules[__name__])
        if result:
            print(result)
    except (ValueError, vyos.opmode.Error) as error:
        print(error)
        sys.exit(1)
