#!/usr/bin/env python3
# Copyright (C) 2026 Perle Systems Limited
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

"""Operator-driven airplane mode (op-mode).

CLI:

    run change wwan wwan0 airplane-mode enable    # disconnect + RF off, park
    run change wwan wwan0 airplane-mode disable   # RF on + reconnect

Airplane mode is a RUNTIME action and is deliberately NOT written to the
configuration: a reboot always comes up in normal operation.  This avoids
bricking a remote unit whose only management path is the cellular link — a
power cycle is always a recovery path.

ENABLE drops the bearer, tears down downstream LAN features and powers the
modem RF off (SetPowerState LOW); the FSM then parks and ignores modem/SIM
events until released.  DISABLE powers RF back on and restarts the connection
from scratch.
"""

import sys
from typing import Literal

import vyos.opmode

from vyos.configquery import ConfigTreeQuery


def _get_interface_number(interface: str) -> int:
    """Extract numeric index from interface name (e.g. 'wwan0' -> 0)."""
    return int(interface.replace('wwan', ''))


def _get_client():
    """Return a WWANClientSync instance."""
    from vyos.utils.wwan.wwan_client import WWANClientSync
    return WWANClientSync()


def _check_interface(interface: str):
    """Verify the interface is configured."""
    config = ConfigTreeQuery()
    if not config.exists(['interfaces', 'wwan', interface]):
        raise vyos.opmode.UnconfiguredSubsystem(
            f'Interface "{interface}" is not configured'
        )


def _set_airplane_mode(interface: str, enabled: bool) -> str:
    _check_interface(interface)
    if_num = _get_interface_number(interface)
    try:
        client = _get_client()
        return client.set_airplane_mode(if_num, enabled)
    except Exception as e:
        raise vyos.opmode.DataUnavailable(
            f'Cannot change airplane mode on {interface}: {e}'
        )


# ── Public op-mode entry points ─────────────────────────────────────────

def set_airplane_mode(interface: str, state: Literal['enable', 'disable']):
    """Toggle airplane mode (RF off / RF on) on a WWAN interface.

    vyos.opmode.run() only registers functions whose names start with an
    approved verb (show|clear|reset|restart|add|update|delete|generate|set|
    renew|release|execute|import|mtr), so the enable/disable CLI nodes both
    invoke this single "set"-verb entry point selected by --state.

    CLI: change wwan <wwan0> airplane-mode <enable|disable>
    """
    enabled = state == 'enable'
    _set_airplane_mode(interface, enabled)
    if enabled:
        print(f'Airplane mode ENABLED on {interface} — bearer dropped and RF '
              f'powered off.')
        print('Not persistent: a reboot returns to normal operation. Run '
              f'"change wwan {interface} airplane-mode disable" to reconnect.')
    else:
        print(f'Airplane mode DISABLED on {interface} — RF powered on, '
              f'reconnecting from scratch.')


if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
