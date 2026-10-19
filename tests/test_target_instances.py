"""Phase 3C tests for target instance management.

Covers store behaviour, validation, API endpoints, determinism through
injection, isolation from existing systems, and security invariants.
"""

from __future__ import annotations

import re
import threading

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.orchestrator.target_instances import (
    create_target_instance,
    resolve_target_instance,
)
from app.engines.discovery.capability_map import TARGET_FORMAT_POLICY
from app.engines.target_instances import (
    DEFAULT_TARGET_INSTANCE_STORE_CAPACITY,
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    DuplicateTargetInstanceError,
    InvalidTargetInstanceError,
    TargetInstanceRecord,
    TargetInstanceStore,
    default_target_instance_store,
    generate_instance_id,
    is_valid_target_type,
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# -- target type validation ------------------------------------------------ #


def test_is_valid_target_type_matches_catalog():
    for known in TARGET_FORMAT_POLICY:
        assert is_valid_target_type(known) is True
    assert not is_valid_target_type(None)
    assert not is_valid_target_type(123)
    assert not is_valid_target_type("")
    assert not is_valid_target_type("unknown")


def test_generate_instance_id_is_canonical_uuid():
    ids = {generate_instance_id() for _ in range(10)}
    assert len(ids) == 10
    for iid in ids:
        assert _UUID_RE.fullmatch(iid)


# -- record immutability ---------------------------------------------------- #


def test_target_instance_record_is_frozen_and_has_slots():
    record = TargetInstanceRecord(
        id="e0a2e9e9-187f-45c9-8f22-5a98fb5c920d",
        name="test",
        target_type="office-document",
        description="desc",
        created_at=123,
    )
    assert record.id == "e0a2e9e9-187f-45c9-8f22-5a98fb5c920d"
    assert record.name == "test"
    assert record.target_type == "office-document"
    assert record.description == "desc"
    assert record.created_at == 123
    assert hasattr(record, "__slots__")
    try:
        record.name = "other"
        raise AssertionError("should be frozen")
    except (AttributeError, TypeError):
        pass


# -- store CRUD -------------------------------------------------------------- #


def test_store_put_get_list_delete_basics():
    store = TargetInstanceStore(capacity=10)
    assert len(store) == 0
    assert store.list() == ()

    record = TargetInstanceRecord(
        id="a9b8c2e4-6d71-4f05-8a60-7e3f9c12d84b",
        name="doc1",
        target_type="office-document",
        description="",
        created_at=100,
    )
    stored = store.put(record)
    assert stored is record
    assert len(store) == 1
    assert store.get(record.id) is record
    assert store.exists(record.id) is True
    assert store.list() == (record,)

    assert store.delete(record.id) is True
    assert len(store) == 0
    assert store.get(record.id) is None
    assert store.exists(record.id) is False
    assert store.delete(record.id) is False


def test_store_rejects_invalid_records():
    store = TargetInstanceStore(capacity=5)

    bad_records = [
        ("not-a-record", None),
        (
            "bad-id",
            TargetInstanceRecord(
                id="bad-id",
                name="n",
                target_type="office-document",
                description="",
                created_at=1,
            ),
        ),
        (
            "empty-name",
            TargetInstanceRecord(
                id="5f3e7a41-9c62-4b08-8d25-2e60f81ac76e",
                name="",
                target_type="office-document",
                description="",
                created_at=1,
            ),
        ),
        (
            "blank-name",
            TargetInstanceRecord(
                id="5f3e7a41-9c62-4b08-8d25-2e60f81ac76f",
                name="   ",
                target_type="office-document",
                description="",
                created_at=1,
            ),
        ),
        (
            "unknown-target-type",
            TargetInstanceRecord(
                id="6c8d2f70-5e4a-4a1b-9c37-0b8e61a7d5c2",
                name="n",
                target_type="unknown-type",
                description="",
                created_at=1,
            ),
        ),
        (
            "bad-description",
            TargetInstanceRecord(
                id="7a92e85c-0f64-4dbd-8c1e-5a7f60e2c43a",
                name="n",
                target_type="pdf-document",
                description=123,
                created_at=1,
            ),
        ),
        (
            "oversized-name",
            TargetInstanceRecord(
                id="c4b7a61d-8e92-4f05-9d3c-2a0e58c7f1ac",
                name="a" * (MAX_NAME_LENGTH + 1),
                target_type="pdf-document",
                description="",
                created_at=1,
            ),
        ),
        (
            "oversized-description",
            TargetInstanceRecord(
                id="d8e5f60a-2c71-4a9b-8e4d-7c0f3a91b5e8",
                name="n",
                target_type="pdf-document",
                description="a" * (MAX_DESCRIPTION_LENGTH + 1),
                created_at=1,
            ),
        ),
        (
            "bad-created-at",
            TargetInstanceRecord(
                id="2b9e7a43-8c61-4f05-9d2a-6e0f8c5a1b70",
                name="n",
                target_type="pdf-document",
                description="",
                created_at="123",
            ),
        ),
        (
            "bool-created-at",
            TargetInstanceRecord(
                id="2b9e7a43-8c61-4f05-9d2a-6e0f8c5a1b71",
                name="n",
                target_type="pdf-document",
                description="",
                created_at=True,
            ),
        ),
        (
            "non-string-target-type",
            TargetInstanceRecord(
                id="2b9e7a43-8c61-4f05-9d2a-6e0f8c5a1b72",
                name="n",
                target_type=None,
                description="",
                created_at=1,
            ),
        ),
    ]

    for label, candidate in bad_records:
        try:
            store.put(candidate)
        except InvalidTargetInstanceError:
            continue
        raise AssertionError(f"expected rejection for {label}")
    assert len(store) == 0


def test_store_accepts_maximum_length_values():
    store = TargetInstanceStore(capacity=5)
    record = TargetInstanceRecord(
        id="7c4f8e92-1a60-4d3b-8e5c-0f26a7d9b3e1",
        name="a" * MAX_NAME_LENGTH,
        target_type="office-document",
        description="d" * MAX_DESCRIPTION_LENGTH,
        created_at=1,
    )
    assert store.put(record) is record


def test_store_rejects_duplicate_ids():
    store = TargetInstanceStore(capacity=10)
    shared_id = "8f62c4a7-0d9e-4b71-8e53-2a5c76f80e1b"
    store.put(
        TargetInstanceRecord(
            id=shared_id,
            name="n1",
            target_type="raw-password-hash",
            description="",
            created_at=1,
        )
    )
    try:
        store.put(
            TargetInstanceRecord(
                id=shared_id,
                name="n2",
                target_type="raw-password-hash",
                description="",
                created_at=2,
            )
        )
        raise AssertionError("expected DuplicateTargetInstanceError")
    except DuplicateTargetInstanceError:
        pass


def test_store_get_and_delete_reject_malformed_ids():
    store = TargetInstanceStore(capacity=5)
    for bad in (None, 123, "not-uuid", "", "  ", "G" * 36):
        assert store.get(bad) is None
        assert store.exists(bad) is False
        assert store.delete(bad) is False
    # unknown but well-formed UUID does not leak a record
    assert store.get("00000000-0000-0000-0000-000000000000") is None


def test_store_bounded_fifo_eviction_is_deterministic():
    store = TargetInstanceStore(capacity=2)
    ids = [
        "2b9e7a43-8c61-4f05-9d2a-6e0f8c5a1b7d",
        "7c4f8e92-1a60-4d3b-8e5c-0f26a7d9b3e1",
        "5a0e2d61-9c7b-4f84-8a3e-7c6f51b9e2d8",
    ]
    for index, iid in enumerate(ids):
        store.put(
            TargetInstanceRecord(
                id=iid,
                name=f"n{index}",
                target_type="office-document",
                description="",
                created_at=index,
            )
        )
    assert len(store) == 2
    assert store.get(ids[0]) is None
    assert store.get(ids[1]) is not None
    assert store.get(ids[2]) is not None


def test_store_delete_inside_capacity_keeps_order():
    store = TargetInstanceStore(capacity=3)
    ids = [
        "2b9e7a43-8c61-4f05-9d2a-6e0f8c5a1b7d",
        "7c4f8e92-1a60-4d3b-8e5c-0f26a7d9b3e1",
        "5a0e2d61-9c7b-4f84-8a3e-7c6f51b9e2d8",
    ]
    records = [
        store.put(
            TargetInstanceRecord(
                id=iid,
                name=f"n{i}",
                target_type="office-document",
                description="",
                created_at=i,
            )
        )
        for i, iid in enumerate(ids)
    ]
    assert store.delete(ids[1]) is True
    assert store.list() == (records[0], records[2])


def test_store_reset_and_iteration():
    store = TargetInstanceStore(capacity=5)
    record = TargetInstanceRecord(
        id="9f61c2e8-7b45-4a0d-8c7e-5d3a6f1e9c4a",
        name="n1",
        target_type="office-document",
        description="",
        created_at=1,
    )
    store.put(record)
    assert list(store) == [record]
    store.reset()
    assert len(store) == 0
    assert list(store) == []


def test_store_capacity_validation_and_default():
    for bad in (0, -1, 1.5, True, None):
        try:
            TargetInstanceStore(capacity=bad)
            raise AssertionError(f"expected rejection for capacity={bad!r}")
        except InvalidTargetInstanceError:
            pass
    assert TargetInstanceStore().capacity == DEFAULT_TARGET_INSTANCE_STORE_CAPACITY


def test_store_concurrent_access_is_thread_safe():
    store = TargetInstanceStore(capacity=100)

    def writer(offset: int) -> None:
        for i in range(20):
            try:
                store.put(
                    TargetInstanceRecord(
                        id=f"{offset:02x}{i:06x}-e4a7-4b62-8d05-{i:012x}",
                        name=f"n{i}",
                        target_type="office-document",
                        description="",
                        created_at=i,
                    )
                )
            except InvalidTargetInstanceError:
                pass
            except DuplicateTargetInstanceError:
                pass

    def reader() -> None:
        for _ in range(50):
            assert len(store.list()) <= 100

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
    threads += [threading.Thread(target=reader) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store) <= 100


def test_default_store_is_process_wide():
    assert default_target_instance_store() is default_target_instance_store()


# -- orchestration + injection ----------------------------------------------- #


def test_create_target_instance_uses_injected_clock_and_id():
    store = TargetInstanceStore(capacity=10)
    record = create_target_instance(
        "alpha",
        "office-document",
        "desc",
        instance_id="d28a3b95-ff91-4c5c-8353-36a294c9c02f",
        clock=lambda: 1700000000,
        store=store,
    )
    assert record.id == "d28a3b95-ff91-4c5c-8353-36a294c9c02f"
    assert record.created_at == 1700000000
    assert record.name == "alpha"
    assert store.get(record.id) is record
    assert resolve_target_instance(record.id, store=store) is record


def test_create_target_instance_mints_unique_ids():
    store = TargetInstanceStore(capacity=10)
    a = create_target_instance("a", "office-document", store=store, clock=lambda: 1)
    b = create_target_instance("b", "pdf-document", store=store, clock=lambda: 2)
    assert a.id != b.id
    assert _UUID_RE.fullmatch(a.id)
    assert _UUID_RE.fullmatch(b.id)
    assert len(store) == 2


def test_create_target_instance_rejects_blank_names_and_unknown_types():
    store = TargetInstanceStore(capacity=10)
    for bad_name in ("", "   ", "\t", "\n"):
        try:
            create_target_instance(bad_name, "office-document", store=store)
            raise AssertionError(f"expected rejection for {bad_name!r}")
        except ValueError:
            pass
    try:
        create_target_instance("ok", "not-a-type", store=store)
        raise AssertionError("expected rejection for unknown target type")
    except ValueError:
        pass
    assert len(store) == 0


def test_resolve_target_instance_never_leaks_malformed_ids():
    store = TargetInstanceStore(capacity=5)
    for bad in (None, 123, "not-uuid"):
        assert resolve_target_instance(bad, store=store) is None
    assert resolve_target_instance("00000000-0000-0000-0000-000000000000", store=store) is None


# -- API --------------------------------------------------------------------- #


def _client(capacity: int = 10) -> tuple[TestClient, TargetInstanceStore]:
    store = TargetInstanceStore(capacity=capacity)
    app = create_app(target_instance_store=store)
    return TestClient(app), store


def test_api_create_list_get_delete_success():
    client, store = _client()

    body = client.get("/api/target-instances").json()
    assert body["instances"] == []
    assert body["count"] == 0
    assert body["total"] == 0

    r = client.post(
        "/api/target-instances",
        json={
            "name": "doc",
            "target_type": "office-document",
            "description": "important",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "doc"
    assert body["target_type"] == "office-document"
    assert body["description"] == "important"
    assert _UUID_RE.fullmatch(body["id"])
    assert isinstance(body["created_at"], int)

    iid = body["id"]

    r = client.get(f"/api/target-instances/{iid}")
    assert r.status_code == 200
    assert r.json()["id"] == iid

    r = client.get("/api/target-instances")
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["instances"][0]["id"] == iid

    r = client.delete(f"/api/target-instances/{iid}")
    assert r.status_code == 204
    assert r.content == b""

    assert client.get(f"/api/target-instances/{iid}").status_code == 404
    assert client.get("/api/target-instances").json()["count"] == 0


def test_api_create_defaults_description_to_empty():
    client, _ = _client()
    r = client.post("/api/target-instances", json={"name": "n", "target_type": "zip-archive"})
    assert r.status_code == 201
    assert r.json()["description"] == ""


def test_api_validation_errors():
    client, store = _client()

    invalid_payloads = [
        {"name": "", "target_type": "office-document"},
        {"name": "   ", "target_type": "office-document"},
        {"name": "\t", "target_type": "office-document"},
        {"target_type": "office-document"},
        {"name": "x", "target_type": "bad"},
        {"name": "x", "target_type": None},
        {"name": "a" * (MAX_NAME_LENGTH + 1), "target_type": "office-document"},
        {
            "name": "x",
            "target_type": "office-document",
            "description": "d" * (MAX_DESCRIPTION_LENGTH + 1),
        },
    ]
    for payload in invalid_payloads:
        r = client.post("/api/target-instances", json=payload)
        assert r.status_code == 422, payload
    assert len(store) == 0


def test_api_unknown_and_malformed_ids_return_404():
    client, _ = _client()
    for bad in (
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-000000000001",
        "not-a-uuid",
        "def",
    ):
        assert client.get(f"/api/target-instances/{bad}").status_code == 404, bad
        assert client.delete(f"/api/target-instances/{bad}").status_code == 404, bad


def test_api_strict_schema_rejects_extra_fields():
    client, store = _client()
    r = client.post(
        "/api/target-instances",
        json={"name": "x", "target_type": "office-document", "extra": "bad"},
    )
    assert r.status_code == 422
    assert len(store) == 0


def test_api_deleting_one_instance_leaves_others_and_jobs_untouched():
    from app.engines.jobs import JobStore

    job_store = JobStore()
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(job_store=job_store, target_instance_store=ti_store)
    client = TestClient(app)

    first = client.post(
        "/api/target-instances", json={"name": "one", "target_type": "office-document"}
    ).json()
    second = client.post(
        "/api/target-instances", json={"name": "two", "target_type": "pdf-document"}
    ).json()

    r = client.post("/api/jobs", json={"target": "pdf-document", "reference": "audit-1"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    deleted = client.delete(f"/api/target-instances/{first['id']}")
    assert deleted.status_code == 204

    assert client.get(f"/api/target-instances/{first['id']}").status_code == 404
    assert client.get(f"/api/target-instances/{second['id']}").status_code == 200

    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["job_id"] == job_id
    assert client.get("/api/jobs").json()["count"] == 1

    remaining = client.get("/api/target-instances").json()
    assert remaining["count"] == 1
    assert remaining["instances"][0]["id"] == second["id"]


# ---------------------------------------------------------------------------
# Target-instance pagination (Phase 7B)
# ---------------------------------------------------------------------------


def test_target_instances_pagination_default():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    for i in range(3):
        client.post("/api/target-instances", json={"name": f"n{i}", "target_type": "pdf-document"})

    res = client.get("/api/target-instances")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert body["count"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0


def test_target_instances_pagination_explicit_limit():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    for i in range(5):
        client.post("/api/target-instances", json={"name": f"n{i}", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?limit=2")
    body = res.json()
    assert body["total"] == 5
    assert body["count"] == 2
    assert body["limit"] == 2


def test_target_instances_pagination_offset():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    for i in range(5):
        client.post("/api/target-instances", json={"name": f"n{i}", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?limit=2&offset=2")
    body = res.json()
    assert body["total"] == 5
    assert body["count"] == 2
    assert body["offset"] == 2


def test_target_instances_pagination_deterministic_order():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    for i in range(5):
        client.post("/api/target-instances", json={"name": f"n{i}", "target_type": "pdf-document"})

    page1 = client.get("/api/target-instances?limit=2&offset=0").json()
    page2 = client.get("/api/target-instances?limit=2&offset=2").json()
    page3 = client.get("/api/target-instances?limit=2&offset=4").json()

    all_ids = [inst["id"] for inst in page1["instances"] + page2["instances"] + page3["instances"]]
    assert len(all_ids) == 5
    assert len(set(all_ids)) == 5


def test_target_instances_pagination_empty():
    app = create_app(target_instance_store=TargetInstanceStore(capacity=10))
    client = TestClient(app)

    res = client.get("/api/target-instances")
    body = res.json()
    assert body["total"] == 0
    assert body["count"] == 0
    assert body["instances"] == []


def test_target_instances_pagination_offset_beyond_end():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    client.post("/api/target-instances", json={"name": "n", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?offset=100")
    body = res.json()
    assert body["total"] == 1
    assert body["count"] == 0
    assert body["instances"] == []


def test_target_instances_invalid_limit():
    app = create_app()
    client = TestClient(app)
    assert client.get("/api/target-instances?limit=0").status_code == 422
    assert client.get("/api/target-instances?limit=-1").status_code == 422
    assert client.get("/api/target-instances?limit=101").status_code == 422


def test_target_instances_invalid_offset():
    app = create_app()
    client = TestClient(app)
    assert client.get("/api/target-instances?offset=-1").status_code == 422


def test_target_instances_pagination_does_not_trigger_discovery():
    app = create_app()
    client = TestClient(app)
    res = client.get("/api/target-instances?limit=10&offset=0")
    assert res.status_code == 200
    assert "engines" not in res.text


def test_target_instances_ordering_insertion_order():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    client.post("/api/target-instances", json={"name": "first", "target_type": "pdf-document"})
    client.post("/api/target-instances", json={"name": "second", "target_type": "pdf-document"})
    client.post("/api/target-instances", json={"name": "third", "target_type": "pdf-document"})

    res = client.get("/api/target-instances")
    body = res.json()
    names = [inst["name"] for inst in body["instances"]]
    assert names == ["first", "second", "third"]


def test_target_instances_pagination_middle_page():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    for i in range(10):
        client.post("/api/target-instances", json={"name": f"n{i}", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?limit=3&offset=3")
    body = res.json()
    assert body["total"] == 10
    assert body["count"] == 3
    assert body["offset"] == 3


def test_target_instances_pagination_final_page():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    for i in range(10):
        client.post("/api/target-instances", json={"name": f"n{i}", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?limit=3&offset=9")
    body = res.json()
    assert body["total"] == 10
    assert body["count"] == 1


def test_target_instances_max_limit():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    for i in range(10):
        client.post("/api/target-instances", json={"name": f"n{i}", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?limit=100")
    body = res.json()
    assert body["total"] == 10
    assert body["count"] == 10
    assert body["limit"] == 100


def test_target_instances_min_limit():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    client = TestClient(app)

    client.post("/api/target-instances", json={"name": "n", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?limit=1")
    body = res.json()
    assert body["total"] == 1
    assert body["count"] == 1
    assert body["limit"] == 1


def test_api_does_not_modify_static_target_catalog():
    client, _ = _client()
    before = client.get("/api/targets")
    assert before.status_code == 200
    before_body = before.json()

    for name, target_type in (("a", "office-document"), ("b", "zip-archive")):
        r = client.post("/api/target-instances", json={"name": name, "target_type": target_type})
        assert r.status_code == 201

    after = client.get("/api/targets")
    assert after.status_code == 200
    assert after.json() == before_body

    # A valid compatibility-plan request still returns a structured preview.
    r = client.post(
        "/api/targets/office-document/compatibility/plan",
        json={"mode": "preview", "reference": "ref"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["target"] == "office-document"
    assert "selection" in body
    assert "request" in body
    assert "authorization" in body
    assert "plan" in body
    assert "simulation" in body


def test_api_does_not_modify_compatibility_plan_behaviour():
    client, _ = _client()
    payload = {"mode": "preview", "reference": "ref"}
    first = client.post("/api/targets/office-document/compatibility/plan", json=payload)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["target"] == "office-document"

    r = client.post("/api/target-instances", json={"name": "x", "target_type": "office-document"})
    assert r.status_code == 201

    second = client.post("/api/targets/office-document/compatibility/plan", json=payload)
    assert second.status_code == 200
    assert second.json() == first_body


def test_api_does_not_contaminate_default_store():
    client, store = _client()
    r = client.post("/api/target-instances", json={"name": "x", "target_type": "office-document"})
    assert r.status_code == 201
    assert len(store) == 1
    assert len(default_target_instance_store()) == 0


def test_api_endpoints_never_execute_or_discover():
    import app.engines.discovery.runner as runner_module

    calls = []
    original = runner_module.run_safe_command
    try:
        runner_module.run_safe_command = lambda *a, **k: calls.append((a, k))  # type: ignore[assignment]
        client, _ = _client()
        r = client.post(
            "/api/target-instances",
            json={"name": "x", "target_type": "office-document", "description": "d"},
        )
        assert r.status_code == 201
        iid = r.json()["id"]
        assert client.get("/api/target-instances").status_code == 200
        assert client.get(f"/api/target-instances/{iid}").status_code == 200
        assert client.delete(f"/api/target-instances/{iid}").status_code == 204
        assert calls == []
    finally:
        runner_module.run_safe_command = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Target-instance filtering (Phase 7D)
# ---------------------------------------------------------------------------


def _filtered_client():
    ti_store = TargetInstanceStore(capacity=10)
    app = create_app(target_instance_store=ti_store)
    return TestClient(app), ti_store


def test_ti_filter_by_target_type():
    client, _ = _filtered_client()
    client.post("/api/target-instances", json={"name": "a", "target_type": "pdf-document"})
    client.post("/api/target-instances", json={"name": "b", "target_type": "zip-archive"})
    client.post("/api/target-instances", json={"name": "c", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?target_type=pdf-document")
    body = res.json()
    assert res.status_code == 200
    assert body["total"] == 2
    assert body["count"] == 2
    assert all(i["target_type"] == "pdf-document" for i in body["instances"])


def test_ti_filter_by_name_substring_is_case_insensitive():
    client, _ = _filtered_client()
    client.post("/api/target-instances", json={"name": "Alpha Server", "target_type": "pdf-document"})
    client.post("/api/target-instances", json={"name": "beta node", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?name=ALPHA")
    body = res.json()
    assert body["total"] == 1
    assert body["instances"][0]["name"] == "Alpha Server"


def test_ti_filter_no_match_returns_empty_page_not_error():
    client, _ = _filtered_client()
    client.post("/api/target-instances", json={"name": "a", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?target_type=not-a-real-type")
    body = res.json()
    assert res.status_code == 200
    assert body["total"] == 0
    assert body["count"] == 0
    assert body["instances"] == []


def test_ti_filter_by_target_type_and_name():
    client, _ = _filtered_client()
    client.post("/api/target-instances", json={"name": "Alpha", "target_type": "pdf-document"})
    client.post("/api/target-instances", json={"name": "Alpha", "target_type": "zip-archive"})
    client.post("/api/target-instances", json={"name": "Beta", "target_type": "pdf-document"})

    res = client.get("/api/target-instances?target_type=pdf-document&name=alpha")
    body = res.json()
    assert body["total"] == 1
    assert body["instances"][0]["name"] == "Alpha"
    assert body["instances"][0]["target_type"] == "pdf-document"


def test_ti_filters_apply_before_pagination():
    client, _ = _filtered_client()
    for i in range(5):
        client.post(
            "/api/target-instances",
            json={"name": f"pdf-{i}", "target_type": "pdf-document"},
        )
    for i in range(2):
        client.post(
            "/api/target-instances",
            json={"name": f"zip-{i}", "target_type": "zip-archive"},
        )

    res = client.get("/api/target-instances?target_type=pdf-document&limit=2")
    body = res.json()
    assert body["total"] == 5
    assert body["count"] == 2
    assert body["limit"] == 2


def test_ti_filtered_pagination_middle_and_final_page():
    client, _ = _filtered_client()
    for i in range(5):
        client.post(
            "/api/target-instances",
            json={"name": f"pdf-{i}", "target_type": "pdf-document"},
        )
    client.post("/api/target-instances", json={"name": "zip", "target_type": "zip-archive"})

    page2 = client.get("/api/target-instances?target_type=pdf-document&limit=2&offset=2").json()
    assert page2["total"] == 5
    assert page2["count"] == 2
    assert page2["offset"] == 2

    page3 = client.get("/api/target-instances?target_type=pdf-document&limit=2&offset=4").json()
    assert page3["total"] == 5
    assert page3["count"] == 1


def test_ti_empty_name_filter_matches_everything():
    client, _ = _filtered_client()
    client.post("/api/target-instances", json={"name": "a", "target_type": "pdf-document"})
    client.post("/api/target-instances", json={"name": "b", "target_type": "zip-archive"})

    res = client.get("/api/target-instances?name=")
    assert res.json()["total"] == 2


def test_ti_filters_preserve_insertion_order():
    client, _ = _filtered_client()
    first = client.post(
        "/api/target-instances", json={"name": "first", "target_type": "pdf-document"}
    ).json()
    client.post("/api/target-instances", json={"name": "skip", "target_type": "zip-archive"})
    third = client.post(
        "/api/target-instances", json={"name": "third", "target_type": "pdf-document"}
    ).json()

    res = client.get("/api/target-instances?target_type=pdf-document")
    ids = [i["id"] for i in res.json()["instances"]]
    assert ids == [first["id"], third["id"]]


def test_ti_filters_still_validate_limit_and_offset():
    client, _ = _filtered_client()
    client.post("/api/target-instances", json={"name": "a", "target_type": "pdf-document"})

    assert client.get("/api/target-instances?target_type=pdf-document&limit=0").status_code == 422
    assert client.get("/api/target-instances?name=a&offset=-1").status_code == 422


def test_ti_filters_do_not_trigger_discovery():
    import app.engines.discovery.runner as runner_module

    calls = []
    original = runner_module.run_safe_command
    try:
        runner_module.run_safe_command = lambda *a, **k: calls.append((a, k))  # type: ignore[assignment]
        client, _ = _filtered_client()
        client.post("/api/target-instances", json={"name": "a", "target_type": "pdf-document"})
        res = client.get("/api/target-instances?target_type=pdf-document&name=a&limit=10&offset=0")
        assert res.status_code == 200
        assert calls == []
    finally:
        runner_module.run_safe_command = original  # type: ignore[assignment]


def test_ti_filters_survive_deletion_of_other_instances():
    client, _ = _filtered_client()
    keep = client.post(
        "/api/target-instances", json={"name": "keep", "target_type": "pdf-document"}
    ).json()
    drop = client.post(
        "/api/target-instances", json={"name": "drop", "target_type": "pdf-document"}
    ).json()
    assert client.delete(f"/api/target-instances/{drop['id']}").status_code == 204

    res = client.get("/api/target-instances?target_type=pdf-document")
    body = res.json()
    assert body["total"] == 1
    assert body["instances"][0]["id"] == keep["id"]
