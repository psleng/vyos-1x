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
import unittest

from glob import glob
from json import loads

from socket import AF_INET
from socket import AF_INET6
from netifaces import ifaddresses # pylint: disable = no-name-in-module

from base_interfaces_test import BasicInterfaceTest
from base_interfaces_test import server_ca_root_cert_data
from base_interfaces_test import server_ca_intermediate_cert_data
from base_interfaces_test import client_cert_data
from base_interfaces_test import client_key_data
from base_vyostest_shim import VyOSUnitTestSHIM

from vyos.configsession import ConfigSessionError
from vyos.ethtool import Ethtool
from vyos.netlink import coalesce
from vyos.frrender import mgmt_daemon
from vyos.ifconfig import Section
from vyos.utils.file import read_file
from vyos.utils.network import is_intf_addr_assigned
from vyos.utils.network import is_ipv6_link_local
from vyos.utils.process import cmd
from vyos.utils.process import process_named_running
from vyos.utils.process import popen

def get_hostapd_value(interface, key):
    tmp = read_file(f'/run/hostapd/{interface}.conf')
    tmp = re.findall(r'\n?{}=(.*)'.format(key), tmp)
    return tmp[0] if tmp else None

class EthernetInterfaceTest(BasicInterfaceTest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._base_path = ['interfaces', 'ethernet']

        # We only test on physical interfaces and not VLAN (sub-)interfaces
        if 'TEST_ETH' in os.environ:
            tmp = os.environ['TEST_ETH'].split()
            cls._interfaces = tmp
        else:
            for tmp in Section.interfaces('ethernet', vlan=False):
                cls._interfaces.append(tmp)

        cls._macs = {}
        for interface in cls._interfaces:
            cls._macs[interface] = read_file(f'/sys/class/net/{interface}/address')

        # call base-classes classmethod
        super(EthernetInterfaceTest, cls).setUpClass()

    def tearDown(self):
        for interface in self._interfaces:
            # when using a dedicated interface to test via TEST_ETH environment
            # variable only this one will be cleared in the end - usable to test
            # ethernet interfaces via SSH
            self.cli_delete(self._base_path + [interface])
            self.cli_set(self._base_path + [interface, 'duplex', 'auto'])
            self.cli_set(self._base_path + [interface, 'speed', 'auto'])
            self.cli_set(self._base_path + [interface, 'hw-id', self._macs[interface]])

        self.cli_commit()

        # Verify that no address remains on the system as this is an eternal
        # interface.
        for interface in self._interfaces:
            self.assertNotIn(AF_INET, ifaddresses(interface))
            # required for IPv6 link-local address
            self.assertIn(AF_INET6, ifaddresses(interface))
            for addr in ifaddresses(interface)[AF_INET6]:
                # checking link local addresses makes no sense
                if is_ipv6_link_local(addr['addr']):
                    continue
                self.assertFalse(is_intf_addr_assigned(interface, addr['addr']))
            # Ensure no VLAN interfaces are left behind
            tmp = [
                x
                for x in Section.interfaces('ethernet')
                if x.startswith(f'{interface}.')
            ]
            self.assertListEqual(tmp, [])

        # check process health and continuity
        self.assertEqual(self.mgmt_daemon_pid, process_named_running(mgmt_daemon))

    def test_offloading_rps(self):
        # enable RPS on all available CPUs, RPS works with a CPU bitmask,
        # where each bit represents a CPU (core/thread). The formula below
        # expands to rps_cpus = 255 for a 8 core system
        rps_cpus = (1 << os.cpu_count()) - 1

        # XXX: we should probably reserve one core when the system is under
        # high preasure so we can still have a core left for housekeeping.
        # This is done by masking out the lowst bit so CPU0 is spared from
        # receive packet steering.
        rps_cpus &= ~1

        for interface in self._interfaces:
            self.cli_set(self._base_path + [interface, 'offload', 'rps'])

        self.cli_commit()

        for interface in self._interfaces:
            cpus = read_file(f'/sys/class/net/{interface}/queues/rx-0/rps_cpus')
            # remove the nasty ',' separation on larger strings
            cpus = cpus.replace(',', '')
            cpus = int(cpus, 16)

            self.assertEqual(f'{cpus:x}', f'{rps_cpus:x}')

    def test_offloading_rfs(self):
        global_rfs_flow = 32768
        rfs_flow = global_rfs_flow

        for interface in self._interfaces:
            self.cli_set(self._base_path + [interface, 'offload', 'rfs'])

        self.cli_commit()

        for interface in self._interfaces:
            queues = len(glob(f'/sys/class/net/{interface}/queues/rx-*'))
            rfs_flow = int(global_rfs_flow / queues)
            for i in range(0, queues):
                tmp = read_file(
                    f'/sys/class/net/{interface}/queues/rx-{i}/rps_flow_cnt'
                )
                self.assertEqual(int(tmp), rfs_flow)

        tmp = read_file('/proc/sys/net/core/rps_sock_flow_entries')
        self.assertEqual(int(tmp), global_rfs_flow)

        # delete configuration of RFS and check all values returned to default "0"
        for interface in self._interfaces:
            self.cli_delete(self._base_path + [interface, 'offload', 'rfs'])

        self.cli_commit()

        for interface in self._interfaces:
            queues = len(glob(f'/sys/class/net/{interface}/queues/rx-*'))
            rfs_flow = int(global_rfs_flow / queues)
            for i in range(0, queues):
                tmp = read_file(
                    f'/sys/class/net/{interface}/queues/rx-{i}/rps_flow_cnt'
                )
                self.assertEqual(int(tmp), 0)

    def test_non_existing_interface(self):
        unknonw_interface = 'eth667'
        self.cli_set(self._base_path + [unknonw_interface])

        # check validate() - interface does not exist
        with self.assertRaises(ConfigSessionError) as cm:
            self.cli_commit()
            self.assertIn(f'Interface "{unknonw_interface}" does not exist!',
                          str(cm.exception))

        # we need to remove this wrong interface from the configuration
        # manually, else tearDown() will have problem in commit()
        self.cli_delete(self._base_path + [unknonw_interface])

    def test_speed_duplex_verify(self):
        for interface in self._interfaces:
            self.cli_set(self._base_path + [interface, 'speed', '1000'])

            # check validate() - if either speed or duplex is not auto, the
            # other one must be manually configured, too
            with self.assertRaises(ConfigSessionError):
                self.cli_commit()
            self.cli_set(self._base_path + [interface, 'speed', 'auto'])
            self.cli_commit()

    def test_8021x_authenticator_radius(self):
        # IEEE 802.1X port-based authenticator relaying EAP to an external
        # RADIUS server. hostapd is spawned per ethernet interface via the
        # hostapd-wired@ template unit.
        radius_server = '192.0.2.10'
        radius_key = 'VyOSpassword123'
        radius_port = '1812'

        for interface in self._interfaces:
            path = self._base_path + [interface, 'authentication', 'radius',
                                      'server', radius_server]
            self.cli_set(path + ['key', radius_key])
            self.cli_set(path + ['port', radius_port])

        self.cli_commit()

        for interface in self._interfaces:
            self.assertEqual(get_hostapd_value(interface, 'driver'), 'wired')
            self.assertEqual(get_hostapd_value(interface, 'ieee8021x'), '1')
            self.assertEqual(get_hostapd_value(interface, 'use_pae_group_addr'), '1')
            self.assertEqual(get_hostapd_value(interface, 'ctrl_interface'), '/run/hostapd')
            self.assertEqual(get_hostapd_value(interface, 'auth_server_addr'), radius_server)
            self.assertEqual(get_hostapd_value(interface, 'auth_server_port'), radius_port)
            self.assertEqual(get_hostapd_value(interface, 'auth_server_shared_secret'), radius_key)

            # per-interface authenticator daemon must be running
            self.assertTrue(process_named_running('hostapd', cmdline=f'{interface}.conf'))

        # Remove authenticator and ensure the daemon is stopped and the runtime
        # configuration is cleaned up.
        for interface in self._interfaces:
            self.cli_delete(self._base_path + [interface, 'authentication'])
        self.cli_commit()

        for interface in self._interfaces:
            self.assertFalse(process_named_running('hostapd', cmdline=f'{interface}.conf'))
            self.assertFalse(os.path.exists(f'/run/hostapd/{interface}.conf'))

    def test_8021x_authenticator_macsec_psk(self):
        # MACsec key distribution (MKA key server) using a pre-shared CAK.
        # gcm-aes-256 requires a 64 hex-digit CAK; encryption is enabled.
        cak = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        ckn = '4142434445464748'
        priority = '10'

        for interface in self._interfaces:
            path = self._base_path + [interface, 'authentication', 'macsec']
            self.cli_set(path + ['cipher', 'gcm-aes-256'])
            self.cli_set(path + ['encrypt'])
            self.cli_set(path + ['mka', 'priority', priority])
            self.cli_set(path + ['mka', 'cak', cak])
            self.cli_set(path + ['mka', 'ckn', ckn])

        self.cli_commit()

        for interface in self._interfaces:
            self.assertEqual(get_hostapd_value(interface, 'driver'), 'macsec_linux')
            self.assertEqual(get_hostapd_value(interface, 'eapol_version'), '3')
            self.assertEqual(get_hostapd_value(interface, 'macsec_policy'), '1')
            # encrypt enabled -> confidentiality (integ_only = 0)
            self.assertEqual(get_hostapd_value(interface, 'macsec_integ_only'), '0')
            # gcm-aes-256 -> cipher suite index 1
            self.assertEqual(get_hostapd_value(interface, 'macsec_csindex'), '1')
            self.assertEqual(get_hostapd_value(interface, 'mka_priority'), priority)
            self.assertEqual(get_hostapd_value(interface, 'mka_cak'), cak)
            self.assertEqual(get_hostapd_value(interface, 'mka_ckn'), ckn)

        for interface in self._interfaces:
            self.cli_delete(self._base_path + [interface, 'authentication'])
        self.cli_commit()

        for interface in self._interfaces:
            self.assertFalse(os.path.exists(f'/run/hostapd/{interface}.conf'))

    def test_8021x_authenticator_local_eap(self):
        # Standalone local EAP-TLS authentication server (no external RADIUS).
        ca_certs = {
            'dot1x-server-ca-root': server_ca_root_cert_data,
            'dot1x-server-ca-intermediate': server_ca_intermediate_cert_data,
        }
        cert_name = 'dot1x-server'

        for name, data in ca_certs.items():
            self.cli_set(['pki', 'ca', name, 'certificate', data.replace('\n', '')])

        self.cli_set(['pki', 'certificate', cert_name, 'certificate', client_cert_data.replace('\n', '')])
        self.cli_set(['pki', 'certificate', cert_name, 'private', 'key', client_key_data.replace('\n', '')])

        for interface in self._interfaces:
            path = self._base_path + [interface, 'authentication', 'eap-server']
            self.cli_set(path + ['ca-certificate', 'dot1x-server-ca-intermediate'])
            self.cli_set(path + ['certificate', cert_name])

        self.cli_commit()

        for interface in self._interfaces:
            self.assertEqual(get_hostapd_value(interface, 'driver'), 'wired')
            self.assertEqual(get_hostapd_value(interface, 'ieee8021x'), '1')
            self.assertEqual(get_hostapd_value(interface, 'eap_server'), '1')
            self.assertEqual(get_hostapd_value(interface, 'eap_user_file'),
                             f'/run/hostapd/{interface}.eap_user')
            self.assertEqual(get_hostapd_value(interface, 'ca_cert'),
                             f'/run/hostapd/{interface}_ca.pem')
            self.assertEqual(get_hostapd_value(interface, 'server_cert'),
                             f'/run/hostapd/{interface}_cert.pem')
            self.assertEqual(get_hostapd_value(interface, 'private_key'),
                             f'/run/hostapd/{interface}_cert.key')

            # runtime key material must have been materialised
            self.assertTrue(os.path.exists(f'/run/hostapd/{interface}_ca.pem'))
            self.assertTrue(os.path.exists(f'/run/hostapd/{interface}_cert.pem'))
            self.assertTrue(os.path.exists(f'/run/hostapd/{interface}_cert.key'))
            self.assertTrue(os.path.exists(f'/run/hostapd/{interface}.eap_user'))

            self.assertTrue(process_named_running('hostapd', cmdline=f'{interface}.conf'))

        for interface in self._interfaces:
            self.cli_delete(self._base_path + [interface, 'authentication'])
        self.cli_commit()

        for interface in self._interfaces:
            self.assertFalse(process_named_running('hostapd', cmdline=f'{interface}.conf'))
            self.assertFalse(os.path.exists(f'/run/hostapd/{interface}.conf'))

        for name in ca_certs:
            self.cli_delete(['pki', 'ca', name])
        self.cli_delete(['pki', 'certificate', cert_name])

    def test_ethtool_ring_buffer(self):
        for interface in self._interfaces:
            # We do not use vyos.ethtool here to not have any chance
            # for invalid testcases. Re-gain data by hand
            tmp = cmd(f'sudo ethtool --json --show-ring {interface}')
            tmp = loads(tmp)
            max_rx = str(tmp[0]['rx-max'])
            max_tx = str(tmp[0]['tx-max'])

            self.cli_set(self._base_path + [interface, 'ring-buffer', 'rx', max_rx])
            self.cli_set(self._base_path + [interface, 'ring-buffer', 'tx', max_tx])

        self.cli_commit()

        for interface in self._interfaces:
            tmp = cmd(f'sudo ethtool --json --show-ring {interface}')
            tmp = loads(tmp)
            max_rx = str(tmp[0]['rx-max'])
            max_tx = str(tmp[0]['tx-max'])
            rx = str(tmp[0]['rx'])
            tx = str(tmp[0]['tx'])

            # validate if the above change was carried out properly and the
            # ring-buffer size got increased
            self.assertEqual(max_rx, rx)
            self.assertEqual(max_tx, tx)

    def test_ethtool_coalesce(self):
        """
        Verify that coalesce configuration is correctly applied to the interface using netlink
        """

        for interface in self._interfaces:
            base_path = self._base_path + [interface, 'interrupt-coalescing']
            ethtool = Ethtool(interface)
            is_virtio = ethtool.get_driver_name() == 'virtio_net'

            with self.subTest(interface=interface):
                # Verify coalesce support on the NIC before check
                supported = ethtool.check_coalesce()

                # If ethtool reports completely unsupported feature, then the CLI commit
                # should correctly raise a ConfigSessionError during commit
                if not supported:
                    self.cli_set(base_path + ['rx-usecs', '32'])
                    self.cli_set(base_path + ['tx-usecs', '32'])

                    msg = 'Driver does not fully support coalesce configuration'
                    with self.assertRaisesRegex(ConfigSessionError, msg):
                        self.cli_commit()
                    continue

                # To find out the supported features
                supported_rx_usecs = ethtool.check_coalesce('rx_usecs')
                supported_tx_usecs = ethtool.check_coalesce('tx_usecs')
                supported_adaptive_rx = ethtool.check_coalesce('adaptive_rx') and not is_virtio
                supported_adaptive_tx = ethtool.check_coalesce('adaptive_tx') and not is_virtio

                # Disabled adaptive modes and set custom values
                if supported_rx_usecs:
                    self.cli_set(base_path + ['rx-usecs', '64'])
                if supported_tx_usecs:
                    self.cli_set(base_path + ['tx-usecs', '64'])

                # Force adaptive to be disabled if it is already enabled
                params = coalesce.get_coalesce(interface)
                if supported_rx_usecs and params['adaptive_rx']:
                    cmd(f'sudo ethtool --coalesce {interface} adaptive-rx off')
                if supported_tx_usecs and params['adaptive_tx']:
                    cmd(f'sudo ethtool --coalesce {interface} adaptive-tx off')

                # Commit CLI configuration to apply coalescing
                self.cli_commit()

                # Query coalesce parameters after applying
                params = coalesce.get_coalesce(interface)

                # Assertions: all should reflect configured values
                if supported_rx_usecs:
                    # `virtio-net` doesn't correctly work with this parameter
                    self.assertEqual(params['rx_usecs'], 0 if is_virtio else 64)
                if supported_tx_usecs:
                    # `virtio-net` doesn't correctly work with this parameter
                    self.assertEqual(params['tx_usecs'], 0 if is_virtio else 64)

                # Not all parameters are adjustable for some of NIC (`virtio-net`)
                if supported_adaptive_rx:
                    # Now test enabling RX adaptive coalescing modes
                    self.cli_delete(base_path + ['rx-usecs'])
                    self.cli_set(base_path + ['adaptive-rx'])

                if supported_adaptive_tx:
                    # Now test enabling TX adaptive coalescing modes
                    self.cli_delete(base_path + ['tx-usecs'])
                    self.cli_set(base_path + ['adaptive-tx'])

                self.cli_commit()

                # Verify that adaptive modes turned on correctly
                params = coalesce.get_coalesce(interface)
                if supported_adaptive_rx:
                    self.assertTrue(params['adaptive_rx'])

                if supported_adaptive_tx:
                    self.assertTrue(params['adaptive_tx'])

    def test_ethtool_flow_control(self):
        for interface in self._interfaces:
            # Disable flow-control
            self.cli_set(self._base_path + [interface, 'disable-flow-control'])
            # Check current flow-control state on ethernet interface
            out, err = popen(f'sudo ethtool --json --show-pause {interface}')
            # Flow-control not supported - test if it bails out with a proper
            # this is a dynamic path where err = 1 on VMware, but err = 0 on
            # a physical box.
            if bool(err):
                with self.assertRaises(ConfigSessionError):
                    self.cli_commit()
            else:
                out = loads(out)
                # Flow control is on
                self.assertTrue(out[0]['autonegotiate'])

                # commit change on CLI to disable-flow-control and re-test
                self.cli_commit()

                out, err = popen(f'sudo ethtool --json --show-pause {interface}')
                out = loads(out)
                self.assertFalse(out[0]['autonegotiate'])

    def test_ethtool_evpn_uplink_tracking(self):
        for interface in self._interfaces:
            self.cli_set(self._base_path + [interface, 'evpn', 'uplink'])

        self.cli_commit()

        for interface in self._interfaces:
            frrconfig = self.getFRRconfig(f'interface {interface}', stop_section='^exit')
            self.assertIn(' evpn mh uplink', frrconfig)

    def test_switchdev(self):
        interface = self._interfaces[0]
        self.cli_set(self._base_path + [interface, 'switchdev'])

        # check validate() - virtual interfaces do not support switchdev
        # should print out warning that enabling failed

        self.cli_delete(self._base_path + [interface, 'switchdev'])

if __name__ == '__main__':
    unittest.main(verbosity=2, failfast=VyOSUnitTestSHIM.TestCase.debug_on())
