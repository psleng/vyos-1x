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
import re

from vyos.config import Config
from vyos.template import render
from vyos.utils.process import call
from vyos import ConfigError
from vyos import airbag

airbag.enable()

service_name = 'igos-wwan-sms-command.service'
config_file = Path('/etc/default/igos-wwan-sms-command')


def _wwan_sort_key(ifname):
    """Sort WWAN names naturally by numeric suffix (wwan2 after wwan1)."""
    match = re.search(r'(\d+)$', str(ifname))
    return int(match.group(1)) if match else 0


def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()

    base = ['service', 'interface', 'wwan']
    if not conf.exists(base):
        return None

    enabled_interfaces = []
    allowed_senders = set()
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
                f'At least one service interface wwan {ifname} sms-command authorized-number is required'
            )
            continue

        enabled_interfaces.append(ifname)
        allowed_senders.update(allowed)

    enabled_interfaces = sorted(enabled_interfaces, key=_wwan_sort_key)
    return {
        'interfaces': enabled_interfaces,
        'allowed_senders': sorted(allowed_senders),
        'enabled': bool(enabled_interfaces and allowed_senders),
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

    render(config_file, 'wwan/igos-wwan-sms-command.j2', config)

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
