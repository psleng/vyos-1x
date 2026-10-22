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

import json
import requests

from pathlib import Path
from sys import exit

from vyos.base import Warning
from vyos.config import Config
from vyos.utils.process import call
from vyos import ConfigError
from vyos import airbag
airbag.enable()

base = ['system', 'update-check']
service_name = 'vyos-system-update'
service_conf = Path(f'/run/{service_name}.conf')
motd_file = Path('/run/motd.d/10-vyos-update')


def get_config(config=None):
    if config:
        conf = config
    else:
        conf = Config()

    if not conf.exists(base):
        return None

    config = conf.get_config_dict(base, key_mangling=('-', '_'),
                                  get_first_key=True, no_tag_node_value_mangle=True)

    return config


def verify(config):
    # bail out early - looks like removal from running config
    if config is None:
        return

    # Nothing to verify when the user has disabled update checks.
    if 'disable' in config:
        return

    # 'url' is optional: when it is omitted the update-check service falls back
    # to the default URL baked into the image at build time
    # (/usr/share/vyos/version.json). Only an explicitly configured URL is
    # verified here for reachability.
    url = config.get('url')
    if not url:
        return

    # Make sure that provided URL is available and responses a valid JSON
    # otherwise print warning and display type of error (connection, timeout and etc.)
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        response.json()
    except requests.exceptions.RequestException as e:
        error_type = type(e).__name__
        Warning(
            '"system update-check url" has a valid URL but '
            f'unable to retrieve data from the server: {error_type}'
        )


def generate(config):
    # Remove the override config (reverting to the baked-in default URL) and
    # any stale MOTD when the node is removed or update checks are disabled.
    if config is None or 'disable' in config:
        service_conf.unlink(missing_ok=True)
        # MOTD used in /run/motd.d/10-update
        motd_file.unlink(missing_ok=True)
        return None

    # Write configuration file
    conf_json = json.dumps(config, indent=4)
    service_conf.write_text(conf_json)

    return None


def apply(config):
    # Update-check notifications are enabled by default in the image (the
    # service uses the URL baked into /usr/share/vyos/version.json when nothing
    # is configured). `system update-check disable` turns them off persistently;
    # otherwise ensure the service is enabled and (re)started so any change here
    # (e.g. an overridden URL) takes effect, or it reverts to the baked default.
    if config is not None and 'disable' in config:
        call(f'systemctl disable --now {service_name}.service')
    else:
        call(f'systemctl enable {service_name}.service')
        call(f'systemctl restart {service_name}.service')


if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
