# Release Notes

## v0.1.0

PassOrch is a **simulation-only** password-audit planning platform. This
release is the first complete vertical slice: capability discovery, plan
preview, target instances, recorded jobs and the full browser console. It does
not crack passwords, execute a cracking engine, or provide real audit results.

### Included

- **FastAPI application** with strict Pydantic schemas (`extra="forbid"`) and
  one structured error envelope.
- **Fail-closed execution policy** — unknown targets, non-simulated modes and
  unverified capability deny with a recorded reason.
- **Metadata-only engine discovery** through the guarded
  `POST /api/engines/refresh`; reads never probe.
- **Simulated jobs** with `simulated`, `denied` and `failed` terminal states,
  optional target-instance association snapshot, JSON audit-trail export and
  deletion.
- **Target instances** with create, read, update (name/description only) and
  delete.
- **Optional SQLite persistence**, opt-in bearer authentication, per-address
  mutation rate limiting, body-size cap and trusted-proxy support.
- **Six-page vanilla-JavaScript SPA** — dashboard, engines, targets, instances,
  plan preview and jobs — with a shared design system, safe text-only rendering,
  per-section loading/empty/error/retry states, duplicate-submission guards and
  stale-response protection.
- **Responsive and accessible UI** — 1440/1280/1024/768/390/360px layouts,
  keyboard-visible focus, semantic captioned tables, labelled controls,
  reduced-motion, high-contrast and forced-colours handling.
- **Documentation** — this file, `README.md`, `THREAT_MODEL.md`,
  `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/DEMO.md` and
  `docs/LIMITATIONS.md`.

### Verification checklist

Run from the repository root:

- [ ] `python -m pytest --tb=short -p no:cacheprovider`
- [ ] `python -m ruff check .`
- [ ] `python -m mypy app/api app/engines`
- [ ] `python scripts/check_dependency_lock.py`
- [ ] `node --test --test-reporter=tap`
- [ ] `git diff --check`
- [ ] Optional local browser pass: `python -m pytest tests/test_browser_e2e.py --tb=short`
- [ ] Manual spot-check at 1440/1280/1024/768/390/360px: no horizontal page
      overflow, one simulation banner, readable tables, keyboard-reachable
      controls.
- [ ] Confirm `git status` contains only intended changes and no generated
      data files, databases or credentials.

### Not included

- Real password auditing or cracking of any kind.
- Multi-user accounts, roles, tenancy or audit separation.
- In-app TLS, CORS or a distributed rate limiter.
- A formal WCAG audit or legacy-browser support.
- Browser automation in CI (the Playwright suite is optional and local).

### Release status

The release notes and checklist are documentation only. Publishing packages,
pushing tags and creating hosted releases are release-manager actions and are
not performed by this checklist.
