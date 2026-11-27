# igOS Feature Requests (tracking)

Status: **Open — collecting requests (2026-11-18)**

Scope: feature requests and gaps for igOS (vyos-1x and related repos) that
don't yet have a design document of their own. Each entry records the
request, what the code supports today, and a proposed approach. When an
entry grows into real work, give it its own `igos-<topic>-design.md` and
leave a pointer here.

---

## 1. Summary

| ID | Feature | Area | Repos | Status |
|---|---|---|---|---|
| F1 | Bind an OpenVPN client certificate to a user (username must match certificate CN) | VPN / OpenVPN | vyos-1x | Proposed |
| F2 | Make the REST API form handler tolerate newer starlette (`_get_form` accepts extra keyword arguments) | HTTP API | vyos-1x | Deferred — do with the next vyos-http-api-tools bump |
| F3 | Web CLI in the GUI, at the privilege level the user logged in with | GUI / CLI | flask-react-dev, image build (vyos-build) | Proposed — GUI page largely built; ttyd and security pieces missing |

---

## 2. F1 — Bind an OpenVPN client certificate to a user

**Request.** In OpenVPN server mode, tie each client certificate to one user,
so a connection is accepted only when the user authenticating is the user
the certificate was issued to.

### 2.1 What exists today

Per-certificate configuration is keyed by the certificate Common Name (CN):

- `interfaces openvpn <if> server client <name>`: `<name>` is the client
  certificate's CN (`interface-definitions/interfaces_openvpn.xml.in`). It
  generates a client-config-dir file `/run/openvpn/ccd/<if>/<name>`
  (`data/templates/openvpn/client.conf.j2`) with the fixed `ip`, `subnet`
  (iroute), `push-route` and `disable` settings for that client.
- `server reject-unconfigured-clients` sets `ccd-exclusive`: only
  certificates whose CN is listed under `server client` may connect, even if
  the CA signed others. This acts as a per-user allow-list.
- `server client <name> disable` blocks one client; `tls crl` revokes
  certificates.
- `server mfa totp` loads the `openvpn-otp` plugin. `interfaces_openvpn.py`
  creates one TOTP secret per `server client <name>` in
  `/config/auth/openvpn/<if>-otp-secrets`. The client then authenticates
  with username `<name>` and the TOTP code as the password.

### 2.2 Gap

- **The username is never checked against the certificate.** The TOTP
  plugin checks only that the username/code pair is valid. A client holding
  alice's certificate who also has bob's TOTP code authenticates
  successfully, and gets alice's CCD settings, which are keyed by CN.
  Holders keep their TOTP secrets to themselves, so this is a missing
  enforcement rather than an open door, but nothing actually binds the
  certificate to the user.
- **No password authentication in server mode.** There is no check against
  local users, RADIUS or LDAP. `authentication username/password` is
  client-mode only, and server mode rejects it (`interfaces_openvpn.py`).
- The only workaround is raw `openvpn-option` (`auth-user-pass-verify`
  script or PAM plugin). The CLI does not validate it, it is not migrated,
  and it is easy to get wrong.

### 2.3 Proposed approach

1. New server option, for example
   `set interfaces openvpn <if> server username-match-certificate`
   (final name to be decided).
   - Render an `auth-user-pass-verify <script> via-env` that accepts the
     connection only when `$username` equals `$common_name`. Add
     `script-security 2`.
   - Requires the client to send a username: either with `mfa totp`, or with
     a password back end (item 2). Reject the option in `verify()` when
     there is nothing that asks for a username.
   - Keep the plugin order so TOTP and the match check both have to pass.
     Alternatively, do the comparison in a `client-connect` script, which
     runs after all authentication.
2. Optional, separate: server-side password authentication
   (`server authentication local-users | radius`), so that "user" can mean a
   real account and not only a TOTP identity. Larger; would need its own
   design.
3. Optional: `x509-username-field` selection (CN, or a SAN/e-mail field) for
   PKIs that don't put the user name in the CN.

### 2.4 Tests

- Smoketest: server with `mfa totp` and the new option; a client with
  certificate CN `alice` authenticating as `alice` connects, and as `bob` is
  rejected (log shows the reason).
- `verify()` rejects the option without an authentication method that
  supplies a username.
- Without the option, behaviour is unchanged.

### 2.5 Open questions

- Option name and location (`server` vs `server mfa` vs a new
  `server authentication` node).
- Case sensitivity of the comparison (CN `Alice` vs username `alice`).
- Whether the GUI should expose it.

---

## 3. F2 — REST API form handler: tolerate newer starlette

**Request.** Make the REST API's form handling robust against an upgrade of
the API's Python libraries (vyos-http-api-tools), so that an upgrade can't
break every form-based REST request, including the GUI's.

### 3.1 What exists today

- `src/services/api/rest/routers.py`, `MultipartRequest._get_form()`,
  overrides starlette's private `Request._get_form()` so it can turn form
  posts (`data=` + `key=`) into JSON for the endpoint models. Its signature
  accepts only `max_files` and `max_fields`.
- This code comes from upstream VyOS; it isn't igOS-specific.
- Units ship starlette 0.41.3 (fastapi 0.115.5) from vyos-http-api-tools,
  which calls `_get_form(max_files=..., max_fields=...)`. This works today.
- Upstream check (2026-11-27): `vyos/vyos-http-api-tools` `rolling` still
  pins fastapi 0.115.5 / starlette 0.41.3. Its 2026 updates (pyjwt,
  python-multipart, anyio, graphql-core) didn't touch fastapi or starlette,
  and no fastapi/starlette bump PR is open. Upstream vyos-1x `current` has
  the same `_get_form` signature, so it's exposed in the same way.

### 3.2 Gap

- Starlette 0.44.0 and later call `_get_form(..., max_part_size=...)`.
  The override doesn't accept that argument, so it raises `TypeError` and
  **every form-encoded REST request fails with 400**. JSON requests are not
  affected.
- Nothing breaks until vyos-http-api-tools is bumped to a fastapi/starlette
  that includes 0.44.0+, but then the failure is total and the first symptom
  would be the GUI's API calls failing.

### 3.3 Proposed approach

1. Add `**kwargs` to the override's signature (or an explicit
   `max_part_size` that is passed to the multipart parser where the
   installed starlette supports it). This is a one-line change.
2. Do it when vyos-http-api-tools is bumped, or at the next upstream merge
   if upstream hasn't already fixed it. Check upstream first to avoid a
   merge conflict.

### 3.4 Tests

- A unit test that calls `MultipartRequest._get_form()` with an extra
  keyword argument (`max_part_size=...`) and gets the converted form back.
- A form POST to `/retrieve` (`data=` + `key=`) returns 200 with the new
  libraries installed. Run it on a unit after the bump.

### 3.5 Open questions

- Whether upstream has already changed this override by the time we bump.

---

## 4. F3 — Web CLI in the GUI

**Request.** Customers run CLI commands from a terminal window in the GUI.
Each user gets the CLI **at the privilege level they logged in with**:

- **administrator** → full CLI, including `configure`;
- **operator** → operational mode only (and only the commands their operator
  group's command policy allows).

Security requirements are in `igos-gui-security-findings.md` G6; this entry
is the feature view. **Prerequisite:** G2 (per-unit cookie-signing key,
permission from the server-side session) must ship first; otherwise a
forged administrator cookie would give a terminal.

### 4.1 What exists today (flask-react-dev, checked 2026-11-27)

- `main`:
  - terminal page `react/src/components/cli-ui/XTerm.tsx` at `/cli`, plus a
    full-screen `/web-cli`. It's an xterm.js terminal speaking ttyd's
    protocol, with recording and save-to-file;
  - nginx `location /cli/shell/ws` → `127.0.0.1:7681`; Flask `/cli/shell`
    and `/cli/shell/token` proxy to ttyd (administrator only);
  - the toolbar button is switched off (`{false && …}` in
    `MainLayout.tsx`); the pages can still be reached by URL and search.
- Branch `origin/web-cli` (Duc Nguyen, March 2026): 13 commits, 3 files
  (`XTerm.tsx`, `xterm.css`, `MainLayout.tsx`). It turns the button on and
  blocks `configure` unless an "Allow configure" box is ticked. It is 1,282
  commits behind `main` but a trial merge has no conflicts. The local copy
  is from 2026-03-25; ask the author whether there is newer work.
- **Missing:** ttyd itself (no package, unit or build step; Debian has it
  only in forky/sid), and all of G6's security pieces. On a unit today the
  WebSocket gets 502 and the page keeps trying to reconnect.

### 4.2 Gap: GUI level vs. router level

The GUI and the router decide a user's level separately, and they can
disagree:

| GUI login | GUI level comes from | Router account level |
|---|---|---|
| Local account via PAM, no RADIUS level | **administrator** by default, unless Flask's database records a lower level (`login_pam` in `flask/app/auth/routes.py`) | admin, or operator if `system login user <u> operator group <g>` is set |
| RADIUS via PAM | privilege level: ≥10 administrator, ≥5 operator+, else operator | set by the router's RADIUS mapping (separate thresholds) |
| SAML | IdP role: admin / super_operator / operator | JIT account role (`system login saml jit`) |
| Flask database only | database | **no router account** |

The GUI also has an **operator+** level that has no direct equivalent in a
router shell.

The `configure` block on the `web-cli` branch runs only in the browser and
can be bypassed, so it can't enforce the level.

### 4.3 Proposed approach

Level rule: **the CLI runs at the lower of the GUI level and the router
account level**, enforced on the router, never in the browser.

1. **Package ttyd ≥ 1.7.0** for the image (vyos-build), as a systemd service
   on a Unix socket with `-W -O -H X-Igos-User` (G6 item 1).
2. **nginx + Flask** (G6 items 2–3): `auth_request` to a Flask endpoint that
   checks the session, the `Origin`, and returns the user **and the GUI
   level** (for example `X-Igos-User`, `X-Igos-Level`); nginx always
   overwrites those headers.
3. **Wrapper** run by ttyd (G6 item 4):
   - check the user name, and refuse users with no router account;
   - GUI level administrator → `exec login -f <user>`. The router account
     decides: an admin account gets the full CLI, an operator account gets
     operational mode. So a GUI administrator whose router account is an
     operator still gets only operational mode;
   - GUI level operator or operator+ → start the **operational-mode-only**
     shell, even if the router account is an admin. Use the same restricted
     shell that `system login user <u> operator` gives. If that can't be
     forced per session, refuse the CLI for that user rather than give a
     full shell.
   - Because it's a real login session, `who`/`last`, AAA accounting,
     `system login timeout` and the commit log all name the user.
4. **GUI:**
   - merge or rebase the `web-cli` branch;
   - `wss://` URL derived from the page;
   - show the button to every level that may use the CLI, not only
     administrator. Flask's `/cli/shell` routes currently require
     administrator; widen them to match;
   - keep "Allow configure" only as a convenience for administrators; hide
     it for operators.
5. **PerleCLOUD:** not supported until igos-cloud-proxy forwards WebSockets
   and passes the cloud user's role (cloud doc §0.6, §16; G7). Until then,
   cloud users are refused (the GUI user `PerleCLOUD` has no router
   account).

### 4.4 Tests

- No cookie → 401/403 on `/cli/shell/ws` (curl check in G6).
- Admin on both sides → full CLI; `configure`, `commit` works; `show system
  commit` and `who` show the user.
- Operator on both sides → operational mode only; `configure` not
  available; commands outside the operator group's policy refused.
- GUI administrator with a router operator account → operational mode only.
- GUI operator (or operator+) with a router admin account → operational
  mode only.
- Flask-database-only user → refused.
- WebSocket from a different `Origin` with a valid cookie → refused.
- Logout or idle timeout ends the terminal session.

### 4.5 Open questions

- **operator+**: proposed to get the operator CLI (operational mode only).
  Confirm, or decide whether operator+ should get more.
- How to force an operational-mode-only shell for an admin account (§4.3
  item 3), or whether to refuse instead.
- GUI default of administrator for local PAM users with no level (§4.2):
  should the GUI instead derive the level from the router account (as
  `check_permission()` does with group `vyattacfg`)? That would remove most
  mismatches. Track in the GUI security/accounting work.
- TACACS/RADIUS-mapped users: confirm `login -f` works and accounting
  records are sent.
- Ending terminals when the GUI session ends (logout, idle, reboot).
- Where ttyd, its unit and the wrapper are packaged.

---

## Revision notes

- 2026-11-18: Document created; F1 (OpenVPN certificate-to-user binding)
  added.
- 2026-11-27: F2 (REST API form handler vs newer starlette) added; deferred
  until the next vyos-http-api-tools bump.
- 2026-11-27: F3 (web CLI in the GUI) added: users get the CLI at the
  level they logged in with (administrator full, operator operational mode);
  enforced on the router as the lower of GUI and router level.
