# igOS HTTP API Concurrency & GUI Lock-out — Design / Analysis

Status: **Analysis complete; fixes proposed, not yet implemented (2026-11-09)**

Scope: why slow operations lock up the igOS web GUI, grounded in the running
unit and the vyos-1x source. The trigger that surfaced it was the "check for new
firmware" notification, but the root cause is **systemic**: multiple
single-process / single-worker chokepoints in series, the worst being that the
**GraphQL API executes every command synchronously on its one event loop**. This
doc records the architecture, the evidence, the concurrency-safety constraints,
and the proposed fixes (vyos-1x side) plus the GUI-side follow-ups (separate
Perle Flask repo).

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

## 2. The three serialization points in the VyOS API

| Surface | Code | Concurrency behavior |
|---|---|---|
| **REST read ops** (`/show`, `/generate`, `/image`, `/reset`, …) | plain `def` endpoints (e.g. `show_op`, `routers.py:823`) | FastAPI runs `def` endpoints in the **anyio threadpool** → **concurrent**, do **not** block the event loop |
| **REST config ops** (`/configure`, `/config-file`, `/import-pki`, `/auth`) | `async def` + global `lock = Lock()` (`routers.py:90`), `lock.acquire()`→`release()` around `_execute_configure_op` (`routers.py:395`,`513`); commits via `run_in_threadpool` | **serialized** by a global lock ("the lock is really global"); loop not blocked (commit offloaded) |
| **GraphQL** (queries *and* mutations) | `async def func_impl` that calls `result = method()` **inline** — no `await`, no `run_in_threadpool` (`graphql/graphql/queries.py:101`); `method()` runs op-mode **in-process** via `load_op_mode_as_module` + `func()` (`graphql/session/session.py:188`) | **runs the command synchronously on the single event loop** → a slow command **freezes the entire API for all clients** |

**The GraphQL row is the systemic bug.** REST shows parallelize (threadpool) and
REST config serializes (lock) — both reasonable. GraphQL, however, executes the
op-mode/config function **inline on the event loop**, so **any** slow GraphQL
operation (a multi-second `show`, a `generate`, anything that waits on I/O or a
subprocess) blocks every other API request — including all of the GUI's, since
**the GUI uses GraphQL** (`graphql/session/composite/system_status.py`;
`ShowUpdateSystem` via the GUI's `graphql_service.send_request`).

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
daemon already polls every 12 h off-API and writes MOTD
(`src/system/vyos-system-update-check.py`), but the GUI/op path ignores it and
re-fetches live.

---

## 4. Concurrency-safety constraints (why we cannot just parallelize everything)

Splitting the proposed "offload to threadpool" fix by operation type:

- **Config mutations (`set`/`delete`/`commit`) — must stay serialized.** All
  resolvers share **one** `ConfigSession`; the backend has a single commit lock.
  Parallel mutations would corrupt the shared session / collide on commit. The
  REST global `lock` exists for exactly this and must be preserved. (Pre-existing
  characteristic: the single shared session also makes multi-client config
  editing coarse — the lock serializes it; parallelism neither causes nor fixes
  that.)
- **Read ops (`show`/`generate`) — generally parallelizable, with caveats.** The
  GIL prevents memory corruption, but **logical races** are possible in op-mode
  scripts using **module-level mutable state**, **fixed temp-file paths**, or
  **non-reentrant resources** (e.g. `src/op_mode/system.py:25` has a module-level
  `config = ConfigTreeQuery()`). Needs a quick audit before enabling N-way
  concurrency.
- **Reassurance:** REST `/show` **already** runs op-mode reads concurrently in
  the threadpool against the same shared session, so concurrent reads are already
  the production model — aligning GraphQL with it is not a new, untested risk.
  (One wrinkle: GraphQL runs op-mode **in-process** vs REST's `session.show()`,
  so the in-process path deserves the audit above.)

---

## 5. Proposed fixes

### 5.1 vyos-1x (this repo) — the high-leverage fix

**A. Offload GraphQL command execution off the event loop, with a reader/writer
lock.** In `graphql/graphql/queries.py` / `mutations.py`, replace the inline
`result = method()` with `result = await run_in_threadpool(method)`, and guard
execution with a **reader/writer lock**:
- **queries → shared (reader) lock** → concurrent `show`s; event loop stays free.
- **mutations → exclusive (writer) lock** → commits remain strictly serialized
  (preserves today's global-lock semantics; no parallel writes).
This fixes the whole *class* of lock-ups, not just the firmware check — a slow
cloud `show` no longer freezes the GUI.
*Prereq:* audit the op-mode `show` scripts the GUI calls for unsafe module-level
state / shared temp files before enabling concurrent reads.

**B. Cache the update check (remove internet from the request path).** Have the
standalone daemon (`src/system/vyos-system-update-check.py`) persist its 12 h
result to a cache (e.g. `/run/vyos-update-status.json`); change
`src/op_mode/system.py` `show_update` / `_compare_version_raw` to **read the
cache** instead of `get_remote_version`, with a bounded live refresh only on
explicit request. Fix B alone makes `ShowUpdateSystem` instant and stops the
GUI-lock chain at the source even before A lands.

**C. Long-running ops → background.** Route genuinely long operations through the
existing `BackgroundOpManager` (`src/services/api/background.py`) — enqueue + poll
— instead of blocking a request.

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

---

## 6. Evidence index (verified 2026-11-09)

- API single process: `src/services/vyos-http-api-server` (`server.run()`, no
  `workers=`, uds `/run/api.sock`).
- REST locking/threadpool: `src/services/api/rest/routers.py` — `lock` (90),
  `_execute_configure_op` acquire/release (395/513), `show_op` `def` (823),
  `run_in_threadpool` (41), import-pki (923), auth (1008), config-file asynclock
  (681).
- GraphQL inline-on-loop: `src/services/api/graphql/graphql/queries.py:101`
  (`result = method()`); op-mode in-process `.../session/session.py:188`.
- Update check: `src/op_mode/system.py` (`_compare_version_raw`, 40; module-level
  `config`, 25); `python/vyos/version.py:101`/`134` (blocking `requests`,
  timeout 3, retry 2); `src/system/vyos-system-update-check.py` (12 h daemon,
  MOTD only).
- GUI run model: `/opt/flask/...` gunicorn `GeventWebSocketWorker --workers 1
  --timeout 600`; `SOCKETIO_ASYNC_MODE=gevent`.
- GUI update chain: `app/socket/socket_manager.py` (`_check_software_update_available`
  inline on connect); `app/event/jobs/system/software_update.py`;
  `app/core/operation/software_management/service.py` (`get_system_update_status`
  → GraphQL `ShowUpdateSystem`).
- nginx routing: GUI `location /` → `127.0.0.1:5000`; API `location
  ~ ^/(retrieve|configure|...|graphql|...)` → `unix:/run/api.sock`; cloud
  `/perlecloud/` → `igos-cloud-proxy`.

---

## 7. Open items / decisions

- **Reader/writer lock vs. conservative single lock** for GraphQL (§5.1 A):
  RW-lock gives concurrent reads (best GUI responsiveness) but requires the
  op-mode read-reentrancy audit; a plain exclusive lock is safer-but-slower
  (fixes the loop-freeze, keeps reads one-at-a-time) and can be relaxed later.
- **Op-mode read-reentrancy audit** — enumerate the `show`/`generate` scripts the
  GUI calls and check for module-level mutable state / shared temp files.
- **Cache format/location** for the update status (`/run/...` vs a persistent
  path) and the "refresh now" UX (background op + poll).
- **GUI worker count / gevent cooperativeness** — Perle-repo decision.
- Whether to extend the threadpool-offload model to any other `async` API paths
  that execute work inline.
