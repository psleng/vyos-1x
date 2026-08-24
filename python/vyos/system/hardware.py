# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

"""Architecture-aware access to system hardware identity."""

import platform
from pathlib import Path

DMI_ID_PATH = Path('/sys/class/dmi/id')
DEVICE_INFO_PATH = Path('/sys/bus/platform/devices/device-info')

_X86_MACHINES = frozenset(
    {
        'x86_64',
        'amd64',
        'i386',
        'i486',
        'i586',
        'i686',
    }
)


def is_x86(machine: str | None = None) -> bool:
    """Return whether the running kernel architecture is x86."""
    return (machine or platform.machine()).lower() in _X86_MACHINES


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode('utf-8', errors='replace').strip()
    except OSError:
        return ''


def get_hardware_info(
    machine: str | None = None,
    dmi_path: Path = DMI_ID_PATH,
    device_info_path: Path = DEVICE_INFO_PATH,
) -> dict[str, str]:
    """Return version-report hardware fields from DMI or board NVMEM."""
    if is_x86(machine):
        vendor = _read_text(dmi_path / 'sys_vendor')
        model = _read_text(dmi_path / 'product_name')
        serial = _read_text(dmi_path / 'product_serial')
        uuid = _read_text(dmi_path / 'product_uuid')
    else:
        product = _read_text(device_info_path / 'product')
        board_model = _read_text(device_info_path / 'model')
        vendor = ''
        model = '-'.join(value for value in (product, board_model) if value)
        serial = _read_text(device_info_path / 'serial')
        uuid = ''

    return {
        'hardware_vendor': vendor or 'Unknown',
        'hardware_model': model or 'Unknown',
        'hardware_serial': serial or 'Unknown',
        'hardware_uuid': uuid or 'Unknown',
    }


def get_stable_hardware_id(
    machine: str | None = None,
    dmi_path: Path = DMI_ID_PATH,
    device_info_path: Path = DEVICE_INFO_PATH,
) -> str:
    """Return the best stable per-system identifier available."""
    if is_x86(machine):
        return _read_text(dmi_path / 'product_uuid') or _read_text(
            dmi_path / 'product_serial'
        )
    return _read_text(device_info_path / 'serial')
