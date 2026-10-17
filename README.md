# PassOrch

Simulation-only password-audit planning platform. No engine is ever executed.

## Architecture

PassOrch is a FastAPI application with a vanilla-JavaScript single-page frontend. It follows a layered architecture:

- **Data layer** (`app/engines/`): Immutable records, bounded thread-safe stores, capability parsing, execution policy
- **Orchestration layer** (`app/core/orchestrator/`): Composes data layer with selection, authorization, and simulation
- **API layer** (`app/api/`): FastAPI routes with strict Pydantic schemas (`extra="forbid"`)
- **Frontend** (`frontend/`): Hash-routed SPA with safe DOM helpers (no `innerHTML`)

## Security Model

- **Simulation-only**: No real password cracking, no engine execution, no subprocesses
- **Fail-closed authorization**: `ExecutionPolicy.authorize` gates everything; unknown/unmapped targets denied
- **Strict schemas**: All request/response models use `extra="forbid"`
- **No discovery via GET**: Read-only endpoints never trigger engine probing or capability refresh
- **Bounded storage**: In-memory stores with FIFO eviction, thread-safe with RLock

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check — returns `{status, mode}` |
| GET | `/api/targets` | List read-only target format policy |
| POST | `/api/targets/{target_id}/compatibility/plan` | Simulation-only plan preview |
| GET | `/api/engines` | List all engine capability states |
| GET | `/api/engines/{engine}` | One engine's capability state |
| POST | `/api/jobs` | Create terminal audit job (simulated/denied/failed) |
| GET | `/api/jobs` | List jobs (newest-first). Query params: `status`, `target`, `limit` (default 50, max 100), `offset` (default 0) |
| GET | `/api/jobs/stats` | Aggregated job statistics |
| GET | `/api/jobs/{job_id}` | Fetch one job (404 for unknown) |
| DELETE | `/api/jobs/{job_id}` | Delete a job (204/404) |
| POST | `/api/target-instances` | Create target instance |
| GET | `/api/target-instances` | List target instances. Query params: `limit` (default 50, max 100), `offset` (default 0) |
| GET | `/api/target-instances/{id}` | Get one instance |
| DELETE | `/api/target-instances/{id}` | Delete instance |

## Frontend Views

| Route | Description |
|-------|-------------|
| `/dashboard` | Health, engines, statistics, recent jobs, instances |
| `/engines` | Read-only engine capability table |
| `/targets` | Read-only target catalog with plan links |
| `/instances` | CRUD for target instances |
| `/plan` | Plan preview form + result |
| `/jobs` | Job creation, list, detail, delete |

## Setup

```bash
pip install -e .
```

## Running

```bash
uvicorn app.api.app:app --reload
```

Then open http://127.0.0.1:8000/

## Testing

```bash
# Python tests
python -m pytest --tb=short -q

# Lint
python -m ruff check .

# JavaScript tests (requires Node.js)
node --test frontend-tests/*.test.mjs
```

## Project Structure

```
app/
  api/           # FastAPI routes and schemas
  core/          # Orchestration layer
  engines/       # Data layer (stores, discovery, policy)
frontend/
  js/            # ES modules (api, dom, components, app, main)
  css/           # Styles
  views/         # One module per route
frontend-tests/  # Node.js test harness
tests/           # Python test suite
```
