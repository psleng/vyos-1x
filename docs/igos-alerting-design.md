# igOS Alerting — Design Sketch

Status: **Proposed design (2026-11-25). Decisions in §11 are proposals, not yet
locked. No code written yet.** Supersedes the earlier alerting roadmap.

Scope: ship a **packaged catalog of standard alerts** with vyos-1x, let the
operator turn them on from the CLI (or GUI) with a few `set` commands, keep the
existing **custom `service event-handler` filters** working exactly as they do
today, and deliver every alert — packaged or custom — through **one pipeline**
to **SMS, email, scripts, the GUI and the cloud**.

The starting point for the catalog is the set of alerts the GUI/cloud team
preselected on the GUI alarm page (§0.2). Several of those presets do not match
the log lines the unit actually produces (§0.3); this design corrects them and
moves them out of the GUI into vyos-1x, so CLI users get the same alerts.

The GUI becomes a **client** of this feature. It does not own rule definitions,
does not scan logs and does not deliver notifications itself.

---

## 0. Context / grounding facts

### 0.1 `service event-handler` — the existing CLI filter engine

- CLI: [`service_event-handler.xml.in`](../interface-definitions/service_event-handler.xml.in).
  Each `event <name>` has `filter pattern <regex>`, optional
  `filter syslog-identifier <id>`, and `script path|arguments|environment`.
- Conf-mode: [`service_event-handler.py`](../src/conf_mode/service_event-handler.py)
  requires **both** `filter pattern` and `script path`, reserves the `message`
  environment variable, writes `/run/vyos-event-handler.conf` (JSON) and
  restarts `vyos-event-handler.service`; with no config it stops the service.
- Runtime: [`vyos-event-handler.py`](../src/system/vyos-event-handler.py)
  - Opens the journal, `seek_tail()`s (no replay of history) and polls for new
    entries. Entries from its own PID are skipped.
  - Matching is `SYSLOG_IDENTIFIER` equality (if configured) plus
    `re.fullmatch(pattern, MESSAGE)` — a **full-message** match, which is why
    every working pattern is wrapped in `.*…​.*`.
  - On match it runs the script **synchronously inside the journal loop** via
    `run()`, with the matched text in the `message` environment variable.
    A slow script delays matching of every subsequent journal entry.
  - The per-event environment dict is built once and mutated on every match
    (`message` is overwritten in place).
- The unit is [`vyos-event-handler.service`](../src/systemd/vyos-event-handler.service)
  (`Restart=always`).
- There is **no** op-mode command for event-handler today.

### 0.2 The preselected alerts (GUI alarm page)

The GUI alarm page in `flask-react-dev`
(`react/src/pages/system/AlarmPage.tsx`, mirrored in
`flask/app/core/system/event_handler/models.py`) offers 11 event types in 6
categories plus "Manually Enter Custom Event". They are identical on every
`flask-react-dev` branch. Each preset only pre-fills `syslog-identifier` and
`filter pattern`; the result is written to the box as an ordinary
`service event-handler event` entry.

| Category | GUI type | Syslog identifier | Pattern as shipped | Parameter |
|---|---|---|---|---|
| Interface | `interface_up` | `vyos-netlinkd` | `RTM_NEWLINK.*<intf>,.*state=UP.*` | interface |
| Interface | `interface_down` | `vyos-netlinkd` | `RTM_NEWLINK.*<intf>,.*state=DOWN.*` | interface |
| Service | `service_up` | `systemd` | `.*Started <unit description>.*` | service |
| Service | `service_down` | `systemd` | `.*Stopped <unit description>.*` | service |
| Login | `login_success` | *(any)* | `.*pam_unix\(([^c]\|c[^r]\|cr[^o]\|cro[^n]).*:session\): session opened.*` | — |
| Login | `login_failed` | *(any)* | `.*pam_unix\(([^c]\|c[^r]\|cr[^o]\|cro[^n]).*:auth\): authentication failure;.*` | — |
| VRRP | `vrrp_status` | `keepalived-fifo.py` | `.*INSTANCE <group> changed state to .*` | VRRP group |
| DHCP server | `dhcp_server_binding` | `kea-dhcp4` | `.*LEASE_ALLOCATED.*` | — |
| DHCP server | `dhcp_server_release` | `kea-dhcp4` | `.*LEASE_RELEASED.*` | — |
| DHCP client | `dhcp_client_bound` | `dhclient` | `.*<intf>: bound to.*` | interface |
| DHCP client | `dhcp_client_release` | `dhclient` | `.*<intf>: released.*` | interface |

The service list is fixed in the GUI: DHCP, DHCPv6, IPsec, Router
Advertisement, SNMP, SSH, VRRP, each mapped to a systemd unit *description*
string (e.g. SSH → `OpenBSD Secure Shell server`).

Each GUI event also carries a display name, description, GUI level
(`critical`/`warning`/`info`, default `warning`), an SMS toggle + number, and an
email toggle.

### 0.3 Verification of the preselected patterns

Checked against the producing code in vyos-1x, upstream daemon sources, and by
running the regexes against representative log lines.

| GUI type | Result | Evidence |
|---|---|---|
| `interface_up` / `_down` | **Matches, but noisy.** | [`vyos-netlinkd`](../src/services/vyos-netlinkd) logs `RTM_NEWLINK -> {ifname}, state={operstate}, mac={mac}` at `LOG_DEBUG` for *every* RTM_NEWLINK, not just transitions, so the alert repeats while the link stays up/down. The unanchored `.*eth0,` also matches `veth0,`. Netlink events are deferred (not dropped) while a commit is in progress. |
| `service_up` / `_down` | **Partially works.** | Matches systemd's text only when the GUI description equals the unit's `Description=`. VyOS's per-VRF Kea units use different descriptions (`Kea IPv4 DHCP daemon`, [`isc-kea-dhcp4-server@.service`](../src/etc/systemd/system/isc-kea-dhcp4-server@.service)). A **crash is not a "Stopped"** — systemd logs a unit *failure* instead — so `service_down` misses the case operators care about most. Every config-commit restart fires both alerts. |
| `login_success` | **Matches far too much.** | The regex excludes only PAM services starting with `cron`. `sudo` and `systemd-user` sessions also match, and many op-mode commands run through `sudo`, so routine CLI use raises "login" alerts. Logins authenticated by TACACS+/RADIUS may not produce a `pam_unix` session line (to verify on unit). |
| `login_failed` | **Works for local accounts.** | `pam_unix(sshd:auth): authentication failure; …` matches. Remote-AAA failures and `Invalid user` probes are not `pam_unix` lines. No threshold, so a brute-force run sends one alert per attempt. |
| `vrrp_status` | **Works.** | [`keepalived-fifo.py`](../src/system/keepalived-fifo.py) logs `INSTANCE {name} changed state to {state}` via `SysLogHandler` with `%(filename)s:` as the identifier. |
| `dhcp_server_binding` / `_release` | **Never matches.** | Kea (2.4, 2.6 and 3.0) logs `DHCP4_LEASE_ALLOC … lease X has been allocated for N seconds` and `DHCP4_RELEASE … address X was released properly.` The strings `LEASE_ALLOCATED`/`LEASE_RELEASED` do not exist. VyOS's Kea logger defaults to `info`, so the real messages are present. |
| `dhcp_client_bound` / `_release` | **Never matches.** | ISC dhclient 4.4.3 logs `bound to %s -- renewal in %ld seconds.` — no interface name — and `DHCPRELEASE of %s on %s to %s port %d`. No VyOS dhclient hook logs `<intf>: bound to` or `<intf>: released`. One dhclient runs per interface as `dhclient@<intf>.service`, so the interface must come from the journal's `_SYSTEMD_UNIT` field, which `service event-handler` cannot filter on today. |

Conclusion: 4 of 11 presets never fire, 1 works only for some services and
misses crashes, 1 fires on routine `sudo`, and the interface pair repeats. The
catalog in §4 fixes each one, and most fixes need **structured journal-field
matching** (§6), not just a better regex.

### 0.4 How the GUI delivers alerts today

- Every GUI-created event uses the script
  `/opt/flask/flask/app/callback/scripts/gui_event_notification.py`, configured
  in `flask-react-dev/flask/config.json` as `event_gui_notification`. Its
  settings travel as script environment variables: `event_name`,
  `description`, `gui_level`, `sms_number`, `email_enabled`.
- The script POSTs to the Flask `/callback` endpoint
  (`http://127.0.0.1:5000/callback`). Flask stores the event, pushes a
  Socket.IO notification to **all** logged-in users, and, if `email_enabled`,
  sends email.
- **SMS is not delivered.** `event_sms_notification` is `""` in `config.json`,
  so the SMS toggle and number are stored but nothing sends them.
- **Email reuses the test-email command.** Flask calls the `EMAIL_TEST` API,
  which runs [`generate_test_email.py`](../src/op_mode/generate_test_email.py).
  Every enabled `system email` recipient gets a message with the subject
  `…: Operational Test Message` and a `Custom Message:` body. There is no
  per-event recipient choice.
- **Everything depends on Flask.** If the web service is stopped, not
  installed, or restarting, no alert is delivered by any channel.
- **It blocks the matcher.** The script runs inside the event-handler loop and
  retries 3 times (3 s connect + 5 s read timeout, 1 s/2 s backoff). One
  unreachable callback stalls all log matching for up to ~27 s per match.
- **Weak callback authentication.** The HMAC covers only the timestamp, not the
  body, and the secret defaults to the literal `secret` when `CALLBACK_SECRET`
  is unset.
- **Presets are GUI-only.** CLI users cannot select them by name; they see only
  the resulting regex in the config.

### 0.5 Delivery facilities already on the unit

- **Email:** `system email`
  ([`system_email.xml.in`](../interface-definitions/system_email.xml.in),
  [`system_email.py`](../src/conf_mode/system_email.py)) defines SMTP
  `profile`s, an `active-profile`, and named `recipient`s (`enable`, `email`,
  `subject`). Conf-mode renders an `msmtprc` with one `account` per profile.
- **SMS:** `WWANClient.send_sms(interface_number, recipient, message)` in
  [`wwan_client.py`](../python/vyos/utils/wwan/wwan_client.py) calls the
  `igos-wwan-manager` over D-Bus. It needs a registered modem and is tied to a
  specific `wwanN`. Long texts are concatenated by ModemManager.
- **WWAN alerts:** the WWAN FSM emits normalized alerts on the D-Bus AlertBus,
  and [`interfaces_wwan_service_manager.py`](../python/vyos/utils/wwan/interfaces_wwan_service_manager.py)
  bridges every alert into journald with `SYSLOG_IDENTIFIER=igos-wwan-alertbus`
  and structured fields (`WWAN_ALERT_CODE`, `WWAN_ALERT_SEVERITY`,
  `WWAN_ALERT_CATEGORY`, `WWAN_ALERT_INTERFACE`, `WWAN_ALERT_ID`, …), plus a
  JSON copy under `igos-wwan-alertbus-json`. Codes emitted today:
  `WWAN_BEARER_UP`, `WWAN_BEARER_DOWN`, `WWAN_SIM_FAILOVER`, `WWAN_SIM_SWITCH`,
  `WWAN_SIM_REMOVED`, `WWAN_SIM_INSERTED`, `WWAN_FSM_FAILED`,
  `WWAN_RECONNECT_ATTEMPT`, `WWAN_USAGE_WARNING`, `WWAN_USAGE_LIMIT_EXCEEDED`.

### 0.6 Cloud reachability

`igos-cloud-proxy` is **pull-only**: the unit long-polls PerleCLOUD for
commands and executes `EXECUTE_REST_COMMAND`s against the local VyOS HTTP API
through its gateway (`/show`, `/retrieve`, `/graphql`, …). PerleCLOUD cannot
receive device-initiated events today. Op-mode scripts listed in
[`op-mode-standardized.json`](../data/op-mode-standardized.json) are also
exposed through GraphQL. Therefore an alert history readable through a `show`
command is reachable from the cloud **with no protocol change**. True push would
be a new cloud-proxy message type, owned by that team.

---

## 1. Goals and non-goals

Goals:

1. A versioned **catalog of standard alerts** shipped in the package and
   usable from the CLI by name.
2. **Custom filters keep working unchanged**; they gain the same notification
   options as packaged alerts.
3. **One delivery pipeline** for SMS, email, scripts, GUI and cloud, running
   independently of the web GUI.
4. Matching never blocks on delivery.
5. Packaged alerts are **opt-in**: an upgrade never starts sending messages on
   its own.
6. Existing GUI-created events are migrated without loss.

Non-goals (v1):

- Metric thresholds (CPU, memory, conntrack table, temperature). These need a
  poller, not a log matcher; see §13.
- Device-initiated cloud push (needs a cloud-proxy protocol extension).
- Alert acknowledgement workflows or escalation chains.
- Replacing SNMP traps; the WWAN trap emitter stays as-is.

---

## 2. Architecture

```
                    journald
                       │  (follow, structured fields)
                       ▼
 ┌─────────────── vyos-event-handler (extended) ───────────────┐
 │  rule set = custom `event`s (config)                        │
 │           + packaged `alert` instances (catalog + config)   │
 │                                                             │
 │  match fields + MESSAGE regex ─► normalize ─► edge/dedupe/  │
 │                                               hold-down     │
 │                                      │                      │
 │                                      ▼                      │
 │                                alert record                 │
 │                    ┌─────────────────┼──────────────────┐   │
 │                    ▼                 ▼                  ▼   │
 │             history file      journald copy      delivery queue
 │             (bounded)         (igos-alert)       (bounded, on disk)
 └────────────────────┼─────────────────┼──────────────────┼───┘
                      ▼                 ▼                  ▼
          show event-handler      remote syslog,    workers: email (msmtp),
          alerts  ──► GUI,        GUI live feed     SMS (WWANClient), script
          cloud (via gateway)
```

Design rules:

- **One reader, one rule engine.** Extend `vyos-event-handler`; do not add a
  second journal follower. Packaged alerts and custom events are evaluated by
  the same code.
- **One route per event.** WWAN alerts enter **only** through their existing
  journald bridge (§0.5), matched on `WWAN_ALERT_CODE`. Nothing subscribes to
  the D-Bus AlertBus for this feature, so WWAN alerts cannot be delivered twice.
- **Match, record, enqueue, return.** The journal loop never runs a script,
  sends mail or calls D-Bus. Delivery workers drain the queue.
- **No dependency on the GUI.** Delivery runs in vyos-1x. Flask only reads.
- **Loop safety.** Records written to journald use
  `SYSLOG_IDENTIFIER=igos-alert`, and the matcher always ignores that
  identifier, in addition to the existing own-PID skip.

### 2a. Zero footprint when unconfigured

With no `service event-handler` config, conf-mode stops the service (as today)
and nothing else runs. The catalog is read only by conf-mode and the running
daemon, and only when at least one `alert` is configured. Op-mode
`show event-handler alert-types` reads the catalog on demand.

---

## 3. Alert record

Every match produces one record. It is written to the history file, to journald
(as structured fields), and passed to delivery workers.

| Field | Type | Notes |
|---|---|---|
| `id` | string | UUID, unique per record |
| `kind` | string | `alert`; delivery-result records use `delivery` (§7d) |
| `sequence` | int | Monotonic, continues across reboots and `clear` (§7d); for ordering and paging |
| `timestamp` | string | ISO-8601 UTC, taken from the journal entry (`__REALTIME_TIMESTAMP`) |
| `hostname` | string | `system host-name` |
| `rule` | string | Config tag: the `alert <name>` or `event <name>` |
| `type` | string | Catalog type ID (e.g. `interface-down`), or `custom` |
| `severity` | string | `critical`, `warning`, `info` |
| `category` | string | `interface`, `service`, `login`, `vrrp`, `dhcp`, `wwan`, `custom` |
| `object` | string | What it is about: `eth0`, `ssh.service`, VRRP group, … |
| `state` | string | Optional: `up`/`down`, `MASTER`/`BACKUP`/`FAULT`, … |
| `message` | string | Rendered human summary (catalog template or raw `MESSAGE`) |
| `log` | object | Source journal fields used: `SYSLOG_IDENTIFIER`, `_SYSTEMD_UNIT`, `MESSAGE` |
| `suppressed` | int | Matches folded into this record by hold-down (§7e) |

Secrets are never copied into records. Only the listed journal fields are
kept.

---

## 4. Packaged catalog

### 4a. File and schema

One JSON file, installed with the rest of `data/` (same pattern as
`op-mode-standardized.json` and the SMS command verb catalog):

- Source: `data/alerts/catalog.json`
- Installed: `/usr/share/vyos/alerts/catalog.json`

```json
{
  "version": 1,
  "types": {
    "interface-down": {
      "category": "interface",
      "description": "Interface operational state changed to DOWN",
      "severity": "critical",
      "parameters": {
        "interface": { "required": true, "validator": "interface-name" }
      },
      "match": {
        "SYSLOG_IDENTIFIER": "vyos-netlinkd",
        "MESSAGE": "RTM_NEWLINK -> {interface}, state=DOWN, mac=.*"
      },
      "edge": { "key": "{interface}", "group": "interface-oper-state" },
      "object": "{interface}",
      "state": "down",
      "message": "Interface {interface} is down"
    }
  }
}
```

Rules:

- `match` is a map of journal field → value. `MESSAGE` and any field ending in
  `_RE` is a regex; every other field is an exact string compare. All
  regexes use `re.fullmatch`, consistent with custom events.
- `{param}` placeholders are substituted with `re.escape(value)` after the value
  passes its validator. Operator input is never interpreted as regex.
- Named regex groups (`(?P<state>…)`) are available to `object`, `state` and
  `message` templates.
- `edge` (optional) suppresses repeats: the record is emitted only when the
  `state` for `key` within `group` differs from the last one seen. This is what
  fixes the repeating netlinkd events.
- `suppress_during_commit` (optional, bool) ignores matches while a config
  commit is in progress. It is used for service start/stop, where every commit
  restarts daemons on purpose.
- Type IDs are **append-only**. A type may be deprecated (`"deprecated": true`)
  but never renamed or reused, because the IDs are stored in user configs.

### 4b. Initial catalog (v1)

Derived from the GUI presets (§0.2), corrected per §0.3, plus the WWAN codes
(§0.5). Patterns marked † must be confirmed on the reference unit before the
catalog is locked (§12).

| Type | Match (journal fields) | Params | Default severity | Fixes / notes |
|---|---|---|---|---|
| `interface-up` | `SYSLOG_IDENTIFIER=vyos-netlinkd`, `MESSAGE=RTM_NEWLINK -> {interface}, state=UP, mac=.*` | interface | info | Exact name (no `veth0` false hit); `edge` on interface state |
| `interface-down` | same, `state=DOWN` | interface | critical | as above |
| `service-start` | `MESSAGE_ID=39f53479d3a045ac8e11786248231fbf`, `UNIT={unit}` | service | info | systemd "start job finished"; matches on unit name, not description; `suppress_during_commit` |
| `service-stop` | `MESSAGE_ID=9d1aaa27d60140bd96365438aad20286`, `UNIT={unit}` | service | warning | systemd "stop job finished"; `suppress_during_commit` |
| `service-failed` | `MESSAGE_ID=d9b373ed55a64feb8242e02dbe79a49c`, `UNIT={unit}` | service | critical | **New.** systemd "Unit failed" — the crash case the GUI preset missed |
| `login-success` | `SYSLOG_IDENTIFIER=sshd`, `MESSAGE=Accepted (?P<method>\S+) for (?P<user>\S+) from (?P<host>\S+) port \d+.*` | — | info | sshd's own line: covers local, TACACS+ and RADIUS; never fires on `sudo` |
| `login-success-console` † | `SYSLOG_IDENTIFIER=login`, `MESSAGE=pam_unix\(login:session\): session opened for user (?P<user>[^(\s]+).*` | — | info | Serial/console logins only |
| `login-failed` | `SYSLOG_IDENTIFIER=sshd`, `MESSAGE=(Failed \S+ for (invalid user )?(?P<user>\S+) from (?P<host>\S+) port \d+.*\|Invalid user (?P<user2>\S+) from (?P<host2>\S+).*)` | — | warning | Covers remote AAA and unknown users; rely on hold-down (§7e) to avoid brute-force floods |
| `vrrp-state` | `SYSLOG_IDENTIFIER=keepalived-fifo.py`, `MESSAGE=INSTANCE (?P<group>{group}) changed state to (?P<state>\S+)` | group (optional; omitted = any) | warning | Unchanged behavior, anchored, state captured |
| `dhcp-server-lease` † | `SYSLOG_IDENTIFIER=kea-dhcp4`, `MESSAGE=.*DHCP4_LEASE_ALLOC .*lease (?P<address>\S+) has been allocated.*` | — | info | Correct Kea message ID; high volume — no SMS by default (§5e) |
| `dhcp-server-release` † | `SYSLOG_IDENTIFIER=kea-dhcp4`, `MESSAGE=.*DHCP4_RELEASE .*address (?P<address>\S+) was released properly\.` | — | info | Correct Kea message ID |
| `dhcp-client-bound` † | `_SYSTEMD_UNIT=dhclient@{interface}.service`, `MESSAGE=bound to (?P<address>\S+) -- renewal in \d+ seconds\.` | interface | info | Interface comes from the unit, not the text |
| `dhcp-client-release` † | `SYSLOG_IDENTIFIER=dhclient`, `MESSAGE=DHCPRELEASE of (?P<address>\S+) on {interface} to .*` | interface | info | dhclient's real text includes the interface |
| `wwan-bearer-down` | `SYSLOG_IDENTIFIER=igos-wwan-alertbus`, `WWAN_ALERT_CODE=WWAN_BEARER_DOWN` | interface (optional) | critical | Via existing journald bridge |
| `wwan-bearer-up` | … `WWAN_BEARER_UP` | interface (optional) | info | |
| `wwan-sim-failover` | … `WWAN_SIM_FAILOVER` | interface (optional) | warning | |
| `wwan-sim-switch` | … `WWAN_SIM_SWITCH` | interface (optional) | info | |
| `wwan-sim-removed` | … `WWAN_SIM_REMOVED` | interface (optional) | critical | |
| `wwan-sim-inserted` | … `WWAN_SIM_INSERTED` | interface (optional) | info | |
| `wwan-fsm-failed` | … `WWAN_FSM_FAILED` | interface (optional) | critical | |
| `wwan-reconnect-attempt` | … `WWAN_RECONNECT_ATTEMPT` | interface (optional) | info | Can be frequent; hold-down applies |
| `wwan-usage-warning` | … `WWAN_USAGE_WARNING` | interface (optional) | warning | |
| `wwan-usage-limit` | … `WWAN_USAGE_LIMIT_EXCEEDED` | interface (optional) | critical | |

For WWAN types, the optional `interface` parameter (`wwanN`) is matched against
`WWAN_ALERT_INTERFACE` (the interface number `N`); severity defaults to the
value in `WWAN_ALERT_SEVERITY` unless the operator overrides it.

The `service` parameter takes a **systemd unit name** (`ssh.service`,
`isc-kea-dhcp4-server.service`, `keepalived.service`, …). Conf-mode offers
completion for the service set the GUI used (DHCP, DHCPv6, IPsec, Router
Advertisement, SNMP, SSH, VRRP) mapped to unit names, but accepts any unit name
so VRF-instance units (`…@<vrf>.service`) also work.

---

## 5. CLI surface

Everything stays under `service event-handler`, so existing configs are not
moved and there is one daemon and one tree. Illustrative shape — not final.

### 5a. Notification targets

```
# Email: reference recipients already defined under `system email`
#   (no new SMTP settings here; uses the system email active-profile)

# SMS: named recipients
set service event-handler sms-recipient oncall number '+15555551234'
set service event-handler sms-recipient oncall interface wwan0   # optional; default: first connected wwan
```

### 5b. Packaged alerts

```
set service event-handler alert wan-down type interface-down
set service event-handler alert wan-down interface eth0
set service event-handler alert wan-down severity critical       # optional override
set service event-handler alert wan-down notify email ops         # system email recipient
set service event-handler alert wan-down notify sms oncall        # sms-recipient
set service event-handler alert wan-down notify script /config/scripts/wan-down.sh
set service event-handler alert wan-down disable                  # keep config, stop matching

set service event-handler alert ssh-fail type login-failed
set service event-handler alert ssh-fail notify email ops
```

An `alert` with no `notify` is still recorded in history (visible to GUI and
cloud) — "monitor only".

### 5c. Custom events (existing tree, extended)

```
set service event-handler event my-filter filter pattern '.*something.*'     # existing
set service event-handler event my-filter filter syslog-identifier 'foo'     # existing
set service event-handler event my-filter script path /config/scripts/x.sh  # existing, now optional
set service event-handler event my-filter severity warning                   # new, default warning
set service event-handler event my-filter notify email ops                   # new
set service event-handler event my-filter notify sms oncall                  # new
```

Custom events keep `re.fullmatch` against `MESSAGE` and the `message`
environment variable for scripts. Every custom match is now also recorded in
history, so it appears in the GUI and cloud without extra configuration.

### 5d. Global settings

```
set service event-handler history size 1000          # records kept (default 1000)
set service event-handler hold-down 60               # seconds; per rule+object (default 60)
set service event-handler rate-limit sms 20          # max SMS per hour, all rules (default 20)
set service event-handler rate-limit email 60        # max emails per hour (default 60)
```

### 5e. `verify()` rules

- `alert <name> type` is required and must exist in the installed catalog
  (completion comes from the catalog). A deprecated type is allowed with a
  warning.
- Required catalog parameters must be set; each is checked by its validator.
  Parameters the type does not define are rejected.
- `event <name>` needs `filter pattern`, plus at least one of `script path` or
  `notify`. The pattern must compile.
- `notify email <r>` must name an existing `system email recipient`, and
  `system email active-profile` must be set.
- `notify sms <r>` must name an existing `sms-recipient`; its `interface`, if
  set, must be a configured `wwan` interface.
- The `message` environment variable stays reserved.
- **Warning** (not error) when SMS is selected on a high-volume type
  (`dhcp-server-lease`, `dhcp-server-release`, `wwan-reconnect-attempt`).

### 5f. Op-mode

```
show event-handler alerts                       # newest first
show event-handler alerts count 50
show event-handler alerts severity critical
show event-handler alerts since 812             # records after sequence 812 (§7d)
show event-handler deliveries                   # delivery results by channel (§7d)
show event-handler alert-types                  # installed catalog
show event-handler status                       # queue depth, last delivery result per channel
clear event-handler alerts
```

Implemented as a standardized op-mode script (`src/op_mode/event_handler.py`)
and added to `op-mode-standardized.json`, so `--raw` JSON and GraphQL work for
the GUI and the cloud.

---

## 6. Matching engine changes (`vyos-event-handler.py`)

1. **Structured field matching.** Generalize a rule's filter from
   `{syslog-identifier, pattern}` to a field map (§4a). Custom events map to
   `{SYSLOG_IDENTIFIER: id, MESSAGE: pattern}` with today's semantics.
2. **Load the catalog** and expand each configured `alert` into a compiled rule
   (parameters substituted and escaped).
3. **Edge state** per `edge.group`/`key` in memory. On start, the first event
   for a key is emitted (unknown → known). Edge state is not persisted.
4. **Commit suppression** for rules with `suppress_during_commit`, using the
   same `commit_in_progress` helper as `vyos-netlinkd`.
5. **Record and enqueue** (§7). The loop does no I/O beyond appending to the
   history file and `journal.send()`.
6. **Scripts move to a worker** with a timeout (default 30 s) and a fresh
   environment copy per run, fixing the shared-dict mutation in §0.1.
   Existing variables (`message`, configured environment) are unchanged; new
   `ALERT_*` variables (`ALERT_ID`, `ALERT_TYPE`, `ALERT_SEVERITY`,
   `ALERT_OBJECT`, `ALERT_STATE`, `ALERT_MESSAGE`) are added.
7. **Ignore `SYSLOG_IDENTIFIER=igos-alert`** to prevent feedback loops.

Known limit (unchanged): events logged while the daemon is down are not
replayed. `Restart=always` keeps that window short. Persisting the journal
cursor is a possible later improvement.

---

## 7. Delivery

### 7a. Queue

- One bounded on-disk queue per channel under `/var/lib/vyos/alerts/queue/`
  (survives daemon restart and reboot, mode `0700`).
- Workers retry transient failures with exponential backoff (2 s → 5 min cap)
  and give up after 24 h.
- On overflow, drop the oldest `info` item first, then the oldest `warning`;
  `critical` items are dropped last. Drops are counted and shown in
  `show event-handler status`.
- A failing channel never blocks another channel or the matcher.

### 7b. Email

- Reuse `system email`: the active profile's `msmtp` account and the selected
  recipients' `email` and `subject` prefix.
- Lift the send logic out of `generate_test_email.py` into a library helper
  (e.g. `vyos.utils.email.send_mail()`), used by both the test command and
  alerting. Alerts get a real message, not "Operational Test Message":
  - Subject: `<recipient subject or "igOS Alert"> [<hostname>] <SEVERITY>: <message>`
  - Body: record fields (time, type, object, state, message, source log line).

### 7c. SMS

- Use `WWANClient.send_sms()`, run from the worker's own asyncio loop.
- Modem: the recipient's `interface`, otherwise the first connected `wwan`
  interface.
- Text: `[<hostname>] <SEVERITY> <message>`, truncated to 160 characters
  (one segment) to bound cost.
- If no modem can send, the item stays queued and is retried. A modem that is
  registered can usually still send SMS when its data bearer is down, so SMS is
  the preferred channel for WAN-down alerts.

### 7d. GUI and cloud (history)

- History is a bounded JSON-lines file at `/var/lib/vyos/alerts/history.jsonl`
  (size from `history size`), read by `show event-handler alerts`.
- History lives in the image's overlay on the persistence partition: it
  survives daemon restarts and reboots. **Carry-over across image upgrade is
  undecided (§12).** Today the installer (`add_image()` in
  `src/op_mode/image_installer.py`) migrates only `/config`, SSH keys and
  known_hosts, and `/var/lib/tee`, so history and queues stay with the old
  image.
- **`sequence` resumes; it is never reset by a reboot or `clear`.** A small
  state file `/var/lib/vyos/alerts/state.json` holds `history_id` (random,
  created with the file) and `last_sequence`. On start the daemon continues
  from the larger of `last_sequence` and the last valid record in history
  (a torn final line after power loss is skipped). `state.json` is written on
  `clear event-handler alerts` and on clean shutdown, not per record.
- If history is not carried over, a new image starts with no history, so it
  gets a new `history_id` and `sequence` starts at 1. Readers that keep a
  cursor (GUI, cloud) treat a changed `history_id` as "start over".
- `show event-handler alerts --raw` includes `history_id` and
  `first_sequence` (oldest record still held), and accepts `since <sequence>`,
  so a reader can fetch only new records and detect overflow gaps.
- **Delivery results are recorded in the same history.** After each send
  attempt a worker appends a `delivery` record (no alert of its own) with its
  own `sequence`:

  ```json
  {"kind": "delivery", "sequence": 815, "alert_id": "…",
   "channel": "sms:oncall", "result": "sent", "at": "…", "error": null}
  ```

  `result` is `sent`, `retrying`, `failed`, `gave-up` or `dropped` (queue
  overflow). Alert records carry `"kind": "alert"`. Readers using `since` pick
  up late outcomes for older alerts without rewriting past records, and
  `show event-handler alerts` shows each alert with its delivery results.
  This answers "was the 03:12 WAN-down alert texted to on-call, and did the
  email fail?" — which neither `show event-handler status` (last result per
  channel) nor the queues (items deleted once sent) can. `history size`
  counts alert records; delivery records are pruned with their alert.
- Each record is also sent to journald (`SYSLOG_IDENTIFIER=igos-alert`,
  `ALERT_*` fields), which gives remote syslog forwarding and a live feed for
  the GUI with no extra code.

### 7e. Noise control

- **Hold-down:** repeated matches of the same rule + object within `hold-down`
  seconds are folded into the first record (`suppressed` count), and delivered
  once.
- **Edge detection** (§4a) for state-type alerts.
- **Channel rate limits** (§5d). When the SMS limit is hit, one final
  "N alerts suppressed, see history" SMS is sent per window.

### 7f. Channel integration findings (effort)

Both channels already have working send paths on the unit; wiring them in is
small. The bulk of the work is the matcher, catalog and queue (§6, §7a).

**SMS — small.**

- Existing end-to-end path: `generate interfaces wwan <wwanN> sms number <n>
  message <text>` ([`wwan_sms.py`](../src/op_mode/wwan_sms.py)) →
  `WWANClientSync.send_sms(interface_number, number, text)` → D-Bus →
  FSM `sms_send()` → ModemManager `Messaging.Create` + `Sms.Send`. The SMS
  worker calls the same `send_sms()`.
- D-Bus policy
  ([`com.igos.IgosModemManager.conf`](../src/etc/dbus-1/system.d/com.igos.IgosModemManager.conf))
  already allows `root`, which `vyos-event-handler` runs as. No policy change.
- `sms_send()` raises when the FSM has no modem; the worker treats that as a
  transient failure and keeps the item queued.
- Sent alert SMS are recorded in the existing SMS store, so they appear in
  `show interfaces wwan <wwanN> sms` — useful as a delivery audit.
- Loop risk: the FSM logs `SMS send failed: …`. A broad custom filter could
  match it and queue another SMS. The SMS rate limit (§7e) bounds this.
- If the recipient is also on the SMS command gateway allow-list, their replies
  reach the gateway like any inbound SMS and still need its token — no
  interaction to design for.

**Email — small to medium.**

- The send code lives inside the test command
  ([`generate_test_email.py`](../src/op_mode/generate_test_email.py)), which
  imports the conf-mode script through a `sys.path` hack to read config. Lift it
  into `vyos.utils.email.send_mail(recipients, subject, body)` and make both the
  test command and the email worker call it.
- Only the **active profile** is rendered into `/run/msmtp/msmtprc`
  ([`system_email.py`](../src/conf_mode/system_email.py)), so alert email always
  uses `system email active-profile`. Per-alert profile selection is out of
  scope.
- msmtp has **no timeout** configured, so a dead SMTP server can hang the
  sender. Add `timeout 30` to the rendered `msmtprc` and a subprocess timeout
  in `send_mail()`.
- Build messages with `email.message.EmailMessage` and strip CR/LF from the
  subject: alert text comes from log lines, and a raw string subject allows
  header injection.
- A recipient without `enable` is muted: `notify email <r>` to a disabled
  recipient records the alert but sends nothing.
- **FIPS:** `system email` is rejected entirely under FIPS (msmtp uses GnuTLS),
  so no recipient can exist and `notify email` fails `verify()` naturally. SMS,
  script, GUI and cloud remain available on FIPS units.

**CLI dependencies — small.**

- `service event-handler` references `system email recipient`s and `wwan`
  interfaces. Add entries to
  [`config-mode-dependencies/vyos-1x.json`](../data/config-mode-dependencies/vyos-1x.json)
  so `system_email` and `interfaces_wwan` changes re-run
  `service_event-handler` `verify()` (deleting a referenced recipient or modem
  is then rejected at commit).
- `service event-handler` has priority `2`, ahead of `system email` (`1000`).
  `verify()` reads the proposed config, so references resolve in the same
  commit. At boot, msmtp may not be configured when the daemon starts; queued
  email is delivered once it is.

---

## 8. GUI and cloud integration

GUI (`flask-react-dev`) — **decided: the GUI uses this feature and keeps no
independent alerting path.**

- The alarm page writes `alert`/`event` config through the existing configure
  API, instead of building regexes and script environment variables.
- Event types and parameter lists come from
  `show event-handler alert-types --raw`, not from the hard-coded presets.
- Channel pickers list `system email recipient`s and
  `service event-handler sms-recipient`s from the config.
- Live notifications come from following `igos-alert` in journald (or polling
  `show event-handler alerts`), replacing the `/callback` endpoint and
  `gui_event_notification.py`.
- Flask's own email-on-callback path (`EMAIL_TEST`) and the
  `event_sms_notification`/`event_email_notification` script settings are
  removed; the unit delivers.
- Per-user read/dismiss state can stay in the Flask database; the alerts
  themselves come from the unit.

Cloud:

- PerleCLOUD reads `show event-handler alerts` (or the GraphQL equivalent)
  through the existing gateway; `sequence` lets it fetch only new records.
- Configuration from the cloud uses the same configure API as the GUI.
- Device-initiated push is a later cloud-proxy protocol extension.

Changes made by CLI, GUI, or cloud all land in the same VyOS config and are
visible to the others.

---

## 9. Migration and compatibility

- **Existing custom events:** unchanged. A config with `filter pattern` +
  `script path` behaves as before, plus it is now recorded in history.
- **GUI-created events:** an igOS migration script converts entries whose
  `script path` is the GUI callback. Per AGENTS.md, it goes in
  `src/migration-scripts/igos/1-to-2`, and
  `interface-definitions/include/version/igos-version.xml.i` is bumped from
  `version='1'` to `version='2'`.
  - If `syslog-identifier` + `pattern` equal a known GUI preset template (§0.2),
    convert to `alert <same name> type <catalog type>` with its parameter
    recovered from the pattern (interface, unit name from service description,
    VRRP group). This also *fixes* the broken presets.
  - Otherwise keep it as an `event`, keep its filter, add `notify`.
  - `gui_level` → `severity`.
  - `email_enabled=true` → `notify email <r>` for every currently enabled
    `system email recipient` (same people who receive mail today).
  - `sms_number` → create an `sms-recipient` and **do not** add `notify sms`,
    because SMS was never actually sent; the operator opts in. Logged in the
    migration output.
  - Remove the callback `script path` and the GUI environment variables;
    `event_name`/`description` are dropped (the tag name identifies the rule).
- The GUI ships its matching change (§8) in the same release; the old
  `/callback` path is removed after the migration has shipped.

---

## 10. Security considerations

- **No GUI trust path.** Delivery no longer depends on an HTTP callback with a
  weak, default-secret HMAC (§0.4).
- **Regex safety:** operator parameters are `re.escape`d; custom patterns are
  compiled at `verify()`. Catalog regexes are reviewed data.
- **Secrets:** SMTP credentials stay in `system email`; records never contain
  passwords, keys or full environment dumps.
- **SMS cost/abuse:** per-hour cap, single-segment messages, warning on
  high-volume types.
- **Log injection:** an attacker who can write to the journal (any local
  process via `logger`) can trigger custom filters. Packaged types match on
  `SYSLOG_IDENTIFIER`/`_SYSTEMD_UNIT`/`MESSAGE_ID`, which raises the bar;
  `_SYSTEMD_UNIT` and `_UID` are trusted journal fields that a client cannot
  forge.
- **Files:** queue and history under `/var/lib/vyos/alerts/`, `0700`/`0600`,
  root-owned.

---

## 11. Decisions (proposed — to lock)

1. Stay under `service event-handler` (one tree, one daemon) rather than a new
   `service alert` node.
2. Packaged alerts are opt-in instances (`alert <name> type <id>`); nothing is
   enabled by an upgrade.
3. Every match is recorded in history; GUI/cloud visibility is not a per-rule
   option. SMS, email and script are per-rule.
4. WWAN alerts enter only through the journald bridge.
5. Email reuses `system email` recipients; SMS uses named `sms-recipient`s.
6. Catalog is JSON in `data/alerts/`, type IDs append-only.
7. GUI-created events are migrated; SMS is not auto-enabled by migration.
8. **The GUI is a client of this feature (decided).** It configures alerts
   through the configure API and reads alerts from the unit; its callback
   script, Flask email path and hard-coded presets are retired.
9. Alert email uses only the `system email` active profile.

---

## 12. Open questions / verify on the reference unit

Before locking the catalog, capture real journal lines on the unit
(`journalctl -o json`) for each † row in §4b:

- Kea: confirm `SYSLOG_IDENTIFIER` (`kea-dhcp4`) and the exact
  `DHCP4_LEASE_ALLOC` / `DHCP4_RELEASE` line format at the default `info` level,
  including per-VRF instances.
- dhclient: confirm `_SYSTEMD_UNIT=dhclient@<intf>.service` and the
  `bound to` / `DHCPRELEASE` text.
- Console login: confirm the identifier and text for serial logins, and what
  TACACS+/RADIUS logins log.
- Interfaces: confirm `vyos-netlinkd` debug lines are stored (journald
  `MaxLevelStore`) and check the volume per interface.
- systemd: confirm `MESSAGE_ID` and `UNIT` fields for start/stop/failed on the
  shipped systemd version.

Design questions:

- Default `hold-down` (60 s) and SMS cap (20/h) — acceptable for the field?
- Should `login-failed` have a threshold mode ("N failures in M seconds")
  instead of hold-down only?
- Should edge state be persisted so a reboot does not re-announce every
  interface as "up"?
- Multi-modem units: is "first connected wwan" the right SMS default?
- **Undecided — carry alerts across image upgrade?** If yes, it becomes one
  more step in the image installer (`add_image()`), copying
  `/var/lib/vyos/alerts/` (history, `state.json`, queues) into the new image's
  overlay, the same way `/var/lib/tee` is migrated today. History, `sequence`,
  `history_id` and pending deliveries then continue unbroken. If no, the new
  image starts empty and undelivered items are left behind.

---

## 13. Implementation checklist

### 13.1 Files to create

- `data/alerts/catalog.json` — the catalog (§4).
- `python/vyos/alerts/catalog.py` — load/validate catalog, expand instances.
- `python/vyos/alerts/record.py` — record build, history append, journald copy.
- `python/vyos/alerts/delivery.py` — queue + email/SMS/script workers.
- `python/vyos/utils/email.py` — `send_mail()` lifted from the test command.
- `src/op_mode/event_handler.py` — `show`/`clear` commands (§5f).
- `op-mode-definitions/event-handler.xml.in`.
- `src/migration-scripts/igos/1-to-2` — GUI event migration (§9).
- `src/tests/test_alert_catalog.py` — every catalog entry compiles; every match
  regex hits recorded sample lines (§12) and rejects near-misses (`veth0`,
  `sudo` sessions).

### 13.2 Files to modify

- `interface-definitions/service_event-handler.xml.in` — `alert`, `notify`,
  `severity`, `sms-recipient`, `history`, `hold-down`, `rate-limit`.
- `src/conf_mode/service_event-handler.py` — `verify()` (§5e); render catalog
  expansion and channel settings into `/run/vyos-event-handler.conf`.
- `src/system/vyos-event-handler.py` — §6.
- `src/op_mode/generate_test_email.py` — use `vyos.utils.email.send_mail()`.
- `src/conf_mode/system_email.py` — add `timeout 30` to the rendered
  `msmtprc` (§7f).
- `data/config-mode-dependencies/vyos-1x.json` — `system_email` and
  `interfaces_wwan` re-run `service_event-handler` (§7f).
- `data/op-mode-standardized.json` — add `event_handler.py`.
- `interface-definitions/include/version/igos-version.xml.i` — `1` → `2`.
- `debian/vyos-1x.install` — only if `data/alerts/` is not already covered by
  the existing `usr/share` entry.

### 13.3 Acceptance criteria (on the reference unit)

- `alert x type interface-down interface eth0` + unplug → exactly one record,
  one email, one SMS; replug with `interface-up` → one record.
- `sudo` and op-mode commands do not trigger `login-success`.
- `systemctl kill -s SEGV ssh` → `service-failed` fires; a config commit that
  restarts SSH does not fire `service-start`/`service-stop`.
- A DHCP client renew on a `dhclient` interface fires `dhcp-client-bound`.
- With the SMTP server unreachable, matching continues, email items queue, and
  are delivered after the server returns.
- With no modem, SMS items queue; email still delivers.
- 100 `login-failed` lines in 10 s → one delivered alert with `suppressed=99`.
- An existing custom event with only `script path` behaves exactly as before.
- A config containing GUI-created events migrates to `alert`/`event` +
  `notify`, with SMS not enabled.
- `show event-handler alerts --raw` is reachable through the cloud-proxy
  gateway.
- Deleting a `system email recipient` that an alert references is rejected
  at commit.
- An unreachable SMTP server does not hang the email worker beyond its timeout.

### 13.4 Out of scope for v1

- Metric/threshold alerts (CPU, memory, disk, conntrack, temperature, PKI
  certificate expiry) — a later poller can feed the same record/delivery path.
- Cloud push, acknowledgement, escalation.
- Changes to SNMP trap emission.
