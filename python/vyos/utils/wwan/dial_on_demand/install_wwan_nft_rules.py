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


def basic_nft_commands(interface, queue_num):
    # Drop IPv6 multicast (ff00::/8 -- RA/RS/MLD/ND-solicit/mDNS chatter) BEFORE
    # the NFQUEUE rule so housekeeping can never wake / hold a dial-on-demand
    # bring-up. IPv6 *unicast* (global/ULA) falls through to the queue exactly
    # like IPv4, so real forwarded or locally-originated IPv6 traffic triggers
    # the dial. Link-local and inbound RAs are already neutralised at the source
    # by the FSM carrier hardening (accept_ra=0, autoconf=0) + output hygiene.
    return f'''
    add table inet {interface}_raw_{queue_num}
    add chain inet {interface}_raw_{queue_num} output {{ type filter hook output priority raw; policy accept; }}
    add rule inet {interface}_raw_{queue_num} output oifname {interface} ip6 daddr ff00::/8 drop
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
    # The prerouting queue is family-agnostic (inet + fib): forwarded IPv4 AND
    # forwarded IPv6 *unicast* whose FIB egress resolves to wwanX are queued, so
    # either family triggers a dial. IPv6 *multicast* never FIB-resolves to the
    # point-to-point wwanX egress, and the dial-on-demand chatter guard in
    # src/conf_mode/interfaces_wwan.py drops it below `raw` as a backstop.
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
