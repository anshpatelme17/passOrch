"""Phase 3B.4 - end-to-end integration and security hardening tests.

Covers the full 3B pipeline end-to-end with injected dependencies:
- authorized simulation flow from registry -> create_job -> API retrieval
- fail-closed denial scenarios (UNKNOWN/ERROR, no verified capability,
  invalid mode, unmapped/unsupported targets)
- determinism, job/simulation identity (job_id != simulation_id), distinct IDs
- FIFO eviction by insertion order (not created_at), bounded store
- concurrency (unique IDs, no corruption, store bounded)
- forbidden-field/raw-output protection (recursive checks on jobs/responses)
- static security boundaries (no subprocess/shell)
- API regression: GET endpoints do not mutate/store or trigger discovery
- existing endpoints still functional
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.orchestrator.jobs import create_job
from app.engines.adapter import FORBIDDEN_REQUEST_FIELDS, ExecutionStatus
from app.engines.discovery.models import (
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    HASHCAT,
    JOHN,
)
from app.engines.discovery.registry import FORBIDDEN_PAYLOAD_KEYS, DiscoveryRegistry
from app.engines.jobs import (
    DEFAULT_JOB_STORE_CAPACITY,
    JobStatus,
    JobStore,
    default_job_store,
)

E2E_MODULE = Path("tests/test_phase3b_e2e.py")
JOBS_ROUTE_MODULE = Path("app/api/routes/jobs.py")

FORBIDDEN_KEYS = FORBIDDEN_REQUEST_FIELDS | FORBIDDEN_PAYLOAD_KEYS

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN
ERROR = CapabilityStatus.ERROR


def _registry(status: CapabilityStatus, *, engine: str = JOHN) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    registry.register(EngineSnapshot(engine=engine, discovered=True, binary_found=True))
    registry.set_capabilities(
        engine,
        EngineCapabilities(engine=engine, capability_status=status, formats=("pdf",)),
    )
    return registry


def _client(
    *,
    registry: DiscoveryRegistry | None = None,
    store: JobStore | None = None,
) -> TestClient:
    reg = registry if registry is not None else _registry(VERIFIED)
    st = store if store is not None else JobStore()
    return TestClient(create_app(discovery_registry=reg, job_store=st))


def _forbidden_keys(payload: object) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_KEYS:
                found.add(key)
            found |= _forbidden_keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _forbidden_keys(item)
    return found


def test_e2e_authorized_simulation_full_flow() -> None:
    store = JobStore()
    reg = _registry(VERIFIED)
    job = create_job("pdf-document", discovery_registry=reg, store=store, clock=lambda: 10)
    assert job.status is JobStatus.SIMULATED
    assert job.target == "pdf-document"
    assert job.failure_reason == ""
    assert job.preview is not None
    assert job.preview.simulation is not None
    assert job.result() is job.preview.simulation
    assert job.result().status is ExecutionStatus.SIMULATED
    assert job.job_id != job.result().simulation_id
    assert store.get(job.job_id) is job

    c = _client(registry=reg, store=store)
    res = c.get(f"/api/jobs/{job.job_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "simulated"
    assert body["job_id"] == job.job_id
    assert body["preview"]["simulation"]["simulation_id"] == job.result().simulation_id
    assert not _forbidden_keys(body)


def test_e2e_denied_unknown_capability() -> None:
    reg = _registry(UNKNOWN)
    job = create_job("pdf-document", discovery_registry=reg)
    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "capability_not_verified"
    assert job.preview is not None
    assert job.preview.simulation is None
    assert job.result() is None


def test_e2e_denied_error_capability() -> None:
    reg = _registry(ERROR)
    job = create_job("pdf-document", discovery_registry=reg)
    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "capability_not_verified"
    assert job.result() is None


def test_e2e_denied_real_mode() -> None:
    reg = _registry(VERIFIED)
    job = create_job("pdf-document", discovery_registry=reg, mode="real")
    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "non_simulation_request"
    assert job.preview is not None
    assert job.preview.simulation is None


def test_e2e_denied_invalid_mode() -> None:
    reg = _registry(VERIFIED)
    job = create_job("pdf-document", discovery_registry=reg, mode="banana")
    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "unsupported_execution_mode"
    assert job.preview is not None
    assert job.preview.simulation is None


def test_e2e_denied_unmapped_target() -> None:
    reg = _registry(VERIFIED)
    job = create_job("unknown-target", discovery_registry=reg)
    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "invalid_request"
    assert job.target == "unknown-target"


def test_e2e_denied_empty_discovery_registry() -> None:
    reg = DiscoveryRegistry()
    job = create_job("pdf-document", discovery_registry=reg)
    assert job.status is JobStatus.DENIED
    assert job.failure_reason == "capability_not_verified"


def test_e2e_determinism_and_identity() -> None:
    reg = _registry(VERIFIED)
    job1 = create_job("pdf-document", discovery_registry=reg, clock=lambda: 5)
    job2 = create_job("pdf-document", discovery_registry=reg, clock=lambda: 6)
    assert job1.job_id != job2.job_id
    assert job1.result() is not None and job2.result() is not None
    assert job1.result().simulation_id == job2.result().simulation_id
    assert job1.result().simulation_id != job1.job_id


def test_e2e_fifo_eviction_ignores_created_at() -> None:
    store = JobStore(capacity=2)
    reg = _registry(VERIFIED)
    j1 = create_job("pdf-document", discovery_registry=reg, store=store, job_id="00000000-0000-4000-8000-000000000001", clock=lambda: 10)
    j2 = create_job("pdf-document", discovery_registry=reg, store=store, job_id="00000000-0000-4000-8000-000000000002", clock=lambda: 1)
    j3 = create_job("pdf-document", discovery_registry=reg, store=store, job_id="00000000-0000-4000-8000-000000000003", clock=lambda: 2)
    assert store.get(j1.job_id) is None
    assert store.get(j2.job_id) is j2
    assert store.get(j3.job_id) is j3
    listed = [j.job_id for j in store.list()]
    assert listed == ["00000000-0000-4000-8000-000000000003", "00000000-0000-4000-8000-000000000002"]


def test_e2e_concurrent_job_creation_unique_ids_bounded_store() -> None:
    store = JobStore(capacity=5)
    reg = _registry(VERIFIED)
    barrier = threading.Barrier(20 + 1)

    def mk(n: int) -> None:
        barrier.wait(timeout=30)
        create_job("pdf-document", discovery_registry=reg, store=store, job_id=f"00000000-0000-4000-8000-{n:012x}")

    threads = [threading.Thread(target=mk, args=(i,)) for i in range(1, 21)]
    for t in threads:
        t.start()
    barrier.wait(timeout=30)
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert len(store) == 5
    assert len(store.list()) == 5
    jobs = store.list()
    ids = {j.job_id for j in jobs}
    assert len(ids) == 5
    for j in jobs:
        assert j.status in (JobStatus.SIMULATED, JobStatus.DENIED)


def test_e2e_concurrent_reads_and_writes_consistent() -> None:
    store = JobStore(capacity=8)
    reg = _registry(VERIFIED)
    n = 12
    barrier = threading.Barrier(n + 4 + 1)
    stop = threading.Event()
    sizes: list[int] = []
    sizes_lock = threading.Lock()

    def writer(k: int) -> None:
        barrier.wait(timeout=30)
        create_job("pdf-document", discovery_registry=reg, store=store, job_id=f"00000000-0000-4000-8000-{k:012x}")

    def reader() -> None:
        barrier.wait(timeout=30)
        while not stop.is_set():
            with sizes_lock:
                sizes.append(len(store))
            time.sleep(0)

    writers = [threading.Thread(target=writer, args=(i,)) for i in range(1, n + 1)]
    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in writers + readers:
        t.start()
    barrier.wait(timeout=30)
    for t in writers:
        t.join(timeout=30)
    stop.set()
    for t in readers:
        t.join(timeout=30)
    assert len(store) <= 8
    assert all(0 <= s <= 8 for s in sizes)


def test_e2e_api_forbidden_fields_never_leak() -> None:
    store = JobStore()
    reg = _registry(VERIFIED)
    c = _client(registry=reg, store=store)
    r = c.post("/api/jobs", json={})
    assert r.status_code == 200
    assert not _forbidden_keys(r.json())
    jid = r.json()["job_id"]
    assert not _forbidden_keys(c.get(f"/api/jobs/{jid}").json())
    assert not _forbidden_keys(c.get("/api/jobs").json())
    c2 = _client(registry=_registry(UNKNOWN), store=JobStore())
    r2 = c2.post("/api/jobs", json={})
    assert r2.status_code == 200
    assert not _forbidden_keys(r2.json())
    jid2 = r2.json()["job_id"]
    assert not _forbidden_keys(c2.get(f"/api/jobs/{jid2}").json())


def test_e2e_get_endpoints_do_not_mutate_or_trigger_discovery() -> None:
    store = JobStore()
    reg = _registry(VERIFIED)
    c = _client(registry=reg, store=store)
    before = len(store)
    assert c.get("/api/jobs").status_code == 200
    assert len(store) == before
    assert c.get("/api/jobs/00000000-0000-4000-8000-000000000000").status_code == 404
    assert len(store) == before
    assert c.get("/api/jobs/not-a-uuid").status_code == 404
    assert len(store) == before


def test_e2e_existing_endpoints_still_functional() -> None:
    c = _client()
    assert c.get("/health").status_code == 200
    assert c.get("/health").json() == {"status": "ok", "mode": "simulated"}
    assert c.get("/api/targets").status_code == 200
    assert c.get("/api/engines").status_code == 200


def test_e2e_jobs_route_has_no_process_execution() -> None:
    tree = ast.parse(JOBS_ROUTE_MODULE.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not (imported & {"subprocess", "os", "shlex", "pty"})
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for bad in ("system", "popen", "Popen", "exec", "eval", "spawnv", "check_output", "subprocess", "run"):
        assert bad not in called


def test_e2e_store_defaults_and_bounds() -> None:
    assert DEFAULT_JOB_STORE_CAPACITY == 100
    s = JobStore()
    assert s.capacity == 100
    assert len(s) == 0
    default_job_store().reset()
    assert len(default_job_store()) == 0


def test_e2e_hashcat_verified_can_be_used() -> None:
    reg = _registry(VERIFIED, engine=HASHCAT)
    job = create_job("pdf-document", discovery_registry=reg)
    assert job.status is JobStatus.SIMULATED
    assert job.target == "pdf-document"
    assert job.preview is not None


def test_e2e_denied_jobs_preserved_through_api() -> None:
    store = JobStore()
    reg = _registry(UNKNOWN)
    c = _client(registry=reg, store=store)
    r = c.post("/api/jobs", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "denied"
    assert body["failure_reason"] == "capability_not_verified"
    jid = body["job_id"]
    got = c.get(f"/api/jobs/{jid}")
    assert got.status_code == 200
    assert got.json()["status"] == "denied"
    assert not _forbidden_keys(got.json())
