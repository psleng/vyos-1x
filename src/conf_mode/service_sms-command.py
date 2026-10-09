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

from pathlib import Path
from sys import exit
import json
import re

from vyos.config import Config
from vyos.template import render
from vyos.utils.process import call
from vyos import ConfigError
from vyos import airbag

airbag.enable()

service_name = 'sms-command.service'
config_file = Path('/etc/default/sms-command')


def _wwan_sort_key(ifname):
    """Sort WWAN names naturally by numeric suffix (wwan2 after wwan1)."""
    match = re.search(r'(\d+)$', str(ifname))
    return int(match.group(1)) if match else 0


def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()

    base = ['interfaces', 'wwan']
    if not conf.exists(base):
        return None

    enabled_interfaces = []
    authorized_numbers = {}
    errors = []

    for ifname in (conf.list_nodes(base) or []):
        sms_base = base + [ifname, 'sms-command']
        if not conf.exists(sms_base):
            continue

        allowed = [
            str(x).strip()
            for x in (conf.return_values(sms_base + ['authorized-number']) or [])
            if str(x).strip()
        ]
        if not allowed:
            errors.append(
                f'At least one interfaces wwan {ifname} sms-command authorized-number is required'
            )
            continue

        enabled_interfaces.append(ifname)
        authorized_numbers[ifname] = {}
        shared_password = conf.return_value(sms_base + ['password'])
        configured_passwords = ({shared_password}
                                if isinstance(shared_password, str)
                                and shared_password else set())
        password_errors = []
        if shared_password:
            if len(shared_password) < 9:
                password_errors.append('at least 9 characters')
            if not re.fullmatch(r'\S+', shared_password):
                password_errors.append('no spaces')
            if not re.search(r'[A-Z]', shared_password):
                password_errors.append('at least one uppercase letter')
            if not re.search(r'[0-9]', shared_password):
                password_errors.append('at least one digit')
            if not re.search(r'[^A-Za-z0-9]', shared_password):
                password_errors.append('at least one special character')
            if password_errors:
                errors.append(
                    f'Invalid SMS password for {ifname}: ' + ', '.join(password_errors)
                )
        for number in allowed:
            password = shared_password
            if not re.fullmatch(r'\+?[0-9]{6,20}', number):
                errors.append(f'Invalid authorized number for {ifname}')
            password_errors = []
            if not isinstance(password, str) or not password:
                password_errors.append('a password is required')
            else:
                if len(password) < 9:
                    password_errors.append('at least 9 characters')
                if not re.fullmatch(r'\S+', password):
                    password_errors.append('no spaces')
                if not re.search(r'[A-Z]', password):
                    password_errors.append('at least one uppercase letter')
                if not re.search(r'[0-9]', password):
                    password_errors.append('at least one digit')
                if not re.search(r'[^A-Za-z0-9]', password):
                    password_errors.append('at least one special character')
            if password_errors:
                errors.append(
                    f'Invalid SMS password for {ifname} authorized-number {number}: '
                    + ', '.join(password_errors)
                )
                continue
            authorized_numbers[ifname][number] = password

    enabled_interfaces = sorted(enabled_interfaces, key=_wwan_sort_key)
    return {
        'interfaces': enabled_interfaces,
        'authorized_numbers_json': json.dumps(authorized_numbers, separators=(',', ':')),
        'enabled': bool(enabled_interfaces and authorized_numbers),
        'errors': errors,
    }


def verify(config):
    if not config:
        return None

    errors = config.get('errors', [])
    if errors:
        raise ConfigError(errors[0])

    return None


def generate(config):
    if not config:
        config_file.unlink(missing_ok=True)
        return None

    if not config.get('enabled'):
        config_file.unlink(missing_ok=True)
        return None

    render(config_file, 'wwan/sms-command.j2', config, permission=0o600)

    return None


def apply(config):
    if not config or not config.get('enabled'):
        call(f'systemctl stop {service_name}')
        return None

    call(f'systemctl restart {service_name}')

    return None


if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
