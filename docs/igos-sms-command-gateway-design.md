# igOS SMS Command Gateway — Design Sketch

Status: **Historical design sketch (2026-11-11); current implementation reference
updated 2026-11-13.**

> Use [igOS SMS command gateway](igos-sms-command-gateway.md) for the implemented
> CLI, source map, troubleshooting, validation, and known limitations. The sketch
> below preserves earlier alternatives and decisions; it is not a current
> implementation specification.

Historical references to `encrypted-password` below describe the former SMS
leaf name; the current SMS CLI uses `password-hash`. System login retains its
existing credential leaf names.

Decisions that supersede passages in this historical sketch:

| Topic | Current implementation |
| --- | --- |
| CLI credentials | `password` / `password-hash`; no nested `token` node |
| Number identity | Full normalized international number; no suffix setting |
| Password policy | Follow `system login enhanced-policy password`; shared validation/defaults |
| Password hashing | Normal Linux context, matching login; no separate SMS FIPS override |
| Replies | One bounded text submission, modem multipart handling, plain `...` truncation |
| History | Incoming first-field masking as `********` when gateway configured |
| Logs | Masked incoming message text, outgoing responses, phone numbers, journal timestamps |
| Reception | Existing event listener; bounded operation retries; no periodic polling |
| Source organization | SMS storage/transport/listener remain in the state machine; `sms_command.py` owns command handling |
| Upgrades | Fresh-install target; no SMS schema migration |


Scope: let an authorized operator send a **CLI operational command in an SMS**
(e.g. `show interfaces wwan wwan0`) to the device's cellular number and receive
the **command output back as one or more SMS replies**. The feature is a thin
**transport + authorization bridge** that reuses two existing pieces — the WWAN
FSM's inbound SMS handler and its SMS sender — and the existing VyOS op-mode
dispatcher. It introduces **no per-command parsing** and **no new command
catalog**: the op-mode wrapper does all command interpretation, and invalid
input is answered with VyOS's own error text.

The default and strongly-recommended posture is **read-only** (`show` only).
A **privileged** tier (`reboot`, `reset`, `renew`, …) is designed in but is
off by default and gated harder, because SMS is an **unauthenticated,
spoofable** channel.

---

## 0. Context / grounding facts

### Verified on the reference unit (`igos@10.10.50.165`) — 2026-11-11

- The WWAN manager (`igos-wwan-manager.service`) runs a full SMS subsystem in
  the modem state machine. On a connected modem it logs `SMS listener
  registered` and, on receipt, `Incoming SMS from <number>, id=<n>` (an
  incoming message from `433460` was observed during boot).
- The inbound and outbound halves both already exist in
  [`python/vyos/utils/wwan/interfaces_wwan_state_machine.py`](../python/vyos/utils/wwan/interfaces_wwan_state_machine.py):
  - **Inbound:** `_handle_incoming_sms(sms_path)` (≈ line 21114) reads the
    message `Number`, `Text`, `Timestamp`, `State` from ModemManager, records it
    in the flat-file store (`_sms_append`), and deletes it from the SIM once
    fully received. This is the single choke point every inbound SMS passes
    through.
  - **Outbound:** `sms_send(recipient, text)` (≈ line 21024) creates + sends an
    SMS via ModemManager's `Messaging` interface and records it. Already async.
  - **Listener wiring:** `_setup_sms_listener()` (≈ line 21179) subscribes to
    ModemManager's `Messaging.Added` signal; it is (re)armed on every
    `CONNECTED` entry (≈ line 4609–4610).
  - **Task scheduling:** `_safe_create_task(coro, name=...)` (≈ line 1003) is the
    FSM's guarded way to run background coroutines without stalling the event
    loop that drives modem monitoring.
  - **Per-interface config:** the FSM holds its applied configuration in
    `self.config` (populated over D-Bus `SetConfiguration` from
    `build_fsm_config()` in
    [`src/conf_mode/interfaces_wwan.py`](../src/conf_mode/interfaces_wwan.py)).

### The op-mode dispatcher is the "command processor"

- VyOS exposes operational commands through
  `/opt/vyatta/bin/vyatta-op-cmd-wrapper <verb> <path…>`, with one entry point
  per verb. [`python/vyos/configsession.py`](../python/vyos/configsession.py)
  wraps these:
  - `SHOW = [wrapper, 'show']` → `ConfigSession.show(path)` (line ≈ 83 / 462)
  - `RESET`, `REBOOT`, `RENEW`, `POWEROFF`, `GENERATE` → matching methods.
- The wrapper tokenizes the path against the CLI schema and routes to the right
  op-mode script. **We never look up a script, never maintain a command list,
  and never parse arguments.** Unknown/invalid input returns an error string we
  forward verbatim.

### Why a gateway (and not "just forward everything")

- The op-mode processor has **no concept of the SMS sender** — from its view the
  command arrives from a privileged local root daemon, so it will execute
  whatever **verb** it is handed, including destructive ones, and return
  success rather than an error. Therefore **authorization** and **verb-scope**
  are the gateway's job; **command validity** is VyOS's job.
- Because `ConfigSession.show()` hard-codes the `show` verb, dispatching through
  it **structurally confines** a sender to the read-only tree with zero
  per-command logic: `reboot now` sent to `.show()` becomes `show reboot now` →
  "invalid command" → forwarded as an error. Scope is chosen by **which entry
  point we call**, not by inspecting each command.

---

## 1. Architecture

```
incoming SMS  (ModemManager Messaging.Added)
      │
      ▼
_handle_incoming_sms(sms_path)         ← existing; records message
      │  one-line hook, after _sms_append():
      ▼
_safe_create_task( handle_sms_command(fsm, number, text) )   ← new, runs off the FSM loop
      │
      ├─ 1. authorize(number, text)      → allowlist + token       (gateway)
      ├─ 2. look up verb in JSON catalog  → tier + wrapper_verb + confirm (data)
      ├─ 3. dispatch generically via the op-mode wrapper             (processor)
      │        cmd([vyatta-op-cmd-wrapper, entry.wrapper_verb, *path])
      │        (one code path for every catalogued verb, show + privileged)
      ├─ 4. bound output: timeout + truncate + split into ≤N segments (gateway)
      └─ 5. for each segment: await fsm.sms_send(number, segment)     (existing)
```

Design rules that fall out of this:

- **The gateway is transport + a fixed-size guard, not a parser.** Its logic is
  the same handful of lines regardless of how many commands exist.
- **Never block the FSM loop.** The op-mode call is blocking (subprocess); run it
  in an executor wrapped in a timeout so modem monitoring keeps running.
- **Fail safe + quiet.** Unauthorized senders get **no reply** (don't confirm the
  number is a live target); authorized-but-bad commands get VyOS's error text.
- **Opt-in, zero footprint when unconfigured (hard requirement).** If
  `interfaces wwan wwanN sms-command` is **not configured**, the gateway loads
  **nothing** and does **nothing** — see §1a.

### 1a. Activation / zero-footprint when unconfigured

The gateway is **not a standalone service** and **must not add any runtime
footprint unless explicitly configured**:

- **No separate systemd unit.** The gateway is library code
  (`sms_command.py`) invoked from inside the existing `igos-wwan-manager`
  process. There is nothing to enable/start, so an unconfigured device has no
  extra unit, socket, timer, or process.
- **Config-gated at the single hook.** `_handle_incoming_sms` calls the gateway
  **only after** checking `self.config.get('sms_command')` is present *and*
  enabled (non-empty allowlist). When absent, it returns immediately — so there
  is **no** verb-catalog load, **no** `ConfigSession`/op-mode process spawn,
  **no** executor task, and no state created. Inbound SMS is still recorded in
  the existing store exactly as today; it simply is never interpreted as a
  command.
- **Unconfigure fully reverts.** Deleting `sms-command` returns the box to the
  above state on the next commit; `build_fsm_config()` emits no `sms_command`
  key, so the hook goes inert again with no restart required.
- **The existing SMS listener is unchanged.** `_setup_sms_listener()` already
  runs for the baseline SMS store feature and is **not** part of the gateway;
  the gateway adds no listener, signal subscription, or polling of its own.

> Net: unconfigured = indistinguishable from a build without the feature, at
> runtime. The only cost is dormant code on disk.

---

## 2. Authorization model (SMS is spoofable — treat as untrusted)

Chosen model: **sender allowlist + shared-secret token** (both required for the
default read-only tier; token mandatory for the privileged tier).

- **Allowlist (`allow-from`)**: E.164 numbers permitted to use the gateway.
  Matching normalizes to digits and compares the last N digits (carriers vary
  on `+`, country code, leading zeros). An empty allowlist means **disabled**,
  not "allow all".
- **Token**: a shared secret that must appear as the **first word** of the SMS
  body; it is stripped before dispatch. Protects against trivially spoofed
  sender IDs. For read-only it MAY be optional (operator choice); for privileged
  verbs it is **mandatory**. The token is a **configured password**: it is
  **policy-enforced and stored hashed** in the config exactly like a login
  password — see §4a. The gateway verifies an inbound token by re-hashing it
  against the stored salted hash (constant-time compare); the plaintext secret
  is not saved in plaintext configuration or gateway audit logs. Received SMS
  bodies are preserved in SMS history by the operator's later decision.
- **Rate limiting**: per-sender minimum interval + per-window cap, to blunt
  spoof-flooding and runaway cost.
- **Replay/loop safety**: ignore messages from short codes and from the device's
  own number; never answer a reply with a reply.

> Authorization is a property of the **channel**, evaluated **once per message**.
> It is not per-command and does not grow with the command set.

### 2a. Per-request authentication — no sessions (decided)

**Decision: the token must be present in *every* command SMS. There is no
standing "authenticated" window.** Each message is self-authenticating and
independently authorized; authorization is valid for exactly **one message** and
does not carry over.

Why not a session (authenticate once, then a grace window)?

- A session would have to be keyed on the **sender number**, which is exactly the
  **spoofable** identifier the token exists to backstop. During any open window,
  an attacker spoofing that number could issue commands **without** the secret —
  collapsing the model to sender-ID-only, which we have already rejected.
- Statelessness removes a whole class of bugs/edge cases: window expiry, per-
  number session tables, persistence across reboots, and clock-skew handling.
- SMS is bursty and low-volume; operators typically send a command or two. The
  cost (retyping the token) is small relative to the security gain.

Consequences / details:

- **Missing or wrong token ⇒ silent drop** (no reply — don't confirm the number
  is live), same as a non-allowlisted sender.
- **The token travels in cleartext over the carrier on every message.** This is
  inherent to SMS and is an accepted risk; it is mitigated by: policy-enforced
  strength (§4a), per-message consume + rate-limit (§5d), and read-only default
  scope. Operators who need confidentiality should not enable privileged verbs.
- **Two-step confirm is the only time-bounded element.** Every **action
  (privileged) verb** — `show` is exempt and runs immediately — triggers it: the
  device issues a **single-use nonce with a short TTL (e.g. 60–120 s)**; the
  `<token> CONFIRM <nonce>` reply must carry that nonce **and** (per the rule
  above) the token. The nonce is not a session — it authorizes one specific
  pending action, once, before it expires.
- **No "logout".** Because nothing is retained, there is nothing to revoke
  mid-stream; rotating the token (a normal `set … token plaintext-password`
  commit) invalidates all future use immediately.

---

## 3. Verb tiers (read-only vs privileged) — **data-driven**

The set of recognized verbs, their tier, their underlying op-mode `wrapper_verb`,
and per-verb policy (e.g. confirm) live in a **JSON catalog shipped with the
package**, not in code. Adding or retiring a verb is a **data edit** — no code
change — because `vyatta-op-cmd-wrapper <wrapper_verb> <path…>` already
dispatches every op-mode verb generically (see §3a). Config then only chooses
**which catalogued verbs are enabled** and **who may use them**.

Shipped defaults (the catalog's initial contents):

| Verb | `wrapper_verb` | Tier | Confirm | Default |
|------|----------------|------|---------|---------|
| `show` | `show` | read-only | no (runs immediately) | **enabled** |
| `renew` | `renew` | privileged | **yes** | disabled |
| `reset` | `reset` | privileged | **yes** | disabled |
| `generate` | `generate` | privileged | **yes** | disabled |
| `reboot` | `reboot` | privileged | **yes** | disabled |
| `poweroff` | `poweroff` | privileged | **yes** | disabled |

Core behavioral rule:

> **Read-only (`show`) commands run immediately. Every action (privileged)
> command requires a two-step confirmation.** Confirmation is a property of the
> **tier**, not a per-verb opinion — so any action verb added to the catalog in
> future automatically requires confirm, and no action can ever execute from a
> single SMS.

Rules:

- If **only** `show` is enabled, the gateway can **force** the `show` verb and
  skip verb detection entirely — the sender can never reach another verb, and
  nothing ever needs confirmation.
- Enabling any privileged verb flips this from an **information channel** into a
  **remote-control channel**. Those verbs therefore require: mandatory token, a
  (optionally separate, tighter) allowlist, an audit-log entry per use, **and the
  two-step confirm**.
- **Two-step confirm** (all action verbs): the gateway replies with a single-use
  nonce; the action runs only if the operator texts `<token> CONFIRM <nonce>`
  within a short TTL (§2a). The original action command is held pending until
  then and discarded on timeout.
- `confirm` defaults to `true` for the `privileged` tier and `false` for
  `read-only`; the per-verb `confirm` key in the catalog is an **optional
  override** for unusual cases (e.g. a read-only-but-expensive verb that you want
  to gate). It is **not** required to make action verbs confirm — the tier does
  that.
- **Tier is set by the catalog, not by config.** An operator can enable or
  disable a catalogued verb and restrict who may use it, but cannot move a verb
  into the read-only tier or invent a verb that isn't in the catalog.

### 3a. Verb catalog file (add a verb without writing code)

A single JSON file, installed with the rest of `data/` to
`/usr/share/vyos/` (same as `op-mode-standardized.json`,
`config-mode-dependencies/`, `fips/approved-algorithms.toml`), with an in-tree
copy for dev/unit tests:

- Source: `data/wwan/sms-command-verbs.json`
- Installed: `/usr/share/vyos/wwan/sms-command-verbs.json`

`confirm` is omitted for normal verbs (it is derived from `tier`); include it
only to override the tier default.

```json
{
  "verbs": {
    "show":     { "wrapper_verb": "show",     "tier": "read-only"  },
    "renew":    { "wrapper_verb": "renew",    "tier": "privileged" },
    "reset":    { "wrapper_verb": "reset",    "tier": "privileged" },
    "generate": { "wrapper_verb": "generate", "tier": "privileged" },
    "reboot":   { "wrapper_verb": "reboot",   "tier": "privileged" },
    "poweroff": { "wrapper_verb": "poweroff", "tier": "privileged" }
  }
}
```

How it is consumed (all generic — adding a catalog entry needs **no code**):

1. **Gateway dispatch** (`sms_command.py`): the catalog is **loaded lazily on
   first configured use and cached** (`lru_cache`), so an unconfigured device
   never reads it (§1a). Look up the inbound verb in the catalog. If absent →
   reject. If present → check its `tier` is enabled and the sender/token satisfy
   that tier's auth. **Read-only → run now; privileged → require confirm**
   (`confirm = entry.get('confirm', tier == 'privileged')`); then run
   `cmd(['/opt/vyatta/bin/vyatta-op-cmd-wrapper', entry['wrapper_verb'], *path])`
   in an executor under timeout. One code path covers every verb, present and
   future.
2. **CLI validation/completion**: the `privileged verb <x>` leaf's allowed values
   (and completion help) are generated from the same JSON, so the CLI only
   offers catalogued verbs and a typo is rejected at commit — again, driven by
   data, not a hand-maintained XML enum.
3. **Safety invariant**: the catalog is the *only* source of dispatchable verbs.
   A verb not in the file can never be executed, so the file is the single,
   reviewable control surface for "what can this channel ever do."

> **Why this is safe to make data-driven:** the risky decision is *which class of
> action* a verb performs, and that is captured by `tier`/`confirm` in the
> catalog plus the config's per-tier auth — all reviewed as data. The dispatch
> itself is already generic in VyOS, so no per-verb code ever existed to bypass.

---

## 4. CLI surface (conf-mode, under the WWAN interface)

Everything is `set`-driven and read by `build_fsm_config()`, surfacing as
`self.config['sms_command']` in the FSM. Placement under the owning interface
keeps it multi-modem-safe (each `wwanN` has its own gateway + allowlist).

```
# illustrative shape — not final
set interfaces wwan wwan0 sms-command                       # enable (read-only show tier)
set interfaces wwan wwan0 sms-command token plaintext-password 'S3cret-token!'
#   -> hashed on commit, stored as:
#      interfaces wwan wwan0 sms-command token encrypted-password '$6$...'
set interfaces wwan wwan0 sms-command allow-from '+15555551234'
set interfaces wwan wwan0 sms-command allow-from '+15555556789'
set interfaces wwan wwan0 sms-command max-reply-segments 4  # output bound
set interfaces wwan wwan0 sms-command rate-limit 3          # msgs/min/sender

# privileged tier (opt-in; token mandatory, confirm for reboot/poweroff)
set interfaces wwan wwan0 sms-command privileged verb reboot
set interfaces wwan wwan0 sms-command privileged verb reset
set interfaces wwan wwan0 sms-command privileged allow-from '+15555551234'
```

Open placement question (see §8): per-interface (above) vs a single global
`set service sms-command` that references the modem. Per-interface is the
default recommendation for parity with the rest of the WWAN tree.

### 4a. Token: policy-enforced + hashed (same as login passwords)

The `token` is a configured password and reuses the **exact mechanism** already
applied to `system login user … authentication` passwords — no new crypto, no
new policy. It is a two-leaf node mirroring the login pattern:

- `token plaintext-password <secret>` — what the operator sets.
- `token encrypted-password <hash>` — what persists (salted modular-crypt hash).

**`verify()`** (in `interfaces_wwan.py`), applied to a freshly-set
`plaintext-password`:

- Enforce the **enhanced password policy** via the shared
  [`password_policy_check()`](../src/conf_mode/system_login.py) using the same
  thresholds configured under `system login enhanced-password-policy`
  (min length / digit / uppercase / special; defaults 9/1/1/1), plus the
  cracklib [`evaluate_strength()`](../python/vyos/utils/auth.py) check — identical
  pass/fail semantics to a user password.
- **FIPS KDF gate:** an operator-supplied `encrypted-password` must be in the
  approved `$5$`/`$6$` family under FIPS (same rejection as
  `system_login.py` does for user accounts).
- **No whitespace** in the plaintext token (rejected at commit): the token is
  position 1 of the SMS, split on whitespace, so a spaced token would be
  unparseable (§5a). All policy-required classes (upper/digit/special) are
  satisfiable without spaces.

**`generate()`** (config write-back): hash the plaintext with the shared,
FIPS-aware `hash_password()` (`sha512_crypt` `$6$` under FIPS, else passlib
`linux_context`), set `encrypted-password`, and remove the `plaintext-password`
node via `delete_cli_node`/`add_cli_node`
([`vyos.utils.configfs`](../python/vyos/utils/configfs.py)). The plaintext secret
never reaches the saved config, `show configuration`, or the commit log — only
the salted hash does.

**Gateway verification** (runtime, in `sms_command.py`): the inbound SMS carries
the token in plaintext; the gateway verifies it by re-running crypt against the
stored `encrypted-password` (i.e. `crypt(received, stored_hash) == stored_hash`)
with a constant-time compare. This differs from a login password (which PAM
verifies against `/etc/shadow`) only in *where* the compare happens — the
storage format and policy are the same. The received plaintext is used for the
compare and then discarded; it is never logged.

> **Small refactor implied:** `password_policy_check()` currently lives in
> `src/conf_mode/system_login.py`. To reuse it from `interfaces_wwan.py` without
> duplication, lift it (and the `PASSWORD_POLICY_DEFAULT_*` constants) into
> `vyos.utils.auth` alongside `evaluate_strength()`, and import it from both
> call sites. Likewise expose one shared FIPS-aware `hash_password()` helper.

---

## 5. Message handling details

### 5a. Request structure (the SMS the operator sends)

The body is a single line, parsed positionally with `shlex.split()`. The
**token is always first**; everything after it is the command.

```
Grammar (explicit-verb mode):
    <token> <verb> [<path> ...]

Grammar (forced-show mode, if configured read-only-only):
    <token> [<path> ...]            # verb implicit = show

Grammar (two-step confirm reply):
    <token> CONFIRM <nonce>

Where:
    <token>  one whitespace-free word — the shared secret (always position 1)
    <verb>   a catalogued verb (show, reset, renew, reboot, …) — see §3a
    <path>   the op-mode command path; remaining words, passed verbatim as argv
             to  vyatta-op-cmd-wrapper <wrapper_verb> <path…>
    <nonce>  the single-use value the device issued for a confirm verb (§2a)
```

Parsing / normalization rules:

- **Position 1 is the token**, split on whitespace (`shlex`), compared by
  re-hash against the stored `encrypted-password` (§4a). A token is a single
  word — **whitespace in the token is rejected at commit**, so position is
  unambiguous and no quoting of the secret is needed.
- **Position 2 is the verb** in explicit mode; it must match a catalogued,
  **enabled** verb (case-sensitive, op verbs are lowercase). In forced-show mode
  there is no verb word and position 2 onward is the `show` path.
- **Remaining words are the op-mode path**, passed as an **argv list** (never a
  shell string) to the wrapper — so `;`/`|`/`` ` ``/`$` cannot inject; an extra
  character allowlist is optional hardening only.
- Leading/trailing whitespace is trimmed; internal whitespace runs collapse via
  `shlex`. Quoting (`"…"`) in `<path>` is honored by `shlex` for values that
  contain spaces, exactly as on the CLI.
- Anything that fails to parse, lacks a token, or whose verb is unknown/disabled
  ⇒ **silent drop** (unauthorized) or VyOS's own error text (authorized but bad
  command), per §1/§2a.

Examples (token shown as `S3cret!`):

```
S3cret! show interfaces wwan wwan0           → runs now: show interfaces wwan wwan0
S3cret! show configuration commands          → runs now: show configuration commands
S3cret! interfaces ethernet eth0             → (forced-show) runs now: show interfaces ethernet eth0
S3cret! reset vpn ipsec peer 203.0.113.5     → action: device replies "CONFIRM 7731 to run 'reset vpn ipsec peer 203.0.113.5' (120s)"
S3cret! reboot now                           → action: device replies "CONFIRM 4821 to reboot (120s)"
S3cret! CONFIRM 4821                          → executes the pending reboot
reboot now                                   → dropped (no token)
WrongPass show version                        → dropped (bad token, no reply)
```

Dispatch after parsing: `entry_point(path)` runs inside `run_in_executor` under
`asyncio.wait_for(timeout)` (§5c).

### 5b. Reply (SMS length + cost bounds)

- GSM-7 is 160 chars/segment (70 for UCS-2); concatenated segments carry a
  header that drops the usable size (~153 / 67). Target **~150 chars** and split
  on line boundaries where possible.
- Submit each reply as one long SMS; ModemManager and the modem handle
  concatenation headers for handset reassembly. Do not add `(i/n)` prefixes.
  Enforce a hard **`max-reply-segments`**
  cap; mark truncation with `...`. This stops `show tech-support` /
  `show configuration` / `show log` from emitting dozens of texts or forming a
  cost/feedback loop.
- Empty output → a short `no output` so the operator always gets an ack.

### 5c. Concurrency & safety

Updated implementation decision: process admitted requests concurrently instead
of rejecting a second request with `command busy`.

- Each admitted request runs in its own `_safe_create_task`, with an independent
  asynchronous subprocess, configured timeout and output budget. There are no
  additional fixed authentication, task, or concurrent-command quotas.
- The configured per-sender rate limit applies to admission. Output retention
  derives from the configured reply-segment cap; no separate fixed byte cap is
  imposed. The gateway does not add limits on the number of allowlisted senders,
  pending confirmations, password length, or request length.
- Replies are serialized per interface to keep multipart segments together,
  without blocking other command executions or modem monitoring.
- Each pending action has its own nonce, including parallel requests from one
  sender. Authentication, single-use consumption and the fixed 120-second TTL
  apply independently to every confirmation.
- Dispatch uses the operational wrapper through asynchronous subprocesses;
  no reusable `ConfigSession` is required. Failures/timeouts affect only their
  own requests. Already-started actions retain their timeout across disconnects.
- Only limits specified by the design or explicitly requested by the operator
  may be added. Configuration values must be positive integers, without extra
  implementation-specific upper caps. E.164's 15-digit maximum is retained.

### 5d. SMS store interaction & queue flushing (replay/staleness guard)

The FSM **already stores every inbound SMS automatically** (per-SIM flat file
`sms_<iccid>.json` via `_sms_load`/`_sms_append`/`_sms_save`, capped at
`SMS_MAX_MESSAGES`). Critically, on CONNECTED the FSM registers the live handler
and then **drains the SIM** via `_drain_existing_sms()`
([≈ line 21243](../python/vyos/utils/wwan/interfaces_wwan_state_machine.py)),
reading **every SMS already on the SIM — including ones received while the unit
was powered off — and feeding them through the same `_handle_incoming_sms`
path** that live commands use. This creates a real hazard for a command channel:
a **backlog of stale / power-off-delivered messages** could be interpreted as
**stale commands** — e.g. a week-old `reboot now` executing the first time the
modem connects after boot.

**Timing — why the commit is *not* the flush anchor.** The startup config
commit (`SetConfiguration`) that enables the gateway runs **early in boot**, and
it *does* fire on every boot (so the enabled state is correctly refreshed each
boot — no separate boot trigger needed). But the SIM **drain runs ~tens of
seconds later**, on CONNECTED, *after* the commit. So anything anchored to the
commit moment (e.g. "mark the store consumed now" or "remember the current
count") happens while the stale messages are **not yet in the store** — they
drain in afterward and look new. **The flush must be anchored to the drain, not
the commit.**

Requirement: **the gateway only ever acts on genuinely new, live arrivals; every
message the drain imports is treated as stale and never dispatched.**

Rules:

- **Drain is always stale (the real flush point).** `_drain_existing_sms()` must
  tag every message it imports as `drained: true` (stale); such records are
  stored as history but are **never** command-eligible. This is what flushes the
  power-off / backlog queue on **every power-up and every modem re-attach**,
  independent of config timing.
- **Arm live dispatch only after the drain completes.** Command execution is
  armed for the **live `on_sms_added`** path only, and only once the initial
  drain/settle has finished — so a drained message can never race ahead of the
  arm point and be mistaken for a live command.
- **Enabled state comes from the commit; staleness comes from the drain.** The
  startup commit refreshes `self.config['sms_command']` every boot (enabled or
  not); the drain tag + arm-after-drain decide *which* messages may run. The two
  are independent, which is why a stale message cannot execute even though the
  gateway is "enabled" the instant the modem connects.
- **Consume on process.** After the gateway handles a live message, mark that
  store record consumed (e.g. `command_consumed: true`) so a later restart, SIM
  re-read, or store reload cannot re-execute it — the cross-reboot replay guard
  that complements the per-message `source_path` dedup.
- **Timestamp sanity (belt-and-suspenders).** Optionally also ignore any message
  whose network timestamp predates the arm point, covering modems that resurface
  a stale SMS via the live `Added` signal rather than the drain.
- **Keep SIM storage clear.** Continue the FSM's existing behavior of deleting
  the message from ModemManager once fully received, so SIM storage does not
  fill and the modem keeps signaling new arrivals. (The flat-file store, not the
  SIM, is the system of record.)
- **Bound growth.** The store's existing `SMS_MAX_MESSAGES` cap still applies;
  the gateway adds only the small `drained`/`consumed` bookkeeping, no new
  unbounded state.

> Rationale: the store is a **log**, not a command inbox. Power-up, reboot, and
> reconnect all funnel stale SIM messages through the **drain**, so the drain is
> the single place staleness is decided — the gateway dispatches exactly once,
> only for live post-arm arrivals, and a queue of old texts can never fire.

---

## 6. Security considerations (summary)

- **Channel is untrusted:** sender ID is spoofable and SMS is cleartext over the
  carrier. Allowlist + token are the floor; privileged verbs add confirm + audit.
- **Least privilege by construction:** the JSON catalog is the *only* source of
  dispatchable verbs; with just `show` enabled, scope cannot widen by accident
  or malformed input, and a verb not in the file can never run.
- **No shell:** argv-list dispatch; no string interpolation into a shell.
- **Replay/staleness:** the FSM auto-stores SMS and **drains the SIM on every
  power-up/reconnect** (incl. messages received while powered off), so the
  gateway tags drained messages stale, arms live dispatch only after the drain,
  and marks messages consumed — a stored/power-off backlog can never fire (§5d).
- **DoS/cost control:** rate limit + reply-segment cap + command timeout.
- **Privacy:** `show` output can contain sensitive config (keys, peers). Operators
  enabling the gateway should understand replies traverse the carrier in the
  clear; consider restricting the allowed `show` subtree if needed (future).
- **Auditability:** log every accepted command with sender, verb, and tier;
  privileged actions always logged, never silent.

---

## 7. Integration seams (small, bounded)

1. **FSM hook (1 line + a guard):** in `_handle_incoming_sms`, after
   `_sms_append(record)` and the "Incoming SMS" log, **only if**
   `self.config.get('sms_command')` is present and enabled, schedule
   `self._safe_create_task(handle_sms_command(self, number, text), name=...)`.
   Unconfigured ⇒ no task, no load (see §1a).
2. **New module:** `python/vyos/utils/wwan/sms_command.py` — the gateway
   (authorize, catalog lookup, bounded generic dispatch, chunk). Keeps the
   already-huge state machine file untouched beyond the hook + one import.
3. **New data file:** `data/wwan/sms-command-verbs.json` — the verb catalog
   (§3a). Shipped to `/usr/share/vyos/wwan/` via the existing `debian/rules`
   `cp -r data/* …`. **Adding a verb is an edit here, not code.**
4. **conf-mode:** add the `sms-command` subtree to the WWAN interface XML
   (an include under `interface-definitions/include/`) and populate
   `self.config['sms_command']` in `build_fsm_config()`; the `privileged verb`
   leaf's allowed values are generated from the catalog.
5. **Shared helpers refactor:** lift `password_policy_check()` +
   `PASSWORD_POLICY_DEFAULT_*` and a FIPS-aware `hash_password()` into
   `vyos.utils.auth` so `system_login.py` and `interfaces_wwan.py` share one
   implementation (§4a).
6. **No op-mode changes:** dispatch uses the existing
   `vyatta-op-cmd-wrapper`; nothing new to register, and every current/future op
   verb is reachable by data alone.

---

## 8. Decisions (locked for v1)

All previously-open items are resolved below so implementation is unblocked.
Anything not listed is out of scope for v1 (see §11.7).

- **Auth model:** sender **allowlist + token**, token in **every** message, no
  sessions (§2a). **Locked.**
- **UX:** **explicit verb** — `<token> <verb> <path…>`. (Forced-show is not used,
  because v1 ships action verbs too.) **Locked.**
- **Config placement:** **per-`wwanN`** under `interfaces wwan wwanN sms-command`.
  **Locked.**
- **Tiers in v1:** ship **both** — read-only `show` (runs immediately) **and**
  the privileged/action tier (two-step confirm). All verbs are **disabled by
  default**; configuring `sms-command` with an allowlist enables `show`, and each
  action verb is individually opt-in via `privileged verb <x>`. **Locked.**
- **Confirm:** derived from tier — `show` immediate, every action verb requires a
  single-use nonce, TTL **120 s**, reply `<token> CONFIRM <nonce>` (§2a/§3).
  **Locked.**
- **Token storage:** stored **hashed** (salted modular-crypt), policy-enforced,
  FIPS-aware KDF (§4a). Accept **both** `plaintext-password` (hashed on commit)
  and a directly-set `encrypted-password` (validated, FIPS `$5$/$6$` gate),
  mirroring `system login`. **Locked.**
- **Verb catalog:** single global file `data/wwan/sms-command-verbs.json`; keys
  `wrapper_verb`, `tier`, optional `confirm` override. No per-verb output caps in
  v1 (global cap applies). **Locked.**
- **`show` subtree restriction:** **not in v1** — all `show` subtrees reachable;
  operators needing confidentiality should not enable the gateway. Revisit later.
- **Reply delivery:** **best-effort** — send segments in order; log any
  ModemManager send failure; **no per-segment ack, no retry** in v1.
- **Defaults/limits:** `rate-limit` 5 msgs/min/sender; command `timeout` 20 s;
  `max-reply-segments` 4; segment size ~150 chars; allowlist match on last 10
  digits. All overridable via config except the confirm TTL (constant in v1).

---

## 9. Status of prerequisite work

- Inbound SMS capture, flat-file store, SIM cleanup — **done** (FSM).
- Outbound SMS send — **done** (`sms_send`).
- SMS listener (re)arming on CONNECTED — **done** (FSM).
- Generic op-mode dispatch via `vyatta-op-cmd-wrapper` / `ConfigSession` —
  **done** (library); every op verb is reachable by data.
- Gateway module, FSM hook, CLI subtree + `build_fsm_config()` wiring,
  on-target validation — **not started** (this document).

---

## 10. First coding session (jump-start)

### 10a. Lock these first (shape the code)

All shaping decisions are **locked** in §8. In short: allowlist + token (every
message), explicit verb, per-`wwanN` config, both tiers (show immediate / action
confirm), token hashed like a login password.

### 10b. Ordered build steps

1. `data/wwan/sms-command-verbs.json`: ship the catalog (§3a) with `show`
   enabled-by-default semantics; add a tiny loader (with in-tree fallback path,
   like `vyos.fips`).
2. `sms_command.py`: `handle_sms_command(fsm, number, text)` with authorize →
   catalog lookup (tier enabled? auth ok? confirm?) → generic
   `run_in_executor(cmd, [wrapper, entry.wrapper_verb, *path])` under timeout →
   chunk → `fsm.sms_send`. Read settings from `fsm.config['sms_command']`.
3. One-line hook in `_handle_incoming_sms` + the import, plus the
   **drain-anchored staleness** bookkeeping: tag `_drain_existing_sms()` imports
   `drained: true`, arm live dispatch only after the drain completes, and mark
   `command_consumed` on process (§5d).
4. Shared-helper refactor (§7.5): move `password_policy_check()` + defaults +
   `hash_password()` into `vyos.utils.auth`.
5. CLI include XML for `sms-command` (incl. `token plaintext/encrypted` +
   `privileged verb` values sourced from the catalog) + `build_fsm_config()`
   population; default disabled (empty allowlist ⇒ off).
6. Logging for accepted/denied commands.

### 10c. Validation harness

- On `igos@10.10.50.165`: text `<token> show interfaces wwan wwan0` from an
  allowlisted number → expect the op-mode output back, segmented.
- Negative: wrong token, non-allowlisted sender, `reboot now` (must become an
  error, not a reboot), oversized output (segment cap honored), rapid messages
  (rate limit honored).
- **Replay/flush:** place a message on the SIM while powered off (or pre-load the
  store), incl. a command-like one; boot/reconnect → the drained backlog must
  **not** execute; only a newly-sent live message runs. Restart the manager
  after a command → the handled message must **not** re-execute (consumed).
- Confirm the FSM loop keeps servicing modem monitoring during a slow command.
