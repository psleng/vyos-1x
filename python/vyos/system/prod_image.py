#!/usr/bin/env python3
#
# Copyright 2026 Perle Systems Limited

import os
import argparse
from pathlib import Path
from shutil import copy, copytree, move, rmtree

from vyos.system import grub
from vyos.system import image
from vyos.template import render
from vyos.flavor import get_image_secure_grub


# -------------------------------
# Constants
# -------------------------------
DEFAULT_BOOT_VARS: dict[str, str] = {
    # TEMP (dm-verity bring-up): show the GRUB menu for 10s so an older image can
    # be selected if a verity image fails to boot. Restore to '0' (and drop
    # timeout_style) to hide/suppress the menu again after testing.
    'timeout': '10',
    'timeout_style': 'menu',
    'console_type': 'tty',
    'console_num': '0',
    'console_speed': '115200',
    'bootmode': 'normal'
}

TARGET_P2 = "/mnt/p3"
ROOTFS = "/"
ISO = "/mnt/iso"

SRC_DTB = f"{ISO}/boot/dtb"
LIVE = f"{ISO}/live"
BOOT = f"{TARGET_P2}/boot"


# -------------------------------
# Utility Functions
# -------------------------------
def log(msg: str):
    print(f"[INFO] {msg}")


def safe_rmtree(path: Path):
    if path.exists():
        log(f"Removing {path}")
        rmtree(path, ignore_errors=True)


# -------------------------------
# GRUB Setup
# -------------------------------
def setup_grub(root_dir: str) -> None:
    log('Installing GRUB configuration files')

    grub_cfg_main = f'{root_dir}/{grub.GRUB_DIR_MAIN}/grub.cfg'
    grub_cfg_vars = f'{root_dir}/{grub.CFG_VYOS_VARS}'
    grub_cfg_modules = f'{root_dir}/{grub.CFG_VYOS_MODULES}'
    grub_cfg_menu = f'{root_dir}/{grub.CFG_VYOS_MENU}'

    render(grub_cfg_main, grub.TMPL_GRUB_MAIN, {})
    grub.common_write(root_dir)
    grub.vars_write(grub_cfg_vars, DEFAULT_BOOT_VARS)
    grub.modules_write(grub_cfg_modules, [])
    grub.write_cfg_ver(1, root_dir)
    render(grub_cfg_menu, grub.TMPL_GRUB_MENU, {})


# -------------------------------
# Image Copy Logic
# -------------------------------
def copy_image(version: str, dest: str):
    version_dir = Path(f"{BOOT}/{version}")
    os.makedirs(version_dir, exist_ok=True)

    log(f"Copying image for version: {version}")

    # Everything the per-version boot needs is already staged (and pruned) on the
    # ISO by vyos-build -- kernel + baked initrd (+ their detached .sig) in /live,
    # and the flat per-model DTBs in /boot/dtb. Pull straight from the ISO, no
    # pruning; this mirrors `add system image <iso>` exactly.
    copytree(f"{LIVE}/",
             f"{BOOT}/{version}/",
             dirs_exist_ok=True,
             symlinks=True)
    move(f'{BOOT}/{version}/filesystem.squashfs',
         f'{BOOT}/{version}/{version}.squashfs')

    if Path(SRC_DTB).exists():
        log("Copying per-image DTB files")
        copytree(SRC_DTB,
                 f'{BOOT}/{version}/dtb',
                 dirs_exist_ok=True,
                 symlinks=True)


def setup_default_firmware():
    default_name = "default-firmware"
    dest = Path(f"{BOOT}/{default_name}")

    safe_rmtree(dest)

    log("Creating default firmware image")

    # Same as copy_image: pull the factory default-firmware straight from the
    # ISO /live (+ /boot/dtb), already pruned by vyos-build.
    copytree(f"{LIVE}/",
             f"{BOOT}/{default_name}/",
             dirs_exist_ok=True,
             symlinks=True)
    move(f'{BOOT}/{default_name}/filesystem.squashfs',
         f'{BOOT}/{default_name}/{default_name}.squashfs')

    if Path(SRC_DTB).exists():
        log("Copying per-image DTB files for default-firmware")
        copytree(SRC_DTB,
                 f'{BOOT}/{default_name}/dtb',
                 dirs_exist_ok=True,
                 symlinks=True)

    return default_name


# -------------------------------
# Main Execution
# -------------------------------
def main():
    parser = argparse.ArgumentParser(description="VyOS installer runner")
    parser.add_argument(
        "--grub-target",
        required=True,
        help="Block device to install GRUB onto (e.g. /dev/loop0)"
    )
    args = parser.parse_args()

    grub_target = args.grub_target
    log(f"Using GRUB target device: {grub_target}")

    version = image.get_image_version(ROOTFS)
    log(f"Detected running version: {version}")

    # persistence config
    Path(f'{TARGET_P2}/persistence.conf').write_text('/ union\n')

    # copy main image (per-image DTBs are pulled from the ISO inside copy_image /
    # setup_default_firmware; no global /boot/dtb any more).
    copy_image(version, BOOT)

    # GRUB setup
    setup_grub(TARGET_P2)
    grub.create_structure()
    grub.version_add(version, TARGET_P2)
    grub.set_current_default(version, TARGET_P2)
    grub.set_console_type('ttyS', TARGET_P2)

    # default firmware
    default_name = setup_default_firmware()
    grub.version_add(default_name, TARGET_P2)
    grub.set_factory_default(default_name, TARGET_P2)

    # install GRUB
    log("Installing GRUB to disk")
    grub.install(grub_target, f'{BOOT}/', f'{BOOT}/efi')

    # secure_grub: grub-install just wrote the STOCK, non-enforcing core to the
    # ESP. Replace it with the signature-ENFORCING monolithic core carried in the
    # image (built by 25-igos-grub-core.chroot). Nothing is built here -- the
    # signed core rides in the squashfs; we only copy it into place (the same file
    # `update firmware --component grub` installs on a running unit).
    if get_image_secure_grub():
        core_src = Path(f'{ROOTFS}usr/lib/grub/arm64-efi/monolithic/grubaa64.efi')
        core_dst = Path(f'{BOOT}/efi/EFI/VyOS/grubaa64.efi')
        if not core_src.is_file():
            raise RuntimeError(
                'secure_grub image is missing the enforcing GRUB core at '
                f'{core_src} (25-igos-grub-core.chroot did not run) -- refusing '
                'to ship a non-enforcing bootloader')
        log('secure_grub: installing enforcing GRUB core to ESP EFI/VyOS/grubaa64.efi')
        core_dst.parent.mkdir(parents=True, exist_ok=True)
        copy(core_src, core_dst)

    # sort inodes
    grub.sort_inodes(f'{TARGET_P2}/{grub.GRUB_DIR_VYOS}')
    grub.sort_inodes(f'{TARGET_P2}/{grub.GRUB_DIR_VYOS_VERS}')

    log("IGOS production image completed successfully.")


# -------------------------------
# Entry Point
# -------------------------------
if __name__ == "__main__":
    main()
