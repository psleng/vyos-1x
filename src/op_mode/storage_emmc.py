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

from glob import glob
from os import path
from re import match

from tabulate import tabulate

import vyos.opmode

# JEDEC MMC manufacturer (MID) identifiers, as exposed through
# /sys/block/mmcblkX/device/manfid. Only the most common vendors are mapped;
# anything else falls back to the raw identifier.
MMC_MANUFACTURERS = {
    0x02: 'SanDisk',
    0x03: 'SanDisk',
    0x11: 'Toshiba',
    0x13: 'Micron',
    0x15: 'Samsung',
    0x45: 'SanDisk',
    0x70: 'Kingston',
    0x90: 'SK Hynix',
    0xfe: 'Micron',
}

# EXT_CSD PRE_EOL_INFO values (EXT_CSD byte 267).
PRE_EOL_INFO = {
    0x00: 'Not defined',
    0x01: 'Normal',
    0x02: 'Warning (80% of reserved blocks consumed)',
    0x03: 'Urgent (90% of reserved blocks consumed)',
}


def _read_sysfs(file_path):
    try:
        with open(file_path, 'r') as f:
            return f.read().strip()
    except OSError:
        return None


def _decode_life_time(raw):
    # EXT_CSD DEVICE_LIFE_TIME_EST_TYP_A/B (bytes 268/269). The sysfs
    # attribute exposes both values as e.g. "0x01 0x01". Each nibble maps to a
    # 10% band of the estimated device lifetime; 0x0b means the device has
    # exceeded its estimated lifetime.
    if not raw:
        return None

    def _band(value):
        try:
            level = int(value, 16)
        except ValueError:
            return None
        if level == 0x00:
            return 'Not defined'
        if level == 0x0b:
            return 'Exceeded estimated lifetime'
        if 0x01 <= level <= 0x0a:
            lower = (level - 1) * 10
            upper = level * 10
            return f'{lower}% - {upper}% used'
        return None

    values = [_band(v) for v in raw.split()]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return ' / '.join(values)


def _decode_pre_eol(raw):
    if raw is None:
        return None
    try:
        value = int(raw, 16)
    except ValueError:
        return None
    return PRE_EOL_INFO.get(value, f'Unknown ({raw})')


def _human_size(sectors):
    try:
        size = int(sectors) * 512
    except (TypeError, ValueError):
        return None
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or unit == 'TiB':
            if unit == 'B':
                return f'{size} {unit}'
            return f'{size:.1f} {unit}'
        size /= 1024
    return None


def _get_emmc_devices():
    devices = []
    for sys_path in sorted(glob('/sys/block/mmcblk*')):
        name = path.basename(sys_path)
        # Only whole-disk eMMC nodes, not partitions (mmcblk0p1) or the
        # boot/rpmb hardware partitions (mmcblk0boot0, mmcblk0rpmb).
        if not match(r'^mmcblk[0-9]+$', name):
            continue
        device_path = path.join(sys_path, 'device')
        # SD cards also enumerate as mmcblkX; restrict to eMMC (type 'MMC').
        card_type = _read_sysfs(path.join(device_path, 'type'))
        if card_type != 'MMC':
            continue

        manfid_raw = _read_sysfs(path.join(device_path, 'manfid'))
        manufacturer = None
        if manfid_raw:
            try:
                manufacturer = MMC_MANUFACTURERS.get(
                    int(manfid_raw, 16), manfid_raw
                )
            except ValueError:
                manufacturer = manfid_raw

        devices.append(
            {
                'device': f'/dev/{name}',
                'name': _read_sysfs(path.join(device_path, 'name')),
                'manufacturer': manufacturer,
                'serial': _read_sysfs(path.join(device_path, 'serial')),
                'date': _read_sysfs(path.join(device_path, 'date')),
                'firmware': _read_sysfs(path.join(device_path, 'fwrev')),
                'size': _human_size(_read_sysfs(path.join(sys_path, 'size'))),
                'life_time': _decode_life_time(
                    _read_sysfs(path.join(device_path, 'life_time'))
                ),
                'pre_eol_info': _decode_pre_eol(
                    _read_sysfs(path.join(device_path, 'pre_eol_info'))
                ),
            }
        )
    return devices


def _format_devices(devices):
    output = []
    for dev in devices:
        rows = [
            ['Device:', dev['device']],
            ['Name:', dev['name'] or 'unknown'],
            ['Manufacturer:', dev['manufacturer'] or 'unknown'],
            ['Serial Number:', dev['serial'] or 'unknown'],
            ['Manufacturing Date:', dev['date'] or 'unknown'],
            ['Firmware Revision:', dev['firmware'] or 'unknown'],
            ['Capacity:', dev['size'] or 'unknown'],
            ['Life Time Estimation:', dev['life_time'] or 'not available'],
            ['Pre-EOL Information:', dev['pre_eol_info'] or 'not available'],
        ]
        output.append(tabulate(rows, tablefmt='plain'))
    return '\n\n'.join(output)


def show(raw: bool):
    devices = _get_emmc_devices()

    if raw:
        return devices

    if not devices:
        raise vyos.opmode.DataUnavailable('No eMMC devices found')

    return _format_devices(devices)


if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
