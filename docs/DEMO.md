# Demo Walkthrough

A copy-paste tour of the running application using only `curl` against the
default loopback address. Start the server first:

```text
python -m uvicorn app.api.app:app --host 127.0.0.1 --port 8000
```

(On Windows PowerShell use `curl.exe`, or the equivalent
`Invoke-RestMethod` cmdlet.) Every response below is JSON produced by the
real API; nothing here executes a password-cracking engine.

## 1. Liveness

```text
curl http://127.0.0.1:8000/health
```

```json
{"status":"ok","mode":"simulated"}
```

## 2. Read-only catalogue

```text
curl http://127.0.0.1:8000/api/targets
curl http://127.0.0.1:8000/api/engines
```

`targets` lists the enabled format policy (e.g. `pdf-document`,
`zip-archive`); `engines` shows each engine's discovery/capability state
(undiscovered engines simply report their state — a GET never probes).

## 3. Create a target instance

```text
curl -X POST http://127.0.0.1:8000/api/target-instances \
  -H "Content-Type: application/json" \
  -d '{"name":"Demo PDF","target_type":"pdf-document","description":"walkthrough"}'
```

```json
{"id":"…uuid…","name":"Demo PDF","target_type":"pdf-document","description":"walkthrough","created_at":1760000000}
```

## 4. Submit an audit job (denied is a valid, recorded result)

```text
curl -X POST http://127.0.0.1:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"target":"pdf-document","mode":"simulated","instance_id":"…uuid…","reference":"demo"}'
```

The response is a full audit record with a terminal `status` of `denied`
(or `simulated` when a verified engine is present locally), a structured
`preview`, and the snapshotted instance association. Unsupported modes such
as `"real"` are also answered with a structured denial — never a crash.

## 5. Browse, filter, aggregate

```text
curl "http://127.0.0.1:8000/api/jobs?limit=10&sort=created_desc"
curl "http://127.0.0.1:8000/api/jobs?status=denied"
curl "http://127.0.0.1:8000/api/jobs/stats"
curl "http://127.0.0.1:8000/api/target-instances?target_type=pdf-document"
```

List responses carry `count`/`total`/`limit`/`offset`; filters are applied
before pagination. Stats aggregate by status and target.

## 6. Instance delete keeps the audit trail

```text
curl -X DELETE http://127.0.0.1:8000/api/target-instances/…uuid…
curl http://127.0.0.1:8000/api/jobs/…job-id…
```

The delete answers `204`; the job's detail still shows the snapshotted
`instance_id` / `instance_target_type` — deletion cannot unread history.

## 7. Persistence across restarts (optional)

```text
PASSORCH_JOB_DB=./data/passorch.db python -m uvicorn app.api.app:app --port 8000
```

Create a job as above, stop the server, start it again with the same
`PASSORCH_JOB_DB`: the same job list reappears (stores hydrate at
construction). Back up with `app.engines.persistence.backup_database`.

## 8. Bearer auth for writers (optional)

```text
PASSORCH_API_TOKEN=change-me python -m uvicorn app.api.app:app --port 8000
curl -X POST http://127.0.0.1:8000/api/jobs -H "Content-Type: application/json" -d '{"target":"pdf-document"}'
# 401 {"detail":{"error":"unauthorized", …}}
curl -X POST http://127.0.0.1:8000/api/jobs \
  -H "Authorization: Bearer change-me" -H "Content-Type: application/json" \
  -d '{"target":"pdf-document"}'
# 200 with the audit record
```

Reads (`/health`, every `GET`) stay open — see README **Authentication**
for why, and how to front the SPA with a token-aware proxy if needed.

## 9. Browser UI

Open `http://127.0.0.1:8000/` — the dashboard shows health, engines, job
statistics, recent jobs and target instances, with forms for creating
instances and submitting simulated audits (same API calls as above).

## Docker variant

```text
docker compose up --build
curl http://127.0.0.1:8000/health
```

Compose publishes the port on loopback only and stores the SQLite database
on the `passorch-data` volume.
