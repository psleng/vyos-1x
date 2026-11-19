#!/usr/bin/env python3
"""Send a test or alert SMS using the active profile to enabled recipients."""
import logging
import sys
from logging.handlers import SysLogHandler
from vyos.configquery import ConfigTreeQuery
from vyos.utils.wwan.wwan_client import WWANClientSync


def send_sms_notifications(config, message, client):
    profiles = config.get('profile', {})
    active = config.get('active-profile')
    if not active or active not in profiles:
        raise ValueError('No valid active SMS profile configured')
    interface = profiles[active]['interface']
    recipients = {name: item for name, item in config.get('recipient', {}).items()
                  if 'enable' in item}
    if not recipients:
        raise ValueError('No enabled SMS recipients configured')
    results = []
    for name, item in recipients.items():
        number = item['number']
        try:
            result = client.send_sms(int(interface[4:]), number, message)
            if isinstance(result, dict) and result.get('success') is False:
                raise RuntimeError(result.get('error') or 'Modem rejected SMS')
            record = {'recipient': name, 'number': number, 'success': True}
            print(f'SMS submitted to [{number}] via [{active}]')
        except Exception as error:
            record = {'recipient': name, 'number': number, 'success': False, 'error': str(error)}
            print(f'Failed sending to [{number}]: {error}')
        logging.getLogger('system-sms').info('SMS result: %s', record)
        results.append(record)
    return results


def main():
    logger = logging.getLogger('system-sms')
    logger.setLevel(logging.INFO)
    handler = SysLogHandler(address='/dev/log')
    handler.setFormatter(logging.Formatter('system-sms: %(message)s'))
    logger.addHandler(handler)
    config = ConfigTreeQuery().get_config_dict(['system', 'sms'], get_first_key=True, no_tag_node_value_mangle=True)
    message = sys.argv[1] if len(sys.argv) > 1 else 'Router SMS notification test'
    if not message.strip():
        raise ValueError('SMS message must not be empty')
    results = send_sms_notifications(config, message, WWANClientSync())
    return 0 if all(item['success'] for item in results) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(f'Unable to send SMS: {error}')
        sys.exit(1)
