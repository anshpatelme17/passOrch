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
  design.
* **Targets are a fixed policy map.** Arbitrary formats cannot be added at
  runtime; new formats are a code change.

## Persistence and state

* **Hydrate-once stores.** Persistent stores load once at construction; a
  second process over the same database file sees a stale snapshot until it
  restarts. There is no live cross-process visibility or replication.
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

## Operations

* **Uvicorn access logs are the only log surface** — the application never
  logs (a tested invariant so secrets cannot reach stdout).
* **Default deployment is loopback-only.** Compose publishes
  `127.0.0.1:8000`; exposing it remotely is an operator decision with the
  consequences listed in README **Deployment**.
* **No browser automation in CI.** Frontend logic is executed under Node in
  `frontend-tests/`; an optional real-browser E2E suite
  (`tests/test_browser_e2e.py`) runs locally when Playwright is installed,
  but CI does not install or run it.
