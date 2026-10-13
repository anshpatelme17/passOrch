"""Phase 4A - API client contract tests.

These assert the *specification* the shared client in ``frontend/js/api.js``
implements, by executing the same rules against a Python transcription of the
response handling. They cover the cases a Python integration test cannot reach
because they are client-side decisions:

* HTTP errors vs transport errors vs invalid arguments;
* 204 / empty body / malformed JSON;
* both error detail shapes (object and array);
* a denied plan arriving as HTTP 200 (never a failure).

No JavaScript runtime is available here (see ``tests/test_frontend_js.py``), so
the rules are asserted on a faithful Python mirror plus static assertions that
``js/api.js`` implements each one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
API_SOURCE = (FRONTEND / "js" / "api.js").read_text(encoding="utf-8")

API_ERRORS = {
    "unknownTarget": "unknown_target",
    "instanceNotFound": "target_instance_not_found",
    "instanceTypeMismatch": "target_instance_type_mismatch",
}

ALLOWED_PREFIXES = ("/api/", "/health")


# --------------------------------------------------------------------------- #
# Python mirror of the client's decision rules
# --------------------------------------------------------------------------- #


class TransportError(Exception):
    pass


def describe_error(status: int, detail: object) -> str:
    if isinstance(detail, list):
        parts = []
        for entry in detail:
            if isinstance(entry, dict):
                where = ".".join(str(p) for p in entry.get("loc", []))
                msg = str(entry.get("msg", ""))
                parts.append(f"{where}: {msg}".strip(": "))
            elif isinstance(entry, str):
                parts.append(entry)
        if parts:
            return "Validation failed - " + " ".join(f"[{p}]" for p in parts)
        return f"Request rejected ({status})"
    if isinstance(detail, dict):
        code = detail.get("error", "")
        text = detail.get("detail", "")
        if code and text:
            return text.rstrip(".") + f" ({code})"
        if code:
            return str(code)
        if text:
            return str(text)
        return f"Request rejected ({status})"
    if isinstance(detail, str) and detail:
        return detail
    return f"Request failed ({status})"


def parse_body(status: int, text: str | None) -> tuple[object, bool]:
    if status == 204:
        return None, False
    if not text:
        return None, False
    try:
        return json.loads(text), False
    except (ValueError, TypeError):
        return None, True


def request(
    path: str,
    status: int = 200,
    body: str | None = None,
    raises_on_fetch: bool = False,
) -> dict:
    if not path.startswith("/"):
        return _err("invalid_request", "api path must start with '/'")
    if not any(path.startswith(p) for p in ALLOWED_PREFIXES):
        return _err("invalid_request", "api path must start with one of: ...")

    if raises_on_fetch:
        return _err("transport", "network request failed")

    data, parse_error = parse_body(status, body)
    if 200 <= status < 300:
        return {
            "ok": True,
            "status": status,
            "data": data,
            "parseError": parse_error,
            "error": None,
        }

    # FastAPI wraps every error payload in a "detail" envelope.
    envelope = (
        data
        if isinstance(data, dict)
        else None
    )
    detail = None if parse_error else (envelope["detail"] if envelope else data)
    code = (
        detail["error"]
        if isinstance(detail, dict) and isinstance(detail.get("error"), str)
        else ""
    )
    return {
        "ok": False,
        "status": status,
        "data": data,
        "parseError": parse_error,
        "error": {
            "kind": "http",
            "message": describe_error(status, detail),
            "code": code,
            "detail": detail,
        },
    }


def _err(kind: str, message: str) -> dict:
    return {
        "ok": False,
        "status": 0,
        "data": None,
        "parseError": True,
        "error": {"kind": kind, "message": message, "code": "", "detail": None},
    }


# --------------------------------------------------------------------------- #
# status classification
# --------------------------------------------------------------------------- #


def test_success_is_ok_with_data() -> None:
    res = request("/api/targets", 200, '{"targets": [], "count": 0}')
    assert res["ok"] is True
    assert res["status"] == 200
    assert res["error"] is None
    assert res["data"]["count"] == 0


def test_201_is_success_not_an_error() -> None:
    res = request("/api/target-instances", 201, '{"id": "x"}')
    assert res["ok"] is True
    assert res["error"] is None


def test_http_error_is_not_ok_and_never_raises() -> None:
    res = request("/api/jobs", 422, '{"detail": {"}}')
    assert res["ok"] is False
    assert res["status"] == 422
    assert res["error"]["kind"] == "http"


def test_transport_error_is_distinguished_from_http_error() -> None:
    res = request("/api/jobs", raises_on_fetch=True)
    assert res["ok"] is False
    assert res["status"] == 0
    assert res["error"]["kind"] == "transport"
    assert res["data"] is None


def test_invalid_path_is_a_result_not_an_exception() -> None:
    for bad in ("api/targets", "https://evil.example/api/targets", "", "/etc/passwd"):
        res = request(bad)
        assert res["ok"] is False, bad
        assert res["error"]["kind"] == "invalid_request", bad
        assert res["status"] == 0, bad


# --------------------------------------------------------------------------- #
# body handling
# --------------------------------------------------------------------------- #


def test_204_returns_no_body_and_is_success() -> None:
    res = request("/api/target-instances/x", 204, None)
    assert res["ok"] is True
    assert res["status"] == 204
    assert res["data"] is None
    assert res["error"] is None


def test_empty_body_is_tolerated() -> None:
    res = request("/api/jobs", 200, "")
    assert res["ok"] is True
    assert res["data"] is None
    assert res["parseError"] is False


def test_malformed_json_is_flagged_not_raised() -> None:
    res = request("/api/jobs", 200, "not json at all")
    assert res["ok"] is True
    assert res["parseError"] is True
    assert res["data"] is None


def test_malformed_json_on_an_error_keeps_the_status() -> None:
    res = request("/api/jobs", 500, "<html>bad gateway</html>")
    assert res["ok"] is False
    assert res["status"] == 500
    assert res["error"]["message"] == "Request failed (500)"


# --------------------------------------------------------------------------- #
# both error detail shapes
# --------------------------------------------------------------------------- #


def test_object_shaped_domain_error_is_normalised() -> None:
    detail = {
        "error": API_ERRORS["unknownTarget"],
        "target": "nope",
        "detail": "target is not mapped to an enabled format policy",
        "supported_targets": ["pdf-document"],
    }
    res = request("/api/jobs", 422, json.dumps({"detail": detail}))
    msg = res["error"]["message"]
    assert API_ERRORS["unknownTarget"] in msg
    assert res["error"]["code"] == API_ERRORS["unknownTarget"]
    assert "nope" not in msg  # only the code and sentence, not the echo


def test_array_shaped_validation_error_is_normalised() -> None:
    detail = [
        {"type": "missing", "loc": ["body", "target"], "msg": "Field required"},
        {"type": "extra_forbidden", "loc": ["body", "command"], "msg": "Extra inputs are not permitted"},
    ]
    res = request("/api/jobs", 422, json.dumps({"detail": detail}))
    msg = res["error"]["message"]
    assert msg.startswith("Validation failed")
    assert "body.target" in msg and "body.command" in msg
    assert res["error"]["code"] == ""


def test_string_detail_is_passed_through() -> None:
    res = request("/api/jobs/x", 404, json.dumps({"detail": "job_not_found"}))
    assert res["error"]["message"] == "job_not_found"


def test_null_detail_degrades_to_a_status_line() -> None:
    res = request("/api/jobs", 500, json.dumps({"detail": None}))
    assert "500" in res["error"]["message"]


def test_instance_error_codes_are_exposed_to_views() -> None:
    for code in (
        API_ERRORS["instanceNotFound"],
        API_ERRORS["instanceTypeMismatch"],
    ):
        res = request("/api/jobs", 422, json.dumps({"detail": {"error": code, "detail": code}}))
        assert res["error"]["code"] == code


# --------------------------------------------------------------------------- #
# the 200-with-denial convention
# --------------------------------------------------------------------------- #


def test_denied_plan_is_a_successful_response() -> None:
    """A denied plan arrives as HTTP 200 - it must not be treated as a failure."""

    denial = {
        "target": "pdf-document",
        "selection": {"status": "no_verified_capability", "reason": "capability_missing"},
        "request": {},
        "authorization": {
            "authorized": False,
            "status": "denied",
            "reason": "capability_not_verified",
        },
        "plan": None,
        "simulation": None,
    }
    res = request("/api/targets/pdf-document/compatibility/plan", 200, json.dumps(denial))
    assert res["ok"] is True
    assert res["error"] is None
    assert res["data"]["authorization"]["authorized"] is False
    assert res["data"]["plan"] is None
    assert res["data"]["simulation"] is None


def test_authorized_plan_is_a_successful_response() -> None:
    plan = {
        "target": "pdf-document",
        "authorization": {"authorized": True, "status": "authorized"},
        "plan": {"adapter": "mock_john"},
        "simulation": {"simulation_id": "abc", "status": "simulated"},
    }
    res = request("/api/targets/pdf-document/compatibility/plan", 200, json.dumps(plan))
    assert res["ok"] is True
    assert res["data"]["plan"]["adapter"] == "mock_john"


# --------------------------------------------------------------------------- #
# static assertions that js/api.js implements each rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "needle",
    [
        'kind: "transport"',
        'kind: "http"',
        'kind: "invalid_request"',
        "response.status === 204",
        "parseError",
        "Array.isArray(detail)",
        "response.ok",
    ],
)
def test_api_js_implements_the_rule(needle: str) -> None:
    assert needle in API_SOURCE, needle


def test_api_js_prefixes_match_the_declared_set() -> None:
    block = re.search(r"API_PREFIXES[^\n]*\n(?:.*\n){0,3}", API_SOURCE).group(0)
    found = set(re.findall(r'"(/[\w/]+)"', block))
    assert found >= set(ALLOWED_PREFIXES), found
