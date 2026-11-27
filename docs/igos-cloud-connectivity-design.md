# igOS PerleCLOUD Connectivity — Design Sketch

Status: design draft (no implementation yet)
Scope: define how an igOS unit configured for PerleCLOUD **calls out to the
cloud, establishes a session, stays connected while work is pending, and
recovers from failures** — and make the configuration, status, and failure
behavior consistent across the three components involved:

| Component | Repository | Role |
| --- | --- | --- |
| Config / op-mode glue | `vyos-1x` (this repo) | `service cloud` CLI, conf script, op-mode `show`/`register`, `/auth` relay, nginx include |
| Cloud agent | `igos-cloud-proxy` | `igos-cloud-proxy.service`: registration, command long-poll, command execution, GUI tunnel, local status/JWT API |
| Management GUI | `flask-react-dev` | Cloud status widget, connect/disconnect buttons, `IGOS_CLOUD_SESSION` JWT login |

The PerleCLOUD backend (`devices.perleapps.com`) is outside these repositories.
Whether it can adopt the asks in §10 is an open decision (§13).

---

## 0. Context / grounding facts

### 0.1 How it is wired today

```text
vyos-1x                         igos-cloud-proxy (systemd)               PerleCLOUD
─────────                       ──────────────────────────               ──────────
set service cloud enabled  ──►  /etc/igos-cloud-proxy/igos-cloud-proxy.conf
  service_cloud.py              cloud-proxy (asyncio)
  (stop → write → start)          CloudClient ── register / long-poll ──► routers/register
                                                                          routers/command
op: set service cloud register    CommandProcessor
op: show cloud status ─nginx─┐      ├ EXECUTE_REST_COMMAND → local VyOS API / gateway
                             │      ├ PROXY_CONNECT → HttpProxySession ◄──► proxyUri (GUI tunnel)
REST /auth service=cloud ────┤      └ UNREGISTER_ROUTER / ERROR / EMPTY_COMMAND
                             └─► StatusApi  unix:/run/igos-cloud-proxy/local-api.sock
                                   /perlecloud/{status, .well-known/jwks.json,
                                                sessions/{sid}, sessions/batch}
flask-react-dev
  core/system/cloud.py   status via socket; connect/disconnect = systemctl start/stop
  auth/cloud_auth.py     verify IGOS_CLOUD_SESSION JWT via vyos-1x /auth (service=cloud)
```

### 0.2 vyos-1x side

- `interface-definitions/service_cloud.xml.in` — `service cloud` with only
  `enabled` (valueless) and `override-device-url` (txt). Priority 1280.
- `src/conf_mode/service_cloud.py`
  - `generate()` **always** runs `systemctl stop igos-cloud-proxy`, then writes
    `/etc/igos-cloud-proxy/igos-cloud-proxy.conf` (JSON:
    `tmpDeviceRegistrationPIN`, `perleCloudURL_OVERRIDE`).
  - The PIN is read back from `/config/igos-cloud-proxy/igos-cloud-proxy.conf`
    (or migrated there from `/etc`), so it survives image upgrades.
  - `apply()` starts the unit when `enabled` is set. The unit is never
    `systemctl enable`d; boot-time start relies on the boot commit.
- `op-mode-definitions/cloud.xml.in` / `src/op_mode/cloud.py`
  - `set service cloud register <6 digits>` — stop unit, write PIN to `/etc`
    and `/config`, start unit. Requires `service cloud enabled`.
  - `show cloud status` — `GET https://127.0.0.1:<https port>/perlecloud/status`
    through nginx (restricted to loopback by `/etc/nginx/cloud.conf`).
- `src/services/api/rest/routers.py` (`/auth`, `service == 'cloud'`) — relays
  `jwks`, `validate`, `validate_batch` to the proxy's local API for the GUI.
- `data/templates/https/nginx.default.j2` includes `/etc/nginx/cloud.conf`
  when the file exists (`service_https.py` sets `cloud_conf_exist`).
- `debian/control` — `vyos-1x` depends on `igos-cloud-proxy`.

### 0.3 igos-cloud-proxy side

- Entry point `src/cloud-proxy` → `Proxy.arun()` (`src/proxy/proxy.py`):
  1. Start `StatusApi` (uvicorn on the unix socket, group `www-data`).
  2. `CloudClient.connect()` (`src/proxy/cloud/cloud_client.py`):
     - if a TPM-sealed token exists for the current PIN → use it, no network
       round-trip;
     - else `REGISTER_ROUTER_REQUEST` to `routers/register`, retry network
       errors with backoff 5 → 30 s **forever**; a cloud `ERROR` reply raises
       `CloudRegistrationError`.
     - `_start_client()` calls `state.connect()` — **status becomes
       `connected` before any successful poll.**
  3. Loop: `GET_NEXT_COMMAND` (cloud holds ≤ 30 s, client timeout 35 s) →
     dispatch command task → flush responses → `sleep(nextPollDelay)`
     (default 0). Poll errors back off 2 → 30 s; `connected` is not cleared.
- Identity (`src/proxy/core/config/proxy_config.py`): `routerId` **and**
  `deviceSecretUID` are both the serial from
  `/sys/devices/platform/device-info/serial`; a malformed/missing serial falls
  back to the shared constant `000-000000M00000`.
- Token: `systemd-creds encrypt --with-key=tpm2`, stored at
  `/config/igos/igos-cloud-proxy/.igos-cloud-proxy.key`, keyed by PIN. The PIN
  is also copied to `/config/igos/igos-cloud-proxy/.reg.key`.
- systemd unit `debian/igos-cloud-proxy.service`: `Restart=on-failure`,
  `RestartSec=1`, no start-rate limit.
- Exit paths:

  | Trigger | Exit code | systemd result |
  | --- | --- | --- |
  | No PIN configured (`MissingRegistrationCodeError`) | 0 | stays stopped |
  | Registration rejected by cloud | 0 | stays stopped |
  | `AUTH_FAILED` while polling (token deleted) | 0 | stays stopped |
  | `UNREGISTER_ROUTER` (token deleted) | 0 | stays stopped |
  | SIGTERM (commit, GUI disconnect) | 0 | stopped (intended) |
  | Any other exception (e.g. `TPMError`, config parse) | 1 | restart after 1 s, unbounded |

- Local VyOS access: REST handlers use `VYOS_API_KEY` from the packaged
  `/etc/igos-cloud-proxy/cloud-env.conf` (ships `mykey`); the VyOS gateway
  route requires the cloud to supply the key.
- GUI tunnel: `PROXY_CONNECT` spawns `HttpProxySession`; responses get a
  `Set-Cookie: IGOS_CLOUD_SESSION=<RS256 JWT>`; the signing key is generated
  per process (`session/auth/local/local_auth.py`).

### 0.4 flask-react-dev side

- `flask/app/core/system/cloud.py` — status via `curl --unix-socket`; connect /
  disconnect = `systemctl start|stop igos-cloud-proxy` (bypasses config).
- `flask/app/auth/cloud_auth.py` — JWT verified against proxy JWKS via
  vyos-1x `/auth`; scheduler job `refresh_cloud_sessions` re-validates active
  `sid`s through `validate_batch`.

### 0.5 Related designs

- `igos-tpm-pki-integration-design.md` §3.11 — factory-provisioned, EK-bound
  **DevID** for authenticating the unit to the cloud (mTLS). This design
  consumes that identity; it does not redefine it.
- `igos-wwan-dial-on-demand-design.md` §0 — bearer-lease API
  (`bearer_lease_acquire/release`, owner allow-list in
  `python/vyos/utils/wwan/interfaces_wwan_config.py`
  `_LEASE_OWNER_ALLOWLIST = {'cli', 'operator', 'failover', 'dod-traffic'}`).
  Also documents the dial-on-demand traffic trigger (NFQUEUE on `output` for
  router-originated traffic to `wwanN`) and `tx_bytes`-based idle watcher that
  this design relies on (§2, §5).

---

## 1. Problems with the current design

1. **Status is wrong.** `connected` is set when the HTTP client object is
   created and never cleared on poll failure. CLI and GUI report "connected"
   with the WAN down or the cloud unreachable.
2. **Silent permanent stop.** Four recoverable conditions exit 0 under
   `Restart=on-failure`, so the unit never calls out again until a commit,
   reboot, or GUI click. Nothing in status says *why*.
3. **Hot restart loop.** Exit-1 failures restart every 1 s without limit. A
   TPM seal failure *after* a successful registration can re-register in a
   loop (unverified on hardware).
4. **Always-on only.** One request per ≤ 30 s forever, no idle/check-in mode.
   On cellular this keeps the bearer busy, defeats dial-on-demand idle-down,
   and costs data; with `nextPollDelay = 0` the cloud fully controls the rate.
5. **Weak identity.** The device secret equals the public device ID (serial);
   bad-EEPROM units collide on a shared default.
6. **Scattered state.** PIN stored plaintext in three files (`/etc/…conf`,
   `/config/igos-cloud-proxy/…conf`, `/config/igos/igos-cloud-proxy/.reg.key`);
   token keyed by PIN; doc/code disagree on token path.
7. **Config ≠ runtime.** GUI connect/disconnect toggles systemd directly;
   every `service cloud` commit restarts the agent even when nothing changed.
8. **CLI/docs drift.** Proxy docs describe `set service cloud registration
   <code>`; actual configure-mode has no such node — registration is op-mode.
   Documented backoff (5 s / 120 s) ≠ code (2 s / 30 s).
9. **Smaller defects** (fix alongside, see Appendix A).

---

## 2. Concept

The cloud agent may reach PerleCLOUD over **any** interface. How it behaves is
decided by the **class of the link the cloud traffic egresses on**, not by an
operator-selected mode:

| Egress link | Behavior |
| --- | --- |
| **Unmetered** (Ethernet, etc.) | **Persistent** — stay connected with the long-poll, as today. The agent does no link management; it just reconnects when the path breaks. |
| **Metered** (cellular) | **Duty cycle** — periodically bring the metered link up, check in, do whatever the cloud has queued, then disconnect and let the link idle until the next check-in. |

The class can change at runtime (Ethernet fails → failover to cellular, or
fail-back), and the agent switches behavior when it does (§4.3).

**Product constraint (proposed): metered WWAN links must use
`connection-mode dial-on-demand`.** The WWAN subsystem then owns link up/down
entirely: the agent's own check-in traffic dials the modem through the existing
traffic trigger, and the existing idle watcher drops the bearer once the agent
goes quiet. The agent never calls the WWAN API to raise or drop the link; its
only obligation is traffic discipline (§5). See §5.4 for the alternative
(supporting every WWAN mode via bearer leases) and why it is not preferred.

---

## 3. Goals / non-goals

### Goals

- G1 — When `service cloud` is enabled, the unit **always eventually
  reconnects** without operator action, for every recoverable failure.
- G2 — On a **metered** egress, the agent keeps the link up **only** for a
  check-in and any work it produces; between check-ins it generates **no
  traffic at all** on that link (no polls, retries, DNS, TCP keep-alives).
- G3 — On an **unmetered** egress, the agent stays connected with the
  existing long-poll (minimum command latency).
- G4 — **Truthful, explained status**: state + reason + egress + last/next
  check-in, identical in CLI, GUI and cloud.
- G5 — **One source of truth** for enablement and settings: the VyOS config
  tree. The GUI changes cloud state only through config.
- G6 — Bounded resource use: capped backoff with jitter, systemd start-rate
  limits, no hot loops, and a hard cap on how long the cloud can keep a metered
  link up.

### Non-goals

- Redefining device identity/PKI (owned by the TPM-PKI design §3.11).
- Changing the GUI tunnel (`PROXY_CONNECT`) data path or the VyOS gateway.
- Changing WWAN FSM / failover / dial-on-demand behavior — the agent relies on
  the existing dial-on-demand trigger and idle watcher.
- Cloud-side (PerleCLOUD) implementation beyond what §10 requires.

---

## 4. Link classification and egress selection

### 4.1 Which links are metered

- **Default rule:** `wwanN` interfaces are metered; everything else is
  unmetered.
- **Override:** operator can reclassify per interface (e.g. an
  unlimited-plan cellular as unmetered, or an Ethernet-attached satellite/LTE
  modem as metered) — §9. Whether this lives under `service cloud` or as a
  generic per-interface attribute is an open decision (§13).

### 4.2 Determining the egress

Before each connect (unmetered) or check-in (metered):

1. Use the **cached** cloud host addresses. DNS is refreshed only when the
   agent already has a usable path (unmetered, or inside a metered check-in
   window) — a lookup on an idle dial-on-demand link would itself dial the
   modem.
2. `ip route get <cloud address>` (no packet sent) → egress interface.
3. Decide:

   | Result | Mode |
   | --- | --- |
   | Egress is unmetered | Persistent |
   | Egress is metered | Metered duty cycle |
   | No route | `NO_PATH` — retry with persistent backoff |

   An **idle dial-on-demand** WWAN still resolves as the egress: dial-on-demand
   idles *silently* — the kernel link stays `UP,LOWER_UP` and the FRR carrier
   default route (`tag 210`) stays configured (`_remove_default_routes()` in
   `interfaces_wwan_state_machine.py` runs only on delete/admin-disable). So
   `ip route get` returns `wwanN` and the "no route" case does not arise for
   metered links. (A `connect-on-demand` link, by contrast, is forced down
   with its address flushed when idle — one reason it is excluded, §5.4.)
   *To verify on hardware: route table and `ip route get` while idle, with and
   without `protocols failover`.*

### 4.3 Switching at runtime

- **Unmetered → metered** (e.g. Ethernet down, failover to cellular): the
  long-poll fails → re-evaluate → egress now metered → close the HTTP client
  and enter the metered schedule. Whether the first metered check-in happens
  immediately (to tell the cloud "now on cellular") or at the next interval is
  an open decision (§13).
- **Metered → unmetered** (fail-back): detected at the next check-in, or
  immediately via an rtnetlink route-change watch (optional) → close the
  metered-window sockets and switch to persistent.
- The agent re-evaluates egress on every poll cycle in persistent mode (a
  route lookup is cheap) so it never keeps long-polling over a link that has
  become metered.

---

## 5. Metered duty cycle

With the dial-on-demand constraint, "bring the link up" and "disconnect" are
side effects of the agent's traffic, not API calls.

### 5.1 Sequence

```text
SLEEP ──(check-in-interval ± jitter, or trigger-now)──► CHECK-IN
CHECK-IN: first packet (DNS or TCP SYN) hits the dial-on-demand NFQUEUE trigger
          → modem dials → queued packets released (or dropped after
          flush-timeout → one retry)
          register if needed; GET_NEXT_COMMAND with short hold, repeat until
          EMPTY_COMMAND and nothing in flight
          nothing to do ──► TEARDOWN
WORK:     execute commands; keep polling while any of:
            - command tasks or response sends in flight
            - VyOS-gateway async tasks running
            - a GUI tunnel (PROXY_CONNECT session) open
            - a cloud hold (holdSeconds, §10)
LINGER:   no work for linger-timeout ──► TEARDOWN   (new command ──► WORK)
TEARDOWN: send DISCONNECT {offlineDuration = seconds to next check-in}
          close all sockets (no keep-alive connections left open)
          ──► SLEEP   (dial-on-demand idle watcher drops the bearer after
                       idle-timeout of no tx)
```

### 5.2 Traffic discipline (the agent's whole contract with WWAN)

1. **Silence in `SLEEP`.** Any router-originated packet routed to `wwanN`
   dials the modem. Between check-ins the agent must send nothing: no polls,
   no retries, no DNS, no TCP keep-alives, no lingering sockets, no status-API
   side traffic (the status handler's UDP `connect()` sends no packets, but
   DNS for the cloud host would — cache it).
2. **Keep tx flowing while working.** The idle watcher looks only at
   `wwanN` `tx_bytes`. A long-poll held longer than `idle-timeout` with no tx
   lets the bearer idle *mid-poll* (and a re-dial may change the carrier IP,
   breaking the TCP connection). While in `CHECK-IN` / `WORK` / `LINGER` the
   poll hold is capped at **`idle-timeout / 2`** of the egress interface
   (read from `/run/wwan/interfaceN.conf` or the WWAN API). An open GUI
   tunnel generates its own traffic; its idle exchanges must also stay under
   that bound.
3. **Tolerate dial latency.** The first connection waits for the bearer:
   the agent's connect timeout must exceed the interface's dial-on-demand
   `connect-timeout`; a flush (packets dropped after `flush-timeout`) gets
   exactly one immediate retry, then back to `SLEEP`.
4. **Hard cap.** `max-connected-time` bounds a single window so a stuck tunnel
   or misbehaving cloud cannot keep cellular up indefinitely. On expiry: send
   `DISCONNECT`, close sockets, `SLEEP`.
5. **No fast backoff on metered.** A failed check-in returns to `SLEEP`; every
   retry would re-dial. At most one early retry (`retry-interval`), then the
   normal interval (§13).

### 5.3 What "something to do" means

The device cannot know what the cloud has queued until it asks, so the cloud
must **store and forward** commands for a sleeping metered device (§10). At
check-in the agent drains the queue; anything the cloud returns other than
`EMPTY_COMMAND` (REST command, gateway request, `PROXY_CONNECT`,
`UNREGISTER_ROUTER`) is work.

Remote GUI access over a metered link therefore has **check-in latency**: the
user's request is queued and the tunnel opens at the next check-in (or on
trigger-now). The cloud UI must convey this.

### 5.4 Other WWAN modes

| WWAN `connection-mode` | With the constraint | If we instead supported it |
| --- | --- | --- |
| `dial-on-demand` | **Supported.** Traffic dials; idle watcher drops. | — |
| `always-on` | Commit error unless the interface is reclassified **unmetered** (e.g. unlimited plan) → persistent mode. | Duty cycle would save data but not airtime/power; bearer stays up regardless. |
| `connect-on-demand` | Commit error. | Idle link is forced DOWN with address flushed → no route; the agent would need a `cloud` bearer lease (D-Bus to `igos-wwan-manager`, renew/release, crash TTL) to raise it. |

Why the constraint is preferred:

- **One owner of the metered link.** Up/down, vetoes (data-limit hold,
  airplane, `disable`), and failover remain entirely in the WWAN subsystem;
  the agent needs no WWAN API, no D-Bus, no lease bookkeeping.
- **Consistent with every other metered user.** Any router-originated or
  forwarded traffic already behaves this way on a dial-on-demand link; the
  cloud is just another well-behaved client.
- **Fits failover.** With `protocols failover` and a dial-on-demand backup, the
  cloud's egress is Ethernet (persistent) until the outage; then its traffic
  falls to the WWAN route and dials like any other flow.
- **Data-limit hold** is enforced by the FSM regardless — the check-in simply
  fails to dial; status reports `METERED_UNREACHABLE` with the WWAN hold
  reason (read-only query).

Costs / risks:

- Depends on the dial-on-demand implementation quality (today a
  restart-per-cycle service, see `igos-wwan-dial-on-demand-design.md`); the
  daemon redesign there is compatible — the agent still only generates traffic.
- Poll hold coupling to `idle-timeout` (§5.2.2). Optional future refinement:
  hold a `cloud` lease only during an open GUI tunnel, once the
  dial-on-demand daemon is lease-based (`dod-traffic`).
- Operators with an always-on cellular primary must either switch to
  dial-on-demand or declare the link unmetered.

---

## 6. Persistent (unmetered) mode

Today's long-poll, made robust:

- "Connected" = a poll succeeded within `2 × poll timeout`.
- Reconnect backoff 2 s → 300 s with full jitter.
- Egress re-evaluated every cycle (§4.3).
- Cloud `nextPollDelay` / `keepAlive` honored as today.

---

## 7. Agent supervisor state machine

Replace the "connect once, loop, exit" structure in `Proxy.arun()` with an
in-process supervisor that **never exits for recoverable conditions**.

| State | Entered when | Next |
| --- | --- | --- |
| `UNPROVISIONED` | No PIN and no token, or invalid serial | stays; wakes on PIN write (SIGHUP) |
| `REG_REJECTED` | Cloud rejected PIN | stays until new PIN; slow retry? (§13) |
| `NO_PATH` | No route to the cloud | persistent backoff |
| `PERSISTENT_CONNECTING` | Unmetered egress, token present | `PERSISTENT_CONNECTED` after first successful poll |
| `PERSISTENT_CONNECTED` | Poll succeeded recently | `PERSISTENT_BACKOFF` on failure; metered states if egress changes |
| `PERSISTENT_BACKOFF` | Network / 5xx / timeout | re-evaluate egress, then connect |
| `METERED_SLEEP` | Metered egress, between check-ins | `METERED_CHECKIN` on timer / trigger-now |
| `METERED_CHECKIN` | Check-in due (traffic dials the bearer) | `METERED_WORK`, `METERED_TEARDOWN`, or `METERED_UNREACHABLE` |
| `METERED_UNREACHABLE` | Dial/flush failed or cloud unreachable; WWAN hold reason (data-limit, airplane, disable) attached if any | `METERED_SLEEP` (status keeps reason) |
| `METERED_WORK` / `METERED_LINGER` | Work in flight / just finished | `METERED_TEARDOWN` |
| `METERED_TEARDOWN` | Done, cap reached, or egress became unmetered | `METERED_SLEEP` or persistent states |
| `AUTH_FAILED` | `AUTH_FAILED` from cloud | token dropped → re-register if PIN still valid, else `UNPROVISIONED` |
| `UNREGISTERED` | `UNREGISTER_ROUTER` | token and PIN dropped → `UNPROVISIONED` |

Rules:

- Registration in metered mode happens **inside** a check-in window, never as a
  free-running retry loop.
- Exit non-zero only for genuinely fatal local errors. Unit gains
  `RestartSec=10`, `StartLimitIntervalSec=300`, `StartLimitBurst=5`.
- On any exit path all sockets are closed so nothing keeps the bearer up.
- TPM seal failure after registration: keep the token in memory, report
  `token_persist_failed`, retry sealing later — never re-register in a loop.

---

## 8. Status model

Single structure produced by the agent's `/perlecloud/status`, consumed by
`show cloud status`, the GUI widget and the `cloudServicesStatus` REST route:

```json
{
  "state": "METERED_SLEEP",
  "reason": null,
  "link_class": "metered",
  "egress": {"name": "wwan0", "address": "10.1.2.3"},
  "device_id": "123-456789M01234",
  "cloud_url": "https://devices.perleapps.com/",
  "last_success": "2026-11-19T16:20:03Z",
  "last_check_in": "2026-11-19T16:20:03Z",
  "next_check_in": "2026-11-19T17:20:00Z",
  "last_error": {"at": "...", "kind": "network", "detail": "..."},
  "active_tunnels": 0,
  "wwan_hold": null
}
```

- Keep the existing `cloud_connection_*` fields until vyos-1x op-mode and the
  GUI are updated. `cloud_connection_status` maps to `connected` only in
  `PERSISTENT_CONNECTED` / `METERED_CHECKIN` / `METERED_WORK` /
  `METERED_LINGER`; `METERED_SLEEP` reports a new value `standby` (not
  `disconnected`).
- `wwan_hold` is a read-only query of the egress WWAN (data-limit hold,
  airplane, `disable`) so "why didn't it check in?" is answerable.
- One shared egress/role rule (route-based, override-URL aware) for the status
  API and the REST route.

---

## 9. Configuration (vyos-1x)

Proposed configure-mode tree:

```text
service cloud
    enable                                   (rename of 'enabled'? §13)
    override-device-url <url>                (unchanged; dev only)
    metered
        check-in-interval <300-86400>        (s; default TBD §13)
        linger-timeout <10-3600>             (s; default TBD)
        retry-interval <60-86400>            (s; one early retry after a failed check-in)
        max-connected-time <60-86400>        (s; hard cap per check-in)
    link-class <ifname> metered|unmetered    (override default rule; §4.1, §13)
```

There is intentionally **no** `connection-mode` knob: behavior follows the
egress link class.

- **PIN stays op-mode** (`set service cloud register <code>`): it is a one-time
  secret and must not land in `config.boot` or config backups. Store it in
  **one** place owned by the agent (`/config/igos/igos-cloud-proxy/`); drop
  the vyos-1x copy in `/config/igos-cloud-proxy/` (needs an upgrade cleanup).
- Op-mode additions: `connect cloud now` (trigger-now: immediate check-in on
  metered, no-op/reconnect on unmetered); `show cloud status` shows the §8
  fields.
- `service_cloud.py`:
  - render config via a Jinja2 template under
    `data/templates/igos-cloud-proxy/` (repo convention);
  - restart only on change; prefer SIGHUP re-read for timer changes;
  - `verify()`: URL format, `link-class` interfaces exist, timer sanity
    (`linger-timeout < max-connected-time`);
  - **`verify()` enforces the product constraint**: with `service cloud
    enable`, every `wwanN` that is metered (default rule or `link-class`) must
    have `connection-mode dial-on-demand`, else `ConfigError`. Because
    `interfaces wwan` and `service cloud` can change in one commit, the same
    check is needed from the wwan side — add `service cloud` to the wwan
    conf-mode dependency (`data/config-mode-dependencies/`), or check from
    both scripts.
- GUI connect/disconnect → configure `service cloud enable` via the VyOS API,
  not `systemctl`. A GUI "check in now" button maps to `connect cloud now`.
- igOS migration under `src/migration-scripts/igos/` only if `enabled` is
  renamed.

---

## 10. Protocol touch-points

Reuses fields that already exist:

- `GET_NEXT_COMMAND.messageParams.timeout` — short hold (≈ 0–2 s) while
  draining at check-in; normal 30 s hold during `METERED_WORK` and persistent
  mode.
- `DISCONNECT.messageParams.offlineDuration` — seconds until the next
  check-in, so the cloud can show "standby, next check-in at T" instead of
  "offline".
- Poll hold on metered is capped at `idle-timeout / 2` (§5.2), so the cloud
  must honor `timeout` values shorter than its default.

Cloud-side asks:

1. **Store and forward**: queue commands (including GUI-access requests) for a
   device in standby and deliver them at its next check-in.
2. **Honor a short `timeout`**: return `EMPTY_COMMAND` immediately when the
   queue is empty, so an idle check-in costs one round trip.
3. **Offline detection**: treat a metered device as healthy until
   `offlineDuration + grace` has passed.
4. Optional `EMPTY_COMMAND.messageParams.holdSeconds`: "stay connected N s,
   more work is coming" (e.g. a user is actively using the cloud UI).
5. Optional out-of-band wake for urgent work (cloud → SMS → `connect cloud
   now`; see `igos-sms-command-gateway-design.md`).

Without 1–3 the duty cycle still works, but queued work may be lost and the
cloud will show the device offline between check-ins.

---

## 11. Identity and secrets (interim vs. target)

- **Target**: DevID mTLS per TPM-PKI §3.11; `deviceSecretUID` retired.
- **Interim**: keep serial-based registration, but
  - refuse to register with the shared `000-000000M00000` fallback in release
    builds (`UNPROVISIONED`, reason `invalid_serial`);
  - replace the packaged `VYOS_API_KEY=mykey` with a per-unit key (§13;
    interacts with `igos-gui-user-accounting-design.md`).

---

## 12. Code layout / changes by repository

| Repo | Change |
| --- | --- |
| igos-cloud-proxy | supervisor (`proxy/supervisor.py`), egress classifier (`proxy/core/net/egress.py`), metered scheduler (traffic discipline, `idle-timeout`-aware poll hold), truthful `CloudProxyState`, status model, SIGHUP reload, systemd limits, Appendix A fixes, docs |
| vyos-1x | `service cloud` XML (`metered …`, `link-class`), `service_cloud.py` template + change-only reload + dial-on-demand constraint check (both directions), op-mode `connect cloud now` and `show cloud status`, PIN single-store + upgrade cleanup |
| flask-react-dev | status widget (`state`, `link_class`, `next_check_in`, `standby`), enable/disable via config API, "check in now" |

---

## 13. Open decisions

1. **Default `check-in-interval`** (and `linger-timeout`) for cellular — e.g.
   15 min vs 1 h vs 24 h; the main latency-vs-data trade-off.
2. **Classification override** — `service cloud link-class …` vs a generic
   per-interface `metered` attribute other services could reuse.
3. **First check-in on unmetered → metered switch** — immediate, or wait for
   the interval?
4. **Failed metered check-in** — one early retry, or wait a full interval?
5. **Rejected/expired PIN** — wait for a new PIN, or slow retry?
6. **PIN lifetime** — delete after registration, or keep for re-registration
   after `AUTH_FAILED`?
7. **Backend** — can PerleCLOUD implement §10 items 1–3 (required for a good
   metered experience) and optionally 4–5?
8. **Rename `enabled` → `enable`** with an igOS migration, or keep?
9. **Local API key** — per-unit generated key vs a restricted API identity.
10. **Dial-on-demand constraint** — adopt it (preferred, §5.4) or support
    `connect-on-demand` / `always-on` metered links via a `cloud` bearer
    lease? Hard commit error vs warning for non-compliant WWANs?
11. **Metered-ness of non-WWAN links** — with the constraint, a link
    reclassified metered via `link-class` (e.g. Ethernet satellite modem) has
    no dial-on-demand to bring it up/down; the agent would duty-cycle traffic
    only. Acceptable, or restrict `metered` to WWAN?

---

## 14. Testing

- Unit (igos-cloud-proxy): every §7 transition; egress classification table
  (§4.2) with mocked `ip route get`; no traffic generated in `METERED_SLEEP`;
  backoff bounds; status truthfulness; TPM seal-failure path.
- Integration: fake cloud driving `EMPTY_COMMAND`, queued commands,
  `PROXY_CONNECT`, `AUTH_FAILED`, `UNREGISTER_ROUTER`; route changes
  (Ethernet down/up) to exercise §4.3.
- On hardware (cellular unit):
  - dial-on-demand: bearer dials only at check-in and idles `idle-timeout`
    after teardown; no idle-down mid-poll during `WORK` / GUI tunnel;
  - idle dial-on-demand route state: `ip route get <cloud>` → `wwanN`;
  - first-packet flush path (`flush-timeout` shorter than dial) → one retry;
  - no wwan tx between check-ins (`/sys/class/net/wwanN/statistics/tx_bytes`);
  - data-limit hold → `METERED_UNREACHABLE` with hold reason, no dial;
  - Ethernet + cellular failover/fail-back switches mode;
  - bytes per check-in and per day for chosen intervals.
- vyos-1x smoketest: `service cloud` renders config, change-only reload,
  `show cloud status` fields, commit rejected for `service cloud enable` +
  non-dial-on-demand metered WWAN (both commit orders).

---

## Appendix A — Defects to fix alongside (igos-cloud-proxy)

- `EmptyCommand.execute` raises `AttributeError` when `keepAlive` /
  `nextPollDelay` are omitted (documented as optional) — use `params.get()`.
- `ERROR` commands execute twice (in `CloudClient.poll_command` and again via
  `CommandProcessor.to_executor`).
- `StatusApi.shutdown` references `cls._remove_socket` without calling it.
- `rest_command._get_cloud_iface` uses `DEFAULT_PERLE_CLOUD_URL`, ignoring the
  override.
- JWT signing key regenerated per process → active cloud GUI sessions are
  invalidated by any agent restart (including every `service cloud` commit).
- Docs: token path (`/var/lib/…` vs `/config/igos/…`), CLI syntax, backoff
  values.
