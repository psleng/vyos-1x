#!/usr/bin/env python3
#
# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This file is part of VyOS.
#
# VyOS is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.

from argparse import ArgumentParser, Namespace
from contextlib import contextmanager
from hashlib import sha256
from os import getuid, sync
from pathlib import Path
from re import match
from shlex import quote
from shutil import copy2
from stat import S_ISBLK
from sys import exit
from time import monotonic

from vyos.utils.io import ask_yes_no
from vyos.utils.process import rc_cmd

DEFAULT_IMAGE = '/usr/lib/u-boot/platform/u-boot-raw-boot.img'
DEFAULT_DEVICE = '/dev/mmcblk0'
DEFAULT_GRUB_SOURCE = '/usr/lib/grub/arm64-efi/monolithic/grubaa64.efi'
DEFAULT_EFI_MOUNT = '/mnt/efi'
DEFAULT_GRUB_TARGET_REL = 'EFI/VyOS/grubaa64.efi'
CHUNK_SIZE = 4 * 1024 * 1024


def parse_arguments() -> Namespace:
    parser = ArgumentParser(description='Update boot firmware from packaged image')
    parser.add_argument('--component', choices=['uboot', 'grub'], default='uboot',
                        help='Component to update')
    parser.add_argument('--image', default=DEFAULT_IMAGE,
                        help='Path to source firmware image')
    parser.add_argument('--device', default=None,
                        help='Target block device (for example /dev/mmcblk0)')
    parser.add_argument('--grub-source', default=DEFAULT_GRUB_SOURCE,
                        help='Path to source GRUB EFI binary')
    parser.add_argument('--grub-target-relpath', default=DEFAULT_GRUB_TARGET_REL,
                        help='Target path relative to EFI mount point')
    parser.add_argument('--efi-mount-point', default=DEFAULT_EFI_MOUNT,
                        help='EFI mount point to use for GRUB update')
    parser.add_argument('--efi-device', default=None,
                        help='EFI partition block device (for example /dev/mmcblk0p2)')
    parser.add_argument('--yes', action='store_true',
                        help='Do not prompt before writing')
    return parser.parse_args()


def detect_target_device() -> str:
    code, source = rc_cmd('findmnt -n -o SOURCE /')
    if code == 0 and source:
        source = source.strip()
        code, parent = rc_cmd(f'lsblk -ndo PKNAME {quote(source)}')
        if code == 0 and parent.strip():
            return f'/dev/{parent.strip().splitlines()[0]}'

    if Path(DEFAULT_DEVICE).exists():
        return DEFAULT_DEVICE

    code, out = rc_cmd('lsblk -ndo NAME,TYPE')
    if code == 0:
        disks = []
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1] == 'disk':
                disks.append(parts[0])
        for name in disks:
            if name.startswith('mmcblk'):
                return f'/dev/{name}'
        if disks:
            return f'/dev/{disks[0]}'

    raise RuntimeError('Unable to detect target block device automatically. Use "update firmware device <path>".')


def sha256_file(path: Path) -> str:
    hasher = sha256()
    with path.open('rb') as f:
        while True:
            data = f.read(CHUNK_SIZE)
            if not data:
                break
            hasher.update(data)
    return hasher.hexdigest()


def sha256_device_prefix(path: Path, size: int) -> str:
    hasher = sha256()
    remaining = size
    with path.open('rb', buffering=0) as f:
        while remaining > 0:
            data = f.read(min(CHUNK_SIZE, remaining))
            if not data:
                raise RuntimeError('Unable to read enough bytes from target device for verification')
            hasher.update(data)
            remaining -= len(data)
    return hasher.hexdigest()


def write_with_progress(source: Path, target: Path, size: int) -> None:
    written = 0
    last_print = 0.0

    with source.open('rb', buffering=0) as src, target.open('r+b', buffering=0) as dst:
        dst.seek(0)

        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
            written += len(chunk)

            now = monotonic()
            if (now - last_print) >= 0.2 or written == size:
                percent = (written / size) * 100
                print(
                    f'\rWriting firmware: {percent:6.2f}% '
                    f'({written / 1024**2:.1f}/{size / 1024**2:.1f} MiB)',
                    end='',
                    flush=True,
                )
                last_print = now

        dst.flush()

    print()
    sync()


def is_mmc_boot_partition(device_path: Path) -> bool:
    return bool(match(r'^mmcblk\d+boot[01]$', device_path.name))


@contextmanager
def mmc_boot_partition_rw(device_path: Path):
    """Temporarily disable kernel read-only gate for eMMC boot partitions.

    Linux exposes /sys/block/<bootdev>/force_ro for mmcblkXboot0/1.
    This does not override hardware write protection configured in EXT_CSD.
    """
    if not is_mmc_boot_partition(device_path):
        yield
        return

    force_ro = Path(f'/sys/block/{device_path.name}/force_ro')
    if not force_ro.exists():
        yield
        return

    original = force_ro.read_text(encoding='utf-8').strip()
    changed = False
    try:
        if original == '1':
            print(f'Unlocking {device_path.name} for write (force_ro=0) ...')
            force_ro.write_text('0', encoding='utf-8')
            changed = True
        yield
    finally:
        if changed:
            force_ro.write_text('1', encoding='utf-8')
            print(f'Relocked {device_path.name} (force_ro=1).')


def resolve_uboot_boot_partition(device_path: Path) -> Path:
    """Redirect a whole-disk eMMC target to its hardware boot partition.

    The packaged U-Boot raw image (tiboot3 + tispl + u-boot) must be written to
    the eMMC *boot* hardware partition (mmcblkXboot0) that the SoC ROM reads --
    NOT to the user data area (mmcblkX), whose offset 0 holds the GPT. Writing
    the raw image to the whole disk would clobber the partition table and would
    never be seen by the ROM.

    When the resolved target is a whole-disk eMMC node (/dev/mmcblkN) that has a
    /dev/mmcblkNboot0 sibling, return that boot partition. An explicit boot
    partition (mmcblkNboot0/1) is respected as-is, and targets with no boot0
    sibling (SD cards, /dev/sdX) are returned unchanged.
    """
    if is_mmc_boot_partition(device_path):
        return device_path

    if match(r'^mmcblk\d+$', device_path.name):
        boot_partition = Path(f'/dev/{device_path.name}boot0')
        if boot_partition.exists() and S_ISBLK(boot_partition.stat().st_mode):
            print(f'Redirecting U-Boot write to eMMC boot partition: {boot_partition}')
            return boot_partition

    return device_path


def validate_inputs(image_path: Path, device_path: Path) -> None:
    if getuid() != 0:
        exit('This command must be run as root')

    if not image_path.is_file():
        exit(f'Firmware image does not exist: {image_path}')

    st = device_path.stat()
    if not S_ISBLK(st.st_mode):
        exit(f'Target is not a block device: {device_path}')

    if image_path.stat().st_size <= 0:
        exit(f'Firmware image is empty: {image_path}')


def validate_block_device(device_path: Path, kind: str = 'Target') -> None:
    if not device_path.exists():
        raise RuntimeError(f'{kind} block device does not exist: {device_path}')
    st = device_path.stat()
    if not S_ISBLK(st.st_mode):
        raise RuntimeError(f'{kind} is not a block device: {device_path}')


def findmnt_source(mount_point: Path) -> str | None:
    code, out = rc_cmd(f'findmnt -n -o SOURCE {quote(str(mount_point))}')
    if code != 0:
        return None
    source = out.strip().splitlines()
    if not source:
        return None
    return source[0]


def detect_efi_device() -> str:
    for mount_point in ('/mnt/efi', '/boot/efi'):
        source = findmnt_source(Path(mount_point))
        if source:
            return source

    code, out = rc_cmd('lsblk -rno PATH,FSTYPE,PARTTYPE,MOUNTPOINT')
    if code == 0:
        candidates: list[tuple[str, bool]] = []
        for line in out.splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) < 3:
                continue
            path = parts[0]
            fs_type = parts[1].lower()
            part_type = parts[2].lower()
            if fs_type != 'vfat':
                continue
            is_esp = part_type.startswith('c12a7328')
            candidates.append((path, is_esp))

        esp = [p for p, is_esp in candidates if is_esp]
        if esp:
            for path in esp:
                if '/mmcblk' in path:
                    return path
            return esp[0]
        if candidates:
            return candidates[0][0]

    root_disk = detect_target_device()
    if match(r'^/dev/mmcblk\d+$', root_disk):
        return f'{root_disk}p2'
    if match(r'^/dev/[a-z]+$', root_disk):
        return f'{root_disk}2'

    raise RuntimeError('Unable to detect EFI partition. Use --efi-device <path>.')


@contextmanager
def ensure_efi_mounted(mount_point: Path, requested_device: str | None):
    existing_source = findmnt_source(mount_point)
    if existing_source:
        yield existing_source
        return

    efi_device = requested_device or detect_efi_device()
    validate_block_device(Path(efi_device), 'EFI')

    mount_point.mkdir(parents=True, exist_ok=True)
    code, out = rc_cmd(f'mount -t vfat {quote(efi_device)} {quote(str(mount_point))}')
    if code != 0:
        raise RuntimeError(f'Failed to mount EFI partition {efi_device} at {mount_point}: {out.strip()}')

    try:
        source_after = findmnt_source(mount_point)
        if not source_after:
            raise RuntimeError(f'EFI mount verification failed for {mount_point}')
        yield source_after
    finally:
        rc_cmd(f'umount {quote(str(mount_point))}')


def update_grub(grub_source: Path, target_relpath: str, mount_point: Path,
                efi_device: str | None, force: bool = False) -> None:
    if not grub_source.is_file():
        exit(f'GRUB source binary does not exist: {grub_source}')
    if grub_source.stat().st_size <= 0:
        exit(f'GRUB source binary is empty: {grub_source}')

    print(f'Source GRUB binary: {grub_source}')
    print(f'EFI mount point  : {mount_point}')

    with ensure_efi_mounted(mount_point, efi_device) as mounted_source:
        print(f'EFI source device: {mounted_source}')
        target_rel = target_relpath.lstrip('/')
        target_path = mount_point.joinpath(target_rel)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Pre-write "already up-to-date" checksum comparison intentionally removed
        # (consistent with the uboot path): manual operation, gated by the prompt.

        if not force:
            if not ask_yes_no('Proceed with GRUB EFI update?', default=False):
                print('GRUB EFI update cancelled.')
                return

        print('Copying GRUB EFI binary. Do not power off the system ...')
        copy2(grub_source, target_path)
        sync()

        print('Verifying written data ...')
        target_hash_after = sha256_file(target_path)

    source_hash = sha256_file(grub_source)
    if source_hash != target_hash_after:
        exit('Verification failed: written GRUB EFI binary does not match source')

    print('GRUB EFI update completed successfully.')


def update_firmware(image_path: Path, device_path: Path, force: bool = False) -> None:
    image_size = image_path.stat().st_size

    print(f'Source image : {image_path}')
    print(f'Target device: {device_path}')
    print(f'Image size   : {image_size} bytes ({image_size / 1024**2:.1f} MiB)')

    # Pre-write "already up-to-date" checksum comparison intentionally removed:
    # U-Boot builds are not byte-reproducible (embedded build timestamp/version),
    # so it almost never matched and was not a meaningful version gate. This is a
    # manual operation, gated by the confirmation prompt below.

    if not force:
        if not ask_yes_no('Proceed with firmware update?', default=False):
            print('Firmware update cancelled.')
            return

    print('Starting firmware write. Do not power off the system ...')
    with mmc_boot_partition_rw(device_path):
        write_with_progress(image_path, device_path, image_size)

        print('Verifying written data ...')
        target_hash_after = sha256_device_prefix(device_path, image_size)

    source_hash = sha256_file(image_path)
    if source_hash != target_hash_after:
        exit('Verification failed: written firmware does not match source image')

    print('Firmware update completed successfully.')


if __name__ == '__main__':
    try:
        args = parse_arguments()
        if getuid() != 0:
            exit('This command must be run as root')

        if args.component == 'uboot':
            image_path = Path(args.image)
            device_path = Path(args.device) if args.device else Path(detect_target_device())
            device_path = resolve_uboot_boot_partition(device_path)
            validate_inputs(image_path, device_path)
            update_firmware(image_path, device_path, args.yes)
        else:
            update_grub(
                grub_source=Path(args.grub_source),
                target_relpath=args.grub_target_relpath,
                mount_point=Path(args.efi_mount_point),
                efi_device=args.efi_device,
                force=args.yes,
            )

        exit(0)
    except KeyboardInterrupt:
        print('\nStopped by Ctrl+C')
        exit(1)
    except Exception as err:
        exit(f'{err}')
