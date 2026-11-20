# igOS NAT64 and `protocols failover` — Design / Concerns Note

Status: concerns note + design sketch (no implementation yet)
Scope: document why **stateful NAT64 (Jool)** flows are *not* recovered by the
multi-WAN `protocols failover` conntrack-flush-on-egress-change mechanism, when
that matters, and what an eventual Jool-aware fix would look like. The rest of
failover (route selection, health checks, scoped probe routes, and
**netfilter-conntrack** flush for IPv4/IPv6 masquerade/SNAT) is considered
working and is **not** changed by this note.

---

## 0. Context / grounding facts

### What already works (and is out of scope to change)

`vyos-failover` (`src/helpers/vyos-failover.py`) now, on an egress change for a
route, flushes the **netfilter conntrack** entries pinned to the vacated
interface's address(es):

```python
def flush_conntrack_for_addrs(addrs, vacated_iface=''):
    for addr in addrs:
        fam = 'ipv6' if ':' in addr else 'ipv4'
        rc, _ = rc_cmd(f'/usr/sbin/conntrack -D -f {fam} -q {addr}')
```

- Keyed on `reply-dst = vacated interface address` (conntrack cannot delete by
  outgoing interface), using the address the interface held while it was the
  active egress (cached), so it works even when the interface later loses the
  address (the common passthrough-upstream-dead case).
- Per-family: an IPv4 (`route`) failover flushes only IPv4 addresses, an IPv6
  (`route6`) failover only IPv6 — the families fail over independently and must
  not disturb each other's healthy flows.
- Verified on the reference unit (`igos@10.10.50.165`): the flush fires at both
  fail-over and failback, e.g.
  `failover: flushed conntrack for 10.12.91.249 after egress change (vacated
  interface eth1)`.

This covers every NAT that netfilter conntrack tracks:

| NAT type | Tracked by netfilter conntrack? | Recovered by the flush? |
|---|---|---|
| IPv4 `nat source … masquerade`/SNAT | yes | **yes** |
| IPv6 `nat66 source … masquerade`/SNAT-to-interface-addr | yes | **yes** |
| Stateless NPTv6 (`nat66 … translation prefix`) | no binding to flush | n/a (prefix renumbering, not pinning) |
| **Stateful NAT64 (`nat64 source …`, Jool)** | **no — separate state table** | **NO** |

### What NAT64 actually is on igOS

`nat64` is implemented with **Jool** (`src/conf_mode/nat64.py`):

- `verify()` calls `check_kmod(['jool'])`; the config lives in
  `/run/jool` (`JOOL_CONFIG_DIR`); instances are managed through
  `jool instance …` and `jool … bib/session …`.
- Jool maintains its **own BIB (Binding Information Base) and session tables**
  inside the `jool` kernel module — these are **completely separate from the
  netfilter conntrack table** that `conntrack -D` operates on.

Consequence: `conntrack -D -q <vacated-addr>` **cannot see or delete a Jool
NAT64 session**. Nothing in `vyos-failover` touches Jool.

---

## 1. The concern

When `protocols failover` moves the active egress between WAN interfaces, any
**stateful NAT64 session whose IPv4 side was pinned to the now-vacated
interface** keeps replaying a translation that no longer has a working return
path. The conntrack flush we rely on for v4/v6 masquerade does nothing for these
sessions, because they do not live in conntrack.

Whether this is actually felt depends on how the NAT64 `pool4` (the IPv4
transport addresses Jool translates onto) is provisioned relative to the
failover interfaces.

### Case A — `pool4` is a stable address behind a second-stage IPv4 masquerade

Topology: Jool translates v6→v4 onto a fixed/private `pool4`, and a normal IPv4
`nat source … masquerade` then SNATs that pool4 traffic out whichever WAN
failover selects.

- The **WAN-facing** stage is ordinary IPv4 masquerade → **conntrack-tracked** →
  **flushed on failover** by the existing mechanism.
- Jool's v6↔v4 binding is to the *stable* pool4, which does not change on
  failover, so it stays valid.
- **Result: recovers on failover** — via the second-stage masquerade flush, not
  via anything Jool-aware. Self-heals for bursty traffic even without the flush;
  the flush makes continuous flows recover immediately.

### Case B — `pool4` is (or is pinned to) the WAN interface address

Topology: Jool translates straight onto the failover interface's own IPv4
address (no second-stage masquerade).

- On failover the Jool session still references the **old WAN address**, which
  is now dead/unowned. The conntrack flush cannot clear it (not in conntrack).
- New sessions use the **same static `pool4`**, which still points at the wrong
  (old) interface — so session expiry does not help either; the replacement is
  equally wrong.
- **Result: does NOT self-heal.** Flows hang until the operator reconfigures
  `pool4` and/or flushes Jool state, *and* `pool4` is made routable on the new
  path. This is both a **state-pinning** problem and a **config/design**
  problem (a WAN-pinned `pool4` is fundamentally at odds with multi-WAN
  failover).

### Does it "sort itself out" eventually?

- **Bursty traffic (web, DNS):** largely yes — short UDP/ICMP conntrack-style
  timeouts mean new flows pick the new path within ~30 s–2 min; most apps also
  open fresh connections. (For Jool, "new flow" still needs a usable `pool4` —
  true in Case A, false in Case B.)
- **Continuous/long-lived flows (streaming, VoIP, VPN, gaming):** **no** — a
  steady packet stream keeps refreshing the stale state (conntrack *and* Jool
  session timeouts), so it can stay pinned to the dead egress indefinitely.
  These are exactly the flows users notice.

---

## 2. Why the generic conntrack flush can't be extended to cover it

`conntrack -D` talks to `nf_conntrack`. Jool sessions are owned by the `jool`
module and are only reachable through `jool … session`/`bib` operations. There
is no netfilter selector (`-q`, `-s`, `-d`, oif, mark) that reaches them.
Covering NAT64 therefore requires a **separate, Jool-specific** action, not a
tweak to `flush_conntrack_for_addrs`.

---

## 3. Sketch of an eventual Jool-aware fix (not implemented)

Only Case B needs code; Case A is already covered. Options, least- to
most-invasive:

1. **Flush Jool sessions on egress change.** When `vyos-failover` detects an
   egress transition for a route that has an active NAT64 instance, additionally
   run a scoped Jool flush (e.g. `jool instance flush` / per-instance
   `session`/`bib` removal) so stale bindings are dropped and re-created on the
   new path. Must be scoped to the affected instance so unrelated translations
   are not nuked, and bounded/debounced like the conntrack flush.
   - Caveat: a flush alone is insufficient if `pool4` still points at the dead
     interface (the renumbering problem below).

2. **Make `pool4` follow the active egress.** Teach the NAT64/failover
   integration to re-point `pool4` at the new egress's address on failover
   (effectively "masquerade-like" NAT64). Larger change; touches `nat64.py` and
   introduces a failover→Jool coupling that does not exist today.

3. **Document Case A as the supported multi-WAN pattern.** Recommend running
   NAT64 with a stable `pool4` behind an IPv4 `nat source masquerade`, so the
   existing conntrack flush already handles failover and **no Jool-aware code is
   needed**. Lowest risk; may be the right answer if nobody runs WAN-pinned
   `pool4`.

### Open questions before building anything

- Do any deployed units actually run `nat64 source` with a WAN-pinned `pool4`
  (Case B)? If not, option 3 (document-only) is sufficient and nothing ships.
- Jool flush granularity/cost: can we flush a single instance's sessions
  cheaply, and what is the failover-loop budget impact (the loop already sleeps
  per-nexthop and now does a conntrack flush on transitions)?
- Interaction with the per-family model: NAT64 is inherently v6→v4, so an
  egress change on the **IPv4** route is the relevant trigger, even though the
  client traffic arrives over IPv6.

---

## 4. Recommendation

Treat NAT64-over-failover as a **known, documented limitation** for now:

- The **balance of failover is working** — route selection, health checks,
  scoped probe routes, and conntrack flush for IPv4/IPv6 masquerade/SNAT all
  recover downstream flows on fail-over and failback.
- **NAT64 (Jool) stateful sessions are not covered.** In the recommended Case A
  layout (stable `pool4` + IPv4 masquerade) they recover anyway via the existing
  flush. In the Case B layout (WAN-pinned `pool4`) they do not self-heal and
  need either a config change to Case A or a future Jool-aware flush.

No code change is proposed by this note. Implementation (option 1/2) should be
gated on confirming a real Case B deployment exists.
