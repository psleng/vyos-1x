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

from sys import exit

from vyos.config import Config
from vyos.configdep import set_dependents, call_dependents
from vyos.utils.dict import dict_search_args
from vyos.utils.process import cmd
from vyos import ConfigError
from vyos import airbag
from vyos.utils.process import call
from vyos.configdict import get_interface_dict
from systemd import journal


airbag.enable()

service = 'vyos-wan-load-balance.service'

valid_groups = [
    'address_group',
    'domain_group',
    'network_group',
    'port_group'
]

def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()

    base = ['load-balancing', 'wan']

    lb = conf.get_config_dict(base, key_mangling=('-', '_'),
                              no_tag_node_value_mangle=True,
                              get_first_key=True,
                              with_recursive_defaults=True)

    if lb:
        lb['firewall_group'] = conf.get_config_dict(['firewall', 'group'], key_mangling=('-', '_'), get_first_key=True,
                                        no_tag_node_value_mangle=True)

    # prune limit key if not set by user
    for rule in lb.get('rule', []):
        if lb.from_defaults(['rule', rule, 'limit']):
            del lb['rule'][rule]['limit']

    set_dependents('conntrack', conf)

    if lb:
        # WWAN interfaces whose dial-on-demand service must be restarted to
        # pick up a load-balancing configuration change (e.g. inbound-interface).
        lb['restart_dial_on_demand'] = _dial_on_demand_restart_interfaces(conf, lb)

    return lb


def verify(lb):
    if not lb:
        return None

    if 'interface_health' in lb:
        for ifname, health_conf in lb['interface_health'].items():
            if 'nexthop' not in health_conf:
                raise ConfigError(f'Nexthop must be configured for interface {ifname}')

            if 'test' not in health_conf:
                continue

            for test_id, test_conf in health_conf['test'].items():
                if 'type' not in test_conf:
                    raise ConfigError(f'No type configured for health test on interface {ifname}')

                if test_conf['type'] == 'user-defined' and 'test_script' not in test_conf:
                    raise ConfigError(f'Missing user-defined script for health test on interface {ifname}')
    else:
        raise ConfigError('Interface health tests must be configured')

    if 'rule' in lb:
        for rule_id, rule_conf in lb['rule'].items():
            if 'interface' not in rule_conf and 'exclude' not in rule_conf:
                raise ConfigError(f'Interface or exclude not specified on load-balancing wan rule {rule_id}')

            if 'failover' in rule_conf and 'exclude' in rule_conf:
                raise ConfigError(f'Failover cannot be configured with exclude on load-balancing wan rule {rule_id}')

            if 'limit' in rule_conf:
                if 'exclude' in rule_conf:
                    raise ConfigError(f'Limit cannot be configured with exclude on load-balancing wan rule {rule_id}')

                if 'rate' in rule_conf['limit'] and 'period' not in rule_conf['limit']:
                    raise ConfigError(f'Missing "limit period" on load-balancing wan rule {rule_id}')

                if 'period' in rule_conf['limit'] and 'rate' not in rule_conf['limit']:
                    raise ConfigError(f'Missing "limit rate" on load-balancing wan rule {rule_id}')

            for direction in ['source', 'destination']:
                if direction in rule_conf:
                    side_conf = rule_conf[direction]

                    if 'group' in side_conf:
                        if len({'address_group', 'network_group', 'domain_group'} & set(side_conf['group'])) > 1:
                            raise ConfigError('Only one address-group, network-group or domain-group can be specified')

                        for group in valid_groups:
                            if group in side_conf['group']:
                                group_name = side_conf['group'][group]
                                error_group = group.replace("_", "-")

                                if group in ['address_group', 'network_group', 'domain_group']:
                                    if 'address' in side_conf:
                                        raise ConfigError(f'{error_group} and address cannot both be defined')

                                if group in ['port_group']:
                                    if 'port' in side_conf:
                                        raise ConfigError(f'{error_group} and port cannot both be defined')

                                if group_name and group_name[0] == '!':
                                    group_name = group_name[1:]

                                group_obj = dict_search_args(lb['firewall_group'], group, group_name)

                                if group_obj is None:
                                    raise ConfigError(f'Invalid {error_group} "{group_name}" on load-balancing wan rule')

                                if not group_obj:
                                    Warning(f'{error_group} "{group_name}" has no members!')

                    if dict_search_args(side_conf, 'group', 'port_group'):
                        if 'protocol' not in rule_conf:
                            raise ConfigError('Protocol must be defined if specifying a port-group')

                        if rule_conf['protocol'] not in ['tcp', 'udp', 'tcp_udp']:
                            raise ConfigError('Protocol must be tcp, udp, or tcp_udp when specifying a port-group')

                    if 'port' in rule_conf[direction]:
                        if 'protocol' not in rule_conf:
                            raise ConfigError(f'Protocol required to specify port on load-balancing wan rule {rule_id}')

                        if rule_conf['protocol'] not in ['tcp', 'udp', 'tcp_udp']:
                            raise ConfigError(f'Protocol must be tcp, udp or tcp_udp to specify port on load-balancing wan rule {rule_id}')

def generate(lb):
    return None


def _dial_on_demand_restart_interfaces(conf, lb):
    """Return dial-on-demand WWAN interfaces whose relevant WAN load-balancing
    configuration changed since the last commit.

    The dial-on-demand@<ifname> service builds its dialing behaviour from the
    WAN load-balancing configuration (which inbound interfaces trigger a dial,
    the failover flag, interface membership and the interface-health tests), so
    a change to any of those must restart the service for that interface --
    e.g. when a rule's inbound-interface is changed.
    """
    effective = conf.get_config_dict(['load-balancing', 'wan'],
                                     key_mangling=('-', '_'),
                                     no_tag_node_value_mangle=True,
                                     get_first_key=True,
                                     effective=True)

    # Rule fields that influence when/how the dial-on-demand service dials.
    rule_keys = ('inbound_interface', 'interface', 'failover', 'exclude')

    def relevant(config, ifname):
        health = dict_search_args(config, 'interface_health', ifname)
        rules = {}
        for rule_no, rule_conf in (config.get('rule') or {}).items():
            if ifname in (rule_conf.get('interface') or []):
                rules[rule_no] = {key: rule_conf.get(key) for key in rule_keys}
        return {'interface_health': health, 'rule': rules}

    # WWAN interfaces referenced anywhere in the new or previous config.
    candidates = set(lb.get('interface_health') or {})
    candidates |= set(effective.get('interface_health') or {})
    for config in (lb, effective):
        for rule_conf in (config.get('rule') or {}).values():
            candidates |= set(rule_conf.get('interface') or [])

    restart = []
    for ifname in sorted(candidates):
        if not ifname.startswith('wwan'):
            continue
        # Only interfaces configured for dial-on-demand run the service.
        if not conf.exists(['interfaces', 'wwan', ifname,
                            'connection-mode', 'dial-on-demand']):
            continue
        if relevant(lb, ifname) != relevant(effective, ifname):
            restart.append(ifname)

    return restart


def apply(lb):
    if not lb:
        cmd(f'systemctl stop {service}')
    else:
        cmd(f'systemctl restart {service}')
        # Restart the dial-on-demand service for any WWAN interface whose
        # load-balancing configuration changed, so it picks up the change.
        for ifname in lb.get('restart_dial_on_demand', []):
            call(f'systemctl restart dial-on-demand@{ifname}:*.service')

    call_dependents()

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
