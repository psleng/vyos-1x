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

"""Re-acquire DHCP on carrier-up for opted-in DHCP-client interfaces.

ISC dhclient (as run by VyOS via dhclient@<if>.service) does not re-DISCOVER
when an interface's carrier returns -- it keeps its lease and waits for its own
renewal timer. VyOS reacts to *admin*-state changes but has no *carrier*-state
re-acquire. That is a problem downstream of a DOCSIS-style WWAN ip-passthrough,
where the upstream signals a carrier IP change by briefly bouncing the LAN port
(see interfaces_wwan_passthrough.py `reacquire-bounce`): the downstream sees a
carrier-only blip (admin stays up) and must grab the new address at once, not
at T1.

This daemon watches netlink link events and, on a NO-CARRIER -> carrier
transition for an interface that opted in via
`interfaces ... dhcp-options reacquire-on-carrier` (marker file written by
vyos.ifconfig.interface.set_dhcp), restarts dhclient@<if> so it re-acquires
immediately. The restart is debounced per interface so a flapping carrier
cannot storm the client.

The opt-in is enforced by the marker file: this is a cheap always-on netlink
listener that does nothing unless a marked interface's carrier comes up.
"""

import logging
import os
import subprocess
import sys
import time

from vyos.defaults import directories

# Linux netdev flags (linux/if.h).
IFF_UP = 0x1
IFF_LOWER_UP = 0x10000

# Minimum seconds between dhclient restarts for the same interface, so a
# flapping carrier cannot storm the client.
REACQUIRE_MIN_INTERVAL_S = 15

logger = logging.getLogger('vyos-dhcp-carrier-reacquire')

_dhclient_dir = directories['isc_dhclient_dir']


def marker_path(ifname):
    """Path of the opt-in marker written by interface.set_dhcp()."""
    return os.path.join(_dhclient_dir, f'dhclient_{ifname}.reacquire')


def opted_in(ifname):
    return os.path.exists(marker_path(ifname))


def dhclient_active(ifname):
    return subprocess.run(
        ['systemctl', 'is-active', '--quiet', f'dhclient@{ifname}.service'],
        check=False,
    ).returncode == 0


def restart_dhclient(ifname):
    logger.info('carrier up on %s with reacquire-on-carrier set; restarting '
                'dhclient@%s to re-acquire', ifname, ifname)
    subprocess.run(['systemctl', 'restart', f'dhclient@{ifname}.service'],
                   check=False)


def should_reacquire(prev_carrier, lower_up, admin_up, is_opted_in,
                     now, last_ts, min_interval=REACQUIRE_MIN_INTERVAL_S):
    """Pure decision helper (unit-testable).

    Fire only on a genuine NO-CARRIER -> carrier transition of an admin-up,
    opted-in interface, and not more often than min_interval per interface.
    """
    if not (lower_up and not prev_carrier and admin_up and is_opted_in):
        return False
    if now - last_ts < min_interval:
        return False
    return True


def _read_sysfs_carrier(ifname):
    """Seed carrier state from sysfs (avoids interleaving a netlink dump with
    the monitor socket). Returns False when carrier is unreadable (e.g. admin
    down), which is the correct 'no carrier' seed."""
    try:
        with open(f'/sys/class/net/{ifname}/carrier') as f:
            return f.read().strip() == '1'
    except OSError:
        return False


def main():
    logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s',
                        stream=sys.stderr)

    from pyroute2 import IPRoute

    carrier = {
        name: _read_sysfs_carrier(name)
        for name in os.listdir('/sys/class/net')
    }
    last_reacquire = {}

    with IPRoute() as ipr:
        ipr.bind()  # subscribe to RTNL multicast groups (incl. link events)
        logger.info('started; watching carrier transitions for '
                    'reacquire-on-carrier interfaces')
        while True:
            for msg in ipr.get():
                if msg.get('event') != 'RTM_NEWLINK':
                    continue
                name = msg.get_attr('IFLA_IFNAME')
                if not name:
                    continue
                flags = msg['flags']
                lower_up = bool(flags & IFF_LOWER_UP)
                admin_up = bool(flags & IFF_UP)
                prev = carrier.get(name, lower_up)
                carrier[name] = lower_up

                now = time.monotonic()
                if not should_reacquire(prev, lower_up, admin_up,
                                        opted_in(name), now,
                                        last_reacquire.get(name, 0.0)):
                    continue
                if not dhclient_active(name):
                    continue
                last_reacquire[name] = now
                restart_dhclient(name)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
