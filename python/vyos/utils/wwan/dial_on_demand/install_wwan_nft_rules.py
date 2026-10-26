import subprocess
import argparse
from vyos.config import Config
from pathlib import Path
import asyncio
import logging

logger = logging.getLogger(__name__)

def get_physical_interfaces():
    net_dir = Path("/sys/class/net")

    interfaces = [
        iface.name
        for iface in net_dir.iterdir()
        if "wwan" not in iface.name
        and "wlan" not in iface.name
        and (iface / "device").exists()
    ]

    return interfaces


def get_config():
    config = Config()
    base = ['load-balancing', 'wan']
    lb = config.get_config_dict(base, key_mangling=('-', '_'),
                              no_tag_node_value_mangle=True,
                              get_first_key=True,
                              with_recursive_defaults=True)
    return lb


def get_failover_interfaces(config=None):
    """Interfaces participating in `protocols failover` (its WAN nexthops).

    Collects both forms: `dhcp-interface <if>` (the tag value) and
    `next-hop <ip> interface <if>` (the child leaf). These are WAN links managed
    by failover, NOT LAN sources, so forwarded traffic arriving on them must
    never trigger a dial-on-demand bring-up -- the caller strips them from the
    prerouting iifname scope. Returns an empty set when failover is not
    configured.
    """
    if config is None:
        config = Config()
    failover = config.get_config_dict(['protocols', 'failover'],
                                      key_mangling=('-', '_'),
                                      get_first_key=True)
    ifaces = set()
    for route_conf in (failover.get('route') or {}).values():
        for ifname in (route_conf.get('dhcp_interface') or {}):
            ifaces.add(ifname)
        for nh_conf in (route_conf.get('next_hop') or {}).values():
            iface = nh_conf.get('interface')
            if iface:
                ifaces.add(iface)
    return ifaces


def get_inbound_interfaces(config=None):
    """Physical interfaces eligible as dial-on-demand trigger sources.

    get_physical_interfaces() minus any interface participating in
    `protocols failover`. Never returns an empty list: an empty `iifname { }`
    is invalid nft and would fail every install, so if every physical interface
    happens to be a failover member we fall back to the full physical list.
    """
    physical = get_physical_interfaces()
    inbound = sorted(set(physical) - get_failover_interfaces(config))
    return inbound or physical


def run_nft_cmd(cmd=''):
    subprocess.run(
        ['nft', '-f', '-'],
        input=cmd,
        text=True,
        check=True
    )

async def async_run_nft_cmd(cmd=''):
    proc = await asyncio.create_subprocess_exec(
        "nft", "-f", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(cmd.encode())
    if proc.returncode != 0:
        raise RuntimeError(f'nft rules installation failed: {stderr.decode().strip()}')


def wwan_queue_num(interface):
    # NFQUEUE numbers are global to the network namespace -- they are NOT scoped
    # to the nft table they are referenced from. Reusing a WLB rule number as the
    # queue id therefore collides whenever two WWANs share a rule, and an
    # interface spanning several rules would be split across several queues while
    # the consumer only binds one. To avoid both, derive ONE deterministic queue
    # per interface from the wwan index (wwan0 -> 1, wwan1 -> 2, ...). The same
    # function is used for standalone and WLB installs, so a given interface
    # always maps to the same unique queue and never collides with another
    # WWAN's queue. The consumer reads the queue number back from the
    # '{interface}_raw_{queue}' table name, so producer and consumer stay in sync.
    try:
        return int(interface[len('wwan'):]) + 1
    except (ValueError, IndexError):
        return 1


# WLB (src/helpers/vyos-load-balancer.py) assigns every interface-health member
# a policy-routing table and matching fwmark starting at this offset.
WLB_MARK_OFFSET = 0xc8


def wlb_interface_mark(interface, config):
    """Return the hex fwmark WLB marks packets with for ``interface``, or None.

    Must mirror src/helpers/vyos-load-balancer.py exactly: each
    ``interface-health`` member, in config order, is assigned
    ``table/mark = WLB_MARK_OFFSET + its 1-based position``.  The dial-on-demand
    trigger uses this to fire on precisely the traffic WLB has steered to the
    interface (see generate_nft_rules), so producer and consumer agree without a
    runtime handshake.  Returns None when the interface is not an
    interface-health member (no WLB mark exists).
    """
    for index, ifname in enumerate(config.get('interface_health') or {}, start=1):
        if ifname == interface:
            return hex(WLB_MARK_OFFSET + index)
    return None


def basic_nft_commands(interface, queue_num):
    # Drop every IPv6 frame egressing wwanX BEFORE the NFQUEUE rule so IPv6
    # chatter is discarded instead of being queued (queuing would trigger /
    # hold a dial-on-demand bring-up).
    return f'''
    add table inet {interface}_raw_{queue_num}
    add chain inet {interface}_raw_{queue_num} output {{ type filter hook output priority raw; policy accept; }}
    add rule inet {interface}_raw_{queue_num} output oifname {interface} meta nfproto ipv6 drop
    add rule inet {interface}_raw_{queue_num} output oifname {interface} queue num {queue_num}
    add chain inet {interface}_raw_{queue_num} prerouting {{ type filter hook prerouting priority raw; policy accept; }}
    '''


async def generate_standalone_nft_rules(interface='wwan0'):
    # Standalone dial-on-demand (no load-balancing wan rules): intercept
    # router-originated (local) traffic leaving the wwan interface via the RAW
    # output hook, and divert LAN->WAN routed traffic whose FIB egress resolves
    # to the wwan interface via the RAW prerouting hook. Both go to NFQUEUE so
    # the on-demand trigger can bring the modem up.
    queue_num = wwan_queue_num(interface)
    physical_interfaces = f"{{ {', '.join(get_inbound_interfaces())} }}"
    commands = basic_nft_commands(interface, queue_num)
    # NOTE: forwarded IPv6 destined for wwanX is dropped persistently by the
    # dial-on-demand prerouting guard in src/conf_mode/interfaces_wwan.py
    # (below `raw` priority, so before this queue), not here.
    commands += (
        f'add rule inet {interface}_raw_{queue_num} prerouting '
        f'iifname {physical_interfaces} fib daddr oifname "{interface}" '
        f'queue num {queue_num}\n'
    )
    try:
        logger.info("Standalone command to install: %s", commands)
        await async_run_nft_cmd(commands)
        logger.info("Generated standalone nft rules...installing now")
    except asyncio.CancelledError:
        raise


async def generate_nft_rules(interface='wwan0'):
    logger.info("Start generating nft rules.")
    config = get_config()
    matching = {}
    interface_test = {}
    if config.get('interface_health') is not None:
        interface_test = config.get('interface_health', {}).get(interface, {})
    tests = interface_test.get("test", {})
    for rule_num, rule in config.get('rule', {}).items():
        if interface_test is not None:
            matching[rule_num] = [
                iface for iface in config.get('interface_health', {})
                if rule_num in tests and iface in rule.get('interface', {})
            ]

    # No load-balancing wan rules: install standalone RAW intercept rules so
    # dial-on-demand still works without WLB failover/load-sharing configured.
    if not matching:
        await generate_standalone_nft_rules(interface)
        return

    # One NFQUEUE (and therefore one '{interface}_raw_{queue}' table) per WWAN
    # interface, regardless of how many WLB rules reference it. Using the shared
    # per-interface queue number keeps the id unique across the namespace (no
    # collision with another WWAN, standalone or WLB) and lets the consumer bind
    # the single queue and tear down the single table. All rules for this
    # interface add their prerouting diversions into that one table/queue.
    queue_num = wwan_queue_num(interface)
    commands = basic_nft_commands(interface, queue_num)
    # fwmark WLB steers to this interface; drives the failover trigger below.
    mark = wlb_interface_mark(interface, config)
    demand_chain_added = False

    failover = False
    for rule_num in matching:
        rule_conf = config.get('rule', {}).get(rule_num, {})
        if rule_conf.get('failover', {}) is not None:
            failover = True
        inbound_interface = rule_conf.get('inbound_interface', {})

        if failover is False or inbound_interface == 'any':
            scope = f"{{ {', '.join(get_inbound_interfaces())} }}"
        else:
            scope = f"{{ {inbound_interface} }}"
        commands += (
            f'add rule inet {interface}_raw_{queue_num} prerouting '
            f'iifname {scope} fib daddr oifname "{interface}" '
            f'queue num {queue_num}\n'
        )

        # Under WLB the FIB rule above cannot bootstrap an idle on-demand bearer.
        # WLB steers traffic to a failover member by fwmark + policy routing, but
        # that RAW-hook (priority raw, -300) FIB lookup runs BEFORE WLB sets the
        # mark (mangle, -150) and so resolves against the main table -- which
        # does not point at the idle failover WWAN. So forwarded traffic is never
        # queued and dial-on-demand can never raise the link; the old design
        # worked around this by pinning the bearer up with a continuous lease.
        #
        # Instead, add a second trigger at a priority AFTER WLB marking that
        # fires on exactly the traffic WLB marked for this interface. Now when
        # WLB selects an idle dial-on-demand WWAN as the active egress, the first
        # marked packet is queued, the modem dials on demand, and the metered
        # bearer idles back down between flows (vyos-load-balancer.py no longer
        # leases dial-on-demand). The whole '{interface}_raw_{queue}' table is
        # torn down once connected (wwan_nft_reconnect), so established traffic
        # is never queued.
        if 'failover' in rule_conf and mark is not None:
            if not demand_chain_added:
                commands += (
                    f'add chain inet {interface}_raw_{queue_num} wlb_demand '
                    f'{{ type filter hook prerouting priority -140; policy accept; }}\n'
                )
                demand_chain_added = True
            commands += (
                f'add rule inet {interface}_raw_{queue_num} wlb_demand '
                f'iifname {scope} meta mark {mark} queue num {queue_num}\n'
            )

    try:
        logger.info("Command to install: %s", commands)
        await async_run_nft_cmd(commands)
        logger.info("Generated nft rules...installing now")
    except asyncio.CancelledError:
        raise


if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog="install-wwan-nft-rules", description="installs the wwan nft rules", epilog="Test")
    parser.add_argument('interface',nargs="?", default="wwan0")
    args = parser.parse_args()
    interface = args.interface
    asyncio.run(generate_nft_rules(interface))
