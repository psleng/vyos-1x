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

"""Operator-driven SIM PIN management (op-mode).

CLI (run from configure mode so the result is written back to the config):

    run change wwan wwan0 sim pin new '<4-8 digits>'   # create or change
    run change wwan wwan0 sim pin remove               # disable the PIN lock

The action targets the ACTIVE, REGISTERED SIM only.  Because a PIN-locked SIM
only reaches the registered state after the service unlocked it with the
configured PIN, the configured PIN is provably correct — so change/remove
cannot burn SIM retries.  On success the new value is written to (or removed
from) the running config for the active slot.

Single-active-SIM is an operational precondition: insert only the SIM you want
to modify in the active slot (or disable the other slot with
`set interfaces wwan wwanN sim slot M disable` and commit), then wait for the
modem to register before running the command.
"""

import os
import sys

import vyos.opmode

from vyos.config import Config
from vyos.configquery import ConfigTreeQuery
from vyos.defaults import base_dir
from vyos.utils.misc import install_into_config
from vyos.utils.process import cmd


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


def _delete_from_config(path: str):
    """Delete a single config node from an active config session.

    Mirrors vyos.utils.misc.install_into_config, which only knows how to *set*
    nodes.  If not in a config session, print the manual command instead.
    """
    if not Config().in_session():
        print('You are not in configure mode, command to remove manually from '
              'configure mode:')
        print(f'delete {path}')
        return
    env = os.environ.copy()
    env['vyos_libexec_dir'] = base_dir
    env['vyos_validators_dir'] = f'{base_dir}/validators'
    try:
        cmd(f'/opt/vyatta/sbin/my_delete {path}', env=env)
    except Exception:
        print('Failed to remove value. Command to remove manually:')
        print(f'delete {path}')


# ── Public op-mode entry points ─────────────────────────────────────────

def change_pin(raw: bool, interface: str, pin: str):
    """Create or change the SIM PIN on the active, registered SIM.

    CLI: change wwan <wwan0> sim pin new <pin>
    """
    _check_interface(interface)
    if_num = _get_interface_number(interface)
    try:
        client = _get_client()
        result = client.change_sim_pin(if_num, pin)
    except Exception as e:
        raise vyos.opmode.DataUnavailable(
            f'Cannot change SIM PIN on {interface}: {e}'
        )

    action = result.get('action', '')
    slot = result.get('slot', '')

    # Persist the new PIN to the active slot's config (unless it was a no-op).
    if action in ('created', 'changed'):
        install_into_config(
            Config(),
            [f"interfaces wwan {interface} sim slot {slot} pin '{pin}'"],
            override_prompt=False,
        )

    if raw:
        return result
    if action == 'created':
        return f'SIM PIN enabled on {interface} slot {slot}.'
    if action == 'changed':
        return f'SIM PIN changed on {interface} slot {slot}.'
    if action == 'unchanged':
        return f'SIM PIN on {interface} slot {slot} already matches — no change.'
    return f'SIM PIN operation on {interface} slot {slot}: {action}.'


def remove_pin(raw: bool, interface: str):
    """Disable the SIM PIN lock on the active, registered SIM.

    CLI: change wwan <wwan0> sim pin remove
    """
    _check_interface(interface)
    if_num = _get_interface_number(interface)
    try:
        client = _get_client()
        result = client.remove_sim_pin(if_num)
    except Exception as e:
        raise vyos.opmode.DataUnavailable(
            f'Cannot remove SIM PIN on {interface}: {e}'
        )

    slot = result.get('slot', '')
    # Ensure the config no longer carries a PIN for the active slot.
    _delete_from_config(f"interfaces wwan {interface} sim slot {slot} pin")

    if raw:
        return result
    action = result.get('action', '')
    if action == 'already-disabled':
        return (f'SIM PIN lock on {interface} slot {slot} was already disabled; '
                'configuration cleared.')
    return f'SIM PIN lock disabled on {interface} slot {slot}.'


if __name__ == '__main__':
    try:
        res = vyos.opmode.run(sys.modules[__name__])
        if res:
            print(res)
    except (ValueError, vyos.opmode.Error) as e:
        print(e)
        sys.exit(1)
