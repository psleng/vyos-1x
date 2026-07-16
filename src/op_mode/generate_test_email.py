#!/usr/bin/env python3
#
# Copyright Perle maintainers and contributors <psleng@perle.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.

import subprocess
import sys
from datetime import datetime

sys.path.append('/usr/libexec/vyos/conf_mode')
try:
    import system_email
except ImportError:
    print("Error: System email configuration subsystem is not installed.")
    sys.exit(1)


def send_test_email(message: str) -> None:
    """Locate active profiles and send out a standard test email cleanly using a string input."""
    cfg = system_email.get_config()
    if not cfg:
        print("Error: No system email client settings found.")
        sys.exit(1)

    active_profile = cfg.get('active_profile')
    if not active_profile:
        print("Aborted: There is no active email profile configured.")
        print("Run: 'set system email active-profile <name>' inside config mode first.")
        sys.exit(1)

    if active_profile not in cfg.get('profiles', {}):
        print(f"Error: Active profile '{active_profile}' has no server specifications.")
        sys.exit(1)

    recipients = cfg.get('recipients', {})
    enabled_recipients = {k: v for k, v in recipients.items() if v.get('enabled') is True}

    if not enabled_recipients:
        print("Aborted: There are no enabled recipients configured to receive alerts.")
        sys.exit(1)

    print(f"Using active SMTP profile: [{active_profile}]")
    print(f"Processing delivery to {len(enabled_recipients)} enabled recipient(s)...")

    success_count = 0
    date_str = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")

    for alias, data in enabled_recipients.items():
        target_email = data.get('email') or alias
        
        if not target_email or "@" not in str(target_email):
            print(f" -> Skipping recipient [{alias}]: Invalid or missing destination email address.")
            continue

        subject_prefix = data.get('subject') or "VyOS Notification"
        subject_line = f"{subject_prefix}: Operational Test Message"

        # Dynamically map custom message input string vs internal system text default
        if message:
            message_content = f"Custom Message:\n{message}"
        else:
            message_content = (
                "This is a live diagnostic verification email generated natively "
                "by the VyOS operational command system."
            )

        email_body = (
            f"To: {target_email}\n"
            f"Subject: {subject_line}\n"
            f"Date: {date_str}\n"
            "X-Mailer: VyOS SMTP Client Extension\n\n"
            "Hello,\n\n"
            f"{message_content}\n\n"
            f"Timestamp: {datetime.now().isoformat()}\n"
            f"Outbound Route: {active_profile}\n"
            "Status: System verification successful.\n"
        )

        cmd = ['msmtp', f'--account={active_profile}', target_email]

        try:
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate(input=email_body)

            if process.returncode == 0:
                print(f" -> Sent successfully to [{target_email}]")
                success_count += 1
            else:
                print(f" -> Failed sending to [{target_email}]: {stderr.strip()}")
        except Exception as e:
            print(f" -> Execution error sending to [{target_email}]: {str(e)}")

    print(f"\nCompleted. Successfully delivered {success_count} of {len(enabled_recipients)} emails.")
    sys.exit(0 if success_count > 0 else 1)


if __name__ == '__main__':
    # Safely extract the raw string variable block sitting at argument index 1
    input_sentence = ""
    if len(sys.argv) > 1:
        input_sentence = sys.argv[1]
        
    send_test_email(input_sentence)

