#!/usr/bin/env python3
# Copyright (C) 2024-2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
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

import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from tabulate import tabulate

import vyos.opmode
from vyos.utils.process import rc_cmd

manager_unit = 'igos-wwan-manager.service'
service_name = 'ModemManager.service'

def _stop_manager_service(unit=manager_unit):
    """Stop a manager service and verify that it is no longer active."""
    print(f'Stopping {unit}...')
    return_code, output = rc_cmd(['systemctl', 'stop', unit])
    if return_code:
        error = output or f'Unable to stop {unit} (exit code {return_code})'
        raise vyos.opmode.InternalError(error)

    return_code, _ = rc_cmd(['systemctl', 'is-active', '--quiet', unit])

    if return_code == 0:
        raise vyos.opmode.InternalError(
            f'{unit} is still active; operation aborted'
        )

    print(f'{unit} stopped.')
    return None


def _get_installed_firmware_info() -> dict[str, str]:
    """Return identifying information for the first installed modem."""

    print('Detecting wwan modem...')

    return_code, output = rc_cmd(
        ['mmcli', '--list-modems', '--output-keyvalue']
    )
    if return_code:
        raise vyos.opmode.DataUnavailable(
            output or 'Unable to list ModemManager modems'
        )

    modem_ids = re.findall(r'/Modem/(\d+)', output)
    if not modem_ids:
        raise vyos.opmode.DataUnavailable('No ModemManager modems were found')

    modem_id = modem_ids[0]
    return_code, output = rc_cmd(
        ['mmcli', '--modem', modem_id, '--output-keyvalue']
    )
    if return_code:
        raise vyos.opmode.DataUnavailable(
            output or f'Unable to query ModemManager modem {modem_id}'
        )

    properties = {}
    for line in output.splitlines():
        key, separator, value = line.partition(':')
        if separator:
            properties[key.strip()] = value.strip()

    return {
        'model': properties.get('modem.generic.model') or 'unknown',
        'firmware_revision': (
            properties.get('modem.generic.revision') or 'unknown'
        ),
        'hardware_revision': (
            properties.get('modem.generic.hardware-revision') or 'unknown'
        ),
    }


def _validate_firmware_archive(archive: str) -> Path:
    """Validate a firmware archive and return its path."""
    archive_path = Path(archive)
    print(f'Validating firmware archive: {archive_path}')
    if not archive_path.is_file():
        raise vyos.opmode.IncorrectValue(
            f'Firmware archive does not exist: {archive}'
        )

    if archive_path.suffix.lower() != '.zip':
        raise vyos.opmode.IncorrectValue(
            'Firmware archive must be a .zip file'
        )

    if not zipfile.is_zipfile(archive_path):
        raise vyos.opmode.IncorrectValue(
            f'Invalid firmware ZIP archive: {archive}'
        )
    print('Firmware archive is valid.')
    return archive_path

def _extract_firmware_archive(
    zip_archive: zipfile.ZipFile,
    firmware_file: zipfile.ZipInfo,
    tmp_dir: str,
) -> Path:
    """Extract a firmware archive safely into a temporary directory."""
    firmware_path = Path(tmp_dir) / Path(firmware_file.filename).name
    print(f'Extracting firmware to: {firmware_path}')
    with zip_archive.open(firmware_file) as source:
        with firmware_path.open('wb') as destination:
            shutil.copyfileobj(source, destination)
    return firmware_path

def _detect_firmware_type(
    zip_archive: zipfile.ZipFile,
) -> tuple[str, zipfile.ZipInfo]:
    members = [
        member
        for member in zip_archive.infolist()
        if not member.is_dir()
    ]

    uxfp_files = [
        member
        for member in members
        if Path(member.filename).suffix.lower() == '.bin'
    ]
    tfl_files = [
        member
        for member in members
        if Path(member.filename).name.lower() == 'information.contents'
    ]

    if uxfp_files and tfl_files:
        raise vyos.opmode.IncorrectValue(
            'Firmware archive contains both UXFP and TFL package formats'
        )

    if len(uxfp_files) == 1:
        return 'uxfp', uxfp_files[0]

    if len(uxfp_files) > 1:
        raise vyos.opmode.IncorrectValue(
            'UXFP archive must contain exactly one .bin file'
        )

    if len(tfl_files) == 1:
        return 'tfl', tfl_files[0]

    if len(tfl_files) > 1:
        raise vyos.opmode.IncorrectValue(
            'TFL archive must contain exactly one information.contents file'
        )

    raise vyos.opmode.IncorrectValue(
        'Unsupported firmware archive: expected one .bin file '
        'or one information.contents file'
    )

def _ensure_qcserial_kernel_module_loaded() -> None:
    """Load qcserial when necessary and print its lsmod entry."""
    for attempt in range(2):
        return_code, output = rc_cmd(['lsmod'])
        if return_code:
            raise vyos.opmode.InternalError(
                output or 'Unable to query loaded kernel modules'
            )

        qcserial_lines = [
            line
            for line in output.splitlines()
            if line.split() and line.split()[0] == 'qcserial'
        ]
        if qcserial_lines:
            print(f'lsmod | grep qcserial: {qcserial_lines[0]}')
            return

        if attempt == 0:
            print('Loading qcserial kernel module.')
            return_code, output = rc_cmd(['modprobe', 'qcserial'])
            if return_code:
                raise vyos.opmode.DataUnavailable(
                    output or 'Unable to load qcserial kernel module'
                )

    raise vyos.opmode.DataUnavailable(
        'qcserial kernel module did not load'
    )

def _ensure_tfl_firmware_package_is_valid(firmware_path: Path) -> None:
    """Run WWAN firmware package validation."""
    print('Validating WWAN firmware package.')
    return_code, output = rc_cmd(['tfl', str(firmware_path), '--dry-run'])

    if return_code:
        raise vyos.opmode.IncorrectValue(
            output or 'WWAN firmware package validation failed'
        )
    print('WWAN firmware package validation succeeded.')


def _run_firmware_installer(
    firmware_type: str,
    firmware_path: Path,
) -> None:
    """Stop modem services and run the selected firmware installer."""
    print(f'Firmware extracted to: {firmware_path}')

    if firmware_type == 'uxfp':
        command = ['uxfp', '--file', str(firmware_path)]
    elif firmware_type == 'tfl':
        command = ['tfl', str(firmware_path)]
    else:
        raise vyos.opmode.IncorrectValue(
            f'Unsupported WWAN firmware type: {firmware_type}'
        )

    print(
        f'Using {firmware_type.upper()} tool '
        'to install WWAN firmware.'
    )

    execute_stop_manager()

    if firmware_type == 'tfl':
        _ensure_qcserial_kernel_module_loaded()
        _ensure_tfl_firmware_package_is_valid(firmware_path)

    print(
        f'Starting WWAN firmware installation. '
        'Do not power off or disconnect the modem.'
    )

    try:
        return_code, output = rc_cmd(
            command,
            stdout=None,
        )
    except OSError as error:
        raise vyos.opmode.InternalError(
            f'Unable to run WWAN '
            f'firmware installer: {error}'
        ) from error

    if return_code:
        raise vyos.opmode.InternalError(
            output
            or (
                f'WWAN firmware installation failed '
                f'with exit code {return_code}'
            )
        )

    print(
        f'WWAN firmware installation '
        'completed successfully.'
    )

def execute_stop_manager():
    """Stop the WWAN manager service."""
    _stop_manager_service(manager_unit)
    _stop_manager_service(service_name)
    return None

def set_firmware(archive: str) -> None:
    """Extract a WWAN firmware archive and install its binary stream header."""
    archive_path = _validate_firmware_archive(archive)

    modem_info = _get_installed_firmware_info()
    print(tabulate(
        [[
            modem_info['model'],
            modem_info['hardware_revision'],
            modem_info['firmware_revision'],
        ]],
        headers=['Model', 'Hardware revision', 'Firmware revision'],
    ))

    try:
        with zipfile.ZipFile(archive_path) as zip_archive:
            firmware_type, package_member = _detect_firmware_type(zip_archive)

            with tempfile.TemporaryDirectory(
                prefix='wwan-firmware-'
            ) as tmp_dir:
                if firmware_type == 'uxfp':
                    uxfp_path = shutil.which('uxfp')
                    if uxfp_path is None:
                        raise vyos.opmode.DataUnavailable(
                            'UXFP firmware update tool is not installed'
                        )

                    firmware_path = _extract_firmware_archive(
                        zip_archive,
                        package_member,
                        tmp_dir,
                    )
                    _run_firmware_installer('uxfp', firmware_path)

                else:
                    tfl_path = shutil.which('tfl')
                    if tfl_path is None:
                        raise vyos.opmode.DataUnavailable(
                            'TFL firmware update tool is not installed'
                        )

                    zip_archive.extractall(tmp_dir)
                    manifest_path = Path(tmp_dir) / package_member.filename
                    firmware_path = manifest_path.parent

                    _run_firmware_installer('tfl', firmware_path)
    except zipfile.BadZipFile as error:
        raise vyos.opmode.IncorrectValue(
            f'Invalid firmware archive: {archive}'
        ) from error

    return None


if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
