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
import sys
import typing

import vyos.opmode

from vyos import fips
from vyos.configquery import ConfigTreeQuery
from vyos.utils.process import rc_cmd

openssl_fipsmodule_config = '/run/ssl/fipsmodule.cnf'
fips_booted_marker = '/run/vyos-fips-booted'
fips_selftest_failed_marker = '/run/vyos-fips-selftest-failed'
kernel_fips_flag = '/proc/sys/crypto/fips_enabled'


def _active_fips_provider() -> dict:
    # The provider the running system's openssl.cnf actually loads.
    rc, out = rc_cmd('openssl list -providers')
    if rc != 0:
        return {}
    return fips.parse_providers(out).get('fips', {})


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
    module = fips.provider_module_path()
    manifest = fips.read_provider_manifest() or {}
    module_errors = fips.verify_provider_module()
    provider = _active_fips_provider()

    configured = config.exists(['system', 'option', 'fips'])
    booted_fips = os.path.exists(fips_booted_marker)
    self_test_failed = os.path.exists(fips_selftest_failed_marker)

    return {
        'configured': configured,
        'module_path': module,
        'module_present': bool(module) and os.path.exists(module),
        'module_verified': not module_errors,
        'module_errors': module_errors,
        'expected_version': manifest.get('version'),
        'provider_version': provider.get('version'),
        'provider_active': provider.get('status') == 'active',
        'self_test_config_present': os.path.exists(openssl_fipsmodule_config),
        'self_test_failed': self_test_failed,
        'kernel_fips_enabled': _kernel_fips_enabled(),
        'booted_fips': booted_fips,
        # Level-triggered and self-correcting: a reboot is pending whenever the
        # configured state differs from the state the system actually booted
        # into. The failure banner already instructs a reboot, so suppress the
        # generic pending line in that case.
        'reboot_required': (configured != booted_fips) and not self_test_failed,
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
    lines.append(f'  Provider version:    {data["provider_version"] or "n/a"}'
                 + (f' (validated: {data["expected_version"]})'
                    if data['expected_version'] else ''))
    lines.append(f'  Validated module:    {_yesno(data["module_verified"])}'
                 + (f' ({data["module_path"]})' if data['module_path'] else ''))
    lines.append(f'  Kernel FIPS:         {_yesno(data["kernel_fips_enabled"])}')
    lines.append('')

    if data['provider_active'] and (
        not data['module_verified']
        or data['provider_version'] != data['expected_version']
    ):
        lines.append('  *** The active FIPS provider is NOT the validated build ***')
        for error in data['module_errors']:
            lines.append(f'  {error}')
        if data['provider_version'] != data['expected_version']:
            lines.append(f'  Loaded provider version {data["provider_version"]}, '
                         f'validated version {data["expected_version"]}.')
        lines.append('')

    if data['reboot_required']:
        if data['configured']:
            lines.append('  FIPS is enabled in the configuration but this boot is '
                         'not running in FIPS mode:')
        else:
            lines.append('  FIPS is disabled in the configuration but this boot is '
                         'still running in FIPS mode:')
        lines.append('  save the configuration and reboot to apply.')

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
