# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

"""Architecture-aware access to system hardware identity."""

import platform
from pathlib import Path
from uuid import UUID

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


def _read_serial(path: Path) -> str:
    value = _read_text(path)
    parts = value.split('-')
    if len(parts) == 2 and len(parts[0]) == 3:
        return value
    return ''


def _read_uuid(path: Path, *, binary: bool = False) -> str:
    try:
        data = path.read_bytes()
        value = UUID(bytes=data) if binary else UUID(data.decode().strip())
        return str(value)
    except (OSError, UnicodeDecodeError, ValueError):
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
        uuid = _read_uuid(dmi_path / 'product_uuid')
    else:
        product = _read_text(device_info_path / 'product')
        board_model = _read_text(device_info_path / 'model')
        vendor = _read_text(device_info_path / 'vendor')
        model = '-'.join(value for value in (product, board_model) if value)
        serial = _read_serial(device_info_path / 'serial')
        uuid = _read_uuid(device_info_path / 'uuid', binary=True)

    return {
        'hardware_vendor': vendor or 'Unknown',
        'hardware_model': model or 'Unknown',
        'hardware_serial': serial or 'Unknown',
        'hardware_uuid': uuid or 'Unknown',
    }


def get_board_identity(
    machine: str | None = None,
    dmi_path: Path = DMI_ID_PATH,
    device_info_path: Path = DEVICE_INFO_PATH,
) -> dict[str, str]:
    """Return detailed board identity from DMI (x86) or board NVMEM.

    Unlike :func:`get_hardware_info`, this keeps ``product``, ``model`` and the
    board ``revision`` as separate fields so ``show hardware model`` can render
    each nvmem cell on its own line. Values are returned verbatim (empty string
    when a cell is absent); formatting/fallbacks are the caller's concern.
    """
    if is_x86(machine):
        vendor = _read_text(dmi_path / 'sys_vendor')
        product = _read_text(dmi_path / 'product_name')
        model = ''
        revision = _read_text(dmi_path / 'product_version')
        serial = _read_text(dmi_path / 'product_serial')
        uuid = _read_uuid(dmi_path / 'product_uuid')
    else:
        vendor = _read_text(device_info_path / 'vendor')
        product = _read_text(device_info_path / 'product')
        model = _read_text(device_info_path / 'model')
        revision = _read_text(device_info_path / 'board_revision')
        serial = _read_serial(device_info_path / 'serial')
        uuid = _read_uuid(device_info_path / 'uuid', binary=True)

    return {
        'vendor': vendor,
        'product': product,
        'model': model,
        'revision': revision,
        'serial': serial,
        'uuid': uuid,
    }


def get_nvmem_password(
    machine: str | None = None,
    device_info_path: Path = DEVICE_INFO_PATH,
) -> str:
    """Return the per-device factory ("sticker") password from board NVMEM.

    Read verbatim from the ``password`` nvmem identity cell on non-x86 boards.
    Returns an empty string when the cell is absent, unreadable, or blank — an
    unprogrammed EEPROM cell typically reads back as all ``0xFF`` (or all
    ``0x00``), which must NOT be treated as a usable password.  The caller
    decides the fallback (e.g. the normal default password) when this is empty.

    This value is plaintext (a label/sticker password); callers are expected to
    hash it before applying it to ``/etc/shadow``.
    """
    # x86 boards carry no board nvmem identity EEPROM.
    if is_x86(machine):
        return ''

    try:
        raw = (device_info_path / 'password').read_bytes()
    except OSError:
        return ''

    # Reject an unprogrammed/blank cell (all 0xFF or all 0x00 fill).
    stripped = raw.strip(b'\xff').strip(b'\x00')
    if not stripped:
        return ''

    value = raw.decode('utf-8', errors='replace').strip()
    # A replacement character means non-UTF-8 bytes (garbage / binary fill);
    # treat the cell as unusable rather than applying a corrupt password.
    if not value or '\ufffd' in value:
        return ''

    return value


def get_stable_hardware_id(
    machine: str | None = None,
    dmi_path: Path = DMI_ID_PATH,
    device_info_path: Path = DEVICE_INFO_PATH,
) -> str:
    """Return the best stable per-system identifier available."""
    if is_x86(machine):
        return _read_uuid(dmi_path / 'product_uuid') or _read_text(
            dmi_path / 'product_serial'
        )
    return _read_uuid(device_info_path / 'uuid', binary=True) or _read_serial(
        device_info_path / 'serial'
    )
