#!/usr/bin/python3

# Copyright VyOS maintainers and contributors <maintainers@vyos.io>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.  If not, see <http://www.gnu.org/licenses/>.

import json
import os
import signal
import sys
import time

from vyos.config import Config
from vyos.template import render
from vyos.utils.commit import commit_in_progress
from vyos.utils.dict import dict_search_args
from vyos.utils.network import get_interface_address
from vyos.utils.process import rc_cmd
from vyos.utils.process import run
from vyos.xml_ref import get_defaults
from vyos.wanloadbalance import health_ping_host
from vyos.wanloadbalance import health_ping_host_ttl
from vyos.wanloadbalance import parse_dhcp_nexthop
from vyos.wanloadbalance import parse_ppp_nexthop

from vyos.utils.wwan.wwan_client import (  # noqa: E402
    WWANClientSync,
    WWANError,
)


nftables_wlb_conf = '/run/nftables_wlb.conf'
wlb_status_file = '/run/wlb_status.json'
wlb_pid_file = '/run/wlb_daemon.pid'
sleep_interval = 5 # Main loop sleep interval

def _interface_modem_check(iface):
    """Health check for a dial-on-demand WWAN interface.

    A dial-on-demand bearer is intentionally DOWN whenever it is idle (that is
    the whole point), so testing bearer status would mark the interface
    permanently unhealthy -- WLB would then never select it as the active
    failover link, never emit a `jump wlb_mangle_isp_<iface>` marking rule, and
    the on-demand nft trigger would never see a marked packet to dial on. So
    instead treat the interface as healthy when the modem is REGISTERED on the
    network (ready to dial on demand) or already connected.

    Uses the ModemManager modem state (8 REGISTERED .. 11 CONNECTED) with the
    3GPP registration state (1 HOME / 5 ROAMING) as a fallback.
    """
    status = None
    try:
        client = WWANClientSync()
        ifnum = int(iface[4:])
        try:
            # add_interface is idempotent and just ensures the service has the
            # interface object so get_status() can resolve it.
            client.add_interface(ifnum)
            status = client.get_status(ifnum)
        finally:
            client.close()
    except Exception as exc:
        print(f'WLB: WWAN health check failed for {iface}: {exc}')
        return False

    # MMModemState: 8 REGISTERED, 9 DISCONNECTING, 10 CONNECTING, 11 CONNECTED
    # all mean "on the network and able to carry a bearer".
    if status is not None:
        try:
            modem_state = int(status.get('modem_state', -1))
            fsm_state = status.get('fsm_state', 'DISCONNECTED')
            print(fsm_state)
        except (TypeError, ValueError):
            modem_state = -1
        if modem_state >= 8 and (fsm_state == 'CONNECTED' or fsm_state == "REGISTERED_IDLE" or fsm_state == "CONNECTING"):
            return True

    # Fallback: 3GPP registration state 1 HOME / 5 ROAMING = registered.
    return False

def _interface_modem_disconnect(iface):
    """Tear down the dial-on-demand bearer on a WWAN interface.

    Uses WWANClientSync to disconnect the bearer (modem stays registered) so
    the modem stops dialing after failing back to a higher-priority interface
    but can still redial on demand later.
    """
    try:
        client = WWANClientSync()
        ifnum = int(iface[4:])
        client.add_interface(ifnum)
        try:
            # Only drop an actually-established bearer; a registered/idle modem
            # has nothing to tear down.
            if client.get_bearer_status(ifnum) == 'connected':
                print(f'WLB failback: disconnecting bearer on {iface}')
                client.disconnect(ifnum)
        finally:
            # Always release the D-Bus connection, even if the poll/disconnect
            # raised — a leaked client would otherwise accumulate one open bus
            # connection per failback in this long-lived daemon.
            client.close()
    except Exception as exc:
        print(f'WLB failback: disconnect failed for {iface}: {exc}')


def get_failover_active_interface(rule_conf, health_state):
    """Return the active interface name for a failover rule, or None.

    In failover mode wlb_weight_interfaces() returns a single-element list
    holding the highest-weight healthy interface (the active one).
    """
    from vyos.wanloadbalance import wlb_weight_interfaces

    weights, _ = wlb_weight_interfaces(rule_conf, health_state)
    if len(weights) == 1:
        return weights[0][0]
    return None

def handle_dial_on_demand_transition(lb, active_failover_iface, conf):
    """Keep every non-active dial-on-demand WWAN bearer torn down.

    In failover mode exactly one interface -- the highest-weight healthy one --
    is active. A dial-on-demand WWAN reports "healthy" while merely registered
    (bearer down), so it is a legitimate failover target, but its bearer must
    only be UP while it is the active link. Bringing it UP is owned by the
    on-demand nft packet-trigger (dial_on_demand pipeline): when WLB steers LAN
    traffic to the WWAN by fwmark, the trigger queues a packet and dials.

    This function owns the opposite direction: ensuring any dial-on-demand WWAN
    that is NOT the active link has its bearer DOWN. It reconciles the actual
    bearer state against the active-link decision on EVERY pass rather than only
    on the switch edge, because a WWAN bearer can come up without WLB ever
    selecting it -- e.g. the FSM auto-connects dial-on-demand at boot/service
    restart, a brief failover blip dialed it, or stray traffic triggered a dial.
    Edge-only handling misses all of those: if a higher-priority link (e.g.
    eth0) has been active the whole time, the "switched off the WWAN" edge never
    fires and the stray bearer lingers up forever.

    `_interface_modem_disconnect` is idempotent (it polls the bearer and only
    disconnects an actually-connected one, leaving the modem registered so it
    stays a valid failover target and can redial on demand later), so running
    the reconcile every pass is safe and self-correcting.

    Args:
        lb(dict): load-balancer state/config dictionary.
        active_failover_iface(dict): per-rule memory of the currently active
            (highest-weight healthy) interface, keyed by rule id. Updated in
            place (used for logging / cross-pass visibility).
        conf(Config): VyOS config handle reused for dial-on-demand checks.
    """
    if 'rule' not in lb:
        return

    health_state = lb['health_state']

    for rule_id, rule_conf in lb['rule'].items():
        if 'failover' not in rule_conf or 'interface' not in rule_conf:
            continue

        current_iface = get_failover_active_interface(rule_conf, health_state)

        # No healthy active link this pass: do not touch any bearer. A WWAN may
        # be the only remaining path (about to become active, or mid-dial), so
        # disconnecting it here could black-hole traffic. Keep the last known
        # active interface as memory.
        if current_iface is None:
            continue

        active_failover_iface[rule_id] = current_iface

        # Reconcile every dial-on-demand WWAN member of this failover rule: the
        # single active link is allowed to stay connected (the nft trigger dials
        # it); any OTHER dial-on-demand WWAN must have its bearer torn down,
        # however it came up.
        for ifname in rule_conf['interface']:
            if ifname == current_iface:
                continue
            if dial_on_demand_check(ifname, conf):
                _interface_modem_disconnect(ifname)

def dial_on_demand_check(ifname, conf=None):
    """
    Return True if ifname is a WWAN interface configured for dial-on-demand.

    Only wwanX interfaces with dial-on-demand use the modem bearer check; every
    other interface (ethX, ...) uses the normal IP health check.
    """
    if not ifname.startswith('wwan'):
        return False
    if conf is None:
        conf = Config()
    mode = conf.return_effective_value(
        ['interfaces', 'wwan', ifname, 'connection-mode'])
    return mode == 'dial-on-demand'


def _in_failover_rule(ifname, lb):
    """Return True if ifname is a member of any WAN load-balancing failover rule.

    The dial-on-demand daemon behaviours (the modem-state health check and the
    non-active bearer teardown in handle_dial_on_demand_transition) exist only
    to support failover. When no failover rule governs the interface, the load
    balancer must behave exactly as it would for any other interface -- normal
    IP health checks, no bearer management -- regardless of the interface's
    connection-mode. Gating on failover membership keeps the daemon's behaviour
    unchanged when failover (or dial-on-demand) is not configured.
    """
    for rule_conf in (lb.get('rule') or {}).values():
        if 'failover' in rule_conf and ifname in (rule_conf.get('interface') or {}):
            return True
    return False


def health_check(ifname, conf, state, test_defaults, lb):
    # Intercept with the modem bearer-state check ONLY for a dial-on-demand
    # WWAN that is governed by a failover rule. Without failover the load
    # balancer must behave identically to any other interface and use the
    # normal IP health check, regardless of connection-mode.
    if _in_failover_rule(ifname, lb) and dial_on_demand_check(ifname):
        return _interface_modem_check(ifname)

    # Run health tests for interface

    if get_ipv4_address(ifname) is None:
        return False

    if 'test' not in conf:
        resp_time = test_defaults['resp-time']
        target = conf['nexthop']

        if target == 'dhcp':
            target = state['dhcp_nexthop']

        if not target:
            return False
        return health_ping_host(target, ifname, wait_time=resp_time)

    for test_id, test_conf in conf['test'].items():
        check_type = test_conf['type']

        if check_type == 'ping':
            resp_time = test_conf['resp_time']
            target = test_conf['target']
            if not health_ping_host(target, ifname, wait_time=resp_time):
                return False
        elif check_type == 'ttl':
            target = test_conf['target']
            ttl_limit = test_conf['ttl_limit']
            if not health_ping_host_ttl(target, ifname, ttl_limit=ttl_limit):
                return False
        elif check_type == 'user-defined':
            script = test_conf['test_script']
            rc = run(script)
            if rc != 0:
                return False

    return True

def on_state_change(lb, ifname, state):
    # Run hook on state change
    if 'hook' in lb:
        script_path = os.path.join('/config/scripts/', lb['hook'])
        env = {
            'WLB_INTERFACE_NAME': ifname,
            'WLB_INTERFACE_STATE': 'ACTIVE' if state else 'FAILED'
        }

        code = run(script_path, env=env)
        if code != 0:
            print('WLB hook returned non-zero error code')

    print(f'INFO: State change: {ifname} -> {state}')

def get_ipv4_address(ifname):
    # Get primary ipv4 address on interface (for source nat)
    addr_json = get_interface_address(ifname)
    if addr_json and 'addr_info' in addr_json and len(addr_json['addr_info']) > 0:
        for addr_info in addr_json['addr_info']:
            if addr_info['family'] == 'inet':
                if 'local' in addr_info:
                    return addr_json['addr_info'][0]['local']
    return None

def get_dynamic_nexthop(ifname: str) -> str | None | bool:
    '''
    Resolve the dynamic next-hop for a WAN interface.

    Determines the current default gateway learned dynamically on the interface:
    - PPPoE interfaces (`pppoe*`): uses `parse_ppp_nexthop`.
    - Other interfaces (e.g. DHCP): uses `parse_dhcp_nexthop`.

    Return values:
    - str: IPv4 next-hop address
    - None: when DHCP lease has no router value
    - False: when PPPoE nexthop state file is missing

    Args:
        ifname: Interface name (e.g. 'pppoe0', 'eth0').

    Returns:
        See above for possible values.
    '''
    if ifname.startswith('pppoe'):
        return parse_ppp_nexthop(ifname)
    else:
        return parse_dhcp_nexthop(ifname)

def dynamic_nexthop_update(lb, ifname):
    # Update on DHCP/PPP address/nexthop changes
    # Return True if nftables needs to be updated - IP change

    if 'dhcp_nexthop' in lb['health_state'][ifname]:
        dhcp_nexthop_addr = get_dynamic_nexthop(ifname)

        table_num = lb['health_state'][ifname]['table_number']

        if dhcp_nexthop_addr and lb['health_state'][ifname]['dhcp_nexthop'] != dhcp_nexthop_addr:
            lb['health_state'][ifname]['dhcp_nexthop'] = dhcp_nexthop_addr
            run(f'ip route replace table {table_num} default dev {ifname} via {dhcp_nexthop_addr}')

    if_addr = get_ipv4_address(ifname)
    if if_addr and if_addr != lb['health_state'][ifname]['if_addr']:
        lb['health_state'][ifname]['if_addr'] = if_addr
        return True

    return False

def restore_default_route(lb: dict, ifname: str) -> None:
    """
    Restores a missing default route for a WAN interface in its policy routing table.

    When a link flap or DHCP/PPP renegotiation removes the per-interface default route,
    this function checks the interface’s assigned table for an existing default entry.
    If none is found, it determines the proper next-hop (from DHCP, PPP, or static config)
    and reinstalls the route using:
      ip route replace table <table_num> default dev <ifname> via <nexthop>

    @param lb       Load-balancer state/config dictionary.
    @param ifname   Interface name whose default route should be verified and restored.
    @returns        None — exits quietly if the table number or next-hop cannot be found.
    """
    table_num = dict_search_args(lb, 'health_state', ifname, 'table_number')
    if not table_num:
        return

    rc, out = rc_cmd(f'ip -j route show default table {table_num}')
    if rc == 0:
        rt_table = json.loads(out)
        if len(rt_table) > 0:
            return
        else:
            if 'dhcp_nexthop' in lb['health_state'][ifname]:
                nexthop_addr = get_dynamic_nexthop(ifname)
            else:
                nexthop_addr = dict_search_args(lb, 'interface_health', ifname, 'nexthop')

            if nexthop_addr:
                run(f'ip route replace table {table_num} default dev {ifname} via {nexthop_addr}')
    else:
        return

def nftables_update(lb):
    # Atomically reload nftables table from template
    if not os.path.exists(nftables_wlb_conf):
        lb['first_install'] = True
    elif 'first_install' in lb:
        del lb['first_install']

    render(nftables_wlb_conf, 'load-balancing/nftables-wlb.j2', lb)

    rc, out = rc_cmd(f'nft -f {nftables_wlb_conf}')

    if rc != 0:
        print('ERROR: Failed to apply WLB nftables config')
        print('Output:', out)
        return False

    return True

def cleanup(lb):
    if 'interface_health' in lb:
        index = 1
        for ifname, health_conf in lb['interface_health'].items():
            table_num = lb['mark_offset'] + index
            run(f'ip route del table {table_num} default')
            run(f'ip rule del fwmark {hex(table_num)} table {table_num}')
            index += 1

    run(f'nft delete table ip vyos_wanloadbalance')

def get_config():
    conf = Config()
    base = ['load-balancing', 'wan']
    lb = conf.get_config_dict(base, key_mangling=('-', '_'),
                            get_first_key=True, with_recursive_defaults=True)

    lb['firewall_group'] = conf.get_config_dict(['firewall', 'group'], key_mangling=('-', '_'), get_first_key=True,
                                    no_tag_node_value_mangle=True)

    # prune limit key if not set by user
    for rule in lb.get('rule', []):
        if lb.from_defaults(['rule', rule, 'limit']):
            del lb['rule'][rule]['limit']

    lb['test_defaults'] = get_defaults(base + ['interface-health', 'A', 'test', 'B'], get_first_key=True)

    return lb

if __name__ == '__main__':
    while commit_in_progress():
        print("Notice: Waiting for commit to complete...")
        time.sleep(1)

    lb = get_config()

    lb['health_state'] = {}
    lb['mark_offset'] = 0xc8

    # Create state dicts, interface address and nexthop, install routes and ip rules
    if 'interface_health' in lb:
        index = 1
        for ifname, health_conf in lb['interface_health'].items():
            table_num = lb['mark_offset'] + index
            addr = get_ipv4_address(ifname)
            lb['health_state'][ifname] = {
                'if_addr': addr,
                'failure_count': 0,
                'success_count': 0,
                'last_success': 0,
                'last_failure': 0,
                'state': addr is not None,
                'state_changed': False,
                'table_number': table_num,
                'mark': hex(table_num)
            }

            if health_conf['nexthop'] == 'dhcp':
                lb['health_state'][ifname]['dhcp_nexthop'] = None

                dynamic_nexthop_update(lb, ifname)
            else:
                run(f'ip route replace table {table_num} default dev {ifname} via {health_conf["nexthop"]}')

            run(f'ip rule add fwmark {hex(table_num)} table {table_num}')

            index += 1

        nftables_update(lb)

        run('ip route flush cache')

        if 'flush_connections' in lb:
            for _state in lb['health_state'].values():
                run(f'conntrack --delete --mark {_state["table_number"]}')

        with open(wlb_status_file, 'w') as f:
            f.write(json.dumps(lb['health_state']))

    # Signal handler SIGUSR2 -> dhcpcd update
    def handle_sigusr2(signum, frame):
        for ifname, health_conf in lb['interface_health'].items():
            if 'nexthop' in health_conf and health_conf['nexthop'] == 'dhcp':
                retval = dynamic_nexthop_update(lb, ifname)

                if retval:
                    nftables_update(lb)

    # Signal handler SIGTERM -> exit
    def handle_sigterm(signum, frame):
        if os.path.exists(wlb_status_file):
            os.unlink(wlb_status_file)

        if os.path.exists(wlb_pid_file):
            os.unlink(wlb_pid_file)

        if os.path.exists(nftables_wlb_conf):
            os.unlink(nftables_wlb_conf)

        cleanup(lb)
        sys.exit(0)

    signal.signal(signal.SIGUSR2, handle_sigusr2)
    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGTERM, handle_sigterm)

    with open(wlb_pid_file, 'w') as f:
        f.write(str(os.getpid()))

    # Main loop

    init = True;
    # keys: failover rule id; values: interface name of the previously active
    # (highest-weight healthy) interface. Comparing it against the current
    # active interface each pass drives dial-on-demand WWAN bearer connect on
    # failover TO a WWAN and disconnect on failback OFF a WWAN.
    active_failover_iface = {}
    failback_conf = Config()
    while True:
        try:
            ip_change = False

            if 'interface_health' in lb:
                for ifname, health_conf in lb['interface_health'].items():
                    state = lb['health_state'][ifname]

                    result = health_check(ifname, health_conf, state=state, test_defaults=lb['test_defaults'], lb=lb)

                    state_changed = result != state['state']
                    state['state_changed'] = False

                    if result:
                        state['failure_count'] = 0
                        state['success_count'] += 1
                        state['last_success'] = time.time()
                        if state_changed and state['success_count'] >= int(health_conf['success_count']):
                            state['state'] = True
                            state['state_changed'] = True
                    elif not result:
                        state['failure_count'] += 1
                        state['success_count'] = 0
                        state['last_failure'] = time.time()
                        if state_changed and state['failure_count'] >= int(health_conf['failure_count']):
                            state['state'] = False
                            state['state_changed'] = True

                    #Force state changed to trigger the first write
                    if init == True:
                        state['state_changed'] = True
                        init = False

                    if state['state_changed']:
                        state['if_addr'] = get_ipv4_address(ifname)
                        on_state_change(lb, ifname, state['state'])

                    if dynamic_nexthop_update(lb, ifname):
                        ip_change = True

                    restore_default_route(lb, ifname)

            # After evaluating every interface's health, reconcile any
            # dial-on-demand WWAN bearer with the active failover link: dial it
            # up when a WWAN has become the active link, or tear it down when a
            # higher-priority link has taken over.
            handle_dial_on_demand_transition(lb, active_failover_iface,
                                             failback_conf)

            if any(state['state_changed'] for ifname, state in lb['health_state'].items()):
                if not nftables_update(lb):
                    break

                run('ip route flush cache')

                if 'flush_connections' in lb:
                    for _state in lb['health_state'].values():
                        run(f'conntrack --delete --mark {_state["table_number"]}')

                with open(wlb_status_file, 'w') as f:
                    f.write(json.dumps(lb['health_state']))
            elif ip_change:
                nftables_update(lb)
        except Exception as e:
            # A transient error (e.g. a modem/D-Bus hiccup during a WWAN
            # health check, or ModemManager restarting) must not tear the
            # daemon down: Restart=always would then respawn it immediately,
            # flooding the journal with a health-check storm and repeatedly
            # tearing down the WLB routing/nftables state.  Log it and keep
            # the normal pacing interval instead of exiting.
            print('WLB ERROR:', e)

        time.sleep(sleep_interval)

    if os.path.exists(wlb_status_file):
        os.unlink(wlb_status_file)

    if os.path.exists(wlb_pid_file):
        os.unlink(wlb_pid_file)

    if os.path.exists(nftables_wlb_conf):
            os.unlink(nftables_wlb_conf)

    cleanup(lb)
