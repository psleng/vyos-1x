# SMS REBOOT command

Configure an authorized sender and a six-digit PIN on the receiving interface:

```text
set service interface wwan wwan0 sms-command authorized-number +11234567890 pin 123456
commit
save
```

The `pin 123456` part is required on the same `set` command. VyOS does not
interactively prompt for omitted leaf values; entering only the phone number
creates an incomplete node and commit correctly reports the missing PIN.

Send `123456 REBOOT` from that number. REBOOT is case-sensitive. PINs must
contain exactly six ASCII digits; leading zeros are preserved. Both the sender
and PIN must match the configuration for the receiving WWAN interface.

Send `SHOW SYSTEM INFO` from an authorized number to receive the hostname,
version, system time, timezone, and uptime. This read-only command does not
require the PIN, but the sender must still be whitelisted.

Send `SHOW WAN IP ADDRESS` to receive a compact list of IPv4 and IPv6 addresses
currently assigned to all interfaces. This is read-only and does not require a
PIN; the sender must be whitelisted.

Send `PING <host-or-ip>` to test reachability. This read-only command does not
accept a PIN; IPv4, IPv6, and resolvable hostnames are supported. The reply is
`PING <target> OK` or `PING <target> FAILED`.

Send `CELL CONNECT` or `CELL DISCONNECT` to control the mobile data bearer on
the receiving WWAN interface. No PIN is required; the sender must be
whitelisted. They reply with `OK` or `FAILED`.

Send `CELL REBOOT` to cycle the cellular radio: airplane mode is enabled,
the service waits two seconds, and airplane mode is disabled. This also requires
No PIN is required; the sender must be whitelisted. It replies with `OK` or
`FAILED`.

Bare `REBOOT` messages are rejected. Existing authorized-number configurations
must have a PIN added before committing. The legacy
`IGOS_SMS_COMMAND_ALLOWED_SENDERS` environment variable no longer authorizes SMS.
The generated environment file stores interface-specific number/PIN mappings
and is restricted to owner access (0600).

Syslog records include the sender, command (REBOOT or UNKNOWN for unrecognized
input), UTC timestamp, interface, message ID, and result. Results distinguish
unauthorized senders, invalid format, incorrect or malformed PIN, acceptance, and reboot
request success or failure. Request success means systemctl accepted the request,
not that the machine has finished rebooting. PINs and SMS bodies are not logged.
