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


airbag.enable()

service = 'vyos-wan-load-balance.service'

# Desired dial-on-demand service state for each affected WWAN interface,
# computed in get_config() (where the sole Config instance is live) and
# consumed in apply().  Kept module-level so apply() can act on it even when
# the load-balancing config was deleted (in which case get_config() returns an
# empty, falsy `lb`).  Reset on every get_config() call.
_dial_on_demand_plan = []

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

    # Re-evaluate the dial-on-demand service policy for the WWAN interfaces this
    # commit can affect.  Computed here, where the sole Config instance is live,
    # and stored module-level so apply() can act on it even when `lb` is empty
    # (load-balancing deleted).
    global _dial_on_demand_plan
    _dial_on_demand_plan = _build_dial_on_demand_plan(conf)

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


def _dial_on_demand_should_run(conf, ifname):
    """Decide whether the ``dial-on-demand@<ifname>`` service should run.

    At commit time this script reaches across into the WWAN interface config
    (``conf`` reflects the configuration being committed) and applies the
    policy:

    * ``connection-mode`` must be ``dial-on-demand`` -- otherwise the service
      never runs.
    * When the interface is governed by WAN load-balancing -- it is both an
      ``interface-health`` target AND a weighted member of a balancing
      ``rule`` -- the service runs ONLY when that rule has ``failover``
      enabled.  A plain weighted load-balance member is expected to stay
      continuously up, so dial-on-demand does not apply.
    * A standalone dial-on-demand interface (not referenced by WAN
      load-balancing) always runs.

    interfaces_wwan applies the identical check from its own file so both
    commit paths agree regardless of which section changes.
    """
    if not ifname.startswith('wwan'):
        return False

    if conf.exists(['interfaces', 'wwan', ifname, 'disable']):
        return False

    if conf.return_value(['interfaces', 'wwan', ifname,
                          'connection-mode']) != 'dial-on-demand':
        return False

    lb = conf.get_config_dict(['load-balancing', 'wan'],
                              key_mangling=('-', '_'),
                              no_tag_node_value_mangle=True,
                              get_first_key=True)

    in_interface_health = ifname in (lb.get('interface_health') or {})

    weighted_member = False
    failover_rule = False
    for rule_conf in (lb.get('rule') or {}).values():
        if ifname in (rule_conf.get('interface') or {}):
            weighted_member = True
            if 'failover' in rule_conf:
                failover_rule = True

    if in_interface_health and weighted_member:
        # Governed by WLB: only dial on demand for a failover rule.
        return failover_rule

    # Standalone dial-on-demand: always run.
    return True


def _build_dial_on_demand_plan(conf):
    """Return the desired dial-on-demand service state for every WWAN interface
    whose dialing behaviour can be affected by this load-balancing commit.

    The ``dial-on-demand@<ifname>`` service is started/stopped by policy
    (:func:`_dial_on_demand_should_run`) that depends on BOTH the WWAN interface
    config and the WAN load-balancing config.  Because the two sections can be
    committed independently, a load-balancing commit must re-evaluate the policy
    for the affected interfaces -- e.g. adding a WWAN to a weighted
    (non-failover) rule must stop its service, enabling failover must start it,
    and deleting the load-balancing config must let a now standalone
    dial-on-demand WWAN start again.

    Candidates are the WWAN interfaces referenced by the new OR the previous
    (effective) load-balancing config -- the set whose policy result may have
    changed.  For each, the plan records whether the service should run, whether
    the dialing-relevant load-balancing config changed this commit, and the
    systemd instance parameters (idle/connect timeouts) needed to start it.

    The ``changed`` flag drives start vs. restart in apply(): when the relevant
    config changed, the running service must be restarted to pick up the new
    configuration; otherwise a plain (idempotent) start keeps it running without
    needlessly bouncing an active bearer on unrelated commits.
    """
    effective = conf.get_config_dict(['load-balancing', 'wan'],
                                     key_mangling=('-', '_'),
                                     no_tag_node_value_mangle=True,
                                     get_first_key=True,
                                     effective=True)
    proposed = conf.get_config_dict(['load-balancing', 'wan'],
                                    key_mangling=('-', '_'),
                                    no_tag_node_value_mangle=True,
                                    get_first_key=True)

    # Dialing-relevant view of a WWAN interface's load-balancing config, used to
    # detect whether a restart is needed: interface-health membership plus the
    # rule fields that influence when/how the service dials.
    rule_keys = ('inbound_interface', 'interface', 'failover', 'exclude')

    def relevant(config, ifname):
        health = (config.get('interface_health') or {}).get(ifname)
        rules = {}
        for rule_no, rule_conf in (config.get('rule') or {}).items():
            if ifname in (rule_conf.get('interface') or {}):
                rules[rule_no] = {key: rule_conf.get(key) for key in rule_keys}
        return {'interface_health': health, 'rule': rules}

    # WWAN interfaces referenced anywhere in the new or previous config.
    candidates = set()
    for config in (proposed, effective):
        candidates |= set(config.get('interface_health') or {})
        for rule_conf in (config.get('rule') or {}).values():
            candidates |= set(rule_conf.get('interface') or [])

    plan = []
    for ifname in sorted(candidates):
        if not ifname.startswith('wwan'):
            continue
        dod_base = ['interfaces', 'wwan', ifname, 'dial-on-demand']
        plan.append({
            'ifname': ifname,
            'run': _dial_on_demand_should_run(conf, ifname),
            'changed': relevant(proposed, ifname) != relevant(effective, ifname),
            'idle_timeout': conf.return_value(dod_base + ['idle-timeout']) or 30,
            'connect_timeout': conf.return_value(dod_base + ['connect-timeout']) or 30,
        })

    return plan


def _apply_dial_on_demand_plan():
    """Start, restart or stop each WWAN dial-on-demand service per the plan.

    * should run and relevant config changed -> restart (pick up the change).
    * should run and unchanged -> start (idempotent; ensures it is running
      without bouncing an already-running bearer).
    * should not run -> stop every instance for the interface.
    """
    for item in _dial_on_demand_plan:
        ifname = item['ifname']
        if item['run']:
            action = 'restart' if item['changed'] else 'start'
            call(f'systemctl {action} dial-on-demand@{ifname}:'
                 f'{item["idle_timeout"]}:{item["connect_timeout"]}.service')
        else:
            call(f'systemctl stop dial-on-demand@{ifname}:*.service')


def apply(lb):
    if not lb:
        cmd(f'systemctl stop {service}')
    else:
        cmd(f'systemctl restart {service}')

    # Re-evaluate the dial-on-demand service policy for affected WWAN
    # interfaces (runs regardless of whether load-balancing itself is
    # configured, so deleting load-balancing can restart a now-standalone
    # dial-on-demand WWAN).
    _apply_dial_on_demand_plan()

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
