from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import aruba2930f_backup.diagnostics as diagnostic_module
from aruba2930f_backup.diagnostics import (
    DiagnosticStatus,
    decode_diagnostic_code,
    diagnostic_code_for_exception,
    diagnostic_code_for_result,
    diagnostic_detail_for_exception,
    encode_diagnostic_code,
    main,
)
from aruba2930f_backup.models import (
    CollectionFailure,
    DeviceResult,
    DeviceStatus,
    DeviceTarget,
    DiagnosticDetail,
    DiagnosticPhase,
    ErrorCode,
)


def test_golden_diagnostic_code_and_round_trip() -> None:
    code = encode_diagnostic_code(
        version="0.1.3",
        phase=DiagnosticPhase.CONFIG_COLLECTION,
        error_code=ErrorCode.PROMPT_PARSE_FAILED,
        status=DiagnosticStatus.RETRY_EXHAUSTED,
        host_key_attempts=1,
        backup_attempts=4,
        detail=DiagnosticDetail.PROMPT_FORMAT,
    )

    assert code == "A3F1-010EPMRC-3"
    assert len(code) == 15
    decoded = decode_diagnostic_code(code)
    assert decoded.version == "0.1.3"
    assert decoded.phase is DiagnosticPhase.CONFIG_COLLECTION
    assert decoded.error_code is ErrorCode.PROMPT_PARSE_FAILED
    assert decoded.status is DiagnosticStatus.RETRY_EXHAUSTED
    assert decoded.host_key_attempts == 1
    assert decoded.backup_attempts == 4
    assert decoded.detail is DiagnosticDetail.PROMPT_FORMAT


def test_previous_release_diagnostic_code_golden_vector() -> None:
    code = encode_diagnostic_code(
        version="0.1.4",
        phase=DiagnosticPhase.CONFIG_COLLECTION,
        error_code=ErrorCode.PROMPT_PARSE_FAILED,
        status=DiagnosticStatus.RETRY_EXHAUSTED,
        host_key_attempts=1,
        backup_attempts=4,
        detail=DiagnosticDetail.PROMPT_FORMAT,
    )

    assert code == "A3F1-010JPMRC-T"
    assert decode_diagnostic_code(code).version == "0.1.4"


def test_v015_diagnostic_code_golden_vector() -> None:
    code = encode_diagnostic_code(
        version="0.1.5",
        phase=DiagnosticPhase.CONFIG_COLLECTION,
        error_code=ErrorCode.PROMPT_PARSE_FAILED,
        status=DiagnosticStatus.RETRY_EXHAUSTED,
        host_key_attempts=1,
        backup_attempts=4,
        detail=DiagnosticDetail.PROMPT_FORMAT,
    )

    assert code == "A3F1-010PPMRC-C"
    assert decode_diagnostic_code(code).version == "0.1.5"


def test_v016_identity_diagnostic_code_golden_vector() -> None:
    code = encode_diagnostic_code(
        version="0.1.6",
        phase=DiagnosticPhase.DEVICE_IDENTITY,
        error_code=ErrorCode.MODEL_UNSUPPORTED,
        status=DiagnosticStatus.FAILED,
        host_key_attempts=1,
        backup_attempts=1,
        detail=DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
    )

    assert code == "A3F1-010T50KG-B"
    decoded = decode_diagnostic_code(code)
    assert decoded.version == "0.1.6"
    assert decoded.detail is DiagnosticDetail.IDENTITY_EVIDENCE_MISSING


def test_v017_identity_diagnostic_code_golden_vector() -> None:
    code = encode_diagnostic_code(
        version="0.1.7",
        phase=DiagnosticPhase.DEVICE_IDENTITY,
        error_code=ErrorCode.MODEL_UNSUPPORTED,
        status=DiagnosticStatus.FAILED,
        host_key_attempts=1,
        backup_attempts=1,
        detail=DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
    )

    assert code == "A3F1-010Y50KG-X"
    decoded = decode_diagnostic_code(code)
    assert decoded.version == "0.1.7"
    assert decoded.detail is DiagnosticDetail.IDENTITY_EVIDENCE_MISSING


def test_current_release_identity_diagnostic_code_golden_vector() -> None:
    code = encode_diagnostic_code(
        version="0.1.8",
        phase=DiagnosticPhase.DEVICE_IDENTITY,
        error_code=ErrorCode.MODEL_UNSUPPORTED,
        status=DiagnosticStatus.FAILED,
        host_key_attempts=1,
        backup_attempts=1,
        detail=DiagnosticDetail.IDENTITY_EVIDENCE_MISSING,
    )

    assert code == "A3F1-011250KG-G"
    decoded = decode_diagnostic_code(code)
    assert decoded.version == "0.1.8"
    assert decoded.detail is DiagnosticDetail.IDENTITY_EVIDENCE_MISSING


def test_reported_v013_session_prompt_code_decodes_without_sensitive_context() -> None:
    decoded = decode_diagnostic_code("A3F1-010DPMRC-S")

    assert decoded.version == "0.1.3"
    assert decoded.phase is DiagnosticPhase.SESSION_SETUP
    assert decoded.error_code is ErrorCode.PROMPT_PARSE_FAILED
    assert decoded.status is DiagnosticStatus.RETRY_EXHAUSTED
    assert decoded.host_key_attempts == 1
    assert decoded.backup_attempts == 4
    assert decoded.detail is DiagnosticDetail.PROMPT_FORMAT


def test_decoder_accepts_lowercase_and_crockford_typo_aliases() -> None:
    for code in ("a3f1-OLOEPMRC-3", "A3F1-0I0EPMRC-3"):
        assert decode_diagnostic_code(code).code == "A3F1-010EPMRC-3"


@pytest.mark.parametrize(
    "code",
    (
        "B3F1-010EPMRC-3",
        "A3F1-010EPMRC-4",
        "A3F1-010EPMR-3",
        "A3F1-010EPMRU-3",
    ),
)
def test_decoder_rejects_invalid_schema_checksum_length_and_characters(code: str) -> None:
    with pytest.raises(ValueError):
        decode_diagnostic_code(code)


def test_every_error_identifier_is_stable_and_round_trips() -> None:
    assert {
        error: identifier for identifier, error in enumerate(ErrorCode, start=1)
    } == diagnostic_module._ERROR_TO_ID
    codes = {
        error: encode_diagnostic_code(
            version="0.1.3",
            phase=DiagnosticPhase.UNKNOWN,
            error_code=error,
            status=DiagnosticStatus.FAILED,
        )
        for error in ErrorCode
    }

    assert len(set(codes.values())) == len(ErrorCode)
    assert all(decode_diagnostic_code(code).error_code is error for error, code in codes.items())


def test_identity_detail_identifiers_fill_the_reserved_schema_values() -> None:
    assert {
        DiagnosticDetail.IDENTITY_EVIDENCE_MISSING: 12,
        DiagnosticDetail.IDENTITY_SKU_UNSUPPORTED: 13,
        DiagnosticDetail.IDENTITY_FAMILY_CONFLICT: 14,
        DiagnosticDetail.IDENTITY_SKU_CONFLICT: 15,
    }.items() <= diagnostic_module._DETAIL_TO_ID.items()


@pytest.mark.parametrize("phase", tuple(DiagnosticPhase))
@pytest.mark.parametrize("detail", tuple(DiagnosticDetail))
def test_every_phase_and_detail_round_trips(
    phase: DiagnosticPhase,
    detail: DiagnosticDetail,
) -> None:
    code = encode_diagnostic_code(
        version="0.1.3",
        phase=phase,
        error_code=ErrorCode.UNEXPECTED_ERROR,
        status=DiagnosticStatus.FAILED,
        detail=detail,
    )

    decoded = decode_diagnostic_code(code)
    assert decoded.phase is phase
    assert decoded.detail is detail


@pytest.mark.parametrize("attempts", range(8))
def test_every_attempt_count_round_trips(attempts: int) -> None:
    code = encode_diagnostic_code(
        version="0.1.3",
        phase=DiagnosticPhase.UNKNOWN,
        error_code=None,
        status=DiagnosticStatus.FAILED,
        host_key_attempts=attempts,
        backup_attempts=7 - attempts,
    )

    decoded = decode_diagnostic_code(code)
    assert decoded.host_key_attempts == attempts
    assert decoded.backup_attempts == 7 - attempts


@pytest.mark.parametrize("status", tuple(DiagnosticStatus))
def test_every_diagnostic_status_round_trips(status: DiagnosticStatus) -> None:
    code = encode_diagnostic_code(
        version="0.1.3",
        phase=DiagnosticPhase.APP,
        error_code=ErrorCode.UNEXPECTED_ERROR,
        status=status,
    )

    assert decode_diagnostic_code(code).status is status


def _code_with_payload(payload: int) -> str:
    alphabet = diagnostic_module._CROCKFORD_ALPHABET
    payload_bytes = payload.to_bytes(5, "big")
    payload_text = "".join(alphabet[(payload >> shift) & 0x1F] for shift in range(35, -1, -5))
    check = alphabet[diagnostic_module._crc5_epc(b"A3F1" + payload_bytes)]
    return f"A3F1-{payload_text}-{check}"


def test_decoder_rejects_nonzero_reserved_payload_bits_with_valid_checksum() -> None:
    payload = 0
    for character in "010EPMRC":
        payload = (payload << 5) | diagnostic_module._CROCKFORD_VALUES[character]

    with pytest.raises(ValueError, match="reserved schema bits"):
        decode_diagnostic_code(_code_with_payload(payload | 0b01))


@pytest.mark.parametrize(
    ("exc", "detail"),
    (
        (ImportError(), DiagnosticDetail.IMPORT_ERROR),
        (OSError(), DiagnosticDetail.OS_ERROR),
        (ValueError(), DiagnosticDetail.VALUE_OR_TYPE_ERROR),
        (TypeError(), DiagnosticDetail.VALUE_OR_TYPE_ERROR),
        (RuntimeError(), DiagnosticDetail.RUNTIME_ERROR),
        (MemoryError(), DiagnosticDetail.MEMORY_ERROR),
    ),
)
def test_fatal_exception_categories_are_stable(
    exc: BaseException,
    detail: DiagnosticDetail,
) -> None:
    assert diagnostic_detail_for_exception(exc) is detail


def _failed_result(target: str, message: str) -> DeviceResult:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    return DeviceResult(
        target=DeviceTarget(target),
        status=DeviceStatus.RETRY_EXHAUSTED,
        attempts=4,
        host_key_attempts=1,
        started_at=now,
        finished_at=now,
        duration_seconds=0.0,
        hostname=message,
        error_code=ErrorCode.PROMPT_PARSE_FAILED,
        error_message=message,
        failure_phase=DiagnosticPhase.CONFIG_COLLECTION,
        diagnostic_detail=DiagnosticDetail.PROMPT_FORMAT,
    )


def test_result_code_is_independent_of_identifiers_and_messages() -> None:
    first = _failed_result("192.0.2.10", "operator secret edge-a")
    second = _failed_result("198.51.100.20", "different account and hostname")
    first.model = "Aruba 2930F 24G PoE+ 4SFP+"
    first.sku = "JL255A"
    second.model = "Aruba 2930F VSF"
    second.sku = "JL253A, JL256A"

    assert diagnostic_code_for_result(first, version="0.1.3") == diagnostic_code_for_result(
        second, version="0.1.3"
    )
    code = diagnostic_code_for_result(first, version="0.1.3")
    assert code is not None
    assert "192.0.2.10" not in code
    assert "operator" not in code
    assert "secret" not in code
    assert "JL255A" not in code


def test_success_and_expected_cancellation_have_no_code() -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    for status in (DeviceStatus.SUCCESS, DeviceStatus.CANCELLED):
        result = DeviceResult(
            target=DeviceTarget("192.0.2.10"),
            status=status,
            attempts=1,
            started_at=now,
            finished_at=now,
            duration_seconds=0.0,
        )
        assert diagnostic_code_for_result(result, version="0.1.3") is None


def test_collection_failure_preserves_report_phase_in_fatal_code() -> None:
    failure = CollectionFailure(
        ErrorCode.REPORT_WRITE_FAILED,
        "sensitive path is deliberately excluded",
        diagnostic_phase=DiagnosticPhase.REPORT_STORAGE,
        diagnostic_detail=DiagnosticDetail.OS_ERROR,
    )

    decoded = decode_diagnostic_code(diagnostic_code_for_exception(failure, version="0.1.3"))
    assert decoded.status is DiagnosticStatus.FATAL_APP
    assert decoded.phase is DiagnosticPhase.REPORT_STORAGE
    assert decoded.error_code is ErrorCode.REPORT_WRITE_FAILED
    assert decoded.detail is DiagnosticDetail.OS_ERROR


def test_decoder_cli_supports_multiple_json_codes(capsys: pytest.CaptureFixture[str]) -> None:
    first = encode_diagnostic_code(
        version="0.1.3",
        phase=DiagnosticPhase.HOST_KEY,
        error_code=ErrorCode.TCP_TIMEOUT,
        status=DiagnosticStatus.RETRY_EXHAUSTED,
        host_key_attempts=4,
    )
    second = diagnostic_code_for_exception(RuntimeError("not serialized"), version="0.1.3")

    assert main(["--json", first, second]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["code"] for item in payload] == [first, second]
    assert payload[1]["detail"] == DiagnosticDetail.RUNTIME_ERROR
