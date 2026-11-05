# igOS FIPS 140-3 — Design Sketch

Status: **implementation in progress** (Phases 0, 1, and the vyos-1x half of
Phase 4 landed; enforcement largely landed — helper/policy + SSH + IPsec + HTTPS +
NTP/NTS + WireGuard/PPTP + RADIUS/TACACS+ + OpenVPN-TPM + Wireless + SNMPv3 +
haproxy + password-hash KDF; remaining = TPM operator rejects + the traced audit
rows + on-target validation + the vyos-build kernel half — see "Implementation
status" below).
Scope: run a **single igOS image** that can be placed into a **FIPS 140-3 mode**
by configuration (`set system option fips`, applied on **reboot**), so that the
system performs its cryptography using a **FIPS 140-3 validated module** (the
OpenSSL FIPS provider) operated in an approved manner, with **non-approved
algorithms and features disabled**. Hard requirement: **no second software path
/ no separate FIPS image** — the same binaries ship everywhere and FIPS is a
runtime provider+policy switch. A reboot to enter or leave FIPS mode is
acceptable and, in fact, preferred (clean service restart + boot self-tests).

This builds directly on upstream VyOS's partial work (which is explicitly flagged
"incomplete") and extends it from "OpenSSL provider toggle" toward a
system-wide, enforceable FIPS mode. It spans `vyos-1x`, `vyos-build`, and the
kernel.

---

## Implementation status (checkpoint: 2026-11-05 — enforcement wave 2 (Wireless/SNMPv3/haproxy) + password-hash KDF + DSA removal landed; audit rows traced)

**Checkpoint 2026-11-05 (session summary):**
- **Landed this session** (code complete; `py_compile` + `ruff` + `test_fips_policy`
  12/12 clean; on-target validation pending a new image build):
  - **Wireless / SNMPv3 / haproxy** — the audit "needs-work ✔verified" trio.
    Wireless: whole-feature reject of WEP + WPA-Enterprise RADIUS, reject TKIP
    (`cipher`/`group-cipher`) and WPA1 (`mode wpa`/`wpa+wpa2`), approved `wpa2`
    default. SNMPv3: reject `md5`/`sha` auth and `des` privacy on every v3
    user/trap-target, approved `sha256`/`aes` defaults. haproxy: reject CHACHA20
    `ssl-bind-ciphers`, AES-GCM approved default. (policy + conf-mode + grammar-
    drift test all updated.)
  - **Password-hash KDF (§8.5)** — `system_login.py` pins SHA-512-crypt under
    FIPS (plaintext + NVMEM sticker paths), rejects a non-approved operator
    `encrypted-password` (`$5$`/`$6$` only), and `initialsetup.make_password_hash()`
    emits SHA-512-crypt under FIPS (yescrypt kept for non-FIPS).
  - **Greenfield DSA removal** (adjacent cleanup — OpenSSH 10.4 dropped DSA):
    host keys (service_ssh + grammar + MOTD), user public keys (system_login +
    `utils/auth.py` + grammar + MOTD), and the PKI op-mode host-key import
    (`op_mode/pki.py` + op-mode def). ed25519 kept (alive in non-FIPS, already
    rejected under FIPS).
  - **Audit rows traced** (2026-11-05) — findings table below; two quick-wins
    (PKI key-gen floor, TLS-version floor) + two decisions (PPP auth, VRRP auth)
    + one newly-found surface (routing-protocol MD5) identified.
- **Remaining vyos-1x code:** TPM operator rejects (`ssh-tpm-agent` + TPM-PKI,
  §6.1); the traced audit-row fixes; then drop the "incomplete" warning.
- **Validation pending (needs a booted build):** reboot-survives the toggle,
  fail-closed `fips.so` removal, end-to-end IKE SA against a live peer.
- **Owned elsewhere:** Phase 4 kernel build (`CONFIG_CRYPTO_FIPS` + HMAC
  initramfs — vyos-build); RadSec (§8.8); claim wording (§8.2); am64x OE + TPM
  cert (§8.6/§8.7).

**Scope decisions locked in:** validated-module claim = **OpenSSL provider only**
(#4985); kernel **FIPS _mode_** (`fips=1`) is additionally enabled as approved-mode
hardening but **no validated-kernel-module claim** (§8.2/§8.3). Single-image,
reboot-applied toggle. WireGuard + PPTP = whole-feature removals; everything else
= crypto-method restriction (§5). TPM operator uses disabled under FIPS on an
uncertified fTPM (§6.1). RADIUS/TACACS+ non-approvable, no RadSec today (§4.1).
NTS (NTP) non-approvable (AES-SIV absent from the FIPS provider); plain NTP keeps
the clock.

**DONE (implemented; most also on-target validated on 10.10.50.165 — see the
per-item "on-target validated" notes below; always compile/ruff/pylint/
configd-inspect/test clean):**
- **Phase 0 — provider toggle foundation.** `system option fips` leaf
  ([interfaces/system_option.xml.in](../interface-definitions/system_option.xml.in));
  verify/generate/apply in
  [system_option.py](../src/conf_mode/system_option.py) (arch detect, `fipsinstall`
  self-test, render openssl.cnf, reboot pending-state + Warning — §7.2.1);
  [openssl.cnf.j2](../data/templates/openssl/openssl.cnf.j2); smoketest
  ([test_system_option.py](../smoketest/scripts/cli/test_system_option.py)).
- **Phase 1 — reboot-applied, fail-closed boot, observability.** Boot-time
  regeneration + **controlled fail-closed**
  ([fips-regenerate.sh](../src/system/fips-regenerate.sh) +
  [vyos-fips-regenerate.service](../src/systemd/vyos-fips-regenerate.service),
  enabled in [postinst](../debian/vyos-1x.postinst)); `show system fips`
  ([system_fips.py](../src/op_mode/system_fips.py) +
  [show-system.xml.in](../op-mode-definitions/show-system.xml.in)).
- **Phase 4 (vyos-1x half) — `fips=1` kernel cmdline** on the toggle, in **both**
  the commit path ([system_option.py](../src/conf_mode/system_option.py)
  `generate()`) and the install/upgrade path
  ([image_installer.py](../src/op_mode/image_installer.py)
  `get_cli_kernel_options()` — moved above the early return so it applies without
  `system option kernel`). Inert until the kernel is built with
  `CONFIG_CRYPTO_FIPS`.
  - **Timing note — one-boot stale window (by design, benign).** Like every
    `system option kernel …` parameter, `fips=1` is written to the **persistent**
    grub cmdline (`/boot/grub/grub.cfg.d/vyos-versions/<version>.cfg`) at
    **commit**, not at `save` — it must be staged before the reboot that consumes
    it, so the intended flow is `set system option fips` → `commit` → **save** →
    reboot (the `apply()` Warning says exactly this). A *commit-without-save then
    reboot* therefore boots **once** with a stale `fips=1` while the saved config
    is non-FIPS; boot-time **config replay** re-runs `generate()` and strips it,
    so grub self-corrects and the next boot is clean. Normal enable (commit+save)
    and disable (delete+save) flows are fully consistent. Note the provider side
    (`openssl.cnf`/`fipsmodule.cnf`) reverts *immediately* on such a reboot while
    the grub cmdline lags by one boot — this asymmetry is expected. `show system
    fips` reports kernel FIPS from `/proc/sys/crypto/fips_enabled` (the real
    kernel state), never from the cmdline, so it cannot report a false positive.
    A true "apply on save" is intentionally **not** pursued: VyOS has no
    conf-mode save hook, and deferring the grub write to boot-from-saved-config
    would require two reboots to apply.
- Provider package (vyos-build) **built + `fipsinstall` INSTALL PASSED on arm64**
  (§7.5) and registered in the flavor + nexus-build Makefile (prior session).
- **Enforcement foundation + SSH/IPsec guards (this session).**
  - **p23-helper-policy** — [`python/vyos/fips.py`](../python/vyos/fips.py)
    (`fips_enabled()`, `approved()`, `default()`, `reject_unapproved()`,
    `apply_fips_defaults()`, `remove_nodes()`) + the single auditable allow-list
    [`data/fips/approved-algorithms.toml`](../data/fips/approved-algorithms.toml)
    (§5.7). Installed to `/usr/share/vyos/fips/` by `debian/rules` (`cp -r data/*`).
    **Both halves are policy-driven:** `verify()` rejects read the `approved`
    sets, and generation-time defaults are emitted from each leaf's ordered
    `default` list via `apply_fips_defaults()` (fill-if-absent; explicit values
    are left for `verify()` to reject). No approved tokens are hardcoded in Python
    or templates -- enforced by tests (`default` subset-of-`approved`, and a guard
    that `sshd_config.j2` contains no literal algorithm tokens).
  - **p3-ssh (enforced)** — FIPS-aware
    [`sshd_config.j2`](../data/templates/ssh/sshd_config.j2) (approved
    Ciphers/MACs/KexAlgorithms/HostKeyAlgorithms/PubkeyAcceptedAlgorithms emitted
    from the policy via `get_config()`, + **approved RSA/ECDSA host keys only**,
    ed25519/DSA + uncertified-TPM host
    keys dropped under FIPS); [`service_ssh.py`](../src/conf_mode/service_ssh.py)
    sets the `fips` flag in `get_config()`, generates approved text host keys, and
    `verify()` rejects explicitly-set non-approved values. **This fixes the SSH
    lockout** found on-target (ed25519-only host key + non-approved KEX made every
    new SSH session under FIPS fail).
  - **Toggle fan-out (dependency)** — the `system option fips` commit must re-run
    the crypto consumers even though *their* subtrees did not change; otherwise
    sshd keeps non-approved host keys under the active provider (the lockout)
    until the next boot. Added a `config-mode-dependencies` `fips` case
    ([vyos-1x.json](../data/config-mode-dependencies/vyos-1x.json): `system_option`
    -> `fips`: `["service_ssh", "vpn_ipsec"]`, plus `fips_openvpn`:
    `["interfaces_openvpn"]`) and `set_dependents()` in
    [`system_option.py`](../src/conf_mode/system_option.py) `get_config()` when the
    fips node changed; `apply()`'s `call_dependents()` runs them. OpenVPN is a
    per-interface tagNode script, so each instance is registered with its tagnode
    (a bare dependent runs with an empty ifname and fails). Result: enabling FIPS
    reconfigures SSH/OpenVPN **at commit, no reboot, no lockout window**.
  - **p3-ipsec (verify half)** — [`vpn_ipsec.py`](../src/conf_mode/vpn_ipsec.py)
    `verify()` fails closed on explicitly-set non-approved IKE/ESP
    encryption/hash/dh-group and ESP `pfs`.
  - **p3-ipsec (openssl-plugin half)** — under FIPS,
    [`vpn_ipsec.py`](../src/conf_mode/vpn_ipsec.py) renders
    [`fips_plugins.conf.j2`](../data/templates/ipsec/fips_plugins.conf.j2) to
    `/etc/strongswan.d/charon/zzz-vyos-fips.conf` (sorts last → overrides the
    shipped per-plugin `load=yes`), disabling the native strongSwan crypto
    plugins **and** the kernel `af-alg` plugin and setting `openssl load_legacy=no`
    so **all IKE crypto is served by the validated OpenSSL provider** (ESP bulk
    stays in the kernel, §6.2). A FIPS toggle forces a full `charon` restart
    (plugin set is only read at startup), wired via a `fips_changed` flag.
  - **OpenVPN (enforced)** — [`server.conf.j2`](../data/templates/openvpn/server.conf.j2)
    renders **`providers fips base`** under FIPS (was `providers default`, which
    explicitly loaded the *non-FIPS* default provider and bypassed FIPS);
    [`interfaces_openvpn.py`](../src/conf_mode/interfaces_openvpn.py) sets the
    `fips` flag, emits the approved `data-ciphers` default **from the policy**
    (`apply_fips_defaults()`, TLS modes), and `verify()` rejects non-approved
    cipher/data-ciphers/hash **and TPM-backed OpenVPN** (uncertified fTPM, §6.1).
  - Regression coverage: [`test_fips_policy.py`](../src/tests/test_fips_policy.py)
    (helper behavior + a **grammar drift-guard** per §5.6.5 asserting every
    allow-list token still exists in the CLI `<completionHelp>`).
  - Validated here: compile / ruff / pylint W0611 / configd-inspect / template
    render across FIPS + non-FIPS + explicit-override cases. **On-target
    validated** (10.10.50.165): enable `system option fips` → commit regenerates
    `/run/sshd/sshd_config` with approved RSA/ECDSA host keys (dependency fired) →
    a fresh SSH session negotiates `ecdsa-sha2-nistp256` host key + `ecdh-sha2-
    nistp256` KEX + `aes128-ctr` + `hmac-sha2-256-etm` and connects (lockout
    gone); a non-approved `service ssh cipher` is rejected at commit; `delete`
    reverts SSH + provider cleanly. OpenVPN template renders `providers fips base`
    + approved data-ciphers under FIPS via the real filter chain on-target, and
    the fips toggle commits cleanly with OpenVPN in the fan-out. **strongSwan**:
    with a minimal site-to-site PSK tunnel (approved aes256/sha256/modp2048)
    configured, enabling FIPS rendered the plugin override and restarted charon
    with **only the `openssl` plugin** for crypto (native aes/sha2/hmac/gcm/gcrypt/
    af-alg/curve25519/chapoly all gone), the connection still loaded, and
    disabling FIPS removed the override and restored the native plugins. Config
    left unsaved (box clean).
  - **Whole-feature rejects (p3-wg-pptp + RADIUS/TACACS+)** — fail-closed
    `verify()` in every owning conf-mode script when FIPS is set: **WireGuard**
    ([interfaces_wireguard.py](../src/conf_mode/interfaces_wireguard.py)),
    **PPTP** ([vpn_pptp.py](../src/conf_mode/vpn_pptp.py)), and **RADIUS/TACACS+**
    across all 9 paths (system login, pppoe/ipoe servers, l2tp/sstp/openconnect,
    wireless WPA-Enterprise, IPsec EAP). Two directions: a data-driven central
    gate in [system_option.py](../src/conf_mode/system_option.py) refuses to
    *enable* FIPS while any is configured (post-boot only, so boot replay is not
    blocked), and each feature's own `verify()` refuses to *add* it while FIPS is
    set (fail-closed, incl. at boot). Paths/labels live in the policy
    (`reject_under_fips`, `*` = tagNode); helper `features_present()` +
    `reject_feature()`. **On-target validated**: enabling FIPS with a `system
    login radius` present is refused (central gate + system_login verify), and
    adding `interfaces wireguard` while FIPS is on is refused (wireguard verify).
  - **NTS reject (NTP)** — [service_ntp.py](../src/conf_mode/service_ntp.py)
    rejects `service ntp server <x> nts` under FIPS (NTS's mandatory AES-SIV AEAD
    is absent from the FIPS provider; verified on-target). **Plain NTP has no
    crypto and keeps working**, so the clock is preserved. On-target validated:
    FIPS enables fine with plain pool servers; adding `nts` is refused with a
    message pointing to plain NTP.
  - **HTTPS / GUI cipher selection** — new operator-selectable
    `service https cipher-suite` (TLS 1.2 `ssl_ciphers`) and `cipher-suite-tls13`
    (TLS 1.3 `ssl_conf_command Ciphersuites`) leaves
    ([service_https.xml.in](../interface-definitions/service_https.xml.in),
    [nginx.default.j2](../data/templates/https/nginx.default.j2),
    [service_https.py](../src/conf_mode/service_https.py)) — **feature parity with
    SSH** (the GUI previously had a hard-coded cipher list, no CLI control). TLS
    1.0/1.1 stay blocked. FIPS-aware via the same policy machinery: `verify()`
    rejects non-approved, approved defaults emitted when unset (no SHA1/DSS/
    chacha). The **non-FIPS default was also hardened** to AEAD-only (Mozilla
    "intermediate": AES-GCM + ChaCha20, dropping the old SHA1-CBC/DSS suites);
    SHA1-CBC/DSS are intentionally **not selectable** (consistent with blocking
    TLS 1.0/1.1), though operators can still pick AEAD + CBC-SHA2. On-target
    validated: under FIPS the GUI negotiates TLSv1.3 / `TLS_AES_256_GCM_SHA384`
    and serves 200; the hardened default is active when FIPS is off. (The new CLI
    leaves need the next image build to be settable on-target; the FIPS
    auto-default + hardened-default paths are build-independent and were
    validated.)

**WORKING-TREE STATE (for resuming):** the main enforcement batch is **committed**
(`99acd2dda` on branch `vyos-1x-jf`, "FIPS 140-3 Phases 2-3 …", 24 files). On top
of it, the **HTTPS GUI cipher-suite feature is uncommitted** (6 files:
`interface-definitions/service_https.xml.in`, `src/conf_mode/service_https.py`,
`data/templates/https/nginx.default.j2`, `data/fips/approved-algorithms.toml`,
`data/config-mode-dependencies/vyos-1x.json`, `src/tests/test_fips_policy.py`, plus
this doc). On-target copies on 10.10.50.165 are **ephemeral test deploys** (reverted
to a clean config after each test); durable delivery = commit + rebuild. The new
HTTPS CLI leaves (`cipher-suite`, `cipher-suite-tls13`) need a grammar rebuild
(node.def/reftree regeneration) to be settable on the unit — not doable via the
file-level test deploys.

**ON-TARGET VALIDATION PENDING (needs the booted image):**
- Toggle `set system option fips` → `show system fips` → reboot survives (tmpfs
  regeneration), `openssl list -providers` shows fips active, `/run/ssl/fipsmodule.cnf`
  present; then `delete` reverts cleanly. **(Toggle + provider-active + revert
  validated this session; reboot-survives still blocked pending save, see below.)**
- **SSH under FIPS stays reachable** (the lockout fix): enable FIPS, confirm a new
  SSH session negotiates an approved host key + KEX/cipher and succeeds.
  **(VALIDATED on 10.10.50.165 — approved ECDSA host key + ecdh/aes-gcm/hmac-sha2,
  fresh session connects; verify-reject + revert also validated.)**
- Fail-closed path: corrupt/remove `fips.so` while openssl.cnf references it →
  confirm crypto services masked + console banner + `show system fips` shows
  SELF-TEST FAILED (watch via the serial console / `socat`).
- Confirm the igOS **3.5** base's shipped `/etc/ssl/openssl.cnf` isn't carrying
  customizations our template would overwrite (§7.1 note). **(Confirmed stock.)**

**NEXT UP:**
1. **Wireless + SNMPv3 + haproxy** — **LANDED** (2026-11-05): wireless rejects
   WEP + WPA-Enterprise RADIUS (whole-feature) and TKIP / WPA1 (`mode wpa`,
   `wpa+wpa2`), emitting the approved `wpa2` default; SNMPv3 rejects `md5`/`sha`
   auth and `des` privacy, emitting `sha256`/`aes` defaults; haproxy rejects
   non-approved (CHACHA20) `ssl-bind-ciphers`, emitting the AES-GCM default.
   HTTPS cipher control + NTP/NTS reject **landed**.
2. Then: TPM operator rejects for `ssh-tpm-agent` + TPM-PKI (§6.1; OpenVPN TPM
   reject already landed; password-hash KDF §8.5 **landed**), and the ⚠audit
   rows below.
   Drop the "incomplete" warning once enforcement lands.

**Smoketest-level validation (needs a live IPsec peer):** end-to-end IKE SA
establishment under FIPS (the on-target check here proved charon loads the
approved connection with only the openssl plugin, but not a completed handshake
against a second endpoint).

**Decision-gated / owned elsewhere:** p4-kernel-build (`CONFIG_CRYPTO_FIPS` +
HMAC initramfs — vyos-build, user owns); RadSec scope (§8.8); claim wording
(§8.2); am64x OE + TPM certification (§8.6/§8.7).

### Remaining enforcement surface — crypto-consumer audit (2026-11-04; rows traced 2026-11-05)

**Audit coverage — honest scope:** this is a **breadth-complete, depth-partial**
audit. I grepped *all* of `interface-definitions/` for crypto-bearing config
(cipher/tls/encryption/hash/psk/radius/tacacs/wpa/snmp/…) to enumerate the full
candidate set below, and **deep-verified** the rows marked ✔verified. The rows
marked ⚠audit are confirmed to *touch* crypto but their exact token sets / whether
the OpenSSL provider already covers them have **not** each been individually
traced yet. Treat ⚠audit as "must be looked at", not "confirmed non-compliant".

**Needs work — confirmed non-approved tokens exposed (✔verified):**

| Feature | CLI / file | Non-approved exposure | Fix pattern |
|---|---|---|---|
| **HTTPS / GUI** | [`service https`](../interface-definitions/service_https.xml.in), [nginx.default.j2](../data/templates/https/nginx.default.j2) | hard-coded `ssl_ciphers` offered SHA1-CBC + DSS suites; no operator cipher control | **LANDED** — new `cipher-suite` (TLS 1.2) + `cipher-suite-tls13` leaves (operator-selectable, parity with SSH); under FIPS, `verify()` rejects non-approved and the approved default is emitted (no SHA1/DSS/chacha); 1.0/1.1 stay blocked. On-target: TLS negotiates TLSv1.3/AES-256-GCM, GUI 200 |
| **Wireless / WiFi** | [`interfaces wireless`](../interface-definitions/interfaces_wireless.xml.in) (hostapd) | `security wep`, cipher `TKIP`, `mode wpa` (WPA1/TKIP) | **LANDED** — `verify()` whole-feature rejects WEP (and WPA-Enterprise RADIUS); rejects explicit TKIP (`cipher`/`group-cipher`) and WPA1 (`mode wpa`/`wpa+wpa2`); emits approved `wpa2` default when `mode` unset (grammar default is `wpa+wpa2`). Allows WPA2/WPA3 + CCMP/GCMP |
| **SNMPv3** | [`service snmp`](../interface-definitions/service_snmp.xml.in) (`authentication-type.xml.i`, `privacy-type.xml.i`) | auth `md5` (and `sha`=SHA1); privacy `des` | **LANDED** — `verify()` rejects explicit `md5`/`sha` auth and `des` privacy for every v3 user + trap-target; emits approved `sha256`/`aes` defaults (grammar defaults `md5`/`des`) so a configured user/trap is approved by default. Requires SHA-2 + AES |
| **haproxy** (if used) | [`load-balancing haproxy`](../interface-definitions/load-balancing_haproxy.xml.in) | `ssl-bind-ciphers` list includes `*-chacha20-poly1305` | **LANDED** — `verify()` rejects explicit non-approved (CHACHA20) `global-parameters ssl-bind-ciphers`; emits the AES-GCM approved default when unset (grammar default includes chacha20) |
| **NTP / NTS** | [`service ntp`](../interface-definitions/service_ntp.xml.in) (`server <x> nts`), chrony 4.3 | NTS mandates `AEAD_AES_SIV_CMAC_256`; **AES-SIV is only in OpenSSL's `default` provider, absent from `fips`+`base`** (verified on-target) — so NTS fails under FIPS, and AES-SIV is not a NIST-approved mode anyway (NTS-KE TLS is fine; the AEAD is the blocker, no interoperable approved substitute) | reject `service ntp server <x> nts` under FIPS — **LANDED** (plain NTP has no crypto and keeps the clock) |
| **WireGuard, PPTP** | [`interfaces wireguard`](../interface-definitions/interfaces_wireguard.xml.in), [`vpn pptp`](../interface-definitions/vpn_pptp.xml.in) | whole feature (Curve25519/ChaCha20; MPPE/RC4) | whole-feature `verify()` reject — **LANDED** |

**Confirmed already approved (no action):**
- **MACsec** ([`interfaces macsec`](../interface-definitions/interfaces_macsec.xml.in)): cipher `gcm-aes-128`/`gcm-aes-256` only — AES-GCM, approved. (CAK/CKN key strength is an operational concern, not an algorithm one.)

**⚠audit — traced 2026-11-05 (findings):**

| Row | Finding | Verdict | Action |
|---|---|---|---|
| **PKI cert signing digest** | [`vyos/pki.py`](../python/vyos/pki.py) signs with **SHA-256** (`create_certificate`/`_request`); fingerprints sha256/384/512 — no MD5/SHA1 | ✅ already approved | none |
| **PKI key generation** | op-mode [`generate pki`](../src/op_mode/pki.py) `size_valid = [512, 1024, 2048, 4096]` allows **512/1024-bit RSA** (config `pki` RSA list is already 2048/3072/4096) | ❌ non-approved exposure | **quick win** — drop 512/1024 (greenfield) or floor ≥2048 under FIPS; DH default 2048 ✓ |
| **accel-ppp PPP auth** (pppoe/ipoe/l2tp) | [`auth-protocols.xml.i`](../interface-definitions/include/accel-ppp/auth-protocols.xml.i) = `pap chap mschap mschap-v2`, **all enabled by default** (plaintext / MD5 / MD4+DES) | ❌ non-approved; **decision** | PPP auth can't be FIPS-approved — reject the PPP servers (or chap/mschap) under FIPS, like RADIUS |
| **VRRP auth** ([`high-availability`](../interface-definitions/high-availability.xml.in)) | `authentication type` = `plaintext-password \| ah` (AH = HMAC-MD5) | ⚠ **decision** | both non-approved; VRRPv3 drops auth anyway → reject `authentication` under FIPS, or declare out-of-scope |
| **TLS version floor** | shared [`tls-version-min.xml.i`](../interface-definitions/include/tls-version-min.xml.i) allows **1.0/1.1**/1.2/1.3 (used by openconnect, …) | ⚠ low-risk | **quick win** — OpenSSL provider blocks <1.2 at runtime; floor/reject explicit 1.0/1.1 under FIPS on the include |
| **TLS consumers** (sstp, sstpc, syslog, email, container, zabbix, salt) | none expose a cipher-list CLI leaf (unlike SSH/HTTPS/haproxy) | ⚠ **backend-dependent** | OpenSSL-backed → fips provider auto-rejects at runtime; **ocserv (openconnect) is historically GnuTLS, NOT the OpenSSL fips module** — needs per-daemon confirmation |
| **HTTP API** | **no** md5/hashlib weak-hash in the API; [`rest/routers.py`](../src/services/api/rest/routers.py) has `verify=False` (×5) | ✅ algorithms clean; ⚠ TLS-trust | the MD5 audit is **clean**; `verify=False` is cert-validation (not an algorithm claim) — track separately |
| **Routing-protocol auth (NEW — found 2026-11-05)** | OSPF / IS-IS / RIP / BGP use **MD5 / plaintext** auth keys (`include/ospf/authentication.xml.i`, etc.) | ❌ non-approved; newly found | add to backlog — reject MD5/plaintext routing auth under FIPS (or classify intent) |

Remaining-ordering: **quick wins** = PKI key-gen floor + TLS-version floor;
**decisions needed** = PPP auth + VRRP auth (reject-under-FIPS vs. out-of-scope,
like RADIUS); **deeper** = per-daemon TLS-backend verification (ocserv/GnuTLS)
and imported-cert strength enforcement in config `pki`.
- **Password hashing** — **LANDED** (2026-11-05): `system_login.py` pins the
  approved SHA-512-crypt KDF under FIPS for both the plaintext-password and the
  NVMEM sticker-password paths, rejects an operator-supplied `encrypted-password`
  that uses a non-approved KDF (`$y$` yescrypt / `$7$` scrypt / `$2*` bcrypt /
  `$1$` MD5 / DES — only `$5$`/`$6$` SHA-2-crypt accepted), and
  `initialsetup.make_password_hash()` emits SHA-512-crypt when the seed config
  enables FIPS (yescrypt retained for non-FIPS). (The **HTTP API `hashlib`/`ssl`**
  MD5 audit is now **traced clean** — see the findings table below.)

**Whole-feature rejects — no approvable configuration (LANDED 2026-11-04):**
RADIUS and TACACS+ are treated like WireGuard/PPTP: they **cannot be used under
FIPS** (classic RADIUS authenticates with MD5; there is **no RadSec** in this tree,
and TACACS+ has no TLS variant at all). SAML remains the approved admin login
(§4.1). Implemented as fail-closed `verify()` rejects wherever a `radius` server
is set — `system login radius`, `service pppoe-server`, `service ipoe-server`,
`vpn l2tp`, `vpn sstp`, `vpn openconnect`, `vpn pptp`, `interfaces wireless`
(WPA-Enterprise), `vpn ipsec` (EAP-RADIUS) — plus `system login tacacs`, and a
central fips-enable gate. Paths are data-driven (`reject_under_fips` in the
policy). This stays a hard reject until/unless RadSec (`radsecproxy`, §8.8) is
implemented.

**NOT audited (out of this tree / needs owner):** kernel-space crypto beyond the
`fips=1` posture, disk/LUKS at-rest (if any), and any crypto inside third-party
daemons not surfaced through the VyOS CLI.

---

## 0. Context / grounding facts

### 0.1 Upstream VyOS prior art (what exists, what is missing)

| Item | Where | State | What it does |
|---|---|---|---|
| **T8529 — CLI to enable OpenSSL FIPS** | `vyos/vyos-1x#5139` | **merged** 2026-07-16 | Adds `set system option fips`; on commit runs `openssl fipsinstall` → `/run/ssl/fipsmodule.cnf`, renders `/etc/ssl/openssl.cnf` to load the `fips` provider; removes it when disabled. `apply()` warns **"FIPS 140-3 support is incomplete and may behave unexpectedly!"** |
| **T9040 — build FIPS-provider OpenSSL** | `vyos/vyos-build#1233` | **merged** 2026-07-03 | Patches OpenSSL build with `enable-fips` (`0001-Enable-FIPS-module.patch`) so `fips.so` is produced. Built against the Debian **3.0.20** base. |
| **T8508 — build OpenSSL 3.1.2** (the validated version) | `vyos/vyos-build#1161` | **not merged** (closed) | Attempted to ship the specific FIPS-**validated** OpenSSL 3.1.2. Superseded by the enable-fips-on-base approach. |
| **T861 — secure boot** | `vyos/vyos-build#1223` | merged | UEFI Secure Boot / shim — relevant to module/boot integrity (§6). |
| Kernel FIPS (`CONFIG_CRYPTO_FIPS`, `fips=1`) | `vyos/vyos-build` kernel config | **absent** | No kernel crypto FIPS mode is wired up. |

Key takeaways:

- VyOS **proved the toggle model** this project wants — one image, a config leaf,
  a provider swap — but **only for OpenSSL**, and **explicitly marked it
  incomplete**.
- The approach that actually landed rebuilds the **base OpenSSL (3.0.x) with
  `enable-fips`**, producing *a* `fips.so`. The attempt to ship the specific
  **validated 3.1.2** module did **not** merge — a real compliance nuance (§8.1).
- **Nothing** addresses the kernel, SSH, IPsec, the GUI, or non-approved-feature
  blocking. "FIPS mode" today means only "OpenSSL loads the fips provider."

### 0.2 The igOS fork's current state

Two relevant facts:

- **`vyos-1x` (fork):** based on a branch **before** T8529/T9040 merged.
  `system option fips` does not exist, and `data/templates/openssl/` contains
  only `cert.cnf.j2` (no `openssl.cnf.j2`). The CLI/config side must be ported.
- **`vyos-build` (fork):** igOS builds its **own OpenSSL 3.5** package
  (`scripts/package-build-iGOS/openssl-3.5/`) from Debian's packaging
  (`salsa.debian.org/debian/openssl` @ `c8211578…`), rebased from trixie naming
  (`libssl3t64` → `libssl3`) via `bookworm-packaging.patch`. **That build does
  not enable the FIPS provider** — there is no `enable-fips` and no FIPS-module
  patch (unlike upstream VyOS's T9040 `0001-Enable-FIPS-module.patch`). So the
  current igOS image ships **no `fips.so` at all**.

Consequence: igOS must (a) port the T8529 CLI/config layer, and (b) make the
3.5 build ship the **validated** FIPS provider (§0.4, §8.1) — *not* simply
`enable-fips` on 3.5.

### 0.3 How the OpenSSL FIPS provider model works (the mechanism we reuse)

OpenSSL 3.x splits crypto into **providers**. FIPS mode means configuring the
library so applications use the **FIPS provider** (`fips.so`) by default instead
of the default provider:

1. A FIPS provider `fips.so` must be **present**. It is *not* in Debian's stock
   build (nor in the current igOS 3.5 build). Critically, the provider is
   **version-decoupled from the base library** (§0.4): the **validated 3.1.2
   provider** is designed to load against a 3.0–3.5 base, so igOS keeps its 3.5
   `libssl` and adds the validated provider — rather than building 3.5's own
   (unvalidated) provider via `enable-fips`.
2. `openssl fipsinstall -module …/fips.so -out fipsmodule.cnf` runs the module's
   **Known-Answer Self-Tests (KAT)** and writes an **integrity MAC** into
   `fipsmodule.cnf`. (VyOS writes it to `/run/ssl/fipsmodule.cnf` — tmpfs —
   so it is regenerated and re-self-tested every boot.)
3. `openssl.cnf` is configured to `.include fipsmodule.cnf` and activate the
   `fips` (and `base`) providers, making every OpenSSL consumer use the validated
   module. `openssl list -providers` then shows `fips … status: active`.

The crucial property for this project: this is a **configuration switch over the
same binaries** — the FIPS provider ships present-but-dormant; nothing forks the
code. That is exactly the "one system, no second path" model.

### 0.4 The validated module (what "140-3" actually attaches to) — and why 3.5 is fine

- The **OpenSSL 3.1.2 FIPS provider holds NIST CMVP certificate #4985**, valid
  through **2030-03-10**, and is documented as **compatible with OpenSSL 3.0,
  3.1, 3.2, 3.3, 3.4, and 3.5** base libraries. (We have used this provider in
  other products.)
- **Base ↔ provider decoupling is the key enabler here.** Because the validated
  provider runs against a 3.5 base, igOS can keep shipping the **latest 3.5
  `libssl`** (current security fixes) while the **validated crypto module stays
  frozen at the 3.1.2 artifact**. That is the intended FIPS-provider model and
  the cleanest compliance boundary: *the thing that is validated never changes;
  the thing that changes is never the validated part.*
- Corollary: **do not `enable-fips` on 3.5.** A 3.5-built `fips.so` is a
  *different*, **unvalidated** module. Only the **3.1.2** provider carries
  cert #4985.
- FIPS 140-3 validates a **cryptographic module**, not an operating system. The
  appliance is not "FIPS 140-3 certified"; it **uses a FIPS 140-3 validated
  module** and is **configured to operate it in the approved manner**. Wording
  matters for customer claims (§8.2).

---

## 1. Goal & constraints

- **Single image.** One build artifact for FIPS and non-FIPS customers. No
  `-fips` variant, no divergent package set, no conditional source paths.
- **Config-driven.** `set system option fips` (reuse upstream leaf) selects FIPS
  mode; `delete …` leaves it. State lives in the normal config.
- **Reboot to apply.** Entering/leaving FIPS mode is applied on reboot, not live.
  This is a deliberate simplification: all services restart fresh under the
  chosen provider/policy and the module self-tests run cleanly at boot, avoiding
  live crypto re-initialization hazards.
- **System-wide, not just OpenSSL.** All crypto consumers must either use the
  validated module or be constrained to approved algorithms; non-approvable
  features must be refused in FIPS mode.
- **Enforceable + observable.** `commit` rejects configurations that cannot be
  honored under FIPS; a `show` command reports FIPS status and self-test results.

---

## 2. What "FIPS 140-3 mode" means for this appliance

Three distinct things, often conflated:

1. **Validated module present** — ship OpenSSL built with the validated FIPS
   provider (`fips.so`, cert #4985 lineage). Build-time only (§0.3/§8.1).
2. **Approved operation** — the running system uses that module for its
   cryptography and only its **approved algorithms**: configure OpenSSL to the
   fips provider, point every OpenSSL-linked consumer at it, and restrict
   protocol algorithm negotiation (SSH, IKE/ESP, TLS) to the approved set.
3. **Non-approved crypto disabled** — features that cannot be done inside the
   validated boundary (e.g. WireGuard, MD5-based constructs, certain curves/
   ciphers) are **blocked** when FIPS mode is on.

"FIPS mode" for igOS = (1) always shipped + (2) and (3) switched on by
`set system option fips` and applied at reboot.

---

## 3. The single-image toggle model

```
 one image (FIPS provider shipped, dormant)
        │
 set system option fips ; commit         → persists intent in config
        │
 reboot                                   → activation point
        │
 boot:
   ├─ (optional) kernel fips=1 + integrity (§6)
   ├─ regenerate /run/ssl/fipsmodule.cnf  → module KAT self-tests
   ├─ openssl.cnf activates fips provider → all OpenSSL consumers
   ├─ services start under FIPS algorithm policy (sshd, strongSwan, nginx, …)
   └─ verify()/policy already guaranteed the config is FIPS-legal at commit
```

Why reboot-applied (and why that is fine here):
- Services pick up the provider/policy cleanly at startup — no need to
  hot-swap crypto in a running `sshd`/`charon`/`nginx`.
- Boot self-tests give a clean, auditable "entered FIPS mode" point.
- The user explicitly accepts a reboot on mode change.

Why this is **not** two software paths:
- Same packages, same binaries in both modes. FIPS is a provider selection plus
  an algorithm policy, expressed in generated config and enforced in `verify()`.
  The only build-time delta is "OpenSSL includes the FIPS provider" — which is
  inert unless activated.

---

## 4. Crypto consumers that must be brought into FIPS mode

Each consumer needs a decision: *route through the OpenSSL FIPS provider*,
*restrict to approved algorithms*, or *disable in FIPS mode*.

| Consumer | Path in FIPS mode | Notes |
|---|---|---|
| **OpenSSL CLI / libssl** | fips provider via `openssl.cnf` | The upstream T8529 piece. Foundation for everything that links OpenSSL. |
| **OpenSSH** (sshd/ssh) | approved KEX/cipher/MAC/host-key set | Debian OpenSSH links OpenSSL but also has internal implementations; FIPS requires pruning the negotiated algorithm lists (no curve25519/chacha20-poly1305/ed25519 unless treated as allowed; approved = ECDH P-curves, AES-GCM/CTR, HMAC-SHA2, RSA/ECDSA host keys). |
| **strongSwan / IPsec** | restrict IKE/ESP proposals to approved set (conf_mode) | **NOT governed by the OpenSSL provider by default.** VyOS charon ([charon.j2](../data/templates/ipsec/charon.j2)) leaves `load`/`load_modular` at defaults, so strongSwan uses its **own native plugins** (aes, sha2, gmp, curve25519, chacha20poly1305). Enabling the FIPS provider does **not** break IPsec and does **not** make it compliant. Enforce by constraining the enumerated `encryption`/`hash`/`prf`/`dh-group` proposal values (rendered by [get_esp_ike_cipher](../python/vyos/template.py) into swanctl proposals) + approved defaults. Routing charon through the `openssl` plugin so crypto runs in the validated module is a heavier option, not needed for a first compliant cut. |
| **GUI / nginx TLS** | fips provider (links OpenSSL) | TLS 1.2/1.3 approved cipher suites only. |
| **HTTP API / Python** | OpenSSL via `hashlib`/`ssl` | Non-approved digests (MD5) raise unless `usedforsecurity=False`; audit call sites. |
| **FRR, SNMP, NTP/chrony, etc.** | approved algorithms / OpenSSL | SNMPv3 auth/priv limited to approved (no MD5/DES); audit each. |
| **Password hashing / local auth** | approved KDF | `yescrypt` (recent VyOS default) is **not** FIPS-approved; FIPS mode may need SHA-2-crypt or an approved PBKDF. Decision in §8. |
| **Disk/secrets at rest (if any)** | approved | Only relevant if igOS encrypts storage/config secrets. |

### 4.1 Authentication backends (RADIUS / TACACS+ / SAML)

Remote auth backends are a distinct FIPS concern from the per-service ciphers
above, because the **classic protocols depend on MD5** in their own packet
protection — independent of whatever OpenSSL is doing.

- **Classic RADIUS** (RFC 2865) hides the User-Password and computes the
  Request/Response Authenticator and Message-Authenticator with **MD5 / HMAC-MD5**
  → **not FIPS-approvable**. The approved path is **RadSec (RFC 6614)** — the whole
  RADIUS exchange wrapped in **TLS** (TCP/2083), so the approved transport
  protects it.
- **TACACS+** likewise obfuscates its payload with an **MD5**-based stream and has
  no deployed TLS variant → **non-approvable with no remediation path** (reject).
- **SAML** (the iGos admin-login path) is TLS + XML-DSig, OpenSSL-backed →
  **approved**. This is why the SAML/RADIUS/TACACS+ mutual-exclusion already in
  `system_login.py` lines up well with FIPS: SAML is the approved choice.

**Current capability in this tree — no RadSec.** All RADIUS usage is classic UDP:

| Path | Implementation | RadSec? |
|---|---|---|
| `system login radius` | `pam_radius_auth` ([pam_radius_auth.conf.j2](../data/templates/login/pam_radius_auth.conf.j2)) — `server:port secret timeout src` over UDP | **No** (pam_radius_auth has no TLS) |
| accel-ppp (PPPoE/L2TP/IPoE) | classic RADIUS over UDP (the `ssl=` lines in accel-ppp are **SSTP** transport, not RADIUS) | **No** |

There is **no `radsecproxy`** and **zero** `radsec`/RADIUS-over-TLS references in
the tree.

**FIPS status summary:**

| Backend | FIPS status | Path under FIPS |
|---|---|---|
| **SAML** | **Approved** — TLS + XML-DSig | keep (iGos login path) |
| **RADIUS (classic UDP)** | **Non-approved** — MD5 | reject **or** route via RadSec |
| **RADIUS + RadSec** | **Approved** — TLS tunnel | **not implemented today** |
| **TACACS+** | **Non-approved** — MD5, no TLS variant | reject (no remediation) |
| **Local** | Approved *iff* KDF approved | §8.5 (yescrypt) |

**Enforcement (honest current posture):** in FIPS mode, `verify()` **fails closed**
on RADIUS and TACACS+ admin auth (SAML remains the approved login), and
**accel-ppp subscriber auth that relies on RADIUS is non-compliant until RadSec is
added**. This is the long pole if broadband/subscriber use cases need RADIUS under
FIPS.

**Remediation — RadSec via `radsecproxy`.** Neither `pam_radius_auth` nor
accel-ppp speaks RadSec natively, so the realistic path is a **local
`radsecproxy`**: RADIUS clients talk plain RADIUS to `127.0.0.1`; `radsecproxy`
wraps it in TLS to the server. Scope: package `radsecproxy` in vyos-build, add
CLI + config generation + cert wiring (PKI), and point the existing RADIUS
clients at localhost. A real feature, not a flag flip — tracked as an open item
(§8.8).

---

## 5. Enforcement: the simpler approach (recommended)

This section captures the **recommended, lighter design** reached after working
through the actual shape of the problem. The earlier instinct was a heavyweight
mechanism (two baked CLI grammars, or model-style leaf removal); the analysis
below shows that for this platform the compliance surface is **mostly
crypto-method restriction, not feature removal**, so a much smaller design gets
to a compliant, shippable posture.

### 5.1 The shape of the problem

Only **two whole features** are non-approvable and must be removed/refused:

- **WireGuard** — Curve25519 / ChaCha20-Poly1305 / BLAKE2; **no approved option at
  all**. `set interfaces wireguard …` must be unavailable/refused in FIPS mode.
- **PPTP** (`vpn pptp`) — MS-CHAP + **MPPE/RC4**; non-approvable. Legacy and rarely
  used, but it is the second whole-feature removal.

Everything else is **crypto-method (algorithm) restriction**, not feature
removal. Notably **MACsec stays** — its cipher suites are **GCM-AES-128/256**,
which *are* FIPS-approved (optionally pin to approved key sizes).

### 5.2 The key sizing insight: OpenSSL-backed vs not

How much work each crypto consumer needs depends on **where its crypto runs**:

- **OpenSSL-backed** (GUI/nginx TLS, OpenVPN, PKI, anything linking libssl): once
  the FIPS provider is active, **non-approved algorithms are simply unavailable —
  the provider is the enforcer.** Here you mainly need **approved defaults** so
  services negotiate cleanly instead of failing ugly. No per-option CLI work
  required for compliance.
- **NOT OpenSSL-backed** (OpenSSH's *native* curve25519/chacha20/ed25519,
  **strongSwan** native plugins, net-snmp, kernel crypto): the provider does
  **nothing**. These must be restricted **explicitly in the generated config**
  (sshd `Ciphers`/`KEX`/`MACs`, swanctl proposals, SNMPv3 auth/priv). This is a
  conf_mode / config-generation job regardless of any CLI grammar.

IPsec is the canonical "NOT OpenSSL-backed" case: it keeps working when FIPS is
on, but is only made compliant by restricting the `ike-group`/`esp-group`
proposal values (see §4).

### 5.3 The recommended enforcement design

| Thing | Enforcement |
|---|---|
| **WireGuard, PPTP** | leaf-removal (model-style `cli-remove`) **or** `verify()`-reject when `fips` set — only 2 features |
| **OpenSSL-backed crypto** (TLS/GUI, OpenVPN, PKI) | FIPS provider auto-blocks non-approved algorithms; just ship **approved defaults** so it's clean, not broken |
| **Non-OpenSSL crypto** (SSH native, IPsec, SNMPv3, kernel) | **FIPS-aware conf_mode generation** (approved-only + approved defaults) + `verify()` reject explicitly-set non-approved values |

Net result: **T8529 (provider) + a short `verify()` guard list + a handful of
FIPS-aware conf_mode generators** — no second grammar, no cache-switching, no
boot-time grammar selector. Enforcement principle remains **fail closed at
commit**: a box that boots FIPS-on but quietly runs a non-approved cipher is
worse than one that refuses to commit.

### 5.4 Parked idea: exclude non-approved options from the CLI (vs. reject)

**Desired, but deferred.** The approach above *rejects* a non-approved value at
commit (`verify()`), rather than *removing it from the available choices*. A
reject is cheaper and fully sufficient for **compliance**, but it has a real
downside for **UX / the GUI**:

- The GUI reads the **available options** from the CLI grammar (reftree /
  completion data) to build its forms. If non-approved algorithms are still
  *present* in the grammar and only rejected at commit, the GUI will keep
  offering them and the user only discovers they're disallowed on commit. Hiding
  them at the grammar level would make the GUI "just work" — it would naturally
  present only approved choices.

Ways to actually exclude options from the choices (for a future iteration) —
designed out in detail in **§5.6**:

- **Two baked CLI grammars** (FIPS vs non-FIPS), selected at boot. Cleanest UX
  but the heaviest machinery: 2× template/reftree generation, lockstep cache
  switching, a boot-time selector, and a classification list. Justified only if
  "non-approved options must be invisible/unsettable in the CLI" becomes a hard
  product/cert requirement — kept in the back pocket, not built now.
- **Model-style `cli-remove`** of specific nodes — fits the 2 whole-feature
  removals (WireGuard/PPTP) but was designed as a once-at-first-boot, model-keyed
  mechanism; using it for a reversible, config-keyed FIPS toggle needs rework
  (see §8.4) and does not naturally express per-value (cipher-list) pruning.

**Decision for now:** ship `verify()`-reject + approved defaults (compliant,
small). Revisit grammar-level exclusion if/when the GUI UX or a cert requirement
makes invisible-not-just-rejected options worth the build cost.

### 5.5 Non-approved items to enforce (block-list)

- **WireGuard, PPTP** — whole-feature (above).
- **Non-approved SSH/IPsec algorithms** — curve25519, chacha20-poly1305, ed25519
  (context-dependent), MD5/SHA-1-based MACs, DH groups below approved strength
  (modp768/1024/1536, brainpool curves, curve25519/448).
- **MD5 / DES / 3DES / RC4 / non-approved curves** anywhere (SNMPv3, legacy TLS,
  MPPE, etc.).
- **Classic RADIUS (UDP) and TACACS+** — MD5-based packet protection (§4.1);
  RADIUS is only approvable via RadSec (not implemented), TACACS+ has no
  remediation.
- Anything leaning on the OpenSSL `legacy` provider.

### 5.6 Grammar pruning — removing methods from the tree (vs. just rejecting)

This records a concrete design for the deferred wish in §5.4: **remove
non-approved crypto methods from the CLI grammar** so the GUI (which reads the
available options from the reference tree) naturally presents only approved
choices, instead of offering them and failing at commit. Captured here for a
later decision; **not** required for a first compliant cut (§5.3 is).

#### 5.6.1 What "a method in the tree" actually is (grounded)

A crypto value such as `3des` or `curve25519-sha256` is a **token in a
`<completionHelp><list>…</list>`** (often with a matching `<valueHelp>`
description and sometimes a `<regex>`/validator constraint). Examples in-tree:
`service ssh {cipher,key-exchange,mac}` ([service_ssh.xml.in](../interface-definitions/service_ssh.xml.in)),
IPsec proposal `encryption`/`hash`
([vpn-ipsec-encryption.xml.i](../interface-definitions/include/vpn-ipsec-encryption.xml.i)).

At build, each token lands in **three** consumable artifacts (see the
`interface_definitions` target in [Makefile](../Makefile)):

1. **node.def template tree** (`/opt/vyatta/share/vyatta-cfg/templates/…`) —
   shell completion (`allowed:`) and validation (`syntax:`).
2. **`reftree.cache`** — generated by `reference_tree_to_json(...)` in
   [generate_cache.py](../python/vyos/xml_ref/generate_cache.py) as **JSON**
   ("unrendered json data for loading by vyconfd"); the config daemon's
   authoritative validation source.
3. **`xml_ref` Python cache** (`pkg_cache/*_cache.py` dict) — what
   [vyos.xml_ref](../python/vyos/xml_ref/) and **the GUI** read for available
   options.

So "remove from the tree" = prune the **same token set** from all three,
consistently. **WireGuard/PPTP** are the easy case — whole-node removal, which
the Makefile already does for other nodes (e.g. `rm -rf $(TMPL_DIR)/protocols/eigrp`).
The fact that `reftree.cache` is JSON (not an opaque blob) is what makes surgical
pruning tractable.

#### 5.6.2 The central piece: one auditable policy file

Rather than scatter approved-ness across dozens of XML includes (which would also
need RNG schema changes), keep a **single FIPS algorithm allow-list**, e.g.
`data/fips/approved-algorithms.toml`, keyed by CLI leaf path → approved token
subset, plus whole-node removals:

```toml
[leaf."service ssh cipher"]
approved = ["aes128-ctr","aes192-ctr","aes256-ctr",
            "aes128-gcm@openssh.com","aes256-gcm@openssh.com"]

[leaf."service ssh key-exchange"]
approved = ["ecdh-sha2-nistp256","ecdh-sha2-nistp384","ecdh-sha2-nistp521",
            "diffie-hellman-group14-sha256","diffie-hellman-group16-sha512"]

[leaf."vpn ipsec esp-group <tagNode> proposal <tagNode> encryption"]
approved = ["aes128","aes192","aes256","aes128gcm128","aes256gcm128"]

remove_node = ["interfaces wireguard", "vpn pptp"]
```

This file **is** the compliance artifact an auditor wants — one place stating the
approved algorithm set per subsystem. It maps 1:1 onto the `<completionHelp>`
tokens, needs no schema change, and is far easier to review than annotations
spread through the grammar.

#### 5.6.3 Options for applying the policy (single image, reboot-applied)

- **Option A — Boot-time prune of the shipped artifacts (lighter).** Ship one
  full grammar + the policy. When `system option fips` is set, a boot-time tool
  prunes **working copies**: rewrite node.def `allowed:`/`syntax:` per leaf and
  `rm -rf` whole-node removals; delete non-approved tokens from the
  `reftree.cache` JSON and the `xml_ref` Python cache; delete removed subtrees.
  Keep pristine copies for toggle-off; **fail closed** if the prune fails.
  Because the caches are JSON/dict, this is a bounded transform.
  *Pro:* no artifact duplication, reversible. *Con:* a hand-edited cache can drift
  from what the daemon expects; must keep the pruner correct.

- **Option B — Regenerate from policy-pruned XML on-box (cleaner, heavier).**
  Ship the interface-definition XML + reuse `reference_tree_to_json`
  (libvyosconfig is already on the device). At FIPS boot, text-prune the XML per
  the policy, then re-run the **authoritative** generator to produce consistent
  artifacts. *Pro:* zero risk of a cache diverging from the real build path.
  *Con:* ships XML + template scripts on-box; adds a few seconds to FIPS boot.

- **Option C — Two baked grammars, boot-selected (zero runtime risk, heaviest).**
  Build normal + approved-only variants of all three artifacts; ship both; a boot
  selector activates the set by `system option fips`. *Pro:* no runtime
  generation/editing. *Con:* doubles every grammar artifact and needs lockstep
  switching; justified only if a cert explicitly demands prebuilt immutable
  grammars. (This is the "two grammars" idea parked in §5.4.)

**Lean:** Option B if shipping the XML + generator on-box is acceptable (most
robust — reuses the real build path); otherwise Option A with a JSON prune.

#### 5.6.4 The subtlety: pruning does NOT replace `verify()`

Removing a value/node from the grammar means a `config.boot` that **already
contains** it (set before enabling FIPS, or restored from backup) will **fail to
parse** — the daemon rejects unknown tokens and the box won't load its config.
So pruning must be paired, in this order, at the toggle:

1. **`verify()` gate** on the FIPS-enable commit: refuse to enable FIPS while the
   running/boot config still contains a non-approved value or a to-be-removed
   node (fail-closed, with a message naming what to change). This is the same
   `verify()` work from §5.3.
2. **Then prune** the grammar for the *next* boot, so the GUI/CLI only present
   approved options going forward.

The two mechanisms are **complementary, not alternatives**: `verify()` guarantees
the config is clean before the grammar narrows; the prune delivers the GUI/UX win
(only approved options visible). Prune-only would brick on stale config;
verify-only leaves the GUI offering disallowed options (the original §5.4
complaint).

#### 5.6.5 Guard against policy drift

Add a **build-time consistency check** (sibling of `check-properties-collision`
in [Makefile](../Makefile)) that every path/token in the policy file actually
exists in the grammar, so the allow-list cannot silently go stale when an
upstream `<completionHelp>` list is reordered or renamed.

### 5.7 Enforcement architecture — helper + policy, reject vs. default-override

This is the **recommended concrete mechanism** for §5.3, designed to avoid
rewriting every feature conf-mode script. The decisive realisation is that the
"non-approved default" problem and the "non-approved explicit value" problem need
**different** handling:

| Case | Where it lives | Handling |
|---|---|---|
| User **explicitly set** a non-approved value (`set service ssh ciphers 3des-cbc`) | VyOS config | **`verify()` reject** |
| Value **unset → baked-in default** (VyOS `defaultValue` *or* the downstream service's own compiled default) | not in config / downstream | **Emit an approved value at config generation** when FIPS is on |

**You do not validate defaults — you override them at generation time.** You
*can't* reject a default (the operator didn't choose it), and some defaults aren't
even in VyOS config. Validation is only for what the operator explicitly typed.

**Why the defaults problem is real — grounded example.** The sshd template
([sshd_config.j2](../data/templates/ssh/sshd_config.j2)) only emits a cipher line
when the user set one:

```jinja
{% if cipher is vyos_defined %}
Ciphers {{ cipher | join(',') }}
{% endif %}
```

If the user never set `service ssh ciphers`, **no `Ciphers` line is emitted and
OpenSSH falls back to its own compiled-in defaults** (curve25519,
chacha20-poly1305, …). That non-approved default is baked into *OpenSSH*, not
VyOS config — so `verify()` has nothing to inspect. The only fix is to **emit an
approved list in the template when FIPS is on**:

```jinja
{% if cipher is vyos_defined %}
Ciphers {{ cipher | join(',') }}
{% elif fips is vyos_defined %}
Ciphers aes256-gcm@openssh.com,aes128-gcm@openssh.com,aes256-ctr,aes192-ctr,aes128-ctr
{% endif %}
```

…plus a one-line reject in `verify()`. That is the whole SSH subsystem — not a
rewrite.

**The mechanism (minimal, centralised):**

1. **One policy + one helper.** The single `approved-algorithms.toml` (§5.6) holds
   the approved value sets; a new `vyos/fips.py` exposes `fips_enabled()`,
   `approved(subsystem)`, `reject_unapproved(data, policy)` (for `verify()`), and
   `apply_fips_defaults(data, policy)` (for the VyOS-`defaultValue` case, swapping
   a non-approved default for an approved one after `get_config_dict()`). The
   approved *values* live in data, not code.
2. **Two thin touchpoints per subsystem:** one `reject_unapproved(...)` call in
   `verify()`, and an approved-defaults branch (template `{% elif fips %}` or a
   Python default-swap) for the baked-in case.

**The surface is smaller than it feels** (ties to §5.2):

- **OpenSSL-backed** (GUI/nginx, OpenVPN, PKI): the **FIPS provider refuses
  non-approved algorithms at runtime** — no `verify()` reject needed; just ship
  **approved defaults** so the service negotiates cleanly instead of erroring.
- **Non-OpenSSL** (SSH, IPsec native plugins, SNMPv3): the only place the
  baked-in-default problem actually bites — a **bounded ~3–4 templates** that need
  the `{% elif fips %}` branch + a thin `verify()` reject.

**Honest total scope:** one helper + one policy file, ~3–4 FIPS-aware templates, a
handful of one-line `verify()` rejects, plus the whole-feature rejects
(WireGuard/PPTP — §5.1) and auth-backend rejects (RADIUS/TACACS+ — §4.1). Not
"rewrite all the functions."

---

## 6. Kernel & boot integrity

> **SCOPE DECISION (revised — §8.3): kernel FIPS _mode_ is IN scope; a validated
> kernel _module_ is NOT claimed.** The **validated-module** claim rests on the
> user-space OpenSSL module (cert #4985). In addition, the kernel is run in
> **FIPS mode** (`fips=1`) as **approved-mode operation / hardening** — it
> self-tests and restricts kernel crypto to approved algorithms, and makes
> `/proc/sys/crypto/fips_enabled = 1`. This is **not** a CMVP-validated kernel
> module (none is available for a Debian/am64x build — see §8.3); it is
> approved-mode *behavior*. Keep the wording precise: "kernel FIPS mode enabled,
> approved algorithms enforced," never "validated kernel."

OpenSSL-only FIPS is **not** a complete 140-3 story. The layers, in increasing
rigor:

- **Kernel crypto FIPS mode (Phase 4 — in scope)** — requires `CONFIG_CRYPTO_FIPS=y`
  (not currently in the VyOS kernel config), the `fips=1` boot parameter, and an
  **integrity-checked initramfs** (the kernel refuses `fips=1` without the HMAC of
  the crypto modules present). Enforces approved algorithms + self-tests for
  kernel-space crypto (dm-crypt, AF_ALG, IPsec ESP data-plane).
- **Module/boot integrity** — ties into **Secure Boot** (T861, already in
  vyos-build): measured/verified boot chain so the module and its config cannot
  be tampered with.
- **Entropy/DRBG** — ensure an approved SP 800-90A/B DRBG seeded from an approved
  entropy source (jitterentropy/`rngd`), especially on embedded (am64x) targets.

**Boundary nuance (with kernel FIPS mode on, module unvalidated).** Enabling
`fips=1` moves kernel-space crypto from *unrestricted* to *approved-mode* (approved
algorithms + self-test), which strengthens posture and passes "FIPS mode enabled"
checks — but the kernel module remains **unvalidated**, so these operations are
**approved-mode but not performed by a validated module**:

- **IPsec ESP data-plane** — strongSwan does IKE in user-space (route through the
  `openssl` plugin to put it in the validated module — §4.1); the **bulk ESP
  packet encryption runs in the kernel** (XFRM). With `fips=1` it uses approved
  algorithms and self-tests, but the data-confidentiality operation is not in a
  *validated* module.
- **LUKS config-encryption** — kernel dm-crypt (§6.1): approved-mode under
  `fips=1`, module unvalidated.
- **Kernel RNG** — approved DRBG under `fips=1`, module unvalidated.

So the **validated-module** claim is OpenSSL-only; kernel FIPS mode is layered on
as approved-mode operation. The claim wording must not blur the two (§8.2/§8.3).

**Single image — build-time capability vs runtime enforcement.** Building the
kernel with `CONFIG_CRYPTO_FIPS=y` **removes no algorithms**; it only *adds* the
FIPS capability (self-tests, integrity check, the `fips_enabled` flag). A kernel
built with it but running `fips=0` (the param absent) behaves **identically to a
non-FIPS kernel** — the full algorithm set is available. The restriction to
approved algorithms is a **runtime** effect of `fips=1` only. So one
`CONFIG_CRYPTO_FIPS=y` kernel serves **both** customers from a single image:

| One kernel binary | `fips` not set (non-FIPS) | `fips=1` (FIPS) |
|---|---|---|
| `CONFIG_CRYPTO_FIPS=y` | all algorithms, normal behavior | approved algorithms only, self-tests run |

This mirrors the OpenSSL provider toggle exactly (one library, provider switched
by `system option fips`) and is how RHEL/Ubuntu ship — non-FIPS users lose
nothing. Costs of the build option are limited to a slightly larger kernel
(self-test vectors), boot-time self-tests **only when `fips=1`**, and the
mandatory `.hmac`/initramfs integrity infrastructure (without which `fips=1`
panics — but `fips=0` still boots normally). None of this affects non-FIPS
runtime behavior, so the FIPS-capable kernel is safe to ship as the single image.

### 6.1 TPM interaction with FIPS

The tree has substantial TPM 2.0 work ([tpm.py](../python/vyos/tpm.py),
[tpm_pki.py](../python/vyos/tpm_pki.py),
[vyos-config-encrypt.py](../src/helpers/vyos-config-encrypt.py),
`ssh-tpm-agent`, TPM-backed OpenVPN/PKI). A TPM is **not forbidden** under FIPS —
FIPS systems routinely use TPMs — but it is a **second cryptographic module**, so
compliance depends on **what the TPM is used for** and **whether the TPM itself is
CMVP-validated**.

**The decisive rule — storage vs. operation:**

- **TPM as key *storage* only** (seal a key, then load it into the OpenSSL FIPS
  provider to perform the operation) → the TPM does **not** need to be certified;
  the approved crypto runs in the validated OpenSSL module, the TPM is just a
  safe.
- **TPM as the crypto *operator*** (the key never leaves the TPM; the TPM performs
  the RSA/ECDSA sign or decrypt) → strictly the TPM must be a **CMVP-validated
  module in approved mode** for that operation to be inside the FIPS boundary.

A non-validated TPM does **not** invalidate the OpenSSL module's certification —
it only means the TPM-performed operation is non-approved / out-of-boundary.

**Grounded reality on this platform.** Every *approved-algorithm* consumer that
touches the TPM uses it as an **operator**, and the only *storage-only* case
(WireGuard) is a feature removed in FIPS mode anyway:

| Feature | TPM's role | FIPS outcome (uncertified fTPM) |
|---|---|---|
| **OpenVPN** ([server.conf.j2](../data/templates/openvpn/server.conf.j2) renders `providers … tpm2`; [interfaces_openvpn.py](../src/conf_mode/interfaces_openvpn.py)) | **Operation** — `tpm2` provider signs in-TPM | **Out** — disable TPM-backed OpenVPN |
| **SSH host keys** ([10-ssh-tpm-agent.conf](../src/etc/ssh/sshd_config.d/10-ssh-tpm-agent.conf)) | **Operation** — TPM signs host-key challenges | **Out** — disable `ssh-tpm-agent` |
| **PKI** ([tpm_pki.py](../python/vyos/tpm_pki.py)) | **Operation** — TPM-resident key ops | **Out** — disable TPM-backed PKI |
| **WireGuard** | **Storage only** — key sealed | **Moot** — feature removed in FIPS (§5.1) |
| **IPsec** | none (seal-only proposal was rejected; no implementation) | **Nothing to do** |
| **Config encryption** ([vyos-config-encrypt.py](../src/helpers/vyos-config-encrypt.py)) | **Storage** — TPM seals the LUKS key; bulk crypto is **kernel dm-crypt** | Governed by the **kernel-FIPS decision (§6/§8.3)**, *not* the OpenSSL provider |

**Net for the current implementation + an uncertified am64x firmware TPM (fTPM):
the TPM effectively goes unused in FIPS mode.** There is no approved storage-only
use left to preserve (WireGuard is removed; the IPsec seal-only path was never
implemented), and all operator uses fall outside an uncertified module.

**Enforcement that falls out of this:**

- In FIPS mode, `verify()` **fails closed** on TPM-backed **OpenVPN**,
  **`ssh-tpm-agent`**, and **TPM-PKI**. Once blocked, the `providers … tpm2`
  branch is never rendered, so the TPM/provider conflict resolves itself.
- **Independent of TPM:** OpenVPN's template previously emitted `providers default`
  even in the non-TPM branch — explicitly loading the `default` provider would
  re-expose non-approved algorithms. It now renders **`providers fips base`** in
  FIPS mode **(landed)**. The same "is `default` loaded?" audit applies anywhere
  the tree selects OpenSSL providers.
- **Config encryption** rides the kernel-FIPS scope call (§6/§8.3), separately
  from the "disable TPM operators" rule.

**Escape hatch / decision.** The enforcement should key off **platform TPM
certification**, not hard-code "TPM off." A FIPS SKU shipping a **discrete,
CMVP-certed TPM 2.0** (e.g. Infineon SLB9670, Nuvoton NPCT75x, ST33) re-enables
all of the above — cite the TPM's cert alongside OpenSSL provider #4985. The
open platform question (parallel to the am64x OE question in §8.6): **is the
target TPM a certed discrete part or an uncertified fTPM?**

### 6.2 IPsec ESP data-plane — in-boundary options and the libipsec trade-off

A recurring question: can IPsec's **bulk (ESP) encryption** run through OpenSSL so
it is fully inside the validated boundary? **Yes — but rarely worth it.**

**The option.** strongSwan's **`kernel-libipsec`** plugin moves the ESP data path
out of the kernel into the charon userspace daemon; combined with the `openssl`
plugin, the bulk crypto is then performed by OpenSSL → the FIPS provider → fully
in-boundary (data plane included, not just IKE).

**Why it is slow — it's the data path, not the cipher.** Default kernel ESP never
leaves the kernel:

```
NIC → kernel net stack → XFRM encrypt (kernel) → NIC out
```

zero-copy, softirq context, multi-queue, often with hardware IPsec offload. The
libipsec path round-trips every packet through userspace:

```
NIC → kernel → TUN → copy UP to charon → OpenSSL encrypt
    → write DOWN to UDP socket → kernel → NIC out
```

So **per packet, both directions**, you pay two kernel↔userspace boundary
crossings (TUN read + UDP write), memory copies, context switches into/out of
charon, and serialization through a single daemon (no multi-core fan-out). The
AES itself is still fast (AES-NI on x86, ARMv8 crypto extensions on the am64x
Cortex-A53) — the cost is **packet movement**, which at small/medium packet sizes
dominates and typically yields an **order-of-magnitude** throughput drop
(multi-Gbps kernel vs hundreds of Mbps userspace) plus higher CPU and latency.

**Why it bites extra on am64x.** TI am64x parts have a hardware security
accelerator (**SA2UL**) that the **kernel** IPsec path can offload to. libipsec
bypasses both the kernel fast path **and** the accelerator, so on this modest
embedded SoC the gap between "kernel ESP + HW offload" and "userspace ESP on a
Cortex-A53" is especially punishing.

**Recommended posture.** Keep ESP in the kernel (approved algorithms via proposal
restriction + kernel `fips=1` for approved-mode), route **IKE** through the
`openssl` plugin (validated), and accept that the ESP *validated-module* claim is
the one thing given up — rather than cripple throughput. Reserve `kernel-libipsec`
for genuinely **low-throughput** tunnels where fully-in-boundary ESP is a hard
requirement.

---

## 7. Proposed implementation

### 7.1 Port the foundation + ship the validated provider (prerequisite)
- **`vyos-1x`:** bring **T8529** (`system option fips`, `openssl.cnf.j2`,
  `system_option.py` generate/verify, smoketest) into the igOS fork, adapting the
  module path to the 3.5 package layout.
  - **Reboot-safety note:** upstream T8529 writes `/run/ssl/fipsmodule.cnf` at
    *commit* time only, and `openssl.cnf` does `.include /run/ssl/fipsmodule.cnf`.
    `/run` is **tmpfs** — wiped on reboot — so after a reboot the include target is
    missing and OpenSSL config loading can **fail for every consumer**. The port
    **must** add a boot-time regeneration step (§7.2) that re-runs `fipsinstall`
    before any OpenSSL consumer starts. This is a hard requirement, not polish.
- **`vyos-build`:** ship the **validated 3.1.2 `fips.so`** via a **separate,
  dedicated provider package** (§7.5) that drops a **pinned, prebuilt** binary
  into the base's `ossl-modules/` — **not** by modifying the `openssl-3.5` build
  and **not** `enable-fips` on 3.5 (§8.1). Confirm load + `fipsinstall` self-test
  on-target (x86_64 and aarch64/am64x).

### 7.2 Make it reboot-applied and system-wide
- Treat `fips` as a **boot-time mode**: on commit, persist intent + (re)generate
  `openssl.cnf`/`fipsmodule.cnf`; require/offer a reboot to fully apply.
- Add a boot-time step (systemd unit) that: runs `openssl fipsinstall` self-test,
  (optionally) asserts kernel `fips=1`, and refuses to come up "FIPS-on" if the
  module self-test fails — i.e. **fail-closed boot**. This unit is also what
  **regenerates `/run/ssl/fipsmodule.cnf`** on every boot (it lives in tmpfs and
  is wiped — see §7.1 note); without it a FIPS-enabled box breaks on first reboot.
  - **Implemented (Phase 1): controlled fail-closed.** On self-test failure the
    boot helper ([src/system/fips-regenerate.sh](../src/system/fips-regenerate.sh))
    runtime-masks the network-exposed crypto services
    (`ssh`/`ssh@`/`ssh-tpm-service`, `strongswan`, `openvpn@`, `nginx`,
    `vyos-http-api`) so they do not operate in an unverified state, raises a loud
    console/syslog message, and drops a persistent failure marker
    (`/run/vyos-fips-selftest-failed`) surfaced by `show system fips` — while
    leaving **console getty** up for recovery. The mask lives in `/run` and
    clears on reboot so the self-test is retried. (Chosen over hard
    emergency/halt to avoid bricking the headless am64x appliance.)

#### 7.2.1 Reboot handling on toggle (two traps + the pattern)

The provider switch only fully applies once every process restarts under the new
`openssl.cnf`, so a FIPS toggle is **reboot-applied**. VyOS already has the
precedent in [system_option.py](../src/conf_mode/system_option.py) `apply()` (the
kexec path): on a normal interactive commit it **does not auto-reboot — it warns
"save the configuration and reboot."** Two traps drive that choice:

1. **Commit ≠ save.** A `commit` puts `fips` in the *running* config but not in
   `config.boot` until `save`. Auto-rebooting after commit would come back up
   **without** FIPS — the toggle is silently lost. Any forced reboot **must save
   first**.
2. **Boot-loop.** conf-mode scripts re-run on every boot as config is replayed.
   "Reboot whenever `fips` is set" would loop forever. Guard with
   `boot_configuration_complete()` (False during boot) **plus** a real-change
   check against the previously-committed value.

**Detect a real toggle** with the `exists_effective` idiom (as other scripts do),
but capture it in `get_config()` so `apply()` never creates a second `Config`
instance (enforced by `test_configd_inspect`):

```python
def get_config(config=None):
    conf = config or Config()
    options = conf.get_config_dict(['system', 'option'], ...)
    # record the previously-committed value using the single Config instance
    options['fips_effective'] = conf.exists_effective(['system', 'option', 'fips'])
    return options

def apply(options):
    if boot_configuration_complete():
        if ('fips' in options) != options.get('fips_effective', False):
            Warning('FIPS mode changed — save config and reboot to apply.')
```

At boot, `boot_configuration_complete()` is False → skipped (no loop); the
boot-time FIPS unit above is what brings a fresh boot up fully FIPS.

**How to "force" the reboot — three options, safest first:**

1. **Warn + self-correcting boot-state marker (recommended default, shipped).**
   On a real toggle, `Warning('FIPS mode changed — save config and reboot to
   apply.')`. The pending state is tracked **level-triggered**: the boot-time
   unit records the FIPS mode the system actually came up in via a **runtime**
   (`/run/vyos-fips-booted`, tmpfs) marker, recreated every boot, and op-mode
   `show system fips` derives `reboot_required = (configured != booted)`. Because
   the marker lives in `/run` and is rewritten each boot, the pending state can
   never go stale across a reboot (an earlier persistent `/config` marker design
   wrongly survived reverts and normal applies). The box is never silently
   half-FIPS and the state is detectable in op-mode / a login banner.
2. **Interactive confirm → save → reboot (the real "force").** Only when
   `boot_configuration_complete()` **and** `sys.stdin.isatty()`: prompt, then
   `conf.save_config()` (must precede) and `cmd('systemctl reboot')`. The
   `isatty()` guard prevents an unattended reboot on API / scripted /
   commit-confirm runs.
3. **Unconditional auto-reboot — rejected.** Breaks non-interactive commits and
   loses the setting without a save.

**Recommendation:** ship option 1 as the baseline and offer option 2 as the
explicit interactive path. "Force the reboot" becomes "make the reboot
necessary, sufficient, and detectable" — the boot-time unit guarantees
correctness rather than yanking power at commit. The same toggle detection is
where `verify()` runs the §5.3/§5.7 reject checks and `generate()` applies the
approved defaults.

### 7.3 Algorithm policy + enforcement
- Use the **helper + central policy** mechanism (§5.7): `vyos/fips.py` +
  `approved-algorithms.toml`, with **`verify()` reject** for explicitly-set
  non-approved values and **approved defaults emitted at generation** for the
  baked-in-default case.
- Generate FIPS-constrained config for each consumer in §4 (sshd Ciphers/MACs/
  KEX, strongSwan proposals, nginx cipher suites, SNMPv3 auth/priv), plus
  **approved defaults** so OpenSSL-backed consumers negotiate cleanly (§5.2).
- Extend the relevant `verify()` paths to **reject non-approved features** in
  FIPS mode (§5.3), starting with **WireGuard** (and **PPTP**), and to reject
  explicitly-set non-approved crypto-method values for the non-OpenSSL consumers.
- **TPM (§6.1):** on an uncertified TPM, `verify()` also fails closed on
  TPM-backed **OpenVPN**, **`ssh-tpm-agent`**, and **TPM-PKI**; and OpenVPN's
  `providers default` must render **`providers fips base`** in FIPS mode. Key the
  TPM rule off platform TPM certification, not a hard-coded "TPM off."
- **Auth backends (§4.1):** `verify()` rejects **classic RADIUS** and **TACACS+**
  in FIPS mode (SAML is the approved login); RADIUS is only re-enabled via RadSec
  (`radsecproxy`, not yet implemented — §8.8).

### 7.4 Observability
- `show system fips` (op-mode): provider status (`openssl list -providers`),
  self-test result, kernel fips state, and a summary of enforced restrictions.
- Replace the upstream "incomplete" warning with an accurate status once the
  scope below is met.

### 7.5 Packaging the validated provider: separate package, built from pinned source

**Decision: ship `fips.so` as its own dedicated Debian package — NOT folded into
the `openssl-3.5` package — built from PINNED OpenSSL 3.1.2 source (`enable-fips`)
per-arch, with a pre-staging escape hatch for a vendor-supplied validated
binary.** At runtime a separate package and a folded-in file land the same
`fips.so` in the same `ossl-modules/` and behave identically; the separation is a
build-hygiene / compliance decision and wins on every axis:

- **Keep the validated artifact decoupled.** The base 3.5 package must stay free
  to be rebuilt for CVEs (§0.4) while the provider is produced from *fixed*
  inputs. Folding `fips.so` into libssl/openssl re-entangles the two lifecycles —
  every base security rebuild would also rebuild the package *containing* the
  provider. Separate package = bump 3.5 anytime without touching the provider,
  and vice versa.
- **No divergence in Debian-derived packaging.** `openssl-3.5` stays a clean
  rebase of Debian's packaging; no patching of `debian/libssl3.install`/`rules`
  to inject a file.
- **No fragile cross-package ordering.** The provider package is self-contained:
  it builds 3.1.2 and packages the result in one unit.
- **Auditability.** An independently-versioned provider package that records the
  `fips.so` SHA (and pins the OpenSSL commit) makes "what exactly did we ship?" a
  one-line check.

**Pin the inputs, not a hand-staged binary.** The provider is produced from
**pinned 3.1.2 source** (git tag `openssl-3.1.2`, commit
`17a2c5111864d8e016c5f2d29c40a3746b559e9d`) with `enable-fips`, built in the
**matching-arch** container exactly like the other `package-build-iGOS` packages
(so `./Configure` natively targets x86_64 / am64x — no cross-compile). The build
records the resulting `fips.so` SHA into `fips/SHA256SUMS`. If a vendor-supplied
validated binary is dropped into `fips/<arch>/fips.so` by hand, the source build
is **skipped** and that binary is used as-is.

> Compliance note: for a defensible 140-3 claim the `./Configure` options MUST
> match the cert #4985 Security Policy and the build host must be a covered
> Operational Environment (§8.1/§8.6). `enable-fips` alone yields a *functional*
> provider for engineering bring-up; the pre-staging hatch is the path for a
> vendor/lab-supplied validated binary.

```
openssl-3.5/            → base libssl/libcrypto (floats, CVE rebuilds)   [UNCHANGED]
openssl-fips-provider/  → build pinned 3.1.2 source → fips.so → ossl-modules/ [per-arch]
                          (or pre-stage a vendor validated fips.so)
vyos-1x (T8529)         → openssl.cnf activation on `set system option fips`
```

Concrete layout (`scripts/package-build-iGOS/openssl-fips-provider/`):
- `package.toml` — `build_mode = "local_app"`; `build_cmd` runs
  `produce-fips-provider.sh` then `dpkg-buildpackage`; `[dependencies]` pulls
  `build-essential`, `perl`, `git`.
- `produce-fips-provider.sh` — clones the pinned OpenSSL tag, verifies the
  commit, `./Configure enable-fips && make`, stages `providers/fips.so` into
  `fips/<arch>/`, records `fips/SHA256SUMS`. Idempotent / pre-stage aware.
- `debian/` — thin binary package `igos-openssl-fips-provider`
  (`Depends: libssl3 (>= 3.0)`, `Multi-Arch: same`, `Architecture: any`) that
  installs only `/usr/lib/${DEB_HOST_MULTIARCH}/ossl-modules/fips.so` and
  refuses to ship a placeholder (ELF + SHA checks in `debian/rules`).
- `fips/<arch>/` — build output (or hand-staged vendor binary) + `SHA256SUMS`.

Packaging notes:
- The base openssl package owns the shared `ossl-modules/` dir (it ships
  `legacy.so`); adding `fips.so` there is a different filename → **no `Conflicts`
  needed**.
- Register the binary package name in each FIPS-capable build flavor's
  `packages = [...]` (live-build only installs *referenced* local debs).
- Do **not** ship a prebuilt `fipsmodule.cnf` — its integrity MAC is computed over
  the exact `fips.so`; the T8529 layer runs `openssl fipsinstall` on-box to
  generate it for the shipped binary (§0.3).

---

## 8. Hard problems / open decisions

### 8.1 Provider artifact & build integration (compliance boundary)
The **validated** artifact is the **3.1.2** FIPS provider (cert #4985). igOS
already ships its **own OpenSSL 3.5 base** (`package-build-iGOS/openssl-3.5/`)
with **no FIPS provider** today. The decision is therefore *not* "which OpenSSL
version" — the base stays 3.5 — but **how to obtain and ship the validated
3.1.2 `fips.so`** alongside it.

The 3.5-base + 3.1.2-provider split is explicitly the OpenSSL-sanctioned model.
From OpenSSL `README-FIPS.md` (§12):

> "Newer OpenSSL Releases … can be used to build all other components … without
> any restrictions, but the **FIPS provider must be built as specified in the
> Security Policy (normally with a different version of the source code).**"

- **Preferred:** ship the **validated 3.1.2 `fips.so`** as tested (the artifact
  behind cert #4985), installed into the 3.5 base's `ossl-modules/` directory,
  activated via `openssl.cnf`. The base floats (3.5 + CVE fixes); the validated
  module is frozen. This is the intended, cleanest boundary (§0.4).
- **Avoid:** `enable-fips` on the 3.5 build — it yields a **different,
  unvalidated** 3.5 provider that does **not** carry cert #4985. (This is the
  subtle trap in upstream VyOS's T9040, which built a provider from the 3.0.x
  base rather than shipping the validated 3.1.2 one.)

**Sourcing the validated `fips.so` — three paths.** You do not "download the
blessed binary" and inherit validation on arbitrary hardware: validation binds
the *source* + the *build procedure in the Security Policy* + the *operational
environment (OE)* on the cert. Options:

1. **Build it yourself from validated source, per the Security Policy** (§12).
   Download `openssl-3.1.2` source, build with `enable-fips` **exactly** as the
   SP dictates, `make install_fips` → `fips.so` + `fipsinstall`. Produces a
   functionally-FIPS module and — *if* the build matches the SP *and* the OE is
   covered — the validated one.
2. **Obtain a vendor-supplied validated module + OE coverage.** OpenSSL
   Corporation's commercial FIPS support / **rebranding + add-OE** program (lets
   you carry the validation under your own identity and add your environments).
   Distro FIPS modules (RHEL, Ubuntu Pro) are tied to *their* OE + licensing and
   are **not** liftable into Debian-based igOS.
3. **Validate / add your OE via a CMVP (NVLAP-accredited) lab** — full validation
   or leveraged/OE-addition naming your appliance's environment.

**OE caveat (the thing that actually gates the claim).** Cert #4985 lists
specific tested OEs. igOS runs **x86_64** *and* **am64x (aarch64)**. If those
exact OS+CPU combos are not on the cert, a strict "validated" claim needs either
**vendor affirmation** (FIPS 140-3 IG porting rules — a claim you make, reduced
assurance) or lab/vendor OE addition (Path 2/3). Embedded ARM is the usual
sticking point — do **not** assume am64x is covered.

**Build-for-engineering vs procure-for-claim.** Use **Path 1 now** to unblock all
implementation (package the `fips.so`, wire the T8529 toggle, prove `fipsinstall`
self-test + `openssl list -providers` on **both** x86_64 and am64x). Run **Path 2
or 3 in parallel** as a procurement/legal track for the actual sellable claim —
that, especially am64x OE coverage, is the long pole, not the engineering.

*Confirm:* exact validated version/revision + tested OEs on the cert; source of
the shipped `fips.so`; and that a 3.1.2-era provider loads against the 3.5
`ossl-modules/` tree (ABI is stable across 3.x providers, but validate on-target).

### 8.2 Validation claim scope
"Certified" vs "uses a validated module, operated in approved mode." Fix the exact
customer-facing wording and the operational-environment constraints the cert
lists. *Decision: legal/marketing-aligned claim + the list of approved
algorithms we advertise.*

**Critical distinction — FIPS _mode_ vs validated _module_** (drives all claim
wording): a CMVP certificate validates a specific **module** build/OE; `fips=1`
(user-space provider activation or kernel `fips=1`) is runtime **approved-mode
behavior**. They satisfy different requirements:
- *"Uses a FIPS 140-3 **validated** cryptographic module, operated in approved
  mode"* — satisfied by the OpenSSL provider (cert #4985). This is the defensible
  validated-module claim.
- *"Operates in FIPS **mode** / approved algorithms enforced"* — satisfied by
  activating the provider **and** kernel `fips=1`; passes checkbox-style
  "`fips_enabled=1`" requirements. Does **not** by itself assert a validated
  kernel module.

Do not conflate: the validated-module claim is **OpenSSL-only**; kernel `fips=1`
is approved-mode operation, not validation. Never word kernel FIPS mode as
"validated kernel."

### 8.3 How deep does the compliance bar go (§6)
**RESOLVED: validated-module claim is user-space-OpenSSL; kernel FIPS _mode_ is
additionally enabled (Phase 4 — IN scope) as approved-mode hardening.** The
**validated boundary** is the OpenSSL module (#4985). On top of that, the kernel
is run with `fips=1` so kernel-space crypto (IPsec ESP data-plane, LUKS,
kernel RNG) operates in **approved mode** (approved algorithms + self-tests) and
`fips_enabled=1` — strengthening posture and satisfying "FIPS mode enabled"
requirements. A **CMVP-validated kernel module is NOT claimed** (none is available
for a Debian/am64x build without self-funding a validation — §8-notes). This adds
real engineering (Phase 4: kernel `CONFIG_CRYPTO_FIPS`, `fips=1` cmdline,
integrity-checked initramfs) but no validation paperwork; the claim wording stays
per §8.2 (approved-mode, not validated, for the kernel).

### 8.4 Non-approved feature policy (§5)
Resolved to the lighter design in §5: only **WireGuard** and **PPTP** are
whole-feature removals; everything else is crypto-method restriction enforced in
conf_mode. Recommended UX is a **hard commit error** (fail closed) via `verify()`
plus approved defaults. **Open / parked:** whether to additionally *exclude*
non-approved options from the CLI grammar so the **GUI** (which reads available
options from the reftree) presents only approved choices — desirable for UX but
deferred because it requires grammar-pruning machinery. **A concrete design for
this (three artifacts, central policy file, Options A/B/C, verify()+prune
ordering) is recorded in §5.6.** Revisit if GUI UX or a cert requirement makes
invisible-not-just-rejected options worth the build cost.

### 8.5 Password hashing
**Resolved (LANDED 2026-11-05):** the FIPS-mode local-auth KDF is **SHA-512-crypt**
(`$6$`). There is no operator-selectable hash method, so enforcement is internal:
`src/conf_mode/system_login.py` pins `sha512_crypt` under FIPS for both the
plaintext-password and the NVMEM sticker-password hashing paths; an
operator-supplied `encrypted-password` is rejected unless it is SHA-2-crypt
(`$5$`/`$6$`); and `python/vyos/initialsetup.py` (installer/first-boot provisioning)
emits SHA-512-crypt when the seed config has `system option fips`. Non-FIPS keeps
the stronger (but unapproved) `yescrypt`. Migration/interop: toggling FIPS does
not rewrite existing shadow hashes; a password is re-hashed with the approved KDF
on its next change (and the sticker/first-login-change flow re-derives it).

### 8.6 Embedded target (am64x)
Confirm the validated provider's operational environment covers aarch64/the SoC,
entropy source adequacy, and Secure Boot availability on the hardware.

### 8.7 TPM certification (§6.1)
Is the target TPM a **discrete, CMVP-certed TPM 2.0** or an **uncertified
firmware TPM (fTPM)**? This decides whether TPM-backed operator uses (OpenVPN,
`ssh-tpm-agent`, TPM-PKI) are in-boundary or must be fail-closed in FIPS mode. On
the current implementation + an uncertified fTPM the TPM goes effectively unused
in FIPS mode; a discrete certed TPM re-enables it (cite its cert alongside
#4985). Keep the enforcement keyed off platform TPM certification, not a
hard-coded disable. *Decision: per-SKU hardware choice.*

### 8.8 RADIUS / TACACS+ and RadSec (§4.1)
Classic RADIUS and TACACS+ are MD5-based and non-approvable; the tree has **no
RadSec** today (all RADIUS is classic UDP via `pam_radius_auth` / accel-ppp).
Decisions: (a) confirm the FIPS-mode auth posture is **SAML-only for admin login**
with RADIUS/TACACS+ rejected; (b) whether broadband/subscriber use cases require
RADIUS under FIPS — if so, implement **RadSec via a local `radsecproxy`** (package
in vyos-build + CLI + config generation + PKI cert wiring + point clients at
localhost). TACACS+ has no remediation path. *Decision: scope + whether RadSec is
in/out for the first release.*

---

## 9. Phasing

- **Phase 0 — Port + parity.** Land the T8529 CLI/config layer in the igOS
  `vyos-1x` fork **and** add the new **`openssl-fips-provider`** package that ships
  the pinned validated 3.1.2 `fips.so` (§7.5/§8.1); `set system option fips`
  activates the provider (OpenSSL only), keeping the "incomplete" warning.
  Establishes the single-image toggle on the 3.5 base.
- **Phase 1 — Reboot-applied, fail-closed boot + observability.** Add boot-time
  module self-test, reboot-to-apply semantics, fail-closed activation (refuse to
  come up FIPS-on if the self-test fails), and `show system fips`.
- **Phase 2 — System-wide approved operation.** FIPS algorithm policy for SSH,
  IPsec, GUI/nginx, SNMP, Python consumers (§4).
- **Phase 3 — Fail-closed enforcement.** `verify()` refuses non-approved features
  (WireGuard + PPTP first), restricts crypto-method values for non-OpenSSL
  consumers (SSH/IPsec/SNMP), rejects classic **RADIUS/TACACS+** admin auth
  (§4.1), rejects TPM-backed operator uses on an uncertified TPM
  (OpenVPN/`ssh-tpm-agent`/TPM-PKI — §6.1), approved-KDF password hashing (§8.5).
  Drop the "incomplete" warning.
- **Phase 3a (conditional) — RadSec.** If RADIUS is required under FIPS, add
  `radsecproxy` + CLI/config/PKI wiring so RADIUS runs over TLS (§4.1/§8.8).
- **Phase 4 — Kernel FIPS mode (approved-mode operation; NOT a validated kernel
  module). IN scope (§8.3).** `vyos-build`: kernel `CONFIG_CRYPTO_FIPS=y` +
  integrity-checked initramfs (HMAC `.hmac` for kernel + crypto modules) +
  approved DRBG/entropy (jitterentropy), optionally Secure Boot (T861) for the
  integrity chain. `vyos-1x`: add **`fips=1`** to the kernel cmdline when
  `system option fips` is set (via the existing `system_option.py` cmdline/kexec
  path). Result: kernel-space crypto self-tests + enforces approved algorithms and
  `fips_enabled=1`. **No validated-kernel claim** — approved-mode only (§6/§8.2).
  Fail-closed if the kernel refuses `fips=1` (missing integrity → boot panic), so
  the initramfs HMAC infrastructure is a hard prerequisite.

---

## 10. Affected components (reference)

- `vyos-1x`
  - `interface-definitions/system_option.xml.in` — `fips` leaf (port T8529).
  - `src/conf_mode/system_option.py` — generate/verify + reboot-apply semantics
    (toggle detection via `exists_effective` + `boot_configuration_complete`,
    persistent reboot-required marker, optional interactive save+reboot — §7.2.1).
  - `data/templates/openssl/openssl.cnf.j2` — provider activation (port T8529).
  - **`python/vyos/fips.py`** (new) + **`data/fips/approved-algorithms.toml`**
    (new) — central helper + policy for the enforcement mechanism (§5.7).
  - New op-mode `show system fips` + `op-mode-definitions` (also surfaces
    "reboot pending" from the marker).
  - `verify()` in feature conf-mode scripts (WireGuard, PPTP, SSH, IPsec, SNMP,
    …) — FIPS fail-closed enforcement (whole-feature reject for WireGuard/PPTP;
    crypto-method value restriction for the rest — §5.3/§5.7).
  - **FIPS-aware templates (§5.7)** — `{% elif fips %}` approved-defaults branch
    for the non-OpenSSL consumers whose defaults are baked in downstream
    (`data/templates/ssh/sshd_config.j2`, strongSwan proposals, SNMPv3) — ~3–4.
  - **TPM-backed conf-mode paths (§6.1)** — `interfaces_openvpn.py` (OpenVPN TPM
    reject **landed**), `service_ssh.py`/`ssh-tpm-agent`,
    `tpm_pki.py`/`src/op_mode/tpm_pki.py`: fail-closed `verify()` on operator uses
    when FIPS + uncertified TPM.
  - `data/templates/openvpn/server.conf.j2` — `providers default` →
    `providers fips base` in FIPS mode (and no `tpm2` branch — §6.1). **Landed.**
  - **Auth backends (§4.1)** — `src/conf_mode/system_login.py`: fail-closed
    `verify()` on classic RADIUS/TACACS+ in FIPS mode (SAML remains). accel-ppp
    RADIUS consumers (PPPoE/L2TP/IPoE) likewise. RadSec (`radsecproxy`) is the
    remediation if RADIUS is required (§8.8) — not implemented.
  - New boot-time systemd unit for self-test / fail-closed activation.
  - **Kernel FIPS mode (Phase 4, §8.3)** — `src/conf_mode/system_option.py`: add
    `fips=1` to the kernel cmdline when `system option fips` is set (reuse the
    existing cmdline/kexec path), so the kernel enters approved mode on the same
    toggle.
- `vyos-build`
  - **`scripts/package-build-iGOS/openssl-fips-provider/`** — **DONE (Phase 0):**
    standalone package that builds the OpenSSL **3.1.2** FIPS provider from pinned
    source (`enable-fips`) per-arch and installs `fips.so` into `ossl-modules/`;
    pre-stage hatch for a vendor validated binary (§7.5/§8.1). Self-test verified
    (`fipsinstall` → INSTALL PASSED).
  - **`data/build-flavors/igos-am64x-all.toml`** — `igos-openssl-fips-provider`
    added to `packages` (**DONE**); add to other FIPS-capable flavors as needed.
  - **`scripts/package-build-iGOS/openssl-3.5/`** — **unchanged** (base library;
    no `enable-fips`, no `fips.so`).
  - **`radsecproxy`** (conditional, §4.1/§8.8) — new package if RADIUS is required
    under FIPS (RadSec/TLS wrapper for the UDP RADIUS clients).
  - **Kernel config + initramfs (Phase 4, §8.3)** — `CONFIG_CRYPTO_FIPS=y`,
    integrity-checked initramfs (kernel + crypto-module `.hmac`), approved
    DRBG/entropy; **approved-mode only, not a validated kernel module**.
  - **Secure Boot (T861) tie-in** — optional, for the integrity chain backing
    kernel FIPS mode.

---

## 11. Non-goals (for this document)

- A separate `-fips` image or any divergent software path (explicitly rejected).
- Live (no-reboot) entry/exit of FIPS mode.
- A **CMVP-validated kernel crypto module**. Kernel **FIPS mode** (`fips=1`,
  approved-mode operation) **is** in scope (Phase 4, §6/§8.3), but no validated
  *kernel module* is claimed — the validated-module claim is OpenSSL-only.
  Kernel-space crypto (IPsec ESP data-plane, LUKS, kernel RNG) is approved-mode
  but not performed by a validated module.
- Obtaining a *new* CMVP validation for igOS itself (we consume the OpenSSL
  provider's existing validation unless §8.2 dictates otherwise).
- Changing cryptographic defaults for non-FIPS customers.

---

## 12. References

### Build & compliance (the FIPS provider)
- **OpenSSL `README-FIPS.md` (3.1.2 tag) — practical build guide** (`enable-fips`,
  `make install_fips`, `fipsinstall`; and the "newer base + validated-provider
  source" split):
  <https://github.com/openssl/openssl/blob/openssl-3.1.2/README-FIPS.md>
- **CMVP certificate #4985 → Security Policy — the *authoritative*,
  compliance-governing build instructions and tested OEs** (follow the SP PDF
  linked from this page):
  <https://csrc.nist.gov/projects/cryptographic-module-validation-program/certificate/4985>
- **OpenSSL `fips_module(7)` — configuring the system to *use* the module**
  (the `openssl.cnf`/`fipsinstall` activation side, i.e. the T8529 layer):
  <https://docs.openssl.org/master/man7/fips_module/>
- **OpenSSL source / FIPS landing page — which versions are validated + links to
  their Security Policies:** <https://www.openssl.org/source/>
- OpenSSL FIPS 140-3 validation announcement (cert #4985, 3.1.2 provider; 3.0–3.5
  base compatibility): <https://openssl-library.org/post/2025-03-11-fips-140-3/>

### VyOS prior art
- VyOS T8529 — CLI to enable OpenSSL FIPS: `vyos/vyos-1x#5139`,
  <https://vyos.dev/T8529>
- VyOS T9040 — build FIPS-provider OpenSSL: `vyos/vyos-build#1233`,
  <https://vyos.dev/T9040>
- VyOS T8508 — build OpenSSL 3.1.2 (not merged): `vyos/vyos-build#1161`,
  <https://vyos.dev/T8508>
- VyOS T861 — Secure Boot: `vyos/vyos-build#1223`
