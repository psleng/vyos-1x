# igOS HTTP API Concurrency & GUI Lock-out — Design / Analysis

Status: **Analysis complete; fixes proposed, not yet implemented**
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
  **Caveat (observed 2026-11-16):** reads that parse the *shared session's
  working config* — REST `/retrieve` (`showConfig`, and `Config(session_env=…)`
  lookups) and GraphQL `ShowConfig` — can observe a **torn** config while an
  API commit is in flight: the unit logged `/retrieve` 500s with
  `ValueError: Failed to parse config: Syntax error on line 9 …` during
  commits, both before and after the fix-A change. On the legacy backend the
  commit tears down and remounts the session's union view
  (`vyatta-cfg` `cstore-unionfs.cpp:815`–`840`), so a concurrent reader can
  catch it mid-way. Since fix A makes each API request's stage+commit atomic,
  between requests the shared session has no pending changes; such reads
  could therefore read the **active** config instead of the session (no
  wait, no tear from the session remount) — see §7.
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
- **exempt:** mutations whose name contains `SystemImage`
  (`uses_config_lock()` in `mutations.py`) — `AddSystemImage`,
  `DeleteSystemImage`, and the Perle `AddSystemImagePerleImageMutation`,
  `RenameSystemImagePerleImageMutation`, `SetSystemImagePerleImageRenameMutation`
  — long-running and not using the shared session.
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
*Remaining:* the narrowed reentrancy audit in §4 (process env, `vyos.*`
globals, fixed temp paths) — no problems seen in testing.

**C. Cache the update check (remove internet from the request path).** Have the
standalone daemon (`src/system/vyos-system-update-check.py`) **always** write a
machine-readable status file (e.g. `/run/vyos-update-status.json` with the
remote/local versions, URL and a timestamp) after each poll — today it only
writes MOTD when an update exists. Change `src/op_mode/system.py`
`_compare_version_raw` to read that file when it is fresh, and otherwise do the
live fetch **and write the file**, so the CLI keeps working when the daemon is
disabled. The cache must be a file: an in-memory cache does not survive the
per-call module reload (§3). Once B lands this no longer freezes the API; its
remaining value is removing a ~9 s wait from the GUI's own (single gevent
worker) request path.

**D. Long-running ops → background.** Route genuinely long operations through the
existing `BackgroundOpManager` (`background.py`) — enqueue + poll — instead of
blocking a request.

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
  open.
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

- ~~**Deferred HTTPS commit hand-off**~~ — **decided 2026-11-16:** acquire the
  lock in the background task only (§5.1 A); the short exposure window is
  accepted.
- **Torn reads of the shared session during a commit** (§4 caveat, observed):
  switch `/retrieve` and GraphQL `ShowConfig` (and other `Config(session_env=…)`
  reads outside a config op) to read the **active** config rather than the
  shared session, or retry once on a parse error. Taking the config lock for
  them is the fallback but makes GUI config reads wait ~20–40 s behind every
  commit.
- **Reentrancy audit** (§4) — check the process env (`IN_COMMIT_CONFIRM`),
  globals in the `vyos.*` modules the GUI's op-mode calls import, and fixed temp
  paths. Decide whether commit-confirm should stop mutating the shared
  `os.environ` (pass an env copy to the commit instead).
- **Cache format/location** for the update status (`/run/...` vs a persistent
  path), freshness window, and the "refresh now" UX (background op + poll).
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
