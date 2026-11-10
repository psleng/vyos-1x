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
import socket
import sys
import json

from time import sleep

from vyos.base import Warning
from vyos.config import Config
from vyos.config import config_dict_merge
from vyos.configverify import verify_vrf
from vyos.configverify import verify_pki_certificate
from vyos.configverify import verify_pki_ca_certificate
from vyos.configverify import verify_pki_dh_parameters
from vyos.configdiff import get_config_diff
from vyos.defaults import api_config_state
from vyos.pki import wrap_certificate
from vyos.pki import wrap_private_key
from vyos.pki import wrap_dh_parameters
from vyos.template import render
from vyos.utils.dict import dict_search
from vyos.utils.process import call
from vyos.utils.process import is_systemd_service_active
from vyos.utils.network import check_port_availability
from vyos.utils.network import is_listen_port_bind_service
from vyos.utils.file import write_file
from vyos import ConfigError
from vyos import airbag
from vyos import fips

airbag.enable()

config_file = '/etc/nginx/sites-enabled/default'
systemd_override = r'/run/systemd/system/nginx.service.d/override.conf'
cert_dir = '/run/nginx/certs'

user = 'www-data'
group = 'www-data'

systemd_service_api = '/run/systemd/system/vyos-http-api.service'


def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()

    base = ['service', 'https']
    if not conf.exists(base):
        return None

    https = conf.get_config_dict(
        base, get_first_key=True, key_mangling=('-', '_'), with_pki=True
    )

    # store path to API config file for later use in templates
    https['api_config_state'] = api_config_state
    # get fully qualified system hostname
    https['hostname'] = socket.getfqdn()

    # We have gathered the dict representation of the CLI, but there are default
    # options which we need to update into the dictionary retrieved.
    default_values = conf.get_config_defaults(**https.kwargs, recursive=True)
    if 'api' not in https or 'graphql' not in https['api']:
        del default_values['api']

    # merge CLI and default dictionary
    https = config_dict_merge(default_values, https)

    # some settings affecting nginx will require a restart:
    # for example, a reload will not suffice when binding the listen address
    # after nginx has started and dropped privileges; add flag here
    diff = get_config_diff(conf)
    children_changed = diff.node_changed_children(base)
    https['nginx_restart_required'] = bool(set(children_changed) != set(['api']))

    # FIPS 140-3: flag FIPS mode so verify() rejects non-approved TLS cipher
    # suites and the approved defaults are emitted from the policy when the
    # operator left the cipher-suite leaves unset.
    if fips.fips_enabled(conf):
        https['fips'] = {}
        fips.apply_fips_defaults(https, {
            'cipher_suite': 'service https cipher-suite',
            'cipher_suite_tls13': 'service https cipher-suite-tls13',
        })

    return https


def verify(https):
    if https is None:
        return None

    if 'fips' in https:
        messages = fips.reject_unapproved(https, {
            'cipher_suite': 'service https cipher-suite',
            'cipher_suite_tls13': 'service https cipher-suite-tls13',
        })
        if messages:
            raise ConfigError('\n'.join(messages))

    if dict_search('certificates.certificate', https) != None:
        verify_pki_certificate(https, https['certificates']['certificate'])

        tmp = dict_search('certificates.ca_certificate', https)
        if tmp != None:
            verify_pki_ca_certificate(https, tmp)

        tmp = dict_search('certificates.dh_params', https)
        if tmp != None:
            verify_pki_dh_parameters(https, tmp, 2048)

    else:
        Warning(
            'No certificate specified, using build-in self-signed certificates. '
            'Do not use them in a production environment!'
        )

    # check if /etc/nginx/flask.conf exists
    https['flask_conf_exist'] = os.path.exists('/etc/nginx/flask.conf')

    # check if /etc/nginx/saml.conf exists
    https['saml_conf_exist'] = os.path.exists('/etc/nginx/saml.conf')

    # check if /etc/nginx/cloud.conf exists
    https['cloud_conf_exist'] = os.path.exists('/etc/nginx/cloud.conf')

    # Check if server port is already in use by a different application
    listen_address = ['0.0.0.0']
    port = int(https['port'])
    if 'listen_address' in https:
        listen_address = https['listen_address']

    for address in listen_address:
        if not check_port_availability(
            address, port, 'tcp'
        ) and not is_listen_port_bind_service(port, 'nginx'):
            raise ConfigError(f'TCP port "{port}" is used by another service!')

    verify_vrf(https)

    # Verify API server settings, if present
    if 'api' in https:
        keys = dict_search('api.keys.id', https)
        gql_auth_type = dict_search('api.graphql.authentication.type', https)

        # If "api graphql" is not defined and `gql_auth_type` is None,
        # there's certainly no JWT auth option, and keys are required
        jwt_auth = gql_auth_type == "token"

        # Check for incomplete key configurations in every case
        valid_keys_exist = False
        if keys:
            for k in keys:
                if 'key' not in keys[k]:
                    raise ConfigError(f'Missing HTTPS API key string for key id "{k}"')
                else:
                    valid_keys_exist = True

        # If only key-based methods are enabled,
        # fail the commit if no valid key configurations are found
        if (not valid_keys_exist) and (not jwt_auth):
            raise ConfigError(
                'At least one HTTPS API key is required unless GraphQL token authentication is enabled!'
            )

        if (not valid_keys_exist) and jwt_auth:
            Warning(
                f'API keys are not configured: classic (non-GraphQL) API will be unavailable!'
            )

    return None


def _nginx_render_snapshot():
    """Capture the content of every file nginx's behaviour depends on.

    Covers the rendered site config, the systemd override and the on-disk
    certificate material. Comparing a before/after snapshot lets apply() tell
    whether nginx actually needs to be bounced, so a redundant re-run (e.g. the
    system_option/fips or pki dependency re-applying service_https on boot, when
    the config diff flags every node as changed) does not pointlessly restart a
    correctly-running nginx and slow the boot.
    """
    files = [config_file, systemd_override]
    if os.path.isdir(cert_dir):
        files += [os.path.join(cert_dir, f) for f in os.listdir(cert_dir)]

    snapshot = {}
    for file in files:
        try:
            with open(file, 'rb') as f:
                snapshot[file] = f.read()
        except OSError:
            snapshot[file] = None
    return snapshot


def generate(https):
    if https is None:
        for file in [systemd_service_api, config_file, systemd_override]:
            if os.path.exists(file):
                os.unlink(file)
        return None

    nginx_state_before = _nginx_render_snapshot()

    if 'api' in https:
        render(systemd_service_api, 'https/vyos-http-api.service.j2', https)
        # Detect whether the API configuration actually changed. This lets
        # apply() avoid an unnecessary reload that could race the server's
        # (slow) startup and strand it - e.g. when service_https is re-run as a
        # dependency of an unrelated change (pki, system_option/fips) that does
        # not touch the API at all.
        old_api_state = None
        if os.path.exists(api_config_state):
            try:
                with open(api_config_state) as f:
                    old_api_state = json.load(f)
            except (OSError, ValueError):
                old_api_state = None
        https['api_config_changed'] = old_api_state != https['api']
        with open(api_config_state, 'w') as f:
            json.dump(https['api'], f, indent=2)
    else:
        if os.path.exists(systemd_service_api):
            os.unlink(systemd_service_api)

    # get certificate data
    if 'certificates' in https and 'certificate' in https['certificates']:
        cert_name = https['certificates']['certificate']
        pki_cert = https['pki']['certificate'][cert_name]

        cert_path = os.path.join(cert_dir, f'{cert_name}_cert.pem')
        key_path = os.path.join(cert_dir, f'{cert_name}_key.pem')

        server_cert = str(wrap_certificate(pki_cert['certificate']))

        # Append CA certificate if specified to form a full chain
        if 'ca_certificate' in https['certificates']:
            ca_cert = https['certificates']['ca_certificate']
            server_cert += '\n' + str(
                wrap_certificate(https['pki']['ca'][ca_cert]['certificate'])
            )

        write_file(cert_path, server_cert, user=user, group=group, mode=0o644)
        write_file(
            key_path,
            wrap_private_key(pki_cert['private']['key']),
            user=user,
            group=group,
            mode=0o600,
        )

        tmp_path = {'cert_path': cert_path, 'key_path': key_path}

        if 'dh_params' in https['certificates']:
            dh_name = https['certificates']['dh_params']
            pki_dh = https['pki']['dh'][dh_name]
            if 'parameters' in pki_dh:
                dh_path = os.path.join(cert_dir, f'{dh_name}_dh.pem')
                write_file(
                    dh_path,
                    wrap_dh_parameters(pki_dh['parameters']),
                    user=user,
                    group=group,
                    mode=0o600,
                )
                tmp_path.update({'dh_file': dh_path})

        https['certificates'].update(tmp_path)

    render(config_file, 'https/nginx.default.j2', https)
    render(systemd_override, 'https/override.conf.j2', https)

    # Record whether any nginx-affecting file actually changed so apply() can
    # skip a needless restart when nothing did.
    https['nginx_config_changed'] = _nginx_render_snapshot() != nginx_state_before
    return None


def apply(https):
    # Reload systemd manager configuration
    call('systemctl daemon-reload')
    http_api_service_name = 'vyos-http-api.service'
    https_service_name = 'nginx.service'

    if https is None:
        if is_systemd_service_active(http_api_service_name):
            call(f'systemctl stop {http_api_service_name}')
        call(f'systemctl stop {https_service_name}')
        return

    if 'api' in https:
        # Only (re)start the API when it is not already running or when its
        # configuration actually changed. Reloading a server that is still in
        # its startup window (e.g. during boot, when service_https is applied a
        # second time as a dependency of system_option/fips) makes it exit and
        # - combined with the race - can leave it stranded. Skipping the
        # needless reload eliminates that race at its source.
        if (not is_systemd_service_active(http_api_service_name)
                or https.get('api_config_changed', True)):
            call(f'systemctl reload-or-restart {http_api_service_name}')
            # Let uvicorn settle before (possibly) restarting nginx
            sleep(1)
    elif is_systemd_service_active(http_api_service_name):
        call(f'systemctl stop {http_api_service_name}')

    # Only (re)start nginx when it is not already running or when its rendered
    # configuration actually changed. On boot the config diff flags every node
    # as changed (empty baseline), so without this content check a redundant
    # dependency re-run would pointlessly restart a correctly-running nginx and
    # slow the boot. A real change (ciphers on a FIPS toggle, cert renewal, ...)
    # still triggers the appropriate restart/reload.
    if not is_systemd_service_active(https_service_name) or https.get(
        'nginx_config_changed', True
    ):
        if https['nginx_restart_required']:
            call(f'systemctl restart {https_service_name}')
        else:
            call(f'systemctl reload-or-restart {https_service_name}')


if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        sys.exit(1)
