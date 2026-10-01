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

import subprocess
import sys

from list_pki_with_tpm import get_pki_certificates

from vyos.tpm import tpm_exist
from vyos.tpm_pki import get_tpm_list


def tpm_mode_enabled():
    if not tpm_exist():
        return False

    try:
        result = subprocess.run(
            [
                'sudo',
                '-n',
                sys.executable,
                '-c',
                'from vyos.tpm import tpm_enabled; print(int(tpm_enabled()))',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0 or result.stdout.strip() not in ('0', '1'):
        return None
    return result.stdout.strip() == '1'


if __name__ == '__main__':
    mode = tpm_mode_enabled()
    if mode is True:
        certificates = get_tpm_list('cert')
    elif mode is False:
        certificates = get_pki_certificates()
    else:
        certificates = []

    if certificates:
        print(' '.join(certificates))
