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
from re import findall
from netaddr import EUI, mac_unix_expanded
from time import sleep

from vyos.config import Config
from vyos.configdep import set_dependents
from vyos.configdep import call_dependents
from vyos.configdict import get_interface_dict
from vyos.configdict import dict_merge
from vyos.configverify import verify_address
from vyos.configverify import verify_bridge_delete
from vyos.configverify import verify_mirror_redirect
from vyos.configverify import verify_vlan_config
from vyos.configverify import verify_vrf
from vyos.configverify import verify_bond_bridge_member
from vyos.ifconfig import WiFiIf
from vyos.template import render
from vyos.utils.dict import dict_search
from vyos.utils.kernel import check_kmod
from vyos.utils.process import call
from vyos.utils.process import cmd
from vyos.utils.process import is_systemd_service_active
from vyos.utils.process import is_systemd_service_running
from vyos.utils.network import interface_exists
from vyos import ConfigError
from vyos import airbag
import vyos.utils.nxpwifiutils as nxpwifiutils
airbag.enable()

# XXX: wpa_supplicant works on the source interface
wpa_suppl_conf = '/run/wpa_supplicant/{ifname}.conf'
hostapd_conf = '/run/hostapd/{ifname}.conf'
hostapd_accept_station_conf = '/run/hostapd/{ifname}_station_accept.conf'
hostapd_deny_station_conf = '/run/hostapd/{ifname}_station_deny.conf'

country_code_path = ['system', 'wireless', 'country-code']

def find_other_stations(conf, base, ifname):
    """
    Only one wireless interface per phy can be in station mode -
    find all interfaces attached to a phy which run in station mode
    """
    old_level = conf.get_level()
    conf.set_level(base)
    dict = {}
    for phy in os.listdir('/sys/class/ieee80211'):
        list = []
        for interface in conf.list_nodes([]):
            if interface == ifname:
                continue
            # the following node is mandatory
            if conf.exists([interface, 'physical-device', phy]):
                tmp = conf.return_value([interface, 'type'])
                if tmp == 'station':
                    list.append(interface)
        if list:
            dict.update({phy: list})
    conf.set_level(old_level)
    return dict

def get_config(config=None):
    """
    Retrieve CLI config as dictionary. Dictionary can never be empty, as at least the
    interface name will be added or a deleted flag
    """
    if config:
        conf = config
    else:
        conf = Config()
    base = ['interfaces', 'wireless']

    _, wifi = get_interface_dict(conf, base)

    # retrieve global Wireless regulatory domain setting
    if conf.exists(country_code_path):
        wifi['country_code'] = conf.return_value(country_code_path)

    if 'deleted' not in wifi:
        # then get_interface_dict provides default keys
        if wifi.from_defaults(['security', 'wep']): # if not set by user
            del wifi['security']['wep']
        if wifi.from_defaults(['security', 'wpa']): # if not set by user
            del wifi['security']['wpa']

    # XXX: Jinja2 can not operate on a dictionary key when it starts of with a number
    if '40mhz_incapable' in (dict_search('capabilities.ht', wifi) or []):
        wifi['capabilities']['ht']['fourtymhz_incapable'] = wifi['capabilities']['ht']['40mhz_incapable']
        del wifi['capabilities']['ht']['40mhz_incapable']

    if dict_search('security.wpa', wifi) != None:
        wpa_cipher = wifi['security']['wpa'].get('cipher')
        wpa_mode = wifi['security']['wpa'].get('mode')
        if not wpa_cipher:
            tmp = None
            if wpa_mode == 'wpa':
                tmp = {'security': {'wpa': {'cipher' : ['TKIP', 'CCMP']}}}
            elif wpa_mode == 'wpa2':
                tmp = {'security': {'wpa': {'cipher' : ['CCMP']}}}
            elif wpa_mode == 'both':
                tmp = {'security': {'wpa': {'cipher' : ['CCMP', 'TKIP']}}}
            elif wpa_mode == 'wpa3':
                # According to WiFi specs (https://www.wi-fi.org/file/wpa3-specification)
                # section 3.5: WPA3-Enterprise 192-bit mode
                # WiFi NICs which would be able to connect to WPA3-Enterprise managed
                # networks MUST support GCMP-256.
                # Reasoning: Provided that chipsets would most likely _not_ be
                # "private user only", they all would come with built-in support
                # for GCMP-256.
                tmp = {'security': {'wpa': {'cipher' : ['CCMP', 'CCMP-256', 'GCMP', 'GCMP-256']}}}

            if tmp: wifi = dict_merge(tmp, wifi)

    # Only one wireless interface per phy can be in station mode
    tmp = find_other_stations(conf, base, wifi['ifname'])
    if tmp: wifi['station_interfaces'] = tmp

    # used in hostapd.conf.j2
    wifi['hostapd_accept_station_conf'] = hostapd_accept_station_conf.format(**wifi)
    wifi['hostapd_deny_station_conf'] = hostapd_deny_station_conf.format(**wifi)

    # Protocols static arp dependency
    if 'static_arp' in wifi:
        set_dependents('static_arp', conf)

    return wifi

def wifi_phy_supports_6ghz(phy):
    # Return True/False if the PHY does (not) provide a 6 GHz band, or None when
    # the capability cannot be determined (e.g. "iw" unavailable).
    try:
        info = cmd(f'iw phy {phy} info')
    except Exception:
        return None
    frequencies = [int(f) for f in findall(r'(\d+)(?:\.\d+)?\s+MHz', info)]
    if not frequencies:
        return None
    # The 6 GHz band spans 5925-7125 MHz
    return any(freq >= 5925 for freq in frequencies)

def verify(wifi):
    if 'deleted' in wifi:
        verify_bridge_delete(wifi)
        return None

    if 'physical_device' not in wifi:
        raise ConfigError('You must specify a physical-device "phy"')

    physical_device = wifi['physical_device']
    if not os.path.exists(f'/sys/class/ieee80211/{physical_device}'):
        raise ConfigError(f'Wirelss interface PHY "{physical_device}" does not exist!')

    if 'type' not in wifi:
        raise ConfigError('You must specify a WiFi mode')

    if 'ssid' not in wifi and wifi['type'] != 'monitor':
        raise ConfigError('SSID must be configured unless type is set to "monitor"!')

    if '9098' in nxpwifiutils.pcie_wifi_nxp_model():
        # PSL: NXP 88W9098 wifi restrictions
        ifname = wifi.get('ifname', '')
        mode = wifi.get('mode', '')
        type_ = wifi.get('type', '')

        if type_ == 'station' and ifname in ('wlan0', 'wlan1'):
            # access-point only
            raise ConfigError(f'{ifname} does not support type {type_}.\n'
                              'Use wlan2 or wlan3 instead.')

        if type_ == 'access-point' and ifname in ('wlan2', 'wlan3'):
            # station only
            raise ConfigError(f'{ifname} does not support type {type_}.\n'
                              'Use wlan0 or wlan1 instead.')

        if mode == 'ax' and ifname in ('wlan1', 'wlan3'):
            # No ax support
            raise ConfigError(f'{ifname} does not support mode {mode}.\n'
                              'Use wlan0 or wlan2 instead.')

    if wifi['type'] == 'access-point':
        if 'country_code' not in wifi:
            raise ConfigError(f'Wireless country-code is mandatory, use: '\
                              f'"set {" ".join(country_code_path)}"!')

        if 'channel' not in wifi:
            raise ConfigError('Wireless channel must be configured!')

        if 'capabilities' in wifi and 'he' in wifi['capabilities']:
            if 'channel_set_width' not in wifi['capabilities']['he']:
                raise ConfigError('Channel width must be configured!')

            # Map the 802.11ax (HE) operating class, configured through
            # "channel-set-width", to its radio band. op_classes drawn from
            # IEEE 802.11 Table E-4 / hostapd global_op_class:
            # https://w1.fi/cgit/hostap/tree/src/common/ieee802_11_common.c
            he_op_class_band = {
                '81': '2.4', '83': '2.4', '84': '2.4',
                '115': '5', '116': '5', '117': '5', '118': '5', '119': '5',
                '120': '5', '121': '5', '122': '5', '123': '5', '124': '5',
                '125': '5', '126': '5', '127': '5', '128': '5', '129': '5',
                '130': '5',
                '131': '6', '132': '6', '133': '6', '134': '6', '135': '6',
            }
            op_class = wifi['capabilities']['he']['channel_set_width']
            he_band = he_op_class_band.get(op_class)
            # channel 0 selects Automatic Channel Selection (ACS)
            channel = int(wifi.get('channel', 0))

            band_channel_valid = {
                '2.4': 1 <= channel <= 14,
                '5': 34 <= channel <= 177,
                '6': 1 <= channel <= 233,
            }
            if channel != 0 and he_band is not None and not band_channel_valid[he_band]:
                raise ConfigError(
                    f'Channel {channel} is not valid for the selected {he_band} GHz '
                    f'802.11ax operating class (channel-set-width {op_class})!')

            # A 6 GHz operating class can only work on a radio that actually
            # provides a 6 GHz band - reject it otherwise instead of rendering a
            # hostapd configuration the hardware cannot honor.
            if he_band == '6' and wifi_phy_supports_6ghz(physical_device) is False:
                raise ConfigError(
                    f'Physical device "{physical_device}" does not provide a 6 GHz '
                    f'band required by 802.11ax operating class {op_class}!')

            # Management Frame Protection (MFP) is mandatory for WPA3 in 6 GHz
            if dict_search('security.wpa.mode', wifi) == 'wpa3' and he_band == '6':
                if wifi.get('mgmt_frame_protection') != 'required':
                    raise ConfigError('Management Frame Protection (MFP) is required with WPA3 at 6GHz! Consider also enabling Beacon Frame Protection (BFP) if your device supports it.')

    if 'security' in wifi:
        if {'wep', 'wpa'} <= set(wifi.get('security', {})):
            raise ConfigError('Must either use WEP or WPA security!')

        if 'wep' in wifi['security']:
            if 'key' in wifi['security']['wep'] and len(wifi['security']['wep']) > 4:
                raise ConfigError('No more then 4 WEP keys configurable')
            elif 'key' not in wifi['security']['wep']:
                raise ConfigError('Security WEP configured - missing WEP keys!')

        elif 'wpa' in wifi['security']:
            wpa = wifi['security']['wpa']
            if not any(i in ['passphrase', 'radius'] for i in wpa):
                raise ConfigError('Missing WPA key or RADIUS server')

            if 'username' in wpa:
                if 'passphrase' not in wpa:
                    raise ConfigError('WPA-Enterprise configured - missing passphrase!')
            elif 'passphrase' in wpa:
                # check if passphrase meets the regex .{8,63}
                if len(wpa['passphrase']) < 8 or len(wpa['passphrase']) > 63:
                    raise ConfigError('WPA passphrase must be between 8 and 63 characters long')
            if 'radius' in wpa:
                if 'server' in wpa['radius']:
                    for server in wpa['radius']['server']:
                        if 'key' not in wpa['radius']['server'][server]:
                            raise ConfigError(f'Missing RADIUS shared secret key for server: {server}')

    if 'capabilities' in wifi:
        capabilities = wifi['capabilities']
        if 'vht' in capabilities:
            if 'ht' not in capabilities:
                raise ConfigError('Specify HT flags if you want to use VHT!')

            if {'beamform', 'antenna_count'} <= set(capabilities.get('vht', {})):
                if capabilities['vht']['antenna_count'] == '1':
                    raise ConfigError('Cannot use beam forming with just one antenna!')

                if capabilities['vht']['beamform'] == 'single-user-beamformer':
                    if int(capabilities['vht']['antenna_count']) < 3:
                        # Nasty Gotcha: see lines 708-721 in:
                        # https://w1.fi/cgit/hostap/tree/hostapd/hostapd.conf?h=hostap_2_10&id=cff80b4f7d3c0a47c052e8187d671710f48939e4#n708
                        raise ConfigError('Single-user beam former requires at least 3 antennas!')

    if 'station_interfaces' in wifi and wifi['type'] == 'station':
        phy = wifi['physical_device']
        if phy in wifi['station_interfaces']:
            if len(wifi['station_interfaces'][phy]) > 0:
                raise ConfigError('Only one station per wireless physical interface possible!')

    verify_address(wifi)
    verify_vrf(wifi)
    verify_bond_bridge_member(wifi)
    verify_mirror_redirect(wifi)

    # use common function to verify VLAN configuration
    verify_vlan_config(wifi)

    return None


def generate(wifi):
    check_kmod('mac80211')

    interface = wifi['ifname']

    # Delete config files if interface is removed
    if 'deleted' in wifi:
        if os.path.isfile(hostapd_conf.format(**wifi)):
            os.unlink(hostapd_conf.format(**wifi))
        if os.path.isfile(hostapd_accept_station_conf.format(**wifi)):
            os.unlink(hostapd_accept_station_conf.format(**wifi))
        if os.path.isfile(hostapd_deny_station_conf.format(**wifi)):
            os.unlink(hostapd_deny_station_conf.format(**wifi))
        if os.path.isfile(wpa_suppl_conf.format(**wifi)):
            os.unlink(wpa_suppl_conf.format(**wifi))

        return None

    # PSL: a board may provision an EXACT per-radio MAC in EEPROM (nvmem, exposed
    # via perle-device-info). Apply it verbatim here so the LAA-mangling
    # derivation below is skipped for provisioned radios.
    if 'mac' not in wifi:
        tmp = nxpwifiutils.provisioned_wifi_mac(wifi['ifname'])
        if tmp:
            wifi['mac'] = tmp

    if 'mac' not in wifi:
        # http://wiki.stocksy.co.uk/wiki/Multiple_SSIDs_with_hostapd
        # generate locally administered MAC address from used phy interface
        tmp = nxpwifiutils.getphymac(wifi)  # PSL: get preferred MAC
        if not tmp:
            with open('/sys/class/ieee80211/{physical_device}/addresses'.format(**wifi), 'r') as f:
                # some PHYs tend to have multiple interfaces and thus supply multiple MAC
                # addresses - we only need the first one for our calculation
                tmp = f.readline().rstrip()
        if nxpwifiutils.module_assigns_own_mac(wifi):
            # PSL: Perle build without nvmem MAC provisioning (no
            # wifi-interfaces.conf, e.g. the AM64x EVM with no identity EEPROM):
            # keep the NXP module's self-assigned MAC verbatim instead of
            # deriving a locally administered one.
            mac = EUI(tmp)
            mac.dialect = mac_unix_expanded
            wifi['mac'] = str(mac)
        else:
            tmp = EUI(tmp).value
            # mask last nibble from the MAC address
            tmp &= 0xfffffffffff0
            # set locally administered bit in MAC address
            tmp |= 0x020000000000
            # we now need to add an offset to our MAC address indicating this
            # subinterfaces index
            tmp += int(findall(r'\d+', interface)[0])

            # convert integer to "real" MAC address representation
            mac = EUI(hex(tmp).split('x')[-1])
            # change dialect to use : as delimiter instead of -
            mac.dialect = mac_unix_expanded
            wifi['mac'] = str(mac)

    # render appropriate new config files depending on access-point or station mode
    if wifi['type'] == 'access-point':
        render(hostapd_conf.format(**wifi), 'wifi/hostapd.conf.j2', wifi)
        render(hostapd_accept_station_conf.format(**wifi), 'wifi/hostapd_accept_station.conf.j2', wifi)
        render(hostapd_deny_station_conf.format(**wifi), 'wifi/hostapd_deny_station.conf.j2', wifi)

    elif wifi['type'] == 'station':
        render(wpa_suppl_conf.format(**wifi), 'wifi/wpa_supplicant.conf.j2', wifi)

    return None

def apply(wifi):
    interface = wifi['ifname']
    # From systemd source code:
    # If there's a stop job queued before we enter the DEAD state, we shouldn't act on Restart=,
    # in order to not undo what has already been enqueued. */
    #
    # It was found that calling restart on hostapd will (4 out of 10 cases) deactivate
    # the service instead of restarting it, when it was not yet properly stopped
    # systemd[1]: hostapd@wlan1.service: Deactivated successfully.
    # Thus kill all WIFI service and start them again after it's ensured nothing lives
    call(f'systemctl stop hostapd@{interface}.service')
    call(f'systemctl stop wpa_supplicant@{interface}.service')

    if 'deleted' in wifi:
        WiFiIf(**wifi).remove()
        return None

    while (is_systemd_service_running(f'hostapd@{interface}.service') or \
           is_systemd_service_active(f'hostapd@{interface}.service')):
        sleep(0.250) # wait 250ms

    # Finally create the new interface
    w = WiFiIf(**wifi)
    w.update(wifi)

    # Enable/Disable interface - interface is always placed in
    # administrative down state in WiFiIf class
    if 'disable' not in wifi:
        # Wait until interface was properly added to the Kernel
        ii = 0
        while not (interface_exists(interface) and ii < 20):
            sleep(0.250) # wait 250ms
            ii += 1

        # Physical interface is now configured. Proceed by starting hostapd or
        # wpa_supplicant daemon. When type is monitor we can just skip this.
        if wifi['type'] == 'access-point':
            call(f'systemctl start hostapd@{interface}.service')

        elif wifi['type'] == 'station':
            call(f'systemctl start wpa_supplicant@{interface}.service')

    if 'static_arp' in wifi:
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
