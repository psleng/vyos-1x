#!/usr/bin/env python3

import os
import sys

from vyos.config import Config
from vyos import ConfigError

CONFIG_DIR = '/run/msmtp'
CONFIG_FILE = f'{CONFIG_DIR}/msmtprc'
GLOBAL_LINK = '/etc/msmtprc'

OAUTH_DIR = f'{CONFIG_DIR}/oauth'

OAUTH_HELPER = '/usr/libexec/vyos/msmtp-oauth-helper.sh'

SYSTEM_CA_DIR = '/etc/ssl/certs'
DEFAULT_CA_BUNDLE = f'{SYSTEM_CA_DIR}/ca-certificates.crt'


def get_config(config=None):
    if config is None:
        config = Config()

    base = ['system', 'email']

    if not config.exists(base):
        return None

    data = {
        'active_profile': config.return_value(
            base + ['active-profile']
        ),
        'profiles': {},
        'recipients': {}
    }

    if config.exists(base + ['profile']):

        for profile in config.list_nodes(base + ['profile']):

            path = base + ['profile', profile]

            ca_val = config.return_value(
                path + ['ca-file']
            )

            data['profiles'][profile] = {
                'smtp_host':
                    config.return_value(
                        path + ['smtp-host']
                    ),

                'port':
                    config.return_value(
                        path + ['port']
                    ) or '587',

                'authentication':
                    config.return_value(
                        path + ['authentication']
                    ) or 'password',

                'username':
                    config.return_value(
                        path + ['username']
                    ),

                'password':
                    config.return_value(
                        path + ['password']
                    ),

                'security':
                    config.return_value(
                        path + ['security']
                    ) or 'starttls',

                'validate_cert':
                    not config.exists(
                        path + ['no-validate-cert']
                    ),

                'ca_file':
                    ca_val.strip()
                    if isinstance(ca_val, str)
                    and ca_val.strip()
                    else None,

                'from_email':
                    config.return_value(
                        path + ['from-email']
                    ),

                'oauth2': {
                    'token_url':
                        config.return_value(
                            path + ['oauth2', 'token-url']
                        ),

                    'client_id':
                        config.return_value(
                            path + ['oauth2', 'client-id']
                        ),

                    'client_secret':
                        config.return_value(
                            path + ['oauth2', 'client-secret']
                        ),

                    'refresh_token':
                        config.return_value(
                            path + ['oauth2', 'refresh-token']
                        ),

                    'scope':
                        config.return_value(
                            path + ['oauth2', 'scope']
                        ),
                }
            }

    if config.exists(base + ['recipient']):

        for r in config.list_nodes(
                base + ['recipient']):

            rpath = base + ['recipient', r]

            data['recipients'][r] = {
                'email':
                    config.return_value(
                        rpath + ['email']
                    ),

                'subject':
                    config.return_value(
                        rpath + ['subject']
                    ),

                'enabled':
                    config.exists(
                        rpath + ['enable']
                    )
            }

    return data


def verify(cfg):

    if not cfg:
        return

    active = cfg['active_profile']

    if active and active not in cfg['profiles']:
        raise ConfigError(
            f"Profile '{active}' not defined"
        )

    for name, p in cfg['profiles'].items():

        if not p['smtp_host']:
            raise ConfigError(
                f"Profile '{name}' requires smtp-host"
            )

        if p['ca_file']:

            full = os.path.join(
                SYSTEM_CA_DIR,
                p['ca_file']
            )

            if not os.path.exists(full):
                raise ConfigError(
                    f"CA file '{full}' not found"
                )

        if p['authentication'] == 'password':

            if not p['username']:
                raise ConfigError(
                    f"Profile '{name}' requires username"
                )

            if not p['password']:
                raise ConfigError(
                    f"Profile '{name}' requires password"
                )

        elif p['authentication'] == 'oauth2':

            oauth = p['oauth2']

            if not p['username']:
                raise ConfigError(
                    f"Profile '{name}' requires username"
                )

            if not oauth['token_url']:
                raise ConfigError(
                    f"Profile '{name}' requires token-url"
                )

            if not oauth['client_id']:
                raise ConfigError(
                    f"Profile '{name}' requires client-id"
                )

            # client-secret is optional for some OAuth2 providers (public clients)
            # and the msmtp helper supports an empty CLIENT_SECRET.
            # if not oauth['client_secret']:
            #     raise ConfigError(
            #         f"Profile '{name}' requires client-secret"
            #     )

            if not oauth['refresh_token']:
                raise ConfigError(
                    f"Profile '{name}' requires refresh-token"
                )


def cleanup_oauth():

    if not os.path.isdir(OAUTH_DIR):
        return

    for filename in os.listdir(OAUTH_DIR):

        if (
            filename.endswith('.conf')
            or filename.endswith('.token')
        ):

            try:
                os.unlink(
                    os.path.join(
                        OAUTH_DIR,
                        filename
                    )
                )

            except OSError:
                pass


def generate_oauth_profile(name, oauth):

    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(OAUTH_DIR, exist_ok=True)

    conf = f'{OAUTH_DIR}/{name}.conf'

    with open(conf, 'w') as f:
        f.write(
            f'TOKEN_URL="{oauth["token_url"] or ""}"\n'
            f'CLIENT_ID="{oauth["client_id"] or ""}"\n'
            f'CLIENT_SECRET="{oauth["client_secret"] or ""}"\n'
            f'REFRESH_TOKEN="{oauth["refresh_token"] or ""}"\n'
            f'SCOPE="{oauth["scope"] or ""}"\n'
        )

    os.chmod(conf, 0o600)


def generate(cfg):

    if not cfg:
        return

    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(OAUTH_DIR, exist_ok=True)

    cleanup_oauth()

    name = cfg.get('active_profile')

    if not name or name not in cfg['profiles']:

        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)

        return

    p = cfg['profiles'][name]

    content = [
        '# Generated by VyOS system_email.py',
        'defaults',
        'syslog LOG_MAIL',
        '',
        f'account {name}',
        f'host {p["smtp_host"]}',
        f'port {p["port"]}',
    ]

    if p['from_email']:
        content.append(
            f'from {p["from_email"]}'
        )

    if p['security'] == 'tls':

        content += [
            'tls on',
            'tls_starttls off'
        ]

    elif p['security'] == 'starttls':

        content += [
            'tls on',
            'tls_starttls on'
        ]

    else:

        content.append('tls off')

    if p['security'] in ['tls', 'starttls']:

        if p['validate_cert']:

            if p['ca_file']:

                content.append(
                    f'tls_trust_file '
                    f'{SYSTEM_CA_DIR}/{p["ca_file"]}'
                )

            else:

                content.append(
                    f'tls_trust_file '
                    f'{DEFAULT_CA_BUNDLE}'
                )

        else:

            content.append(
                'tls_certcheck off'
            )

    if p['authentication'] == 'password':

        content += [
            'auth on',
            'auth plain',
            f'user "{p["username"]}"',
            f'password "{p["password"]}"'
        ]

    elif p['authentication'] == 'oauth2':

        generate_oauth_profile(
            name,
            p['oauth2']
        )

        content += [
            'auth on',
            'auth oauthbearer',
            f'user "{p["username"]}"',
            f'passwordeval {OAUTH_HELPER} {name}'
        ]

    else:

        content.append('auth off')

    content += [
        '',
        f'account default : {name}'
    ]

    tmp = CONFIG_FILE + '.tmp'

    with open(tmp, 'w') as f:
        f.write('\n'.join(content))

    os.chmod(tmp, 0o600)

    os.replace(tmp, CONFIG_FILE)


def apply(cfg):

    if not cfg:
        return

    name = cfg.get('active_profile')

    if name and name in cfg['profiles']:

        if os.path.islink(GLOBAL_LINK):
            os.remove(GLOBAL_LINK)

        elif os.path.exists(GLOBAL_LINK):
            os.remove(GLOBAL_LINK)

        os.symlink(
            CONFIG_FILE,
            GLOBAL_LINK
        )

    else:

        if os.path.islink(GLOBAL_LINK):
            os.remove(GLOBAL_LINK)


if __name__ == '__main__':

    try:
        cfg = get_config()
        verify(cfg)
        generate(cfg)
        apply(cfg)

    except ConfigError as e:

        print(
            f'Configuration Error: {e}',
            file=sys.stderr
        )

        sys.exit(1)
