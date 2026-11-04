#!/usr/bin/env python3
# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the shared notify-only cellular firmware check every 12 hours."""

import subprocess
import time

CHECK_INTERVAL = 12 * 60 * 60
CHECK_TIMEOUT = 180
FIRMWARE_HANDLER = '/usr/libexec/vyos/op_mode/wwan_firmware.py'
PYTHON = '/usr/bin/python3'


def main():
    while True:
        try:
            subprocess.run(
                [PYTHON, FIRMWARE_HANDLER, 'execute_check_firmware', '--notify'], check=False, timeout=CHECK_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            print('Cellular firmware check timed out; retrying next interval.', flush=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    main()
