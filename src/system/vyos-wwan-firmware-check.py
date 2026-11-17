#!/usr/bin/env python3
# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the shared notify-only cellular firmware check every 12 hours.

Each run records its result in the cellular firmware status file
(vyos.utils.update_status). After a failed run -- typically at boot, before
the modem or uplink is up -- retry with backoff instead of waiting 12 hours.
"""

import subprocess
import time

from vyos.utils.update_status import CELLULAR_FIRMWARE_STATUS
from vyos.utils.update_status import CHECK_INTERVAL
from vyos.utils.update_status import next_retry
from vyos.utils.update_status import read_last_status

CHECK_TIMEOUT = 180
FIRMWARE_HANDLER = '/usr/libexec/vyos/op_mode/wwan_firmware.py'
PYTHON = '/usr/bin/python3'


def run_check() -> bool:
    """Run one check; True if it recorded a successful result."""
    started = time.time()
    try:
        subprocess.run(
            [PYTHON, FIRMWARE_HANDLER, 'execute_check_firmware', '--notify'],
            check=False,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print('Cellular firmware check timed out.', flush=True)
        return False
    record = read_last_status(CELLULAR_FIRMWARE_STATUS) or {}
    return record.get('checked_at', 0) >= started and not record.get('error')


def main():
    retry = None
    while True:
        if run_check():
            retry = None
            time.sleep(CHECK_INTERVAL)
        else:
            retry = next_retry(retry)
            print(
                f'Cellular firmware check incomplete; retrying in {retry // 60} min.',
                flush=True,
            )
            time.sleep(retry)


if __name__ == '__main__':
    main()
