import subprocess
import argparse
from vyos.config import Config
from pathlib import Path
import subprocess
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

def run_nft_cmd(cmd=''):
    subprocess.run(
        ['nft', '-f', '-'],
        input=cmd,
        text=True,
        check=True
    )

async def async_run_nft_cmd(cmd=''):
    proc = await asyncio.create_subprocess_exec(
        "nft", "-f", "-", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()


def standalone_queue_num(interface):
    # When no load-balancing wan rule governs this interface there is no user
    # rule number to reuse as the NFQUEUE id, so derive a deterministic one from
    # the wwan index (wwan0 -> 1, wwan1 -> 2, ...). The consumer reads the queue
    # number back from the '{interface}_raw_{queue}' table name, so producer and
    # consumer stay in sync.
    try:
        return int(interface[len('wwan'):]) + 1
    except (ValueError, IndexError):
        return 1


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
    queue_num = standalone_queue_num(interface)
    physical_interfaces = f"{{ {', '.join(get_physical_interfaces())} }}"
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

    failover = False

    commands = f''''''
    for rule_num in matching:
        basic_nft_commands = f'''
        add table inet {interface}_raw_{rule_num}
        add chain inet {interface}_raw_{rule_num} output {{ type filter hook output priority raw; policy accept; }}
        add rule inet {interface}_raw_{rule_num} output oifname {interface} queue num {rule_num}
        add chain inet {interface}_raw_{rule_num} prerouting {{ type filter hook prerouting priority raw; policy accept; }}
        '''
        if config.get('rule', {}).get(rule_num, {}).get('failover', {}) is not None:
            failover = True
        inbound_interface = config.get('rule', {}).get(rule_num, {}).get('inbound_interface', {})

        if failover is False or inbound_interface == 'any':
            physical_interfaces = f"{{ {', '.join(get_physical_interfaces())} }}"
            commands = f'''add rule inet {interface}_raw_{rule_num} prerouting iifname {physical_interfaces} fib daddr oifname "{interface}" queue num {rule_num}
            '''
        else:
            commands = f'''add rule inet {interface}_raw_{rule_num} prerouting iifname {{ {inbound_interface} }} fib daddr oifname "{interface}" queue num {rule_num}
            '''
        try:
            logger.info("Command to install: %s", basic_nft_commands + commands)
            await async_run_nft_cmd(basic_nft_commands + commands)
            logger.info("Generated nft rules...installing now")
        except asyncio.CancelledError:
            raise


if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog="install-wwan-nft-rules", description="installs the wwan nft rules", epilog="Test")
    parser.add_argument('interface',nargs="?", default="wwan0")
    args = parser.parse_args()
    interface = args.interface
    asyncio.run(generate_nft_rules(interface))
