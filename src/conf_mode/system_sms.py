#!/usr/bin/env python3
"""Validate system SMS notification profiles and recipients."""
import re
from vyos.config import Config
from vyos import ConfigError


def get_config(config=None):
    config = config if config is not None else Config()
    return config.get_config_dict(['system', 'sms'], get_first_key=True,
                                  no_tag_node_value_mangle=True)


def verify(config):
    if not config:
        return
    profiles = config.get('profile', {})
    active = config.get('active-profile')
    if active and active not in profiles:
        raise ConfigError(f"SMS profile '{active}' is not defined")
    interfaces = Config().list_nodes(['interfaces', 'wwan'])
    for name, profile in profiles.items():
        interface = profile.get('interface', '')
        if not re.fullmatch(r'wwan[0-9]+', interface) or interface not in interfaces:
            raise ConfigError(f"SMS profile '{name}' requires a configured WWAN interface")
    for name, recipient in config.get('recipient', {}).items():
        if not re.fullmatch(r'\+[1-9][0-9]{1,14}', recipient.get('number', '')):
            raise ConfigError(f"SMS recipient '{name}' requires an international number (for example +14165551234)")


if __name__ == '__main__':
    try:
        verify(get_config())
    except ConfigError as error:
        print(error)
        raise SystemExit(1)
