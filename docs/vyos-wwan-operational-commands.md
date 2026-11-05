# IGOS WWAN Enhanced Interface — Operational Command Reference

This document defines the IGOS operational-mode commands implemented for the
enhanced WWAN interface management service.  All data is sourced from the
WWAN FSM D-Bus service (no raw QMI/mmcli calls from op-mode scripts, avoiding
conflicts with ModemManager).

For the configuration (`set`) commands, see `vyos-wwan-set-commands.md`.

---

## Command Tree

```
show
  └── interfaces
        └── wwan                                           # summary of all WWAN interfaces
              ├── detail                                    # detailed info for all WWAN interfaces
              └── <wwanN>                                   # summary for specific interface
                    ├── detail                              # full detail (all sections combined)
                    ├── hardware                            # modem hardware info
                    ├── signal                              # RF signal metrics
                    ├── sim                                 # SIM/slot/PIN information
                    ├── status                              # connection state and IP config
                    ├── firmware                            # compatible releases, cache state, and running version
                    ├── bearer-leases                       # who is holding the bearer up (failover/operator) and why
                    ├── wait-failover-alert                 # wait for next failover alert
                    │     └── timeout <1-600>               # override wait timeout (seconds)
                    ├── monitor-alerts                      # collect live alerts for a timeout window
                    │     ├── timeout <1-600>               # monitor duration in seconds
                    │     ├── severity {info|warning|critical}
                    │     └── category {connectivity|sim|usage}
                    ├── sms                                 # list all SMS messages
                    │     ├── <id>                          # read specific SMS message (preferred)
                    │     └── message <id>                  # read specific SMS message
                    └── event-log                           # WWAN alert/event history (AlertBus)
                          ├── limit <1-500>                 # max recent events (default 100)
                          ├── severity {info|warning|critical}
                          └── category {connectivity|sim|usage}

connect
  └── interface <wwanN>                                    # bring up WWAN bearer

disconnect
  └── interface <wwanN>                                    # tear down WWAN bearer

update
  └── firmware
        └── wwan <wwanN>
              ├── <igos_version|latest> [now]              # install a cataloged release
              └── archive <URL|local-path>
                    ├── now                               # signed archive, no prompt
                    └── force-unsigned [now]               # enabled development images, local files only

delete
  └── firmware
        └── wwan <wwanN>
              └── <igos_version> [now]                     # delete a retained archive

generate
  └── interfaces
        └── wwan <wwanN>
              └── sms
                    └── number <phone>
                          └── message <text>              # send SMS

clear
  └── interfaces
        └── wwan <wwanN>
              ├── sms                                      # clear all SMS messages
              │     └── message <id>                       # clear specific SMS message
              └── data-usage                               # clear per-SIM data-usage counters
                    └── slot <N>                           # reset counters for SIM slot N
```

---

## Show Commands

### `show interfaces wwan`

Summary of all WWAN interfaces (standard IGOS interface listing).

```
igos@igos:~$ show interfaces wwan
```

**Script:** `interfaces.py show_summary --intf-type=wwan`

Displays the standard interface summary table (admin/link state, IPv4/IPv6
addresses, description) for every `wwan*` interface.

---

### `show interfaces wwan detail`

Detailed standard interface information for all WWAN interfaces.

```
igos@igos:~$ show interfaces wwan detail
```

**Script:** `interfaces.py show --intf-type=wwan`

Standard IGOS detailed interface output (counters, MTU, MAC, etc.) for every
`wwan*` interface.

---

### `show interfaces wwan <wwanN>`

Standard interface summary for a specific WWAN interface.

```
igos@igos:~$ show interfaces wwan wwan0
```

**Script:** `interfaces.py show --intf-name="$4" --intf-type=wirelessmodem`

---

### `show interfaces wwan <wwanN> status`

Current connection state, IP configuration, signal overview, SMS count, and
live bearer session data usage.

```
igos@igos:~$ show interfaces wwan wwan0 status
```

**Script:** `show_wwan.py show_status --interface="$4"`

**Output fields:**

| Section | Field | Description |
|---|---|---|
| **Connection** | State | FSM state (e.g. `CONNECTED`, `REGISTERED`, `FAILED`) |
| | Connection mode | `always-on`, `connect-on-demand`, `dial-on-demand` |
| | Power state | Modem power state (e.g. `on`, `low`, `off`) |
| | Access technology | Active RAT (e.g. `lte`, `5gnr`, `umts`) |
| | Operator | Network operator name |
| | Operator code | MCC/MNC code |
| | APN | Connected APN name |
| | Requested APN | APN requested by configuration (shown with Negotiated APN) |
| | Negotiated APN | APN the carrier actually activated |
| | Log level | Effective WWAN log level |
| | Log sink | Effective WWAN log destination (`both`, `journal`, `syslog`) |
| | Failure reason | Set when state is `FAILED` |
| **IP Configuration** | IPv4 address | Bearer IPv4 address |
| | IPv4 gateway | IPv4 default gateway |
| | IPv4 DNS | IPv4 DNS server(s) |
| | IPv6 address | Bearer IPv6 address |
| | IPv6 gateway | IPv6 default gateway |
| | IPv6 DNS | IPv6 DNS server(s) |
| | MTU | Effective MTU (carrier-negotiated or fallback) |
| **Signal** | Quality | Signal quality as percentage |
| | Strength | Signal strength in dBm |
| | Serving band | Band the modem is currently camped on |
| | Configured bands | Operator-selected band restriction (shown when not `all`) |
| | Enabled bands | Currently active radio bands |
| **SMS** | SMS supported | Whether modem supports SMS |
| | Messages | Total SMS message count |
| | Unread | Unread incoming SMS count |
| **Data Usage (session)** | RX bytes | Bytes received on the current bearer session |
| | TX bytes | Bytes transmitted on the current bearer session |
| | Duration | Current bearer session duration (hours/minutes/seconds) |

**JSON mode:** `show interfaces wwan wwan0 status --raw`

---

### `show interfaces wwan <wwanN> bearer-leases`

Shows who is currently holding the cellular bearer up and why.  Use it to
answer "why is this metered link up right now?".  It lists the bearer-demand
leases held by clients such as the `protocols failover` daemon.  See
*Bearer-Demand Leases* in the `set` command reference.

```
igos@igos:~$ show interfaces wwan wwan0 bearer-leases
Interface:        wwan0
Connection mode:  dial-on-demand
Bearer demanded:  yes
Lease-managed:    yes
Operator request: no
Connect-retry:    failed-retry schedule

Owner           Reason                      TTL(s)    Expires(s)  Age(s)
--------------------------------------------------------------------------
failover        active failover target      120       117         184
```

| Field | Description |
|---|---|
| Bearer demanded | `yes` if an operator `connect_bearer()` request or any live lease wants the bearer up |
| Lease veto | Shown only when active: airplane mode / `disable` or a **data-limit hold** is overriding the leases, so they can't bring the bearer up |
| Lease-managed | `yes` while the bearer is up because of lease demand; the FSM idles it ~5 s after the last lease ends |
| Operator request | Persistent operator intent from `connect_bearer()` (cleared by `disconnect_bearer()`) |
| Connect-retry | The configured `connect-retry` profile, or `failed-retry schedule` when unset |
| Owner / Reason | Lease holder (`failover`, `cli`, `operator`, …) and its annotation |
| TTL / Expires / Age | Renewal budget, seconds until the lease lapses if not renewed (`-` = persistent), and seconds since it was first acquired |

With no live leases the table is replaced by `No active bearer-demand leases.`

> **Note:** In `always-on` mode the bearer is FSM-managed and leases can't be
> acquired, so the command shows the mode with no active leases.

**JSON mode:** `show interfaces wwan wwan0 bearer-leases --raw`.  The keys are
`interface`, `connection_mode`, `demanded`, `vetoed`, `data_limit_hold`,
`lease_owns_bearer`, `operator_requested`, `retry_profile` (`""` when unset)
and `leases`.  Each lease has `owner`, `reason`, `ttl`, `expires_in` and `age`.

---

### `show interfaces wwan <wwanN> hardware`

Modem hardware identification.

```
igos@igos:~$ show interfaces wwan wwan0 hardware
```

**Script:** `show_wwan.py show_hardware --interface="$4"`

**Output fields:**

| Field | Description |
|---|---|
| Manufacturer | Modem manufacturer (e.g. `Quectel`) |
| Model | Modem model (e.g. `RM520N-GL`) |
| IMEI | International Mobile Equipment Identity |
| Firmware revision | Modem firmware version string |
| Hardware revision | Modem hardware revision |
| Phone number | Primary MSISDN |
| All numbers | All MSISDNs (shown when more than one) |
| Device path | ModemManager device path |
| Power state | Current modem power state |

**JSON mode:** `show interfaces wwan wwan0 hardware --raw`

---

### `show interfaces wwan <wwanN> signal`

RF signal metrics and active radio bands.

```
igos@igos:~$ show interfaces wwan wwan0 signal
```

**Script:** `show_wwan.py show_signal --interface="$4"`

**Output fields:**

| Section | Field | Description |
|---|---|---|
| **Summary** | Quality | Signal quality percentage (0–100%) |
| | Strength | Signal strength in dBm |
| | Technology | Active radio technology (e.g. `lte`, `5gnr`) |
| **Detailed Metrics** | RSSI | Received Signal Strength Indicator (dBm) |
| | RSRP | Reference Signal Received Power (dBm) — LTE/5G |
| | RSRQ | Reference Signal Received Quality (dB) — LTE/5G |
| | SNR | Signal-to-Noise Ratio (dB) |
| | RSCP | Received Signal Code Power (dBm) — UMTS/HSPA (3G only) |
| | Ec/Io | Chip energy-to-interference ratio (dB) — UMTS/HSPA (3G only) |
| **Serving Cell** | Band | Band the modem is camped on (e.g. `eutran-7`) |
| | EARFCN / ARFCN | Serving channel number (EARFCN for LTE, ARFCN otherwise) |
| | Cell ID | Serving cell identifier |
| | PCI | Physical Cell Identity |
| | TAC | Tracking/Location Area Code |
| **Active Bands** | *(list)* | Currently active radio bands (e.g. `eutran-7`, `ngran-78`) |

**Signal quality interpretation:**

| RSRP (dBm) | Quality | Description |
|---|---|---|
| ≥ −80 | Excellent | Strong signal |
| −80 to −90 | Good | Reliable connection |
| −90 to −100 | Fair | May experience reduced throughput |
| −100 to −110 | Poor | Connection instability likely |
| < −110 | Very poor | Frequent disconnections expected |

**JSON mode:** `show interfaces wwan wwan0 signal --raw`

---

### `show interfaces wwan <wwanN> sim`

SIM card, slot, failover, and PIN/PUK status.

```
igos@igos:~$ show interfaces wwan wwan0 sim
```

**Script:** `show_wwan.py show_sim --interface="$4"`

**Output fields:**

| Section | Field | Description |
|---|---|---|
| **Active SIM** | Active slot | Currently active SIM slot number |
| | Configured slot | Primary slot from configuration |
| | On configured SIM | `yes` if active slot matches configured primary |
| | On failover SIM | `yes` if active SIM is the failover (backup) |
| | Switch reason | Why the SIM was switched (e.g. signal loss, connect failure, data limit) |
| | IMSI | International Mobile Subscriber Identity |
| | ICCID | Integrated Circuit Card ID |
| | Operator | Network operator name |
| | SPN | Service Provider Name |
| | MCC/MNC | Mobile Country Code / Mobile Network Code |
| **Failover** | Failover enabled | Whether automatic SIM failover is on |
| | Failback enabled | Whether automatic failback to primary is on |
| **PIN/PUK Status** | Status | `ok`, `FAILED`, or `PERMANENTLY LOCKED` |
| | PIN unlock | `ok` or `FAILED` |
| | PUK unlock | `ok` or `FAILED` |
| | PIN retries | Remaining PIN entry attempts |
| | PUK retries | Remaining PUK entry attempts |
| **Slot N** *(per slot)* | Present | Whether a SIM is physically inserted |
| | Enabled | Whether the slot is administratively enabled |
| | IMSI | SIM's IMSI |
| | ICCID | SIM's ICCID |
| | Operator | SIM's home operator |

**JSON mode:** `show interfaces wwan wwan0 sim --raw`

---

### `show interfaces wwan <wwanN> detail`

Comprehensive display combining all sections: status, hardware, SIM, signal,
plus billing-cycle cumulative usage, runtime bearer/recovery counters,
failover history, and configuration summary.

```
igos@igos:~$ show interfaces wwan wwan0 detail
```

**Script:** `show_wwan.py show_detail --interface="$4"`

**Additional fields** (beyond the sections above):

| Section | Field | Description |
|---|---|---|
| **Cumulative Data Usage** | Cumulative bytes | Total bytes across all sessions in current billing cycle |
| | Including session | Cumulative + current session bytes |
| | Billing day | Day of month on which the billing-cycle counters reset |
| | Tracked slot | Active slot associated with the persisted usage record |
| | Tracked ICCID | Physical SIM ICCID used for billing usage tracking when available; falls back to slot-based tracking if unavailable |
| | Data limit | Configured data limit in bytes (if set) |
| | Usage | Percentage of data limit consumed |
| | Limit action | Action when limit reached (`none`, `disable`, `sim-failover`, `sim-failover-sticky`) |
| **Failover History** | Failover count | Number of SIM failover events since power on |
| | Last failover | Timestamp of last failover |
| | Recovery attempts | Number of connectivity recovery attempts |
| **Runtime Counters** | Bearer drops | Number of bearer-down events since boot |
| | Registration losses | Number of bearer losses that surfaced as registration-only state since boot |
| | Reconnect attempts | Automatic reconnection attempts since boot |
| | Reconnect successes | Successful bearer recoveries since boot |
| | SIM switches | Runtime SIM slot changes since boot |
| | Total downtime | Accumulated bearer downtime since boot |
| | Current downtime | Current bearer downtime if the interface is presently disconnected |
| | Last disconnect | Timestamp of the last bearer-down event |
| | Last reason | Reason recorded for the last bearer-down event |
| **Diagnostics (since power on)** | Service starts | WWAN manager starts since power on (a value >1 indicates a manager crash/restart) |
| | ModemManager restarts | ModemManager crashes the manager recovered from since power on |
| | Modem hardware resets | Successful modem hardware/soft resets on this interface since power on |
| | Modem nuclear resets | ModemManager-restart recoveries deliberately triggered by the FSM since power on |
| **Configuration** | Network mode | Configured RAT selection mode |
| | Reconnection | Enhanced reconnection status |
| | Monitoring | Connectivity monitoring status |
| | Interface mgmt | Interface management status |
| | Log level | Effective WWAN log level |
| | Log sink | Effective WWAN log destination (`both`, `journal`, `syslog`) |
| | Verbose logging | Verbose logging status |

**JSON mode:** `show interfaces wwan wwan0 detail --raw` (returns the full FSM status dict)

---

### `show interfaces wwan <wwanN> wait-failover-alert`

Wait for the next failover-related alert for the specified interface.

```
igos@igos:~$ show interfaces wwan wwan0 wait-failover-alert
```

Optional timeout override:

```
igos@igos:~$ show interfaces wwan wwan0 wait-failover-alert timeout 300
```

**Script:** `show_wwan.py show_wait_failover --interface="$4"`

**Behavior:**
- Uses `WWANClientSync.wait_for_failover_alert()` (AlertBus-backed helper)
- Waits for a new failover/switch alert (default timeout: 120 seconds)
- Timeout can be tuned via `timeout <1-600>`
- Prints a structured alert block when received
- Prints timeout notice if no failover alert arrives in time

**JSON mode:** `show interfaces wwan wwan0 wait-failover-alert --raw`

---

### `show interfaces wwan <wwanN> monitor-alerts`

Collect matching live alerts from AlertBus over a fixed monitoring window.

```
igos@igos:~$ show interfaces wwan wwan0 monitor-alerts
```

Optional filters:

```
igos@igos:~$ show interfaces wwan wwan0 monitor-alerts timeout 120
igos@igos:~$ show interfaces wwan wwan0 monitor-alerts severity critical
igos@igos:~$ show interfaces wwan wwan0 monitor-alerts category connectivity
```

**Script:** `show_wwan.py show_monitor_alerts --interface="$4"`

**Behavior:**
- Uses `WWANClientSync.monitor_alerts()` (AlertBus signal subscription)
- Collects matching alerts during timeout window (default: 30s)
- Optional server-side filter on `severity` and `category`
- Returns count + per-alert summary (sequence, timestamp, code/type/category/message)

**JSON mode:** `show interfaces wwan wwan0 monitor-alerts --raw`

---

### `show interfaces wwan <wwanN> sms`

List all SMS messages for the active SIM.

```
igos@igos:~$ show interfaces wwan wwan0 sms
```

**Script:** `wwan_sms.py show_sms --interface="$4"`

**Output:** Tabular list with columns:

| Column | Description |
|---|---|
| ID | Message ID (numeric) |
| Direction | `<-` incoming, `->` outgoing |
| Number | Phone number |
| Timestamp | Date/time (first 19 characters) |
| Message | Text (truncated to 50 chars in list view) |
| Flag | `[NEW]` appended for unread incoming messages |

**JSON mode:** `show interfaces wwan wwan0 sms --raw` (returns array of message dicts)

---

### `show interfaces wwan <wwanN> sms <id>`

Read a specific SMS message by ID. Incoming messages are marked as read.

```
igos@igos:~$ show interfaces wwan wwan0 sms 3
```

**Script:** `wwan_sms.py show_sms_message --interface="$4" --message-id="$6"`

**JSON mode:** `show interfaces wwan wwan0 sms 3 --raw`

---

### `show interfaces wwan <wwanN> sms message <id>`

Alternate syntax for reading a specific SMS message by ID.

```
igos@igos:~$ show interfaces wwan wwan0 sms message 3
```

**Script:** `wwan_sms.py show_sms_message --interface="$4" --message-id="$7"`

**Output fields:**

| Field | Description |
|---|---|
| ID | Message ID |
| Direction | `incoming` or `outgoing` |
| Number | Phone number |
| Timestamp | Full date/time |
| Status | Message delivery status |
| Read | `yes` / `no` (incoming only) |
| *(text)* | Full message body |

**JSON mode:** `show interfaces wwan wwan0 sms message 3 --raw`

---

### `show interfaces wwan <wwanN> event-log`

Show recent WWAN alert/event history for the specified interface from the FSM
AlertBus (connectivity, SIM, and data-usage events — this is the modem service's
own event stream, not the kernel/journalctl network-event log).  Newest entries
first; defaults to the last 100.

```
igos@igos:~$ show interfaces wwan wwan0 event-log
igos@igos:~$ show interfaces wwan wwan0 event-log limit 50
igos@igos:~$ show interfaces wwan wwan0 event-log severity critical
igos@igos:~$ show interfaces wwan wwan0 event-log category connectivity
```

**Sub-commands:**

| Command | Description |
|---|---|
| `event-log` | All recent events (newest first, default limit 100) |
| `event-log limit <1-500>` | Retrieve up to N recent events |
| `event-log severity {info\|warning\|critical}` | Filter by severity |
| `event-log category {connectivity\|sim\|usage}` | Filter by category |

**Script:** `show_wwan.py show_event_log --interface="$4"` (AlertBus recent-alert history via `WWANClientSync.get_recent_alerts()`)

**JSON mode:** `show interfaces wwan wwan0 event-log --raw`

---

## Connect / Disconnect Commands

### `connect interface <wwanN>`

Bring up (or re-establish) the WWAN bearer connection.

```
igos@igos:~$ connect interface wwan0
```

**Script:** `connect_disconnect.py --connect --interface "$3"`

**Behaviour:**
- If already connected: prints `Interface wwan0: already connected!`
- If not connected: re-applies the interface configuration via `interfaces_wwan.py`,
  which causes the FSM to initiate the connection sequence
- After connection, QoS policy is re-applied if configured

> **Note:** In `connect-on-demand` and `dial-on-demand` modes, the
> `connect_bearer()` D-Bus method can also be used programmatically.

---

### `disconnect interface <wwanN>`

Tear down the WWAN bearer connection.

```
igos@igos:~$ disconnect interface wwan0
```

**Script:** `connect_disconnect.py --disconnect --interface "$3"`

**Behaviour:**
- If already disconnected: prints `Interface wwan0: connection is already down`
- If connected: issues `mmcli --modem N --simple-disconnect` to tear down the bearer

> **Note:** In `connect-on-demand` and `dial-on-demand` modes, the
> `disconnect_bearer()` D-Bus method can also be used programmatically.
> In those modes, the bearer is released but the modem stays registered
> on the network (SMS available, no data).  If another client holds a live
> bearer-demand lease (for example, `protocols failover` is holding the link up),
> `disconnect_bearer()` only withdraws the operator request and the bearer
> stays up.  Check `show interfaces wwan <wwanN> bearer-leases` to see who is
> holding it.

---

## Airplane Mode Commands

### `change wwan <wwanN> airplane-mode enable`

Put the modem into airplane mode: disconnect the bearer, tear down downstream
LAN features, and power the modem RF **off** (`SetPowerState LOW`). The
interface then parks and ignores modem/SIM events until released.

```
igos@igos:~$ change wwan wwan0 airplane-mode enable
```

**Script:** `wwan_airplane.py set_airplane_mode --interface "$3" --state=enable`

> **Non-persistent by design.** Airplane mode is an operational action and is
> **never written to the configuration** — a reboot always returns to normal
> operation. This is deliberate: on a unit whose only management path is the
> cellular link, a persistent radio-off would be unrecoverable remotely, so a
> power cycle is always a way back.

### `change wwan <wwanN> airplane-mode disable`

Power the modem RF back **on** and restart the connection from scratch.

```
igos@igos:~$ change wwan wwan0 airplane-mode disable
```

**Script:** `wwan_airplane.py set_airplane_mode --interface "$3" --state=disable`

> **Airplane mode vs `set … disable`:** airplane mode is a *runtime* radio
> silence that keeps the interface configured; `set interfaces wwan <wwanN>
> disable` is a *configuration* teardown (delete-style, purges history) that
> recreates the interface when removed. Use airplane mode for transient RF-off
> (transport, maintenance); use `disable` to keep the config but not run the
> interface.

---

## SIM PIN Commands

Manage the PIN lock on the **active, registered** SIM. These act on the
physical SIM, so they run in op-mode; the resulting state is written back to the
running configuration for the active slot so the manager can auto-unlock the SIM
on future boots.

> **Active, registered SIM only.** A PIN-locked SIM only reaches the registered
> state after the manager unlocked it with the configured PIN, so that PIN is
> proven correct — change/remove therefore cannot burn the SIM's PIN-retry
> counter (which would drive it to PUK-lock). Insert/enable only the SIM you
> want to modify in the active slot before running these.

### `change wwan <wwanN> sim pin new <PIN>`

Enable a PIN lock or change the existing PIN (4–8 digits) on the active SIM, and
persist it to `interfaces wwan <wwanN> sim slot <slot> pin`.

```
igos@igos:~$ change wwan wwan0 sim pin new '1234'
```

**Script:** `wwan_pin.py update_pin --interface="$3" --pin="$7"`

> **Enabling a PIN is retry-guarded.** Creating a lock verifies the PIN against
> the SIM, so the command refuses to proceed if it cannot read the retry
> counter or only one attempt remains (recover via PUK first).

### `change wwan <wwanN> sim pin remove`

Disable the PIN lock on the active SIM and clear the stored PIN from config.

```
igos@igos:~$ change wwan wwan0 sim pin remove
```

**Script:** `wwan_pin.py delete_pin --interface="$3"`

## Firmware Commands

Firmware updates use signed, per-version archives and a signed schema-1
`modem-firmware.json` catalog. Firmware payloads are downloaded on demand;
no firmware Debian packages are installed by these commands. The router must
already have the appropriate `uxfp` or `tfl` installer.

`src/op_mode/wwan_firmware.py` obtains manufacturer, model, hardware revision,
and installed firmware revision from `WWANClientSync().get_status(N)` for the
selected `wwanN`. It does not probe AT ports for modem identity. USB/sysfs
information is used separately to target the physical modem for flashing.

### Version selection and compatibility

| Field | Meaning |
|---|---|
| `igos_version` | Operator-facing release label used by CLI selection and ordering |
| `telit_version` | Native modem firmware revision used to identify the running release |
| `telit_package_version` | Vendor package metadata; not the CLI version selector |

igOS labels are compared as numeric tuples within the selected model/hardware
track: `1.10` is newer than `1.9`. `latest` selects the maximum label in that
track. It is computed from the catalog rather than stored as a release.

Manufacturer/model and hardware revision must match. `Telit` and
`Telit Cinterion` are normalized to the same vendor. Revision patterns use
most-specific precedence: exact (`1.20`), longest matching whole-segment prefix
(`1.*`), then catch-all (`*`). The winning block supplies the complete track;
releases from broader patterns are not merged. Ambiguous matches are rejected.

The running release is found by matching the modem's revision to
`telit_version`. An exact full-string match takes precedence. For numeric
revisions, a ModemManager build suffix is also supported: for example,
`25.21.564  1  [Dec 27 2023 05:00:00]` can match catalog `25.21.564`.
An unmapped revision is reported as unknown; the updater does not guess its
ordering relative to cataloged releases.

### `show interfaces wwan <wwanN> firmware`

List compatible releases and their cache/running status. Example output with
illustrative catalog entries:

```text
igos@igos:~$ show interfaces wwan wwan0 firmware
Version   Telit Firmware      Released      Cached    Running
1.1       M0L.001005          2026-11-01    No        No
1.0       M0L.001004          2026-09-01    Yes       Yes
```

**Script:** `wwan_firmware.py show_firmware --interface="$4"`

Handler options used by completion and automation:

- `--quiet`: print only igOS labels; return no completion output if lookup fails.
- `--downloaded-only`: restrict results to retained archives in the archive cache.
- `--raw`: return structured interface, modem, release, cache, and running data.
  This is a Python handler option, not an extra node in the operational XML.

Update completion lists compatible labels plus `latest`. Delete completion
lists only cached labels. Completion uses the authenticated cached catalog;
a normal `show` refreshes it when the server is reachable.

### `update firmware wwan <wwanN> <igos_version|latest> [now]`

Install one cataloged release on a specific modem:

```text
update firmware wwan wwan0 1.1
update firmware wwan wwan0 latest
update firmware wwan wwan0 1.1 now
```

**Script:** `wwan_firmware.py set_firmware --interface="$4" --version="$5"`
The `now` node adds `--no-prompt`. It skips interactive confirmation; signature,
compatibility, storage, and port-targeting checks still apply.

The updater:

1. Reads the modem identity, authenticates the catalog, and selects the matching
   hardware track. It uses the release's full archive URL directly.
2. Returns without flashing if that igOS release is already installed. A lower
   target label prints a downgrade warning; an unmapped running revision prints
   an unknown-ordering warning.
3. Downloads only the selected archive, or reuses an eligible cached copy.
   Checks free space and the catalog's `archive_sha256` before extraction.
   Extraction rejects absolute/traversal paths, symbolic/hard links, and special
   device entries. Storage checks preserve a 32 MiB free-space reserve.
4. Extracts safely and reads `firmware.json` inside the archive. Manufacturer,
   model, hardware revision, installer, igOS/Telit versions, and optional package
   and payload metadata are checked against the selected release/modem.
5. Captures the selected interface's physical USB path and serial. Checks the
   installer is available, displays firmware/modem details, and asks for
   confirmation unless `now` was supplied.
6. Stops `igos-wwan-manager.service` and confirms ModemManager is inactive.
   The manager's shutdown tears down bearers and stops ModemManager, freeing
   diagnostic ports. **This disconnects all managed cellular interfaces.**
7. Resolves the captured physical modem again and selects only its serial ports.
   UXFP's reported diagnostic ports are intersected with that modem's port group.
   TFL loads `qcserial` if needed, selects the highest-numbered `ttyUSB` in that
   group, and validates with `--port ... --dry-run --debug`. Port resolution is
   repeated before flashing to handle USB re-enumeration.
8. Runs `uxfp --file <payload> --port <diag>` or
   `tfl <payload-directory> --port <diag> --force`.
9. Removes temporary files. After successful flashing, leaves the WWAN manager
   stopped and asks the operator to reboot the router.

FN920 TFL vendor ZIPs inside release archives are safely extracted to a temporary
directory containing a single `contents.xml` before service teardown. UXFP
receives the selected stream binary. Flashing never relies on another modem's
ports or the installer's automatic target selection.

Cancellation at the confirmation prompt leaves services running. If teardown or
pre-flash preparation fails, the updater attempts to restore a manager it
stopped. Once a flasher process has started, the manager is not automatically
restarted on failure or interruption because the modem may already have changed.
Review the error and modem state before recovery; reboot after successful flashing.

### `update firmware wwan <wwanN> archive <URL|local-path> [now]`

Install a self-describing archive independently of the catalog:

```text
update firmware wwan wwan0 archive https://firmware.example.com/releases/test.tar
update firmware wwan wwan0 archive /tmp/test.tar now
```

**Script:** `wwan_firmware.py update_archive --interface="$4" --archive="$6"`

Provide the archive and its detached `.sig` and `.crl` siblings at the same URL
or local path, for example `/tmp/test.tar.sig` and `/tmp/test.tar.crl`.
The signing certificate is embedded in the CMS signature; the router does not
need the publisher's private key or a separately downloaded `.pem` sidecar.

The archive's own signature is authenticated before extraction. Its internal
`firmware.json` supplies identity and payload metadata, and model/hardware gates
still apply. HTTP(S) downloads and local regular files are supported. This path
has no catalog release ordering, so it prints an ordering-unknown notice and
uses the same confirmation, teardown, explicit-port flashing, and cleanup flow.
Standalone installs use temporary storage rather than archive retention.

### Development-only `force-unsigned`

```text
update firmware wwan wwan0 archive /tmp/lab.tar force-unsigned
update firmware wwan wwan0 archive /tmp/lab.tar force-unsigned now
```

This bypasses only the standalone archive signature check and prints an unsigned
firmware warning. All of the following are required:

- The archive is a local file; URL-based unsigned installs are rejected.
- `/usr/share/vyos/version.json` records `build_type = "development"`.
- The image was built with `wwan_firmware_allow_unsigned = true`.
- The operator explicitly supplies `force-unsigned`.

The flag defaults to `false`; release image builds reject enabling it. It is a
build-time setting, not a router configuration command. Compatibility and safe
extraction checks remain mandatory. Catalog signature verification is never
bypassed. `now` skips confirmation, not these checks.

### `delete firmware wwan <wwanN> <igos_version> [now]`

Remove a retained archive and its cache bookkeeping:

```text
delete firmware wwan wwan0 1.0
delete firmware wwan wwan0 1.0 now
```

**Script:** `wwan_firmware.py delete_firmware --interface="$4" --version="$5"`

The label must exist in the compatible catalog track and its archive must be
cached. The handler uses the authenticated cached catalog, asks for confirmation
unless `now` is supplied, and serializes cache changes with the update lock.
Deleting the running release's cached archive is allowed: it does not uninstall
or change the firmware already flashed into the modem. No Debian package is
removed. Deletion remains useful for retained files after cache retention is disabled.

### Catalog source and signature trust

The default catalog URL comes from `wwan_firmware_map_url` in
`/usr/share/vyos/version.json`. Override it in configuration mode:

```text
configure
set system update-check cellular-firmware url 'https://firmware.example.com/cellular-firmware/modem-firmware.json'
commit
save
exit
```

Deleting that `url` node restores the image default. Changing the server does not
change the trusted signing root. The override accepts HTTP or HTTPS; catalog
and firmware authentication are enforced independently of transport.

The router downloads the catalog, `.sig`, and `.crl` siblings. It validates the
detached binary CMS signature, root-issued leaf certificate, certificate purpose
and validity, and root-signed CRL before accepting catalog data. Learned CRL
numbers are persisted to reject revocation-list rollback. A cataloged archive is
then authenticated by its hash in that signed catalog; standalone installation
checks the archive's own signature and current trust/expiry/revocation status.

On transport failure, the updater may use the same-source cached catalog only
after re-verifying its signature and CRL. An expired or invalid cached trust
state is rejected. A downloaded catalog with an invalid signature does not fall
back to an older valid cache. No catalog is bundled as a fallback in the image.

| Router path | Purpose |
|---|---|
| `/usr/share/vyos/keys/igos-firmware-root-*.pem` | Public root CA certificates installed by the ISO builder |
| `/var/cache/vyos/modem-firmware.json` | Authenticated catalog cache envelope, including signature and CRL |
| `/config/firmware-signing/revocations.json` | Persistent authenticated CRL rollback state |
| `/var/cache/vyos/wwan-firmware/` | Optional retained archives and `.sha256` bookkeeping |
| `/usr/lib/modem/<vendor>/<model>/<hw_rev>/<telit_version>/` | Temporary cataloged-release work directory; version is path-escaped when needed |
| `/run/lock/igos-wwan-firmware.lock` | Serializes firmware installs and cache deletion |

The root private key stays offline. Leaf private keys and passphrases are needed
on the signing host, never on the router. Public certificate/CRL sidecars must
be renewed and distributed before expiry; successful signature creation alone
does not mean a release will pass router trust checks.

### Optional archive retention

Retention is off by default. Enable it in configuration mode:

```text
configure
set system update-check cellular-firmware cache enable
set system update-check cellular-firmware cache max-archives '2'
set system update-check cellular-firmware cache max-size '512'
commit
save
exit
```

`max-archives` accepts 1–100 (default 2); `max-size` accepts 1–409600 MiB
(default 512). Limits apply to the shared archive cache. Least-recently-used
archives are pruned to meet the count and size budgets. An archive larger than
the size budget is not retained. Retained files use readable archive basenames
and are hash-checked before reuse; corrupt entries are discarded and downloaded
again. Cache-storage failure does not invalidate an otherwise authenticated install.

With retention disabled, cataloged installs download, authenticate, extract, and
clean up their selected archive. Temporary extraction directories are removed
on normal completion, cancellation, and handled failures regardless of retention.
Deletion of the `cache enable` node stops future reuse/retention but does not
immediately erase previously retained archives; use `delete firmware wwan` to
remove cataloged cached releases.

### Automatic checks and notifications

Enable notification-only checks, optionally broadcasting changed notices:

```text
configure
set system update-check cellular-firmware auto-check
set system update-check cellular-firmware wall
commit
save
exit
```

`vyos-cellular-firmware-check.service` runs an immediate check when started and
then every 12 hours, with a 180-second timeout per check. It uses the same FSM
identity, authenticated catalog, revision matching, and running-version mapping
as the operational handler. It checks present configured WWAN interfaces and
aggregates newer-release notices into `/run/motd.d/11-vyos-cellular-firmware`.
Each notice names the interface, available igOS label, and exact update command.
Unknown running versions are not guessed to be upgrades. Unavailable modems are
skipped; checks never stop the manager, download firmware archives, or auto-flash.

`wall` broadcasts only when notice content changes. Removing `wall` leaves MOTD
notices enabled. To disable cellular checks explicitly:

```text
configure
delete system update-check cellular-firmware auto-check
set system update-check cellular-firmware disable
commit
save
exit
```

`auto-check` and `disable` cannot coexist. Removing `auto-check`, or setting
`disable`, stops/disables the cellular service and clears its MOTD fragment.
Delete `disable` before enabling `auto-check` again. These cellular toggles are
independent of the parent image-update notification toggle.

For diagnostics from the router shell:

```bash
systemctl status vyos-cellular-firmware-check.service
journalctl -u vyos-cellular-firmware-check.service
```

### Firmware build and signing workflow

The separate private `igos-modem-firmwares` repository supplies the release
builder. Source layout is `telit/<model>/<hardware>/<igos_version>/meta.toml`
plus a `payload/` directory. The generator extracts LN920/LE910 stream binaries
from source ZIPs while assembling `.tar` archives; FN920 vendor ZIPs stay intact
inside the release archive for the TFL preparation step on the router.

On a provisioned signing host, run `./docker-build.sh` in that repository. It
fetches private `igos-firmware-keys`, runs tests/build/signing in Docker, and
writes the catalog and archives under `dist/`. Public certificates, CRL, and an
encrypted leaf key come from the keys repository; the passphrase is supplied
separately in the ignored `.signing-secrets/leaf.passphrase` or through
`FIRMWARE_SIGNING_PASSWORD_FILE`. Root private keys are not part of daily builds.

Generated archive directories/names use `telit_version`, for example:

```text
telit/LN920A12-WW/1.20/M0L.001004/LN920A12-WW-1.20-M0L.001004.tar
```

`telit_package_version` remains metadata. `FIRMWARE_BASE_URL` supplies the archive
hosting prefix; the catalog URL baked into the ISO must point to that deployment's
`modem-firmware.json`. The current signer creates fresh archive and catalog
signatures each run, and writes `.sig`, `.pem`, and `.crl` sidecars. The signer
creates signatures; router trust verification occurs separately.

Nexus provides an independent `bin/iGOS-firmware-build` job that calls this Docker
workflow and records logs. ISO assembly separately fetches the keys repository
and stages only validated public roots through `vyos-build`'s
`--firmware-signing-key-dir`. Offline root creation, leaf issuance, and CRL
maintenance live in the separate `igos-firmware-ca/wwan-firmware-ca` tool.

The build does not publish releases or flash routers. Configure scheduling and
hosting separately. Publish completed archives and all sidecars first, then the
signed catalog and its sidecars. No GraphQL firmware integration is included in
this implementation.

---

## SMS Commands

### `generate interfaces wwan <wwanN> sms number <phone> message <text>`

Send an SMS message via the WWAN modem.

```
igos@igos:~$ generate interfaces wwan wwan0 sms number '+15551234567' message 'Router rebooted successfully'
```

**Script:** `wwan_sms.py generate_sms --interface="$4" --number="$7" --message="$9"`

> **Phone number format:** Use E.164 format (`+<countrycode><subscriber>`,
> for example `+14168310352`) for best interoperability. Non-E.164 formats
> may work on some modem/carrier combinations but are not guaranteed.

**Output:**
- Human: `SMS sent to +15551234567 (message id: 42)`
- JSON (`--raw`): `{"message_id": 42}`

**Tab completion:** WWAN interface names are auto-completed from `/sys/class/net/wwan*`.

> **Note:** SMS is available whenever the modem is registered on the network,
> even in `connect-on-demand` mode when the bearer is not established.

---

### `clear interfaces wwan <wwanN> sms`

Delete all SMS messages on the active SIM.

```
igos@igos:~$ clear interfaces wwan wwan0 sms
```

**Script:** `wwan_sms.py delete_all_sms --interface="$4"`

**Output:**
- Human: `All SMS messages deleted`
- JSON (`--raw`): `{"status": "ok"}`

---

### `clear interfaces wwan <wwanN> sms message <id>`

Delete a specific SMS message by ID.

```
igos@igos:~$ clear interfaces wwan wwan0 sms message 3
```

**Script:** `wwan_sms.py delete_sms --interface="$4" --message-id="$7"`

**Output:**
- Human: `SMS message 3 deleted`
- JSON (`--raw`): `{"status": "ok"}`

**Tab completion:** Message IDs accept values 1–999.

---

### `clear interfaces wwan <wwanN> data-usage slot <N>`

Reset the persisted per-SIM data-usage counters for one SIM slot to zero — for
a new monthly billing cycle or for diagnostics.  The SIM slot must always be
given explicitly so the reset is unambiguous.

The counters cleared are the cumulative byte totals tracked per SIM slot (the
values shown under **Cumulative Data Usage** in `show interfaces wwan <wwanN>
detail`).  Runtime diagnostic counters (bearer drops, reconnects, SIM switches,
downtime) are **not** affected.  The previous values are written to the service
log and emitted as a `data_usage_cleared` alert before they are cleared, so the
reset stays auditable.

```
igos@igos:~$ clear interfaces wwan wwan0 data-usage slot 1
Cleared data-usage counters for wwan0 SIM slot 1.
  Previous total:          1,234,567,890 bytes (1177.4 MB)
  Previous cumulative:     1,200,000,000 bytes
  Previous session:           34,567,890 bytes
```

(The **Previous session** line is shown only when the cleared slot is the
currently active SIM.)

**Script:** `show_wwan.py clear_data_usage --interface="$4" --slot="$7"`

**Output:**
- Human: multi-line summary of the previous counters (as above)
- JSON (`--raw`):
  ```json
  {
    "status": "cleared",
    "slot": 1,
    "was_active": true,
    "previous_cumulative_bytes": 1200000000,
    "previous_session_bytes": 34567890,
    "previous_total_bytes": 1234567890
  }
  ```

**Tab completion:** SIM slot accepts values 1–4.

**Concurrency:** The reset runs atomically on the FSM event loop, so it is safe
to issue at any time — including during a SIM switchover or while data-usage
monitoring is active.  A running monitor converges the on-disk total to zero on
its next poll rather than re-inflating it.

---

## D-Bus Service Interface

All operational commands flow through the WWAN FSM D-Bus service.  Op-mode
scripts use the `WWANClientSync` client library from
`python/vyos/utils/wwan/wwan_client.py`.

### D-Bus Object Paths

| Path | Interface | Purpose |
|---|---|---|
| `/com/igos/IgosModemManager/Control` | `com.igos.IgosModemManager.Control` | Service management (add/remove interfaces) |
| `/com/igos/IgosModemManager/Interface<N>` | `com.igos.IgosModemManager.Interface` | Per-interface operations |
| `/com/igos/IgosModemManager/AlertBus` | `com.igos.IgosModemManager.AlertBus` | Structured WWAN alert stream + recent-alert history |

### Per-Interface D-Bus Methods

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `get_status()` | — | Full status dict | All status fields (used by all `show` commands) |
| `get_bearer_status()` | — | `"connected"` / `"disconnected"` | Quick bearer state check |
| `connect()` | — | `"accepted"` | Initiate bearer connection |
| `disconnect()` | — | `"accepted"` | Terminate bearer connection |
| `connect_bearer()` | — | `"accepted"` | Establish data bearer (on-demand modes) |
| `disconnect_bearer()` | — | `"accepted"` | Release data bearer (on-demand modes); teardown skipped while another client holds a live lease |
| `bearer_lease_acquire(owner, ttl, reason)` | string, double, string | `"accepted"` | Acquire/renew a bearer-demand lease (on-demand modes).  `owner` must be `cli`, `operator`, `failover` or `dod-traffic`; `ttl <= 0` = persistent (operator owners only); finite TTL capped at 3600 s |
| `bearer_lease_release(owner)` | string | `"accepted"` | Release a lease (no-op if none); bearer stays up if other leases remain |
| `bearer_lease_list()` | — | JSON string | Lease/demand view used by `show … bearer-leases` |
| `send_sms(recipient, message)` | number, text | `{"message_id": N}` | Send SMS |
| `list_sms()` | — | Array of message dicts | List all SMS messages |
| `read_sms(message_id)` | integer | Message dict | Read specific SMS (marks as read) |
| `delete_sms(message_id)` | integer | `{"status": "ok"}` | Delete specific SMS |
| `delete_all_sms()` | — | `{"status": "ok"}` | Delete all SMS for active SIM |
| `clear_data_usage(slot)` | integer | Status dict | Zero per-SIM data-usage counters for a slot |
| `change_sim_pin(new_pin)` | string | `{"action": ..., "slot": N}` | Create or change the SIM PIN on the active, registered SIM |
| `remove_sim_pin()` | — | `{"action": ..., "slot": N}` | Disable the SIM PIN lock on the active, registered SIM |
| `SetAirplaneMode(enabled)` | boolean | string | Toggle airplane mode (RF off / RF on) on the interface |

### Control D-Bus Methods

| Method | Parameters | Returns | Description |
|---|---|---|---|
| `AddInterface(interface_number)` | integer | — | Create and export new interface handler |
| `RemoveInterface(interface_number)` | — | — | Shutdown and unexport interface |

### AlertBus D-Bus Methods and Signal

| Name | Parameters | Returns | Description |
|---|---|---|---|
| `Alert` (signal) | `payload` (`a{sv}`) | — | Emitted for normalized WWAN alerts (bearer down/up, failover, data-limit, FSM failure) |
| `AlertRaised` (signal) | `alert_json` (`s`) | — | Compatibility signal carrying the same alert envelope as compact JSON |
| `GetRecentAlerts(limit, interface_number)` | integer, integer (`-1` = all) | Array of alert dicts | Retrieve recent alerts from in-memory ring buffer |
| `AckAlert(alert_id)` | string | boolean | Mark an alert as acknowledged (`state=acked`) |
| `ClearAlerts(interface_number)` | integer (`-1` = all) | Status string | Clear alert history globally or per-interface |

### WWAN client subscription helpers

The WWAN Python client library includes callback and monitor helpers:

- `WWANClient.subscribe_alerts(callback, ...) -> subscription_id`
- `WWANClient.unsubscribe_alerts(subscription_id) -> bool`
- `WWANClient.monitor_alerts(timeout=..., ...) -> list[alert]`
- `WWANClientSync.monitor_alerts(...) -> list[alert]`

These helpers are the base for protocol adapters (SNMP/REST/MQTT) without
coupling protocol-specific code into the WWAN FSM.

### Protocol adapter skeletons (SNMP/REST/MQTT)

The module `python/vyos/utils/wwan/alert_adapters.py` provides decoupled
subscriber skeletons:

- `AlertSubscriptionRunner` — shared subscriber dispatcher
- `RestAlertAdapter` — in-memory recent/active alert store for REST endpoints
- `MqttAlertAdapter` — topic mapping to `igos/alerts/<category>/<source>`
- `SnmpAlertAdapter` — stable `code -> OID` trap mapping via callback

Adapters are transport-agnostic by design:
- MQTT uses injected `publish_func(topic, payload, qos, retain)`
- SNMP uses injected `send_trap_func(oid, varbinds)`

This keeps WWAN core stable while protocol-specific implementations evolve in
separate daemons/services.

### Alert envelope schema (stable contract)

All AlertBus payloads use a normalized envelope with stable machine fields:

| Field | Type | Description |
|---|---|---|
| `id` | string (UUID) | Unique alert id |
| `timestamp` | string (UTC ISO8601) | Alert creation time |
| `sequence` | integer | Monotonic sequence number |
| `severity` | string | `info` \| `warning` \| `critical` |
| `category` | string | `connectivity` \| `sim` \| `usage` |
| `source` | string | Source component (e.g. `wwan-fsm`) |
| `code` | string | Stable machine code (e.g. `WWAN_BEARER_DOWN`) |
| `message` | string | Human-readable message |
| `labels` | dict | Structured tags (interface, fsm_state, etc.) |
| `state` | string | `open` \| `cleared` \| `acked` |
| `dedupe_key` | string | Stable dedupe/suppression key |
| `interface_number` | integer | Interface index (`-1` = global/service) |

### AlertBus → Journald bridge (for standard IGOS `service event-handler`)

AlertBus events are also written to journald with:

- `SYSLOG_IDENTIFIER=igos-wwan-alertbus`
- `MESSAGE=<alert message>`
- `WWAN_ALERT=1`
- `WWAN_ALERT_TYPE`, `WWAN_ALERT_SEVERITY`, `WWAN_ALERT_INTERFACE`
- `WWAN_ALERT_SEQUENCE`, `WWAN_ALERT_TIMESTAMP`, `WWAN_ALERT_SOURCE`, `WWAN_ALERT_FSM_STATE`
- `SYSLOG_IDENTIFIER=igos-wwan-alertbus-json` (parallel JSON line)
- `MESSAGE=<compact JSON alert envelope>`
- `WWAN_ALERT_JSON=1`

This allows native `service event-handler` rules to trigger scripts from WWAN
alerts without replacing the structured D-Bus AlertBus API.

#### Event-handler + HTTPS webhook note

The IGOS `service event-handler` passes only the journal `MESSAGE` text into the
script environment variable `message`. If your script needs structured JSON for
an HTTPS POST body, use `syslog-identifier igos-wwan-alertbus-json` and parse
`$message` as JSON.

---

## Implementation Files

| File | Purpose |
|---|---|
| `op-mode-definitions/show-interfaces-wwan.xml.in` | XML: `show interfaces wwan` command tree |
| `op-mode-definitions/generate-sms.xml.in` | XML: `generate interfaces wwan ... sms` command |
| `op-mode-definitions/clear-sms.xml.in` | XML: `clear interfaces wwan … sms` and `data-usage` commands |
| `op-mode-definitions/connect.xml.in` | XML: `connect interface` (shared with PPPoE/SSTPC) |
| `op-mode-definitions/disconnect.xml.in` | XML: `disconnect interface` (shared with PPPoE/SSTPC) |
| `src/op_mode/show_wwan.py` | Python: status, hardware, sim, signal, detail, bearer-leases, clear data-usage handlers |
| `op-mode-definitions/wwan-firmware.xml.in` | XML: firmware update, standalone archive, and cache-delete commands |
| `src/op_mode/wwan_sms.py` | Python: send, list, read, delete SMS handlers |
| `src/op_mode/connect_disconnect.py` | Python: connect/disconnect handler (shared) |
| `src/op_mode/wwan_firmware.py` | Python: firmware listing, install/delete, USB targeting, service lifecycle, and notify-only checks |
| `python/vyos/utils/wwan/interfaces_wwan_firmware.py` | Catalog, CMS trust/CRL, archive safety, compatibility, and cache helpers |
| `interface-definitions/system_update-check.xml.in` | Configuration: cellular catalog URL, cache policy, and notification controls |
| `src/conf_mode/system_update-check.py` | Apply image/cellular update-check settings and manage notification services |
| `src/system/vyos-cellular-firmware-check.py` | 12-hour notify-only firmware check loop |
| `src/systemd/vyos-cellular-firmware-check.service` | Systemd unit for cellular firmware notifications |
| `src/tests/test_wwan_firmware_archive.py` | Archive, cache, compatibility, USB targeting, notification, and lifecycle tests |
| `src/tests/test_wwan_firmware_signing.py` | CMS trust, revocation, catalog cache, and development-bypass tests |
| `python/vyos/utils/wwan/wwan_client.py` | Python: `WWANClientSync` D-Bus client library |
| `python/vyos/utils/wwan/alert_adapters.py` | Python: AlertBus subscriber adapters (REST/MQTT/SNMP skeletons) |
| `python/vyos/utils/wwan/interfaces_wwan_config.py` | Python: D-Bus service — per-interface methods |
| `python/vyos/utils/wwan/interfaces_wwan_service_manager.py` | Python: D-Bus service — control plane |
| `python/vyos/utils/wwan/bearer_lease.py` | Python: bearer-demand lease registry (TTL leases per interface/owner) |
| `python/vyos/utils/wwan/bearer_retry.py` | Python: `connect-retry` backoff profiles |
| `src/helpers/vyos-failover.py` | Python: `protocols failover` daemon — holds the `failover` lease while a connect-on-demand WWAN is the active failover egress |

---

## Quick Reference

| Operational Command | What It Does |
|---|---|
| `show interfaces wwan` | List all WWAN interfaces (summary table) |
| `show interfaces wwan detail` | Detailed standard info for all WWAN interfaces |
| `show interfaces wwan wwan0` | Standard interface info for wwan0 |
| `show interfaces wwan wwan0 status` | Connection state, IP config, signal, SMS, data usage |
| `show interfaces wwan wwan0 bearer-leases` | Who is holding the bearer up (failover/operator leases) and why |
| `show interfaces wwan wwan0 hardware` | Manufacturer, model, IMEI, firmware |
| `show interfaces wwan wwan0 signal` | RSSI, RSRP, RSRQ, SNR, active bands |
| `show interfaces wwan wwan0 sim` | SIM slots, IMSI, ICCID, failover, PIN/PUK status |
| `show interfaces wwan wwan0 detail` | All of the above + data usage + failover history |
| `show interfaces wwan wwan0 wait-failover-alert` | Wait for next failover-related alert |
| `show interfaces wwan wwan0 monitor-alerts` | Collect live alerts during monitor window |
| `show interfaces wwan wwan0 sms` | List all SMS messages |
| `show interfaces wwan wwan0 sms message 3` | Read SMS message #3 |
| `show interfaces wwan wwan0 event-log` | WWAN event log for wwan0 (last 100 events) |
| `show interfaces wwan wwan0 event-log limit 50` | Last 50 WWAN events |
| `show interfaces wwan wwan0 event-log category connectivity` | Connectivity events only |
| `connect interface wwan0` | Bring up WWAN bearer |
| `disconnect interface wwan0` | Tear down WWAN bearer |
| `show interfaces wwan wwan0 firmware` | Show compatible igOS labels, native revisions, dates, cache/running status |
| `update firmware wwan wwan0 1.1` | Authenticate and install a cataloged release with confirmation |
| `update firmware wwan wwan0 latest now` | Install the newest compatible label without confirmation |
| `update firmware wwan wwan0 archive /tmp/test.tar` | Authenticate and install a standalone archive |
| `update firmware wwan wwan0 archive /tmp/lab.tar force-unsigned` | Explicit local unsigned install on an enabled development image |
| `delete firmware wwan wwan0 1.0` | Delete a retained archive without changing flashed firmware |
| `generate interfaces wwan wwan0 sms number '+15551234567' message 'hello'` | Send an SMS |
| `clear interfaces wwan wwan0 sms` | Clear all SMS messages |
| `clear interfaces wwan wwan0 sms message 3` | Clear SMS message #3 |
| `clear interfaces wwan wwan0 data-usage slot 1` | Reset SIM slot 1 data-usage counters |
