# igOS GUI User Accounting — Design Sketch

Status: design draft (no implementation yet)
Scope: make **web GUI sessions first-class citizens of the box's device-side
accounting**, so that a user who authenticates through the HTTPS GUI is recorded
and visible like a console / serial / SSH user — in `show users`,
`show users recent` (`who`/`last`), the local audit log, and the commit log
(correctly attributed) — and, *only where that method is actually in use*, in the
central **TACACS+/RADIUS accounting** server. The emphasis is deliberate: the
target is the **device-side** accounting that only the box can produce
(on-device presence + change attribution), **not** re-emitting login-accounting
that already lives at the IdP (SAML) or AAA server (TACACS+/RADIUS) — see §2.1.
Today the GUI runs its own authentication plane that is completely disjoint from
the OS login plane, so none of the standard device accounting tools can see GUI
users. This document explains **why** that happens, why it is a real
product/compliance gap (not a cosmetic one), and the architecture required to
close it.

This is a cross-repo effort spanning the GUI (`flask-react-dev`) and this
repository (`vyos-1x`, plus a small privileged host-side component).

---

## 0. Context / grounding facts

The following were verified by reading the code in both repositories and by
inspecting a running reference unit (`igos@10.10.50.165`). File references are to
the state of the repositories at the time of writing.

### 0.1 How `show users` works today (`vyos-1x`)

`op-mode-definitions/show-users.xml.in`:

- `show users` runs **`who -H`**, which reads **`/var/run/utmp`**.
- `show users recent [N]` runs **`last -aF -n N`**, which reads
  **`/var/log/wtmp`**.

Both `utmp` and `wtmp` are the kernel/login **accounting databases**. They are
only ever written by interactive **TTY/PTY logins** — console, serial, SSH —
because the program that establishes such a login (`getty`+`login`, or `sshd`)
runs the full PAM stack **including the `session` phase**, and that session phase
(`pam_lastlog` / `systemd-logind` / the login program) is what writes the
`utmp`/`wtmp` record. No TTY login ⇒ no record ⇒ invisible to `who`/`last`.

### 0.2 The GUI authenticates at the application layer (`flask-react-dev`)

`flask/app/auth/routes.py` and `flask/app/auth/models.py` show the GUI is its
**own identity/authentication provider**, with several parallel auth sources:

- **`authenticate_db`** — validates against a private **`User` table**
  (email + bcrypt hash, in Postgres). These GUI users **do not exist as Unix /
  system accounts at all**.
- **`authenticate_ssh` / `login_pam` / `login_mfa_paramiko`** — call
  `pam.authenticate(...)` (and in one path, an actual paramiko SSH) purely as a
  **password-checking oracle**. They run PAM's **auth phase only**; they never
  run the **session phase**, never allocate a TTY, never spawn a shell.
- **cloud** (`cloud_auth.py`), **RADIUS** (`auth/radius_server/`), and **OTP**
  (`auth/pam/otp_store.py`) are further app-layer auth sources.

On success the app issues a **web session**, tracked in a durable store (see
§0.3) plus a Flask cookie. At no point is there a TTY, a login program, a PAM
`session` open, or a Unix shell — so there is **nothing for `who`/`last`/`utmp`
to record**.

### 0.3 The GUI already has a durable session store (`flask-react-dev`)

`flask/app/auth/web_sessions.py` defines a SQLAlchemy model **`WebSession`**
(table `web_sessions`) with a full lifecycle and rich attributes:

| Column | Meaning |
|---|---|
| `id` | session UUID |
| `username` | authenticated principal |
| `permission` | operator / administrator |
| `auth_source` | db / pam / cloud / radius / … |
| `ip_address` | client IP (proxy-aware, `client_ip_address()`) |
| `device` | parsed from User-Agent |
| `authenticated_at` | **login** timestamp |
| `expires_at` | **expiry** timestamp |
| `revoked_at` | **logout / disconnect** timestamp |

Helper functions already exist:

- `start_web_session(auth_source)` — login hook (creates the row; prunes rows
  with `expires_at < now`).
- `revoke_web_session(session_id)` — logout/disconnect hook (sets `revoked_at`).
- `list_web_sessions()` — returns **active** sessions
  (`revoked_at IS NULL AND expires_at > now`) with all fields above.
- HTTP surface: `GET /sessions` and `DELETE /sessions/<id>`
  (`flask/app/auth/routes.py`), both `@login_required`.

Deployment (from `docker-compose*.yml`, `nginx/conf.d/flask.conf`): the Flask
backend runs as a **container** (`:5000`) behind nginx, with its own Postgres
(`SQLALCHEMY_DATABASE_URI`). It therefore has its **own PID/mount namespace** and
is structurally isolated from the host's `/var/run/utmp`.

### 0.4 Config-change accounting is also wrong for GUI users (`vyos-1x`)

`python/vyos/config_mgmt.py:_new_log_entry()` records the commit-log `user` from
`os.getlogin()`/`geteuid()` (the **OS user the API server runs as**, e.g.
`igos`) and `via` from the `COMMIT_VIA` env var (`vyos-http-api`). So
`show system commit` shows GUI-initiated changes as `by igos via vyos-http-api`
— it captures **that** the HTTP API made a change, never **which GUI user** did.
This is the same two-planes problem applied to *command/config* accounting.

### 0.5 What accounting actually fires today, per auth method (`vyos-1x`)

Verified from `src/pam-configs/*`, `data/templates/login/*`, `debian/control`,
and the SAML package's `pam-configs/saml_auth`. "Remote AAA login acct" means a
TACACS+/RADIUS Accounting start/stop (or equivalent) is sent off-box for the
login session.

| Auth method (access path) | `who`/`last` (utmp/wtmp) | journald / auth.log | Remote AAA login acct | Per-command acct | Commit-log change record |
|---|:--:|:--:|:--:|:--:|:--:|
| **Local** `pam_unix` (console/SSH) | yes | yes | n/a (no server) | no | yes, correct user |
| **RADIUS** (console/SSH) | yes | yes | yes — `pam_radius_auth.so` **Session** phase (acct port = auth+1, default 1813) | no | yes |
| **TACACS+** (console/SSH) | yes | yes | yes — `pam_tacplus.so` **Session** phase, `acct_all=1` | no (audisp referenced, unwired) | yes |
| **SAML** (console/serial, backed by real/JIT user) | yes | yes | **no — `saml_auth` is Auth-phase only (no Session)** | no | yes |
| **SAML via GUI** | no | no | no | no | misattributed |
| **GUI** (non-SAML web) | no | no | no | no | misattributed |

Key facts behind the table:

- **`utmp`/`wtmp` are written by the login program, not the auth module.** Any
  interactive TTY/PTY login (`getty`+`login`, `sshd`) runs the common-session
  (`pam_lastlog`/`pam_systemd`), so **all** OS logins land in `who`/`last`
  regardless of whether the backend is local, RADIUS, TACACS+, or SAML. The web
  (GUI) plane is the only one with no login program, hence no utmp.
- **RADIUS and TACACS+ carry their accounting in the PAM `Session` phase.** Both
  `radius-optional`/`radius-mandatory` and `tacplus-optional`/`tacplus-mandatory`
  include a `Session:` line that sends login/logout accounting off-box
  (`tacplus_servers.j2` sets `acct_all=1`).
- **SAML has no Session phase at all.** The shipped `saml_auth` profile is
  `Auth:` only (`pam_saml.so`), so the device sends **no** SAML login/logout
  accounting. This is **normal and expected**: SAML is a browser-SSO
  authentication protocol, not an AAA-accounting protocol, and the **Identity
  Provider (IdP) is the system of record** for SAML authentication events
  (who/when/IP/MFA/SP). Device-side SAML *login* accounting is therefore **not a
  target** — the IdP already covers "who authenticated."
- **Per-command accounting is unconfigured platform-wide.** `debian/control`
  installs `auditd`, but nothing under `src/` generates audit rules or an
  `audisp-tacplus` configuration; `tacplus_servers.j2` only *mentions*
  audisp-tacplus as a potential consumer. So per-command/per-`set`/`delete`
  accounting is an **unwired hook for every method**, not active behavior.
- **The only per-change trail that exists is the local commit log** (§0.4):
  commit-granular, local (optionally archivable off-box via
  `system config-management commit-archive`), and — for the GUI/API — attributed
  to the API's OS user rather than the real actor.

---

## 1. Problem statement

The box has **two parallel identity + accounting planes**:

1. **OS/PAM plane** — console / serial / SSH → PAM session phase →
   `utmp`/`wtmp` + journald + logind + (if configured) TACACS+/RADIUS
   accounting → `show users` / `last` / central AAA server.
2. **GUI application plane** — browser → Flask app auth → `web_sessions` row +
   cookie. Touches **none** of the OS accounting sinks.

Nothing bridges them. Consequently:

- `show users` / `show users recent` never show GUI users.
- The local audit log (journald/auth.log) has no GUI login/logout events.
- `show system commit` misattributes GUI config changes to the API's OS user.
- A customer's central **TACACS+/RADIUS accounting** server never receives a
  start/stop record for GUI admin sessions — *where that method is the one in
  use*. (For SAML-authenticated GUIs this particular point does not apply, since
  SAML login accounting lives at the IdP, not the device — see §0.5 and §2.1.)

### 1.1 Why this is a product/compliance gap, not cosmetics

For any customer that enforces centralized administrative access control
(common in enterprise, carrier, and regulated environments), **administrative
access must be accountable regardless of the channel used**. A web GUI with its
own shadow login that the device's accounting cannot see is a classic
audit/security-review finding: "unaccounted administrative access path." It also
undermines incident response ("who was on the box at 02:00?") and change
attribution ("who made this change?").

The requirement, stated carefully (and narrowed by §2.1): **GUI sessions and
GUI-initiated changes must be visible and correctly attributed on the device
itself, to the same degree an equivalent OS login already is.**

---

## 2. What "the same accounting" actually means

"Same accounting as other users" is broader than `show users`. On this platform
the login-accounting pipeline has four sinks; the relevant ones for a GUI
session are:

1. **utmp / wtmp** → `who` / `last` / `show users` / `show users recent`.
2. **journald / auth.log** (authpriv) → local audit log.
3. **systemd-logind** sessions → `loginctl`.
4. **Central AAA accounting** — TACACS+/RADIUS **Accounting start/stop** when the
   box is configured for it (and, ideally, command/config accounting).

`show users` is merely the most visible of these; sink (2) is the local audit
trail, and sink (4) is what enterprise buyers mean by "accounting." See §0.5 for
which sinks each existing auth method feeds today.

### 2.1 What the device owes vs. what the IdP already owns

Not every "accounting" obligation belongs on the device. The key split:

| Question | Authoritative source | On a SAML box | Device gap for GUI? |
|---|---|---|---|
| **Who authenticated?** | The **IdP** (or TACACS+/RADIUS for those methods) | IdP logs it — normal, sufficient | No — covered off-box |
| **Who was on the device, and for how long?** | The **device** (`who`/`last`, logind) | device only | **Yes** — web sessions are invisible |
| **Who changed what on the device?** | The **device** (commit log + attribution) | device only | **Yes** — API/GUI changes misattributed |

The consequence is important and narrows the whole effort: the gap is **not**
"the device must emit a login-accounting record for every auth method." For
authentication, the IdP (SAML) and the AAA server (TACACS+/RADIUS) already are
the systems of record. The gap is the **device-side activity accounting that no
external system — IdP included — can provide**, because only the box knows what
happened on the box *after* login: that a web session was established, by whom,
from where, for how long, and what it changed.

This is also why a SAML-centric design does not get accounting "for free": SAML
tells the IdP *that* a user authenticated, but it is silent on on-device
presence and on-device changes. So the bridge below is required **regardless of
whether the GUI authenticates via local, RADIUS, or SAML** underneath.

---

## 3. Root cause (the "why")

A GUI login is an **application-level login**, not an **operating-system
login**. The OS login path records sessions because a login program allocates a
TTY and runs the PAM **session** phase, whose modules write the accounting
records. The GUI does none of that: it validates credentials in-process (often
against principals that are not even Unix users) and issues a cookie-backed web
session. Specifically, three structural facts guarantee the bypass:

1. **It is a web app, not a shell.** HTTP request/response has no terminal line;
   `utmp`/`wtmp` are built around "user attached to a TTY," which a browser
   session cannot map to.
2. **Its users are not necessarily OS users.** The private `User` table (and
   cloud/RADIUS identities) means there is frequently **no Unix account** to
   record even in principle.
3. **It runs in a container.** Any PAM call or `utmp` write it performed would be
   the container's, not the host's — isolated from what VyOS reads.

Using PAM for the password check (`pam.authenticate`) does **not** help: that is
the PAM **auth** phase. Accounting is produced by the PAM **session** phase
(`pam_open_session`/`pam_close_session`), which the GUI never runs.

---

## 4. Design principle

**Do not institutionalize the split.** The wrong answer is a separate
`show users web` / "GUI sessions" view — that would make the two-plane divide a
permanent, documented product behavior, which is exactly what customers will
reject. The right answer is to **bridge GUI sessions into the real device-side
accounting pipeline** so that `show users`, `last`, journald, logind, and the
commit log all reflect GUI users **automatically**, with no special-case
command.

Scope the bridge to **what only the device can provide** (§2.1): on-device
session presence and correct change attribution. Emitting a TACACS+/RADIUS
*login*-accounting record for the GUI is optional and only relevant when the GUI
actually authenticates via that method — it is **not** needed to reach parity for
a SAML- or locally-authenticated GUI, where authentication is already accounted
at the IdP (SAML) or is local.

The cleanest way to hit the device-side sinks "for free" is to drive a **real
PAM session** (`pam_open_session` … `pam_close_session`) for each GUI session,
because the session-phase modules already present on the box
(`pam_lastlog` → wtmp, `pam_systemd` → logind, and — only if the deployment uses
them — `pam_tacplus`/`pam_radius` → AAA accounting) do the work. The challenge is
purely **where** that runs, given the container boundary.

---

## 5. Proposed architecture: host-side accounting broker

Because the GUI is containerized and (partly) backed by non-Unix users, it must
**not** open host PAM sessions or write host `utmp` itself. Instead:

```
  Browser ──HTTPS──> Flask GUI (container)        VyOS host (root)
                         │  authenticates user        │
   login  ───────────────┼──► notify (local socket) ──┼─► accounting broker
                         │                            │     ├─ pam_open_session (login-class service)
                         │                            │     │    → wtmp (pam_lastlog)
                         │                            │     │    → TACACS+/RADIUS accounting START
                         │                            │     │    → logind session (pam_systemd)
                         │                            │     ├─ write utmp entry  (who/show users)
                         │                            │     └─ journald audit line
   logout/expiry ────────┼──► notify (local socket) ──┼─► broker: pam_close_session
                         │                            │     → wtmp logout + AAA accounting STOP
                         │                            │     → remove utmp entry
```

Responsibilities:

- **GUI (`flask-react-dev`)** stays the authenticator. On `start_web_session`
  and `revoke_web_session` (and on expiry cleanup) it emits a small event to a
  **host-local** channel: `{action, username, ip_address, device, auth_source,
  session_id, timestamp}`. No new privileges in the container.
- **Broker (host, `vyos-1x` + packaging)** is a small privileged service that
  translates those events into **real accounting**: open/close a PAM session via
  a dedicated login-class PAM service, write/remove the `utmp` record, and log to
  journald. It is the only component that needs root and host namespace access,
  and it is the single integration point with TACACS+/RADIUS accounting.

With the broker in place, **no changes to `show users` are required** — `who`,
`last`, `loginctl`, journald, and the AAA server all observe GUI sessions as
ordinary logins (optionally tagged with a pseudo-line such as `web`/`gui` and the
client IP in the host field).

### 5.1 Transport options for the GUI→broker channel
- Unix-domain socket bind-mounted into the container (simple, local-only).
- Localhost-only HTTP endpoint on the broker (easy from Flask; must be strictly
  localhost + token-gated, never exposed through nginx).
- A spooled append-only event file the broker tails (most decoupled; adds
  latency and its own cleanup).

---

## 6. Hard problems / open decisions

These are the design questions that determine effort and must be decided before
implementation.

1. **GUI users that are not system users.** `authenticate_db` uses a private
   `User` (email + bcrypt) store. An AAA server cannot meaningfully account a
   principal it has never heard of. The stronger, more defensible product
   position is to **converge GUI auth onto the box's real identity sources**
   (local VyOS users, RADIUS, TACACS+, SAML) and retire the shadow `User` table,
   so there are no unaccountable local admin accounts. This is the same
   "two planes" concern as the earlier SAML/RADIUS/TACACS exclusivity work.
   *Decision needed: keep the private user store (and define how to account for
   it) vs. converge on system identity sources.*

2. **Reliable logout / expiry.** OS sessions end when the process exits; web
   sessions end on explicit logout, cookie expiry, idle timeout, or revoke. To
   write a `wtmp` logout and send an AAA **STOP** you need a dependable
   end-of-session signal. `revoke_web_session` + expiry pruning exist, but a
   **reaper** is required for idle/crash cases so sessions do not dangle as
   "logged in forever" (and so `utmp` entries are reclaimed).

3. **Command / config accounting.** The only per-change trail today is the local
   commit log, and for the GUI it is misattributed to the API's OS user (§0.4).
   Note that **per-command accounting is unconfigured platform-wide** — `auditd`
   is installed but no `audisp-tacplus`/audit-rule configuration is generated, so
   *no* auth method (local, RADIUS, TACACS+, SAML) emits per-command accounting
   today (§0.5). Therefore the realistic, device-only win here is to attribute
   GUI-initiated **config changes** to the **real GUI user** in the commit log
   (e.g. by threading the user into `COMMIT_VIA`/commit-log attribution). Full
   per-command TACACS+ accounting is greenfield for the whole platform and is a
   separate, optional ambition — not "catching the GUI up."
   *Decision needed: session login/logout + commit attribution only, or also
   build platform-wide per-command accounting?*

4. **Containerization boundary.** Confirmed from the compose files; it is the
   reason the broker pattern (root, host namespace) is necessary rather than
   writing `utmp`/opening PAM sessions from inside the container.

5. **utmp semantics for a non-TTY session.** Web sessions have no TTY line.
   Decide on a convention (e.g. a synthetic `web`/`gui` line id, client IP in the
   host field, a reserved UID range or the mapped system user) so `who`/`last`
   output is honest and unambiguous.

---

## 7. Scope / phasing

A reasonable phasing that front-loads the device-only wins the IdP/AAA server
cannot provide (§2.1):

- **Phase 1 — device-side parity (the real target).** GUI→broker events + broker
  that writes `utmp`/`wtmp` and journald, plus correct **commit-log attribution**
  for GUI-initiated changes. Result: `show users`, `show users recent`,
  `loginctl`, local audit logs, and `show system commit` all reflect the real GUI
  user. Add the expiry reaper. This closes the gap that no external system can.
- **Phase 2 — central AAA login accounting (conditional).** Only relevant when
  the GUI authenticates via **RADIUS/TACACS+** and the customer wants the GUI
  session's start/stop on the AAA server. The broker opens/closes a real PAM
  session so `pam_radius`/`pam_tacplus` emit it. **Not applicable to
  SAML-authenticated GUIs** — SAML login accounting already lives at the IdP
  (§0.5/§2.1), so there is nothing to add there.
- **Phase 3 — identity convergence + platform-wide command accounting.** Route
  GUI auth through the box's real identity sources (retire the shadow `User`
  table), and — if desired — stand up `audisp-tacplus` per-command accounting,
  which is currently unconfigured for *every* method, not just the GUI.

### 7.1 Scoping questions that size the effort
1. Is device-side parity (Phase 1: `who`/`last`/journald + commit-log
   attribution) sufficient for v1, or is central **RADIUS/TACACS+** login
   accounting for the GUI also required (Phase 2, and only when the GUI uses
   those methods)?
2. Are you willing to **drop the private `User` table** and route GUI auth
   through the box's real identity sources?
3. Is **platform-wide per-command accounting** (audisp-tacplus — new for every
   method) in scope, or only session presence + change attribution?

Answers to (1)–(3) determine whether this is a few weeks (Phase 1 only) or a
quarter (all three phases).

---

## 8. Affected components (reference)

- `vyos-1x`
  - `op-mode-definitions/show-users.xml.in` — unchanged under the broker
    approach (benefits automatically); listed for traceability.
  - New host-side **accounting broker** service + packaging (Debian).
  - A dedicated login-class **PAM service** definition for GUI sessions.
  - `python/vyos/config_mgmt.py` — Phase 1 commit-log attribution of
    GUI-initiated changes to the real user.
- `flask-react-dev`
  - `flask/app/auth/web_sessions.py` — emit login/logout/expiry events to the
    broker from `start_web_session` / `revoke_web_session` / expiry cleanup.
  - `flask/app/auth/routes.py` — for Phase 3, identity-source convergence.
- Host / deployment
  - GUI→broker transport (Unix socket / localhost endpoint) and its access
    controls; container bind-mount if a socket is used.

---

## 9. Non-goals (for this document)

- A separate "GUI sessions" CLI view as the *primary* surface (explicitly
  rejected in §4; it institutionalizes the split).
- Re-architecting the GUI into a thin front-end that proxies OS logins directly
  (possible in theory, out of scope given the existing app).
- Changing the semantics of `who`/`last`/`show users` themselves.

---

## 10. Command / config accounting — complexity & implementation sketch

This chapter scopes how hard it is to add **command** and **config-change**
accounting, independent of (but complementary to) the session-visibility broker
in §5. The headline: **config-change accounting is cheap because the machinery
already exists; command accounting is genuinely greenfield for the whole
platform.** The cost splits along two axes — *config vs command* and *local vs
central-AAA*.

### 10.1 Grounding facts that set the cost

- **Config changes have a single choke point: the commit.** The CLI and the
  HTTP API/GUI both mutate through `python/vyos/configsession.py` →
  `cli-shell-api`/vyconf → **commit**, and a post-commit hook already writes the
  commit log for **every** commit from **every** channel
  (`python/vyos/config_mgmt.py`). A per-change audit trail therefore already
  exists (`show system commit` + `show system commit diff`).
- **The only real defect is attribution.** `_new_log_entry()` takes `commit_via`
  and `commit_comment` from **env vars** (`COMMIT_VIA`, `COMMIT_COMMENT`) but
  derives the **user** from `_get_user()` (`getlogin()`/`geteuid()`). That is why
  GUI/API changes are logged as `by igos`. There is **no `COMMIT_USER` env**
  today — adding one is the whole fix.
- **No central command logging exists.** Op-mode commands run via `vbash`; only a
  handful of scripts make ad-hoc `syslog` calls. There is no systematic
  per-command record.
- **`audisp-tacplus` is not packaged.** Only `auditd` is a dependency
  (`debian/control`); central TACACS+ command accounting would be new packaging,
  audit rules, plugin config, and a CLI knob.

### 10.2 Complexity, graded

| Deliverable | Complexity | Why |
|---|---|---|
| **Config-change accounting, correctly attributed, local** — fix the `by igos` problem; optionally mirror commit entries to syslog/journald | **Low — days** | Commit log already fires on every commit. Add a `COMMIT_USER` env override preferred by `_new_log_entry` (mirrors existing `COMMIT_VIA`); have `configsession`/the GUI pass the real actor. The GUI already knows it (`web_sessions`). |
| **Per-`set`/`delete` granularity (with values), local** | **Low–Medium — 1–2 wks** | Do not hook every entry point; the **commit diff is already computed** — emit it (the real deltas) to syslog with user+via at the post-commit hook. Uniform across CLI/API/GUI because it happens at commit. |
| **Local command accounting (op-mode/shell → syslog/journald)** | **Medium — 2–4 wks** | No central runner today. Options: auditd `execve` rules (noisy, all-exec), a shell hook (CLI-only, misses API), or hook the op-mode dispatch. Fidelity-vs-coverage tradeoffs. |
| **Central TACACS+ command accounting (`audisp-tacplus`)** | **Medium–High — 1–2 mo** | Package audisp-tacplus, generate auditd rules + plugin config (sourcing `/etc/tacplus_servers`), add `system login tacacs … command-accounting` + conf_mode. Covers **shell** execs only — **not** GUI/API config changes (not `execve`'d), which need a separate record path. |

### 10.3 The GUI wrinkle (why config accounting is the smart first target)

For the GUI, "commands" are not shell commands — they are API calls / config
changes. So GUI command/config accounting **collapses to the config-change row**:
attribute the change to the real GUI user. That identity already exists
(`web_sessions`), already flows through `configsession`, and already lands in a
log that fires on every commit. So the single highest-value, lowest-cost move —
fixing commit-log attribution (+ optionally emitting the diff to syslog) —
simultaneously:

- fixes the `by igos` misattribution,
- gives per-change accounting for the GUI, and
- works for CLI and API too, for free.

That is **days of plumbing on existing machinery**, not new accounting
infrastructure.

### 10.4 Recommended first slice

1. Add an optional `COMMIT_USER` env var, honoured by `_new_log_entry()` in
   `python/vyos/config_mgmt.py` (prefer it over `_get_user()` when set) — exactly
   parallel to how `COMMIT_VIA`/`COMMIT_COMMENT` already work.
2. Have `python/vyos/configsession.py` accept the real actor and inject it into
   the session env (the HTTP API server / GUI passes the authenticated GUI user;
   the GUI already has it in `web_sessions`).
3. (Optional) At the post-commit hook, emit the already-computed commit **diff**
   plus `{user, via, timestamp}` to syslog/journald for off-box SIEM ingestion.

This delivers correctly-attributed per-change config accounting across **all**
channels (CLI, API, GUI) and is the cheapest, highest-value increment. It is
independent of the session-visibility broker (§5) and can ship first.

### 10.5 Explicitly heavier, separate efforts

- **Local command (op-mode) accounting** and, above it, **central TACACS+
  command accounting via `audisp-tacplus`** are **greenfield for every auth
  method**, not just the GUI. They should be scoped and sold as their own
  features, not framed as "catching the GUI up." The shell path alone does not
  cover GUI/API config changes, so GUI parity there still routes back to §10.4.

  > Note: **RADIUS is intentionally omitted here.** RADIUS accounting is
  > session-oriented (login/logout only — see §0.5) and has no per-command /
  > per-config-change model, so it is not a mechanism for change/command
  > accounting. Central change/command accounting means **TACACS+**; for
  > RADIUS/SAML deployments the central record for config changes is
  > **syslog/SIEM** (the §10.4 diff-to-syslog feed).
