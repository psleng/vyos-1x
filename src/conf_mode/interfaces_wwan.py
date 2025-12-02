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

from sys import exit
from time import sleep

from vyos.config import Config
from vyos.configdep import set_dependents
from vyos.configdep import call_dependents
from vyos.configdict import get_interface_dict
from vyos.configdict import is_node_changed
from vyos.configverify import verify_authentication
from vyos.configverify import verify_interface_exists
from vyos.configverify import verify_mirror_redirect
from vyos.configverify import verify_vrf
from vyos.configverify import verify_mtu_ipv6
from vyos.ifconfig import WWANIf
#from vyos.utils.dict import dict_search
from vyos.utils.network import is_wwan_connected
from vyos.utils.process import cmd
#from vyos.utils.process import call
#from vyos.utils.process import DEVNULL
from vyos.utils.process import is_systemd_service_active
from vyos.utils.file import write_file
from vyos import ConfigError
from vyos import airbag
airbag.enable()

service_name = 'ModemManager.service'
cron_script = '/etc/cron.d/vyos-wwan'

def get_config(config=None):
    """
    Retrive CLI config as dictionary. Dictionary can never be empty, as at least the
    interface name will be added or a deleted flag
    """
    if config:
        conf = config
    else:
        conf = Config()
    base = ['interfaces', 'wwan']
    ifname, wwan = get_interface_dict(conf, base)
    # TODO :clean up this debug print statements
    print("***config***")
    print("***ifname***")
    print(ifname)
    print("***wwan***")
    print(wwan)
    print("***base***")
    print(base)
    wwan_config = wwan.get('wwan_profile', {})
    primary_conf_dict = parse_wwan_profile_config(wwan_config.get('primary', {}))
    alternate_conf_dict = parse_wwan_profile_config(wwan_config.get('alternate', {}))
    print("***primary_conf_dict***")
    print(primary_conf_dict)
    print("***alternate_conf_dict***")
    print(alternate_conf_dict)
    # We should only terminate the WWAN session if critical parameters change.
    # All parameters that can be changed on-the-fly (like interface description)
    # should not lead to a reconnect!
    tmp = is_node_changed(conf, base + [ifname, 'address'])
    if tmp: wwan.update({'shutdown_required': {}})

    tmp = is_node_changed(conf, base + [ifname, 'wwan_profile'])
    if tmp: wwan.update({'shutdown_required': {}})

    tmp = is_node_changed(conf, base + [ifname, 'disable'])
    if tmp: wwan.update({'shutdown_required': {}})

    tmp = is_node_changed(conf, base + [ifname, 'vrf'])
    if tmp: wwan.update({'shutdown_required': {}})

    tmp = is_node_changed(conf, base + [ifname, 'authentication'])
    if tmp: wwan.update({'shutdown_required': {}})

    tmp = is_node_changed(conf, base + [ifname, 'ipv6', 'address', 'autoconf'])
    if tmp: wwan.update({'shutdown_required': {}})

    # We need to know the amount of other WWAN interfaces as ModemManager needs
    # to be started or stopped.
    wwan['other_interfaces'] = conf.get_config_dict([], key_mangling=('-', '_'),
                                                       get_first_key=True,
                                                       no_tag_node_value_mangle=True)

    # This if-clause is just to be sure - it will always evaluate to true
    if ifname in wwan['other_interfaces']:
        del wwan['other_interfaces'][ifname]
    if len(wwan['other_interfaces']) == 0:
        del wwan['other_interfaces']

    # Protocols static arp dependency
    if 'static_arp' in wwan:
        set_dependents('static_arp', conf)

    return wwan

def verify(wwan):
    if 'deleted' in wwan:
        return None

    ifname = wwan['ifname']

    verify_interface_exists(wwan, ifname)
    verify_authentication(wwan)
    verify_vrf(wwan)
    verify_mtu_ipv6(wwan)
    verify_mirror_redirect(wwan)

    return None

def generate(wwan):
    if 'deleted' in wwan:
        # We are the last WWAN interface - there are no other ones remaining
        # thus the cronjob needs to go away, too
        if 'other_interfaces' not in wwan:
            if os.path.exists(cron_script):
                os.unlink(cron_script)
        return None

    # Install cron triggered helper script to re-dial WWAN interfaces on
    # disconnect - e.g. happens during RF signal loss. The script watches every
    # WWAN interface - so there is only one instance.
    if not os.path.exists(cron_script):
        write_file(cron_script, '*/5 * * * * root /usr/libexec/vyos/vyos-check-wwan.py\n')

    return None

from dbus_next.aio import MessageBus
from dbus_next import BusType
import asyncio
async def simple_connect(modem_path: str, apn: str): # TODO : Temporary function to test usage of the new DBus interface
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect() # TODO : ensure system bus is connected before attempting
    proxy_object = bus.get_proxy_object(
        'com.perle.ModemConnectionService',
        '/com/perle/ModemConnectionService',
        await bus.introspect('com.perle.ModemConnectionService', '/com/perle/ModemConnectionService')
    )
    interface = proxy_object.get_interface('com.perle.ModemConnectionService.Interface')

    await interface.call_Connect(modem_path, apn) # TODO : properly handle connection failure

# takes in the primary or alternate modem profile and parses it
# parses the modem profile and returns a dictionary with the parsed values to be passed to modem connection
def parse_wwan_profile_config(wwan_profile:dict):
    if wwan_profile:
        apn = wwan_profile.get('apn', '')
        cid = wwan_profile.get('cid', '')
        pdp_type = wwan_profile.get('pdp_type', 'ipv4v6defaulttest')
        roaming = wwan_profile.get('roaming', False)
        sim_slot = wwan_profile.get('sim_slot', -1) # Default to -1 if not set and fail
        if int(sim_slot) < 0:
            raise ConfigError("Sim slot must be set.")
        technology = wwan_profile.get('technology', '')
        if technology:
            technology_type = next(iter(technology), None)
            if technology_type:
                bands = technology.get(technology_type, {}).get('band', [])
                if not bands:
                    bands = []
        else:
            technology_type = ''
            bands = []
        wwan_data_limit = wwan_profile.get('wwan_data_limit', {})
        if wwan_data_limit:
            pass # TODO handle data limit
        authentication_info = wwan_profile.get('wwan_authentication', {})
        if authentication_info:
            auth_type = next(iter(authentication_info), None)
            if auth_type:
                username_data = authentication_info.get(auth_type, {}).get("username", {})
                username = next(iter(username_data), None)
                password = username_data.get(username, {}).get("password")

        else:
            auth_type = None
            username = ''
            password = ''

        wwan_dict = {
            "APN": apn,
            "CID": cid,
            "PDP Type": pdp_type,
            "Roaming": roaming,
            "SIM Slot": sim_slot,
            "Technology": {technology_type: bands},
            "Authentication": {auth_type: {username: password}}
        }
        print(f"***wwan_dict parse_wwan_profile_config***")
        print(wwan_dict) # TODO : debug - properly log this

    else:
        wwan_dict = {}

    return wwan_dict

def apply(wwan):
    # Ensure ModemManager is running
    if not is_systemd_service_active(service_name):
        cmd(f'systemctl start {service_name}')
        counter = 100
        while counter > 0:
            counter -= 1
            tmp = cmd('mmcli -L')
            if tmp != 'No modems were found':
                break
            sleep(0.250)

    w = WWANIf(wwan['ifname'])
    if 'deleted' in wwan or 'disable' in wwan:
        w.remove()
        if 'other_interfaces' not in wwan:
            cmd(f'systemctl stop {service_name}')
            if os.path.exists(cron_script):
                os.unlink(cron_script)
        return None

    # Use DBus ModemConnectionService to set connection params and connect
    async def dbus_connect():
        modem_index = wwan['ifname'].lstrip('wwan')
        modem_path = f'/org/freedesktop/ModemManager1/Modem/{modem_index}'
        apn = wwan.get('apn', '')
        username = wwan.get('authentication', {}).get('username', '')
        password = wwan.get('authentication', {}).get('password', '')
        sim_slot = wwan.get('sim_slot', -1)
        pdp_type = wwan.get('pdp_type', '')

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        proxy_object = bus.get_proxy_object(
            'com.perle.ModemConnectionService',
            '/com/perle/ModemConnectionService',
            await bus.introspect('com.perle.ModemConnectionService', '/com/perle/ModemConnectionService')
        )
        interface = proxy_object.get_interface('com.perle.ModemConnectionService.Interface')
        # Set connection params (now includes sim_slot and pdp_type)
        await interface.call_SetConnectionParams(modem_path, apn, username, password, int(sim_slot), pdp_type)
        # Connect
        await interface.call_Connect(modem_path, apn)

    # Only connect if shutdown required or not connected
    if 'shutdown_required' in wwan or (not is_wwan_connected(wwan['ifname'])):
        asyncio.run(dbus_connect())

    w.update(wwan)

    if 'static_arp' in wwan:
        call_dependents()

    return None

if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
