import subprocess
import argparse
from vyos.config import Config
from pathlib import Path
import subprocess

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

def run_cmd(cmd=''):
    subprocess.run(
        ['nft', '-f', '-'],
        input=cmd,
        text=True,
        check=True
    )

def generate_nft_rules(interface='wwan0'):
    config = get_config()
    print(config)
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

    failover = False

    commands = f''''''
    for rule_num in matching:
        basic_nft_commands = f'''
        add table inet wwan_raw_{rule_num}
        add chain inet wwan_raw_{rule_num} output {{ type filter hook output priority raw; policy accept; }}
        add rule inet wwan_raw_{rule_num} output oifname {interface} queue num {rule_num}
        add chain inet wwan_raw_{rule_num} prerouting {{ type filter hook prerouting priority raw; policy accept; }}
        add rule inet wwan_raw_{rule_num} prerouting queue num {rule_num}
        '''
        if config.get('rule', {}).get(rule_num, {}).get('failover', {}) is not None:
            failover = True
        inbound_interface = config.get('rule', {}).get(rule_num, {}).get('inbound_interface', {})

        if failover is False or inbound_interface == 'any':
            physical_interfaces = f"{{ {', '.join(get_physical_interfaces())} }}"
            commands = f'''add rule inet wwan_raw_{rule_num} prerouting iifname {physical_interfaces} fib daddr oifname "{interface}" queue num {rule_num}
            '''
        else:
            commands = f'''add rule inet wwan_raw_{rule_num} prerouting iifname {{ {inbound_interface} }} fib daddr oifname "{interface}" queue num {rule_num}
            '''
        run_cmd(basic_nft_commands + commands)


if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog="install-wwan-nft-rules", description="installs the wwan nft rules", epilog="Test")
    parser.add_argument('interface',nargs="?", default="wwan0")
    args = parser.parse_args()
    interface = args.interface
    generate_nft_rules(interface)
