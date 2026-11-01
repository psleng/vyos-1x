# igOS Cellular (WWAN) GPS / GNSS — Design Sketch

Status: design draft (no implementation yet)
Scope: expose a GNSS position fix as a standard, system-wide, **fully
`set`-configurable** GPS service. The fix normally comes from the cellular
modem's GNSS receiver, but the design is **source-agnostic**: configured under a
standalone `service gps` tree that resolves its source under the covers (a WWAN
modem, or a dedicated serial/USB GPS receiver), so the same CLI works either way
(§5). The WWAN FSM does the minimum modem bring-up (resolve the GPS port, start a
1 Hz raw-NMEA stream) and hands the port to the service; **`gpsd`** owns the
serial stream and fans it out on TCP. The position is then both **readable via
`show` commands** and **streamable off-box** — either as raw **NMEA** or
converted to the **fleet / telematics protocol commonly used today (TAIP)** — for
operators, scripts/APIs, and fleet back-ends. It can also **discipline the system
clock** (via gpsd → chrony) as a coarse fallback when no NTP is reachable (§7).
Validated end-to-end on the reference unit — see §0.

---

## 0. Context / grounding facts

Design intent: the modem is just an NMEA source. Keep the VyOS-specific logic as
small as possible — the only thing that genuinely *must* live in our code is the
**wwan ↔ physical-modem ↔ GPS-port** mapping, because ModemManager (MM)
re-enumerates and renames `ttyUSB*` nodes constantly. Everything downstream (the
raw stream read, the 1 Hz cadence, the TCP fan-out, NMEA parsing) is handled by
standard off-the-shelf software (`gpsd` + `python3-gps`).

### Verified on the reference unit (`igos@10.10.50.165`) — 2026-10-29

Live board: Telit **LN920A12-WW**, modem index `2`, MM `1.20.6`, plugin `telit`,
drivers `option, qmi_wwan`.

- **GPS NMEA port = `/dev/ttyUSB1`**, carrying the MM port tag `gps`. Full MM
  port map for this modem:
  `cdc-wdm0 (qmi), ttyUSB0 (ignored), ttyUSB1 (gps), ttyUSB2 (at),
  ttyUSB3 (at), ttyUSB4 (ignored), wwan0 (net)`.
- **MM Location capabilities:** `3gpp-lac-ci, gps-raw, gps-nmea, agps-msa,
  agps-msb`; only `3gpp-lac-ci` was enabled; MM GPS refresh rate 30 s (i.e. MM's
  own location engine is **not** a 1 Hz source).
- **AT passthrough works** via `mmcli -m <N> --command="..."` but **requires
  root** — as the normal user it is PolicyKit-blocked
  (`org.freedesktop.ModemManager1.Device.Control` → `Unauthorized`). MM is
  started `--filter-policy=strict` (no `--debug`), and the AT command interface
  still works for root, so **no MM relaunch is required**. The WWAN FSM already
  runs privileged, so this is free.
- **Starting modem state:** `AT$GPSP?` → `$GPSP: 0` (GNSS powered off);
  `AT$GPSNMUN?` → `$GPSNMUN: 0,0,0,0,0,0,0` (no unsolicited NMEA).
- **`gpsd` and `python3-gps` are NOT installed** on the board — they are genuine
  new packaging dependencies, not pre-existing.

#### The validated bring-up sequence (Telit LN920A-series)

```
AT$GPSP=1                       # power on the GNSS receiver        → $GPSP: 1
AT$GPSNMUN=2,1,1,1,1,1,1        # unsolicited NMEA, mode 2 = raw on dedicated port
                               #   GGA,GLL,GSA,GSV,RMC,VTG all enabled
```

- Mode byte matters:
  - `$GPSNMUN=1,...` streams but **wraps** every sentence as a Telit unsolicited
    result code: `$GPSNMUN: $GPGGA,,,,,,0,...`. **Not** parseable by `gpsd`.
  - `$GPSNMUN=2,...` streams **raw NMEA-0183** on the dedicated NMEA port:
    `$GPGGA,,,,,,0,...`, `$GPGSV,...`, `$GPRMC,...`. This is what `gpsd` wants.
- **Cadence confirmed at 1 Hz** by per-sentence host timestamps — the `$GPGGA`
  fix sentence recurred at `…16.580 → 17.590 → 18.594 → 19.593 → 20.593 →
  21.589` (≈1.00 s spacing), with a full GGA/GLL/GSA/GSV/RMC/VTG cycle per
  second.
- No satellite fix during the test (`$GPGGA` fix quality `0`, 1 SV at 0°) — the
  antenna was indoors. The transport/cadence is proven; the empty fields
  populate once the receiver locks. A fix is **not** required to validate the
  pipeline.
- **Board was restored** to the original state after testing:
  `AT$GPSNMUN=0,0,0,0,0,0,0` then `AT$GPSP=0` (re-verified `$GPSP: 0`,
  `$GPSNMUN: 0,0,0,0,0,0,0`).

### Why `gpsd` owns the port (not MM)

MM tags `ttyUSB1` as `gps`, so MM's own location engine will claim that serial
node the instant `gps-nmea`/`gps-raw` is enabled. We therefore **leave MM's GPS
location sources off** and let `gpsd` open `/dev/ttyUSB1` directly. The FSM only
issues the AT bring-up; MM never fights for the port. (MM's `3gpp-lac-ci`
coarse-location source is orthogonal and can stay as-is.)

---

## 1. Architecture

`service gps` is the orchestrator (source-agnostic, §5). For a WWAN source it
delegates modem bring-up to the FSM; for a dedicated receiver it points gpsd
straight at the device.

```
  CLI: set service gps ...  →  service gps orchestrator
        │                                    │
        │ source = wwan <wwanN>              │ source = device <path>
        ▼                                    ▼
  WWAN FSM  (per wwanN, privileged)     (dedicated serial/USB GPS — no FSM)
    ├─ resolve the GPS port             │
    │    → /dev/ttyUSB1 (MM tag "gps")  │
    ├─ AT-map[model] bring-up via mmcli --command :
    │    AT$GPSP=1 ; AT$GPSNMUN=2,1,1,1,1,1,1
    └─ publish resolved /dev/ttyUSBx ───┤
                                        ▼
                     service gps points gpsd at the resolved device
                                        │
                                        ▼
  gpsd  (standard pkg)  — reads raw NMEA @1 Hz, serves JSON on TCP 2947
                                   │                         └─ SHM time → chrony refclock (§7)
        ┌──────────────────┬───────┴───────────┬─────────────────────┐
        ▼                  ▼                   ▼                     ▼
 op-mode `show gps`     other local apps   NMEA stream out     fleet stream out
 (python3-gps client)   (gpsd TCP clients)  (raw NMEA → TCP)    (TPV → TAIP → TCP/
                                                                 dial-out to back-end)

  ModemManager: GPS location sources left OFF so it never claims ttyUSB1.
  Optional: gpsd SHM feeds chrony so GPS disciplines the clock when no NTP (§7).
```

Division of labour:

| Concern | Owner | Rationale |
|---|---|---|
| CLI config + source resolution/orchestration | **`service gps`** | Source-agnostic; one tree for modem or dedicated GPS (§5) |
| wwan ↔ modem ↔ port mapping | **FSM** | Only place that knows it; MM renames ports |
| Start/stop the NMEA stream (AT) | **FSM** | Needs privileged MM AT passthrough |
| Read serial, 1 Hz, TCP fan-out | **gpsd** | Standard software; don't reinvent |
| Parse NMEA / report position | **op-mode + python3-gps** | Standard client lib |
| Readable `show` output (text/json) | **op-mode** | Operator + API visibility (§6a) |
| Stream NMEA / fleet protocol off-box | **exporter service** | Network-facing, format-pluggable (§6b) |
| Clock discipline (fallback, no NTP) | **chrony** (fed by gpsd SHM) | Reuse existing NTP daemon; GPS is a refclock (§7) |
| Fleet-format conversion (TAIP, …) | exporter / converter | Downstream consumer, selectable encoding |

---

## 2. Port resolution (FSM)

Two viable sources; prefer robustness over convenience.

1. **MM port tag (simple):** read the modem's `Ports` property and pick the
   entry whose type is `MM_MODEM_PORT_TYPE_GPS` (enum value `5`, 1-based:
   `UNKNOWN=1, NET=2, AT=3, QCDM=4, GPS=5, QMI=6, MBIM=7, AUDIO=8, IGNORED=9`).
   This mirrors the existing `_qmi_control_device()` helper, which already walks
   `Ports` looking for `QMI=6`. A sibling `_gps_nmea_device()` returning the
   `GPS=5` node is the natural shape.
2. **sysfs anchor (robust, preferred for multi-modem):** as documented in
   `igos-cellular-firmware-update-design.md` §0, anchor on the stable USB
   path / `ID_USB_SERIAL_SHORT` for `wwanN` and enumerate its `ttyUSB*` group,
   rather than trusting a `ttyUSBN` number that shifts across re-enumeration.
   The firmware doc notes MM can hide some ports from its `Ports` property, so
   for flashing it enumerates from sysfs; the GPS port, however, was reliably
   surfaced with the `gps` tag in testing.

**Decision bias:** use the MM `gps` tag as the primary selector (it is the modem
vendor's own declaration of which node is the NMEA port), but bind it to the
`wwanN`→physical-modem identity the FSM already maintains
(`ID_MM_PHYSDEV_UID = modem{N}`) so the right modem's port is chosen in a
multi-modem chassis.

---

## 3. AT-command model map (new, data-driven, configurable)

A table keyed by modem model → the AT sequences that enable/disable the raw 1 Hz
NMEA stream. Keeping it declarative — **and configurable/overridable per
modem (see below)** — is what lets a different or future modem drop in without
touching FSM control flow.

```python
# illustrative shape — not final
GPS_AT_PROFILES = {
    # Telit LN920A-series (Qualcomm-based) — VALIDATED 2026-10-29 on LN920A12-WW
    'LN920A': {
        'enable':  ['AT$GPSP=1', 'AT$GPSNMUN=2,1,1,1,1,1,1'],
        'disable': ['AT$GPSNMUN=0,0,0,0,0,0,0', 'AT$GPSP=0'],
        'query':   ['AT$GPSP?', 'AT$GPSNMUN?'],
        'nmea_mode': 2,     # 2 = raw NMEA on dedicated port (gpsd-friendly)
    },
    # 'FN920C04': { ... }   # 5G RedCap — TBD, likely different GNSS AT set
}
```

- **Keying / model identity:** derive the match key from the FSM's existing
  `modem_model` (MM `Model`), the same string the firmware design keys off.
  Match by **prefix / family** (`LN920A*`) so hardware/firmware point revisions
  share a profile. A vyos-build pinmux/model hint can override when the MM model
  string is ambiguous, but MM model is the default source of truth.
- Commands are issued through the FSM's existing privileged MM path
  (`mmcli -m <N> --command=` equivalent, or the `Modem.Command` D-Bus method).
- The map also owns the **sentence selection** (GGA/GLL/GSA/GSV/RMC/VTG) so a
  model that needs a reduced set can express it.
- **The AT-map must be configurable, not hardcoded.** Different modems (and even
  firmware variants) use different GNSS AT dialects — the Telit `$GPSP`/`$GPSNMUN`
  set validated here is **not** universal (e.g. u-blox, Quectel `+QGPS`, Sierra,
  and likely the FN920C04 differ). So the profile table should be **data-driven
  and overridable** rather than frozen in Python:
  - Ship the known-good profiles as a **data file** (e.g. JSON/TOML under
    `data/` in vyos-1x, or a per-model entry in the vyos-build `data/models/`
    tree) so a new modem can be supported by **adding data, not editing FSM
    code**. This mirrors how `models_dir` / pinmaps already carry per-board data
    in vyos-build.
  - Allow a **vyos-build / per-model override** (the model definition tree is
    the natural home) to add or replace a profile for a board whose modem isn't
    in the shipped table.
  - Consider a **CLI escape hatch** for field bring-up of an unknown modem —
    e.g. `set interfaces wwan <wwanN> gps at-profile <name>` to pick a profile,
    and/or a raw `enable-command`/`disable-command` override — so a new part can
    be qualified without a firmware/image respin. (See §8 open decision 5.)
  - The built-in table stays as the **fallback / sane default** keyed on MM
    model; config/data overrides win when present.

---

## 4. gpsd integration

- **New deb deps:** `gpsd` and `python3-gps` are added to the `vyos-1x`
  **`Recommends`** field (soft deps, alongside `tfl`/`uxfp`/`python3-libgpiod`)
  so they are pulled in only on igOS hardware flavors via vyos-build fragments —
  not every modem model exposes a GPS port. `gpsd-clients` (optional, for
  `gpspipe`/`cgps` debugging) can be added the same way if wanted.
- **Config:** render `/etc/default/gpsd` (or a dedicated unit drop-in) from a
  Jinja2 template with:
  - `DEVICES="/dev/ttyUSB1"` (the FSM-resolved port),
  - `GPSD_OPTIONS="-n"` (poll even with no client; `-G` only if remote listen is
    wanted),
  - listen on TCP `2947` (gpsd default) — bound to loopback unless a CLI knob
    opens it.
- **Lifecycle:** the FSM (re)starts gpsd *after* the AT stream is confirmed up,
  and stops/forgets the device when GPS config is removed or the modem drops.
  Because port numbers can shift on re-enumeration, the FSM must re-resolve and
  re-point gpsd on modem re-appearance (reuse existing MM re-enumeration hooks).
- **Robustness option:** `gpsd` can take a `udev`-driven hotplug device, but
  since our port identity is FSM-owned, explicit FSM re-point is more
  predictable in a multi-modem box.

---

## 5. CLI surface & placement (conf-mode) — everything is `set`-driven

Like the rest of the config, **all GPS behaviour is driven by `set` commands**
(enable, source, AT profile, stream format/listener, downsample). The open
question is *where* in the tree it lives.

### Placement options

- **Option A — under `interfaces wwan <wwanN> gps`.** Natural because the GNSS
  receiver is physically part of the cellular modem, and the FSM already owns
  that modem. **Downside:** it hard-couples "GPS" to "WWAN". A board with a
  **dedicated GPS** (serial/USB receiver, no modem) then has nowhere sensible to
  configure it, and the same capability would be expressed two different ways
  depending on hardware — confusing.

- **Option B — a standalone `service gps` tree that resolves its source under
  the covers (RECOMMENDED).** GPS is modelled as a position *service* (which is
  essentially what gpsd is), decoupled from any one piece of hardware. The
  service figures out where the fix comes from — a WWAN modem's GNSS port, or a
  dedicated GPS device — and binds to it automatically, or the operator pins it
  explicitly. This keeps one consistent CLI regardless of whether the fix comes
  from the modem or a dedicated receiver, and matches gpsd's own
  device-agnostic model. It is also the normal VyOS idiom (`set service …` for
  daemons that listen/serve).

### Recommended tree (`set service gps`)

```
set service gps                                   # enable the position service (gpsd + exporter)

# Source selection — auto by default, explicit when needed:
set service gps source auto                       # (default) discover a GPS source under the covers
set service gps source wwan <wwanN>               # bind to a cellular modem's GNSS (FSM drives it)
set service gps source device <path>              # bind to a dedicated serial/USB GPS receiver
set service gps source wwan <wwanN> at-profile <name>   # modem-only: pin the AT profile (§3)
set service gps source wwan <wwanN> nmea-sentences <...> # modem-only: sentence subset

# Local gpsd listener (readable by clients / op-mode):
set service gps listen-address <addr>             # default loopback only
set service gps port <2947>                       # gpsd TCP port

# Off-box streaming (see §6b) — NMEA passthrough or fleet protocol:
set service gps stream format <nmea|taip>
set service gps stream listen-address <addr>
set service gps stream port <n>
set service gps stream interval <seconds>         # downsample from 1 Hz
set service gps stream remote <host> port <n>     # (follow-on) dial-out to a fleet back-end
```

- **`source auto` is the "figures itself out under the covers" path:** prefer a
  dedicated GPS device if present, else discover a WWAN modem that exposes a GPS
  port (via the FSM) and bind there. Explicit `source wwan`/`source device`
  override discovery.
- **Modem-only knobs** (`at-profile`, `nmea-sentences`) live under
  `source wwan` because they are meaningless for a dedicated receiver — the tree
  shape itself keeps hardware-specific options where they belong.
- Minimal default: a bare `set service gps` + `source auto` brings up the
  validated sequence (for a modem source) with all sentences and loopback-only
  gpsd.

### The FSM ↔ service seam (why this still needs the FSM)

For a **WWAN source**, the modem work still belongs to the FSM — it owns AT
passthrough, port resolution, and re-enumeration (§2–§3). So `service gps` does
not talk to the modem directly; instead:

1. `service gps source wwan <wwanN>` (or `auto` landing on a modem) signals the
   FSM to run the AT bring-up and **publish the resolved `/dev/ttyUSBx`** (e.g.
   via a runtime state file / D-Bus) to the GPS service.
2. The GPS service points gpsd at that device and runs the listener + exporter.
3. On modem re-enumeration the FSM re-resolves and re-publishes; the service
   re-points gpsd. For a **dedicated device** source there is no FSM
   involvement — the service points gpsd straight at `source device <path>`.

This split is what lets the *same* `service gps` config work for both a
modem-backed fix and a dedicated receiver.

---

## 6. Consuming the data: show commands + streaming out

Two distinct consumers, both fed from gpsd:

### 6a. Readable position via `show` commands

`op-mode-definitions/` + a `src/op_mode/` script using `python3-gps` (the `gps`
client library) to query gpsd on `127.0.0.1:2947` and render human/structured
output:

```
show gps                        # fix status, lat/lon, alt, speed, sats, HDOP (source-agnostic)
show gps detail                 # raw TPV/SKY, per-satellite table
show gps nmea                   # live raw NMEA tail (debug)
show gps source                 # which source is bound (wwanN / device) and how it was resolved
```

- Source-agnostic by design: `show gps` reports the fix regardless of whether it
  comes from a WWAN modem or a dedicated receiver — matching the `service gps`
  placement decision (§5). For a modem source it cross-references the FSM status
  (which `wwanN`, resolved port); for a dedicated device it shows the device
  path.
- The modem-centric `show interfaces wwan <wwanN>` view can still surface a short
  "GPS: enabled, port ttyUSB1" line for context, but the canonical position
  readout is `show gps`.
- Position data comes from gpsd, not the FSM.
- Should support the standard op-mode `--format json` so the same position data
  is scriptable / API-consumable, not just a text table.

### 6b. Streaming the position off-box (NMEA **or** fleet protocol)

The box must be able to **stream live positional data to other systems**, in
either of two encodings, selectable by config (CLI under `service gps stream`,
see §5):

1. **NMEA-0183 passthrough** — the raw sentences the modem already emits. gpsd
   can serve these directly (`gpspipe -r`, or a client `?WATCH={"nmea":true}`),
   so this is essentially free: expose an NMEA stream on a TCP port.
2. **Fleet / telematics protocol** — the ASCII position format commonly used by
   fleet/AVL back-ends today, **TAIP (Trimble ASCII Interface Protocol)** being
   the strong default candidate (gpsd even ships a TAIP driver). A small
   converter reads gpsd TPV reports and emits fleet-format position records.

Design shape for the streamer:

- A lightweight **exporter service** (new `src/services/` daemon, or a gpsd
  client) subscribes to gpsd and republishes on a TCP listener:
  - `nmea` mode → verbatim NMEA sentences,
  - `fleet` mode (e.g. TAIP) → converted position reports,
  - cadence follows the 1 Hz source (configurable downsample).
- **Transport:** TCP server that remote fleet systems connect to is the first
  target (mirrors how gpsd serves 2947). A **push/client mode** (device dials
  out to a fleet endpoint, optionally over the WWAN link itself) is a likely
  follow-on — many fleet back-ends expect the device to connect out.
- The converter is **format-pluggable** for the same reason the AT-map is
  (see §3): TAIP first, but the output encoding should be a selectable profile
  so other fleet dialects can be added as data/config, not a rewrite.

The `set service gps stream …` knobs are defined in §5.

---

## 7. GPS as a time source (clock discipline)

When no NTP server is reachable (isolated / field deployment, or a dead-battery
RTC at boot), the GPS fix can discipline the system clock. It is **not** a
precision time source without PPS, but it is often *all we have* — good enough to
get the clock into the right ballpark.

VyOS already uses **chrony** for NTP (`set service ntp`), and gpsd integrates
with chrony out of the box via a **shared-memory (SHM) refclock** — so we reuse
the existing time plumbing rather than inventing a parallel time daemon.

How it works:

- gpsd publishes time samples into SHM segments: **unit 0 = NMEA time**,
  **unit 1 = PPS** (only if a pulse-per-second line exists). chrony consumes them
  as a refclock, e.g.:
  ```
  refclock SHM 0 refid GPS precision 1e-1 offset 0.0 stratum 10 noselect
  ```
- **No PPS on this hardware:** the Telit NMEA-over-USB stream exposes **no PPS
  line**, so only the coarse NMEA-time SHM (unit 0) is available. Accuracy is
  bounded by USB/serial latency + the 1 Hz NMEA granularity — realistically tens
  to a few hundred ms, with jitter. Fine for correcting a wrong RTC; **not** a
  stratum-1 replacement.
- **Treat it as a fallback:** give it a **high stratum** (e.g. 10) and keep real
  NTP servers preferred, so chrony only leans on GPS when nothing better is
  reachable. If a PPS line is ever wired on a future board, add SHM unit 1 at a
  low stratum for real precision.
- **No fix → no time:** NMEA time is only valid once the receiver reports a time
  fix (date/time populated in RMC/ZDA); until then, don't feed chrony.

Boot behaviour:

- Optional **one-shot coarse set at boot** (`chronyd -s` / `initstepslew`-style,
  or `chronyc makestep`) when the RTC is far off, then let chrony discipline
  smoothly from the SHM refclock afterwards.

CLI (under `service gps`, feeding chrony):

```
set service gps time                      # enable GPS clock discipline (gpsd SHM → chrony refclock)
set service gps time stratum <10>         # fallback stratum (default high, so real NTP wins)
set service gps time step-at-boot         # optional: one-shot coarse set if the clock is far off
```

Wire-up: when `service gps time` is set, the service (a) ensures gpsd runs with
SHM time export enabled, and (b) injects a `refclock SHM 0 … refid GPS` line into
the **existing chrony config** (the `set service ntp` / chrony template), so GPS
time rides the same daemon as regular NTP. Coordinate with the `service ntp`
conf-mode script so the refclock survives an NTP config regeneration.

---

## 8. Open decisions

1. **gpsd remote exposure default** — loopback-only (safest) vs. a CLI opt-in to
   bind `0.0.0.0:2947`. Leaning loopback-only by default.
2. **CLI placement** — standalone source-agnostic `service gps` (RECOMMENDED,
   §5) vs. `interfaces wwan <wwanN> gps`. Leaning `service gps` so a dedicated
   (non-modem) GPS receiver uses the same tree and the service resolves the
   source under the covers. Confirm the tree name/shape (`service gps` vs.
   `system gps`/`service location`).
3. **Fleet-GPS output protocol** — confirm **TAIP** as the default fleet
   encoding (gpsd has a TAIP driver) vs. another AVL dialect, and the exact
   record subset/fields. The NMEA passthrough path is settled; the fleet
   converter output and whether it's TCP-listen first or dial-out-to-back-end
   first is the open piece. Output encoding should be a selectable profile so
   additional fleet dialects are data/config, not a rewrite.
4. **AGPS / assisted lock** — MM advertises `agps-msa/agps-msb`; do we wire
   assistance (faster TTFF) or ship cold-start only for v1? Lean cold-start v1.
5. **Configurable AT-map home & override mechanism** — where the per-modem AT
   profile table lives (vyos-1x `data/` file vs. vyos-build per-model tree) and
   how it is overridden (build data, model definition, and/or the
   `gps at-profile` CLI escape hatch). The FN920C04 (and any non-Telit part)
   needs its own profile; its GNSS AT set is **not yet validated**. The `$GPSP`/
   `$GPSNMUN` dialect validated here must not be assumed universal.
6. **Source auto-discovery policy** — for `source auto`, the precedence rule
   (dedicated device vs. modem GPS) and behaviour with multiple candidate modems.
7. **Coexistence with MM location** — confirm leaving `3gpp-lac-ci` enabled
   while gpsd owns `ttyUSB1` causes no contention (expected fine; different
   port).
8. **Enable trigger** — standalone `gps` toggle vs. requiring the modem
   registered/connected. GNSS worked with the modem merely enabled; lean
   standalone.
9. **GPS time accuracy / stratum policy** — default fallback stratum for the
   chrony SHM refclock, whether to ship `step-at-boot` on by default for boards
   with no battery-backed RTC, and whether to expose a PPS SHM unit on any board
   that wires a PPS line (sub-ms vs. the coarse NMEA-only time here).

---

## 9. Status of prerequisite work

| Item | Status |
|---|---|
| GPS port identification (`/dev/ttyUSB1`, tag `gps`) | ✅ verified on LN920A12-WW |
| AT passthrough under root via MM | ✅ verified (no `--debug` needed) |
| Raw 1 Hz NMEA bring-up sequence | ✅ verified (`$GPSP=1`, `$GPSNMUN=2,...`) |
| gpsd present on image | ⚙️ added to `vyos-1x` Recommends (soft dep) — pulled in via vyos-build hardware fragments |
| python3-gps present on image | ⚙️ added to `vyos-1x` Recommends (soft dep) — pulled in via vyos-build hardware fragments |
| AT-map module (data-driven + per-modem override) | ⬜ not started |
| FSM port-resolver + bring-up hook | ⬜ not started |
| gpsd config template + lifecycle | ⬜ not started |
| CLI (`service gps` conf-mode tree, source-agnostic) | ⬜ not started |
| Op-mode `show ... gps` (text + json) | ⬜ not started |
| Stream-out exporter (NMEA passthrough) | ⬜ not started |
| Stream-out exporter (fleet / TAIP conversion) | ⬜ not started |
| GPS clock discipline (gpsd SHM → chrony refclock) | ⬜ not started |
| FN920C04 GNSS AT profile | ⬜ not validated |

---

## 10. First coding session (jump-start)

### 10a. Lock these first (shape the code — decide before/at the start)

These three open decisions (from §8) determine file locations and tree shape, so
pin them before writing code to avoid rework:

1. **CLI tree name** (§8 #2) — `service gps` (recommended) vs.
   `interfaces wwan <wwanN> gps`. Determines where the XML definition and
   conf-mode script live (`interface-definitions/` vs. a new
   `interface-definitions/service_gps.xml.in`, and `src/conf_mode/`).
2. **AT-map home** (§8 #5) — vyos-1x `data/` file (e.g.
   `data/gps-at-profiles.json`) vs. a vyos-build per-model tree entry.
   Determines the module's load path and whether vyos-build also changes.
3. **Fleet protocol = TAIP?** (§8 #3) — confirm TAIP (gpsd has a driver) vs.
   another AVL dialect. **Non-blocking for the core path** (steps 1–5 below); only
   needed before the exporter (step 6). Can be deferred a day without stalling.

Everything else in §8 (gpsd exposure default, AGPS, auto-discovery precedence,
MM coexistence, enable trigger, time stratum policy) has a sane leaning default
already stated and does **not** block the first session.

### 10b. Ordered build steps

Each step lists its goal, target files, and the acceptance check. Steps 1–5 get
a position fix readable via `show gps`; 6–7 are the follow-on consumers.

1. **AT-map module (validated LN920A profile).**
   - *Files:* new `python/vyos/utils/wwan/gps_at_profiles.py` (+ data file per
     10a#2).
   - *Content:* `LN920A` profile with the §3 enable/disable/query sequences;
     match-by-prefix on MM `modem_model`; built-in fallback + override hook.
   - *Accept:* unit-select the right profile for `LN920A12-WW`; disable sequence
     is the exact inverse used to restore the board in §0.

2. **FSM port-resolver + bring-up/teardown hook.**
   - *Files:* `python/vyos/utils/wwan/interfaces_wwan_state_machine.py`.
   - *Content:* `_gps_nmea_device()` returning the `GPS=5` port (model on
     `_qmi_control_device()`, §2); a bring-up path that issues the AT-map enable
     sequence and **publishes the resolved `/dev/ttyUSBx`** (runtime state file /
     D-Bus, §5 seam); teardown on disable/modem-drop; re-publish on
     re-enumeration.
   - *Accept:* on the board, enabling drives `$GPSP=1`/`$GPSNMUN=2,…` and
     publishes `/dev/ttyUSB1`; disabling restores `$GPSP:0`/`$GPSNMUN:0,…`.

3. **gpsd config template + lifecycle.**
   - *Files:* `data/templates/gps/gpsd.j2` (→ `/etc/default/gpsd`), service
     (re)start helper.
   - *Content:* `DEVICES=<resolved port>`, `GPSD_OPTIONS="-n"`, loopback `2947`
     by default (§4).
   - *Accept:* `gpspipe -r` / a python3-gps client on the board shows live TPV
     once the stream is up.

4. **`service gps` CLI + conf-mode script** (wires 1–3).
   - *Files:* `interface-definitions/*.xml.in` (per 10a#1), new
     `src/conf_mode/service_gps.py`.
   - *Content:* `enable`, `source auto|wwan <wwanN>|device <path>`, gpsd
     listener knobs (§5); resolve source → signal FSM (wwan) or point gpsd
     directly (device).
   - *Accept:* `set service gps` + commit brings the whole chain up; delete tears
     it down cleanly (config round-trips).

5. **`show gps` op-mode → first end-to-end test.**
   - *Files:* `op-mode-definitions/show-gps.xml.in`, `src/op_mode/gps.py`.
   - *Content:* query gpsd via python3-gps; `show gps` / `gps detail` /
     `gps nmea` / `gps source`; `--format json` (§6a).
   - *Accept:* on the board `show gps` reports fix status + (once locked)
     lat/lon/sats; json output parses.

6. **Stream-out exporter (NMEA passthrough, then fleet/TAIP).**
   - *Files:* new `src/services/vyos-gps-streamd` (or gpsd client), config under
     `service gps stream` (§5/§6b).
   - *Accept:* a remote TCP client receives NMEA (`format nmea`); then TAIP
     records (`format taip`) once 10a#3 is confirmed.

7. **GPS clock discipline (gpsd SHM → chrony refclock).**
   - *Files:* chrony template / `service ntp` conf-mode coordination (§7).
   - *Content:* `set service gps time` injects `refclock SHM 0 … refid GPS`
     at a high fallback stratum; optional `step-at-boot`.
   - *Accept:* `chronyc sources` shows the `GPS` refclock; real NTP still
     preferred when reachable.

### 10c. Validation harness

- Board: `igos@10.10.50.165` (Telit LN920A12-WW, modem index 2). AT passthrough
  and the full bring-up/restore were exercised on 2026-10-29 (§0) — re-run the
  same sequence to validate each step.
- Note: the hardware path can't be unit-tested off-box; use the board for
  integration checks and keep logic (profile selection, parsing, config
  rendering) unit-testable without a modem.
