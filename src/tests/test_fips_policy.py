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

import re

from unittest import TestCase

from vyos import fips

# Map each policy leaf to the grammar file + <leafNode> whose <completionHelp>
# <list> is its source of truth, so the allow-list cannot silently drift from
# the CLI tokens (igos-fips-140-3-design.md section 5.6.5).
GRAMMAR_SOURCES = {
    'service ssh cipher': ('interface-definitions/service_ssh.xml.in', 'cipher'),
    'service ssh mac': ('interface-definitions/service_ssh.xml.in', 'mac'),
    'service ssh key-exchange': (
        'interface-definitions/service_ssh.xml.in', 'key-exchange'),
    'service ssh hostkey-algorithm': (
        'interface-definitions/service_ssh.xml.in', 'hostkey-algorithm'),
    'service ssh pubkey-accepted-algorithm': (
        'interface-definitions/service_ssh.xml.in', 'pubkey-accepted-algorithm'),
    'vpn ipsec ike-group proposal encryption': (
        'interface-definitions/include/vpn-ipsec-encryption.xml.i', 'encryption'),
    'vpn ipsec ike-group proposal hash': (
        'interface-definitions/include/vpn-ipsec-hash.xml.i', 'hash'),
    'vpn ipsec ike-group proposal dh-group': (
        'interface-definitions/vpn_ipsec.xml.in', 'dh-group'),
    'vpn ipsec esp-group proposal encryption': (
        'interface-definitions/include/vpn-ipsec-encryption.xml.i', 'encryption'),
    'vpn ipsec esp-group proposal hash': (
        'interface-definitions/include/vpn-ipsec-hash.xml.i', 'hash'),
    'vpn ipsec esp-group pfs': (
        'interface-definitions/vpn_ipsec.xml.in', 'pfs'),
    'interfaces openvpn encryption cipher': (
        'interface-definitions/interfaces_openvpn.xml.in', 'cipher'),
    'interfaces openvpn encryption data-ciphers': (
        'interface-definitions/interfaces_openvpn.xml.in', 'data-ciphers'),
    'interfaces openvpn encryption data-ciphers-fallback': (
        'interface-definitions/interfaces_openvpn.xml.in', 'data-ciphers-fallback'),
    'interfaces openvpn hash': (
        'interface-definitions/interfaces_openvpn.xml.in', 'hash'),
}


def _grammar_tokens(path, leaf):
    with open(path) as f:
        text = f.read()
    # The first <list> inside the named leafNode's completionHelp.
    m = re.search(
        rf'<leafNode name="{re.escape(leaf)}">.*?<completionHelp>.*?'
        rf'<list>(.*?)</list>',
        text, re.DOTALL)
    if not m:
        return None
    return set(m.group(1).split())


class _FakeConf:
    """Minimal Config stand-in for features_present(): a set of configured paths."""

    def __init__(self, paths):
        self._paths = [tuple(p) for p in paths]

    def exists(self, path):
        t = tuple(path)
        return any(p[: len(t)] == t for p in self._paths)

    def list_nodes(self, path):
        t = tuple(path)
        return sorted({p[len(t)] for p in self._paths if len(p) > len(t) and p[: len(t)] == t})


class TestFipsPolicy(TestCase):
    def test_approved_known_and_unknown(self):
        self.assertIn('aes256-ctr', fips.approved('service ssh cipher'))
        self.assertIsNone(fips.approved('service ssh does-not-exist'))

    def test_features_present_detects_fixed_and_tagnode_paths(self):
        conf = _FakeConf([
            ['interfaces', 'wireguard', 'wg0'],
            ['system', 'login', 'radius', 'server', '1.2.3.4'],
            ['interfaces', 'wireless', 'wlan0', 'security', 'wpa', 'radius', 'server', 'x'],
        ])
        present = fips.features_present(conf)
        self.assertIn('WireGuard', present)
        self.assertIn('RADIUS (system login)', present)
        self.assertIn('RADIUS (wireless WPA-Enterprise)', present)
        self.assertNotIn('PPTP VPN', present)

    def test_features_present_empty_when_nothing_configured(self):
        self.assertEqual(fips.features_present(_FakeConf([])), [])

    def test_reject_feature_raises(self):
        from vyos import ConfigError
        with self.assertRaises(ConfigError):
            fips.reject_feature('WireGuard')

    def test_remove_nodes(self):
        self.assertEqual(
            fips.remove_nodes(), ['interfaces wireguard', 'vpn pptp'])

    def test_reject_unapproved_flags_bad_values(self):
        config = {
            'cipher': ['aes256-ctr', 'chacha20-poly1305@openssh.com'],
            'mac': ['hmac-sha2-256'],
        }
        key_to_leaf = {'cipher': 'service ssh cipher', 'mac': 'service ssh mac'}
        messages = fips.reject_unapproved(config, key_to_leaf)
        self.assertEqual(len(messages), 1)
        self.assertIn('chacha20-poly1305@openssh.com', messages[0])

    def test_reject_unapproved_accepts_approved(self):
        config = {'encryption': 'aes256', 'hash': 'sha512', 'dh_group': '19'}
        key_to_leaf = {
            'encryption': 'vpn ipsec ike-group proposal encryption',
            'hash': 'vpn ipsec ike-group proposal hash',
            'dh_group': 'vpn ipsec ike-group proposal dh-group',
        }
        self.assertEqual(fips.reject_unapproved(config, key_to_leaf), [])

    def test_reject_unapproved_ignores_unset(self):
        self.assertEqual(
            fips.reject_unapproved({}, {'cipher': 'service ssh cipher'}), [])

    def test_apply_fips_defaults_fills_only_when_absent(self):
        policy = {
            'leaf': {
                'some leaf': {'approved': ['a', 'b'], 'default': ['b', 'a']},
                'no default leaf': {'approved': ['a']},
            }
        }
        # Absent -> filled with the policy default.
        config = {}
        fips.apply_fips_defaults(config, {'val': 'some leaf'}, policy)
        self.assertEqual(config['val'], ['b', 'a'])
        # Present (even non-approved) -> left untouched, so verify() can reject it.
        config = {'val': ['x']}
        fips.apply_fips_defaults(config, {'val': 'some leaf'}, policy)
        self.assertEqual(config['val'], ['x'])
        # Leaf without a declared default -> absent key stays absent.
        config = {}
        fips.apply_fips_defaults(config, {'val': 'no default leaf'}, policy)
        self.assertNotIn('val', config)

    def test_policy_defaults_are_subset_of_approved(self):
        # A declared default must only contain approved tokens, otherwise the
        # generated config would drift from the allow-list it is validated against.
        policy = fips.load_policy()
        for leaf, entry in policy.get('leaf', {}).items():
            if 'default' not in entry:
                continue
            approved = entry.get('approved')
            self.assertIsNotNone(approved, f"'{leaf}' has a default but no approved set")
            drift = [t for t in entry['default'] if t not in approved]
            self.assertEqual(
                drift, [], f"'{leaf}' default is not a subset of approved: {drift}")

    def test_sshd_template_has_no_hardcoded_fips_algorithms(self):
        # The FIPS algorithm lists must be emitted from the policy via
        # get_config, never hardcoded in the template (single source of truth).
        with open('data/templates/ssh/sshd_config.j2') as f:
            text = f.read()
        for token in ('aes256-gcm@openssh.com', 'hmac-sha2-512-etm@openssh.com',
                      'ecdh-sha2-nistp521', 'rsa-sha2-512'):
            self.assertNotIn(
                token, text,
                f"sshd_config.j2 hardcodes '{token}'; FIPS algorithm lists must be "
                f"policy-driven (data/fips/approved-algorithms.toml)")

    def test_policy_tokens_exist_in_grammar(self):
        # Every approved token must still be a valid grammar <completionHelp>
        # token; otherwise the allow-list has drifted from the CLI.
        for leaf, (path, node) in GRAMMAR_SOURCES.items():
            approved = fips.approved(leaf)
            self.assertIsNotNone(approved, f"policy leaf '{leaf}' missing")
            tokens = _grammar_tokens(path, node)
            self.assertIsNotNone(
                tokens, f"could not read grammar tokens for '{leaf}' ({path})")
            drift = [t for t in approved if t not in tokens]
            self.assertEqual(
                drift, [],
                f"policy '{leaf}' has tokens not in grammar {path}: {drift}")
