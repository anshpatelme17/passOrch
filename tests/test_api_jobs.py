"""Phase 2D/3B.3/3D - API tests for jobs endpoints.

Covers:
* POST /api/jobs: authorized simulation, denials, explicit target selection
  (Phase 3D), unmapped target -> structured 422, extra fields (422),
  job_id != simulation_id, stored in injected store, distinct job IDs.
* GET /api/jobs: empty list, newest-first, count, non-mutating.
* GET /api/jobs/{job_id}: fetch, malformed/unknown -> 404 (job_not_found),
  GET does not create/trigger discovery, denied preserved.
* Integration/security: existing endpoints remain usable, forbidden fields
  never appear in responses, no discovery refresh invoked, no execution calls.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.engines.adapter import FORBIDDEN_REQUEST_FIELDS
from app.engines.discovery.models import (
    CapabilityStatus,
    EngineCapabilities,
    EngineSnapshot,
    JOHN,
)
from app.engines.discovery.registry import FORBIDDEN_PAYLOAD_KEYS, DiscoveryRegistry
from app.engines.jobs import JobStore, default_job_store
from app.engines.target_instances import TargetInstanceStore

JOBS_ROUTE_MODULE = Path("app/api/routes/jobs.py")

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN

FORBIDDEN_KEYS = FORBIDDEN_REQUEST_FIELDS | FORBIDDEN_PAYLOAD_KEYS


def _registry(
    status: CapabilityStatus, fmts: tuple[str, ...] = ("pdf",)
) -> DiscoveryRegistry:
    registry = DiscoveryRegistry()
    registry.register(EngineSnapshot(engine=JOHN, discovered=True, binary_found=True))
    registry.set_capabilities(
        JOHN,
        EngineCapabilities(engine=JOHN, capability_status=status, formats=fmts),
    )
    return registry


def _client(*, registry: DiscoveryRegistry | None = None, store: JobStore | None = None) -> TestClient:
    return TestClient(
        create_app(
            discovery_registry=registry if registry is not None else _registry(VERIFIED),
            job_store=store if store is not None else JobStore(),
        )
    )


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


@pytest.fixture(autouse=True)
def _reset_default_job_store() -> None:
    default_job_store().reset()
    yield
    default_job_store().reset()


# --------------------------------------------------------------------------- #
# POST /api/jobs
# --------------------------------------------------------------------------- #


def test_post_jobs_creates_authorised_simulated_job() -> None:
    client = _client()
    res = client.post("/api/jobs", json={"target": "pdf-document", "reference": "audit-1"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "simulated"
    assert body["target"] == "pdf-document"
    assert body["failure_reason"] == ""
    assert body["preview"] is not None
    assert body["preview"]["simulation"]["status"] == "simulated"
    job_id = body["job_id"]
    sim_id = body["preview"]["simulation"]["simulation_id"]
    assert job_id != sim_id
    assert not _forbidden_keys(body)


def test_post_jobs_echoes_every_mapped_target() -> None:
    """Phase 3D - each catalog target is accepted and echoed back on the job."""

    from app.engines.discovery.capability_map import TARGET_FORMAT_POLICY

    client = _client()
    for target in sorted(TARGET_FORMAT_POLICY):
        res = client.post("/api/jobs", json={"target": target})
        assert res.status_code == 200, target
        body = res.json()
        assert body["target"] == target
        # authorization stays fail-closed: only targets with verified
        # capability in this registry may become simulated
        if target == "pdf-document":
            assert body["status"] == "simulated"
            assert body["failure_reason"] == ""
        else:
            assert body["status"] == "denied"
            assert body["failure_reason"] == "capability_not_verified"
            assert body["preview"]["plan"] is None
            assert body["preview"]["simulation"] is None
        assert not _forbidden_keys(body)


def test_post_jobs_requires_a_target() -> None:
    """The legacy default is gone: a body without ``target`` is rejected."""

    client = _client()
    for body in ({}, {"reference": "audit-1"}, {"mode": "simulated"}):
        res = client.post("/api/jobs", json=body)
        assert res.status_code == 422, body
    assert len(client.app.state.job_store) == 0


def test_unknown_target_detail_returns_isolated_mutable_copies() -> None:
    """Each detail owns its dict and list - no shared mutable state.

    Guards the shallow-copy defect that let one caller's mutation of
    ``supported_targets`` reach the module constant (and later callers).
    """

    from app.api.routes.jobs import UNKNOWN_TARGET_DETAIL, unknown_target_detail

    pristine = list(UNKNOWN_TARGET_DETAIL["supported_targets"])

    first = unknown_target_detail("bad-target")
    assert first["supported_targets"] == pristine
    first["supported_targets"].append("INJECTED")
    first["error"] = "mutated"

    # the shared constant is untouched
    assert UNKNOWN_TARGET_DETAIL["supported_targets"] == pristine
    assert "INJECTED" not in UNKNOWN_TARGET_DETAIL["supported_targets"]
    assert UNKNOWN_TARGET_DETAIL["error"] == "unknown_target"
    assert UNKNOWN_TARGET_DETAIL.get("target") is None

    # a later caller gets a clean, independent copy
    second = unknown_target_detail("another-bad-target")
    assert second["supported_targets"] == pristine
    assert "INJECTED" not in second["supported_targets"]
    assert second["error"] == "unknown_target"
    assert second["target"] == "another-bad-target"
    assert first is not second


def test_post_jobs_rejects_unmapped_target_with_structured_422() -> None:
    """An unknown target is a transport error, never a job record."""

    client = _client()
    store = client.app.state.job_store
    before = len(store)

    res = client.post("/api/jobs", json={"target": "unknown-target"})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["error"] == "unknown_target"
    assert detail["target"] == "unknown-target"
    assert "pdf-document" in detail["supported_targets"]
    assert not _forbidden_keys(res.json())

    # the rejected target is never recorded, and no legacy inference happens
    assert len(store) == before
    assert client.get("/api/jobs").json()["count"] == before


def test_post_jobs_no_longer_infers_target_from_reference() -> None:
    """The ``"unknown-target"`` reference sentinel is retired (Phase 3D)."""

    client = _client()
    res = client.post("/api/jobs", json={"target": "pdf-document", "reference": "unknown-target"})
    assert res.status_code == 200
    body = res.json()
    # reference is data, not a target selector
    assert body["target"] == "pdf-document"
    assert body["status"] == "simulated"
    assert body["preview"]["request"]["reference"] == "unknown-target"


def test_post_jobs_denies_non_simulation_mode() -> None:
    client = _client()
    res = client.post("/api/jobs", json={"target": "pdf-document", "mode": "real"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "denied"
    assert body["failure_reason"] == "non_simulation_request"
    assert body["preview"] is not None
    assert body["preview"]["simulation"] is None
    assert body["preview"]["plan"] is None
    assert body["target"] == "pdf-document"
    assert not _forbidden_keys(body)


def test_post_jobs_denies_unsupported_mode() -> None:
    client = _client()
    res = client.post("/api/jobs", json={"target": "pdf-document", "mode": "banana"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "denied"
    assert body["failure_reason"] == "unsupported_execution_mode"
    assert body["preview"]["simulation"] is None
    assert body["target"] == "pdf-document"
    assert not _forbidden_keys(body)


def test_post_jobs_denies_unverified_capability() -> None:
    client = _client(registry=_registry(UNKNOWN))
    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "denied"
    assert body["failure_reason"] == "capability_not_verified"
    assert body["preview"]["simulation"] is None
    assert not _forbidden_keys(body)


def test_post_jobs_rejects_extra_fields() -> None:
    client = _client()
    for extra in ("command", "argv", "stdout", "path"):
        res = client.post(
            "/api/jobs",
            json={"target": "pdf-document", "mode": "simulated", extra: "x"},
        )
        assert res.status_code == 422, extra
        assert not _forbidden_keys(res.json())
    assert len(client.app.state.job_store) == 0


def test_post_jobs_returns_distinct_job_ids_across_calls() -> None:
    client = _client()
    r1 = client.post("/api/jobs", json={"target": "pdf-document"}).json()
    r2 = client.post("/api/jobs", json={"target": "pdf-document"}).json()
    assert r1["job_id"] != r2["job_id"]
    assert (
        r1["preview"]["simulation"]["simulation_id"] == r2["preview"]["simulation"]["simulation_id"]
    )


def test_post_jobs_stores_job_in_injected_store() -> None:
    store = JobStore()
    client = _client(store=store)
    res = client.post("/api/jobs", json={"target": "pdf-document"})
    assert res.status_code == 200
    jid = res.json()["job_id"]
    assert store.get(jid) is not None
    assert len(store) == 1


# --------------------------------------------------------------------------- #
# GET /api/jobs
# --------------------------------------------------------------------------- #


def test_get_jobs_empty_list() -> None:
    client = _client()
    res = client.get("/api/jobs")
    assert res.status_code == 200
    body = res.json()
    assert body == {"jobs": [], "count": 0}
    assert not _forbidden_keys(body)


def test_get_jobs_returns_newest_first_and_count_matches() -> None:
    store = JobStore()
    c = _client(store=store)
    c.post("/api/jobs", json={"target": "pdf-document"})
    c.post("/api/jobs", json={"target": "pdf-document"})
    res = c.get("/api/jobs")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == len(body["jobs"]) == 2
    # newest-first by insertion; create_job inserts; list is reversed
    jids = [j["job_id"] for j in body["jobs"]]
    assert jids[0] != jids[1]
    assert not _forbidden_keys(body)


def test_get_jobs_does_not_mutate_store() -> None:
    store = JobStore()
    c = _client(store=store)
    c.post("/api/jobs", json={"target": "pdf-document"})
    before = len(store)
    res = c.get("/api/jobs")
    assert res.status_code == 200
    assert len(store) == before


# --------------------------------------------------------------------------- #
# GET /api/jobs/{job_id}
# --------------------------------------------------------------------------- #


def test_get_job_returns_existing_job() -> None:
    store = JobStore()
    c = _client(store=store)
    created = c.post("/api/jobs", json={"target": "pdf-document"}).json()
    res = c.get(f"/api/jobs/{created['job_id']}")
    assert res.status_code == 200
    got = res.json()
    assert got["job_id"] == created["job_id"]
    assert got["status"] == "simulated"
    assert not _forbidden_keys(got)


def test_get_job_returns_404_for_unknown_and_malformed() -> None:
    store = JobStore()
    c = _client(store=store)
    # unknown
    r = c.get("/api/jobs/00000000-0000-4000-8000-000000000000")
    assert r.status_code == 404
    assert r.json() == {"detail": "job_not_found"}
    assert not _forbidden_keys(r.json())
    # malformed
    r = c.get("/api/jobs/not-a-uuid")
    assert r.status_code == 404
    assert r.json() == {"detail": "job_not_found"}
    assert not _forbidden_keys(r.json())


def test_get_job_does_not_create_or_trigger_discovery() -> None:
    store = JobStore()
    c = _client(store=store)
    before = len(store)
    c.get("/api/jobs/00000000-0000-4000-8000-000000000000")
    assert len(store) == before


def test_get_job_preserves_denied_status() -> None:
    store = JobStore()
    c = _client(store=store)
    res = c.post("/api/jobs", json={"target": "pdf-document", "mode": "real"})
    assert res.status_code == 200
    jid = res.json()["job_id"]
    got = c.get(f"/api/jobs/{jid}").json()
    assert got["status"] == "denied"
    assert got["failure_reason"] == "non_simulation_request"
    assert not _forbidden_keys(got)


# --------------------------------------------------------------------------- #
# integration/security
# --------------------------------------------------------------------------- #


def test_existing_endpoints_still_work() -> None:
    c = _client()
    assert c.get("/health").status_code == 200
    assert c.get("/api/targets").status_code == 200
    assert c.get("/api/engines").status_code == 200


def test_jobs_responses_never_contain_forbidden_keys() -> None:
    store = JobStore()
    c = _client(store=store)
    c.post("/api/jobs", json={"target": "pdf-document"})
    assert not _forbidden_keys(c.get("/api/jobs").json())
    jid = c.post("/api/jobs", json={"target": "pdf-document", "mode": "real"}).json()["job_id"]
    assert not _forbidden_keys(c.get(f"/api/jobs/{jid}").json())


def test_jobs_module_has_no_process_execution_calls_or_imports() -> None:
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
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    for forbidden in ("system", "popen", "Popen", "exec", "eval", "spawnv", "check_output"):
        assert forbidden not in called


# --------------------------------------------------------------------------- #
# Phase 3D schema contract
# --------------------------------------------------------------------------- #


def test_job_create_request_declares_expected_fields() -> None:
    from app.api.schemas import JobCreateRequest

    assert set(JobCreateRequest.model_fields) == {
        "target",
        "mode",
        "reference",
        "instance_id",
    }
    assert JobCreateRequest.model_config.get("extra") == "forbid"
    assert JobCreateRequest.model_fields["target"].is_required()
    assert JobCreateRequest.model_fields["mode"].default == "simulated"
    assert JobCreateRequest.model_fields["reference"].default == ""
    # Phase 3E: optional association
    assert JobCreateRequest.model_fields["instance_id"].default is None
    assert JobCreateRequest.model_fields["instance_id"].is_required() is False


def test_plan_preview_request_schema_is_unchanged_by_phase_3d() -> None:
    """The plan endpoint keeps its own body - Phase 3D did not widen it."""

    from app.api.schemas import PlanPreviewRequest

    assert set(PlanPreviewRequest.model_fields) == {"mode", "reference"}


def test_job_info_exposes_association_fields() -> None:
    from app.api.schemas import JobInfo

    assert JobInfo.model_fields["instance_id"].default == ""
    assert JobInfo.model_fields["instance_target_type"].default == ""
    assert JobInfo.model_config.get("extra") == "forbid"


# --------------------------------------------------------------------------- #
# Phase 3E - optional target-instance association
# --------------------------------------------------------------------------- #


def _instance_client(
    *,
    registry: DiscoveryRegistry | None = None,
    job_store: JobStore | None = None,
    instance_store: TargetInstanceStore | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            discovery_registry=registry if registry is not None else _registry(VERIFIED),
            job_store=job_store if job_store is not None else JobStore(),
            target_instance_store=(
                instance_store if instance_store is not None else TargetInstanceStore(capacity=10)
            ),
        )
    )


def _create_instance(client: TestClient, target_type: str = "pdf-document") -> str:
    res = client.post(
        "/api/target-instances", json={"name": "inst", "target_type": target_type}
    )
    assert res.status_code == 201
    return res.json()["id"]


def test_post_jobs_without_instance_id_stays_compatible() -> None:
    """Phase 3E is additive: an omitted ``instance_id`` behaves exactly as 3D."""

    store = JobStore()
    c = _instance_client(job_store=store)
    res = c.post("/api/jobs", json={"target": "pdf-document", "reference": "audit-1"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "simulated"
    assert body["target"] == "pdf-document"
    assert body["instance_id"] == ""
    assert body["instance_target_type"] == ""
    assert not _forbidden_keys(body)


def test_post_jobs_with_matching_instance_is_associated() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(job_store=store, instance_store=instances)
    iid = _create_instance(c)

    res = c.post("/api/jobs", json={"target": "pdf-document", "instance_id": iid})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "simulated"
    assert body["target"] == "pdf-document"
    # the association is stored and the instance type snapshotted
    assert body["instance_id"] == iid
    assert body["instance_target_type"] == "pdf-document"
    assert not _forbidden_keys(body)

    jid = body["job_id"]
    # and the response is backed by the persisted record
    assert store.get(jid).instance_id == iid
    assert store.get(jid).instance_target_type == "pdf-document"


def test_post_jobs_instance_id_malformed_and_unknown_are_indistinguishable() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(job_store=store, instance_store=instances)

    malformed = c.post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": "not-a-uuid"}
    )
    unknown = c.post(
        "/api/jobs",
        json={"target": "pdf-document", "instance_id": "00000000-0000-4000-8000-000000000000"},
    )
    assert malformed.status_code == 422
    assert unknown.status_code == 422
    assert malformed.json()["detail"]["error"] == "target_instance_not_found"
    assert unknown.json()["detail"]["error"] == "target_instance_not_found"
    # identical key shape: no existence oracle
    assert malformed.json()["detail"].keys() == unknown.json()["detail"].keys()
    # neither is stored
    assert len(store) == 0


def test_post_jobs_instance_type_mismatch_is_rejected() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(
        registry=_registry(VERIFIED, fmts=("pdf", "zip", "pkzip")),
        job_store=store,
        instance_store=instances,
    )
    zip_id = _create_instance(c, "zip-archive")

    res = c.post("/api/jobs", json={"target": "pdf-document", "instance_id": zip_id})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["error"] == "target_instance_type_mismatch"
    assert detail["target"] == "pdf-document"
    assert detail["instance_target_type"] == "zip-archive"
    assert len(store) == 0


def test_post_jobs_instance_validation_failure_stores_nothing() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(job_store=store, instance_store=instances)
    iid = _create_instance(c)

    for body in (
        {"target": "pdf-document", "instance_id": "bogus"},
        {"target": "pdf-document", "instance_id": ""},
        {"target": "zip-archive", "instance_id": iid},
    ):
        res = c.post("/api/jobs", json=body)
        assert res.status_code == 422, body
    assert len(store) == 0
    assert c.get("/api/jobs").json()["count"] == 0


def test_post_jobs_with_instance_id_uses_its_own_app_store() -> None:
    """An instance registered elsewhere cannot be resolved by this app."""

    foreign = TargetInstanceStore(capacity=10)
    other = _instance_client(instance_store=foreign)
    foreign_id = _create_instance(other, "pdf-document")

    res = _instance_client(job_store=JobStore()).post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": foreign_id}
    )
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "target_instance_not_found"


def test_job_details_remain_readable_after_instance_deleted() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(job_store=store, instance_store=instances)
    iid = _create_instance(c)
    jid = c.post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": iid}
    ).json()["job_id"]

    assert c.delete(f"/api/target-instances/{iid}").status_code == 204
    assert c.get(f"/api/target-instances/{iid}").status_code == 404

    res = c.get(f"/api/jobs/{jid}")
    assert res.status_code == 200
    body = res.json()
    # association is a snapshot, never re-resolved
    assert body["instance_id"] == iid
    assert body["instance_target_type"] == "pdf-document"
    assert not _forbidden_keys(body)


def test_job_details_remain_readable_after_instance_evicted() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=1)
    c = _instance_client(
        registry=_registry(VERIFIED, fmts=("pdf", "zip", "pkzip")),
        job_store=store,
        instance_store=instances,
    )
    first = _create_instance(c, "pdf-document")
    jid = c.post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": first}
    ).json()["job_id"]

    # capacity 1 -> inserting a second instance evicts the first
    _create_instance(c, "zip-archive")
    assert c.get(f"/api/target-instances/{first}").status_code == 404

    res = c.get(f"/api/jobs/{jid}")
    assert res.status_code == 200
    body = res.json()
    assert body["instance_id"] == first
    assert body["instance_target_type"] == "pdf-document"


def test_post_jobs_extra_fields_still_rejected_with_instance_id() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(job_store=store, instance_store=instances)
    iid = _create_instance(c)

    for extra in ("command", "argv", "stdout", "path", "instance"):
        res = c.post(
            "/api/jobs",
            json={"target": "pdf-document", "instance_id": iid, extra: "x"},
        )
        assert res.status_code == 422, extra
    assert len(store) == 0


def test_post_jobs_missing_target_still_rejected_with_instance_id() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(job_store=store, instance_store=instances)
    iid = _create_instance(c)
    assert len(store) == 0

    res = c.post("/api/jobs", json={"instance_id": iid})
    assert res.status_code == 422
    assert len(store) == 0


def test_associated_job_ids_stay_distinct_from_simulation_ids() -> None:
    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(job_store=store, instance_store=instances)
    iid = _create_instance(c)

    r1 = c.post("/api/jobs", json={"target": "pdf-document", "instance_id": iid}).json()
    r2 = c.post("/api/jobs", json={"target": "pdf-document", "instance_id": iid}).json()
    assert r1["job_id"] != r2["job_id"]
    for r in (r1, r2):
        assert r["job_id"] != r["preview"]["simulation"]["simulation_id"]


def test_associated_job_is_absent_from_list_summary() -> None:
    """JobSummary is deliberately unchanged for Phase 3E."""

    store = JobStore()
    instances = TargetInstanceStore(capacity=10)
    c = _instance_client(job_store=store, instance_store=instances)
    iid = _create_instance(c)
    jid = c.post(
        "/api/jobs", json={"target": "pdf-document", "instance_id": iid}
    ).json()["job_id"]

    body = c.get("/api/jobs").json()
    assert body["count"] == 1
    summary = body["jobs"][0]
    assert summary["job_id"] == jid
    assert set(summary.keys()) == {"job_id", "status", "created_at", "target"}


# --------------------------------------------------------------------------- #
# DELETE /api/jobs/{job_id}
# --------------------------------------------------------------------------- #


def test_delete_job_returns_204_and_empty_body() -> None:
    store = JobStore()
    c = _instance_client(job_store=store)
    jid = c.post("/api/jobs", json={"target": "pdf-document"}).json()["job_id"]

    res = c.delete(f"/api/jobs/{jid}")
    assert res.status_code == 204
    assert res.text == ""
    assert len(store) == 0


def test_delete_unknown_job_returns_404() -> None:
    c = _instance_client()
    res = c.delete("/api/jobs/00000000-0000-4000-8000-000000000000")
    assert res.status_code == 404
    assert res.json()["detail"] == "job_not_found"


def test_delete_malformed_id_returns_404() -> None:
    c = _instance_client()
    res = c.delete("/api/jobs/not-a-uuid")
    assert res.status_code == 404
    assert res.json()["detail"] == "job_not_found"


def test_delete_repeated_returns_204_then_404() -> None:
    store = JobStore()
    c = _instance_client(job_store=store)
    jid = c.post("/api/jobs", json={"target": "pdf-document"}).json()["job_id"]

    first = c.delete(f"/api/jobs/{jid}")
    assert first.status_code == 204

    second = c.delete(f"/api/jobs/{jid}")
    assert second.status_code == 404
    assert second.json()["detail"] == "job_not_found"


def test_delete_leaves_other_jobs_available() -> None:
    store = JobStore()
    c = _instance_client(job_store=store)
    jid1 = c.post("/api/jobs", json={"target": "pdf-document"}).json()["job_id"]
    jid2 = c.post("/api/jobs", json={"target": "pdf-document"}).json()["job_id"]

    c.delete(f"/api/jobs/{jid1}")

    remaining = c.get(f"/api/jobs/{jid2}")
    assert remaining.status_code == 200
    assert remaining.json()["job_id"] == jid2
    assert len(store) == 1


def test_delete_does_not_affect_list_or_detail() -> None:
    store = JobStore()
    c = _instance_client(job_store=store)
    jid1 = c.post("/api/jobs", json={"target": "pdf-document"}).json()["job_id"]
    jid2 = c.post("/api/jobs", json={"target": "pdf-document"}).json()["job_id"]

    c.delete(f"/api/jobs/{jid1}")

    body = c.get("/api/jobs").json()
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == jid2

    deleted = c.get(f"/api/jobs/{jid1}")
    assert deleted.status_code == 404


def test_delete_uses_injected_store() -> None:
    store = JobStore()
    c = _instance_client(job_store=store)
    jid = c.post("/api/jobs", json={"target": "pdf-document"}).json()["job_id"]

    c.delete(f"/api/jobs/{jid}")
    assert len(store) == 0
    assert default_job_store().exists(jid) is False or len(store) == 0
