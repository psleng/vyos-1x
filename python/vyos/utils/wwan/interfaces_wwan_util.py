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

"""
WWAN Interface Utilities

This module provides utility functions for WWAN interface management,
including hardware reset capabilities and other common operations.
"""

import asyncio
import logging
import subprocess
import time
from pathlib import Path

from vyos.hardware import api as hw_api
from vyos.utils.wwan import interfaces_wwan_diag as wwan_diag

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ModemManager managed-downtime guard
# ---------------------------------------------------------------------------
# A deliberate ``systemctl stop ModemManager`` -- done around the quiesced
# hardware reset below -- is indistinguishable from an MM crash to the watchdog
# in interfaces_wwan_main.ModemManagerMonitor, which would "recover" it by
# restarting MM mid-reset and defeat the whole point (MM would immediately
# re-probe the not-yet-ready modem).  The reset path publishes an EXPIRING
# deadline here; the watchdog consults is_managed_mm_downtime() and leaves MM
# alone until it passes.  The deadline auto-expires so a failure mid-reset can
# never leave MM permanently unmonitored.
_mm_managed_downtime_until = 0.0


def begin_managed_mm_downtime(seconds: float) -> None:
    """Declare ModemManager intentionally stopped for up to ``seconds``."""
    global _mm_managed_downtime_until
    _mm_managed_downtime_until = time.monotonic() + max(0.0, seconds)


def end_managed_mm_downtime() -> None:
    """Clear the managed-downtime window (reset finished / MM back)."""
    global _mm_managed_downtime_until
    _mm_managed_downtime_until = 0.0


def is_managed_mm_downtime() -> bool:
    """True while a deliberate ModemManager stop is in effect.

    The MM watchdog checks this and stands down (does not restart MM) so a
    quiesced reset can hold MM down through the modem's unprobed settle window.
    """
    return time.monotonic() < _mm_managed_downtime_until


def system_is_stopping() -> bool:
    """True when systemd is taking the whole system down (reboot/poweroff/halt).

    During a system shutdown systemd transitions its manager state to
    ``stopping`` *before* it sends SIGTERM to individual units, so by the
    time the WWAN manager is asked to stop this already reads ``stopping``.

    This is what lets the shutdown path distinguish a real reboot — where
    the board keeps the modem powered across the soft reboot, so a GPIO
    reset is wanted — from a plain ``systemctl restart igos-wwan-manager``,
    where power-cycling the modem out from under a service that is about to
    come right back would be harmful. Best-effort: any error reads as "not
    stopping" so we never reset a modem on uncertainty.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:  # noqa: BLE001 -- best-effort probe
        logger.debug(f"Could not determine system running state: {e}")
        return False
    return result.stdout.strip() == "stopping"


async def hardware_reset_all_modems() -> None:
    """Pulse the board GPIO reset line on every pinmap-declared modem.

    Intended for the system reboot/shutdown path. On a soft reboot the
    board keeps each modem powered, so the modem retains its internal state
    (e.g. a ``failed``/``sim-missing`` latch). A GPIO reset on the way down
    guarantees every modem re-enumerates clean at the next boot.

    Best-effort and self-contained: a board with no pinmap overlay, or a
    modem with no declared reset pin, is simply skipped. Each reset is a
    short (~200 ms) local GPIO pulse — no D-Bus, no ModemManager — so this
    cannot stall on a wedged MM.
    """
    try:
        modems = await asyncio.to_thread(hw_api.list_modems)
    except Exception as e:  # noqa: BLE001 -- no pinmap / not a board
        logger.debug(f"Could not enumerate modems for shutdown reset: {e}")
        return

    for modem_name in modems:
        try:
            caps = await asyncio.to_thread(hw_api.modem_capabilities, modem_name)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Could not read capabilities for {modem_name}: {e}")
            continue
        if "reset" not in caps:
            logger.debug(f"Skipping shutdown reset for {modem_name} (no reset pin)")
            continue
        try:
            logger.info(f"Shutdown GPIO reset for {modem_name}")
            await asyncio.to_thread(hw_api.modem_reset, modem=modem_name)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Shutdown GPIO reset failed for {modem_name} "
                f"({type(e).__name__}: {e})"
            )


def _count_hardware_reset(interface_number: int) -> None:
    """Record a successful modem hardware reset in the boot-scoped counters."""
    try:
        wwan_diag.increment(f'hardware_reset_count_{interface_number}')
    except Exception:
        pass


async def _bring_interface_down_safe(interface_name: str) -> bool:
    """
    Safely bring down network interface before USB reset operations.

    This prevents network stack corruption when USB devices disappear abruptly.
    Critical for VM stability during USB reset operations.

    Args:
        interface_name: Interface name (e.g., "wwan0")

    Returns:
        bool: True if interface was brought down or didn't exist, False on error
    """
    try:
        # Check if interface exists first
        check_cmd = ["ip", "link", "show", interface_name]
        result = await asyncio.create_subprocess_exec(
            *check_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()

        if result.returncode != 0:
            # Interface doesn't exist, that's fine
            logger.debug(f"Interface {interface_name} doesn't exist, nothing to bring down")
            return True

        # Check if interface is UP
        interface_info = stdout.decode()
        if "state UP" not in interface_info and ",UP," not in interface_info:
            logger.debug(f"Interface {interface_name} already down")
            return True

        # Bring interface down
        logger.info(f"Bringing interface {interface_name} down before USB reset")
        down_cmd = ["ip", "link", "set", interface_name, "down"]
        result = await asyncio.create_subprocess_exec(
            *down_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()

        if result.returncode == 0:
            logger.info(f"Interface {interface_name} brought down successfully")
            return True
        else:
            logger.warning(f"Failed to bring interface {interface_name} down: {stderr.decode().strip()}")
            return False

    except Exception as e:
        logger.error(f"Error bringing interface {interface_name} down: {e}")
        return False


def _is_running_in_vm() -> bool:
    """Detect if we're running in a virtual machine"""
    try:
        # Check common VM indicators
        vm_indicators = [
            '/sys/class/dmi/id/product_name',
            '/sys/class/dmi/id/sys_vendor',
            '/sys/class/dmi/id/board_vendor'
        ]

        for path in vm_indicators:
            try:
                with open(path, 'r') as f:
                    content = f.read().lower()
                    if any(vm in content for vm in ['qemu', 'kvm', 'virtualbox', 'vmware', 'xen', 'hyper-v']):
                        return True
            except (OSError, IOError):
                continue

        # Check for VM-specific devices
        try:
            with open('/proc/cpuinfo', 'r') as f:
                if 'hypervisor' in f.read().lower():
                    return True
        except (OSError, IOError):
            pass

        return False
    except Exception:
        return False

async def modem_reset(interface_number: int, *,
                       prefer_hardware: bool = False,
                       allow_nuclear: bool = True) -> bool:
    """
    Perform hardware reset of the modem for the specified interface.

    This function attempts various reset methods depending on the hardware
    and system configuration available.

    VM CRASH PROTECTION: Automatically detects VMs and uses safer reset methods.

    Args:
        interface_number: The interface number (e.g., 0 for wwan0)
        prefer_hardware: Try the board GPIO reset BEFORE the ModemManager
            (mmcli) reset.  Used by the GPIO-mux SIM switch, where the
            deterministic hardware reset is the correct way to make the modem
            re-read the newly-selected SIM.
        allow_nuclear: Permit escalation to the nuclear option (restarting the
            ModemManager service) as a last resort.  MUST be False for a SIM
            switch: restarting ModemManager re-enumerates the modem, which
            fires the FSM's reconnect-after-restart path and launches a
            CONCURRENT initial-configuration that collides with the in-progress
            switch (FSM transition errors, enable failures, reset storms).

    Returns:
        bool: True if reset was attempted, False if no reset method available
    """
    logger.info(f"Attempting hardware reset for interface {interface_number}")

    # VM CRASH PROTECTION: Disable hardware resets in VMs
    if _is_running_in_vm():
        logger.warning(f"VM detected - hardware reset disabled for safety (interface {interface_number})")
        if not allow_nuclear:
            logger.warning("Nuclear reset disallowed for this caller (e.g. SIM "
                           "switch) — no usable reset method in VM")
            return False
        logger.info("Using nuclear reset (ModemManager restart) instead of hardware reset")
        return await modem_reset_nuclear(interface_number)

    try:
        # Ordered standard methods.  For a SIM switch (prefer_hardware) the
        # board GPIO reset goes first because it is the deterministic way to
        # make the modem re-read the SIM; otherwise the historical
        # ModemManager (mmcli) reset is tried first.
        if prefer_hardware:
            standard_methods = (
                ('Board hardware', _try_board_modem_reset),
                ('ModemManager', _try_modemmanager_reset),
            )
        else:
            standard_methods = (
                ('ModemManager', _try_modemmanager_reset),
                ('Board hardware', _try_board_modem_reset),
            )

        for label, method in standard_methods:
            if await method(interface_number):
                logger.info(f"{label} reset successful for interface {interface_number}")
                _count_hardware_reset(interface_number)
                return True

        # Nuclear option - restart ModemManager (last resort, opt-out).
        if not allow_nuclear:
            logger.error(f"All hardware reset methods failed for interface "
                         f"{interface_number} and nuclear reset is disallowed "
                         "for this caller (SIM switch) — not restarting ModemManager")
            return False

        logger.warning(f"All standard reset methods failed for interface {interface_number}, trying nuclear option...")
        if await modem_reset_nuclear(interface_number):
            logger.info(f"Nuclear reset (ModemManager restart) successful for interface {interface_number}")
            return True

        logger.error(f"All reset methods failed for interface {interface_number}")
        return False

    except Exception as e:
        logger.error(f"Error during modem reset for interface {interface_number}: {e}")
        return False


async def _try_modemmanager_reset(interface_number: int) -> bool:
    """Try to reset modem using ModemManager"""
    try:
        # Find modem by PhysDevUID

        # Use mmcli to find and reset the modem
        find_cmd = ["mmcli", "-L"]
        result = await asyncio.create_subprocess_exec(
            *find_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()

        if result.returncode != 0:
            logger.debug("mmcli not available or failed")
            return False

        # Parse mmcli output to find modem with matching PhysDevUID
        modem_id = None
        for line in stdout.decode().split('\n'):
            if '/Modem/' in line:
                # Extract modem ID from line like "/org/freedesktop/ModemManager1/Modem/0"
                parts = line.split('/')
                if len(parts) > 0:
                    try:
                        potential_id = parts[-1].split()[0]
                        if potential_id.isdigit():
                            modem_id = potential_id
                            break
                    except (IndexError, ValueError):
                        continue

        if modem_id is None:
            logger.debug("No modem found in ModemManager")
            return False

        # Step 1: Disable the modem to ensure clean bearer teardown
        logger.info(f"Disabling modem {modem_id} before reset")
        disable_cmd = ["mmcli", "-m", modem_id, "--disable"]
        result = await asyncio.create_subprocess_exec(
            *disable_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()

        if result.returncode != 0:
            logger.warning(f"Modem disable failed (continuing with reset): {stderr.decode().strip()}")
        else:
            logger.info(f"Modem {modem_id} disabled successfully")
            # Wait a moment for clean shutdown
            await asyncio.sleep(2)

        # Step 2: Reset the modem
        logger.info(f"Resetting modem {modem_id}")
        reset_cmd = ["mmcli", "-m", modem_id, "--reset"]
        result = await asyncio.create_subprocess_exec(
            *reset_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()

        if result.returncode == 0:
            logger.info(f"Modem {modem_id} reset successfully")
        else:
            logger.error(f"Modem reset failed: {stderr.decode().strip()}")

        if result.returncode != 0:
            return False

        return await _wait_for_modemmanager_reenumeration(interface_number)

    except Exception as e:
        logger.debug(f"ModemManager reset failed: {e}")
        return False


async def _try_board_modem_reset(interface_number: int) -> bool:
    """Try to reset the modem through the board hardware API.

    The board implementation owns the actual GPIO/pulse details. WWAN only
    maps its interface number to the board modem naming convention
    (wwan0 -> MODEM0, wwan1 -> MODEM1, ...).

    The pinmap declares modems with the canonical UPPERCASE name (``MODEM0``)
    and :meth:`Board._resolve_modem` does a case-sensitive dict lookup, so the
    name must be produced via :func:`hw_api.wwan_to_modem`. Building it by hand
    as ``f"modem{n}"`` yields lowercase ``modem0`` which raises
    ``ValueError: unknown modem 'modem0'; declared: MODEM0`` — that silently
    aborts the GPIO reset (the only reset that works while the modem is in the
    ``failed``/``sim-missing`` state, since mmcli --disable/--reset both fail
    with WrongState/InvalidArgument), stranding SIM failover forever.
    """
    modem_name = hw_api.wwan_to_modem(f"wwan{interface_number}")

    try:
        logger.info(f"Performing board hardware reset for {modem_name}")
        await asyncio.to_thread(hw_api.modem_reset, modem=modem_name)
    except Exception as e:
        # Elevated from DEBUG to WARNING with the exception type+message in the
        # visible text: when the board GPIO reset throws, the caller silently
        # falls through to the next method (or nuclear), so a hidden reason
        # here makes a broken hardware-reset path look like "no reset method".
        logger.warning(f"Board hardware reset failed for {modem_name} "
                       f"({type(e).__name__}: {e})")
        return False

    # A reset pulse alone is not enough — wait until the modem is back in a
    # state that ModemManager can see again.
    if await _wait_for_modemmanager_reenumeration(interface_number):
        return True

    logger.warning(f"Board hardware reset completed but modem did not re-enumerate in ModemManager for interface {interface_number}")
    return False


async def _try_usb_reset(interface_number: int) -> bool:
    """Try to reset modem via USB device reset"""
    try:
        # Look for USB device paths that might correspond to the modem
        usb_devices = Path("/sys/bus/usb/devices")
        if not usb_devices.exists():
            return False

        # Common patterns for modem device paths
        device_patterns = [
            f"ttyUSB{interface_number}",
            f"cdc-wdm{interface_number}",
            f"wwan{interface_number}"
        ]

        # Find USB device associated with this interface
        for device_dir in usb_devices.iterdir():
            if device_dir.is_dir():
                # Check if this USB device has our interface
                for pattern in device_patterns:
                    if (device_dir / "**" / pattern).exists():
                        # Found matching device, try to reset it
                        reset_file = device_dir / "authorized"
                        if reset_file.exists():
                            # CRITICAL: Bring down network interface before USB reset
                            # This prevents network stack corruption and VM crashes
                            interface_name = f"wwan{interface_number}"
                            await _bring_interface_down_safe(interface_name)

                            # Disable and re-enable device
                            try:
                                logger.info(f"Performing USB reset on device {device_dir.name}")

                                # Deauthorize device (this makes USB device disappear)
                                reset_file.write_text("0")
                                await asyncio.sleep(2)

                                # Re-authorize device (this triggers USB re-enumeration)
                                reset_file.write_text("1")
                                await asyncio.sleep(3)

                                logger.info(f"USB reset completed for device {device_dir.name}")
                                return True
                            except PermissionError:
                                logger.debug("Permission denied for USB reset")
                                return False

        return False

    except Exception as e:
        logger.debug(f"USB reset failed: {e}")
        return False


async def _try_gpio_reset(interface_number: int) -> bool:
    """Try to reset modem via GPIO control"""
    try:
        # Look for GPIO reset pins in common locations
        gpio_paths = [
            f"/sys/class/gpio/modem{interface_number}_reset",
            f"/sys/class/gpio/wwan{interface_number}_reset",
            "/sys/class/gpio/modem_reset",
            "/sys/class/gpio/cellular_reset"
        ]

        for gpio_path in gpio_paths:
            gpio_dir = Path(gpio_path)
            if gpio_dir.exists():
                value_file = gpio_dir / "value"
                if value_file.exists():
                    try:
                        # CRITICAL: Bring down interface before GPIO reset
                        interface_name = f"wwan{interface_number}"
                        await _bring_interface_down_safe(interface_name)

                        logger.info(f"Performing GPIO reset using {gpio_path}")

                        # Pull reset low, wait, then high
                        value_file.write_text("0")
                        await asyncio.sleep(1)
                        value_file.write_text("1")
                        await asyncio.sleep(2)

                        logger.info(f"GPIO reset completed using {gpio_path}")
                        return True
                    except PermissionError:
                        logger.debug(f"Permission denied for GPIO reset at {gpio_path}")

        return False

    except Exception as e:
        logger.debug(f"GPIO reset failed: {e}")
        return False


async def _try_usb_power_cycle(interface_number: int) -> bool:
    """Try to power cycle modem via USB hub control"""
    try:
        # Look for uhubctl or other USB hub control utilities
        uhubctl_cmd = ["uhubctl", "--action", "cycle", "--delay", "2"]

        # Try to find uhubctl
        which_result = await asyncio.create_subprocess_exec(
            "which", "uhubctl",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await which_result.communicate()

        if which_result.returncode != 0:
            logger.debug("uhubctl not available")
            return False

        # CRITICAL: Bring down interface before USB power cycle
        interface_name = f"wwan{interface_number}"
        await _bring_interface_down_safe(interface_name)

        logger.info(f"Performing USB power cycle for interface {interface_number}")

        # Execute power cycle
        result = await asyncio.create_subprocess_exec(
            *uhubctl_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await result.communicate()

        return result.returncode == 0

    except Exception as e:
        logger.debug(f"USB power cycle failed: {e}")
        return False


def get_interface_device_path(interface_number: int) -> str:
    """
    Get the device path for a WWAN interface.

    Args:
        interface_number: The interface number

    Returns:
        str: Device path (e.g., /dev/ttyUSB0) or empty string if not found
    """
    common_patterns = [
        f"/dev/ttyUSB{interface_number}",
        f"/dev/cdc-wdm{interface_number}",
        f"/dev/wwan{interface_number}",
        f"/dev/ttyACM{interface_number}"
    ]

    for pattern in common_patterns:
        if Path(pattern).exists():
            return pattern

    return ""


def get_interface_sysfs_path(interface_number: int) -> str:
    """
    Get the sysfs path for a WWAN interface.

    Args:
        interface_number: The interface number

    Returns:
        str: Sysfs path or empty string if not found
    """
    common_patterns = [
        f"/sys/class/net/wwan{interface_number}",
        f"/sys/class/wwan/wwan{interface_number}"
    ]

    for pattern in common_patterns:
        if Path(pattern).exists():
            return pattern

    return ""


async def wait_for_interface_ready(interface_number: int, timeout: int = 30) -> bool:
    """
    Wait for interface to become ready after reset.

    Args:
        interface_number: The interface number
        timeout: Maximum time to wait in seconds

    Returns:
        bool: True if interface became ready, False if timeout
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        # Check if device path exists
        device_path = get_interface_device_path(interface_number)
        if device_path:
            # Additional check: try to access the device
            try:
                path_obj = Path(device_path)
                if path_obj.exists() and path_obj.is_char_device():
                    logger.info(f"Interface {interface_number} ready at {device_path}")
                    return True
            except Exception:
                pass

        await asyncio.sleep(1)

    logger.warning(f"Interface {interface_number} not ready after {timeout} seconds")
    return False


async def _wait_for_modemmanager_reenumeration(interface_number: int, timeout: int = 60) -> bool:
    """Wait until ModemManager sees the modem again after a hardware reset."""
    deadline = time.time() + timeout
    modem_name = f"modem{interface_number}"

    while time.time() < deadline:
        # Check that the underlying device node has come back first.
        if not await wait_for_interface_ready(interface_number, timeout=1):
            await asyncio.sleep(1)
            continue

        # Then verify ModemManager can enumerate at least one modem again.
        try:
            result = await asyncio.create_subprocess_exec(
                "mmcli", "-L",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await result.communicate()
            if result.returncode == 0 and "/Modem/" in stdout.decode():
                logger.info(f"ModemManager re-detected modem after reset for {modem_name}")
                return True
        except Exception as e:
            logger.debug(f"ModemManager re-enumeration check failed for {modem_name}: {e}")

        await asyncio.sleep(1)

    return False


# Synchronous wrapper for callers outside an asyncio event loop
def modem_reset_sync(interface_number: int) -> bool:
    """
    Synchronous wrapper around :func:`modem_reset` for callers that are
    not running inside an asyncio event loop.

    Args:
        interface_number: The interface number

    Returns:
        bool: True if reset was attempted
    """
    try:
        return asyncio.run(modem_reset(interface_number))
    except Exception as e:
        logger.error(f"Synchronous modem reset failed: {e}")
        return False


async def modem_reset_quiesced(interface_number: int, *,
                              settle_seconds: float = 70.0,
                              reenumerate_timeout: float = 60.0) -> bool:
    """Hardware-reset the modem with ModemManager QUIESCED around the pulse.

    Some modems (notably the Telit FN920C04 on early firmware) enumerate on USB
    within seconds of a reset but leave their QMI/AT command stack unresponsive
    for ~a minute afterwards.  If ModemManager keeps probing across the reset it
    talks to the not-yet-ready modem, fails to classify it, and gives up -- so a
    plain board reset "succeeds" (the modem re-enumerates) yet MM never creates
    a modem object, and recovery only happens by luck after several attempts.

    This variant removes the race: it STOPS ModemManager, PERST-resets the
    modem, waits an UNPROBED ``settle_seconds`` window for the command stack to
    come up, then starts ModemManager so it probes an already-ready modem and
    classifies it on the first pass.  The MM watchdog is told, via
    begin_managed_mm_downtime(), not to "recover" the deliberate stop.

    Returns True if ModemManager re-enumerated a modem afterwards.
    """
    if _is_running_in_vm():
        logger.warning("VM detected -- quiesced hardware reset unavailable "
                       f"for interface {interface_number}")
        return False

    logger.info("Quiesced hardware reset for interface %d: stop MM -> PERST -> "
                "%.0fs unprobed settle -> start MM",
                interface_number, settle_seconds)

    # Keep the MM watchdog from restarting MM while we deliberately hold it
    # down.  Cover the whole operation (stop + settle + start + re-enum wait)
    # plus margin; it auto-expires so a failure mid-reset cannot wedge MM off.
    begin_managed_mm_downtime(settle_seconds + reenumerate_timeout + 60.0)
    try:
        # 1. Stop ModemManager so it stops probing the modem.
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "systemctl", "stop", "ModemManager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=20)
            logger.info("ModemManager stopped for quiesced reset")
        except Exception as e:  # noqa: BLE001 -- best effort
            logger.warning(
                f"Could not stop ModemManager for quiesced reset: {e}")

        # 2. PERST the modem (deterministic board GPIO reset).
        pulsed = await _try_board_modem_reset(interface_number)
        if not pulsed:
            logger.warning("Board PERST reset unavailable/failed; continuing "
                           "with an unprobed settle + MM restart anyway")

        # 3. UNPROBED settle -- let the modem boot its command stack with
        #    nobody poking it.  This is the whole point of the quiesce.
        logger.info("Modem settling for %.0fs with ModemManager stopped "
                    "(unprobed)", settle_seconds)
        await asyncio.sleep(max(0.0, settle_seconds))

        # 4. Re-trigger USB udev rules (so MM gets the physical-slot UID, as at
        #    boot) then start ModemManager to probe the now-ready modem.
        try:
            trig = await asyncio.create_subprocess_exec(
                "udevadm", "trigger", "--action=change",
                "--subsystem-match=usb",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(trig.communicate(), timeout=15)
            settle = await asyncio.create_subprocess_exec(
                "udevadm", "settle", "--timeout=10",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(settle.communicate(), timeout=15)
        except Exception as e:  # noqa: BLE001 -- best effort
            logger.warning(f"udev re-trigger before MM start failed: {e}")

        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "systemctl", "start", "ModemManager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=20)
            logger.info("ModemManager started after quiesced reset settle")
        except Exception as e:  # noqa: BLE001
            logger.error(
                f"Failed to start ModemManager after quiesced reset: {e}")
            return False

        # 5. Confirm MM re-enumerated the modem.
        ok = await _wait_for_modemmanager_reenumeration(
            interface_number, timeout=int(reenumerate_timeout))
        if ok:
            logger.info("Quiesced reset: ModemManager re-enumerated the modem "
                        f"for interface {interface_number}")
            _count_hardware_reset(interface_number)
        else:
            logger.warning("Quiesced reset: ModemManager still shows no modem "
                           f"for interface {interface_number} after settle")
        return ok
    finally:
        end_managed_mm_downtime()


async def modem_reset_nuclear(interface_number: int) -> bool:
    """
    Nuclear option: Restart ModemManager entirely to recover from QMI issues.

    This is a last resort when normal modem reset fails due to corrupted
    QMI interface state. It will:
    1. Stop ModemManager service
    2. Wait for cleanup
    3. Restart ModemManager service
    4. Wait for modem re-detection

    Args:
        interface_number: The interface number (e.g., 0 for wwan0)

    Returns:
        bool: True if ModemManager restart succeeded, False otherwise
    """
    logger.warning(f"Attempting nuclear reset (ModemManager restart) for interface {interface_number}")

    try:
        # Step 1: Stop ModemManager
        logger.info("Stopping ModemManager service...")
        stop_cmd = ["sudo", "systemctl", "stop", "ModemManager"]
        result = await asyncio.create_subprocess_exec(
            *stop_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await result.communicate()

        if result.returncode != 0:
            logger.error("Failed to stop ModemManager")
            return False

        # Step 2: Wait for cleanup
        logger.info("Waiting for ModemManager cleanup...")
        await asyncio.sleep(5)

        # Step 3: Start ModemManager
        logger.info("Starting ModemManager service...")
        start_cmd = ["sudo", "systemctl", "start", "ModemManager"]
        result = await asyncio.create_subprocess_exec(
            *start_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await result.communicate()

        if result.returncode != 0:
            logger.error("Failed to start ModemManager")
            return False

        # Step 4: Wait for modem re-detection
        logger.info("Waiting for modem re-detection...")
        await asyncio.sleep(10)

        if await _wait_for_modemmanager_reenumeration(interface_number):
            logger.info(f"Nuclear reset completed for interface {interface_number}")
            try:
                wwan_diag.increment('modem_nuclear_reset_count')
            except Exception:
                pass
            return True

        logger.warning(f"Modem did not re-enumerate after ModemManager restart for interface {interface_number}")
        return False

    except Exception as e:
        logger.error(f"Nuclear reset failed for interface {interface_number}: {e}")
        return False


async def restart_modemmanager_only(interface_number: int, *,
                                    reenumerate_timeout: int = 30) -> bool:
    """Restart ONLY the ModemManager service (no hardware/modem reset) and wait
    for the modem to be re-detected.

    Recovers the "ModemManager gave up on the modem" case: after consecutive
    control-port timeouts MM invalidates and drops the modem object
    ("marking modem as invalid") while the modem's own AT/QMI command stack is
    still alive.  A fresh MM process re-probes and finds it — far cheaper than a
    board reset, and it never power-cycles the modem or consumes the
    hardware-reset budget.

    It does NOT recover a genuine firmware command-stack wedge (the modem will
    time out the re-probe too); the caller should then escalate to a hardware
    reset.

    The stop/start is wrapped in the managed-MM-downtime guard so the
    ModemManagerMonitor does not race it with its own crash-recovery restart.

    Returns True only if the modem re-appears in ModemManager within
    ``reenumerate_timeout`` seconds.
    """
    logger.warning(
        "Restarting ModemManager only (no hardware reset) for interface %d — "
        "recovering a dropped/invalidated modem object", interface_number)

    # Hold the MM monitor off for the stop+start+re-enum window.  The guard is
    # deadline-based and auto-expires, so a crash mid-restart cannot leave MM
    # unmonitored forever.
    begin_managed_mm_downtime(reenumerate_timeout + 30.0)
    try:
        stop = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "stop", "ModemManager",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await stop.communicate()
        if stop.returncode != 0:
            logger.error("restart_modemmanager_only: failed to stop ModemManager")
            return False

        # Brief pause so MM fully releases the QMI/AT ports before it re-probes.
        await asyncio.sleep(3)

        start = await asyncio.create_subprocess_exec(
            "sudo", "systemctl", "start", "ModemManager",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await start.communicate()
        if start.returncode != 0:
            logger.error("restart_modemmanager_only: failed to start ModemManager")
            return False

        ok = await _wait_for_modemmanager_reenumeration(
            interface_number, timeout=reenumerate_timeout)
        if ok:
            logger.info(
                "restart_modemmanager_only: modem re-appeared after MM restart "
                "for interface %d", interface_number)
            try:
                wwan_diag.increment('modem_nuclear_reset_count')
            except Exception:
                pass
        else:
            logger.info(
                "restart_modemmanager_only: modem still absent after MM restart "
                "for interface %d (likely a real command-stack wedge)",
                interface_number)
        return ok
    except Exception as e:
        logger.error(
            "restart_modemmanager_only failed for interface %d: %s",
            interface_number, e)
        return False
    finally:
        end_managed_mm_downtime()
