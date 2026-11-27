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

## Revision notes

- 2026-11-18: Document created; F1 (OpenVPN certificate-to-user binding)
  added.
- 2026-11-27: F2 (REST API form handler vs newer starlette) added; deferred
  until the next vyos-http-api-tools bump.
