from __future__ import annotations

import pytest

from aruba2930f_backup.models import CollectionFailure, DiagnosticDetail, ErrorCode
from aruba2930f_backup.validation import (
    ARUBA_2930F_MODELS,
    InputValidationError,
    contains_pager_marker,
    hostname_from_prompt,
    hostname_from_running_config,
    normalize_config_text,
    parse_ipv4_targets,
    require_valid_prompt,
    validate_cli_response,
    validate_device_identity,
    validate_output_limits,
)


def test_parse_ipv4_targets_preserves_order_and_ignores_blank_lines() -> None:
    targets = parse_ipv4_targets(" 192.0.2.10\n\n198.51.100.4  \n", port=2222)

    assert [(target.ip, target.port) for target in targets] == [
        ("192.0.2.10", 2222),
        ("198.51.100.4", 2222),
    ]


def test_parse_ipv4_targets_blocks_entire_batch_and_reports_all_issues() -> None:
    with pytest.raises(InputValidationError) as captured:
        parse_ipv4_targets("192.0.2.1\nnot-an-ip\n192.0.2.1\n2001:db8::1")

    assert len(captured.value.issues) == 3
    assert "Line 2" in captured.value.issues[0]
    assert "duplicate" in captured.value.issues[1]
    assert "Line 4" in captured.value.issues[2]


@pytest.mark.parametrize("text", ["", "\n  \n"])
def test_parse_ipv4_targets_requires_at_least_one_address(text: str) -> None:
    with pytest.raises(InputValidationError, match="at least one"):
        parse_ipv4_targets(text)


@pytest.mark.parametrize("port", [0, 65536])
def test_parse_ipv4_targets_rejects_invalid_port(port: int) -> None:
    with pytest.raises(InputValidationError, match="SSH port"):
        parse_ipv4_targets("192.0.2.1", port=port)


@pytest.mark.parametrize(("sku", "model"), ARUBA_2930F_MODELS.items())
def test_all_official_2930f_skus_are_recognized(sku: str, model: str) -> None:
    identity = validate_device_identity(
        f"Aruba {sku} 2930F Switch\nSoftware revision WC.16.11.0025"
    )

    assert identity.sku == sku
    assert identity.model == model
    assert identity.software_version == "WC.16.11.0025"


def test_modules_can_supply_sku_when_version_identifies_family() -> None:
    identity = validate_device_identity(
        "Aruba 2930F Switch\nSoftware revision WC.16.10.0012",
        "Chassis: 2930F-48G-PoE+-4SFP+ JL256A",
    )

    assert identity.sku == "JL256A"


def test_modules_can_supply_jl255a_when_version_reports_only_software() -> None:
    identity = validate_device_identity(
        "Image stamp:\nAug 20 2026\nWC.16.10.0024\nBoot Image: Primary",
        "Status and Counters - Module Information\nChassis: Aruba 2930F-24G-PoE+-4SFP+ JL255A",
    )

    assert identity.model == "Aruba 2930F 24G PoE+ 4SFP+"
    assert identity.sku == "JL255A"
    assert identity.software_version == "WC.16.10.0024"


def test_show_version_exact_evidence_does_not_require_modules() -> None:
    identity = validate_device_identity("Aruba JL258A 2930F Switch")

    assert identity.sku == "JL258A"


@pytest.mark.parametrize(
    ("version", "modules", "expected_model"),
    [
        ("Aruba 2930F Switch", "", "Aruba 2930F"),
        ("ArubaOS-Switch WC.16.11.0025", "Chassis: Aruba 2930F", "Aruba 2930F"),
    ],
)
def test_family_marker_without_sku_is_accepted(
    version: str,
    modules: str,
    expected_model: str,
) -> None:
    identity = validate_device_identity(version, modules)

    assert identity.model == expected_model
    assert identity.sku is None


def test_mixed_official_skus_are_reported_as_vsf() -> None:
    identity = validate_device_identity(
        "ArubaOS-Switch WC.16.10.0024",
        "Aruba JL255A 2930F-24G-PoE+-4SFP+ Switch\n"
        "Aruba JL253A 2930F-24G-4SFP+ Switch\n"
        "Aruba JL256A 2930F-48G-PoE+-4SFP+ Switch",
    )

    assert identity.model == "Aruba 2930F VSF"
    assert identity.sku == "JL253A, JL255A, JL256A"


def test_module_and_transceiver_part_numbers_are_not_chassis_conflicts() -> None:
    identity = validate_device_identity(
        "ArubaOS-Switch WC.16.10.0024",
        "Chassis: Aruba 2930F-24G-PoE+-4SFP+ JL255A\n"
        "A JL083A 4-port SFP+ expansion module\n"
        "1 J9150D SFP+ transceiver",
    )

    assert identity.sku == "JL255A"


@pytest.mark.parametrize(
    ("version", "modules", "detail"),
    [
        ("", "Chassis: Aruba 2930F JL255A", DiagnosticDetail.IDENTITY_EVIDENCE_MISSING),
        (
            "ArubaOS-Switch WC.16.11.0025",
            "",
            DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
        ),
        ("Aruba 2930F JL999A", "", DiagnosticDetail.IDENTITY_SKU_UNSUPPORTED),
        (
            "Aruba 2930F JL999A",
            "Chassis: Aruba 2930F JL253A",
            DiagnosticDetail.IDENTITY_SKU_CONFLICT,
        ),
        ("Aruba 2930M JL253A", "", DiagnosticDetail.IDENTITY_FAMILY_CONFLICT),
        (
            "Aruba CX 6200F JL724A",
            "Chassis: Aruba 2930F JL255A",
            DiagnosticDetail.IDENTITY_FAMILY_CONFLICT,
        ),
    ],
)
def test_unsupported_identity_evidence_has_stable_diagnostic_detail(
    version: str,
    modules: str,
    detail: DiagnosticDetail,
) -> None:
    with pytest.raises(CollectionFailure) as captured:
        validate_device_identity(version, modules)

    assert captured.value.code is ErrorCode.MODEL_UNSUPPORTED
    assert captured.value.transient is False
    assert captured.value.diagnostic_detail is detail


def test_cli_error_is_sanitized_and_rejected() -> None:
    with pytest.raises(CollectionFailure) as captured:
        validate_cli_response("show modules", "% Invalid input: modules")

    assert captured.value.code is ErrorCode.COMMAND_REJECTED
    assert "Invalid input" not in captured.value.safe_message


def test_pager_markers_and_windows_config_normalization() -> None:
    marker = "-- MORE --, next page: Space, next line: Enter, quit: Control-C"
    assert contains_pager_marker(f"output\n{marker}")
    with pytest.raises(CollectionFailure) as captured:
        normalize_config_text(f"hostname lab\n{marker}\n")
    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED
    assert normalize_config_text("hostname lab\nvlan 1\n") == "hostname lab\r\nvlan 1\r\n"


def test_empty_running_config_is_rejected() -> None:
    with pytest.raises(CollectionFailure) as captured:
        normalize_config_text("\r\n  \r\n")

    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED


def test_pager_words_embedded_in_config_are_not_control_markers() -> None:
    config = 'banner motd "Press any key to continue"\n; literal -- MORE -- text must remain\n'

    assert not contains_pager_marker(config)
    assert "Press any key" in normalize_config_text(config)
    assert "-- MORE --" in normalize_config_text(config)


def test_output_bounds_check_bytes_and_lines() -> None:
    validate_output_limits("a\nb", max_bytes=10, max_lines=2)
    with pytest.raises(CollectionFailure) as bytes_error:
        validate_output_limits("한글", max_bytes=5, max_lines=10)
    assert bytes_error.value.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    with pytest.raises(CollectionFailure):
        validate_output_limits("a\nb\nc", max_bytes=100, max_lines=2)


def test_prompt_hostname_requires_single_exec_prompt() -> None:
    assert hostname_from_prompt("edge-lab#") == "edge-lab"
    assert hostname_from_prompt("edge-lab>") == "edge-lab"
    assert hostname_from_prompt("edge-lab(config)#") is None
    assert hostname_from_prompt("bad prompt#") is None
    assert require_valid_prompt("bad prompt#") is None
    assert require_valid_prompt("(Aruba 2930F) #") is None
    assert require_valid_prompt("Aruba-2930F-48G-PoE+ #") is None
    with pytest.raises(CollectionFailure) as captured:
        require_valid_prompt("not a prompt")
    assert captured.value.code is ErrorCode.PROMPT_PARSE_FAILED


@pytest.mark.parametrize(
    ("config_text", "expected"),
    (
        ('Running configuration:\r\nhostname "edge-lab"\r\n', "edge-lab"),
        ("hostname edge-lab\nvlan 1\n", "edge-lab"),
        ('hostname "floor 3 edge"\n', "floor 3 edge"),
        ('\x1b[32mhostname "edge-lab"\x1b[0m\n', "edge-lab"),
        ('hostname "edge-lab"\nhostname "edge-lab"\n', "edge-lab"),
    ),
)
def test_running_config_hostname_accepts_unambiguous_top_level_forms(
    config_text: str,
    expected: str,
) -> None:
    assert hostname_from_running_config(config_text) == expected


@pytest.mark.parametrize(
    "config_text",
    (
        "no hostname edge-lab\n",
        " hostname nested-value\n",
        'hostname "unterminated\n',
        "hostname unquoted value\n",
        'hostname ""\n',
        'hostname "bad\tvalue"\n',
        f'hostname "{"x" * 256}"\n',
        'hostname "edge-one"\nhostname "edge-two"\n',
    ),
)
def test_running_config_hostname_ignores_missing_malformed_or_ambiguous_values(
    config_text: str,
) -> None:
    assert hostname_from_running_config(config_text) is None


@pytest.mark.parametrize(
    ("prompt", "detail"),
    (
        ("", DiagnosticDetail.PROMPT_EMPTY),
        ("edge-lab(config)#", DiagnosticDetail.PROMPT_NON_EXEC_MODE),
        ("edge-lab(vlan-10)#", DiagnosticDetail.PROMPT_NON_EXEC_MODE),
        ("bad prompt", DiagnosticDetail.PROMPT_FORMAT),
        ("bad\nprompt#", DiagnosticDetail.PROMPT_FORMAT),
        ("bad\x00prompt#", DiagnosticDetail.PROMPT_FORMAT),
        ("x" * 256 + "#", DiagnosticDetail.PROMPT_FORMAT),
    ),
)
def test_prompt_failure_details_are_stable(prompt: str, detail: DiagnosticDetail) -> None:
    with pytest.raises(CollectionFailure) as captured:
        require_valid_prompt(prompt)

    assert captured.value.diagnostic_detail is detail
