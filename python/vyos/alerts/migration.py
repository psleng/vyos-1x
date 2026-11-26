"""Conservative recognition of legacy GUI preset filters."""

import re

GUI_SCRIPT = '/opt/flask/flask/app/callback/scripts/gui_event_notification.py'
SERVICE_UNITS = {
    'Kea DHCPv4 Service': 'isc-kea-dhcp4-server.service',
    'Kea DHCPv6 Service': 'isc-kea-dhcp6-server.service',
    'strongSwan IPsec IKEv1/IKEv2 daemon using swanctl': 'strongswan.service',
    'Router advertisement daemon for IPv6': 'radvd.service',
    'Simple Network Management Protocol (SNMP) Daemon': 'snmpd.service',
    'OpenBSD Secure Shell server': 'ssh.service',
    'Keepalive Daemon (LVS and VRRP)': 'keepalived.service',
}


def preset(identifier, pattern):
    if identifier == 'vyos-netlinkd':
        match = re.fullmatch(r'RTM_NEWLINK\.\*(.+),\.\*state=(UP|DOWN)\.\*', pattern)
        if match and re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.:-]{0,14}', match[1]):
            return 'interface-' + match[2].lower(), {'interface': match[1]}
    if identifier == 'systemd':
        for verb, typ in [('Started', 'service-start'), ('Stopped', 'service-stop')]:
            for description, unit in SERVICE_UNITS.items():
                if pattern == f'.*{verb} {description}.*':
                    return typ, {'service': unit}
    if (
        pattern
        == r'.*pam_unix\(([^c]|c[^r]|cr[^o]|cro[^n]).*:session\): session opened.*'
    ):
        return 'login-success', {}
    if (
        pattern
        == r'.*pam_unix\(([^c]|c[^r]|cr[^o]|cro[^n]).*:auth\): authentication failure;.*'
    ):
        return 'login-failed', {}
    if identifier == 'keepalived-fifo.py':
        match = re.fullmatch(
            r'\.\*INSTANCE ([a-zA-Z0-9_.:-]+) changed state to \.\*', pattern
        )
        if match:
            return 'vrrp-state', {'group': match[1]}
    if identifier == 'kea-dhcp4':
        for token, typ in [
            ('LEASE_ALLOCATED', 'dhcp-server-lease'),
            ('LEASE_RELEASED', 'dhcp-server-release'),
        ]:
            if pattern == f'.*{token}.*':
                return typ, {}
    if identifier == 'dhclient':
        match = re.fullmatch(
            r'\.\*([a-zA-Z][a-zA-Z0-9_.:-]{0,14}): (bound to|released)\.\*', pattern
        )
        if match:
            return 'dhcp-client-' + (
                'bound' if match[2] == 'bound to' else 'release'
            ), {'interface': match[1]}
    return None


def migrate(config):
    base = ['service', 'event-handler']
    events = base + ['event']
    if not config.exists(events):
        return
    for name in config.list_nodes(events):
        old = events + [name]

        def value(path, old=old):
            return (
                config.return_value(old + path) if config.exists(old + path) else None
            )

        if value(['script', 'path']) != GUI_SCRIPT:
            continue
        env = lambda key: value(['script', 'environment', key, 'value']) or ''
        email = env('email_enabled').lower() in ('true', '1', 'yes', 'on')
        sms_number, severity = env('sms_number'), env('gui_level')
        recognized = preset(
            value(['filter', 'syslog-identifier']), value(['filter', 'pattern']) or ''
        )
        target = old
        if recognized:
            typ, params = recognized
            target = base + ['alert', name]
            config.set(target + ['type'], value=typ)
            for key, val in params.items():
                config.set(target + [key], value=val)
            config.delete(old)
        else:
            config.delete(old + ['script'])
            config.set(target + ['monitor-only'])
        if severity in ('critical', 'warning', 'info'):
            config.set(target + ['severity'], value=severity)
        recipients = ['system', 'email', 'recipient']
        if email and config.exists(recipients):
            for recipient in config.list_nodes(recipients):
                if config.exists(recipients + [recipient, 'enable']):
                    config.set(
                        target + ['notify', 'email'], value=recipient, replace=False
                    )
        if sms_number and re.fullmatch(r'\+?[0-9]{3,15}', sms_number):
            alias = 'migrated-' + name
            while config.exists(base + ['sms-recipient', alias]):
                alias += '-sms'
            config.set(base + ['sms-recipient', alias, 'number'], value=sms_number)
            print(f'Event {name}: preserved SMS recipient {alias}; SMS remains opt-in')
