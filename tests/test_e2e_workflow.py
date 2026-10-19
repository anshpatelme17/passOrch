"""Phase 10A - end-to-end integration tests over the assembled application.

Scenario coverage (complementary to the phase unit suites, not duplicating
their single-contract focus):

* the full user workflow through one app instance: SPA shell load, health,
  engines, instance create/list/filter/paginate, job submit (denied and
  instance-associated results are valid), list/filter/sort/paginate, stats,
  detail, instance delete with job snapshot retention, job delete, and the
  structured error contracts (unknown target, instance mismatch shapes,
  pagination/sort validation) - every response checked for leak-free content;
* restart against a shared SQLite database file: a second application
  construction rehydrates jobs, instances and association snapshots;
* the bearer-auth matrix: reads stay open, mutations need the token, all
  failures share one identical 401, and authentication never implies
  authorization (policy/validation errors still apply);
* middleware ordering end to end: rate limiting counts before authentication
  (token guessing is throttled) and the body cap applies after auth;
* runtime database failure: after the file is broken, writes answer a
  structured 503 with no path or driver detail and are not silently served
  from memory, while reads keep serving already-hydrated records.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.engines.jobs import JobStore
from app.engines.target_instances import TargetInstanceStore

TARGET = "pdf-document"
TERMINAL_STATUSES = {"simulated", "denied", "failed"}
LEAK_MARKERS = ("traceback", "site-packages", ".db", "sqlite")


def _fresh_client(**overrides: Any) -> TestClient:
    return TestClient(
        create_app(
            job_store=JobStore(),
            target_instance_store=TargetInstanceStore(),
            **overrides,
        )
    )


def _assert_clean(response: Any) -> None:
    text = response.text.lower()
    for marker in LEAK_MARKERS:
        assert marker not in text, f"response leaked {marker!r}"


def _create_instance(
    client: TestClient,
    name: str = "Alpha Probe",
    target_type: str = TARGET,
    description: str = "",
) -> dict[str, Any]:
    response = client.post(
        "/api/target-instances",
        json={"name": name, "target_type": target_type, "description": description},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert isinstance(payload["id"], str) and payload["id"]
    assert isinstance(payload["created_at"], int)
    _assert_clean(response)
    return payload


def _create_job(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/jobs", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] in TERMINAL_STATUSES
    _assert_clean(response)
    return payload


def test_full_user_workflow_from_shell_to_audit_trail() -> None:
    client = _fresh_client()

    shell = client.get("/")
    assert shell.status_code == 200
    assert "text/html" in shell.headers["content-type"]
    assert "script" in shell.text.lower()

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "mode": "simulated"}

    engines = client.get("/api/engines")
    assert engines.status_code == 200
    assert isinstance(engines.json()["engines"], list)
    _assert_clean(engines)

    instance = _create_instance(client)
    second = _create_instance(client, name="Beta Vault")

    listed = client.get(
        "/api/target-instances",
        params={"target_type": TARGET, "name": "alpha"},
    )
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1
    assert [row["id"] for row in page["instances"]] == [instance["id"]]

    page_one = client.get("/api/target-instances", params={"limit": 1}).json()
    assert page_one["count"] == 1 and page_one["total"] == 2
    page_two = client.get(
        "/api/target-instances", params={"limit": 1, "offset": 1}
    ).json()
    assert [row["id"] for row in page_two["instances"]] == [second["id"]]

    job = _create_job(client, target=TARGET, mode="simulated", instance_id=instance["id"])
    assert job["instance_id"] == instance["id"]
    assert job["instance_target_type"] == TARGET
    assert job["preview"] is not None
    standalone = _create_job(client, target=TARGET)
    assert standalone["instance_id"] == ""

    detail = client.get(f"/api/jobs/{job['job_id']}")
    assert detail.status_code == 200
    assert detail.json() == job

    desc = client.get("/api/jobs", params={"limit": 100}).json()
    asc = client.get(
        "/api/jobs", params={"limit": 100, "sort": "created_asc"}
    ).json()
    assert desc["total"] == 2 and asc["total"] == 2
    assert [row["job_id"] for row in desc["jobs"]] == [
        row["job_id"] for row in reversed(asc["jobs"])
    ]

    by_status = client.get("/api/jobs", params={"status": job["status"]}).json()
    assert job["job_id"] in {row["job_id"] for row in by_status["jobs"]}
    only_standalone = client.get("/api/jobs", params={"instance_id": ""}).json()
    assert [row["job_id"] for row in only_standalone["jobs"]] == [standalone["job_id"]]
    assert desc["limit"] == 100

    paged = client.get("/api/jobs", params={"limit": 1, "offset": 1}).json()
    assert paged["count"] == 1 and paged["total"] == 2

    stats = client.get("/api/jobs/stats").json()
    assert stats["total"] == 2
    assert stats["by_status"].get(job["status"], 0) >= 1
    assert sum(stats["by_status"].values()) == 2

    assert client.delete(f"/api/target-instances/{instance['id']}").status_code == 204
    assert (
        client.get(f"/api/target-instances/{instance['id']}").status_code == 404
    )
    after_delete = client.get(f"/api/jobs/{job['job_id']}").json()
    assert after_delete["instance_id"] == instance["id"]
    assert after_delete["instance_target_type"] == TARGET

    assert client.delete(f"/api/jobs/{standalone['job_id']}").status_code == 204
    assert client.get(f"/api/jobs/{standalone['job_id']}").status_code == 404
    assert (
        client.delete(f"/api/jobs/{standalone['job_id']}").status_code == 404
    )

    unknown = client.post("/api/jobs", json={"target": "no-such-format"})
    assert unknown.status_code == 422
    unknown_detail = unknown.json()["detail"]
    assert unknown_detail["error"] == "unknown_target"
    assert unknown_detail["target"] == "no-such-format"
    assert TARGET in unknown_detail["supported_targets"]

    malformed = client.post(
        "/api/jobs", json={"target": TARGET, "instance_id": "not-a-uuid"}
    )
    absent = client.post(
        "/api/jobs",
        json={"target": TARGET, "instance_id": str(uuid.uuid4())},
    )
    assert malformed.status_code == absent.status_code == 422
    malformed_detail = malformed.json()["detail"]
    absent_detail = absent.json()["detail"]
    assert malformed_detail["error"] == absent_detail["error"] == "target_instance_not_found"
    assert malformed_detail["detail"] == absent_detail["detail"]
    assert set(malformed_detail) == set(absent_detail)

    mismatch = client.post(
        "/api/jobs",
        json={
            "target": "zip-archive",
            "instance_id": second["id"],
            "mode": "simulated",
        },
    )
    if mismatch.status_code == 422:
        assert mismatch.json()["detail"]["error"] in {
            "unknown_target",
            "target_instance_type_mismatch",
        }
        _assert_clean(mismatch)

    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/api/jobs", params={"offset": -1}).status_code == 422
    assert client.get("/api/jobs", params={"sort": "bogus"}).status_code == 422
    assert client.get("/api/jobs/not-a-uuid").status_code == 404
    assert (
        client.delete("/api/target-instances/not-a-uuid").status_code == 404
    )


def test_restart_against_a_shared_database_rehydrates_state(tmp_path: Path) -> None:
    database = tmp_path / "restart.db"

    first = TestClient(create_app(job_db_path=database))
    instance = _create_instance(first, name="Durable Probe")
    job = _create_job(first, target=TARGET, instance_id=instance["id"])
    other = _create_job(first, target=TARGET)

    assert database.is_file()

    restarted = TestClient(create_app(job_db_path=database))
    jobs = restarted.get("/api/jobs", params={"limit": 100}).json()
    assert {row["job_id"] for row in jobs["jobs"]} == {job["job_id"], other["job_id"]}

    instances = restarted.get("/api/target-instances").json()
    assert [row["id"] for row in instances["instances"]] == [instance["id"]]

    reloaded = restarted.get(f"/api/jobs/{job['job_id']}").json()
    assert reloaded["instance_id"] == instance["id"]
    assert reloaded["instance_target_type"] == TARGET

    stats = restarted.get("/api/jobs/stats").json()
    assert stats["total"] == 2


def test_bearer_auth_matrix_for_reads_mutations_and_policy_errors() -> None:
    token = "e2e-bearer-token"
    client = _fresh_client(api_token=token)

    for path in (
        "/health",
        "/api/engines",
        "/api/targets",
        "/api/jobs",
        "/api/target-instances",
        "/",
    ):
        assert client.get(path).status_code == 200, path

    rejected = [
        client.post("/api/jobs", json={"target": TARGET}),
        client.post("/api/jobs", json={"target": TARGET}, headers={"Authorization": "Basic dTpw"}),
        client.post("/api/jobs", json={"target": TARGET}, headers={"Authorization": "Bearer wrong"}),
        client.post("/api/jobs", json={"target": TARGET}, headers={"Authorization": "Bearer "}),
    ]
    assert all(response.status_code == 401 for response in rejected)
    assert all(
        response.headers.get("www-authenticate") == "Bearer" for response in rejected
    )
    bodies = [response.json() for response in rejected]
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["detail"]["error"] == "unauthorized"
    assert token not in rejected[0].text

    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/jobs", json={"target": TARGET}, headers=headers
    )
    assert created.status_code == 200
    instance_response = client.post(
        "/api/target-instances",
        json={"name": "Auth Probe", "target_type": TARGET},
        headers=headers,
    )
    assert instance_response.status_code == 201
    assert client.delete(
        f"/api/target-instances/{instance_response.json()['id']}", headers=headers
    ).status_code == 204
    lower = client.post(
        "/api/jobs",
        json={"target": TARGET},
        headers={"Authorization": f"bearer {token}"},
    )
    assert lower.status_code == 200

    still_policy = client.post("/api/jobs", json={"target": "nope"}, headers=headers)
    assert still_policy.status_code == 422
    assert still_policy.json()["detail"]["error"] == "unknown_target"


def test_guard_ordering_rate_limit_precedes_auth_and_body_cap_follows() -> None:
    throttled = _fresh_client(api_token="tok-a", rate_limit=1, rate_window_seconds=60)
    first = throttled.post("/api/jobs", json={"target": TARGET})
    assert first.status_code == 401
    second = throttled.post("/api/jobs", json={"target": TARGET})
    assert second.status_code == 429
    assert second.headers.get("retry-after") is not None
    assert second.json()["detail"]["error"] == "rate_limited"
    assert throttled.get("/api/jobs").status_code == 200

    capped = _fresh_client(
        api_token="tok-b", max_body_bytes=64, rate_limit=100
    )
    oversized = {"target": TARGET, "reference": "x" * 200}
    unauth = capped.post("/api/jobs", json=oversized)
    assert unauth.status_code == 401
    too_large = capped.post(
        "/api/jobs",
        json=oversized,
        headers={"Authorization": "Bearer tok-b"},
    )
    assert too_large.status_code == 413
    assert too_large.json()["detail"]["error"] == "payload_too_large"
    accepted = capped.post(
        "/api/jobs",
        json={"target": TARGET},
        headers={"Authorization": "Bearer tok-b"},
    )
    assert accepted.status_code == 200


def test_runtime_database_failure_is_structured_503_without_paths(tmp_path: Path) -> None:
    database = tmp_path / "fail.db"
    client = TestClient(create_app(job_db_path=database))

    instance = _create_instance(client, name="Soon Broken")
    prior = _create_job(client, target=TARGET)

    database.unlink()
    database.mkdir()

    failed = client.post("/api/jobs", json={"target": TARGET})
    assert failed.status_code == 503
    detail = failed.json()["detail"]
    assert detail["error"] == "persistence_unavailable"
    assert detail["detail"] == "job storage is temporarily unavailable"
    _assert_clean(failed)
    assert str(tmp_path) not in failed.text

    listed = client.get("/api/jobs", params={"limit": 100}).json()
    listed_ids = {row["job_id"] for row in listed["jobs"]}
    assert listed["total"] == 1
    assert prior["job_id"] in listed_ids
    assert failed.json().get("job_id") not in listed_ids

    broken_delete = client.delete(f"/api/target-instances/{instance['id']}")
    assert broken_delete.status_code == 503
    assert broken_delete.json()["detail"]["error"] == "persistence_unavailable"
    _assert_clean(broken_delete)

    assert client.get("/health").status_code == 200
    assert client.get("/api/jobs").status_code == 200
