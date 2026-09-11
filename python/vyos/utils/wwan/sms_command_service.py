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
- IGOS_SMS_COMMAND_ALLOWED_SENDERS: comma-separated list of E.164 numbers
  (required)
- IGOS_SMS_COMMAND_POLL_INTERVAL: polling interval in seconds (default: 5)
- IGOS_SMS_COMMAND_RESPONSE_ENABLED: 1/true/yes to send
    ACCEPTED/REJECTED SMS responses (default: true)

Accepted command format:

    REBOOT

Matching is case-sensitive (capital letters only).
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
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
    allowed_senders: List[str]
    poll_interval: float
    response_enabled: bool

    @classmethod
    def from_env(cls) -> 'ServiceConfig':
        interfaces = _parse_interfaces(
            os.environ.get('IGOS_SMS_COMMAND_INTERFACES', 'wwan0')
        )

        senders_raw = os.environ.get('IGOS_SMS_COMMAND_ALLOWED_SENDERS', '')
        allowed_senders = [s.strip() for s in senders_raw.split(',') if s.strip()]

        poll_interval = float(os.environ.get('IGOS_SMS_COMMAND_POLL_INTERVAL', '1'))
        response_enabled = _as_bool(
            os.environ.get('IGOS_SMS_COMMAND_RESPONSE_ENABLED'),
            True,
        )

        return cls(
            interfaces=interfaces,
            allowed_senders=allowed_senders,
            poll_interval=max(1.0, poll_interval),
            response_enabled=response_enabled,
        )


class SmsCommandService:
    CMD_RE = re.compile(r'^\s*REBOOT\s*$')

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

    def _execute_reboot(self):
        subprocess.Popen(['/usr/bin/systemctl', 'reboot'])

    def _process_message(self, if_name: str, if_num: int, msg: dict):
        msg_id = int(msg.get('id', -1))
        number = str(msg.get('number', '')).strip()

        if number not in self.cfg.allowed_senders:
            logger.warning(
                'Command rejected: unauthorized number (id=%s interface=%s number=%s)',
                msg_id,
                if_name,
                number,
            )
            self._send_response(if_num, number, 'ERROR: UNAUTHORIZED')
            return

        text = str(msg.get('text', ''))
        match = self.CMD_RE.fullmatch(text)
        if not match:
            logger.warning(
                'Command rejected: invalid format (id=%s interface=%s)',
                msg_id,
                if_name,
            )
            self._send_response(if_num, number, 'ERROR: INVALID FORMAT')
            return

        logger.warning(
            'Command accepted: reboot (id=%s interface=%s number=%s)',
            msg_id,
            if_name,
            number,
        )
        self._send_response(if_num, number, 'OK: REBOOT')
        self._execute_reboot()

    def _poll_interface(self, if_name: str):
        if_num = _if_number(if_name)
        seen = self._seen.setdefault(if_num, set())

        try:
            messages = self.client.list_sms(if_num)
        except WWANError as err:
            logger.debug('SMS poll skipped for %s: %s', if_name, err)
            return

        for msg in messages:
            if msg.get('direction') != 'incoming':
                continue
            msg_id = int(msg.get('id', -1))
            if msg_id <= 0:
                continue
            if msg.get('read', False):
                seen.add(msg_id)
                continue
            if msg_id in seen:
                continue

            try:
                full_msg = self.client.read_sms(if_num, msg_id)
            except Exception as err:
                logger.warning('Failed to read SMS id=%s on %s: %s', msg_id, if_name, err)
                continue

            seen.add(msg_id)
            self._process_message(if_name, if_num, full_msg)

    def run(self):
        if not self.cfg.allowed_senders:
            logger.error('No allowed senders configured; set IGOS_SMS_COMMAND_ALLOWED_SENDERS')
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
        format='%(asctime)s igos-wwan-sms-command[%(process)d]: %(levelname)s: %(message)s',
    )


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
