# System SMS notifications

System SMS uses the same profile / active profile / globally enabled recipients
model as system email. The WWAN interface must be configured and its SIM must
support outgoing SMS. This feature does not change default routes or enable LTE
data failover.

```
configure
set system sms profile cellular interface 'wwan0'
set system sms active-profile 'cellular'
set system sms recipient operator number '+14165551234'
set system sms recipient operator enable
commit
save
exit

generate test-sms
generate test-sms message 'Router SMS test'
```

Use actual assigned mobile numbers. Test commands contact all enabled recipients.
Modem acceptance is logged per recipient; it is not a handset delivery receipt.
One recipient's failure does not skip others. A partial failure makes the test
command exit unsuccessfully. There are no automatic delivery retries.

Configuration deletions mirror email:

```
delete system sms recipient operator enable
delete system sms recipient operator
delete system sms active-profile
delete system sms profile cellular
delete system sms
```

Run only the desired deletions inside configuration mode, then commit and save.
An active profile must be cleared or changed before its profile is removed.
View settings with `show system sms` inside configuration mode. No new operational
show commands are added.

## Alert integration

The Flask/React application exposes profiles, active profile, and recipients in
Alarms > SMS. Apply changes there and save the router configuration to persist
across reboot. Configure the event's SMS toggle; phone numbers are global rather
than per-event. The event script passes `sms_enabled` through the signed callback.
Flask invokes `generate test-sms message <alert text>` through the existing HTTP
API generate transport. Duplicate callback event IDs are handled using the
existing callback deduplication. Delivery errors are logged; matching email,
failed deliveries are not automatically retried.

For a custom test event, match identifier `sms-test` and pattern `SMS_TEST_TRIGGER`,
then emit:

```
logger -t sms-test SMS_TEST_TRIGGER
```

Recipient results are available in the normal journal:

```
sudo journalctl -t system-sms --since '10 minutes ago' --no-pager
```

## Deployment

Build and install the updated vyos-1x package so the configuration and operational
XML definitions are compiled into CLI templates and the operation cache. Deploy
the matching Flask code and rebuilt React assets together. Restart Flask after
installation. Copying only the Python scripts does not register new CLI commands.
No SMS messages are sent by package installation or configuration commit.
