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

import os
import re
import json
import shutil
import ipaddress

from sys import exit
from time import sleep
from pathlib import Path
from vyos.hardware import api as hw

from vyos.config import Config
from vyos.utils.dict import dict_search
from vyos.utils.dict import dict_search_args
from vyos.utils.process import cmd
from vyos.utils.process import is_systemd_service_active
from vyos.utils.serial import send_command_to_iolan
from vyos.utils.serial import find_all_ttyS_devices_without_console
from vyos import ConfigError
from vyos.tpm import tpm_enabled
from vyos.tpm_pki import get_path_str
from vyos.tpm_pki import validate_certificate_against_tpm_priv_key

from vyos.pki import wrap_certificate
from vyos.pki import wrap_private_key

from vyos.configdict import node_changed
from vyos.configdict import is_node_changed
from vyos.configdict import dict_merge
from vyos.configdict import get_interface_dict
from vyos.configdep import called_as_dependent

CERT_PATH = Path('/run/vyos_pki')
SERIAL_PATH = Path('/run/serial')
SERIAL_SERVICE = 'iolan-monitor.service'

PROC = Path('/proc')
LOGIN_SERVICE_EXE = 'iol_direct'

def _resolve_hw_serial_port(port):
    if not port:
        raise ValueError('serial protocol requires a port')
    if port.startswith('/'):
        return hw.serial_port_for_tty(port)
    if port.startswith('ttyS'):
        return hw.serial_port_for_tty(f'/dev/{port}')
    return port.upper()


def _apply_serial_protocol(port, protocol, termination=None, slew_rate=None):
    port_name = _resolve_hw_serial_port(port)
    term = None if termination is None else str(termination).lower() in (
        'on', 'true', '1', 'enable', 'enabled'
    )
    slr = None if slew_rate is None else str(slew_rate).lower() in (
        'on', 'true', '1', 'enable', 'enabled'
    )
    hw.serial_protocol(port_name, protocol, term=term, slr=slr)


def serial_protocol(args):
    port = getattr(args, 'port', '')
    termination = getattr(args, 'termination', None)
    slew_rate = getattr(args, 'slew_rate', None)
    _apply_serial_protocol(port, args.protocol, termination=termination, slew_rate=slew_rate)


def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()

    tag_value = os.environ.get('VYOS_TAGNODE_VALUE', '')
    if called_as_dependent():
        invoke_context = 'dependent'
    elif re.fullmatch(r'(slttys|pppttys)\d+', tag_value):
        invoke_context = 'interface'
    elif re.fullmatch(r'ttyS\d+', tag_value):
        invoke_context = 'serial'
    else:
        invoke_context = 'serial'

    if invoke_context == 'interface':
        interface_type = 'ppp' if tag_value.startswith('pppttys') else 'slip'
        base = ['interfaces', 'ppp-tty' if interface_type == 'ppp' else 'slip-tty', tag_value]
        _, result = get_interface_dict(conf, base[:-1], tag_value)
        result['interface_type'] = interface_type
        if interface_type == 'ppp':
            result['authentication_protocol_selected'] = [
                proto for proto in ['chap', 'pap', 'none']
                if conf.exists(base + ['authentication', 'protocol', proto])
            ]
        if 'deleted' in result and 'device' not in result:
            device = conf.return_effective_value(base + ['device'])
            if device:
                result['device'] = device

        device = result.get('device')
        serial_service = None
        if device:
            if conf.exists(['service', 'serial', 'device', device, 'ppp']):
                serial_service = 'ppp'
            elif conf.exists(['service', 'serial', 'device', device, 'slip']):
                serial_service = 'slip'

            other_type = 'slip' if interface_type == 'ppp' else 'ppp'
            other_base = ['interfaces', 'slip-tty' if interface_type == 'ppp' else 'ppp-tty']
            for other_if in conf.list_nodes(other_base):
                other_device = conf.return_value(other_base + [other_if, 'device'])
                if other_device == device:
                    result['interface_conflict'] = f'interfaces {other_base[1]} {other_if}'
                    result['conflicting_interface'] = f'interfaces {base[1]} {tag_value}'
                    result['conflicting_interface_type'] = other_type
                    break
        result['serial_service'] = serial_service
    else:
        result = get_serial_config(conf)
    result['_invoke_context'] = invoke_context
    # result.update(service_flags)
    return result


def get_interface_config(conf, device, interface_type):
    '''
    Get config for PPP or SLIP interface that uses the specified device.
    Also detects if the interface config has changed.
    Returns (interface_config, is_changed) tuple.
    '''
    if interface_type == 'ppp':
        base = ['interfaces', 'ppp-tty']
    else:
        base = ['interfaces', 'slip-tty']

    # Find the interface that uses this device
    interfaces = conf.list_nodes(base)
    for ifname in interfaces:
        if_device = conf.return_value(base + [ifname, 'device'])
        if if_device == device:
            # Found the interface, gather its config
            config = conf.get_config_dict(
                base + [ifname],
                key_mangling=('-', '_'),
                get_first_key=True,
                with_recursive_defaults=True
            )
            config['ifname'] = ifname
            config['interface_type'] = interface_type
            config['device'] = device
            if interface_type == 'ppp':
                config['authentication_protocol_selected'] = [
                    proto for proto in ['chap', 'pap', 'none']
                    if conf.exists(base + [ifname, 'authentication', 'protocol', proto])
                ]

            # Check if interface config changed
            is_changed = is_node_changed(conf, base + [ifname])

            # Check for conflict with other interfaces using same device
            for other_if in interfaces:
                if other_if == ifname:
                    continue
                other_device = conf.return_value(base + [other_if, 'device'])
                if other_device == device:
                    config['interface_conflict'] = f'{base[0]} {base[1]} {other_if}'
                    config['conflicting_interface'] = f'{base[0]} {base[1]} {ifname}'
                    break

            # Check SLIP interfaces too if this is PPP (and vice versa)
            other_type = 'slip-tty' if interface_type == 'ppp' else 'ppp-tty'
            other_base = ['interfaces', other_type]
            for other_if in conf.list_nodes(other_base):
                other_device = conf.return_value(other_base + [other_if, 'device'])
                if other_device == device:
                    # Store the OTHER interface as the conflict (the one already using the device)
                    config['interface_conflict'] = f'interfaces {other_type} {other_if}'
                    # Also store which interface has the conflict for clearer error message
                    config['conflicting_interface'] = f'interfaces {base[1]} {ifname}'
                    break

            return config, is_changed

    # No interface found for this device
    return None, False


def get_serial_config(conf):
    '''
    Get config for serial devices.
    '''
    base = ['service', 'serial']

    proxy = conf.get_config_dict(base, key_mangling=('-', '_'),
                                     no_tag_node_value_mangle=True,
                                     get_first_key=True,
                                     with_defaults=False,
                                     with_recursive_defaults=False,
                                     with_pki=True)

    # Process profile settings for devices
    # If a device has a profile set, merge profile settings as base, then overlay device settings
    profiles = proxy.get('profile', {}).get('tty_profile', {})
    missing_profiles = {}
    if 'device' in proxy:
        for device, device_conf in proxy['device'].items():
            if 'profile' in device_conf:
                profile_name = device_conf['profile']
                if profile_name in profiles:
                    profile_conf = profiles[profile_name]
                    # Start with profile settings as base, overlay device settings
                    # Remove 'profile' key from merge as it's just a reference
                    device_overlay = {k: v for k, v in device_conf.items() if k != 'profile'}
                    merged_conf = dict_merge(profile_conf, device_overlay)
                    # Keep profile reference for debugging/logging
                    merged_conf['_applied_profile'] = profile_name
                    proxy['device'][device] = merged_conf
                else:
                    # Profile doesn't exist - track for validation
                    missing_profiles[device] = profile_name

    if missing_profiles:
        proxy['missing_profile'] = missing_profiles

    # Track restart reasons for each device: {device: [reasons]}
    restart_reasons = {}

    def add_restart(device, reason):
        if device not in restart_reasons:
            restart_reasons[device] = []
        restart_reasons[device].append(reason)

    for device in proxy.get('device', []):
        # Want to restart serial if its config changed
        tmp = is_node_changed(conf, base + ['device', device])
        if tmp:
            add_restart(device, 'device config changed')

    # Check if flush-on-close changed - affects certain services
    flush_on_close_changed = is_node_changed(conf, base + ['global-parameters', 'flush-on-close'])
    if flush_on_close_changed:
        # Services affected: datalogging, multihost, tcp client, trueport, ssh server, telnet server
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue

            should_restart = False
            if 'trueport' in device_conf:
                tp_conf = device_conf['trueport']
                if 'serial_buffering' in tp_conf:
                    should_restart = True
                # trueport client or multihost (server with allow_multiple_connections)
                if 'client' in tp_conf:
                    should_restart = True
                elif 'server' in tp_conf and 'allow_multiple_connections' in tp_conf.get('server', {}):
                    should_restart = True
            elif 'tcp' in device_conf:
                tcp_conf = device_conf['tcp']
                if 'serial_buffering' in tcp_conf:
                    should_restart = True
                # tcp client or multihost (server with allow_multiple_connections)
                if 'client' in tcp_conf:
                    should_restart = True
                elif 'server' in tcp_conf and 'allow_multiple_connections' in tcp_conf.get('server', {}):
                    should_restart = True
            elif 'ssh' in device_conf:
                ssh_conf = device_conf['ssh']
                if 'server' in ssh_conf:
                    should_restart = True
            elif 'telnet' in device_conf:
                telnet_conf = device_conf['telnet']
                if 'server' in telnet_conf:
                    should_restart = True
            if should_restart:
                add_restart(device, 'flush-on-close changed')

    # Check if modbus-gateway global settings changed - affects all modbus devices
    modbus_global_changed = is_node_changed(conf, base + ['global-parameters', 'modbus-gateway'])
    if modbus_global_changed:
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue
            if 'modbus_gateway' in device_conf:
                add_restart(device, 'modbus-gateway global changed')

    # Check if process-break changed - affects ssh server, telnet server, trueport
    process_break_changed = is_node_changed(conf, base + ['global-parameters', 'process-break'])
    if process_break_changed:
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue
            should_restart = False
            if 'trueport' in device_conf:
                should_restart = True
            elif 'ssh' in device_conf and 'server' in device_conf.get('ssh', {}):
                should_restart = True
            elif 'telnet' in device_conf and 'server' in device_conf.get('telnet', {}):
                should_restart = True
            if should_restart:
                add_restart(device, 'process-break changed')

    # Check if ssh-string changed - affects ssh server only
    ssh_string_changed = is_node_changed(conf, base + ['global-parameters', 'process-break', 'ssh-string'])
    if ssh_string_changed:
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue
            if 'ssh' in device_conf and 'server' in device_conf.get('ssh', {}):
                add_restart(device, 'ssh-string changed')

    # Check if ssh-telnet-server-logging changed - affects ssh server and telnet server
    logging_changed = is_node_changed(conf, base + ['global-parameters', 'ssh-telnet-server-logging'])
    if logging_changed:
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue
            if 'ssh' in device_conf and 'server' in device_conf.get('ssh', {}):
                add_restart(device, 'ssh-telnet-server-logging changed')
            elif 'telnet' in device_conf and 'server' in device_conf.get('telnet', {}):
                add_restart(device, 'ssh-telnet-server-logging changed')

    # Check if tcp-keep-alive changed - affects ports with send-tcp-keepalive set
    tcp_keepalive_changed = is_node_changed(conf, base + ['global-parameters', 'tcp-keep-alive'])
    if tcp_keepalive_changed:
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue
            # Check each service that has send_tcp_keepalive option
            for svc_name in ['trueport', 'tcp', 'telnet', 'ssh', 'serial_tunnel', 'virtual_modem']:
                if svc_name in device_conf:
                    svc_conf = device_conf[svc_name]
                    if 'send_tcp_keepalive' in svc_conf:
                        add_restart(device, 'tcp-keep-alive changed')
                        break

    # Check if trueport global settings changed - affects all trueport devices
    trueport_global_changed = is_node_changed(conf, base + ['global-parameters', 'trueport'])
    if trueport_global_changed:
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue
            if 'trueport' in device_conf:
                add_restart(device, 'trueport global changed')

    # Check if vmodem directory (phone book) changed - restart all virtual modem ports
    vmodem_directory_changed = is_node_changed(conf, base + ['global-parameters', 'virtual-modem', 'directory-entry'])
    if vmodem_directory_changed:
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue
            if 'virtual_modem' in device_conf:
                add_restart(device, 'vmodem directory changed')

    # Check if any TLS template changed - restart ports using that template
    tls_templates = dict_search('global_parameters.tls.template', proxy) or {}
    for template_name in tls_templates:
        template_changed = is_node_changed(conf, base + ['global-parameters', 'tls', 'template', template_name])
        if template_changed:
            # Find all devices using this template
            for device, device_conf in proxy.get('device', {}).items():
                if device in restart_reasons:
                    continue
                # Check services that support TLS
                for svc_name in ['trueport', 'tcp', 'serial_tunnel', 'virtual_modem', 'modbus_gateway']:
                    if svc_name in device_conf:
                        svc_conf = device_conf[svc_name]
                        if dict_search('tls.template', svc_conf) == template_name:
                            add_restart(device, f'tls template {template_name} changed')
                            break

    # Check if called as dependent from service_ssh - restart all ssh server ports
    if called_as_dependent():
        # When called from service_ssh via dependency, restart all ssh server ports
        for device, device_conf in proxy.get('device', {}).items():
            if device in restart_reasons:
                continue
            if 'ssh' in device_conf and 'server' in device_conf.get('ssh', {}):
                add_restart(device, 'service ssh dependency')

    # Check if any profile changed - restart devices using that profile
    profiles = proxy.get('profile', {}).get('tty_profile', {})
    for profile_name in profiles:
        profile_changed = is_node_changed(conf, base + ['profile', 'tty-profile', profile_name])
        if profile_changed:
            for device, device_conf in proxy.get('device', {}).items():
                if device in restart_reasons:
                    continue
                if device_conf.get('_applied_profile') == profile_name:
                    add_restart(device, f'profile {profile_name} changed')

    # Delete serial port if was deleted from config tree
    tmp = node_changed(conf, base + ['device'])
    # print(f'serial_remove {tmp}')
    if tmp: proxy.update({'serial_remove': tmp})

    # Check if serial device with ppp/slip service has required interface configuration
    # Also gather interface config and detect interface changes
    missing_interfaces = {}
    interface_configs = {}
    for device, device_conf in proxy.get('device', {}).items():
        # Check if device has ppp service configured
        if 'ppp' in device_conf:
            if_config, if_changed = get_interface_config(conf, device, 'ppp')
            if if_config is None:
                missing_interfaces[device] = {'service': 'ppp', 'interface_type': 'ppp-tty'}
            else:
                interface_configs[device] = if_config
                # Add to restart list if interface config changed
                if if_changed and device not in restart_reasons:
                    add_restart(device, 'ppp interface config changed')

        # Check if device has slip service configured
        if 'slip' in device_conf:
            if_config, if_changed = get_interface_config(conf, device, 'slip')
            if if_config is None:
                missing_interfaces[device] = {'service': 'slip', 'interface_type': 'slip-tty'}
            else:
                interface_configs[device] = if_config
                # Add to restart list if interface config changed
                if if_changed and device not in restart_reasons:
                    add_restart(device, 'slip interface config changed')

    if missing_interfaces:
        proxy['missing_interface'] = missing_interfaces

    if interface_configs:
        proxy['interface_config'] = interface_configs

    # Update serial_restart after interface change detection
    if restart_reasons:
        for device, reasons in restart_reasons.items():
            print(f'''DEBUG: serial_restart: {device} - {', '.join(reasons)}''')
        proxy['serial_restart'] = list(restart_reasons.keys())

    return proxy

def get_ppid(pid):
    try:
        for line in (PROC / str(pid) / 'status').read_text().splitlines():
            if line.startswith('PPid:'):
                return int(line.split()[1])
    except Exception:
        return None
    return None

def is_login(pid):
    try:
        return Path(f'/proc/{pid}/exe').resolve().name == LOGIN_SERVICE_EXE
    except Exception:
        return False

def get_ttyS_from_pid(pid):
    fd_dir = PROC/str(pid)/'fd'
    if not fd_dir.exists():
        return None
    try:
        for fd in fd_dir.iterdir():
            try:
                target = fd.readlink()
                if target.name.startswith('ttyS') and target.parent == Path('/dev'):
                    return str(target.name)
            except Exception:
                continue
    except Exception:
        pass

    return None

def resolve_device_from_leaf_pid(pid):
    while pid and pid != 1:
        if is_login(pid):
            return get_ttyS_from_pid(pid)
        pid = get_ppid(pid)
    return None

def get_vbash_pid_from_temp_config_dir():
    temp_config_dir = os.environ.get('VYATTA_TEMP_CONFIG_DIR', '')
    pid_match = re.search(r'new_config_(\d+)$', temp_config_dir)
    if not pid_match:
        return None

    return int(pid_match.group(1))

def verify_mutual_exclusion(config, options, context, require_one=False,
                            missing_msg=None, conflict_msg=None):
    configured = [opt for opt in options if opt in config]

    if require_one and not configured:
        if missing_msg:
            raise ConfigError(missing_msg)
        raise ConfigError(f'{context} requires a service to be configured')

    if len(configured) > 1:
        display_configured = [opt.replace('_', '-') for opt in configured]
        if conflict_msg:
            raise ConfigError(conflict_msg.format(configured=', '.join(display_configured)))
        raise ConfigError(f'{context} has multiple options configured: {", ".join(display_configured)}\n'
                          f'Only one is allowed')

    return configured


def validate_ppp_interface_auth(if_config):
    if if_config.get('interface_type') != 'ppp':
        return

    selected_protocols = if_config.get('authentication_protocol_selected')
    if selected_protocols is None and 'authentication' in if_config and 'protocol' in if_config['authentication']:
        selected_protocols = [
            proto for proto in ['chap', 'pap', 'none']
            if proto in if_config['authentication']['protocol']
            and (proto == 'none' or if_config['authentication']['protocol'][proto])
        ]

    if not selected_protocols:
        raise ConfigError(f'interfaces ppp-tty {if_config.get("ifname", "?")}: authentication protocol required')

    if len(selected_protocols) > 1:
        raise ConfigError(
            f'interfaces ppp-tty {if_config.get("ifname", "?")} authentication protocol: only one of chap, pap, or none allowed')

def verify(config):
    if not config:
        return None
    '''Verify serial device configuration.'''
    invoke_context = config.get('_invoke_context', 'other')
    run_interface_validation = invoke_context in ('interface', 'dependent', 'other')
    run_serial_validation = invoke_context in ('serial', 'dependent', 'other')

    if invoke_context == 'interface':
        if 'deleted' in config:
            return None

        if 'device' not in config:
            raise ConfigError(f'interfaces {config.get("interface_type", "serial")} {config.get("ifname", "?")}: device required')

        if 'interface_conflict' in config:
            conflict_type = config.get('conflicting_interface_type', 'unknown')
            conflict_if = config.get('interface_conflict', 'unknown interface')
            raise ConfigError(
                f'interfaces {config.get("interface_type", "serial")}-tty {config.get("ifname", "?")}: '
                f'tty {config["device"]} is already used by {conflict_if} '
            )

        serial_service = config.get('serial_service')
        if serial_service is None:
            raise ConfigError(
                f'interfaces {config.get("interface_type", "serial")}-tty {config.get("ifname", "?")}: '
                f'tty {config["device"]} is not configured in service serial'
            )

        if serial_service != config.get('interface_type'):
            raise ConfigError(
                f'interfaces {config.get("interface_type", "serial")}-tty {config.get("ifname", "?")}: '
                f'tty {config["device"]} is configured in service serial for {serial_service}, '
                f'not {config.get("interface_type", "serial")}'
            )

        validate_ppp_interface_auth(config)

    if run_serial_validation and 'missing_profile' in config:
        for device, profile_name in config['missing_profile'].items():
            raise ConfigError(f'device {device}: profile "{profile_name}" does not exist')

    if run_serial_validation and 'missing_interface' in config:
        for device, info in config['missing_interface'].items():
            service = info['service']
            interface_type = info['interface_type']
            raise ConfigError(
                f'device {device} {service}: requires "interfaces {interface_type} <name> device {device}"')

    if run_serial_validation and 'interface_config' in config:
        for device, if_config in config['interface_config'].items():
            validate_ppp_interface_auth(if_config)

    if run_interface_validation and 'interface_config' in config:
        for device, if_config in config['interface_config'].items():
            if 'interface_conflict' in if_config:
                conflict_type = if_config.get('conflicting_interface_type')
                interface_type = if_config.get('interface_type', 'unknown')
                if conflict_type and conflict_type != interface_type:
                    conflict_if = if_config.get('interface_conflict', 'unknown interface')
                    raise ConfigError(
                        f'interfaces {interface_type}-tty {if_config.get("ifname", "?")}: '
                        f'tty {device} is already used by {conflict_if} '
                    )

                conflict = if_config['interface_conflict']
                this_if = if_config.get('conflicting_interface', f"interfaces {interface_type}-tty {if_config.get('ifname', '?')}")
                raise ConfigError(f'''device {device}: "{this_if}" conflicts with "{conflict}"''')

            if if_config.get('interface_type') == 'ppp' or if_config.get('interface_type') == 'slip':
                local_addr = if_config.get('local_address')
                remote_addr = if_config.get('remote_address')
                if local_addr and remote_addr and remote_addr != 'negotiation':
                    local_inet = ipaddress.IPv4Interface(local_addr)
                    remote_inet = ipaddress.IPv4Interface(remote_addr)
                    if local_inet.network.prefixlen != remote_inet.network.prefixlen:
                        raise ConfigError(
                            f'interfaces ppp-tty {if_config["ifname"]}: '
                            f'local-address and remote-address must have the same netmask')

    conf_session_device = resolve_device_from_leaf_pid(get_vbash_pid_from_temp_config_dir())

    if run_serial_validation and conf_session_device:
        affected_devices = set(config.get('serial_restart', [])) | set(config.get('serial_remove', []))
        if conf_session_device in affected_devices:
            raise ConfigError(f'cannot modify config on active serial session: {conf_session_device}')

    # global tls template
    if run_serial_validation and 'global_parameters' in config and 'tls' in config['global_parameters']:
        if 'template' not in config['global_parameters']['tls']:
            raise ConfigError('global-parameters tls: template required')
        for template_name, template_conf in config['global_parameters']['tls']['template'].items():
            if template_conf.get('role') == 'server' and tpm_enabled():
                if 'certificate' not in template_conf:
                    # TPM disabled case can use snakeoil cert
                    raise ConfigError(f'tls template "{template_name}": certificate required for TPM server')
                cert_name = template_conf['certificate']
                if not validate_certificate_against_tpm_priv_key(get_path_str('certificate', 'pem', cert_name), get_path_str('certificate', 'key', cert_name)):
                    raise ConfigError(f'tls template "{template_name}": certificate does not match TPM key')

            min_key = int(template_conf.get('min_key_size', 40))
            max_key = int(template_conf.get('max_key_size', 256))
            if max_key < min_key:
                raise ConfigError(f'tls template "{template_name}": max-key-size cannot be less than min-key-size')

    # global vmodem phone list
    vmodem_entries = dict_search('global_parameters.vmodem.directory_entry', config) if run_serial_validation else None
    if vmodem_entries:
        for entry_id, entry_conf in vmodem_entries.items():
            if 'address' not in entry_conf or 'phone_number' not in entry_conf or 'port' not in entry_conf:
                raise ConfigError(f'vmodem directory-entry {entry_id}: address, phone-number, and port required')

    serial_root_config = config

    def find_service_tls_config(svc_conf):
        if not isinstance(svc_conf, dict):
            return None

        for scope in (svc_conf, svc_conf.get('master'), svc_conf.get('slave'),
                      svc_conf.get('client'), svc_conf.get('server')):
            if isinstance(scope, dict) and 'tls' in scope and isinstance(scope['tls'], dict):
                return scope['tls']
        return None

    # Helper function to validate a port/profile configuration
    def validate_port_config(config, context, is_profile=False):
        '''Validate port configuration (used for both devices and profiles).

        For profiles: Only validate mutual exclusion rules (can't have conflicting options).
        For devices: Also validate required fields (ports, addresses, etc.).

        Args:
            config: The config dict
            context: Description string for error messages
            is_profile: True if validating a profile (partial config allowed)
        '''
        # Services - for profiles, service is optional; for devices, required
        valid_services = ['login', 'modbus_gateway', 'nine_bits', 'serial_tunnel', 'ppp', 'remote_printer',
                           'slip', 'ssh', 'tcp', 'telnet', 'udp', 'trueport', 'virtual_modem']

        configured_services = verify_mutual_exclusion(config, valid_services, context,
                                require_one=not is_profile,
                                missing_msg=f'{context}: service must be configured' if not is_profile else None,
                                conflict_msg=f'{context}: only one service allowed, found: {{configured}}')

        if configured_services:
            service = configured_services[0]
            svc_conf = config[service]
            svc_name = service.replace('_', '-')

            # ppp/slip not allowed in profiles (they require interface binding)
            if is_profile and service in ('ppp', 'slip'):
                raise ConfigError(f'{context}: {svc_name} service not allowed in profiles')

            # modbus_gateway uses master/slave instead of server/client
            if service == 'modbus_gateway':
                verify_mutual_exclusion(svc_conf, ['master', 'slave'],
                                        f'{context} {svc_name}',
                                        conflict_msg=f'{context} {svc_name}: only one of master or slave allowed')

            def require_addr_port(cfg, sub_context):
                if 'address' not in cfg or 'port' not in cfg:
                    raise ConfigError(f'{context} {svc_name} {sub_context}: address and port required')

            # Services that can be server or client (mutually exclusive)
            if service in ('serial_tunnel', 'ssh', 'tcp', 'telnet', 'trueport', 'virtual_modem'):
                verify_mutual_exclusion(svc_conf, ['server', 'client'],
                                        f'{context} {svc_name}',
                                        require_one=not is_profile,
                                        missing_msg=f'{context} {svc_name}: server or client required' if not is_profile else None,
                                        conflict_msg=f'{context} {svc_name}: only one of server or client allowed')

                if not is_profile:
                    if 'server' in svc_conf and 'port' not in svc_conf['server']:
                        raise ConfigError(f'{context} {svc_name} server: port required')

                # serial-buffering and allow-multiple-connections are mutually exclusive (server mode)
                # Only trueport and tcp support these modes
                if service in ('trueport', 'tcp') and 'server' in svc_conf:
                    server = svc_conf['server']
                    if 'allow_multiple_connections' in server and 'serial_buffering' in svc_conf:
                        raise ConfigError(f'{context} {svc_name}: serial-buffering and allow-multiple-connections are mutually exclusive!')

                # tcp server: allow-multiple-connections and authenticate-user are mutually exclusive
                if service == 'tcp' and 'server' in svc_conf:
                    server = svc_conf['server']
                    if 'allow_multiple_connections' in server and 'authenticate_user' in server:
                        raise ConfigError(f'{context} {svc_name} server: allow-multiple-connections and authenticate-user are mutually exclusive!')

            # Services requiring remote address/port (nine_bits always, others in client mode)
            if not is_profile:
                if service in ('nine_bits', 'serial_tunnel', 'ssh', 'telnet', 'virtual_modem'):
                    # Check if client is actually configured (for services with client mode)
                    if service == 'nine_bits' or 'client' in svc_conf:
                        base = svc_conf if service == 'nine_bits' else svc_conf.get('client')
                        if base is not None:
                            if 'remote' not in base:
                                raise ConfigError(f'{context} {svc_name}: remote required')
                            require_addr_port(base['remote'], 'remote')

            # Services with multi-host support (primary/backup/multi_host)
            if service in ('trueport', 'tcp') and 'client' in svc_conf:
                client = svc_conf['client']

                if not is_profile:
                    if 'remote' not in client:
                        raise ConfigError(f'{context} {svc_name} client: remote required')

                    remote = client['remote']
                    # primary is only required when multi_host is not configured
                    if 'multi_host' not in remote:
                        if 'primary' not in remote:
                            raise ConfigError(f'{context} {svc_name} client: remote primary required')
                        require_addr_port(remote['primary'], 'client remote primary')

                    if 'backup' in remote:
                        require_addr_port(remote['backup'], 'client remote backup')

                    if 'multi_host' in remote:
                        if 'primary' in remote:
                            raise ConfigError(f'{context} {svc_name} client: remove primary when using multi-host')
                        if 'entry' not in remote['multi_host']:
                            raise ConfigError(f'{context} {svc_name} client: multi-host entry required')
                        for entry_id, entry_cfg in remote['multi_host']['entry'].items():
                            require_addr_port(entry_cfg, f'client multi-host entry {entry_id}')

                # Mutual exclusion check - applies to both profiles and devices
                if 'remote' in client:
                    remote = client['remote']
                    if 'backup' in remote and 'multi_host' in remote:
                        raise ConfigError(f'{context} {svc_name} client: only one of backup or multi-host allowed')

                    # serial-buffering not allowed with backup or multi-host
                    if ('backup' in remote or 'multi_host' in remote) and 'serial_buffering' in svc_conf:
                        raise ConfigError(f'{context} {svc_name}: serial-buffering not allowed with backup or multi-host')

            # TLS template reference validation in service config
            tls_conf = find_service_tls_config(svc_conf)
            if tls_conf is not None:
                if 'template' not in tls_conf:
                    raise ConfigError(f'{context} {svc_name} tls: template required')

                template_name = tls_conf['template']
                available_templates = dict_search('global_parameters.tls.template', serial_root_config) or {}
                if template_name not in available_templates:
                    raise ConfigError(f'{context} {svc_name} tls: template "{template_name}" does not exist')

            # Modem dial validation
            if 'modem' in svc_conf and 'dial' in svc_conf['modem']:
                dial_conf = svc_conf['modem']['dial']
                dial_modes = ['in', 'out', 'both']
                verify_mutual_exclusion(dial_conf, dial_modes,
                                        f'{context} {svc_name} modem dial',
                                        require_one=not is_profile,
                                        missing_msg=f'{context} {svc_name} modem dial: in, out, or both required' if not is_profile else None,
                                        conflict_msg=f'{context} {svc_name} modem dial: only one of in, out, or both allowed')

                # phone-number required for out/both
                for mode in ('out', 'both'):
                    if mode in dial_conf and 'phone_number' not in dial_conf[mode]:
                        raise ConfigError(f'{context} {svc_name} modem dial {mode}: phone-number required')

            # UDP rule validation - at least one rule required, and direction required per rule
            if service == 'udp':
                if 'rule' not in svc_conf:
                    raise ConfigError(f'{context} {svc_name}: at least one rule must be configured')
                if 'rule' in svc_conf:
                    direction_types = ['both', 'lan_serial', 'serial_lan']
                    for rule_id, rule_conf in svc_conf['rule'].items():
                        if 'direction' not in rule_conf:
                            raise ConfigError(f'{context} {svc_name} rule {rule_id}: direction required')
                        verify_mutual_exclusion(rule_conf['direction'], direction_types,
                                                f'{context} {svc_name} rule {rule_id} direction',
                                                require_one=True,
                                                missing_msg=f'{context} {svc_name} rule {rule_id}: direction must be both, lan-serial, or serial-lan',
                                                conflict_msg=f'{context} {svc_name} rule {rule_id}: only one direction allowed, found: {{configured}}')

        # packet forwarding mode validation (mutual exclusion - applies to both)
        if 'packet_forwarding' in config:
            pf_conf = config['packet_forwarding']
            pf_modes = ['minimize_latency', 'optimize_network_throughput',
                        'prevent_message_fragmentation', 'custom']
            verify_mutual_exclusion(pf_conf, pf_modes,
                                    f'{context} packet-forwarding',
                                    conflict_msg=f'{context} packet-forwarding: only one mode allowed, found: {{configured}}')

            if 'custom' in pf_conf:
                verify_mutual_exclusion(pf_conf.get('custom', {}), ['frame', 'packet'],
                                        f'{context} packet-forwarding custom',
                                        require_one=not is_profile,
                                        missing_msg=f'{context} packet-forwarding custom: frame or packet required' if not is_profile else None,
                                        conflict_msg=f'{context} packet-forwarding custom: only one of frame or packet allowed')

        protocol_types = ['rs232', 'rs422', 'rs485_full', 'rs485_half']
        verify_mutual_exclusion(config.get('protocol', {}), protocol_types,
                                f'{context} protocol',
                                require_one=not is_profile,
                                missing_msg=f'{context}: protocol must be configured (rs232, rs422, rs485-full, or rs485-half)' if not is_profile else None,
                                conflict_msg=f'{context}: only one protocol allowed, found: {{configured}}')

        # Flow control validation - only one mode allowed per protocol
        if 'protocol' in config:
            for proto in protocol_types:
                if proto in config['protocol']:
                    proto_conf = config['protocol'][proto]
                    if 'flow_control' in proto_conf:
                        fc_conf = proto_conf['flow_control']
                        # RS232 supports: none, both, hardware, software
                        # RS422/RS485: none, software only
                        if proto == 'rs232':
                            fc_modes = ['none', 'both', 'hardware', 'software']
                        else:
                            fc_modes = ['none', 'software']
                        verify_mutual_exclusion(fc_conf, fc_modes,
                                                f'{context} {proto.replace("_", "-")} flow-control',
                                                conflict_msg=f'{context} {proto.replace("_", "-")} flow-control: only one mode allowed, found: {{configured}}')
                    break

    # Validate profile configurations
    if run_serial_validation:
        profiles = config.get('profile', {}).get('tty_profile', {})
        for profile_name, profile_conf in profiles.items():
            validate_port_config(profile_conf, f'Profile "{profile_name}"', is_profile=True)

    # Validate device configurations
    if run_serial_validation and 'device' in config:
        active_devices = {
            device: device_conf
            for device, device_conf in config['device'].items()
            if 'disable' not in device_conf
        }

        for device, device_conf in active_devices.items():
            validate_port_config(device_conf, f'Serial device {device}', is_profile=False)

        # Ensure local alias/listen addresses are unique across serial ports.
        address_paths = {
            'ssh': ('server', 'address'),
            'tcp': ('server', 'address'),
            'telnet': ('server', 'address'),
            'modbus_gateway': ('slave', 'address'),
        }
        address_owners = {}

        for device, device_conf in active_devices.items():
            for service, path in address_paths.items():
                if service not in device_conf:
                    continue

                current = device_conf[service]
                for key in path:
                    if not isinstance(current, dict) or key not in current:
                        current = None
                        break
                    current = current[key]

                if not isinstance(current, str):
                    continue

                if current in address_owners:
                    owner_device, owner_service = address_owners[current]
                    service_name = service.replace('_', '-')
                    raise ConfigError(
                        f'device {device} {service_name}: address {current} '
                        f'already used by device {owner_device} {owner_service}')

                address_owners[current] = (device, service.replace('_', '-'))

        # Ensure TCP server listen ports do not collide across different services.
        # This uses merged candidate config, so existing committed config is included.
        server_port_paths = {
            'ssh': ('server', 'port'),
            'tcp': ('server', 'port'),
            'telnet': ('server', 'port'),
        }
        port_owners = {}

        for device, device_conf in active_devices.items():
            for service, path in server_port_paths.items():
                if service not in device_conf:
                    continue

                current = device_conf[service]
                for key in path:
                    if not isinstance(current, dict) or key not in current:
                        current = None
                        break
                    current = current[key]

                if current is None:
                    continue

                port = str(current)
                service_name = service.replace('_', '-')
                if port in port_owners:
                    owner_device, owner_service = port_owners[port]
                    if owner_service != service_name:
                        raise ConfigError(
                            f'device {device} {service_name} server: port {port} '
                            f'already used by device {owner_device} {owner_service} server')
                else:
                    port_owners[port] = (device, service_name)

    return None

def prepare_for_json(obj):
    '''Convert empty dicts to 1 for JSON serialization without mutating original.'''
    if isinstance(obj, dict):
        return {k: 1 if v == {} else prepare_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [prepare_for_json(item) for item in obj]
    return obj

def subtract_from_key(items):
    '''Convert 1-based VyOS config keys to 0-based for C arrays.'''
    return {str(int(k) - 1): v for k, v in items.items()}

def set_nested(d, keys, value):
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value

def generate(config):
    if not config:
        return None
    '''Generate serial device configuration.'''
    if config.get('_invoke_context') == 'interface':
        device = config['device']
        interface_type = config['interface_type']
        interface_config = {
            key: value for key, value in config.items()
            if key not in ('_invoke_context', 'ifname', 'interface_type')
        }
        config = {
            '_invoke_context': 'interface',
            'device': {
                device: {
                    interface_type: {}
                }
            },
            'interface_config': {
                device: interface_config
            }
        }

    if 'device' in config:
        for device, serial_config in config['device'].items():
            port_config = serial_config
            ttynum = int(re.findall(r'\d+', device)[0])
            service = ''
            port_config['ttynum'] = ttynum

            if 'global_parameters' in config:
                port_config['global_parameters'] = config['global_parameters']
                logging_conf = port_config['global_parameters'].get('ssh_telnet_server_logging')
                if logging_conf:
                    local_enabled = 1 if ('local' in logging_conf or 'syslog' in logging_conf) else 0
                    remote_enabled = 1 if 'syslog' in logging_conf else 0
                    if 'syslog' in logging_conf:
                        logging_conf['syslog_enable'] = '1'

                    if dict_search('nfs.server.address', logging_conf):
                        logging_conf['port_buffer_remote'] = 1
                        remote_enabled = 1

                    if local_enabled and remote_enabled:
                        logging_conf['mode'] = 'both'
                    elif local_enabled:
                        logging_conf['mode'] = 'local'
                    elif remote_enabled:
                        logging_conf['mode'] = 'remote'

                # Process break configuration - add break_enabled flag
                if 'process_break' in port_config['global_parameters']:
                    pb_conf = port_config['global_parameters']['process_break']
                    if not isinstance(pb_conf, dict):
                        pb_conf = {}
                    pb_conf['break_enabled'] = 1
                    port_config['global_parameters']['process_break'] = pb_conf

            # Find the configured service from port_config keys
            valid_services = ['login', 'modbus_gateway', 'nine_bits', 'serial_tunnel',
                              'ssh', 'tcp', 'telnet', 'udp', 'trueport', 'virtual_modem',
                              'ppp', 'slip', 'remote_printer']
            service = None
            for svc in valid_services:
                if svc in port_config:
                    service = svc
                    break

            if service:
                svc_conf = port_config[service]
                is_server = 'server' in svc_conf
                is_client = 'client' in svc_conf

                # # Save server listen port if configured
                # if is_server and 'port' in svc_conf.get('server', {}):
                #     port_config['listen_port'] = svc_conf['server']['port']

                # login
                if service == 'login':
                    port_config['service'] = 'login'

                # trueport
                elif service == 'trueport':
                    port_config['service'] = 'trueport'
                    if is_server:
                        svc_conf['mode'] = 'server'
                        # Check for multihost via backup host
                        if dict_search('server.allow_multiple_connections', svc_conf) is not None:
                            port_config['service'] = 'multihost'
                    elif is_client:
                        port_config['outbound'] = '1'
                        # Check for backup/multihost configuration
                        if dict_search('client.remote.backup', svc_conf) is not None:
                            port_config['service'] = 'multihost'
                            set_nested(port_config, ['multihost', 'mode'], 'backup-failover')
                        elif dict_search('client.remote.multi_host', svc_conf) is not None:
                            port_config['service'] = 'multihost'
                    if 'serial_buffering' in svc_conf:
                        port_config['service'] = 'serial-buffering'
                        set_nested(port_config, ['datalogging', 'init_service'], 'trueport')

                # virtual_modem
                elif service == 'virtual_modem':
                    port_config['service'] = 'vmodem'
                    if 'global_parameters' in port_config:
                        if 'virtual_modem' in port_config['global_parameters']:
                            if 'directory_entry' in port_config['global_parameters']['virtual_modem']:
                                port_config['global_parameters']['virtual_modem']['directory_entry'] = subtract_from_key(
                                    port_config['global_parameters']['virtual_modem']['directory_entry'])
                    if 'send_connect_status' in svc_conf:
                        if svc_conf['send_connect_status'] == 'none':
                            svc_conf['suppress'] = '1'
                            del svc_conf['send_connect_status']
                    if 'disable_echo' in svc_conf:
                        svc_conf['echo'] = '0'

                # remote_printer
                elif service == 'remote_printer':
                    port_config['service'] = 'remote-printer'

                # udp
                elif service == 'udp':
                    port_config['service'] = 'udp'
                    if 'rule' in svc_conf:
                        svc_conf['rule'] = subtract_from_key(svc_conf['rule'])
                        for key, rule_conf in svc_conf['rule'].items():
                            # Flatten direction: move contents up and set direction to type name
                            if 'direction' in rule_conf:
                                direction_types = ['both', 'lan_serial', 'serial_lan']
                                for dir_type in direction_types:
                                    if dir_type in rule_conf['direction']:
                                        dir_content = rule_conf['direction'][dir_type]
                                        # Flatten contents to rule level
                                        for dir_key, dir_val in dir_content.items():
                                            # Rename xxx_port to port
                                            if dir_key.endswith('_port'):
                                                rule_conf['port'] = dir_val
                                            else:
                                                rule_conf[dir_key] = dir_val
                                        # Set direction to just the type name
                                        rule_conf['direction'] = dir_type
                                        break
                            # Set outbound_port if port is numeric
                            if 'port' in rule_conf:
                                port_val = rule_conf['port']
                                if isinstance(port_val, str) and port_val.isnumeric():
                                    rule_conf['outbound_port'] = port_val
                                    del rule_conf['port']

                # tcp (server=reverse, client=direct/silent)
                elif service == 'tcp':
                    if is_server:
                        port_config['service'] = 'tcp-reverse'
                        if dict_search('server.allow_multiple_connections', svc_conf) is not None:
                            port_config['service'] = 'multihost'
                    elif is_client:
                        port_config['outbound'] = '1'
                        # Default to silent, change to direct if initiate settings exist
                        port_config['service'] = 'tcp-silent'
                        # Check multihost first (backup or multi-host)
                        if dict_search('client.remote.backup', svc_conf) is not None:
                            port_config['service'] = 'multihost'
                            set_nested(port_config, ['multihost', 'mode'], 'backup-failover')
                        elif dict_search('client.remote.multi_host', svc_conf) is not None:
                            port_config['service'] = 'multihost'
                    # serial_buffering overrides other service types
                    if 'serial_buffering' in svc_conf:
                        port_config['service'] = 'serial-buffering'
                        set_nested(port_config, ['datalogging', 'init_service'], 'tcp')

                # telnet (server=reverse, client=direct/silent)
                elif service == 'telnet':
                    if is_server:
                        port_config['service'] = 'telnet-reverse'
                    elif is_client:
                        port_config['outbound'] = '1'
                        port_config['service'] = 'telnet-silent'

                # ssh (server=reverse, client=direct/silent)
                elif service == 'ssh':
                    if is_server:
                        port_config['service'] = 'ssh-reverse'
                    elif is_client:
                        port_config['outbound'] = '1'
                        port_config['service'] = 'ssh-silent'

                # serial_tunnel
                elif service == 'serial_tunnel':
                    if is_server:
                        port_config['service'] = 'serial-tunnel-server'
                    elif is_client:
                        port_config['service'] = 'serial-tunnel-client'

                # modbus_gateway (uses master/slave instead of server/client)
                elif service == 'modbus_gateway':
                    is_master = 'master' in svc_conf
                    if is_master:
                        port_config['service'] = 'modbus-master'
                        master_conf = svc_conf.pop('master')
                        for key, value in master_conf.items():
                            port_config[key] = value

                        # Process slave mapping entries from the current nested config shape.
                        for mapping_key in ('slave_mapping_list', 'mapping'):
                            mapping_conf = port_config.get(mapping_key)
                            if not mapping_conf or 'entry' not in mapping_conf:
                                continue

                            mapping_conf['entry'] = subtract_from_key(mapping_conf['entry'])
                            for key, value in mapping_conf['entry'].items():
                                if 'uid' in value:
                                    uid_str = value['uid']
                                    if '-' in uid_str:
                                        uid_start, uid_end = map(int, uid_str.split('-'))
                                    else:
                                        uid_start = uid_end = int(uid_str)
                                    mapping_conf['entry'][key]['uid_start'] = uid_start
                                    mapping_conf['entry'][key]['uid_end'] = uid_end
                                    del mapping_conf['entry'][key]['uid']
                    else:
                        # Default to slave
                        port_config['service'] = 'modbus-slave'
                        slave_conf = svc_conf.pop('slave')
                        for key, value in slave_conf.items():
                            if key == 'remap_uid':
                                remap_conf = value or {}
                                remapped_entries = {}
                                for idx, (source_key, entry_val) in enumerate(remap_conf.items()):
                                    entry = dict(entry_val) if isinstance(entry_val, dict) else {}
                                    entry['from'] = str(source_key)
                                    if 'to' in entry and isinstance(entry['to'], str):
                                        remapped_entries[str(idx)] = {
                                            'from': entry['from'],
                                            'to': entry['to'],
                                        }
                                        continue
                                    if isinstance(entry_val, dict) and 'to' in entry_val and isinstance(entry_val.get('to'), str):
                                        entry['to'] = entry_val['to']
                                    elif 'uid' in entry:
                                        entry['to'] = entry['uid']
                                        del entry['uid']
                                    remapped_entries[str(idx)] = {
                                        'from': entry['from'],
                                        'to': entry['to'],
                                    }
                                port_config['remap_uid'] = remapped_entries
                            else:
                                port_config[key] = value

                # nine_bits
                elif service == 'nine_bits':
                    port_config['service'] = 'nine-bits'

                # ppp
                elif service == 'ppp':
                    port_config['service'] = 'ppp'
                    # Merge interface config if available
                    if 'interface_config' in config and device in config['interface_config']:
                        if_config = config['interface_config'][device]
                        selected_protocols = if_config.pop('authentication_protocol_selected', [])
                        port_config['ppp_interface'] = if_config
                        # Process IPv4 addresses
                        if 'local_address' in if_config:
                            inet = ipaddress.IPv4Interface(if_config['local_address'])
                            if_config['v4_local_inet'] = str(inet.ip)
                            if_config['v4_mask'] = str(inet.network.netmask)
                            del port_config['ppp_interface']['local_address']
                        if 'remote_address' in if_config:
                            if if_config['remote_address'] == 'negotiation':
                                if_config['ip_address_negotiation'] = 1
                                del if_config['remote_address']
                            else:
                                inet = ipaddress.IPv4Interface(if_config['remote_address'])
                                if_config['v4_remote_inet'] = str(inet.ip)
                                if_config['v4_mask'] = str(inet.network.netmask)
                                del port_config['ppp_interface']['remote_address']
                        # Process IPv6 global network prefix
                        if 'ipv6' in if_config and 'global_parameters_network_prefix' in if_config['ipv6']:
                            prefix = if_config['ipv6']['global_parameters_network_prefix']
                            if '/' in prefix:
                                if_config['ipv6']['v6_local_prefix'], if_config['ipv6']['prefix_length'] = prefix.split('/')
                                del if_config['ipv6']['global_parameters_network_prefix']
                        # Flatten authentication.protocol using the explicitly selected branch only.
                        if 'authentication' in if_config and 'protocol' in if_config['authentication']:
                            auth_proto = if_config['authentication']['protocol']
                            if selected_protocols:
                                protocol = selected_protocols[0]
                                if protocol in auth_proto:
                                    if protocol != 'none':
                                        for key, val in auth_proto[protocol].items():
                                            if_config['authentication'][key] = val
                                    if_config['authentication']['protocol'] = protocol
                        # Invert vj_compression to disable_vj_comp
                        if 'vj_compression' in if_config:
                            if_config['disable_vj_comp'] = 0
                            del if_config['vj_compression']
                        else:
                            if_config['disable_vj_comp'] = 1

                # slip
                elif service == 'slip':
                    port_config['service'] = 'slip'
                    # Merge interface config if available
                    if 'interface_config' in config and device in config['interface_config']:
                        if_config = config['interface_config'][device]
                        port_config['slip_interface'] = if_config
                        # Process IPv4 addresses
                        if 'local_address' in if_config:
                            inet = ipaddress.IPv4Interface(if_config['local_address'])
                            if_config['local_inet'] = str(inet.ip)
                            if_config['subnet_mask'] = str(inet.network.netmask)
                            del port_config['slip_interface']['local_address']
                        if 'remote_address' in if_config:
                            inet = ipaddress.IPv4Interface(if_config['remote_address'])
                            if_config['remote_inet'] = str(inet.ip)
                            if_config['subnet_mask'] = str(inet.network.netmask)
                            del port_config['slip_interface']['remote_address']

            # hardware - rts_toggle under protocol.rs232
            if 'protocol' in port_config:
                if 'rs232' in port_config['protocol']:
                    if 'rts_toggle' in port_config['protocol']['rs232']:
                        port_config['protocol']['rs232']['rts_toggle']['enabled'] = '1'
                    # Transform monitor_signal from list to dict
                    if 'monitor_signal' in port_config['protocol']['rs232']:
                        signals = port_config['protocol']['rs232']['monitor_signal']
                        if isinstance(signals, list):
                            port_config['protocol']['rs232']['monitor_signal'] = {s: 1 for s in signals}

            # Flatten protocol config: move all sub-keys to port_config, keep only protocol type
            # Flatten protocol config and flow_control direction
            if 'protocol' in port_config:
                protocol_types = ['rs232', 'rs422', 'rs485_full', 'rs485_half']
                for proto in protocol_types:
                    if proto in port_config['protocol']:
                        proto_conf = port_config['protocol'][proto]
                        # Move all sub-keys to port_config
                        for key in list(proto_conf.keys()):
                            port_config[key] = proto_conf.pop(key)
                        break

            # TLS processing - copy selected template config into port_config['tls']
            if service and 'tls' in svc_conf and 'template' in svc_conf['tls']:
                template_name = svc_conf['tls']['template']
                template_conf = dict_search_args(config, 'global_parameters', 'tls', 'template', template_name)
                if template_conf:
                    # Copy template config to port_config['tls']
                    port_config['tls'] = dict(template_conf)
                    tls_conf = port_config['tls']
                    tls_conf['enabled'] = 1

                    if 'peer_verification' in tls_conf:
                        # If peer_verification has any fields set, enable verify_peer
                        if tls_conf['peer_verification']:
                            tls_conf['verify_peer'] = 1

                    if 'cipher_options' in tls_conf:
                        tls_conf['cipher_options'] = subtract_from_key(tls_conf['cipher_options'])

                    if 'certificate' in tls_conf and not tpm_enabled():
                        cert_name = tls_conf['certificate']

                        cert_data = dict_search_args(config['pki'], 'certificate', cert_name, 'certificate')
                        key_data = dict_search_args(config['pki'], 'certificate', cert_name, 'private', 'key')

                        CERT_PATH.mkdir(parents=True, exist_ok=True)
                        cert_file_path = (CERT_PATH/f'ssl_cert_{cert_name}.pem').absolute()
                        with cert_file_path.open('w') as f:
                            f.write(wrap_certificate(cert_data))

                        password_protected = 0
                        if 'passphrase' in tls_conf:
                            if 'password_protected' not in config['pki']['certificate'][cert_name]['private']:
                                tls_conf['passphrase'] = ''
                            else:
                                password_protected = 1

                        key_file_path = (CERT_PATH/f'ssl_key_{cert_name}.pem').absolute()
                        with key_file_path.open('w') as f:
                            f.write(wrap_private_key(key_data, password_protected))

                # Remove tls from svc_conf so it doesn't overwrite during service flattening
                del svc_conf['tls']
                del port_config['global_parameters']['tls']

            # Flatten service config: keep service with mode, flatten client/server contents to port_config
            if service and service in port_config:
                svc_conf = port_config[service]
                for key in list(svc_conf.keys()):
                    if key in ('client', 'server'):
                        # Set mode and flatten client/server contents to port_config
                        svc_conf['mode'] = key
                        for sub_key in list(svc_conf[key].keys()):
                            port_config[sub_key] = svc_conf[key].pop(sub_key)
                        del svc_conf[key]
                    elif key == 'protocol':
                        # Do not flatten service-specific protocol nodes such as
                        # modbus protocol.ascii; those belong under the service,
                        # while the hardware serial protocol remains a top-level
                        # port configuration value.
                        continue
                    else:
                        # Move non-client/server keys directly to port_config
                        port_config[key] = svc_conf.pop(key)

            # multihost list processing for backup-failover mode (after service flattening)
            if service and port_config.get('service') == 'multihost' and 'outbound' in port_config:
                if dict_search('remote.multi_host.entry', port_config) is not None:
                    entries = port_config['remote']['multi_host']['entry']
                    port_config['remote']['multi_host']['entry'] = subtract_from_key(entries)
                if port_config.get('multihost', {}).get('mode') == 'backup-failover':
                    # Set up multihost_list from primary/backup
                    primary_addr = dict_search('remote.primary.address', port_config)
                    primary_port = dict_search('remote.primary.port', port_config)
                    backup_addr = dict_search('remote.backup.address', port_config)
                    backup_port = dict_search('remote.backup.port', port_config)
                    # Replace remote with multi_host structure
                    port_config['remote'] = {
                        'multi_host': {
                            'entry': {
                                '0': {'address': primary_addr, 'port': primary_port},
                                '1': {'address': backup_addr, 'port': backup_port}
                            }
                        }
                    }
                    port_config['send_tcp_keepalive'] = '1'

            # Transform disable_* keys: remove and set corresponding key to 0
            for disable_key in ['disable_line_termination', 'disable_echo_suppression']:
                if disable_key in port_config:
                    del port_config[disable_key]
                    # Remove 'disable_' prefix to get the actual key
                    actual_key = disable_key[8:]  # len('disable_') = 8
                    port_config[actual_key] = 0

            # Flatten modem dial config: move out/both contents up to modem level
            if 'modem' in port_config and 'dial' in port_config['modem']:
                dial_conf = port_config['modem']['dial']
                for mode in ('out', 'both'):
                    if mode in dial_conf:
                        # Flatten: move contents up and set dial to mode name
                        for key, val in dial_conf[mode].items():
                            port_config['modem'][key] = val
                        port_config['modem']['dial'] = mode
                        break
                else:
                    # dial in - just set dial to 'in'
                    if 'in' in dial_conf:
                        port_config['modem']['dial'] = 'in'

            # Set initiate options for tcp/telnet/ssh client services
            current_svc = port_config.get('service', '')
            if current_svc.startswith(('tcp-', 'telnet-', 'ssh-')) and current_svc != 'multihost':
                svc_prefix = current_svc.split('-')[0]
                if dict_search('initiate.any_character', port_config) is not None:
                    port_config['service'] = f'{svc_prefix}-direct'
                    port_config['raw_option'] = 'initiate-any-char'
                elif dict_search('initiate.character', port_config) is not None:
                    port_config['service'] = f'{svc_prefix}-direct'
                    port_config['raw_option'] = 'initiate-specific-char'

            SERIAL_PATH.mkdir(parents=True, exist_ok=True)
            cfg_filename = (SERIAL_PATH/f'ttyS{ttynum}.json').absolute()
            with cfg_filename.open('w') as f:
                json.dump(prepare_for_json(port_config), f, indent=4)

    # print(proxy)
    return config


def apply(config):
    if not config:
        return None

    if config.get('_invoke_context') == 'interface':
        return None

    def stop_all_serial_ports(devices=None):
        if devices is None:
            devices = find_all_ttyS_devices_without_console()
        for device in devices:
            _apply_serial_protocol(f'/dev/{device}', 'isolate', termination='off', slew_rate='off')

    has_devices = bool(config.get('device'))
    has_removals = bool(config.get('serial_remove'))

    # Handle the removal-only / no-devices case before any service restart.
    # A config like {'device': {}, 'serial_remove': [...] } still means no
    # active serial devices remain, and starting the monitor here would race
    # with the shutdown we are about to do.
    if not has_devices:
        if is_systemd_service_active(SERIAL_SERVICE):
            cmd(f'systemctl stop {SERIAL_SERVICE}')
        stop_all_serial_ports()
        if SERIAL_PATH.exists():
            shutil.rmtree(SERIAL_PATH)
        return None

    # Start service only for real device work; removal-only shutdown already
    # returned above.
    if has_removals or 'serial_restart' in config:
        if not is_systemd_service_active(SERIAL_SERVICE):
            cmd(f'systemctl start {SERIAL_SERVICE}')
            cmd(f'systemctl is-active --wait {SERIAL_SERVICE}')

    '''Apply serial device configuration.'''
    if 'serial_remove' in config:
        for device in config['serial_remove']:
            send_command_to_iolan('delete', device)

    if 'serial_restart' in config:
        for device in config['serial_restart']:
            serial_config = config['device'].get(device, {})
            while not is_systemd_service_active(SERIAL_SERVICE):
                sleep(0.100)

            if 'disable' not in serial_config:
                # Interface-context commits can reach apply() before serial-only
                # validation runs; skip restart for incomplete serial config.
                if 'protocol' not in serial_config:
                    print(f'''DEBUG: serial_restart skip: {device} - missing protocol''')
                    continue

                protocol_cfg = serial_config['protocol']
                if 'rs232' in protocol_cfg:
                    protocol = 'rs232'
                elif 'rs422' in protocol_cfg:
                    protocol = 'rs422'
                elif 'rs485_full' in protocol_cfg:
                    protocol = 'rs485f'
                elif 'rs485_half' in protocol_cfg:
                    protocol = 'rs485h'

                if dict_search('disable_line_termination', protocol_cfg) is not None:
                    termination = 'off'
                else:
                    termination = 'on'

                _apply_serial_protocol(
                    f'/dev/{device}',
                    protocol,
                    termination=termination,
                    slew_rate='off',
                )
                send_command_to_iolan('restart', device)
            else:
                _apply_serial_protocol(
                    f'/dev/{device}',
                    'isolate',
                    termination='off',
                    slew_rate='off',
                )
                send_command_to_iolan('stop', device)


    return None

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        modified = generate(c)
        apply(modified)
    except ConfigError as e:
        print(e)
        exit(1)
