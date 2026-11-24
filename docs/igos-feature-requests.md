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

## Revision notes

- 2026-11-18: Document created; F1 (OpenVPN certificate-to-user binding)
  added.
