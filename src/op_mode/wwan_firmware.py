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

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from enum import Enum
from pathlib import Path

import vyos.opmode
from vyos.remote import download
from vyos.utils.io import ask_yes_no
from vyos.utils.process import rc_cmd

MANAGER_UNIT = 'igos-wwan-manager.service'
FIRMWARE_ROOT = Path('/usr/lib/modem')
FIRMWARE_MAP_NAME = 'firmware-map.conf'
FIRMWARE_DOWNLOAD_URL = 'https://download.perle.com/Engineering/updates'

class ModemManufacturer(str, Enum):
    TELIT = "Telit"
    TELIT_CINTERION = "Telit Cinterion"

manufacturer_vendors = {
    ModemManufacturer.TELIT: "telit",
    ModemManufacturer.TELIT_CINTERION: "telit",
}

def _stop_modem_manager_service(unit=MANAGER_UNIT) -> bool:
    """Stop the manager service and return whether it was active."""
    return_code, _ = rc_cmd(['systemctl', 'is-active', '--quiet', unit])
    if return_code:
        print(f'{unit} is not active; no stop is required.')
        return False

    print(f'Stopping {unit}...')
    return_code, output = rc_cmd(['systemctl', 'stop', unit])
    if return_code:
        error = output or f'Unable to stop {unit} (exit code {return_code})'
        raise vyos.opmode.InternalError(error)

    return_code, _ = rc_cmd(['systemctl', 'is-active', '--quiet', unit])

    if return_code == 0:
        raise vyos.opmode.InternalError(f'{unit} is still active; operation aborted')

    print(f'{unit} stopped.')
    return True

def _restore_manager_service(was_active: bool) -> None:
    """Restart the manager service if it was previously active."""
    if not was_active:
        return

    print(f'Restarting {MANAGER_UNIT}...')
    return_code, output = rc_cmd(['systemctl', 'start', MANAGER_UNIT])
    if return_code:
        raise vyos.opmode.InternalError(
            output or f'Unable to restart {MANAGER_UNIT} (exit code {return_code})'
        )
    print(f'{MANAGER_UNIT} restarted.')

def _get_wwan_interfaces() -> list[str]:
    """Return WWAN network interfaces currently exposed by the kernel."""
    return sorted(
        path.name for path in Path('/sys/class/net').glob('wwan*') if path.is_dir()
    )

def _select_wwan_interface(interface: str) -> str:
    """Validate and return an explicitly selected WWAN interface."""
    interfaces = _get_wwan_interfaces()
    if not interfaces:
        raise vyos.opmode.DataUnavailable('No WWAN interfaces were detected')

    if interface not in interfaces:
        raise vyos.opmode.IncorrectValue(
            f'WWAN interface {interface} does not exist; '
            f'available interfaces: {", ".join(interfaces)}'
        )
    print(f'Selected WWAN interface: {interface}')
    return interface

def _get_usb_device_path(interface: str) -> Path:
    """Return the physical USB parent for a WWAN network interface."""
    device_link = Path('/sys/class/net') / interface / 'device'
    try:
        interface_path = device_link.resolve(strict=True)
    except OSError as error:
        raise vyos.opmode.DataUnavailable(
            f'Unable to resolve the physical device for {interface}: {error}'
        ) from error

    usb_device = interface_path.parent
    if not (usb_device / 'idVendor').is_file():
        raise vyos.opmode.DataUnavailable(
            f'Unable to find the physical USB device for {interface}'
        )
    print(f'Physical USB device for {interface}: {usb_device}')
    return usb_device

def _get_modem_serial_ports(usb_device: Path) -> list[Path]:
    """Return ttyUSB device nodes belonging to one physical USB modem."""
    names = {
        path.name
        for path in usb_device.rglob('ttyUSB*')
        if path.name.startswith('ttyUSB')
    }
    ports = [Path('/dev') / name for name in sorted(names)]
    return [port for port in ports if port.exists()]

def _send_at_command(
    port: Path,
    command: str,
    timeout: float = 2.0,
) -> tuple[bool, list[str]]:
    """Send one AT command through Minicom and return its response."""
    minicom = shutil.which('minicom')
    if minicom is None:
        raise vyos.opmode.DataUnavailable(
            'Minicom is required to query the modem AT port'
        )

    script_timeout = max(1, int(timeout + 0.999))
    minicom_env = os.environ.copy()
    minicom_env['TERM'] = 'xterm'

    with tempfile.TemporaryDirectory(prefix='wwan-minicom-') as tmp_dir:
        script_path = Path(tmp_dir) / 'query.run'
        capture_path = Path(tmp_dir) / 'response.log'
        script_path.write_text(
            f'timeout {script_timeout}\n'
            f'send "{command}^M"\n'
            'expect {\n'
            '    "OK" exit 0\n'
            '    "ERROR" exit 1\n'
            '}\n'
            'exit 1\n'
        )

        try:
            result = subprocess.run(
                [
                    minicom,
                    '--device',
                    str(port),
                    '--baudrate',
                    '115200',
                    '--noinit',
                    '--capturefile',
                    str(capture_path),
                    '--script',
                    str(script_path),
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout + 3,
                start_new_session=True,
                env=minicom_env,
            )
            process_output = '\n'.join(
                part for part in (result.stdout, result.stderr) if part
            )
        except subprocess.TimeoutExpired as error:
            process_output = '\n'.join(
                str(part) for part in (error.stdout, error.stderr) if part
            )

        captured = (
            capture_path.read_text(errors='replace') if capture_path.exists() else ''
        )
        response = captured or process_output

    lines = [
        line.strip()
        for line in response.replace('\r', '\n').splitlines()
        if line.strip()
    ]
    success = 'OK' in lines and 'ERROR' not in lines
    data_lines = [line for line in lines if line not in (command, 'OK', 'ERROR')]
    return success, data_lines

def _get_uxfp_diagnostic_port(
    firmware_path: Path,
    usb_device: Path,
) -> Path:
    """Return the UXFP reported diagnostic port for one physical modem."""
    modem_ports = set(_get_modem_serial_ports(usb_device))
    if not modem_ports:
        raise vyos.opmode.DataUnavailable(
            f'No serial ports were found below {usb_device}'
        )

    return_code, output = rc_cmd(
        [
            'uxfp',
            '--file',
            str(firmware_path),
            '--show-diag-ports',
        ]
    )
    if return_code:
        raise vyos.opmode.DataUnavailable(
            output or 'Unable to query UXFP diagnostic ports'
        )

    reported_ports = {
        Path(port)
        for port in re.findall(
            r'diag port:\s*(/dev/ttyUSB\d+)',
            output,
            flags=re.IGNORECASE,
        )
    }
    matching_ports = sorted(modem_ports & reported_ports)
    if not matching_ports:
        raise vyos.opmode.DataUnavailable(
            'UXFP did not report a compatible diagnostic port '
            f'for the selected modem at {usb_device}'
        )
    if len(matching_ports) > 1:
        raise vyos.opmode.DataUnavailable(
            'UXFP reported multiple diagnostic ports for the selected modem: '
            f'{", ".join(str(port) for port in matching_ports)}'
        )

    return matching_ports[0]

def _get_tfl_diagnostic_port(
    firmware_path: Path,
    usb_device: Path,
) -> Path:
    """Validate TFL compatibility and return the diagnostic port."""
    print('Validating TFL firmware compatibility and modem ports...')
    return_code, output = rc_cmd(
        [
            'tfl',
            str(firmware_path),
            '--dry-run',
            '--debug',
        ]
    )
    if return_code:
        raise vyos.opmode.IncorrectValue(
            output or 'TFL firmware compatibility validation failed'
        )

    modem_ports = _get_modem_serial_ports(usb_device)
    if not modem_ports:
        raise vyos.opmode.DataUnavailable(
            f'No serial ports were found below {usb_device}'
        )

    print(
        'TFL modem serial-port group: '
        f'{", ".join(str(port) for port in modem_ports)}'
    )

    diagnostic_port = max(
        modem_ports,
        key=lambda port: int(re.search(r'\d+$', port.name).group()),
    )

    print('TFL firmware compatibility validation succeeded.')
    return diagnostic_port

def _display_firmware_details(
    interface: str,
    diagnostic_port: Path,
    firmware_manifest: dict[str, object],
    modem_identity: dict[str, str],
) -> None:
    """Display validated firmware metadata and the selected modem identity."""
    print(f'Diagnostic port selected: {diagnostic_port}')

    print('\nFirmware file details:')
    for field, value in firmware_manifest.items():
        if field == 'payload_sha256':
            continue
        label = field.replace('_', ' ').capitalize()
        print(f'{label:<24}: {value}')

    print(f'\nCurrent {interface} details:')
    print(f'{"Interface":<24}: {interface}')
    print(f'{"Model":<24}: {modem_identity["model"]}')
    print(f'{"Firmware revision":<24}: {modem_identity["firmware_revision"]}')
    print(
        f'{"Software package version":<24}: '
        f'{modem_identity["software_package_version"]}'
    )
    print(f'{"H/W revision":<24}: {modem_identity["hardware_revision"]}')
    print()

def _parse_firmware_map(map_path: Path) -> list[dict[str, object]]:
    """Parse ordered firmware-map rules while preserving duplicate sections."""
    rules: list[dict[str, object]] = []
    current_rule: dict[str, object] | None = None

    try:
        lines = map_path.read_text(encoding='utf-8').splitlines()
    except OSError as error:
        raise vyos.opmode.DataUnavailable(
            f'Unable to read modem firmware map {map_path}: {error}'
        ) from error

    for line_number, original_line in enumerate(lines, start=1):
        line = original_line.strip()
        if not line or line.startswith(('#', ';')):
            continue

        section = re.fullmatch(r'\[([^]]+)\]', line)
        if section:
            current_rule = {
                'modem_model': section.group(1).strip(),
                'map_path': map_path,
                'line_number': line_number,
            }
            rules.append(current_rule)
            continue

        if current_rule is None or '=' not in line:
            raise vyos.opmode.IncorrectValue(
                f'Invalid firmware map entry at {map_path}:{line_number}'
            )

        key, value = (part.strip() for part in line.split('=', 1))
        if not key or not value:
            raise vyos.opmode.IncorrectValue(
                f'Invalid firmware map entry at {map_path}:{line_number}'
            )
        current_rule[key] = value

    return rules

def _get_manufacturer_type(value: str) -> ModemManufacturer:
    """Return the supported manufacturer type for an external name."""
    try:
        return ModemManufacturer(value.strip())
    except ValueError as error:
        raise vyos.opmode.IncorrectValue(
            f'Unsupported modem manufacturer: {value}'
        ) from error

def _get_vendor_name(value: str) -> str:
    """Return the vendor directory for a modem manufacturer name."""
    return manufacturer_vendors[_get_manufacturer_type(value)]

def _get_modem_identity(interface: str) -> dict[str, str]:
    """Return the selected modem identity using AT commands."""
    if not re.fullmatch(r'wwan\d+', interface):
        raise vyos.opmode.IncorrectValue(f'Invalid WWAN interface: {interface}')

    device_link = Path('/sys/class/net') / interface / 'device'
    try:
        interface_path = device_link.resolve(strict=True)
    except OSError as error:
        raise vyos.opmode.DataUnavailable(
            f'Unable to resolve the physical device for {interface}: {error}'
        ) from error

    usb_device = interface_path.parent
    ports = _get_modem_serial_ports(usb_device)
    if not ports:
        raise vyos.opmode.DataUnavailable(
            f'No modem serial ports were found for {interface}'
        )

    errors = []
    for port in ports:
        try:
            handshake_ok, _ = _send_at_command(port, 'AT')
            if not handshake_ok:
                continue

            manufacturer_ok, manufacturer_lines = _send_at_command(
                port, 'AT+CGMI', timeout=3.0
            )
            model_ok, model_lines = _send_at_command(
                port, 'AT+CGMM', timeout=3.0
            )
            hardware_ok, hardware_lines = _send_at_command(
                port, 'AT#HWREV', timeout=3.0
            )
            revision_ok, revision_lines = _send_at_command(
                port, 'AT+CGMR', timeout=3.0
            )
            package_ok, package_lines = _send_at_command(
                port, 'AT#SWPKGV', timeout=3.0
            )
            manufacturer = ';'.join(manufacturer_lines).strip()
            model = ';'.join(model_lines).strip()
            hardware_revision = ';'.join(hardware_lines).strip()
            firmware_revision = ';'.join(revision_lines).strip()
            software_package_version = ';'.join(package_lines).strip()
            firmware_version = software_package_version.partition(';')[0].partition(
                '-'
            )[0]
            if (
                manufacturer_ok
                and model_ok
                and hardware_ok
                and revision_ok
                and package_ok
                and manufacturer
                and model
                and hardware_revision
                and firmware_revision
                and firmware_version
            ):
                return {
                    'manufacturer': manufacturer,
                    'model': model,
                    'hardware_revision': hardware_revision,
                    'firmware_revision': firmware_revision,
                    'software_package_version': software_package_version,
                    'firmware_version': firmware_version,
                }
        except OSError as error:
            errors.append(f'{port}: {error}')

    detail = f' ({", ".join(errors)})' if errors else ''
    raise vyos.opmode.DataUnavailable(
        f'Unable to read modem identity for {interface} using AT commands{detail}'
    )

def _get_compatible_firmware(
    interface: str,
    root: Path = FIRMWARE_ROOT,
    show_progress: bool = False,
) -> dict[str, object]:
    """Return the first firmware-map rule matching the selected modem."""
    if show_progress:
        print('Detecting modem identity...', end='', flush=True)
    modem_identity = _get_modem_identity(interface)
    if show_progress:
        print(' Done', flush=True)

    if show_progress:
        print('Checking firmware compatibility...', end='', flush=True)
    model = modem_identity['model']
    hardware_revision = modem_identity['hardware_revision']
    map_paths = sorted(root.glob(f'*/{FIRMWARE_MAP_NAME}'))
    if not map_paths:
        raise vyos.opmode.DataUnavailable(
            f'No modem firmware maps were found below {root}'
        )

    for map_path in map_paths:
        for rule in _parse_firmware_map(map_path):
            if rule['modem_model'] != model:
                continue
            if str(rule.get('hardware-revision') or '') != hardware_revision:
                continue

            variant = str(rule.get('variant') or '').strip()
            if not variant or Path(variant).name != variant:
                raise vyos.opmode.IncorrectValue(
                    f'The matching rule in {map_path} has an invalid variant'
                )
            versions = [
                version.strip()
                for version in str(rule.get('compatible-firmware') or '').split(',')
                if version.strip()
            ]
            if not versions:
                raise vyos.opmode.IncorrectValue(
                    f'The matching rule in {map_path} has no compatible firmware'
                )
            if show_progress:
                print(' Done', flush=True)
            return {
                'vendor': map_path.parent.name,
                'variant': variant,
                'versions': versions,
                'modem': modem_identity,
            }

    raise vyos.opmode.DataUnavailable(
        f'No available firmware rule matches {model} hardware revision '
        f'{hardware_revision}'
    )

def _validate_firmware_files(
    version_dir: Path,
    manufacturer: str,
    model: str,
    variant: str,
    hardware_revision: str,
    version: str,
) -> tuple[Path, str, dict[str, object]]:
    """Validate a manifest and return its payload, installer, and metadata."""
    manifest_path = version_dir / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except OSError as error:
        raise vyos.opmode.DataUnavailable(
            f'Unable to read firmware manifest {manifest_path}: {error}'
        ) from error
    except json.JSONDecodeError as error:
        raise vyos.opmode.IncorrectValue(
            f'Invalid firmware manifest {manifest_path}: {error}'
        ) from error

    manifest_manufacturer = str(manifest.get('manufacturer') or '')
    if _get_vendor_name(manifest_manufacturer) != _get_vendor_name(manufacturer):
        raise vyos.opmode.IncorrectValue(
            f'Firmware manifest manufacturer {manifest_manufacturer} does not '
            f'match modem manufacturer {manufacturer}'
        )

    expected = {
        'model': model,
        'variant': variant,
        'hardware_revision': hardware_revision,
        'package_version': version,
    }
    for field, expected_value in expected.items():
        if str(manifest.get(field) or '') != expected_value:
            raise vyos.opmode.IncorrectValue(
                f'Firmware manifest {field} does not match {expected_value}'
            )

    installer = str(manifest.get('installer') or '').lower()
    if installer not in ('tfl', 'uxfp'):
        raise vyos.opmode.IncorrectValue(
            f'Firmware manifest has an unsupported installer: {installer}'
        )

    payload_name = str(manifest.get('payload') or '')
    if not payload_name or Path(payload_name).name != payload_name:
        raise vyos.opmode.IncorrectValue(
            f'Firmware manifest has an invalid payload name: {payload_name}'
        )
    payload_path = version_dir / payload_name
    if not payload_path.exists():
        raise vyos.opmode.IncorrectValue(
            f'Firmware payload does not exist: {payload_path}'
        )

    expected_sha256 = str(manifest.get('payload_sha256') or '').lower()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise vyos.opmode.IncorrectValue(
            'Firmware manifest payload_sha256 must contain 64 hexadecimal characters'
        )
    actual_sha256 = _get_firmware_payload_sha256(payload_path)
    if actual_sha256 != expected_sha256:
        raise vyos.opmode.IncorrectValue(
            f'Firmware payload SHA-256 does not match {manifest_path}'
        )
    print('Validating firmware payload... Done')
    return payload_path, installer, manifest

def _get_firmware_payload_sha256(payload_path: Path) -> str:
    """Return a deterministic SHA-256 digest for a payload file or directory."""
    digest = hashlib.sha256()
    if payload_path.is_file():
        with payload_path.open('rb') as payload_file:
            for chunk in iter(lambda: payload_file.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    if not payload_path.is_dir():
        raise vyos.opmode.IncorrectValue(
            f'Firmware payload is not a regular file or directory: {payload_path}'
        )

    payload_files = sorted(
        path for path in payload_path.rglob('*') if path.is_file()
    )
    if not payload_files:
        raise vyos.opmode.IncorrectValue(
            f'Firmware payload directory is empty: {payload_path}'
        )

    for payload_file in payload_files:
        if payload_file.is_symlink():
            raise vyos.opmode.IncorrectValue(
                f'Firmware payload directory contains a symbolic link: '
                f'{payload_file}'
            )
        relative_path = payload_file.relative_to(payload_path).as_posix().encode()
        file_digest = hashlib.sha256()
        with payload_file.open('rb') as file_stream:
            for chunk in iter(lambda: file_stream.read(1024 * 1024), b''):
                file_digest.update(chunk)
        digest.update(relative_path)
        digest.update(b'\0')
        digest.update(file_digest.hexdigest().encode())
        digest.update(b'\n')
    return digest.hexdigest()

def _download_and_install_firmware_package(
    vendor: str,
    model: str,
    variant: str,
    hardware_revision: str,
    version: str,
) -> None:
    """Download and install the Debian package for an available firmware version."""
    for value, label in (
        (vendor, 'vendor'),
        (model, 'model'),
        (variant, 'variant'),
        (hardware_revision, 'hardware revision'),
        (version, 'version'),
    ):
        if not re.fullmatch(r'[A-Za-z0-9.-]+', value):
            raise vyos.opmode.IncorrectValue(
                f'Invalid firmware package {label}: {value}'
            )

    return_code, architecture = rc_cmd(['dpkg', '--print-architecture'])
    architecture = architecture.strip()
    if return_code or not re.fullmatch(r'[a-z0-9]+', architecture):
        raise vyos.opmode.DataUnavailable(
            'Unable to determine the Debian system architecture'
        )

    package_name = (
        f'cellular_firmware_{model}_{hardware_revision}_{version}_'
        f'{architecture}.deb'
    )
    download_name = f'{Path(package_name).stem}.zip'
    package_url = (
        f'{FIRMWARE_DOWNLOAD_URL}/cellular_firmware/modem/{vendor}/{model}/'
        f'{variant}/{hardware_revision}/{version}/{download_name}'
    )

    with tempfile.TemporaryDirectory(prefix='cellular-firmware-') as tmp_dir:
        download_path = Path(tmp_dir) / download_name
        package_path = Path(tmp_dir) / package_name
        print(f'Downloading firmware package: {package_url}')
        try:
            download(
                str(download_path),
                package_url,
                progressbar=True,
                check_space=True,
                raise_error=True,
            )
        except Exception as error:
            raise vyos.opmode.DataUnavailable(
                f'Unable to download firmware {version} from download.perle.com: '
                'verify network connectivity and retry.'
            ) from error
        download_path.rename(package_path)
        Path(tmp_dir).chmod(0o755)
        package_path.chmod(0o644)

        print(f'Installing firmware package: {package_name}')
        try:
            install = subprocess.run(
                [
                    'apt-get',
                    'install',
                    '--yes',
                    '--reinstall',
                    str(package_path),
                ],
                check=False,
            )
        except OSError as error:
            raise vyos.opmode.DataUnavailable(
                f'Unable to run firmware package installer: {error}'
            ) from error
        if install.returncode:
            raise vyos.opmode.InternalError(
                f'Unable to install firmware package {package_name}'
            )

def _resolve_firmware_files(
    interface: str,
    version: str,
    root: Path = FIRMWARE_ROOT,
) -> dict[str, object]:
    """Prepare an available firmware version for installation."""
    compatible = _get_compatible_firmware(interface, root, show_progress=True)
    modem = compatible['modem']
    vendor = str(compatible['vendor'])
    variant = str(compatible['variant'])
    versions = compatible['versions']
    manufacturer = modem['manufacturer']
    model = modem['model']
    hardware_revision = modem['hardware_revision']

    if vendor != _get_vendor_name(manufacturer):
        raise vyos.opmode.IncorrectValue(
            f'Modem manufacturer {manufacturer} does not match firmware vendor '
            f'{vendor}'
        )
    if version not in versions:
        raise vyos.opmode.IncorrectValue(
            f'Firmware {version} is not available for the modem on {interface}; '
            f'compatible versions: {", ".join(versions)}'
        )
    version_dir = root / vendor / model / variant / hardware_revision / version
    if not version_dir.is_dir():
        _download_and_install_firmware_package(
            vendor,
            model,
            variant,
            hardware_revision,
            version,
        )
    if not version_dir.is_dir():
        raise vyos.opmode.DataUnavailable(
            f'Firmware package installation did not create {version_dir}'
        )
    payload_path, installer, firmware_manifest = _validate_firmware_files(
        version_dir,
        manufacturer,
        model,
        variant,
        hardware_revision,
        version,
    )
    return {
        'payload': payload_path,
        'installer': installer,
        'manifest': firmware_manifest,
        'modem': modem,
    }

def _ensure_qcserial_kernel_module_loaded() -> None:
    """Load qcserial when necessary and print its lsmod entry."""
    print('Checking for qcserial kernel module...')
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
            print(f'qcserial: {qcserial_lines[0]}')
            return

        if attempt == 0:
            print('Loading qcserial kernel module.')
            return_code, output = rc_cmd(['modprobe', 'qcserial'])
            if return_code:
                raise vyos.opmode.DataUnavailable(
                    output or 'Unable to load qcserial kernel module'
                )

    raise vyos.opmode.DataUnavailable('qcserial kernel module did not load')

def _run_uxfp_installer(command: list[str]) -> tuple[int, str]:
    """Run UXFP and display output beginning with the flashing workflow."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = bytearray()
    display_output = False
    marker = b'[+] checking connected devices...'

    if process.stdout is None:
        raise vyos.opmode.InternalError('Unable to read UXFP output')

    while chunk := process.stdout.read(4096):
        previous_length = len(output)
        output.extend(chunk)
        if not display_output:
            marker_position = output.find(marker)
            if marker_position >= 0:
                display_output = True
                visible_output = output[marker_position:]
            else:
                continue
        else:
            visible_output = output[previous_length:]

        sys.stdout.write(visible_output.decode(errors='replace'))
        sys.stdout.flush()

    return_code = process.wait()
    error_output = '' if display_output else output.decode(errors='replace')
    return return_code, error_output

def _run_firmware_installer(
    firmware_type: str,
    firmware_path: Path,
    diagnostic_port: Path | None,
) -> None:
    """Run the selected firmware installer against the selected modem."""

    if diagnostic_port is None:
        raise vyos.opmode.DataUnavailable(
            'No diagnostic port was found for the selected WWAN interface'
        )

    if firmware_type == 'uxfp':
        command = [
            'uxfp',
            '--file',
            str(firmware_path),
        ]
        command.extend(['--port', str(diagnostic_port)])
    elif firmware_type == 'tfl':
        command = ['tfl', str(firmware_path)]
        command.extend(
            [
                '--port',
                str(diagnostic_port),
                '--force',
            ]
        )
    else:
        raise vyos.opmode.IncorrectValue(
            f'Unsupported WWAN firmware type: {firmware_type}'
        )

    print(f'Using {firmware_type.upper()} tool to install WWAN firmware.')

    print(f'Executing firmware command: {shlex.join(command)}')

    try:
        if firmware_type == 'uxfp':
            return_code, output = _run_uxfp_installer(command)
        else:
            return_code, output = rc_cmd(
                command,
                stdout=None,
            )
    except OSError as error:
        raise vyos.opmode.InternalError(
            f'Unable to run WWAN firmware installer: {error}'
        ) from error

    if return_code:
        raise vyos.opmode.InternalError(
            output
            or (f'WWAN firmware installation failed with exit code {return_code}')
        )

    print('WWAN firmware installation completed successfully.')

def execute_stop_manager():
    """Stop the WWAN manager service."""
    _stop_modem_manager_service(MANAGER_UNIT)

def _install_firmware_payload(
    firmware: dict[str, object],
    interface: str,
    no_prompt: bool = False,
) -> None:
    """Run the selected package's installer against the selected modem."""
    payload = firmware['payload']
    installer = str(firmware['installer'])
    manifest = firmware['manifest']
    modem = firmware['modem']

    _select_wwan_interface(interface)
    usb_device = _get_usb_device_path(interface)

    if shutil.which(installer) is None:
        raise vyos.opmode.DataUnavailable(
            f'{installer.upper()} firmware update tool is not installed'
        )

    manager_was_active = _stop_modem_manager_service()
    try:
        if installer == 'uxfp':
            diagnostic_port = _get_uxfp_diagnostic_port(
                payload,
                usb_device,
            )
        else:
            _ensure_qcserial_kernel_module_loaded()
            diagnostic_port = _get_tfl_diagnostic_port(
                payload,
                usb_device,
            )
        _display_firmware_details(
            interface,
            diagnostic_port,
            manifest,
            modem,
        )
    except KeyboardInterrupt:
        _restore_manager_service(manager_was_active)
        raise
    except Exception:
        _restore_manager_service(manager_was_active)
        raise

    if not no_prompt and not ask_yes_no(
        'Would you like to proceed with the firmware installation?',
        default=False,
    ):
        print('Firmware update cancelled.')
        _restore_manager_service(manager_was_active)
        return

    _run_firmware_installer(
        installer,
        payload,
        diagnostic_port,
    )

def show_firmware(
    interface: str,
    quiet: bool = False,
    downloaded_only: bool = False,
    raw: bool = False,
) -> dict[str, object] | None:
    """Show compatible, downloaded, and running firmware versions."""
    try:
        compatible = _get_compatible_firmware(interface)
    except (OSError, ValueError, vyos.opmode.Error):
        if quiet:
            return
        raise

    modem = compatible['modem']
    version_root = (
        FIRMWARE_ROOT
        / str(compatible['vendor'])
        / modem['model']
        / str(compatible['variant'])
        / modem['hardware_revision']
    )
    running_version = modem['firmware_version']

    firmware_versions = [
        {
            'version': version,
            'downloaded': (version_root / version).is_dir(),
            'running': version == running_version,
        }
        for version in compatible['versions']
    ]
    if downloaded_only:
        firmware_versions = [
            firmware for firmware in firmware_versions if firmware['downloaded']
        ]

    if quiet:
        print('\n'.join(firmware['version'] for firmware in firmware_versions))
        return

    if raw:
        return {
            'interface': interface,
            'vendor': compatible['vendor'],
            'model': modem['model'],
            'variant': compatible['variant'],
            'hardware_revision': modem['hardware_revision'],
            'firmware_revision': modem['firmware_revision'],
            'running_version': running_version,
            'versions': firmware_versions,
        }

    print(f'{"Compatible Version":<24}{"Downloaded":<14}Running')
    print(f'{"-" * 18:<24}{"-" * 10:<14}{"-" * 7}')
    for firmware in firmware_versions:
        downloaded = 'Yes' if firmware['downloaded'] else 'No'
        running = 'Yes' if firmware['running'] else 'No'
        print(f'{firmware["version"]:<24}{downloaded:<14}{running}')


def delete_firmware(
    interface: str,
    version: str,
    no_prompt: bool = False,
) -> None:
    """Remove an installed, available firmware version and its Debian package."""
    compatible = _get_compatible_firmware(interface)
    modem = compatible['modem']
    versions = compatible['versions']
    if version not in versions:
        raise vyos.opmode.IncorrectValue(
            f'Firmware {version} is not available for the modem on {interface}; '
            f'compatible versions: {", ".join(versions)}'
        )
    if version == modem['firmware_version']:
        raise vyos.opmode.IncorrectValue(
            f'Firmware {version} is currently running on {interface} and cannot '
            'be deleted'
        )

    version_dir = (
        FIRMWARE_ROOT
        / str(compatible['vendor'])
        / modem['model']
        / str(compatible['variant'])
        / modem['hardware_revision']
        / version
    )
    if not version_dir.is_dir():
        raise vyos.opmode.DataUnavailable(
            f'Firmware {version} is not downloaded for {interface}'
        )

    return_code, output = rc_cmd(
        ['dpkg-query', '--search', str(version_dir / 'manifest.json')]
    )
    if return_code or ':' not in output:
        raise vyos.opmode.DataUnavailable(
            f'Unable to identify the Debian package that owns firmware {version}'
        )
    package_name = output.partition(':')[0].strip()
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', package_name):
        raise vyos.opmode.DataUnavailable(
            f'Invalid firmware package name returned by dpkg-query: {package_name}'
        )

    if not no_prompt and not ask_yes_no(
        f'Do you really want to delete firmware {version} for {interface}?',
        default=False,
    ):
        return

    try:
        remove = subprocess.run(
            ['apt-get', 'remove', '--yes', package_name],
            check=False,
        )
    except OSError as error:
        raise vyos.opmode.DataUnavailable(
            f'Unable to run firmware package removal: {error}'
        ) from error
    if remove.returncode:
        raise vyos.opmode.InternalError(
            f'Unable to remove firmware package {package_name}'
        )
    if version_dir.exists():
        raise vyos.opmode.InternalError(
            f'Firmware package {package_name} was removed, but {version_dir} remains'
        )
    print(f'Firmware {version} for {interface} was successfully deleted')


def set_firmware(
    version: str,
    interface: str,
    no_prompt: bool = False,
) -> None:
    """Install an available modem firmware version from /usr/lib/modem."""
    firmware = _resolve_firmware_files(interface, version)
    _install_firmware_payload(
        firmware,
        interface=interface,
        no_prompt=no_prompt,
    )

if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except KeyboardInterrupt:
        print('\nFirmware update cancelled.')
        sys.exit(0)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
