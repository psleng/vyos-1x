#!/usr/bin/env python3
"""Send an operational test through the shared SMTP helper."""

import sys

from vyos.utils.email import email_settings, send_mail


def send_test_email(message=''):
    account, recipients = email_settings()
    if not account:
        raise ValueError('No active system email profile configured')
    enabled = [r for r in recipients.values() if r['enabled']]
    if not enabled:
        raise ValueError('No enabled email recipients configured')
    successes = 0
    for recipient in enabled:
        try:
            send_mail(
                [recipient['email']],
                f'{recipient["subject"] or "VyOS Notification"}: Operational Test Message',
                f'Custom Message:\n{message}'
                if message
                else 'System email verification message.',
                account,
            )
            successes += 1
        except Exception as exc:  # noqa: BLE001 - report per-recipient failures
            print(f'Delivery failed: {type(exc).__name__}')
    print(f'Delivered {successes} of {len(enabled)} test emails')
    return 0 if successes else 1


if __name__ == '__main__':
    try:
        sys.exit(send_test_email(sys.argv[1] if len(sys.argv) > 1 else ''))
    except ValueError as exc:
        print(exc)
        sys.exit(1)
