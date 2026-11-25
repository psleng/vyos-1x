# igOS HTTP API Concurrency & GUI Lock-out — Design / Analysis

Status: **Analysis complete; fixes A, B, C, D (GraphQL op-mode mutations) and
E.1/E.3 implemented; remaining items in §7 and the GUI follow-ups in §5.2**
(analysis 2026-11-09; revised 2026-11-16 after a second code review — see
"Revision notes" at the end)

Scope: why slow operations lock up the igOS web GUI, grounded in the running
unit and the vyos-1x source. The trigger that surfaced it was the "check for new
firmware" notification, but the root cause is **systemic**: multiple
single-process / single-worker chokepoints in series, the worst being that the
**GraphQL API executes every command synchronously on its one event loop**. A
second, related defect is that **config-changing operations do not share one
lock** across the API surfaces, so they can race on the single shared
`ConfigSession`. This doc records the architecture, the evidence, the
concurrency-safety constraints, and the proposed fixes (vyos-1x side) plus the
GUI-side follow-ups (separate Perle Flask repo).

---

## 1. Request-path architecture (verified on the unit 2026-11-09)

```
client ──> nginx (multi-worker) ──┬─ location /            → Flask GUI  @127.0.0.1:5000
                                  ├─ /retrieve|configure|show|generate|
                                  │   graphql|image|... → VyOS HTTP API @ unix:/run/api.sock
                                  ├─ /socket.io           → Flask GUI (Socket.IO)
                                  └─ /perlecloud/...      → igos-cloud-proxy (unix sock)
```

- **nginx** — 3 worker processes; proxy only, not a chokepoint.
- **Flask GUI backend** (`/opt/flask/flask/app`, a **separate Perle repo**):
  `gunicorn -k GeventWebSocketWorker --workers 1 --timeout 600 --bind
  127.0.0.1:5000`, `SOCKETIO_ASYNC_MODE=gevent`. **One gevent worker** — a single
  process, cooperatively scheduled greenlets.
- **VyOS HTTP API** (`src/services/`, this repo): a **single** uvicorn process —
  `ApiServerConfig(app, uds="/run/api.sock")` + `server.run()` with **no
  `workers=`** (`src/services/vyos-http-api-server`). One asyncio event loop +
  an anyio threadpool. Fronted by nginx on the unix socket.
- **Shared config state:** one `ConfigSession` created at startup
  (`session_state.session = ConfigSession(os.getpid())`), shared across all
  requests.

So there are **two single-process/single-worker stages in series** (gevent GUI
worker → single-loop API), plus a **single shared ConfigSession** behind the API.

---

## 2. Serialization points and loop-blockers in the VyOS API

All paths below are relative to `src/services/api/` unless stated otherwise.

| Surface | Code | Concurrency behavior |
|---|---|---|
| **REST op ops** (`/show`, `/generate`, `/image`, `/reset`, `/reboot`, …) | plain `def` endpoints (e.g. `show_op`, `rest/routers.py:823`) | FastAPI runs `def` endpoints in the **anyio threadpool** → **concurrent**, do **not** block the event loop, take **no** config lock. OK. |
| **REST `/configure`, `/configure-section`** | `async def` → `run_in_threadpool(_execute_configure_op)`; inside the worker, the **threading** `lock = Lock()` (`rest/routers.py:90`) is held around the set/delete/commit (`395`→`513`) | **serialized** against other holders of `lock`; loop not blocked. OK — but see the deferred-commit gap below. |
| **REST `/import-pki`** | plain `def` (threadpool) holding the same threading `lock` (`rest/routers.py:923`) | serialized with `/configure`; loop not blocked. OK. |
| **REST `/config-file`** (save/load/merge + commit) | `async def` guarded by a **different** lock, `asynclock = asyncio.Lock()` (`rest/routers.py:92`, `681`); `save_config` / `migrate_and_load_config` / `merge_config` / `get_config_diff` run **inline on the loop** (`685`–`704`); only the commit is offloaded (`710`/`712`) | **not** mutually exclusive with `/configure` (two unrelated locks) → load/merge can interleave with a `/configure` on the shared session; **blocks the loop** during load/merge/save. **Bug.** |
| **REST `/auth`** | `async def` that calls the **threading** `lock.acquire()` **on the event loop** (`rest/routers.py:1008`) just to read the HTTPS port | while any `/configure` commit holds `lock` in a worker thread, this **freezes the whole API until that commit finishes**. **Bug.** |
| **REST `/retrieve`** | `async def` running `Config(...)` / `session.show_config()` (subprocess) inline (`rest/routers.py:601`) | **blocks the loop** for the duration (usually short). Minor. |
| **REST deferred commit** (config touches `service https`) | `/configure` and `/config-file` stage the change, release the lock, then commit via `background_tasks.add_task(call_commit…)` (`rest/routers.py:489`/`491`, `719`/`721`) **without** re-acquiring any lock | the commit of the shared session runs **unlocked** → can race another config op. **Bug** (narrow window). |
| **GraphQL queries and mutations** | `async def func_impl` that calls `result = method()` **inline** — no `await`, no `run_in_threadpool` (`graphql/graphql/queries.py:100`, `graphql/graphql/mutations.py:100`); op-mode runs **in-process** via `load_op_mode_as_module` + `func()` (`graphql/session/session.py:176`, `199`) | **runs the command synchronously on the single event loop** → a slow command **freezes the entire API for all clients**. **Systemic bug.** |
| **GraphQL mutations vs. REST config ops** | GraphQL mutations take **no lock at all** | GraphQL `LoadConfigFile` (load + commit, `session.py:107`) or a composite set/commit can run **while a REST `/configure` commit is in flight** in a worker thread → both drive the one shared `ConfigSession`. **Bug.** |
| **GraphQL `AuthToken`** (token-auth mode only) | **sync** resolver (`graphql/graphql/auth_token_mutation.py:30`) → `pam.authenticate` (`graphql/libs/token_auth.py:25`) on the loop | a slow PAM stack (unreachable RADIUS/TACACS+, fail delay) stalls the API. Minor for igOS: the GUI uses **key** auth. |

**The GraphQL row is the systemic bug.** REST op ops parallelize (threadpool)
and REST `/configure` serializes (lock) — both reasonable. GraphQL, however,
executes the op-mode/config function **inline on the event loop**, so **any**
slow GraphQL operation (a multi-second `show`, a `generate`, anything that waits
on I/O or a subprocess) blocks every other API request — including all of the
GUI's, since **the GUI uses GraphQL** (`graphql/session/composite/system_status.py`;
`ShowUpdateSystem` via the GUI's `graphql_service.send_request`). The worst case
is not the firmware check but **`AddSystemImage`** (`session.py:129`), which
downloads an ISO **inline** — freezing the API for minutes.

**The config-lock rows are the second defect.** There is no single "config
lock": REST uses two unrelated locks (`lock` and `asynclock`), the deferred
HTTPS commit uses none, and GraphQL mutations use none. Today the only thing
that partially serializes GraphQL mutations is the very loop-blocking we want to
remove — and even that does not stop them overlapping a REST commit already
running in a worker thread.

Classification check: GraphQL **queries are read-only by construction** —
op-mode functions are exposed as queries only if their name starts with `show`
(`graphql/generate/schema_from_op_mode.py:181`), and the config-session queries
are `show_config`, `show`, `show_user_info` plus the `SystemStatus` composite
(`graphql/generate/config_session_function.py:14`). Everything else is a
mutation: config-session mutations (`save_config_file`, `load_config_file`,
`add_system_image`, `delete_system_image`, `config_session_function.py:27`),
composite/override config mutations (e.g. firewall address-group templates,
`graphql/session/templates/`, `graphql/session/override/`), and op-mode
non-`show` functions (`reset_*`, `clear_*`, `generate_*`, `restart_*`, …).

---

## 3. The firmware-check case (first observed symptom)

Chain, top to bottom:

1. **Fires on every GUI open/login/reconnect.** `socket_manager.py`
   `register_client` → `_check_software_update_available()` **inline in the
   Socket.IO connect handler** (comment: *"Check software update (when
   login/open authorized page)"*).
2. → `check_software_update_available()`
   (`app/event/jobs/system/software_update.py`) →
   `get_system_update_status()` (`app/core/operation/software_management/service.py`),
   a **blocking `requests` GraphQL call** `ShowUpdateSystem` (`verify=False`).
3. API side: `ShowUpdateSystem` → op-mode `show system updates`
   (`src/op_mode/system.py:40` `_compare_version_raw`) →
   `vyos.version.get_remote_version()` — a **blocking `requests.get`** to the
   firmware site with `timeout=3` and 2 retries (`python/vyos/version.py:101`,
   `134`) ⇒ **up to ~9 s**, longer if DNS/TCP stalls.
4. Both stages are single-process: the gevent connect handler is tied up, and
   the GraphQL call runs inline on the uvicorn loop → the whole API freezes for
   the duration.

There is **no caching**: `_compare_version_raw` always live-fetches. A standalone
daemon already polls every 12 h off-API (`src/system/vyos-system-update-check.py`),
but it only writes MOTD (`/run/motd.d/10-vyos-update`) and only **when an update
is available** — it persists no machine-readable status, and the GUI/op path
ignores it and re-fetches live. An in-process cache would not help either: the
GraphQL path re-executes the op-mode file on every call (§4), so module-level
state does not survive between requests.

---

## 4. Concurrency-safety constraints (why we cannot just parallelize everything)

Splitting the proposed "offload to threadpool" fix by operation type:

- **Config-session mutations must be serialized — against *every* other config
  op, not just each other.** All API surfaces share **one** `ConfigSession`
  (and the backend has a single commit lock). Set/delete/load/merge/commit from
  REST *and* GraphQL must hold **the same** lock, or one client's staged changes
  get committed (or discarded) by another. Today that invariant does not hold
  (§2): REST has two unrelated locks, the deferred HTTPS commit holds none, and
  GraphQL mutations hold none. The fix must **establish** a single config lock,
  not "preserve" one. (Pre-existing characteristic: the single shared session
  also makes multi-client config editing coarse — the lock serializes it;
  parallelism neither causes nor fixes that.)
- **Image ops need no config lock; other op-mode mutations do (in GraphQL).**
  `AddSystemImage`/`DeleteSystemImage` and the Perle image mutations do not use
  the shared session — REST `/image` already runs them lock-free — and putting
  them under the config lock would let a multi-minute image download block
  every commit. Other op-mode mutations (`reset_*`, `restart_*`, …) usually
  don't touch the shared session either, but GraphQL runs op-mode scripts
  **in-process**, where they can reach it through `SessionState()` — and the
  Perle onboarding mutation does exactly that (sets and commits on the shared
  session, `perle_igos_onboarding_mutation.py`). So GraphQL mutations take the
  lock by default (§5.1 B). REST `/reset`/`/generate` run op-mode out of
  process and stay lock-free.
- **Read ops (GraphQL queries, REST `/show`) need no lock — except reads of the
  shared session's working config.** They are read-only by construction (§2),
  and REST `/show`/`/retrieve` already run concurrently with commits. A
  reader/writer lock (shared for reads, exclusive for writes) was considered
  and **rejected**: it would make every GUI read wait behind any in-flight
  commit (commits take ~20–40 s on the reference unit), re-creating the
  lock-out this work is meant to remove. Op-mode `show` reads system state, not
  the session, and is safe.
  **Caveat — diff output, not torn reads (observed 2026-11-16, root-caused
  and fixed 2026-11-17):** REST `/retrieve` `showConfig` and GraphQL
  `ShowConfig` returned 500 / `ValueError: Failed to parse config: Syntax error
  on line 9 …` while an API config op was in flight. Cause:
  `ConfigSession.show_config()` ran `cli-shell-api showConfig` **without**
  `--show-active-only`/`--show-working-only`, and while a session has
  uncommitted changes `showConfig` prints a **diff** with `+`/`-` markers,
  which `ConfigTree` cannot parse (the same FIXUP is documented in
  `python/vyos/configsource.py:245`). Every API config op stages changes in
  the shared session for its whole duration, so any concurrent read of it
  failed. **Fix:** `ConfigSession.show_config(..., effective=True)` adds
  `--show-active-only`; `/retrieve` (`showConfig`, `exists`, `returnValue(s)`)
  and GraphQL `ShowConfig` now read the **committed** config. Because every
  API config op commits or discards under the config lock, between requests
  the committed config is what the session would show; during one, readers no
  longer see another client's uncommitted changes. Verified on the unit: 0
  failures across 16 reads during a commit. Note the active config is updated
  when the config is applied (~10 s into a ~25 s `/configure` on the unit),
  before post-commit hooks finish.
- **In-process op-mode reentrancy — narrower than first feared.** GraphQL loads
  op-mode scripts with `load_as_module` (`python/vyos/utils/system.py:100`),
  which **re-executes the file into a fresh module object on every call**. So
  module-level state in op-mode scripts (e.g. `config = ConfigTreeQuery()` at
  `src/op_mode/system.py:25`) is **per request, not shared**. The real shared
  state to audit is:
  - **process environment** — the shared session's env *is* `os.environ`
    (`python/vyos/configsession.py:184`), and REST commit-confirm toggles
    `IN_COMMIT_CONFIRM` in it (`rest/routers.py:323`, `347`), so it is visible
    to any concurrent thread/subprocess for the duration;
  - **globals in imported `vyos.*` library modules** (imported normally, so
    shared across threads);
  - **fixed temp-file paths** (e.g. `/tmp/config.file` in `/config-file`) and
    any `os.chdir`/umask changes.
- **Reassurance:** REST already runs op-mode work concurrently in the threadpool
  against the same shared session, so concurrent reads are already the
  production model — aligning GraphQL with it is not a new, untested risk.
- **Do not add uvicorn workers.** `SessionState`, the shared `ConfigSession`,
  `token_users`, the JWT secret and `BackgroundOpManager` records all live in
  process memory; multiple worker processes would split them. Concurrency must
  come from the threadpool within the single process.

### 4.1 Interaction with CLI / SSH config sessions

The API config lock (§5.1 A) is an **in-process** Python lock. It serializes API
requests against the API's one shared session and nothing else. Coordination
with console / serial / SSH users happens at a different layer:

- **Every CLI login has its own private config session.** Entering `configure`
  and staging changes holds **no lock**, so an idle or abandoned CLI session
  cannot block the GUI/API (and `system login timeout` is therefore an
  accounting/security control, not a lock-release mechanism).
- **The only cross-channel lock is the system commit lock** — a POSIX `lockf`
  on `/opt/vyatta/config/.lock` (`python/vyos/defaults.py:73`,
  `python/vyos/utils/commit.py:133`), held **only while a commit runs**, by
  whichever session (CLI or API) is committing.
- **It is non-blocking.** A second committer does not wait; it fails at once
  with *"Configuration system locked by another commit in progress"*
  (`acquire_commit_lock_file`, `LOCK_EX | LOCK_NB`; `vyconf_session.py:230`
  returns `COMMIT_IN_PROGRESS`). So after fix A, API requests queue behind each
  other in order, but an API commit that collides with a CLI commit **fails**
  instead of queuing. A non-acquiring check and a polling wait already exist
  (`commit_in_progress2()`, `wait_for_commit_lock()`, `commit.py:58`, `119`).
- **Concurrent edits: last commit wins, per node** (legacy backend; verified in
  `vyatta-cfg`). A session's working config is a `unionfs-fuse` mount of the
  session's private changes directory (read-write) over the **live** active
  config (read-only) (`vyatta-cfg` `src/cstore/unionfs/cstore-unionfs.cpp:430`,
  `do_mount` at `1646`; on the vyos-1x side `VYATTA_TEMP_CONFIG_DIR` is the
  merged view and `VYATTA_CHANGES_ONLY_DIR` the deltas,
  `python/vyos/utils/configfs.py:33`). The commit writes that merged tree back
  as the new active config. Consequences:
  - changes from two sessions to **different** nodes both survive — the merged
    view already contains the other session's committed changes, so a commit
    does not revert them;
  - for the **same** node, the later commit's value wins **silently** — there
    is no pre-commit conflict check;
  - a delete of a subtree removes it including anything another session
    committed beneath it in the meantime;
  - a session sees another's committed changes immediately in its own
    `show` view (the overlay is live).
- **The only conflict signal is an after-the-fact terminal message.** After a
  commit that applied anything, `vyatta-cfg` runs `vyatta-cfg-notify`
  (`src/commit/commit-algorithm.cpp:1353`), which `write`s *"Active
  configuration has been changed by user … Please make sure you do not have
  conflicting changes"* to other users' configure-mode terminals
  (`scripts/vyatta-cfg-notify`). It is advisory, it targets ttys only, and it
  runs for API commits too — so CLI users are warned about GUI/API changes, but
  **GUI users are never warned** about CLI changes (no tty, and the GUI has no
  equivalent).
- **Legacy commit lock confirmed non-blocking.** `vyatta-cfg`
  `UnionfsCstore::getCommitLock()` takes `lockf(F_TLOCK)` on the same
  `/opt/vyatta/config/.lock` (`cstore-unionfs.cpp:868`–`883`), matching the
  vyos-1x side.
- **vyconf backend: unverified.** When the vyconf backend is enabled
  (`vyos.utils.backend.vyconf_backend()`), sessions are held by `vyconfd`
  (separate repo); whether a session is a delta over the running config or a
  snapshot taken at session start — the latter would make the later commit win
  for the **whole tree**, reverting the other session's changes — has not been
  confirmed. Must be checked before relying on per-node semantics there. (The
  reference unit runs the legacy backend: `vyconf_backend()` is `False`.)
- **Within the API there is a stronger hazard.** All API clients (all GUI users)
  share **one** session, so without per-request atomicity one client's
  uncommitted changes can be committed — or discarded on error — by another.
  Fix A makes each request's stage+commit atomic under the API lock; this is
  the main correctness argument for it, independent of loop-blocking.

---

## 5. Proposed fixes

### 5.1 vyos-1x (this repo) — the high-leverage fix

**A. One config lock for every config-changing path.** *(Implemented for REST,
2026-11-16; see "A — implementation and test results" below.)* Move the
threading `lock` out of `rest/routers.py` into a shared module
(`api/config_lock.py`) and make it **the** config lock, always acquired
**inside a worker thread** (never on the event loop):
- `/configure`, `/configure-section`, `/import-pki` — unchanged (already use it).
- `/config-file` — replace `asynclock` with the shared lock and run the whole
  body (save/load/merge/diff/commit) in the threadpool (plain `def` endpoint).
- Deferred HTTPS commit (`call_commit` / `call_commit_confirm`) — acquire the
  lock inside the background task (`deferred_commit*` wrappers;
  `call_commit*` stay lock-free because the background-op path calls them while
  already holding the lock). The staged changes remain visible to another
  client between lock release and the deferred commit — accepted (decided
  2026-11-16).
- `/auth` — move the HTTPS-port read into the threadpool; it is a read and
  should not take the config lock at all.
- **Commit vs. another session's commit** (decided 2026-11-16): before every
  API commit, wait up to 120 s (`SYSTEM_COMMIT_WAIT_SECONDS`; initially 30 s,
  raised because commits can exceed 30 s) for the system
  commit lock to be free (`commit_in_progress2()` poll), then fail with
  *"Configuration system locked by another commit in progress; try again
  later"*. Note the legacy commit only takes the lock **after** its pre-commit
  hooks (`vyatta-cfg` `commit2.cpp:236`, `commit-algorithm.cpp:1263`), so a
  commit can still lose a race that starts after the check; that falls back to
  today's immediate failure.

**A — implementation and test results** (reference unit, legacy backend,
2026-11-16; same script before/after; another session's commit simulated by
holding `lockf` on `/opt/vyatta/config/.lock`):

| Scenario | Before | After |
|---|---|---|
| `/configure` while another commit holds the lock 10 s | fails after ~9 s ("temporarily locked"), change discarded | waits, then **succeeds** (~33 s incl. commit) |
| …concurrent GraphQL `ShowConfig` | **10.0 s** (event loop frozen by `/auth`) | **0.7 s** |
| …concurrent `/auth` | **10.3 s** (blocked on threading lock on the loop) | 3.5 s (its own upstream call) |
| …concurrent `/config-file save` | 8.2 s — ran **concurrently** with `/configure` (separate locks) | 37.9 s — **serialized** after `/configure` |
| `/configure` while the lock is held 35 s | fails after ~5 s | fails after ~30 s wait with the new message (tested with the initial 30 s bound); change discarded |
| No contention: set/delete/save | OK | OK |

Commits on the reference unit take ~20–40 s each and can run longer, so the
bound was raised to 120 s after testing. Budget for a GUI config request:
queueing on the config lock + up to 120 s wait + its own commit (~20–40 s),
which must stay under the GUI's nginx `proxy_read_timeout` of 300 s
(`/webapi/`; the API location allows 600 s). Several GUI saves queued behind
each other can still exceed that — see §5.2.

**B. Offload GraphQL command execution off the event loop.** *(Implemented
2026-11-16.)* In `graphql/graphql/queries.py` / `mutations.py`, the inline
`result = method()` becomes `await run_in_threadpool(execute)`, where
`execute` builds the session wrapper and runs the method in a worker thread.
Lock policy:
- **queries** → **no lock**; concurrent, like REST `/show`.
- **mutations** → **the shared config lock from A by default**, acquired
  inside the worker thread. Default-on because in-process op-mode mutations can
  commit the shared session (Perle onboarding does, §4).
- **exempt:** mutations whose name contains `SystemImage` or `WwanFirmware`
  (`_CONFIG_LOCK_EXEMPT` / `uses_config_lock()` in `mutations.py`) —
  `AddSystemImage`, `DeleteSystemImage`, the Perle
  `AddSystemImagePerleImageMutation`, `RenameSystemImagePerleImageMutation`,
  `SetSystemImagePerleImageRenameMutation`, and the cellular firmware
  mutations `SetFirmwareWwanFirmware`, `UpdateArchiveWwanFirmware`,
  `DeleteFirmwareWwanFirmware`, `ExecuteCheckFirmwareWwanFirmware` (added
  2026-11-17, §5.1 C.2) — long-running and not using the shared session.
- `LoadConfigFile` (the only in-tree GraphQL commit, `session/session.py`)
  waits for another session's commit like REST (`wait_for_system_commit`).
- Trade-off accepted: quick op-mode mutations (restart/reset) can now wait
  behind an in-flight API commit (~20–40 s).

Found while doing this: the template override
`session/override/remove_firewall_address_group_members.py` calls
`super().configure()`, but `Session` has no `configure` method — the
template-based config mutations (`session/templates/*.tmpl`) are already
non-functional. Left as is.

**B — test results** (reference unit, phase 1 code as baseline, 2026-11-16):

| Scenario | Before (phase 1 only) | After |
|---|---|---|
| Slow GraphQL query (`ShowUpdateSystem`, update host unreachable, ~12 s) — concurrent `/info`, `/retrieve`, GraphQL `ShowVersion` | all **~12.4 s** (API frozen) | unaffected — `/info` polled every 0.5 s stayed **~10 ms** throughout (same as idle) |
| GraphQL `SaveConfigFile` during a REST `/configure` commit | ran **during** the commit (done at 12.6 s; commit done at 38 s) and froze the loop ~10 s | **waits**, runs after the commit (done at 32.5 s; commit at 25.8 s) |
| GraphQL `DeleteSystemImage` (exempt) during that commit | delayed by the loop freeze | not queued behind the lock |
| GraphQL `ShowConfig` during that commit | 14.4 s | 1.3 s |

A and B together fix the whole *class* of lock-ups — a slow `show`, a firmware
check, or an image download no longer freezes the GUI — and close the
GraphQL-vs-REST config race.
Reentrancy audit (2026-11-17): see §7 — no issues in vyos-1x; one Perle item.

**C. Cache the update checks (remove internet from the request path).**
*(Implemented 2026-11-17 — see "C — implementation and test results".)* Applies
to **both** the ISO update check and the cellular modem firmware check (C.2).
Have each standalone daemon **always** write a machine-readable status file
after each poll (e.g. `/run/vyos-update-status.json` and
`/run/vyos-cellular-firmware-status.json`: local/remote versions, URL, a
timestamp, and a "not checked yet / last error" state) — today both only write
MOTD, and only when an update exists. Change the show functions
(`src/op_mode/system.py` `_compare_version_raw`; `src/op_mode/wwan_firmware.py`
`show_firmware`) to read that file when it is fresh, and otherwise do the live
fetch **and write the file**, so the CLI keeps working when a daemon is
disabled. The cache must be a file: an in-memory cache does not survive the
per-call module reload (§3). Once B lands a live fetch no longer freezes the
API; the remaining value is removing the wait from the GUI's own (single gevent
worker) request path — up to ~9 s for the ISO check and up to ~90 s for the
modem check (three catalog fetches at 30 s timeout each).

Daemon retry: both daemons checked once at boot and then slept 12 h. At boot
the uplink (often the cellular bearer) is usually not up yet, so the check
failed and nothing was retried for 12 h. Now, after a failed check, they retry
after 5 min, doubling up to once an hour, and record the failure in the status
file.

**C — implementation and test results** (2026-11-17):
- `python/vyos/utils/update_status.py`: status files
  (`/run/vyos-update-status.json`, `/run/vyos-cellular-firmware-status.json`,
  0644, written atomically, best effort so non-root CLI callers just skip the
  write), freshness (success: 13 h; failure: 65 min, i.e. until the daemon's
  next retry is due), URL match, backoff (5 min → 60 min), and the shared ISO
  compare (`compare_system_version`, moved from `src/op_mode/system.py`).
- ISO: `vyos-system-update-check.py` records every attempt and retries;
  `show system updates` / GraphQL `ShowUpdateSystem` answer from the cache
  when fresh, else fetch live and record the result.
- Cellular: `execute_check_firmware` records every attempt — an error if the
  catalog download failed (`load_catalog()` silently falls back to its cached
  copy; detected by the cache file not being rewritten), a modem was skipped
  (e.g. FSM identity not ready), or a wwan interface is configured but not
  present yet. The daemon reads that record after each run to decide between
  12 h and a retry. `show_firmware` (CLI and `ShowFirmwareWwanFirmware`) uses
  the authenticated cached catalog while the record is fresh.
- Unit tests: `src/tests/test_update_status.py`. Reference unit (ISO path,
  legacy backend): with the update host unreachable, `ShowUpdateSystem` took
  11.5 s live and 2.1 s from the cached error; with the host reachable, 1.5 s
  cached. The remaining ~1.4 s was `src/op_mode/system.py` building a
  module-level `ConfigTreeQuery()` on every call (the module is re-executed
  per GraphQL call); it now looks up only the URL with one
  `cli-shell-api returnActiveValue` (`active_config_value()`; vyconf backend
  keeps `ConfigTreeQuery`), and cached `ShowUpdateSystem` takes **0.1 s**.
  `show_firmware` uses the same lookup for the catalog URL.
- "Check now": `show system updates refresh` / `ShowUpdateSystem(refresh:
  true)` and `show interfaces wwan <x> firmware refresh` /
  `ShowFirmwareWwanFirmware(refresh: true)` bypass the cache and record the
  new result (unit: 1.1 s live, status rewritten; the GraphQL field appears
  when the schema is regenerated at image build).
  Daemon: status written with the error immediately when unreachable, and
  with the result once reachable.

**C.2 Cellular modem firmware check — parity with the ISO check** (merged from
`psl-master` 2026-11-17: `src/system/vyos-wwan-firmware-check.py`,
`src/systemd/vyos-wwan-firmware-check.service`, `src/op_mode/wwan_firmware.py`,
`system update-check cellular-firmware …` in
`src/conf_mode/system_update-check.py`). It is meant to behave like the ISO
check. Comparison as merged:

| | ISO check | Modem check |
|---|---|---|
| On by default | **yes** — `vyos-build` hook `18-enable-disable_services.chroot` enables `vyos-system-update.service` | **no** — only with `system update-check cellular-firmware auto-check` |
| Turn off | `system update-check disable` | `cellular-firmware disable`; top-level `disable` does not stop it |
| URL | baked `update_url`, override `url` | baked `wwan_firmware_map_url`, override `cellular-firmware url` |
| Schedule | boot, then every 12 h | same |
| Notice | MOTD + `wall` when an update exists | MOTD; `wall` only with the `wall` option and when the notice changes |
| CLI | `show system updates` (live fetch) | `show interfaces wwan X firmware` (live fetch) |
| GraphQL for the GUI | `ShowUpdateSystem`; GUI calls it at login | `ShowFirmwareWwanFirmware` (was broken — fixed, below); GUI does not call it yet |
| Other GraphQL ops | none | `SetFirmware…`, `UpdateArchive…`, `DeleteFirmware…`, `ExecuteCheckFirmware…` mutations |

Done 2026-11-17:
- **GraphQL query fixed.** The op-mode GraphQL resolver calls show functions
  as `func(True, **data)` (convention: `raw` first); `show_firmware` had `raw`
  last, so `True` bound to `interface` and every call raised `TypeError`.
  `raw` is now the first parameter. CLI callers pass keywords and
  `vyos.opmode.run` supplies `raw=False`, so the CLI is unaffected.
- **Firmware mutations exempt from the config lock** (§5.1 B) — a flash takes
  minutes and would otherwise block every API commit.

Done 2026-11-17 (with C):
1. **On by default — only on units with a modem.** Not every product has a
   modem, so the check is tied to wwan configuration rather than enabled in
   the image: like `igos-wwan-manager`, `vyos-wwan-firmware-check.service`
   now has no `[Install]` section and is started/stopped by conf_mode —
   `interfaces_wwan.py` (on every wwan commit, including boot replay) and
   `system_update-check.py` (restarted there so a changed URL or re-enable
   checks at once). It runs while an enabled wwan interface is configured and
   neither `system update-check disable` nor `... cellular-firmware disable`
   is set (`cellular_firmware_check_wanted()`). `auto-check` is kept as an
   accepted no-op, like the ISO's legacy `auto-check`. No `vyos-build` hook
   change is needed (the ISO check's hook just runs `systemctl enable` at image
   build time). `After=vyos-router.service` was removed from the unit: it is
   started from inside vyos-router at boot (same note as the manager unit).
2. **Status file + retry** as above.
2a. **MOTD notice aligned with the ISO check:** same `---` layout with the
   version in blue, no extra lines (the `Run: update firmware wwan …` hint was
   dropped for consistency; `execute_check_firmware` without `--notify` still
   prints it); a failed or
   incomplete check leaves the existing notice in place (it used to delete it
   on any catalog failure or skipped modem). One intended difference: a
   successful check with nothing newer clears the notice, because a modem
   flash does not reboot the unit. `wall` stays opt-in and change-only, as
   plain text.

Remaining:
3. **GUI:** show modem firmware status the same way as ISO status via
   `ShowFirmwareWwanFirmware`, which now answers from the cache (§5.2).
4. **Perle owner to weigh:** firmware flash via GraphQL
   (`SetFirmwareWwanFirmware`, `UpdateArchiveWwanFirmware`) runs **inside the
   API process** — it stops ModemManager, and an API restart mid-flash kills
   the flash; without `noPrompt` it calls `ask_yes_no()` on the service's
   stdin. Consider running it as a background op in a separate process
   (fix D), or not exposing flash over GraphQL.
5. **Not tested on hardware:** the cellular parts (default-on start/stop,
   status, retry) are covered by unit tests and local checks only; the
   reference unit's image predates the merged firmware code.

**D. Long-running ops → background.** *(Implemented for GraphQL op-mode
mutations, 2026-11-25.)* Genuinely long operations can run as background jobs —
start, get an ID, poll — instead of blocking a request. REST already had this
for configure (`/configure?in_background=true` +
`/retrieve/background-operations`); GraphQL now has the same pattern:

- Every generated **op-mode mutation** (in-tree and Perle, since both go through
  `schema_from_op_mode.py`) accepts `in_background: Boolean` in its input and
  returns an `operation: BackgroundOperation` field. With `in_background: true`
  the mutation returns at once with `success: true` and the operation record
  (`op_id`, `name`, `status`, timestamps); `data.result` is empty.
  Timestamps (`created_at`, `started_at`, `finished_at`) are Unix seconds typed
  `Float`, not `Int`: GraphQL `Int` is 32-bit and would fail from 2038.
- Poll with the new `BackgroundOperations(data: {key, op_id})` query (omit
  `op_id` to list all). `status` (enum `BackgroundOperationStatus`) is
  `queued` → `running` → `succeeded` or `failed` — the same strings as REST.
  `result` holds the mutation's normal synchronous response
  (`success`, `errors`, `op_mode_error`, `data.result`), so a client handles
  both the same way; on failure `error` has the message.
- Without `in_background` (or `false`) a mutation behaves exactly as before.
  The flag is always removed before the op-mode function is called.
- Same auth as other operations (API key or token), checked when the job is
  started and on every poll.
- Separate manager from REST (`graphql/graphql/background_operations.py`), so a
  long GraphQL job never delays a REST `in_background` configure. Each job runs
  in its own thread (`BackgroundOpManager.start()`); REST keeps its FIFO
  (`enqueue()`/`drain()`, unchanged behavior). Jobs that use the config lock
  still serialize on it; exempt ones (`SystemImage`, `WwanFirmware`) do not.
  At most 128 jobs may be in progress; more are rejected with an error.
- Records gained an optional `name` (the mutation name). REST
  `/retrieve/background-operations` records therefore now include
  `"name": null` — additive; the REST smoketest reads fields with `.get()` and
  is unaffected.
- Records are in memory: an API restart loses them and kills running jobs, as
  for REST background ops.
- Tests: `src/tests/test_graphql_background.py` (real Ariadne schema built
  from the generator template; skipped where Ariadne/FastAPI are missing).
- **Not covered:** config-session and composite mutations (`LoadConfigFile`,
  `SaveConfigFile`, …) — their generators were not changed. Not yet tested on
  the reference unit; the GraphQL schema is regenerated at package install.

GUI use (Perle repo, optional): for an image install, call
`AddSystemImagePerleImageMutation(data: {key, …, in_background: true})`, keep
`operation.op_id`, and poll `BackgroundOperations` from the Flask job — no
single HTTP request then spans the install, which removes the nginx 600 s
limit (E.2) for that path. A firmware flash started this way still runs inside
the API process (§5.1 C.2 item 4): it no longer blocks a request, but an API
restart still kills it.

**E. Image download reliability (reported 2026-11-17).** A user reported that a
GUI system image (ISO) download failed. Phase 1/2 do **not** fix this: they
stop an install from freezing the rest of the API but change no timeouts. Two
independent causes on the GUI path:

GUI path: browser creates a job and polls it (no browser timeout) → Flask job
thread (`flask/app/core/operation/software_management/software_update.py`)
→ GraphQL `AddSystemImagePerleImageMutation` at `https://127.0.0.1:443`, i.e.
**through nginx**, with no `requests` timeout → in the API process,
`image_installer.add_image()` → `download_file()` →
`vyos.remote.download()` → `HttpC.download()` (`python/vyos/remote.py`),
then install.

*E.1 No stall tolerance or resume in the downloader (CLI and GUI) — fixed, see below.*
`HttpC.download()` opens the local file with `'wb'` (truncate), makes a single
GET with no `Range` header and no retry, and uses `timeout=10.0`, which applies
per read. Any stall over 10 s raises, the install fails, and the next attempt
starts from byte 0. The current ISO is ~806 MB, so on a slow cellular link at
least one >10 s stall is likely and the download fails every time.
`download.perle.com` **supports resume** (verified: `Accept-Ranges: bytes`,
a range request returns `206 Partial Content`); only the client lacks it.
Browsers / `wget -c` / `curl -C -` would resume, which is what users expect
for large files. CLI `add system image <url>` uses the same code, so it fails
the same way (VRF downloads too, via `src/helpers/simple-download.py`).

**Implemented 2026-11-17 — resume after stalls and drops:**
- `vyos.remote.HttpC.download()` (CLI, GUI and VRF ISO downloads, and other
  `vyos.remote` HTTP users such as config load): on a dropped connection, a
  read stalled past the timeout, or a 5xx, reconnect and continue with
  `Range: bytes=<bytes on disk>-`, appending to the file. Up to 8 consecutive
  attempts **without receiving data** (backoff 2 s → 30 s); the count resets
  whenever data arrives, so a slow but progressing link never gives up.
  A server answering `200` instead of `206` restarts from byte 0; 4xx errors
  are not retried. The 10 s per-read timeout is kept: with resume, a short
  timeout recovers faster from a dead connection. The existing space check
  and post-download signature check are unchanged.
- Cellular firmware archives use a **separate** downloader
  (`download_archive()` in `vyos/utils/wwan/interfaces_wwan_firmware.py`,
  urllib, 30 s timeout) — not `vyos.remote` as first noted here — and are
  usually fetched over the cellular link itself. Same resume logic added;
  it also now reads with `read1()`, because the 1 MiB `read()` discarded up
  to 1 MiB of received data on every stall. The archive SHA-256 is still
  verified afterwards.
- **Resume across runs (ISO).** In-run retries only bridge outages of about
  2.5 minutes; in poor coverage a download may need several attempts. ISO
  downloads therefore go to a stable path per URL,
  `/var/cache/vyos/image-downloads/<sha256(url)[:16]>.iso` (persistent
  overlay, survives reboots), with a `<file>.resume` record (URL, size,
  validator). When a run fails, is interrupted, or the session drops, the
  partial file is **kept** and the installer prints "Partial download kept
  (N MiB); run the same command again to resume." Re-running the same command
  (`add system image latest` or the same URL) continues from the end of the
  file. Ranged requests carry `If-Range` with the server's strong ETag (or
  Last-Modified), so if the ISO on the server changed the server sends the
  whole new file instead of a mismatched remainder. A complete file left by a
  run killed during install is reused without downloading again; the
  signature check still validates the result. Housekeeping: only the partial
  for the most recent URL is kept, nothing older than 7 days, and the file and
  its record are deleted after a normal install or abort, or when a complete
  download fails later (e.g. signature).
  `vyos.remote.download(..., resume=True)` is opt-in; other callers are
  unchanged. VRF downloads pass `--resume` to `simple-download.py`.
- Correction: Ctrl+C **during the download** never reached the installer's
  cleanup — `vyos.remote.download()` catches it and calls `sys.exit(1)` —
  which likely explains the 16 MB leftover in E.3. With resume this now
  deliberately keeps the partial file.
- Verified on the reference unit against `download.perle.com`: a run killed
  with SIGKILL at 7.3 MB, then a second run printed "Resuming download at
  6.9 MiB of 769.0 MiB" and continued; the 128 KB around the join matched the
  server's bytes exactly.
- **Resume across runs (cellular firmware).** Catalog release downloads go
  to `/var/cache/vyos/wwan-firmware-downloads/<archive_sha256>.part` (separate
  from the archive cache, which prunes `.partial-*`). On failure or
  interruption the partial is kept with the same "run the same command again
  to resume" hint; the next `update firmware wwan …` for that release
  continues it. Here the catalog's SHA-256 is the integrity guard: only a
  file matching it is moved into the transient release directory, a
  mismatching one is deleted (so a changed server file cannot survive), and a
  complete partial left by a killed run (server answers 416) is verified and
  used. Same housekeeping: only the current release's partial, max 7 days.
  Standalone archives (`update firmware wwan … archive <url>`) have no
  catalog digest and resume within a run only. Verified on the reference
  unit with the real LE910C4-NFD archive (117.7 MB): run killed at 0.3 MB,
  second run "Resuming firmware download at 0.3 MiB", completed and passed
  the catalog SHA-256 check; nothing left behind.
- Tests: `src/tests/test_remote_resume.py` runs both downloaders against a
  local HTTP server (drop mid-file, stall past the read timeout, no range
  support, 404, no data at all). `download.perle.com` range support was
  verified earlier (`206` on the 806 MB ISO); an end-to-end resume over a
  real cellular link has not been tested yet.
- Note `vyos.remote` is upstream code; the change is self-contained in
  `HttpC.download()`.

*E.2 nginx 600 s limit on the GUI → API request (GUI only).* Download plus
install must finish within one request through nginx's API location
(`proxy_read_timeout 600`, `data/templates/https/nginx.default.j2`). An
~806 MB ISO over a few Mbit/s takes longer even without stalls: nginx returns
504, the GUI job shows **failed**, but the install continues in the API and
**may succeed** — the user may later find the image installed. CLI is not
affected (no proxy in the path).
- Proper fix: fix D — the install runs as an API background job polled by
  the GUI's job, so no single HTTP request spans the install.
- Stop-gap: raise `proxy_read_timeout` on the API location (e.g. 1 h), or
  have the GUI call the API's unix socket directly instead of going through
  nginx.

How to tell which cause hit a given failure: `/var/log/nginx/error.log`
`upstream timed out` and a 504 / Gateway Time-out in the GUI job error, with
the image later present in `show system image` → E.2; a read timeout /
connection error in `journalctl -u vyos-http-api` → E.1. Retrying from the CLI
also separates them: it succeeds where the GUI failed only for E.2.

*E.3 `latest` downloaded an older release; partial downloads left behind
(observed on the reference unit 2026-11-17, fixed same day).* The unit ran a
development build (`999.202611171841`, built that day); `add system image
latest` resolved, via the baked-in update URL, to the latest release
`1.5-rolling-202611160734` (a day and a half older) and started downloading
it with no version check. 16 MB of the 806 MB ISO was left in
`/root/<uuid>.iso`: Ctrl+C cleaned up, but a download error (`image_fetch`'s
`except` → `exit(1)`) or a dropped session (SIGHUP) did not. The update check
also said "Update available" because it tested *different*, not *newer*.
Fixed:
- `vyos.version.compare_build_versions()` orders images by the trailing
  `YYYYMMDDHHMM` build stamp shared by release (`1.5-rolling-…`) and
  development (`999.…`) versions; versions without one are "not orderable".
- `add system image latest`: `latest-image-url.py` also returns the listed
  version; if it is older than or the same as the running image, the
  installer says so and asks "Download and install it anyway?" (default no)
  **before** downloading; with `--no-prompt` it declines. Explicit URLs and
  files are unchanged.
- Update check (`show system updates`, `ShowUpdateSystem`, MOTD): an update is
  reported only when the listed image is newer; unorderable versions keep the
  "different" check.
- Partial ISO handling: superseded by resume across runs (E.1) — an
  incomplete download is now kept for resuming (with housekeeping) rather
  than deleted; SIGHUP/SIGTERM still take the Ctrl+C path.
- Cellular firmware, same principle: a downgrade now asks before downloading
  (it used to download first and only confirm at install time, and `now`
  downgraded without asking); `now` refuses a downgrade unless `force` is also
  given. Its transient download directory was already removed on errors and
  Ctrl+C; SIGHUP/SIGTERM now take that path too — a killed run left the
  directory behind, which then blocked retrying that version
  (`work.mkdir(exist_ok=False)`).

**F. Download authenticity (follow-ups, reviewed 2026-11-17).** What a unit
downloads and how each item is authenticated today:

| Download | Signed? | Check today |
|---|---|---|
| `image-version.json` (image update listing) | **no** (`.sig` 404 on download.perle.com) | HTTPS certificate only |
| ISO | yes, `.minisig` | `minisign` against `/usr/share/vyos/keys/*.minisign.pub` (on igOS: `perle.minisign.pub` only; key ID matches the published signature) |
| `modem-firmware.json` (firmware catalog) | yes, detached CMS `.sig` + CRL | `verify_file()` against `igos-firmware-root-ca.pem` (expiry, purpose, revocation) |
| Modem firmware archives | via the signed catalog | SHA-256 from the signed catalog |

Follow-ups:
1. **Sign the image update listing.** Publish `image-version.json.sig` and
   verify it before trusting the version/URL (`vyos.version.get_remote_version`,
   used by `add system image latest`, the update check, MOTD and GUI) — reuse
   the firmware catalog's CMS + CRL scheme (`verify_file()`, same trust store)
   so both listings are handled alike. Today a tampered listing cannot get an
   unsigned ISO accepted, but it can point units at a different, validly
   signed Perle ISO (e.g. an old one) or hide updates; HTTPS limits this to
   someone able to impersonate download.perle.com. Needs a publishing step,
   since the build writes the listing but nothing signs or uploads it.
2. **Refuse unsigned ISOs when nobody can be asked.** In `image_fetch()` a
   missing `.minisig` only prompts interactively; with `no_prompt` the install
   continues unsigned. Make `no_prompt` refuse a missing signature (an
   invalid signature already always prompts), and change the GUI default
   `proceed_without_signature` from `True` to `False`
   (`flask/app/core/operation/service.py`, Perle repo).
3. **Build-input (TOML) integrity.** The build-flavor TOMLs (e.g.
   `igos-am64x-all.toml`, which sets `update_url` and
   `wwan_firmware_map_url`) are never downloaded by units; their values are
   baked into `/usr/share/vyos/version.json` inside the image and are
   protected only as part of the image (ISO signature, signed boot
   artifacts). Their integrity is therefore a build-pipeline concern: who can
   change them in `vyos-build`, and whether builds run from reviewed/signed
   commits. Worth deciding alongside item 1, since a changed `update_url`
   redirects every unit built from it.

**Small, done with B:**
- `mutations.py` returned the key `'errore'` instead of `'errors'`, so op-mode
  errors from mutations lost their error list — fixed.
- `AuthToken` (token-auth mode) — PAM and the user lookup now run in the
  threadpool.

### 5.2 GUI (separate Perle Flask repo) — follow-ups

- **Never run the update check inline on Socket.IO connect.** Background it
  (`gevent.spawn` / the existing scheduler job) and **emit the result over
  Socket.IO when ready**; the connect handler must not block on the network.
- **Prefer reading a cached status** over triggering a live fetch on every page
  open — for both the ISO update status and the cellular firmware status
  (§5.1 C.2).
- **Consider `--workers > 1`** (or ensure all blocking calls are gevent-
  cooperative) so a single blocking greenlet can't freeze the whole GUI. Note
  the GUI already uses `from gevent import subprocess` in places (PAM) — apply
  that discipline everywhere, and confirm `requests` calls are monkey-patched.

#### 5.2.1 GUI save while another commit is running (traced 2026-11-16)

Traced on the General Routing page (`flask/app/core/routing/routes.py`
`update_general_routing`, `.../routing/service.py`,
`react/src/pages/routing/GeneralRoutingPage.tsx`); NAT
(`core/network/nat/service.py`) follows the same pattern, and a rough scan
suggests most config services do. Flow with fix A in place:

1. Browser `PUT /webapi/...` (RTK Query; no client timeout; nginx
   `/webapi/` `proxy_read_timeout 300`, `builds/FILES/flask_include.conf`).
2. Flask reads current config, diffs, runs verify.
3. **Before committing anything**, Flask writes the new config into the GUI's
   local DB (`GeneralRouting.refresh(...)`) and emits a hidden (`type:
   'ignore'`) Socket.IO "Success: Update General Routing" event; the page
   treats it as **finished** and stops its progress indicator.
4. Flask calls REST `/configure` directly — config writes are never queued
   (`send_request` forces `queue=False` for `CONFIGURE`) and the `requests`
   call has **no timeout** (`timeout=` commented out in
   `core/api/http/http_request.py`). gevent keeps other GUI requests running.
5. API: takes the config lock (other GUI/API config writes queue behind it),
   stages the commands, waits up to 120 s for the other commit.
6. Outcome:
   - other commit ends in time → our commit runs → success; nothing further
     shown (the page already looked done);
   - still locked after 120 s (or a commit starts after the check) → API
     **discards the staged change** and returns an error; Flask wraps it as
     **HTTP 200** `[{"success": false, "error": "Configuration system locked
     ..."}]`; React shows a red popup with the first error line and re-fetches
     from VyOS, so the table reverts. **The user's edits are lost.**

Without fix A the same path fails after ~5 s, and `/auth` calls during it froze
the whole API.

GUI follow-ups from this trace:

- **Report success only after the commit succeeds.** Move the "Success"
  Socket.IO emit (and the "finished" page stage) after `/configure` returns
  `success: true`; today a failure can arrive minutes after the page looked
  done.
- **Return a non-2xx status on commit failure** instead of HTTP 200 with
  `success: false`, so every page's error path fires; some services may not
  check `success` at all (rough scan: `nat66`, `wan_load_balancing`, `email`,
  `amp` — unconfirmed).
- **Update the local DB only after a successful commit** (or roll it back on
  failure); today it keeps config that was never applied until the next
  refresh.
- **Keep the user's edits on a "commit in progress" failure** so they can retry
  without re-entering them.
- **Use API background operations for config writes**
  (`/configure?in_background=true` + `/retrieve/background-operations`): the
  request returns immediately, the GUI polls, can show "Waiting for another
  commit…", and is no longer bounded by nginx's 300 s — which several queued
  GUI saves (each up to 120 s wait + 20–40 s commit) can otherwise exceed,
  yielding the "server unavailable" page while the change still applies later.
- **Set a timeout on the Flask→API `requests` call** consistent with the
  above (> 120 s wait + commit), so a hung API can't pin a greenlet forever.

#### 5.2.2 To consider: process-wide output redirection in Perle GraphQL scripts (found 2026-11-17)

`perle_image_mutation.add_system_image()`
(`perle-graphql/vyos-custom-queries/usr/share/vyos-custom-queries/scripts/perle_image_mutation.py`,
around line 406) wraps the image install in
`contextlib.redirect_stdout()` / `redirect_stderr()` to capture the
installer's output.

- Those context managers swap `sys.stdout` / `sys.stderr` for the **whole
  process**, not just the calling thread.
- The script runs **in-process** inside the VyOS HTTP API (GraphQL op-mode),
  and since fix B (§5.1) GraphQL resolvers run concurrently in the threadpool.
- So during a multi-minute install, anything other API requests print goes
  into this mutation's captured output instead of the API log, and the
  install's output can pick up their text. Before fix B this could not happen
  only because the install froze the whole API.
- Impact is low: output and log text only, no config or state effect. But any
  error message the mutation builds from the captured text can include
  unrelated output.

Options:
1. Run the installer in a **subprocess** and capture its output there
   (also isolates the API process from the install — see the in-process flash
   concern in §5.1 C.2 item 4).
2. Stop redirecting: log the installer's output normally and return a
   structured result.
3. Accept as is (document that captured text may contain unrelated lines).

Option 1 is preferred, and fits fix D (long-running ops as background jobs).
`wwan_query.show_all()` also uses `redirect_stdout`, but only for
`raw=False`, which GraphQL never uses, so it is not affected. Any new
in-process GraphQL script should avoid `redirect_stdout`/`redirect_stderr`,
`os.chdir`, `os.umask`, and writes to `os.environ` for the same reason.

---

## 6. Evidence index

Paths under `src/services/api/` unless stated. Line numbers re-verified
2026-11-16.

- API single process: `src/services/vyos-http-api-server` (`server.run()`, no
  `workers=`, uds `/run/api.sock`; one `ConfigSession(os.getpid())` in
  `SessionState`).
- REST locking/threadpool (`rest/routers.py`): threading `lock` (90) and
  separate `asynclock` (92); `_execute_configure_op` acquire/release (395/513);
  deferred HTTPS commit without lock (489/491, 719/721); `/config-file` under
  `asynclock` (681) with inline save/load/merge/diff (685–704); `/retrieve`
  `async` inline (601); `show_op` `def` (823); `/import-pki` lock (923);
  `/auth` `async` acquiring the threading lock on the loop (1008);
  commit-confirm toggling `IN_COMMIT_CONFIRM` in the session env (323, 347).
- GraphQL inline-on-loop: `graphql/graphql/queries.py:100`,
  `graphql/graphql/mutations.py:100` (`result = method()`, no lock);
  `'errore'` typo `mutations.py:109`; op-mode in-process
  `graphql/session/session.py:176` (`gen_op_query`), `199` (`gen_op_mutation`);
  `load_config_file` (load + commit) `session.py:107`; `add_system_image`
  `session.py:129`.
- GraphQL query/mutation classification:
  `graphql/generate/schema_from_op_mode.py:181` (`is_show_function_name`);
  `graphql/generate/config_session_function.py:14` (queries), `27` (mutations).
- Op-mode module reload per call: `graphql/libs/op_mode.py`
  `load_op_mode_as_module` → `python/vyos/utils/system.py:100`
  (`load_as_module`: fresh module + `exec_module` each call).
- Session env is the process env: `python/vyos/configsession.py:184`
  (`session_env = os.environ`).
- Token auth: sync `AuthToken` resolver
  `graphql/graphql/auth_token_mutation.py:30` → PAM
  `graphql/libs/token_auth.py:25`.
- Update check: `src/op_mode/system.py` (`_compare_version_raw`, 39; module-level
  `config`, 25); `python/vyos/version.py` `get_remote_version` (blocking
  `requests`, timeout 3, retry 2); `src/system/vyos-system-update-check.py`
  (12 h daemon, writes MOTD only when an update is available).
- GUI run model (verified on the unit 2026-11-09): `/opt/flask/...` gunicorn
  `GeventWebSocketWorker --workers 1 --timeout 600`;
  `SOCKETIO_ASYNC_MODE=gevent`.
- GUI update chain: `app/socket/socket_manager.py` (`_check_software_update_available`
  inline on connect); `app/event/jobs/system/software_update.py`;
  `app/core/operation/software_management/service.py` (`get_system_update_status`
  → GraphQL `ShowUpdateSystem`).
- nginx routing: GUI `location /` → `127.0.0.1:5000`; API `location
  ~ ^/(retrieve|configure|...|graphql|...)` → `unix:/run/api.sock`; cloud
  `/perlecloud/` → `igos-cloud-proxy`.

---

## 7. Open items / decisions

- **Download authenticity** (§5.1 F): sign the image update listing; refuse
  unsigned ISOs under `no_prompt` and flip the GUI's
  `proceed_without_signature` default; decide build-input (TOML) integrity
  controls.
- **Image download reliability** (§5.1 E): resume/retry done (E.1, ISO and
  cellular firmware). The proper fix for E.2, GraphQL background jobs (fix D),
  is now in vyos-1x; the GUI still has to adopt it. Until it does, decide
  whether to apply the stop-gap (raise the API location's
  `proxy_read_timeout`, or GUI → unix socket) — resume makes slow downloads
  succeed but they can still exceed nginx's 600 s on the GUI path.
- ~~**Deferred HTTPS commit hand-off**~~ — **decided 2026-11-16:** acquire the
  lock in the background task only (§5.1 A); the short exposure window is
  accepted.
- ~~**Torn reads of the shared session during a commit**~~ — **fixed
  2026-11-17:** root cause was `showConfig` diff output, not tearing; reads
  now use the committed config (§4 caveat).
- ~~**Reentrancy audit**~~ — **done 2026-11-17.** GraphQL-reachable code
  (in-tree op-mode in `op-mode-standardized.json`, the GraphQL session, and
  the Perle scripts) checked for process-wide state changes:
  - `IN_COMMIT_CONFIRM` in the shared `os.environ`: only read by the
    post-commit hook (`config_mgmt.py`), which runs inside commits that are
    already serialized — harmless.
  - `ConfigSourceSession.show_config()` writes edit-level vars into
    `os.environ`: always the same root values — harmless.
  - `os.umask()` / `ConfigSession(os.getpid())` in `config_mgmt.py`
    (rollback/commit hooks) and the Perle onboarding CLI path: not reachable
    over GraphQL (exposed `config_mgmt` functions are read-only `show_*`).
  - `/tmp/config.file` in `/config-file`: serialized by the config lock.
  - **To consider (Perle):** process-wide `redirect_stdout`/`redirect_stderr`
    in `perle_image_mutation.add_system_image` — see §5.2.2.
- ~~**Cache format/location**~~ — **decided 2026-11-17:** `/run` JSON files
  (cleared on reboot, the daemons re-check at boot), freshness 13 h after a
  success / 65 min after a failure. "Check now" added as a `refresh` option
  (§5.1 C).
- ~~**Cellular firmware check default-on mechanism**~~ — **decided
  2026-11-17:** conf_mode start/stop tied to wwan configuration, not a
  `vyos-build` hook (§5.1 C.2).
- ~~**Out-of-tree GraphQL resolvers**~~ — **checked 2026-11-16:** the Perle
  `vyos-custom-queries` package installs op-mode scripts, so its operations go
  through the standard op-mode resolver factories and inherit B. Perle
  follow-up: `perle_igos_onboarding_mutation.py` commits the shared session
  directly — it is now serialized by the config lock, but should also call
  `api.config_lock.wait_for_system_commit()` before `session.commit()` so it
  waits for a CLI commit like the in-tree paths.
- **GUI worker count / gevent cooperativeness** — Perle-repo decision.
- ~~**API commit vs. a CLI commit in progress**~~ — **decided 2026-11-16:**
  bounded wait (120 s) then a clear "commit in progress; try again later" error
  (§5.1 A). Revisit the bound if real CLI commits routinely exceed it.
- **Concurrent-edit policy** (§4.1): accept per-node last-commit-wins (today's
  CLI-to-CLI behavior), or add optimistic concurrency for the GUI — e.g. the GUI
  sends the config revision/hash it edited against and the API rejects the
  commit if the touched subtree changed since. At minimum, give GUI users the
  equivalent of `vyatta-cfg-notify` (e.g. a Socket.IO "configuration changed by
  X via Y" event driven from the post-commit hook). Also verify the vyconf
  backend's session model.

---

## Revision notes

**2026-11-16** — second review against the code changed the design:

- The original claim that GraphQL mutations "preserve today's global-lock
  semantics" was wrong: they take no lock, and REST itself uses two unrelated
  locks plus an unlocked deferred commit. Added fix A (one shared config lock)
  and the related rows in §2.
- Found two more loop-blockers outside GraphQL: `/auth` acquiring the threading
  lock on the loop, and `/config-file` running load/merge/save inline.
- Replaced the reader/writer lock with a per-kind policy: queries and op-mode
  mutations lock-free, config mutations under the shared lock. A reader lock
  would make GUI reads wait behind commits.
- Narrowed the reentrancy audit: op-mode scripts are re-executed per call, so
  their module-level state is not shared; the real shared state is `os.environ`,
  `vyos.*` library globals, and fixed temp paths.
- Update-check cache must be a file written on every poll (the daemon only
  writes MOTD when an update exists), with the op-mode writing it on a live
  fetch so the CLI still works without the daemon.
- Recorded `AddSystemImage` (inline ISO download) as the worst-case freeze,
  the `'errore'` typo, and the sync `AuthToken`/PAM resolver; ruled out uvicorn
  `workers=`.
- Added §4.1: the API lock does not reach CLI/SSH sessions; the only
  cross-channel lock is the non-blocking system commit lock; concurrent edits
  are last-commit-wins per node on the legacy backend, with only an advisory
  tty notice to CLI users (verified against `vyatta-cfg`; vyconf unverified).
  Two new open items in §7.

**2026-11-16 (later)** — fix A implemented for REST (`api/config_lock.py`,
`rest/routers.py`) and tested on the reference unit (results table in §5.1).
Decided: deferred HTTPS commit takes the lock in the background task; API
commits wait up to 30 s (later raised to 120 s) for another session's commit. Found that reads of the
shared session's working config can be torn during a commit (§4 caveat, new §7
item). Reference unit confirmed on the legacy backend.

**2026-11-17 (later)** — fix C implemented for both update checks (status
files, cached reads, retry after failure) and tested on the unit for the ISO
path. Cellular firmware check now on by default on units with a configured
wwan interface (conf_mode start/stop, no image hook). The "torn reads" were
root-caused to `showConfig` printing a diff while the shared session has
staged changes; `/retrieve` and GraphQL `ShowConfig` now read the committed
config.

**2026-11-17 (small items)** — `system.py`/`show_firmware` look up only the
URL (cached `ShowUpdateSystem` 1.5 s → 0.1 s); `refresh` option on both show
commands and GraphQL queries; reentrancy audit done (one Perle follow-up).

**2026-11-17 (image downloads)** — added §5.1 E after a user-reported GUI ISO
download failure: the downloader has a 10 s per-read timeout with no retry or
resume (CLI and GUI), although the Perle download server supports range
requests; and the GUI's install request is capped by nginx's 600 s
`proxy_read_timeout`. Neither is fixed by phases 1/2.

**2026-11-17 (latest / downgrades)** — §5.1 E.3: `add system image latest`
and cellular firmware ask before downloading an older image/firmware; the
update check reports only newer images; partial ISO downloads are cleaned up
on failure and on SIGHUP/SIGTERM.

**2026-11-17 (resume)** — §5.1 E.1 implemented: ISO (`vyos.remote`) and
cellular firmware downloads resume with HTTP Range after stalls and drops.

**2026-11-17 (resume across runs)** — interrupted ISO downloads are kept in
`/var/cache/vyos/image-downloads/` and resumed by re-running the command
(If-Range protects against a changed file); verified against the real
server on the reference unit.

**2026-11-17 (firmware resume)** — cellular firmware catalog downloads also
resume across runs (keyed by archive SHA-256); verified on the reference
unit with a real archive.

**2026-11-17 (authenticity)** — added §5.1 F: review of what units download
and how it is authenticated; follow-ups to sign the image update listing,
refuse unsigned ISOs without a prompt, and address build-input (TOML)
integrity.

**2026-11-25 (background jobs)** — fix D implemented for GraphQL op-mode
mutations: `in_background: true` on any generated op-mode mutation starts it
as a background job and returns an `operation` record; poll with the new
`BackgroundOperations` query. Separate manager from REST, one thread per job;
REST background behavior unchanged. Unit-tested locally; not yet tested on the
reference unit.
