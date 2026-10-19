"""Phase 9A - opt-in bearer-token authentication for state-changing requests.

Deployment context: PassOrch is a local, single-user simulation tool.  By
default **no token is configured**, authentication is off, the bundled SPA
keeps working unchanged, and every endpoint behaves exactly as in earlier
phases.

Enabling it: ``create_app(api_token=...)`` or the ``PASSORCH_API_TOKEN``
environment variable (the explicit argument wins) turns on a bearer check
for ``POST`` / ``PUT`` / ``PATCH`` / ``DELETE`` requests.  Reads
(``GET``/``HEAD``/``OPTIONS``), including ``/health`` and every list
endpoint, stay unauthenticated - a deliberate, documented choice: they are
side-effect free, and the browser SPA has no token store.

Guarantees:

* :func:`is_authorized` compares with :func:`secrets.compare_digest`
  (constant time) - never an early-exit ``==``;
* missing, malformed and wrong credentials receive one identical generic
  ``401`` (``WWW-Authenticate: Bearer``) with no reason distinction and no
  token echo;
* the configured token is never logged, never returned, and never placed
  on ``application.state`` - only the boolean ``auth_enabled``;
* an explicitly passed empty or blank token fails fast with ``ValueError``
  at construction; an empty ``PASSORCH_API_TOKEN`` is treated as "unset"
  (authentication off) and documented in README;
* authentication grants nothing on its own: route validation, the
  execution policy and persistence checks still decide every request, and
  the rate limiter counts requests *before* authentication so token
  guessing is rate-limited too.
"""

from __future__ import annotations

import os
import secrets

__all__ = [
    "AUTHORIZATION_ERROR",
    "configured_api_token",
    "bearer_token",
    "is_authorized",
    "unauthorized_detail",
]

#: Stable machine-readable error code for rejections.
AUTHORIZATION_ERROR = "unauthorized"

#: Generic failure message: reveals the expected scheme, not what failed.
_UNAUTHORIZED_DETAIL = "a valid bearer token is required"

#: Fail-fast message for an explicitly passed blank token: names no value.
_EMPTY_TOKEN = "api token must be a non-empty string"

_BEARER_SCHEME = "bearer"


def unauthorized_detail() -> dict[str, str]:
    """Structured 401 detail - identical for every failure mode, rebuilt per call."""

    return {"error": AUTHORIZATION_ERROR, "detail": _UNAUTHORIZED_DETAIL}


def configured_api_token(explicit: str | None = None) -> str | None:
    """The configured bearer token, or ``None`` when authentication is off.

    An explicit ``create_app`` argument wins over the ``PASSORCH_API_TOKEN``
    environment variable.  Blank/whitespace-only environment values count
    as unset (documented); an explicitly passed blank value is a
    configuration error and fails fast.
    """

    if explicit is not None:
        token = explicit.strip()
        if not token:
            raise ValueError(_EMPTY_TOKEN)
        return token
    raw = os.environ.get("PASSORCH_API_TOKEN", "")
    return raw.strip() or None


def bearer_token(authorization: str | None) -> str | None:
    """The credential from an ``Authorization: Bearer ...`` header, else ``None``.

    The scheme match is case-insensitive; a header without a non-empty
    token after the scheme yields ``None`` (rejected later).
    """

    if not authorization:
        return None
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != _BEARER_SCHEME:
        return None
    return value.strip() or None


def is_authorized(authorization: str | None, expected: str | None) -> bool:
    """Whether a request passes the bearer check.

    Authentication disabled (``expected`` is ``None``) allows everything;
    otherwise the credential must be present and match the expected token
    under a constant-time comparison.
    """

    if expected is None:
        return True
    provided = bearer_token(authorization)
    if provided is None:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
