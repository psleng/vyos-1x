"""Shared SMTP sender for operational tests and alert delivery."""

import subprocess
from email.message import EmailMessage
from email.utils import formatdate


def email_settings(config=None):
    if config is None:
        from vyos.config import Config

        config = Config()
    base = ['system', 'email']
    profile = config.return_value(base + ['active-profile'])
    recipients = {}
    for name in config.list_nodes(base + ['recipient']):
        path = base + ['recipient', name]
        recipients[name] = {
            'email': config.return_value(path + ['email']) or name,
            'subject': config.return_value(path + ['subject']),
            'enabled': config.exists(path + ['enable']),
        }
    return profile, recipients


def send_mail(recipients, subject, body, account=None):
    if not recipients:
        return
    message = EmailMessage()
    message['To'] = ', '.join(recipients)
    message['Subject'] = str(subject).replace('\r', '').replace('\n', '')
    message['Date'] = formatdate(localtime=True)
    message.set_content(body)
    command = ['msmtp', '--file=/run/msmtp/msmtprc']
    if account:
        command.append(f'--account={account}')
    command.extend(['--', *recipients])
    result = subprocess.run(
        command,
        input=message.as_string(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f'msmtp failed (exit {result.returncode})')
