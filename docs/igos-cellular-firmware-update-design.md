# igOS Cellular (WWAN) Firmware Update — Design Sketch

Status: design draft (no implementation yet)
Scope: add operator-driven modem firmware update to igOS, distributed as
per-version **signed archives** (no Debian packages), cataloged in a signed
JSON map, driven from the existing WWAN FSM.

---

## 0. Context / grounding facts

- Product ships **multiple different Telit modems** and **multiple hardware
  revisions** of each, in one image.
  - LN920A-series and similar → flashed with **`uxfp`**.
  - FN920C04 (5G RedCap) → flashed with **`tfl`**.
  - Both tools (`tfl`, `uxfp`) now ship **in the ISO** via the apt mirror
    (`psleng.github.io`) and are listed in the igOS flavor `packages` arrays.
- A live reference unit (`igos@10.10.50.165`) confirmed:
  - Modem: Telit **LN920A12-WW**, h/w rev **1.20**, firmware rev **M0L.001004**.
  - `tfl 1.6.6-0` and `uxfp 1.14.5-0` installed.
  - `igos-wwan-manager.service` + `ModemManager.service` running; `/usr/lib/modem`
    empty; no `wwan_firmware_map_url` in `/usr/share/vyos/version.json` yet.
- A co-worker prototype exists in `~/vyos-1x-firmware` (commit
  `d6193e183 "wwan firmware commands"`) and `vyos-build` commit
  `f33fedfc` (a draft `firmware-map.conf`). This design **generalizes** that
  prototype; key deltas are called out inline.

### Identity comes from the FSM, not AT probing
`show interfaces wwan <wwanN>` → `WWANClientSync().get_status(if_num)` (D-Bus into
the `igos-wwan-manager` FSM) already exposes `modem_manufacturer`, `modem_model`,
`modem_imei`, `modem_firmware` (MM `Revision`), `modem_hardware_revision`. The FSM
binds each interface to a physical modem via `ID_MM_PHYSDEV_UID = modem{N}`
(Perle udev rule), so **multi-modem mapping is already solved** — drive everything
off `wwanN`/`if_num`. The modem only reports its **native Telit** string
(`M0L.001004`); it has no concept of an igOS version (see §6). Drop the
prototype's AT identity probe (`AT+CGMI/CGMM/#HWREV/CGMR/#SWPKGV`) — it is also
PolicyKit-blocked through MM for the normal user.

### Port targeting comes from sysfs, not the FSM
The FSM gives *identity + version* (above); it is **not** what you point the Telit
flashing tool at. uxfp/tfl are targeted with an explicit `--port /dev/ttyUSBn` — you
**never** let them auto-detect, or with multiple modems they could flash the wrong
one. Resolve the port from sysfs, keyed by `wwanN` (verified on `10.10.50.165`):

```
/sys/class/net/wwan0/device → .../usb1/1-1/1-1:1.2        (the net function)
  → walk up to the node with idVendor:
    .../usb1/1-1  = 1bc7:1060 Telit LN920                  ← the physical modem
       ID_USB_SERIAL_SHORT=2d0c1e3a                        ← unique per-device id
  → enumerate ttyUSB* under that subtree:
    ttyUSB0 (1-1:1.0) ttyUSB1 (1-1:1.3) ttyUSB2 (1-1:1.4)
    ttyUSB3 (1-1:1.5) ttyUSB4 (1-1:1.6)                    ← THIS modem's port group
```

- That `ttyUSB*` set is the **disambiguation boundary**: another modem lives under a
  different USB device (`1-2`, …) with its own ports, so it's never a candidate.
- **Enumerate from sysfs, not MM:** ModemManager surfaced only 2 of the 5 ports here
  (`ttyUSB0` ignored, `ttyUSB1` gps); the DIAG port is among the ones MM hides.
- **Anchor on the stable USB path/serial, not the `ttyUSBN` number** — numbers shift
  when the modem switches composition / re-enumerates during flashing; re-resolve
  after re-enumeration. Capture the USB device path **before** stopping the manager.
- Pick the DIAG port within the group and pass it via `--port` (uxfp:
  `--show-diag-ports` ∩ the group; tfl: highest-numbered `ttyUSB` in the group).

### "Unload the manager" = teardown + MM stop (verified)
A standalone `systemctl stop igos-wwan-manager.service` (system staying up):
1. SIGTERM → graceful `fsm.shutdown()` per interface → **bearer disconnect**
   (MM kept alive until this finishes).
2. Then `ModemManagerMonitor.stop_modemmanager()` → `systemctl stop ModemManager`
   ("this manager owns ModemManager's lifecycle").

So the single stop both tears down connections **and frees the diag `ttyUSB`
ports from ModemManager**. (On reboot/poweroff MM is left to systemd instead.)
Capture identity + USB device path **before** stopping — the D-Bus object is gone
afterward.

---

## 1. Trust model (rotation-friendly)

Goal: operators rotate signing keys frequently **without** re-signing the
firmware archive or reshipping an image.

- **Baked into the image (stable, rarely changed):** igOS Firmware **Root CA**
  public cert. Kept offline.
- **Rotated at will, server-side:** a **leaf** signing cert issued by the root.
- **Normal path — the catalog is the signed object.** `modem-firmware.json` carries
  one detached signature (`.sig`) plus the leaf cert. Each firmware **archive** is
  authenticated by its `archive_sha256` listed in that signed catalog — a signed hash
  is equivalent to signing the bytes. A rotation re-signs **only the catalog**.
- **Catalog-less path — per-archive signature.** For ad-hoc installs that bypass the
  catalog (§4, case 3), each archive also ships a detached `<archive>.sig` produced
  **once at build time** and verified against the baked root. This is build-time, not
  per-rotation, so it adds no rotation cost: historical archives + their sigs stay
  valid; only newly built archives are signed by the current leaf.
- **On-device verification order:** catalog signature valid → leaf chains to the
  baked root → leaf not expired/revoked → then verify each downloaded archive's
  `sha256` against the catalog before extracting/flashing. (Ad-hoc path verifies the
  archive's own `.sig` to the root instead.)
- **Three independent key domains** (rotate independently, no cross-impact):
  boot signing (`sign_boot`), apt-mirror signing (`psleng.github.io`), firmware
  signing (this).
- **Prefer X.509 cert chain** (root CA → rotating leaf) over raw GPG: native
  expiry, revocation (CRL/OCSP-style list can ride in the signed catalog), and
  clean "trust the root, accept any leaf it issued" semantics. Short leaf
  validity windows limit blast radius of a leaked leaf.
- **Root rotation (rare):** via the normal image-update channel (new ISO carries
  new root) with an overlap window trusting old+new. Day-to-day rotation stays at
  the leaf.

---

## 2. Catalog: `modem-firmware.json`

Hosted on the firmware server; URL baked into `version.json` as
`wwan_firmware_map_url` (mirrors `update_url`). Fetched at runtime, cached at
`/var/cache/vyos/firmware-map.json`.

**The catalog is metadata only** (URLs + hashes + version info) — a few KB even
with many entries, containing **no firmware**. The device downloads the catalog
in full, then fetches only the **single selected** firmware archive (§5). The
large archives live at their own individual URLs and are never bundled into the
catalog; you never download more than the one version you install.

```jsonc
{
  "schema": 1,
  "generated": "2026-10-28T19:00:00Z",
  "cellular-firmware": [
    {
      "manufacturer": "Telit",              // == FSM modem_manufacturer
      "model": "LN920A12-WW",               // == FSM modem_model
      "installer": "uxfp",                  // uxfp | tfl (which flashing tool)
      "revisions": [
        {
          "hardware_revision": "1.*",               // prefix pattern: all 1.x share this track
          "firmware": [
            { "igos_version": "1.3", "telit_version": "M0L.001004",
              "release_date": "2026-09-01",
              "url": "https://fw.igos/telit/LN920A12-WW/M0L.001004/fw.tar",
              "archive_sha256": "…",          // authenticated by the signed catalog
              "payload_entry": "stream.bin"   // uxfp --file target; optional for tfl (contents.xml auto-found)
            },
            { "igos_version": "1.2", "telit_version": "M0L.001003",
              "release_date": "2026-05-10", "url": "…",
              "archive_sha256": "…", "payload_entry": "stream.bin" }
          ]
        },
        { "hardware_revision": "1.20", "firmware": [ /* exact override: wins over 1.* for rev 1.20 */ ] },
        { "hardware_revision": "2.*",  "firmware": [ /* independent 2.x track */ ] },
        { "hardware_revision": "*",    "firmware": [ /* catch-all: any rev not matched above */ ] }
      ]
    },
    { "manufacturer": "Telit", "model": "FN920C04-WW", "installer": "tfl",
      "revisions": [ /* tfl model — see the fully-populated example below */ ] }
  ]
}
```

### Field reference
**Top level**
- `schema` (int, **required**) — catalog schema version.
- `generated` (ISO-8601 string, optional) — build timestamp (informational).
- `cellular-firmware` (array, **required**) — one entry per modem **model**.

**Model entry**
- `manufacturer` (string, **required**) — exact FSM `modem_manufacturer`.
- `model` (string, **required**) — exact FSM `modem_model`.
- `installer` (string, **required**) — `"uxfp"` | `"tfl"` (which flashing tool).
- `revisions` (array, **required**) — one or more revision blocks.

**Revision block**
- `hardware_revision` (string, **required**) — a pattern: exact (`1.20`), whole-segment
  prefix (`1.*`), or catch-all (`*`); resolved most-specific-wins (rules below).
- `firmware` (array, **required**) — the versions available for this track.

**Firmware entry**
- `igos_version` (string, **required**) — igOS overlay version; operator-facing
  selector / sort key (numeric-tuple compare). Unique within the block.
- `telit_version` (string, **required**) — native MM `Revision` string; must equal the
  modem's reported `modem_firmware`; unique within the model (deterministic "running"
  lookup).
- `release_date` (ISO date string, optional) — informational / stamp.
- `url` (string, **required**) — absolute `https` URL of the firmware **archive**.
- `archive_sha256` (hex string, **required**) — SHA-256 of the archive; authenticated by
  the signed catalog, verified before extract.
- `payload_entry` (string, optional) — what the updater hands the tool: the uxfp
  `--file` stream file, or the tfl firmware dir / `contents.xml`. Omit for tfl
  (auto-found) or single-file uxfp.

### Fully-populated example — FN920C04 (tfl)
Catalog entry, every field present (no elisions):
```jsonc
{
  "manufacturer": "Telit",
  "model": "FN920C04-WW",
  "installer": "tfl",
  "revisions": [
    {
      "hardware_revision": "1.*",
      "firmware": [
        {
          "igos_version": "2.1",
          "telit_version": "M0H.010003",
          "release_date": "2026-10-15",
          "url": "https://fw.igos/telit/FN920C04-WW/M0H.010003/fn920c04-M0H.010003.tar",
          "archive_sha256": "9f2b1c…(64 hex chars)…e4d0",
          "payload_entry": "firmware/contents.xml"
        },
        {
          "igos_version": "2.0",
          "telit_version": "M0H.010001",
          "release_date": "2026-08-30",
          "url": "https://fw.igos/telit/FN920C04-WW/M0H.010001/fn920c04-M0H.010001.tar",
          "archive_sha256": "4c7a90…(64 hex chars)…11af",
          "payload_entry": "firmware/contents.xml"
        }
      ]
    }
  ]
}
```
Matching in-archive manifest (`firmware.json`) inside the igOS-2.1 archive:
```jsonc
{
  "manufacturer": "Telit",
  "model": "FN920C04-WW",
  "hardware_revision": "1.*",
  "telit_version": "M0H.010003",
  "igos_version": "2.1",
  "installer": "tfl",
  "payload_entry": "firmware/contents.xml"
}
```
Archive (`fn920c04-M0H.010003.tar`) layout — the tfl package, `contents.xml` at the
entry path:
```
firmware/
  contents.xml          ← tfl --meta target (or pass firmware/ positionally)
  <modem binaries…>     ← referenced by contents.xml
```
Notes:
- `installer: tfl`, so flashing runs `tfl firmware/ --port /dev/ttyUSBn --force`
  (validated first with `tfl firmware/ --dry-run --debug`). `payload_entry` is shown
  for completeness — tfl can auto-discover `contents.xml`, so it is optional here.
- `telit_version` values are **illustrative** — use the exact string from
  `show interfaces wwan <wwanN>` (`modem_firmware`) on a real FN920C04.
- `latest` for this track = max `igos_version` = `2.1` (computed, not stored).

### Matching & validation rules (enforced on device AND in CI)
1. Exact match on `manufacturer` + `model` against FSM identity.
2. **Revision match by most-specific precedence.** `hardware_revision` is a *pattern*:
   an exact dotted value (`1.20`), a whole-segment prefix ending `.*` (`1.*`, `2.*`),
   or the catch-all `*`. Match the modem's rev against every block of the model and
   pick the **most specific** winner: exact > longest matching prefix > `*`. So `1.*`,
   `2.*`, `3.*` and an exact `1.2` coexist as independent entries, and `1.2` overrides
   `1.*` for just that rev.
3. **Reject ambiguity, not overlap.** Overlap is now expected (an exact nests inside a
   prefix nests inside `*`); only a **tie at the top specificity** is illegal —
   duplicate exacts, two equal-length prefixes matching the same rev, or more than one
   `*`. The CI validator rejects any pattern set where some representable rev lacks a
   unique most-specific block.
4. The winning block **fully defines** the track (no merge/inheritance from a broader
   pattern). If nothing matches (no exact, no prefix, no `*`) ⇒ explicit "no firmware
   mapped for <model> rev <x>" error.
5. `igos_version` unique within a block; each `telit_version` maps to exactly one
   `igos_version` (deterministic "running" reverse-lookup).
6. `telit_version` must equal the FSM `modem_firmware` string exactly.
7. `model` and version strings validate as `[A-Za-z0-9.-]+`; a `hardware_revision`
   pattern is an exact `[0-9.]+`, a whole-segment prefix `[0-9.]+\.\*` (e.g. `1.*`),
   or `*`.
8. `latest` is **not stored** — it is computed as the max `igos_version` in a
   block (numeric-tuple compare, see §6). Optional explicit pin only if a build
   must be published without becoming default.

### Variants (country / carrier firmware) — DEFERRED
Not in scope now. The motivating case is a **one-off, carrier-specific bug-fix**
build that may not work on other carriers — handled by the existing **ad-hoc archive**
path (§4, case 3): push the signed build to just the affected units with
`update firmware wwan <wwanN> archive <url|path>` (model/revision-gated,
operator-initiated), with no catalog/schema changes, and it never enters the shared
catalog so other carriers' units are never offered it. A small fleet needing scheduled
tracking can instead use a **private catalog** (case-2 URL override).
If carrier/country variants ever become *systematic*, revisit adding an optional
`variant` key (default `global`) to a revision-block, selected by config, with ordering
scoped per `(model, hw_rev, variant)` track.

---

## 3. Firmware archive layout + on-disk handling

Each firmware version is **one archive** (tar/zip) at its own `url` — the only
large object, fetched individually and on demand. The archive wraps the payload
in whatever shape the flashing tool expects:
- uxfp: a single file or a directory of files.
- tfl: its native ZIP (already contains `contents.xml`) — can be used as-is.

Each archive carries a **minimal in-archive manifest** (`firmware.json`:
`installer`, `payload_entry`, `model`, `hardware_revision`, `telit_version`,
`igos_version`). `payload_entry` tells the **updater** what to hand each tool after
extraction — it is **not read by uxfp/tfl**. uxfp requires `--file <stream-file>`
(so `payload_entry` names that single stream file, or is inferred when the archive
holds exactly one file); tfl takes the firmware **directory** / `--meta <contents.xml>`,
which the updater auto-discovers (the lone `contents.xml`), so it is **optional for
tfl**. The manifest makes the archive **self-describing**, so it can be installed
without the catalog (§4, case 3); on the normal path it is cross-checked against the
catalog entry.

**Model/revision safety gate (ALL paths):** before flashing, the FSM-detected modem's
`model` MUST equal the archive's, and its revision MUST match the archive manifest's
`hardware_revision` pattern (exact / `N.*` / `*`), or the install is refused —
flashing the wrong firmware can brick it. A `*`/`N.*` firmware thus intentionally
applies to revisions it was never explicitly qualified against, so scope the pattern
deliberately. Overridable only via the lab `force-unsigned` path.

### On-disk handling — archives are huge, device storage is limited
Default flow is **download-one → verify → extract → flash → clean up**. Do NOT
assume multiple versions are retained:
- **Only one archive is ever downloaded** (the selected version); the catalog
  itself is tiny metadata (§2).
- Work dir: `/usr/lib/modem/<vendor>/<model>/<hw_rev>/<telit_version>/` (extract
  target), removed after a successful flash unless retention is enabled.
- **Free-space check before download and before extract**; stream the archive to
  a temp file, verify `archive_sha256` against the signed catalog, then extract.
- **Extraction safety:** reject `..`, absolute paths, symlinks, and
  non-regular/dir entries (apply the prototype's tfl-ZIP checks to every archive).
- **Retention is optional, off by default:** if space allows, keep the running
  version's archive and/or an LRU of 1–2 to re-flash without re-download. With
  retention off, rollback = simply re-download the older (still-cataloged)
  version.

---

## 4. CLI surface (slots under the existing `update firmware` tree)

`op-mode-definitions/update-firmware.xml.in` already defines
`update firmware uboot` and `update firmware grub`; add a `wwan` sibling. This
replaces the prototype's `set/delete interfaces wwan … firmware` and
`execute wwan stop-manager` nodes (op-mode `set`/`delete` read as config syntax).

```
show interfaces wwan <wwanN> firmware              # keep where prototype put it (show tree)
update firmware wwan <wwanN> <igos_version>        # install (interactive confirm)
update firmware wwan <wwanN> <igos_version> now    # non-interactive (matches `update firmware uboot now`)
update firmware wwan <wwanN> latest                # = max igos_version
delete firmware wwan <wwanN> <igos_version>        # remove a cached version (only when retention enabled)
```

`show interfaces wwan <wwanN> firmware` table (all FSM-sourced):
```
Version  Telit Firmware  Released     Cached  Running
1.3      M0L.001004      2026-09-01   No      Yes
1.2      M0L.001003      2026-05-10   No      No
```
("Cached" is usually No — archives aren't retained by default; §3.) Completion
for `<igos_version>` lists available igOS versions for the detected model+rev;
`delete` completion restricts to cached ones.

### Choosing the version and the source
- **Specific cataloged version (case 1):** `update firmware wwan <wwanN> <igos_version>`
  — completion lists **every** version for the detected model+rev, not just latest;
  `latest` is only a shortcut for the max.
- **Own catalog URL (case 2):** like image updates, the catalog source is
  user-configurable. Default is the baked `wwan_firmware_map_url`; override with a
  config node mirroring `system update-check url` (same file/handler):
  ```
  set system update-check cellular-firmware url <http(s)-url>
  ```
  The override does **not** relax trust — the alternate catalog must still be signed
  by a leaf chaining to the baked root.
- **Ad-hoc archive not in any catalog (case 3):**
  ```
  update firmware wwan <wwanN> archive <url-or-local-path> [now]
  update firmware wwan <wwanN> archive <local-path> force-unsigned [now]   # lab only, loud warning
  ```
  URL (download) or a local file already on the box. Default **requires** a detached
  `<archive>.sig` verified against the baked root, plus the model/revision gate (§3);
  metadata comes from the in-archive manifest. `force-unsigned` skips the signature
  (dev/debug images only).

### Scheduled check & notifications (integrate with `system update-check`)
Reuse the image update-check UX; do **not** merge modem logic into the upstream
image checker (`vyos-system-update-check.py`).
- **Shared (config + notifications):** live under the same `system update-check`
  subtree and conf_mode (`system_update-check.py`), which already writes the `/run`
  config, manages the service, and owns the MOTD. Add cellular `auto-check`/`disable`
  toggles + the `cellular-firmware url` override; surface results via a sibling MOTD
  fragment (`/run/motd.d/11-vyos-cellular-firmware`) + optional `wall`, matching the
  image-update notice.
- **Separate (check logic):** its own loop service
  (`vyos-cellular-firmware-check.service`, modeled on `vyos-system-update.service`,
  ~12h) — it must talk to the FSM per-modem (D-Bus) and map telit→igOS, logic that
  shouldn't fork the generic image checker or couple it to QMI/FSM. It **reuses the
  op-handler's compare core** in a non-interactive "check-only" mode and **verifies
  the signed catalog** just like the op path (respecting the `cellular-firmware url`
  override).
- **Notify only, never auto-flash.** Flashing tears down the modem + needs a reboot,
  so it stays operator-initiated. The notice names the interface + available
  `igos_version` + the exact `update firmware wwan <wwanN> <igos_version>` command.
- **Graceful no-op** when no wwan is configured / FSM down / no modem present; checks
  every present `wwanN` and aggregates into one MOTD.

---

## 5. Runtime procedure (per `wwanN`, multi-modem safe)

1. `WWANClientSync().get_status(N)` → manufacturer/model/hw_rev/running
   `telit_version` (no AT). Resolve `/sys/class/net/wwanN/device` → USB device.
   **Capture both now** (before any teardown).
2. **Resolve the source:**
   - *catalog path* (default / specific version / `latest`): fetch + **GPG-verify**
     the catalog — the config `system update-check cellular-firmware url` if set,
     else the baked `wwan_firmware_map_url` — cache it; match the model+revision block.
   - *ad-hoc path* (`archive <url|path>`): skip the catalog entirely; the archive is
     obtained directly and authenticated by its own `.sig` in step 4.
3. Map running `telit_version` → running `igos_version`. Resolve target
   (`<igos_version>` or `latest`). Compare (numeric-tuple):
   - equal → "already installed"
   - target > running → upgrade
   - target < running → **downgrade warning + confirm**
   - running not in catalog → "running: unknown", ordering indeterminate (warn)
4. **Free-space check**, then obtain the **one** archive (the only large download):
   - catalog path: download by `url` → verify `archive_sha256` vs the signed catalog.
   - ad-hoc path: download/open the archive → verify `<archive>.sig` against the baked
     root (unless `force-unsigned`).
   Extract safely into the work dir → read the in-archive manifest → **enforce the
   model/revision gate** (archive model+rev == detected modem). No apt; nothing else
   is downloaded.
5. **Unload `igos-wwan-manager`** → graceful bearer teardown + ModemManager stop
   (frees diag ports).
6. **Target the correct modem explicitly — never auto-detect.** Derive the modem's
   serial-port group from sysfs: `/sys/class/net/wwanN/device` → walk up to the USB
   device node with `idVendor` (its `ID_USB_SERIAL_SHORT` uniquely identifies this
   physical modem) → enumerate `ttyUSB*` under that subtree. Pick the DIAG port
   **within that group** and pass it via `--port`, so another modem's ports are never
   candidates (MM may surface only some ports — e.g. 2 of 5 — so enumerate from sysfs,
   not MM):
   - uxfp: `uxfp --file <payload> --show-diag-ports`, intersect with the modem's
     `ttyUSB` set → `uxfp --file <payload> --port /dev/ttyUSBn`.
   - tfl: `modprobe qcserial` if needed, `tfl <payload> --dry-run --debug` validate,
     pick the highest-numbered `ttyUSB` in the group → `tfl <payload> --port
     /dev/ttyUSBn --force`.
   Anchor selection on the stable USB path/serial, not the volatile `ttyUSBN` name
   (numbers can shift across the mode switch / re-enumeration during flashing).
7. Show firmware + current-modem details, confirm (unless `now`/`--no-prompt`).
8. Flash: `uxfp --file <payload> --port <diag>` or
   `tfl <payload> --port <diag> --force`.
9. On success: **leave manager stopped**, prompt **reboot**, then **clean up** the
   archive + work dir (unless retention is enabled). On cancel or pre-flash error:
   **restart** the manager and remove the temp download.

`delete firmware wwan <wwanN> <igos_version>` → remove that version's cached
archive/work dir (only meaningful when retention is enabled).

---

## 6. igOS version overlay & ordering

Telit's native versioning is opaque/unorderable, so igOS assigns its own
human-readable tag.

- `igos_version` is the **operator-facing selector + sort key**; `telit_version`
  is the ground-truth match string + on-disk path key.
- **The modem only reports `telit_version`.** "Running igOS version" is derived by
  mapping the FSM `modem_firmware` string through the catalog. If the running
  Telit version isn't catalogued → "unknown", ordering indeterminate.
- **Scope:** igOS versions are monotonic only **within a (model, hw_rev) track**.
  An igOS "1.3" for LN920A12 is NOT comparable to "1.3" for FN920C04, nor across
  hardware revisions. Do not make it a global number.
- **Comparison:** parse `igos_version` as an integer tuple (`1.10` → `(1,10)`),
  compare numerically (avoids the `1.10 < 1.9` string-sort trap). (Alt: keep an
  explicit integer `sequence` sort key + `igos_version` label if label renumbering
  is ever needed — not chosen by default.)
- The in-archive manifest (§3) records `igos_version` + `telit_version`, so an archive
  is self-labeling (and installable catalog-less); **running** still derives from the
  modem's reported `telit_version`, mapped via the catalog when available.

---

## 7. Automation — generated, not hand-built

Hand-authoring N models × M revisions × K firmwares of archives + metadata +
hashes + catalog is untenable. Make it **tree-driven + generated in
CI**. Humans drop a firmware blob and a tiny metadata file; a generator does the
rest.

### 7a. Single declarative source of truth (a tree)
```
firmware-src/
  telit/
    LN920A12-WW/
      1.10+1.20/                 # revisions sharing a track (dir name encodes the list)
        1.3/                     # igos_version
          payload/FN…bin         # uxfp payload (file or dir)
          meta.toml
        1.2/
          payload/…
          meta.toml
      2.00/
        2.0/ …
    FN920C04-WW/
      1.10/
        2.0/
          payload/firmware.zip   # tfl payload (contents.xml inside)
          meta.toml
```
`meta.toml` (the only thing a human writes per firmware):
```toml
telit_version = "M0L.001004"
release_date  = "2026-09-01"
installer     = "uxfp"          # or tfl
notes         = "GPS fix, carrier X cert"
```
Path supplies manufacturer/model/revision-set/igos_version; `meta.toml` supplies
the rest. Adding firmware = drop a payload + a `meta.toml`.

### 7b. Generator (one CLI, runs in CI) — per leaf dir
1. Parse path + `meta.toml`; validate installer vs payload shape (uxfp file/dir
   vs tfl zip containing `contents.xml`).
2. Write the in-archive manifest (`firmware.json`) and assemble the **archive**
   (tar/zip) with the payload laid out as the tool expects; determine
   `payload_entry`; compute `archive_sha256`; emit a detached `<archive>.sig`
   (current leaf) to enable the catalog-less path (§4, case 3).
3. Accumulate a catalog entry (`url`, `archive_sha256`, `payload_entry`, version
   metadata; grouping revisions from the `1.10+1.20` dir).

Global:
6. Emit `modem-firmware.json` (auto `latest`; overlap/uniqueness checks).
7. **Run the device-identical validator** on the generated catalog — fail the
   build on any violation.
8. Optional: `uxfp --show-diag-ports` / `tfl --dry-run` smoke-check each payload.
9. **Sign** the catalog with the current **leaf** key (CI secret / HSM; root
   offline) → `modem-firmware.json.sig` + publish leaf cert.
10. Publish to the firmware server at the `url` paths: firmware archives (+ each
    archive's `.sig`) + catalog + `modem-firmware.json.sig` + leaf cert.

### 7c. Scale & hygiene
- **Incremental/idempotent:** key each archive by a content hash of
  (payload + meta); skip unchanged, rebuild only diffs. Catalog regenerated
  wholesale (cheap).
- **Rotation:** swap the CI leaf secret, re-run → only the catalog is re-signed;
  historical archives stay valid (authenticated by their hashes in the signed
  catalog).
- **Reuses existing muscle:** same shape as the nightly that builds/pushes
  `tfl`/`uxfp` to the mirror — a separate CI job on the firmware side (private
  repo; blobs are licensed/large). The ISO never carries firmware.

---

## 8. Integration seams (small, bounded)

- **vyos-build:**
  - add `wwan_firmware_map_url` to the igOS flavor TOML (next to `update_url`);
  - bake the firmware **root CA** cert into the image via the `includes.chroot`
    + certificate mechanism.
- **vyos-1x:**
  - `update firmware wwan …` op-mode nodes in `update-firmware.xml.in`
    (+ keep `show … firmware`); add the `archive <url|path>` + `force-unsigned`
    variants (§4, case 3);
  - a config node to override the catalog source, mirroring `system update-check url`:
    `system update-check cellular-firmware url <url>` (in
    `interface-definitions/system_update-check.xml.in`; the handler reads it and falls
    back to the baked `wwan_firmware_map_url`);
  - a handler (generalize the prototype `src/op_mode/wwan_firmware.py`) that is
    FSM-sourced for identity, GPG+SHA verifying, igOS-version aware, and drives
    uxfp/tfl with the teardown ordering above; drop the AT identity probe.
  - a scheduled **check-only** service (`vyos-cellular-firmware-check.service`,
    modeled on `vyos-system-update.service`) + conf_mode wiring in
    `system_update-check.py`; reuses the handler's compare core, notifies via
    MOTD/`wall`, never auto-flashes.
- **Firmware side (separate/private):** the `firmware-src/` tree + generator CI
  (§7) and the hosting of firmware archives + signed catalog (+ `.sig`, leaf cert).

---

## 9. Open decisions

1. **Revision grouping** in the source tree: `1.10+1.20` dir-name convention, or a
   `revisions = [...]` key in `meta.toml`?
2. **Generator home/language:** standalone Python CLI in the private firmware
   repo, run by the existing nightly CI? (default: yes)
3. **Leaf key custody:** CI secret vs HSM/KMS for the catalog-signing step.
4. **`delete firmware`** placement: keep under `delete …`, or fold into a
   maintenance/`reset` verb. (Only relevant if retention is enabled.)
5. **igOS version scope:** per-(model+revision-block) track (recommended) vs
   per-model.
6. **Comparison style:** single `igos_version` numeric-tuple (default) vs
   `igos_version` label + explicit integer `sequence`.
7. **Retention policy:** download-on-demand + cleanup (default) vs keep
   running + an LRU cache; cache size/location given huge archives.
8. **`payload_entry` location:** catalog field (default) vs a small marker inside
   the archive.
9. **Archive format:** tar vs zip (tfl payload is already a zip — reuse as-is or
   re-wrap for uniformity?).
10. **Catalog-URL config node** name/placement: `system update-check cellular-firmware
    url` (recommended — mirrors image update-check) vs a flat `cellular-firmware-url`
    leaf vs a cellular-specific config area.
11. **Transient catalog override:** also allow a per-command `… catalog <url>` for
    no-commit testing, or rely on the config node only (default)?
12. **`force-unsigned` gating:** permit on production images (loud warning) or restrict
    to dev/debug builds only?
13. **Scheduled-check hosting:** a dedicated `vyos-cellular-firmware-check.service` loop
    (recommended) vs extending the image checker; cadence (reuse the 12h interval?).
14. **Toggle granularity:** independent cellular `auto-check`/`disable` under
    `system update-check` vs inheriting the image toggles.
15. **Revision pattern semantics (decided):** exact + whole-segment prefix (`1.*`) +
    bare `*` catch-all (**allowed**), most-specific wins (exact > longest prefix > `*`),
    override-not-merge from broader patterns. Remaining nit: keep partial-segment globs
    (`1.1*`) disallowed (recommended).

(Country/carrier **variants** are intentionally deferred — see §2; the one-off
carrier bug-fix case is covered by the ad-hoc archive path, §4 case 3.)

---

## 10. Status of prerequisite work

- Done: `tfl` + `uxfp` added to all three igOS flavor `packages` arrays in
  vyos-build; confirmed published in the `psleng.github.io` repo
  (`tfl 1.6.6-0`, `uxfp 1.14.5-0`) and installed on the live unit.
- Done: `tfl`/`uxfp` added as `Recommends:` (soft runtime dep) to vyos-1x
  `debian/control`.
- Pending: everything in §§1–8 above.
