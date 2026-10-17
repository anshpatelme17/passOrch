"""Phase 2B.5 - verified capability parsing.

This module turns *allowlisted probe output* into structured capability
evidence.  It is the only place where a ``VERIFIED`` capability status can be
produced.

Hardening rules implemented here:

* **provenance binding** - every parser is bound to one engine *and* one
  operation; output presented with a foreign engine or operation is rejected
  with :class:`ForeignOutputError`.
* **required header** - output without the parser's declared header line is
  ``UNKNOWN``.
* **oversized buffers** - output larger than ``MAX_CAPABILITY_OUTPUT_BYTES``
  is ``UNKNOWN`` (never partially parsed).
* **truncation markers** - format tokens ending in ``_``, ``+``, ``.``,
  ``-`` are rejected as truncation markers; any rejection downgrades the
  result to ``UNKNOWN``.
* **no partial evidence** - ``formats``/``mode_names`` are populated only when
  the result is ``VERIFIED``.

Parsing is pure string analysis: no subprocess, no filesystem access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Iterable, Mapping

from app.engines.discovery.errors import CapabilityParseError, ForeignOutputError
from app.engines.discovery.models import HASHCAT, JOHN, CapabilityStatus

__all__ = [
    "HASHCAT_MODES_HEADER",
    "JOHN_FORMATS_HEADER",
    "MAX_CAPABILITY_OUTPUT_BYTES",
    "MODE_LIST_OPERATION",
    "TRUNCATION_MARKERS",
    "FormatCapabilityParser",
    "HashcatFormatParser",
    "JohnFormatParser",
    "ParsedCapabilities",
    "ParserProvenance",
    "TokenPartition",
    "get_parser",
    "is_truncation_marker_token",
    "normalize_format_names",
    "normalize_mode_names",
    "parse_capabilities",
    "partition_tokens",
    "registered_parsers",
]

MAX_CAPABILITY_OUTPUT_BYTES = 64 * 1024

TRUNCATION_MARKERS: tuple[str, ...] = ("_", "+", ".", "-")

JOHN_FORMATS_HEADER = "Supported hash algorithms:"
HASHCAT_MODES_HEADER = "Supported hash modes:"

#: Hashcat's mode-list operation.  It is deliberately NOT on the allowlist,
#: therefore the HashcatFormatParser can never be reached by the pipeline.
MODE_LIST_OPERATION = "--hash-info"

_SPLIT_FORMAT_RE = re.compile(r"[,;\s]+")
_WHITESPACE_RE = re.compile(r"\s+")
_COMMA_RE = re.compile(r"\s*,\s*")
_FORMAT_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.+/-]*$")
_MODE_LINE_RE = re.compile(r"^\s*(?P<mode>[0-9]+)\s*\|(?P<body>.*)$")


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #


def normalize_format_names(values: str | Iterable[str] | None) -> tuple[str, ...]:
    """Token-based normalisation for *format* names.

    Input is split on commas, semicolons, newlines and whitespace - format
    names are a flat token list, never phrases.  Duplicates are removed while
    preserving first-seen order.
    """

    if values is None:
        return ()
    items = [values] if isinstance(values, str) else list(values)
    tokens: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        for raw in _SPLIT_FORMAT_RE.split(item):
            token = raw.strip().lower()
            if token:
                tokens.append(token)
    return tuple(dict.fromkeys(tokens))


def normalize_mode_names(values: str | Iterable[str] | None) -> tuple[str, ...]:
    """Phrase-aware normalisation for *hashcat mode* designations.

    Each input element is treated as one whole designation: it is never split
    on whitespace or commas, so a phrase such as
    ``"Ethereum Wallet, Password Protected"`` normalises as a single unit.
    """

    if values is None:
        return ()
    items = [values] if isinstance(values, str) else list(values)
    phrases: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        phrase = _normalize_phrase(item)
        if phrase:
            phrases.append(phrase)
    return tuple(dict.fromkeys(phrases))


def _normalize_phrase(value: str) -> str:
    phrase = value.strip()
    if not phrase:
        return ""
    phrase = _COMMA_RE.sub(" ", phrase)
    phrase = _WHITESPACE_RE.sub(" ", phrase).strip()
    phrase = phrase.lower()
    return phrase.replace(" ", "_")


# --------------------------------------------------------------------------- #
# truncation detection
# --------------------------------------------------------------------------- #


def is_truncation_marker_token(token: str) -> bool:
    """True when a token ends on a truncation marker character."""

    return bool(token) and token[-1] in TRUNCATION_MARKERS


@dataclass(frozen=True, slots=True)
class TokenPartition:
    """Outcome of applying the truncation / charset rules to parsed tokens."""

    accepted: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    truncated: bool = False
    invalid: bool = False


def partition_tokens(tokens: Iterable[str]) -> TokenPartition:
    """Reject truncation markers and malformed tokens, fail closed."""

    accepted: list[str] = []
    rejected: list[str] = []
    truncated = False
    invalid = False
    for token in tokens:
        if is_truncation_marker_token(token):
            rejected.append(token)
            truncated = True
            continue
        if not _FORMAT_TOKEN_RE.match(token):
            rejected.append(token)
            invalid = True
            continue
        accepted.append(token)
    return TokenPartition(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        truncated=truncated,
        invalid=invalid,
    )


# --------------------------------------------------------------------------- #
# parsed result + provenance
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ParserProvenance:
    """Engine + operation binding of a capability parser."""

    engine: str
    operation: str
    parser: str


@dataclass(frozen=True, slots=True)
class ParsedCapabilities:
    """Structured, raw-output-free result of one capability parse."""

    engine: str
    operation: str
    parser: str
    status: CapabilityStatus
    formats: tuple[str, ...] = ()
    mode_names: tuple[str, ...] = ()
    token_count: int = 0
    rejected_count: int = 0
    truncated: bool = False
    reason: str | None = None

    @property
    def verified(self) -> bool:
        return self.status is CapabilityStatus.VERIFIED

    @property
    def provenance(self) -> ParserProvenance:
        return ParserProvenance(engine=self.engine, operation=self.operation, parser=self.parser)


class FormatCapabilityParser:
    """Base class binding one parser to one engine + one operation."""

    name: ClassVar[str] = "format-capability-parser"
    engine: ClassVar[str] = ""
    operation: ClassVar[str] = ""
    required_header: ClassVar[str] = ""

    @property
    def provenance(self) -> ParserProvenance:
        return ParserProvenance(engine=self.engine, operation=self.operation, parser=self.name)

    def matches(self, engine: object, operation: object) -> bool:
        return engine == self.engine and operation == self.operation

    def accepts(self, engine: object, operation: object) -> bool:
        return self.matches(engine, operation)

    def parse(self, output: str, *, engine: str, operation: str) -> ParsedCapabilities:
        """Parse probe output after verifying its provenance."""

        if not self.matches(engine, operation):
            raise ForeignOutputError(engine, operation, self.name)
        return self._parse(output, operation=operation)

    # -- helpers shared by concrete parsers ------------------------------- #

    def _unknown(
        self,
        reason: str,
        *,
        operation: str,
        token_count: int = 0,
        rejected: int = 0,
        truncated: bool = False,
    ) -> ParsedCapabilities:
        return ParsedCapabilities(
            engine=self.engine,
            operation=operation,
            parser=self.name,
            status=CapabilityStatus.UNKNOWN,
            token_count=token_count,
            rejected_count=rejected,
            truncated=truncated,
            reason=reason,
        )

    def _verified(self, formats: tuple[str, ...], *, operation: str) -> ParsedCapabilities:
        return ParsedCapabilities(
            engine=self.engine,
            operation=operation,
            parser=self.name,
            status=CapabilityStatus.VERIFIED,
            formats=formats,
            mode_names=formats,
            token_count=len(formats),
            reason=None,
        )

    def _oversized(self, output: object) -> bool:
        if not isinstance(output, str):
            return False
        return len(output.encode("utf-8", errors="replace")) > MAX_CAPABILITY_OUTPUT_BYTES

    def _header_present(self, output: str) -> bool:
        return any(line.strip() == self.required_header for line in output.splitlines())

    def _body_after_header(self, output: str) -> str:
        lines = output.splitlines()
        for index, line in enumerate(lines):
            if line.strip() == self.required_header:
                return "\n".join(lines[index + 1 :])
        return ""

    def _parse(self, output: str, *, operation: str) -> ParsedCapabilities:  # pragma: no cover
        raise NotImplementedError


class JohnFormatParser(FormatCapabilityParser):
    """Parses ``john --list=formats`` output."""

    name = "john-format-parser"
    engine = JOHN
    operation = "--list=formats"
    required_header = JOHN_FORMATS_HEADER

    def _parse(self, output: str, *, operation: str) -> ParsedCapabilities:
        if not isinstance(output, str):
            return self._unknown("invalid_output", operation=operation)
        if self._oversized(output):
            return self._unknown("oversized_output", operation=operation)
        if not self._header_present(output):
            return self._unknown("missing_header", operation=operation)

        body = self._body_after_header(output)
        raw_tokens = [t for t in normalize_format_names(body) if t]
        if not raw_tokens:
            return self._unknown("no_formats", operation=operation)

        partition = partition_tokens(raw_tokens)
        if partition.truncated:
            return self._unknown(
                "truncated_output",
                operation=operation,
                token_count=len(raw_tokens),
                rejected=len(partition.rejected),
                truncated=True,
            )
        if partition.invalid:
            return self._unknown(
                "invalid_tokens",
                operation=operation,
                token_count=len(raw_tokens),
                rejected=len(partition.rejected),
            )
        if not partition.accepted:
            return self._unknown("no_formats", operation=operation)
        return self._verified(partition.accepted, operation=operation)


class HashcatFormatParser(FormatCapabilityParser):
    """Parses hashcat mode-list output.

    Bound to :data:`MODE_LIST_OPERATION`, which is **not** on the discovery
    allowlist - this parser exists for completeness but is unreachable from
    the discovery pipeline.
    """

    name = "hashcat-mode-parser"
    engine = HASHCAT
    operation = MODE_LIST_OPERATION
    required_header = HASHCAT_MODES_HEADER

    def _parse(self, output: str, *, operation: str) -> ParsedCapabilities:
        if not isinstance(output, str):
            return self._unknown("invalid_output", operation=operation)
        if self._oversized(output):
            return self._unknown("oversized_output", operation=operation)
        if not self._header_present(output):
            return self._unknown("missing_header", operation=operation)

        body = self._body_after_header(output)
        designations: list[str] = []
        for line in body.splitlines():
            match = _MODE_LINE_RE.match(line)
            if not match:
                continue
            body_text = match.group("body")
            parts = [p.strip() for p in body_text.split("|") if p.strip()]
            if not parts:
                continue
            designations.append(parts[0])

        # Phrase-aware: designations are never split on whitespace/commas.
        phrases = normalize_mode_names(designations)
        if not phrases:
            return self._unknown("no_modes", operation=operation)

        partition = partition_tokens(phrases)
        if partition.truncated or partition.invalid:
            reason = "truncated_output" if partition.truncated else "invalid_tokens"
            return self._unknown(
                reason,
                operation=operation,
                token_count=len(phrases),
                rejected=len(partition.rejected),
                truncated=partition.truncated,
            )
        return self._verified(partition.accepted, operation=operation)


# --------------------------------------------------------------------------- #
# parser registry
# --------------------------------------------------------------------------- #


def _build_parsers() -> Mapping[tuple[str, str], FormatCapabilityParser]:
    parsers: dict[tuple[str, str], FormatCapabilityParser] = {}
    for parser in (JohnFormatParser(), HashcatFormatParser()):
        parsers[(parser.engine, parser.operation)] = parser
    return MappingProxyType(parsers)


_PARSERS: Mapping[tuple[str, str], FormatCapabilityParser] = _build_parsers()


def get_parser(engine: str, operation: str) -> FormatCapabilityParser | None:
    """Return the parser bound to ``engine`` + ``operation``, or ``None``."""

    if not isinstance(engine, str) or not isinstance(operation, str):
        return None
    return _PARSERS.get((engine, operation))


def parse_capabilities(engine: str, operation: str, output: str) -> ParsedCapabilities:
    """Parse output with the parser bound to ``engine`` + ``operation``."""

    parser = get_parser(engine, operation)
    if parser is None:
        raise CapabilityParseError(
            f"no capability parser is bound to engine={engine!r} operation={operation!r}"
        )
    return parser.parse(output, engine=engine, operation=operation)


def registered_parsers() -> tuple[FormatCapabilityParser, ...]:
    """All registered parsers, deterministic order."""

    return tuple(sorted(_PARSERS.values(), key=lambda p: p.name))
