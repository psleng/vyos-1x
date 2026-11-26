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
import os
import re
import sys
from pathlib import Path

from vyos import ConfigError, airbag
from vyos.alerts.catalog import load_catalog, rules
from vyos.alerts.delivery import as_list
from vyos.base import Warning
from vyos.config import Config
from vyos.utils.dict import dict_search
from vyos.utils.email import email_settings
from vyos.utils.process import call

airbag.enable()
service_name = 'vyos-event-handler'
service_conf = Path(f'/run/{service_name}.conf')


def get_config(config=None):
    conf = config if config is not None else Config()
    base = ['service', 'event-handler']
    if not conf.exists(base):
        return {}
    result = conf.get_config_dict(
        base, get_first_key=True, key_mangling=('-', '_'), no_tag_node_value_mangle=True
    )
    profile, recipients = email_settings(conf)
    result['_email'] = {'profile': profile, 'recipients': recipients}
    result['_wwan'] = conf.list_nodes(['interfaces', 'wwan'])
    return result


def verify(config):
    if not config:
        return
    try:
        for name, event in config.get('event', {}).items():
            if not dict_search('filter.pattern', event):
                raise ValueError(f'Event {name} requires filter pattern')
            re.compile(event['filter']['pattern'])
            if event.get('script') and not dict_search('script.path', event):
                raise ValueError(f'Event {name} script requires path')
            if (
                not dict_search('script.path', event)
                and not any(event.get('notify', {}).values())
                and 'monitor_only' not in event
            ):
                raise ValueError(
                    f'Event {name} requires script path, notify or monitor-only'
                )
            if 'message' in event.get('script', {}).get('environment', {}):
                raise ValueError('The message environment variable is reserved')
        for name, recipient in config.get('sms_recipient', {}).items():
            if not re.fullmatch(r'\+?[0-9]{3,15}', recipient.get('number', '')):
                raise ValueError(f'SMS recipient {name} requires a valid number')
            if (
                recipient.get('interface')
                and recipient['interface'] not in config['_wwan']
            ):
                raise ValueError(
                    f'SMS recipient {name} references an unconfigured WWAN interface'
                )
        catalog = load_catalog() if config.get('alert') else None
        for name, alert in config.get('alert', {}).items():
            from vyos.alerts.catalog import expand

            definition = expand(name, alert, catalog)
            if definition.get('deprecated'):
                Warning(f'Alert type {alert["type"]} is deprecated')
            if definition.get('high_volume') and alert.get('notify', {}).get('sms'):
                Warning(f'Alert {name} can generate high SMS volume')
            if (
                definition.get('category') == 'wwan'
                and alert.get('interface')
                and not re.fullmatch(r'wwan[0-9]+', alert['interface'])
            ):
                raise ValueError('WWAN alert interface must be wwanN')
        # Include disabled rules: references must remain valid when re-enabled.
        for rule in list(config.get('event', {}).values()) + list(
            config.get('alert', {}).values()
        ):
            notify = rule.get('notify', {})
            for target in as_list(notify.get('email')):
                if (
                    not config['_email']['profile']
                    or target not in config['_email']['recipients']
                ):
                    raise ValueError(
                        f'Email recipient {target} or active profile is missing'
                    )
            for target in as_list(notify.get('sms')):
                if target not in config.get('sms_recipient', {}):
                    raise ValueError(f'SMS recipient {target} is missing')
        rules(config, catalog)
    except (ValueError, KeyError, re.error) as exc:
        raise ConfigError(f'Event-handler: {exc}') from exc


def generate(config):
    if not config.get('event') and not config.get('alert'):
        service_conf.unlink(missing_ok=True)
        return
    # SMTP credentials are never included in the daemon configuration.
    runtime = {k: v for k, v in config.items() if k != '_email'}
    fd = os.open(service_conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(runtime, stream, indent=4)


def apply(config):
    action = 'restart' if config.get('event') or config.get('alert') else 'stop'
    call(f'systemctl {action} {service_name}.service')


if __name__ == '__main__':
    try:
        config = get_config()
        verify(config)
        generate(config)
        apply(config)
    except ConfigError as exc:
        print(exc)
        sys.exit(1)
