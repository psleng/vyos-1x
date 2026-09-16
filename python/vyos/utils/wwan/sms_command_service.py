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
- IGOS_SMS_COMMAND_POLL_INTERVAL: polling interval in seconds (default: 5)
- IGOS_SMS_COMMAND_RESPONSE_ENABLED: 1/true/yes to send
    ACCEPTED/REJECTED SMS responses (default: true)

Accepted command format:

    123456 REBOOT

Matching is case-sensitive (capital letters only).
"""

from __future__ import annotations

import logging
from logging.handlers import SysLogHandler
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List

from vyos.utils.wwan.wwan_client import WWANClientSync, WWANError


logger = logging.getLogger('vyos.wwan.sms_command_service')


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

        poll_interval = float(os.environ.get('IGOS_SMS_COMMAND_POLL_INTERVAL', '1'))
        response_enabled = _as_bool(
            os.environ.get('IGOS_SMS_COMMAND_RESPONSE_ENABLED'),
            True,
        )

        return cls(
            interfaces=interfaces,
            authorized_numbers=authorized_numbers,
            poll_interval=max(1.0, poll_interval),
            response_enabled=response_enabled,
        )


class SmsCommandService:
    CMD_RE = re.compile(r'\s*([0-9]{6})\s+REBOOT\s*')
    INFO_RE = re.compile(r'\s*SHOW\s+SYSTEM\s+INFO\s*')

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
                ['/usr/bin/systemctl', 'reboot'],
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
        def output(command, fallback='N/A'):
            try:
                return subprocess.check_output(command, text=True, timeout=3).strip() or fallback
            except (OSError, subprocess.SubprocessError):
                return fallback

        version = 'N/A'
        try:
            with open('/opt/vyatta/etc/version', encoding='utf-8') as version_file:
                version = version_file.read().strip()
        except OSError:
            pass
        timezone_name = output(['timedatectl', 'show', '-p', 'Timezone', '--value'])
        info = (
            f'Hostname: {socket.gethostname()}\n'
            f'Version: {version}\n'
            f'System time: {time.strftime("%Y-%m-%d %H:%M:%S %Z")}\n'
            f'Timezone: {timezone_name}\n'
            f'Uptime: {output(["uptime", "-p"])}'
        )
        return info[:160]

    def _process_message(self, if_name: str, if_num: int, msg: dict):
        msg_id = int(msg.get('id', -1))
        number = str(msg.get('number', '')).strip()

        text = str(msg.get('text', ''))
        match = self.CMD_RE.fullmatch(text)
        # Show the received message for audit purposes, while masking the
        # leading PIN (including malformed PIN attempts).
        command = re.sub(r'^(\s*)[0-9]+(?=\s+)', r'\1[PIN]', text).strip()
        if not command:
            command = '[EMPTY]'

        def audit(result):
            logger.warning(
                'SMS command timestamp=%s sender=%r command=%s result=%s interface=%s id=%s',
                datetime.now(timezone.utc).isoformat(), number, repr(command),
                result, if_name, msg_id,
            )

        expected_pin = self.cfg.authorized_numbers.get(if_name, {}).get(number)
        if expected_pin is None:
            audit('UNAUTHORIZED')
            self._send_response(if_num, number, 'UNAUTHORIZED')
            return

        if self.INFO_RE.fullmatch(text):
            response = self._system_info()
            # Keep the complete reply in one syslog line for easy filtering.
            audit(response.replace('\n', ' | '))
            self._send_response(if_num, number, response)
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

        if self._execute_reboot():
            audit('OK')
            self._send_response(if_num, number, 'OK')
        else:
            audit('REBOOT FAILED')
            self._send_response(if_num, number, 'REBOOT FAILED')

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

        logger.warning(
            'Starting WWAN SMS command service for interfaces=%s response_enabled=%s',
            ','.join(self.cfg.interfaces),
            self.cfg.response_enabled,
        )

        while self._running:
            for if_name in self.cfg.interfaces:
                self._poll_interface(if_name)
            time.sleep(self.cfg.poll_interval)

        logger.warning('WWAN SMS command service stopped')
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
