"""Phase 8E - in-process mutation rate limiting and body-size bounds.

Fail-closed HTTP guards, layered *on top of* - never instead of -
route validation, the execution policy and persistence checks:

* :class:`RateLimiter` - a fixed-window counter per client address with a
  bounded key table; over-limit mutations answer ``HTTP 429`` with a
  ``Retry-After`` header and a structured, generic detail;
* :func:`exceeds_body_limit` - the ``Content-Length`` bound rejecting
  oversized mutation bodies with ``HTTP 413`` and
  ``{"error": "payload_too_large", ...}``;
* :func:`unsupported_transfer_encoding_detail` - state-changing requests
  that use ``Transfer-Encoding`` (chunked uploads) are rejected with
  ``HTTP 400`` before any body is read, so the size bound cannot be
  bypassed by omitting ``Content-Length``;
* :func:`configured_trusted_proxies` / :func:`resolve_client_address` -
  by default the rate-limit key is the socket peer address and
  ``X-Forwarded-For`` is never consulted; an operator running behind a
  reverse proxy can set an exact-IP trust anchor
  (``PASSORCH_TRUSTED_PROXIES`` or the ``trusted_proxies`` argument) so
  that requests arriving *from* a trusted proxy are keyed by the real
  client hop instead.  Misconfiguration fails fast at construction, and
  any malformed or missing forwarding data fails closed to the peer;
* :func:`configured_limits` - explicit ``create_app`` arguments win over
  the ``PASSORCH_RATE_LIMIT`` / ``PASSORCH_RATE_WINDOW_SECONDS`` /
  ``PASSORCH_MAX_BODY_BYTES`` environment variables; a bad value is a
  fail-fast ``ValueError`` at construction, never a silent "no limit".

Scope: only ``POST`` / ``PUT`` / ``PATCH`` / ``DELETE`` requests are
counted or size-checked - reads stay unlimited.

Documented limitations (also in README, kept honest by tests):

* counters live in process memory: restarting the application clears
  them, and separate worker processes do not share them;
* the client key is the socket peer address by default; ``X-Forwarded-For``
  is deliberately **ignored** unless the operator configures the exact-IP
  trust anchor (``PASSORCH_TRUSTED_PROXIES``), because the header is
  spoofable without a trusted proxy in front.  Even with the anchor set,
  only requests whose socket peer is itself trusted consult the header,
  and malformed data fails closed to the peer;
* a body without a usable ``Content-Length`` *and* without
  ``Transfer-Encoding`` is treated as absent (empty-body mutations such
  as ``DELETE`` are allowed); a present-but-invalid ``Content-Length``
  fails closed as a violation, and ``Transfer-Encoding`` on a mutation
  is rejected outright (``HTTP 400``) - chunked uploads are unsupported
  by design;
* the per-address table is bounded (newest
  :data:`MAX_TRACKED_ADDRESSES` addresses tracked); evicting an old
  address resets its counter, trading strictness for bounded memory.
"""

from __future__ import annotations

import ipaddress
import math
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any, Callable, NamedTuple

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_RATE_WINDOW_SECONDS",
    "MAX_TRACKED_ADDRESSES",
    "MUTATING_METHODS",
    "RATE_LIMIT_EXCEEDED_ERROR",
    "PAYLOAD_TOO_LARGE_ERROR",
    "TRUSTED_PROXIES_ENV_VAR",
    "UNSUPPORTED_TRANSFER_ENCODING_ERROR",
    "RateLimiter",
    "RateLimitConfig",
    "configured_limits",
    "configured_trusted_proxies",
    "exceeds_body_limit",
    "payload_too_large_detail",
    "rate_limited_detail",
    "resolve_client_address",
    "unsupported_transfer_encoding_detail",
]

#: Methods that change state - the only requests guards apply to.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Opt-in reverse-proxy trust anchor (exact IPs, comma-separated).
TRUSTED_PROXIES_ENV_VAR = "PASSORCH_TRUSTED_PROXIES"

#: Stable machine-readable error codes.
RATE_LIMIT_EXCEEDED_ERROR = "rate_limited"
PAYLOAD_TOO_LARGE_ERROR = "payload_too_large"
UNSUPPORTED_TRANSFER_ENCODING_ERROR = "unsupported_transfer_encoding"

#: Generous defaults: normal interactive and test traffic never trips them.
DEFAULT_RATE_LIMIT = 120
DEFAULT_RATE_WINDOW_SECONDS = 60.0
DEFAULT_MAX_BODY_BYTES = 64 * 1024

#: Bounded per-address table (LRU-evicted) - see module docstring.
MAX_TRACKED_ADDRESSES = 1000

#: Generic configuration messages: name no value and no path.
_INVALID_CONFIG = "rate limit settings must be positive numbers"
_INVALID_TRUSTED_PROXIES = "trusted proxies must be a list of exact IP addresses"


class RateLimitConfig(NamedTuple):
    """The resolved guards for one application instance."""

    limit: int
    window_seconds: float
    max_body_bytes: int


def _cast_positive(raw: object, caster: Callable[[Any], Any]) -> Any:
    try:
        value = caster(raw)
    except (TypeError, ValueError):
        raise ValueError(_INVALID_CONFIG) from None
    if value <= 0:
        raise ValueError(_INVALID_CONFIG)
    return value


def _resolve(
    explicit: object,
    env_name: str,
    caster: Callable[[Any], Any],
    default: object,
) -> Any:
    """An explicit ``create_app`` argument, else the environment, else default."""

    if explicit is not None:
        return _cast_positive(explicit, caster)
    raw = os.environ.get(env_name, "")
    if not raw.strip():
        return default
    return _cast_positive(raw, caster)


def configured_limits(
    rate_limit: int | None = None,
    rate_window_seconds: float | None = None,
    max_body_bytes: int | None = None,
) -> RateLimitConfig:
    """Resolve all three guard settings, failing fast on invalid input."""

    return RateLimitConfig(
        limit=int(
            _resolve(rate_limit, "PASSORCH_RATE_LIMIT", int, DEFAULT_RATE_LIMIT)
        ),
        window_seconds=float(
            _resolve(
                rate_window_seconds,
                "PASSORCH_RATE_WINDOW_SECONDS",
                float,
                DEFAULT_RATE_WINDOW_SECONDS,
            )
        ),
        max_body_bytes=int(
            _resolve(max_body_bytes, "PASSORCH_MAX_BODY_BYTES", int, DEFAULT_MAX_BODY_BYTES)
        ),
    )


def configured_trusted_proxies(
    explicit: str | Iterable[str] | None = None,
) -> frozenset[str]:
    """Resolve the opt-in reverse-proxy trust anchor (exact IPs only).

    An explicit ``create_app`` argument (a comma-separated string or an
    iterable of addresses) wins over the ``PASSORCH_TRUSTED_PROXIES``
    environment variable (comma-separated).  Unset or blank means "no
    trusted proxies" - the default - and ``X-Forwarded-For`` is never
    consulted.  Every entry must be a valid exact IP address (IPv4 or
    IPv6, canonicalised); hostnames, CIDR ranges, blank entries inside a
    non-empty list, and any other value fail fast with a generic
    ``ValueError`` that never echoes the raw input.
    """

    if explicit is None:
        raw = os.environ.get(TRUSTED_PROXIES_ENV_VAR, "")
        if not raw.strip():
            return frozenset()
        entries = raw.split(",")
    elif isinstance(explicit, str):
        if not explicit.strip():
            return frozenset()
        entries = explicit.split(",")
    else:
        entries = list(explicit)

    resolved: set[str] = set()
    for entry in entries:
        candidate = entry.strip() if isinstance(entry, str) else ""
        if not candidate:
            raise ValueError(_INVALID_TRUSTED_PROXIES) from None
        try:
            resolved.add(str(ipaddress.ip_address(candidate)))
        except ValueError:
            raise ValueError(_INVALID_TRUSTED_PROXIES) from None
    return frozenset(resolved)


def resolve_client_address(
    peer: str,
    x_forwarded_for: str | None,
    trusted_proxies: frozenset[str],
) -> str:
    """The rate-limit key for a request: the real client, or the peer.

    Default (``trusted_proxies`` empty): the socket peer address, always -
    ``x_forwarded_for`` is never consulted, so a spoofed header cannot
    move a client between buckets.

    With a trust anchor configured: only when the socket peer is itself
    a trusted proxy is the ``X-Forwarded-For`` chain walked right-to-left
    past trusted hops; the first untrusted hop becomes the key.  Any
    malformed hop, a missing header, or a chain that is entirely trusted
    fails closed to the peer address (the proxy's own bucket) - a client
    identity is never invented.
    """

    if not trusted_proxies:
        return peer
    try:
        peer_ip = str(ipaddress.ip_address(peer.strip()))
    except ValueError:
        return peer
    if peer_ip not in trusted_proxies:
        return peer
    if x_forwarded_for is None:
        return peer
    hops = [hop.strip() for hop in x_forwarded_for.split(",")]
    if not all(hops):
        return peer
    for hop in reversed(hops):
        try:
            hop_ip = str(ipaddress.ip_address(hop))
        except ValueError:
            return peer
        if hop_ip not in trusted_proxies:
            return hop_ip
    return peer


def exceeds_body_limit(content_length: str | None, limit: int) -> bool:
    """Whether a ``Content-Length`` header rejects the body for ``limit``.

    ``None`` (header absent) is not checked here - an absent header with
    no ``Transfer-Encoding`` means "no body" (empty-body mutations such
    as ``DELETE``); ``Transfer-Encoding`` on a mutation is rejected
    outright by the request guard (``HTTP 400``) before any body is
    read.  A present-but-unparseable or negative value fails closed as a
    violation.
    """

    if content_length is None:
        return False
    try:
        size = int(content_length.strip())
    except ValueError:
        return True
    return size < 0 or size > limit


def rate_limited_detail(retry_after: int) -> dict[str, object]:
    """Structured 429 detail - generic, rebuilt per call, no internals."""

    return {
        "error": RATE_LIMIT_EXCEEDED_ERROR,
        "detail": "too many requests",
        "retry_after": int(retry_after),
    }


def payload_too_large_detail() -> dict[str, str]:
    """Structured 413 detail - generic, rebuilt per call, no internals."""

    return {
        "error": PAYLOAD_TOO_LARGE_ERROR,
        "detail": "request body exceeds the configured size limit",
    }


def unsupported_transfer_encoding_detail() -> dict[str, str]:
    """Structured 400 detail for chunked/unknown body framing on mutations.

    Generic and rebuilt per call: names no internals, no limits, no
    configuration - chunked uploads are unsupported by design so the
    ``Content-Length`` body cap can never be bypassed.
    """

    return {
        "error": UNSUPPORTED_TRANSFER_ENCODING_ERROR,
        "detail": "transfer-encoded request bodies are not supported",
    }


class RateLimiter:
    """Fixed-window counter per client address with bounded memory.

    ``check`` records one hit and returns the whole seconds a caller
    should wait when the address is over its limit, else ``None``.  A
    new window starts automatically once the old one expires; the table
    keeps at most :data:`MAX_TRACKED_ADDRESSES` addresses (oldest
    evicted).  Thread-safe: one lock around a dict of tuples, no I/O.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = int(limit)
        self._window = float(window_seconds)
        self._counts: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> float:
        return self._window

    @property
    def tracked_addresses(self) -> int:
        with self._lock:
            return len(self._counts)

    def check(self, key: str, now: float | None = None) -> int | None:
        """Count one hit for ``key``; retry-after seconds when over limit."""

        moment = time.monotonic() if now is None else now
        with self._lock:
            window_start, count = self._counts.get(key, (moment, 0))
            if moment - window_start >= self._window:
                window_start, count = moment, 0
            count += 1
            self._counts[key] = (window_start, count)
            self._counts.move_to_end(key)
            while len(self._counts) > MAX_TRACKED_ADDRESSES:
                self._counts.popitem(last=False)
            if count <= self._limit:
                return None
            remaining = window_start + self._window - moment
            return max(1, math.ceil(remaining))
