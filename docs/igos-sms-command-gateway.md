# igOS SMS command gateway

Implementation reference — updated 2026-11-13.

This document describes the current source implementation and supersedes the
historical [design sketch](igos-sms-command-gateway-design.md). It does not imply
that the latest source changes have been installed or validated on every router.

## 1. Overview and activation

The gateway accepts authenticated operational commands through SMS and returns
command output through the same WWAN interface. It runs inside
`igos-wwan-manager.service`, using the existing SMS receive and send facilities.
It requires no separate `sms-command.service`, process, or periodic polling loop.

Configuration is per interface under `interfaces wwan <interface> sms-command`.
There is no separate enable leaf. An explicitly configured gateway requires both
a nonempty `allow-from` list and a password or supported password hash. Without
that subtree, XML defaults do not activate command handling, and ordinary SMS
reception/history remain available. The gateway is not invoked for those messages.

This implementation targets fresh installations. There is no SMS configuration
migration or interfaces schema-version bump associated with this feature.

## 2. Router configuration

### Basic setup

Replace the example number and password before use:

```text
configure
set interfaces wwan wwan0 sms-command allow-from '+15555551234'
set interfaces wwan wwan0 sms-command password 'UseYourOwnStrongSecret!42'
set interfaces wwan wwan0 sms-command max-reply-segments 4
set interfaces wwan wwan0 sms-command rate-limit 5
set interfaces wwan wwan0 sms-command timeout 20
commit
save
exit
```

Only `show` is enabled by this setup. A successful commit replaces the plaintext
`password` leaf with `password-hash`; `save` persists the configuration in
`/config/config.boot`. Setting `password` again rotates the secret. A complete
supported hash may instead be supplied through `password-hash`.

### Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `allow-from` | Required; no default | One or more authorized E.164 sender numbers |
| `password` | Required unless a hash is supplied | New nonempty, whitespace-free secret; hashed on commit |
| `password-hash` | Generated from `password` | Complete SHA-256 crypt or SHA-512 crypt hash |
| `rate-limit` | 5 | Admitted requests per sender in a rolling 60-second window |
| `timeout` | 20 | Command execution timeout in seconds; not an SMS delivery timeout |
| `max-reply-segments` | 4 | Encoding-unit budget for replies to one incoming request |
| `privileged verb` | None | Individually enabled action verbs from the catalog |
| `privileged allow-from` | No additional restriction | Additional sender list for action verbs; main list still applies |

The three numeric limits must be positive integers. The implementation adds no
separate fixed upper bounds on these settings, allowlist size, or request length.

For short replies only:

```text
configure
set interfaces wwan wwan0 sms-command max-reply-segments 1
commit
save
exit
```

### E.164 matching

Configured sender numbers must start with `+`, followed by 2–15 digits, with a
nonzero first digit. Spaces, separators, and `00` international-access prefixes
are rejected. Validation checks syntax, not whether a number is assigned.

The complete international number is compared. Only a modem-omitted `+` is
tolerated. For example, `+15555551234` and `15555551234` have the same identity;
`+25555551234` and `5555551234` do not match that allowlist entry. Different
country codes cannot match merely because the last digits agree.

Authorization, optional privileged authorization, own-number checks, rate-limit
keys, and confirmation ownership all use complete normalized numbers. Own
numbers are excluded when ModemManager reports them; that information is optional.

`phone-match-digits` is no longer a CLI setting. The configuration normalizer
accepts and discards the obsolete `phone_match_digits` key in older runtime
caches; its value cannot enable suffix matching.

### Enable privileged verbs

Supported action verbs are `renew`, `reset`, `generate`, `reboot`, and `poweroff`.
Each is disabled until explicitly enabled. For example:

```text
configure
set interfaces wwan wwan0 sms-command privileged verb reset
set interfaces wwan wwan0 sms-command privileged verb reboot
set interfaces wwan wwan0 sms-command privileged allow-from '+15555551234'
commit
save
exit
```

With the optional privileged list, the sender must match both lists. Every
privileged verb requires confirmation. CLI completion and validation use the
same JSON catalog as dispatch; catalog policy cannot disable action confirmation.

### Disable the gateway

```text
configure
delete interfaces wwan wwan0 sms-command
commit
save
exit
```

Normal SMS reception remains available. Removing or changing gateway settings
invalidates pending confirmations and cancels queued work and running read-only
commands. Listener teardown also invalidates confirmations. An already-started
privileged action can finish under its command timeout even if it disconnects
the modem. Replies stop when their gateway configuration is no longer current.

## 3. Password policy and hashing

SMS complexity enforcement follows the presence of:

```text
system login enhanced-policy password
```

| Login enhanced policy | New plaintext SMS password validation |
| --- | --- |
| Absent | No enhanced complexity or Cracklib enforcement |
| Present | Configured minimum requirements and Cracklib strength checks enforced |

Nonempty, whitespace-free passwords are required in both modes because the first
whitespace-delimited field of every command SMS is the password.

Enable the policy using its defaults:

```text
configure
set system login enhanced-policy password
commit
save
exit
```

Defaults are minimum length 9, one digit, one uppercase letter, and one special
character. To customize them:

```text
configure
set system login enhanced-policy password minimum-length 14
set system login enhanced-policy password minimum-digit 2
set system login enhanced-policy password minimum-uppercase 1
set system login enhanced-policy password minimum-special 1
commit
save
exit
```

SMS reads that actual policy path with defaults. With the policy enabled, weak
passwords and Cracklib errors reject a new plaintext password. Existing hashes
are not retroactively checked for complexity; a directly imported hash cannot
establish the strength of its original plaintext password.

Login and SMS import one shared `password_policy_check()` and its default
constants from `vyos.utils.auth`. Both use the existing strength checker. SMS
hashing calls `passlib.hosts.linux_context.hash()`, the same call used by login;
there is no separate SMS FIPS-mode override. Login hashing itself is unchanged.
Runtime verification uses Passlib. SMS accepts complete SHA-256 crypt (`$5$`) and
SHA-512 crypt (`$6$`) hashes and rejects malformed, locked, DES, and MD5 hashes.

## 4. Requests, authorization, and confirmation

### Request syntax

Send these texts from an authorized phone to the router SIM:

```text
<password> show uptime
<password> show version
<password> show interfaces wwan wwan0
<password> show interfaces wwan wwan0 hardware
```

Use the actual password without the angle brackets. The password is mandatory
on every message, including confirmation messages. There is no authenticated
session or password-free grace period. Verbs are explicit and case-sensitive;
there is no implicit `show` mode.

The password is split as a raw field before `shlex` parses the command. Quoted
spaces in arguments are supported. Commands must be a single line. Arguments
are restricted to ASCII letters, digits, spaces, and `_ . / : @ % + = , -`.
Shell operators, substitutions, escapes, and globs are rejected because the
operational wrapper evaluates shell templates internally. These argument
restrictions do not apply to the password field.

### Authorization and rate limiting

Unauthorized senders, own-number matches, missing/wrong passwords, and rate-limited
requests receive no reply. Authenticated unknown/disabled verbs receive a gateway
error. Invalid paths under an enabled verb receive the normal operational CLI
output, including command errors.

Admission uses a per-sender rolling 60-second window, not a calendar-minute reset.
Every admitted attempt counts, including incorrect passwords and `CONFIRM`.
Messages rejected by sender/own-number checks do not consume that sender's quota.
There is no separate minimum spacing between requests. The window is based on
router receipt/admission time, so carrier delays affect burst tests.

Counters live in `Gateway.rates` in memory and reset when the gateway is recreated.
The configured limit is persistent, but there is no CLI showing live counts such
as “3 of 5 used.” Logs expose rate-limit rejections.

### Two-step action confirmation

For an enabled action, send:

```text
<password> reset vpn ipsec
```

The gateway returns a challenge in this form, subject to the reply budget:

```text
CONFIRM 68f38b95131bbed0 to run reset vpn ipsec. Expires in 120s. Reply: <password> CONFIRM 68f38b95131bbed0
```

Then send:

```text
<password> CONFIRM 68f38b95131bbed0
```

The 16-hex-digit nonce is single-use, expires after 120 seconds, and is bound to
the full sender number, exact pending command, interface, and gateway instance.
It is consumed before execution and is not restored after failure. Multiple
pending actions from one sender retain independent nonces. Configuration changes
and listener teardown invalidate outstanding confirmations.

A confirmed request first attempts `Accepted: <verb>`, then executes the command
if its gateway remains current. The acceptance and output share that confirmation
request's segment budget. With `max-reply-segments 1`, acceptance consumes the
budget, so execution output is not also sent. Waiting for acceptance transport
can delay the action; it does not prove the phone received the acceptance.

## 5. Execution and reply behavior

### Command execution

The catalog selects the verb passed to:

```text
/opt/vyatta/bin/vyatta-op-cmd-wrapper <verb> <arguments...>
```

The gateway does not implement individual operational commands. Only wrapper
`show` can be classified read-only. All `show` subtrees are reachable; there is
no additional subtree allowlist.

Admitted requests execute independently as background tasks. Password checks run
in worker threads; commands use asynchronous subprocesses. There are no extra
global authentication, task-count, or concurrent-command ceilings and no
`command busy` rejection. A slow command does not block normal modem monitoring.

Stdout and stderr are merged. Pagers are disabled. Retained output is limited to
`max-reply-segments * 150 * 4` bytes before final encoding-aware reply bounding.
Timeout/cancellation kills and reaps the process group, including descendants
that retain the output pipe.

### One long SMS submission

Each output reply is submitted once as a single text string. ModemManager and
the modem supply multipart segmentation/headers; the phone normally reassembles
the network parts into one displayed message. The gateway adds no manual `(i/n)`
numbering and does not split output on line boundaries.

`bound_reply()` strips surrounding whitespace, substitutes `no output` for an
empty result, and applies these conservative text budgets:

| Encoding | Units per configured segment | Character accounting |
| --- | --- | --- |
| GSM alphabet | 150 septets | Basic characters cost 1; `^ { } \\ [ ~ ] \| €` cost 2 |
| Unicode | 67 UTF-16 code units | BMP characters cost 1; supplementary characters, including many emoji, cost 2 |

A character outside the GSM basic/extension alphabets selects Unicode accounting
for the whole reply. Four configured segments budget up to 600 GSM septets or
268 UTF-16 units. If text exceeds the budget, its beginning is retained and `...`
is included within the budget. These are application text budgets; actual network
segmentation, delivery, display, and billing are controlled by the modem/carrier.

Gateway notices use plain text without surrounding brackets, for example:

```text
no output
command timed out
command failed: exit 1
command execution failed
invalid or expired confirmation
unknown or disabled command verb
```

A nonzero exit preserves command output if present; `command failed: exit <code>`
is used when no output was produced. Actual command output may contain brackets
and is not rewritten to remove them. Replies and outgoing history do not replace
substrings matching the password or its hash: a password of `igos` must not mask
legitimate hostname/user text in command output. Whitespace trimming, capture
limits, and truncation still apply.

### Delivery and concurrency limits

Replies share a per-interface lock across gateway instances. This serializes
reply submissions while allowing other commands to execute. Responses can be
queued in completion order rather than request order. Ordinary `show` normally
produces one output reply; privileged flows have separate challenge/acceptance
messages and may also produce output when budget permits.

Delivery is best-effort. There are no automatic outgoing retries, delivery-receipt
tracking, gateway-specific send deadlines, or automatic send-stall recovery.
Normal ModemManager/D-Bus behavior controls send completion. A slow send holds
the reply lock and delays later responses. `timeout` limits command execution,
not time spent waiting for authentication workers, the reply lock, or SMS sending.

`SMS reply sent` records successful completion of the send API, not handset
receipt. `outcome=finished` means the handler finished, and can follow a handled
send failure. After a failed send, that request's remaining reply budget is
closed. Command replies call the existing state-machine `sms_send()` method.
Its original ordering is retained: send, save outgoing history, attempt modem
cleanup, then return. A slow cleanup can therefore delay the reply result too.
There is no separate command-only sender or background-cleanup implementation.

## 6. Event-driven reception and replay protection

`interfaces_wwan_state_machine.py` is unchanged. The service manager constructs a
small subclass using `with_sms_commands(ModemStateMachine)` from `sms_command.py`.
Its hooks call the original methods through `super()`; storage file I/O, sending,
reading, deleting, listener attachment, and backlog enumeration are not copied. Protocol
constants are read lazily from the state-machine module rather than duplicated;
CLI validation/completion does not import the FSM just to load the gateway.

When configured, `SmsCommandRuntime` adds authentication-related receive context,
masking, stale-message/confirmation state, completion watching, and bounded retry
logic around those existing methods. Callback wrapping captures the current
attachment generation and rejects old callbacks. With the gateway disabled,
hooks delegate directly to the original behavior. No periodic
SMS polling is implemented. Reconciliation occurs on lifecycle/state transitions;
`CONNECTED` and `REGISTERED_IDLE` are ready states, so a data bearer is not
required for SMS reception.

The attachment/receive sequence is:

1. Disarm dispatch, invalidate the previous attachment, and subscribe to incoming
   notifications before reading the existing-message list.
2. Import existing modem messages as stale history. Drained messages never execute.
3. Arm dispatch only after a successful drain. Capture eligibility at notification
   arrival, so pre-arm messages cannot become live commands merely by running later.
4. Save partial incoming messages and subscribe to their `State` property changes.
   Re-read state after subscribing to close the completion race. Execute only
   fully received messages, using the complete body.
5. Require a valid timezone-aware network timestamp at or after the arm time.
   Missing, invalid, or older timestamps prevent execution but not history storage.
6. Save the record and execution claim before submitting authentication/execution
   or deleting the complete modem copy. Failed persistence prevents dispatch and
   deletion.

Receive callbacks are serialized to prevent notification/drain races. Attachment
generations and modem/SIM identity checks reject stale callbacks. Records use the
source path and generation to suppress repeat processing within an attachment.
A reused modem path in a new generation can represent a new record.

Transient setup or receive failures get up to three attempts, with 0.5-second and
1-second backoffs. These are retries of the failed operation, not periodic scans.
Setup exceptions remove partially attached handlers. Exhausted receive failures
leave the modem copy intact and are logged. Further recovery after exhausted
attempts depends on a later lifecycle event or operator intervention; it is not
guaranteed automatically. A failed backlog drain leaves command dispatch disarmed.

The execution claim gives an at-most-once attempt for an observed message, not
transactional exactly-once execution. A crash after saving a claim can lose a
request. SMS has no cryptographic request identity: redelivery as a fresh live
object is not fully prevented. Confirmation protects actions with a fresh nonce
but does not make the SMS channel confidential or sender IDs trustworthy.

## 7. SMS history, masking, and management

Messages are kept per SIM ICCID in `/var/lib/wwan/sms/sms_<iccid>.json`, capped at
100 records. Writes use a temporary file, flush/fsync, and atomic replacement.
Complete incoming modem copies are deleted only after local persistence succeeds.

With the gateway configured, the first whitespace-delimited field of every
incoming SMS is replaced with a fixed eight-asterisk mask before saving or API
exposure:

```text
Received: <password> show uptime
History:  ******** show uptime
```

This includes partial, unauthorized, and drained messages. It also masks the
first word of ordinary texts: `TEST USER` becomes `******** USER`. The fixed mask
does not reveal password length. Authentication uses the original body in memory.
Ordinary SMS stored while the gateway is disabled retain their text and the
original receive log behavior; masked-body diagnostic logging belongs to command mode.

Older history is masked on read while the gateway is enabled. If saving that
masked history fails, the loaded, masked records are still returned and a warning
is logged. A writeback failure no longer makes loaded history appear empty; a
later successful append retains it. Already-lost history cannot be recovered.
Unreadable or invalid JSON history still follows the existing empty-history path.

Masking does not alter the SMS transmitted by the phone, its sent-message history,
or existing journal entries. Outgoing command output is preserved within its
budget, including any sensitive information that the operational command prints.

Operational commands on the router:

```text
# Send a direct test message; replace the destination
generate interfaces wwan wwan0 sms number +15555551234 message "SMS test"

# List and inspect local history
show interfaces wwan wwan0 sms
show interfaces wwan wwan0 sms message 1
show interfaces wwan wwan0 sms 1

# Delete one record or all local history
clear interfaces wwan wwan0 sms message 1
clear interfaces wwan wwan0 sms
```

Clearing local history does not cancel an active send or clear failed outgoing
ModemManager objects. ModemManager SMS IDs and local history IDs are separate.

## 8. Logs and troubleshooting

The gateway uses the existing WWAN logger and configured sinks; it adds no
separate logging service. Full configuration values are no longer dumped by the
SMS-related initialization path. Incoming SMS entries log the sender number and message with its first field
masked as `********`, while the gateway is configured. Original incoming
credential fields are not logged. Reply attempts log the actual bounded outgoing
response and destination number. The journal supplies
the timestamp; multiline text is JSON-quoted so newlines and control characters
remain escaped within one log message. Reply content is not password-substring
filtered and may contain sensitive output printed by an operational command.
Transport errors log their class or D-Bus name rather than exception payloads.

| Level | Examples |
| --- | --- |
| Debug | Setup/receive attempts, duplicate/stale callbacks, multipart completion, modem-copy deletion |
| Info | Listener registration, backlog counts/results, gateway arming, incoming sender/masked text/IDs/state/eligibility, receive recovery, command outcomes, destination numbers, outgoing response text, reply timing |
| Warning/error | Receive retries, disarmed gateway after drain failure, exhausted attempts, history writeback failures, execution/send failures |

Debug messages require the WWAN logger to be configured at debug level. Audit
entries identify interface, sender, outcome, verb/tier where available, and timing.
Not every malformed input has an audit outcome; missing request fields can return
silently.

Native operational CLI commands for SMS reception, command processing, and replies:

```text
show log sms-command
monitor log sms-command
```

`show log sms-command` shows matching entries from the current boot.
`monitor log sms-command` follows only new matching entries until Ctrl+C.
Both select all case-insensitive `SMS` entries from the WWAN manager, including
incoming masked messages, listener setup/retries, command authentication/outcomes,
rate-limit rejection, outgoing response text, and send results. They do not query
ModemManager's own journal or display the saved inbox. These CLI definitions must
be installed from the current build before the commands are available.

Watch all SMS-related WWAN logs directly:

```sh
sudo journalctl -u igos-wwan-manager -f -n 0 | grep --line-buffered -i sms
```

Read recent gateway and modem logs:

```sh
sudo journalctl -u igos-wwan-manager --since "15 minutes ago" --no-pager | grep -i sms
sudo journalctl _COMM=ModemManager --since "15 minutes ago" --no-pager | grep -Ei 'sms|wms|cause'
```

Watch rate-limit rejections:

```sh
sudo journalctl -u igos-wwan-manager -f -n 0 | grep --line-buffered 'rate-limited'
```

For example, a reply attempt and its successful send completion appear as:

```text
Nov 13 11:59:59 igos ... Incoming SMS interface=wwan0 number=+15555551234 path=/sms/1 id=1 state=3 drained=False command_eligible=True message="******** show uptime"
Nov 13 12:00:00 igos ... SMS reply sending interface=wwan0 number=+15555551234 budget_parts=1 response="up 2 days\nload average: 0.01"
Nov 13 12:00:01 igos ... SMS reply sent interface=wwan0 number=+15555551234 budget_parts=1 elapsed_ms=1000
```

The response is logged before attempting transport, including when sending later
fails. A `sending` entry alone is not proof of a successful send or handset receipt.
The corresponding success/failure entry also includes the destination number.

A normal output request progresses through:

```text
Incoming SMS ... command_eligible=True
outcome=authenticated
outcome=accepted
outcome=executed
SMS reply sending
SMS created ... sending...
SMS reply sent
outcome=finished
```

| Observation | Interpretation / next check |
| --- | --- |
| No history record | Check whether ModemManager received the SMS and whether the listener is attached |
| History record, `command_eligible=False` | Check backlog/pre-arm classification, timestamp, and gateway arming |
| `denied-authentication` | Verify the configured SMS password; changing password policy does not change it |
| `rate-limited` | Wait for admitted requests to age out of the rolling window |
| `executed`, no reply-send start | A previous send may hold the shared reply lock |
| `SMS created`, no completion | Inspect modem transport; the gateway has no send recovery deadline |
| Send error | Delivery failed at the API/transport level; inspect ModemManager diagnostics |
| `SMS reply sent`, no handset message | API completed; handset delivery is not confirmed |

Discover the modem ID before inspecting it; it need not equal the WWAN suffix:

```sh
sudo mmcli -L
sudo mmcli -m <modem-id>
sudo mmcli -m <modem-id> --messaging-list-sms
sudo mmcli -s <modem-sms-id>
```

Inspecting an incoming modem object can expose its original SMS/password. Do not
paste unmasked incoming message bodies into diagnostic reports.

A legacy standalone `sms-command.service` is not part of this implementation.
Its status does not establish whether the integrated gateway is armed. Verify
`SMS command gateway armed` and the active WWAN configuration instead. Restarting
the WWAN manager can interrupt cellular connectivity and is not guaranteed to
resolve modem/carrier delivery faults.

## 9. Source map

| File | Responsibility |
| --- | --- |
| `data/wwan/sms-command-verbs.json` | Verb, wrapper verb, tier, and optional confirmation policy |
| `python/vyos/utils/wwan/sms_command.py` | Configuration defaults/validation, command authentication/execution/replies, receive context, masking, completion/retries, and thin subclass hooks reusing the original SMS methods |
| `python/vyos/utils/wwan/interfaces_wwan_state_machine.py` | Unmodified existing FSM and single implementation of ordinary SMS operations |
| `python/vyos/utils/wwan/interfaces_wwan_service_manager.py` | Instantiates the command-capable subclass through the factory |
| `python/vyos/utils/wwan/interfaces_wwan_config.py` | Runtime configuration replacement/removal and restart restoration |
| `python/vyos/utils/auth.py` | Shared login/SMS policy helper/defaults, strength checker, SMS hashing/verification |
| `src/conf_mode/system_login.py` | Login flow using shared policy helper; existing login hashing retained |
| `src/conf_mode/interfaces_wwan.py` | Policy lookup, optional activation, hash writeback, FSM configuration |
| `interface-definitions/include/interface/wwan-sms-command.xml.i` | Gateway CLI leaves/help |
| `src/completion/list_sms_command_verbs.py` | Catalog-backed privileged-verb completion |
| `src/op_mode/wwan_sms.py` | Existing SMS management operations |
| `src/tests/test_sms_command.py` | Workstation gateway/configuration/lifecycle tests |
| `smoketest/scripts/cli/test_interfaces_wwan_sms_command.py` | Opt-in target CLI smoke tests |

The installed catalog is `/usr/share/vyos/wwan/sms-command-verbs.json`. It is loaded
lazily and cached. Checkouts fall back to the in-tree catalog only when the
installed file is absent; malformed installed data fails closed. Adding a valid
action catalog entry requires no per-command dispatch branch.

### Maintenance boundaries

`sms_command.py` is organized into protocol/encoding constants, catalog/message
helpers, shared CLI/D-Bus configuration validation, bounded subprocess execution,
`Gateway`, and `SmsCommandRuntime`.
The runtime tracks command-specific state. Thin subclass hooks reuse the host's
original methods; no instance methods are monkey-patched. The service-manager
factory is the integration point, and the state-machine file must remain unchanged.
Ordinary SMS methods remain
in `interfaces_wwan_state_machine.py`, grouped into history persistence,
outgoing/public operations, and incoming listener lifecycle. Fixed GSM alphabets,
number patterns, and masking/truncation markers have one definition in the command
module. Keep existing state-machine code in place; avoid optional refactoring of
that file. Keep SMS command changes in `sms_command.py` and its integration/tests.
Reply sizing counts units without allocating a per-character cost list.

Keep policy and D-Bus input validation in `sms_command.py`, CLI hash
writeback in `interfaces_wwan.py`, and operational display in `wwan_sms.py`.
Changes to unrelated connection/failover logic are not needed to extend SMS
command handling. Preserve subscribe-before-drain, persist-before-dispatch/delete,
and the difference between incoming credential masking and unmodified outgoing
command output when maintaining these paths.

## 10. Validation and implementation boundaries

Run workstation tests from the repository root:

```sh
PYTHONPATH=python python3 -m unittest discover -s src/tests -p test_sms_command.py
```

The latest run for this source state passed 106 tests. A behavior-preserving
cleanup also matched 3,054 before/after helper and validation comparisons,
including invalid inputs and GSM/Unicode boundaries. The suite uses real Passlib
hashes and subprocesses. An AST harness loads the original SMS methods and composes the real command
hooks around them without loading all target-only dependencies. Tests verify
that send/read/delete methods are inherited unchanged and that disabling the
gateway preserves original storage/transition/configuration behavior. Positive password-policy
checks mock strength results where workstation Cracklib is unavailable; actual
Cracklib behavior needs target validation. Shared policy behavior was also compared
with the former login helper across 1,344 cases with identical results. The
separate WWAN bearer-reconciliation and SIM-detection suites passed 37 tests
after the SMS organization changes; ordinary SMS methods now remain in the
state-machine class. The final cleanup run
passed all 143 SMS/WWAN tests. All six SMS-related operational XML
inputs and the SMS configuration subtree passed schema validation. A standalone
schema check of the complete WWAN interface definition failed outside this
cleanup; that definition was unchanged. This does not constitute a successful
full image build or target CLI validation.

On a configured lab WWAN interface, run the opt-in CLI test:

```sh
sudo env IGOS_SMS_TEST_INTERFACE=wwan0 python3 /usr/libexec/vyos/tests/smoke/cli/test_interfaces_wwan_sms_command.py
```

Its standard shim saves/restores configuration. It changes gateway configuration
but does not execute action commands.

After installing the current build, validate:

- Password hash writeback/rotation; policy absent, default-enabled, and customized;
  whitespace rejection; invalid imported hashes.
- Full international-number matching, modem-omitted `+`, and rejection of numbers
  sharing only a suffix, including confirmation ownership and privileged lists.
- Short and long command output, errors, truncation, GSM extensions, Unicode,
  handset reassembly, and unchanged words that match the configured password.
- Wrong-password/unauthorized silence, rolling rate limits, and confirmation usage
  counting toward the same limit.
- Confirmation expiry, replay rejection, config changes while pending, and a safe
  action in an appropriate lab maintenance window.
- Pre-boot backlog, reconnect, repeated notifications, multipart completion,
  transient receive/save failures, and preservation of loaded history after a
  failed redaction writeback.
- Slow commands/timeouts while modem monitoring continues; slow/failed outgoing
  transport without assuming handset delivery from API completion.

Earlier lab observations on 2026-11-12 showed successful command execution with
slow/failing modem sends, including RP cause 38 and short sends completing after
about 118 and 66 seconds. On the reinstalled router on 2026-11-13, a manual send
completed in about 0.5 seconds, but an incoming command had no gateway execution
logs and the runtime cache lacked gateway configuration. These observations came
from different installed/runtime states and modems; they do not validate all
latest source changes or establish a universal carrier/firmware cause.

Not implemented: automatic outgoing retries/send-stall recovery, delivery-receipt
tracking, periodic inbox polling, live rate-counter CLI, `show` subtree filtering,
or transactional exactly-once execution. Ongoing recovery after the listener's
bounded retries is also not implemented. Outgoing retries/receipts and subtree
filtering were outside the original v1 requirements; periodic polling is excluded
by the chosen event-driven design.
