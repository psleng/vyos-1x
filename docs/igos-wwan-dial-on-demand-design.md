# igOS Cellular (WWAN) Dial-on-Demand Daemon — Design Sketch

Status: design draft (no implementation yet)
Scope: replace today's restart-per-cycle dial-on-demand service with a
**long-running, separate daemon** (`dial-on-demand@.service`) that detects
traffic demand on `wwanN` and expresses it to the WWAN FSM manager
(`igos-wwan-manager`) through the **existing bearer-lease API**, instead of
issuing `connect_bearer()` / `disconnect_bearer()` itself. The FSM's lease
reconciler already owns the actual connect/idle decision; the daemon becomes a
pure *demand sensor*. The service stays separate from the FSM (by choice), the
conf-mode scripts and CLI are unchanged.

---

## 0. Context / grounding facts

### Verified on the reference unit (`igos@10.10.50.165`) — 2026-11-12

Kernel 6.18.62, igOS image `999.202611122142`, 2 × Cortex-A53, `wwan0` in
`connection-mode dial-on-demand` (`idle-timeout 30`, `connect-timeout 30`,
`flush-timeout 60`), `eth1` unplugged, `protocols failover` with a `wwan0`
route (ICMP check to 8.8.8.8).

- `dial-on-demand@wwan0:30:30:60.service` had **194 restarts in 4 h 12 m**
  (`NRestarts=194`, one every ~78 s). Every run ends with
  `Finished service successfully!` → `Deactivated successfully` →
  `Scheduled restart job` (unit has `Restart=always`, `RestartSec=5s`).
- Each run costs **~7 s of CPU** (~4 s of it is just interpreter start-up and
  imports); **1,117 s CPU over 195 runs** — the single largest bursty consumer
  on the box (other steady consumers: `vyos-http-api` 1,324 s, `flask`
  1,245 s, `igos-wwan-manager` 960 s, `dhclient@eth1` 949 s, `vyos-failover`
  614 s). `top` shows each restart as a ~55 % single-core spike.
- The modem reconnects within ~1 min of every idle-down because all DNS goes
  to the carrier resolvers via `wwan0` (`/etc/resolv.conf` contains only
  `161.216.x.x`, routed out `wwan0`) and `eth1` has no link, so the only
  default route is `wwan0`. `wwan0` got a new carrier IP on each reconnect.
  (Config/environment issue — see §11; not fixed by this design but made
  cheap by it.)

### How the code works today

- `python/vyos/utils/wwan/dial_on_demand/dial_on_demand_controller.py`
  `run()` executes **one** cycle and returns:
  1. `idle_wwan_watcher.idler()` — poll `/sys/class/net/wwanN/statistics/tx_bytes`
     every `idle-timeout` s; when unchanged, `disconnect_bearer()`.
  2. `install_wwan_nft_rules.generate_nft_rules()` — add table
     `inet wwanN_raw_<q>` with an NFQUEUE trigger on `output` (router-originated,
     `oifname wwanN`) and `prerouting` (`iifname <LAN phys> fib daddr oifname wwanN`).
  3. `wwan_nft_reconnect.main()` — on the first queued packet:
     `connect_bearer()` + `wait_for_bearer("connected")`, release queued packets
     (or drop them after `flush-timeout`), tear down the table, set
     `shutdown_event`.
  4. Close the D-Bus client, exit 0 → systemd restarts the unit 5 s later.
- `wwan_nft_reconnect.py` keeps `shutdown_event`, `modem_ready_event` and
  `packet_queue` as **module globals that are never `.clear()`ed** — they are
  reset only because the process exits.
- Errors raised in `handle_packet_async()` are swallowed by the task
  done-callback; the `flush-timeout` is the only thing that ends a wedged cycle.

### What the manager already provides (reused, not changed)

- **Bearer leases** — `python/vyos/utils/wwan/bearer_lease.py`: time-bounded
  positive-demand tokens keyed by `(interface, owner)`. Owner allow-list in
  `interfaces_wwan_config.py` `_LEASE_OWNER_ALLOWLIST` already contains
  **`dod-traffic`** (machine owner → finite TTL required, max 3600 s).
- **Reconciler** — `interfaces_wwan_state_machine.py`
  `_reconcile_bearer_demand()` + `_lease_sweep_loop()` (1 s tick):
  - `always-on` → no-op; vetoes (admin/airplane/data-limit/shutdown) → no-op;
  - any live lease → take ownership, `CONNECT` from `REGISTERED_IDLE`;
  - no live lease and the bearer is lease-owned → `ENTER_IDLE` after a
    5 s grace (`_lease_idle_grace`);
  - invariant: a bearer brought up by other logic (dial-on-demand
    connect-at-boot, operator `connect_bearer()`) is **never** torn down unless
    a lease was held while it was up (adoption).
- **Client API** — `wwan_client.py`: `bearer_lease_acquire(ifnum, owner, ttl,
  reason)`, `bearer_lease_release()`, `bearer_lease_list()`,
  `get_bearer_status()`; `open_client_with_retry()` in
  `dial_on_demand/_client.py`.
- **Failover** already uses the lease API (`owner="failover"`) —
  `src/helpers/vyos-failover.py`. `disconnect_bearer()` is ignored while another
  client holds a lease.
- **Manager restart**: config is replayed from `/run/wwan/interfaceN.conf`
  (`interfaces_wwan_service_manager.py` `_bootstrap_persisted_interfaces()`);
  in dial-on-demand mode the FSM **connects at start-up** (like always-on).
  The lease registry is **in-memory only** and is empty after a restart.
  The manager unit (`src/etc/systemd/system/igos-wwan-manager.service`) has
  `Restart=on-failure`, `RestartSec=30`, `StartLimitBurst=3` /
  `StartLimitIntervalSec=300`, so after a manager crash it is absent from the
  bus for **≥ 30 s**.
- **Restart detection precedent**: the manager watches
  `org.freedesktop.DBus.NameOwnerChanged` for ModemManager
  (`interfaces_wwan_main.py` ~L242). `wwan_client.py` has no equivalent for
  `com.igos.IgosModemManager` today.

---

## 1. Problems with the current design

| # | Problem | Consequence |
|---|---|---|
| P1 | systemd `Restart=always` is the loop | A crash is indistinguishable from a normal cycle; `NRestarts` is meaningless; failures are hidden. |
| P2 | Per-cycle process start | ~7 s CPU per cycle, D-Bus reconnect, nft table rebuild every cycle. |
| P3 | State reset by process death | Module-global events/queue would break any in-process loop; correctness depends on an undocumented side-effect. |
| P4 | Two components drive the bearer | The daemon calls `connect_bearer()`/`disconnect_bearer()` directly while the FSM has its own reconciler and failover holds leases; ordering/ownership is implicit. |
| P5 | Manager restart detected indirectly | Noticed only when a later D-Bus call fails (≤ idle-timeout in the idle stage; ≤ flush-timeout in the trigger stage). |
| P6 | Errors swallowed in packet handler | A D-Bus error during dial leaves the cycle waiting for `flush-timeout`. |
| P7 | Noisy logging | `tx vs last tx` / `Still detecting packets` every idle-timeout, forever. |

---

## 2. Goals / non-goals

Goals
- One long-running process per interface; systemd restarts it **only on
  failure**.
- The daemon only **expresses demand** (lease acquire/renew); the FSM
  reconciler alone connects and idles the bearer.
- Explicit, testable internal state machine; all per-cycle state owned by an
  object, no module globals.
- Deterministic resync after manager restart, daemon restart, or config
  change — on purpose, not as a side-effect.
- Safe on a metered link: a crashed/stuck daemon can never pin the bearer up
  (lease TTL lapses on its own).
- No change to the CLI, conf-mode scripts (`interfaces_wwan.py`,
  `protocols_failover.py`) or the manager's D-Bus API.

Non-goals
- Moving dial-on-demand into the FSM process (explicitly rejected — keep it
  separate).
- Fixing *why* the link is woken (carrier DNS, failover target on `wwan0`) —
  documented in §11 as configuration guidance.
- Changing nftables trigger scope rules (`install_wwan_nft_rules.py` logic is
  kept as-is).

---

## 3. Architecture

```
             LAN / local traffic
                    │
        ┌───────────▼────────────┐   NFQUEUE (only while ARMED)
        │ nft inet wwanN_raw_<q> │──────────────┐
        └───────────┬────────────┘              │
                    │                           ▼
               wwanN egress            ┌──────────────────────┐
                    │                  │ dial-on-demand@wwanN │  (long-running)
   /sys/.../tx_bytes└─────────────────►│  DemandDaemon        │
                                       │  ACTIVE/ARMED/DIAL   │
                                       └──────────┬───────────┘
                         bearer_lease_acquire     │  get_bearer_status
                         (owner=dod-traffic,      │  NameOwnerChanged
                          ttl=idle-timeout)       ▼
                                       ┌──────────────────────┐
                    failover lease ───►│ igos-wwan-manager    │
                    operator intent ──►│  lease registry      │
                                       │  _reconcile_bearer_  │
                                       │   demand() (1 s)     │──► ModemManager
                                       └──────────────────────┘
```

Key change: **the daemon never calls `connect_bearer()` or
`disconnect_bearer()`.**

- *Demand present* → acquire/renew the `dod-traffic` lease with
  `ttl = idle-timeout`.
- *Demand absent* → stop renewing. The lease expires after `idle-timeout`;
  the reconciler idles the bearer after its 5 s grace (if no other lease —
  e.g. failover — is live and the operator has not requested the bearer).

This collapses P4: the FSM is the only component that changes bearer state,
and the daemon's demand is ORed with failover/operator demand by the
existing reconciler.

---

## 4. Daemon state machine

States (per interface, owned by a `DemandDaemon` instance):

| State | Bearer | nft trigger | What the daemon does |
|---|---|---|---|
| `SYNC` | unknown | removed | (Re)connect D-Bus (`open_client_with_retry`), read `get_bearer_status()`, choose next state. |
| `ACTIVE` | up | removed | Every `poll` s read `tx_bytes`; if it moved, renew the lease. Watch for the bearer going down. |
| `ARMED` | down | installed | Wait for the first queued packet (or the bearer coming up by other means). |
| `DIALING` | coming up | installed (queueing) | Lease held; wait for `connected` up to `connect-timeout`; release queued packets on success, drop them at `flush-timeout`. |

Transitions:

| From | Event | To | Action |
|---|---|---|---|
| start | — | `SYNC` | remove stale nft tables for this interface (existing `cleanup()`) |
| `SYNC` | status `connected` | `ACTIVE` | **adopt**: acquire lease once (`reason="adopt"`) so the reconciler owns the bearer and can idle it (see §6) |
| `SYNC` | status `disconnected` | `ARMED` | install trigger |
| `ACTIVE` | `tx_bytes` changed | `ACTIVE` | renew lease (`ttl = idle-timeout`) |
| `ACTIVE` | status `disconnected` (lease lapsed → FSM idled, or link dropped) | `ARMED` | install trigger |
| `ARMED` | packet queued | `DIALING` | acquire lease (`reason="egress traffic"`) |
| `ARMED` | status `connected` (failover/operator/FSM brought it up) | `ACTIVE` | remove trigger, accept anything queued; adopt (acquire once) |
| `DIALING` | status `connected` | `ACTIVE` | accept queued packets; remove trigger |
| `DIALING` | `flush-timeout` | `ARMED` | drop queued packets; keep trigger; stop renewing (lease lapses) |
| any | D-Bus error / manager `NameOwnerChanged` | `SYNC` | in-process reconnect (bounded); exit non-zero if the manager stays gone (§7) |
| any | SIGTERM | exit 0 | remove trigger; **do not** release the lease (§8) |

Notes
- Bearer status is polled at a modest cadence (e.g. every 2 s in `ACTIVE`,
  1 s in `DIALING`) — the manager emits no bearer-state signal (open decision
  D3).
- `tx_bytes` is sampled every `poll` s (proposal: `min(5, idle-timeout/3)`) and
  the lease is renewed **only when traffic is seen**, so the effective idle
  time is `idle-timeout` + up to `poll` + 5 s FSM grace + ≤ 1 s sweep.
- The trigger is installed **idempotently** (delete-then-add of the
  per-interface table) so a crash between install and remove cannot stack
  tables.

---

## 5. Traffic detection

- Source: `/sys/class/net/wwanN/statistics/tx_bytes` (same as today — egress
  is what indicates demand for the WAN). Open decision D1: also count
  `rx_bytes` so a long inbound download with sparse ACKs is never idled.
- The FSM already suppresses its own probe traffic in dial-on-demand mode
  (`connectivity-monitoring` ping loop off), so FSM chatter does not hold the
  link (see `docs/vyos-wwan-set-commands.md`, dial-on-demand).
- IPv6 multicast is already dropped ahead of the queue (existing nft rule +
  conf-mode chatter guard) — unchanged.

---

## 6. Bearer up without our lease (adoption)

Cases where the bearer is up but the daemon holds no lease:
- FSM connect-at-boot / connect after a manager restart (dial-on-demand mode
  connects at start-up and the lease registry is empty);
- failover or the operator brought it up while the daemon was `ARMED`.

The reconciler never idles a bearer it does not own. On entering `ACTIVE`
from `SYNC` or `ARMED`, the daemon therefore acquires the lease **once**
(adopt). From then on normal renew-on-traffic applies, and with no traffic the
lease lapses and the bearer is idled — the same "connected but no data →
disconnect" behaviour the restart loop provided implicitly today, now
explicit.

Operator intent wins: if `bearer_requested` is set (operator
`connect_bearer()`), the reconciler hands ownership back to the operator path
and will not idle the bearer even when our lease lapses (existing behaviour,
`_reconcile_bearer_demand()`).

---

## 7. Manager restart / D-Bus loss

- Subscribe to `org.freedesktop.DBus.NameOwnerChanged` for
  `com.igos.IgosModemManager` (same pattern as the manager uses for
  ModemManager in `interfaces_wwan_main.py`). A new owner → drop the client,
  go to `SYNC`.
- Any D-Bus exception from a call → `SYNC` as well (belt and braces; covers a
  missed signal).
- In `SYNC`, reconnect with backoff for longer than the manager's own restart
  delay (manager `RestartSec=30`; proposal: 90 s, i.e. `open_client_with_retry(
  connect_timeout=90)` instead of today's 20 s default). Only if the manager is
  still unreachable after that, **exit non-zero** and let `Restart=on-failure`
  take over (bounded by `StartLimit*` so a dead manager does not cause a tight
  restart loop). While in `SYNC` the trigger stays removed, so `wwanN` egress
  is never queued to a consumer that cannot dial.
- After the manager restarts its lease registry is empty and dial-on-demand
  mode connects at start-up; `SYNC` sees `connected` → `ACTIVE` → adopt (§6)
  → idles normally if there is no traffic.

## 8. Daemon crash / stop

- `Restart=on-failure` (was `always`), `RestartSec=5s`,
  `StartLimitIntervalSec=300`, `StartLimitBurst=5`.
- Crash: the lease is not renewed and lapses within `idle-timeout`; the bearer
  idles if nothing else wants it — safe on a metered link. The nft trigger may
  be left behind; the next start removes it in `SYNC` (`cleanup()`), and
  `ExecStopPost=` can run the same cleanup so a permanently failed unit never
  leaves a queue with no consumer (which would black-hole `wwanN` egress).
- SIGTERM (config change, `protocols_failover.py` restart for trigger scope):
  remove the trigger, close D-Bus, exit 0. **Do not release the lease** —
  a stop/start for a scope rebuild would otherwise idle a busy link after the
  5 s grace; the new instance re-adopts in `SYNC`, and if the interface really
  left dial-on-demand mode the lease is harmless (`always-on` ignores leases;
  delete/disable tear the FSM down).
- Optional (D4): `ExecReload=` → SIGHUP rebuilds the trigger scope in place, so
  `protocols_failover.py` can `reload` instead of `restart`.

---

## 9. Configuration handling (unchanged)

All config changes already reach the service as `systemctl` operations from
conf-mode; the daemon only has to honour SIGTERM:

| Config change | Existing conf-mode action |
|---|---|
| `connection-mode` leaves dial-on-demand | `systemctl stop dial-on-demand@wwanN:*` (`interfaces_wwan.py` `wwan_on_demand_service()`) |
| `idle-timeout` / `connect-timeout` / `flush-timeout` changed | stop old instance (params in the instance name), start new |
| `disable` / interface deleted | stop (`apply()` disable/deleted paths) |
| failover interface set changes | `systemctl restart dial-on-demand@wwanN:*` (`protocols_failover.py` `_restart_dial_on_demand()`) |

The instance name format `wwanN:idle:connect:flush` is kept.

---

## 10. Code layout

New / changed
- `python/vyos/utils/wwan/dial_on_demand/daemon.py` — `DemandDaemon`
  (state machine, timers, lease renewal, NameOwnerChanged watch). All state on
  the instance.
- `python/vyos/utils/wwan/dial_on_demand/trigger.py` — `NfqueueTrigger`:
  idempotent install/remove (reusing `install_wwan_nft_rules.basic_nft_commands`
  and `get_inbound_interfaces()`), NetfilterQueue bind/unbind, a per-instance
  packet queue, accept-all / drop-all. No module globals.
- `dial_on_demand_controller.py` — reduced to argument parsing + signal
  handling + `DemandDaemon(...).run()`.
- `src/systemd/dial-on-demand@.service` — `Restart=on-failure`,
  `StartLimit*`, `ExecStopPost=` cleanup.
- `wwan_client.py` — small helper to watch `NameOwnerChanged` for the manager
  (or implement it inside the daemon).

Removed
- `idle_wwan_watcher.py` connect/disconnect logic and the boot
  `wait_for_initial_connection` flag (`/run/dial-on-demand.wwanN.connected-once`)
  — superseded by `SYNC` + adoption.
- `wwan_nft_reconnect.py` module globals and direct `connect_bearer()` use.

Unchanged
- `install_wwan_nft_rules.py` scope logic, conf-mode scripts, manager D-Bus
  API, lease registry and reconciler.

---

## 11. Related issues found on the unit (configuration guidance, out of scope)

These are why the reference unit reconnects every ~78 s; the new daemon makes
each cycle cheap but does not stop them:

1. **DNS only via the carrier.** With `eth1` down, `/etc/resolv.conf` holds
   only the carrier resolvers, reached via `wwan0`; every lookup (NTP pool
   names, GUI, …) is demand. Guidance: provide non-WWAN resolvers on the
   primary path, or accept that name resolution wakes the link.
2. **Failover check on `wwan0`.** A health check targeted through a
   dial-on-demand interface is demand by definition (failover holds its own
   lease). Guidance: dial-on-demand is for links that are normally idle; do not
   use it as the active primary path, or make the failover check status-based
   only for dial-on-demand members (already partly done in
   `vyos-failover.py`).
3. **`eth1 address dhcp` with no cable** — `dhclient` fails every ~2 min and
   each failure runs `dhclient-script-vyos` (several Python helpers via
   `vyos-hostsd-client`): ~950 s CPU in 4 h. Upstream VyOS behaviour; guidance
   is to disable unused DHCP ports.

---

## 12. Testing

Unit (no hardware; fake client + fake clock + fake nft runner), extending the
existing suites (`src/tests/test_dial_on_demand_nft.py`,
`test_dial_on_demand_flush.py`, `test_dial_on_demand_failover_seam.py`,
`test_bearer_lease.py`, `test_wwan_bearer_reconcile.py`,
`test_failover_lease.py`):
- each transition in §4, including adopt-on-`SYNC` and adopt-from-`ARMED`;
- lease renewed only on traffic; never renewed while idle;
- flush-timeout drops queued packets and stays `ARMED`;
- D-Bus error and `NameOwnerChanged` → `SYNC` → correct state;
- manager unreachable past the deadline → non-zero exit;
- SIGTERM removes the trigger and does **not** release the lease;
- two consecutive cycles in one process (regression for P3).

On the reference unit:
- steady state: `NRestarts` stays 0 over hours; CPU of the unit ≈ idle;
- idle → bearer idles after ~idle-timeout + grace; first packet dials and is
  delivered (DNS query succeeds);
- `systemctl restart igos-wwan-manager` while idle and while connected →
  daemon resyncs, bearer idles if no traffic;
- `kill -9` the daemon while `ARMED` → restarted, stale table removed, no
  egress black-hole;
- switch `connection-mode` to always-on and back; change `idle-timeout`;
- failover active on `wwan0` → bearer held by the failover lease regardless of
  our lease.

---

## 13. Open decisions

- **D1** Count `rx_bytes` as demand as well as `tx_bytes`?
- **D2** `poll` cadence for `tx_bytes` and bearer status (CPU vs reaction
  time); proposal 5 s / 2 s.
- **D3** Add a manager `BearerStateChanged` D-Bus signal so the daemon stops
  polling status (manager change — optional, not required for v1).
- **D4** Add `ExecReload=` (SIGHUP → rebuild trigger scope) and switch
  `protocols_failover.py` from `restart` to `reload`.
- **D5** Lease TTL = `idle-timeout` exactly, or `idle-timeout` + `poll` to
  avoid an early lapse between samples.
- **D6** Keep `wait_for_initial_connection` semantics at boot (wait for the
  FSM's connect-at-boot before deciding), or rely on `SYNC` re-reading status
  until the FSM leaves its transitional states.

---

## 14. Rollout

1. Implement `NfqueueTrigger` + `DemandDaemon` behind the existing unit name;
   keep the CLI/instance format.
2. Unit tests (§12) green; run the reference-unit checklist.
3. Switch the unit to `Restart=on-failure` in the same change (the old code
   exits 0 every cycle and would stop working with `on-failure`).
4. Remove the superseded code paths (§10).
