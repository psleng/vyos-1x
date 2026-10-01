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

from list_pki_with_tpm import get_pki_certificates

from vyos.tpm import tpm_enabled
from vyos.tpm import tpm_exist
from vyos.tpm_pki import get_tpm_list


if __name__ == '__main__':
    if tpm_exist() and tpm_enabled():
        certificates = get_tpm_list('cert')
    else:
        certificates = get_pki_certificates()

    if certificates:
        print(' '.join(certificates))
