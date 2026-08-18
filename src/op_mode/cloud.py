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

import sys
import warnings

import requests
from tabulate import tabulate
import urllib3

from vyos.configquery import ConfigTreeQuery
import vyos.opmode

default_https_port = 443
cloud_status_path = '/perlecloud/status'


def _get_https_port(config: ConfigTreeQuery) -> int:
    path = ['service', 'https', 'port']
    if config.exists(path):
        port = config.value(path)
        if port is not None:
            return int(str(port))
    return default_https_port


def _get_connection_details(config: ConfigTreeQuery) -> dict[str, str]:
    details = {
        'cloud_connection_status': 'disconnected',
        'cloud_device_id': '',
        'cloud_connection_if_name': '',
        'cloud_connection_ip_address': '',
        'cloud_connection_if_role': '',
    }
    port = _get_https_port(config)
    url = f'https://127.0.0.1:{port}{cloud_status_path}'

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', urllib3.exceptions.InsecureRequestWarning)
            response = requests.get(url, timeout=5, verify=False)
        response.raise_for_status()
        response_data = response.json()
    except (requests.RequestException, ValueError):
        return details

    for field in details:
        if isinstance(response_data.get(field), str):
            details[field] = response_data[field]
    return details


def _get_status(config: ConfigTreeQuery) -> dict:
    return _get_connection_details(config)


def _get_formatted_status(status: dict) -> str:
    data = [
        ['Connection status', status['cloud_connection_status']],
        ['Device ID', status['cloud_device_id']],
        ['Connection interface', status['cloud_connection_if_name']],
        ['Connection IP address', status['cloud_connection_ip_address']],
        ['Connection interface role', status['cloud_connection_if_role']],
    ]
    return tabulate(data)


def show(raw: bool):
    config = ConfigTreeQuery()
    if not config.exists(['service', 'cloud']):
        raise vyos.opmode.UnconfiguredSubsystem('Cloud service is not configured')

    status = _get_status(config)
    if raw:
        return status
    return _get_formatted_status(status)


if __name__ == '__main__':
    try:
        result = vyos.opmode.run(sys.modules[__name__])
        if result:
            print(result)
    except (ValueError, vyos.opmode.Error) as error:
        print(error)
        sys.exit(1)
