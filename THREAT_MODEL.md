# PassOrch Threat Model

PassOrch is a **capability-aware password-auditing orchestration platform that
never executes a cracking engine**. Everything it presents — discovery,
compatibility plans, selection, "audits" — is a simulation over static policy
and (optionally) engine metadata discovered at startup. This document states
what we protect, from whom, how, and what we deliberately do not claim.

## Scope and non-goals

* **In scope**: the FastAPI application (`app/`), its SQLite persistence, the
  bundled SPA (`frontend/`), request guards (rate limit, bearer auth, body
  cap), and the Docker/Compose deployment artifacts.
* **Out of scope / non-goals**: real password cracking of any kind (there is
  no execution path), multi-user authorization (single-operator tool),
  in-app TLS termination, cluster-scale rate limiting, secret vaulting, and
  availability under deliberate resource exhaustion beyond the documented
  guards.
* **Default posture**: loopback-only, no authentication, in-memory stores.
  Every hardening control below is either on by default (simulation-only,
  fail-closed policy, strict schemas, parameterised SQL, same-origin SPA) or
  opt-in for deployment (`PASSORCH_API_TOKEN`, `PASSORCH_JOB_DB`, rate/body
  limits).

## Assets

| Asset | Why it matters |
| --- | --- |
| Job and target-instance records (SQLite file) | The operator's audit trail; integrity and confidentiality |
| Bearer token (`PASSORCH_API_TOKEN`) | Write access to jobs/instances when auth is enabled |
| Engine discovery allowlist and capability evidence | The boundary that decides which engines *could* ever be selected |
| Host process and filesystem | No engine execution; no arbitrary file writes |
| Operator environment (env vars, DB path) | Misconfiguration is a security event |

## Trust boundaries

```
network peer ──► uvicorn ──► rate limit ──► bearer auth ──► body cap
                                   │
                                   ▼
                     FastAPI routes (strict schemas)
                                   │
                                   ▼
              ExecutionPolicy (fail-closed, allowlist-backed)
                                   │
                                   ▼
              SQLite persistence (parameterised, fail-closed)
```

* Everything left of the policy boundary is **attacker-controlled input**:
  method, path, headers (including `Authorization`, `Content-Length`), query
  parameters, and JSON bodies.
* Environment variables and the DB path are **operator-controlled**, read
  lazily, validated at construction, and never echo raw values in errors.
* The SPA is same-origin, cookie-free, and token-free: it issues JSON requests
  with no ambient credentials.

## Threats, mitigations, and where they are enforced

| Threat | Mitigation | Locked by |
| --- | --- | --- |
| Engine execution / command injection | No execution path exists: adapters never invoke processes; the single subprocess site (`discovery/runner.py`) uses a fixed allowlist, `shell=False`, captured output, hard timeout; AST regression scans ban `shell=True`, `os.system`, dynamic code | `tests/test_security_regressions.py`, discovery suites, protected adapter files |
| Attacker-supplied argv/targets reaching an engine | Request targets are validated against the read-only `TARGET_FORMAT_POLICY`; unknown targets are structured 422s that never reach job creation | `tests/test_api_jobs.py`, `tests/test_e2e_workflow.py` |
| Unauthorised writes (token guessing) | Opt-in bearer auth with `secrets.compare_digest`, one identical generic 401 for every failure mode, rate limiting counts *before* authentication | `tests/test_auth.py`, `tests/test_e2e_workflow.py` |
| Token leakage | Token never logged (application modules never log), never returned, never placed on `application.state`; constant-time compare; responses never echo it | `tests/test_config_security.py`, `tests/test_security_regressions.py` |
| SQL injection | Parameterised SQL only; every statement is a module constant (single documented `PRAGMA user_version` integer exception); `executescript` banned; AST-locked | `tests/test_persistence_security.py` |
| Data exposure via error messages | Generic structured details (`persistence_unavailable`, `unauthorized`, `rate_limited`…); no SQL text, paths, driver strings or tracebacks in any API response | `tests/test_persistence_security.py`, `tests/test_e2e_workflow.py` |
| Stored XSS via job/instance fields | SPA renders exclusively through `textContent`-style helpers; `innerHTML`/`insertAdjacentHTML`/`document.write` banned and executed in CI; a strict same-origin `Content-Security-Policy` (`default-src 'none'` + explicit same-origin sources, `frame-ancestors 'none'`, `base-uri 'none'`) is issued on every response as defence in depth | `frontend-tests/safety.test.mjs`, `tests/test_frontend_js.py`, `tests/test_config_security.py` |
| CSRF | No cookies, no ambient credentials, no CORS headers; bearer token must be attached explicitly; every GET is side-effect free | `tests/test_config_security.py`, `tests/test_persistence_security.py` |
| Cross-origin data reads | Same-origin SPA, CORS never issued | `tests/test_config_security.py` |
| Request forgery via forwarded headers | `X-Forwarded-For` is never consulted by default; rate limiting keys on the socket peer address only. The opt-in exact-IP trust anchor (`PASSORCH_TRUSTED_PROXIES`) reads the header solely for rate-limit keying, and only when the socket peer is itself trusted; malformed chains fail closed to the peer | `tests/test_security_regressions.py` (single sanctioned read scan), `tests/test_rate_limiting.py` |
| Resource exhaustion (flood of writes/bodies) | Per-peer mutation rate limit (429 + `Retry-After`), body cap (413) with chunked framing rejected outright (400) so the cap cannot be bypassed, bounded FIFO stores, SQLite busy timeout fails closed | `tests/test_rate_limiting.py`, `tests/test_persistence_security.py` |
| Silent data loss / dishonest persistence | Writes are transactional and mirrored fail-closed (503 with rollback, never memory-only success); schema migrations are all-or-nothing; startup failures stop the process | `tests/test_job_persistence.py`, `tests/test_e2e_workflow.py` |
| Privilege confusion at the policy layer | Authenticated ≠ authorised: the execution policy independently denies non-simulated modes and unverified engines, even for valid tokens | `tests/test_e2e_workflow.py`, policy suites |
| Hostile DB file / schema tampering | Rejects newer schema versions fail-closed; corruption surfaces as generic errors; backup via the SQLite online-backup API only | `tests/test_persistence_migrations.py` |

## Residual risks (accepted and documented)

1. **No in-app TLS** — deploy behind a TLS-terminating reverse proxy; the app
   speaks plain HTTP by design.
2. **Reads are unauthenticated** — deliberate: GET endpoints are side-effect
   free and the SPA has no token store. Network-restrict read access if job
   data is sensitive.
3. **Per-process, in-memory rate counters** — reset on restart, not shared
   across workers; multi-worker deployments need proxy-level limits. The
   opt-in `PASSORCH_TRUSTED_PROXIES` anchor (exact IPs only) lets a
   deployment behind a known reverse proxy key on real client hops; a
   client able to reach the app from a trusted address can choose its own
   rate-limit bucket (manipulation of the limiter, never authorisation).
4. **Hydrate-once stores** — a second process reading the same database file
   sees a stale snapshot until restart.
5. **Chunked uploads are unsupported** — state-changing requests that carry
   `Transfer-Encoding` are rejected with HTTP 400 (the body cap cannot be
   bypassed by omitting `Content-Length`); clients must send
   `Content-Length`-framed bodies.
6. **Uvicorn access logs** include method/path/status; they are the only
   operational log surface, and application modules never log.
7. **Dependency supply chain** — application code is stdlib-only; every
   third-party artifact CI and Docker install is hash-verified from
   `requirements.lock` (digests taken from the PyPI JSON API), GitHub
   Actions are pinned to full commit SHAs, and the container base image
   is pinned by registry digest. Residual exposure: pip's isolated build
   environment fetches the `setuptools` build backend without hash
   pinning, and the lock is maintained by hand (regeneration procedure in
   docs/DEVELOPMENT.md; `tests/test_deployment_artifacts.py` enforces the
   pinning format).
8. **Single-operator model** — no per-user authorisation, audit separation or
   tenancy; anyone with the token (or loopback access when auth is off) can
   write.

## Verification

```text
python -m pytest --tb=short          # full suite incl. all security contracts
python -m pytest tests/test_security_regressions.py tests/test_persistence_security.py tests/test_config_security.py tests/test_auth.py tests/test_rate_limiting.py
python -m pytest tests/test_frontend_js_execution.py   # executed frontend safety tests (Node required)
python -m pytest tests/test_browser_e2e.py --tb=short  # optional real-browser E2E (Playwright required)
```
