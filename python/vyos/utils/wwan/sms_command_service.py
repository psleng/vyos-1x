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

"""SMS command service for the WWAN subsystem.

Self-contained daemon that polls unread incoming SMS messages and executes a
strictly allowlisted command set.  v1 supports only REBOOT.

Environment variables:

- IGOS_SMS_COMMAND_INTERFACES: comma-separated list, e.g. "wwan0,wwan1"
  (default: "wwan0")
- IGOS_SMS_COMMAND_AUTHORIZED_NUMBERS: JSON mapping of interface names to
  sender-number/PIN mappings (required)
- IGOS_SMS_COMMAND_POLL_INTERVAL: polling interval in seconds (default: 0.5)
- IGOS_SMS_COMMAND_RESPONSE_ENABLED: 1/true/yes to send
    ACCEPTED/REJECTED SMS responses (default: true)

Accepted command format:

    123456 REBOOT

Matching is case-sensitive (capital letters only).

Commands older than 60 seconds when read are rejected as expired.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import SysLogHandler
from typing import Dict, List

from vyos.utils.wwan.wwan_client import WWANClientSync, WWANError


logger = logging.getLogger('vyos.wwan.sms_command_service')

SMS_COMMAND_MAX_AGE = 60.0

REBOOT_COMMAND_RE = re.compile(r'\s*([0-9]{6})\s+REBOOT\s*')
SHOW_SYSTEM_INFO_COMMAND_RE = re.compile(r'\s*SHOW\s+SYSTEM\s+INFO\s*')
PING_COMMAND_RE = re.compile(r'\s*PING\s+(\S+)\s*')
SHOW_WAN_IP_ADDRESS_COMMAND_RE = re.compile(r'\s*SHOW\s+WAN\s+IP\s+ADDRESS\s*')
SHOW_CELL_STATUS_COMMAND_RE = re.compile(r'\s*SHOW\s+CELL\s+STATUS\s*')
CELL_CONNECT_DISCONNECT_COMMAND_RE = re.compile(r'\s*CELL\s+(CONNECT|DISCONNECT)\s*')


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _parse_interfaces(raw: str) -> List[str]:
    result = []
    for item in raw.split(','):
        name = item.strip()
        if not name:
            continue
        if not re.fullmatch(r'wwan\d+', name):
            raise ValueError(f'Invalid WWAN interface name: {name}')
        result.append(name)
    if not result:
        raise ValueError('No valid WWAN interfaces configured')
    return result


def _if_number(name: str) -> int:
    return int(name.replace('wwan', ''))


@dataclass
class ServiceConfig:
    interfaces: List[str]
    authorized_numbers: Dict[str, Dict[str, str]] = field(repr=False)
    poll_interval: float
    response_enabled: bool

    @classmethod
    def from_env(cls) -> 'ServiceConfig':
        interfaces = _parse_interfaces(
            os.environ.get('IGOS_SMS_COMMAND_INTERFACES', 'wwan0')
        )

        try:
            authorized_numbers = json.loads(
                os.environ.get('IGOS_SMS_COMMAND_AUTHORIZED_NUMBERS', '{}')
            )
        except (ValueError, TypeError):
            raise ValueError('Invalid authorized-number configuration') from None
        if not isinstance(authorized_numbers, dict) or not authorized_numbers:
            raise ValueError('Authorized numbers with six-digit PINs are required')
        for interface in interfaces:
            numbers = authorized_numbers.get(interface)
            if not isinstance(numbers, dict) or not numbers:
                raise ValueError(f'Authorized numbers are required for {interface}')
            for number, pin in numbers.items():
                if (not re.fullmatch(r'\+?[0-9]{6,20}', number)
                        or not isinstance(pin, str)
                        or not re.fullmatch(r'[0-9]{6}', pin)):
                    raise ValueError(f'Invalid authorized-number or PIN for {interface}')

        poll_interval = float(os.environ.get('IGOS_SMS_COMMAND_POLL_INTERVAL', '0.5'))
        response_enabled = _as_bool(
            os.environ.get('IGOS_SMS_COMMAND_RESPONSE_ENABLED'),
            True,
        )

        return cls(
            interfaces=interfaces,
            authorized_numbers=authorized_numbers,
            poll_interval=max(0.5, poll_interval),
            response_enabled=response_enabled,
        )


class SmsCommandService:
    def __init__(self, cfg: ServiceConfig):
        self.cfg = cfg
        self.client = WWANClientSync()
        self._running = True
        self._seen: Dict[int, set[int]] = {}

    def stop(self, *_args):
        self._running = False

    def _send_response(self, if_num: int, number: str, message: str):
        if not self.cfg.response_enabled:
            return
        try:
            self.client.send_sms(if_num, number, message)
        except Exception as err:  # pragma: no cover - best effort
            logger.warning('Failed to send response SMS: %s', err)

    def _execute_reboot(self) -> bool:
        try:
            subprocess.run(
                ['/usr/libexec/vyos/op_mode/powerctrl.py', '--yes', '--reboot'],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return True
        except (OSError, subprocess.SubprocessError) as err:
            logger.error('Failed to execute reboot: %s', err)
            return False

    @staticmethod
    def _system_info() -> str:
        """Return the compact system information displayed by the web UI."""
        from vyos.utils.system import get_uptime_seconds

        def output(command, fallback='N/A'):
            try:
                return subprocess.check_output(command, text=True, timeout=3).strip() or fallback
            except (OSError, subprocess.SubprocessError):
                return fallback

        version_output = output(['/usr/libexec/vyos/op_mode/version.py', 'show'])
        version_match = re.search(
            r'^\s*Version:\s*(.+?)\s*$', version_output, re.MULTILINE
        )
        version = version_match.group(1) if version_match else ''
        if not version or version == 'N/A':
            try:
                from vyos.version import get_version
                version = get_version()
            except (ImportError, OSError, TypeError, ValueError):
                version = 'N/A'
        timezone_name = output(['timedatectl', 'show', '-p', 'Timezone', '--value'])
        try:
            uptime_minutes, uptime_seconds = divmod(int(get_uptime_seconds()), 60)
            uptime = f'{uptime_minutes} minutes {uptime_seconds} seconds'
        except (TypeError, ValueError, OSError):
            uptime = 'N/A'
        info = (
            f'Hostname: {socket.gethostname()}\n'
            f'Version: {version}\n'
            f'System time: {time.strftime("%m/%d/%Y, %I:%M:%S %p")}\n'
            f'Timezone: {timezone_name}\n'
            f'Uptime: {uptime}'
        )
        return info[:160]

    @staticmethod
    def _interface_addresses() -> str:
        """Return interface addresses as plain, one-address-per-line text."""
        try:
            output = subprocess.check_output(
                ['/usr/libexec/vyos/op_mode/interfaces.py', 'show'],
                text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as err:
            logger.warning('Failed to query interface addresses via op-mode: %s', err)
            try:
                output = subprocess.check_output(
                    ['/usr/bin/ip', '-o', 'address', 'show'],
                    text=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError) as fallback_err:
                logger.warning('Failed to query interface addresses via ip: %s', fallback_err)
                return 'Interface addresses unavailable'
        addresses = {}
        current_interface = None
        address_re = re.compile(r'(?<![\w:])(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]+)/(?:\d{1,3})')
        for line in output.splitlines():
            ip_output_match = re.match(r'^\d+:\s+(\S+)\s+(?:inet6?|link)\s+', line)
            if ip_output_match:
                current_interface = ip_output_match.group(1)
            match = re.match(r'^\s*(\S+)\s+', line)
            if match and not line.startswith((' ', '\t')) and not ip_output_match:
                current_interface = match.group(1).rstrip(':')
            if current_interface:
                values = address_re.findall(line)
                if values:
                    addresses.setdefault(current_interface, []).extend(values)
        lines = []
        for interface, values in addresses.items():
            lines.extend(f'{interface}: {address}' for address in values)
        return '\n'.join(lines) if lines else 'No interface addresses'

    @staticmethod
    def _ping_host(host: str) -> str:
        try:
            result = subprocess.run(
                ['/usr/bin/ping', '-c', '5',
                 '-i', '0.1', '-W', '5', host],
                check=False, capture_output=True, text=True, timeout=10,
            )
            output = (result.stdout or result.stderr).strip()
            return '\n'.join(line.strip() for line in output.splitlines()) if output else 'FAILED, no output'
        except (OSError, socket.gaierror, subprocess.SubprocessError):
            return 'FAILED, loss=100%'

    def _handle_system_info(self, if_num, number, audit):
        response = self._system_info()
        audit(response.replace('\n', ' | '))
        self._send_response(if_num, number, response)

    def _handle_interface_addresses(self, if_num, number, audit):
        response = self._interface_addresses()
        audit(response)
        self._send_response(if_num, number, response)

    def _handle_cell_status(self, if_name, if_num, number, audit):
        """Return the output of ``show interfaces wwan <name> status``."""
        try:
            result = subprocess.run(
                ['/usr/libexec/vyos/op_mode/show_wwan.py', 'show_status',
                 f'--interface={if_name}'],
                check=False, capture_output=True, text=True, timeout=15,
            )
            response = (result.stdout or result.stderr).strip() or 'FAILED'
        except (OSError, subprocess.SubprocessError):
            response = 'FAILED'
        audit(response)
        self._send_response(if_num, number, response)

    def _handle_ping(self, if_num, number, host, audit):
        response = f'PING {host}: {self._ping_host(host)}'
        audit(response)
        self._send_response(if_num, number, response)

    def _handle_reboot(self, if_num, number, audit):
        if self._execute_reboot():
            audit('OK')
            self._send_response(if_num, number, 'OK')
        else:
            audit('REBOOT FAILED')
            self._send_response(if_num, number, 'REBOOT FAILED')

    def _handle_cell(self, if_num, number, action, audit):
        try:
            operation = '--connect' if action == 'CONNECT' else '--disconnect'
            result = subprocess.run(
                ['/usr/libexec/vyos/op_mode/connect_disconnect.py', operation,
                 '--interface', f'wwan{if_num}'],
                check=False, capture_output=True, text=True, timeout=10,
            )
            output = (result.stdout or result.stderr).strip()
            if result.returncode != 0:
                response = output or 'FAILED'
            else:
                target = 'connected' if action == 'CONNECT' else 'disconnected'
                verified = self._wait_bearer_status(if_num, target)
                status = self.client.get_bearer_status(if_num)
                logger.info('Cellular %s interface=%s bearer_status=%s verified=%s',
                            action.lower(), if_num, status, verified)
                response = 'OK' if verified else 'FAILED'
        except FileNotFoundError:
            # Unit-test/minimal environments may not contain the installed
            # op-mode script; retain the direct WWAN-client fallback there.
            try:
                if action == 'CONNECT':
                    verified = self.client.connect_bearer_and_wait(
                        if_num, timeout=30, poll_interval=0.5)
                else:
                    verified = self.client.disconnect_bearer_and_wait(
                        if_num, timeout=30, poll_interval=0.5)
                response = 'OK' if verified else 'FAILED'
            except Exception as err:
                logger.warning('Cellular %s failed on interface %s: %s', action.lower(), if_num, err)
                response = 'FAILED'
        except Exception as err:
            logger.warning('Cellular %s failed on interface %s: %s', action.lower(), if_num, err)
            response = 'FAILED'
        audit(response)
        self._send_response(if_num, number, response)

    def _wait_bearer_status(self, if_num, target, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.client.get_bearer_status(if_num) == target:
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _message_age_seconds(timestamp):
        """Return message age, or ``None`` when the modem timestamp is invalid."""
        if not timestamp:
            return None
        try:
            value = str(timestamp).strip()
            if value.endswith('Z'):
                value = value[:-1] + '+00:00'
            received = datetime.fromisoformat(value)
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - received.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _message_expired(cls, timestamp, *, now=None):
        """Reject messages older than one minute; invalid timestamps fail closed."""
        age = cls._message_age_seconds(timestamp) if now is None else None
        if now is not None:
            try:
                value = str(timestamp).strip()
                if value.endswith('Z'):
                    value = value[:-1] + '+00:00'
                received = datetime.fromisoformat(value)
                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
                age = (now - received.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                age = None
        return age is None or age > SMS_COMMAND_MAX_AGE or age < -SMS_COMMAND_MAX_AGE

    def _process_message(self, if_name: str, if_num: int, msg: dict):
        msg_id = int(msg.get('id', -1))
        number = str(msg.get('number', '')).strip()

        text = str(msg.get('text', ''))
        match = REBOOT_COMMAND_RE.fullmatch(text)
        # Show the received message for audit purposes, while masking the
        # leading PIN (including malformed PIN attempts).
        command = re.sub(r'^(\s*)[0-9]+(?=\s+)', r'\1[PIN]', text).strip()
        if not command:
            command = '[EMPTY]'

        def audit(result):
            logger.info(
                'SMS command timestamp=%s sender=%r command=%s result=%s interface=%s id=%s',
                datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                number, repr(command),
                result, if_name, msg_id,
            )

        if self._message_expired(msg.get('timestamp')):
            audit('EXPIRED')
            self._send_response(if_num, number, 'EXPIRED')
            return

        expected_pin = self.cfg.authorized_numbers.get(if_name, {}).get(number)
        if expected_pin is None:
            audit('UNAUTHORIZED')
            self._send_response(if_num, number, 'UNAUTHORIZED')
            return

        if SHOW_SYSTEM_INFO_COMMAND_RE.fullmatch(text):
            self._handle_system_info(if_num, number, audit)
            return

        if SHOW_WAN_IP_ADDRESS_COMMAND_RE.fullmatch(text):
            self._handle_interface_addresses(if_num, number, audit)
            return

        if SHOW_CELL_STATUS_COMMAND_RE.fullmatch(text):
            self._handle_cell_status(if_name, if_num, number, audit)
            return

        ping_match = PING_COMMAND_RE.fullmatch(text)
        if ping_match:
            self._handle_ping(if_num, number, ping_match.group(1), audit)
            return

        cell_match = CELL_CONNECT_DISCONNECT_COMMAND_RE.fullmatch(text)
        if cell_match:
            self._handle_cell(if_num, number, cell_match.group(1), audit)
            return

        if not match:
            tokens = text.split()
            # Any numeric first token is a PIN attempt, regardless of command
            # casing or spelling; malformed attempts receive UNAUTHORIZED.
            malformed_pin = len(tokens) >= 2 and tokens[0].isdigit()
            result = 'UNAUTHORIZED' if malformed_pin else 'INVALID'
            audit(result)
            self._send_response(if_num, number, 'UNAUTHORIZED' if malformed_pin else 'INVALID')
            return

        if not hmac.compare_digest(match.group(1), expected_pin):
            audit('UNAUTHORIZED')
            self._send_response(if_num, number, 'UNAUTHORIZED')
            return

        self._handle_reboot(if_num, number, audit)

    def _poll_interface(self, if_name: str):
        if_num = _if_number(if_name)
        seen = self._seen.setdefault(if_num, set())

        try:
            messages = self.client.list_sms(if_num)
        except WWANError as err:
            logger.debug('SMS poll skipped for %s: %s', if_name, err)
            return

        # Clearing SMS storage recycles message IDs from 1; drop stale
        # "seen" entries no longer present so recycled IDs aren't skipped.
        current_ids = {int(msg.get('id', -1)) for msg in messages}
        seen.intersection_update(current_ids)

        for msg in messages:
            msg_id = int(msg.get('id', -1))
            if msg.get('direction') != 'incoming':
                logger.debug(
                    'Skipping SMS id=%s on %s: direction=%r',
                    msg_id,
                    if_name,
                    msg.get('direction'),
                )
                continue
            if msg_id <= 0:
                logger.debug('Skipping SMS with invalid id=%s on %s', msg_id, if_name)
                continue
            if msg.get('read', False):
                seen.add(msg_id)
                continue
            if msg_id in seen:
                continue

            logger.debug('New unread SMS id=%s on %s', msg_id, if_name)
            try:
                full_msg = self.client.read_sms(if_num, msg_id)
            except Exception as err:
                logger.warning('Failed to read SMS id=%s on %s: %s', msg_id, if_name, err)
                continue

            seen.add(msg_id)
            try:
                self._process_message(if_name, if_num, full_msg)
            except Exception as err:
                # Message is already marked read/seen and won't be retried,
                # so a crash here must never pass silently.
                logger.error(
                    'Error processing SMS id=%s on %s: %s', msg_id, if_name, err
                )

    def run(self):
        if not self.cfg.authorized_numbers:
            logger.error('No authorized numbers with PINs configured')
            return 2

        logger.info(
            'Starting WWAN SMS command service for interfaces=%s response_enabled=%s',
            ','.join(self.cfg.interfaces),
            self.cfg.response_enabled,
        )

        while self._running:
            for if_name in self.cfg.interfaces:
                self._poll_interface(if_name)
            time.sleep(self.cfg.poll_interval)

        logger.info('WWAN SMS command service stopped')
        return 0


def _configure_logging():
    level_name = os.environ.get('IGOS_SMS_COMMAND_LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format='%(asctime)s sms-command[%(process)d]: %(levelname)s: %(message)s',
    )
    if os.path.exists('/dev/log'):
        handler = SysLogHandler(address='/dev/log')
        handler.setFormatter(logging.Formatter(
            'sms-command[%(process)d]: %(levelname)s: %(message)s'
        ))
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False


def main() -> int:
    _configure_logging()

    try:
        cfg = ServiceConfig.from_env()
    except Exception as err:
        logger.error('Invalid configuration: %s', err)
        return 2

    svc = SmsCommandService(cfg)
    signal.signal(signal.SIGTERM, svc.stop)
    signal.signal(signal.SIGINT, svc.stop)
    return svc.run()


if __name__ == '__main__':
    raise SystemExit(main())
