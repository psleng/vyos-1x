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

import fcntl
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import vyos.opmode
from vyos.utils.io import ask_yes_no
from vyos.utils.process import rc_cmd
from vyos.utils.wwan.interfaces_wwan_firmware import (
    ArchiveCache,
    archive_cache_name,
    extract_archive,
    parse_catalog,
    prepare_release,
    prepare_standalone,
    revision_rank,
    version_key,
)
from vyos.version import get_version_data

MANAGER_UNIT = 'igos-wwan-manager.service'
MODEMMANAGER_UNIT = 'ModemManager.service'
FIRMWARE_ROOT = Path('/usr/lib/modem')
FIRMWARE_CATALOG_JSON_NAME = 'modem-firmware.json'
FIRMWARE_CATALOG_CACHE = Path('/var/cache/vyos') / FIRMWARE_CATALOG_JSON_NAME
FIRMWARE_ARCHIVE_CACHE = Path('/var/cache/vyos/wwan-firmware')
FIRMWARE_LOCK = Path('/run/lock/igos-wwan-firmware.lock')
FIRMWARE_MOTD = Path('/run/motd.d/11-vyos-cellular-firmware')
FIRMWARE_CONFIG = ['system', 'update-check', 'cellular-firmware']
SYSFS_NET = Path('/sys/class/net')
SYSFS_USB = Path('/sys/bus/usb/devices')
USB_WAIT_TIMEOUT = 30.0
USB_POLL_INTERVAL = 0.25
DEFAULT_CACHE_ARCHIVES = 2
DEFAULT_CACHE_SIZE_MIB = 512


def _get_archive_cache(retention_only=False):
    """Read committed cache policy independently of image-check enable/disable."""
    from vyos.configquery import ConfigTreeQuery

    config = ConfigTreeQuery()
    base = FIRMWARE_CONFIG + ['cache']
    if retention_only and not config.exists(base + ['enable']):
        return None
    count = int(config.value(base + ['max-archives']) or DEFAULT_CACHE_ARCHIVES)
    size = int(config.value(base + ['max-size']) or DEFAULT_CACHE_SIZE_MIB) * 1024**2
    return ArchiveCache(FIRMWARE_ARCHIVE_CACHE, count, size)


@contextmanager
def _firmware_update_lock():
    with FIRMWARE_LOCK.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise vyos.opmode.DataUnavailable('Another WWAN firmware operation is running') from error
        yield


def _stop_modem_manager_service(unit=MANAGER_UNIT) -> bool:
    """Stop the manager service and return whether it was active."""
    return_code, _ = rc_cmd(['systemctl', 'is-active', '--quiet', unit])
    if return_code:
        print(f'{unit} is not active; no stop is required.')
        return False

    print(f'Stopping {unit}...')
    try:
        return_code, output = rc_cmd(['systemctl', 'stop', unit])
        if return_code:
            error = output or f'Unable to stop {unit} (exit code {return_code})'
            raise vyos.opmode.InternalError(error)

        return_code, _ = rc_cmd(['systemctl', 'is-active', '--quiet', unit])
        if return_code != 3:
            raise vyos.opmode.InternalError(f'{unit} is not confirmed inactive; operation aborted')
    except (Exception, KeyboardInterrupt):
        # A failed/interrupted stop may already have disconnected bearers.
        _restore_manager_service(True, unit=unit)
        raise

    print(f'{unit} stopped.')
    return True


def _restore_manager_service(was_active: bool, unit=MANAGER_UNIT) -> None:
    """Restart the manager service if it was previously active."""
    if not was_active:
        return

    print(f'Restarting {unit}...')
    return_code, output = rc_cmd(['systemctl', 'start', unit])
    if return_code:
        raise vyos.opmode.InternalError(output or f'Unable to restart {unit} (exit code {return_code})')
    print(f'{unit} restarted.')


def _get_wwan_interfaces() -> list[str]:
    """Return WWAN network interfaces currently exposed by the kernel."""
    return sorted(path.name for path in SYSFS_NET.glob('wwan*') if path.is_dir())


def _select_wwan_interface(interface: str) -> str:
    """Validate and return an explicitly selected WWAN interface."""
    interfaces = _get_wwan_interfaces()
    if not interfaces:
        raise vyos.opmode.DataUnavailable('No WWAN interfaces were detected')

    if interface not in interfaces:
        raise vyos.opmode.IncorrectValue(
            f'WWAN interface {interface} does not exist; available interfaces: {", ".join(interfaces)}'
        )
    print(f'Selected WWAN interface: {interface}')
    return interface


def _get_usb_device_path(interface: str) -> Path:
    """Return the physical USB parent for a WWAN network interface."""
    device_link = SYSFS_NET / interface / 'device'
    try:
        interface_path = device_link.resolve(strict=True)
    except OSError as error:
        raise vyos.opmode.DataUnavailable(f'Unable to resolve the physical device for {interface}: {error}') from error

    usb_device = next(
        (parent for parent in (interface_path, *interface_path.parents) if (parent / 'idVendor').is_file()), None
    )
    if usb_device is None:
        raise vyos.opmode.DataUnavailable(f'Unable to find the physical USB device for {interface}')
    print(f'Physical USB device for {interface}: {usb_device}')
    return usb_device


def _capture_usb_identity(usb_device: Path) -> dict[str, str]:
    """Capture identity before teardown; USB serial survives tty renumbering."""
    serial_path = usb_device / 'serial'
    try:
        serial = serial_path.read_text().strip() if serial_path.exists() else ''
    except OSError as error:
        raise vyos.opmode.DataUnavailable(f'Unable to read USB serial for {usb_device}: {error}') from error
    if not serial:
        print('USB serial unavailable; tracking modem by physical USB path.')
    return {'path': str(usb_device.resolve()), 'serial': serial}


def _resolve_usb_device(identity: dict[str, str], timeout: float = USB_WAIT_TIMEOUT) -> Path:
    """Wait for the captured modem and its serial ports after re-enumeration.

    With a captured serial, never fall back to another device at the old path.
    Without one, only the original physical USB location is eligible.
    """
    deadline = time.monotonic() + timeout
    while True:
        matches = []
        if identity['serial']:
            for device in SYSFS_USB.iterdir():
                try:
                    if (device / 'idVendor').is_file() and (device / 'serial').read_text().strip() == identity[
                        'serial'
                    ]:
                        matches.append(device.resolve())
                except OSError:
                    # A device can disappear while sysfs is being read.
                    continue
            matches = sorted(set(matches))
        else:
            device = Path(identity['path'])
            if (device / 'idVendor').is_file():
                matches = [device]
        if len(matches) > 1:
            raise vyos.opmode.DataUnavailable(
                'Multiple USB devices match the captured modem serial; refusing to select a flashing target'
            )
        if matches and _get_modem_serial_ports(matches[0]):
            return matches[0]
        if time.monotonic() >= deadline:
            raise vyos.opmode.DataUnavailable(
                f'Timed out waiting for modem USB device {identity["path"]} and its serial ports after re-enumeration'
            )
        time.sleep(USB_POLL_INTERVAL)


def _get_modem_serial_ports(usb_device: Path) -> list[Path]:
    """Return ttyUSB device nodes belonging to one physical USB modem."""
    names = {path.name for path in usb_device.rglob('ttyUSB*') if path.name.startswith('ttyUSB')}
    ports = [Path('/dev') / name for name in sorted(names)]
    return [port for port in ports if port.exists()]


def _get_uxfp_diagnostic_port(firmware_path: Path, usb_device: Path) -> Path:
    """Return the UXFP reported diagnostic port for one physical modem."""
    modem_ports = set(_get_modem_serial_ports(usb_device))
    if not modem_ports:
        raise vyos.opmode.DataUnavailable(f'No serial ports were found below {usb_device}')

    return_code, output = rc_cmd(['uxfp', '--file', str(firmware_path), '--show-diag-ports'])
    if return_code:
        raise vyos.opmode.DataUnavailable(output or 'Unable to query UXFP diagnostic ports')

    reported_ports = {Path(port) for port in re.findall(r'diag port:\s*(/dev/ttyUSB\d+)', output, flags=re.IGNORECASE)}
    matching_ports = sorted(modem_ports & reported_ports)
    if not matching_ports:
        raise vyos.opmode.DataUnavailable(
            f'UXFP did not report a compatible diagnostic port for the selected modem at {usb_device}'
        )
    if len(matching_ports) > 1:
        raise vyos.opmode.DataUnavailable(
            'UXFP reported multiple diagnostic ports for the selected modem: '
            f'{", ".join(str(port) for port in matching_ports)}'
        )

    return matching_ports[0]


def _get_tfl_diagnostic_port(firmware_path: Path, usb_device: Path) -> Path:
    """Validate TFL compatibility and return the diagnostic port."""
    modem_ports = _get_modem_serial_ports(usb_device)
    if not modem_ports:
        raise vyos.opmode.DataUnavailable(f'No serial ports were found below {usb_device}')

    print(f'TFL modem serial-port group: {", ".join(str(port) for port in modem_ports)}')

    diagnostic_port = max(modem_ports, key=lambda port: int(re.search(r'\d+$', port.name).group()))

    print('Validating TFL firmware compatibility and modem ports...')
    return_code, output = rc_cmd(['tfl', str(firmware_path), '--port', str(diagnostic_port), '--dry-run', '--debug'])
    if return_code:
        raise vyos.opmode.IncorrectValue(output or 'TFL firmware compatibility validation failed')

    print('TFL firmware compatibility validation succeeded.')
    return diagnostic_port


def _display_firmware_details(
    interface: str, diagnostic_port: Path | None, firmware_manifest: dict[str, object], modem_identity: dict[str, str]
) -> None:
    """Display validated firmware metadata and the selected modem identity."""
    if diagnostic_port is not None:
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
    print(f'{"H/W revision":<24}: {modem_identity["hardware_revision"]}')
    print()


def _load_firmware_catalog(refresh: bool = True) -> list[dict[str, object]]:
    """Authenticate online or same-source cached catalogs before use."""
    from vyos.configquery import ConfigTreeQuery
    from vyos.utils.wwan.interfaces_wwan_firmware import load_catalog

    config = ConfigTreeQuery()
    url = config.value(FIRMWARE_CONFIG + ['url']) or get_version_data().get('wwan_firmware_map_url', '')
    try:
        return load_catalog(url, FIRMWARE_CATALOG_CACHE, parse_catalog, refresh=refresh)
    except (OSError, ValueError, KeyError, TypeError) as error:
        from urllib.error import HTTPError, URLError

        if isinstance(error, HTTPError):
            prefix = 'HTTP Error'
        elif isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError):
            prefix = 'Timeout error'
        elif isinstance(error, URLError):
            prefix = 'Connecting error'
        else:
            prefix = 'Unable to load firmware catalog'
        raise vyos.opmode.DataUnavailable(f'{prefix}: {error}') from error


def _get_vendor_name(value: str) -> str:
    """Return a package vendor slug for a manufacturer name."""
    normalized = ' '.join(value.split()).casefold()
    if normalized in ('telit', 'telit cinterion'):
        return 'telit'
    vendor = re.sub(r'[^a-z0-9]+', '-', normalized).strip('-')
    if not vendor:
        raise vyos.opmode.IncorrectValue(f'Invalid modem manufacturer: {value}')
    return vendor


def _get_modem_identity(interface: str) -> dict[str, str]:
    """Return the selected modem identity from the WWAN manager FSM."""
    if not re.fullmatch(r'wwan\d+', interface):
        raise vyos.opmode.IncorrectValue(f'Invalid WWAN interface: {interface}')

    try:
        from vyos.utils.wwan.wwan_client import WWANClientSync

        client = WWANClientSync()
        try:
            status = client.get_status(int(interface[4:]))
        finally:
            client.close()
    except Exception as error:
        raise vyos.opmode.DataUnavailable(
            f'Unable to read modem identity for {interface} from the WWAN FSM: {error}'
        ) from error

    fields = {
        'manufacturer': 'modem_manufacturer',
        'model': 'modem_model',
        'hardware_revision': 'modem_hardware_revision',
        'firmware_revision': 'modem_firmware',
    }
    if not isinstance(status, dict):
        raise vyos.opmode.DataUnavailable(f'Invalid WWAN FSM status for {interface}')
    identity = {}
    for field, status_field in fields.items():
        value = status.get(status_field)
        if not isinstance(value, str) or not value.strip():
            raise vyos.opmode.DataUnavailable(
                f'WWAN FSM identity for {interface} is incomplete: missing {status_field}'
            )
        identity[field] = value.strip()

    # ModemManager Revision is the native firmware identifier. The FSM does
    # not expose the AT#SWPKGV software-package version used by older code.
    identity['firmware_version'] = identity['firmware_revision']
    return identity


def _get_compatible_firmware(
    interface: str, show_progress: bool = False, refresh_map: bool = True, catalog_entries=None
) -> dict[str, object]:
    """Return compatible versions from the remote JSON firmware map."""
    if show_progress:
        print('Detecting modem identity...', end='', flush=True)
    modem_identity = _get_modem_identity(interface)
    if show_progress:
        print(' Done', flush=True)

    if show_progress:
        print('Checking firmware compatibility...', end='', flush=True)
    model = modem_identity['model']
    hardware_revision = modem_identity['hardware_revision']
    try:
        if catalog_entries is None:
            catalog_entries = _load_firmware_catalog(refresh=refresh_map)
    except vyos.opmode.Error:
        raise
    except (OSError, ValueError) as error:
        raise vyos.opmode.DataUnavailable('Unable to read the firmware catalog.') from error

    matched_entries = [
        rule
        for rule in catalog_entries
        if _get_vendor_name(str(rule['manufacturer'])) == _get_vendor_name(modem_identity['manufacturer'])
        and rule['model'] == model
        and revision_rank(rule['hardware_revision'], hardware_revision) >= 0
    ]
    if matched_entries:
        rank = max(revision_rank(rule['hardware_revision'], hardware_revision) for rule in matched_entries)
        winners = [
            rule for rule in matched_entries if revision_rank(rule['hardware_revision'], hardware_revision) == rank
        ]
        if len(winners) != 1:
            raise vyos.opmode.IncorrectValue('Ambiguous firmware revision mapping')
        selected = winners[0]
        if show_progress:
            print(' Done', flush=True)
        return {
            'vendor': _get_vendor_name(selected['manufacturer']),
            'versions': sorted(selected['releases'], key=version_key, reverse=True),
            'releases': selected['releases'],
            'installer': selected['installer'],
            'manufacturer': selected['manufacturer'],
            'hardware_revision': selected['hardware_revision'],
            'modem': modem_identity,
        }
    raise vyos.opmode.DataUnavailable(
        f'No available firmware JSON map matches '
        f'{modem_identity["manufacturer"]} {model} '
        f'hardware revision {hardware_revision}'
    )


def _ensure_qcserial_kernel_module_loaded() -> None:
    """Load qcserial when necessary and print its lsmod entry."""
    print('Checking for qcserial kernel module...')
    for attempt in range(2):
        return_code, output = rc_cmd(['lsmod'])
        if return_code:
            raise vyos.opmode.InternalError(output or 'Unable to query loaded kernel modules')

        qcserial_lines = [line for line in output.splitlines() if line.split() and line.split()[0] == 'qcserial']
        if qcserial_lines:
            print(f'qcserial: {qcserial_lines[0]}')
            return

        if attempt == 0:
            print('Loading qcserial kernel module.')
            return_code, output = rc_cmd(['modprobe', 'qcserial'])
            if return_code:
                raise vyos.opmode.DataUnavailable(output or 'Unable to load qcserial kernel module')

    raise vyos.opmode.DataUnavailable('qcserial kernel module did not load')


def _run_uxfp_installer(command: list[str], on_started=None) -> tuple[int, str]:
    """Run UXFP and display output beginning with the flashing workflow."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if on_started is not None:
        on_started()
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
    firmware_type: str, firmware_path: Path, diagnostic_port: Path | None, on_started=None
) -> None:
    """Run the selected firmware installer against the selected modem."""

    if diagnostic_port is None:
        raise vyos.opmode.DataUnavailable('No diagnostic port was found for the selected WWAN interface')

    if firmware_type == 'uxfp':
        command = ['uxfp', '--file', str(firmware_path)]
        command.extend(['--port', str(diagnostic_port)])
    elif firmware_type == 'tfl':
        command = ['tfl', str(firmware_path)]
        command.extend(['--port', str(diagnostic_port), '--force'])
    else:
        raise vyos.opmode.IncorrectValue(f'Unsupported WWAN firmware type: {firmware_type}')

    print(f'Using {firmware_type.upper()} tool to install WWAN firmware.')

    print(f'Executing firmware command: {shlex.join(command)}')

    try:
        if firmware_type == 'uxfp':
            return_code, output = _run_uxfp_installer(command, on_started=on_started)
        else:
            process = subprocess.Popen(command, stderr=subprocess.PIPE)
            if on_started is not None:
                on_started()
            _, error_output = process.communicate()
            return_code = process.returncode
            output = error_output.decode(errors='replace') if error_output else ''
    except OSError as error:
        raise vyos.opmode.InternalError(f'Unable to run WWAN firmware installer: {error}') from error

    if return_code:
        raise vyos.opmode.InternalError(output or (f'WWAN firmware installation failed with exit code {return_code}'))

    print('WWAN firmware installation completed successfully.')


@contextmanager
def _prepare_firmware_payload(payload: Path, installer: str):
    """Extract a nested vendor TFL ZIP on the same checked work filesystem."""
    if installer != 'tfl' or payload.is_dir():
        yield payload
        return
    with tempfile.TemporaryDirectory(prefix='tfl-', dir=payload.parent) as temporary:
        extracted = Path(temporary) / 'firmware'
        extract_archive(payload, extracted)
        contents = list(extracted.rglob('contents.xml'))
        if len(contents) != 1:
            raise vyos.opmode.IncorrectValue('Expected exactly one TFL contents.xml')
        yield contents[0].parent


def _select_diagnostic_port(installer, payload, identity):
    """Resolve the current ports of the captured physical modem."""
    device = _resolve_usb_device(identity)
    if installer == 'uxfp':
        return _get_uxfp_diagnostic_port(payload, device)
    _ensure_qcserial_kernel_module_loaded()
    return _get_tfl_diagnostic_port(payload, device)


def _install_firmware_payload(firmware: dict[str, object], interface: str, no_prompt: bool = False) -> None:
    """Run the selected package's installer against the selected modem."""
    payload = firmware['payload']
    installer = str(firmware['installer'])
    manifest = firmware['manifest']
    modem = firmware['modem']

    _select_wwan_interface(interface)
    usb_device = _get_usb_device_path(interface)
    usb_identity = _capture_usb_identity(usb_device)

    if shutil.which(installer) is None:
        raise vyos.opmode.DataUnavailable(f'{installer.upper()} firmware update tool is not installed')

    with _prepare_firmware_payload(payload, installer) as prepared_payload:
        # Review metadata without touching diagnostic ports or stopping services.
        _display_firmware_details(interface, None, manifest, modem)
        if firmware.get('unsigned'):
            print('WARNING: unsigned firmware; publisher authenticity is not verified.')
        print('Updating firmware disconnects ALL managed cellular interfaces.')
        if not no_prompt and not ask_yes_no('Proceed with firmware installation?', default=False):
            print('Firmware update cancelled.')
            return
        manager_was_active = _stop_modem_manager_service()
        installer_started = False

        def mark_installer_started():
            nonlocal installer_started
            installer_started = True

        try:
            mm_status, _ = rc_cmd(['systemctl', 'is-active', '--quiet', MODEMMANAGER_UNIT])
            if mm_status != 3:
                raise vyos.opmode.DataUnavailable('ModemManager is not confirmed inactive')
            diagnostic_port = _select_diagnostic_port(installer, prepared_payload, usb_identity)
            # Validation may have changed USB composition. Resolve the captured
            # modem again immediately before launching the flashing process.
            diagnostic_port = _select_diagnostic_port(installer, prepared_payload, usb_identity)
            print(f'Diagnostic port selected: {diagnostic_port}')
            _run_firmware_installer(
                installer, prepared_payload, diagnostic_port, on_started=mark_installer_started
            )
        except (Exception, KeyboardInterrupt):
            # Once a tool has started, it may have changed the modem even if it
            # fails. Only restore automatically when no flasher was launched.
            if not installer_started:
                _restore_manager_service(manager_was_active)
            raise
        print('Firmware flashed. WWAN manager remains stopped; reboot the router.')


def _running_version(compatible):
    """Map the modem's native firmware revision to its catalog label."""
    revision = compatible['modem']['firmware_revision']
    exact = next(
        (label for label, release in compatible['releases'].items() if release['telit_version'] == revision), None
    )
    if exact is not None:
        return exact
    # ModemManager may append a build number/date to the catalog's native
    # version, e.g. "25.21.564  1  [Dec 27 2023 05:00:00]".
    parts = revision.split()
    if not parts or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', parts[0]):
        return None
    matches = [
        label for label, release in compatible['releases'].items()
        if release['telit_version'] == parts[0]
    ]
    return matches[0] if len(matches) == 1 else None


def show_firmware(
    interface: str, quiet: bool = False, downloaded_only: bool = False, raw: bool = False
) -> dict[str, object] | None:
    """Show available releases, native firmware, dates, cache and running status."""
    try:
        compatible = _get_compatible_firmware(interface, refresh_map=not quiet)
    except (OSError, ValueError, vyos.opmode.Error):
        if quiet:
            return
        raise

    modem = compatible['modem']
    running_version = _running_version(compatible)

    cache = _get_archive_cache()
    firmware_versions = [
        {
            'version': version,
            'telit_version': compatible['releases'][version]['telit_version'],
            'release_date': compatible['releases'][version].get('release_date') or '',
            'downloaded': cache.contains(compatible['releases'][version]['archive_sha256'], archive_cache_name(compatible['releases'][version])),
            'running': version == running_version,
        }
        for version in compatible['versions']
    ]
    if downloaded_only:
        firmware_versions = [firmware for firmware in firmware_versions if firmware['downloaded']]

    if quiet:
        print('\n'.join(firmware['version'] for firmware in firmware_versions))
        return

    if raw:
        return {
            'interface': interface,
            'vendor': compatible['vendor'],
            'model': modem['model'],
            'hardware_revision': modem['hardware_revision'],
            'firmware_revision': modem['firmware_revision'],
            'running_version': running_version,
            'versions': firmware_versions,
        }

    if not compatible['versions']:
        print('No compatible firmware versions are listed for this modem.')
        return

    print(f'{"Version":<10}{"Telit Firmware":<20}{"Released":<14}{"Cached":<10}Running')
    print(f'{"-" * 7:<10}{"-" * 14:<20}{"-" * 10:<14}{"-" * 6:<10}{"-" * 7}')
    for firmware in firmware_versions:
        cached = 'Yes' if firmware['downloaded'] else 'No'
        running = 'Yes' if firmware['running'] else 'No'
        released = firmware['release_date'] or '-'
        print(f'{firmware["version"]:<10}{firmware["telit_version"]:<20}{released:<14}{cached:<10}{running}')



def delete_firmware(interface: str, version: str, no_prompt: bool = False) -> None:
    """Delete a retained archive without changing the modem or installed firmware."""
    try:
        with _firmware_update_lock():
            compatible = _get_compatible_firmware(interface, refresh_map=False)
            if version not in compatible['releases']:
                raise vyos.opmode.IncorrectValue(f'Unknown firmware version {version}')
            cache = _get_archive_cache()
            release = compatible['releases'][version]
            digest = release['archive_sha256']
            filename = archive_cache_name(release)
            if not cache.contains(digest, filename):
                raise vyos.opmode.DataUnavailable(f'Firmware {version} is not cached')
            if not no_prompt and not ask_yes_no(f'Delete cached firmware {version} for {interface}?', default=False):
                return
            cache.delete(digest, filename)
            print(f'Deleted cached firmware {version}.')
    except (OSError, ValueError) as error:
        raise vyos.opmode.DataUnavailable(f'Unable to delete firmware cache: {error}') from error


def set_firmware(version: str, interface: str, no_prompt: bool = False) -> None:
    """Download one cataloged archive, validate, flash, and remove transient files."""
    try:
        with _firmware_update_lock():
            compatible = _get_compatible_firmware(interface, show_progress=True)
            if version == 'latest':
                version = compatible['versions'][0]
            if version not in compatible['releases']:
                raise vyos.opmode.IncorrectValue(f'Firmware {version} is not available for {interface}')
            release = {**compatible['releases'][version], 'manufacturer': compatible['manufacturer']}
            running = _running_version(compatible)
            if running == version:
                print(f'Firmware {version} is already installed.')
                return
            if running is None:
                print('Running firmware is not in the catalog; version ordering is unknown.')
            elif version_key(version) < version_key(running):
                print(f'WARNING: downgrading firmware from {running} to {version}.')
            print('Preparing authenticated firmware archive...')
            cache = _get_archive_cache(retention_only=True)
            with prepare_release(
                FIRMWARE_ROOT,
                compatible['vendor'],
                compatible['modem'],
                release,
                compatible['installer'],
                compatible['hardware_revision'],
                cache=cache,
            ) as firmware:
                _install_firmware_payload(firmware, interface=interface, no_prompt=no_prompt)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise vyos.opmode.DataUnavailable(f'Unable to update WWAN firmware: {error}') from error


def update_archive(interface: str, archive: str, no_prompt: bool = False, force_unsigned: bool = False) -> None:
    """Authenticate a standalone archive before extraction and compatibility checks."""
    version = get_version_data()
    if force_unsigned and not (
        version.get('build_type') == 'development' and version.get('wwan_firmware_allow_unsigned') is True
    ):
        raise vyos.opmode.IncorrectValue('force-unsigned is permitted only on explicitly enabled development images')
    if force_unsigned and (urlsplit(archive).scheme or not Path(archive).is_file()):
        raise vyos.opmode.IncorrectValue('force-unsigned requires a local archive file')
    try:
        with _firmware_update_lock():
            modem = _get_modem_identity(interface)
            with prepare_standalone(FIRMWARE_ROOT, modem, archive, force_unsigned=force_unsigned) as firmware:
                manifest = firmware['manifest']
                if _running_version({'modem': modem, 'releases': {'archive': manifest}}) is not None:
                    print(f"Firmware {manifest['telit_version']} is already installed.")
                    return
                print('Standalone archive: release ordering is unknown without a catalog.')
                _install_firmware_payload(firmware, interface, no_prompt=no_prompt)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise vyos.opmode.DataUnavailable(f'Unable to install firmware archive: {error}') from error


def execute_check_firmware(notify: bool = False):
    """Check present configured modems; notify only, never flash or stop services."""
    from vyos.configquery import ConfigTreeQuery

    config = ConfigTreeQuery()
    base = FIRMWARE_CONFIG
    motd = FIRMWARE_MOTD
    if notify and (not config.exists(base + ['auto-check']) or config.exists(base + ['disable'])):
        motd.unlink(missing_ok=True)
        return
    interfaces = [name for name in _get_wwan_interfaces() if config.exists(['interfaces', 'wwan', name])]
    notices = []
    if interfaces:
        try:
            entries = _load_firmware_catalog()
        except vyos.opmode.Error as error:
            print(f'Cellular firmware check unavailable: {error}')
            if notify:
                motd.unlink(missing_ok=True)
            return
        for interface in interfaces:
            try:
                compatible = _get_compatible_firmware(interface, catalog_entries=entries)
                latest = compatible['versions'][0]
                current = _running_version(compatible)
                # Unknown native firmware cannot be ordered safely.
                if current is not None and version_key(latest) > version_key(current):
                    notices.append(
                        f'{interface}: firmware {latest} available (running {current}). '
                        f'Run: update firmware wwan {interface} {latest}'
                    )
            except (OSError, ValueError, vyos.opmode.Error) as error:
                print(f'Skipping {interface}: {error}')
    message = '\n'.join(notices)
    if notify:
        previous = motd.read_text() if motd.exists() else ''
        if message:
            content = 'Cellular firmware updates:\n' + message + '\n'
            motd.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=motd.parent, mode='w', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(content)
                    stream.flush()
                    temporary.chmod(0o644)
                    temporary.replace(motd)
                finally:
                    temporary.unlink(missing_ok=True)
            if content != previous and config.exists(base + ['wall']):
                subprocess.run(['wall', '-n'], input=content, text=True, check=False)
        else:
            motd.unlink(missing_ok=True)
    elif message:
        print(message)
    else:
        print('No newer mapped cellular firmware found.')


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
