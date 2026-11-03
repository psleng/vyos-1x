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

import os
import platform
import sys
import typing

import vyos.opmode

from vyos.configquery import ConfigTreeQuery
from vyos.utils.process import rc_cmd

openssl_fipsmodule_config = '/run/ssl/fipsmodule.cnf'
fips_reboot_marker = '/config/.fips_reboot_required'
fips_selftest_failed_marker = '/run/vyos-fips-selftest-failed'
kernel_fips_flag = '/proc/sys/crypto/fips_enabled'

arch_map = {
    'x86_64': 'x86_64-linux-gnu',
    'aarch64': 'aarch64-linux-gnu',
}


def _fips_module_path() -> typing.Optional[str]:
    arch = arch_map.get(platform.machine())
    if arch is None:
        return None
    return f'/usr/lib/{arch}/ossl-modules/fips.so'


def _provider_active() -> bool:
    # The FIPS provider is active when 'openssl list -providers' reports a
    # 'fips' entry with status active.
    rc, out = rc_cmd('openssl list -providers')
    if rc != 0:
        return False
    in_fips = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped == 'fips':
            in_fips = True
            continue
        if in_fips:
            if stripped.startswith('status:'):
                value = stripped.split(':', 1)[1].strip()
                return value == 'active'
            # a new top-level provider name (no leading whitespace) ends the
            # fips block without a status line
            if line and not line[0].isspace():
                in_fips = False
    return False


def _kernel_fips_enabled() -> typing.Optional[bool]:
    if not os.path.exists(kernel_fips_flag):
        return None
    try:
        with open(kernel_fips_flag) as f:
            return f.read().strip() == '1'
    except OSError:
        return None


def _get_raw_data() -> dict:
    config = ConfigTreeQuery()
    module = _fips_module_path()

    return {
        'configured': config.exists(['system', 'option', 'fips']),
        'module_path': module,
        'module_present': bool(module) and os.path.exists(module),
        'provider_active': _provider_active(),
        'self_test_config_present': os.path.exists(openssl_fipsmodule_config),
        'self_test_failed': os.path.exists(fips_selftest_failed_marker),
        'kernel_fips_enabled': _kernel_fips_enabled(),
        'reboot_required': os.path.exists(fips_reboot_marker),
    }


def _yesno(value) -> str:
    if value is None:
        return 'n/a'
    return 'yes' if value else 'no'


def _format(data: dict) -> str:
    lines = []
    lines.append('FIPS 140-3 approved mode')
    lines.append('')

    if data['self_test_failed']:
        lines.append('  *** FIPS MODULE SELF-TEST FAILED at boot ***')
        lines.append('  The system is NOT operating in FIPS mode and network '
                     'crypto services have')
        lines.append('  been disabled. Investigate the validated module, then '
                     'reboot to retry.')
        lines.append('')

    lines.append(f'  Configured:          {_yesno(data["configured"])}')
    lines.append(f'  Provider active:     {_yesno(data["provider_active"])}')
    lines.append(f'  Module self-test:    {_yesno(data["self_test_config_present"])}')
    lines.append(f'  Validated module:    {_yesno(data["module_present"])}'
                 + (f' ({data["module_path"]})' if data['module_path'] else ''))
    lines.append(f'  Kernel FIPS:         {_yesno(data["kernel_fips_enabled"])}')
    lines.append('')

    if data['reboot_required']:
        lines.append('  A FIPS mode change is pending: save the configuration '
                     'and reboot to apply.')
    elif data['configured'] and not data['provider_active'] and not data['self_test_failed']:
        lines.append('  FIPS is configured but the provider is not active: '
                     'a reboot is required to fully apply.')

    # Phase 0/1 scope note: the provider is switched, but system-wide approved
    # operation (algorithm restriction, non-approved feature refusal) is not yet
    # enforced.
    lines.append('  Note: FIPS 140-3 enforcement is incomplete; only the '
                 'OpenSSL provider is switched.')

    return '\n'.join(lines)


def show(raw: bool):
    data = _get_raw_data()
    if raw:
        return data
    return _format(data)


if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
