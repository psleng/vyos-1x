# igOS FIPS 140-3 — Design Sketch

Status: design draft (no implementation yet)
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

---

## 6. Kernel & boot integrity

OpenSSL-only FIPS (today's VyOS state) is **not** a complete 140-3 story. Options,
in increasing rigor:

- **Kernel crypto FIPS mode** — requires `CONFIG_CRYPTO_FIPS=y` (currently not in
  the VyOS kernel config), the `fips=1` boot parameter, and an **integrity-checked
  initramfs** (the kernel refuses `fips=1` without the HMAC of the crypto modules
  present). Affects kernel-space crypto (dm-crypt, AF_ALG, kernel IPsec if used).
- **Module/boot integrity** — ties into **Secure Boot** (T861, already in
  vyos-build): measured/verified boot chain so the validated module and its
  config cannot be tampered with.
- **Entropy/DRBG** — ensure an approved SP 800-90A/B DRBG seeded from an approved
  entropy source (jitterentropy/`rngd`), especially on embedded (am64x) targets.

Decision needed (§8): does the target compliance claim require kernel FIPS mode
and boot integrity, or is "all user-space crypto via the validated OpenSSL
module + disabled non-approved features" the bar? This materially changes effort.

---

## 7. Proposed implementation

### 7.1 Port the foundation + ship the validated provider (prerequisite)
- **`vyos-1x`:** bring **T8529** (`system option fips`, `openssl.cnf.j2`,
  `system_option.py` generate/verify, smoketest) into the igOS fork, adapting the
  module path to the 3.5 package layout.
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
  module self-test fails — i.e. **fail-closed boot**.

### 7.3 Algorithm policy + enforcement
- Generate FIPS-constrained config for each consumer in §4 (sshd Ciphers/MACs/
  KEX, strongSwan proposals, nginx cipher suites, SNMPv3 auth/priv), plus
  **approved defaults** so OpenSSL-backed consumers negotiate cleanly (§5.2).
- Extend the relevant `verify()` paths to **reject non-approved features** in
  FIPS mode (§5.3), starting with **WireGuard** (and **PPTP**), and to reject
  explicitly-set non-approved crypto-method values for the non-OpenSSL consumers.

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

### 8.3 How deep does the compliance bar go (§6)
User-space-OpenSSL-only, or kernel `fips=1` + boot integrity + Secure Boot?
Drives whether this is weeks or a quarter. *Decision needed before scoping.*

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
`yescrypt` is not FIPS-approved; decide the FIPS-mode local-auth KDF (e.g.
SHA-512-crypt / PBKDF2) and how migration/interop works when toggling.

### 8.6 Embedded target (am64x)
Confirm the validated provider's operational environment covers aarch64/the SoC,
entropy source adequacy, and Secure Boot availability on the hardware.

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
  consumers (SSH/IPsec/SNMP), approved-KDF password hashing (§8.5). Drop the
  "incomplete" warning.
- **Phase 4 (conditional) — Kernel FIPS + boot integrity.** `CONFIG_CRYPTO_FIPS`,
  `fips=1`, integrity-checked initramfs, Secure Boot tie-in — only if §8.3
  requires it.

---

## 10. Affected components (reference)

- `vyos-1x`
  - `interface-definitions/system_option.xml.in` — `fips` leaf (port T8529).
  - `src/conf_mode/system_option.py` — generate/verify + reboot-apply semantics.
  - `data/templates/openssl/openssl.cnf.j2` — provider activation (port T8529).
  - New op-mode `show system fips` + `op-mode-definitions`.
  - `verify()` in feature conf-mode scripts (WireGuard, PPTP, SSH, IPsec, SNMP,
    …) — FIPS fail-closed enforcement (whole-feature reject for WireGuard/PPTP;
    crypto-method value restriction for the rest — §5.3).
  - New boot-time systemd unit for self-test / fail-closed activation.
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
  - Kernel config + initramfs for Phase 4 (conditional).
  - Secure Boot (T861) tie-in for integrity (conditional).

---

## 11. Non-goals (for this document)

- A separate `-fips` image or any divergent software path (explicitly rejected).
- Live (no-reboot) entry/exit of FIPS mode.
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
