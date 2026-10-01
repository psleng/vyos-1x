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
  sender-number/password mappings (required)
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

REBOOT_COMMAND_RE = re.compile(r'\s*(\S+)\s+REBOOT\s*')
CELL_REBOOT_COMMAND_RE = re.compile(r'\s*(\S+)\s+CELL\s+REBOOT\s*')
SHOW_SYSTEM_INFO_COMMAND_RE = re.compile(r'\s*SHOW\s+SYSTEM\s+INFO\s*')
PING_COMMAND_RE = re.compile(r'\s*PING\s+(\S+)\s*')
SHOW_WAN_STATUS_COMMAND_RE = re.compile(r'\s*SHOW\s+WAN\s+STATUS\s*')
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
            for number, password in numbers.items():
                if (not re.fullmatch(r'\+?[0-9]{6,20}', number)
                        or not isinstance(password, str)
                        or len(password) < 9
                        or not re.fullmatch(r'\S+', password)
                        or not re.search(r'[A-Z]', password)
                        or not re.search(r'[0-9]', password)
                        or not re.search(r'[^A-Za-z0-9]', password)):
                    raise ValueError(f'Invalid authorized-number or password for {interface}')

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
            result = subprocess.run(
                ['/usr/bin/systemctl', 'reboot'],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True
            error = (result.stderr or result.stdout).strip()
            logger.error('Failed to execute reboot (exit status %s): %s',
                         result.returncode, error or 'no output')
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
    def _interface_addresses(interface: str) -> str:
        """Return live WAN address, type, link, and failover status blocks."""
        interfaces = {interface}
        route_metrics = {}
        try:
            routes = json.loads(subprocess.check_output(
                ['/usr/bin/ip', '-j', 'route', 'show', 'table', 'main', 'default'],
                text=True, timeout=5,
            ))
            for route in routes:
                if route.get('dev'):
                    interfaces.add(route['dev'])
                    route_metrics[route['dev']] = route.get('metric', 0)
                for nexthop in route.get('nexthops', []):
                    if nexthop.get('dev'):
                        interfaces.add(nexthop['dev'])
                        route_metrics[nexthop['dev']] = nexthop.get('metric', 0)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            logger.warning('Failed to discover WAN interfaces from default routes')

        # Include addressed interfaces without a default route as well.  This
        # lets SHOW WAN STATUS report an interface as Disable instead of
        # silently omitting it when another WAN is active.
        try:
            addresses = json.loads(subprocess.check_output(
                ['/usr/bin/ip', '-j', 'address', 'show'],
                text=True, timeout=5,
            ))
            for link in addresses:
                name = link.get('ifname')
                if not name or name == 'lo' or name.startswith('pim'):
                    continue
                if any(addr.get('family') in ('inet', 'inet6')
                       for addr in link.get('addr_info', [])):
                    interfaces.add(name)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            logger.warning('Failed to discover addressed interfaces')

        ranked_interfaces = [name for name, _ in sorted(
            route_metrics.items(), key=lambda item: (item[1], item[0]))
        ]
        route_rank = {name: position for position, name
                      in enumerate(ranked_interfaces)}

        records = []
        for current_interface in sorted(interfaces):
            try:
                output = subprocess.check_output(
                    ['/usr/bin/ip', '-o', 'address', 'show', 'dev', current_interface],
                    text=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                output = ''
            values_for_interface = []
            address_re = re.compile(
                r'(?<![\w:])(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F:]+)/(?:\d{1,3})'
            )
            for line in output.splitlines():
                values_for_interface.extend(address_re.findall(line))
            try:
                link = json.loads(subprocess.check_output(
                    ['/usr/bin/ip', '-j', 'link', 'show', 'dev', current_interface],
                    text=True, timeout=5))[0]
                flags = link.get('flags', [])
                status = str(link.get('operstate', 'UNKNOWN')).upper()
                if 'UP' not in flags:
                    status = 'DOWN'
                elif 'LOWER_UP' in flags:
                    status = 'UP'
            except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                status = 'UNKNOWN'
            if current_interface.startswith('wwan'):
                kind = 'Cellular'
            elif current_interface.startswith(('wlan', 'wifi')):
                kind = 'WLAN'
            elif current_interface.startswith(('eth', 'enp', 'ens', 'eno')):
                kind = 'Ethernet'
            elif current_interface.startswith(('br', 'bond', 'ppp')):
                kind = 'Virtual'
            else:
                kind = 'Other'
            if current_interface in route_rank:
                position = route_rank[current_interface] + 1
                ordinal = {1: '1st', 2: '2nd', 3: '3rd'}.get(
                    position, f'{position}th'
                )
                failover_status = (
                    f'Active ({ordinal})' if position == 1
                    else f'Standby ({ordinal})'
                )
            else:
                failover_status = 'Disable'
            records.append((current_interface, values_for_interface, kind,
                            status, failover_status))

        if not records:
            return 'No WAN interface status'
        records.sort(key=lambda record: (
            route_rank.get(record[0], len(route_rank)), record[0]
        ))
        lines = []
        for name, values, kind, status, failover_status in records:
            ipv4 = next((value for value in values if '.' in value), '-')
            ipv6 = next((value for value in values if ':' in value), '-')
            lines.extend((
                f'WAN Interface: {name}',
                f'IPv4 Address: {ipv4}',
                f'IPv6 Address: {ipv6}',
                f'Type: {kind}',
                f'Status: {status}',
                f'Failover: {failover_status}',
                '',
            ))
        return '\n'.join(lines).rstrip()

    @staticmethod
    def _ping_host(host: str) -> str:
        try:
            result = subprocess.run(
                ['/usr/bin/ping', '-c', '5',
                 '-i', '0.1', '-W', '5', host],
                check=False, capture_output=True, text=True, timeout=30,
            )
            output = (result.stdout or result.stderr).strip()
            return '\n'.join(line.strip() for line in output.splitlines()) if output else 'FAILED, no output'
        except (OSError, socket.gaierror, subprocess.SubprocessError):
            return 'FAILED, loss=100%'

    def _handle_system_info(self, if_num, number, audit):
        response = self._system_info()
        audit(response.replace('\n', ' | '))
        self._send_response(if_num, number, response)

    def _handle_interface_addresses(self, if_name, if_num, number, audit):
        response = self._interface_addresses(if_name)
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
            audit('SUCCESS')
            self._send_response(if_num, number, 'SUCCESS')
        else:
            audit('FAILED')
            self._send_response(if_num, number, 'FAILED')

    def _handle_cell_reboot(self, if_num, number, audit):
        """Issue the Telit module-reboot command to the receiving modem."""
        try:
            result = subprocess.run(
                ['/usr/bin/mmcli', '--modem', str(if_num),
                 '--command=AT#REBOOT'],
                check=False, capture_output=True, text=True, timeout=10,
            )
            succeeded = result.returncode == 0
            if not succeeded:
                logger.error('AT#REBOOT failed for modem %s: %s',
                             if_num, (result.stderr or result.stdout).strip())
            else:
                # AT#REBOOT drops the modem and causes it to re-enumerate.
                # Wait for that lifecycle instead of guessing with a fixed
                # delay, then use the manager's existing FSM path.
                self._wait_for_modem_reenumeration(if_num)
                self.client.reinitialize_modem(if_num)
                succeeded = self.client.wait_for_bearer(
                    if_num, target='connected', timeout=180, poll_interval=2
                )
                if not succeeded:
                    logger.error('Cellular reconnection timed out for modem %s', if_num)
        except (OSError, subprocess.SubprocessError, WWANError) as err:
            logger.error('AT#REBOOT failed for modem %s: %s', if_num, err)
            succeeded = False
        response = 'SUCCESS' if succeeded else 'FAILED'
        audit(response)
        self._send_response(if_num, number, response)

    @staticmethod
    def _wait_for_modem_reenumeration(if_num, timeout=90):
        """Wait for a rebooted modem to disappear and return on reappearance."""
        deadline = time.monotonic() + timeout
        disappeared = False
        while time.monotonic() < deadline:
            result = subprocess.run(
                ['/usr/bin/mmcli', '--modem', str(if_num)],
                check=False, capture_output=True, text=True, timeout=5,
            )
            available = result.returncode == 0
            if not available:
                disappeared = True
                logger.info('Modem %s disappeared after AT#REBOOT', if_num)
            elif disappeared:
                logger.info('Modem %s reappeared; starting FSM reinitialization', if_num)
                return
            time.sleep(2)
        if not disappeared:
            logger.warning('Modem %s did not disappear; continuing with FSM reinitialization',
                           if_num)
        else:
            logger.warning('Modem %s did not reappear within %ss; continuing with FSM reinitialization',
                           if_num, timeout)

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
                logger.warning(
                    'Cellular %s failed on interface %s (exit status %s): %s',
                    action.lower(), if_num, result.returncode,
                    output or 'no output',
                )
                response = 'FAILED'
            else:
                target = 'connected' if action == 'CONNECT' else 'disconnected'
                verified = self._wait_bearer_status(if_num, target)
                status = self.client.get_bearer_status(if_num)
                logger.info('Cellular %s interface=%s bearer_status=%s verified=%s',
                            action.lower(), if_num, status, verified)
                response = 'SUCCESS' if verified else 'FAILED'
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
                response = 'SUCCESS' if verified else 'FAILED'
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
        cell_reboot_match = CELL_REBOOT_COMMAND_RE.fullmatch(text)
        # Show the received message for audit purposes, while masking the
        # leading password used by REBOOT.
        command = (re.sub(r'^(\s*)\S+(?=\s+(?:REBOOT|CELL\s+REBOOT)\s*$)',
                          r'\1[PASSWORD]', text, flags=re.IGNORECASE)
                   .strip())
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
            return

        expected_password = self.cfg.authorized_numbers.get(if_name, {}).get(number)
        configured_passwords = set(
            self.cfg.authorized_numbers.get(if_name, {}).values())
        sender_whitelisted = expected_password is not None
        password_authenticated = sender_whitelisted

        if not sender_whitelisted and (match or cell_reboot_match):
            password_authenticated = (
                match.group(1) if match else cell_reboot_match.group(1)
            ) in configured_passwords

        # A password is optional for informational/control commands. If an
        # authorized sender includes it anyway, accept and remove the prefix.
        # REBOOT keeps its dedicated password-required syntax below.
        if not match and not cell_reboot_match:
            optional_password = re.match(r'^\s*(\S+)\s+(.+?)\s*$', text)
            if optional_password:
                supplied_password = optional_password.group(1)
                valid_password = (sender_whitelisted
                                  and hmac.compare_digest(supplied_password, expected_password))
                if not sender_whitelisted:
                    valid_password = supplied_password in configured_passwords
                if valid_password:
                    text = optional_password.group(2)
                    command = text
                    password_authenticated = True

        # All commands require a configured password for non-whitelisted
        # senders. Whitelisted senders may omit it for non-disruptive commands.
        if not password_authenticated:
            audit('UNAUTHORIZED')
            self._send_response(if_num, number, 'UNAUTHORIZED')
            return

        if cell_reboot_match:
            if not hmac.compare_digest(cell_reboot_match.group(1), expected_password):
                audit('UNAUTHORIZED')
                self._send_response(if_num, number, 'UNAUTHORIZED')
                return
            self._handle_cell_reboot(if_num, number, audit)
            return

        if SHOW_SYSTEM_INFO_COMMAND_RE.fullmatch(text):
            self._handle_system_info(if_num, number, audit)
            return

        if SHOW_WAN_STATUS_COMMAND_RE.fullmatch(text):
            self._handle_interface_addresses(if_name, if_num, number, audit)
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
            result = 'INVALID'
            audit(result)
            self._send_response(if_num, number, 'INVALID')
            return

        if not hmac.compare_digest(match.group(1), expected_password):
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
