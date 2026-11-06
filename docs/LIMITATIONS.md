# Limitations

What PassOrch does **not** do, stated plainly. Security-relevant residual
risks are expanded in [THREAT_MODEL.md](../THREAT_MODEL.md#residual-risks-accepted-and-documented).

## Functional

* **Simulation only.** No password is ever cracked and no cracking engine is
  ever executed. A job's terminal status is one of `simulated`, `denied` or
  `failed`; `denied` is a normal, fully recorded outcome (unverified engines,
  unsupported modes such as `real`, unmapped targets).
* **Engine discovery is metadata-only** and runs at most allowlisted
  `(binary, operation)` commands (`shell=False`, fixed names, timeout,
  captured output). With no engines installed, every audit is denied — by
  design. Discovery is triggered explicitly — the guarded
  `POST /api/engines/refresh` endpoint or a code/test call — never by a
  `GET`, a page load, or any automatic schedule; the endpoint performs
  metadata discovery only, never password auditing or cracking.
* **Hashcat cannot produce `VERIFIED` capability evidence today.** No
  capability operation for hashcat is on the discovery allowlist, so it is
  never probed for capabilities and reports `capability_status: "unknown"`
  even when its binary is found. Only John can currently reach `verified`.
* **Targets are a fixed policy map.** Arbitrary formats cannot be added at
  runtime; new formats are a code change.
* **Job audit-trail exports are point-in-time records, not event logs.**
  `GET /api/jobs/{job_id}/export` carries the single persisted `JobInfo`
  record inside a versioned envelope (`schema_version: "1"`): there is
  **no lifecycle event history, no actor identity, no fabricated completion
  timestamp**, and **no engine-version snapshot where the record never held
  one**. `exported_at` marks file production only — it is not a job
  lifecycle event — and later capability refreshes never rewrite an
  exported record (creation-time evidence is preserved by design). Exports
  contain no raw output or secrets, and like everything else here they
  describe simulations: **no real password auditing or cracking exists to
  export**.

## Persistence and state

* **Hydrate-once stores.** Persistent stores load once at construction; a
  second process over the same database file sees a stale snapshot until it
  restarts. There is no live cross-process visibility or replication, and
  writes are not coordinated: one process's capacity eviction or delete
  removes rows the other process still serves from memory. **Run exactly one
  worker process per database file.**
* **Discovery registry and refresh lock are process-local.** The capability
  registry and the non-blocking refresh lock live in one process: separate
  worker processes neither share refreshed capability state nor serialise
  refreshes against each other (concurrency is rejected with `409` only
  *within* a process). There is no distributed lock.
* **Single database file.** Jobs and target instances share one SQLite file;
  there is no multi-node setup, external database, or HA story.
* **Backups are your job.** `backup_database` (online-backup API) is
  provided; scheduling, encryption and retention are not.
* **Capacity-bounded retention, pruned at open.** Each store keeps at
  most `capacity` records (default 100 per store, newest wins); opening
  a database that holds more rows prunes the oldest rows from the file
  (one-way — there is no archive or undelete).
* **Schema upgrades are one-way.** A database stamped by a *newer* schema
  than the running code is rejected fail-closed.

## Request guards

* **Rate limits are per process and per peer address.** Counters reset on
  restart and are not shared across workers; multi-worker or multi-host
  deployments need proxy-level limits. `X-Forwarded-For` is ignored by
  default; operators behind a known reverse proxy can pin an exact-IP
  trust anchor (`PASSORCH_TRUSTED_PROXIES`) so real client hops become
  the key — every other request guard is unaffected, and malformed
  forwarding data fails closed to the socket peer (README, "Trusted
  proxies").
* **Chunked uploads are unsupported.** State-changing requests that carry
  `Transfer-Encoding` are rejected with HTTP 400 before any body is read,
  so the `Content-Length` body cap cannot be bypassed; clients must send
  `Content-Length`-framed bodies (the bundled SPA always does).
* **No in-app TLS, CORS, or multi-user authorisation.** Put a reverse proxy
  in front for TLS; the API is same-origin with no CORS by design; the
  bearer token (when enabled) is a single shared secret, not an identity
  system. Reads stay unauthenticated by design.

## Frontend and accessibility

* **No formal WCAG conformance claim.** The UI aims for WCAG 2.2 AA practices
  (landmarks, skip link, visible focus, labelled controls, captioned tables,
  reduced-motion and forced-colour rules), but it has not been through a formal
  accessibility audit or assistive-technology matrix. Treat accessibility as a
  continuing requirement, not a certified result.
* **Modern evergreen browsers only.** The stylesheet uses modern CSS such as
  `color-mix()`, `:focus-visible`, `prefers-*` media queries and forced-colour
  keywords. There are no polyfills; very old browsers may lose colour tints or
  focus styling, though content remains text-based and readable.
* **English only.** There is no localisation layer or translated copy.
* **Narrow-screen tables scroll, they do not reflow into cards.** Below the
  table minimum width the wrapper scrolls horizontally, which keeps headers
  aligned; the page itself does not overflow. Very large tables therefore need
  a horizontal swipe on a phone.
* **No client-side persistence.** Filters, pagination, form values and the
  selected job live in memory for the session; a reload returns to the default
  view. The backend remains the only source of truth.
* **Browser automation is local-only.** The Playwright suite is optional and is
  not installed or run by CI. Frontend logic is covered in CI by the Node
  harness (`frontend-tests/`) and static Python contracts; responsive and
  accessibility checks are manual review steps, so they are not regression-proof.

## Operations

* **Uvicorn access logs plus the audit-event stdout are the only log
  surfaces** — the application otherwise never logs (a tested invariant so
  secrets cannot reach stdout). Both surfaces share stdout (uvicorn's
  access-log handler defaults to it), so consumers identify audit records
  by the closed JSON schema, never by stream position or order. The audit
  service emits only its closed schema, best-effort and fail-silent (a
  sink failure never changes a response, discovery result or registry
  state) — and it is not the authoritative record (the persisted job
  record and the discovery registry are): since M3.2/M3.3 the job-create
  route emits `job_created` / `job_create_rejected` and the
  discovery-refresh route emits `refresh_requested` / `refresh_rejected` /
  `refresh_completed`, while GET routes are excluded. No raw probe output,
  executable paths, subprocess arguments or exception messages are ever
  emitted, and there is no persistent audit-event store and no
  export-history feature; retention and rotation are the runtime's
  responsibility (stdout, no files).
* **Default deployment is loopback-only.** Compose publishes
  `127.0.0.1:8000`; exposing it remotely is an operator decision with the
  consequences listed in README **Deployment**.
* **No browser automation in CI.** Frontend logic is executed under Node in
  `frontend-tests/`; an optional real-browser E2E suite
  (`tests/test_browser_e2e.py`) runs locally when Playwright is installed,
  but CI does not install or run it.
