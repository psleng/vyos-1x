# igOS Overlay (Writable-Layer) Encryption — Design Sketch

Status: **Design draft — no implementation yet (2026-11-11)**

Scope: encrypt **everything the system writes** on the igOS AM64x product by
placing the live-boot **overlay upper/work layer** inside a LUKS container,
unlocked early in boot from the on-board firmware TPM (fTPM) with a recovery-key
fallback. The read-only OS (squashfs) stays plaintext (integrity via dm-verity),
and the boot artifacts stay plaintext-but-signed (Secure Boot). This is the
right-sized "data encrypted at rest" story for this platform and a natural
generalization of the existing **config-file encryption**
([vyos-config-encrypt.py](../src/helpers/vyos-config-encrypt.py)) from `/config`
to the entire writable layer — which it **supersedes**.

This is a cross-repo effort: the unlock hook and image layout live in
`vyos-build` (initramfs + partitioning); this repo (`vyos-1x`) owns the
op-mode/management surface, the key-sealing primitives
([python/vyos/tpm.py](../python/vyos/tpm.py)), and FIPS policy.

---

## 0. Context / grounding facts

### Verified on the reference unit (`igos@10.10.50.165`) — 2026-11-11

- **Boot/storage is VyOS live-boot on eMMC `mmcblk0` (14.3 GB):**
  - `p1` 256K (TI firmware), `p2` 512M **EFI/vfat** (boot), `p3` 13.5G **ext4**
    = the persistence partition (`/lib/live/mount/persistence`).
  - The OS root is a **read-only squashfs** (`loop0`, ~694 MB) overlaid with a
    writable layer. Confirmed root mount:
    ```
    overlay / overlay lowerdir=/live/rootfs/<ver>.squashfs/,
                      upperdir=/live/persistence/boot/<ver>/rw,
                      workdir=/live/persistence/boot/<ver>/work
    ```
  - Everything writable — `/config`, logs, app state, **and `/var/lib/tee`** —
    lands in `…/boot/<ver>/rw` on **p3**. `/config` is just a path inside that
    same overlay upper.
- **Boot artifacts are plaintext + signed.** `…/boot/<ver>/` holds `vmlinuz`,
  `initrd.img`, `dtb/`, `grub/` each with a `.sig`; GRUB verifies kernel +
  initramfs + DTB (Secure Boot). These **must** remain readable by the boot
  chain — encryption can never cover them (integrity, not secrecy, is their
  protection).
- **The fTPM's secure storage lives in the writable layer.** `/var/lib/tee`
  resolves to `…/rw/var/lib/tee` and contains opaque numbered blobs (`file` →
  `data`) — the **OP-TEE REE-FS** secure-storage objects. The "TPM" here is a
  **firmware TPM in OP-TEE TrustZone**, not a discrete chip.
- **Hardware AES is present.** Cores are **Cortex-A53 ×2** with ARMv8 Crypto;
  `/proc/crypto` serves `xts(aes)` via **`xts-aes-ce`** (HW-accelerated AES-XTS).
- **Per-image isolation.** Each installed image has its own
  `boot/<ver>/{squashfs,rw,work,vmlinuz,initrd.img,…}` on p3.
- **Prior art.** [vyos-config-encrypt.py](../src/helpers/vyos-config-encrypt.py)
  already does TPM-sealed LUKS for `/config` using
  `write_tpm_key`/`read_tpm_key` ([python/vyos/tpm.py](../python/vyos/tpm.py),
  PCR-sealed at NV handle `0x81000000`) + a recovery key in a second LUKS slot.
  This design reuses those primitives at a larger scope.

### Why "overlay encryption", not "full disk"
"Full disk" is a misnomer on this layout: `p2`/boot must stay plaintext, and the
squashfs is **non-secret public OS code** whose need is *integrity* (dm-verity),
not confidentiality. Encrypting the squashfs wastes cycles and forces an awkward
dm-crypt↔dm-verity layering. The **only** thing worth encrypting is the
**writable delta** — which is exactly the overlay upper. See the TPM-PKI doc
[igos-tpm-pki-integration-design.md](./igos-tpm-pki-integration-design.md) §6 and
the FIPS doc [igos-fips-140-3-design.md](./igos-fips-140-3-design.md) for the
related boot-integrity and FIPS posture.

---

## 1. Architecture

Put the overlay **upperdir + workdir** inside a LUKS container (an ext4
filesystem inside a LUKS image file on p3, exactly as config-encryption does for
`/config`, just scoped to the whole writable layer). The root overlay is then
assembled with its upper on the *decrypted* volume.

```
            p2 (EFI)  ──► GRUB (signed) ──► kernel + initramfs + dtb  (plaintext, SIGNED)
                                                   │
 p3 (ext4, plaintext):                             ▼  initramfs:
   boot/<ver>/<ver>.squashfs   (RO OS) ───────────┐   1. mount squashfs (lower)   [+ dm-verity]
   boot/<ver>/var-tee/  (plaintext, HUK-encrypted)│   2. fTPM unseal LUKS key  ◄── /var/lib/tee
   boot/<ver>/luks-upper  (LUKS container) ────────┘      (carved out — see §2)
            │  cryptsetup open                            3. open luks-upper → ext4
            ▼                                             4. overlay: lower=squashfs,
   /dev/mapper/vyos_overlay  → ext4 (upper + work) ──────────► upper/work=decrypted vol
                                                             5. mount overlay as root, pivot
```

Result:
- **Encrypted:** all writes — `/config`, logs, runtime/app state, user data.
- **Plaintext:** the squashfs OS (non-secret; integrity via dm-verity), the boot
  artifacts (signed), and the carved-out `/var/lib/tee` (OP-TEE-encrypted blobs).
- **Supersedes** per-`/config` encryption: `/config` is inside the encrypted
  upper, so there is no separate config container.

---

## 2. The `/var/lib/tee` carve-out (fTPM bootstrap — the key design move)

**Problem.** For TPM auto-unlock, the initramfs must unseal the LUKS key from the
fTPM. The fTPM needs its secure storage `/var/lib/tee` served by
`tee-supplicant`. But `/var/lib/tee` lives in the writable upper — the very thing
we're encrypting. That is a chicken-and-egg: you can't unseal the key to decrypt
the volume that holds the state needed to unseal.

**Resolution.** Keep `/var/lib/tee` **outside** the LUKS container, on a plaintext
location on p3 (e.g. `boot/<ver>/var-tee/`, bind-mounted to `/var/lib/tee`).

**Why that is safe (no confidentiality loss).** OP-TEE's REE-FS objects are
already encrypted and integrity-protected with a key derived from the SoC
**hardware-unique key (HUK)**. The files in `/var/lib/tee` are opaque HUK-sealed
blobs (verified: `file` reports `data`), useless off-SoC. Storing them in
plaintext-filesystem space exposes nothing — their secrecy comes from the SoC
RoT, not from disk encryption. So carving them out both **breaks the
chicken-and-egg** and **leaks no secret**.

Consequence: the fTPM can bootstrap from plaintext `/var/lib/tee`, unseal the
LUKS key, and the initramfs can then open the encrypted upper.

---

## 3. Boot / unlock flow (initramfs)

1. GRUB loads and **verifies** kernel + initramfs + dtb (Secure Boot). ✔
2. Kernel + initramfs start. Mount the squashfs lower (dm-verity if enabled, §4).
3. Bring up OP-TEE `tee-supplicant` against the **plaintext** `/var/lib/tee`
   carve-out (§2); the fTPM now has its state.
4. **Unseal** the LUKS key via the fTPM (`read_tpm_key`, PCR-sealed as today). On
   failure (no fTPM / PCR/key mismatch / different hardware), **prompt for the
   recovery key** (second LUKS slot) — mirrors
   [vyos-config-encrypt.py](../src/helpers/vyos-config-encrypt.py).
5. `cryptsetup open` the upper container → ext4 → use as overlay `upperdir` +
   `workdir`.
6. Assemble the root overlay (lower = squashfs, upper/work = decrypted volume),
   mount as root, pivot. Normal VyOS boot continues; `/config` is already inside
   the encrypted upper.

Requirements for the initramfs (the main engineering cost, `vyos-build` side):
`cryptsetup`/`dm-crypt`, the TSS2 stack + `tpm2` tooling, and
`tee-supplicant` + OP-TEE client — present and ordered **before** the overlay is
assembled. (`cryptsetup`, `dm_crypt`, `tpm2-tools`, OP-TEE are already on the
*running* image; the work is getting the unlock subset into the **initramfs**.)

---

## 4. dm-verity coexistence (clean — no layering fight)

Because only the **upper** is encrypted and the **lower** squashfs is left
plaintext, dm-verity runs on the squashfs exactly as designed — **no dm-crypt
underneath it**. The two mechanisms are orthogonal:

- **Lower (OS):** squashfs + **dm-verity** → integrity (tamper-evident, read-only).
- **Upper (data):** ext4-in-**LUKS** → confidentiality (encrypted at rest).

This avoids the full-disk approach's awkward dm-crypt→squashfs→dm-verity stack.
dm-verity is currently disabled on test benches and enabled for production (see
[igos-tpm-pki-integration-design.md](./igos-tpm-pki-integration-design.md)); this
design does not change that toggle and does not depend on it.

---

## 5. Key management (reuse existing primitives)

- **Seal/unseal:** reuse `write_tpm_key`/`read_tpm_key`/`clear_tpm_key`
  ([python/vyos/tpm.py](../python/vyos/tpm.py)), PCR-sealed at NV `0x81000000`.
  Note (per the TPM-PKI doc + FIPS doc): on this platform the boot chain does
  **not** currently populate PCRs (measured boot is not enabled), so PCR sealing
  is effectively bound to a constant state — acceptable here because boot
  **integrity** is provided by Secure Boot + dm-verity, not by the TPM. If/when
  U-Boot measured boot is enabled, the seal gains real PCR binding (and the
  usual upgrade-time re-seal considerations).
- **Recovery key:** second LUKS keyslot, generated and shown once at enable
  time, exactly as config-encryption does today.
- **Rotation / portability:** a sealed container is bound to this SoC's fTPM; it
  cannot be moved to other hardware. Cross-unit migration is done at the
  **config level** (`save`/`load` of `config.boot`), not by copying the volume.

---

## 6. FIPS posture

Consistent with the config-encryption decision recorded in
[igos-fips-140-3-design.md](./igos-fips-140-3-design.md) §6.1/§8.3:

**Decision: overlay encryption is ALLOWED under FIPS, as a standalone feature.**
It is **local data-at-rest only** — it never participates in data *leaving the
box* (no TLS/IKE/SSH/network crypto, no key export, no attestation). FIPS mode
restricts the crypto used for **data in transit and for externally-verifiable
operations**; a feature that only encrypts local storage has no bearing on those
guarantees, so there is no reason to reject or disable it under FIPS.

- **Standalone / orthogonal to FIPS mode.** Enabling FIPS neither requires nor
  disables overlay encryption, and enabling overlay encryption does not change
  FIPS posture. The two are independent switches: you may run it on a non-FIPS
  box, a FIPS box, or neither. (This mirrors how it is enforced in code — the
  `§6.1 "disable TPM operators"` rule does not apply, because the TPM here is
  storage-only.)
- **Allowed under FIPS** — bulk crypto is **kernel dm-crypt AES-XTS**
  (approved-mode under `fips=1`, hardware-accelerated `xts-aes-ce`).
- **TPM is storage-only** (seals the key) → needs no TPM certification even on
  the uncertified fTPM; the recovery-key path uses no TPM.
- **Approved LUKS parameters (required *when run under FIPS*):**
  `--cipher aes-xts-plain64`, `--hash sha256`, and **`--pbkdf pbkdf2`** —
  LUKS2's default **argon2id is not FIPS-approved**. The volume key is a
  high-entropy random value from an approved RNG (`os.urandom`), so the KDF role
  is minimal, but PBKDF2 is required for a clean approved-mode claim. Non-FIPS
  behavior is unchanged; outside FIPS the feature is likewise allowed with the
  same cipher (argon2id KDF is acceptable there).

> Rationale in one line: **this is a local disk-at-rest control, not a
> communications-security control**, so it is permitted independently of the FIPS
> network/transit posture — enabled or disabled purely on the operator's
> data-at-rest requirement.

---

## 7. Relationship to the existing config-encryption feature

This **supersedes** [vyos-config-encrypt.py](../src/helpers/vyos-config-encrypt.py):

- Config-encryption wraps only `/config`; overlay-encryption wraps the whole
  upper, so `/config` is encrypted as a subset. **Do not run both** on one image.
- Shared DNA: same LUKS + TPM-seal + recovery-key primitives; the differences are
  (a) **scope** (upper vs `/config`), (b) **where it's mounted** (initramfs,
  before the overlay is assembled, vs op-mode after boot), and (c) the
  `/var/lib/tee` carve-out (§2), which config-encryption didn't need because it
  left the whole rootfs plaintext.
- The op-mode UX generalizes from "encrypt `/config`" to "encrypt the writable
  system"; the disable/recovery paths follow the same shape (and must inherit
  the robustness fix already landed for config-encryption — remove the container
  durably, graceful `--deferred` close).

---

## 8. Performance

Only the **writable delta** goes through dm-crypt; reads of unmodified OS files
hit the plaintext squashfs directly. With `xts-aes-ce` (ARMv8 AES) on the A53,
the overhead on eMMC I/O is modest and far below full-p3 encryption. No expected
impact on boot time beyond the one-time unseal + `cryptsetup open` in initramfs.

---

## 9. New-image install & upgrade flow (`add system image`)

The overlay **upper is per-image and tied to its own squashfs**, so the install
story is the single most important operational detail: once the product is on the
overlay-encryption path, a newly installed image must come up **the same way** —
encrypted upper, fTPM auto-unlock, migrated config — without manual steps.

### 9.1 Baseline (today, unencrypted)
`add system image` ([image_installer.py](../src/op_mode/image_installer.py)) runs
in the current system and, for a new version `<new>`:
1. writes `boot/<new>/` with the **new** squashfs + kernel + initrd + dtb + grub
   (+ `.sig`s);
2. creates a **fresh, empty** `boot/<new>/rw` and `work` (the new overlay upper);
3. prompts to copy data from a previous install and, if accepted, migrates the
   **portable** data only — `/config` and the SSH host keys — into the new `rw`;
4. adds a GRUB entry. First boot assembles a fresh overlay from the **new**
   squashfs + the migrated config.

Key invariant: **each image's upper is bound to its own squashfs lower**; the
installer never carries the previous image's overlay deltas forward — it migrates
only version-independent config/data into a fresh upper.

### 9.2 The governing principle for encryption
Encryption keeps that model exactly and only changes *where* the new upper lives:

> The installer creates a **fresh encrypted upper** for the new image and migrates
> the **portable config/data** into it. It must **not** copy the previous
> encrypted upper verbatim.

Copying the old upper verbatim would be wrong — it is full of the *old* OS's
overlay deltas that do not match the *new* squashfs. (This differs from the
`/config`-only feature, which may copy its container verbatim because `/config`
is version-independent; the whole-upper scope requires fresh-upper-plus-migrate.)

### 9.3 Install flow with overlay encryption committed
Running in the current (already-unlocked) image, `add system image` would:
1. lay down `boot/<new>/` with the new squashfs/kernel/initrd as usual;
2. **detect the posture is overlay-encrypted** (a product marker, or presence of
   the current image's `luks-upper`);
3. create `boot/<new>/luks-upper` as a **LUKS container sealed under the *same*
   fTPM key** as the current image (shared-key model, §5), recovery key in slot 2,
   with the FIPS-approved params (§6: `aes-xts-plain64` / `sha256` / `pbkdf2`);
4. format ext4 inside it, mount it, and **migrate the portable config/data**
   (config.boot, scripts, SSH host keys, user data) into the encrypted upper —
   the same set copied today, just into the encrypted destination;
5. provision the **`/var/lib/tee` carve-out** for the new image (plaintext
   sub-dir, §2) so its initramfs can bootstrap the fTPM;
6. add the GRUB entry.

### 9.4 First boot of the new image — identical startup
GRUB (signed) → new kernel/initramfs → initramfs brings up `tee-supplicant` on the
carved-out plaintext `/var/lib/tee` → **unseals the shared fTPM key** →
`cryptsetup open boot/<new>/luks-upper` → assembles the overlay (new squashfs
lower + decrypted upper) → pivots. Same auto-unlock, same encrypted writable
layer, config present — it "starts up the same way".

### 9.5 Why upgrades stay seamless
- **Shared TPM key:** one sealed key unlocks every image's container → no
  per-image key provisioning, and **one** recovery key for all images.
- **Empty PCRs help here:** because this platform does not populate PCRs (no
  measured boot, §5), installing a **new kernel does not change the seal policy**,
  so the unseal still succeeds. On a measured-boot platform a kernel change would
  shift PCR 4/7 and *break* the unseal (forcing re-seal / recovery key); this
  architecture sidesteps that.
- **Rollback is safe:** the previous image keeps its own encrypted upper under the
  same key, so booting back works unchanged.

### 9.6 Required safeguards
1. **Initramfs compatibility:** the new image must carry the unlock hook
   (cryptsetup + TSS2/`tpm2` + `tee-supplicant`). Guaranteed only for images from
   the same encryption-capable build line. The installer must **verify the new
   image supports unlock and warn/refuse otherwise**, rather than silently
   produce a non-booting or (worse) plaintext image that drops the protection.
2. **Recovery-key backstop:** if the fTPM unseal fails on first boot (TPM glitch,
   hardware swap), the initramfs must fall through to the recovery-key prompt
   (same path as config-encryption).
3. **Automatic posture propagation:** since the product has committed to this
   path, encryption must be the **default** for new images (or a one-time product
   setting) so an unencrypted image can't be installed by accident.
4. **New installer logic:** detect posture, create encrypted upper under the
   shared key, migrate data in, carve out `/var/lib/tee`, run the compatibility
   check — all new code in
   [image_installer.py](../src/op_mode/image_installer.py), extending its existing
   encrypted-config handling.

### 9.7 Config migration between images
Independent of all the above, moving config between images stays a
**config-text** operation (`save`/`load` of `config.boot`), unaffected by the
volume being encrypted.

---

## 10. Disable (turning it off)

If it can be turned on, someone will turn it off — and this is the **riskiest**
path, because unlike the `/config`-only feature you are decrypting the **live
root's writable layer**, not a subtree. You cannot move the running overlay upper
out from under a running system. So disable is a **staged, boot-time migration**,
and it must be **crash-safe** — the config-encryption disable bug (a busy
`cryptsetup close` stranding an empty container that then shadowed config and
booted the default config) is the exact failure class to design out here, where
the blast radius is the whole writable system.

### 10.1 Why not "live" like config-encryption
Config-encryption disables live because `/config` is a subtree it can unmount and
re-migrate while the box runs. The overlay upper **is** the running root's write
layer — every open file, every write lives there — so it cannot be rewritten in
place. Disable therefore happens where nothing is writing to the upper: **in the
initramfs on a controlled reboot.**

### 10.2 Staged model (the recommended design)
1. **Op-mode `disable`** validates (recovery key available / fTPM reachable),
   writes a **durable "decrypt pending" marker** for the current image, and
   reboots. It does **not** migrate anything itself.
2. **Initramfs** (which already carries the crypto tooling for unlock) sees the
   marker and performs the migration with nothing writing to the upper:
   a. open the encrypted upper (fTPM key or recovery key);
   b. create a **fresh plaintext** `rw`/`work` for the image;
   c. **copy** the encrypted upper's contents into the plaintext upper and
      **fsync/verify**;
   d. only after a verified copy, flip the image's boot record to the plaintext
      upper and clear the marker;
   e. **then** remove the LUKS container (durably — see §10.3).
3. Assemble the overlay from the plaintext upper and continue boot. The system is
   now unencrypted and "starts up the same way", just without the unlock step.

### 10.3 Crash-safety invariants (hard requirements)
- **Copy-before-remove, never remove-before-switch.** The encrypted container is
  deleted **only after** the plaintext upper is fully written, verified, and
  selected as the boot source. An interrupt before that point leaves the
  encrypted upper intact and the box still boots encrypted (safe rollback).
- **Durable state machine, not ordering luck.** The "decrypt pending /
  migrating / done" state is a committed marker so an interrupted disable
  **resumes or safely rolls back** on the next boot — it never lands in a
  half-state that mounts an empty/`wrong` upper and falls to default config.
- **Explicit boot-source selection.** Which upper to assemble (encrypted vs
  plaintext) is driven by the committed marker, not by "whichever file happens to
  exist" — this is the precise lesson from the config-encryption stale-container
  bug.
- **Durable container removal.** Reuse the landed config-encryption fix shape:
  remove the backing file robustly and close the mapping gracefully
  (`--deferred`), so a busy close can never strand a container that re-mounts on
  a later boot.

### 10.4 What disable does and does not touch
- **`/var/lib/tee` is untouched.** It was always outside the container (§2), so
  the fTPM keeps working throughout; disable neither moves nor exposes it.
- **TPM key clearing is optional and shared-key-aware.** The fTPM-sealed key is
  **shared across images** (§5/§9). Offer to clear it **only** when **no image
  still uses encryption** — clearing it while another image is still encrypted
  would force that image onto its recovery key (same warning the
  config-encryption helper already prints).
- **Keep a backup.** Mirror config-encryption's behaviour of preserving the prior
  state (e.g. retain the encrypted container or a `.old` copy) until the operator
  confirms the plaintext boot is good, then reclaim the space.

### 10.5 Scope & posture
- Disable is **per-image** by default (it decrypts the current image's upper);
  other installed images keep their own posture.
- Because the product "committed to the path" (§9.6), disabling should also
  **flip the product posture** (or prompt), so subsequently installed images are
  provisioned **plaintext** rather than silently re-encrypting — the inverse of
  the automatic-propagation rule for enable.

---

## 11. Open decisions

- **Enablement model:** build-time (always-on for a FIPS/secure SKU) vs runtime
  op-mode toggle like config-encryption. A runtime toggle that re-lays the whole
  writable layer is heavier than the `/config`-only toggle; may be
  install-time-only in v1.
- **`/var/lib/tee` location:** dedicated plaintext sub-dir under `boot/<ver>/`
  (bind-mounted) vs a small separate partition. Sub-dir is simplest; a partition
  is cleaner for isolation.
- **No-fTPM / passphrase mode:** whether to support passphrase-at-boot (kills
  unattended boot) or require the fTPM + recovery key only.
- **dm-verity dependency:** ship overlay-encryption independent of the dm-verity
  toggle (recommended) vs gate on it.
- **Installer UX & compatibility gate** (§9.6): how to detect that a new image
  supports unlock, and whether to hard-refuse or warn on an incompatible image.
- **Disable UX** (§10): confirm whether disable is reboot-staged (recommended) or
  attempted live; and whether `disable` is per-image or product-wide by default.
- **Measured boot:** out of scope here; if added later, the seal gains real PCR
  binding — and upgrades would then need an explicit re-seal step (coordinate
  with the TPM-PKI doc).

---

## 12. Status of prerequisite work

- LUKS + TPM-seal + recovery-key primitives — **done** (config-encryption).
- Hardware AES-XTS (`xts-aes-ce`) — **present** (verified).
- fTPM + OP-TEE + `/var/lib/tee` secure storage — **present** (verified).
- Config-encryption disable robustness fix — **landed** (reused shape for §10).
- Initramfs unlock hook, `/var/lib/tee` carve-out, installer provisioning,
  op-mode enable **and disable** surface, boot-time decrypt-migration (§10),
  FIPS LUKS params — **not started** (this document).

---

## 13. First steps (jump-start)

1. **Image layout (`vyos-build`):** carve `/var/lib/tee` out of the LUKS scope
   (plaintext sub-dir or partition), bind-mount to `/var/lib/tee`.
2. **Initramfs hook (`vyos-build`):** add `cryptsetup`/`dm_crypt` + TSS2/`tpm2` +
   `tee-supplicant`; sequence unseal → open upper → assemble overlay → pivot;
   recovery-key prompt on unseal failure; **plus** the §10 "decrypt pending"
   branch (migrate encrypted→plaintext upper, crash-safe) for disable.
3. **Provisioning tool (`vyos-1x`):** generalize
   [vyos-config-encrypt.py](../src/helpers/vyos-config-encrypt.py) (or a sibling)
   to create/format the encrypted **upper**, seal the key, emit the recovery key,
   using FIPS-approved LUKS params (§6). Reuse the landed disable robustness, and
   add the `enable`/`disable` op-mode surface (disable = set marker + reboot, §10).
4. **Installer (`vyos-1x`):** extend
   [image_installer.py](../src/op_mode/image_installer.py) to provision/copy the
   encrypted upper + carve-out on new installs, with the §9.6 compatibility gate.
5. **Validation (on `igos@10.10.50.165`):** enable → reboot → auto-unlock into
   real config; pull/tamper the eMMC offline → data unreadable; recovery-key path;
   dm-verity still enforced on the lower; FIPS params present (`aes-xts-plain64` +
   `pbkdf2`); `/var/lib/tee` reachable pre-unlock so the fTPM bootstraps;
   **disable → reboot → comes up plaintext with config intact; interrupt the
   disable mid-migration → next boot safely resumes/rolls back (never default
   config).**
