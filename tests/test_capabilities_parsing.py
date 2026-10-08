"""Phase 2B.5 - verified capability parsing hardening."""

from __future__ import annotations

import pytest

from app.engines.discovery.allowlist import DISCOVERY_ALLOWLIST
from app.engines.discovery.capabilities import (
    HASHCAT_MODES_HEADER,
    JOHN_FORMATS_HEADER,
    MAX_CAPABILITY_OUTPUT_BYTES,
    MODE_LIST_OPERATION,
    TRUNCATION_MARKERS,
    FormatCapabilityParser,
    HashcatFormatParser,
    JohnFormatParser,
    ParsedCapabilities,
    get_parser,
    is_truncation_marker_token,
    normalize_format_names,
    normalize_mode_names,
    parse_capabilities,
    partition_tokens,
    registered_parsers,
)
from app.engines.discovery.errors import CapabilityParseError, ForeignOutputError
from app.engines.discovery.models import HASHCAT, JOHN, CapabilityStatus

VERIFIED = CapabilityStatus.VERIFIED
UNKNOWN = CapabilityStatus.UNKNOWN

GOOD_JOHN_OUTPUT = (
    f"{JOHN_FORMATS_HEADER}\n"
    "  md5crypt, bcrypt, sha512crypt, sha256crypt,\n"
    "  ntlm, raw-md5, raw-sha1, raw-sha256,\n"
    "  zip, pkzip, pdf, office2007, office2010, office2013\n"
)

GOOD_HASHCAT_OUTPUT = (
    f"{HASHCAT_MODES_HEADER}\n"
    "     0 | MD5 | Raw Hash\n"
    "   900 | MD4 | Raw Hash\n"
    "  1000 | NTLM | Raw Hash\n"
    " 15600 | Ethereum Wallet, Password Protected\n"
)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #


def test_normalize_format_names_splits_on_commas_and_whitespace() -> None:
    assert normalize_format_names("md5crypt, bcrypt   sha512crypt") == (
        "md5crypt",
        "bcrypt",
        "sha512crypt",
    )


def test_normalize_format_names_splits_newlines_and_semicolons() -> None:
    assert normalize_format_names("a\nb;c") == ("a", "b", "c")


def test_normalize_format_names_lowercases() -> None:
    assert normalize_format_names("MD5Crypt") == ("md5crypt",)


def test_normalize_format_names_deduplicates_preserving_order() -> None:
    assert normalize_format_names("b, a, b, c") == ("b", "a", "c")


def test_normalize_format_names_handles_none_and_empty() -> None:
    assert normalize_format_names(None) == ()
    assert normalize_format_names("") == ()
    assert normalize_format_names("  , ; ") == ()


def test_normalize_format_names_skips_non_string_items() -> None:
    assert normalize_format_names(["ok", None, 5, ["x"]]) == ("ok",)


def test_normalize_format_names_accepts_iterable_input() -> None:
    assert normalize_format_names(["a", "b"]) == ("a", "b")


def test_normalize_mode_names_is_phrase_aware_and_never_splits() -> None:
    result = normalize_mode_names("Ethereum Wallet, Password Protected")
    assert result == ("ethereum_wallet_password_protected",)
    assert len(result) == 1


def test_normalize_mode_names_keeps_multiword_phrases_intact() -> None:
    result = normalize_mode_names(["Ethereum Wallet, Password Protected", "Raw Hash"])
    assert result == ("ethereum_wallet_password_protected", "raw_hash")


def test_normalize_mode_names_splits_from_format_normalisation() -> None:
    phrase = "Ethereum Wallet, Password Protected"
    assert len(normalize_mode_names(phrase)) == 1
    assert len(normalize_format_names(phrase)) == 4


def test_normalize_mode_names_lowercases_and_underscores_spaces() -> None:
    assert normalize_mode_names("SHA-256 Salted Phrase") == ("sha-256_salted_phrase",)


def test_normalize_mode_names_deduplicates() -> None:
    assert normalize_mode_names(["MD5", "MD5", "md5"]) == ("md5",)


def test_normalize_mode_names_handles_none_and_empty() -> None:
    assert normalize_mode_names(None) == ()
    assert normalize_mode_names("") == ()
    assert normalize_mode_names("   ") == ()


def test_normalize_mode_names_skips_non_strings() -> None:
    assert normalize_mode_names([None, 7, "NTLM"]) == ("ntlm",)


# --------------------------------------------------------------------------- #
# truncation markers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("marker", TRUNCATION_MARKERS)
def test_truncation_markers_detected(marker: str) -> None:
    assert is_truncation_marker_token(f"bcrypt{marker}") is True


@pytest.mark.parametrize("token", ["md5crypt", "raw-md5", "office2007", "pkzip"])
def test_normal_tokens_are_not_truncation_markers(token: str) -> None:
    assert is_truncation_marker_token(token) is False


def test_empty_token_is_not_a_marker() -> None:
    assert is_truncation_marker_token("") is False


@pytest.mark.parametrize("marker", TRUNCATION_MARKERS)
def test_partition_rejects_truncation_marker_tokens(marker: str) -> None:
    partition = partition_tokens(["md5crypt", f"bcrypt{marker}"])
    assert partition.accepted == ("md5crypt",)
    assert partition.rejected == (f"bcrypt{marker}",)
    assert partition.truncated is True
    assert partition.invalid is False


def test_partition_flags_invalid_charset_tokens() -> None:
    partition = partition_tokens(["md5crypt", "md5<script>"])
    assert partition.invalid is True
    assert partition.truncated is False
    assert partition.rejected == ("md5<script>",)


def test_partition_accepts_good_tokens() -> None:
    partition = partition_tokens(["md5crypt", "raw-md5", "office2010"])
    assert partition.accepted == ("md5crypt", "raw-md5", "office2010")
    assert partition.rejected == ()
    assert partition.truncated is False
    assert partition.invalid is False


def test_partition_rejects_token_starting_with_dash() -> None:
    partition = partition_tokens(["-md5"])
    assert partition.accepted == ()
    assert partition.invalid is True


# --------------------------------------------------------------------------- #
# provenance binding
# --------------------------------------------------------------------------- #


def test_john_parser_provenance() -> None:
    parser = JohnFormatParser()
    assert parser.engine == JOHN
    assert parser.operation == "--list=formats"
    assert parser.name == "john-format-parser"
    assert parser.provenance.engine == JOHN
    assert parser.provenance.operation == "--list=formats"


def test_hashcat_parser_provenance() -> None:
    parser = HashcatFormatParser()
    assert parser.engine == HASHCAT
    assert parser.operation == MODE_LIST_OPERATION
    assert parser.provenance.parser == "hashcat-mode-parser"


def test_john_parser_accepts_matching_provenance() -> None:
    parsed = JohnFormatParser().parse(
        GOOD_JOHN_OUTPUT, engine=JOHN, operation="--list=formats"
    )
    assert parsed.status is VERIFIED


def test_john_parser_rejects_foreign_engine() -> None:
    with pytest.raises(ForeignOutputError):
        JohnFormatParser().parse(GOOD_JOHN_OUTPUT, engine=HASHCAT,
                                 operation="--list=formats")


def test_john_parser_rejects_foreign_operation() -> None:
    with pytest.raises(ForeignOutputError):
        JohnFormatParser().parse(GOOD_JOHN_OUTPUT, engine=JOHN, operation="--version")


def test_hashcat_parser_rejects_john_operation() -> None:
    with pytest.raises(ForeignOutputError):
        HashcatFormatParser().parse(GOOD_HASHCAT_OUTPUT, engine=HASHCAT,
                                    operation="--list=formats")


def test_hashcat_parser_rejects_foreign_engine() -> None:
    with pytest.raises(ForeignOutputError):
        HashcatFormatParser().parse(GOOD_HASHCAT_OUTPUT, engine=JOHN,
                                    operation=MODE_LIST_OPERATION)


def test_foreign_output_error_carries_context() -> None:
    with pytest.raises(ForeignOutputError) as excinfo:
        JohnFormatParser().parse(GOOD_JOHN_OUTPUT, engine=HASHCAT,
                                 operation="--list=formats")
    assert excinfo.value.engine == HASHCAT
    assert excinfo.value.parser == "john-format-parser"


def test_get_parser_bindings() -> None:
    assert isinstance(get_parser(JOHN, "--list=formats"), JohnFormatParser)
    assert isinstance(get_parser(HASHCAT, MODE_LIST_OPERATION), HashcatFormatParser)


def test_get_parser_rejects_foreign_operation() -> None:
    assert get_parser(JOHN, MODE_LIST_OPERATION) is None
    assert get_parser(HASHCAT, "--list=formats") is None


def test_get_parser_rejects_unbound_operations() -> None:
    assert get_parser(JOHN, "--version") is None
    assert get_parser(JOHN, "--help") is None
    assert get_parser(HASHCAT, "--devices") is None
    assert get_parser(HASHCAT, "--backend-info") is None


def test_get_parser_rejects_non_strings() -> None:
    assert get_parser(JOHN, None) is None  # type: ignore[arg-type]
    assert get_parser(None, "--version") is None  # type: ignore[arg-type]


def test_parse_capabilities_requires_a_bound_parser() -> None:
    with pytest.raises(CapabilityParseError):
        parse_capabilities(JOHN, "--version", "anything")


def test_parse_capabilities_returns_parsed_result() -> None:
    parsed = parse_capabilities(JOHN, "--list=formats", GOOD_JOHN_OUTPUT)
    assert parsed.status is VERIFIED
    assert parsed.provenance.parser == "john-format-parser"


def test_parser_registry_is_small_and_deterministic() -> None:
    parsers = registered_parsers()
    assert len(parsers) == 2
    assert {p.name for p in parsers} == {"hashcat-mode-parser", "john-format-parser"}


def test_base_parser_is_abstract() -> None:
    with pytest.raises(NotImplementedError):
        FormatCapabilityParser()._parse("x", operation="y")


def test_base_parser_never_matches_anything() -> None:
    parser = FormatCapabilityParser()
    assert parser.matches(JOHN, "--list=formats") is False
    assert parser.engine == ""


def test_mode_list_operation_is_not_allowlisted() -> None:
    assert DISCOVERY_ALLOWLIST.allows(HASHCAT, MODE_LIST_OPERATION) is False


# --------------------------------------------------------------------------- #
# parse outcomes - John
# --------------------------------------------------------------------------- #


def test_john_happy_path_is_verified() -> None:
    parsed = parse_capabilities(JOHN, "--list=formats", GOOD_JOHN_OUTPUT)
    assert parsed.status is VERIFIED
    assert parsed.verified is True
    assert "md5crypt" in parsed.formats
    assert parsed.reason is None
    assert parsed.token_count == len(parsed.formats)


def test_john_formats_are_normalised() -> None:
    output = f"{JOHN_FORMATS_HEADER}\n  MD5Crypt, BCRYPT\n"
    parsed = parse_capabilities(JOHN, "--list=formats", output)
    assert parsed.formats == ("md5crypt", "bcrypt")


def test_john_missing_header_is_unknown() -> None:
    parsed = parse_capabilities(JOHN, "--list=formats", "md5crypt, bcrypt\n")
    assert parsed.status is UNKNOWN
    assert parsed.reason == "missing_header"
    assert parsed.formats == ()


def test_john_header_only_is_unknown() -> None:
    parsed = parse_capabilities(JOHN, "--list=formats", f"{JOHN_FORMATS_HEADER}\n")
    assert parsed.status is UNKNOWN
    assert parsed.reason == "no_formats"


def test_john_header_with_blank_body_is_unknown() -> None:
    parsed = parse_capabilities(JOHN, "--list=formats", f"{JOHN_FORMATS_HEADER}\n\n  \n")
    assert parsed.status is UNKNOWN
    assert parsed.reason == "no_formats"


def test_john_oversized_output_is_unknown() -> None:
    output = f"{JOHN_FORMATS_HEADER}\n" + ("a" * (MAX_CAPABILITY_OUTPUT_BYTES + 100))
    parsed = parse_capabilities(JOHN, "--list=formats", output)
    assert parsed.status is UNKNOWN
    assert parsed.reason == "oversized_output"


def test_john_output_just_below_limit_is_parsed() -> None:
    padding = "x" * (MAX_CAPABILITY_OUTPUT_BYTES - len(GOOD_JOHN_OUTPUT) - 10)
    output = f"{JOHN_FORMATS_HEADER}\n  md5crypt\n" + padding + "\n"
    assert len(output.encode("utf-8")) < MAX_CAPABILITY_OUTPUT_BYTES
    parsed = parse_capabilities(JOHN, "--list=formats", output)
    assert parsed.status is VERIFIED


@pytest.mark.parametrize("marker", TRUNCATION_MARKERS)
def test_john_truncation_marker_forces_unknown(marker: str) -> None:
    output = f"{JOHN_FORMATS_HEADER}\n  md5crypt, bcrypt{marker}\n"
    parsed = parse_capabilities(JOHN, "--list=formats", output)
    assert parsed.status is UNKNOWN
    assert parsed.reason == "truncated_output"
    assert parsed.truncated is True
    assert parsed.formats == ()
    assert parsed.rejected_count == 1


def test_john_invalid_token_forces_unknown() -> None:
    output = f"{JOHN_FORMATS_HEADER}\n  md5crypt, <script>\n"
    parsed = parse_capabilities(JOHN, "--list=formats", output)
    assert parsed.status is UNKNOWN
    assert parsed.reason == "invalid_tokens"
    assert parsed.formats == ()


def test_john_non_string_output_is_unknown() -> None:
    parsed = JohnFormatParser().parse(None, engine=JOHN, operation="--list=formats")  # type: ignore[arg-type]
    assert parsed.status is UNKNOWN
    assert parsed.reason == "invalid_output"


def test_john_header_may_be_preceded_by_a_banner() -> None:
    output = f"banner line\nanother banner\n{GOOD_JOHN_OUTPUT}"
    parsed = parse_capabilities(JOHN, "--list=formats", output)
    assert parsed.status is VERIFIED
    assert "zip" in parsed.formats


def test_john_no_partial_formats_on_unknown() -> None:
    output = f"{JOHN_FORMATS_HEADER}\n  md5crypt, bad_token!\n"
    parsed = parse_capabilities(JOHN, "--list=formats", output)
    assert parsed.formats == ()
    assert parsed.mode_names == ()


# --------------------------------------------------------------------------- #
# parse outcomes - Hashcat
# --------------------------------------------------------------------------- #


def test_hashcat_happy_path_is_verified_with_phrases() -> None:
    parsed = parse_capabilities(HASHCAT, MODE_LIST_OPERATION, GOOD_HASHCAT_OUTPUT)
    assert parsed.status is VERIFIED
    assert "md5" in parsed.formats
    assert "ethereum_wallet_password_protected" in parsed.formats
    assert parsed.formats == parsed.mode_names


def test_hashcat_missing_header_is_unknown() -> None:
    parsed = parse_capabilities(HASHCAT, MODE_LIST_OPERATION, "  0 | MD5 | Raw Hash\n")
    assert parsed.status is UNKNOWN
    assert parsed.reason == "missing_header"


def test_hashcat_oversized_output_is_unknown() -> None:
    output = f"{HASHCAT_MODES_HEADER}\n" + ("b" * (MAX_CAPABILITY_OUTPUT_BYTES + 10))
    parsed = parse_capabilities(HASHCAT, MODE_LIST_OPERATION, output)
    assert parsed.status is UNKNOWN
    assert parsed.reason == "oversized_output"


def test_hashcat_header_only_is_unknown() -> None:
    parsed = parse_capabilities(HASHCAT, MODE_LIST_OPERATION, f"{HASHCAT_MODES_HEADER}\n")
    assert parsed.status is UNKNOWN
    assert parsed.reason == "no_modes"


def test_hashcat_truncated_phrase_is_unknown() -> None:
    output = f"{HASHCAT_MODES_HEADER}\n     0 | MD5_\n"
    parsed = parse_capabilities(HASHCAT, MODE_LIST_OPERATION, output)
    assert parsed.status is UNKNOWN
    assert parsed.reason == "truncated_output"


# --------------------------------------------------------------------------- #
# parsed result shape
# --------------------------------------------------------------------------- #


def test_parsed_capabilities_has_no_raw_output_field() -> None:
    fields = {f.name for f in ParsedCapabilities.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert not fields & {"stdout", "stderr", "raw_output", "output", "probe_output"}


def test_parsed_capabilities_is_frozen() -> None:
    parsed = parse_capabilities(JOHN, "--list=formats", GOOD_JOHN_OUTPUT)
    with pytest.raises(Exception):
        parsed.status = UNKNOWN  # type: ignore[misc]


def test_parsed_provenance_is_bound_to_engine_and_operation() -> None:
    parsed = parse_capabilities(JOHN, "--list=formats", GOOD_JOHN_OUTPUT)
    assert parsed.provenance == parsed.provenance
    assert parsed.provenance.engine == JOHN
    assert parsed.provenance.operation == "--list=formats"
    assert parsed.provenance.parser == "john-format-parser"
