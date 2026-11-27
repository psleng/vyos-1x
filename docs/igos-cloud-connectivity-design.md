# igOS PerleCLOUD Connectivity — Design Sketch

Status: design draft (no implementation yet). Two approaches: Option 1 patches
what exists (§18); Option 2 is the full solution (§2–§16, §19).
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

Also covered: how information moves today (§0.6), device-originated alerts
(§15), remote access to the router and connected devices (§16), and a
hand-off checklist for the cloud and GUI teams (§17).

---

## Approaches at a glance

This document offers two approaches. Both start from the same findings (§0,
§1, Appendix A).

| | **Option 1 — Patch what exists** (§18) | **Option 2 — Full solution** (§2–§16, §19) |
| --- | --- | --- |
| Idea | Keep today's protocol, long-poll and GUI tunnel; fix defects, make status truthful, close the security gaps, add the smallest controls | Rework connection handling for metered links, add a general-purpose stream tunnel, device-sent events and capability negotiation |
| Remote GUI of the router | Yes (today's GUI tunnel, hardened) | Yes, over the stream tunnel (phase 2) |
| Web pages of LAN devices | Optional, HTTP only, allow-listed (no WebSockets) | Yes (HTTPS via cloud reverse proxy) |
| SSH to LAN devices / router / serial ports | **No** | Yes |
| Web CLI from the cloud | No | Yes (phase 2) |
| VNC / RDP / GPS live view | No | When the cloud adds clients; no router change |
| Cloud on cellular | Block it (`egress`) or let the cloud slow polling (`nextPollDelay`) | Metered duty cycle (§2–§7), or block it (`egress`) |
| Alerts to the cloud | Cloud reads history through the gateway (§15.1) | Plus push (`DEVICE_EVENT`, §15.3) |
| Cloud user roles on the device | Passed in `PROXY_CONNECT` (small cloud change) | Passed with every session and tunnel |
| Router API access | GUI and agent use a root-only local API socket, no keys; cloud holds no key and sends the user's role (§11.2, §19.9) | Same |
| PerleCLOUD effort | **Small** (a few optional fields; status display) | **Large** (relay, browser clients, store-and-forward) |
| Device effort | Small–medium | Medium–large |

**Option 1 is not wasted work for Option 2.** Its fixes, truthful status,
`egress` control, capability reporting, unknown-command handling and role
passing are all needed by Option 2 (§18.4).

Section map:

| Section | Option 1 | Option 2 |
| --- | --- | --- |
| §0 Context, §1 Problems, Appendix A Defects | ✔ | ✔ |
| §2–§7 Metered concept, duty cycle, supervisor | Supervisor and status only (§18.1) | ✔ |
| §8 Status model, §9 Configuration | Subset (§18.1–§18.2) | ✔ |
| §10 Protocol touch-points, §10.1 Capability exchange | Capability reporting only (§18.5) | ✔ |
| §15 Alerts | §15.1–15.2 (read history) | ✔ incl. push |
| §16 Remote access | — (HTTP-only LAN access via GUI tunnel, §18.3) | ✔ |
| §17 Hand-off | Option 1 rows (§18.6) | ✔ |
| §19 Wire protocol details | — | ✔ |

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
  `/etc/igos-cloud-proxy/cloud-env.conf` (a fixed packaged value; security
  finding G11); the VyOS gateway
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
- `igos-alerting-design.md` — the alert record, history file and per-channel
  delivery queue that device-originated cloud information builds on (§15).

### 0.6 Information transfer today

Verified against the code in all three repositories (2026-11-26).

**Direction.** The protocol is pull-only. The device long-polls
`routers/command`; the cloud returns one command per poll; the device posts a
response when `requiresResponse` is set. The cloud cannot send anything
unprompted, and the device sends nothing except registration, poll requests,
command responses and `DISCONNECT`.

**What the cloud can reach on the device:**

| Path (cloud command) | Reaches | Cloud must supply | Runs on the device as |
| --- | --- | --- | --- |
| Built-in managed-device routes (`EXECUTE_REST_COMMAND`): `/api/v1.1/managed-devices/system`, `…/services/cloud-services/status`, `…/easyport/interfaces/all`, `…/cli` | Fixed `show` commands. `cli` returns `show config`, version, memory, storage and the log. | Nothing | The proxy's own key, `VYOS_API_KEY` from `cloud-env.conf` (a fixed packaged value, same on every unit) |
| VyOS gateway (`EXECUTE_REST_COMMAND` to `/api/v1.1/managed-devices/vyos`) | The VyOS HTTP API: `/info`, `/retrieve`, `/show`, `/generate`, `/configure`, `/config-file`, `/reset`, `/reboot`, `/poweroff`, `/image`, `/graphql` | The API key in `body.key` on every request except `/info`; for `/graphql`, in the GraphQL variables | Whatever key the cloud sends |
| GUI tunnel (`PROXY_CONNECT`) | Any URL the cloud puts in `proxyRequestUri`; intended to be the local GUI | Nothing beyond its device registration | A Flask session as user `PerleCLOUD` with role `administrator`; Flask then uses its own API key |

**VyOS HTTP API authentication (vyos-1x).** Every REST route requires `key` on
every request (router-wide `auth_required` dependency in
`src/services/api/rest/routers.py`); only `/info` is open. GraphQL queries and
mutations require the key, or a bearer JWT when the API is in token mode. There
is no login session.

**GUI tunnel authentication.** The proxy adds
`Set-Cookie: IGOS_CLOUD_SESSION=<JWT>` to every forwarded response, signed with
a key generated per process. Flask verifies it through vyos-1x `/auth`
(`service=cloud`) and logs the user in as `PerleCLOUD`.

**User levels today:**

| Access path | Levels | Where defined and enforced |
| --- | --- | --- |
| CLI (SSH, console) | admin; operator | Admin = member of `vyattacfg` (no `operator` node). Operator = `system login user <u> operator group <g>`, with `system login operator-group <g> command-policy allow '<command>'`. `vyos-op-run` (`src/ocaml/vyos_op_run.ml`) restricts operators to op-mode commands in their groups' allow lists, and always denies `configure`, `commit`, `commit-confirm`, `confirm`, `execute shell` and commands containing special characters. |
| GUI | administrator, operator+, operator | Flask session `permission`. Local login: `vyattacfg` → administrator, otherwise operator unless the Flask user database says otherwise (operator+ exists only there). TACACS+/RADIUS: privilege ≥ 10 → administrator, ≥ 5 → operator+, else operator. SAML JIT: `admin` / `super-operator` / `operator` → administrator / operator+ / operator. |
| HTTP API (REST, GraphQL, cloud gateway) | none | `service https api keys id <id> key <k>` has no role; any valid key has full access. |
| Cloud GUI tunnel | always administrator | Hard-coded in the proxy (see findings). PerleCLOUD's own user levels never reach the device. |
| GUI web CLI | administrator only | `/cli/shell` in Flask requires `administrator`. The terminal itself is a WebSocket from nginx `/cli/shell/ws` straight to ttyd on `127.0.0.1:7681`, without the Flask check. Which account the ttyd shell runs as is not defined in these repositories (to verify). |

Notes:

- `operator+` has no CLI equivalent, and a GUI operator is not a CLI operator:
  the GUI levels are enforced by Flask, the CLI levels by `vyos-op-run`.

**CLI access from the cloud.** Cloud users have no interactive terminal:

- The GUI web CLI needs a WebSocket. `XTerm.tsx` opens
  `ws://<host>/cli/shell/ws` (ttyd protocol), and the iframe variant
  (`ShellCLI.tsx`) loads ttyd's own page from `/cli/shell`, which opens the
  same WebSocket. nginx forwards `/cli/shell/ws` to ttyd on `127.0.0.1:7681`.
- The GUI tunnel carries only request/response HTTP, wrapped in JSON one
  exchange at a time (protocol §6). `HttpProxySession._handle_request` answers
  any WebSocket upgrade with `501 Not Implemented`, so a cloud user can open
  the CLI page but the terminal never connects.
- What the cloud can do instead, non-interactively:
  - run individual op-mode or configuration commands through the VyOS gateway
    (`/show`, `/configure`, `/graphql`, …) — today needs the device's API key
    (§11.2 removes that), and PerleCLOUD must provide its own command-entry
    screen;
  - fetch the fixed tech-support bundle through
    `/api/v1.1/managed-devices/cli` (config, version, memory, storage, log) —
    no key needed.
- A cloud terminal would need WebSocket support in both the proxy and the
  PerleCLOUD relay, or a different terminal transport.
- Derived from code; not yet confirmed on a unit with a live PerleCLOUD
  session.
- Separately, the web CLI builds its URLs from the page's hostname with
  hard-coded schemes and no port (`react/src/constants/api.ts`:
  `WEBCLI_URL = ${hostname}/cli/shell`; `ws://…/ws` in `XTerm.tsx`,
  `http://…` in `ShellCLI.tsx`). Browsers normally block `ws://` and `http://`
  from an HTTPS page, and it will break when the GUI is served from a cloud
  subdomain (§16 phase 2). To verify on a unit; fix with relative or
  same-scheme URLs.

**Findings:**

- **The cloud is fully trusted.** Through the tunnel, anyone the cloud
  authenticates is a GUI administrator and can change configuration. Requiring
  the API key on gateway calls therefore adds no protection; it only makes the
  cloud store each device's key.
- **Role is hard-coded.** `_local_auth_cookie()` in
  `session/types/http_proxy.py` always issues `role: "administrator"`. Flask
  also accepts `operator` and `operator+`, but `PROXY_CONNECT` has no role
  field, so the cloud user's role never reaches the device.
- **Tunnel target is not restricted.** `proxyRequestUri` is forwarded exactly
  as supplied, so the cloud can reach any address the device can reach,
  including LAN hosts, not only the local GUI. That includes **looping back
  out to the internet** (e.g. `https://google.com`) over whatever WAN is
  active, cellular included, appearing to come from the customer's address.
  The firewall cannot block this selectively: the `output` chain cannot tell
  proxy traffic from the router's own (no match by process).
- **Status route is static.** `cloud-services/status` always reports
  `cloudConnectionStatus: "connected"`.
- **Responses are fire-and-forget.** A failed response send is logged and
  dropped (`Proxy._send_response`). Gateway results are kept in memory for
  24 h, recoverable by `taskId`, and lost when the proxy restarts.
- **Inline transfers only.** Everything travels as JSON in the command channel.
  Gateway limits: 8 MB request, 1 MB response.
- **No version or capability advertisement.** The cloud cannot tell which
  routes or gateway endpoints a firmware supports, and an unknown command type
  is dropped without a reply while the proxy backs off polling (§10.1).

**Alerts and events today:**

- `vyos-event-handler` (vyos-1x) runs a script when a log line matches. It
  keeps no history and has no op-mode command; it logs only
  `Pattern found: "<regex>", script executed: "<path>"`.
- GUI-created events run `gui_event_notification.py`, which posts to Flask
  `/callback`. Flask stores them in its own database (at most 40 critical,
  30 warning, 30 info), shows them on the alarm monitor page and pushes a
  Socket.IO notification. Email goes out as the test email; SMS is not sent.
  CLI-created events have no history at all.
- SMS and email keep no record of which alert was sent.
- The cloud cannot read Flask's database through the gateway. It can search
  `show log` through the gateway, or view the GUI alarm page through the
  tunnel. There is no device-initiated alert message.

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
| `BLOCKED` | Route to the cloud leaves on a denied link (§9 `egress`) | stays, sending nothing; re-evaluates on route change (rtnetlink) or every 60 s (route lookup only); persistent or metered states once the route moves to an allowed link |
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
- `BLOCKED` reports `reason: "egress_denied"` with the denied `egress`;
  `cloud_connection_status` is `blocked`, so the GUI and cloud can show
  "offline by policy (on wwan0)" rather than a fault.
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
    egress
        metered allow|deny                   (default allow; deny = no cloud traffic on metered links)
        deny-interface <ifname>              (multi; never send cloud traffic out this interface)
```

There is intentionally **no** `connection-mode` knob: behavior follows the
egress link class.

**Keeping cloud traffic off a link (`egress`).** Before every cloud connection
— command channel, registration, GUI tunnel (`proxyUri`), remote-access relay
(`relayUri`, §16) and alert push (§15) — the agent looks up the route to the
host without sending anything (`ip route get`, §4.2). If the egress is a
denied interface, or metered while `metered deny` is set, it sends nothing,
enters `BLOCKED` (§7) and reconnects automatically when the route moves to an
allowed link. With `metered deny`, the metered duty cycle (§5) never runs.
Routing still decides the egress: denying `wwan0` does not move the cloud to
Ethernet; it keeps the cloud offline while the only route is cellular. This
covers hosts the cloud supplies at runtime, which a firewall rule cannot.

Options available today, without code (operator-configured):

- **Firewall drop:** `firewall group domain-group PERLECLOUD address
  devices.perleapps.com` (plus every proxy/relay host), and a
  `firewall ipv4 output filter` rule with `destination group domain-group
  PERLECLOUD`, `outbound-interface name wwan0`, `action drop`. Packets are
  dropped inside the router, so no cellular data is used.
- **Pin to Ethernet:** an `output` rule matching the same group sets a mark;
  `policy local-route rule … fwmark <n> set table <t>` routes it through a
  table containing only the Ethernet route.
- Limits of both: they miss hosts supplied at runtime (`proxyUri`,
  `relayUri`); the firewall cannot match by process, only by destination; DNS
  lookups for the cloud host still leave on the active link; and today's
  status still says "connected" (§1). Keep the firewall rule as a backstop
  once `egress` exists.

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
  - `verify()`: URL format, `link-class` and `egress deny-interface`
    interfaces exist, timer sanity (`linger-timeout < max-connected-time`);
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

### 10.1 Capability exchange (prerequisite for any new message type)

Today an unknown `messageType` is not answered. `CommandFactory.create()`
raises `CommandError`, `poll_command` turns it into "invalid cloud command
response", and `Proxy.arun()` backs off polling (2 → 30 s). A cloud that sends
`TUNNEL_OPEN` (§16) or any future command to older firmware gets no reply, and
other commands for that device are delayed.

Proposal:

- The proxy advertises what it supports in `REGISTER_ROUTER_REQUEST` and in
  the first `GET_NEXT_COMMAND` after each start (and whenever it changes), as
  a `device` block in `messageParams`:

  ```json
  "device": {
    "protocolVersion": 2,
    "firmware": "igOS 1.5.x",
    "capabilities": ["TUNNEL_OPEN", "DEVICE_EVENT", "EVENT_HANDLER_ALERTS"]
  }
  ```

  Full schema: §19.1.

- The cloud stores this per device and **never sends a type the device did
  not advertise**. No capabilities field means protocol version 1 (today).
- The proxy answers an unknown type with `ERROR` `UNSUPPORTED_COMMAND`
  carrying the original `messageId`, and keeps polling without backoff
  (Appendix A).

---

## 11. Identity and secrets (interim vs. target)

### 11.1 Device identity toward the cloud

- **Target**: DevID mTLS per TPM-PKI §3.11; `deviceSecretUID` retired.
- **Interim**: keep serial-based registration, but refuse to register with
  the shared `000-000000M00000` fallback in release builds (`UNPROVISIONED`,
  reason `invalid_serial`).

### 11.2 Router API access for the GUI and the agent — local socket, no keys (decided direction)

Today the router HTTP API key is the same on every unit and is shipped in the
GUI package, the cloud agent package and the default configuration (security
findings G1, G11). Every gateway call also requires the cloud to supply it
(§0.6).

Why a key exists at all: the API server listens only on a Unix socket
(`/run/api.sock`, `vyos-http-api-server`), and nginx publishes it on port 443
(`proxy_pass http://unix:/run/api.sock`, `data/templates/https/nginx.default.j2`).
Flask calls `https://127.0.0.1:443` (`builds/FILES/env.conf`), so it arrives
through nginx like any network client, and the API cannot tell it from one.
Both Flask and the agent run as root, so a key held by them protects nothing
they could not already do. Replace keys for these internal callers with:

1. **A second, root-only API socket.** The API server also listens on
   `/run/api-local.sock`, owner root, mode `0600`. Only root processes can
   connect; the socket permission is the authorization. It is never exposed
   through nginx or a TCP port.
2. **No key on the local socket.** REST `auth_required`
   (`src/services/api/rest/routers.py`) and GraphQL `key_auth` (including
   token mode) skip the key check for requests on the local socket only.
   Requests through nginx are unchanged and still need a key.
3. **Flask and the agent connect to the local socket** instead of
   `https://127.0.0.1:443`: no key, no TLS handshake, no nginx hop.
4. **Acting user on every request.** Callers on the local socket send the
   acting user (e.g. `X-Igos-User: alice`, `X-Igos-Via: gui` or
   `cloud`), which the API trusts only on the local socket and uses for
   commit-log attribution (`igos-gui-user-accounting-design.md` §6.3, §10.4).
   The agent sends the cloud user (e.g. `jsmith@example.com`, via `cloud`).
5. **The cloud never holds a router key.** The agent sends gateway requests
   over the local socket (§19.9). PerleCLOUD sends none and stores none.
6. **Authorization by cloud role, not by key possession.** The cloud sends the
   user's role with each request; the agent enforces what each role may call
   and logs every call with the cloud user (§19.9).
7. **Operator keys stay.** Keys configured under `service https api keys`
   (any number) keep working for remote API clients through nginx. With the
   API's nginx locations localhost-only or disabled by default (security
   finding G1 fix), a factory-fresh unit has no API key at all.
8. **Remove the shipped key.** Drop it from `config.boot.default.igos`, the
   GUI `.env` and `cloud-env.conf`. An igOS migration
   (`src/migration-scripts/igos/`) removes the shipped default key entry from
   existing configurations; operator-added keys are kept.

Constraints:

- The local socket must stay root-only — never writable by `www-data`
  (nginx), and not group-accessible unless a dedicated group is created for a
  non-root GUI later (running Flask as root is itself a large exposure; when
  it is dropped, the socket gets a group that only the GUI is in).
- The API code is upstream VyOS: a small fork change in the two auth checks
  plus the second listener.

**Remote API access by operators.** Operators can still use the API from
their own tools:

```text
set service https api keys id ops key '<long random value>'
set service https api rest               # and/or graphql
set service https api remote allow-from <network>   # proposed; off by default
```

- **Off by default.** The API's nginx locations accept only loopback until
  remote access is explicitly enabled, ideally limited to source networks
  (command name to be decided).
- **Key required.** Through nginx, every API endpoint except `/info` needs a
  valid configured key (or a JWT in GraphQL token mode) — unchanged VyOS
  behavior. With no keys configured, remote callers get nothing.
- **Keys are full access.** VyOS API keys have no roles or scopes; use one key
  id per user or integration so changes can be told apart (`Configuration
  modified via HTTP API using key '<id>'`) and revoked individually.
- **Independent of the GUI and the cloud.** Those use the local socket and
  hold no keys, so operator keys can be added, changed or deleted without
  affecting either.

**Enabling remote access cannot reach the GUI or cloud paths:**

1. **The key-less socket is not reachable remotely.** nginx proxies only to
   `/run/api.sock`; `/run/api-local.sock` is never configured in nginx or on
   TCP, and is root-only.
2. **Identity headers are ignored from remote callers.** The API honours
   `X-Igos-User` / `X-Igos-Via` only on the local socket; nginx also clears
   them on the way in (`proxy_set_header X-Igos-User ""`, likewise
   `X-Igos-Via`) as defense in depth. A remote key holder cannot pose as a GUI
   or cloud user; their changes are attributed to their key id.
3. **The GUI is a separate application.** Flask routes need a Flask login; an
   API key is not accepted by Flask, and a Flask session cookie is not
   accepted by the API.
4. **The cloud agent is not reachable through it.** The agent's local status
   socket is published by nginx for loopback only (`allow 127.0.0.1; allow
   ::1; deny all` in `igos-cloud-proxy` `nginx/cloud.conf`); the agent's
   command channel is outbound only. The API's `/auth` route (`service=cloud`)
   needs a key and reaches only read-only agent operations (JWKS, session
   validation).
5. **What a key holder can do is the API itself.** Since keys are full access,
   a key holder can change configuration — including enabling or disabling
   services. That is the nature of an API key, not a bypass; protect keys
   accordingly and restrict `allow-from`.

Why: nothing to rotate, distribute or leak; no secret in any package or
backup; the socket gives a trustworthy caller, which also solves trusted actor
identity for accounting; and it removes no protection — the router already
fully trusts an authenticated cloud session (§0.6). Applies to both options
(§18.7).

### 11.2a Rollout order

vyos-1x can ship first; only removing the shipped key waits for the other
repositories.

| Step | Change | Safe alone? |
| --- | --- | --- |
| 1 | vyos-1x: local socket, key optional there (any key sent is ignored), identity headers honoured only there and cleared by nginx, commit-attribution plumbing | Yes — additive; `/run/api.sock` unchanged |
| 1 | vyos-1x: API nginx locations loopback-only by default, `remote allow-from` option | Yes — the GUI and agent call `127.0.0.1`; there are no remote API users today, so no migration is needed |
| 2 | flask-react-dev and igos-cloud-proxy switch to the local socket and stop sending keys (either order) | Yes, once step 1 is installed |
| 3 | igOS migration removing the shipped `myid` key; remove the key from vyos-build `config.boot.default.igos` and model defaults | Only after step 2 — earlier, the GUI and agent would lose API access |

Step 3 is enforced with package version constraints (`Breaks:` / `Depends:`
in `debian/control`) so the migration never runs with an old GUI or agent.

Follow-up (optional): vyos-1x has no GUI configuration today — only
`service_https.py` including `/etc/nginx/flask.conf` when the GUI package
installed it. A small vyos-1x-owned node (e.g. `service https gui`) could
later hold GUI enable/disable, idle timeout from `system login timeout`,
whether cloud GUI access is allowed, and the GUI's local group if Flask stops
running as root (`igos-gui-user-accounting-design.md` §5.4, §11). Not needed
for this change.

### 11.3 Alternatives considered

- **Internal keys generated at every boot** (one per internal caller, in
  `/run`, merged into the API key list under reserved ids). Works, but adds
  key files, reserved-id and duplicate-value rules, and start-up ordering —
  all to authenticate root processes that need no authentication. The local
  socket is simpler and also gives trusted actor identity.
- **A per-unit key shared with the cloud** over the command channel (new
  `API_CREDENTIAL` message), rotated at boot and on a timer. Not preferred:
  routers rarely reboot, so boot-only rotation can leave a key valid for
  months (it would also need timed rotation, a cloud-requested
  `ROTATE_API_KEY` and a grace period); after a reboot the cloud holds a stale
  key until the unit reconnects — worst on metered links that check in
  rarely; a cloud breach exposes every unit's current key; and a key is
  all-or-nothing, so roles still need enforcing.

---

## 12. Code layout / changes by repository

| Repo | Change |
| --- | --- |
| igos-cloud-proxy | supervisor (`proxy/supervisor.py`), egress classifier (`proxy/core/net/egress.py`) with `egress` deny check, metered scheduler (traffic discipline, `idle-timeout`-aware poll hold), truthful `CloudProxyState`, status model, capability exchange (§10.1), SIGHUP reload, systemd limits, Appendix A fixes, docs |
| vyos-1x | `service cloud` XML (`metered …`, `link-class`, `egress`), `service_cloud.py` template + change-only reload + dial-on-demand constraint check (both directions), op-mode `connect cloud now` and `show cloud status`, PIN single-store + upgrade cleanup |
| flask-react-dev | status widget (`state`, `link_class`, `next_check_in`, `standby`, `blocked`), enable/disable via config API, "check in now" |

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
9. **Local API key** — direction chosen (§11.2): root-only local API socket
   for the GUI and the agent, no keys; authorization by cloud role. Open: the
   per-role endpoint list (§19.9).
10. **Dial-on-demand constraint** — adopt it (preferred, §5.4) or support
    `connect-on-demand` / `always-on` metered links via a `cloud` bearer
    lease? Hard commit error vs warning for non-compliant WWANs?
11. **Metered-ness of non-WWAN links** — with the constraint, a link
    reclassified metered via `link-class` (e.g. Ethernet satellite modem) has
    no dial-on-demand to bring it up/down; the agent would duty-cycle traffic
    only. Acceptable, or restrict `metered` to WWAN?
12. **`egress` defaults** — `metered allow` (today's behavior) or `deny` out of
    the box? Should `BLOCKED` raise an alert (alerting design catalog)?
13. **Capability exchange** (§10.1) — fields and where they travel
    (registration, first poll, every poll); can PerleCLOUD store and honor
    them?

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

## 15. Device-originated information (alerts)

The protocol is pull-only: the cloud learns about the device only by sending it
commands. Apart from the WWAN AlertBus and syslog, igOS produces no user-facing
alerts of its own; the alert source is the event-handler pipeline in
`igos-alerting-design.md` (catalog alerts and custom events). This section
defines how those alerts reach PerleCLOUD.

**Baseline (§15.1): the cloud reads alert history through the existing gateway.
Push (§15.3) is an optional upgrade** for lower latency, and pull stays as its
backfill path.

### 15.1 Baseline — cloud reads history (no protocol change)

The cloud reads `show event-handler alerts --raw` through the VyOS gateway
(GraphQL, since the op-mode script is standardized) and keeps a cursor. This is
already the plan in `igos-alerting-design.md` §8. **Neither the command nor the
history exists yet**: today's `vyos-event-handler` only runs scripts on regex
matches and keeps no record, so the baseline depends on the alerting design
being implemented. The protocol needs no change; the gateway is already there.

Persistence of what the cloud reads (as designed; no code yet):

| Event | History (`/var/lib/vyos/alerts/history.jsonl`) | Delivery queues (`/var/lib/vyos/alerts/queue/`) |
| --- | --- | --- |
| Daemon restart | kept | kept |
| Reboot | kept — the file is in the image's overlay upper (`boot/<image>/rw`) on the persistence partition | kept |
| Image upgrade | **undecided** — today left behind with the old image; the new image starts empty (§15.5) | same as history |
| History full (`history size`, default 1000) | oldest records dropped | n/a |

Today the image installer carries only `/config`, SSH host keys and
known_hosts, and optionally `/var/lib/tee` into a new image (`add_image()` in
`src/op_mode/image_installer.py`); nothing under `/var/lib/vyos/` is migrated.
Booting back into the old image shows the old history again.

The journald copy (`SYSLOG_IDENTIFIER=igos-alert`) is not the cloud's source;
history is.

### 15.2 What the cursor relies on (alerting design §3, §7d)

- **`sequence` resumes across reboots and `clear`**, continuing from the last
  recorded alert, so numbers are never reused while history exists.
- **`history_id`** changes only when an image starts with no history (an image
  upgrade, unless history is carried over — §15.5). A changed `history_id`
  tells the cloud to resynchronize from the start.
- **`since <sequence>` and `first_sequence`** in `show event-handler alerts
  --raw` let the cloud fetch only new records and detect records lost to
  history overflow.
- **Delivery results** (alerting design §7d) appear in the same history as
  `delivery` records with their own `sequence`, so the cloud and GUI see late
  SMS/email outcomes for older alerts with the same `since` read.

Cloud algorithm: remember `(history_id, last_sequence)`; request
`since last_sequence`; on a different `history_id`, read everything; on
`first_sequence > last_sequence + 1`, flag a gap.

Limits of the baseline: alert latency equals the cloud's read interval, and
every read costs a gateway round trip even when there is nothing new.

### 15.3 Optional upgrade — push

A cloud delivery channel in the alerting pipeline, alongside email, SMS and
script:

```text
vyos-event-handler ── cloud queue ── cloud worker ──► igos-cloud-proxy local API
                      (§7a of the                       POST /perlecloud/events
                       alerting design)                     │
                                                            ▼
                                                 DEVICE_EVENT ──► PerleCLOUD
                                                 ◄── ack (record ids)
```

- **vyos-1x (event-handler):** a cloud worker drains a cloud queue with the
  same retry, backoff, 24 h give-up and severity-ordered overflow as the other
  channels. An item is removed only when the proxy reports that the cloud
  acknowledged it.
- **igos-cloud-proxy:** a `POST /perlecloud/events` route on the existing unix
  socket. It sends `DEVICE_EVENT` on `routers/command` and returns success only
  after the cloud's acknowledgement; while not connected it returns `503`, and
  the worker retries. The proxy stores nothing — durability is the alert queue.
  It does not follow journald itself, keeping one reader and one pipeline.
- **Protocol:** `DEVICE_EVENT` (proxy → cloud, `requiresResponse: true`):

  ```json
  {
    "eventType": "alert",
    "historyId": "…",
    "records": [ { "id": "…", "sequence": 812, "severity": "critical", "…": "…" } ]
  }
  ```

  The cloud replies with the accepted record `id`s. `records` is a list so a
  backlog after an outage drains in a few requests. `eventType` keeps the
  message general for later device-originated notices (§15.4).
- **Delivery semantics:** at-least-once. The cloud deduplicates on `id`; pull
  (§15.1) remains the reconciliation path, so a lost or expired push item is
  recovered at the next read.
- **Unsupported cloud:** if PerleCLOUD answers `DEVICE_EVENT` with an error for
  an unknown message type, the proxy returns `501` to the worker, which drops
  cloud items and stops pushing until the proxy restarts. Pull still covers
  everything.
- **Noise control:** the alerting design's hold-down and edge detection apply
  unchanged; add `rate-limit cloud` alongside SMS and email.

### 15.4 Later uses of `DEVICE_EVENT`

- `config-changed` from a commit post-hook, so the cloud re-reads configuration
  after a CLI or GUI commit instead of polling for it.
- Metric/threshold alerts once the alerting design's poller exists (its §13.4);
  they use the same record and channel.

### 15.5 Open decisions

1. **Which alerts are pushed:** per-rule `notify cloud` (consistent with email
   and SMS), every recorded alert while `service cloud` is enabled, or a
   minimum severity.
2. **Carry alerts across image upgrade? (undecided)** If yes, it becomes one
   more step in the image installer (`add_image()`), copying
   `/var/lib/vyos/alerts/` into the new image the same way `/var/lib/tee` is
   migrated; the cloud cursor then continues without a resync. Tracked in
   `igos-alerting-design.md` §12.
3. **Backend:** can PerleCLOUD accept `DEVICE_EVENT` and acknowledge by `id`?
   Until then, the baseline is the only path.

### 15.6 Changes by repository

| Repo | Baseline | Push (optional) |
| --- | --- | --- |
| vyos-1x | `sequence` resume, `history_id`, `since` (alerting design §7d) | cloud worker and queue, `notify cloud` / selection rule, `rate-limit cloud` |
| igos-cloud-proxy | none | `POST /perlecloud/events`, `DEVICE_EVENT` message, ack handling, `501` for unsupported cloud |
| flask-react-dev | none | `cloud` in the alert channel picker (if per-rule) |
| PerleCLOUD | cursor read of `show event-handler alerts` | accept and acknowledge `DEVICE_EVENT`, deduplicate on `id` |

---

## 16. Remote access to the router and connected devices (proposed)

Status: proposed solution; not yet agreed with the PerleCLOUD team.

The product is a router. Cloud users need browser-based access to devices
connected to it (SSH and HTTPS first), and ideally to the router's own GUI,
CLI, serial ports and GPS stream, through one mechanism.

### 16.1 Requirements (decided)

1. The PerleCLOUD relay and browser clients are a **requirement**; the cloud
   team builds them.
2. **Browser only.** No support for a cloud user's own SSH client.
3. **SSH and HTTPS are required.** VNC is desirable (especially), RDP and
   Telnet nice to have; each depends on the cloud implementing a browser
   client.
4. Works over metered links with bounded, visible data use (§16.7).

### 16.2 What exists today

- **SSH: not possible.** The only remote-access path is the GUI tunnel
  (`PROXY_CONNECT`), which carries request/response HTTP wrapped in JSON, one
  exchange at a time. There is no raw TCP stream.
- **HTTP to LAN devices: works by accident, unsafely.** `proxyRequestUri` is
  forwarded as supplied and certificates are not checked
  (`VERIFY_VYOS_SSL = False`), so plain HTTP(S) to a LAN address partly works,
  but:
  - no WebSockets, one request per exchange;
  - the router's **administrator** session cookie (`IGOS_CLOUD_SESSION`) is
    added to every response regardless of target, and whatever headers the
    cloud sends (including cookies) are forwarded to the target — a LAN device
    can receive a token that Flask accepts from any client while the session
    is open;
  - no opt-in, no allow-list, no audit;
  - the cloud can also make the router fetch from the **internet** (loop-out)
    over the active WAN, cellular included (§0.6).
- See §0.6 for user levels and the web CLI.

### 16.3 Proposal: device-initiated stream tunnel

```text
PerleCLOUD ── TUNNEL_OPEN ──► igos-cloud-proxy (via the command channel)
                                1. check local policy (target allowed?)
                                2. TCP connect to the target
                                3. open an outbound wss to relayUri
                                4. copy bytes both ways until closed

browser ◄──► PerleCLOUD relay ◄══ wss (router-initiated) ══► proxy ◄──► target
             (xterm.js + SSH client,
              HTTPS reverse proxy,
              noVNC, …)
```

- **The router only dials out**, as today, so it works behind NAT and carrier
  NAT on cellular.
- **Protocol-agnostic on the router.** The proxy checks the target and copies
  bytes; it does not understand SSH, HTTPS or VNC. Each protocol is a
  cloud-side browser client:
  - **SSH** — browser terminal (xterm.js) with a cloud-side SSH client. The
    user logs in with the target's own credentials.
  - **HTTPS** — cloud reverse proxy on a **per-session subdomain**, so each
    device's cookies stay separate and absolute links work. The cloud is the
    HTTPS client to the target.
  - **VNC** (later) — noVNC, which already speaks RFB over WebSocket.
  - **RDP** (later) — e.g. Apache Guacamole in the cloud.
- **One mechanism for every target:**

  | Target | Example | Benefit |
  | --- | --- | --- |
  | LAN device | `192.168.1.20:22`, `:443`, `:5900` | SSH, web UI or VNC of equipment behind the router |
  | Router serial port | Perle serial service on the port (SSH/Telnet/TCP server, `interface-definitions/include/serial/service/`) | Out-of-band console of equipment wired to the router (switch or firewall console, PLC, meter), even when that equipment's network is down. Reuses the cloud SSH terminal; no new cloud client. |
  | Router GPS | gpsd `127.0.0.1:2947` or the `service gps stream` port (`igos-wwan-gps-design.md` §6b) | On-demand live position view. Continuous fleet tracking is better as device-sent position reports (§15 `DEVICE_EVENT` or `service gps stream remote`), not a permanently open tunnel. |
  | Router SSH | `127.0.0.1:22` (phase 2) | Cloud CLI with real device accounts, so CLI levels (admin, operator with command policy) apply as locally |
  | Router GUI | `127.0.0.1:443` (phase 2) | Replaces `PROXY_CONNECT` (§16.8) |

### 16.4 Protocol

Summary below; full schemas, refusal reasons, close handling and the relay
WebSocket protocol are in §19.2–§19.6.

New command `TUNNEL_OPEN` (cloud → proxy, `requiresResponse: true`):

```json
{
  "tunnelId": "t-8f2c…",
  "target": {"host": "192.168.1.20", "port": 22},
  "relayUri": "wss://relay.perleapps.com/tunnel",
  "relayToken": "<one-time token>",
  "idleTimeout": 600,
  "maxDuration": 3600,
  "user": {"id": "…", "name": "…", "role": "administrator"}
}
```

- The proxy replies `TUNNEL_OPEN_RESPONSE` with `accepted` or a reason
  (`not_enabled`, `target_not_allowed`, `route_not_allowed`,
  `name_not_resolved`, `role_not_allowed`, `limit_reached`, `connect_failed`,
  `metered_not_allowed`, `egress_blocked`).
- The proxy opens `relayUri` presenting `relayToken` (single use, short-lived);
  the relay pairs that connection with the user's browser session.
- Either side closes the tunnel; the proxy also closes it on idle timeout, max
  duration, byte cap, `service cloud` disable, or a local `reset` command.
- Optional later: multiplex all connections of one user session over one
  WebSocket (a `streamId` per TCP connection). Start with one WebSocket per
  connection; add multiplexing when HTTPS browsing needs it (§16.7).

**Target names are resolved by the router, not the cloud.** A name like
`www.myequipmentonthelan.com` that exists only on the customer's network is
unknown to public DNS, so the cloud sends it unchanged in `target.host`:

1. The proxy resolves it with the router's resolver.
2. It checks the **resolved address** against the allow-list and the route
   check (§16.5), so a name cannot be used to reach a disallowed address, and
   a name that resolves publicly is refused unless public addresses are
   allowed.
3. It connects to that address. For HTTPS, the cloud still uses the typed
   name for the `Host` header and TLS server name, so name-based sites and
   name-matched certificates behave normally. LAN devices usually have
   self-signed certificates; the cloud needs a warn-and-continue policy.

Name sources the router's own processes can use:

| Source | Configured with | Resolves for the proxy? |
| --- | --- | --- |
| `/etc/hosts` | `system static-host-mapping host-name <name> inet <addr>` | Yes |
| DHCP lease names | `service dhcp-server … hostfile-update` (lease hostnames into `/etc/hosts`) | Yes, for clients that send a hostname |
| Upstream DNS | `system name-server <addr>` (`/etc/resolv.conf`) | Yes, for names those servers know (e.g. a customer's internal DNS) |
| Router DNS service | `service dns forwarding` (PowerDNS recursor): `authoritative-domain … records`, per-domain forwarding to a LAN DNS server, `/etc/hosts` export | Only if the router's own lookups are pointed at it; router-originated lookups use `system name-server` (from config reading; to verify) |
| mDNS `.local` | `service mdns-repeater` | Probably not; the repeater relays between interfaces only |

Convenience: the cloud can offer a pick list of known local devices (DHCP
leases with hostnames, static host mappings) read through the gateway instead
of free typing.

### 16.5 Router-side controls and configuration

The cloud is fully trusted today (§0.6); remote access adds explicit local
policy:

```text
service cloud remote-access
    enable                                     (default: off)
    allow <network|address> port <list>        (LAN targets, e.g. 192.168.1.0/24 port 22,443)
    allow-interface <ifname>                   (multi; route to the target must leave here, e.g. br0 eth1)
    allow-local serial|gps|ssh|https           (router-local services)
    max-sessions <n>
    idle-timeout <s>
    max-duration <s>
    max-bytes <n>                              (per session; optional)
    metered allow|deny                         (on a metered egress, §4)
    role administrator|operator+               (minimum cloud role)
```

- Denied unless enabled and the target matches an `allow` entry.
- **No loop-out to the internet.** In addition to the address check, the
  route to the resolved target (`ip route get`, no packet sent) must leave
  through an `allow-interface` (LAN side) — or be a router-local service.
  Even an over-broad `allow` entry or an unexpected DNS answer cannot send
  tunnel traffic out a WAN or cellular interface. The same check applies to
  the GUI tunnel's `proxyRequestUri` until `PROXY_CONNECT` is retired. Router
  connectivity tests from the cloud use the gateway (`/traceroute`, `show`),
  not tunnels.
- The firewall is only a coarse backstop here: tunnel connections are
  router-originated, and the `output` chain cannot tell them from the router's
  own DNS, NTP, updates or cloud connection.
- `metered allow|deny` here applies to tunnels; `service cloud egress` (§9)
  applies to all cloud traffic and takes precedence.
- The cloud must send the user's role; below `role` is refused. This fixes the
  hard-coded administrator for this path.
- No router session cookie is ever added to tunnel traffic.
- Op-mode: `show cloud remote-access sessions` (user, target, start, bytes,
  idle), `reset cloud remote-access session <id>`.
- Every open and close is logged with cloud user, target, duration and bytes,
  and offered as a `cloud-remote-access` alert type in
  `igos-alerting-design.md`.
- Router-originated connections to LAN targets pass the firewall `output`
  chain; the target sees the router's LAN address as the client.

### 16.6 Effect on `igos-cloud-proxy`

Not a replacement — it adds one capability and later retires one part:

| Part (today) | Size | With the tunnel |
| --- | --- | --- |
| Registration, token, TPM (`cloud/`, `core/`) | ~630 lines | Unchanged |
| Command channel (`protocol/`, `proxy.py`) | ~900 lines | Unchanged; carries `TUNNEL_OPEN` |
| Built-in REST routes and VyOS gateway (`vyos/`, `rest_command.py`) | ~650 lines | Unchanged |
| Status API, JWT keys, session checks (`state/`, `session/auth/`) | ~460 lines | Unchanged; gains tunnel session list and close route |
| GUI tunnel (`http_proxy.py`, `proxy_connect.py`) | ~480 lines | Kept in phase 1; removable in phase 2 |

### 16.7 Data overhead (estimates, not measured)

| Item | Approximate cost |
| --- | --- |
| No tunnel open | **0** — tunnels exist only on demand |
| Opening a tunnel | ~5–8 KB (`TUNNEL_OPEN` ~1 KB, TLS 1.3 handshake to the relay ~4–6 KB, WebSocket upgrade ~0.5–1 KB); ~1 KB with TLS session resumption |
| Per packet | ~25–35 bytes (WebSocket 2–14 B + TLS record ~22 B) |
| Bulk transfer | < 3 % |
| Interactive SSH | ~+30 bytes per keystroke packet; a few hundred bytes per keystroke including echo, about the same as direct SSH |
| Open but idle | ~10–15 KB/hour with a 60 s keepalive (needed for carrier NAT); capped by `idle-timeout` |

For comparison, today's GUI tunnel base64-encodes bodies (+33 %), sends each
response as a separate HTTPS POST with headers, and issues an idle poll every
≤ 30 s while a session is open (~120 KB/hour doing nothing). Moving the router
GUI onto the tunnel reduces data use.

**Content dominates:** SSH is small; a LAN device's web UI first load can be
hundreds of KB to several MB, plus a ~3–5 KB TLS handshake per browser
connection; VNC is the heaviest (hundreds of KB to MB per minute). The inner
traffic is already encrypted, so tunnel compression does not help.

Metered controls: on demand only; idle timeout and max duration; per-session
byte counter and optional `max-bytes`; `metered allow|deny`; multiplexing
connections over one WebSocket and TLS resumption to save handshakes; cloud
defaults of browser caching and low VNC quality on metered links.

### 16.8 Phasing

1. **Phase 1a — SSH to LAN devices and router serial ports.** Single
   connection, no URL or cookie handling; the fastest end-to-end result.
2. **Phase 1b — HTTPS to LAN devices.** Per-session subdomains; add
   multiplexing if handshake and keepalive cost warrants.
3. **Phase 2 — router GUI and SSH over the tunnel.** Fixes the web CLI
   (WebSocket), binary uploads, the hard-coded administrator role and the
   GUI tunnel's data cost. Keep `PROXY_CONNECT` for older cloud/firmware
   combinations during the transition, then retire it.
4. **Later — VNC, RDP, Telnet, GPS live view**, as the cloud adds clients. No
   router change beyond allow-list entries.

Independent of the polling / metered-link redesign (§2–§7).

### 16.9 Changes by repository

| Repo | Work | Relative size |
| --- | --- | --- |
| igos-cloud-proxy | `TUNNEL_OPEN` / response, policy check, TCP connect, outbound WebSocket (`aiohttp`, already a dependency) with byte pump, limits and byte counter, audit log, status-API session list and close route; later multiplexing | Small–medium: ~400–700 lines (+200–300 for multiplexing) |
| vyos-1x | `service cloud remote-access` XML, `verify()`, render into the proxy config; op-mode `show cloud remote-access sessions` / `reset …`; `cloud-remote-access` alert type | Small |
| flask-react-dev | Phase 1: none (optional sessions view). Phase 2: GUI works when served from a cloud subdomain | Small |
| PerleCLOUD | Relay (WebSocket endpoint, pairing, one-time tokens, scaling and node routing), browser SSH (xterm.js + server-side SSH), HTTPS reverse proxy with per-session subdomains (wildcard DNS/cert, cookie and redirect handling), UI, audit; later noVNC / Guacamole | **Large — most of the effort** |

### 16.10 Alternatives considered

- **Extend the GUI tunnel:** HTTP only; no SSH, no WebSockets. Rejected.
- **Full VPN to the cloud (WireGuard/OpenVPN):** exposes the whole LAN, and
  customer subnets overlap (most use `192.168.1.0/24`), so the cloud would need
  per-router NAT or namespaces. Too heavy for this purpose. Rejected.
- **Customer SSH client to a cloud endpoint:** out of scope (browser only).

### 16.11 Open decisions

1. Allow-list form: networks + ports, named targets, or both.
2. Default `idle-timeout`, `max-duration`, `max-sessions`.
3. Minimum cloud role for remote access, and whether operator+ may open
   tunnels to some targets.
4. On metered egress: deny by default, or allow with `max-bytes`?
5. One WebSocket per connection vs multiplexed from the start.
6. Relay certificate trust on the router: public CA, or pinned to PerleCLOUD
   (ties to DevID mTLS, §11).
7. Phase 2 GUI single sign-on: keep the `IGOS_CLOUD_SESSION` JWT (with the
   cloud user's real role) or a different mechanism.
8. Should the router's own lookups also query its DNS forwarding service
   (local zones, forwarded domains) — automatically when `remote-access` is
   enabled?
9. Allow-list by domain as well as network (e.g. `allow-domain
   plant.example.com`)?
10. `allow-interface` default: required, or derived from interfaces that are
    not WAN / not the cloud egress?

---

## 17. Hand-off: cloud team and GUI team

What each team needs from this document, and what is still open. Rows marked
**O1** are needed for Option 1 (§18); all rows apply to Option 2.

### 17.1 Prerequisites for both

1. **Capability exchange (§10.1, §19.1)** before any new message type is sent:
   `TUNNEL_OPEN` (§16), `DEVICE_EVENT` (§15.3). Older firmware drops unknown
   commands without replying. Recommended in Option 1 too (§18.5).
2. **Role mapping**: PerleCLOUD user levels → `administrator` / `operator+` /
   `operator`, and what each may do (GUI, gateway, tunnels). Today the device
   always grants administrator (§0.6). Option 1 passes it in `PROXY_CONNECT`
   (§18.3).
3. **No API key in the cloud** (§11.2, §19.9): the agent reaches the API over
   the root-only local socket; the cloud stops sending `body.key` once the
   device advertises `GATEWAY_LOCAL_AUTH`, and sends the user's role instead.

### 17.2 Cloud (PerleCLOUD) team

| Item | Where | Status |
| --- | --- | --- |
| **O1** Capability report and `UNSUPPORTED_COMMAND` | §19.1 | Drafted. To confirm: the cloud ignores unknown `messageParams` fields |
| **O1** Optional `role` in `PROXY_CONNECT` | §18.3 | Drafted |
| **O1** Gateway: stop sending `body.key`, send `user` with role | §11.2, §19.9 | Drafted; per-role endpoint list to agree |
| **O1** `nextPollDelay` for metered devices | §18.2 | Drafted; choose the delay |
| **O1** Status display | §8, §18.1 | States and reasons, including `blocked` (and `standby` in Option 2) |
| **O1** Alert reading | §15.1–15.2, §19.8 f | Drafted; exact GraphQL query and `--raw` output schema to be fixed with the alerting implementation |
| `TUNNEL_OPEN` / response / close | §19.2–§19.4 | Drafted |
| Relay wire protocol `igos-tunnel.v1` | §19.5 | Drafted (token header, binary frames, flow control, keepalive, close codes) |
| Multiplexing `igos-tunnel.v2` | §19.6 | Sketched; deferred |
| `DEVICE_EVENT` and ack | §19.7 | Drafted |
| Sequence diagrams | §19.8 | Drafted |
| Relay security | §16.3–16.5, §19.5 | Single-use short-lived tokens; per-session subdomain; cookie isolation; self-signed certificate policy for LAN devices; audit; certificate trust/pinning (§16.11 item 6) |
| Browser clients | §16.3, §16.8 | SSH (xterm.js + server-side SSH) first, then HTTPS reverse proxy; later noVNC, Guacamole |
| Target names | §16.4 | Send names unchanged; use the typed name for `Host`/SNI; optional device pick list from the gateway |
| Store-and-forward and short polls | §10 | Asks 1–5 (metered duty cycle) |
| Transition | §16.8, §19.8 g | Protocol-1 devices get `PROXY_CONNECT` and gateway only; retire `PROXY_CONNECT` after phase 2 |

### 17.3 GUI (flask-react-dev) team

| Item | Where | Work |
| --- | --- | --- |
| **O1** Cloud status widget | §8, §18.1 | Show `state`, `reason`, egress, `blocked` (Option 2 adds `next_check_in`, `standby`) instead of connected/disconnected |
| **O1** Connect/disconnect | §9 | Configure `service cloud enable` through the VyOS API instead of `systemctl` (Option 2 adds "check in now" → `connect cloud now`) |
| **O1** Cloud egress policy | §9, §18.2 | Settings for `service cloud egress` (`metered`, `deny-interface`) |
| Remote-access settings and sessions | §16.5, §18.3 | `service cloud remote-access` page; session list with end-session action (Option 1: the HTTP-only subset, no session list) |
| **O1** Alarm page | §15, alerting design §8 | Read `show event-handler alerts` with `since`; keep only read/dismissed state in Flask; remove `/callback`; show delivery results |
| **O1** Web CLI URLs | §0.6 | Replace hard-coded `ws://` / `http://` and missing port in `constants/api.ts`, `XTerm.tsx`, `ShellCLI.tsx` with relative or same-scheme URLs |
| **O1** Web CLI authorization | §0.6 | Verify nginx `/cli/shell/ws` → ttyd bypassing Flask's administrator check, and which account the ttyd shell runs as |
| **O1** Router API access | §11.2, §18.7 | Call the API on `/run/api-local.sock` instead of `https://127.0.0.1:443`; drop `VYOS_APIKEY` from `.env`; send the acting user (`X-Igos-User`, `X-Igos-Via: gui`) |
| Phase 2 readiness | §16.8 | GUI served from a cloud subdomain: relative URLs, cookie path/SameSite, Socket.IO over the tunnel, real cloud role in the session token |
| Phase 2 readiness | §16.8 | GUI served from a cloud subdomain: relative URLs, cookie path/SameSite, Socket.IO over the tunnel, real cloud role in the session token |

---

## 18. Option 1 — Patch what exists

Keep today's protocol, long-poll and GUI tunnel. Fix what is broken, make
status truthful, close the security gaps, and add only the smallest controls.
No new transport, no SSH, no metered duty cycle. PerleCLOUD changes are small
and mostly optional.

### 18.1 Reliability and truthful status (igos-cloud-proxy)

- **Truthful `connected`.** Set only after a successful poll; cleared on poll
  failure, `AUTH_FAILED`, unregister and shutdown (§1 item 1).
- **Status gains a state and a reason** — a subset of §7: `UNPROVISIONED`,
  `REG_REJECTED`, `CONNECTING`, `CONNECTED`, `BACKOFF`, `BLOCKED`,
  `AUTH_FAILED`, `UNREGISTERED` — plus `last_success`, `last_error`, egress
  interface and link class. The existing `cloud_connection_*` fields stay;
  the `cloud-services/status` route reports the real state instead of a fixed
  `"connected"`.
- **Never stop silently.** Recoverable conditions (no PIN, rejected PIN,
  `AUTH_FAILED`, `UNREGISTER_ROUTER`) keep the process alive in a waiting
  state that status explains, instead of exiting 0. A minimal loop around the
  existing connect/poll code; it wakes on SIGHUP when a new PIN is written
  (op-mode `register` sends SIGHUP instead of stop/start).
- **Bounded restarts and backoff.** systemd `RestartSec=10`,
  `StartLimitIntervalSec=300`, `StartLimitBurst=5`. Poll and registration
  backoff 2 s → 300 s with full jitter.
- **TPM seal failure** after registration: keep the token in memory, report
  it, retry sealing later — never re-register in a loop.
- **All Appendix A fixes.**

### 18.2 Cellular control without the duty cycle

- **Block it:** `service cloud egress metered deny` / `deny-interface <if>`
  (§9) — route check before every connection, `BLOCKED` state, automatic
  return when the route moves back. The firewall recipe in §9 works today and
  remains a backstop.
- **Or slow it down from the cloud, using an existing field.** The cloud sees
  the link class (status route or capability report, §18.5) and answers
  metered devices with `EMPTY_COMMAND` `nextPollDelay` (e.g. 600 s). No device
  change beyond reporting the link class. Command latency becomes up to the
  delay.

  Rough data estimate (not measured): today's continuous 30 s long-poll is
  ~120 polls/hour at ~1 KB each with keep-alive, about **120 KB/hour
  (~85 MB/month)**. With a 600 s delay, ~6 polls/hour; carrier NAT will likely
  drop the idle connection, so each poll pays a TLS handshake (~5 KB): about
  **35 KB/hour (~25 MB/month)**.
- Option 1 does not integrate with WWAN dial-on-demand; an always-on cellular
  link stays busy with polls (§2–§5 are Option 2).

### 18.3 GUI tunnel hardening — and optional HTTP-only LAN access

- **Default target: the local GUI only.** `proxyRequestUri` must be the
  router's own GUI (`https://127.0.0.1:<service https port>`, the `routerURI`
  advertised in `PROXY_CONNECT_RESPONSE`). Anything else gets a `403`
  response for that request. This closes LAN and internet loop-out (§0.6).
- **Optional "just get by" LAN web access.** A subset of §16.5:

  ```text
  service cloud remote-access
      enable
      allow <network|address> port <list>
      allow-interface <ifname>
  ```

  The same allow-list and route check as §16.5, applied to
  `proxyRequestUri`. Limits: HTTP(S) request/response only, one request per
  exchange, no WebSockets; pages that use absolute links back to their own
  address or need WebSockets break; all LAN devices share the cloud origin in
  the browser, so their cookies can collide (unless the cloud uses a
  per-target path or subdomain).
- **No router session leaks.** `IGOS_CLOUD_SESSION` is added only to responses
  from the router GUI, and stripped from the `Cookie` header forwarded to any
  other target.
- **Cloud user role.** New optional `PROXY_CONNECT` parameter
  `role` (`administrator` | `operator+` | `operator`), placed in the session
  JWT. Absent → `administrator` (today's behavior, for compatibility).
- **Audit.** Log each `PROXY_CONNECT` (proxy id, role, user if supplied) and
  each request to a non-GUI target.
- **Fixes:** poll retry without delay, binary request bodies (Appendix A).

### 18.4 Alerts

Cloud reads alert history through the gateway (§15.1–15.2), once the
alerting design is implemented. No push.

### 18.5 Capability reporting and unknown commands (recommended)

- Answer unknown command types with `ERROR` `UNSUPPORTED_COMMAND` instead of
  dropping them and backing off (Appendix A).
- Send the §19.1 `device` block (version, capabilities, link class) on the
  first poll after start. It feeds the `nextPollDelay` decision (§18.2) and
  lets the cloud adopt Option 2 features device by device later.

### 18.6 Work by repository

| Repo | Work | Size |
| --- | --- | --- |
| igos-cloud-proxy | §18.1, `egress` check (§18.2), §18.3, §18.5, §18.7, Appendix A | Small–medium |
| vyos-1x | `service cloud egress` and optional `remote-access` subset (XML, `verify()`, render); `register` → SIGHUP; `show cloud status` state/reason; restart only on change; root-only local API socket with key-less auth and trusted acting user, migration removing the shipped key (§18.7) | Small |
| flask-react-dev | Status widget states and reasons; connect/disconnect through config; web CLI URL fix (local use); call the API on the local socket, drop the key from `.env` | Small |
| PerleCLOUD | Show device state/reason; optional `role` in `PROXY_CONNECT`; set `nextPollDelay` for metered devices; ignore the `device` block if unused; handle `UNSUPPORTED_COMMAND`; read alert history; stop sending `body.key`, send the user's role on gateway calls (§18.7) | Small |

**What Option 1 does not give:** SSH (LAN devices, router, serial ports); the
web CLI from the cloud; VNC, RDP, GPS live view; WebSockets through the cloud;
low idle data on cellular beyond blocking or slower polling; alert push;
store-and-forward for sleeping devices.

**Carries over to Option 2 unchanged:** §18.1 (the supervisor grows into §7),
`egress`, status state/reason, role passing, capability reporting,
unknown-command handling, allow-list and route check, local API socket and
gateway authorization (§18.7), Appendix A fixes.

### 18.7 Router API access (both options)

Implement §11.2 in Option 1 as well — it fixes security findings G1 and G11
and is small:

- vyos-1x: API server also listens on root-only `/run/api-local.sock`; REST and
  GraphQL auth skip the key there and accept the acting user
  (`X-Igos-User`, `X-Igos-Via`) only there; igOS migration removing the
  shipped key entry; drop the key from `config.boot.default.igos`.
- igos-cloud-proxy: call the API (built-in routes and gateway) over the local
  socket, no key; drop `VYOS_API_KEY` from `cloud-env.conf`; send the cloud
  user as the acting user; enforce the per-role endpoint list and log each call
  (§19.9). In Option 1 the role arrives as an optional `user` object
  (`id`, `name`, `role`) in the `EXECUTE_REST_COMMAND` parameters; absent →
  `administrator` (today's effective behavior) until the cloud sends it.
- flask-react-dev: call the API over the local socket instead of
  `https://127.0.0.1:443`; drop `VYOS_APIKEY` from `.env`; send the acting
  GUI user.
- PerleCLOUD: stop sending `body.key`; send the user's role.

---

## 19. Option 2 — Wire protocol details

Drafts for the PerleCLOUD team. Field names are proposals until agreed.
Envelope rules from the protocol doc apply (`messageParams` is a JSON string;
examples show the decoded object).

### 19.1 Capability report

Carried as `messageParams.device` in `REGISTER_ROUTER_REQUEST` and in the
first `GET_NEXT_COMMAND` after each proxy start, and again whenever it changes
(firmware upgrade, `remote-access` or `egress` change, link class change).

```json
"device": {
  "protocolVersion": 2,
  "firmware": "igOS 1.5.2",
  "proxyVersion": "2.0.0",
  "capabilities": [
    "EXECUTE_REST_COMMAND", "PROXY_CONNECT", "PROXY_CONNECT_ROLE",
    "TUNNEL_OPEN", "TUNNEL_CLOSE", "DEVICE_EVENT", "EVENT_HANDLER_ALERTS",
    "GATEWAY_LOCAL_AUTH"
  ],
  "tunnel": {
    "subprotocols": ["igos-tunnel.v1"],
    "maxSessions": 4,
    "remoteAccess": "enabled",
    "localServices": ["ssh", "https", "gps", "serial:ttyS1", "serial:ttyS2"]
  },
  "link": {"egress": "wwan0", "class": "metered", "state": "CONNECTED"}
}
```

Rules:

- No `device` block → protocol version 1 (today's firmware).
- The cloud sends only advertised message types and hides UI for features the
  device lacks or has disabled (`remoteAccess: "disabled"`).
- **To confirm with PerleCLOUD:** the cloud must ignore unknown
  `messageParams` fields in `GET_NEXT_COMMAND` and `REGISTER_ROUTER_REQUEST`.
  If it rejects them, use a separate `DEVICE_INFO` message only after the
  cloud has advertised support.

Unknown command reply (proxy → cloud):

```json
{"messageType": "ERROR", "messageId": 57, "requiresResponse": false,
 "messageParams": {"errorCode": "UNSUPPORTED_COMMAND",
                   "errorMessage": "TUNNEL_OPEN not supported"}}
```

### 19.2 `TUNNEL_OPEN` (cloud → proxy, `requiresResponse: true`)

```json
{
  "tunnelId": "t-8f2c41",
  "target": {"host": "www.myequipmentonthelan.com", "port": 443},
  "relayUri": "wss://relay.perleapps.com/tunnel",
  "relayToken": "<opaque, single use>",
  "subprotocol": "igos-tunnel.v1",
  "idleTimeout": 600,
  "maxDuration": 3600,
  "maxBytes": 52428800,
  "keepalive": 60,
  "user": {"id": "u-1234", "name": "jsmith@example.com", "role": "administrator"},
  "client": {"address": "203.0.113.7"}
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `tunnelId` | yes | 1–128 of `[A-Za-z0-9_-]`; unique per device |
| `target.host` + `target.port` | one of | Name or address; names resolved by the router (§16.4) |
| `target.service` | one of | Router-local service from `localServices`: `ssh`, `https`, `gps`, `serial:<tty>`; the router maps it to its local port |
| `relayUri`, `relayToken` | yes | §19.5 |
| `subprotocol` | no | Default `igos-tunnel.v1`; must be in `tunnel.subprotocols` |
| `idleTimeout`, `maxDuration`, `maxBytes` | no | The lower of the cloud's and the router's configured limit applies |
| `keepalive` | no | Seconds between WebSocket pings; default 60 |
| `user` | yes | `role` checked against `remote-access role`; `id`/`name` for audit |
| `client.address` | no | End user's address, for audit only |

### 19.3 `TUNNEL_OPEN_RESPONSE` (proxy → cloud)

Sent after the target connection **and** the relay WebSocket are both
established, or immediately on refusal. Timeouts: target connect 10 s, relay
connect 15 s.

```json
{
  "tunnelId": "t-8f2c41",
  "status": "accepted",
  "reason": null,
  "detail": null,
  "resolved": {"address": "192.168.1.20", "interface": "br0"},
  "limits": {"idleTimeout": 600, "maxDuration": 3600, "maxBytes": 52428800}
}
```

`status` is `accepted` or `refused`. Refusal `reason`:

| Reason | Meaning |
| --- | --- |
| `not_enabled` | `remote-access` not enabled |
| `target_not_allowed` | Resolved address/port not in the allow-list |
| `route_not_allowed` | Route to the target does not leave through an `allow-interface` |
| `name_not_resolved` | Router could not resolve `target.host` |
| `service_unknown` | `target.service` not available |
| `role_not_allowed` | `user.role` below `remote-access role` |
| `limit_reached` | `max-sessions` reached |
| `metered_not_allowed` | Cloud egress is metered and tunnels are denied on metered |
| `egress_blocked` | Cloud egress denied by `service cloud egress` |
| `connect_failed` | Target refused or timed out (`detail` has the error) |
| `relay_failed` | Relay unreachable, TLS failure, or relay rejected the token (`detail`) |
| `subprotocol_unsupported` | Requested subprotocol not supported |

### 19.4 `TUNNEL_CLOSE` and `TUNNEL_CLOSED`

`TUNNEL_CLOSE` (cloud → proxy, `requiresResponse: false`) — close a tunnel
the relay has lost track of, or end a session from the cloud UI:

```json
{"tunnelId": "t-8f2c41", "reason": "user_closed"}
```

`TUNNEL_CLOSED` (proxy → cloud, `requiresResponse: false`) — sent when any
tunnel ends, for the cloud audit trail (best effort; the router also logs it):

```json
{
  "tunnelId": "t-8f2c41",
  "code": 4001,
  "reason": "idle_timeout",
  "opened": "2026-11-27T15:02:11Z",
  "closed": "2026-11-27T15:14:40Z",
  "bytesToTarget": 18234,
  "bytesFromTarget": 402118
}
```

### 19.5 Relay WebSocket — `igos-tunnel.v1`

**Connect.** The proxy opens `relayUri`:

```http
GET /tunnel HTTP/1.1
Host: relay.perleapps.com
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Protocol: igos-tunnel.v1
Authorization: Bearer <relayToken>
X-Igos-Router-Id: 123-456789M01234
X-Igos-Tunnel-Id: t-8f2c41
```

- TLS is verified against the system CAs, as for the command channel
  (DevID mTLS later, §11).
- The relay accepts the token once, only for that router and tunnel, and only
  within 60 s of issuing `TUNNEL_OPEN`. Otherwise it answers `401` before the
  upgrade; the proxy refuses with `relay_failed`.
- The relay selects `igos-tunnel.v1` in its response; any other selection is
  a failure.

**Data.**

- Binary frames only; payload is raw TCP bytes, at most 64 KiB per frame.
  The proxy reads the target in chunks of up to 16 KiB.
- Text frames are reserved; receiving one closes the tunnel with `1003`.
- No compression extension (the inner traffic is already encrypted).

**Flow control.** The proxy stops reading from the target while more than
256 KiB is queued toward the relay, and stops reading the WebSocket while the
target's send buffer is full. The relay applies the same rule toward the
browser.

**Keepalive and idle.** The proxy pings every `keepalive` seconds; no pong
within twice that closes with `4011`. Pings do not count as activity; no
payload in either direction for `idleTimeout` closes with `4001`.

**Close.** Either side sends a Close frame with a code from the table. When
the target closes its side, the proxy flushes pending data and closes `4000`.
When the relay closes, the proxy flushes and closes the target connection.

| Code | Sent by | Meaning |
| --- | --- | --- |
| `4000` | proxy | Target closed the connection |
| `4001` | proxy | Idle timeout |
| `4002` | proxy | Max duration reached |
| `4003` | proxy | Max bytes reached |
| `4004` | proxy | Ended locally (`reset cloud remote-access session`) |
| `4005` | proxy | `remote-access` disabled or `service cloud` stopped |
| `4006` | proxy | Cloud egress became blocked (`egress`) |
| `4007` | proxy | Target connection error |
| `4008` | relay | User closed the session |
| `4009` | relay | Session ended by the cloud (`TUNNEL_CLOSE`, admin action) |
| `4010` | either | Shutting down |
| `4011` | either | Keepalive timeout |
| `4012` | relay | Browser side lost |

### 19.6 Multiplexing — `igos-tunnel.v2` (deferred)

Carries several TCP connections of one user session over one WebSocket
(§16.7). Negotiated through `tunnel.subprotocols` and `subprotocol`. Frame
layout:

| Bytes | Field |
| --- | --- |
| 0 | Type: `0x01` OPEN, `0x02` OPEN_OK, `0x03` DATA, `0x04` CLOSE, `0x05` WINDOW |
| 1–4 | Stream id (big-endian) |
| 5… | Payload: OPEN = JSON target (`host`/`port` or `service`); OPEN_OK = empty; DATA = bytes; CLOSE = 2-byte code; WINDOW = 4-byte credit |

Each stream is checked against the allow-list and route check like a v1
tunnel. Per-stream credit-based flow control (WINDOW). To be specified when
HTTPS browsing needs it.

### 19.7 `DEVICE_EVENT` and acknowledgement

**Local API** (event-handler cloud worker → proxy, unix socket):

```http
POST /perlecloud/events
{"eventType": "alert", "historyId": "…", "records": [ … ]}
```

Answers: `200 {"accepted": ["id", …]}`, `503` not connected (retry),
`501` cloud does not support `DEVICE_EVENT` (stop pushing).

**Cloud message** (proxy → cloud on `routers/command`,
`requiresResponse: true`), at most 50 records or 64 KB:

```json
{"eventType": "alert", "historyId": "h-5b1e",
 "records": [{"kind": "alert", "id": "…", "sequence": 812, "severity": "critical", "…": "…"},
             {"kind": "delivery", "id": "…", "sequence": 815, "alert_id": "…", "channel": "sms:oncall", "result": "sent"}]}
```

**Acknowledgement** — the HTTP response body:

```json
{"messageType": "DEVICE_EVENT_ACK",
 "messageParams": {"accepted": ["…"], "rejected": [{"id": "…", "reason": "invalid"}]}}
```

Accepted ids leave the queue; rejected ids with a permanent reason are
dropped; anything missing is retried. The cloud deduplicates on `id`.

### 19.8 Sequence diagrams

**a) Start-up and capability report**

```text
proxy                                   PerleCLOUD
  │ GET_NEXT_COMMAND {timeout, device}  │
  │────────────────────────────────────►│ store capabilities, link class
  │ EMPTY_COMMAND {nextPollDelay}       │ (metered → longer delay, Option 1)
  │◄────────────────────────────────────│
```

**b) SSH to a LAN device**

```text
browser      PerleCLOUD (UI + relay)          proxy                 LAN device
  │ open SSH to  │                              │                        │
  │ 192.168.1.20 │                              │                        │
  │─────────────►│ TUNNEL_OPEN (next poll)      │                        │
  │              │─────────────────────────────►│ resolve, allow-list,   │
  │              │                              │ route check            │
  │              │                              │ TCP connect :22 ──────►│
  │              │◄═══ wss + relayToken ════════│                        │
  │              │ TUNNEL_OPEN_RESPONSE accepted│                        │
  │              │◄─────────────────────────────│                        │
  │ xterm.js ◄──►│ cloud SSH client ◄══ bytes ══╪══ bytes ══════════════►│ sshd
  │ (user logs in with the device's own credentials)                     │
```

**c) Refused tunnel**

```text
PerleCLOUD                         proxy
  │ TUNNEL_OPEN 8.8.8.8:443         │
  │────────────────────────────────►│ route leaves wwan0, not an allow-interface
  │ TUNNEL_OPEN_RESPONSE refused    │
  │  reason route_not_allowed       │
  │◄────────────────────────────────│ (no connection made, nothing sent)
```

**d) Closing**

```text
user closes tab:   relay ── Close 4008 ──► proxy ── FIN ──► target
                   proxy ── TUNNEL_CLOSED {4008, bytes} ──► PerleCLOUD
idle timeout:      proxy ── Close 4001 ──► relay;  proxy ── FIN ──► target
                   proxy ── TUNNEL_CLOSED {4001, bytes} ──► PerleCLOUD
cloud admin:       PerleCLOUD ── TUNNEL_CLOSE ──► proxy ── Close 4009 ──► relay
```

**e) HTTPS to a LAN device**

As (b) with port 443. The cloud reverse proxy serves the session at its own
subdomain (e.g. `s-8f2c41.remote.perleapps.com`), acts as the HTTPS client to
the device with `Host`/SNI set to the typed name, and applies its
self-signed-certificate policy. Additional browser connections open more
tunnels (v1) or streams (v2).

**f) Alerts**

```text
baseline (pull):  PerleCLOUD ── gateway /graphql "alerts since 812" ──► proxy ──► VyOS API
                  ◄── {history_id, first_sequence, records} ─────────────────────
push (optional):  event-handler ── POST /perlecloud/events ──► proxy
                  proxy ── DEVICE_EVENT ──► PerleCLOUD ── DEVICE_EVENT_ACK ──► proxy
                  proxy ── 200 accepted ──► event-handler (items removed from queue)
```

**g) Older firmware**

```text
proxy (protocol 1)                     PerleCLOUD
  │ GET_NEXT_COMMAND {timeout}          │ no device block → protocol 1:
  │────────────────────────────────────►│ never send TUNNEL_OPEN / DEVICE_EVENT,
  │                                     │ use PROXY_CONNECT and gateway only
```

### 19.9 Gateway authorization (local socket, cloud role)

Replaces "caller must supply `body.key`" (`igos-cloud-proxy`
`docs/vyos-gateway.md`). Applies to both options (§11.2, §18.7).

**Request.** `EXECUTE_REST_COMMAND` to `/api/v1.1/managed-devices/vyos` gains
a `user` object; `body.key` is no longer sent:

```json
{
  "restCommandUri": "/api/v1.1/managed-devices/vyos",
  "requestMethod": "POST",
  "taskId": "cloud-request-123",
  "user": {"id": "u-1234", "name": "jsmith@example.com", "role": "operator"},
  "commandBody": "{\"endpoint\":\"/show\",\"body\":{\"data\":{\"op\":\"show\",\"path\":[\"version\"]}}}"
}
```

**Agent behavior.**

1. Check `user.role` against the endpoint table below; refuse with `403`
   (`{"error": "role not allowed"}`) otherwise.
2. Send the request to the API over the root-only local socket
   (`/run/api-local.sock`, §11.2) with no key, and with the acting user
   (`X-Igos-User: <user.name>`, `X-Igos-Via: cloud`) so commits are
   attributed to the cloud user. Any `body.key` or GraphQL `key` variable the
   cloud still sends is removed.
3. Log the call: cloud user, role, endpoint, operation, result status.

**Per-role endpoints (proposal; §13 item 9):**

| Endpoint | operator | operator+ | administrator |
| --- | --- | --- | --- |
| `/info`, `/retrieve`, `/show` | ✔ | ✔ | ✔ |
| `/graphql` queries | ✔ | ✔ | ✔ |
| `/generate`, `/reset`, `/traceroute`, `/renew` | | ✔ | ✔ |
| `/graphql` mutations | | | ✔ |
| `/configure`, `/configure-section`, `/config-file`, `/image`, `/container-image`, `/import-pki`, `/reboot`, `/poweroff` | | | ✔ |
| Built-in `cli` (tech-support) route | | | ✔ |

Telling GraphQL queries from mutations needs the agent to parse the operation
type (e.g. `graphql-core`) or an allow-list of operation names — to decide.

**Compatibility.**

- The agent advertises `GATEWAY_LOCAL_AUTH` in its capabilities (§19.1). The
  cloud stops sending `body.key` only to devices that advertise it.
- During transition the agent ignores any `body.key` the cloud still sends.
- No `user` object → treated as `administrator` (today's effective behavior)
  until the cloud sends roles; a `service cloud` option can later require it.

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
- `HttpProxySession._poll_loop` retries immediately after a failed poll, with
  no delay — a tight loop that burns CPU and, on cellular, data while the link
  is down or flapping during a GUI session.
- `HttpProxyRequest.body()` decodes request bodies as UTF-8 text, so binary
  uploads through the GUI tunnel (firmware image, config restore,
  certificates) can fail.
- GUI tunnel: role hard-coded `administrator`, `proxyRequestUri` not
  restricted (LAN and internet loop-out, any WAN including cellular), and
  `IGOS_CLOUD_SESSION` added to every response regardless of target (§0.6,
  §16.2). Interim fix: apply the §16.5 route check (`allow-interface`) to
  `proxyRequestUri`.
- Unknown `messageType` raises `CommandError`, which `poll_command` treats as a
  bad response: no reply to the cloud and polling backs off. Reply `ERROR`
  `UNSUPPORTED_COMMAND` with the `messageId` and keep polling (§10.1).
- Docs: token path (`/var/lib/…` vs `/config/igos/…`), CLI syntax, backoff
  values.
