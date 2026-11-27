# igOS GUI User Accounting — Design Sketch

Status: design draft (no implementation yet)

Scope: make web GUI sessions first-class citizens of the box's device-side
accounting, so that a user who authenticates through the HTTPS GUI is recorded
and visible like a console / serial / SSH user — in `show users`,
`show users recent` (`who`/`last`), the local audit log, and the commit log
(correctly attributed) — and, only where that method is actually in use, in the
central TACACS+/RADIUS accounting server.

The target is the **device-side** accounting that only the box can produce
(on-device presence + change attribution), not re-emitting login accounting
that already lives at the IdP (SAML) or AAA server (TACACS+/RADIUS) — see §2.1.

Today the GUI runs its own authentication plane that is disjoint from the OS
login plane, so none of the standard device accounting tools can see GUI users.
This document explains why that happens, why it is a real product/compliance
gap, and the architecture required to close it.

This is a cross-repo effort spanning the GUI (`flask-react-dev`) and this
repository (`vyos-1x`, plus a small privileged host-side component).

---

## 0. Context / grounding facts

The following were verified by reading the code in both repositories and by
inspecting a running reference unit. File references are to the state of the
repositories at the time of writing.

### 0.1 How `show users` works today (`vyos-1x`)

`op-mode-definitions/show-users.xml.in`:

- `show users` runs `who -H`, which reads `/var/run/utmp`.
- `show users recent [N]` runs `last -aF -n N`, which reads `/var/log/wtmp`.

`utmp`/`wtmp` are the login accounting databases. They are written by the
**login program** that establishes an interactive TTY/PTY session — `login`
(console/serial, via `getty`) or `sshd` — as part of setting up that session,
after PAM authentication and the PAM `session` phase have run. PAM auth modules
do not write them. No login program ⇒ no record ⇒ invisible to `who`/`last`.

> Verify on the target Debian base: newer Debian releases are moving `last` to
> `wtmpdb` and can build coreutils `who` against systemd-logind instead of
> `utmp`. Whatever `show users` actually reads on the shipping image is what
> the broker (§5) must write.

### 0.2 The GUI authenticates at the application layer (`flask-react-dev`)

`flask/app/auth/routes.py` shows the GUI is its own identity/authentication
provider. The active `POST /login` path tries, in order:

- `authenticate_db` — validates against a private `User` table (email + bcrypt
  hash). These GUI users **do not exist as Unix accounts at all**. Recorded
  `auth_source`: `local`.
- `login_pam` — runs host PAM (service `login`) in an isolated worker
  (`flask/app/auth/pam/`), relaying OTP/MFA prompts to the browser. It calls
  `pam_authenticate`, `pam_acct_mgmt` and `pam_setcred` only — it never calls
  `pam_open_session`/`pam_close_session`, never allocates a TTY, never spawns a
  shell. Because this goes through the host PAM stack, it is also how local,
  RADIUS and TACACS+ users authenticate to the GUI. Recorded `auth_source`:
  `pam`.

Other entry points: SAML (`saml_validate`, `auth_source` `saml`) and cloud
(`cloud_auth.py`, `auth_source` `cloud`). Older helpers in `routes.py`
(`authenticate_ssh`, `login_mfa` which drives `sudo login`, and
`login_mfa_paramiko`) are not reachable from the current `/login` route.

On success the app issues a web session, tracked in a durable store (§0.3) plus
a Flask cookie. At no point is there a TTY, a login program, a PAM session, or a
Unix shell — so there is nothing for `who`/`last` to record.

### 0.3 The GUI already has a durable session store (`flask-react-dev`)

`flask/app/auth/web_sessions.py` defines a SQLAlchemy model `WebSession`
(table `web_sessions`):

| Column | Meaning |
|---|---|
| `id` | session UUID |
| `username` | authenticated principal |
| `permission` | operator / operator+ / administrator |
| `auth_source` | `local` / `pam` / `saml` / `cloud` |
| `cloud_sid` | cloud session id (cloud logins only) |
| `ip_address` | client IP (proxy-aware, `client_ip_address()`) |
| `device` | parsed from User-Agent |
| `authenticated_at` | login timestamp |
| `expires_at` | expiry timestamp |
| `revoked_at` | logout / revoke timestamp |

Helper functions:

- `start_web_session(auth_source)` — login hook. Revokes the browser's previous
  session (if any), **deletes** rows with `expires_at < now`, and creates the
  new row.
- `revoke_web_session(session_id)` — logout/revoke hook (sets `revoked_at`).
- `list_web_sessions()` — returns active sessions
  (`revoked_at IS NULL AND expires_at > now`).
- HTTP surface: `GET /sessions` and `DELETE /sessions/<id>`.

Two lifecycle facts matter for accounting:

- **Expiry produces no event.** Expired rows are not marked; they are deleted,
  and only lazily, the next time *someone* logs in. There is no scheduled
  expiry job.
- **Sessions are long.** DB users default to an 8-hour session (or a
  per-user custom expiry); other sources use the app's
  `permanent_session_lifetime`. There is no idle timeout tied to the record.

Deployment on the device (`builds/FILES/DEBIAN/flask.service`,
`builds/FILES/env.conf`): the GUI is the `flask-react-web` Debian package,
running as a **host systemd service** (gunicorn, `User=root`, bound to
`127.0.0.1:5000` behind nginx) with a local **SQLite** database. The
`docker-compose*.yml` files are development setups only. The GUI therefore
*could* write host `utmp`/`wtmp` itself; §5 explains why it should not.

### 0.4 Config-change accounting is also wrong for GUI users (`vyos-1x`)

The GUI drives configuration through the VyOS HTTP API (`/graphql`,
`/config-file`), authenticating every request with **one shared API key**
(`VYOS_APIKEY`). The API therefore has no idea which GUI user a request is for.

The commit log (`show system commit`) is written by
`python/vyos/config_mgmt.py:commit_revision()`, a post-commit hook installed
only when `system config-management commit-revisions` is configured.
`_new_log_entry()` records:

- `user` from `_get_user()` — `os.getlogin()`, falling back to the name of the
  effective UID. The `vyos-http-api` unit runs as `User=root`, so API commits
  are attributed to the API server's own OS identity (in practice `root`),
  never the GUI user.
- `via` from the `COMMIT_VIA` env var, which `ConfigSession` sets to its `app`
  argument (default `vyos-http-api`).

So `show system commit` records **that** the HTTP API made a change, never
**which GUI user** did. This is the same two-planes problem applied to config
accounting.

### 0.5 What accounting actually fires today, per auth method (`vyos-1x`)

Verified from `src/pam-configs/*`, `data/templates/login/*`, `debian/control`,
and the SAML package's `pam-configs/saml_auth`. "Remote AAA login acct" means a
TACACS+/RADIUS accounting start/stop sent off-box for the login session.

| Auth method (access path) | `who`/`last` (utmp/wtmp) | journald / auth.log | Remote AAA login acct | Per-command acct | Commit-log change record¹ |
|---|:--:|:--:|:--:|:--:|:--:|
| Local `pam_unix` (console/SSH) | yes | yes | n/a (no server) | no | yes, correct user |
| RADIUS (console/SSH) | yes | yes | yes — `pam_radius_auth.so` in `Session:` | no | yes |
| TACACS+ (console/SSH) | yes | yes | yes — `pam_tacplus.so` in `Session:`, `acct_all=1` | no (audisp referenced, unwired) | yes |
| SAML (console/serial, backed by real/JIT user) | yes | yes | no — `saml_auth` is `Auth:` only | no | yes |
| SAML via GUI | no | no² | no | no | misattributed |
| GUI (local DB or PAM) | no | no² | no | no | misattributed |

¹ Only when `system config-management commit-revisions` is configured.
² `login_pam` runs host PAM auth, so PAM modules may log the *authentication*
attempt to auth.log; nothing records the web session's start or end.

Key facts behind the table:

- **`utmp`/`wtmp` are written by the login program, not the auth module**
  (§0.1). All OS logins land in `who`/`last` regardless of backend. The GUI is
  the only access path with no login program.
- **RADIUS and TACACS+ send login accounting from the PAM `Session` phase.**
  Both `*-optional` and `*-mandatory` profiles include a `Session:` stanza.
  That stanza is guarded by `pam_succeed_if.so user notingroup radius|tacacs`,
  so it only fires for users that NSS resolves into the `radius`/`tacacs`
  group (the mapped users VyOS creates for those methods).
- **SAML has no Session phase.** The shipped `saml_auth` profile is `Auth:`
  only. This is expected: SAML is a browser-SSO authentication protocol, and
  the IdP is the system of record for SAML authentication events. Device-side
  SAML *login* accounting is not a target.
- **Per-command accounting is unconfigured platform-wide.** `debian/control`
  installs `auditd`, but nothing under `src/` generates audit rules or an
  `audisp-tacplus` configuration; `tacplus_servers.j2` only mentions
  audisp-tacplus as a potential consumer.
- **The only per-change trail is the local commit log** (§0.4): commit-granular,
  local (optionally archived off-box via `system config-management
  commit-archive`), and, for the GUI/API, attributed to the API's OS user.

### 0.6 Idle logout on the OS plane today (`vyos-1x`)

- **`set system login timeout <5–604800>`** (seconds) is the only idle-logout
  control. `src/conf_mode/system_login.py` renders it into
  `/etc/profile.d/autologout.sh` (`data/templates/login/autologout.j2`), which
  exports a `readonly TMOUT`. It therefore applies to every interactive login
  shell — console, serial, and SSH alike.
- **Off by default.** No default value and nothing in the default config sets
  it, so out of the box no channel has an idle logout.
- **Prompt-idle semantics.** Bash `TMOUT` fires only while the shell is idle at
  a prompt; a session sitting in `monitor …`, a pager, or an editor never times
  out. Non-interactive SSH (`ssh host cmd`), SFTP/SCP, and forward-only
  (`ssh -N`) sessions are not covered.
- **`set system login max-login-session`** (per-user concurrent session cap,
  via `limits.d`) requires `login timeout` to be configured
  (`system_login.py` verify).
- **SSH has no idle disconnect of its own.** `service ssh
  client-keepalive-interval` sets `ClientAliveInterval`, which detects a *dead*
  client (a live client answers the probes), not an idle user;
  `ClientAliveCountMax` is not exposed (sshd default 3). `LoginGraceTime 120`
  is hard-coded and bounds authentication only.

The GUI's session lifetime (§0.3) is independent of all of this: a GUI session
lives until logout or its 8 h-or-longer expiry regardless of
`system login timeout`.

---

## 1. Problem statement

The box has two parallel identity + accounting planes:

1. **OS/PAM plane** — console / serial / SSH → login program + PAM session →
   `utmp`/`wtmp` + journald + (if configured) TACACS+/RADIUS accounting →
   `show users` / `last` / central AAA server.
2. **GUI application plane** — browser → Flask app auth → `web_sessions` row +
   cookie. Touches none of the OS accounting sinks.

Nothing bridges them. Consequently:

- `show users` / `show users recent` never show GUI users.
- The local audit log has no GUI session start/end events.
- `show system commit` misattributes GUI config changes to the API's OS user.
- A central TACACS+/RADIUS accounting server never receives a start/stop record
  for GUI admin sessions — where that method is the one in use. (For SAML GUIs
  this does not apply; SAML login accounting lives at the IdP — see §2.1.)

### 1.1 Why this is a product/compliance gap, not cosmetics

For any customer that enforces centralized administrative access control
(enterprise, carrier, regulated environments), administrative access must be
accountable regardless of channel. A web GUI with its own login that the
device's accounting cannot see is a classic audit finding: "unaccounted
administrative access path." It also undermines incident response ("who was on
the box at 02:00?") and change attribution ("who made this change?").

The requirement, narrowed by §2.1: **GUI sessions and GUI-initiated changes
must be visible and correctly attributed on the device itself, to the same
degree an equivalent OS login already is.**

---

## 2. What "the same accounting" actually means

The login accounting pipeline has these sinks:

1. `utmp` / `wtmp` → `who` / `last` / `show users` / `show users recent`.
2. journald / auth.log (authpriv) → local audit log.
3. Central AAA accounting — TACACS+/RADIUS accounting start/stop when
   configured (and, ideally, command/config accounting).

systemd-logind (`loginctl`) is deliberately **not** a target: `pam_systemd`
binds a logind session to the calling process, and a process can belong to only
one session, so a single service cannot represent many concurrent web sessions
there. (If the shipping `who` reads logind rather than `utmp` — see §0.1 — this
must be revisited.)

`show users` is the most visible sink; (2) is the local audit trail; (3) is
what enterprise buyers mean by "accounting." See §0.5 for which sinks each
existing auth method feeds.

### 2.1 What the device owes vs. what the IdP already owns

| Question | Authoritative source | On a SAML box | Device gap for GUI? |
|---|---|---|---|
| Who authenticated? | The IdP (or TACACS+/RADIUS for those methods) | IdP logs it | No — covered off-box |
| Who was on the device, and for how long? | The device (`who`/`last`, audit log) | device only | **Yes** — web sessions are invisible |
| Who changed what on the device? | The device (commit log + attribution) | device only | **Yes** — API/GUI changes misattributed |

The gap is therefore not "the device must emit a login-accounting record for
every auth method." For authentication, the IdP and AAA server already are the
systems of record. The gap is the device-side activity accounting that no
external system can provide: that a web session was established, by whom, from
where, for how long, and what it changed.

This is also why a SAML-centric design does not get accounting for free. The
bridge below is required regardless of whether the GUI authenticates via the
local DB, PAM (local/RADIUS/TACACS+), SAML, or cloud.

---

## 3. Root cause

A GUI login is an application-level login, not an operating-system login. The
OS records sessions because a login program establishes a TTY session and
writes the accounting records. The GUI validates credentials in-process and
issues a cookie-backed web session. Specifically:

1. **It is a web app, not a shell.** HTTP has no terminal line; `utmp`/`wtmp`
   are built around "user attached to a TTY."
2. **Its users are not necessarily OS users.** The private `User` table (and
   cloud identities) means there is often no Unix account to record.
3. **It never runs a login program or a PAM session.** `login_pam` runs PAM
   `auth`/`account`/`setcred` only. Login accounting for RADIUS/TACACS+ is
   produced by the PAM `session` phase (`pam_open_session`/`pam_close_session`),
   which the GUI never runs.

---

## 4. Design principle

**Do not institutionalize the split.** A separate `show users web` view would
make the two-plane divide a permanent product behavior. The right answer is to
bridge GUI sessions into the real device-side accounting sinks so that
`show users`, `last`, the audit log, and the commit log reflect GUI users with
no special-case command.

Scope the bridge to what only the device can provide (§2.1): on-device session
presence and correct change attribution. That does **not** require a PAM
session:

- **Presence** is recorded by writing `utmp`/`wtmp` directly (`pututxline` /
  `updwtmpx`, i.e. what `login`/`sshd` do) plus a structured journald/authpriv
  entry. This works for every GUI user, including DB users with no Unix
  account.
- **Central AAA login accounting** (optional, §7 Phase 2) is the only part that
  needs a real PAM session, because that is where `pam_radius_auth`/
  `pam_tacplus` emit start/stop. It only applies when the GUI user is a
  RADIUS/TACACS+ user.

---

## 5. Proposed architecture: host-side accounting broker

```
  Browser ─HTTPS─> nginx ─> Flask GUI (host service)       vyos-1x broker (root)
                            │  authenticates user              │
   login  ──────────────────┼─► event over Unix socket ───────┼─► validate peer + event
                            │                                  │   ├─ utmp USER_PROCESS + wtmp login
                            │                                  │   ├─ journald/authpriv audit line
                            │                                  │   └─ [Phase 2] pam_open_session
                            │                                  │        → RADIUS/TACACS+ acct START
   logout / revoke /        │                                  │
   expiry (reaper) ─────────┼─► event over Unix socket ───────┼─► utmp DEAD_PROCESS + wtmp logout
                            │                                  │   ├─ journald audit line
                            │                                  │   └─ [Phase 2] pam_close_session
                            │                                  │        → acct STOP
```

Responsibilities:

- **GUI (`flask-react-dev`)** stays the authenticator. On `start_web_session`,
  `revoke_web_session`, and expiry, it emits an event:
  `{action, session_id, username, auth_source, ip_address, device, timestamp}`.
  Expiry needs new code: today expired rows are silently deleted (§0.3), so a
  reaper must emit `end` for each expired session *before* deleting it.
- **Broker (`vyos-1x` + Debian packaging)** is a small privileged service that
  owns the device-side records: writes/removes the `utmp` entry, appends
  `wtmp` login/logout, logs to journald, and (Phase 2) holds the PAM handle for
  the session's lifetime. It is the single integration point with AAA.

With the broker in place, `show users` itself does not change.

### 5.1 Why a separate broker, given the GUI already runs as root on the host

The GUI process could write `utmp`/`wtmp` itself. It should not, because:

- **Wrong layer.** Device accounting semantics would live in the GUI package
  — the anti-pattern in §11. A vyos-1x broker can also serve other non-TTY
  access paths (e.g. future per-user HTTP API keys).
- **Lifetime.** For Phase 2, the accounting STOP is sent by
  `pam_close_session` on the same PAM handle that sent START (the modules keep
  per-session state with the handle/process). Gunicorn restarts would lose
  every open handle and no STOP would be sent.
- **Least privilege.** Keeping the privileged writer separate makes it
  possible to drop the GUI's `User=root` later.

### 5.2 Transport and trust

Accounting records are only worth something if they cannot be forged. The
broker must therefore authenticate its caller, not just accept well-formed
events:

- Use a Unix-domain socket (e.g. under `/run/vyos/`), mode `0660`, owned by a
  dedicated group that only the GUI service is in.
- Check the caller with `SO_PEERCRED` against the expected service UID/GID.
- Accept `end` only for a `session_id` the broker has open; reject duplicate
  `start` for an open `session_id`; bound the number of open sessions and rate
  limit.
- Never expose the channel through nginx or on a TCP port.

A localhost HTTP endpoint or a spooled event file are possible but weaker
(no peer credentials; file spool adds latency and its own cleanup) and are not
recommended.

### 5.3 Record conventions

- `ut_type` `USER_PROCESS` on start, `DEAD_PROCESS` on end; matching `wtmp`
  records so `last` shows duration.
- `ut_line`: synthetic `web/<n>` (unique per open session); `ut_id`: derived
  from `<n>`.
- `ut_host`: client IP. `ut_user`: GUI username — note `ut_user` is 32 bytes,
  so long email-style DB usernames are truncated; the full name and
  `session_id` go in the journald record.
- `ut_pid`: the broker's PID. Coreutils `who` hides `USER_PROCESS` entries
  whose PID is not alive, so a live PID is required; it also means entries
  disappear from `who` if the broker dies (desired — the broker reconciles on
  restart, below).

### 5.4 Lifecycle and reconciliation

- **What counts as "on the box".** A web session row can live 8 h or more
  with the user long gone. Define presence as the web session *plus* an idle
  timeout (e.g. no authenticated request or websocket activity for N minutes),
  and emit `end` when it lapses.
- **Idle timeout source — reuse `system login timeout` (§0.6).** Do not add a
  GUI-only knob. When `system login timeout` is set, the GUI ends (logs out and
  revokes) a web session after that many seconds without user activity, and
  the broker records the `end`; one setting then governs idle logout on every
  channel, which is what an auditor checking "idle sessions are terminated"
  expects. When it is unset, keep today's behavior (session expiry only) to
  match the CLI default. Two semantic notes:
  - "Activity" must mean **user-initiated** requests. Background polling and
    websocket keepalives from an open tab must not reset the timer, or an
    abandoned browser stays logged in forever — the web analogue of `TMOUT`
    not firing inside `monitor`, but avoidable here.
  - The equivalence is approximate: `TMOUT` measures time idle at a prompt,
    the GUI measures time since the last user request. Document it as
    "same setting, per-channel meaning of idle."
- **Multiple sessions per user** are normal (several browsers); each gets its
  own `utmp` line.
- **Broker restart / crash.** On start, the broker rewrites `utmp` from the
  GUI's list of active sessions (request a resync over the same socket) and
  closes any stale lines with a `wtmp` logout.
- **GUI restart / crash.** The GUI resyncs on startup; sessions it no longer
  considers active are ended.
- **Reboot.** `utmp` lives on tmpfs and is cleared; `last` already marks open
  sessions at a reboot record.

---

## 6. Hard problems / open decisions

1. **GUI users that are not system users.** `authenticate_db` uses a private
   `User` store. Phase 1 can still record them (utmp/wtmp do not need a UID),
   but an AAA server cannot account a principal it has never heard of. The
   stronger product position is to converge GUI auth onto the box's real
   identity sources (local VyOS users, RADIUS, TACACS+, SAML) and retire the
   shadow `User` table. *Decision needed: keep the private user store (and
   accept local-only accounting for it) vs. converge on system identity
   sources.*

2. **Reliable end of session.** Web sessions end on logout, revoke, expiry, or
   idleness, and today only logout/revoke produce an event (§0.3). A reaper and
   an idle definition (§5.4) are required so sessions do not stay "logged in"
   forever. Proposed idle source: `system login timeout` (§0.6, §5.4).
   *Decision needed: when `system login timeout` is unset, rely on session
   expiry only (CLI parity), or apply a GUI default idle timeout anyway
   (stricter than the CLI); and whether the GUI should also honour
   `max-login-session`.*

3. **Trusted actor identity for config changes.** The GUI calls the HTTP API
   with one shared key (§0.4), so the API cannot know or verify the real user.
   Any fix has the GUI *assert* the actor on each request. That assertion is
   only as trustworthy as the key: the API should accept an actor only from an
   API key that is marked as allowed to assert one (the GUI's), and record
   both — e.g. `by alice via gui` — so a different key holder cannot
   impersonate a GUI user. *Decision needed: accept asserted identity from a
   designated key, or move to per-user API credentials.*

   **Resolved (2026-11-27): root-only local API socket.** The GUI and the
   cloud agent call the API over a second socket, `/run/api-local.sock`
   (owner root, mode `0600`, never exposed through nginx or TCP), with no
   key. The API accepts an asserted actor (`X-Igos-User`,
   `X-Igos-Via: gui|cloud`) **only** on that socket, so only root processes
   can assert one, and callers through nginx cannot. No designated key is
   needed, and the shared GUI key goes away (security findings G1;
   `igos-cloud-connectivity-design.md` §11.2). This is the same trust pattern
   as the broker socket (§5.2). It settles *who may assert an actor*; the
   per-commit plumbing in §10.4 is still required.

4. **Command / config accounting.** Per-command accounting is unconfigured
   for every method (§0.5). The realistic device-only win is correct commit-log
   attribution for GUI changes (§10.4). Full per-command TACACS+ accounting is
   separate platform work. *Decision needed: session presence + commit
   attribution only, or also platform-wide per-command accounting?*

5. **utmp/wtmp vs. the shipping base.** Confirm what `who` and `last` read on
   the target image (§0.1) before building the writer.

---

## 7. Scope / phasing

- **Phase 1 — device-side parity (the real target).** GUI→broker events,
  broker writing `utmp`/`wtmp` + journald with peer authentication, reaper +
  idle timeout + reconciliation, and correct commit-log attribution for
  GUI-initiated changes (§10.4). Result: `show users`, `show users recent`,
  local audit logs, and `show system commit` reflect the real GUI user. No PAM
  session is needed for this phase.
- **Phase 2 — central AAA login accounting (conditional).** Only when the GUI
  user authenticated via RADIUS/TACACS+ (`auth_source` `pam` and the user
  resolves into the `radius`/`tacacs` group) and the customer wants GUI
  start/stop on the AAA server. The broker opens a PAM session on a dedicated
  service and keeps the handle until the session ends. Not applicable to SAML,
  cloud, or DB users.
- **Phase 3 — identity convergence + platform-wide command accounting.** Route
  GUI auth through the box's real identity sources (retire the shadow `User`
  table), and optionally stand up `audisp-tacplus` per-command accounting for
  every method.

### 7.1 Scoping questions that size the effort

1. Is Phase 1 sufficient for v1, or is central RADIUS/TACACS+ login accounting
   for the GUI also required (Phase 2)?
2. Are you willing to drop the private `User` table and route GUI auth through
   the box's real identity sources?
3. Is platform-wide per-command accounting in scope, or only session presence +
   change attribution?

Answers determine whether this is a few weeks (Phase 1) or a quarter (all three
phases).

---

## 8. Affected components (reference)

- `vyos-1x`
  - `op-mode-definitions/show-users.xml.in` — unchanged; listed for
    traceability.
  - New host-side accounting broker service + socket + Debian packaging.
  - Phase 2: a dedicated PAM service definition for GUI sessions.
  - `python/vyos/config_mgmt.py` — honour an explicit commit actor.
  - `python/vyos/configsession.py`, `src/services/vyos-http-api-server` (and
    its GraphQL session handling) — accept and pass the actor per commit.
  - `src/services/vyos-commitd` / commit protobuf — carry the actor on the
    vyconf backend (§10.4).
- `flask-react-dev`
  - `flask/app/auth/web_sessions.py` — emit start/end events; expiry reaper;
    idle tracking (timeout read from `system login timeout`, §5.4); resync on
    startup.
  - HTTP API client (`flask/app/core/api/http/`) — send the acting user with
    config requests.
  - `flask/app/auth/routes.py` — Phase 3 identity convergence.
- Host / deployment
  - Broker socket group membership for the GUI service; GUI calls the API on
    the root-only local API socket, which is the only place an asserted actor
    is accepted (§6.3).

---

## 9. Non-goals (for this document)

- A separate "GUI sessions" CLI view as the primary surface (rejected in §4).
- Re-architecting the GUI into a thin front-end that proxies OS logins.
- Changing the semantics of `who`/`last`/`show users` themselves.
- systemd-logind sessions for web users (§2).

---

## 10. Command / config accounting — complexity & implementation sketch

This chapter scopes command and config-change accounting, independent of (but
complementary to) the session broker in §5. Config-change accounting is cheap
because the commit log already exists; command accounting is greenfield for the
whole platform.

### 10.1 Grounding facts that set the cost

- **Config changes have a single choke point: the commit.** The CLI (vbash →
  `my_set`/`my_delete`/`my_commit`) and the HTTP API/GUI (`ConfigSession`,
  which wraps the same `cli-shell-api`/vyconf operations) all end in a commit,
  and the `commit_revision` post-commit hook writes the commit log for every
  commit from every channel — when `commit-revisions` is configured (§0.4).
- **The defect is attribution.** `_new_log_entry()` takes `via` and comment
  from env vars (`COMMIT_VIA`, `COMMIT_COMMENT`) but derives the user from
  `_get_user()`. There is no way today to tell it the real actor.
- **The API has no actor to pass.** The GUI uses one shared API key (§0.4), so
  the actor must be added to the GUI→API requests as well (§6.3).
- **The API server shares one config session.** `vyos-http-api-server` creates
  a single `ConfigSession(os.getpid())`, and `ConfigSession`'s environment is
  the process's own `os.environ`. Setting a per-request env var there is a
  process-global mutation and can misattribute concurrent requests (see
  `igos-http-api-concurrency-design.md`).
- **Two commit backends.** On the legacy backend the post-commit hook inherits
  the session environment. On the vyconf backend, `vyos-commitd` builds its
  environment from the commit message, which identifies the caller only by
  `session_pid`, `sudo_user` and `user` — an env var set in the API process
  does not reach it.
- **No central command logging exists.** Op-mode commands run via `vbash`; only
  a handful of scripts make ad-hoc `syslog` calls.
- **`audisp-tacplus` is not packaged.** Only `auditd` is a dependency.

### 10.2 Complexity, graded

| Deliverable | Complexity | Why |
|---|---|---|
| Config-change accounting, correctly attributed, local | Low–Medium — 1–2 wks | Commit log already fires. Needs: actor field on GUI→API requests, trusted on the local API socket (§6.3); explicit actor in `config_mgmt`; per-commit (not process-global) plumbing in the API server; carrying it through `vyos-commitd` on the vyconf backend. |
| Per-`set`/`delete` granularity (with values), local | Low–Medium — 1–2 wks | The commit diff is already computed; emit it to syslog with user+via from the post-commit hook. Uniform across CLI/API/GUI. |
| Local command accounting (op-mode/shell → syslog) | Medium — 2–4 wks | No central runner. Options: auditd `execve` rules (noisy), a shell hook (CLI-only), or hooking op-mode dispatch. |
| Central TACACS+ command accounting (`audisp-tacplus`) | Medium–High — 1–2 mo | Package audisp-tacplus, generate audit rules + plugin config (sourcing `/etc/tacplus_servers`), add a CLI knob + conf_mode. Covers shell execs only — not GUI/API config changes. |

### 10.3 The GUI wrinkle

For the GUI, "commands" are API calls / config changes, so GUI command/config
accounting collapses to the config-change row: attribute the change to the real
GUI user. The GUI knows the user (`web_sessions`); the missing pieces are
getting that identity to the API in a trusted way and threading it through to
the commit-log writer on both backends. Doing so:

- fixes the API-user misattribution,
- gives per-change accounting for the GUI, and
- gives the same mechanism to the cloud agent (cloud users attributed as
  `<user> via cloud`) and any other root-only local caller.

### 10.4 Recommended first slice

1. **Commit log.** Let `_new_log_entry()` take the actor explicitly (it already
   accepts `user=`), with an optional `COMMIT_USER` env fallback that is
   preferred over `_get_user()` when set — parallel to `COMMIT_VIA`.
2. **API server.** Accept an optional actor on config-changing requests,
   honour it only on the root-only local API socket (§6.3), and set it for the
   duration of that one commit under the API's commit lock (or pass it
   explicitly) rather than leaving it in the shared process environment.
   Record `via` from `X-Igos-Via` (`gui` or `cloud`).
3. **vyconf backend.** Carry the actor on the session/commit message (e.g. a
   new optional field alongside `user`/`sudo_user`) and have `vyos-commitd`
   expose it to the post-commit hook.
4. **GUI.** Send the authenticated username with every config-changing API
   call.
5. *(Optional)* In the post-commit hook, emit the commit diff plus
   `{user, via, timestamp}` to syslog/journald for off-box SIEM ingestion.

This is independent of the session broker (§5) and can ship first.

### 10.5 Explicitly heavier, separate efforts

Local command (op-mode) accounting and central TACACS+ command accounting via
`audisp-tacplus` are greenfield for every auth method, not just the GUI. They
should be scoped as their own features. The shell path alone does not cover
GUI/API config changes, so GUI parity there still routes back to §10.4.

> RADIUS is intentionally omitted here. RADIUS accounting is session-oriented
> (login/logout) with no per-command model. Central change/command accounting
> means TACACS+; for RADIUS/SAML deployments the central record for config
> changes is syslog/SIEM (the §10.4 diff-to-syslog feed).

---

## 11. Broader pattern: device concerns in the GUI plane (cross-cutting)

The accounting gap is one instance of a single architectural decision: the GUI
was built as a parallel identity + control plane (§1, §3) rather than as a
frontend that drives vyos-1x's existing planes. The same root cause produces a
recurring class of defects whenever a device-level concern is implemented in
the GUI plane instead of in the shared core that all access paths use.

### 11.1 Instances of the same flaw

| Concern | How it lives in the GUI plane | Failure mode | Tracked where |
|---|---|---|---|
| First-boot onboarding (force off the default admin credential, apply the password policy, set timezone) | Logic in `perle_igos_onboarding_query.py` / `_mutation.py` in the `vyos-custom-queries` package (built from `flask-react-dev/perle-graphql`). Enforced by the React `OnboardingGuard`, and for CLI by the separate optional `igos-cli-onboarding` package (a `/etc/profile.d` hook that runs an interactive wizard on login). "Required" is inferred from `system time-zone` being unset, not a real completion flag. | Enforcement exists only where those GUI-repo packages are installed, and the CLI hook only fires for interactive login shells. An image without them — or access that does not start an interactive shell — can run indefinitely on the default `igos/igos` credential, at odds with the FIPS posture of `IGOS_SECURE=1` builds (implemented — see `igos-fips-140-3-design.md`). Setting a timezone by any other means also marks onboarding complete. | not covered by a design doc |
| Session presence + accounting (this doc) | GUI authenticates in-process, issues a `web_sessions` row + cookie; never runs a login program or PAM session; often non-Unix users | GUI users invisible to `who`/`last`, the audit log, and central AAA accounting | §1–§7 |
| Config-change attribution (§0.4, §10) | GUI uses one shared API key; commits run as the API's OS user | `show system commit` never shows the real GUI user | §10.4 |
| GUI identity itself | Private `User` table (email + bcrypt) with no Unix account | Local admin principals an AAA server has never heard of | §6.1 / §7 Phase 3 |

### 11.2 The invariant that decides where a concern belongs

Generalising §2.1, responsibilities split by who can be the system of record:

- **Delegable off-box** — authentication and identity provenance: the IdP, the
  AAA server, or cloud-init can own "who authenticated" / "what the credential
  is."
- **Device-only** — forced first-boot hardening, on-device presence, and change
  attribution. Only the box can produce or enforce these, so a frontend can
  never be their system of record.

### 11.3 Deployment-form corollary

Because these concerns live in packages built from the GUI repository, they are
absent whenever those packages are not installed — headless images, and cloud
images that rely on cloud-init. A security control (force the operator off
`igos/igos`, apply the password policy) that disappears with the GUI packages
is in the wrong layer. Cloud is a partial exception only because credential
provisioning can be delegated to cloud-init — but that is a different,
un-unified enforcement path.

### 11.4 The shared remedy

1. **Push the requirement + enforcement down** into a layer present in every
   deployment — a vyos-1x first-boot service, activation script, or login hook
   in an always-installed base package.
2. **Demote GUI / CLI wizard / cloud-init to interchangeable frontends** that
   satisfy the same core gate.
3. **Use a real signal, not a heuristic** — a dedicated "onboarding complete"
   flag, not "`system time-zone` is set."
4. **Converge GUI identity onto the box's real sources** and retire the shadow
   `User` table (§6.1).

For accounting, (1)–(2) are the broker (§5) and §10.4; (4) is Phase 3. For
onboarding, the same move is: relocate the detect + apply logic and the login
hook into vyos-1x, add a completion flag, and have the GUI wizard, CLI wizard,
and cloud-init all drive that gate.

### 11.5 Recommendation

Track "device concerns in the GUI plane" as a single cross-cutting
architectural decision with a shared remedy. Accounting/attribution (this doc)
is the furthest-developed instance; onboarding is an equally real instance with
no design doc yet and should be written up separately.
