#!/usr/bin/env python3
#
# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
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

import argparse
import atexit
import json
import signal
import time

from collections import namedtuple
from vyos.template import get_dhcp_router
from vyos.utils.process import rc_cmd
from vyos.utils.process import run
from pathlib import Path
from systemd import journal
from vyos.utils.wwan.wwan_client import (  # noqa: E402
    WWANClientSync,
)
from vyos.config import Config


def _interface_modem_check(iface):
    """Health check for a dial-on-demand WWAN interface.

    Mirrors the WAN load-balancer spoof (vyos-load-balancer.py): a dial-on-demand
    bearer is intentionally DOWN whenever it is idle, so testing bearer status
    would mark the interface permanently unhealthy -- failover would then never
    select it as a backup nexthop and the on-demand trigger would never get the
    traffic it needs to dial. Instead treat the interface as healthy when the
    FSM reports the link is on the network (registered-and-idle, connecting, or
    already connected).

    The FSM owns the interface, so its published ``fsm_state`` is the sole
    source of truth here -- we deliberately do NOT consult the
    ModemManager-derived ``modem_state`` field, to avoid coupling failover health
    to a second, lower-level signal the FSM already subsumes.

    NB: this runs on every failover poll from the long-lived daemon loop, so the
    client MUST be closed each time. A leaked WWANClientSync keeps its D-Bus
    MessageBus connection open; accumulating one per poll eventually exhausts
    dbus-daemon's max_connections_per_user (default 256), after which the IGOS
    daemon is no longer reachable (ServiceUnknown).
    """
    status = None
    try:
        client = WWANClientSync()
        ifnum = int(iface[4:])
        try:
            # add_interface is idempotent and just ensures the service has the
            # interface object so get_status() can resolve it. No
            # set_configuration here: re-applying the config every poll makes the
            # FSM fire a RECONFIGURE that bounces a connected bearer.
            client.add_interface(ifnum)
            status = client.get_status(ifnum)
        finally:
            client.close()
    except Exception as exc:
        print_debug(f'    [ MODEM_CHECK ] health check failed for {iface}: {exc}')
        return False

    # The FSM is the authoritative owner of the interface; a dial-on-demand
    # link is usable as a backup next-hop whenever the FSM reports it on the
    # network. REGISTERED_IDLE (parked, ready to dial), CONNECTING (bringing the
    # bearer up) and CONNECTED all qualify.
    if status is not None:
        fsm_state = status.get('fsm_state', 'DISCONNECTED')
        if fsm_state in ('CONNECTED', 'REGISTERED_IDLE', 'CONNECTING'):
            return True

    return False


my_name = Path(__file__).stem

# Timeout between configuration reading
# When no checks timeouts worked (e.g. no config files)
config_timeout = 1

# Useful debug info to console, use debug = True
# sudo systemctl stop vyos-failover.service
# sudo /usr/libexec/vyos/vyos-failover.py --config /run/vyos-failover.conf
debug = False
debug_output_journal = False
debug_output_print = True

def _wwan_connection_mode(ifname):
    """Return the effective connection-mode of a WWAN interface, or None for a
    non-WWAN interface / unset mode."""
    if not ifname.startswith('wwan'):
        return None
    return Config().return_effective_value(
        ['interfaces', 'wwan', ifname, 'connection-mode'])


def on_demand_check(ifname):
    """True for a WWAN in either on-demand mode (connect-on-demand or
    dial-on-demand).

    Both modes park the bearer idle, so failover must use the
    modem-registration spoof (:func:`_interface_modem_check`) to keep the link
    selectable as a backup next-hop while idle; a normal IP probe would mark it
    permanently down.  How the bearer is then raised differs by mode:
    dial-on-demand relies on its own traffic trigger once the route is installed
    via it, while connect-on-demand is raised deterministically by the failover
    bearer lease (:func:`reconcile_failover_leases`).
    """
    return _wwan_connection_mode(ifname) in ('connect-on-demand',
                                             'dial-on-demand')


def connect_on_demand_check(ifname):
    """True only for a connect-on-demand WWAN -- the mode that needs the
    failover bearer lease (it parks fully link-down with no route, so nothing on
    the data path can bootstrap it; dial-on-demand uses its traffic trigger
    instead)."""
    return _wwan_connection_mode(ifname) == 'connect-on-demand'


def dial_on_demand_check(ifname):
    """
    Return True if ifname is a WWAN interface configured for dial-on-demand.

    Only wwanX interfaces with dial-on-demand use the modem bearer check; every
    other interface (ethX, ...) uses the normal IP health check.
    """
    return _wwan_connection_mode(ifname) == 'dial-on-demand'


# ── connect-on-demand failover bearer lease ─────────────────────────────────
# A connect-on-demand WWAN parks fully link-down with no route, so when it
# becomes the active (lowest-metric healthy) failover next-hop nothing on the
# data path can bootstrap it. Hold a TTL-bounded bearer lease on the modem FSM
# so it raises the bearer deterministically for the whole outage, and release it
# (after a hold-down) when a higher-priority path recovers. dial-on-demand WWANs
# are NOT leased -- their own traffic trigger dials once the route is installed
# via them.
failover_lease_owner = 'failover'
failover_lease_ttl = 120        # spans several main-loop iterations
failover_release_holddown = 30  # keep the metered bearer up briefly on failback


def _bearer_lease(iface, acquire, ttl=None, reason=''):
    """Acquire or release the connect-on-demand failover bearer lease for iface.

    Uses a short-lived WWANClientSync (create-use-close) to avoid leaking D-Bus
    connections across the long-lived daemon loop -- the same discipline as
    _interface_modem_check. The lease lives in the modem FSM (TTL-bounded), so it
    persists after the client closes.
    """
    ifnum = int(iface[len('wwan'):])
    client = WWANClientSync()
    try:
        client.add_interface(ifnum)
        if acquire:
            client.bearer_lease_acquire(ifnum, failover_lease_owner, ttl=ttl,
                                        reason=reason)
        else:
            client.bearer_lease_release(ifnum, failover_lease_owner)
    finally:
        client.close()


def active_connect_on_demand_wwans(nexthops, alive_by_iface):
    """Return (managed, active) sets of connect-on-demand WWAN interfaces for one
    route's next-hops.

    * managed -- every connect-on-demand WWAN next-hop (lease-managed).
    * active  -- the connect-on-demand WWAN that is the lowest-metric ALIVE
      next-hop (all higher-priority next-hops are down), so its bearer must be
      raised. This mirrors the kernel's metric-based selection that the installed
      routes produce, so the lease tracks exactly the link failover steers to.
    """
    managed = set()
    for nhc in nexthops:
        iface = nhc.conf_iface
        if iface and iface.startswith('wwan') and connect_on_demand_check(iface):
            managed.add(iface)
    active = set()
    alive = [nhc for nhc in nexthops if alive_by_iface.get(nhc.conf_iface)]
    if alive:
        winner = min(alive, key=lambda n: int(n.conf_metric))
        if winner.conf_iface in managed:
            active.add(winner.conf_iface)
    return managed, active


def reconcile_failover_leases(all_routes, alive_by_iface, hold_state, now=None):
    """Hold a bearer lease on each connect-on-demand WWAN failover is failing
    over to, releasing it (after a hold-down) when a better path recovers.

    ``hold_state`` (iface -> last time it was the active egress) and ``now`` are
    injectable for testing; the daemon passes a persistent dict.
    """
    if now is None:
        now = time.time()
    managed = set()
    active = set()
    for route_config in all_routes:
        m, a = active_connect_on_demand_wwans(route_config.nexthops,
                                              alive_by_iface)
        managed |= m
        active |= a
    for iface in managed:
        try:
            if iface in active:
                hold_state[iface] = now
                _bearer_lease(iface, acquire=True, ttl=failover_lease_ttl,
                              reason='active failover target')
            else:
                last_active = hold_state.get(iface)
                if (last_active is not None
                        and now - last_active < failover_release_holddown):
                    # Better path recovered but not yet stable: keep the bearer
                    # up through the hold-down so a flapping link does not drop
                    # the metered cellular connection.
                    _bearer_lease(iface, acquire=True, ttl=failover_lease_ttl,
                                  reason='failover release hold-down')
                else:
                    hold_state.pop(iface, None)
                    _bearer_lease(iface, acquire=False)
        except Exception as exc:
            # A transient D-Bus/modem hiccup must not kill the daemon; the lease
            # TTL makes a missed renewal self-correcting.
            print_debug(f'    [ LEASE ] reconcile failed for {iface}: {exc}')


def print_debug(*args, **kwargs):
    if debug:
        if debug_output_print:
            print(*args, **kwargs)
        if debug_output_journal:
            journal.send(*args, **kwargs, SYSLOG_IDENTIFIER=my_name)


def wrap_vrf(command, vrf):
    if not vrf:
        return command
    return f"sudo ip vrf exec {vrf} {command}"


def is_route_exists(ip_args):
    """Check if route with expected gateway, dev and metric exists"""
    rc, data = rc_cmd(f'ip --json route show {ip_args}')
    if rc == 0:
        data = json.loads(data)
        if len(data) > 0:
            return True
    return False


def get_iface_addresses(iface):
    """Return the global-scope IPv4/IPv6 addresses currently on `iface`."""
    rc, out = rc_cmd(f'ip --json addr show dev {iface}')
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return []
    addrs = []
    for link in data:
        for a in link.get('addr_info', []):
            ip = a.get('local')
            if ip and a.get('scope') == 'global':
                addrs.append(ip)
    return addrs


def flush_conntrack_for_addrs(addrs, vacated_iface=''):
    """Flush conntrack entries for masqueraded flows pinned to these addresses.

    On a failover the active egress changes, but conntrack keeps replaying the
    source-NAT translation captured when each flow was created -- so an
    established downstream (LAN) flow keeps egressing with the *old* interface's
    address and is dropped (the carrier/return path no longer owns it). The
    router's own traffic re-sources naturally, which is why only forwarded/
    masqueraded flows stick. Deleting the stale entries (reply-dst = the vacated
    interface's address) forces them to re-establish and re-NAT via the new
    egress.

    Keyed on the vacated interface's address rather than its oif (conntrack
    cannot filter deletes by outgoing interface), using the address the
    interface held while it was active -- so it still works when the interface
    kept its address (the common passthrough-upstream-dead case)."""
    for addr in addrs:
        fam = 'ipv6' if ':' in addr else 'ipv4'
        # Absolute path: the daemon runs under systemd's restricted PATH, which
        # omits /usr/sbin (where conntrack-tools installs conntrack), so a bare
        # `conntrack` would not be found -- matching the /usr/bin/ping and
        # /usr/bin/arping convention used by the health checks.
        rc, _ = rc_cmd(f'/usr/sbin/conntrack -D -f {fam} -q {addr}')
        print_debug(f'    [ CT-FLUSH ] conntrack -D -f {fam} -q {addr} '
                    f'(vacated {vacated_iface}) rc={rc}')
        journal.send(
            f'failover: flushed conntrack for {addr} after egress change '
            f'(vacated interface {vacated_iface})',
            SYSLOG_IDENTIFIER=my_name,
        )


def is_port_open(ip, port, vrf=''):
    """
    Check connection to remote host and port
    Return True if host alive

    % is_port_open('example.com', 8080)
    True
    """
    cmd = wrap_vrf(f"nc -w2 -z {ip} {port}", vrf)
    rc, data = rc_cmd(cmd)
    return rc == 0


def is_target_alive(
    targets=None,
    iface='',
    proto='icmp',
    port=None,
    nexthop_vrf='',
    policy='any-available',
    is_dhcp_interface=False,
) -> bool:
    """Check the availability of each target in the target_dict using
    the specified protocol ICMP, ARP, TCP

    Args:
        targets (tuple of TargetNamedTuple): A dict: keys are IP addresses to check, values - dicts with options.
            Possible keys (all optional): 'vrf' and 'interface'.
        iface (str): The name of the network interface to use for the check.
        proto (str): The protocol to use for the check. Options are 'icmp', 'arp', or 'tcp'.
        port (int): The port number to use for the TCP check. Only applicable if proto is 'tcp'.
        nexthop_vrf (str): Nexthop VRF name - if specific vrf is not given for target, use this one
        policy (str): The policy to use for the check. Options are 'any-available' or 'all-available'.

    Returns:
        bool: True if all targets are reachable according to the policy, False otherwise.

    Example:
        % is_target_alive(['192.0.2.1', '192.0.2.5'], 'eth1', proto='arp', policy='all-available')
        True
    """
    # Perle change start: save the iface name
    target_iface = iface
    # Perle change end

    if iface != '':
        iface = f'-I {iface}'

    num_reachable_targets = 0
    for options in targets:
        target = options.target

        # Perle change start
        print_debug(f'    [ CHECK-TARGET ]: target {target} on {target_iface}: is_dhcp_interface={is_dhcp_interface}')
        if target == '0.0.0.0' and is_dhcp_interface:
            cur_dhcpgw = get_dhcp_router(target_iface)
            if cur_dhcpgw:
                print_debug(f'    [ CHECK-TARGET ]: target {target} is replaced with actual DHCP gateway: {cur_dhcpgw}')
                target = cur_dhcpgw
            else:
                # set target to unreachable value
                print_debug(f'    [ CHECK-TARGET ]: target {target} no DHCP gateway found on interface {target_iface}, target=gateway-not-found')
                target = 'gateway-not-found'
        # Perle change end

        vrf = options.vrf if options.vrf else nexthop_vrf
        # don't use nexthop interface if 'vrf' is given
        iface_opt = iface if vrf == nexthop_vrf else ''
        # in any case if 'interface' is given, use it
        if options.interface:
            iface_opt = f'-I {options.interface}'

        if on_demand_check(target_iface):
            return _interface_modem_check(target_iface)

        match proto:
            case 'icmp':
                command = f'/usr/bin/ping -q {target} {iface_opt} -n -c 2 -W 1'
                command = wrap_vrf(command, vrf)
                rc, response = rc_cmd(command)
                print_debug(
                    f'    [ CHECK-TARGET ]: [{command}] -- return-code [RC: {rc}]'
                )
                if rc == 0:
                    num_reachable_targets += 1
                    if policy == 'any-available':
                        return True

            case 'arp':
                command = f'/usr/bin/arping -b -c 2 -f -w 1 -i 1 {iface_opt} {target}'
                command = wrap_vrf(command, vrf)
                rc, response = rc_cmd(command)
                print_debug(
                    f'    [ CHECK-TARGET ]: [{command}] -- return-code [RC: {rc}]'
                )
                if rc == 0:
                    num_reachable_targets += 1
                    if policy == 'any-available':
                        return True

            case _ if proto == 'tcp' and port is not None:
                if is_port_open(target, port, vrf=vrf):
                    num_reachable_targets += 1
                    if policy == 'any-available':
                        return True

            case _:
                return False

        if policy == 'all-available' and num_reachable_targets == len(targets):
            return True

    return False


TargetNamedTuple = namedtuple(
    'TargetConfig',
    [
        'target',
        'vrf',
        'interface',
    ],
)

NextHopNamedTuple = namedtuple(
    'NextHopConfig',
    [
        'route',
        'dhcp_interface',
        'next_hop',
        'vrf',
        'vrf_opt',
        'conf_iface',
        'conf_metric',
        'port',
        'port_opt',
        'policy',
        'proto',
        'targets',
        'pretty_targets',
        'timeout',
        'onlink',
        'failure_count',
        'success_count',
    ],
)


def get_nexthop_config_vars(
    destination, vrf, vrf_opt, nexthop_config, next_hop, dhcp_interface,
    device_interface=None
):
    port = nexthop_config.get('check').get('port')

    targets = tuple(
        TargetNamedTuple(
            target=target,
            vrf=target_config.get('vrf', None),
            interface=target_config.get('interface', None),
        )
        for target, target_config in nexthop_config.get('check').get('target').items()
    )

    # for print to journal and debug
    pretty_targets = []
    for target in targets:
        p = target.target
        options = []
        if target.vrf:
            options.append(f"vrf: {target.vrf}")
        if target.interface:
            options.append(f"interface: {target.interface}")
        if options:
            p += ' (' + ', '.join(options) + ')'
        pretty_targets.append(p)
    pretty_targets = ', '.join(pretty_targets)

    return NextHopNamedTuple(
        route=destination,
        dhcp_interface=dhcp_interface,
        next_hop=next_hop,
        vrf=vrf,
        vrf_opt=vrf_opt,
        # For next-hop interface is mandatory
        # For dhcp-interface it may be not given, then dhcp-interface is used
        conf_iface=(nexthop_config.get('interface') or dhcp_interface
                    or device_interface),
        conf_metric=int(nexthop_config.get('metric')),
        port=port,
        port_opt=f'port {port}' if port else '',
        policy=nexthop_config.get('check').get('policy'),
        proto=nexthop_config.get('check').get('type'),
        targets=targets,
        pretty_targets=pretty_targets,
        timeout=nexthop_config.get('check').get('timeout'),
        onlink='onlink' if 'onlink' in nexthop_config else '',
        # Hysteresis: a next-hop must fail the check for this many consecutive
        # polls before its route is withdrawn, and pass for this many
        # consecutive polls before its route is (re)installed. Defaults to 1
        # (immediate transition) so behaviour is unchanged unless configured --
        # mainly useful to stop a lossy link (e.g. Wi-Fi) from flapping on a
        # single dropped probe.
        failure_count=int(nexthop_config.get('check').get('failure_count') or 1),
        success_count=int(nexthop_config.get('check').get('success_count') or 1),
    )


RouteNamedTuple = namedtuple(
    'RouteConfig',
    [
        'destination',
        'vrf',
        'vrf_opt',
        'config_path',
        'nexthops',
    ],
)


def get_route_config(route, route_config, config_path, vrf):
    vrf_opt = f'vrf {vrf}' if vrf else ''
    nexthops = []
    if route_config.get('next_hop'):
        nexthops.extend(
            get_nexthop_config_vars(route, vrf, vrf_opt, nexthop_config, next_hop, None)
            for next_hop, nexthop_config in route_config.get('next_hop').items()
        )
    if route_config.get('dhcp_interface'):
        nexthops.extend(
            get_nexthop_config_vars(
                route, vrf, vrf_opt, dhcp_nexthop_config, None, interface
            )
            for interface, dhcp_nexthop_config in route_config.get(
                'dhcp_interface'
            ).items()
        )
    if route_config.get('interface'):
        # Via-less interface routes (e.g. a point-to-point cellular WWAN with no
        # gateway): installed as `<route> dev <iface>`. The tag value is the
        # egress interface; there is no next-hop to resolve.
        nexthops.extend(
            get_nexthop_config_vars(
                route, vrf, vrf_opt, if_config, None, None,
                device_interface=interface
            )
            for interface, if_config in route_config.get('interface').items()
        )
    nexthops = tuple(nexthops)
    return RouteNamedTuple(
        destination=route,
        vrf=vrf,
        vrf_opt=vrf_opt,
        config_path=config_path,
        nexthops=nexthops,
    )


def parse_config(config, path):
    parsed = []
    vrf = config.get('vrf_context', '')
    # route (IPv4) and route6 (IPv6) share the same next-hop/check model; the
    # route prefix and next-hop values carry the family, which `ip route`
    # auto-detects, so both are parsed into the same RouteNamedTuple list.
    for key in ('route', 'route6'):
        for route, route_config in (config.get(key) or {}).items():
            parsed.append(get_route_config(route, route_config, path, vrf))
    return parsed


def flush_all_routes():
    print_debug("flush_all_routes called")
    # Flush both families' failover routes (IPv4 and IPv6 are separate tables).
    # "table all" includes the dedicated probe table, so scoped probe routes
    # are reaped here too; their policy rules are removed separately.
    for flush_cmd in ('ip route flush protocol failover table all',
                      'ip -6 route flush protocol failover table all'):
        run(flush_cmd)
        journal.send(
            flush_cmd,
            SYSLOG_IDENTIFIER=my_name,
        )
    flush_probe_rules()


kill_called = False


def kill_handler(*args):
    global kill_called
    if kill_called:
        return
    kill_called = True
    print_debug(f"kill_handler called for signal {args[0]}")


def get_ip_command_args(nhc):
    # A via-less interface route (e.g. a point-to-point cellular WWAN with no
    # gateway) is installed as `<route> dev <iface>`; a gateway next-hop keeps
    # the `via <next-hop>` form.
    via = f'via {nhc.next_hop} ' if nhc.next_hop else ''
    return (
        f'{nhc.route} {via}dev {nhc.conf_iface} '
        f'{nhc.onlink} metric {nhc.conf_metric} {nhc.vrf_opt} proto failover'
    )


def delete_route(ip_args):
    print_debug(f'    [ DEL ] -- ip route del {ip_args} [DELETE]')
    rc_cmd(f'ip route del {ip_args}')
    journal.send(
        f'ip route del {ip_args}',
        SYSLOG_IDENTIFIER=my_name,
    )


# Scoped health-probe routing. A probe route must let the health-check ping
# reach an off-link target via the next-hop gateway even when the failover
# default is withdrawn -- but it must NOT be visible to forwarded/downstream
# traffic, or it would hijack LAN traffic to the target IP (e.g. a check target
# that is also the clients' DNS server) down a possibly-dead interface. So the
# route lives in a dedicated table reached only by a policy rule matching
# locally-generated packets (iif lo) to that specific target.
probe_table = 250
probe_rule_prio = 12500


def get_probe_route_args(target, nhc):
    """ip-route args (in the dedicated probe table) for a transient host route
    that lets the health probe reach ``target`` through this next-hop's
    gateway, independent of whether the next-hop's own failover route is
    currently installed.

    It reuses the next-hop's via/dev/onlink verbatim, so it makes no assumption
    about the interface's own lease prefix: an on-subnet gateway (no onlink), an
    off-subnet /32 passthrough gateway (onlink, from config), or a link-local
    IPv6 gateway are all handled exactly as the real route handles them. Marked
    proto failover so a crash leaves it for flush_all_routes() to clean up."""
    onlink = f'{nhc.onlink} ' if nhc.onlink else ''
    return (f'{target} via {nhc.next_hop} dev {nhc.conf_iface} '
            f'{onlink}table {probe_table} proto failover')


def get_probe_rule_args(target):
    """ip-rule args scoping the probe route to locally-generated packets
    (iif lo) destined to this specific target, so forwarded/downstream traffic
    (iif = a real interface) never uses the probe route."""
    return f'iif lo to {target} lookup {probe_table} priority {probe_rule_prio}'


def add_probe_routes(nhc):
    """Install scoped per-target probe routes so an off-link check target is
    reached through the next-hop gateway even while this next-hop's failover
    route is withdrawn.

    Without this the probe is bound to the egress interface (ping -I <iface>),
    the kernel treats the target as on-link, ARPs it directly and never gets a
    reply -- so after the route is withdrawn (e.g. by a DHCP gateway
    re-resolution) the check can never pass to bring it back: a self-latching
    failure whose only escape is some other actor transiently re-installing a
    usable route.

    The route is confined to a dedicated table reached only by an `iif lo to
    <target>` policy rule, so it is invisible to forwarded/downstream traffic
    and only steers the health-check ping itself.

    Returns the list of (route_args, rule_args, fam) to remove once the probe
    completes. No-op for a via-less (point-to-point) next-hop that needs no
    gateway resolution, for ARP checks (which test the on-link neighbour
    itself), and for targets carrying their own interface/vrf override or a
    null host."""
    added = []
    if not nhc.next_hop or nhc.proto not in ('icmp', 'tcp'):
        return added
    for t in nhc.targets:
        if t.interface or t.vrf or t.target in ('0.0.0.0', '::'):
            continue
        fam = '-6 ' if ':' in t.target else ''
        route_args = get_probe_route_args(t.target, nhc)
        rule_args = get_probe_rule_args(t.target)
        rc, _ = rc_cmd(f'ip {fam}route replace {route_args}')
        if rc != 0:
            continue
        rc_cmd(f'ip {fam}rule add {rule_args}')
        added.append((route_args, rule_args, fam))
    return added


def del_probe_routes(probe_routes):
    """Remove the scoped probe routes/rules added by add_probe_routes()."""
    for route_args, rule_args, fam in probe_routes:
        rc_cmd(f'ip {fam}rule del {rule_args}')
        rc_cmd(f'ip {fam}route del {route_args}')


def flush_probe_rules():
    """Remove any leftover probe policy rules at probe_rule_prio (crash-safety;
    the probe routes themselves are in the probe table and marked proto
    failover, so flush_all_routes() reaps them)."""
    for fam in ('', '-6 '):
        # Deleting by priority removes one matching rule per call; loop until
        # none remain (bounded to avoid spinning on an unexpected error).
        for _ in range(16):
            rc, _ = rc_cmd(f'ip {fam}rule del priority {probe_rule_prio}')
            if rc != 0:
                break


def update_configuration(last_modification_times, all_routes, config_dir):
    """
    Updates configuration:
        rechecks config_dir for new/updated files,
        deletes routes that were deleted from configuration

    Args:
        last_modification_times(dict): keys: relative path to file, value: last modification time.
            Is updated.
        all_routes(list): list of routes that were configured in previous call.
            Is updated.
        config_dir(Path): path to configuration directory
    """

    try:
        # First check if there are any changes at all
        have_changes = False
        for child in config_dir.iterdir():
            file_key = str(child)
            if file_key not in last_modification_times:
                have_changes = True
                print_debug(f"New file '{child}', have changes, rereading all")
                break
            modtime = child.stat().st_mtime_ns
            if modtime != last_modification_times[file_key]:
                have_changes = True
                print_debug(f"File '{child} modified, have changes, rereading all...")
                break

        if not have_changes:
            print_debug("No changes in configuration detected.")
            return

        last_modification_times.clear()
        new_routes = []

        # It is important that in configuration directory there MUST be
        # only files generated by conf_mode/protocols_failover.py - otherwise
        # the script won't be able to detect when all VRFs are disabled and
        # won't be able to stop the service gracefully
        for child in config_dir.iterdir():
            if not child.is_file():
                print(
                    f"Path {child} under configuration dir is not a file! Please clean configuration directory {config_dir}."
                )
                exit(1)

            modtime = child.stat().st_mtime_ns
            file_key = str(child)
            last_modification_times[file_key] = modtime

            try:
                config = json.loads(child.read_text())
                print_debug(f"Config from '{child}': {config}")
            except OSError as err:
                print(f'Configuration file "{child}" could not be read: {err}')
                exit(1)
            except json.JSONDecodeError as err:
                print(
                    f'Configuration file "{child}" could not be parsed as JSON: {err}'
                )
                exit(1)
            except UnicodeDecodeError as err:
                print(f'Configuration file "{child}" has Unicode errors: {err}')
                exit(1)

            parsed_config = parse_config(config, file_key)
            new_routes.extend(parsed_config)
    except OSError as err:
        print(f'Configuration dir "{config_dir}" does not exist or not readable: {err}')
        exit(1)

    old_routes_set = set(all_routes)
    new_routes_set = set(new_routes)

    delete_routes = old_routes_set - new_routes_set
    add_routes = new_routes_set - old_routes_set

    # Delete not needed routes
    for route_config in delete_routes:
        print_debug(
            f"Deleting route {route_config}, not present in updated configuration"
        )
        for nhc in route_config.nexthops:
            ip_args = get_ip_command_args(nhc)
            if is_route_exists(ip_args):
                delete_route(ip_args)
        all_routes.remove(route_config)

    # Add new routes
    print_debug(f"Adding routes {add_routes}, new in updated configuration")
    all_routes.extend(add_routes)

    print_debug(f"All routes: {all_routes}")


# Number of consecutive polls that may resolve no DHCP gateway (while one was
# previously known) before a working dhcp-interface route is withdrawn. Keeps a
# single transient lease-file read miss from instantly dropping a healthy route.
dhcp_gw_absent_limit = 3


def process_dhcp_interface(nhc, nexthop_by_dhcp_nexthop, gw_miss_state=None):
    """
    Processes NextHopNamedTuple with dhcp_interface != None
    Return NextHopNamedTuple with next_hop equal to DHCP gateway of nhc.
        If there is no gateway for nhc, return None

    Args:
        nhc(NextHopNamedTuple): configuration with dhcp_interface
        nexthop_by_dhcp_nexthop(dict): dict with previous returned values
        gw_miss_state(dict): per-nexthop count of consecutive polls that
            resolved no DHCP gateway while one was previously known; used to
            debounce a transient "gateway gone" reading. Optional; when omitted
            the previous (delete-on-first-miss) behaviour is kept.
    """
    cur_dhcpgw = get_dhcp_router(nhc.dhcp_interface)
    if not cur_dhcpgw:
        cur_dhcpgw = False

    if nhc in nexthop_by_dhcp_nexthop:
        prev_dhcpgw = nexthop_by_dhcp_nexthop[nhc].next_hop
    else:
        prev_dhcpgw = False

    # Debounce a transient "no gateway" reading. The lease env file is now
    # written atomically (03-vyos-dhclient-hook), so a missing gateway should
    # be a real signal -- but keeping a healthy route across a single unlucky
    # read (lease-file rotation, a stat hiccup, or any future non-atomic
    # writer) avoids instantly dropping it, and removes the asymmetry where one
    # miss withdrew the route while recovery needed success_count good polls.
    if gw_miss_state is not None:
        if not cur_dhcpgw and prev_dhcpgw:
            misses = gw_miss_state.get(nhc, 0) + 1
            gw_miss_state[nhc] = misses
            if misses < dhcp_gw_absent_limit:
                print_debug(
                    f"No DHCP gateway for interface {nhc.dhcp_interface} "
                    f"(miss {misses}/{dhcp_gw_absent_limit}); keeping previous "
                    f"gateway '{prev_dhcpgw}'"
                )
                return nexthop_by_dhcp_nexthop[nhc]
        else:
            # Any definitive reading (gateway present, or confirmed absent with
            # no previous gateway) clears the miss counter.
            gw_miss_state.pop(nhc, None)

    # Equal - do nothing, just return previous value
    if prev_dhcpgw == cur_dhcpgw:
        if not cur_dhcpgw:
            return None
        return nexthop_by_dhcp_nexthop[nhc]

    print_debug(
        f"DHCP Gateway changed for interface {nhc.dhcp_interface} from '{prev_dhcpgw}' to '{cur_dhcpgw}'"
    )

    # dhcpgw differ and there was previous dhcpgw
    if prev_dhcpgw:
        prevnhc = nexthop_by_dhcp_nexthop.pop(nhc)
        print_debug(
            f"Deleting previous nexthop {prevnhc} because of DHCP interface change"
        )
        ip_args = get_ip_command_args(prevnhc)
        if is_route_exists(ip_args):
            delete_route(ip_args)

    newnhc = None
    # dhcpgw differ and there is new dhcpgw
    if cur_dhcpgw:
        newnhc = nhc._replace(next_hop=cur_dhcpgw, dhcp_interface=None)
        print_debug(f"Saving new nexthop {newnhc} because of DHCP interface change")
        nexthop_by_dhcp_nexthop[nhc] = newnhc

    return newnhc


if __name__ == '__main__':
    print_debug(f"{my_name} started")

    # Parse command arguments and get config
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-c',
        '--config-dir',
        action='store',
        help='Path to protocols failover configuration dir',
        required=True,
        type=Path,
    )

    args = parser.parse_args()
    config_dir = Path(args.config_dir)

    last_modification_times = {}
    all_routes = []


    # Clean all `failover` routes now and at exit
    flush_all_routes()
    atexit.register(flush_all_routes)
    signal.signal(signal.SIGINT, kill_handler)
    signal.signal(signal.SIGTERM, kill_handler)

    # keys: NextHopNamedTuple with dhcp_interface != None
    # values: NextHopNamedTuple with next_hop != None
    # Translates nexthop with dhcp_interface to usual nexthop
    nexthop_by_dhcp_nexthop = {}

    # iface -> last time it was the active (selected) failover egress; drives the
    # connect-on-demand bearer-lease release hold-down across loop iterations.
    wwan_lease_hold_state = {}

    # Per-next-hop health hysteresis counters, persisted across loop iterations.
    # key -> {'fail': consecutive failed polls, 'success': consecutive OK polls}.
    # A route is only withdrawn after `failure_count` consecutive failures and
    # only (re)installed after `success_count` consecutive successes, so a lossy
    # link does not flap on a single dropped probe.
    nexthop_health_state = {}

    # Per-dhcp-interface next-hop count of consecutive polls that resolved no
    # DHCP gateway while one was previously known; debounces a transient lease
    # read miss in process_dhcp_interface().
    dhcp_gw_miss_state = {}

    # Per-route (vrf, dest) -> currently active (lowest-metric, installed)
    # egress interface. When it changes (failover/failback) we flush conntrack
    # bound to the vacated interface so established masqueraded downstream flows
    # re-establish via the new egress instead of staying pinned to the old
    # interface's source address. Deliberately NOT cleared when a route goes
    # fully down, so a later recovery on a *different* egress is still detected
    # as a change and triggers the flush.
    active_egress_by_route = {}
    # iface -> last-known global addresses while it was the active egress; used
    # to flush conntrack by the address the interface held even if it has since
    # lost it (e.g. a link-down failover).
    iface_addr_cache = {}

    had_sleeps = True
    while not kill_called:
        # Check in case daemon was launched without routes
        if not had_sleeps:
            time.sleep(int(config_timeout))

        update_configuration(last_modification_times, all_routes, config_dir)
        had_sleeps = False

        # Per-iteration health snapshot (iface -> alive bool), collected as each
        # next-hop is checked below and consumed by the connect-on-demand lease
        # reconcile at the end of the iteration.
        alive_by_iface = {}

        for route_config in all_routes:
            if kill_called:
                break
            route = route_config.destination
            vrf = route_config.vrf
            vrf_opt = route_config.vrf_opt

            # Per-nexthop (metric, iface, route-present-after-this-iteration),
            # used after the loop to find the active egress and detect failover.
            route_nexthop_states = []

            for nhc in route_config.nexthops:
                # Perle change start: process dhcp_interface
                is_dhcp_iface = True if nhc.dhcp_interface is not None else False
                # Perle change end

                if nhc.dhcp_interface:
                    nhc = process_dhcp_interface(nhc, nexthop_by_dhcp_nexthop,
                                                 dhcp_gw_miss_state)
                    if not nhc:
                        continue

                next_hop = nhc.next_hop
                ip_args = get_ip_command_args(nhc)

                # Break the probe<->route latch: install transient per-target
                # host routes via this next-hop's gateway so the check can reach
                # an off-link target even while the failover route is withdrawn,
                # then remove them once the probe completes (no-op for via-less
                # next-hops / ARP checks). Without this the probe can never pass
                # to bring a withdrawn route back on its own.
                probe_routes = add_probe_routes(nhc)
                try:
                    is_alive = is_target_alive(
                        nhc.targets,
                        nhc.conf_iface,
                        nhc.proto,
                        nhc.port,
                        nexthop_vrf=vrf,
                        policy=nhc.policy,
                        # Perle change start
                        is_dhcp_interface=is_dhcp_iface,
                        # Perle change end
                    )
                finally:
                    del_probe_routes(probe_routes)

                # Update this next-hop's consecutive fail/success counters so a
                # single dropped probe on a lossy link does not flip the route.
                state_key = (vrf or '', route, nhc.conf_iface or '',
                             nhc.conf_metric, nhc.next_hop or '')
                hstate = nexthop_health_state.setdefault(
                    state_key, {'fail': 0, 'success': 0})
                if is_alive:
                    hstate['success'] += 1
                    hstate['fail'] = 0
                else:
                    hstate['fail'] += 1
                    hstate['success'] = 0

                route_present = is_route_exists(ip_args)
                # Tracks whether this next-hop's route is installed after this
                # iteration's add/delete action (seeds the active-egress /
                # conntrack-flush decision below).
                present_now = route_present

                # Effective (hysteresis-smoothed) health: bring a route up only
                # after success_count consecutive OK polls, take it down only
                # after failure_count consecutive failed polls, and otherwise
                # keep whatever state the route is already in.
                if route_present:
                    effective_alive = hstate['fail'] < nhc.failure_count
                else:
                    effective_alive = hstate['success'] >= nhc.success_count

                # Record this next-hop's (smoothed) health for the
                # connect-on-demand lease reconcile at the end of the iteration
                # (which next-hop is the active egress is decided from this
                # snapshot).
                if nhc.conf_iface:
                    alive_by_iface[nhc.conf_iface] = effective_alive

                # Route not found in the current routing table
                if not route_present:
                    print_debug(f"    [NEW_ROUTE_DETECTED] route: [{route} {vrf_opt}]")
                    # Add route once the success threshold is met
                    if effective_alive:
                        print_debug(f'    [ ADD ] -- ip route add {ip_args}\n###')
                        rc, command = rc_cmd(f'ip route add {ip_args}')
                        # If something is wrong and gateway not added
                        # Example: Error: Next-hop has invalid gateway.
                        if rc != 0:
                            present_now = False
                            print_debug(
                                f'{command} -- return-code [RC: {rc}] {next_hop} dev {nhc.conf_iface}'
                            )
                        else:
                            present_now = True
                            journal.send(
                                f'ip route add {ip_args}',
                                SYSLOG_IDENTIFIER=my_name,
                            )
                    else:
                        if is_alive:
                            # Probe passed but the route is not installed yet:
                            # we are still accumulating success_count
                            # consecutive successes before (re)installing it.
                            # This is NOT a failure -- log it distinctly so it
                            # is not misread as a dead target (e.g. during the
                            # convergence that follows a daemon restart).
                            print_debug(
                                f'    [ TARGET_OK_PENDING ] target check passed '
                                f'for [{nhc.pretty_targets}] on {nhc.conf_iface}; '
                                f'awaiting success-count '
                                f'({hstate["success"]}/{nhc.success_count}) '
                                f'before (re)installing route'
                            )
                            journal.send(
                                f'Check ok for route {route} interface '
                                f'"{nhc.conf_iface}" target {nhc.pretty_targets} '
                                f'proto {nhc.proto} {nhc.port_opt}-- awaiting '
                                f'success-count '
                                f'({hstate["success"]}/{nhc.success_count}) '
                                f'before installing route',
                                SYSLOG_IDENTIFIER=my_name,
                            )
                        else:
                            print_debug(
                                f'    [ TARGET_FAIL ] target checks fails for [{nhc.pretty_targets}] '
                                f'(fail {hstate["fail"]}, success {hstate["success"]}/{nhc.success_count}), do nothing'
                            )
                            journal.send(
                                f'Check fail for route {route} interface "{nhc.conf_iface}" target {nhc.pretty_targets} proto {nhc.proto} '
                                f'{nhc.port_opt}',
                                SYSLOG_IDENTIFIER=my_name,
                            )
                else:
                    # Route is installed; withdraw it only once the failure
                    # threshold is reached (failure_count consecutive failures).
                    if not effective_alive:
                        print_debug(
                            f"Next_hop {next_hop} fail, target check didn't pass "
                            f"({hstate['fail']}/{nhc.failure_count} consecutive failures)"
                        )
                        delete_route(ip_args)
                        present_now = False

                # Record this next-hop's post-action state for the active-egress
                # decision after the loop.
                if nhc.conf_iface:
                    route_nexthop_states.append(
                        (int(nhc.conf_metric), nhc.conf_iface, present_now))

                had_sleeps = True
                time.sleep(int(nhc.timeout))
                if kill_called:
                    break

            # After processing all next-hops for this route, determine the
            # active (lowest-metric, installed) egress. If it changed since the
            # last iteration, flush conntrack bound to the vacated interface so
            # established masqueraded downstream flows re-establish via the new
            # egress rather than staying pinned to the old interface's address
            # (otherwise the router itself fails over but LAN clients hang).
            present_egress = [(m, i) for (m, i, p) in route_nexthop_states if p]
            new_active = min(present_egress)[1] if present_egress else None
            eg_key = (vrf or '', route)
            old_active = active_egress_by_route.get(eg_key)
            if new_active:
                iface_addr_cache[new_active] = get_iface_addresses(new_active)
            if old_active and new_active and new_active != old_active:
                addrs = set(iface_addr_cache.get(old_active, []))
                addrs |= set(get_iface_addresses(old_active))
                # Flush only the family that actually failed over -- this
                # route's destination family. IPv4 (route) and IPv6 (route6)
                # defaults fail over independently, so flushing the vacated
                # interface's *other*-family addresses would needlessly reset
                # still-healthy connections (e.g. a v4 failover must not drop v6
                # NAT flows that are still correctly routed via that interface).
                route_is_v6 = ':' in route
                addrs = {a for a in addrs if (':' in a) == route_is_v6}
                if addrs:
                    print_debug(
                        f'    [ FAILOVER ] route {route} egress '
                        f'{old_active} -> {new_active}; flushing conntrack')
                    flush_conntrack_for_addrs(sorted(addrs),
                                              vacated_iface=old_active)
            # Only record a concrete egress; leave the last-known value in place
            # across a full outage so a recovery on a different egress is still
            # seen as a change.
            if new_active:
                active_egress_by_route[eg_key] = new_active

            # Note: a dial-on-demand WWAN bearer is NOT torn down on failback
            # here. Dial-on-demand idles its own bearer down when no traffic is
            # directed at it, so vyos-failover does not need to disconnect it.

        # End of iteration: raise/hold a bearer lease on any connect-on-demand
        # WWAN that is now the active (lowest-metric healthy) failover egress,
        # and release it (after a hold-down) once a better path recovers. No-op
        # unless a connect-on-demand WWAN is actually a failover next-hop.
        if not kill_called:
            reconcile_failover_leases(all_routes, alive_by_iface,
                                      wwan_lease_hold_state)

    print_debug(f"Out of main loop, {kill_called=}")
