"""Step 6 - one structured error envelope for manually-raised HTTP errors.

Every non-2xx response whose body this application builds - the request-guard
middleware and every route's ``HTTPException`` - carries the same envelope::

    {"detail": {"error": "<machine-code>", "detail": "<human message>", ...}}

``error`` is a stable snake_case machine code a client can branch on; ``detail``
is a short human sentence (never an exception text, SQL fragment, file path or
traceback); any extra keys are structured payload specific to that code (for
example ``supported_targets`` on ``unknown_target``).  The SPA's
``describeError`` renders this object shape as ``"<message> (<code>)"``, and the
string fallback in ``frontend/js/api.js`` remains only for robustness against
proxies or older servers - the server itself no longer emits bare-string
details.

The single documented exception is FastAPI's automatic request-body validation
``422`` (``HTTPValidationError``): it keeps its ``loc`` / ``msg`` / ``type``
array shape so OpenAPI tooling and clients that understand Pydantic errors keep
working unchanged.  Domain and query-parameter rejections raised inside handlers
are *not* Pydantic validation - they use this envelope.

``persistence_unavailable`` (503), ``unauthorized`` (401), ``rate_limited``
(429), ``payload_too_large`` (413), ``unsupported_transfer_encoding`` (400),
``unknown_target`` (422), ``target_instance_type_mismatch`` (422) and the
instance-association 422s already follow this envelope; their builders stay in
their domain modules (``app.api.auth``, ``app.api.rate_limit``,
``app.api.routes.jobs``) so their tests and imports are untouched.
"""

from __future__ import annotations

from fastapi import HTTPException

__all__ = [
    "ENGINE_NOT_REGISTERED_ERROR",
    "INVALID_REQUEST_ERROR",
    "JOB_NOT_FOUND_ERROR",
    "TARGET_INSTANCE_NOT_FOUND_ERROR",
    "TARGET_NOT_MAPPED_ERROR",
    "UNKNOWN_ENGINE_ERROR",
    "api_error",
    "engine_not_registered",
    "error_body",
    "instance_not_found",
    "invalid_request",
    "job_not_found",
    "target_not_mapped",
    "unknown_engine",
]

#: Domain/validation rejection inside a handler (never a Pydantic 422).
INVALID_REQUEST_ERROR = "invalid_request"

#: Stable machine-readable 404 codes.
JOB_NOT_FOUND_ERROR = "job_not_found"
TARGET_INSTANCE_NOT_FOUND_ERROR = "target_instance_not_found"
TARGET_NOT_MAPPED_ERROR = "target_not_mapped"
UNKNOWN_ENGINE_ERROR = "unknown_engine"
ENGINE_NOT_REGISTERED_ERROR = "engine_not_registered"


def error_body(code: str, message: str, **extra: object) -> dict[str, object]:
    """The inner detail object: ``error`` + ``detail`` plus any extra fields.

    Rebuilt per call so a caller mutating the result can never poison a later
    response; ``extra`` keys never overwrite ``error`` or ``detail``.
    """

    body: dict[str, object] = {"error": code, "detail": message}
    for key, value in extra.items():
        if key not in ("error", "detail"):
            body[key] = value
    return body


def api_error(
    status_code: int, code: str, message: str, **extra: object
) -> HTTPException:
    """An ``HTTPException`` whose JSON body is the standard error envelope."""

    return HTTPException(status_code=status_code, detail=error_body(code, message, **extra))


def invalid_request(message: str) -> HTTPException:
    """Structured 422 for a domain or query-parameter rejection."""

    return api_error(422, INVALID_REQUEST_ERROR, message)


def job_not_found() -> HTTPException:
    """Structured 404 for a malformed or unknown job identifier."""

    return api_error(404, JOB_NOT_FOUND_ERROR, "job does not exist")


def instance_not_found() -> HTTPException:
    """Structured 404 for a malformed or unknown target-instance identifier.

    The message matches the POST ``/api/jobs`` association 422 so malformed
    and unknown identifiers stay indistinguishable and the two responses read
    the same to a user.
    """

    return api_error(
        404, TARGET_INSTANCE_NOT_FOUND_ERROR, "target instance does not exist"
    )


def target_not_mapped() -> HTTPException:
    """Structured 404 for a target missing from the read-only format policy."""

    return api_error(
        404,
        TARGET_NOT_MAPPED_ERROR,
        "target is not mapped to an enabled format policy",
    )


def unknown_engine() -> HTTPException:
    """Structured 404 for an engine that is not in ``KNOWN_ENGINES``."""

    return api_error(404, UNKNOWN_ENGINE_ERROR, "engine is not a known engine")


def engine_not_registered() -> HTTPException:
    """Structured 404 for a known engine that has no discovery registration."""

    return api_error(404, ENGINE_NOT_REGISTERED_ERROR, "engine is not registered")
