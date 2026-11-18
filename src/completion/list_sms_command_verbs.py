#!/usr/bin/env python3
# Copyright (C) 2026 Perle Systems Limited
# SPDX-License-Identifier: GPL-2.0-or-later
from vyos.utils.wwan.sms_command import load_catalog

if __name__ == '__main__':
    try:
        print(
            ' '.join(v for v, e in load_catalog().items() if e['tier'] == 'privileged')
        )
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(1)
